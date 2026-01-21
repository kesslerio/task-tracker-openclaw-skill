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


def generate_standup(date_str: str = None, json_output: bool = False) -> str | dict:
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
    
    # Build output
    output = {
        'date': str(standup_date),
        'date_display': date_display,
        'calendar': get_calendar_events(),
        'priority': None,
        'due_today': [],
        'blocking': [],
        'high_priority': [],
        'completed': [],
        'upcoming': [],
    }
    
    # #1 Priority (blocking tasks first, then high priority)
    if tasks_data['blocking']:
        output['priority'] = tasks_data['blocking'][0]
    elif tasks_data['high_priority']:
        output['priority'] = tasks_data['high_priority'][0]
    
    # Due today
    output['due_today'] = tasks_data['due_today']
    
    # Blocking others
    output['blocking'] = tasks_data['blocking']
    
    # Other high priority
    output['high_priority'] = [t for t in tasks_data['high_priority'] 
                               if t not in tasks_data['blocking']]
    
    # Completed
    output['completed'] = tasks_data['done']
    
    # Upcoming
    output['upcoming'] = tasks_data['upcoming']
    
    if json_output:
        return output
    
    # Format as markdown
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
    
    if output['blocking']:
        lines.append("🚧 **Blocking Others:**")
        for t in output['blocking']:
            lines.append(f"  • {t['title']} → {t.get('blocks', '?')}")
        lines.append("")
    
    if output['high_priority']:
        lines.append("🔴 **High Priority:**")
        for t in output['high_priority']:
            lines.append(f"  • {t['title']}")
        lines.append("")
    
    if output['completed']:
        lines.append(f"✅ **Recently Completed:** ({len(output['completed'])} items)")
        for t in output['completed']:
            lines.append(f"  • {t['title']}")
        lines.append("")
    
    if output['upcoming']:
        lines.append("📅 **Upcoming:**")
        for t in output['upcoming']:
            due_str = f" ({t['due']})" if t.get('due') else ""
            lines.append(f"  • {t['title']}{due_str}")
    
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description='Generate daily standup summary')
    parser.add_argument('--date', help='Date for standup (YYYY-MM-DD)')
    parser.add_argument('--json', action='store_true', help='Output as JSON')
    
    args = parser.parse_args()
    
    result = generate_standup(date_str=args.date, json_output=args.json)
    
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(result)


if __name__ == '__main__':
    main()
