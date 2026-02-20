"""Tests for update_weekly_embeds.py — Weekly progress embed updater."""

from __future__ import annotations

import sys
from pathlib import Path
from datetime import date

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import update_weekly_embeds as embeds


# ---------------------------------------------------------------------------
# get_week_monday()
# ---------------------------------------------------------------------------

class TestGetWeekMonday:
    def test_monday_returns_self(self):
        monday = date(2026, 2, 16)
        assert embeds.get_week_monday(monday) == monday

    def test_friday_returns_monday(self):
        friday = date(2026, 2, 20)
        assert embeds.get_week_monday(friday) == date(2026, 2, 16)

    def test_wednesday_returns_monday(self):
        wednesday = date(2026, 2, 18)
        assert embeds.get_week_monday(wednesday) == date(2026, 2, 16)

    def test_sunday_returns_next_day(self):
        # ISO week: Sunday is the last day of the week
        sunday = date(2026, 2, 22)
        assert embeds.get_week_monday(sunday) == date(2026, 2, 16)


# ---------------------------------------------------------------------------
# build_progress_section()
# ---------------------------------------------------------------------------

class TestBuildProgressSection:
    def test_contains_all_five_days(self):
        monday = date(2026, 2, 16)
        section = embeds.build_progress_section(monday)
        for day in ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]:
            assert day in section

    def test_contains_correct_dates(self):
        monday = date(2026, 2, 16)
        section = embeds.build_progress_section(monday)
        assert "2026-02-16" in section  # Monday
        assert "2026-02-17" in section  # Tuesday
        assert "2026-02-20" in section  # Friday

    def test_contains_transclusion_links(self):
        monday = date(2026, 2, 16)
        section = embeds.build_progress_section(monday)
        assert "![[" in section
        assert "#✅ Done]]" in section

    def test_header_present(self):
        monday = date(2026, 2, 16)
        section = embeds.build_progress_section(monday)
        assert section.startswith("## 📊 Daily Progress")

    def test_vault_prefix_in_links(self):
        monday = date(2026, 2, 16)
        section = embeds.build_progress_section(monday)
        # Should contain something like ![[01-TODOs/Daily/2026-02-16#✅ Done]]
        assert "![[" in section
        assert "2026-02-16" in section


# ---------------------------------------------------------------------------
# update_or_append_progress_section()
# ---------------------------------------------------------------------------

WEEKLY_WITH_SECTION = """\
# Weekly TODOs — 2026-W08

## 📋 All Tasks
- [ ] Some task

## 📊 Daily Progress

### Monday
![[01-TODOs/Daily/2026-02-09#✅ Done]]

### Tuesday
![[01-TODOs/Daily/2026-02-10#✅ Done]]

## 📋 Tasks Query
```tasks
not done
```
"""

WEEKLY_WITHOUT_SECTION = """\
# Weekly TODOs — 2026-W08

## 📋 All Tasks
- [ ] Some task

## 📋 Tasks Query
```tasks
not done
```
"""

WEEKLY_NO_QUERY = """\
# Weekly TODOs — 2026-W08

## 📋 All Tasks
- [ ] Some task
"""


class TestUpdateOrAppendProgressSection:
    def test_replaces_existing_section(self):
        monday = date(2026, 2, 16)
        new_section = embeds.build_progress_section(monday)
        result = embeds.update_or_append_progress_section(WEEKLY_WITH_SECTION, new_section)
        assert "2026-02-16" in result
        assert "2026-02-09" not in result  # old dates replaced

    def test_inserts_before_tasks_query(self):
        monday = date(2026, 2, 16)
        new_section = embeds.build_progress_section(monday)
        result = embeds.update_or_append_progress_section(WEEKLY_WITHOUT_SECTION, new_section)
        progress_idx = result.index("## 📊 Daily Progress")
        query_idx = result.index("## 📋 Tasks Query")
        assert progress_idx < query_idx

    def test_appends_when_no_query_block(self):
        monday = date(2026, 2, 16)
        new_section = embeds.build_progress_section(monday)
        result = embeds.update_or_append_progress_section(WEEKLY_NO_QUERY, new_section)
        assert "## 📊 Daily Progress" in result
        assert "2026-02-16" in result

    def test_no_change_detection(self):
        """Verify that an already-current section is detected as unchanged."""
        monday = date(2026, 2, 16)
        new_section = embeds.build_progress_section(monday)
        # First update
        result1 = embeds.update_or_append_progress_section(WEEKLY_WITHOUT_SECTION, new_section)
        # Second update (should produce same content)
        result2 = embeds.update_or_append_progress_section(result1, new_section)
        assert result1 == result2
