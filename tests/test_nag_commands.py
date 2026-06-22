"""U4 reactive commands: synchronous nag close + akrasia cap + body-double.

Invariant focus (NAG-CLOSES-ONLY-ON-ACK):

* /done and /reschedule close the nag loop SYNCHRONOUSLY in the same turn -- the
  next nag-check fire then skips, so there is no 3h ack-lag.
* A failed /done (task not on board) does NOT close the loop.
* /snooze pauses but never closes; the 4th snooze is REFUSED (loop unchanged).
* /body-double refuses a non-active task and a second concurrent session; its
  check-in crons carry an explicit proven delivery.to + agentId + deleteAfterRun.

Fake chat ids: valid chat-id shape but NOT matching the public-hygiene grep.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import utils  # noqa: E402
import cron_backend  # noqa: E402
import nag_state  # noqa: E402
import nag_commands  # noqa: E402
import nag_check  # noqa: E402
from datetime import datetime, timezone  # noqa: E402

PRODUCTIVITY = "-4242424242"
WORK_GROUP = "-5252525252"
REF = datetime(2026, 6, 19, tzinfo=timezone.utc)


def _fake_sender(record):
    """H3 deliver_once-shaped fake sender: records (target, text), returns a canned
    receipt. Never calls real openclaw."""
    def _send(target, text):
        record.append((target, text))
        return {"message_id": "-4242424242"}
    return _send

BOARD = """# Work

## 🟡 Q2
- [ ] **Re-evaluate ActiveCampaign** task_id::tsk_abc123 🗓️2026-06-15 area:: Marketing
"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    board = tmp_path / "Work Tasks.md"
    board.write_text(BOARD, encoding="utf-8")
    state = tmp_path / "state"
    monkeypatch.setenv("TASK_TRACKER_WORK_FILE", str(board))
    monkeypatch.setenv("TASK_TRACKER_LEDGER_FILE", str(state / "events.jsonl"))
    monkeypatch.setenv("TASK_TRACKER_DAILY_NOTES_DIR", str(tmp_path / "daily"))
    monkeypatch.setenv("TASK_TRACKER_DONE_LOG_DIR", str(tmp_path / "daily"))
    monkeypatch.setenv("TASK_MGMT_STATE_DIR", str(state))
    monkeypatch.setenv("TELEGRAM_CHAT_ID_PRODUCTIVITY", PRODUCTIVITY)
    monkeypatch.setenv("TELEGRAM_CHAT_ID_WORK", WORK_GROUP)
    monkeypatch.setenv("OPENCLAW_TOPIC_PRODUCTIVITY_STANDUP", "2")
    monkeypatch.setattr(utils, "OBSIDIAN_WORK", Path(board))
    monkeypatch.setattr(nag_check, "_today", lambda: REF)
    return board, state


def _open_loop(state, task_id="tsk_abc123"):
    """Open a nag loop directly (as a prior nag-check fire would).

    A real fire OPENS then SENDS in the same locked transition, so the loop the
    user sees has nag_count>=1 (a genuine, fired nag). record_sent mirrors that so
    /snooze's genuine-loop guard recognises it.
    """
    target = {"chat_id": PRODUCTIVITY, "topic_id": "2",
              "agent_id": "niemand-work", "channel": "telegram"}

    def fire(s):
        nag_state.open_loop(s, task_id, task_title="t", threshold_crossed=4,
                            threshold_type="q2", delivery_target=target)
        nag_state.record_sent(s, task_id)

    nag_state.transition(fire)


def _state(state):
    path = state / "nag-state.json"
    return json.loads(path.read_text()) if path.exists() else {}


# --- /done closes synchronously, next nag-check skips -----------------------

def test_done_closes_loop_synchronously_same_turn(env):
    board, state = env
    _open_loop(state)
    result = nag_commands.handle_done("tsk_abc123")
    assert result["ok"] is True
    assert result["nag_closed"] is True
    nag = _state(state)["tsk_abc123"]
    assert nag["ack"] is True
    assert nag["closed_by"] == "explicit_done"
    # Same-turn close means the NEXT nag-check fire skips this task (no push).
    sent = []
    nag_check.run_nag_check(sender=_fake_sender(sent))
    assert sent == []


def test_recurring_done_clears_loop_so_next_recurrence_nags(env, monkeypatch):
    """A recurring task keeps its canonical_id and rolls forward; acking the loop
    would terminally mute every future recurrence (the cron skips acked entries).
    So /done CLEARS the loop -- and the next overdue crossing opens a fresh one."""
    board, state = env
    board.write_text(
        "# Work\n\n## 🟡 Q2\n"
        "- [ ] **Weekly report** task_id::tsk_rec 🗓️2026-06-15 recur::weekly area:: Ops\n",
        encoding="utf-8")
    _open_loop(state, task_id="tsk_rec")
    result = nag_commands.handle_done("tsk_rec")
    assert result["ok"] is True and result["recurring"] is True
    # The loop entry is CLEARED (no lingering acked entry), so the rolled-forward
    # recurrence can open a clean fresh loop when it next goes overdue.
    assert "tsk_rec" not in _state(state) or _state(state)["tsk_rec"].get("ack") is not True
    # Drive the clock past the new due date: a fresh nag opens (not muted).
    monkeypatch.setattr(nag_check, "_today",
                        lambda: datetime(2026, 7, 1, tzinfo=timezone.utc))
    sent = []
    nag_check.run_nag_check(sender=_fake_sender(sent))
    assert len(sent) == 1  # re-nags the next recurrence; never permanently muted


def test_recycle_paths_log_nag_acked_with_recycled_metadata(env, monkeypatch):
    """A recycle (reschedule / recurring done) logs nag_acked with recycled:true so
    the audit trail distinguishes a reset loop from a terminal ack."""
    import task_ledger
    board, state = env
    _open_loop(state)
    monkeypatch.setattr(nag_commands, "_now", lambda: REF)
    nag_commands.handle_reschedule("tsk_abc123", "2026-06-30")
    acked = [e for e in task_ledger.read_events(state / "events.jsonl")
             if e["event_type"] == "nag_acked"]
    assert acked and acked[-1]["metadata"]["recycled"] is True
    assert acked[-1]["metadata"]["closed_by"] == "rescheduled"


def test_failed_done_does_not_close_loop(env):
    """NAG-CLOSES-ONLY-ON-ACK: a /done that does not complete the task (not on the
    board) must NOT clear the nag -- the task is still open."""
    board, state = env
    _open_loop(state, task_id="tsk_ghost")  # loop exists but task is NOT on board
    result = nag_commands.handle_done("tsk_ghost")
    assert result["ok"] is False  # complete_by_id refuses (no such active task)
    assert _state(state)["tsk_ghost"]["ack"] is False  # loop stays OPEN


# --- /reschedule closes synchronously --------------------------------------

def test_reschedule_to_future_moves_due_and_recycles_loop(env, monkeypatch):
    """A future reschedule moves the board and RECYCLES the loop (clears it, never
    acks). A future date is no longer overdue, so the next nag-check sends nothing;
    but if the new date later lapses, a fresh loop nags (no permanent mute)."""
    board, state = env
    _open_loop(state)
    monkeypatch.setattr(nag_commands, "_now", lambda: REF)
    result = nag_commands.handle_reschedule("tsk_abc123", "2026-06-30")
    assert result["ok"] is True
    assert result["new_due"] == "2026-06-30"
    assert result["nag_closed"] is False and result["nag_recycled"] is True
    assert "2026-06-30" in board.read_text()  # board moved
    # No lingering acked entry that would mute a future lapse.
    assert "tsk_abc123" not in _state(state) or _state(state)["tsk_abc123"].get("ack") is not True
    # The future date is not overdue at REF -> nag-check is quiet for now.
    sent = []
    nag_check.run_nag_check(sender=_fake_sender(sent))
    assert sent == []
    # When the new date later lapses, a fresh loop nags (no permanent mute).
    monkeypatch.setattr(nag_check, "_today",
                        lambda: datetime(2026, 7, 10, tzinfo=timezone.utc))
    sent2 = []
    nag_check.run_nag_check(sender=_fake_sender(sent2))
    assert len(sent2) == 1


def test_reschedule_to_still_overdue_date_recycles_loop_for_renag(env, monkeypatch):
    """T10: rescheduling to a date that is STILL overdue is deliberate -- the loop
    is cleared (not acked-terminal) so the next nag-check opens a FRESH loop."""
    board, state = env
    _open_loop(state)
    monkeypatch.setattr(nag_commands, "_now", lambda: REF)  # today = 2026-06-19
    # 2026-06-14 is 5 days overdue at REF -- past the q2 threshold (3 days).
    result = nag_commands.handle_reschedule("tsk_abc123", "2026-06-14")
    assert result["ok"] is True
    assert result["nag_closed"] is False  # NOT closed -- it must re-nag
    assert result["nag_recycled"] is True
    # The cron then opens a fresh loop (the task is still overdue past threshold).
    sent = []
    nag_check.run_nag_check(sender=_fake_sender(sent))
    assert len(sent) == 1


def test_reschedule_rejects_bad_date(env):
    board, state = env
    _open_loop(state)
    result = nag_commands.handle_reschedule("tsk_abc123", "not-a-date")
    assert result["ok"] is False
    assert result["error"]["code"] == "invalid-due-date"
    assert _state(state)["tsk_abc123"]["ack"] is False  # loop untouched


# --- /snooze: pause not close; akrasia cap of 3 ----------------------------

def test_snooze_pauses_but_does_not_close(env, monkeypatch):
    board, state = env
    _open_loop(state)
    monkeypatch.setattr(nag_commands, "_now", lambda: REF)  # snooze from REF
    result = nag_commands.handle_snooze("tsk_abc123", "1d")
    assert result["ok"] is True
    assert result["snooze_count"] == 1
    nag = _state(state)["tsk_abc123"]
    assert nag["ack"] is False  # snooze != close
    assert nag["snoozed_until"] is not None
    # Within the snooze window the next nag-check sends nothing.
    sent = []
    nag_check.run_nag_check(sender=_fake_sender(sent))
    assert sent == []


def test_fourth_snooze_is_refused_loop_unchanged(env):
    """T7 akrasia cap: the 4th /snooze is refused; state + count unchanged."""
    board, state = env
    _open_loop(state)
    for _ in range(3):
        assert nag_commands.handle_snooze("tsk_abc123", "1d")["ok"] is True
    before = _state(state)["tsk_abc123"]["snoozed_until"]
    result = nag_commands.handle_snooze("tsk_abc123", "1d")
    assert result["ok"] is False
    assert result["error"]["code"] == "snooze-cap-reached"
    after = _state(state)["tsk_abc123"]
    assert after["snooze_count"] == 3  # NOT incremented to 4
    assert after["snoozed_until"] == before  # unchanged


def test_snooze_refused_when_no_open_nag_loop(env):
    """A /snooze must PAUSE an existing fired nag, not materialise a phantom
    snoozed stub that pre-suppresses a future first nag."""
    board, state = env  # no loop opened for tsk_abc123
    result = nag_commands.handle_snooze("tsk_abc123", "1d")
    assert result["ok"] is False
    assert result["error"]["code"] == "no-open-nag"
    assert _state(state) == {}  # no phantom entry materialised


def test_snooze_rejects_invalid_duration(env):
    board, state = env
    _open_loop(state)
    result = nag_commands.handle_snooze("tsk_abc123", "garbage")
    assert result["ok"] is False
    assert result["error"]["code"] == "invalid-duration"


def test_t6_snooze_expires_then_nag_refires(env, monkeypatch):
    """T6 akrasia asymmetry: a snooze pauses the nag, but AFTER the window the loop
    re-fires (snooze_count is still 1 -- the loop never closed)."""
    board, state = env
    _open_loop(state)
    # Pin the snooze clock so snoozed_until is deterministic relative to the
    # nag-check clock advanced below (otherwise the 1d snooze is measured from
    # real wall-time and never expires against the fake re-fire clock).
    monkeypatch.setattr(nag_commands, "_now", lambda: REF)
    nag_commands.handle_snooze("tsk_abc123", "1d")  # snoozed until REF + 1d
    nag = _state(state)["tsk_abc123"]
    assert nag["snooze_count"] == 1 and nag["ack"] is False
    # Advance the nag-check clock past the snooze window (REF + 2 days).
    monkeypatch.setattr(nag_check, "_today",
                        lambda: datetime(2026, 6, 21, tzinfo=timezone.utc))
    sent = []
    nag_check.run_nag_check(sender=_fake_sender(sent))
    assert len(sent) == 1  # re-fires after expiry
    assert _state(state)["tsk_abc123"]["snooze_count"] == 1  # still 1; loop re-open


def test_long_snooze_adds_akrasia_visibility_note(env):
    board, state = env
    _open_loop(state)
    result = nag_commands.handle_snooze("tsk_abc123", "5d")
    assert result["ok"] is True
    assert "hides this until then" in result["reprompt"]


def test_parse_duration_minutes_supports_days_hours_minutes():
    assert nag_commands.parse_duration_minutes("1d") == 1440
    assert nag_commands.parse_duration_minutes("3d") == 4320
    assert nag_commands.parse_duration_minutes("1h") == 60
    assert nag_commands.parse_duration_minutes("90m") == 90
    assert nag_commands.parse_duration_minutes("garbage") == 0
    assert nag_commands.parse_duration_minutes("") == 0


# --- /body-double ----------------------------------------------------------

def test_body_double_creates_two_ephemeral_crons_with_explicit_target(env):
    board, state = env
    created = []
    result = nag_commands.handle_body_double(
        "tsk_abc123", "90m", create_cron=lambda d: created.append(d) or f"cron_{len(created)}")
    assert result["ok"] is True
    assert len(created) == 2
    for descriptor in created:
        # Explicit proven delivery.to + agentId + ephemeral deleteAfterRun.
        assert descriptor["delivery"]["to"] == f"{PRODUCTIVITY}:topic:2"
        assert descriptor["agentId"] == "niemand-work"
        assert descriptor["deleteAfterRun"] is True
        assert descriptor["schedule"]["kind"] == "at"
    session = nag_state.active_body_double_session(_state(state)["tsk_abc123"])
    assert session is not None and len(session["cron_ids"]) == 2


def test_body_double_refuses_inactive_task(env):
    board, state = env
    result = nag_commands.handle_body_double("tsk_ghost", "90m")
    assert result["ok"] is False
    assert result["error"]["code"] == "task-not-active"


def test_body_double_refuses_second_concurrent_session(env):
    board, state = env
    nag_commands.handle_body_double("tsk_abc123", "90m", create_cron=lambda d: "c")
    result = nag_commands.handle_body_double("tsk_abc123", "60m", create_cron=lambda d: "c")
    assert result["ok"] is False
    assert result["error"]["code"] == "session-already-active"


def test_body_double_second_session_under_lock_rolls_back_crons(env, monkeypatch):
    """If a session is inserted AFTER the early pre-check but BEFORE the locked
    append (the TOCTOU window), the under-lock guard rejects and the just-created
    crons are rolled back -- no orphaned cron pair, no second session."""
    board, state = env
    deleted = []
    monkeypatch.setattr(cron_backend, "delete_cron", lambda cid: deleted.append(cid))

    real_transition = nag_state.transition
    inserted = {"done": False}

    def racing_transition(mutator):
        # Simulate a concurrent /body-double landing a session right before THIS
        # call's locked append (only for the body-double add, once).
        if not inserted["done"]:
            inserted["done"] = True
            other = {"session_id": "bd_other", "cron_ids": ["c_other"],
                     "started_at": "x", "ended_at": None}
            real_transition(lambda s: nag_state.add_body_double_session(s, "tsk_abc123", other))
        return real_transition(mutator)

    monkeypatch.setattr(nag_state, "transition", racing_transition)
    result = nag_commands.handle_body_double("tsk_abc123", "90m",
                                             create_cron=lambda d: "c_mine")
    monkeypatch.setattr(nag_state, "transition", real_transition)
    assert result["ok"] is False
    assert result["error"]["code"] == "session-already-active"
    assert deleted == ["c_mine", "c_mine"]  # both of MY crons rolled back
    # Only the other session survives.
    sessions = _state(state)["tsk_abc123"]["body_double_sessions"]
    assert [s["session_id"] for s in sessions] == ["bd_other"]


def test_body_double_blocks_when_delivery_unproven(env, monkeypatch):
    """A body-double whose check-in target cannot be proven must NOT start (no
    headless session whose check-ins would have nowhere to deliver)."""
    board, state = env
    monkeypatch.delenv("TELEGRAM_CHAT_ID_PRODUCTIVITY", raising=False)
    created = []
    result = nag_commands.handle_body_double(
        "tsk_abc123", "90m", create_cron=lambda d: created.append(d) or "c")
    assert result["ok"] is False
    assert result["error"]["code"] == "delivery-target-unproven"
    assert created == []  # no crons created


def test_cancel_session_ends_and_deletes_crons(env):
    board, state = env
    nag_commands.handle_body_double("tsk_abc123", "90m",
                                    create_cron=lambda d: "cron_x")
    deleted = []
    result = nag_commands.handle_cancel_session(
        "tsk_abc123", delete_cron=lambda cid: deleted.append(cid))
    assert result["ok"] is True
    assert result["outcome"] == "cancelled"
    assert deleted == ["cron_x", "cron_x"]
    # The session is now ended -- no active session remains.
    assert nag_state.active_body_double_session(_state(state)["tsk_abc123"]) is None


def test_cancel_session_with_no_session_is_refused(env):
    board, state = env
    result = nag_commands.handle_cancel_session("tsk_abc123")
    assert result["ok"] is False
    assert result["error"]["code"] == "no-active-session"
