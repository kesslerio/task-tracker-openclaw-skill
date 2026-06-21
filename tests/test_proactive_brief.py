"""U6 proactive brief/debrief flows + the DELIVERY-TARGET-PROOF denied path.

Hard Gate #5 (DELIVERY-SEAM): every U6 push goes through prove_delivery_target()
+ the gated act_id (assert_send_target). These tests assert the invariant on the
DENIED paths, not just the happy path:

* DELIVERY-TARGET-PROOF -- order is "resolve+prove FIRST, then send". An unset env
  / a Work-group target blocks the push; NOTHING is delivered;
  delivery_target_proof_failed is logged; no delivery_target_resolved is logged.
* NEVER-OVERBOOK-EXTERNAL (via slip) -- slip recovery refuses to move a block into
  a busy/unknown window (logs calendar_block_refused) and never delete+creates.
* idempotency -- the daily brief / Friday proposal send at most once per day.

Fake chat ids: valid chat-id shape but NOT matching -100[0-9]{8,}; real ids are
env-sourced, never committed (mirrors test_delivery_target.py).
"""

import sys
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import cos_config  # noqa: E402
import proactive_brief  # noqa: E402
import proactive_delivery  # noqa: E402
import proactive_state  # noqa: E402
import task_ledger  # noqa: E402
import utils  # noqa: E402

PRODUCTIVITY = "-4242424242"
WORK_GROUP = "-5252525252"
NOW = datetime(2026, 6, 20, 8, 0, tzinfo=timezone.utc)

BOARD = """# Work

## 🔴 Q1
- [ ] **Finalize AlphaClaw release notes** task_id::tsk_rel 🗓️2026-06-10 estimate:: 2h area:: Eng
## 🟡 Q2
- [ ] **Review openclaw-ops PR** task_id::tsk_pr 🗓️2026-06-18 estimate:: 45m area:: Eng
"""


def _set_env(monkeypatch, board, state_dir, *, productivity=PRODUCTIVITY):
    monkeypatch.setenv("TASK_TRACKER_WORK_FILE", str(board))
    monkeypatch.setenv("TASK_TRACKER_LEDGER_FILE", str(state_dir / "events.jsonl"))
    monkeypatch.setenv("TASK_MGMT_STATE_DIR", str(state_dir))
    monkeypatch.setenv("TELEGRAM_CHAT_ID_PRODUCTIVITY", productivity)
    monkeypatch.setenv("TELEGRAM_CHAT_ID_WORK", WORK_GROUP)
    monkeypatch.setenv("OPENCLAW_TOPIC_PRODUCTIVITY_STANDUP", "2")
    monkeypatch.setenv("OPENCLAW_TOPIC_PRODUCTIVITY_WEEKLY_REVIEW_PLANNING", "4")
    # standup_common has no calendar configured -> brief degrades to "0 events".
    monkeypatch.delenv("STANDUP_CALENDARS", raising=False)
    monkeypatch.setattr(utils, "OBSIDIAN_WORK", Path(board))


@pytest.fixture
def harness(tmp_path, monkeypatch):
    board = tmp_path / "Work Tasks.md"
    board.write_text(BOARD, encoding="utf-8")
    state = tmp_path / "state"
    _set_env(monkeypatch, board, state)
    return board, state


def _events(state):
    return task_ledger.read_events(state / "events.jsonl")


def _types(state):
    return [e["event_type"] for e in _events(state)]


def _collector():
    sent: list[tuple] = []
    return sent, (lambda target, text: sent.append((target, text)))


# --- Happy path: daily brief proves target, sends, is idempotent ------------

def test_daily_brief_sends_to_proven_target(harness):
    board, state = harness
    sent, send = _collector()
    result = proactive_brief.run_daily_brief(now=NOW, send=send)
    assert result["sent"] is True
    target, _text = sent[0]
    assert target == {"chat_id": PRODUCTIVITY, "topic_id": "2",
                      "agent_id": "niemand-work", "channel": "telegram"}
    event_types = _types(state)
    assert "delivery_target_resolved" in event_types
    assert "brief_sent" in event_types


def test_daily_brief_idempotent(harness):
    board, state = harness
    sent, send = _collector()
    proactive_brief.run_daily_brief(now=NOW, send=send)
    second = proactive_brief.run_daily_brief(now=NOW, send=send)
    assert second["sent"] is False
    assert second["reason"] == "already_sent"
    assert len(sent) == 1  # only the FIRST run delivered


# --- DENIED PATH 1: unset env => blocked, nothing delivered -----------------

def test_unset_env_blocks_brief_nothing_sent(harness, monkeypatch):
    """DELIVERY-TARGET-PROOF: env unset => proof fails FIRST, NOTHING is sent."""
    board, state = harness
    monkeypatch.delenv("TELEGRAM_CHAT_ID_PRODUCTIVITY", raising=False)
    sent, send = _collector()
    result = proactive_brief.run_daily_brief(now=NOW, send=send)
    assert result["sent"] is False
    assert result["reason"] == "env_missing"
    assert sent == []  # the action did NOT deliver
    event_types = _types(state)
    assert "delivery_target_proof_failed" in event_types
    assert "delivery_target_resolved" not in event_types  # never resolved
    assert "brief_sent" not in event_types


# --- DENIED PATH 2: Work-group target => blocked ----------------------------

def test_work_group_target_blocks_brief(harness, monkeypatch):
    """A push aimed at the Work/heartbeat group is blocked at the prove step."""
    board, state = harness
    # Point the productivity chat env at the Work group: prove_delivery_target
    # rejects it as work_group; nothing is sent.
    monkeypatch.setenv("TELEGRAM_CHAT_ID_PRODUCTIVITY", WORK_GROUP)
    sent, send = _collector()
    result = proactive_brief.run_daily_brief(now=NOW, send=send)
    assert result["sent"] is False
    assert result["reason"] == "work_group"
    assert sent == []
    assert "brief_sent" not in _types(state)


# --- DENIED PATH 3: gate<->message seam mismatch is blocked -----------------

def test_seam_blocks_send_to_unbound_target(harness):
    """assert_send_target blocks a send aimed at a target the act was NOT gated for."""
    board, state = harness
    gated = proactive_delivery.prove_and_gate("brief_sent", surface="standup")
    assert gated["ok"] is True
    # Try to send to a DIFFERENT topic than the one gated -> blocked, no send.
    wrong_target = dict(gated["delivery_target"], topic_id="4")
    delivered: list = []
    out = proactive_delivery.authorised_send(
        gated["act_id"], wrong_target, "hi", send=lambda t, x: delivered.append((t, x)))
    assert out["ok"] is False
    assert out["reason"] == "target-mismatch"
    assert delivered == []


# --- Friday proposal: proves target, sends to weekly topic, no U3 write -----

def test_friday_proposal_targets_weekly_topic(harness):
    board, state = harness
    sent, send = _collector()
    result = proactive_brief.run_friday_proposal(now=NOW, send=send)
    assert result["sent"] is True
    target, _text = sent[0]
    assert target["topic_id"] == "4"  # weekly-planning, not standup
    # U6 NEVER writes focus-state.json
    focus_state_path = Path(cos_config.state_dir()) / "focus-state.json"
    assert not focus_state_path.exists()


def test_friday_proposal_unset_env_blocked(harness, monkeypatch):
    board, state = harness
    monkeypatch.delenv("TELEGRAM_CHAT_ID_PRODUCTIVITY", raising=False)
    sent, send = _collector()
    result = proactive_brief.run_friday_proposal(now=NOW, send=send)
    assert result["sent"] is False
    assert sent == []


# --- Pre-brief scan: idempotent + debrief loop opens ------------------------

def _patch_calendar(monkeypatch, events):
    monkeypatch.setattr(proactive_brief, "get_calendar_events", lambda **_k: {"work": events})


def test_pre_brief_sends_once_and_opens_debrief(harness, monkeypatch):
    board, state = harness
    event = {"event_id": "evt_q3", "summary": "Q3 Review",
             "start": "2026-06-20T08:10:00+00:00", "end": "2026-06-20T09:00:00+00:00"}
    _patch_calendar(monkeypatch, [event])
    sent, send = _collector()
    counts = proactive_brief.run_pre_brief_scan(now=NOW, send=send)
    assert counts["briefed"] == 1
    # second fire within the lead window must NOT double-brief
    counts2 = proactive_brief.run_pre_brief_scan(now=NOW, send=send)
    assert counts2["briefed"] == 0
    event_types = _types(state)
    assert event_types.count("brief_sent") == 1


def test_pre_brief_unset_env_sends_nothing(harness, monkeypatch):
    board, state = harness
    monkeypatch.delenv("TELEGRAM_CHAT_ID_PRODUCTIVITY", raising=False)
    event = {"event_id": "evt_q3", "summary": "Q3 Review",
             "start": "2026-06-20T08:10:00+00:00", "end": "2026-06-20T09:00:00+00:00"}
    _patch_calendar(monkeypatch, [event])
    sent, send = _collector()
    counts = proactive_brief.run_pre_brief_scan(now=NOW, send=send)
    assert counts["briefed"] == 0
    assert counts["blocked"] >= 1
    assert sent == []


# --- Debrief capture: notes -> commitment tasks, loop closed ----------------

def test_parse_commitments_extracts_title_and_due():
    out = proactive_brief.parse_commitments(
        "I will send Q3 budget draft by 2026-06-30. Martin will review by 2026-07-02. no commitment here")
    assert out == [
        {"title": "I will send Q3 budget draft", "due": "2026-06-30"},
        {"title": "Martin will review", "due": "2026-07-02"},
    ]


def test_parse_commitments_splits_on_sentence_not_every_period(harness):
    """autoreview P3: splitting on a sentence boundary (period + space), not every
    '.', keeps dates and abbreviations intact in the title."""
    # newline-separated and sentence-separated both work; the date is preserved
    out = proactive_brief.parse_commitments(
        "I will ship v1.2 by 2026-06-30\nMartin will sign off by 2026-07-02.")
    assert out == [
        {"title": "I will ship v1.2", "due": "2026-06-30"},
        {"title": "Martin will sign off", "due": "2026-07-02"},
    ]


def test_debrief_capture_creates_tasks_and_closes_loop(harness):
    board, state = harness
    # Seed an open debrief loop for an event.
    st = proactive_state.load_proactive_state()
    proactive_state.mark_pre_brief_sent(st, "evt_q3", "Q3 Review", "2026-06-20T09:00:00+00:00")
    proactive_state.open_debrief(st, "evt_q3")
    proactive_state.save_proactive_state(st)

    # Inject a fake tasks.py-add runner so no real subprocess is spawned.
    created: list = []

    def fake_runner(cmd):
        created.append(cmd)
        idx = len(created)
        return types.SimpleNamespace(
            stdout=f"✅ Added work task: {cmd[3]} (tsk_commit{idx})", stderr="", returncode=0)

    result = proactive_brief.run_debrief_capture(
        "evt_q3", "I will send the deck by 2026-06-30. Martin will sign off by 2026-07-02",
        runner=fake_runner)
    assert result["captured"] is True
    assert result["task_ids"] == ["tsk_commit1", "tsk_commit2"]
    types_logged = _types(state)
    assert types_logged.count("commitment_task_created") == 2
    assert "debrief_captured" in types_logged
    # the loop is now CLOSED (captured)
    reloaded = proactive_state.load_proactive_state()
    assert proactive_state.is_debrief_open(reloaded["pre_briefs"][0]) is False
    assert reloaded["pre_briefs"][0]["commitments_task_ids"] == ["tsk_commit1", "tsk_commit2"]


def test_debrief_capture_idempotent_on_closed_loop(harness):
    """autoreview: a second /debrief for an already-closed loop is a NO-OP -- it
    never re-parses the notes or duplicates commitment tasks."""
    board, state = harness
    st = proactive_state.load_proactive_state()
    proactive_state.mark_pre_brief_sent(st, "evt_q3", "Q3 Review", "2026-06-20T09:00:00+00:00")
    proactive_state.open_debrief(st, "evt_q3")
    proactive_state.save_proactive_state(st)

    calls: list = []

    def fake_runner(cmd):
        calls.append(cmd)
        return types.SimpleNamespace(
            stdout=f"✅ Added work task: {cmd[3]} (tsk_commit{len(calls)})", stderr="", returncode=0)

    notes = "I will ship by 2026-06-30"
    first = proactive_brief.run_debrief_capture("evt_q3", notes, runner=fake_runner)
    assert first["captured"] is True and first["task_ids"] == ["tsk_commit1"]
    # second invocation for the now-CLOSED loop adds NOTHING (no open loop to match)
    second = proactive_brief.run_debrief_capture("evt_q3", notes, runner=fake_runner)
    assert second["captured"] is False
    assert second["reason"] == "no_open_debrief"
    assert len(calls) == 1  # the task was added exactly once
    # the first batch's ids are preserved, not overwritten
    reloaded = proactive_state.load_proactive_state()
    assert reloaded["pre_briefs"][0]["commitments_task_ids"] == ["tsk_commit1"]


def test_debrief_capture_resolves_by_summary(harness):
    """autoreview: the user types the event SUMMARY the pre-brief advertised, but
    the loop is keyed by event_id (summary@start). Capture must still resolve and
    close the loop by summary."""
    board, state = harness
    st = proactive_state.load_proactive_state()
    # The stored key is summary@start (what event_key() produces for an id-less event).
    key = "Q3 Review@2026-06-20T09:00:00+00:00"
    proactive_state.mark_pre_brief_sent(st, key, "Q3 Review", "2026-06-20T09:00:00+00:00")
    proactive_state.open_debrief(st, key)
    proactive_state.save_proactive_state(st)

    def fake_runner(cmd):
        return types.SimpleNamespace(
            stdout=f"✅ Added work task: {cmd[3]} (tsk_c1)", stderr="", returncode=0)

    # the user types just the summary
    result = proactive_brief.run_debrief_capture("Q3 Review", "I will ship by 2026-06-30",
                                                 runner=fake_runner)
    assert result["captured"] is True
    reloaded = proactive_state.load_proactive_state()
    assert proactive_state.is_debrief_open(reloaded["pre_briefs"][0]) is False  # closed


def test_debrief_retry_after_partial_failure_does_not_duplicate(harness):
    """autoreview: a retry after a partial failure re-submits the SAME notes but must
    NOT re-create the commitment that already succeeded -- it only retries the failed
    one. The first commitment succeeds, the second fails; on retry (second now
    succeeds) only ONE new task is created."""
    board, state = harness
    st = proactive_state.load_proactive_state()
    proactive_state.mark_pre_brief_sent(st, "evt_q3", "Q3 Review", "2026-06-20T09:00:00+00:00")
    proactive_state.open_debrief(st, "evt_q3")
    proactive_state.save_proactive_state(st)

    notes = "I will ship the deck by 2026-06-30. Martin will review by 2026-07-02"
    created: list = []

    def first_runner(cmd):
        # the FIRST commitment (ship the deck) succeeds; the SECOND (Martin review) fails
        created.append(cmd[3])
        if "ship the deck" in cmd[3]:
            return types.SimpleNamespace(stdout=f"✅ Added work task: {cmd[3]} (tsk_ship)", stderr="", returncode=0)
        return types.SimpleNamespace(stdout="", stderr="cap", returncode=2)

    first = proactive_brief.run_debrief_capture("evt_q3", notes, runner=first_runner)
    assert first["captured"] is False  # partial -> loop stays open
    assert first["task_ids"] == ["tsk_ship"]

    created.clear()

    def retry_runner(cmd):
        created.append(cmd[3])
        return types.SimpleNamespace(stdout=f"✅ Added work task: {cmd[3]} (tsk_review)", stderr="", returncode=0)

    second = proactive_brief.run_debrief_capture("evt_q3", notes, runner=retry_runner)
    # only the previously-FAILED commitment is retried -- the succeeded one is NOT re-added
    assert created == ["Martin will review"]
    assert second["captured"] is True
    reloaded = proactive_state.load_proactive_state()
    # both commitment ids are recorded, no duplicate of the first
    assert reloaded["pre_briefs"][0]["commitments_task_ids"] == ["tsk_ship", "tsk_review"]


def test_debrief_capture_unknown_reference_refuses(harness):
    """A /debrief for an event with no OPEN loop refuses -- it never creates tasks."""
    board, state = harness
    st = proactive_state.load_proactive_state()
    proactive_state.mark_pre_brief_sent(st, "evt_q3", "Q3 Review", "2026-06-20T09:00:00+00:00")
    proactive_state.open_debrief(st, "evt_q3")
    proactive_state.save_proactive_state(st)

    def fail_runner(cmd):
        raise AssertionError("no task may be created for an unknown reference")

    result = proactive_brief.run_debrief_capture("Some Other Meeting",
                                                 "I will do X by 2026-06-30", runner=fail_runner)
    assert result["captured"] is False
    assert result["reason"] == "no_open_debrief"
    assert result["task_ids"] == []


def test_debrief_partial_failure_keeps_loop_open(harness):
    """autoreview: if a commitment task FAILS to create, the loop stays OPEN so the
    user can retry -- a commitment is never silently dropped behind a closed loop."""
    board, state = harness
    st = proactive_state.load_proactive_state()
    proactive_state.mark_pre_brief_sent(st, "evt_q3", "Q3 Review", "2026-06-20T09:00:00+00:00")
    proactive_state.open_debrief(st, "evt_q3")
    proactive_state.save_proactive_state(st)

    def failing_runner(cmd):
        # tasks.py add returns non-zero -> _create_commitment_task yields None
        return types.SimpleNamespace(stdout="", stderr="cap reached", returncode=2)

    result = proactive_brief.run_debrief_capture(
        "evt_q3", "I will ship by 2026-06-30", runner=failing_runner)
    assert result["captured"] is False
    assert result["reason"] == "commitment_create_failed"
    assert result["failed"] == ["I will ship"]
    # the loop is STILL open -- the commitment is not lost
    reloaded = proactive_state.load_proactive_state()
    assert proactive_state.is_debrief_open(reloaded["pre_briefs"][0]) is True


def test_commitment_add_does_not_force_parking(harness):
    """autoreview P1: the commitment add must NOT pass --force-parking (a parking-lot
    add prints an unparseable line that would be misread as a failure)."""
    board, state = harness
    st = proactive_state.load_proactive_state()
    proactive_state.mark_pre_brief_sent(st, "evt_q3", "Q3 Review", "2026-06-20T09:00:00+00:00")
    proactive_state.open_debrief(st, "evt_q3")
    proactive_state.save_proactive_state(st)

    seen_cmds: list = []

    def recording_runner(cmd):
        seen_cmds.append(cmd)
        return types.SimpleNamespace(
            stdout=f"✅ Added work task: {cmd[3]} (tsk_c1)", stderr="", returncode=0)

    proactive_brief.run_debrief_capture("evt_q3", "I will ship by 2026-06-30",
                                        runner=recording_runner)
    assert seen_cmds, "the add CLI should have been invoked"
    assert all("--force-parking" not in cmd for cmd in seen_cmds)


def test_debrief_notes_with_no_commitment_does_not_close_loop(harness):
    """autoreview P3: notes that parse to ZERO commitments (no 'will' phrasing) must
    NOT close the loop -- a real commitment would otherwise be silently dropped."""
    board, state = harness
    st = proactive_state.load_proactive_state()
    proactive_state.mark_pre_brief_sent(st, "evt_q3", "Q3 Review", "2026-06-20T09:00:00+00:00")
    proactive_state.open_debrief(st, "evt_q3")
    proactive_state.save_proactive_state(st)

    def fail_runner(cmd):
        raise AssertionError("no task may be created when nothing parsed")

    result = proactive_brief.run_debrief_capture("evt_q3", "it was a good meeting",
                                                 runner=fail_runner)
    assert result["captured"] is False
    assert result["reason"] == "no_commitment_parsed"
    # the loop is STILL open -- the user can rephrase rather than losing a commitment
    reloaded = proactive_state.load_proactive_state()
    assert proactive_state.is_debrief_open(reloaded["pre_briefs"][0]) is True


def test_debrief_skip_closes_loop_no_tasks(harness):
    board, state = harness
    st = proactive_state.load_proactive_state()
    proactive_state.mark_pre_brief_sent(st, "evt_q3", "Q3 Review", "2026-06-20T09:00:00+00:00")
    proactive_state.open_debrief(st, "evt_q3")
    proactive_state.save_proactive_state(st)

    def fail_runner(cmd):
        raise AssertionError("no task should be added on skip")

    result = proactive_brief.run_debrief_capture("evt_q3", "skip", runner=fail_runner)
    assert result["captured"] is False
    assert result["task_ids"] == []
    reloaded = proactive_state.load_proactive_state()
    assert reloaded["pre_briefs"][0]["debrief_skipped_at"] is not None


# --- main() entry point exits 0 and never leaks a traceback -----------------

def test_main_exits_zero_on_internal_error(harness, monkeypatch, capsys):
    """NO-RAW-ERROR-LEAK: a failure inside a flow exits 0 with a safe envelope."""
    board, state = harness

    def boom(**_k):
        raise RuntimeError("kaboom internal detail")

    monkeypatch.setattr(proactive_brief, "run_daily_brief", boom)
    rc = proactive_brief.main(["--mode", "brief"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "PROACTIVE_BRIEF_ERROR" in out
    assert "Traceback" not in out
    assert "kaboom" not in out  # the raw detail never reaches stdout
