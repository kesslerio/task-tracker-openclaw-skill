"""Tests for tasks CLI command handlers."""

from pathlib import Path
from types import SimpleNamespace
import os
import re
import subprocess

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


def test_add_command_emits_canonical_task_id(tmp_path):
    tasks_file = tmp_path / "Work Tasks.md"
    tasks_file.write_text("# Work\n\n## 🟡 Q2\n")
    env = os.environ.copy()
    env["TASK_TRACKER_WORK_FILE"] = str(tasks_file)

    proc = subprocess.run(
        ["python3", "scripts/tasks.py", "add", "New task"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert proc.returncode == 0
    content = tasks_file.read_text()
    match = re.search(r"task_id::(tsk_[a-f0-9]{16})", content)
    assert match
    assert match.group(1) in proc.stdout


def test_extract_inline_identifiers_accepts_trailing_punctuation():
    identifiers = tasks._extract_inline_identifiers("Completed task_id::tsk_ship, and id::legacy-1)")

    assert "tsk_ship" in identifiers["exact"]
    assert "legacy-1" in identifiers["exact"]
