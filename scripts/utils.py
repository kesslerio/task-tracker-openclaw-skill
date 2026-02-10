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
from datetime import datetime, timedelta
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
    
    current_section = None
    current_task = None
    today = datetime.now().date()
    
    for line in content.split('\n'):
        # Detect section headers
        if line.startswith('## '):
            if format == 'obsidian':
                # Match emoji at start of section name
                section_match = re.match(r'## ([🔴🟡🟠👥⚪✅])', line)
            else:
                # Legacy format: ## 🔴 High Priority
                section_match = re.match(r'## ([🔴🟡🟢📅✅])', line)
            
            if section_match:
                emoji = section_match.group(1)
                current_section = mapping.get(emoji)
            continue
        
        # Detect task line
        # Format: - [ ] **Task name** 🗓️2026-01-22 area:: Sales
        task_match = re.match(r'^- \[([ xX])\] \*\*(.+?)\*\*(.*)$', line)
        
        if task_match:
            done = task_match.group(1).lower() == 'x'
            title = task_match.group(2).strip()
            rest = task_match.group(3).strip()
            
            due_str = None
            area = None
            goal = None
            owner = None
            blocks = None
            task_type = None
            
            if format == 'obsidian':
                # Parse emoji date
                date_match = re.search(r'🗓️(\d{4}-\d{2}-\d{2})', rest)
                if date_match:
                    due_str = date_match.group(1)
                
                # Parse inline fields (handle multi-word values)
                # Pattern: field:: value (but not field:: next_field::)
                area_match = re.search(r'area::\s*(?!(\s|\w+::))([^\n]+?)(?=\s+\w+::|$)', rest)
                if area_match:
                    area = area_match.group(2).strip()
                
                goal_match = re.search(r'goal::\s*(\[\[[^\]]+\]\]|[^\s]+)', rest)
                if goal_match:
                    goal = goal_match.group(1).strip()
                
                owner_match = re.search(r'owner::\s*(?!(\s|\w+::))([^\n]+?)(?=\s+\w+::|$)', rest)
                if owner_match:
                    owner = owner_match.group(2).strip()

                blocks_match = re.search(r'blocks::\s*(?!(\s|\w+::))([^\n]+?)(?=\s+\w+::|$)', rest)
                if blocks_match:
                    blocks = blocks_match.group(2).strip()

                type_match = re.search(r'type::\s*(?!(\s|\w+::))([^\n]+?)(?=\s+\w+::|$)', rest)
                if type_match:
                    task_type = type_match.group(2).strip()
            
            current_task = {
                'title': title,
                'done': done,
                'section': current_section,
                'due': due_str,
                'area': area,
                'goal': goal,
                'owner': owner,
                'blocks': blocks,
                'type': task_type,
                'raw_line': line,
            }
            
            result['all'].append(current_task)
            
            if done:
                result['done'].append(current_task)
            elif current_section:
                result[current_section].append(current_task)
            
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
        if current_task and line.startswith('  '):
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


def get_section_display_name(section: str, personal: bool = False) -> str:
    """Get human-readable section name."""
    section_names = {
        'q1': '🔴 Q1: Urgent & Important',
        'q2': '🟡 Q2: Important, Not Urgent',
        'q3': '🟠 Q3: Waiting / Blocked',
        'team': '👥 Team Tasks',
        'backlog': '⚪ Backlog',
        'done': '✅ Done',
    }
    
    if personal:
        section_names['q1'] = '🔴 Must Do Today'
        section_names['q2'] = '🟡 Should Do This Week'
        section_names['q3'] = '🟠 Waiting On'
    
    return section_names.get(section, section or 'Uncategorized')
