"""U6 slip recovery + debrief follow-up: NEVER-OVERBOOK-EXTERNAL through the flow.

Asserts the unit invariant on the slip path (the cron flow, not just the
calendar_blocks unit):

* a slipped agent-owned block slides via gog UPDATE (never delete+create) and the
  state persists atomically (T5);
* a freebusy-unknown / busy new window REFUSES the move, logs
  calendar_block_refused, and leaves the block in place (NEVER-OVERBOOK-EXTERNAL);
* a debrief follow-up re-prompts an OPEN loop after the event ends but never closes
  it (NAG-CLOSES-ONLY-ON-ACK).
"""

import sys
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import focus_calendar  # noqa: E402
import proactive_brief  # noqa: E402
import proactive_state  # noqa: E402
import task_ledger  # noqa: E402
import utils  # noqa: E402

PRODUCTIVITY = "-4242424242"
WORK_GROUP = "-5252525252"
NOW = datetime(2026, 6, 20, 12, 0, tzinfo=timezone.utc)  # noon -- a 09:00 block has slipped

BOARD = """# Work

## 🔴 Q1
- [ ] **Finalize AlphaClaw release notes** task_id::tsk_rel 🗓️2026-06-20 estimate:: 2h area:: Eng
"""


def _set_env(monkeypatch, board, state_dir):
    monkeypatch.setenv("TASK_TRACKER_WORK_FILE", str(board))
    monkeypatch.setenv("TASK_TRACKER_LEDGER_FILE", str(state_dir / "events.jsonl"))
    monkeypatch.setenv("TASK_MGMT_STATE_DIR", str(state_dir))
    monkeypatch.setenv("TELEGRAM_CHAT_ID_PRODUCTIVITY", PRODUCTIVITY)
    monkeypatch.setenv("TELEGRAM_CHAT_ID_WORK", WORK_GROUP)
    monkeypatch.setenv("OPENCLAW_TOPIC_PRODUCTIVITY_STANDUP", "2")
    monkeypatch.setenv("TASK_TRACKER_FOCUS_CALENDAR_ID", "focus-cal")
    monkeypatch.delenv("STANDUP_CALENDARS", raising=False)
    monkeypatch.setattr(utils, "OBSIDIAN_WORK", Path(board))


@pytest.fixture
def harness(tmp_path, monkeypatch):
    board = tmp_path / "Work Tasks.md"
    board.write_text(BOARD, encoding="utf-8")
    state = tmp_path / "state"
    _set_env(monkeypatch, board, state)
    return board, state


def _completed(stdout, returncode=0):
    return types.SimpleNamespace(stdout=stdout, stderr="", returncode=returncode)


def _seed_block(start="2026-06-20T09:00:00+00:00", end="2026-06-20T11:00:00+00:00"):
    state = focus_calendar.load_focus_calendar()
    state["agent_calendar_id"] = "focus-cal"
    state["active_blocks"].append({
        "event_id": "evt_1", "task_id": "tsk_rel", "task_title": "Finalize AlphaClaw release notes",
        "start": start, "end": end, "slip_count": 0,
    })
    focus_calendar.save_focus_calendar(state)


def _types(state):
    return [e["event_type"] for e in task_ledger.read_events(state / "events.jsonl")]


def _agent_event_runner(freebusy_stdout):
    """A gog runner: event -> agent-created, freebusy -> the given stdout, update -> ok."""
    import json
    responses = {
        "calendar.event": json.dumps({"id": "evt_1", "extendedProperties": {"private": {"agent_created": "task-tracker"}}}),
        "calendar.freebusy": freebusy_stdout,
        "calendar.update": json.dumps({"id": "evt_1"}),
    }
    calls = []

    def runner(cmd):
        calls.append(cmd)
        key = f"{cmd[1]}.{cmd[2]}"
        return _completed(responses[key])

    runner.calls = calls  # type: ignore[attr-defined]
    return runner


# --- T5: slip recovery slides via UPDATE, never delete+create ---------------

def test_t5_slip_recovery_moves_via_update(harness):
    board, state = harness
    _seed_block()
    runner = _agent_event_runner('{"calendars": {"focus-cal": {"busy": []}}}')  # new window free
    counts = proactive_brief.run_slip_recovery(now=NOW, send=None, runner=runner)
    assert counts["moved"] == 1
    assert counts["refused"] == 0
    # the block kept its id and was UPDATED, not delete+created
    assert any(c[1:3] == ["calendar", "update"] for c in runner.calls)
    assert not any(c[1:3] == ["calendar", "delete"] for c in runner.calls)
    assert not any(c[1:3] == ["calendar", "create"] for c in runner.calls)
    reloaded = focus_calendar.load_focus_calendar()
    moved = focus_calendar.find_block(reloaded, "evt_1")
    assert moved["slip_count"] == 1
    assert "calendar_block_moved" in _types(state)


# --- NEVER-OVERBOOK-EXTERNAL: busy new window refuses the move --------------

def test_slip_into_busy_window_refused_and_block_stays(harness):
    board, state = harness
    _seed_block()
    runner = _agent_event_runner(
        '{"calendars": {"focus-cal": {"busy": [{"start": "2026-06-20T13:00:00+00:00", "end": "2026-06-20T14:00:00+00:00"}]}}}')
    counts = proactive_brief.run_slip_recovery(now=NOW, send=None, runner=runner)
    assert counts["moved"] == 0
    assert counts["refused"] == 1
    assert not any(c[1:3] == ["calendar", "update"] for c in runner.calls)  # no move
    # the block is left in place at its original time
    reloaded = focus_calendar.load_focus_calendar()
    assert focus_calendar.find_block(reloaded, "evt_1")["start"] == "2026-06-20T09:00:00+00:00"
    assert "calendar_block_refused" in _types(state)


def test_slip_freebusy_unknown_refused(harness):
    """A freebusy-unknown new window is treated as busy -> refuse (T7 through flow)."""
    board, state = harness
    _seed_block()
    runner = _agent_event_runner("not json")  # freebusy unparseable -> unknown
    counts = proactive_brief.run_slip_recovery(now=NOW, send=None, runner=runner)
    assert counts["refused"] == 1
    assert "calendar_block_refused" in _types(state)


def test_no_focus_calendar_degrades_silently(harness, monkeypatch):
    """No focus calendar configured => slip recovery is a no-op (degrade silently)."""
    board, state = harness
    # focus-calendar.json has no agent_calendar_id
    runner = _agent_event_runner('{"calendars": {}}')
    counts = proactive_brief.run_slip_recovery(now=NOW, send=None, runner=runner)
    assert counts == {"moved": 0, "refused": 0}
    assert runner.calls == []  # never touched gog


def test_future_block_not_slipped(harness):
    """A block whose start is still in the future is NOT a slip."""
    board, state = harness
    _seed_block(start="2026-06-20T15:00:00+00:00", end="2026-06-20T17:00:00+00:00")
    runner = _agent_event_runner('{"calendars": {"focus-cal": {"busy": []}}}')
    counts = proactive_brief.run_slip_recovery(now=NOW, send=None, runner=runner)
    assert counts == {"moved": 0, "refused": 0}
    assert runner.calls == []


# --- Debrief follow-up re-prompts an OPEN loop but never closes it ----------

def test_debrief_followup_reprompts_open_loop(harness, monkeypatch):
    board, state = harness
    # Seed a pre-brief + open debrief for an event that has already ended.
    st = proactive_state.load_proactive_state()
    proactive_state.mark_pre_brief_sent(st, "evt_q3", "Q3 Review", "2026-06-20T09:00:00+00:00")
    proactive_state.open_debrief(st, "evt_q3")
    proactive_state.save_proactive_state(st)
    monkeypatch.setattr(proactive_brief, "get_calendar_events", lambda **_k: {"work": []})

    sent: list = []
    counts = proactive_brief.run_pre_brief_scan(now=NOW, send=lambda t, x: sent.append((t, x)))
    assert counts["debrief_reprompts"] == 1
    # the loop is STILL open -- a re-prompt never closes it
    reloaded = proactive_state.load_proactive_state()
    assert proactive_state.is_debrief_open(reloaded["pre_briefs"][0]) is True
