from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from weekly_review import _clean_stale_done_lines


def test_clean_stale_done_lines_removes_parent_and_children(tmp_path):
    tasks_file = tmp_path / "Work Tasks.md"
    tasks_file.write_text("""# Work

## 🔴 Q1
- [x] **Completed parent** task_id::tsk_done
  - [ ] Child note
- [ ] **Active sibling** task_id::tsk_active
""")

    removed = _clean_stale_done_lines(
        tasks_file,
        [
            {
                "raw_line": "- [x] **Completed parent** task_id::tsk_done",
                "line_number": 4,
            }
        ],
    )

    content = tasks_file.read_text()
    assert removed == 1
    assert "Completed parent" not in content
    assert "Child note" not in content
    assert "Active sibling" in content


def test_clean_stale_done_lines_handles_tab_indented_children(tmp_path):
    tasks_file = tmp_path / "Work Tasks.md"
    tasks_file.write_text("""# Work

## 🔴 Q1
- [x] **Completed parent** task_id::tsk_done
\t- [ ] Tab child
- [ ] **Active sibling** task_id::tsk_active
""")

    removed = _clean_stale_done_lines(
        tasks_file,
        [
            {
                "raw_line": "- [x] **Completed parent** task_id::tsk_done",
                "line_number": 4,
            }
        ],
    )

    content = tasks_file.read_text()
    assert removed == 1
    assert "Completed parent" not in content
    assert "Tab child" not in content
    assert "Active sibling" in content


def test_clean_stale_done_lines_refuses_shifted_duplicate_raw_line(tmp_path):
    tasks_file = tmp_path / "Work Tasks.md"
    duplicate_line = "- [x] **Duplicate done** task_id::tsk_done"
    original = f"""# Work

## 🔴 Q1
{duplicate_line}
- [ ] **Inserted line changed numbering** task_id::tsk_inserted
{duplicate_line}
"""
    tasks_file.write_text(original)

    removed = _clean_stale_done_lines(
        tasks_file,
        [
            {
                "raw_line": duplicate_line,
                "line_number": 5,
            }
        ],
    )

    assert removed == 0
    assert tasks_file.read_text() == original
