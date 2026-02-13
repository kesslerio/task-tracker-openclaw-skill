"""Tests for delegation file operations."""

import json
import os
from datetime import date, timedelta
from pathlib import Path

import pytest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))

from delegation import (
    resolve_delegation_file,
    ensure_file,
    list_items,
    list_items_json,
    add_item,
    complete_item,
    extend_item,
    take_back_item,
    _is_overdue,
    _find_section,
)


SAMPLE_DELEGATION = """\
# Delegated Tasks

## Active
- [ ] **Check merch delivery** → Alex [delegated::2026-02-10] [followup::{overdue_date}] #Ops
- [ ] **CRM research** → Lilla [delegated::2026-02-11] [followup::{future_date}] #Dev

## Awaiting Follow-up

## Completed
- [x] **Reschedule demos** → Lilla [delegated::2026-01-25] [completed::2026-01-26] #Sales
"""


@pytest.fixture
def delegation_file(tmp_path):
    overdue = (date.today() - timedelta(days=3)).isoformat()
    future = (date.today() + timedelta(days=3)).isoformat()
    content = SAMPLE_DELEGATION.replace('{overdue_date}', overdue).replace('{future_date}', future)
    f = tmp_path / 'Delegated.md'
    f.write_text(content)
    return f


def test_ensure_file_creates_template(tmp_path):
    f = tmp_path / 'Delegated.md'
    assert not f.exists()
    ensure_file(f)
    assert f.exists()
    content = f.read_text()
    assert '## Active' in content
    assert '## Completed' in content


def test_list_items(delegation_file):
    items = list_items(delegation_file)
    assert len(items) == 2
    assert items[0]['title'] == 'Check merch delivery'
    assert items[0]['assignee'] == 'Alex'
    assert items[0]['department'] == 'Ops'
    assert items[1]['title'] == 'CRM research'
    assert items[1]['assignee'] == 'Lilla'


def test_list_items_overdue_only(delegation_file):
    items = list_items(delegation_file, overdue_only=True)
    assert len(items) == 1
    assert items[0]['title'] == 'Check merch delivery'


def test_list_items_json(delegation_file):
    result = json.loads(list_items_json(delegation_file))
    assert len(result) == 2
    overdue_item = next(it for it in result if it['title'] == 'Check merch delivery')
    assert overdue_item['overdue'] is True


def test_add_item(delegation_file):
    item = add_item(delegation_file, 'New task', 'Martin', '2026-03-01', 'Sales')
    assert item['title'] == 'New task'
    assert item['assignee'] == 'Martin'

    content = delegation_file.read_text()
    assert '**New task** → Martin' in content
    assert '#Sales' in content


def test_complete_item(delegation_file):
    item = complete_item(delegation_file, 1)
    assert item['title'] == 'Check merch delivery'

    content = delegation_file.read_text()
    # Should be in Completed section, not Active
    lines = content.split('\n')
    active_start, active_end = _find_section(lines, 'Active')
    active_text = '\n'.join(lines[active_start:active_end])
    assert 'Check merch delivery' not in active_text

    comp_start, comp_end = _find_section(lines, 'Completed')
    comp_text = '\n'.join(lines[comp_start:comp_end])
    assert 'Check merch delivery' in comp_text
    assert f'completed::{date.today().isoformat()}' in comp_text


def test_extend_item(delegation_file):
    new_date = (date.today() + timedelta(days=10)).isoformat()
    item = extend_item(delegation_file, 2, new_date)
    assert item['followup'] == new_date

    content = delegation_file.read_text()
    assert f'followup::{new_date}' in content


def test_take_back_item(delegation_file):
    item = take_back_item(delegation_file, 1)
    assert item['title'] == 'Check merch delivery'

    content = delegation_file.read_text()
    assert 'Check merch delivery' not in content


def test_complete_nonexistent_raises(delegation_file):
    with pytest.raises(ValueError, match='not found'):
        complete_item(delegation_file, 99)


def test_is_overdue():
    past = (date.today() - timedelta(days=1)).isoformat()
    future = (date.today() + timedelta(days=1)).isoformat()
    assert _is_overdue({'followup': past}) is True
    assert _is_overdue({'followup': future}) is False
    assert _is_overdue({}) is False
