#!/usr/bin/env python3
"""
Task Tracker CLI - CRUD operations for work tasks.
"""

import argparse
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

TASKS_FILE = Path.home() / "clawd" / "memory" / "work" / "TASKS.md"
ARCHIVE_DIR = Path.home() / "clawd" / "memory" / "work"


def get_current_quarter():
    """Return current quarter string like '2026-Q1'."""
    now = datetime.now()
    quarter = (now.month - 1) // 3 + 1
    return f"{now.year}-Q{quarter}"


def parse_tasks(content: str) -> list[dict]:
    """Parse TASKS.md content into structured task list."""
    tasks = []
    current_section = None
    current_task = None
    
    for line in content.split('\n'):
        # Detect section headers
        if line.startswith('## '):
            section_match = re.match(r'## ([🔴🟡🟢📅✅]) (.+)', line)
            if section_match:
                current_section = section_match.group(2).strip()
            continue
        
        # Detect task line
        task_match = re.match(r'^- \[([ x])\] \*\*(.+?)\*\*(.*)$', line)
        if task_match:
            if current_task:
                tasks.append(current_task)
            
            done = task_match.group(1) == 'x'
            title = task_match.group(2).strip()
            rest = task_match.group(3).strip()
            description = rest.lstrip('—').lstrip('-').strip() if rest else ''
            
            current_task = {
                'title': title,
                'description': description,
                'done': done,
                'section': current_section,
                'owner': 'martin',
                'due': None,
                'status': 'Done' if done else 'Todo',
                'completed': None,
                'blocks': None,
                'url': None,
                'raw_lines': [line],
            }
            continue
        
        # Detect task metadata
        if current_task and line.strip().startswith('-'):
            meta_line = line.strip()[1:].strip()
            current_task['raw_lines'].append(line)
            
            if meta_line.lower().startswith('owner:'):
                current_task['owner'] = meta_line.split(':', 1)[1].strip().lower()
            elif meta_line.lower().startswith('due:'):
                current_task['due'] = meta_line.split(':', 1)[1].strip()
            elif meta_line.lower().startswith('status:'):
                current_task['status'] = meta_line.split(':', 1)[1].strip()
            elif meta_line.lower().startswith('completed:'):
                current_task['completed'] = meta_line.split(':', 1)[1].strip()
            elif meta_line.lower().startswith('blocks:'):
                current_task['blocks'] = meta_line.split(':', 1)[1].strip()
            elif meta_line.lower().startswith('location:'):
                current_task['url'] = meta_line.split(':', 1)[1].strip()
    
    if current_task:
        tasks.append(current_task)
    
    return tasks


def load_tasks() -> tuple[str, list[dict]]:
    """Load and parse tasks from file."""
    if not TASKS_FILE.exists():
        print(f"\n❌ Tasks file not found: {TASKS_FILE}\n", file=sys.stderr)
        print("To create a new tasks file, run:")
        print(f"  cp {Path(__file__).parent.parent / 'assets' / 'templates' / 'TASKS.md'} {TASKS_FILE}")
        print(f"\nOr create from template:")
        print(f"  python3 scripts/init.py\n")
        sys.exit(1)
    
    content = TASKS_FILE.read_text()
    tasks = parse_tasks(content)
    return content, tasks


def list_tasks(args):
    """List tasks with optional filters."""
    _, tasks = load_tasks()
    
    # Apply filters
    filtered = tasks
    
    if args.priority:
        priority_map = {
            'high': 'High Priority',
            'medium': 'Medium Priority',
            'low': 'Delegated',
        }
        target_section = priority_map.get(args.priority.lower(), args.priority)
        filtered = [t for t in filtered if target_section.lower() in (t.get('section') or '').lower()]
    
    if args.status:
        # Normalize status: replace hyphens with spaces for comparison
        # Handle both "in-progress" (CLI) and "In Progress" (stored)
        normalized_status = args.status.lower().replace('-', ' ')
        filtered = [t for t in filtered if t.get('status', '').lower().replace('-', ' ') == normalized_status]
    
    if args.due:
        today = datetime.now().date()
        week_end = today + timedelta(days=(6 - today.weekday()))
        
        def check_due(task):
            due = task.get('due')
            if not due or due.lower() in ['asap', 'immediately']:
                return args.due == 'today'  # ASAP counts as today
            
            # Strip "Before" prefix if present
            date_str = due
            if due.lower().startswith('before '):
                date_str = due[7:].strip()  # Remove "Before " prefix
            
            # Try to parse date with various formats
            for fmt in ['%Y-%m-%d', '%B %d', '%b %d']:
                try:
                    due_date = datetime.strptime(date_str, fmt).date()
                    if due_date.year == 1900:
                        due_date = due_date.replace(year=today.year)
                    
                    if args.due == 'today':
                        return due_date <= today
                    elif args.due == 'this-week':
                        return due_date <= week_end
                    elif args.due == 'overdue':
                        return due_date < today
                except ValueError:
                    continue
            
            # If we get here, it's a non-date like "Before IMCAS" - treat as future
            return False
        
        filtered = [t for t in filtered if check_due(t)]
    
    if args.completed_since:
        # Filter by completion date
        cutoff_date = None
        now = datetime.now().date()
        
        if args.completed_since == '24h':
            cutoff_date = now - timedelta(days=1)
        elif args.completed_since == '7d':
            cutoff_date = now - timedelta(days=7)
        elif args.completed_since == '30d':
            cutoff_date = now - timedelta(days=30)
        else:
            try:
                cutoff_date = datetime.strptime(args.completed_since, '%Y-%m-%d').date()
            except ValueError:
                print(f"❌ Invalid date format: {args.completed_since}", file=sys.stderr)
                sys.exit(1)
        
        def has_recent_completion(task):
            completed = task.get('completed')
            if not completed:
                return False
            try:
                completed_date = datetime.strptime(completed, '%Y-%m-%d').date()
                return completed_date >= cutoff_date
            except ValueError:
                return False
        
        filtered = [t for t in filtered if has_recent_completion(t)]
    
    if args.owner:
        filtered = [t for t in filtered if t.get('owner') == args.owner.lower()]
    
    # Output
    if not filtered:
        print("No tasks found matching criteria.")
        return
    
    print(f"\n📋 Tasks ({len(filtered)} items)\n")
    
    current_section = None
    for task in filtered:
        if task.get('section') != current_section:
            current_section = task.get('section')
            print(f"\n### {current_section or 'Uncategorized'}\n")
        
        checkbox = '✅' if task['done'] else '⬜'
        due_str = f" (due: {task['due']})" if task.get('due') else ''
        blocks_str = f" [blocks: {task['blocks']}]" if task.get('blocks') else ''
        
        print(f"{checkbox} **{task['title']}**{due_str}{blocks_str}")
        if task.get('description'):
            print(f"   {task['description']}")


def add_task(args):
    """Add a new task."""
    content, _ = load_tasks()
    
    # Build task entry
    priority_section = {
        'high': '🔴 High Priority',
        'medium': '🟡 Medium Priority',
        'low': '🟢 Delegated / Waiting',
    }.get(args.priority, '🟡 Medium Priority')
    
    task_lines = [f'- [ ] **{args.title}**']
    if args.owner:
        task_lines.append(f'  - Owner: {args.owner}')
    if args.due:
        task_lines.append(f'  - Due: {args.due}')
    task_lines.append(f'  - Status: Todo')
    if args.blocks:
        task_lines.append(f'  - Blocks: {args.blocks}')
    
    task_entry = '\n'.join(task_lines)
    
    # Find section and insert
    section_pattern = rf'(## {re.escape(priority_section)}.*?\n)(.*?)(\n## |\n---|\Z)'
    match = re.search(section_pattern, content, re.DOTALL)
    
    if match:
        section_start = match.start(2)
        section_content = match.group(2)
        
        # Find insertion point (after existing tasks in section)
        lines = section_content.rstrip().split('\n')
        insert_content = section_content.rstrip() + '\n\n' + task_entry + '\n'
        
        new_content = content[:match.start(2)] + insert_content + content[match.end(2):]
        TASKS_FILE.write_text(new_content)
        print(f"✅ Added task: {args.title}")
    else:
        print(f"⚠️ Section '{priority_section}' not found. Add manually.", file=sys.stderr)
        print(f"\nTask entry:\n{task_entry}")


def done_task(args):
    """Mark a task as done using fuzzy matching."""
    content, tasks = load_tasks()
    
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
    
    # Update in content
    old_line = task['raw_lines'][0]
    new_line = old_line.replace('- [ ]', '- [x]')
    
    new_content = content.replace(old_line, new_line)
    
    # Add completion date if not already present
    completion_date = datetime.now().strftime('%Y-%m-%d')
    has_completed = any('completed:' in line.lower() for line in task['raw_lines'])
    
    if not has_completed:
        # Find the task in new_content and add completion date after the first metadata line
        lines = new_content.split('\n')
        task_index = next(i for i, line in enumerate(lines) if new_line in line)
        
        # Insert completion date after task title
        indent = '  '
        completion_line = f'{indent}- Completed: {completion_date}'
        lines.insert(task_index + 1, completion_line)
        new_content = '\n'.join(lines)
    
    # Also update status if present
    for line in task['raw_lines']:
        if 'Status:' in line:
            new_content = new_content.replace(line, line.replace(task['status'], 'Done'))
    
    TASKS_FILE.write_text(new_content)
    print(f"✅ Completed: {task['title']}")


def show_blockers(args):
    """Show tasks that are blocking others."""
    _, tasks = load_tasks()
    
    blockers = [t for t in tasks if t.get('blocks') and not t['done']]
    
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
    """Archive completed tasks to quarterly file."""
    content, tasks = load_tasks()
    
    done_tasks = [t for t in tasks if t['done']]
    
    if not done_tasks:
        print("No completed tasks to archive.")
        return
    
    # Create archive entry
    quarter = get_current_quarter()
    archive_file = ARCHIVE_DIR / f"ARCHIVE-{quarter}.md"
    
    archive_entry = f"\n## Archived {datetime.now().strftime('%Y-%m-%d')}\n\n"
    for task in done_tasks:
        archive_entry += f"- ✅ **{task['title']}**\n"
    
    # Append to archive
    if archive_file.exists():
        archive_content = archive_file.read_text()
    else:
        archive_content = f"# Task Archive - {quarter}\n"
    
    archive_content += archive_entry
    archive_file.write_text(archive_content)
    
    # Remove from done section in TASKS.md
    # Find and clear the Done section content
    done_section_pattern = r'(## ✅ Done.*?\n\n).*?(\n## |\n---|\Z)'
    new_content = re.sub(
        done_section_pattern,
        r'\1_Move completed items here during daily standup_\n\n\2',
        content,
        flags=re.DOTALL
    )
    
    TASKS_FILE.write_text(new_content)
    print(f"✅ Archived {len(done_tasks)} tasks to {archive_file.name}")


def main():
    parser = argparse.ArgumentParser(description='Task Tracker CLI')
    subparsers = parser.add_subparsers(dest='command', required=True)
    
    # List command
    list_parser = subparsers.add_parser('list', help='List tasks')
    list_parser.add_argument('--priority', choices=['high', 'medium', 'low'])
    list_parser.add_argument('--status', choices=['todo', 'in-progress', 'blocked', 'waiting', 'done'])
    list_parser.add_argument('--due', choices=['today', 'this-week', 'overdue'])
    list_parser.add_argument('--completed-since', help='Filter done tasks by completion date (24h, 7d, 30d, or YYYY-MM-DD)')
    list_parser.add_argument('--owner', help='Filter by task owner')
    list_parser.set_defaults(func=list_tasks)
    
    # Add command
    add_parser = subparsers.add_parser('add', help='Add a task')
    add_parser.add_argument('title', help='Task title')
    add_parser.add_argument('--priority', default='medium', choices=['high', 'medium', 'low'])
    add_parser.add_argument('--due', help='Due date (YYYY-MM-DD or description)')
    add_parser.add_argument('--owner', default='martin')
    add_parser.add_argument('--blocks', help='Who/what this blocks')
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
