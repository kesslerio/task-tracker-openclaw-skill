#!/usr/bin/env python3
"""Tests for archive operations."""

from pathlib import Path
import pytest
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from archive import archive_week, get_archive_dir


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
