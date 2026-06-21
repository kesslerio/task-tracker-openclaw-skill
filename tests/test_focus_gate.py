"""U3 write-time cap gate (I-CAP denied path) + standup capacity display.

The gate at ``add_task()`` is the Layer-2 enforcement point: an add that would
push the active board past ~1 week of capacity is blocked BEFORE the board write,
so the board stays byte-identical. The cap NEVER force-evicts; ``--force-parking``
routes the over-cap add to the parking lot instead.
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


# --- I-CAP DENIED PATH (the invariant test) --------------------------------

def test_add_blocked_at_cap_board_byte_identical(tmp_path):
    # 28h of estimated active work > 25h WEEKLY_CAPACITY_HOURS.
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
    before = board.read_bytes()

    proc = _run_add(tmp_path, board, "Urgent new thing")

    assert proc.returncode != 0
    # Board byte-for-byte identical: a blocked write changed nothing.
    assert board.read_bytes() == before
    # Friendly denial, NOT a raw traceback.
    assert "cap reached" in proc.stdout.lower()
    assert "Traceback" not in proc.stdout
    assert "Traceback" not in proc.stderr
    # A wip_cap_enforced event was appended.
    events = _ledger_events(tmp_path)
    assert any(e["event_type"] == "wip_cap_enforced" for e in events)


def test_denial_message_has_no_contradictory_capacity_ok_line(tmp_path):
    # Boundary case: current load is exactly at the hard cap (not strictly over),
    # so the current-state display would read "Capacity OK" -- but the projected
    # add breaches it. The denial must NOT splice an "✅ Capacity OK" line into
    # the "❌ cap reached" block.
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
    assert proc.returncode != 0
    assert "cap reached" in proc.stdout.lower()
    assert "Capacity OK" not in proc.stdout  # no self-contradiction


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


def test_force_parking_routes_over_cap_add_to_parking_lot(tmp_path):
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
    # would breach, so the add is blocked. Proves section=None is counted.
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
    before = board.read_bytes()
    proc = _run_add(
        tmp_path,
        board,
        "Fourth",
        env_overrides={"ACTIVE_TASK_HARD_CAP": "3", "WEEKLY_CAPACITY_HOURS": "1000"},
    )
    assert proc.returncode != 0
    assert board.read_bytes() == before


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
    # All Tasks area, so it MUST be gated (the writer's real fallback, not the
    # nominal priority->section map, decides cap scope).
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
    before = board.read_bytes()
    proc = _run_add(tmp_path, board, "Sneaky low", "--priority", "low")
    assert proc.returncode != 0  # gated: it would land in the active All Tasks area
    assert board.read_bytes() == before


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
