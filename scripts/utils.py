#!/usr/bin/env python3
"""
Shared utilities for task tracker scripts.
Supports both Obsidian (preferred) and legacy TASKS.md formats.

Configuration via environment variables:
- TASK_TRACKER_WORK_FILE: Path to work tasks file
- TASK_TRACKER_PERSONAL_FILE: Path to personal tasks file
- TASK_TRACKER_LEGACY_FILE: Path to legacy tasks file (fallback)
- TASK_TRACKER_ARCHIVE_DIR: Path to archive directory
"""

import os
import re
import calendar
from datetime import datetime, timedelta, date
from pathlib import Path
import sys

# Configurable paths with sensible defaults
# Users should set these environment variables for their own setup
OBSIDIAN_WORK = Path(os.getenv(
    'TASK_TRACKER_WORK_FILE',
    Path.home() / "Obsidian" / "03-Areas" / "Work" / "Work Tasks.md"
))
OBSIDIAN_PERSONAL = Path(os.getenv(
    'TASK_TRACKER_PERSONAL_FILE',
    Path.home() / "Obsidian" / "03-Areas" / "Personal" / "Personal Tasks.md"
))
LEGACY_WORK = Path(os.getenv(
    'TASK_TRACKER_LEGACY_FILE',
    Path.home() / "clawd" / "memory" / "work" / "TASKS.md"
))
ARCHIVE_DIR = Path(os.getenv(
    'TASK_TRACKER_ARCHIVE_DIR',
    Path.home() / "clawd" / "memory" / "work"
))


def get_current_quarter() -> str:
    """Return current quarter string like '2026-Q1'."""
    now = datetime.now()
    quarter = (now.month - 1) // 3 + 1
    return f"{now.year}-Q{quarter}"


def get_tasks_file(personal: bool = False, force_legacy: bool = False) -> tuple[Path, str]:
    """Get the appropriate tasks file and its format.
    
    Returns:
        tuple: (file_path, format) where format is 'obsidian' or 'legacy'
    """
    if force_legacy:
        return LEGACY_WORK, 'legacy'
    
    # Try Obsidian first
    obsidian_file = OBSIDIAN_PERSONAL if personal else OBSIDIAN_WORK
    if obsidian_file.exists():
        return obsidian_file, 'obsidian'
    
    # Fall back to legacy for work tasks only
    if not personal and LEGACY_WORK.exists():
        return LEGACY_WORK, 'legacy'
    
    # Return Obsidian path anyway (will show error if missing)
    return obsidian_file, 'obsidian'


DEPARTMENT_TAGS = {
    'hr': 'HR',
    'sales': 'Sales',
    'finance': 'Finance',
    'ops': 'Ops',
    'marketing': 'Marketing',
    'dev': 'Dev',
    'product': 'Product',
    'bizdev': 'BizDev',
    'legal': 'Legal',
}

PRIORITY_TAGS = {
    'urgent': 'urgent',
    'high': 'high',
    'medium': 'medium',
    'low': 'low',
}

# Priority emojis used in new Tasks plugin format
PRIORITY_EMOJI_MAP = {
    '🔺': 'urgent',  # Highest
    '⏫': 'high',
    '🔼': 'medium',
    '🔽': 'low',
    '⏬': 'low',     # Lowest
}

PRIORITY_TO_SECTION = {
    'urgent': 'q1',
    'high': 'q1',
    'medium': 'q2',
    'low': 'backlog',
}


def detect_format(content: str, fallback: str = 'obsidian') -> str:
    """Detect task format from content.

    'objectives' is always auto-detected (highest priority).
    Otherwise respects the caller's fallback hint so that legacy
    callers are not silently reclassified as obsidian.
    """
    if re.search(r'^\s*##\s+Objectives\b', content, re.IGNORECASE | re.MULTILINE):
        return 'objectives'
    if fallback not in ('obsidian', 'objectives') and re.search(
        r'^\s*##\s+🔴(?:\s|$)', content, re.MULTILINE
    ):
        # Caller explicitly requested a non-default format (e.g. 'legacy').
        # Don't override it just because 🔴 is present — both obsidian and
        # legacy use that emoji.
        return fallback
    if re.search(r'^\s*##\s+🔴(?:\s|$)', content, re.MULTILINE):
        return 'obsidian'
    return fallback


def _extract_tags_from_title(title: str) -> tuple[str, str | None, str | None]:
    """Extract supported #tags from title and return cleaned title + metadata."""
    department = None
    priority = None

    def _replace(match):
        nonlocal department, priority
        prefix = match.group(1)
        raw_tag = match.group(2)
        tag = raw_tag.lower()
        if tag in DEPARTMENT_TAGS:
            if department is None:
                department = DEPARTMENT_TAGS[tag]
            return prefix
        if tag in PRIORITY_TAGS:
            if priority is None:
                priority = PRIORITY_TAGS[tag]
            return prefix
        return match.group(0)

    cleaned = re.sub(r'(^|\s)#([A-Za-z][A-Za-z0-9_-]*)', _replace, title)
    cleaned = re.sub(r'\s{2,}', ' ', cleaned).strip()
    return cleaned, department, priority


def _split_plain_task_body(task_body: str) -> tuple[str, str]:
    """Split plain task body into title and metadata suffix."""
    marker_match = re.search(
        r'\s+(🗓️\d{4}-\d{2}-\d{2}|📅\d{4}-\d{2}-\d{2}|📅\s+\d{4}-\d{2}-\d{2}|🔺|⏫|🔼|🔽|⏬|(?:area|goal|owner|blocks|type|recur|estimate|depends|sprint)::)',
        task_body,
    )
    if marker_match:
        return task_body[:marker_match.start()].strip(), task_body[marker_match.start():].strip()
    return task_body.strip(), ''


def parse_tasks(content: str, personal: bool = False, format: str = 'obsidian') -> dict:
    """Parse tasks content into categorized task lists.
    
    Args:
        content: File content to parse
        personal: If True, use personal task categories
        format: 'obsidian' or 'legacy'
    
    Returns dict with keys:
    - q1: list of Q1 (Urgent & Important) tasks
    - q2: list of Q2 (Important, Not Urgent) tasks
    - q3: list of Q3 (Waiting/Blocked) tasks
    - team: list of Team Tasks (monitored) - work only
    - backlog: list of Backlog (someday/maybe) tasks
    - done: list of completed tasks
    - due_today: list of tasks due today
    - all: list of all tasks
    """
    result = {
        'q1': [],
        'q2': [],
        'q3': [],
        'team': [],
        'backlog': [],
        'objectives': [],
        'today': [],
        'parking_lot': [],
        'done': [],
        'due_today': [],
        'all': [],
    }
    
    section_mapping = {
        '🔴': 'q1',
        '🟡': 'q2',
        '🟠': 'q3',
        '👥': 'team',
        '⚪': 'backlog',
        '✅': 'done',
    }
    
    # Personal task sections differ
    personal_section_mapping = {
        '🔴': 'q1',
        '🟡': 'q2',
        '🟠': 'q3',
        '⚪': 'backlog',
        '✅': 'done',
    }
    
    mapping = personal_section_mapping if personal else section_mapping
    parsed_format = detect_format(content, format)
    
    current_section = None
    current_department = None  # Track department from ### lines
    current_task = None
    current_objective = None
    today = datetime.now().date()
    
    for line in content.split('\n'):
        # Detect section headers
        if line.startswith('## '):
            current_task = None
            current_department = None  # Reset department at new section
            if parsed_format == 'objectives':
                if re.match(r'##\s+Objectives\b', line, re.IGNORECASE):
                    current_section = 'objectives'
                    current_objective = None
                elif re.match(r'##\s+Today(?::.*)?$', line, re.IGNORECASE):
                    current_section = 'today'
                    current_objective = None
                elif re.match(r'##\s+🅿️\s*Parking Lot\b', line, re.IGNORECASE) or re.match(
                    r'##\s+Parking Lot\b',
                    line,
                    re.IGNORECASE,
                ):
                    current_section = 'parking_lot'
                    current_objective = None
                else:
                    section_match = re.match(r'## ([🔴🟡🟠👥⚪✅])', line)
                    current_section = mapping.get(section_match.group(1)) if section_match else None
                    current_objective = None
            elif parsed_format == 'obsidian':
                # Match emoji at start of section name
                section_match = re.match(r'## ([🔴🟡🟠👥⚪✅])', line)
                if section_match:
                    emoji = section_match.group(1)
                    current_section = mapping.get(emoji)
            continue
        
        # Handle ### sub-sections (e.g. ### 👥 Hiring #hiring) — objectives format only.
        # In objectives format these indicate department groupings under ## 📋 All Tasks.
        # In legacy/obsidian format, ### lines are ignored (current_section stays intact).
        if line.startswith('### ') and parsed_format == 'objectives':
            # Extract department from ### line, e.g. ### 👥 Hiring #hiring
            section_match = re.match(r'###\s+[^\s]+\s+([A-Za-z]+)\s*#?', line)
            if section_match:
                current_department = section_match.group(1).title()
            # Sub-sections under ## 📋 All Tasks belong to 'today' storage bucket
            current_section = 'today'
            current_objective = None
            continue
        
        # Detect task line
        # Format examples:
        # - [ ] **Task name** 🗓️2026-01-22 area:: Sales
        # - [ ] Task name #HR #high
        task_match = re.match(r'^(\s*)- \[([ xX])\] (.+)$', line)
        
        if task_match:
            indent = task_match.group(1)
            done = task_match.group(2).lower() == 'x'
            body = task_match.group(3).strip()
            completed_date = None

            # Parse completion timestamp suffix on done tasks:
            # "... ✅ YYYY-MM-DD" or "... ✅YYYY-MM-DD"
            if done:
                completed_match = re.search(r'✅\s*(\d{4}-\d{2}-\d{2})\s*$', body)
                if completed_match:
                    completed_date = completed_match.group(1)
                    # Strip completion suffix before parsing inline fields
                    body = body[:completed_match.start()].rstrip()

            bold_match = re.match(r'^\*\*(.+?)\*\*(.*)$', body)
            if bold_match:
                title = bold_match.group(1).strip()
                rest = bold_match.group(2).strip()
            else:
                title, rest = _split_plain_task_body(body)
            
            due_str = None
            area = None
            goal = None
            owner = None
            blocks = None
            task_type = None
            recur = None
            estimate = None
            depends = None
            sprint = None

            department = None
            priority = None
            parent_objective = None
            is_objective = False

            if parsed_format == 'objectives':
                title, department, priority = _extract_tags_from_title(title)
                if current_section == 'objectives':
                    if len(indent) >= 2:
                        parent_objective = current_objective
                    else:
                        is_objective = True
                        current_objective = title
                else:
                    current_objective = None

            if parsed_format in ('obsidian', 'objectives'):
                # Parse emoji date
                date_match = re.search(r'🗓️(\d{4}-\d{2}-\d{2})', rest)
                if date_match:
                    due_str = date_match.group(1)
                
                # NEW: Parse 📅 YYYY-MM-DD format (Tasks plugin)
                date_match = re.search(r'📅\s*(\d{4}-\d{2}-\d{2})', rest)
                if date_match:
                    due_str = date_match.group(1)
                
                # NEW: Parse priority emojis 🔺 ⏫ 🔼 🔽 ⏬
                for emoji, prio in PRIORITY_EMOJI_MAP.items():
                    if emoji in rest and priority is None:
                        priority = prio
                        # Strip the emoji from rest
                        rest = rest.replace(emoji, '').strip()
                
                # Parse inline fields (handle multi-word values)
                # Pattern: field:: value (but not field:: next_field::)
                area_match = re.search(r'(?<!\w)area::\s*(?!(\s|\w+::))([^\n]+?)(?=\s+\w+::|$)', rest)
                if area_match:
                    area = area_match.group(2).strip()
                
                goal_match = re.search(r'(?<!\w)goal::\s*(\[\[[^\]]+\]\]|[^\s]+)', rest)
                if goal_match:
                    goal = goal_match.group(1).strip()
                
                owner_match = re.search(r'(?<!\w)owner::\s*(?!(\s|\w+::))([^\n]+?)(?=\s+\w+::|$)', rest)
                if owner_match:
                    owner = owner_match.group(2).strip()

                blocks_match = re.search(r'(?<!\w)blocks::\s*(?!(\s|\w+::))([^\n]+?)(?=\s+\w+::|$)', rest)
                if blocks_match:
                    blocks = blocks_match.group(2).strip()

                type_match = re.search(r'(?<!\w)type::\s*(?!(\s|\w+::))([^\n]+?)(?=\s+\w+::|$)', rest)
                if type_match:
                    task_type = type_match.group(2).strip()

                recur_match = re.search(r'(?<!\w)recur::\s*(?!(\s|\w+::))([^\n]+?)(?=\s+\w+::|\s*🗓️|$)', rest)
                if recur_match:
                    recur = recur_match.group(2).strip()

                estimate_match = re.search(r'(?<!\w)estimate::\s*(?!(\s|\w+::))([^\n]+?)(?=\s+\w+::|\s*🗓️|$)', rest)
                if estimate_match:
                    estimate = estimate_match.group(2).strip()

                depends_match = re.search(r'(?<!\w)depends::\s*(?!(\s|\w+::))([^\n]+?)(?=\s+\w+::|\s*🗓️|$)', rest)
                if depends_match:
                    depends = depends_match.group(2).strip()

                sprint_match = re.search(r'(?<!\w)sprint::\s*(?!(\s|\w+::))([^\n]+?)(?=\s+\w+::|\s*🗓️|$)', rest)
                if sprint_match:
                    sprint = sprint_match.group(2).strip()
            
            current_task = {
                'title': title,
                'done': done,
                'section': current_section,
                'parent_objective': parent_objective,
                'is_objective': is_objective,
                'department': department or current_department,
                'priority': priority,
                'due': due_str,
                'area': area,
                'goal': goal,
                'owner': owner,
                'blocks': blocks,
                'type': task_type,
                'recur': recur,
                'estimate': estimate,
                'depends': depends,
                'sprint': sprint,
                'completed_date': completed_date,
                'raw_line': line,
            }
            
            result['all'].append(current_task)
            
            if done:
                result['done'].append(current_task)
            elif current_section and current_section in result:
                result[current_section].append(current_task)

            if not done and priority:
                mapped_section = PRIORITY_TO_SECTION.get(priority)
                if mapped_section and current_task not in result[mapped_section]:
                    result[mapped_section].append(current_task)
            
            # Check if due today (only for tasks WITH a due date)
            if due_str and not done:
                try:
                    due_date = datetime.strptime(due_str, '%Y-%m-%d').date()
                    if due_date == today:
                        result['due_today'].append(current_task)
                except ValueError:
                    pass
            
            continue
        
        # Handle task continuation (indented lines)
        if current_task and line.startswith('  ') and not re.match(r'^\s*- \[([ xX])\] ', line):
            meta_line = line.strip()
            
            # Remove leading "- " if present
            if meta_line.startswith('- '):
                meta_line = meta_line[2:]
            
            # Parse legacy format metadata
            if meta_line.lower().startswith('due:'):
                due_str = meta_line.split(':', 1)[1].strip()
                if not current_task['due']:
                    current_task['due'] = due_str
            elif meta_line.lower().startswith('blocks:'):
                current_task['blocks'] = meta_line.split(':', 1)[1].strip()
            elif meta_line.lower().startswith('owner:'):
                if not current_task.get('owner'):
                    current_task['owner'] = meta_line.split(':', 1)[1].strip()
    
    return result


def load_tasks(personal: bool = False, force_legacy: bool = False) -> tuple[str, dict]:
    """Load and parse tasks from file."""
    tasks_file, format = get_tasks_file(personal, force_legacy)
    
    if not tasks_file.exists():
        task_type = "Personal" if personal else "Work"
        
        print(f"\n❌ {task_type} tasks file not found: {tasks_file}\n", file=sys.stderr)
        print("Configure paths via environment variables:", file=sys.stderr)
        print("  TASK_TRACKER_WORK_FILE=~/path/to/Work Tasks.md", file=sys.stderr)
        print("  TASK_TRACKER_PERSONAL_FILE=~/path/to/Personal Tasks.md", file=sys.stderr)
        print("", file=sys.stderr)
        
        sys.exit(1)
    
    content = tasks_file.read_text()
    tasks = parse_tasks(content, personal, format)
    return content, tasks


def check_due_date(due: str, check_type: str = 'today') -> bool:
    """Check if a due date matches the given type."""
    if not due:
        return False  # Tasks without due dates don't match any filter
    
    today = datetime.now().date()
    week_end = today + timedelta(days=(6 - today.weekday()))
    
    try:
        due_date = datetime.strptime(due, '%Y-%m-%d').date()
        
        if check_type == 'today':
            return due_date == today
        elif check_type == 'this-week':
            return today <= due_date <= week_end
        elif check_type == 'due-or-overdue':
            return due_date <= today
        elif check_type == 'overdue':
            return due_date < today
    except ValueError:
        pass
    
    return False


def next_recurrence_date(recur_value: str, from_date) -> str:
    """Calculate next due date (YYYY-MM-DD) from recurrence rule and starting date."""
    if not recur_value:
        raise ValueError("recurrence value is required")

    recur = recur_value.strip().lower()

    if isinstance(from_date, datetime):
        base_date = from_date.date()
    elif isinstance(from_date, date):
        base_date = from_date
    elif isinstance(from_date, str):
        base_date = datetime.strptime(from_date, '%Y-%m-%d').date()
    else:
        raise ValueError("from_date must be a date/datetime object or YYYY-MM-DD string")

    if recur == 'daily':
        next_date = base_date + timedelta(days=1)
    elif recur == 'weekly':
        next_date = base_date + timedelta(days=7)
    elif recur == 'biweekly':
        next_date = base_date + timedelta(days=14)
    elif recur == 'monthly':
        year = base_date.year
        month = base_date.month + 1
        if month > 12:
            year += 1
            month = 1
        last_day = calendar.monthrange(year, month)[1]
        next_date = base_date.replace(year=year, month=month, day=min(base_date.day, last_day))
    elif recur.startswith('every '):
        weekday_name = recur.removeprefix('every ').strip()
        weekday_map = {
            'monday': 0,
            'tuesday': 1,
            'wednesday': 2,
            'thursday': 3,
            'friday': 4,
            'saturday': 5,
            'sunday': 6,
        }
        if weekday_name not in weekday_map:
            raise ValueError(f"unsupported recurrence pattern: {recur_value}")

        target_weekday = weekday_map[weekday_name]
        days_ahead = (target_weekday - base_date.weekday()) % 7
        if days_ahead == 0:
            days_ahead = 7
        next_date = base_date + timedelta(days=days_ahead)
    else:
        raise ValueError(f"unsupported recurrence pattern: {recur_value}")

    return next_date.strftime('%Y-%m-%d')


def parse_duration(duration_str: str | None) -> int:
    """Parse duration string (e.g. '2h', '30m', '1.5h') into minutes."""
    if not duration_str:
        return 0

    duration_str = duration_str.lower().strip()
    total_minutes = 0

    # Match hours and minutes (e.g. '2h 30m', '1.5h')
    parts = re.findall(r'([\d.]+)([hm])', duration_str)
    if not parts:
        # Try pure numbers as minutes
        try:
            return int(float(duration_str))
        except ValueError:
            return 0

    for val, unit in parts:
        try:
            v = float(val)
            if unit == 'h':
                total_minutes += int(v * 60)
            elif unit == 'm':
                total_minutes += int(v)
        except ValueError:
            continue

    return total_minutes


def format_duration(minutes: int) -> str:
    """Format minutes into a human-readable string (e.g. '2h 30m')."""
    if minutes <= 0:
        return ""

    h = minutes // 60
    m = minutes % 60

    parts = []
    if h > 0:
        parts.append(f"{h}h")
    if m > 0:
        parts.append(f"{m}m")

    return " ".join(parts)


def get_missed_tasks(tasks_data: dict, lookback_days: int = 1, reference_date: str = None) -> list:
    """Return tasks missed within the lookback window (excluding reference date).
    
    Args:
        tasks_data: Dict containing 'all' key with list of tasks
        lookback_days: Number of days to look back (default 1 = yesterday only)
        reference_date: Date string (YYYY-MM-DD) to use as "today". If None, uses actual today.
    """
    if lookback_days < 1:
        return []

    if reference_date:
        try:
            today = datetime.strptime(reference_date, '%Y-%m-%d').date()
        except ValueError:
            today = datetime.now().date()
    else:
        today = datetime.now().date()
    
    start_date = today - timedelta(days=lookback_days)
    end_date = today - timedelta(days=1)

    missed = []
    for task in tasks_data.get('all', []):
        if task.get('done'):
            continue
        due_str = task.get('due')
        if not due_str:
            continue

        try:
            due_date = datetime.strptime(due_str, '%Y-%m-%d').date()
        except ValueError:
            continue

        if start_date <= due_date <= end_date:
            missed.append(task)

    return missed


def get_missed_tasks_bucketed(tasks_data: dict, reference_date: str = None) -> dict:
    """Return missed tasks bucketed by age: yesterday, last7, last30, older.
    
    Args:
        tasks_data: Dict containing 'all' key with list of tasks
        reference_date: Date string (YYYY-MM-DD) to use as "today". If None, uses actual today.
    
    Returns:
        Dict with keys: yesterday, last7, last30, older (each contains list of tasks)
    """
    if reference_date:
        try:
            today = datetime.strptime(reference_date, '%Y-%m-%d').date()
        except ValueError:
            today = datetime.now().date()
    else:
        today = datetime.now().date()

    yesterday = today - timedelta(days=1)
    last_week = today - timedelta(days=7)
    last_month = today - timedelta(days=30)

    buckets = {
        'yesterday': [],
        'last7': [],
        'last30': [],
        'older': []
    }

    for task in tasks_data.get('all', []):
        if task.get('done'):
            continue
        due_str = task.get('due')
        if not due_str:
            continue

        try:
            due_date = datetime.strptime(due_str, '%Y-%m-%d').date()
        except ValueError:
            continue

        # Only include overdue tasks (due date < today)
        if due_date >= today:
            continue

        # Bucket by age
        if due_date == yesterday:
            buckets['yesterday'].append(task)
        elif due_date >= last_week:
            buckets['last7'].append(task)
        elif due_date >= last_month:
            buckets['last30'].append(task)
        else:
            buckets['older'].append(task)

    return buckets


def effective_priority(task: dict, reference_date=None) -> dict:
    """Return display priority for a task, escalating overdue tasks.

    Escalation rules (display-only — never modifies the task file):
    - 🟡 (q2) overdue >3 days  → display as 🟠 (q3)
    - 🟡 (q2) overdue >7 days  → display as 🔴 (q1)
    - 🟠 (q3) overdue >14 days → display as 🔴 (q1)

    Args:
        task: Task dict with at least 'section' and 'due' keys.
        reference_date: A datetime.date or YYYY-MM-DD string. Defaults to today.

    Returns:
        dict with keys:
        - section: the effective display section (e.g. 'q1')
        - escalated: True if priority was escalated
        - original_section: the original section from the task
        - indicator: a human-readable escalation note, or empty string
    """
    from datetime import datetime as _dt

    section = task.get('section')
    due_str = task.get('due')
    original_section = section

    # Resolve reference date
    if reference_date is None:
        ref = _dt.now().date()
    elif isinstance(reference_date, str):
        try:
            ref = _dt.strptime(reference_date, '%Y-%m-%d').date()
        except ValueError:
            ref = _dt.now().date()
    else:
        ref = reference_date

    # Normalize non-q sections (objectives/today/parking_lot) to q1/q2/q3
    # based on task priority so regroup_by_effective_priority never gets
    # an unknown key.
    SECTION_PRIORITY_FALLBACK = {
        'urgent': 'q1',
        'high': 'q1',
        'medium': 'q2',
        'low': 'q3',
    }
    if section not in ('q1', 'q2', 'q3'):
        priority = task.get('priority') or 'medium'
        section = SECTION_PRIORITY_FALLBACK.get(priority, 'q2')
        original_section = original_section  # keep original for reference

    # Only escalate open tasks with a due date in eligible sections
    if task.get('done') or not due_str or section not in ('q2', 'q3'):
        return {
            'section': section,
            'escalated': False,
            'original_section': original_section,
            'indicator': '',
        }

    try:
        due_date = _dt.strptime(due_str, '%Y-%m-%d').date()
    except ValueError:
        return {
            'section': section,
            'escalated': False,
            'original_section': original_section,
            'indicator': '',
        }

    overdue_days = (ref - due_date).days
    if overdue_days <= 0:
        return {
            'section': section,
            'escalated': False,
            'original_section': original_section,
            'indicator': '',
        }

    section_emoji = {'q1': '🔴', 'q2': '🟡', 'q3': '🟠'}
    original_emoji = section_emoji.get(original_section, '')

    new_section = section  # default: no change

    if section == 'q2':
        if overdue_days > 7:
            new_section = 'q1'
        elif overdue_days > 3:
            new_section = 'q3'
    elif section == 'q3':
        if overdue_days > 14:
            new_section = 'q1'

    if new_section != section:
        return {
            'section': new_section,
            'escalated': True,
            'original_section': original_section,
            'indicator': f'⬆️ escalated from {original_emoji}',
        }

    return {
        'section': section,
        'escalated': False,
        'original_section': original_section,
        'indicator': '',
    }


def regroup_by_effective_priority(tasks_data: dict, reference_date=None) -> dict:
    """Regroup q1/q2/q3 tasks by their effective (escalated) display priority.

    Returns dict with keys 'q1', 'q2', 'q3'. Each task is a shallow copy
    with a transient '_escalation_indicator' key — original task dicts are
    not mutated.

    Deduplicates by (title, due) to avoid double-counting tasks that appear
    in both objectives and today sections of the objectives format.
    """
    regrouped = {'q1': [], 'q2': [], 'q3': []}
    seen: set = set()
    for section_key in ('q1', 'q2', 'q3'):
        for task in tasks_data.get(section_key, []):
            # Skip section-header pseudo-tasks in objectives format.
            # These are parent objective lines like "- [ ] Hiring #hiring" or
            # "- [ ] Marketing" that have no due date and no priority emoji —
            # they act as grouping headers, not actionable tasks.
            if task.get('section') == 'objectives' and not task.get('due') and not any(
                c in task.get('title', '') for c in ['📅', '🔺', '⏫', '🔼', '🔽', '⏬']
            ):
                title = task.get('title', '')
                # Header heuristic: single word, or word(s) followed only by #tags
                import re as _re
                stripped = _re.sub(r'\s*#\w+', '', title).strip()
                if not stripped or ' ' not in stripped:
                    continue
            # Dedup key includes original section + department to avoid
            # false merges of distinct tasks that share title+due (e.g. same
            # task name in different departments, or recurring tasks).
            dedup_key = (
                task.get('title', ''),
                task.get('due', ''),
                task.get('section', ''),
                task.get('department', ''),
            )
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            eff = effective_priority(task, reference_date)
            # Shallow copy to avoid mutating the shared task dict
            display_task = {**task, '_escalation_indicator': eff['indicator']}
            regrouped[eff['section']].append(display_task)
    return regrouped


def escalation_suffix(task: dict) -> str:
    """Return escalation indicator suffix for a task, if any."""
    indicator = task.get('_escalation_indicator', '')
    return f" {indicator}" if indicator else ""


def recurrence_suffix(task: dict) -> str:
    """Return recurrence indicator suffix for a task, if any."""
    recur = (task.get('recur') or '').strip()
    return f" 🔄 {recur}" if recur else ""


def dependency_suffix(task: dict) -> str:
    """Return dependency indicator suffix for a task, if any."""
    depends = (task.get('depends') or '').strip()
    return f" 🔗 depends: {depends}" if depends else ""


def sprint_suffix(task: dict) -> str:
    """Return sprint indicator suffix for a task, if any."""
    sprint = (task.get('sprint') or '').strip()
    return f" 🏃 {sprint}" if sprint else ""


def get_objective_progress(tasks_data: dict) -> list[dict]:
    """Build objective-level progress rows from parsed tasks."""
    objective_rows: dict[str, dict] = {}
    objective_order: list[str] = []

    for task in tasks_data.get('all', []):
        if task.get('is_objective'):
            title = task.get('title')
            if not title:
                continue
            if title not in objective_rows:
                objective_order.append(title)
                objective_rows[title] = {
                    'title': title,
                    'department': task.get('department'),
                    'priority': task.get('priority'),
                    'tasks': [],
                }
            else:
                if not objective_rows[title].get('department'):
                    objective_rows[title]['department'] = task.get('department')
                if not objective_rows[title].get('priority'):
                    objective_rows[title]['priority'] = task.get('priority')

        parent = task.get('parent_objective')
        if parent:
            if parent not in objective_rows:
                objective_order.append(parent)
                objective_rows[parent] = {
                    'title': parent,
                    'department': None,
                    'priority': None,
                    'tasks': [],
                }
            objective_rows[parent]['tasks'].append(
                {
                    'title': task.get('title', ''),
                    'done': bool(task.get('done')),
                }
            )

    progress = []
    for title in objective_order:
        row = objective_rows[title]
        total_tasks = len(row['tasks'])
        completed_tasks = sum(1 for task in row['tasks'] if task['done'])
        completion_pct = round((completed_tasks / total_tasks) * 100, 1) if total_tasks else 0.0

        progress.append(
            {
                'title': row['title'],
                'department': row.get('department'),
                'priority': row.get('priority'),
                'total_tasks': total_tasks,
                'completed_tasks': completed_tasks,
                'completion_pct': completion_pct,
                'tasks': row['tasks'],
            }
        )

    return progress


def summarize_objective_progress(tasks_data: dict) -> dict:
    """Return objective progress summary counts and at-risk objective list."""
    objectives = get_objective_progress(tasks_data)
    at_risk = [
        objective for objective in objectives
        if objective['total_tasks'] > 0 and objective['completed_tasks'] == 0
    ]
    on_track = sum(1 for objective in objectives if objective['completion_pct'] > 0)

    return {
        'total_objectives': len(objectives),
        'on_track_objectives': on_track,
        'at_risk_objectives': at_risk,
    }


def get_section_display_name(section: str, personal: bool = False) -> str:
    """Get human-readable section name."""
    section_names = {
        'q1': '🔴 Q1: Urgent & Important',
        'q2': '🟡 Q2: Important, Not Urgent',
        'q3': '🟠 Q3: Waiting / Blocked',
        'team': '👥 Team Tasks',
        'backlog': '⚪ Backlog',
        'objectives': '🎯 Objectives',
        'today': '📌 Today',
        'parking_lot': '🅿️ Parking Lot',
        'done': '✅ Done',
    }
    
    if personal:
        section_names['q1'] = '🔴 Must Do Today'
        section_names['q2'] = '🟡 Should Do This Week'
        section_names['q3'] = '🟠 Waiting On'
    
    return section_names.get(section, section or 'Uncategorized')
