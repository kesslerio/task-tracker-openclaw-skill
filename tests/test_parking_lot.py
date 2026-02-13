"""Tests for parking lot operations."""

import json
import os
import tempfile
from datetime import date, timedelta
from pathlib import Path

import pytest

# Allow imports from scripts/
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))

import parking_lot
from parking_lot import (
    _find_parking_lot_bounds,
    _parse_items,
    _is_stale,
    list_items,
    list_stale,
    add_item,
    promote_item,
    drop_item,
)


SAMPLE_CONTENT = """\
# Weekly Objectives

## Objectives

- [ ] **Launch V2** #Dev #high
  - [ ] Ship API endpoint
  - [ ] Write docs

## 🅿️ Parking Lot

- [ ] **Set up webhook integration** #Dev created::2026-01-01
- [ ] **Review pricing page** #Marketing created::{recent}
- [ ] **Notion integration research** #Dev #medium created::2025-12-01

## ✅ Done

- [x] **Old task**
""".replace('{recent}', date.today().isoformat())


@pytest.fixture
def tasks_file(tmp_path):
    f = tmp_path / 'Work Tasks.md'
    f.write_text(SAMPLE_CONTENT)
    return f


def test_find_parking_lot_bounds():
    lines = SAMPLE_CONTENT.split('\n')
    start, end = _find_parking_lot_bounds(lines)
    assert start >= 0
    assert lines[start].startswith('## 🅿️ Parking Lot')
    assert end > start
    # Next section is ## ✅ Done
    assert lines[end].startswith('## ✅ Done')


def test_parse_items():
    lines = SAMPLE_CONTENT.split('\n')
    start, end = _find_parking_lot_bounds(lines)
    items = _parse_items(lines, start, end)
    assert len(items) == 3
    assert items[0]['title'] == 'Set up webhook integration'
    assert items[0]['department'] == 'Dev'
    assert items[0]['created'] == '2026-01-01'
    assert items[1]['title'] == 'Review pricing page'
    assert items[1]['department'] == 'Marketing'
    assert items[2]['priority'] == 'medium'


def test_is_stale():
    old_date = (date.today() - timedelta(days=45)).isoformat()
    recent_date = date.today().isoformat()
    assert _is_stale({'created': old_date}) is True
    assert _is_stale({'created': recent_date}) is False
    assert _is_stale({}) is False


def test_list_items(tasks_file):
    output = list_items(tasks_file)
    assert '1. Set up webhook integration' in output
    assert '2. Review pricing page' in output
    assert '3. Notion integration research' in output
    assert '/25 items' in output
    assert 'STALE' in output  # old items should be marked stale


def test_list_stale(tasks_file):
    result = json.loads(list_stale(tasks_file))
    # At least the 2026-01-01 and 2025-12-01 items should be stale
    stale_titles = [it['title'] for it in result]
    assert 'Set up webhook integration' in stale_titles
    assert 'Notion integration research' in stale_titles
    # Recent item should NOT be stale
    assert 'Review pricing page' not in stale_titles


def test_add_item(tasks_file):
    result = add_item(tasks_file, 'New backlog task', dept='Sales', priority='low')
    assert '✅' in result
    assert '4/25' in result

    content = tasks_file.read_text()
    assert '**New backlog task**' in content
    assert '#Sales' in content
    assert f'created::{date.today().isoformat()}' in content


def test_add_item_respects_cap(tasks_file):
    os.environ['PARKING_LOT_CAP'] = '3'
    try:
        result = add_item(tasks_file, 'Should fail')
        assert '❌' in result
        assert 'full' in result.lower()
    finally:
        del os.environ['PARKING_LOT_CAP']


def test_promote_item(tasks_file):
    result = promote_item(tasks_file, 1)
    assert '✅' in result
    assert 'Set up webhook integration' in result

    content = tasks_file.read_text()
    # Should be removed from parking lot
    lines = content.split('\n')
    start, end = _find_parking_lot_bounds(lines)
    items = _parse_items(lines, start, end)
    titles = [it['title'] for it in items]
    assert 'Set up webhook integration' not in titles

    # Should appear in objectives section (before parking lot)
    pl_start = content.index('## 🅿️ Parking Lot')
    assert '**Set up webhook integration**' in content[:pl_start]
    # created:: field should be stripped
    obj_section = content[:pl_start]
    assert 'created::' not in obj_section or 'Set up webhook' not in obj_section.split('created::')[0]


def test_drop_item(tasks_file, tmp_path):
    archive_dir = tmp_path / 'Done Archive'
    result = drop_item(tasks_file, 2, archive_dir=archive_dir)
    assert '✅' in result
    assert 'Review pricing page' in result

    # Removed from parking lot
    content = tasks_file.read_text()
    assert 'Review pricing page' not in content

    # Archived
    archives = list(archive_dir.iterdir())
    assert len(archives) == 1
    arc_content = archives[0].read_text()
    assert 'Review pricing page' in arc_content
    assert '(dropped)' in arc_content
    assert '## Marketing' in arc_content


def test_drop_item_uses_iso_year_for_archive_filename(tmp_path, monkeypatch):
    class FakeDate(date):
        @classmethod
        def today(cls):
            return cls(2021, 1, 1)

    monkeypatch.setattr(parking_lot, 'date', FakeDate)

    f = tmp_path / 'Work Tasks.md'
    f.write_text("""# Weekly Objectives

## Objectives

## 🅿️ Parking Lot

- [ ] **Boundary task** #Ops created::2020-12-31

## ✅ Done
""")
    archive_dir = tmp_path / 'Done Archive'
    result = drop_item(f, 1, archive_dir=archive_dir)
    assert '✅' in result
    assert (archive_dir / '2020-W53.md').exists()
    assert not (archive_dir / '2021-W53.md').exists()


def test_drop_item_removes_child_lines(tmp_path):
    f = tmp_path / 'Work Tasks.md'
    f.write_text("""# Weekly Objectives

## Objectives

## 🅿️ Parking Lot

- [ ] **Parent task** #Dev created::2026-01-01
  - child note that must be removed
  - another child line
- [ ] **Another task** #Ops created::2026-01-02

## ✅ Done
""")
    result = drop_item(f, 1)
    assert '✅' in result

    content = f.read_text()
    assert 'Parent task' not in content
    assert 'child note that must be removed' not in content
    assert 'another child line' not in content
    assert 'Another task' in content


def test_promote_item_moves_child_lines_with_parent(tmp_path):
    f = tmp_path / 'Work Tasks.md'
    f.write_text("""# Weekly Objectives

## Objectives

## 🅿️ Parking Lot

- [ ] **Parent task** #Dev created::2026-01-01
  - child note that must move
  - second child line
- [ ] **Another task** #Ops created::2026-01-02

## ✅ Done
""")
    result = promote_item(f, 1)
    assert '✅' in result

    content = f.read_text()
    pl_start = content.index('## 🅿️ Parking Lot')
    objectives_part = content[:pl_start]
    parking_lot_part = content[pl_start:]

    assert '**Parent task**' in objectives_part
    assert 'child note that must move' in objectives_part
    assert 'second child line' in objectives_part
    assert 'Parent task' not in parking_lot_part
    assert 'child note that must move' not in parking_lot_part


def test_drop_nonexistent_item(tasks_file):
    result = drop_item(tasks_file, 99)
    assert '❌' in result


def test_promote_nonexistent_item(tasks_file):
    result = promote_item(tasks_file, 99)
    assert '❌' in result


def test_no_parking_lot_section(tmp_path):
    f = tmp_path / 'tasks.md'
    f.write_text('# Tasks\n\n- [ ] **Task 1**\n')
    assert 'No Parking Lot' in list_items(f)
    assert '❌' in add_item(f, 'test')
