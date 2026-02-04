#!/usr/bin/env python3
"""
Personal Daily Standup Generator - Creates a concise summary of personal priorities.
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# Add parent directory to path for utils import
sys.path.insert(0, str(Path(__file__).parent))
from utils import load_tasks, check_due_date, get_section_display_name, get_missed_tasks


def get_calendar_events() -> dict:
    """Fetch today's calendar events via gog CLI."""
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
    """Format ISO datetime to human-readable time."""
    try:
        dt = datetime.fromisoformat(iso_time.replace('Z', '+00:00'))
        return dt.strftime('%I:%M %p').lstrip('0')
    except:
        return iso_time


def format_personal_standup(output: dict, date_display: str) -> str:
    """Format personal standup as markdown."""
    lines = [f"🏠 **Personal Daily Standup — {date_display}**\n"]
    
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
    
    # #1 Priority
    if output['priority']:
        lines.append(f"🎯 **#1 Priority:** {output['priority']['title']}")
        lines.append("")
    
    # Due Today
    if output['due_today']:
        lines.append("⏰ **Due Today:**")
        for t in output['due_today']:
            lines.append(f"  • {t['title']}")
        lines.append("")
    
    # Q1 Must Do
    if output['q1']:
        lines.append("🔴 **Must Do Today:**")
        for t in output['q1']:
            lines.append(f"  • {t['title']}")
        lines.append("")
    
    # Q2 Should Do
    if output['q2']:
        lines.append("🟡 **Should Do This Week:**")
        for t in output['q2']:
            due_str = f" (🗓️{t['due']})" if t.get('due') else ""
            lines.append(f"  • {t['title']}{due_str}")
        lines.append("")
    
    # Q3 Waiting On
    if output['q3']:
        lines.append("🟠 **Waiting On:**")
        for t in output['q3']:
            lines.append(f"  • {t['title']}")
        lines.append("")
    
    # Completed
    if output['completed']:
        lines.append(f"✅ **Completed:** ({len(output['completed'])} items)")
        for t in output['completed'][:5]:  # Limit to 5
            lines.append(f"  • {t['title']}")
        if len(output['completed']) > 5:
            lines.append(f"  • ... and {len(output['completed']) - 5} more")
    
    return '\n'.join(lines)


def generate_personal_standup(
    date_str: str = None,
    json_output: bool = False,
    tasks_data: dict | None = None,
) -> str | dict:
    """Generate personal daily standup summary."""
    if tasks_data is None:
        _, tasks_data = load_tasks(personal=True)
    
    today = datetime.now()
    if date_str:
        try:
            standup_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        except ValueError:
            standup_date = today.date()
    else:
        standup_date = today.date()
    
    date_display = standup_date.strftime("%A, %B %d")
    
    # Build output using new task structure
    output = {
        'date': str(standup_date),
        'date_display': date_display,
        'calendar': get_calendar_events(),
        'priority': None,
        'due_today': tasks_data['due_today'],
        'q1': tasks_data['q1'],
        'q2': tasks_data['q2'],
        'q3': tasks_data['q3'],
        'completed': tasks_data['done'],
    }
    
    # #1 Priority: first Q1 item, or first Q2 if no Q1
    if tasks_data['q1']:
        output['priority'] = tasks_data['q1'][0]
    elif tasks_data['q2']:
        output['priority'] = tasks_data['q2'][0]
    
    if json_output:
        return output
    
    return format_personal_standup(output, date_display)


def main():
    parser = argparse.ArgumentParser(description='Generate personal daily standup')
    parser.add_argument('--date', help='Date for standup (YYYY-MM-DD)')
    parser.add_argument('--json', action='store_true', help='Output as JSON')
    parser.add_argument('--skip-missed', action='store_true', help='Skip missed tasks section')
    
    args = parser.parse_args()
    
    _, tasks_data = load_tasks(personal=True)
    missed_tasks = []
    if not args.skip_missed:
        missed_tasks = get_missed_tasks(tasks_data)

    result = generate_personal_standup(
        date_str=args.date,
        json_output=args.json,
        tasks_data=tasks_data,
    )

    missed_block = ""
    if missed_tasks and not args.json:
        missed_lines = ["🔴 **Missed (due yesterday):**"]
        for task in missed_tasks:
            title = task.get('title', '')
            missed_lines.append(f"  • {title} — say \"done {title}\" to mark complete")
        missed_lines.append("")
        missed_block = "\n".join(missed_lines)
        result = f"{missed_block}{result}"
    
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(result)


if __name__ == '__main__':
    main()
