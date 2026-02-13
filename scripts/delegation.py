#!/usr/bin/env python3
"""
Delegation file operations for the task tracker.

Manages a separate Delegated.md file with sections:
  ## Active — open delegated items
  ## Awaiting Follow-up — items past follow-up date (auto-populated)
  ## Completed — done items

Operations: list, add, complete, extend, take-back.
"""

import json
import os
import re
import tempfile
from datetime import datetime, date
from pathlib import Path


_DEFAULT_TEMPLATE = """\
# Delegated Tasks

## Active

## Awaiting Follow-up

## Completed
"""


def resolve_delegation_file() -> Path:
    """Resolve delegation file path from env or default."""
    explicit = os.getenv('TASK_TRACKER_DELEGATION_FILE')
    if explicit:
        return Path(explicit)
    # Default: same directory as work tasks file
    from utils import get_tasks_file
    work_file, _ = get_tasks_file(personal=False)
    return work_file.parent / 'Delegated.md'


def ensure_file(path: Path) -> None:
    """Create delegation file from template if it doesn't exist."""
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_DEFAULT_TEMPLATE)


def _atomic_write(path: Path, content: str) -> None:
    """Write content atomically via tempfile + rename."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix='.tmp')
    try:
        os.write(fd, content.encode())
        os.close(fd)
        fd = -1
        os.replace(tmp, path)
    except Exception:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _find_section(lines: list[str], header: str) -> tuple[int, int]:
    """Find (start, end) of a section by header text.

    Returns (header_line, next_header_or_eof).  (-1, -1) if not found.
    """
    start = None
    for i, line in enumerate(lines):
        if re.match(rf'##\s+{re.escape(header)}\b', line, re.IGNORECASE):
            start = i
            break
    if start is None:
        return -1, -1
    end = start + 1
    while end < len(lines):
        if lines[end].startswith('## '):
            break
        end += 1
    return start, end


def _parse_item(line: str, line_num: int, idx: int) -> dict | None:
    """Parse a single delegation item line."""
    m = re.match(r'^- \[( |x|X)\] (.+)', line)
    if not m:
        return None
    done = m.group(1).lower() == 'x'
    body = m.group(2).strip()

    # Assignee: text after → arrow
    assignee = None
    assignee_match = re.search(r'→\s*(\S+)', body)
    if assignee_match:
        assignee = assignee_match.group(1)

    # Inline fields
    delegated = _extract_field(body, 'delegated')
    followup = _extract_field(body, 'followup')
    completed = _extract_field(body, 'completed')

    # Department tag
    dept_match = re.search(r'#([A-Z]\w+)', body)
    department = dept_match.group(1) if dept_match else None

    # Title: text before → or inline fields
    title = body
    title = re.sub(r'\*\*(.+?)\*\*', r'\1', title)  # strip bold
    title = re.sub(r'\s*→.*', '', title)  # strip everything from arrow onwards
    title = title.strip()

    return {
        'id': idx,
        'line_num': line_num,
        'title': title,
        'done': done,
        'assignee': assignee,
        'delegated': delegated,
        'followup': followup,
        'completed': completed,
        'department': department,
        'raw_line': line,
    }


def _extract_field(text: str, field: str) -> str | None:
    """Extract [field::value] or field::value from text."""
    m = re.search(rf'\[?{field}::(\S+?)\]?(?:\s|$)', text)
    return m.group(1) if m else None


def _is_overdue(item: dict) -> bool:
    """Check if a delegation item is past its follow-up date."""
    followup = item.get('followup')
    if not followup:
        return False
    try:
        d = datetime.strptime(followup, '%Y-%m-%d').date()
        return d < date.today()
    except ValueError:
        return False


def _parse_section_items(lines: list[str], start: int, end: int) -> list[dict]:
    """Parse items in a section, returning list of dicts."""
    items = []
    idx = 0
    for i in range(start + 1, end):
        idx += 1
        item = _parse_item(lines[i], i, idx)
        if item:
            items.append(item)
    return items


# ── Public API ───────────────────────────────────────────────


def list_items(path: Path, overdue_only: bool = False) -> list[dict]:
    """List active delegated items. Returns list of parsed dicts."""
    content = path.read_text()
    lines = content.split('\n')

    active_start, active_end = _find_section(lines, 'Active')
    items = _parse_section_items(lines, active_start, active_end) if active_start >= 0 else []

    # Also include awaiting follow-up
    await_start, await_end = _find_section(lines, 'Awaiting Follow-up')
    if await_start >= 0:
        for it in _parse_section_items(lines, await_start, await_end):
            it['status'] = 'awaiting_followup'
            items.append(it)

    for it in items:
        if 'status' not in it:
            it['status'] = 'overdue' if _is_overdue(it) else 'active'

    if overdue_only:
        items = [it for it in items if _is_overdue(it)]

    return items


def list_items_json(path: Path, overdue_only: bool = False) -> str:
    """List delegated items as JSON."""
    items = list_items(path, overdue_only)
    output = []
    for it in items:
        output.append({
            'id': it['id'],
            'title': it['title'],
            'assignee': it.get('assignee'),
            'delegated': it.get('delegated'),
            'followup': it.get('followup'),
            'department': it.get('department'),
            'overdue': _is_overdue(it),
            'status': it.get('status', 'active'),
        })
    return json.dumps(output, indent=2)


def add_item(path: Path, title: str, assignee: str,
             followup: str, department: str | None = None) -> dict:
    """Add a delegated task to the Active section."""
    ensure_file(path)
    content = path.read_text()
    lines = content.split('\n')

    active_start, active_end = _find_section(lines, 'Active')
    if active_start == -1:
        raise ValueError("No ## Active section found in delegation file.")

    today_str = date.today().isoformat()
    task_line = f'- [ ] **{title}** → {assignee} [delegated::{today_str}] [followup::{followup}]'
    if department:
        task_line += f' #{department}'

    # Insert after last item in Active, or right after header
    insert_at = active_start + 1
    for i in range(active_start + 1, active_end):
        if re.match(r'^- \[', lines[i]):
            insert_at = i + 1

    lines.insert(insert_at, task_line)
    _atomic_write(path, '\n'.join(lines))

    return {
        'title': title,
        'assignee': assignee,
        'delegated': today_str,
        'followup': followup,
        'department': department,
    }


def complete_item(path: Path, item_id: int) -> dict:
    """Mark a delegated item as completed and move to Completed section."""
    content = path.read_text()
    lines = content.split('\n')

    # Find in Active section
    active_start, active_end = _find_section(lines, 'Active')
    items = _parse_section_items(lines, active_start, active_end) if active_start >= 0 else []
    target = next((it for it in items if it['id'] == item_id), None)
    if not target:
        raise ValueError(f"Item #{item_id} not found in Active section.")

    # Remove from Active
    removed = lines.pop(target['line_num'])

    # Build completed line
    today_str = date.today().isoformat()
    completed_line = removed.replace('- [ ]', '- [x]', 1)
    # Add completed:: field
    completed_line = completed_line.rstrip() + f' [completed::{today_str}]'

    # Insert into Completed section
    comp_start, comp_end = _find_section(lines, 'Completed')
    if comp_start >= 0:
        insert_at = comp_start + 1
        for i in range(comp_start + 1, comp_end):
            if re.match(r'^- \[', lines[i]):
                insert_at = i + 1
        lines.insert(insert_at, completed_line)
    else:
        lines.append('\n## Completed')
        lines.append(completed_line)

    _atomic_write(path, '\n'.join(lines))
    return target


def extend_item(path: Path, item_id: int, new_followup: str) -> dict:
    """Update the follow-up date for a delegated item."""
    content = path.read_text()
    lines = content.split('\n')

    active_start, active_end = _find_section(lines, 'Active')
    items = _parse_section_items(lines, active_start, active_end) if active_start >= 0 else []
    target = next((it for it in items if it['id'] == item_id), None)
    if not target:
        raise ValueError(f"Item #{item_id} not found in Active section.")

    old_line = lines[target['line_num']]
    new_line = re.sub(r'\[?followup::\S+\]?', f'[followup::{new_followup}]', old_line)
    lines[target['line_num']] = new_line

    _atomic_write(path, '\n'.join(lines))
    target['followup'] = new_followup
    return target


def get_active_item(path: Path, item_id: int) -> dict:
    """Return an active delegated item without modifying the file."""
    content = path.read_text()
    lines = content.split('\n')

    active_start, active_end = _find_section(lines, 'Active')
    items = _parse_section_items(lines, active_start, active_end) if active_start >= 0 else []
    target = next((it for it in items if it['id'] == item_id), None)
    if not target:
        raise ValueError(f"Item #{item_id} not found in Active section.")

    return dict(target)


def take_back_item(path: Path, item_id: int) -> dict:
    """Remove a delegated item and return it for re-insertion into tasks file."""
    content = path.read_text()
    lines = content.split('\n')

    active_start, active_end = _find_section(lines, 'Active')
    items = _parse_section_items(lines, active_start, active_end) if active_start >= 0 else []
    target = next((it for it in items if it['id'] == item_id), None)
    if not target:
        raise ValueError(f"Item #{item_id} not found in Active section.")

    lines.pop(target['line_num'])
    _atomic_write(path, '\n'.join(lines))
    return target
