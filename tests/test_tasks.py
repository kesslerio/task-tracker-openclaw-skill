"""Tests for tasks CLI command handlers."""

from pathlib import Path
from types import SimpleNamespace

import pytest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))

import tasks


def test_cmd_delegated_take_back_write_failure_keeps_delegated_item(tmp_path, monkeypatch):
    delegation_file = tmp_path / 'Delegated.md'
    delegation_file.write_text("""# Delegated Tasks

## Active
- [ ] **Check merch delivery** → Alex [delegated::2026-02-10] [followup::2026-02-17] #Ops

## Awaiting Follow-up

## Completed
""")
    tasks_file = tmp_path / 'Work Tasks.md'
    tasks_file.write_text("""# Weekly Objectives

## Objectives
""")

    monkeypatch.setenv('TASK_TRACKER_DELEGATION_FILE', str(delegation_file))
    monkeypatch.setattr(tasks, 'get_tasks_file', lambda personal=False: (tasks_file, 'markdown'))

    original_write_text = Path.write_text

    def fail_task_file_write(path_obj, content, *args, **kwargs):
        if path_obj == tasks_file:
            raise OSError('simulated write failure')
        return original_write_text(path_obj, content, *args, **kwargs)

    monkeypatch.setattr(Path, 'write_text', fail_task_file_write)

    with pytest.raises(OSError, match='simulated write failure'):
        tasks.cmd_delegated(SimpleNamespace(del_command='take-back', id=1))

    assert 'Check merch delivery' in delegation_file.read_text()
