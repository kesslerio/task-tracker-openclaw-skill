#!/usr/bin/env python3
"""
Task Tracker CLI - Supports both Work and Personal tasks.

Usage:
    tasks.py list [--priority high|medium|low] [--due today|this-week|overdue|due-or-overdue]
    tasks.py --personal list
    tasks.py add "Task title" [--priority high|medium|low] [--due YYYY-MM-DD]
    tasks.py done "task query"
    tasks.py blockers [--person NAME]
    tasks.py archive
"""

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from utils import (
    get_tasks_file,
    get_section_display_name,
    parse_tasks,
    load_tasks,
    check_due_date,
    get_current_quarter,
    ARCHIVE_DIR,
)


def list_tasks(args):
    """List tasks with optional filters."""
    _, tasks_data = load_tasks(args.personal)
    tasks = tasks_data['all']
    
    # Apply filters
    filtered = tasks
    
    if args.priority:
        priority_map = {
            'high': 'q1',
            'medium': 'q2',
            'low': 'backlog',
        }
        target_key = priority_map.get(args.priority.lower())
        if target_key:
            filtered = tasks_data.get(target_key, [])
    
    if args.due:
        filtered = [t for t in filtered if check_due_date(t.get('due', ''), args.due)]
    
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
    if args.owner and args.owner not in ('me', 'martin'):
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


def done_task(args):
    """Mark a task as done using fuzzy matching."""
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
    
    # Update in content - use raw_line (single line)
    old_line = task.get('raw_line', '')
    if not old_line:
        print("⚠️ Could not find task line to update.")
        return
    
    new_line = old_line.replace('- [ ]', '- [x]')
    
    new_content = content.replace(old_line, new_line)
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
    """Archive completed tasks to quarterly file."""
    tasks_file, format = get_tasks_file(args.personal)
    
    if not tasks_file.exists():
        print(f"❌ Tasks file not found: {tasks_file}")
        return
    
    content = tasks_file.read_text()
    tasks_data = parse_tasks(content, args.personal, format)
    done_tasks = tasks_data['done']
    
    if not done_tasks:
        print("No completed tasks to archive.")
        return
    
    # Create archive entry
    quarter = get_current_quarter()
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    archive_file = ARCHIVE_DIR / f"ARCHIVE-{quarter}.md"
    
    task_type = "Personal" if args.personal else "Work"
    archive_entry = f"\n## Archived {datetime.now().strftime('%Y-%m-%d')} ({task_type})\n\n"
    for task in done_tasks:
        archive_entry += f"- ✅ **{task['title']}**\n"
    
    # Append to archive
    if archive_file.exists():
        archive_content = archive_file.read_text()
    else:
        archive_content = f"# Task Archive - {quarter}\n"
    
    archive_content += archive_entry
    archive_file.write_text(archive_content)
    
    # Remove done tasks from main file
    new_content = content
    for task in done_tasks:
        raw_line = task.get('raw_line', '')
        if raw_line:
            new_content = new_content.replace(raw_line + '\n', '')
    
    tasks_file.write_text(new_content)
    print(f"✅ Archived {len(done_tasks)} {task_type} tasks to {archive_file.name}")


def main():
    parser = argparse.ArgumentParser(description='Task Tracker CLI (Work & Personal)')
    parser.add_argument('--personal', action='store_true', help='Use Personal Tasks instead of Work Tasks')
    
    subparsers = parser.add_subparsers(dest='command', required=True)
    
    # List command
    list_parser = subparsers.add_parser('list', help='List tasks')
    list_parser.add_argument('--priority', choices=['high', 'medium', 'low'])
    list_parser.add_argument('--due', choices=['today', 'this-week', 'overdue', 'due-or-overdue'])
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
