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
    assert "## 🟡 Q2\n- [ ] **New medium task**\n- [ ] **Existing medium**" in updated
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
    assert "### 📣 Marketing #marketing\n- [ ] **Marketing follow-up** 🗓️2026-02-20 area:: Marketing\n- [ ] Marketing existing" in updated
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
