#!/usr/bin/env python3
"""
Weekly Review Generator - Summarizes last week and plans this week.
"""

import argparse
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

from utils import (
    get_tasks_file,
    ARCHIVE_DIR,
    get_current_quarter,
    parse_tasks,
    load_tasks,
)


def archive_done_tasks(content: str, done_tasks: list) -> str:
    """Archive done tasks and return updated content."""
    if not done_tasks:
        return content
    
    # Create archive entry
    quarter = get_current_quarter()
    archive_file = ARCHIVE_DIR / f"ARCHIVE-{quarter}.md"
    
    archive_entry = f"\n## Week of {datetime.now().strftime('%Y-%m-%d')}\n\n"
    for task in done_tasks:
        archive_entry += f"- ✅ **{task['title']}**\n"
    
    # Append to archive
    if archive_file.exists():
        archive_content = archive_file.read_text()
    else:
        archive_content = f"# Task Archive - {quarter}\n"
    
    archive_content += archive_entry
    archive_file.write_text(archive_content)
    
    # Clear done section in original content
    done_section_pattern = r'(## ✅ Done.*?\n\n).*?(\n## |\n---|\Z)'
    new_content = re.sub(
        done_section_pattern,
        r'\1_Move completed items here during daily standup_\n\n\2',
        content,
        flags=re.DOTALL
    )
    
    return new_content


def generate_weekly_review(archive: bool = False) -> str:
    """Generate weekly review summary."""
    _, tasks_data = load_tasks()
    
    today = datetime.now()
    week_start = today - timedelta(days=today.weekday())
    week_end = week_start + timedelta(days=6)
    
    lines = [f"📊 **Weekly Review — Week of {week_start.strftime('%B %d')}**\n"]
    
    # Completed last week
    done_count = len(tasks_data['done'])
    lines.append(f"✅ **Completed:** {done_count} items")
    if tasks_data['done']:
        for t in tasks_data['done'][:5]:
            lines.append(f"  • {t['title']}")
        if done_count > 5:
            lines.append(f"  • ... and {done_count - 5} more")
    lines.append("")
    
    # What got pushed (Q1 items still open)
    open_q1 = [t for t in tasks_data.get('q1', []) if not t['done']]
    if open_q1:
        lines.append(f"⏳ **Still Open (Urgent):** {len(open_q1)} items")
        for t in open_q1[:5]:
            due_str = f" (due: {t['due']})" if t.get('due') else ""
            lines.append(f"  • {t['title']}{due_str}")
        lines.append("")
    
    # Waiting/Blocked
    waiting = [t for t in tasks_data.get('q3', []) if not t['done']]
    if waiting:
        lines.append(f"🟠 **Waiting/Blocked:** {len(waiting)} items")
        for t in waiting[:3]:
            blocks_str = f" → {t['blocks']}" if t.get('blocks') else ""
            lines.append(f"  • {t['title']}{blocks_str}")
        lines.append("")
    
    # This week's priorities (Q1 tasks)
    lines.append("🎯 **This Week's Priorities:**")
    priorities = tasks_data.get('q1', [])[:5]
    for i, t in enumerate(priorities, 1):
        due_str = f" (due: {t['due']})" if t.get('due') else ""
        lines.append(f"  {i}. {t['title']}{due_str}")
    lines.append("")
    
    # Upcoming deadlines (Q2 tasks with due dates)
    upcoming = [t for t in tasks_data.get('q2', []) if t.get('due')]
    if upcoming:
        lines.append("📅 **Upcoming Deadlines:**")
        for t in upcoming[:5]:
            due_str = f" — {t['due']}" if t.get('due') else ""
            lines.append(f"  • {t['title']}{due_str}")
    
    # Archive if requested
    if archive and tasks_data['done']:
        tasks_file, format = get_tasks_file()
        content = tasks_file.read_text()
        new_content = archive_done_tasks(content, tasks_data['done'])
        tasks_file.write_text(new_content)
        lines.append(f"\n📦 Archived {done_count} completed tasks.")
    
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description='Generate weekly review summary')
    parser.add_argument('--week', help='Week to review (YYYY-WNN)')
    parser.add_argument('--archive', action='store_true', help='Archive completed tasks')
    
    args = parser.parse_args()
    
    print(generate_weekly_review(archive=args.archive))


if __name__ == '__main__':
    main()
