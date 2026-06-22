"""U3 write-time cap gate + H6 capture-never-blocks + standup capacity display.

The gate at ``add_task()`` is the Layer-2 enforcement point. H6 changed what
HAPPENS when the active set is full: capture NEVER blocks. An over-cap add now
ALWAYS succeeds and is routed to the parking lot (the inbox) instead of landing on
the active board, so a commitment is never pushed out of the system. The cap now
gates PROMOTION onto the active board (see ``test_focus_promote_swap.py``), not
capture. These tests pin that an over-cap add does NOT bloat the active set -- it
lands in parking -- and that a real capture FAILURE (lot full) still exits nonzero.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))


def _run_add(tmp_path, board: Path, title: str, *extra, env_overrides=None):
    env = os.environ.copy()
    env["TASK_TRACKER_WORK_FILE"] = str(board)
    env["TASK_MGMT_STATE_DIR"] = str(tmp_path / "state")
    env["TASK_TRACKER_LEDGER_FILE"] = str(tmp_path / "events.jsonl")
    env["WEEKLY_CAPACITY_HOURS"] = "25"
    env["UNESTIMATED_TASK_HOURS"] = "2"
    env["ACTIVE_TASK_HARD_CAP"] = "20"
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        ["python3", str(SCRIPTS / "tasks.py"), "add", title, *extra],
        capture_output=True,
        text=True,
        env=env,
    )


def _ledger_events(tmp_path):
    path = tmp_path / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


# --- H6 CAPTURE-NEVER-BLOCKS (the invariant test) --------------------------

def test_over_cap_add_captured_to_parking_not_active_set(tmp_path):
    # 28h of estimated active work > 25h WEEKLY_CAPACITY_HOURS. H6: the add is NOT
    # rejected -- it SUCCEEDS (exit 0) and is captured to the parking lot, so the
    # committed active set does not bloat past the cap. Intent preserved: an
    # over-cap task does not become an active commitment.
    board = tmp_path / "Work Tasks.md"
    board.write_text(
        "# Work\n\n"
        "## 🔴 Q1\n"
        "- [ ] **T1** estimate:: 10h task_id::tsk_aaaaaaaaaaaaaaaa\n"
        "## 🟡 Q2\n"
        "- [ ] **T2** estimate:: 10h task_id::tsk_bbbbbbbbbbbbbbbb\n"
        "- [ ] **T3** estimate:: 8h task_id::tsk_cccccccccccccccc\n"
        "## 🅿️ Parking Lot\n"
    )

    proc = _run_add(tmp_path, board, "Urgent new thing")

    # Capture SUCCEEDS -- never blocks.
    assert proc.returncode == 0
    text = board.read_text()
    # The task lands in the Parking Lot, NOT as a new active task.
    pl_index = text.index("Parking Lot")
    assert text.index("Urgent new thing") > pl_index
    # Non-punitive, "saved not lost" capture message; NOT a raw traceback.
    assert "captured to the parking lot" in proc.stdout.lower()
    assert "saved, not lost" in proc.stdout.lower()
    assert "Traceback" not in proc.stdout
    assert "Traceback" not in proc.stderr
    # The wip_cap_enforced ledger event is kept, routed to parking_lot.
    events = _ledger_events(tmp_path)
    routed = next(e for e in events if e["event_type"] == "wip_cap_enforced")
    assert routed["metadata"]["proposed_routing"] == "parking_lot"


def test_over_cap_capture_message_is_non_punitive(tmp_path):
    # Boundary case: current load is exactly at the hard cap. H6: the over-cap add
    # is captured to parking with a non-punitive message that points at /promote
    # and /swap -- never a punitive "cap reached" rejection block.
    board = tmp_path / "Work Tasks.md"
    board.write_text(
        "# Work\n\n"
        "## 🔴 Q1\n"
        "- [ ] **A** estimate:: 1m task_id::tsk_aaaaaaaaaaaaaaaa\n"
        "- [ ] **B** estimate:: 1m task_id::tsk_bbbbbbbbbbbbbbbb\n"
        "- [ ] **C** estimate:: 1m task_id::tsk_cccccccccccccccc\n"
        "## 🅿️ Parking Lot\n"
    )
    proc = _run_add(
        tmp_path, board, "Fourth",
        env_overrides={"ACTIVE_TASK_HARD_CAP": "3", "WEEKLY_CAPACITY_HOURS": "1000"},
    )
    assert proc.returncode == 0
    out = proc.stdout.lower()
    assert "captured to the parking lot" in out
    assert "/promote" in proc.stdout
    assert "/swap" in proc.stdout
    # The Fourth task is parked, not added to the active set.
    text = board.read_text()
    assert text.index("Fourth") > text.index("Parking Lot")


def test_add_passes_under_cap(tmp_path):
    board = tmp_path / "Work Tasks.md"
    board.write_text(
        "# Work\n\n## 🟡 Q2\n- [ ] **T1** estimate:: 2h task_id::tsk_aaaaaaaaaaaaaaaa\n"
    )
    proc = _run_add(tmp_path, board, "Small task")
    assert proc.returncode == 0
    assert "Small task" in board.read_text()
    # No cap event when under capacity.
    assert not any(e["event_type"] == "wip_cap_enforced" for e in _ledger_events(tmp_path))


def test_force_parking_flag_is_harmless_alias_of_default(tmp_path):
    # H6: over-cap adds route to parking by DEFAULT. --force-parking is kept as a
    # harmless alias so existing callers don't break; it routes to parking exactly
    # like the no-flag path.
    board = tmp_path / "Work Tasks.md"
    board.write_text(
        "# Work\n\n"
        "## 🔴 Q1\n"
        "- [ ] **T1** estimate:: 13h task_id::tsk_aaaaaaaaaaaaaaaa\n"
        "- [ ] **T2** estimate:: 13h task_id::tsk_bbbbbbbbbbbbbbbb\n"
        "## 🅿️ Parking Lot\n"
    )
    proc = _run_add(tmp_path, board, "Overflow task", "--force-parking")
    assert proc.returncode == 0
    text = board.read_text()
    # The task lands in the Parking Lot section, not as a new active task.
    pl_index = text.index("Parking Lot")
    assert text.index("Overflow task") > pl_index
    events = _ledger_events(tmp_path)
    routed = next(e for e in events if e["event_type"] == "wip_cap_enforced")
    assert routed["metadata"]["proposed_routing"] == "parking_lot"


def test_over_cap_capture_fails_nonzero_when_lot_full_no_flag(tmp_path):
    # H6 capture FAILURE path (no flag): if the parking lot itself is full, the
    # over-cap add cannot be captured. It must NOT report "saved" -- it exits
    # nonzero, the board is unchanged, and no parking-routing is logged, so a
    # truly-uncapturable task is never reported as captured.
    board = tmp_path / "Work Tasks.md"
    board.write_text(
        "# Work\n\n"
        "## 🔴 Q1\n"
        "- [ ] **T1** estimate:: 13h task_id::tsk_aaaaaaaaaaaaaaaa\n"
        "- [ ] **T2** estimate:: 13h task_id::tsk_bbbbbbbbbbbbbbbb\n"
        "## 🅿️ Parking Lot\n"
        "- [ ] **P1** created::2026-06-01 task_id::tsk_cccccccccccccccc\n"
    )
    before = board.read_bytes()
    proc = _run_add(
        tmp_path, board, "Overflow",
        env_overrides={"PARKING_LOT_CAP": "1"},
    )
    assert proc.returncode != 0
    assert board.read_bytes() == before  # nothing added anywhere
    assert "captured to the parking lot" not in proc.stdout.lower()
    routed = [
        e for e in _ledger_events(tmp_path)
        if e["event_type"] == "wip_cap_enforced"
        and e["metadata"].get("proposed_routing") == "parking_lot"
    ]
    assert routed == []


def test_force_parking_full_lot_exits_nonzero_and_logs_no_routing(tmp_path):
    # When the parking lot is full, --force-parking must NOT silently exit 0 or
    # log a successful routing -- the task was dropped, not captured.
    board = tmp_path / "Work Tasks.md"
    board.write_text(
        "# Work\n\n"
        "## 🔴 Q1\n"
        "- [ ] **T1** estimate:: 13h task_id::tsk_aaaaaaaaaaaaaaaa\n"
        "- [ ] **T2** estimate:: 13h task_id::tsk_bbbbbbbbbbbbbbbb\n"
        "## 🅿️ Parking Lot\n"
        "- [ ] **P1** created::2026-06-01 task_id::tsk_cccccccccccccccc\n"
    )
    before = board.read_bytes()
    # PARKING_LOT_CAP=1 means the single existing item fills the lot.
    proc = _run_add(
        tmp_path, board, "Overflow", "--force-parking",
        env_overrides={"PARKING_LOT_CAP": "1"},
    )
    assert proc.returncode != 0
    assert board.read_bytes() == before  # nothing added anywhere
    # No wip_cap_enforced with a parking_lot routing -- the route failed.
    routed = [
        e for e in _ledger_events(tmp_path)
        if e["event_type"] == "wip_cap_enforced"
        and e["metadata"].get("proposed_routing") == "parking_lot"
    ]
    assert routed == []


def test_section_none_tasks_counted_toward_hard_cap(tmp_path):
    # 1 Q1 + 2 "All Tasks" (section=None) = 3 active; hard cap 3 means a 4th add
    # would breach. H6: the 4th is CAPTURED to parking (not blocked), proving
    # section=None is still counted toward the cap that triggers the capture --
    # the COUNTING logic is unchanged, only the over-cap OUTCOME changed.
    board = tmp_path / "Work Tasks.md"
    board.write_text(
        "# Work\n\n"
        "## 🔴 Q1\n"
        "- [ ] **Q1 task** task_id::tsk_aaaaaaaaaaaaaaaa\n"
        "## 📋 All Tasks\n"
        "### Dev\n"
        "- [ ] **All one** task_id::tsk_bbbbbbbbbbbbbbbb\n"
        "- [ ] **All two** task_id::tsk_cccccccccccccccc\n"
        "## 🅿️ Parking Lot\n"
    )
    proc = _run_add(
        tmp_path,
        board,
        "Fourth",
        env_overrides={"ACTIVE_TASK_HARD_CAP": "3", "WEEKLY_CAPACITY_HOURS": "1000"},
    )
    assert proc.returncode == 0
    text = board.read_text()
    # The 4th task is parked (it tripped the cap), not added to the active set.
    assert text.index("Fourth") > text.index("Parking Lot")
    assert any(
        e["event_type"] == "wip_cap_enforced" for e in _ledger_events(tmp_path)
    )


def test_personal_add_exempt_from_work_capacity_cap(tmp_path):
    # The cap governs the WORK board only; a personal add over the work-tuned cap
    # must be allowed (the knobs are sized for the work inventory).
    personal = tmp_path / "Personal Tasks.md"
    personal.write_text(
        "# Personal\n\n"
        "## 🔴 Q1\n"
        "- [ ] **Big chore** estimate:: 30h task_id::tsk_aaaaaaaaaaaaaaaa\n"
        "## 🟡 Q2\n"
        "## 🅿️ Parking Lot\n"
    )
    env = os.environ.copy()
    env["TASK_TRACKER_PERSONAL_FILE"] = str(personal)
    env["TASK_MGMT_STATE_DIR"] = str(tmp_path / "state")
    env["TASK_TRACKER_LEDGER_FILE"] = str(tmp_path / "events.jsonl")
    env["WEEKLY_CAPACITY_HOURS"] = "25"
    proc = subprocess.run(
        ["python3", str(SCRIPTS / "tasks.py"), "--personal", "add", "New personal task"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0
    assert "New personal task" in personal.read_text()
    assert not any(e["event_type"] == "wip_cap_enforced" for e in _ledger_events(tmp_path))


def test_low_priority_add_to_backlog_not_blocked_by_cap(tmp_path):
    # --priority low routes to the inactive Backlog (excluded from active load),
    # so it must NOT be blocked even when the active board is over the cap.
    board = tmp_path / "Work Tasks.md"
    board.write_text(
        "# Work\n\n"
        "## 🔴 Q1\n"
        "- [ ] **T1** estimate:: 13h task_id::tsk_aaaaaaaaaaaaaaaa\n"
        "- [ ] **T2** estimate:: 13h task_id::tsk_bbbbbbbbbbbbbbbb\n"
        "## ⚪ Backlog\n"
        "## 🅿️ Parking Lot\n"
    )
    proc = _run_add(tmp_path, board, "Someday idea", "--priority", "low")
    assert proc.returncode == 0
    assert "Someday idea" in board.read_text()
    # No cap event: a backlog add never trips the active-inventory gate.
    assert not any(e["event_type"] == "wip_cap_enforced" for e in _ledger_events(tmp_path))


def test_low_priority_add_gated_when_no_backlog_section(tmp_path):
    # With NO ## ⚪ Backlog section, a --priority low add falls back to the active
    # All Tasks area, so it IS gated (the writer's real fallback, not the nominal
    # priority->section map, decides cap scope). H6: "gated" now means CAPTURED to
    # parking (not blocked) -- the low add does not bloat the over-cap active set,
    # it lands in the inbox instead.
    board = tmp_path / "Work Tasks.md"
    board.write_text(
        "# Work\n\n"
        "## 🔴 Q1\n"
        "- [ ] **T1** estimate:: 13h task_id::tsk_aaaaaaaaaaaaaaaa\n"
        "- [ ] **T2** estimate:: 13h task_id::tsk_bbbbbbbbbbbbbbbb\n"
        "## 📋 All Tasks\n"
        "### Dev\n"
        "## 🅿️ Parking Lot\n"
    )
    proc = _run_add(tmp_path, board, "Sneaky low", "--priority", "low")
    assert proc.returncode == 0  # captured, not blocked
    text = board.read_text()
    # It lands in parking (the gated active fallback was over-cap), not All Tasks.
    assert text.index("Sneaky low") > text.index("Parking Lot")


def test_force_parking_logged_as_user_command(tmp_path):
    # Both add paths are user CLI commands; the --force-parking route must be
    # source=user_command (routing is in metadata), not agent_autonomous.
    board = tmp_path / "Work Tasks.md"
    board.write_text(
        "# Work\n\n"
        "## 🔴 Q1\n"
        "- [ ] **T1** estimate:: 13h task_id::tsk_aaaaaaaaaaaaaaaa\n"
        "- [ ] **T2** estimate:: 13h task_id::tsk_bbbbbbbbbbbbbbbb\n"
        "## 🅿️ Parking Lot\n"
    )
    proc = _run_add(tmp_path, board, "Overflow", "--force-parking")
    assert proc.returncode == 0
    routed = next(
        e for e in _ledger_events(tmp_path) if e["event_type"] == "wip_cap_enforced"
    )
    assert routed["source"] == "user_command"
    assert routed["metadata"]["proposed_routing"] == "parking_lot"


def test_corrupt_focus_state_does_not_leak_traceback_on_add(tmp_path):
    # A corrupt focus-state.json must never surface a traceback through add_task.
    # (The cap is date-independent and does not read focus-state, so the add
    # itself proceeds cleanly under-cap -- the invariant is no raw leak.)
    board = tmp_path / "Work Tasks.md"
    board.write_text(
        "# Work\n\n## 🟡 Q2\n- [ ] **T1** estimate:: 1h task_id::tsk_aaaaaaaaaaaaaaaa\n"
    )
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "focus-state.json").write_text("{}invalid json")

    proc = _run_add(tmp_path, board, "Another")
    assert "Traceback" not in proc.stdout
    assert "JSONDecodeError" not in proc.stdout


# --- Standup capacity display ----------------------------------------------

def test_standup_renders_capacity_line(tmp_path):
    board = tmp_path / "Work Tasks.md"
    board.write_text(
        "# Work\n\n"
        "## 🔴 Q1\n"
        "- [ ] **A** estimate:: 2h task_id::tsk_aaaaaaaaaaaaaaaa\n"
        "## 🅿️ Parking Lot\n"
    )
    env = os.environ.copy()
    env["TASK_TRACKER_WORK_FILE"] = str(board)
    env["TASK_MGMT_STATE_DIR"] = str(tmp_path / "state")
    env["WEEKLY_CAPACITY_HOURS"] = "25"
    proc = subprocess.run(
        ["python3", str(SCRIPTS / "standup.py")],
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0
    assert "Capacity OK" in proc.stdout
    assert "active load" in proc.stdout


def test_standup_summary_omits_capacity_for_personal_board(tmp_path):
    # The Layer-2 cap governs the WORK board only; the personal standup-summary
    # must not emit a work-tuned capacity block.
    personal = tmp_path / "Personal Tasks.md"
    personal.write_text(
        "# Personal\n\n## 🔴 Q1\n- [ ] **Chore** estimate:: 30h task_id::tsk_aaaaaaaaaaaaaaaa\n"
    )
    env = os.environ.copy()
    env["TASK_TRACKER_PERSONAL_FILE"] = str(personal)
    env["TASK_MGMT_STATE_DIR"] = str(tmp_path / "state")
    env["WEEKLY_CAPACITY_HOURS"] = "25"
    proc = subprocess.run(
        ["python3", str(SCRIPTS / "tasks.py"), "--personal", "standup-summary"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert payload["capacity"] is None


def test_standup_capacity_line_flags_overcommit(tmp_path):
    board = tmp_path / "Work Tasks.md"
    board.write_text(
        "# Work\n\n"
        "## 🔴 Q1\n"
        "- [ ] **Big** estimate:: 30h task_id::tsk_aaaaaaaaaaaaaaaa\n"
        "## 🅿️ Parking Lot\n"
    )
    env = os.environ.copy()
    env["TASK_TRACKER_WORK_FILE"] = str(board)
    env["TASK_MGMT_STATE_DIR"] = str(tmp_path / "state")
    env["WEEKLY_CAPACITY_HOURS"] = "25"
    proc = subprocess.run(
        ["python3", str(SCRIPTS / "standup.py")],
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0
    assert "Overcommitted" in proc.stdout
