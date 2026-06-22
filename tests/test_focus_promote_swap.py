"""H6 promotion gate + swap: capture-never-blocks completed by /promote and /swap.

H6 made capture never block (see test_focus_gate.py). The cap now gates PROMOTION
onto the committed-active set. These tests pin:

* /promote moves a parked task ONTO the active board when there is room, removes
  it from parking, and logs ``task_promoted``.
* /promote REFUSES (nothing moved, nonzero exit, /swap hint) when the committed
  set is full.
* /swap parks an active task and promotes a parked one in one move, leaving the
  net committed count unchanged.
* A bad out/in id refuses cleanly with no partial move.

Fake ids only.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))


def _base_env(tmp_path, board, **overrides):
    env = os.environ.copy()
    env["TASK_TRACKER_WORK_FILE"] = str(board)
    env["TASK_MGMT_STATE_DIR"] = str(tmp_path / "state")
    env["TASK_TRACKER_LEDGER_FILE"] = str(tmp_path / "events.jsonl")
    env["WEEKLY_CAPACITY_HOURS"] = "25"
    env["UNESTIMATED_TASK_HOURS"] = "2"
    env["ACTIVE_TASK_HARD_CAP"] = "20"
    env.update(overrides)
    return env


def _run(tmp_path, board, *args, env_overrides=None):
    env = _base_env(tmp_path, board, **(env_overrides or {}))
    return subprocess.run(
        ["python3", str(SCRIPTS / "tasks.py"), *args],
        capture_output=True,
        text=True,
        env=env,
    )


def _ledger_events(tmp_path):
    path = tmp_path / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def _board_with_room(tmp_path):
    board = tmp_path / "Work Tasks.md"
    board.write_text(
        "# Work\n\n"
        "## 🔴 Q1\n"
        "## 🟡 Q2\n"
        "- [ ] **Small** estimate:: 2h task_id::tsk_aaaaaaaaaaaaaaaa\n"
        "## 🅿️ Parking Lot\n"
        "- [ ] **Parked idea** #Sales task_id::tsk_pppppppppppppppp created::2026-06-01\n"
    )
    return board


def _full_board(tmp_path):
    # Two 13h active tasks => 26h > 25h cap, so the committed set is full.
    board = tmp_path / "Work Tasks.md"
    board.write_text(
        "# Work\n\n"
        "## 🔴 Q1\n"
        "- [ ] **T1** estimate:: 13h task_id::tsk_aaaaaaaaaaaaaaaa\n"
        "## 🟡 Q2\n"
        "- [ ] **T2** estimate:: 13h task_id::tsk_bbbbbbbbbbbbbbbb\n"
        "## 🅿️ Parking Lot\n"
        "- [ ] **Parked idea** #Dev task_id::tsk_pppppppppppppppp created::2026-06-01\n"
    )
    return board


# --- /promote --------------------------------------------------------------

def test_promote_with_room_moves_parked_to_active(tmp_path):
    board = _board_with_room(tmp_path)
    proc = _run(tmp_path, board, "promote", "1")
    assert proc.returncode == 0
    assert "Promoted" in proc.stdout
    text = board.read_text()
    pl_index = text.index("Parking Lot")
    # The task now lives BEFORE the parking lot (on the active board)...
    assert text.index("Parked idea") < pl_index
    # ...and is gone from the parking-lot section.
    assert "Parked idea" not in text[pl_index:]
    # task_promoted logged.
    assert any(e["event_type"] == "task_promoted" for e in _ledger_events(tmp_path))


def test_promote_when_full_refuses_nothing_moved(tmp_path):
    board = _full_board(tmp_path)
    before = board.read_bytes()
    proc = _run(tmp_path, board, "promote", "1")
    assert proc.returncode != 0
    # Nothing moved: board byte-identical.
    assert board.read_bytes() == before
    assert "full" in proc.stdout.lower()
    assert "/swap" in proc.stdout  # swap hint
    # No task_promoted event when the gate refuses.
    assert not any(e["event_type"] == "task_promoted" for e in _ledger_events(tmp_path))


def test_promote_unknown_id_refuses(tmp_path):
    board = _board_with_room(tmp_path)
    before = board.read_bytes()
    proc = _run(tmp_path, board, "promote", "99")
    assert proc.returncode != 0
    assert board.read_bytes() == before
    assert "not found" in proc.stdout.lower()


# --- /swap -----------------------------------------------------------------

def test_swap_parks_out_promotes_in_net_count_unchanged(tmp_path):
    board = _full_board(tmp_path)
    proc = _run(tmp_path, board, "swap", "tsk_aaaaaaaaaaaaaaaa", "1")
    assert proc.returncode == 0
    assert "Swapped" in proc.stdout
    text = board.read_text()
    pl_index = text.index("Parking Lot")
    active_part = text[:pl_index]
    parking_part = text[pl_index:]
    # T1 (parked out) left the active board and is now parked.
    assert "T1" not in active_part
    assert "T1" in parking_part
    # Parked idea (promoted in) is now active and gone from parking.
    assert "Parked idea" in active_part
    assert "Parked idea" not in parking_part
    # Net committed count unchanged: still T2 + Parked idea = 2 active tasks.
    assert active_part.count("- [ ] ") == 2
    # task_swapped logged with the parked-out title in metadata.
    swapped = next(e for e in _ledger_events(tmp_path) if e["event_type"] == "task_swapped")
    assert swapped["metadata"]["parked_out"] == "T1"


def test_swap_bad_out_id_refuses_no_partial_move(tmp_path):
    board = _full_board(tmp_path)
    before = board.read_bytes()
    proc = _run(tmp_path, board, "swap", "tsk_does_not_exist", "1")
    assert proc.returncode != 0
    # No partial move: the board is byte-identical.
    assert board.read_bytes() == before
    assert "Nothing moved" in proc.stdout
    assert not any(e["event_type"] == "task_swapped" for e in _ledger_events(tmp_path))


def test_swap_bad_in_id_refuses_no_partial_move(tmp_path):
    board = _full_board(tmp_path)
    before = board.read_bytes()
    proc = _run(tmp_path, board, "swap", "tsk_aaaaaaaaaaaaaaaa", "99")
    assert proc.returncode != 0
    # No partial move: the active task was NOT parked because the in_id was invalid.
    assert board.read_bytes() == before
    assert "Nothing moved" in proc.stdout
    assert not any(e["event_type"] == "task_swapped" for e in _ledger_events(tmp_path))
