from pathlib import Path
from datetime import date

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import weekly_review
from weekly_review import _clean_stale_done_lines, generate_weekly_review, parse_iso_week


def _task(title, completed_date=None, *, area="Ops", done=True):
    return {
        "title": title,
        "completed_date": completed_date,
        "area": area,
        "done": done,
        "due": None,
        "type": None,
    }


def _tasks_data(done=None, open_tasks=None):
    done = done or []
    open_tasks = open_tasks or []
    return {
        "done": done,
        "all": [*done, *open_tasks],
        "q1": [],
        "q2": [],
        "q3": [],
        "team": [],
        "backlog": [],
        "objectives": [],
        "today": [],
        "parking_lot": [],
        "due_today": [],
    }


def _note(notes_dir: Path, day: str, *titles: str) -> None:
    lines = [f"- 09:{idx:02d} ✅ {title}" for idx, title in enumerate(titles)]
    (notes_dir / f"{day}.md").write_text("\n".join(lines))


def _configure_review(monkeypatch, tmp_path, tasks_data, notes_dir=None):
    monkeypatch.setattr(weekly_review, "load_tasks", lambda: ("", tasks_data))
    monkeypatch.setattr(weekly_review, "get_missed_tasks_bucketed", lambda *_a, **_k: {})
    monkeypatch.setattr(
        weekly_review,
        "candidate_review_summary",
        lambda limit=5: {"available": True, "total": 0, "items": []},
    )
    monkeypatch.setattr(
        weekly_review,
        "task_audit_summary",
        lambda limit=5: {"available": True, "total": 0, "items": []},
    )
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    monkeypatch.setattr(weekly_review, "ARCHIVE_DIR", archive_dir)
    if notes_dir:
        monkeypatch.setenv("TASK_TRACKER_DAILY_NOTES_DIR", str(notes_dir))
    else:
        monkeypatch.delenv("TASK_TRACKER_DAILY_NOTES_DIR", raising=False)


def test_parse_iso_week_defaults_to_previous_completed_week_on_monday():
    week_start, week_end = parse_iso_week(None, today=date(2026, 6, 29))

    assert week_start == date(2026, 6, 22)
    assert week_end == date(2026, 6, 28)


def test_parse_iso_week_this_week_selects_current_week():
    week_start, week_end = parse_iso_week(
        None,
        this_week=True,
        today=date(2026, 6, 29),
    )

    assert week_start == date(2026, 6, 29)
    assert week_end == date(2026, 7, 5)


def test_parse_iso_week_explicit_week_resolves_window():
    week_start, week_end = parse_iso_week("2026-W26", today=date(2026, 6, 29))

    assert week_start == date(2026, 6, 22)
    assert week_end == date(2026, 6, 28)


def test_parse_iso_week_default_crosses_iso_year_boundary():
    week_start, week_end = parse_iso_week(None, today=date(2026, 1, 1))

    assert week_start == date(2025, 12, 22)
    assert week_end == date(2025, 12, 28)


def test_completed_list_and_velocity_use_same_windowed_completion_set(
    tmp_path,
    monkeypatch,
):
    notes_dir = tmp_path / "daily"
    notes_dir.mkdir()
    _note(notes_dir, "2026-06-23", "Daily note completion")
    tasks_data = _tasks_data(done=[
        _task("Board completion in window", "2026-06-24"),
        _task("Board completion outside window", "2026-06-29"),
    ])
    _configure_review(monkeypatch, tmp_path, tasks_data, notes_dir)

    output = generate_weekly_review(
        week="2026-W26",
        today=date(2026, 6, 29),
    )

    assert "✅ **Completed This Week** (2)" in output
    assert "Daily note completion" in output
    assert "Board completion in window" in output
    assert "Board completion outside window" not in output
    assert "  Completed: 2 tasks" in output
    assert "Added:" not in output
    assert "Net:" not in output


def test_board_done_without_completion_date_is_not_attributed_to_window(
    tmp_path,
    monkeypatch,
):
    tasks_data = _tasks_data(done=[_task("Undated board completion")])
    _configure_review(monkeypatch, tmp_path, tasks_data)

    output = generate_weekly_review(
        week="2026-W26",
        today=date(2026, 6, 29),
    )

    assert "✅ **Completed This Week** (0)" in output
    assert "Undated board completion" not in output
    assert "  Completed: 0 tasks" in output
    assert "Board done items without completion dates: 1" in output


def test_empty_week_reports_zero_completed_and_zero_velocity(tmp_path, monkeypatch):
    tasks_data = _tasks_data(done=[
        _task(f"Undated board completion {idx}") for idx in range(6)
    ])
    _configure_review(monkeypatch, tmp_path, tasks_data)

    output = generate_weekly_review(
        week="2026-W26",
        today=date(2026, 6, 29),
    )

    assert "✅ **Completed This Week** (0)" in output
    assert "  Completed: 0 tasks" in output
    assert "Undated board completion 0" not in output
    assert "Added:" not in output
    assert "Net:" not in output


def test_coverage_warning_when_prior_week_daily_completions_exist(
    tmp_path,
    monkeypatch,
):
    notes_dir = tmp_path / "daily"
    notes_dir.mkdir()
    _note(notes_dir, "2026-06-23", "Prior one", "Prior two")
    _configure_review(monkeypatch, tmp_path, _tasks_data(), notes_dir)

    output = generate_weekly_review(
        week="2026-W27",
        today=date(2026, 6, 29),
    )

    assert "⚠️ **Coverage Warning**" in output
    assert "prior-week daily-note completions: 2" in output


def test_coverage_warning_when_rolling_7_day_count_exceeds_window(
    tmp_path,
    monkeypatch,
):
    tasks_data = _tasks_data(done=[
        _task("Recent board one", "2026-06-25"),
        _task("Recent board two", "2026-06-26"),
    ])
    _configure_review(monkeypatch, tmp_path, tasks_data)

    output = generate_weekly_review(
        week="2026-W20",
        today=date(2026, 6, 29),
    )

    assert "⚠️ **Coverage Warning**" in output
    assert "rolling-7-day completions: 2" in output


def test_coverage_warning_when_board_done_items_lack_completion_dates(
    tmp_path,
    monkeypatch,
):
    tasks_data = _tasks_data(done=[_task("Undated board completion")])
    _configure_review(monkeypatch, tmp_path, tasks_data)

    output = generate_weekly_review(
        week="2026-W26",
        today=date(2026, 6, 29),
    )

    assert "⚠️ **Coverage Warning**" in output
    assert "Board done items without completion dates: 1" in output


def test_no_coverage_warning_when_sources_agree(tmp_path, monkeypatch):
    notes_dir = tmp_path / "daily"
    notes_dir.mkdir()
    _note(notes_dir, "2026-06-23", "Daily note completion")
    tasks_data = _tasks_data(done=[_task("Board completion in window", "2026-06-24")])
    _configure_review(monkeypatch, tmp_path, tasks_data, notes_dir)

    output = generate_weekly_review(
        week="2026-W26",
        today=date(2026, 6, 29),
    )

    assert "✅ **Completed This Week** (2)" in output
    assert "  Completed: 2 tasks" in output
    assert "⚠️ **Coverage Warning**" not in output


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
