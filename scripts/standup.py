#!/usr/bin/env python3
"""
Daily Standup Generator - Creates a concise summary of today's priorities.
"""

import argparse
import json
import os
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from daily_notes import extract_completed_actions
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
                rec = recurrence_suffix(t)
                msg1_lines.append(f"  • {t['title']}{rec}")
            msg1_lines.append("")
    else:
        msg1_lines.append("_No completed items_")
    messages.append('\n'.join(msg1_lines).strip())
    
    # Message 2: Calendar events
    msg2_lines = [f"📅 **Calendar — {date_display}**\n"]
    all_events = flatten_calendar_events(output['calendar'])
    if all_events:
        for event in all_events:
            time_str = format_time(event['start'])
            msg2_lines.append(f"• {time_str} — {event['summary']}")
    else:
        msg2_lines.append("_No calendar events today_")
    messages.append('\n'.join(msg2_lines).strip())
    
    # Message 3: Active todos
    msg3_lines = [f"📋 **Todos — {date_display}**\n"]
    
    # #1 Priority
    if output['priority']:
        priority = output['priority']
        rec = recurrence_suffix(priority)
        msg3_lines.append(f"🎯 **#1 Priority:** {priority['title']}{rec}")
        if priority.get('blocks'):
            msg3_lines.append(f"   ↳ Blocking: {priority['blocks']}")
        msg3_lines.append("")
    
    # Due today
    if output['due_today']:
        msg3_lines.append("⏰ **Due Today:**")
        for t in output['due_today']:
            rec = recurrence_suffix(t)
            msg3_lines.append(f"  • {t['title']}{rec}")
        msg3_lines.append("")
    
    # Q1 - Urgent & Important
    if output.get('q1'):
        msg3_lines.append("🔴 **Urgent & Important (Q1):**")
        by_area = group_by_area(output['q1'])
        for area in sorted(by_area.keys()):
            msg3_lines.append(f"  **{area}:**")
            for t in by_area[area]:
                esc = escalation_suffix(t)
                rec = recurrence_suffix(t)
                msg3_lines.append(f"    • {t['title']}{esc}{rec}")
        msg3_lines.append("")
    
    # Q2 - Important, Not Urgent
    if output.get('q2'):
        msg3_lines.append("🟡 **Important, Not Urgent (Q2):**")
        by_area = group_by_area(output['q2'])
        for area in sorted(by_area.keys()):
            msg3_lines.append(f"  **{area}:**")
            for t in by_area[area]:
                due_str = f" (🗓️{t['due']})" if t.get('due') else ""
                rec = recurrence_suffix(t)
                msg3_lines.append(f"    • {t['title']}{due_str}{rec}")
        msg3_lines.append("")
    
    # Q3 - Waiting/Blocked
    if output.get('q3'):
        msg3_lines.append("🟠 **Waiting/Blocked (Q3):**")
        for t in output['q3']:
            blocks_str = f" → {t['blocks']}" if t.get('blocks') else ""
            esc = escalation_suffix(t)
            rec = recurrence_suffix(t)
            msg3_lines.append(f"  • {t['title']}{blocks_str}{esc}{rec}")
        msg3_lines.append("")
    
    # Team tasks
    if output.get('team'):
        msg3_lines.append("👥 **Team Tasks:**")
        for t in output['team']:
            owner_str = f" ({t['owner']})" if t.get('owner') else ""
            rec = recurrence_suffix(t)
            msg3_lines.append(f"  • {t['title']}{rec}{owner_str}")
    
    messages.append('\n'.join(msg3_lines).strip())
    
    return messages


def generate_standup(
    date_str: str = None,
    json_output: bool = False,
    split_output: bool = False,
    tasks_data: dict | None = None,
) -> str | dict | list:
    """Generate daily standup summary.
    
    Args:
        date_str: Optional date string (YYYY-MM-DD) for standup
        json_output: If True, return dict instead of markdown
    
    Returns:
        String summary (default) or dict if json_output=True
    """
    if tasks_data is None:
        _, tasks_data = load_tasks()
    
    standup_date = resolve_standup_date(date_str)
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
    
    # Apply display-only priority escalation
    regrouped = regroup_by_effective_priority(tasks_data, reference_date=standup_date)

    # #1 Priority (escalated Q1 first, then Q2)
    if regrouped['q1']:
        output['priority'] = regrouped['q1'][0]
    elif regrouped['q2']:
        output['priority'] = regrouped['q2'][0]
    
    # Due today
    output['due_today'] = tasks_data.get('due_today', [])
    
    # Q1 - Urgent & Important (includes escalated tasks)
    output['q1'] = regrouped['q1']
    
    # Q2 - Important, Not Urgent
    output['q2'] = regrouped['q2']
    
    # Q3 - Waiting/Blocked (includes escalated tasks)
    output['q3'] = regrouped['q3']
    
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
    all_events = flatten_calendar_events(output['calendar'])
    if all_events:
        lines.append("📅 **Today's Calendar:**")
        for event in all_events:
            time_str = format_time(event['start'])
            lines.append(f"  • {time_str} — {event['summary']}")
        lines.append("")
    
    if output['priority']:
        priority = output['priority']
        rec = recurrence_suffix(priority)
        lines.append(f"🎯 **#1 Priority:** {priority['title']}{rec}")
        if priority.get('blocks'):
            lines.append(f"   ↳ Blocking: {priority['blocks']}")
        lines.append("")
    
    if output['due_today']:
        lines.append("⏰ **Due Today:**")
        for t in output['due_today']:
            rec = recurrence_suffix(t)
            lines.append(f"  • {t['title']}{rec}")
        lines.append("")
    
    # Q1 - Urgent & Important
    if output['q1']:
        lines.append("🔴 **Urgent & Important (Q1):**")
        by_area = group_by_area(output['q1'])
        for cat in sorted(by_area.keys()):
            lines.append(f"  **{cat}:**")
            for t in by_area[cat]:
                esc = escalation_suffix(t)
                rec = recurrence_suffix(t)
                lines.append(f"    • {t['title']}{esc}{rec}")
        lines.append("")
    
    # Q2 - Important, Not Urgent
    if output['q2']:
        lines.append("🟡 **Important, Not Urgent (Q2):**")
        by_area = group_by_area(output['q2'])
        for cat in sorted(by_area.keys()):
            lines.append(f"  **{cat}:**")
            for t in by_area[cat]:
                due_str = f" (🗓️{t['due']})" if t.get('due') else ""
                rec = recurrence_suffix(t)
                lines.append(f"    • {t['title']}{due_str}{rec}")
        lines.append("")
    
    # Q3 - Waiting/Blocked
    if output['q3']:
        lines.append("🟠 **Waiting/Blocked (Q3):**")
        for t in output['q3']:
            blocks_str = f" → {t['blocks']}" if t.get('blocks') else ""
            esc = escalation_suffix(t)
            rec = recurrence_suffix(t)
            lines.append(f"  • {t['title']}{blocks_str}{esc}{rec}")
        lines.append("")
    
    # Team tasks
    if output['team']:
        lines.append("👥 **Team Tasks:**")
        for t in output['team']:
            owner_str = f" ({t['owner']})" if t.get('owner') else ""
            rec = recurrence_suffix(t)
            lines.append(f"  • {t['title']}{rec}{owner_str}")
        lines.append("")
    
    if output['completed']:
        lines.append(f"✅ **Recently Completed:** ({len(output['completed'])} items)")
        for t in output['completed'][:5]:  # Limit to 5
            rec = recurrence_suffix(t)
            lines.append(f"  • {t['title']}{rec}")
        if len(output['completed']) > 5:
            lines.append(f"  • ... and {len(output['completed']) - 5} more")
    
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description='Generate daily standup summary')
    parser.add_argument('--date', help='Date for standup (YYYY-MM-DD)')
    parser.add_argument('--json', action='store_true', help='Output as JSON')
    parser.add_argument('--split', action='store_true', help='Split into 3 messages (completed/calendar/todos)')
    parser.add_argument('--skip-missed', action='store_true', help='Skip missed tasks section')
    
    args = parser.parse_args()
    
    _, tasks_data = load_tasks()
    missed_buckets = None
    if not args.skip_missed:
        missed_buckets = get_missed_tasks_bucketed(tasks_data, reference_date=args.date)

    completed_from_notes: list[str] = []
    notes_dir_raw = os.getenv("TASK_TRACKER_DAILY_NOTES_DIR")
    if notes_dir_raw:
        standup_date = resolve_standup_date(args.date)
        yesterday = standup_date - timedelta(days=1)
        completed_from_notes = extract_completed_actions(
            notes_dir=Path(notes_dir_raw),
            start_date=yesterday,
            end_date=yesterday,
        )

    result = generate_standup(
        date_str=args.date,
        json_output=args.json,
        split_output=args.split,
        tasks_data=tasks_data,
    )

    missed_block = ""
    if not args.json:
        missed_block = format_missed_tasks_block(missed_buckets)
        if missed_block:
            if args.split:
                result = [f"{missed_block}{result[0]}"] + result[1:]
            else:
                result = f"{missed_block}{result}"

    if not args.json and completed_from_notes:
        notes_lines = ["📌 **Also Done Yesterday:**"]
        for action in completed_from_notes:
            notes_lines.append(f"  • {action}")
        notes_block = "\n".join(notes_lines)

        if args.split:
            result = [f"{result[0]}\n\n{notes_block}"] + result[1:]
        else:
            result = f"{result}\n\n{notes_block}"
    
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
