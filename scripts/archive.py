#!/usr/bin/env python3
"""Done archive operations for objectives workflow."""

from __future__ import annotations

import os, re, tempfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from utils import parse_tasks


DEPARTMENT_DISPLAY = {"HR": "HR/People"}

@dataclass(frozen=True)
class ArchiveRecord:
    department: str
    title: str
    completed_date: str | None
    raw_line: str


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            tmp.write(content)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def _remove_task_line(content: str, raw_line: str) -> str:
    lines = content.splitlines(keepends=True)
    result = []
    i = 0
    while i < len(lines):
        if lines[i].rstrip() == raw_line.rstrip():
            i += 1
            while i < len(lines) and lines[i].startswith(('  ', '\t')):
                i += 1
        else:
            result.append(lines[i])
            i += 1
    return ''.join(result)


def get_archive_dir(tasks_file: Path) -> Path:
    env_dir = os.getenv("TASK_TRACKER_ARCHIVE_DIR")
    if env_dir:
        return Path(env_dir)
    return tasks_file.parent / "Done Archive"


def get_week_number(d):
    iso = d.isocalendar()
    return iso[0], iso[1]


def get_week_start(year, week):
    jan4 = date(year, 1, 4)
    week_1_monday = jan4 - timedelta(days=jan4.weekday())
    return week_1_monday + timedelta(weeks=week - 1)


def archive_week(tasks_file: Path, personal: bool) -> dict:
    if not tasks_file.exists():
        return {'error': f"Tasks file not found: {tasks_file}", 'archived': 0, 'removed': 0}
    
    content = tasks_file.read_text()
    tasks_data = parse_tasks(content, personal, "objectives")
    
    completed_by_dept = defaultdict(list)
    all_tasks = tasks_data.get('all', [])
    
    objective_depts = {}
    for task in all_tasks:
        if task.get('is_objective') and task.get('department'):
            objective_depts[task['title']] = DEPARTMENT_DISPLAY.get(task['department'], task['department'])
    
    for task in all_tasks:
        if not task.get('is_objective') and task.get('done'):
            parent = task.get('parent_objective')
            raw_line = task.get('raw_line', '')
            title = task.get('title', raw_line)
            completed_date = task.get('completed_date')
            dept = objective_depts.get(parent, 'Uncategorized')
            record = ArchiveRecord(department=dept, title=title, completed_date=completed_date, raw_line=raw_line)
            completed_by_dept[dept].append(record)
    
    if not completed_by_dept:
        return {'archived': 0, 'removed': 0}
    
    today = date.today()
    year, week = get_week_number(today)
    week_start = get_week_start(year, week)
    
    archive_dir = get_archive_dir(tasks_file)
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_file = archive_dir / f"{year}-W{week:02d}.md"
    
    archive_lines = [f"# Done Archive — Week of {week_start.strftime('%b %d, %Y')} (W{week:02d})\n"]
    for dept in sorted(completed_by_dept.keys()):
        archive_lines.append(f"\n## {dept}\n")
        for record in completed_by_dept[dept]:
            date_suffix = f" ✅ {record.completed_date}" if record.completed_date else ""
            archive_lines.append(f"- [x] {record.title}{date_suffix}\n")
    
    if archive_file.exists():
        existing = archive_file.read_text()
        content = existing.rstrip() + "\n" + "".join(archive_lines[1:])
    else:
        content = "".join(archive_lines)
    
    atomic_write(archive_file, content)
    
    updated_content = tasks_file.read_text()
    removed = 0
    for dept_records in completed_by_dept.values():
        for record in dept_records:
            if record.raw_line and record.raw_line in updated_content:
                updated_content = _remove_task_line(updated_content, record.raw_line)
                removed += 1
    
    if removed > 0:
        atomic_write(tasks_file, updated_content)
    
    total_archived = sum(len(records) for records in completed_by_dept.values())
    return {'archived': total_archived, 'removed': removed, 'archive_file': archive_file}


def consolidate_month(archive_dir: Path, month: str, delete_weekly: bool = False) -> dict:
    try:
        year, month_num = map(int, month.split('-'))
    except ValueError:
        return {'error': f"Invalid month format: {month}. Use YYYY-MM"}
    
    if not archive_dir.exists():
        return {'error': f"Archive directory not found: {archive_dir}"}
    
    weekly_files = []
    for week_file in sorted(archive_dir.glob(f"{year}-W*.md")):
        match = re.match(r'(\d{4})-W(\d{2})\.md', week_file.name)
        if not match:
            continue
        w_year, w_week = int(match.group(1)), int(match.group(2))
        week_start = get_week_start(w_year, w_week)
        if week_start.month == month_num or (week_start + timedelta(days=6)).month == month_num:
            weekly_files.append(week_file)
    
    if not weekly_files:
        return {'error': f"No weekly archives found for {month}"}
    
    items_by_dept = defaultdict(list)
    for week_file in weekly_files:
        content = week_file.read_text()
        current_dept = None
        for line in content.splitlines():
            if line.startswith("## "):
                current_dept = line[3:].strip()
            elif line.startswith("- [x] ") and current_dept:
                items_by_dept[current_dept].append(line)
    
    month_name = date(year, month_num, 1).strftime("%B %Y")
    monthly_file = archive_dir / f"{year}-{month_num:02d}-monthly.md"
    
    lines = [f"# Done Archive — {month_name}\n"]
    for dept in sorted(items_by_dept.keys()):
        lines.append(f"\n## {dept}\n")
        seen = set()
        for item in items_by_dept[dept]:
            if item not in seen:
                lines.append(item + "\n")
                seen.add(item)
    
    atomic_write(monthly_file, "".join(lines))
    
    deleted = 0
    if delete_weekly:
        for week_file in weekly_files:
            week_file.unlink()
            deleted += 1
    
    total_items = sum(len(items) for items in items_by_dept.values())
    return {'merged': total_items, 'weekly_files': weekly_files, 'monthly_file': monthly_file, 'deleted': deleted}


def search_archives(archive_dir: Path, query: str) -> list[dict]:
    if not archive_dir.exists():
        return []
    hits = []
    query_lower = query.lower()
    for archive_file in sorted(archive_dir.glob("*.md")):
        content = archive_file.read_text()
        for line_num, line in enumerate(content.splitlines(), 1):
            if query_lower in line.lower():
                hits.append({'file': archive_file, 'line': line_num, 'text': line.strip()})
    return hits


def archive_stats(archive_dir: Path, period: str) -> dict:
    today = date.today()
    
    if period == 'week':
        year, week = get_week_number(today)
        week_start = get_week_start(year, week)
        start_date = week_start
        end_date = week_start + timedelta(days=6)
        period_display = f"Week {week}"
    elif period == 'month':
        start_date = date(today.year, today.month, 1)
        end_date = (start_date + timedelta(days=32)).replace(day=1) - timedelta(days=1)
        period_display = start_date.strftime("%B")
    elif period == 'quarter':
        quarter = (today.month - 1) // 3 + 1
        start_month = (quarter - 1) * 3 + 1
        start_date = date(today.year, start_month, 1)
        end_date = (start_date + timedelta(days=95)).replace(day=1) - timedelta(days=1)
        period_display = f"Q{quarter}"
    else:
        return {'error': f"Invalid period: {period}"}
    
    if not archive_dir.exists():
        return {'period': period_display, 'start': start_date.isoformat(), 'end': end_date.isoformat(), 'total': 0, 'by_department': {}}
    
    counts = defaultdict(int)
    total = 0
    
    for archive_file in archive_dir.glob("*.md"):
        content = archive_file.read_text()
        current_dept = None
        for line in content.splitlines():
            if line.startswith("## "):
                current_dept = line[3:].strip()
            elif line.startswith("- [x] ") and current_dept:
                date_match = re.search(r'✅\s*(\d{4}-\d{2}-\d{2})', line)
                if date_match:
                    try:
                        item_date = datetime.strptime(date_match.group(1), '%Y-%m-%d').date()
                        if start_date <= item_date <= end_date:
                            counts[current_dept] += 1
                            total += 1
                    except ValueError:
                        pass
                else:
                    counts[current_dept] += 1
                    total += 1
    
    return {'period': period_display, 'start': start_date.isoformat(), 'end': end_date.isoformat(), 'total': total, 'by_department': dict(counts)}
