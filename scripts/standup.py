#!/usr/bin/env python3
"""
Daily Standup Generator - Creates a concise summary of today's priorities.
"""

import argparse
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

TASKS_FILE = Path.home() / "clawd" / "memory" / "work" / "TASKS.md"


def parse_tasks(content: str) -> dict:
    """Parse TASKS.md into categorized task lists."""
    result = {
        'high_priority': [],
        'blocking': [],
        'due_today': [],
        'done': [],
        'upcoming': [],
    }
    
    current_section = None
    current_task = None
    today = datetime.now().date()
    
    for line in content.split('\n'):
        # Detect section headers
        if line.startswith('## '):
            section_match = re.match(r'## ([🔴🟡🟢📅✅]) (.+)', line)
            if section_match:
                emoji = section_match.group(1)
                current_section = emoji
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
            }
            
            # Categorize immediately based on section
            if done:
                result['done'].append(current_task)
            elif current_section == '🔴':
                result['high_priority'].append(current_task)
            elif current_section == '📅':
                result['upcoming'].append(current_task)
            
            continue
        
        # Detect task metadata
        if current_task and line.strip().startswith('-'):
            meta_line = line.strip()[1:].strip()
            
            if meta_line.lower().startswith('due:'):
                due_str = meta_line.split(':', 1)[1].strip()
                current_task['due'] = due_str
                
                # Check if due today or ASAP
                if due_str.lower() in ['asap', 'immediately', 'today']:
                    if current_task not in result['due_today']:
                        result['due_today'].append(current_task)
                else:
                    # Try to parse date - strip "Before" prefix first
                    date_str = due_str
                    if due_str.lower().startswith('before '):
                        date_str = due_str[7:].strip()  # Remove "Before " prefix
                    
                    # Try various formats (full and abbreviated month names)
                    for fmt in ['%Y-%m-%d', '%B %d', '%b %d']:
                        try:
                            due_date = datetime.strptime(date_str, fmt).date()
                            if due_date.year == 1900:
                                due_date = due_date.replace(year=today.year)
                            
                            if due_date <= today:
                                if current_task not in result['due_today']:
                                    result['due_today'].append(current_task)
                            break
                        except ValueError:
                            continue
            
            elif meta_line.lower().startswith('blocks:'):
                blocks = meta_line.split(':', 1)[1].strip()
                current_task['blocks'] = blocks
                if not current_task['done'] and current_task not in result['blocking']:
                    result['blocking'].append(current_task)
    
    return result


def generate_standup(date_str: str = None) -> str:
    """Generate daily standup summary."""
    if not TASKS_FILE.exists():
        return f"❌ Tasks file not found: {TASKS_FILE}"
    
    content = TASKS_FILE.read_text()
    tasks = parse_tasks(content)
    
    today = datetime.now()
    date_display = today.strftime("%A, %B %d")
    
    lines = [f"📋 **Daily Standup — {date_display}**\n"]
    
    # #1 Priority
    if tasks['blocking']:
        top = tasks['blocking'][0]
        lines.append(f"🎯 **#1 Priority:** {top['title']}")
        if top.get('blocks'):
            lines.append(f"   ↳ Blocking: {top['blocks']}")
        lines.append("")
    elif tasks['high_priority']:
        top = tasks['high_priority'][0]
        lines.append(f"🎯 **#1 Priority:** {top['title']}")
        lines.append("")
    
    # Due Today / ASAP
    if tasks['due_today']:
        lines.append("⏰ **Due Today:**")
        for t in tasks['due_today'][:5]:
            lines.append(f"  • {t['title']}")
        lines.append("")
    
    # Blockers
    if tasks['blocking']:
        lines.append("🚧 **Blocking Others:**")
        for t in tasks['blocking'][:3]:
            lines.append(f"  • {t['title']} → {t.get('blocks', '?')}")
        lines.append("")
    
    # High Priority (not already shown)
    other_high = [t for t in tasks['high_priority'] 
                  if t not in tasks['due_today'] and t not in tasks['blocking']]
    if other_high:
        lines.append("🔴 **High Priority:**")
        for t in other_high[:3]:
            lines.append(f"  • {t['title']}")
        lines.append("")
    
    # Yesterday's completions
    if tasks['done']:
        lines.append(f"✅ **Recently Completed:** {len(tasks['done'])} items")
        for t in tasks['done'][:3]:
            lines.append(f"  • {t['title']}")
        lines.append("")
    
    # Upcoming deadlines
    if tasks['upcoming']:
        lines.append("📅 **Upcoming:**")
        for t in tasks['upcoming'][:3]:
            due_str = f" ({t['due']})" if t.get('due') else ""
            lines.append(f"  • {t['title']}{due_str}")
    
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description='Generate daily standup summary')
    parser.add_argument('--date', help='Date for standup (YYYY-MM-DD)')
    parser.add_argument('--json', action='store_true', help='Output as JSON')
    
    args = parser.parse_args()
    
    output = generate_standup(args.date)
    print(output)


if __name__ == '__main__':
    main()
