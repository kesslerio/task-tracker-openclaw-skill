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


def _active_estimated_minutes(board, title):
    """Minutes a single active task by ``title`` contributes to the capacity cap.

    Reads the board with the canonical capacity layer so the assertion pins what
    ``focus_core`` actually COUNTS, not the raw token. A task whose ``estimate::``
    survived the round-trip counts at its real duration; one that lost it counts
    at the unestimated default (UNESTIMATED_TASK_HOURS).
    """
    from task_records import task_records, active_records
    from focus_core import estimate_minutes_for

    records = task_records(board.read_text(), personal=False, fmt="obsidian")
    target = next(
        r for r in active_records(records)
        if r.title == title and not r.is_objective
    )
    return estimate_minutes_for(target)


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


# --- Fix 1: swap is all-or-nothing on CAPACITY (no partial board) ----------

def _unequal_board(tmp_path):
    # Active load 12h + 12h + 1m ≈ 24.02h against a 25h cap. The "Tiny" task frees
    # only 1m when parked out, while promoting "Parked idea" (unestimated -> 2h)
    # needs 2h. After the swap the projected load would be ~26h > 25h, so an
    # UNEQUAL swap (out frees less than in needs) must NOT fit.
    board = tmp_path / "Work Tasks.md"
    board.write_text(
        "# Work\n\n"
        "## 🔴 Q1\n"
        "- [ ] **Big1** estimate:: 12h task_id::tsk_aaaaaaaaaaaaaaaa\n"
        "## 🟡 Q2\n"
        "- [ ] **Big2** estimate:: 12h task_id::tsk_bbbbbbbbbbbbbbbb\n"
        "- [ ] **Tiny** estimate:: 1m task_id::tsk_cccccccccccccccc\n"
        "## 🅿️ Parking Lot\n"
        "- [ ] **Parked idea** #Dev task_id::tsk_pppppppppppppppp created::2026-06-01\n"
    )
    return board


def test_swap_unequal_estimate_wont_fit_refuses_byte_identical(tmp_path):
    # Pre-flight invariant: an unequal swap where the IN task needs more room than
    # the OUT task frees is refused BEFORE any write. The board is byte-identical
    # (out still active, in still parked) and no task_swapped event is logged --
    # the documented no-partial-move invariant holds.
    board = _unequal_board(tmp_path)
    before = board.read_bytes()
    proc = _run(tmp_path, board, "swap", "tsk_cccccccccccccccc", "1")
    assert proc.returncode != 0
    assert board.read_bytes() == before  # no partial board
    assert "won't fit" in proc.stdout.lower()
    assert "Nothing moved" in proc.stdout
    # Out task still active, in task still parked.
    text = board.read_text()
    pl_index = text.index("Parking Lot")
    assert "Tiny" in text[:pl_index]
    assert "Parked idea" in text[pl_index:]
    assert not any(e["event_type"] == "task_swapped" for e in _ledger_events(tmp_path))


def test_swap_unequal_estimate_that_fits_succeeds(tmp_path):
    # The complement: an unequal swap that DOES fit (out=Big1 frees 12h, in is
    # unestimated -> 2h) succeeds, the net committed count is unchanged, and the
    # parked-out task carries no estimate hint that would block a later re-promote.
    board = _unequal_board(tmp_path)
    # Drop the 1m Tiny task so the only active work is Big1 + Big2 (24h). Swapping
    # Big1 (frees 12h) for Parked idea (2h) projects to 14h <= 25h -> fits.
    text = board.read_text().replace(
        "- [ ] **Tiny** estimate:: 1m task_id::tsk_cccccccccccccccc\n", ""
    )
    board.write_text(text)
    proc = _run(tmp_path, board, "swap", "tsk_aaaaaaaaaaaaaaaa", "1")
    assert proc.returncode == 0
    assert "Swapped" in proc.stdout
    text = board.read_text()
    pl_index = text.index("Parking Lot")
    active_part = text[:pl_index]
    parking_part = text[pl_index:]
    # Big1 parked out; Parked idea promoted in.
    assert "Big1" not in active_part and "Big1" in parking_part
    assert "Parked idea" in active_part and "Parked idea" not in parking_part
    # Net committed count unchanged: Big2 + Parked idea = 2 active tasks.
    assert active_part.count("- [ ] ") == 2
    swapped = next(e for e in _ledger_events(tmp_path) if e["event_type"] == "task_swapped")
    assert swapped["metadata"]["parked_out"] == "Big1"


# --- H6 estimate: estimate survives park-out / capture -> promote -----------

def _estimated_swap_board(tmp_path):
    # One estimated active task (12h) carrying a due date + non-default owner, and
    # a small parked filler to swap in. Cap 25h leaves room for either alone.
    board = tmp_path / "Work Tasks.md"
    board.write_text(
        "# Work\n\n"
        "## 🔴 Q1\n"
        "## 🟡 Q2\n"
        "- [ ] **Heavy job** 🗓️2026-09-01 estimate:: 12h owner:: alice "
        "task_id::tsk_hhhhhhhhhhhhhhhh\n"
        "## 🅿️ Parking Lot\n"
        "- [ ] **Filler idea** #Dev task_id::tsk_ffffffffffffffff created::2026-06-01\n"
    )
    return board


def test_swap_out_task_estimate_survives_to_promoted_in(tmp_path):
    # Invariant: a swap-out of an ESTIMATED task (12h) carrying a due date + a
    # non-default owner parks a self-describing copy (estimate + due + owner all
    # retained), and a later /promote of it restores ALL THREE onto the active
    # line. The restored task COUNTS at its real 12h, not the 2h unestimated
    # default -- the capacity under-count is the bug this pins.
    board = _estimated_swap_board(tmp_path)
    # Swap: park out the 12h Heavy job, promote in the small Filler idea.
    swap = _run(tmp_path, board, "swap", "tsk_hhhhhhhhhhhhhhhh", "1")
    assert swap.returncode == 0, swap.stdout + swap.stderr
    text = board.read_text()
    pl_index = text.index("Parking Lot")
    parked = text[pl_index:]
    # The parked-out copy is self-describing: estimate + due + owner all survived.
    assert "estimate:: 12h" in parked
    assert "🗓️2026-09-01" in parked
    assert "owner:: alice" in parked
    assert "Heavy job" in parked
    # Now promote the parked-out Heavy job back (Filler 2h + Heavy 12h = 14h <= 25h).
    promo = _run(tmp_path, board, "promote", "1")
    assert promo.returncode == 0, promo.stdout + promo.stderr
    text = board.read_text()
    pl_index = text.index("Parking Lot")
    active = text[:pl_index]
    # All three metadata fields restored onto the active line.
    assert "Heavy job" in active
    assert "estimate:: 12h" in active
    assert "🗓️2026-09-01" in active
    assert "owner:: alice" in active
    # Capacity COUNTS it at 12h (720m), NOT the 2h unestimated default (120m).
    assert _active_estimated_minutes(board, "Heavy job") == 12 * 60


def test_repromote_of_estimated_parked_task_does_not_exceed_capacity(tmp_path):
    # The under-count consequence: the swap pre-flight + a re-promote of a
    # previously-estimated parked-out task must NOT silently let the board exceed
    # capacity. A 12h task already active leaves 13h headroom under a 25h cap;
    # trying to swap in a 20h parked task (which frees only the 12h out task)
    # would project to 12h(remaining other) ... here we keep one 12h active task
    # and a 20h parked task. Removing the 12h out frees 12h but the 20h in needs
    # 20h, so the projected board (0h + 20h) actually FITS at 20h <= 25h -- so we
    # instead pin the inverse: with TWO 12h active tasks (24h), swapping out one
    # 12h for a 20h parked task projects 12h + 20h = 32h > 25h and MUST refuse,
    # because the real 20h estimate (not the 2h default) is honoured.
    board = tmp_path / "Work Tasks.md"
    board.write_text(
        "# Work\n\n"
        "## 🔴 Q1\n"
        "- [ ] **Keep job** estimate:: 12h task_id::tsk_aaaaaaaaaaaaaaaa\n"
        "## 🟡 Q2\n"
        "- [ ] **Out job** estimate:: 12h task_id::tsk_bbbbbbbbbbbbbbbb\n"
        "## 🅿️ Parking Lot\n"
        "- [ ] **Whale task** estimate:: 20h #Dev task_id::tsk_wwwwwwwwwwwwwwww "
        "created::2026-06-01\n"
    )
    before = board.read_bytes()
    proc = _run(tmp_path, board, "swap", "tsk_bbbbbbbbbbbbbbbb", "1")
    # The 20h in-task's REAL estimate is honoured by the pre-flight: 12h kept +
    # 20h in = 32h > 25h, so the swap is refused with the board byte-identical.
    assert proc.returncode != 0
    assert board.read_bytes() == before
    assert "won't fit" in proc.stdout.lower()
    assert "Nothing moved" in proc.stdout
    assert not any(e["event_type"] == "task_swapped" for e in _ledger_events(tmp_path))


def test_swap_due_and_owner_survive_to_promoted_in_and_parked_copy(tmp_path):
    # P3: a swap whose out_id carries a due date AND a non-default owner -- both
    # must survive onto the parked-out COPY and (after a re-promote) back onto the
    # active line. Extends the due/owner round-trip coverage to the SWAP path, not
    # just capture->promote.
    board = tmp_path / "Work Tasks.md"
    board.write_text(
        "# Work\n\n"
        "## 🔴 Q1\n"
        "## 🟡 Q2\n"
        "- [ ] **Owned job** 🗓️2026-10-15 owner:: carol "
        "task_id::tsk_oooooooooooooooo\n"
        "## 🅿️ Parking Lot\n"
        "- [ ] **Filler idea** #Dev task_id::tsk_ffffffffffffffff created::2026-06-01\n"
    )
    swap = _run(tmp_path, board, "swap", "tsk_oooooooooooooooo", "1")
    assert swap.returncode == 0, swap.stdout + swap.stderr
    text = board.read_text()
    pl_index = text.index("Parking Lot")
    parked = text[pl_index:]
    # Parked-out copy carries due + owner.
    assert "🗓️2026-10-15" in parked
    assert "owner:: carol" in parked
    # Re-promote it and confirm due + owner land back on the active line.
    promo = _run(tmp_path, board, "promote", "1")
    assert promo.returncode == 0, promo.stdout + promo.stderr
    text = board.read_text()
    active = text[:text.index("Parking Lot")]
    assert "Owned job" in active
    assert "🗓️2026-10-15" in active
    assert "owner:: carol" in active
