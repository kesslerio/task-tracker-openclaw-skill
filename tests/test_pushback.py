import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import pushback
from task_records import task_records

TODAY = date(2026, 6, 24)


def _records(content: str):
    return task_records(content)


# Over the weekly capacity (26h estimated > 25h WEEKLY_CAPACITY_HOURS) with three
# active tasks: two dated (one >14d overdue = stale, one <14d overdue), one undated.
_OVER_CAP_BOARD = """# Work

## 🔴 Q1
- [ ] **Ship payroll sync** 🗓️2026-05-01 estimate:: 13h task_id::tsk_aaaaaaaaaaaaaaaa
- [ ] **Fix onboarding** 🗓️2026-06-20 estimate:: 13h task_id::tsk_bbbbbbbbbbbbbbbb
- [ ] **Write the docs** task_id::tsk_cccccccccccccccc

## 🅿️ Parking Lot
"""

_UNDER_CAP_BOARD = """# Work

## 🔴 Q1
- [ ] **One small thing** 🗓️2026-05-01 estimate:: 1h task_id::tsk_aaaaaaaaaaaaaaaa

## 🅿️ Parking Lot
"""


def test_under_cap_is_silent():
    assert pushback.capacity_pushback(_records(_UNDER_CAP_BOARD), today=TODAY) is None


def test_no_records_is_silent():
    assert pushback.capacity_pushback([], today=TODAY) is None
    assert pushback.capacity_pushback(None, today=TODAY) is None


def test_over_cap_lists_candidates_most_overdue_first_undated_last():
    block = pushback.capacity_pushback(_records(_OVER_CAP_BOARD), today=TODAY, stale_days=14)
    assert block is not None
    lines = block.splitlines()
    candidate_lines = [ln for ln in lines if ln.startswith("  - ")]
    # Ordered: oldest/most-overdue due first, undated last.
    assert "Ship payroll sync" in candidate_lines[0]
    assert "Fix onboarding" in candidate_lines[1]
    assert "Write the docs" in candidate_lines[2]
    # 2026-05-01 is 54d overdue (> 14) -> stale; 2026-06-20 is 4d overdue -> not stale.
    assert "stale" in candidate_lines[0]
    assert "overdue" in candidate_lines[1] and "stale" not in candidate_lines[1]
    assert "no due date" in candidate_lines[2]
    # The ask is present and explicitly disclaims any board mutation.
    assert "Cut / defer / edit" in block
    assert "won't change the board" in lines[-1]


def test_stale_threshold_is_strictly_more_than_knob():
    board = """# Work

## 🔴 Q1
- [ ] **Thirteen days** 🗓️2026-06-11 estimate:: 13h task_id::tsk_aaaaaaaaaaaaaaaa
- [ ] **Fifteen days** 🗓️2026-06-09 estimate:: 13h task_id::tsk_bbbbbbbbbbbbbbbb
"""
    block = pushback.capacity_pushback(_records(board), today=TODAY, stale_days=14)
    lines = block.splitlines()
    thirteen = next(ln for ln in lines if "Thirteen days" in ln)
    fifteen = next(ln for ln in lines if "Fifteen days" in ln)
    assert "13d overdue" in thirteen and "stale" not in thirteen  # 13 is not > 14
    assert "15d overdue" in fifteen and "stale" in fifteen        # 15 is > 14


def test_ties_broken_by_canonical_id():
    # Two undated active tasks -> deterministic order by canonical id.
    board = """# Work

## 🔴 Q1
- [ ] **Beta task** estimate:: 13h task_id::tsk_bbbbbbbbbbbbbbbb
- [ ] **Alpha task** estimate:: 13h task_id::tsk_aaaaaaaaaaaaaaaa
"""
    block = pushback.capacity_pushback(_records(board), today=TODAY)
    candidate_lines = [ln for ln in block.splitlines() if ln.startswith("  - ")]
    assert candidate_lines[0].index("tsk_aaaa") >= 0  # id-ascending: aaaa before bbbb
    assert "Alpha task" in candidate_lines[0]
    assert "Beta task" in candidate_lines[1]


def test_max_candidates_truncates_with_more_count():
    rows = "\n".join(
        f"- [ ] **Task {i}** 🗓️2026-05-0{i} estimate:: 4h task_id::tsk_{chr(97 + i) * 16}"
        for i in range(1, 8)
    )
    board = f"# Work\n\n## 🔴 Q1\n{rows}\n"
    block = pushback.capacity_pushback(_records(board), today=TODAY)
    candidate_lines = [ln for ln in block.splitlines() if ln.startswith("  - ")]
    # 7 active, capped at MAX_CANDIDATES shown + a "... and N more" line.
    assert len([ln for ln in candidate_lines if "more active" not in ln]) == pushback.MAX_CANDIDATES
    assert any("and 2 more active" in ln for ln in candidate_lines)


def test_fail_open_on_unparseable_board():
    # A garbage "board" must not raise; it degrades to no push-back.
    assert pushback.capacity_pushback("not a board", today=TODAY) is None  # type: ignore[arg-type]


def test_pure_read_does_not_mutate_records():
    records = _records(_OVER_CAP_BOARD)
    snapshot = [(r.task_id, r.title, r.due, r.estimate) for r in records]
    pushback.capacity_pushback(records, today=TODAY)
    after = [(r.task_id, r.title, r.due, r.estimate) for r in records]
    assert snapshot == after  # the engine reads; it never edits the board/records


def test_newline_in_title_is_collapsed_to_one_bullet():
    board = (
        "# Work\n\n## 🔴 Q1\n"
        "- [ ] **Ship the thing** 🗓️2026-05-01 estimate:: 13h task_id::tsk_aaaaaaaaaaaaaaaa\n"
        "- [ ] **Second big** 🗓️2026-05-02 estimate:: 13h task_id::tsk_bbbbbbbbbbbbbbbb\n"
    )
    records = _records(board)
    # Inject an embedded newline into a parsed title (a hostile board line).
    object.__setattr__(records[0], "title", "Ship the thing\n- [x] FORGED DONE")
    block = pushback.capacity_pushback(records, today=TODAY)
    bullet_lines = [ln for ln in block.splitlines() if ln.startswith("  - ")]
    # The forged second line must NOT appear as its own bullet.
    assert not any("FORGED DONE" in ln and not ln.startswith("  - Ship") for ln in bullet_lines)
    assert "FORGED DONE" not in block or "Ship the thing - [x] FORGED DONE" in block
