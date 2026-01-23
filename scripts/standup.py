#!/usr/bin/env python3
"""
Daily Standup Generator - Creates a concise summary of today's priorities.
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from utils import load_tasks, check_due_date


def get_calendar_events() -> dict:
    """Fetch today's calendar events via gog CLI.
    
    Returns:
        dict with calendar_id keys, each containing list of events
        
    Configuration (optional):
    Set STANDUP_CALENDARS environment variable with JSON:
    {
        "work": {"cmd": "gog-work", "calendar_id": "user@work.com", "account": "user@work.com", "label": "Work"},
        "personal": {"cmd": "gog", "calendar_id": "user@personal.com", "account": "user@personal.com", "label": null},
        "family": {"cmd": "gog", "calendar_id": "family_calendar_id", "account": "user@personal.com", "label": "Family"}
    }
    
    If not set, returns empty dict (no calendar integration).
    """
    config_str = os.getenv('STANDUP_CALENDARS')
    if not config_str:
        return {}
    
    try:
        calendars_config = json.loads(config_str)
    except json.JSONDecodeError:
        return {}
    
    events = {}
    
    for key, config in calendars_config.items():
        events[key] = []
        cmd = config.get('cmd', 'gog')
        calendar_id = config.get('calendar_id')
        account = config.get('account')
        label = config.get('label')
        
        if not calendar_id or not account:
            continue
        
        try:
            result = subprocess.run(
                [cmd, 'calendar', 'list', calendar_id,
                 '--account', account, '--today', '--json'],
                capture_output=True,
                text=True,
                timeout=10
            )
            if result.returncode == 0:
                data = json.loads(result.stdout)
                for event in data.get('events', []):
                    # Skip birthdays and all-day events without times
                    if event.get('eventType') == 'birthday':
                        continue
                    if 'dateTime' not in event.get('start', {}):
                        continue
                    
                    summary = event.get('summary', 'Untitled')
                    if label:
                        summary = f"{summary} ({label})"
                    
                    events[key].append({
                        'summary': summary,
                        'start': event['start'].get('dateTime'),
                        'end': event['end'].get('dateTime'),
                    })
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError, json.JSONDecodeError, FileNotFoundError):
            pass
    
    return events


def format_time(iso_time: str) -> str:
    """Format ISO datetime to human-readable time (e.g., '2:30 PM')."""
    try:
        dt = datetime.fromisoformat(iso_time.replace('Z', '+00:00'))
        return dt.strftime('%I:%M %p').lstrip('0')
    except:
        return iso_time


def group_by_area(tasks):
    """Group tasks by area."""
    areas = {}
    for t in tasks:
        area = t.get('area', 'Uncategorized')
        if area not in areas:
            areas[area] = []
        areas[area].append(t)
    return areas


def format_split_standup(output: dict, date_display: str) -> list:
    """Format standup as 3 separate messages.
    
    Returns list of 3 strings:
    1. Completed items (by category)
    2. Calendar events
    3. Active todos (by priority + category)
    """
    messages = []
    
    # Message 1: Completed items
    msg1_lines = [f"✅ **Completed — {date_display}**\n"]
    if output['completed']:
        by_area = group_by_area(output['completed'])
        for cat in sorted(by_area.keys()):
            msg1_lines.append(f"**{cat}:**")
            for t in by_area[cat]:
                msg1_lines.append(f"  • {t['title']}")
            msg1_lines.append("")
    else:
        msg1_lines.append("_No completed items_")
    messages.append('\n'.join(msg1_lines).strip())
    
    # Message 2: Calendar events
    msg2_lines = [f"📅 **Calendar — {date_display}**\n"]
    cal = output['calendar']
    if cal:
        all_events = []
        for key in sorted(cal.keys()):
            all_events.extend(cal[key])
        
        if all_events:
            for event in all_events:
                time_str = format_time(event['start'])
                msg2_lines.append(f"• {time_str} — {event['summary']}")
        else:
            msg2_lines.append("_No calendar events today_")
    else:
        msg2_lines.append("_No calendar events today_")
    messages.append('\n'.join(msg2_lines).strip())
    
    # Message 3: Active todos
    msg3_lines = [f"📋 **Todos — {date_display}**\n"]
    
    # #1 Priority
    if output['priority']:
        priority = output['priority']
        msg3_lines.append(f"🎯 **#1 Priority:** {priority['title']}")
        if priority.get('blocks'):
            msg3_lines.append(f"   ↳ Blocking: {priority['blocks']}")
        msg3_lines.append("")
    
    # Due today
    if output['due_today']:
        msg3_lines.append("⏰ **Due Today:**")
        for t in output['due_today']:
            msg3_lines.append(f"  • {t['title']}")
        msg3_lines.append("")
    
    # Q1 - Urgent & Important
    if output.get('q1'):
        msg3_lines.append("🔴 **Urgent & Important (Q1):**")
        by_area = group_by_area(output['q1'])
        for area in sorted(by_area.keys()):
            msg3_lines.append(f"  **{area}:**")
            for t in by_area[area]:
                msg3_lines.append(f"    • {t['title']}")
        msg3_lines.append("")
    
    # Q2 - Important, Not Urgent
    if output.get('q2'):
        msg3_lines.append("🟡 **Important, Not Urgent (Q2):**")
        by_area = group_by_area(output['q2'])
        for area in sorted(by_area.keys()):
            msg3_lines.append(f"  **{area}:**")
            for t in by_area[area]:
                due_str = f" (🗓️{t['due']})" if t.get('due') else ""
                msg3_lines.append(f"    • {t['title']}{due_str}")
        msg3_lines.append("")
    
    # Q3 - Waiting/Blocked
    if output.get('q3'):
        msg3_lines.append("🟠 **Waiting/Blocked (Q3):**")
        for t in output['q3']:
            blocks_str = f" → {t['blocks']}" if t.get('blocks') else ""
            msg3_lines.append(f"  • {t['title']}{blocks_str}")
        msg3_lines.append("")
    
    # Team tasks
    if output.get('team'):
        msg3_lines.append("👥 **Team Tasks:**")
        for t in output['team']:
            owner_str = f" ({t['owner']})" if t.get('owner') else ""
            msg3_lines.append(f"  • {t['title']}{owner_str}")
    
    messages.append('\n'.join(msg3_lines).strip())
    
    return messages


def generate_standup(date_str: str = None, json_output: bool = False, split_output: bool = False) -> str | dict | list:
    """Generate daily standup summary.
    
    Args:
        date_str: Optional date string (YYYY-MM-DD) for standup
        json_output: If True, return dict instead of markdown
    
    Returns:
        String summary (default) or dict if json_output=True
    """
    _, tasks_data = load_tasks()
    
    today = datetime.now()
    if date_str:
        try:
            standup_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        except ValueError:
            standup_date = today.date()
    else:
        standup_date = today.date()
    
    date_display = standup_date.strftime("%A, %B %d")
    
    # Build output using new task structure (q1, q2, q3, team, backlog)
    output = {
        'date': str(standup_date),
        'date_display': date_display,
        'calendar': get_calendar_events(),
        'priority': None,
        'due_today': [],
        'q1': [],  # Urgent & Important
        'q2': [],  # Important, Not Urgent
        'q3': [],  # Waiting/Blocked
        'team': [],  # Team tasks to monitor
        'completed': [],
    }
    
    # #1 Priority (Q1 first, then Q2)
    if tasks_data.get('q1'):
        output['priority'] = tasks_data['q1'][0]
    elif tasks_data.get('q2'):
        output['priority'] = tasks_data['q2'][0]
    
    # Due today
    output['due_today'] = tasks_data.get('due_today', [])
    
    # Q1 - Urgent & Important
    output['q1'] = tasks_data.get('q1', [])
    
    # Q2 - Important, Not Urgent
    output['q2'] = tasks_data.get('q2', [])
    
    # Q3 - Waiting/Blocked
    output['q3'] = tasks_data.get('q3', [])
    
    # Team tasks
    output['team'] = tasks_data.get('team', [])
    
    # Completed
    output['completed'] = tasks_data.get('done', [])
    
    if json_output:
        return output
    
    if split_output:
        return format_split_standup(output, date_display)
    
    # Format as markdown (single message)
    lines = [f"📋 **Daily Standup — {date_display}**\n"]
    
    # Calendar events
    cal = output['calendar']
    if cal:
        all_events = []
        for key in sorted(cal.keys()):
            all_events.extend(cal[key])
        
        if all_events:
            lines.append("📅 **Today's Calendar:**")
            for event in all_events:
                time_str = format_time(event['start'])
                lines.append(f"  • {time_str} — {event['summary']}")
            lines.append("")
    
    if output['priority']:
        priority = output['priority']
        lines.append(f"🎯 **#1 Priority:** {priority['title']}")
        if priority.get('blocks'):
            lines.append(f"   ↳ Blocking: {priority['blocks']}")
        lines.append("")
    
    if output['due_today']:
        lines.append("⏰ **Due Today:**")
        for t in output['due_today']:
            lines.append(f"  • {t['title']}")
        lines.append("")
    
    # Q1 - Urgent & Important
    if output['q1']:
        lines.append("🔴 **Urgent & Important (Q1):**")
        by_area = group_by_area(output['q1'])
        for cat in sorted(by_area.keys()):
            lines.append(f"  **{cat}:**")
            for t in by_area[cat]:
                lines.append(f"    • {t['title']}")
        lines.append("")
    
    # Q2 - Important, Not Urgent
    if output['q2']:
        lines.append("🟡 **Important, Not Urgent (Q2):**")
        by_area = group_by_area(output['q2'])
        for cat in sorted(by_area.keys()):
            lines.append(f"  **{cat}:**")
            for t in by_area[cat]:
                due_str = f" (🗓️{t['due']})" if t.get('due') else ""
                lines.append(f"    • {t['title']}{due_str}")
        lines.append("")
    
    # Q3 - Waiting/Blocked
    if output['q3']:
        lines.append("🟠 **Waiting/Blocked (Q3):**")
        for t in output['q3']:
            blocks_str = f" → {t['blocks']}" if t.get('blocks') else ""
            lines.append(f"  • {t['title']}{blocks_str}")
        lines.append("")
    
    # Team tasks
    if output['team']:
        lines.append("👥 **Team Tasks:**")
        for t in output['team']:
            owner_str = f" ({t['owner']})" if t.get('owner') else ""
            lines.append(f"  • {t['title']}{owner_str}")
        lines.append("")
    
    if output['completed']:
        lines.append(f"✅ **Recently Completed:** ({len(output['completed'])} items)")
        for t in output['completed'][:5]:  # Limit to 5
            lines.append(f"  • {t['title']}")
        if len(output['completed']) > 5:
            lines.append(f"  • ... and {len(output['completed']) - 5} more")
    
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description='Generate daily standup summary')
    parser.add_argument('--date', help='Date for standup (YYYY-MM-DD)')
    parser.add_argument('--json', action='store_true', help='Output as JSON')
    parser.add_argument('--split', action='store_true', help='Split into 3 messages (completed/calendar/todos)')
    
    args = parser.parse_args()
    
    result = generate_standup(date_str=args.date, json_output=args.json, split_output=args.split)
    
    if args.json:
        print(json.dumps(result, indent=2))
    elif args.split:
        # Print 3 messages separated by double newlines
        for i, msg in enumerate(result, 1):
            print(msg)
            if i < len(result):
                print("\n---\n")
    else:
        print(result)


if __name__ == '__main__':
    main()
