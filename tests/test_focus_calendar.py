"""U6 focus-calendar state: atomic, single-writer, corrupt-safe.

Asserts the spec §3.1 contract: active blocks + dry-run history persist
atomically; a corrupt file is quarantined aside (never erased) and treated as
"no blocks"; dry-run history is bounded.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import cos_config  # noqa: E402
import focus_calendar  # noqa: E402


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_MGMT_STATE_DIR", str(tmp_path / "state"))
    return cos_config.state_dir()


def test_roundtrip_active_block(state_dir):
    state = focus_calendar.load_focus_calendar()
    state["agent_calendar_id"] = "focus-cal"
    state["active_blocks"].append({
        "event_id": "evt_1", "task_id": "tsk_abc", "start": "2026-06-20T09:00:00+00:00",
        "end": "2026-06-20T11:00:00+00:00", "slip_count": 0,
    })
    focus_calendar.save_focus_calendar(state)
    reloaded = focus_calendar.load_focus_calendar()
    assert reloaded["agent_calendar_id"] == "focus-cal"
    assert focus_calendar.find_block(reloaded, "evt_1")["task_id"] == "tsk_abc"
    assert focus_calendar.block_for_task(reloaded, "tsk_abc")["event_id"] == "evt_1"


def test_corrupt_file_quarantined(state_dir):
    path = focus_calendar.focus_calendar_path()
    path.write_text("}{ not json", encoding="utf-8")
    state = focus_calendar.load_focus_calendar()  # must NOT raise
    assert state["active_blocks"] == []
    assert len(list(path.parent.glob(f"{path.name}.corrupt-*"))) == 1


def test_dry_run_history_bounded(state_dir):
    state = focus_calendar.load_focus_calendar()
    for i in range(focus_calendar.MAX_DRY_RUN_HISTORY + 20):
        focus_calendar.record_dry_run(state, "calendar.create", {"i": i}, {"ok": True})
    focus_calendar.save_focus_calendar(state)
    reloaded = focus_calendar.load_focus_calendar()
    assert len(reloaded["dry_run_history"]) == focus_calendar.MAX_DRY_RUN_HISTORY
    # the most recent writes survive (the trim keeps the tail)
    assert reloaded["dry_run_history"][-1]["request"]["i"] == focus_calendar.MAX_DRY_RUN_HISTORY + 19


def test_missing_keys_normalised(state_dir):
    path = focus_calendar.focus_calendar_path()
    path.write_text(json.dumps({"agent_calendar_id": "focus-cal"}), encoding="utf-8")
    state = focus_calendar.load_focus_calendar()
    assert state["active_blocks"] == []  # backfilled, no crash
    assert state["dry_run_history"] == []
