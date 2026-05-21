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
from evidence_matching import extract_inline_identifiers


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


def test_cmd_delegated_take_back_adds_canonical_task_id(tmp_path, monkeypatch):
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

    tasks.cmd_delegated(SimpleNamespace(del_command='take-back', id=1))

    assert re.search(r'\btask_id::tsk_[a-f0-9]{16}\b', tasks_file.read_text())


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
    identifiers = extract_inline_identifiers("Completed task_id::tsk_ship, and id::legacy-1)")

    assert "tsk_ship" in identifiers["exact"]
    assert "legacy-1" in identifiers["exact"]


def test_add_task_legacy_priority_section(tmp_path, monkeypatch, capsys):
    tasks_file = tmp_path / 'Work Tasks.md'
    tasks_file.write_text("""# Weekly TODOs

## 🔴 Q1
- [ ] **Existing high**

## 🟡 Q2
- [ ] **Existing medium**
""")
    monkeypatch.setattr(tasks, 'get_tasks_file', lambda personal=False: (tasks_file, 'obsidian'))

    args = SimpleNamespace(
        title='New medium task',
        priority='medium',
        due=None,
        area=None,
        owner='me',
        personal=False,
    )
    tasks.add_task(args)

    updated = tasks_file.read_text()
    assert re.search(
        r"## 🟡 Q2\n- \[ \] \*\*New medium task\*\* task_id::tsk_[a-f0-9]{16}\n- \[ \] \*\*Existing medium\*\*",
        updated,
    )
    assert "⚠️" not in capsys.readouterr().out


def test_add_task_fallback_to_all_tasks_department_section(tmp_path, monkeypatch, capsys):
    tasks_file = tmp_path / 'Work Tasks.md'
    tasks_file.write_text("""# Weekly TODOs — 2026-W08

## 📋 All Tasks
### 🚀 Sales #sales
- [ ] Sales existing

### 📣 Marketing #marketing
- [ ] Marketing existing

## 📋 Tasks Query
```tasks
not done
```
""")
    monkeypatch.setattr(tasks, 'get_tasks_file', lambda personal=False: (tasks_file, 'obsidian'))

    args = SimpleNamespace(
        title='Marketing follow-up',
        priority='medium',
        due='2026-02-20',
        area='Marketing',
        owner='me',
        personal=False,
    )
    tasks.add_task(args)

    updated = tasks_file.read_text()
    assert re.search(
        r"### 📣 Marketing #marketing\n- \[ \] \*\*Marketing follow-up\*\* 🗓️2026-02-20 task_id::tsk_[a-f0-9]{16} area:: Marketing\n- \[ \] Marketing existing",
        updated,
    )
    assert "## 📋 Tasks Query" in updated
    assert "⚠️" not in capsys.readouterr().out


def test_add_task_warns_when_no_anchor_found(tmp_path, monkeypatch, capsys):
    tasks_file = tmp_path / 'Work Tasks.md'
    tasks_file.write_text("# Weekly TODOs\n\nNo task sections yet.\n")
    monkeypatch.setattr(tasks, 'get_tasks_file', lambda personal=False: (tasks_file, 'obsidian'))

    args = SimpleNamespace(
        title='Orphan task',
        priority='high',
        due=None,
        area=None,
        owner='me',
        personal=False,
    )
    tasks.add_task(args)

    out = capsys.readouterr().out
    assert "⚠️ Could not find section matching" in out
    assert "Orphan task" not in tasks_file.read_text()
