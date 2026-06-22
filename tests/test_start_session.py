"""H7 /start: the initiation loop (reuses the body-double + quiet machinery).

A task list surfaces tasks but doesn't help you START. ``/start`` is the
moment-of-friction helper: the next small action, a timer, distractions muted, a
resumption cue saved, and a structured choice at the end. It REUSES the existing
body-double focus-session + ephemeral check-in-cron machinery (DRY) and layers on
the resumption cue, the H5 quiet mute, and the end disposition.

Invariants pinned here:

* a cue is stored ON the session (survives a crash via nag-state.json): default
  ``Work on: <title>``; explicit ``next:`` text honoured;
* quiet is set for the session duration; the check-in crons carry an EXPLICIT
  proven ``delivery.to`` + ``agentId`` (Hard Gate #4, inherited from body-double);
* the end check-in text is the done/continue/blocked/redefine disposition;
* ``/start status`` (and the no-arg form) shows the active session's cue;
* ``/cancel-session`` ends the session AND clears THIS session's quiet -- but NEVER
  cuts a longer independent ``/quiet`` short (the quiet-guard);
* one focus/body-double session per task; the default-duration knob.

Fake chat ids only (valid shape, not matching the public-hygiene grep).
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import utils  # noqa: E402
import cos_config  # noqa: E402
import nag_commands  # noqa: E402
import nag_state  # noqa: E402
import quiet_state  # noqa: E402

PRODUCTIVITY = "-4242424242"

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
    monkeypatch.setenv("OPENCLAW_TOPIC_PRODUCTIVITY_STANDUP", "2")
    monkeypatch.setattr(utils, "OBSIDIAN_WORK", Path(board))
    return board, state


def _state(state):
    path = state / "nag-state.json"
    return json.loads(path.read_text()) if path.exists() else {}


def _session(state, task_id="tsk_abc123"):
    return nag_state.active_body_double_session(_state(state).get(task_id))


# --- /start creates a session with the cue, sets quiet, schedules check-ins ---

def test_start_stores_default_cue_sets_quiet_and_proves_delivery_target(env):
    """Default cue = "Work on: <title>"; quiet set for the duration; both check-in
    crons carry an explicit proven delivery target + ephemeral deleteAfterRun."""
    board, state = env
    created = []
    result = nag_commands.handle_start(
        "tsk_abc123", create_cron=lambda d: created.append(d) or f"cron_{len(created)}")
    assert result["ok"] is True
    # Default cue from the task title (no explicit next:).
    assert result["cue"] == "Work on: Re-evaluate ActiveCampaign"
    assert _session(state)["cue"] == "Work on: Re-evaluate ActiveCampaign"
    # Two ephemeral check-in crons, each with the explicit proven delivery target.
    assert len(created) == 2
    for descriptor in created:
        assert descriptor["delivery"]["to"] == f"{PRODUCTIVITY}:topic:2"
        assert descriptor["agentId"] == "niemand-work"
        assert descriptor["deleteAfterRun"] is True
        assert descriptor["schedule"]["kind"] == "at"
    # Quiet was set for the session window.
    assert result["quiet_set"] is True
    assert quiet_state.is_quiet(datetime.now(timezone.utc)) is True


def test_start_honours_explicit_next_cue(env):
    board, state = env
    result = nag_commands.handle_start(
        "tsk_abc123", "30m", "open the campaign editor and pick one list",
        create_cron=lambda d: "c")
    assert result["ok"] is True
    assert result["cue"] == "open the campaign editor and pick one list"
    assert _session(state)["cue"] == "open the campaign editor and pick one list"


def test_start_end_checkin_text_is_the_disposition(env):
    """The FINAL check-in cron's prompt is the structured done/continue/blocked/
    redefine disposition, directing to /done + /reschedule + continue + redefine."""
    board, state = env
    created = []
    nag_commands.handle_start("tsk_abc123", create_cron=lambda d: created.append(d) or "c")
    final_prompt = created[-1]["prompt"]
    assert "done -> /done tsk_abc123" in final_prompt
    assert "continue -> /start tsk_abc123" in final_prompt
    assert "blocked -> /reschedule tsk_abc123" in final_prompt
    assert "redefine -> just reply" in final_prompt
    # The halfway check-in is NOT the disposition (still the body-double marker).
    assert "BODY_DOUBLE_CHECKIN" in created[0]["prompt"]


def test_start_refuses_inactive_task(env):
    board, state = env
    result = nag_commands.handle_start("tsk_ghost", create_cron=lambda d: "c")
    assert result["ok"] is False
    assert result["error"]["code"] == "task-not-active"


def test_start_blocks_when_delivery_unproven(env, monkeypatch):
    """No headless focus session whose check-ins would have nowhere to deliver."""
    board, state = env
    monkeypatch.delenv("TELEGRAM_CHAT_ID_PRODUCTIVITY", raising=False)
    created = []
    result = nag_commands.handle_start(
        "tsk_abc123", create_cron=lambda d: created.append(d) or "c")
    assert result["ok"] is False
    assert result["error"]["code"] == "delivery-target-unproven"
    assert created == []  # no crons, no session, no quiet
    assert _session(state) is None
    assert quiet_state.is_quiet(datetime.now(timezone.utc)) is False


# --- one-session-per-task guard --------------------------------------------

def test_start_refuses_second_concurrent_session(env):
    board, state = env
    nag_commands.handle_start("tsk_abc123", create_cron=lambda d: "c")
    result = nag_commands.handle_start("tsk_abc123", create_cron=lambda d: "c")
    assert result["ok"] is False
    assert result["error"]["code"] == "session-already-active"


def test_start_and_body_double_share_the_one_session_guard(env):
    """A /start session blocks a /body-double on the same task (and vice versa) --
    they share active_body_double_session's one-per-task guard."""
    board, state = env
    nag_commands.handle_start("tsk_abc123", create_cron=lambda d: "c")
    bd = nag_commands.handle_body_double("tsk_abc123", "90m", create_cron=lambda d: "c")
    assert bd["ok"] is False
    assert bd["error"]["code"] == "session-already-active"


# --- elapsed-session auto-expire (P2): an ended block frees the task ---------

REF = datetime(2026, 6, 19, 9, 0, tzinfo=timezone.utc)


def test_start_after_block_elapsed_succeeds_so_continue_works(env, monkeypatch):
    """The advertised "continue -> /start <id>" must work after the block ends.

    A /start block ELAPSES (its final check-in cron fires + is reaped -- nothing
    marks the session ended). A second /start on the same task, with the clock
    advanced PAST the session's ends_at, AUTO-EXPIRES the stale session and SUCCEEDS
    -- the new session is created. Before the fix this was refused
    "session-already-active" until a manual /cancel-session."""
    board, state = env
    monkeypatch.setattr(nag_commands, "_now", lambda: REF)
    first = nag_commands.handle_start("tsk_abc123", "25m", create_cron=lambda d: "c")
    assert first["ok"] is True
    first_id = first["session_id"]
    # The block has fully elapsed (25 min < the +1h advance); the prior session is
    # still on disk, non-ended -- only the clock makes it elapsed.
    monkeypatch.setattr(nag_commands, "_now", lambda: REF + timedelta(hours=1))
    second = nag_commands.handle_start("tsk_abc123", "25m", create_cron=lambda d: "c")
    assert second["ok"] is True
    assert second["session_id"] != first_id  # a genuinely new session
    # Both session records are on disk (the elapsed one is lazily expired, not
    # deleted) -- the new one was appended past the auto-expired prior block.
    ids = [s["session_id"] for s in _state(state)["tsk_abc123"]["body_double_sessions"]]
    assert ids == [first_id, second["session_id"]]
    # Judged at the advanced clock, the new (not-yet-elapsed) session is the active
    # one and the prior block is expired.
    active = nag_state.active_body_double_session(
        _state(state)["tsk_abc123"], now=REF + timedelta(hours=1))
    assert active is not None and active["session_id"] == second["session_id"]


def test_start_while_block_still_active_refuses(env, monkeypatch):
    """The live one-per-task guard holds: a second /start while the first block is
    STILL within its window (clock inside the duration) is REFUSED."""
    board, state = env
    monkeypatch.setattr(nag_commands, "_now", lambda: REF)
    nag_commands.handle_start("tsk_abc123", "25m", create_cron=lambda d: "c")
    # Only 10 min in -- the block is still active, so the guard refuses.
    monkeypatch.setattr(nag_commands, "_now", lambda: REF + timedelta(minutes=10))
    second = nag_commands.handle_start("tsk_abc123", "25m", create_cron=lambda d: "c")
    assert second["ok"] is False
    assert second["error"]["code"] == "session-already-active"


def test_active_body_double_session_back_compat_no_ends_at(env):
    """Back-compat: a legacy session with NO ends_at is still active with no `now`
    (old rule: only ended_at ends it), and a passed `now` never auto-expires what
    it cannot date -- a missing/garbage ends_at is treated as still active (never
    crashes)."""
    legacy = {"session_id": "bd_legacy", "cron_ids": [],
              "started_at": "2026-06-19T09:00:00+00:00", "ended_at": None}
    entry = {"body_double_sessions": [legacy]}
    # No now -> back-compat rule, active.
    assert nag_state.active_body_double_session(entry) is legacy
    # A passed now does NOT auto-expire a session it cannot date.
    assert nag_state.active_body_double_session(entry, now=REF) is legacy
    # Garbage ends_at is treated as active (not crashed, not expired).
    garbage = {"session_id": "bd_garbage", "cron_ids": [],
               "started_at": "x", "ends_at": "not-a-date", "ended_at": None}
    entry2 = {"body_double_sessions": [garbage]}
    assert nag_state.active_body_double_session(entry2, now=REF) is garbage


def test_start_status_after_elapse_shows_no_active_session(env, monkeypatch):
    """/start status after the block elapsed reports "no active session" -- it must
    not advertise a stale Resume: cue for a block that already ended."""
    board, state = env
    monkeypatch.setattr(nag_commands, "_now", lambda: REF)
    nag_commands.handle_start("tsk_abc123", "25m", "draft the list",
                              create_cron=lambda d: "c")
    # While active, status shows the cue.
    assert nag_commands.handle_start_status()["active"] is True
    # After the block elapses, status reports no active session.
    monkeypatch.setattr(nag_commands, "_now", lambda: REF + timedelta(hours=1))
    status = nag_commands.handle_start_status()
    assert status["active"] is False
    assert "No active focus session" in status["message"]


# --- /start status / no-arg shows the active session's cue ------------------

def test_start_status_shows_active_session_cue(env):
    board, state = env
    nag_commands.handle_start("tsk_abc123", "30m", "draft the segment list",
                              create_cron=lambda d: "c")
    status = nag_commands.handle_start_status()
    assert status["ok"] is True and status["active"] is True
    assert status["task_id"] == "tsk_abc123"
    assert status["cue"] == "draft the segment list"
    assert "Resume: draft the segment list" in status["message"]


def test_start_status_with_no_active_session(env):
    board, state = env
    status = nag_commands.handle_start_status()
    assert status["ok"] is True and status["active"] is False
    assert "No active focus session" in status["message"]


# --- default-duration knob (default / override / floor) ---------------------

def test_start_default_duration_knob(env):
    """Omitted duration falls back to cos_config.start_session_minutes() (25)."""
    board, state = env
    result = nag_commands.handle_start("tsk_abc123", create_cron=lambda d: "c")
    assert result["duration_min"] == 25
    assert _session(state)["duration_min"] == 25


def test_start_duration_override(env):
    board, state = env
    result = nag_commands.handle_start("tsk_abc123", "45m", create_cron=lambda d: "c")
    assert result["duration_min"] == 45


def test_start_default_duration_env_override(env, monkeypatch):
    monkeypatch.setenv("START_SESSION_MINUTES", "50")
    board, state = env
    result = nag_commands.handle_start("tsk_abc123", create_cron=lambda d: "c")
    assert result["duration_min"] == 50


def test_start_session_minutes_floor_and_default():
    """The knob defaults to 25 and is floored at 1 (a 0/negative misconfig)."""
    import os
    os.environ.pop("START_SESSION_MINUTES", None)
    assert cos_config.start_session_minutes() == 25
    os.environ["START_SESSION_MINUTES"] = "0"
    try:
        assert cos_config.start_session_minutes() == 1  # floored
    finally:
        os.environ.pop("START_SESSION_MINUTES", None)


# --- /cancel-session ends the session AND clears THIS session's quiet -------

def test_cancel_session_ends_and_clears_session_quiet(env):
    board, state = env
    nag_commands.handle_start("tsk_abc123", create_cron=lambda d: "c")
    assert quiet_state.is_quiet(datetime.now(timezone.utc)) is True
    result = nag_commands.handle_cancel_session(
        "tsk_abc123", delete_cron=lambda cid: None)
    assert result["ok"] is True
    assert result["outcome"] == "cancelled"
    assert result["quiet_cleared"] is True
    # Session ended + quiet cleared.
    assert _session(state) is None
    assert quiet_state.is_quiet(datetime.now(timezone.utc)) is False


def test_cancel_session_does_not_cut_a_longer_manual_quiet_short(env):
    """The quiet-guard: a user who ran /quiet 24h, then /start + /cancel-session,
    keeps the 24h quiet intact -- ending a short focus block must never clobber a
    longer manual quiet."""
    board, state = env
    # User sets a manual day-long quiet FIRST.
    manual_until = datetime.now(timezone.utc) + timedelta(hours=24)
    quiet_state.set_quiet(manual_until)
    # /start does NOT shorten it (its 25-min window is earlier, so it leaves the
    # longer manual quiet in place); cancel then leaves it intact too.
    start = nag_commands.handle_start("tsk_abc123", create_cron=lambda d: "c")
    assert start["ok"] is True
    assert start["quiet_set"] is False  # did NOT overwrite the longer manual quiet
    result = nag_commands.handle_cancel_session(
        "tsk_abc123", delete_cron=lambda cid: None)
    assert result["ok"] is True
    assert result["quiet_cleared"] is False  # the manual 24h quiet is untouched
    still = quiet_state.quiet_until(datetime.now(timezone.utc))
    assert still is not None and still == manual_until  # 24h quiet intact


def test_cancel_session_keeps_a_manual_quiet_set_after_start(env):
    """If the user runs a LONGER /quiet AFTER /start (deadline now differs from the
    session's), cancel must not clear it -- the deadline no longer matches."""
    board, state = env
    nag_commands.handle_start("tsk_abc123", create_cron=lambda d: "c")
    # User then sets a longer manual quiet, overwriting the session window.
    manual_until = datetime.now(timezone.utc) + timedelta(hours=12)
    quiet_state.set_quiet(manual_until)
    result = nag_commands.handle_cancel_session(
        "tsk_abc123", delete_cron=lambda cid: None)
    assert result["ok"] is True
    assert result["quiet_cleared"] is False  # deadline no longer matches the session
    still = quiet_state.quiet_until(datetime.now(timezone.utc))
    assert still is not None and still == manual_until


def test_cancel_session_clears_crons(env):
    board, state = env
    nag_commands.handle_start("tsk_abc123", create_cron=lambda d: "cron_x")
    deleted = []
    nag_commands.handle_cancel_session(
        "tsk_abc123", delete_cron=lambda cid: deleted.append(cid))
    assert deleted == ["cron_x", "cron_x"]  # both pending check-in crons cancelled


# --- CLI tail parsing (status / minutes / next: cue) ------------------------

def test_parse_start_tail_no_args_is_status():
    assert nag_commands.parse_start_tail([]) == {"status": True}
    assert nag_commands.parse_start_tail(["status"]) == {"status": True}


def test_parse_start_tail_task_only():
    parsed = nag_commands.parse_start_tail(["tsk_abc123"])
    assert parsed == {"status": False, "task_id": "tsk_abc123",
                      "duration": None, "cue": None}


def test_parse_start_tail_task_minutes_and_cue():
    parsed = nag_commands.parse_start_tail(
        ["tsk_abc123", "45m", "next:", "open", "the", "editor"])
    assert parsed["task_id"] == "tsk_abc123"
    assert parsed["duration"] == "45m"
    assert parsed["cue"] == "open the editor"


def test_parse_start_tail_cue_without_minutes():
    parsed = nag_commands.parse_start_tail(["tsk_abc123", "next:", "call", "the", "vendor"])
    assert parsed["duration"] is None
    assert parsed["cue"] == "call the vendor"


def test_start_cli_status_via_main(env, capsys):
    """The CLI `start` (no task) routes to status without tripping the id guard."""
    rc = nag_commands.main(["start"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["active"] is False
