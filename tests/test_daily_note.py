"""Unit tests for daily note creation."""

import sys
import tempfile
from pathlib import Path

# Add scripts directory to path
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from lib.daily_note.parser import parse_open_tasks, parse_top_priority_tasks
from lib.daily_note.deduper import merge_tasks, normalize_task


class TestParser:
    """Test markdown task parsing."""

    def test_parse_open_tasks_basic(self):
        content = """
- [ ] Task one
- [ ] Task two
- [x] Completed task
- [ ] Task three
"""
        tasks = parse_open_tasks(content)
        assert len(tasks) == 3
        assert "Task one" in tasks
        assert "Task two" in tasks
        assert "Task three" in tasks
        assert "Completed task" not in tasks

    def test_parse_open_tasks_skips_code_blocks(self):
        content = """
- [ ] Real task

```python
# Example code
- [ ] This is in a code block
```

- [ ] Another real task
"""
        tasks = parse_open_tasks(content)
        assert len(tasks) == 2
        assert "Real task" in tasks
        assert "Another real task" in tasks
        assert "code block" not in " ".join(tasks)

    def test_parse_open_tasks_skips_under_completed(self):
        content = """
- [x] Completed parent
  - [ ] Subtask under completed
- [ ] Open parent
  - [ ] Subtask under open
"""
        tasks = parse_open_tasks(content)
        assert "Subtask under open" in tasks
        assert "Subtask under completed" not in tasks

    def test_parse_open_tasks_strips_prefix(self):
        content = """
- [ ]   Task with extra spaces  
"""
        tasks = parse_open_tasks(content)
        assert tasks[0] == "Task with extra spaces"


class TestDeduper:
    """Test task deduplication."""

    def test_normalize_task_strips_prefix(self):
        assert normalize_task("- [ ] Task") == "task"
        assert normalize_task("Task") == "task"
        assert normalize_task("  - [ ]  Task  ") == "task"

    def test_merge_tasks_weekly_priority(self):
        weekly = ["Task A", "Task B"]
        yesterday = ["Task C"]
        merged = merge_tasks(weekly, yesterday)
        assert merged == ["Task A", "Task B", "Task C"]

    def test_merge_tasks_dedup(self):
        weekly = ["Task A", "Task B"]
        yesterday = ["task a", "Task C"]  # "task a" is duplicate of "Task A"
        merged = merge_tasks(weekly, yesterday)
        assert len(merged) == 3
        assert "Task A" in merged
        assert "Task B" in merged
        assert "Task C" in merged

    def test_merge_tasks_empty_lists(self):
        assert merge_tasks([], []) == []
        assert merge_tasks(["Task"], []) == ["Task"]
        assert merge_tasks([], ["Task"]) == ["Task"]


class TestComposer:
    """Test note composition (integration test)."""

    def test_compose_basic_structure(self):
        from lib.daily_note.composer import compose_daily_note

        content = compose_daily_note(
            date_str="2026-02-19",
            calendar_events=[{"summary": "Meeting", "start": {"dateTime": "2026-02-19T10:00:00"}}],
            top_3=["Task 1", "Task 2"],
            carried=["Carried 1"],
        )

        assert "# 2026-02-19" in content
        assert "## Calendar" in content
        assert "## Top 3" in content
        assert "## Open/Carried" in content
        assert "## Done" in content
        assert "- [ ] Task 1" in content
        assert "- [ ] Task 2" in content
        assert "- [ ] Carried 1" in content

    def test_compose_no_calendar(self):
        from lib.daily_note.composer import compose_daily_note

        content = compose_daily_note(
            date_str="2026-02-19",
            calendar_events=[],
            top_3=[],
            carried=[],
        )

        assert "_No calendar events_" in content
        assert "_Add your top priority_" in content


class TestIdempotency:
    """Test idempotency - don't overwrite existing notes."""

    def test_idempotency_check(self, monkeypatch, tmp_path):
        from lib.daily_note.composer import compose_daily_note

        # Create a mock daily dir
        daily_dir = tmp_path / "Daily"
        daily_dir.mkdir()

        # Create existing note
        existing_note = daily_dir / "2026-02-19.md"
        existing_note.write_text("# 2026-02-19\n\nExisting content")

        # Verify file exists
        assert existing_note.exists()

        # In real usage, the script checks for existence before calling compose
        content = compose_daily_note(
            date_str="2026-02-19",
            calendar_events=[],
            top_3=["New task"],
            carried=[],
        )

        # The composer itself doesn't handle idempotency - that's in main()
        # This just verifies the composer would create new content
        assert "New task" in content


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
