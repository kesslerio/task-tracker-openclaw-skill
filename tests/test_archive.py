#!/usr/bin/env python3
"""Tests for archive operations."""

from datetime import date
from pathlib import Path
import pytest
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import archive
from archive import archive_week, get_archive_dir, consolidate_month, archive_stats


def test_archive_week_no_completed(tmp_path):
    """Test archive_week with no completed tasks."""
    objectives = tmp_path / "objectives.md"
    objectives.write_text("""# Objectives 2026

## HR/People

### [ ] Hire Senior Engineer
- [ ] Draft job description
""")
    result = archive_week(tasks_file=objectives, personal=False)
    assert result['archived'] == 0


def test_get_archive_dir(tmp_path, monkeypatch):
    """Test archive directory resolution."""
    tasks_file = tmp_path / "objectives.md"
    monkeypatch.delenv("TASK_TRACKER_ARCHIVE_DIR", raising=False)
    archive_dir = get_archive_dir(tasks_file)
    assert archive_dir == tmp_path / "Done Archive"
    
    custom_dir = tmp_path / "custom-archive"
    monkeypatch.setenv("TASK_TRACKER_ARCHIVE_DIR", str(custom_dir))
    archive_dir = get_archive_dir(tasks_file)
    assert archive_dir == custom_dir


def test_consolidate_month_includes_previous_iso_year_week(tmp_path):
    archive_dir = tmp_path / "Done Archive"
    archive_dir.mkdir()
    (archive_dir / "2020-W53.md").write_text("""# Done Archive — Week of Dec 28, 2020 (W53)

## Dev
- [x] Cross-year item ✅ 2021-01-02
""")
    (archive_dir / "2021-W01.md").write_text("""# Done Archive — Week of Jan 04, 2021 (W01)

## Dev
- [x] January item ✅ 2021-01-06
""")

    result = consolidate_month(archive_dir, "2021-01")
    assert "error" not in result
    weekly_names = {f.name for f in result["weekly_files"]}
    assert "2020-W53.md" in weekly_names
    assert "2021-W01.md" in weekly_names

    monthly_content = result["monthly_file"].read_text()
    assert "Cross-year item" in monthly_content
    assert "January item" in monthly_content


def test_archive_stats_ignores_monthly_consolidation_files(tmp_path, monkeypatch):
    class FakeDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 2, 15)

    monkeypatch.setattr(archive, "date", FakeDate)

    archive_dir = tmp_path / "Done Archive"
    archive_dir.mkdir()
    (archive_dir / "2026-W07.md").write_text("""# Done Archive — Week of Feb 09, 2026 (W07)

## Dev
- [x] Weekly item ✅ 2026-02-10
""")
    (archive_dir / "2026-02-monthly.md").write_text("""# Done Archive — February 2026

## Dev
- [x] Weekly item ✅ 2026-02-10
""")

    stats = archive_stats(archive_dir, "month")
    assert stats["total"] == 1
    assert stats["by_department"] == {"Dev": 1}
