#!/usr/bin/env python3
"""
Personal Daily Standup Generator - Creates a concise summary of personal priorities.
"""

import argparse
import json
import sys
from pathlib import Path

# Add parent directory to path for utils import
sys.path.insert(0, str(Path(__file__).parent))
from standup_common import (
    flatten_calendar_events,
    format_missed_tasks_block,
    format_time,
    get_calendar_events,
    resolve_standup_date,
)
from utils import (
    load_tasks,
    get_missed_tasks_bucketed,
    regroup_by_effective_priority,
    escalation_suffix,
    recurrence_suffix,
)


def format_personal_standup(output: dict, date_display: str) -> str:
    """Format personal standup as markdown."""
    lines = [f"🏠 **Personal Daily Standup — {date_display}**\n"]
    
    # Calendar events
    all_events = flatten_calendar_events(output['calendar'])
    if all_events:
        lines.append("📅 **Today's Calendar:**")
        for event in all_events:
            time_str = format_time(event['start'])
            lines.append(f"  • {time_str} — {event['summary']}")
        lines.append("")
    
    # #1 Priority
    if output['priority']:
        rec = recurrence_suffix(output['priority'])
        lines.append(f"🎯 **#1 Priority:** {output['priority']['title']}{rec}")
        lines.append("")
    
    # Due Today
    if output['due_today']:
        lines.append("⏰ **Due Today:**")
        for t in output['due_today']:
            rec = recurrence_suffix(t)
            lines.append(f"  • {t['title']}{rec}")
        lines.append("")
    
    # Q1 Must Do
    if output['q1']:
        lines.append("🔴 **Must Do Today:**")
        for t in output['q1']:
            esc = escalation_suffix(t)
            rec = recurrence_suffix(t)
            lines.append(f"  • {t['title']}{esc}{rec}")
        lines.append("")
    
    # Q2 Should Do
    if output['q2']:
        lines.append("🟡 **Should Do This Week:**")
        for t in output['q2']:
            due_str = f" (🗓️{t['due']})" if t.get('due') else ""
            rec = recurrence_suffix(t)
            lines.append(f"  • {t['title']}{due_str}{rec}")
        lines.append("")
    
    # Q3 Waiting On
    if output['q3']:
        lines.append("🟠 **Waiting On:**")
        for t in output['q3']:
            esc = escalation_suffix(t)
            rec = recurrence_suffix(t)
            lines.append(f"  • {t['title']}{esc}{rec}")
        lines.append("")
    
    # Completed
    if output['completed']:
        lines.append(f"✅ **Completed:** ({len(output['completed'])} items)")
        for t in output['completed'][:5]:  # Limit to 5
            rec = recurrence_suffix(t)
            lines.append(f"  • {t['title']}{rec}")
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
    
    standup_date = resolve_standup_date(date_str)
    date_display = standup_date.strftime("%A, %B %d")
    
    # Apply display-only priority escalation
    regrouped = regroup_by_effective_priority(tasks_data, reference_date=standup_date)

    # Build output using new task structure
    output = {
        'date': str(standup_date),
        'date_display': date_display,
        'calendar': get_calendar_events(),
        'priority': None,
        'due_today': tasks_data['due_today'],
        'q1': regrouped['q1'],
        'q2': regrouped['q2'],
        'q3': regrouped['q3'],
        'completed': tasks_data['done'],
    }
    
    # #1 Priority: first Q1 item, or first Q2 if no Q1
    if regrouped['q1']:
        output['priority'] = regrouped['q1'][0]
    elif regrouped['q2']:
        output['priority'] = regrouped['q2'][0]
    
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
    missed_buckets = None
    if not args.skip_missed:
        missed_buckets = get_missed_tasks_bucketed(tasks_data, reference_date=args.date)

    result = generate_personal_standup(
        date_str=args.date,
        json_output=args.json,
        tasks_data=tasks_data,
    )

    missed_block = ""
    if not args.json:
        missed_block = format_missed_tasks_block(missed_buckets)
        if missed_block:
            result = f"{missed_block}{result}"
    
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(result)


if __name__ == '__main__':
    main()
