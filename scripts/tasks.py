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
import json
import os
import re
import sys
from difflib import SequenceMatcher
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).parent))
from daily_notes import extract_completed_tasks
from log_done import log_task_completed
from standup_common import get_calendar_events, flatten_calendar_events
import delegation
from utils import (
    detect_format,
    get_tasks_file,
    get_section_display_name,
    parse_tasks,
    load_tasks,
    check_due_date,
    next_recurrence_date,
    get_current_quarter,
    ARCHIVE_DIR,
    get_objective_progress,
)

TASK_PRIMITIVES_SCHEMA_VERSION = "v1"
FUZZY_AUTO_LINK_THRESHOLD = 0.90
FUZZY_REVIEW_THRESHOLD = 0.70


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
            'high': {'section': 'q1', 'tags': {'high', 'urgent'}},
            'medium': {'section': 'q2', 'tags': {'medium'}},
            'low': {'section': 'backlog', 'tags': {'low'}},
        }
        target = priority_map.get(args.priority.lower())
        if target:
            filtered = [
                t for t in filtered
                if t.get('section') == target['section'] or t.get('priority') in target['tags']
            ]
    
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
    """Remove a task line and its child/continuation lines."""
    lines = content.split('\n')
    try:
        target_index = lines.index(raw_line)
    except ValueError:
        return content

    target_indent = len(raw_line) - len(raw_line.lstrip(' '))
    remove_until = target_index + 1

    while remove_until < len(lines):
        line = lines[remove_until]

        if line.strip() == '':
            lookahead = remove_until + 1
            while lookahead < len(lines) and lines[lookahead].strip() == '':
                lookahead += 1

            if lookahead < len(lines):
                next_line = lines[lookahead]
                next_indent = len(next_line) - len(next_line.lstrip(' '))
                if next_indent > target_indent:
                    remove_until += 1
                    continue
            break

        indent = len(line) - len(line.lstrip(' '))
        if indent > target_indent:
            remove_until += 1
            continue
        break

    return '\n'.join(lines[:target_index] + lines[remove_until:])


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

    # Build set of (title, completed_date) already archived to prevent
    # duplicate entries across repeated runs while preserving recurring
    # tasks completed on different dates.
    already_archived: set[tuple[str, str]] = set()
    for line in archive_content.splitlines():
        m = re.match(r'^- ✅ \*\*(.+?)\*\*', line)
        if m:
            title_key = m.group(1).strip().casefold()
            date_m = re.search(r'✅\s*(\d{4}-\d{2}-\d{2})\s*$', line)
            date_key = date_m.group(1) if date_m else ''
            already_archived.add((title_key, date_key))

    new_tasks = [
        t for t in all_done
        if (t['title'].casefold(), t.get('completed_date') or '') not in already_archived
    ]
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


def cmd_delegated(args):
    """Dispatch delegated subcommands."""
    import delegation

    sub = args.del_command
    path = delegation.resolve_delegation_file()
    delegation.ensure_file(path)

    if sub == 'list':
        if getattr(args, 'json', False):
            print(delegation.list_items_json(path, overdue_only=getattr(args, 'overdue', False)))
        else:
            items = delegation.list_items(path, overdue_only=getattr(args, 'overdue', False))
            if not items:
                print("No delegated tasks.")
                return
            for it in items:
                icon = '⏰' if it.get('status') == 'overdue' else '📋'
                dept = f" #{it['department']}" if it.get('department') else ''
                fu = f" [followup::{it['followup']}]" if it.get('followup') else ''
                print(f"{it['id']:2d}. {icon} {it['title']} → {it.get('assignee', '?')}{dept}{fu}")
    elif sub == 'add':
        item = delegation.add_item(path, args.task, args.to, args.followup, args.dept)
        print(f"✅ Delegated: {item['title']} → {item['assignee']} [followup::{item['followup']}]")
    elif sub == 'complete':
        try:
            item = delegation.complete_item(path, args.id)
            print(f"✅ Completed: {item['title']} → {item.get('assignee', '?')}")
        except ValueError as e:
            print(f"❌ {e}")
            sys.exit(1)
    elif sub == 'extend':
        try:
            item = delegation.extend_item(path, args.id, args.followup)
            print(f"✅ Extended: {item['title']} [new followup::{item['followup']}]")
        except ValueError as e:
            print(f"❌ {e}")
            sys.exit(1)
    elif sub == 'take-back':
        try:
            item = delegation.get_active_item(path, args.id)
            # Re-insert into work tasks first; only delete delegated entry after write succeeds.
            tasks_file, _ = get_tasks_file(personal=False)
            content = tasks_file.read_text()
            dept_tag = f" #{item.get('department')}" if item.get('department') else ''
            task_line = f"- [ ] **{item['title']}**{dept_tag}"
            # Insert at beginning of first section
            lines = content.split('\n')
            insert_at = 0
            for i, line in enumerate(lines):
                if re.match(r'^- \[', line):
                    insert_at = i
                    break
                if line.startswith('## '):
                    insert_at = i + 1
            lines.insert(insert_at, task_line)
            tasks_file.write_text('\n'.join(lines))
            delegation.take_back_item(path, args.id)
            print(f"✅ Took back: {item['title']} (added to {tasks_file.name})")
        except ValueError as e:
            print(f"❌ {e}")
            sys.exit(1)


def cmd_parking_lot(args):
    """Dispatch parking-lot subcommands."""
    from parking_lot import list_items, list_stale, add_item, promote_item, drop_item

    tasks_file, _ = get_tasks_file(args.personal)
    sub = args.pl_command

    if sub == 'list':
        print(list_items(tasks_file))
    elif sub == 'add':
        print(add_item(tasks_file, args.title, dept=args.dept, priority=args.priority))
    elif sub == 'stale':
        print(list_stale(tasks_file))
    elif sub == 'promote':
        print(promote_item(tasks_file, args.id))
    elif sub == 'drop':
        archive_dir = Path(os.getenv(
            'TASK_TRACKER_ARCHIVE_DIR',
            str(tasks_file.parent / 'Done Archive')
        ))
        print(drop_item(tasks_file, args.id, archive_dir=archive_dir))


def _find_open_task(personal: bool, query: str) -> tuple[Path, dict | None, str]:
    tasks_file, fmt = get_tasks_file(personal)
    if not tasks_file.exists():
        return tasks_file, None, f"❌ Tasks file not found: {tasks_file}"
    content = tasks_file.read_text()
    tasks_data = parse_tasks(content, personal, fmt)
    matches = [t for t in tasks_data.get('all', []) if not t.get('done') and query.lower() in t.get('title', '').lower()]
    if not matches:
        return tasks_file, None, f"❌ No open task matches: {query}"
    if len(matches) > 1:
        return tasks_file, None, f"❌ Multiple matches for '{query}'. Be more specific."
    return tasks_file, matches[0], ""


def cmd_state(args):
    """First-class state transitions: pause/delegate/backlog/drop."""
    from parking_lot import add_item

    tasks_file, task, err = _find_open_task(args.personal, args.query)
    if err:
        print(err)
        return

    content = tasks_file.read_text()
    old_line = task.get('raw_line', '')
    if not old_line:
        print("❌ Task has no raw line; cannot transition.")
        return

    if args.state_command == 'pause':
        new_line = old_line if 'paused::' in old_line else f"{old_line} paused::{datetime.now().date().isoformat()}"
        if args.until:
            if 'pause_until::' in new_line:
                new_line = re.sub(r'pause_until::\d{4}-\d{2}-\d{2}', f'pause_until::{args.until}', new_line)
            else:
                new_line = f"{new_line} pause_until::{args.until}"
        tasks_file.write_text(content.replace(old_line, new_line, 1))
        print(f"✅ Paused: {task['title']}")
        return

    if args.state_command == 'delegate':
        item = delegation.add_item(delegation.resolve_delegation_file(), task['title'], args.to, args.followup, task.get('department'))
        tasks_file.write_text(_remove_task_line(content, old_line))
        print(f"✅ Delegated: {item['title']} → {item['assignee']} [followup::{item['followup']}]")
        return

    if args.state_command == 'backlog':
        pri = args.priority or task.get('priority') or 'low'
        msg = add_item(tasks_file, task['title'], dept=args.dept or task.get('department'), priority=pri)
        if not msg.startswith('✅'):
            print(f"❌ Backlog move failed: {msg}", file=sys.stderr)
            return
        tasks_file.write_text(_remove_task_line(tasks_file.read_text(), old_line))
        print(f"✅ Backlog: {task['title']} ({msg})")
        return

    if args.state_command == 'drop':
        archive_dir = Path(os.getenv('TASK_TRACKER_ARCHIVE_DIR', str(tasks_file.parent / 'Done Archive')))
        archive_dir.mkdir(parents=True, exist_ok=True)
        archive_file = archive_dir / f"ARCHIVE-{get_current_quarter()}.md"
        entry = f"- [x] ~~{task['title']}~~ (dropped) ✅ {datetime.now().date().isoformat()}\n"
        with archive_file.open('a', encoding='utf-8') as fh:
            fh.write(entry)
        tasks_file.write_text(_remove_task_line(content, old_line))
        print(f"✅ Dropped: {task['title']}")
        return


def cmd_promote_from_backlog(args):
    from parking_lot import promote_item
    tasks_file, _ = get_tasks_file(args.personal)
    cap = max(int(args.cap or 1), 1)
    promoted = []
    for _ in range(cap):
        out = promote_item(tasks_file, 1)
        if out.startswith('✅'):
            promoted.append(out)
        else:
            break
    if not promoted:
        print("No backlog items promoted.")
    else:
        for row in promoted:
            print(row)


def cmd_review_backlog(args):
    from parking_lot import list_stale
    old = os.getenv('PARKING_LOT_STALE_DAYS')
    os.environ['PARKING_LOT_STALE_DAYS'] = str(args.stale_days)
    try:
        raw = list_stale(get_tasks_file(args.personal)[0])
    finally:
        if old is None:
            del os.environ['PARKING_LOT_STALE_DAYS']
        else:
            os.environ['PARKING_LOT_STALE_DAYS'] = old
    if args.json:
        print(raw)
        return
    items = json.loads(raw)
    if not items:
        print(f"No stale backlog items (threshold: {args.stale_days}d).")
        return
    print(f"Stale backlog items ({len(items)}):")
    for it in items:
        print(f"- #{it['id']} {it['title']} ({it['age_days']}d)")


def _calendar_classification(task: dict) -> str:
    raw = str(task.get('raw_line') or '').lower()
    title = str(task.get('title') or '').lower()
    if 'status::blocked' in raw or 'depends::' in raw:
        return 'blocked'
    if '#private' in raw or 'private::true' in raw:
        return 'private'
    if 'buffer' in title or 'buffer::true' in raw:
        return 'buffer'
    return 'normal'


def cmd_calendar_sync(args):
    """Calendar sync payload for orchestration consumers."""
    _, tasks_data = load_tasks(args.personal)
    events = flatten_calendar_events(get_calendar_events())
    meetings = []
    for task in tasks_data.get('all', []):
        raw = str(task.get('raw_line') or '')
        if 'meeting::' not in raw:
            continue
        status_match = re.search(r'status::(scheduled|done|canceled|blocked)', raw, flags=re.IGNORECASE)
        status = status_match.group(1).lower() if status_match else ('done' if task.get('done') else 'scheduled')
        meetings.append({
            'title': task.get('title', ''),
            'status': status,
            'classification': _calendar_classification(task),
            'done': bool(task.get('done')),
        })

    payload = {
        'command': 'calendar sync',
        'idempotent': True,
        'events_seen': len(events),
        'meetings': meetings,
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"Synced {len(meetings)} meeting task(s); events seen: {len(events)}")


def cmd_calendar_resolve(args):
    """Resolve calendar lifecycle from note completions in a date window."""
    _, tasks_data = load_tasks(args.personal)
    today = datetime.now().date()
    if args.window == 'today':
        start = end = today
    else:
        start = end = (today - timedelta(days=1))

    notes_dir_raw = os.getenv("TASK_TRACKER_DAILY_NOTES_DIR")
    completed = extract_completed_tasks(Path(notes_dir_raw), start, end) if notes_dir_raw else []
    done_titles = {t.get('title', '').casefold() for t in completed}

    resolved = []
    for task in tasks_data.get('all', []):
        raw = str(task.get('raw_line') or '')
        if 'meeting::' not in raw:
            continue
        title = task.get('title', '')
        raw_l = raw.lower()
        status = 'done' if title.casefold() in done_titles else 'scheduled'
        if 'status::blocked' in raw_l:
            status = 'blocked'
        if 'status::done' in raw_l or task.get('done'):
            status = 'done'
        if 'status::canceled' in raw_l:
            status = 'canceled'
        resolved.append({'title': title, 'status': status, 'window': args.window})

    payload = {
        'command': 'calendar resolve',
        'window': args.window,
        'resolved': resolved,
        'idempotent': True,
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"Resolved {len(resolved)} meeting lifecycle item(s) for {args.window}")


def cmd_done_scan(args):
    """Scan completed items in a true rolling time window for standup consumers."""
    window_map = {'24h': timedelta(hours=24), '7d': timedelta(days=7), '30d': timedelta(days=30)}
    cutoff = datetime.now() - window_map[args.window]
    end = datetime.now().date()
    start = (cutoff.date() - timedelta(days=1))

    notes_dir_raw = os.getenv("TASK_TRACKER_DAILY_NOTES_DIR")
    items = []
    if notes_dir_raw:
        raw_items = extract_completed_tasks(Path(notes_dir_raw), start, end)
        for item in raw_items:
            try:
                item_date = datetime.strptime(item.get('completed_date', ''), '%Y-%m-%d').date()
            except ValueError:
                continue
            ts = item.get('timestamp')
            if ts:
                try:
                    item_dt = datetime.strptime(f"{item_date.isoformat()} {ts}", '%Y-%m-%d %H:%M')
                except ValueError:
                    item_dt = datetime.combine(item_date, datetime.max.time())
            else:
                item_dt = datetime.combine(item_date, datetime.min.time())
            if item_dt >= cutoff:
                items.append(item)

    payload = {
        'command': 'done scan',
        'window': args.window,
        'count': len(items),
        'items': items,
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"Done items ({args.window}): {len(items)}")


def _new_schema(command: str) -> dict:
    return {
        "schema_version": TASK_PRIMITIVES_SCHEMA_VERSION,
        "command": command,
    }


def _safe_load_tasks(personal: bool = False) -> dict:
    """Load tasks, returning an empty skeleton on failures."""
    empty = {
        "all": [],
        "done": [],
        "q1": [],
        "q2": [],
        "q3": [],
        "backlog": [],
        "today": [],
        "objectives": [],
        "team": [],
        "parking_lot": [],
        "due_today": [],
    }
    tasks_file, fmt = get_tasks_file(personal)
    if not tasks_file.exists():
        return empty
    try:
        content = tasks_file.read_text()
    except OSError:
        return empty
    try:
        return parse_tasks(content, personal, fmt)
    except Exception:
        return empty


def _normalize_title(title: str) -> str:
    lowered = (title or "").strip().casefold()
    lowered = re.sub(r"\[x\]|\[ \]|✅|☑️", " ", lowered)
    lowered = re.sub(r"\*\*|__|~~", "", lowered)
    lowered = re.sub(r"[^\w\s/-]", " ", lowered)
    lowered = re.sub(r"\s+", " ", lowered)
    return lowered.strip()


def _slugify(value: str) -> str:
    normalized = _normalize_title(value)
    slug = re.sub(r"[^a-z0-9]+", "-", normalized)
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    return slug or "task"


def _extract_inline_identifiers(text: str) -> set[str]:
    identifiers: set[str] = set()
    if not text:
        return identifiers

    for match in re.findall(r"\b(?:id|task_id|task)::([A-Za-z0-9._:-]+)", text, flags=re.IGNORECASE):
        identifiers.add(match.casefold())

    for url in re.findall(r"https?://[^\s)>\]]+", text):
        identifiers.add(url.casefold())
        issue_match = re.search(r"/issues/(\d+)\b", url)
        if issue_match:
            identifiers.add(f"issue:{issue_match.group(1)}")

    for match in re.findall(r"\b#(\d+)\b", text):
        identifiers.add(f"issue:{match}")

    return identifiers


def _task_identifier_bundle(task: dict, fallback_id: str) -> dict:
    raw_line = str(task.get("raw_line") or "")
    title = str(task.get("title") or "")
    explicit_id = None
    explicit_match = re.search(
        r"\b(?:id|task_id|task)::([A-Za-z0-9._:-]+)",
        raw_line,
        flags=re.IGNORECASE,
    )
    if explicit_match:
        explicit_id = explicit_match.group(1)

    identifiers = _extract_inline_identifiers(raw_line)
    identifiers.update(_extract_inline_identifiers(title))
    if explicit_id:
        identifiers.add(explicit_id.casefold())

    return {
        "task_id": explicit_id or fallback_id,
        "identifiers": identifiers,
    }


def _canonical_task(task: dict, task_id: str) -> dict:
    return {
        "task_id": task_id,
        "title": task.get("title", ""),
        "done": bool(task.get("done")),
        "section": task.get("section"),
        "area": task.get("area") or task.get("department") or "Uncategorized",
        "priority": task.get("priority"),
        "due": task.get("due"),
        "owner": task.get("owner"),
        "goal": task.get("goal"),
    }


def _group_tasks_by_area(tasks: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for task in tasks:
        key = task.get("area") or "Uncategorized"
        grouped.setdefault(key, []).append(task)
    return dict(sorted(grouped.items(), key=lambda item: item[0].casefold()))


def _group_tasks_by_category(tasks: list[dict]) -> dict[str, list[dict]]:
    labels = {
        "q1": "Q1",
        "q2": "Q2",
        "q3": "Q3",
        "team": "Team",
        "backlog": "Backlog",
        "today": "Today",
        "objectives": "Objectives",
        "parking_lot": "Parking Lot",
    }
    grouped: dict[str, list[dict]] = {}
    for task in tasks:
        section = task.get("section")
        key = labels.get(section, section or "Uncategorized")
        grouped.setdefault(key, []).append(task)
    return dict(sorted(grouped.items(), key=lambda item: item[0].casefold()))


def _parse_range_inputs(week: str | None, start_raw: str | None, end_raw: str | None) -> tuple[date, date, str]:
    if start_raw or end_raw:
        if not start_raw or not end_raw:
            raise ValueError("Both --start and --end are required together.")
        try:
            start_date = datetime.strptime(start_raw, "%Y-%m-%d").date()
            end_date = datetime.strptime(end_raw, "%Y-%m-%d").date()
        except ValueError as exc:
            raise ValueError("Invalid date. Use YYYY-MM-DD.") from exc
        if start_date > end_date:
            start_date, end_date = end_date, start_date
        return start_date, end_date, "custom-range"

    today = datetime.now().date()
    if not week:
        start_date = today - timedelta(days=today.weekday())
        return start_date, start_date + timedelta(days=6), "current-week"

    match = re.fullmatch(r"(\d{4})-W(\d{2})", week)
    if not match:
        raise ValueError("Invalid --week format. Use YYYY-WNN (example: 2026-W07).")
    start_date = date.fromisocalendar(int(match.group(1)), int(match.group(2)), 1)
    return start_date, start_date + timedelta(days=6), "iso-week"


def _extract_done_lines(content: str) -> list[dict]:
    parsed: list[dict] = []
    for raw in content.splitlines():
        line = raw.strip()
        if not line:
            continue

        is_checkbox = bool(re.match(r"^\s*[-*+]\s+\[(?:x|X| )\]\s+", raw))
        is_checked = bool(re.match(r"^\s*[-*+]\s+\[(?:x|X)\]\s+", raw))

        is_plain_bullet = bool(re.match(r"^\s*[-*+]\s+", raw))
        if is_checkbox and not is_checked:
            continue
        if not is_checkbox and not is_plain_bullet and not line.startswith("✅"):
            # Plain lines are accepted as completed actions too.
            pass

        cleaned = re.sub(r"^\s*[-*+]\s+", "", raw).strip()
        cleaned = re.sub(r"^\[(?:x|X| )\]\s+", "", cleaned)
        cleaned = re.sub(r"^✅\s*", "", cleaned)
        cleaned = re.sub(r"\s*✅\s*\d{4}-\d{2}-\d{2}\s*$", "", cleaned)
        cleaned = cleaned.strip()
        if not cleaned:
            continue

        parsed.append(
            {
                "raw_line": raw.rstrip("\n"),
                "title": cleaned,
                "normalized_title": _normalize_title(cleaned),
                "identifiers": _extract_inline_identifiers(cleaned),
            }
        )
    return parsed


def _fuzzy_score(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left, right).ratio()


def _build_task_catalog(tasks_data: dict) -> list[dict]:
    catalog: list[dict] = []
    for idx, task in enumerate(tasks_data.get("all", []), start=1):
        fallback_id = f"{_slugify(task.get('title', 'task'))}-{idx:03d}"
        bundle = _task_identifier_bundle(task, fallback_id=fallback_id)
        canonical = _canonical_task(task, bundle["task_id"])
        catalog.append(
            {
                "task": task,
                "canonical": canonical,
                "normalized_title": _normalize_title(canonical["title"]),
                "identifiers": bundle["identifiers"],
            }
        )
    return catalog


def _ingest_match_line(
    line: dict,
    catalog: list[dict],
    auto_threshold: float,
    review_threshold: float,
) -> dict:
    for candidate in catalog:
        if line["identifiers"] and (line["identifiers"] & candidate["identifiers"]):
            return {
                "raw_line": line["raw_line"],
                "parsed_title": line["title"],
                "normalized_title": line["normalized_title"],
                "canonical_task": candidate["canonical"],
                "match_metadata": {
                    "matched_task_id": candidate["canonical"]["task_id"],
                    "score": 1.0,
                    "decision": "auto-link",
                    "match_type": "exact-id-or-link",
                },
            }

    exact_title_matches = [
        candidate for candidate in catalog if candidate["normalized_title"] == line["normalized_title"]
    ]
    if exact_title_matches:
        chosen = sorted(exact_title_matches, key=lambda c: c["canonical"]["task_id"])[0]
        return {
            "raw_line": line["raw_line"],
            "parsed_title": line["title"],
            "normalized_title": line["normalized_title"],
            "canonical_task": chosen["canonical"],
            "match_metadata": {
                "matched_task_id": chosen["canonical"]["task_id"],
                "score": 1.0,
                "decision": "auto-link",
                "match_type": "normalized-title",
            },
        }

    scored = []
    for candidate in catalog:
        score = _fuzzy_score(line["normalized_title"], candidate["normalized_title"])
        scored.append((score, candidate["canonical"]["task_id"], candidate))
    scored.sort(key=lambda item: (-item[0], item[1]))
    best_score, _, best = scored[0] if scored else (0.0, "", None)

    decision = "no-match"
    if best and best_score >= auto_threshold:
        decision = "auto-link"
    elif best and best_score >= review_threshold:
        decision = "needs-review"

    return {
        "raw_line": line["raw_line"],
        "parsed_title": line["title"],
        "normalized_title": line["normalized_title"],
        "canonical_task": best["canonical"] if best and decision != "no-match" else None,
        "match_metadata": {
            "matched_task_id": best["canonical"]["task_id"] if best and decision != "no-match" else None,
            "score": round(float(best_score), 4),
            "decision": decision,
            "match_type": "fuzzy",
        },
    }


def cmd_standup_summary(args):
    tasks_data = _safe_load_tasks(args.personal)
    today = datetime.now().date()

    notes_dir_raw = os.getenv("TASK_TRACKER_DAILY_NOTES_DIR")
    dones: list[dict] = []
    if notes_dir_raw:
        notes_items = extract_completed_tasks(Path(notes_dir_raw), today - timedelta(days=1), today)
        dones = [
            {
                "title": item.get("title", ""),
                "completed_date": item.get("completed_date"),
                "timestamp": item.get("timestamp"),
                "area": item.get("area") or "Uncategorized",
            }
            for item in notes_items
        ]
    else:
        dones = [
            {
                "title": task.get("title", ""),
                "completed_date": task.get("completed_date"),
                "timestamp": None,
                "area": task.get("area") or task.get("department") or "Uncategorized",
            }
            for task in tasks_data.get("done", [])
        ]

    dos_raw = [
        task
        for task in tasks_data.get("all", [])
        if not task.get("done") and task.get("section") in {"q1", "q2", "today"}
    ]
    dos = [_canonical_task(task, _task_identifier_bundle(task, _slugify(task.get("title", "")))["task_id"]) for task in dos_raw]

    overdue_raw = []
    for task in tasks_data.get("all", []):
        if task.get("done") or not task.get("due"):
            continue
        try:
            due_date = datetime.strptime(task.get("due"), "%Y-%m-%d").date()
        except ValueError:
            continue
        if due_date < today:
            overdue_raw.append(task)
    overdue = [_canonical_task(task, _task_identifier_bundle(task, _slugify(task.get("title", "")))["task_id"]) for task in overdue_raw]

    carryover_suggestions = []
    for task in overdue_raw:
        carryover_suggestions.append(
            {
                "title": task.get("title", ""),
                "reason": "overdue",
                "suggestion": "carry-to-today",
                "due": task.get("due"),
                "area": task.get("area") or task.get("department") or "Uncategorized",
            }
        )

    payload = _new_schema("standup-summary")
    payload.update(
        {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "date": today.isoformat(),
            "dones": dones,
            "dos": dos,
            "overdue": overdue,
            "carryover_suggestions": carryover_suggestions,
            "groups": {
                "dones_by_area": _group_tasks_by_area(dones),
                "dos_by_area": _group_tasks_by_area(dos),
                "overdue_by_area": _group_tasks_by_area(overdue),
                "dos_by_category": _group_tasks_by_category(dos),
            },
        }
    )
    print(json.dumps(payload, indent=2))


def cmd_weekly_review_summary(args):
    try:
        start_date, end_date, selection_mode = _parse_range_inputs(args.week, args.start, args.end)
    except ValueError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        sys.exit(2)

    tasks_data = _safe_load_tasks(args.personal)
    notes_dir_raw = os.getenv("TASK_TRACKER_DAILY_NOTES_DIR")

    done_items: list[dict] = []
    if notes_dir_raw:
        note_tasks = extract_completed_tasks(Path(notes_dir_raw), start_date, end_date)
        done_items = [
            {
                "task_id": None,
                "title": item.get("title", ""),
                "done": True,
                "section": "done",
                "area": item.get("area") or "Uncategorized",
                "priority": item.get("priority"),
                "due": item.get("due"),
                "owner": None,
                "goal": None,
                "completed_date": item.get("completed_date"),
            }
            for item in note_tasks
        ]
    else:
        for task in tasks_data.get("done", []):
            completed = task.get("completed_date")
            if not completed:
                continue
            try:
                completed_date = datetime.strptime(completed, "%Y-%m-%d").date()
            except ValueError:
                continue
            if start_date <= completed_date <= end_date:
                row = _canonical_task(task, _task_identifier_bundle(task, _slugify(task.get("title", "")))["task_id"])
                row["completed_date"] = completed
                done_items.append(row)

    do_items = []
    for task in tasks_data.get("all", []):
        if task.get("done"):
            continue
        due_raw = task.get("due")
        if due_raw:
            try:
                due_date = datetime.strptime(due_raw, "%Y-%m-%d").date()
            except ValueError:
                continue
            if due_date < start_date or due_date > end_date:
                continue
        do_items.append(_canonical_task(task, _task_identifier_bundle(task, _slugify(task.get("title", "")))["task_id"]))

    payload = _new_schema("weekly-review-summary")
    payload.update(
        {
            "range": {
                "mode": selection_mode,
                "week": args.week,
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
            },
            "DONE": {
                "items": done_items,
                "by_area": _group_tasks_by_area(done_items),
                "by_category": _group_tasks_by_category(done_items),
            },
            "DO": {
                "items": do_items,
                "by_area": _group_tasks_by_area(do_items),
                "by_category": _group_tasks_by_category(do_items),
            },
        }
    )
    print(json.dumps(payload, indent=2))


def cmd_ingest_daily_log(args):
    if args.file:
        source_content = Path(args.file).read_text(encoding="utf-8")
        source = {"type": "file", "path": str(Path(args.file))}
    else:
        source_content = sys.stdin.read()
        source = {"type": "stdin"}

    parsed_lines = _extract_done_lines(source_content)
    tasks_data = _safe_load_tasks(args.personal)
    catalog = _build_task_catalog(tasks_data)
    auto_threshold = float(args.auto_threshold)
    review_threshold = float(args.review_threshold)
    if review_threshold > auto_threshold:
        print("❌ --review-threshold cannot be greater than --auto-threshold", file=sys.stderr)
        sys.exit(2)

    matched = [
        _ingest_match_line(line, catalog, auto_threshold=auto_threshold, review_threshold=review_threshold)
        for line in parsed_lines
    ]

    counts = {"auto-link": 0, "needs-review": 0, "no-match": 0}
    for item in matched:
        counts[item["match_metadata"]["decision"]] += 1

    payload = _new_schema("ingest-daily-log")
    payload.update(
        {
            "source": source,
            "thresholds": {
                "auto_link": auto_threshold,
                "needs_review": review_threshold,
            },
            "totals": {
                "input_lines": len(source_content.splitlines()),
                "parsed_done_lines": len(parsed_lines),
                "auto_linked": counts["auto-link"],
                "needs_review": counts["needs-review"],
                "no_match": counts["no-match"],
            },
            "items": matched,
        }
    )
    print(json.dumps(payload, indent=2))


def cmd_calendar_sync_primitive(args):
    payload = _new_schema("calendar-sync")
    warnings: list[str] = []
    events = []
    meetings = []

    try:
        events = flatten_calendar_events(get_calendar_events())
    except Exception:
        warnings.append("calendar-events-unavailable")

    try:
        tasks_data = _safe_load_tasks(args.personal)
        for task in tasks_data.get("all", []):
            raw = str(task.get("raw_line") or "")
            if "meeting::" not in raw:
                continue
            raw_l = raw.lower()
            status = "scheduled"
            if task.get("done") or "status::done" in raw_l:
                status = "done"
            elif "status::canceled" in raw_l:
                status = "canceled"
            elif "status::blocked" in raw_l:
                status = "blocked"

            meetings.append(
                {
                    "task_id": _task_identifier_bundle(task, _slugify(task.get("title", "")))["task_id"],
                    "title": task.get("title", ""),
                    "status": status,
                    "classification": _calendar_classification(task),
                }
            )
    except Exception:
        warnings.append("task-meetings-unavailable")

    lifecycle_map = {
        "scheduled": [m for m in meetings if m["status"] == "scheduled"],
        "done": [m for m in meetings if m["status"] == "done"],
        "blocked": [m for m in meetings if m["status"] == "blocked"],
        "canceled": [m for m in meetings if m["status"] == "canceled"],
    }

    payload.update(
        {
            "idempotent": True,
            "optional_helper": True,
            "warnings": warnings,
            "events_seen": len(events),
            "meetings_seen": len(meetings),
            "lifecycle_map": lifecycle_map,
        }
    )
    print(json.dumps(payload, indent=2))


def _daily_note_link(which: str) -> dict:
    rel_dir = os.getenv('TASK_TRACKER_DAILY_NOTES_RELATIVE_DIR', '01-TODOs/Daily').strip('/')
    vault = os.getenv('TASK_TRACKER_OBSIDIAN_VAULT', 'Obsidian')
    offset = 0 if which == 'today' else -1
    target = date.today() + timedelta(days=offset)
    rel = f"{rel_dir}/{target.isoformat()}.md"
    enc_vault = quote(vault, safe='')
    enc_rel = quote(rel, safe='')
    return {
        'date': target.isoformat(),
        'universal': f"https://obsidian.md/open?vault={enc_vault}&file={enc_rel}",
        'deep': f"obsidian://open?vault={enc_vault}&file={enc_rel}",
    }


def cmd_daily_links(args):
    payload = {
        'command': 'daily links',
        'window': args.window,
        'links': {args.window: _daily_note_link(args.window)},
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(payload['links'][args.window]['deep'])


def _format_completion_pct(value: float) -> str:
    """Format completion percentage for human-readable output."""
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.1f}"


def cmd_objectives(args):
    """Show objective-level completion status."""
    content, tasks_data = load_tasks(args.personal)
    parsed_format = detect_format(content)
    if parsed_format != 'objectives':
        print("Objective tracking is only available for Objectives format files.")
        return

    objectives = get_objective_progress(tasks_data)
    if args.at_risk:
        objectives = [
            objective for objective in objectives
            if objective['total_tasks'] > 0 and objective['completed_tasks'] == 0
        ]

    if args.json:
        print(json.dumps(objectives, indent=2))
        return

    if not objectives:
        print("No objectives found.")
        return

    for objective in objectives:
        pct = _format_completion_pct(objective['completion_pct'])
        dept = f" #{objective['department']}" if objective.get('department') else ""
        priority = f" #{objective['priority']}" if objective.get('priority') else ""
        print(
            f"🎯 {objective['title']} — {pct}% "
            f"({objective['completed_tasks']}/{objective['total_tasks']}){dept}{priority}"
        )
        for task in objective['tasks']:
            mark = "✅" if task['done'] else "⬜"
            print(f"  {mark} {task['title']}")
        print()


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
    
    done_scan_parser = subparsers.add_parser('done-scan', help='Scan completed items from daily notes')
    done_scan_parser.add_argument('--window', choices=['24h', '7d', '30d'], default='24h')
    done_scan_parser.add_argument('--json', action='store_true')
    done_scan_parser.set_defaults(func=cmd_done_scan)

    daily_links_parser = subparsers.add_parser('daily-links', help='Generate daily note links')
    daily_links_parser.add_argument('--window', choices=['today', 'yesterday'], default='today')
    daily_links_parser.add_argument('--json', action='store_true')
    daily_links_parser.set_defaults(func=cmd_daily_links)

    standup_summary_parser = subparsers.add_parser(
        'standup-summary',
        help='Return standup primitive summary JSON',
    )
    standup_summary_parser.set_defaults(func=cmd_standup_summary)

    weekly_review_summary_parser = subparsers.add_parser(
        'weekly-review-summary',
        help='Return weekly review primitive summary JSON',
    )
    weekly_review_summary_parser.add_argument('--week', help='ISO week to review (YYYY-WNN)')
    weekly_review_summary_parser.add_argument('--start', help='Start date (YYYY-MM-DD)')
    weekly_review_summary_parser.add_argument('--end', help='End date (YYYY-MM-DD)')
    weekly_review_summary_parser.set_defaults(func=cmd_weekly_review_summary)

    ingest_daily_log_parser = subparsers.add_parser(
        'ingest-daily-log',
        help='Ingest done lines and map to canonical tasks',
    )
    ingest_daily_log_parser.add_argument('--file', help='Input log file; default is stdin')
    ingest_daily_log_parser.add_argument(
        '--auto-threshold',
        type=float,
        default=FUZZY_AUTO_LINK_THRESHOLD,
        help='Fuzzy score threshold for auto-linking',
    )
    ingest_daily_log_parser.add_argument(
        '--review-threshold',
        type=float,
        default=FUZZY_REVIEW_THRESHOLD,
        help='Fuzzy score threshold for needs-review',
    )
    ingest_daily_log_parser.set_defaults(func=cmd_ingest_daily_log)

    calendar_sync_parser = subparsers.add_parser(
        'calendar-sync',
        help='Optional helper payload for calendar lifecycle mapping',
    )
    calendar_sync_parser.set_defaults(func=cmd_calendar_sync_primitive)

    # Blockers command
    blockers_parser = subparsers.add_parser('blockers', help='Show blocking tasks')
    blockers_parser.add_argument('--person', help='Filter by person being blocked')
    blockers_parser.set_defaults(func=show_blockers)
    
    # Archive command
    archive_parser = subparsers.add_parser('archive', help='Archive completed tasks')
    archive_parser.set_defaults(func=archive_done)

    calendar_parser = subparsers.add_parser('calendar', help='Calendar domain commands')
    calendar_sub = calendar_parser.add_subparsers(dest='calendar_command', required=True)

    cal_sync = calendar_sub.add_parser('sync', help='Sync calendar meeting classification/lifecycle')
    cal_sync.add_argument('--json', action='store_true', help='Output as JSON')
    cal_sync.set_defaults(func=cmd_calendar_sync)

    cal_resolve = calendar_sub.add_parser('resolve', help='Resolve calendar task lifecycle')
    cal_resolve.add_argument('--window', choices=['today', 'yesterday'], default='today')
    cal_resolve.add_argument('--json', action='store_true', help='Output as JSON')
    cal_resolve.set_defaults(func=cmd_calendar_resolve)

    objectives_parser = subparsers.add_parser('objectives', help='Show objective progress')
    objectives_parser.add_argument('--json', action='store_true', help='Output as JSON')
    objectives_parser.add_argument(
        '--at-risk',
        action='store_true',
        help='Show only objectives with 0% completion',
    )
    objectives_parser.set_defaults(func=cmd_objectives)
    
    # Parking Lot subcommands
    pl_parser = subparsers.add_parser('parking-lot', help='Manage parking lot (backlog)')
    pl_sub = pl_parser.add_subparsers(dest='pl_command', required=True)

    pl_sub.add_parser('list', help='List parking lot items').set_defaults(func=cmd_parking_lot)

    pl_add = pl_sub.add_parser('add', help='Add item to parking lot')
    pl_add.add_argument('title', help='Task title')
    pl_add.add_argument('--dept', help='Department tag (Dev, Sales, etc.)')
    pl_add.add_argument('--priority', default='low', choices=['urgent', 'high', 'medium', 'low'])
    pl_add.set_defaults(func=cmd_parking_lot)

    pl_sub.add_parser('stale', help='List stale items (JSON)').set_defaults(func=cmd_parking_lot)

    pl_promote = pl_sub.add_parser('promote', help='Promote item to objectives')
    pl_promote.add_argument('id', type=int, help='Item ID from list')
    pl_promote.set_defaults(func=cmd_parking_lot)

    pl_drop = pl_sub.add_parser('drop', help='Drop item (archive as dropped)')
    pl_drop.add_argument('id', type=int, help='Item ID from list')
    pl_drop.set_defaults(func=cmd_parking_lot)

    # Delegated subcommands
    del_parser = subparsers.add_parser('delegated', help='Manage delegated tasks')
    del_sub = del_parser.add_subparsers(dest='del_command', required=True)

    del_list = del_sub.add_parser('list', help='List delegated items')
    del_list.add_argument('--overdue', action='store_true', help='Show only overdue items')
    del_list.add_argument('--json', action='store_true', help='JSON output')
    del_list.set_defaults(func=cmd_delegated)

    del_add = del_sub.add_parser('add', help='Delegate a task')
    del_add.add_argument('task', help='Task title')
    del_add.add_argument('--to', required=True, help='Person to delegate to')
    del_add.add_argument('--followup', required=True, help='Follow-up date (YYYY-MM-DD)')
    del_add.add_argument('--dept', help='Department tag')
    del_add.set_defaults(func=cmd_delegated)

    del_complete = del_sub.add_parser('complete', help='Mark delegation as complete')
    del_complete.add_argument('id', type=int, help='Item ID from list')
    del_complete.set_defaults(func=cmd_delegated)

    del_extend = del_sub.add_parser('extend', help='Extend follow-up date')
    del_extend.add_argument('id', type=int, help='Item ID from list')
    del_extend.add_argument('--followup', required=True, help='New follow-up date (YYYY-MM-DD)')
    del_extend.set_defaults(func=cmd_delegated)

    del_takeback = del_sub.add_parser('take-back', help='Take back delegated task')
    del_takeback.add_argument('id', type=int, help='Item ID from list')
    del_takeback.set_defaults(func=cmd_delegated)

    state_parser = subparsers.add_parser('state', help='Transition active task state')
    state_sub = state_parser.add_subparsers(dest='state_command', required=True)

    st_pause = state_sub.add_parser('pause', help='Pause an active task')
    st_pause.add_argument('query', help='Task title query')
    st_pause.add_argument('--until', help='Optional resume date YYYY-MM-DD')
    st_pause.set_defaults(func=cmd_state)

    st_delegate = state_sub.add_parser('delegate', help='Delegate an active task')
    st_delegate.add_argument('query', help='Task title query')
    st_delegate.add_argument('--to', required=True, help='Assignee')
    st_delegate.add_argument('--followup', required=True, help='Follow-up date YYYY-MM-DD')
    st_delegate.set_defaults(func=cmd_state)

    st_backlog = state_sub.add_parser('backlog', help='Move active task to backlog')
    st_backlog.add_argument('query', help='Task title query')
    st_backlog.add_argument('--dept', help='Department tag')
    st_backlog.add_argument('--priority', choices=['urgent', 'high', 'medium', 'low'])
    st_backlog.set_defaults(func=cmd_state)

    st_drop = state_sub.add_parser('drop', help='Drop active task and archive as dropped')
    st_drop.add_argument('query', help='Task title query')
    st_drop.set_defaults(func=cmd_state)

    promote_parser = subparsers.add_parser('promote-from-backlog', help='Promote top backlog item(s)')
    promote_parser.add_argument('--cap', type=int, default=1, help='Max items to promote')
    promote_parser.set_defaults(func=cmd_promote_from_backlog)

    review_parser = subparsers.add_parser('review-backlog', help='Review stale backlog items')
    review_parser.add_argument('--stale-days', type=int, default=int(os.getenv('PARKING_LOT_STALE_DAYS', '30')))
    review_parser.add_argument('--json', action='store_true')
    review_parser.set_defaults(func=cmd_review_backlog)

    args = parser.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()
