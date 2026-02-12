from datetime import date
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from daily_notes import _clean_action_line, extract_completed_tasks


def test_clean_action_line_strips_done_date_stamp():
    line = "- [x] Review Fizzi leads ✅ 2026-02-11"
    assert _clean_action_line(line) == "Review Fizzi leads"


def test_clean_action_line_strips_trailing_checkbox_artifacts():
    line = "- [x] Review Fizzi leads ✅ 2026-02-11- [X]   "
    assert _clean_action_line(line) == "Review Fizzi leads"


def test_extract_completed_tasks_returns_clean_titles(tmp_path):
    note = tmp_path / "2026-02-11.md"
    note.write_text(
        "- [x] Review Fizzi leads ✅ 2026-02-11\n"
        "- [x] Fix parser ✅ 2026-02-11- [ ]\n"
        "- [ ] Leave unchanged\n"
    )

    tasks = extract_completed_tasks(
        notes_dir=tmp_path,
        start_date=date(2026, 2, 11),
        end_date=date(2026, 2, 11),
    )

    assert [task["title"] for task in tasks] == [
        "Review Fizzi leads",
        "Fix parser",
    ]


def test_extract_completed_tasks_deduplicates_by_title_and_date(tmp_path):
    (tmp_path / "2026-02-11.md").write_text(
        "- [x] Ship release ✅ 2026-02-11\n"
        "- [x] ship release ✅ 2026-02-11\n"
        "- [x] Ship release ✅ 2026-02-11- [x]\n"
    )
    (tmp_path / "2026-02-12.md").write_text(
        "- [x] Ship release ✅ 2026-02-12\n"
    )

    tasks = extract_completed_tasks(
        notes_dir=tmp_path,
        start_date=date(2026, 2, 11),
        end_date=date(2026, 2, 12),
    )

    assert [(task["title"], task["completed_date"]) for task in tasks] == [
        ("Ship release", "2026-02-11"),
        ("Ship release", "2026-02-12"),
    ]
