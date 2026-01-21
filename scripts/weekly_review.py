#!/usr/bin/env python3
"""
Weekly Review Generator - Summarizes last week and plans this week.
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


def parse_tasks(content: str) -> dict:
    """Parse TASKS.md into categorized task lists."""
    result = {
        'high_priority': [],
        'medium_priority': [],
        'delegated': [],
        'done': [],
        'upcoming': [],
        'all': [],
    }
    
    current_section = None
    current_task = None
    
    for line in content.split('\n'):
        # Detect section headers
        if line.startswith('## '):
            section_match = re.match(r'## ([🔴🟡🟢📅✅]) (.+)', line)
            if section_match:
                emoji = section_match.group(1)
                name = section_match.group(2).strip()
                current_section = (emoji, name)
            continue
        
        # Detect task line
        task_match = re.match(r'^- \[([ x])\] \*\*(.+?)\*\*(.*)$', line)
        if task_match:
            done = task_match.group(1) == 'x'
            title = task_match.group(2).strip()
            rest = task_match.group(3).strip()
            description = rest.lstrip('—').lstrip('-').strip() if rest else ''
            
            current_task = {
                'title': title,
                'description': description,
                'done': done,
                'section': current_section,
                'due': None,
                'blocks': None,
                'owner': 'martin',
            }
            
            result['all'].append(current_task)
            
            # Categorize
            if done:
                result['done'].append(current_task)
            elif current_section and current_section[0] == '🔴':
                result['high_priority'].append(current_task)
            elif current_section and current_section[0] == '🟡':
                result['medium_priority'].append(current_task)
            elif current_section and current_section[0] == '🟢':
                result['delegated'].append(current_task)
            elif current_section and current_section[0] == '📅':
                result['upcoming'].append(current_task)
            
            continue
        
        # Detect task metadata
        if current_task and line.strip().startswith('-'):
            meta_line = line.strip()[1:].strip()
            
            if meta_line.lower().startswith('due:'):
                current_task['due'] = meta_line.split(':', 1)[1].strip()
            elif meta_line.lower().startswith('blocks:'):
                current_task['blocks'] = meta_line.split(':', 1)[1].strip()
            elif meta_line.lower().startswith('owner:'):
                current_task['owner'] = meta_line.split(':', 1)[1].strip().lower()
    
    return result


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
    if not TASKS_FILE.exists():
        return (f"❌ Tasks file not found: {TASKS_FILE}\n\n"
                f"To create a new tasks file, run:\n"
                f"  python3 {Path(__file__).parent / 'init.py'}\n")
    
    content = TASKS_FILE.read_text()
    tasks = parse_tasks(content)
    
    today = datetime.now()
    week_start = today - timedelta(days=today.weekday())
    week_end = week_start + timedelta(days=6)
    
    lines = [f"📊 **Weekly Review — Week of {week_start.strftime('%B %d')}**\n"]
    
    # Completed last week
    done_count = len(tasks['done'])
    lines.append(f"✅ **Completed:** {done_count} items")
    if tasks['done']:
        for t in tasks['done'][:5]:
            lines.append(f"  • {t['title']}")
        if done_count > 5:
            lines.append(f"  • ... and {done_count - 5} more")
    lines.append("")
    
    # What got pushed (high priority items still open)
    open_high = [t for t in tasks['high_priority'] if not t['done']]
    if open_high:
        lines.append(f"⏳ **Still Open (High Priority):** {len(open_high)} items")
        for t in open_high[:5]:
            due_str = f" (due: {t['due']})" if t.get('due') else ""
            lines.append(f"  • {t['title']}{due_str}")
        lines.append("")
    
    # Blockers
    blockers = [t for t in tasks['all'] if t.get('blocks') and not t['done']]
    if blockers:
        lines.append(f"🚧 **Blocking Others:** {len(blockers)} items")
        for t in blockers[:3]:
            lines.append(f"  • {t['title']} → {t['blocks']}")
        lines.append("")
    
    # This week's priorities
    lines.append("🎯 **This Week's Priorities:**")
    priorities = tasks['high_priority'][:5]
    for i, t in enumerate(priorities, 1):
        due_str = f" (due: {t['due']})" if t.get('due') else ""
        lines.append(f"  {i}. {t['title']}{due_str}")
    lines.append("")
    
    # Upcoming deadlines
    if tasks['upcoming']:
        lines.append("📅 **Upcoming Deadlines:**")
        for t in tasks['upcoming'][:5]:
            due_str = f" — {t['due']}" if t.get('due') else ""
            lines.append(f"  • {t['title']}{due_str}")
    
    # Archive if requested
    if archive and tasks['done']:
        new_content = archive_done_tasks(content, tasks['done'])
        TASKS_FILE.write_text(new_content)
        lines.append(f"\n📦 Archived {done_count} completed tasks.")
    
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description='Generate weekly review summary')
    parser.add_argument('--week', help='Week to review (YYYY-WNN)')
    parser.add_argument('--archive', action='store_true', help='Archive completed tasks')
    parser.add_argument('--json', action='store_true', help='Output as JSON')
    
    args = parser.parse_args()
    
    output = generate_weekly_review(archive=args.archive)
    print(output)


if __name__ == '__main__':
    main()
