#!/usr/bin/env python3
"""
Parking Lot (backlog) operations for the task tracker.

Manages the 🅿️ Parking Lot section in the objectives file:
- list: show items with stale status and count
- add: add item respecting hard cap
- stale: list items older than threshold (JSON)
- promote: move item to objectives section
- drop: remove item and archive as "dropped"
"""

import json
import os
import re
import tempfile
from datetime import datetime, date
from pathlib import Path


def _parking_lot_cap() -> int:
    return int(os.getenv('PARKING_LOT_CAP', '25'))


def _parking_lot_stale_days() -> int:
    return int(os.getenv('PARKING_LOT_STALE_DAYS', '30'))


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


def _find_parking_lot_bounds(lines: list[str]) -> tuple[int, int]:
    """Find start (header line) and end (exclusive) of the Parking Lot section.

    Returns (header_index, end_index). end_index points to the next
    ## header or len(lines).  Returns (-1, -1) if not found.
    """
    start = None
    for i, line in enumerate(lines):
        if re.match(r'##\s+🅿️\s*Parking Lot\b', line, re.IGNORECASE) or \
           re.match(r'##\s+Parking Lot\b', line, re.IGNORECASE):
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


def _parse_items(lines: list[str], start: int, end: int) -> list[dict]:
    """Parse task items from the parking lot section lines."""
    items = []
    idx = 0
    for i in range(start + 1, end):
        line = lines[i]
        m = re.match(r'^- \[( |x|X)\] (.+)', line)
        if not m:
            continue
        idx += 1
        done = m.group(1).lower() == 'x'
        body = m.group(2).strip()

        # Extract created:: date
        created_match = re.search(r'created::(\S+)', body)
        created_date = created_match.group(1) if created_match else None

        # Extract stale:: marker
        stale_match = re.search(r'stale::(\S+)', body)
        stale_date = stale_match.group(1) if stale_match else None

        # Extract department tags (#Dev, #Sales, etc.)
        dept_tags = re.findall(r'#([A-Z]\w+)', body)
        department = dept_tags[0] if dept_tags else None

        # Extract priority tag (#urgent, #high, #medium, #low)
        priority = None
        for p in ('urgent', 'high', 'medium', 'low'):
            if re.search(rf'#{p}\b', body, re.IGNORECASE):
                priority = p
                break

        # Clean title: strip bold, inline fields, tags
        title = body
        title = re.sub(r'\*\*(.+?)\*\*', r'\1', title)  # strip bold
        title = re.sub(r'\s*created::\S+', '', title)
        title = re.sub(r'\s*stale::\S+', '', title)
        title = re.sub(r'\s*#\w+', '', title)
        title = title.strip().rstrip('—').strip()

        items.append({
            'id': idx,
            'line_num': i,
            'title': title,
            'done': done,
            'created': created_date,
            'stale': stale_date,
            'department': department,
            'priority': priority,
            'raw_line': line,
        })
    return items


def _days_since(date_str: str | None) -> int | None:
    """Return days since a YYYY-MM-DD date string, or None."""
    if not date_str:
        return None
    try:
        d = datetime.strptime(date_str, '%Y-%m-%d').date()
        return (date.today() - d).days
    except ValueError:
        return None


def _is_stale(item: dict) -> bool:
    """Check if a parking lot item is stale based on created date."""
    threshold = _parking_lot_stale_days()
    age = _days_since(item.get('created'))
    return age is not None and age >= threshold


# ── Public API ───────────────────────────────────────────────


def list_items(tasks_file: Path) -> str:
    """List all parking lot items with stale indicators. Returns formatted text."""
    content = tasks_file.read_text()
    lines = content.split('\n')
    start, end = _find_parking_lot_bounds(lines)
    if start == -1:
        return "No Parking Lot section found."

    items = _parse_items(lines, start, end)
    cap = _parking_lot_cap()

    if not items:
        return f"Parking Lot is empty. [0/{cap} items]"

    stale_count = sum(1 for it in items if _is_stale(it))
    result = []
    for it in items:
        stale_tag = " — STALE" if _is_stale(it) else ""
        age = _days_since(it.get('created'))
        age_str = f" ({age} days)" if age is not None else ""
        dept = f" #{it['department']}" if it.get('department') else ""
        pri = f" #{it['priority']}" if it.get('priority') and it['priority'] != 'low' else ""
        result.append(f"{it['id']}. {it['title']}{dept}{pri}{age_str}{stale_tag}")

    result.append(f"[{len(items)}/{cap} items, {stale_count} stale]")
    return '\n'.join(result)


def list_stale(tasks_file: Path) -> str:
    """List stale items as JSON (for agent/weekly review consumption)."""
    content = tasks_file.read_text()
    lines = content.split('\n')
    start, end = _find_parking_lot_bounds(lines)
    if start == -1:
        return json.dumps([])

    items = _parse_items(lines, start, end)
    stale = []
    for it in items:
        if _is_stale(it):
            stale.append({
                'id': it['id'],
                'title': it['title'],
                'department': it.get('department'),
                'priority': it.get('priority'),
                'created': it.get('created'),
                'age_days': _days_since(it.get('created')),
            })
    return json.dumps(stale, indent=2)


def add_item(tasks_file: Path, title: str, dept: str | None = None,
             priority: str = 'low') -> str:
    """Add an item to the parking lot. Returns status message."""
    content = tasks_file.read_text()
    lines = content.split('\n')
    start, end = _find_parking_lot_bounds(lines)

    if start == -1:
        return "❌ No Parking Lot section found in tasks file."

    items = _parse_items(lines, start, end)
    cap = _parking_lot_cap()
    if len(items) >= cap:
        return f"❌ Parking lot full ({len(items)}/{cap}). Drop an item first."

    # Build task line
    today_str = date.today().isoformat()
    task_line = f'- [ ] **{title}**'
    if dept:
        task_line += f' #{dept}'
    if priority and priority != 'low':
        task_line += f' #{priority}'
    task_line += f' created::{today_str}'

    # Insert after last existing item, or right after header+blank
    insert_at = start + 1
    for i in range(start + 1, end):
        if re.match(r'^- \[', lines[i]):
            insert_at = i + 1

    lines.insert(insert_at, task_line)
    _atomic_write(tasks_file, '\n'.join(lines))
    return f"✅ Added to Parking Lot: {title} [{len(items) + 1}/{cap}]"


def promote_item(tasks_file: Path, item_id: int) -> str:
    """Move item from parking lot to objectives/high-priority section."""
    content = tasks_file.read_text()
    lines = content.split('\n')
    start, end = _find_parking_lot_bounds(lines)

    if start == -1:
        return "❌ No Parking Lot section found."

    items = _parse_items(lines, start, end)
    target = next((it for it in items if it['id'] == item_id), None)
    if not target:
        return f"❌ Item #{item_id} not found in Parking Lot."

    # Remove from parking lot
    removed_line = lines.pop(target['line_num'])

    # Clean the line: strip created/stale fields
    promoted = removed_line
    promoted = re.sub(r'\s*created::\S+', '', promoted)
    promoted = re.sub(r'\s*stale::\S+', '', promoted)
    promoted = promoted.rstrip()

    # Find insertion target: Objectives header > 🔴 header > before parking lot
    insert_at = None
    for i, line in enumerate(lines):
        if re.match(r'##\s+Objectives\b', line, re.IGNORECASE):
            insert_at = i + 1
            while insert_at < len(lines) and lines[insert_at].strip() == '':
                insert_at += 1
            break
        if re.match(r'##\s+🔴', line):
            insert_at = i + 1
            while insert_at < len(lines) and lines[insert_at].strip() == '':
                insert_at += 1
            break

    if insert_at is None:
        # Fallback: insert right before parking lot header
        for i, line in enumerate(lines):
            if re.match(r'##\s+🅿️|##\s+Parking Lot\b', line, re.IGNORECASE):
                insert_at = i
                break
        if insert_at is None:
            insert_at = len(lines)

    lines.insert(insert_at, promoted)
    _atomic_write(tasks_file, '\n'.join(lines))
    return f"✅ Promoted from Parking Lot: {target['title']}"


def drop_item(tasks_file: Path, item_id: int,
              archive_dir: Path | None = None) -> str:
    """Remove item from parking lot and optionally archive as dropped."""
    content = tasks_file.read_text()
    lines = content.split('\n')
    start, end = _find_parking_lot_bounds(lines)

    if start == -1:
        return "❌ No Parking Lot section found."

    items = _parse_items(lines, start, end)
    target = next((it for it in items if it['id'] == item_id), None)
    if not target:
        return f"❌ Item #{item_id} not found in Parking Lot."

    lines.pop(target['line_num'])
    _atomic_write(tasks_file, '\n'.join(lines))

    # Append to weekly archive if dir provided
    if archive_dir:
        archive_dir.mkdir(parents=True, exist_ok=True)
        today = date.today()
        week_num = today.isocalendar()[1]
        archive_file = archive_dir / f"{today.year}-W{week_num:02d}.md"

        if archive_file.exists():
            arc = archive_file.read_text()
        else:
            arc = f"# Done Archive — Week of {today.strftime('%b %d, %Y')} (W{week_num:02d})\n\n"

        dept = target.get('department') or 'Uncategorized'
        dept_header = f"## {dept}\n"
        entry = f"- [x] ~~{target['title']}~~ (dropped) ✅ {today.isoformat()}\n"

        if dept_header in arc:
            idx = arc.index(dept_header) + len(dept_header)
            arc = arc[:idx] + entry + arc[idx:]
        else:
            arc += f"\n{dept_header}{entry}"

        _atomic_write(archive_file, arc)

    return f"✅ Dropped from Parking Lot: {target['title']}"
