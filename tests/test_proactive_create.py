"""U6 focus-block CREATE flow + main() wiring (create / debrief-capture modes).

Asserts NEVER-OVERBOOK-EXTERNAL on the create path and that the modes the
pre-brief copy advertises are actually wired into main():

* create: freebusy-gated block creation from the Defended Three; an overlap
  refuses that block (the rest still place); idempotent per task; degrades
  silently with no focus calendar.
* debrief-capture: main() routes user notes into run_debrief_capture.
"""

import sys
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import focus_calendar  # noqa: E402
import focus_state  # noqa: E402
import proactive_brief  # noqa: E402
import proactive_state  # noqa: E402
import task_ledger  # noqa: E402
import utils  # noqa: E402

PRODUCTIVITY = "-4242424242"
WORK_GROUP = "-5252525252"
NOW = datetime(2026, 6, 20, 7, 0, tzinfo=timezone.utc)  # before the 09:00 day start

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
    monkeypatch.setenv("STANDUP_CALENDARS", '{"work": {"calendar_id": "primary", "account": "me"}}')
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


def _seed_focus_calendar():
    state = focus_calendar.load_focus_calendar()
    state["agent_calendar_id"] = "focus-cal"
    focus_calendar.save_focus_calendar(state)


def _seed_priorities(rows):
    focus_state.save_focus_state({
        "date": focus_state.today_str(), "status": "approved", "daily_priorities": rows,
    })


def _create_runner(freebusy_stdout):
    import json
    responses = {"calendar.freebusy": freebusy_stdout, "calendar.create": json.dumps({"id": "evt_new"})}
    calls = []

    def runner(cmd):
        calls.append(cmd)
        return _completed(responses[f"{cmd[1]}.{cmd[2]}"])

    runner.calls = calls  # type: ignore[attr-defined]
    return runner


def _ext_free(busy):
    import json
    return json.dumps({"calendars": {"primary": {"busy": busy}}})


def _types(state):
    return [e["event_type"] for e in task_ledger.read_events(state / "events.jsonl")]


# --- create: freebusy-gated block creation ----------------------------------

def test_create_blocks_places_freebusy_clear_priority(harness):
    board, state = harness
    _seed_focus_calendar()
    _seed_priorities([{"task_id": "tsk_rel", "title": "Finalize release notes", "estimate_minutes": 120}])
    runner = _create_runner(_ext_free([]))
    sent: list = []
    counts = proactive_brief.run_create_blocks(now=NOW, tz_offset_hours=0, day_start_hour=9, send=lambda t, x: sent.append(x), runner=runner)
    assert counts["created"] == 1
    assert any(c[1:3] == ["calendar", "create"] for c in runner.calls)
    reloaded = focus_calendar.load_focus_calendar()
    assert focus_calendar.block_for_task(reloaded, "tsk_rel") is not None
    assert "calendar_block_created" in _types(state)


def test_create_blocks_refuses_on_external_overlap(harness):
    """NEVER-OVERBOOK-EXTERNAL: a busy external slot refuses THAT block, no create."""
    board, state = harness
    _seed_focus_calendar()
    _seed_priorities([{"task_id": "tsk_rel", "title": "Finalize release notes", "estimate_minutes": 120}])
    # The 09:00-11:00 window overlaps an external meeting -> refuse.
    runner = _create_runner(_ext_free([{"start": "2026-06-20T09:30:00+00:00", "end": "2026-06-20T10:00:00+00:00"}]))
    counts = proactive_brief.run_create_blocks(now=NOW, tz_offset_hours=0, day_start_hour=9, send=lambda t, x: None, runner=runner)
    assert counts["created"] == 0
    assert counts["refused"] == 1
    assert not any(c[1:3] == ["calendar", "create"] for c in runner.calls)
    assert "calendar_block_refused" in _types(state)


def test_create_blocks_idempotent_per_task(harness):
    board, state = harness
    _seed_focus_calendar()
    _seed_priorities([{"task_id": "tsk_rel", "title": "Finalize release notes", "estimate_minutes": 120}])
    runner = _create_runner(_ext_free([]))
    proactive_brief.run_create_blocks(now=NOW, tz_offset_hours=0, day_start_hour=9, send=lambda t, x: None, runner=runner)
    # second run: the block already exists -> skipped, no second create
    runner2 = _create_runner(_ext_free([]))
    counts = proactive_brief.run_create_blocks(now=NOW, tz_offset_hours=0, day_start_hour=9, send=lambda t, x: None, runner=runner2)
    assert counts["created"] == 0
    assert counts["skipped"] == 1
    assert not any(c[1:3] == ["calendar", "create"] for c in runner2.calls)


def test_create_blocks_no_focus_calendar_degrades(harness):
    board, state = harness
    # no focus calendar seeded
    _seed_priorities([{"task_id": "tsk_rel", "title": "x", "estimate_minutes": 60}])
    runner = _create_runner(_ext_free([]))
    counts = proactive_brief.run_create_blocks(now=NOW, tz_offset_hours=0, day_start_hour=9, send=lambda t, x: None, runner=runner)
    assert counts == {"created": 0, "refused": 0, "skipped": 0}
    assert runner.calls == []


def test_create_blocks_never_writes_focus_state(harness):
    """U6 reads focus-state.json but NEVER writes it (U3 is the sole writer)."""
    board, state = harness
    _seed_focus_calendar()
    _seed_priorities([{"task_id": "tsk_rel", "title": "x", "estimate_minutes": 60}])
    before = focus_state.focus_state_path().read_text()
    runner = _create_runner(_ext_free([]))
    proactive_brief.run_create_blocks(now=NOW, tz_offset_hours=0, day_start_hour=9, send=lambda t, x: None, runner=runner)
    assert focus_state.focus_state_path().read_text() == before


def test_create_blocks_anchors_to_local_morning_not_utc(harness):
    """autoreview: a UTC `now` must place blocks at the user's LOCAL morning, not
    UTC 09:00. With offset -7 (PT), a 09:00-local block starts at 16:00 UTC."""
    board, state = harness
    _seed_focus_calendar()
    _seed_priorities([{"task_id": "tsk_rel", "title": "Finalize", "estimate_minutes": 60}])
    runner = _create_runner(_ext_free([]))
    # UTC now; offset -7 -> 09:00 PT == 16:00 UTC
    proactive_brief.run_create_blocks(now=NOW, tz_offset_hours=-7, day_start_hour=9,
                                      send=lambda t, x: None, runner=runner)
    create_cmd = next(c for c in runner.calls if c[1:3] == ["calendar", "create"])
    from_idx = create_cmd.index("--from") + 1
    assert "T09:00:00-07:00" in create_cmd[from_idx]  # 09:00 LOCAL (PT), not UTC


def test_create_block_is_gated_in_autonomy_log(harness):
    """autoreview: an autonomous calendar write is recorded in the autonomy log via
    the gate (the calendar_block_created rung is actually exercised)."""
    import autonomy_gate

    board, state = harness
    _seed_focus_calendar()
    _seed_priorities([{"task_id": "tsk_rel", "title": "Finalize", "estimate_minutes": 120}])
    runner = _create_runner(_ext_free([]))
    proactive_brief.run_create_blocks(now=NOW, tz_offset_hours=0, day_start_hour=9, send=lambda t, x: None, runner=runner)
    acts = [a for a in autonomy_gate.read_autonomy_log() if a.get("act_type") == "calendar_block_created"]
    assert len(acts) == 1
    assert acts[0]["status"] == "executed"
    assert acts[0]["rung"] == autonomy_gate.RUNG_MONITORED_AUTO
    # the act carries the reversal substrate (the calendar window), not a board line
    assert acts[0]["pre_action_snapshot"]["task_id"] == "tsk_rel"


# --- main() wiring: debrief-capture is reachable ----------------------------

def test_main_debrief_capture_routes_to_handler(harness, monkeypatch, capsys):
    board, state = harness
    st = proactive_state.load_proactive_state()
    proactive_state.mark_pre_brief_sent(st, "evt_q3", "Q3", "2026-06-20T09:00:00+00:00")
    proactive_state.open_debrief(st, "evt_q3")
    proactive_state.save_proactive_state(st)

    # stub the commitment-add subprocess so no real task is spawned
    monkeypatch.setattr(proactive_brief, "_create_commitment_task", lambda spec, runner=None: "tsk_x")
    rc = proactive_brief.main(["--mode", "debrief-capture", "--event-key", "evt_q3",
                               "--notes", "I will ship by 2026-06-30"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "DEBRIEF_CAPTURE: captured=True" in out
    reloaded = proactive_state.load_proactive_state()
    assert proactive_state.is_debrief_open(reloaded["pre_briefs"][0]) is False


def test_main_create_mode_is_wired(harness, monkeypatch):
    board, state = harness
    called = {}
    monkeypatch.setattr(proactive_brief, "run_create_blocks",
                        lambda **kw: called.setdefault("kw", kw) or {"created": 0, "refused": 0, "skipped": 0})
    rc = proactive_brief.main(["--mode", "create", "--dry-run"])
    assert rc == 0
    assert called["kw"]["dry_run"] is True
