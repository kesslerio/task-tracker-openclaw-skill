#!/usr/bin/env python3
"""
Task Tracker CLI - Supports both Work and Personal tasks.

Usage:
    tasks.py list [--priority high|medium|low] [--status open|done] [--completed-since 24h|7d|30d] [--due today|this-week|overdue|due-or-overdue]
    tasks.py --personal list
    tasks.py add "Task title" [--priority high|medium|low] [--due YYYY-MM-DD]
    tasks.py done "task query"
    tasks.py blockers [--person NAME]
    tasks.py archive
"""

import argparse
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from daily_notes import extract_completed_tasks
from log_done import log_task_completed
from utils import (
    get_tasks_file,
    get_section_display_name,
    parse_tasks,
    load_tasks,
    check_due_date,
    next_recurrence_date,
    get_current_quarter,
    ARCHIVE_DIR,
)


def list_tasks(args):
    """List tasks with optional filters."""
    _, tasks_data = load_tasks(args.personal)
    tasks = tasks_data['all']
    
    # Apply filters
    filtered = tasks

    if args.status == 'done':
        filtered = [t for t in filtered if t['done']]
    elif args.status == 'open':
        filtered = [t for t in filtered if not t['done']]
    
    if args.priority:
        priority_map = {
            'high': 'q1',
            'medium': 'q2',
            'low': 'backlog',
        }
        target_key = priority_map.get(args.priority.lower())
        if target_key:
            filtered = [t for t in filtered if t.get('section') == target_key]
    
    if args.due:
        filtered = [t for t in filtered if check_due_date(t.get('due', ''), args.due)]

    if args.completed_since:
        # Note: timestamps are date-only (YYYY-MM-DD), so "24h" actually
        # means "yesterday or today" and "7d" means "last 7 calendar days".
        cutoff_days = {
            '24h': 1,
            '7d': 7,
            '30d': 30,
        }[args.completed_since]
        cutoff_date = datetime.now().date() - timedelta(days=cutoff_days)

        # Completion windows only apply to done tasks.
        filtered = [t for t in filtered if t.get('done')]

        recent_done = []
        for task in filtered:
            completed_date = task.get('completed_date')
            if not completed_date:
                continue
            try:
                parsed_date = datetime.strptime(completed_date, '%Y-%m-%d').date()
            except ValueError:
                continue
            if parsed_date >= cutoff_date:
                recent_done.append(task)

        # Augment with daily notes completions
        notes_dir_raw = os.getenv("TASK_TRACKER_DAILY_NOTES_DIR")
        if notes_dir_raw:
            notes_tasks = extract_completed_tasks(
                notes_dir=Path(notes_dir_raw),
                start_date=cutoff_date,
                end_date=datetime.now().date(),
            )
            board_titles = {t['title'].casefold() for t in recent_done}
            for nt in notes_tasks:
                if nt['title'].casefold() not in board_titles:
                    recent_done.append(nt)

        filtered = recent_done
    
    if not filtered:
        task_type = "Personal" if args.personal else "Work"
        print(f"No {task_type} tasks found matching criteria.")
        return
    
    print(f"\n📋 {('Personal' if args.personal else 'Work')} Tasks ({len(filtered)} items)\n")
    
    current_section = None
    for task in filtered:
        section = task.get('section')
        if section != current_section:
            current_section = section
            print(f"### {get_section_display_name(section, args.personal)}\n")
        
        checkbox = '✅' if task['done'] else '⬜'
        due_str = f" (🗓️{task['due']})" if task.get('due') else ''
        area_str = f" [{task.get('area')}]" if task.get('area') else ''
        
        print(f"{checkbox} **{task['title']}**{due_str}{area_str}")


def add_task(args):
    """Add a new task."""
    tasks_file, format = get_tasks_file(args.personal)
    
    if not tasks_file.exists():
        print(f"❌ Tasks file not found: {tasks_file}")
        return
    
    content = tasks_file.read_text()
    
    # Build task entry with emoji date format
    priority_patterns = {
        'high': r'## 🔴',
        'medium': r'## 🟡',
        'low': r'## ⚪',
    }
    priority_pattern = priority_patterns.get(args.priority, r'## 🟡')
    
    # Build task line
    task_line = f'- [ ] **{args.title}**'
    if args.due:
        task_line += f' 🗓️{args.due}'
    if args.area:
        task_line += f' area:: {args.area}'
    default_owner = os.getenv('TASK_TRACKER_DEFAULT_OWNER', 'me')
    if args.owner and args.owner not in ('me', default_owner):
        task_line += f' owner:: {args.owner}'
    
    # Find section and insert after header
    section_match = re.search(rf'({priority_pattern}[^\n]*\n)', content)
    
    if section_match:
        insert_pos = section_match.end()
        # Skip any subsection headers or blank lines
        remaining = content[insert_pos:]
        lines = remaining.split('\n')
        skip_lines = 0
        for line in lines:
            if line.strip() == '' or line.startswith('**') or line.startswith('>'):
                skip_lines += 1
            else:
                break
        insert_pos += sum(len(lines[i]) + 1 for i in range(skip_lines))
        
        new_content = content[:insert_pos] + task_line + '\n' + content[insert_pos:]
        tasks_file.write_text(new_content)
        task_type = "Personal" if args.personal else "Work"
        print(f"✅ Added {task_type} task: {args.title}")
    else:
        print(f"⚠️ Could not find section matching '{priority_pattern}'. Add manually.")


def _remove_task_line(content: str, raw_line: str) -> str:
    """Remove a task line and any indented continuation lines below it."""
    lines = content.split('\n')
    result: list[str] = []
    skip_continuations = False
    for line in lines:
        if skip_continuations:
            if line.startswith('  ') and not re.match(r'^- \[', line):
                continue  # skip continuation line
            skip_continuations = False
        if line == raw_line:
            skip_continuations = True
            continue
        result.append(line)
    return '\n'.join(result)


def done_task(args):
    """Complete a task: log to daily notes and remove from the board."""
    tasks_file, format = get_tasks_file(args.personal)

    if not tasks_file.exists():
        print(f"❌ Tasks file not found: {tasks_file}")
        return

    content = tasks_file.read_text()
    tasks_data = parse_tasks(content, args.personal, format)
    tasks = tasks_data['all']

    query = args.query.lower()
    matches = [t for t in tasks if query in t['title'].lower() and not t['done']]

    if not matches:
        print(f"No matching task found for: {args.query}")
        return

    if len(matches) > 1:
        print(f"Multiple matches found:")
        for i, t in enumerate(matches, 1):
            print(f"  {i}. {t['title']}")
        print("\nBe more specific.")
        return

    task = matches[0]

    old_line = task.get('raw_line', '')
    if not old_line:
        print("⚠️ Could not find task line to update.")
        return

    # Log completion to daily notes — abort board changes if this fails
    logged = log_task_completed(
        title=task['title'],
        section=task.get('section'),
        area=task.get('area'),
        due=task.get('due'),
        recur=task.get('recur'),
    )
    if not logged:
        print(
            "❌ Could not log completion to daily notes. "
            "Task was NOT removed from the board to prevent data loss.\n"
            "Check that TASK_TRACKER_DAILY_NOTES_DIR (or TASK_TRACKER_DONE_LOG_DIR) "
            "is set and writable.",
            file=sys.stderr,
        )
        return

    completed_today = datetime.now().strftime('%Y-%m-%d')
    recur_value = (task.get('recur') or '').strip()

    if recur_value:
        # Recurring: replace with next instance (no completed line on board)
        from_date = task.get('due') or completed_today
        try:
            next_due = next_recurrence_date(recur_value, from_date)
            next_task_line = old_line

            if re.search(r'🗓️\d{4}-\d{2}-\d{2}', next_task_line):
                next_task_line = re.sub(
                    r'🗓️\d{4}-\d{2}-\d{2}',
                    f'🗓️{next_due}',
                    next_task_line,
                    count=1,
                )
            else:
                inline_field_match = re.search(r'\s+\w+::', next_task_line)
                if inline_field_match:
                    pos = inline_field_match.start()
                    next_task_line = f"{next_task_line[:pos]} 🗓️{next_due}{next_task_line[pos:]}"
                else:
                    next_task_line = f"{next_task_line.rstrip()} 🗓️{next_due}"

            new_content = content.replace(old_line, next_task_line, 1)
        except ValueError as e:
            print(f"⚠️ Could not create recurring task for '{task['title']}': {e}")
            new_content = _remove_task_line(content, old_line)
    else:
        # Non-recurring: remove the task line entirely
        new_content = _remove_task_line(content, old_line)

    tasks_file.write_text(new_content)
    task_type = "Personal" if args.personal else "Work"
    print(f"✅ Completed {task_type} task: {task['title']}")


def show_blockers(args):
    """Show tasks that are blocking others."""
    _, tasks_data = load_tasks(args.personal)
    blockers = [t for t in tasks_data['all'] if t.get('blocks') and not t['done']]
    
    if args.person:
        blockers = [t for t in blockers if args.person.lower() in t['blocks'].lower()]
    
    if not blockers:
        print("No blocking tasks found.")
        return
    
    print(f"\n🚧 Blocking Tasks ({len(blockers)} items)\n")
    
    for task in blockers:
        print(f"⬜ **{task['title']}**")
        print(f"   Blocks: {task['blocks']}")
        if task.get('due'):
            print(f"   Due: {task['due']}")
        print()


def archive_done(args):
    """Archive completed tasks from daily notes into quarterly file.

    Also cleans any stale [x] lines still on the board (backward compat).
    """
    notes_dir_raw = os.getenv("TASK_TRACKER_DAILY_NOTES_DIR")
    if not notes_dir_raw:
        print(
            "❌ TASK_TRACKER_DAILY_NOTES_DIR is not set. "
            "Set it to the directory containing your daily notes (YYYY-MM-DD.md).",
            file=sys.stderr,
        )
        return

    # Collect completions from daily notes (last 30 days by default)
    today = datetime.now().date()
    start = today - timedelta(days=30)
    notes_tasks = extract_completed_tasks(
        notes_dir=Path(notes_dir_raw),
        start_date=start,
        end_date=today,
    )

    # Also collect any stale [x] items still on the board
    tasks_file, format = get_tasks_file(args.personal)
    stale_board: list[dict] = []
    if tasks_file.exists():
        content = tasks_file.read_text()
        tasks_data = parse_tasks(content, args.personal, format)
        stale_board = tasks_data.get('done', [])

    # Merge (deduplicate by title + date)
    all_done: list[dict] = list(notes_tasks)
    seen = {(t['title'].casefold(), t.get('completed_date', '')) for t in all_done}
    for bt in stale_board:
        key = (bt['title'].casefold(), bt.get('completed_date', ''))
        if key not in seen:
            seen.add(key)
            all_done.append(bt)

    if not all_done:
        print("No completed tasks to archive.")
        return

    # Write to quarterly archive, skipping entries already present
    quarter = get_current_quarter()
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    archive_file = ARCHIVE_DIR / f"ARCHIVE-{quarter}.md"

    if archive_file.exists():
        archive_content = archive_file.read_text()
    else:
        archive_content = f"# Task Archive - {quarter}\n"

    # Build set of titles already archived (case-insensitive) to prevent
    # duplicate entries across repeated runs.
    already_archived: set[str] = set()
    for line in archive_content.splitlines():
        m = re.match(r'^- ✅ \*\*(.+?)\*\*', line)
        if m:
            already_archived.add(m.group(1).strip().casefold())

    new_tasks = [t for t in all_done if t['title'].casefold() not in already_archived]
    if not new_tasks:
        print("All completed tasks are already archived.")
        return

    task_type = "Personal" if args.personal else "Work"
    archive_entry = f"\n## Archived {today.strftime('%Y-%m-%d')} ({task_type})\n\n"
    for task in new_tasks:
        date_suffix = f" ✅ {task['completed_date']}" if task.get('completed_date') else ""
        area_suffix = f" [{task.get('area')}]" if task.get('area') else ""
        archive_entry += f"- ✅ **{task['title']}**{area_suffix}{date_suffix}\n"

    archive_content += archive_entry
    archive_file.write_text(archive_content)

    # Clean stale [x] lines from the board
    removed = 0
    if stale_board and tasks_file.exists():
        board_content = tasks_file.read_text()
        for task in stale_board:
            raw_line = task.get('raw_line', '')
            if raw_line and raw_line in board_content:
                board_content = _remove_task_line(board_content, raw_line)
                removed += 1
        tasks_file.write_text(board_content)

    total = len(new_tasks)
    extra = f" (cleaned {removed} stale lines from board)" if removed else ""
    print(f"✅ Archived {total} {task_type} tasks to {archive_file.name}{extra}")


def main():
    parser = argparse.ArgumentParser(description='Task Tracker CLI (Work & Personal)')
    parser.add_argument('--personal', action='store_true', help='Use Personal Tasks instead of Work Tasks')
    
    subparsers = parser.add_subparsers(dest='command', required=True)
    
    # List command
    list_parser = subparsers.add_parser('list', help='List tasks')
    list_parser.add_argument('--priority', choices=['high', 'medium', 'low'])
    list_parser.add_argument('--status', choices=['open', 'done'])
    list_parser.add_argument('--due', choices=['today', 'this-week', 'overdue', 'due-or-overdue'])
    list_parser.add_argument('--completed-since', choices=['24h', '7d', '30d'])
    list_parser.set_defaults(func=list_tasks)
    
    # Add command
    add_parser = subparsers.add_parser('add', help='Add a task')
    add_parser.add_argument('title', help='Task title')
    add_parser.add_argument('--priority', default='medium', choices=['high', 'medium', 'low'])
    add_parser.add_argument('--due', help='Due date (YYYY-MM-DD)')
    add_parser.add_argument('--owner', default='me')
    add_parser.add_argument('--area', help='Area/category')
    add_parser.set_defaults(func=add_task)
    
    # Done command
    done_parser = subparsers.add_parser('done', help='Mark task as done')
    done_parser.add_argument('query', help='Task title (fuzzy match)')
    done_parser.set_defaults(func=done_task)
    
    # Blockers command
    blockers_parser = subparsers.add_parser('blockers', help='Show blocking tasks')
    blockers_parser.add_argument('--person', help='Filter by person being blocked')
    blockers_parser.set_defaults(func=show_blockers)
    
    # Archive command
    archive_parser = subparsers.add_parser('archive', help='Archive completed tasks')
    archive_parser.set_defaults(func=archive_done)
    
    args = parser.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()
