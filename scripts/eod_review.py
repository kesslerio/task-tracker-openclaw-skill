#!/usr/bin/env python3
"""
EOD Review Generator - Aggregates daily note + work tasks into a structured EOD review.

Reads from:
1. 06-Daily/{date}.md — primary source for today's done/not-done items
2. Work Tasks.md — open Q1/Q2 items for "tomorrow's top 3"
3. Google Calendar via gog CLI (optional, reuses standup.py pattern)

Outputs:
- Default: writes 01-Reports/{date}-eod.md AND prints to stdout
- --json: JSON dict for programmatic use
- --telegram: condensed markdown for Telegram delivery
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

from utils import load_tasks

OBSIDIAN_VAULT = Path(os.getenv('OBSIDIAN_VAULT', Path.home() / "Obsidian"))
DAILY_DIR = Path(os.getenv('EOD_DAILY_DIR', OBSIDIAN_VAULT / "06-Daily"))
OUTPUT_DIR = Path(os.getenv('EOD_OUTPUT_DIR', OBSIDIAN_VAULT / "01-Reports"))

WEEKDAY_MAP = {
    'mon': 0, 'monday': 0,
    'tue': 1, 'tuesday': 1,
    'wed': 2, 'wednesday': 2,
    'thu': 3, 'thursday': 3,
    'fri': 4, 'friday': 4,
    'sat': 5, 'saturday': 5,
    'sun': 6, 'sunday': 6,
}


def parse_daily_note(content: str, target_date: datetime) -> dict:
    """Parse a 06-Daily note into done/not-done items.

    Handles two observed formats:
    - Checkboxes directly under "### Today (...)"
    - A "#### Done" subsection under "### Today"
    - Weekly priorities with "(done Weekday)" annotations
    """
    done = []
    not_done = []
    target_weekday = target_date.weekday()

    lines = content.split('\n')
    in_today = False
    in_today_done = False
    in_weekly = False
    current_weekly_section = None

    for line in lines:
        stripped = line.strip()

        # Detect section headers
        if stripped.startswith('### Today'):
            in_today = True
            in_today_done = False
            in_weekly = False
            continue
        if stripped.startswith('### Yesterday') or stripped.startswith('### '):
            if stripped.startswith('### This Week'):
                in_weekly = True
                in_today = False
                in_today_done = False
                current_weekly_section = None
                continue
            if not stripped.startswith('### This Week'):
                in_today = False
                in_today_done = False
                if not in_weekly:
                    continue

        # "#### Done" subsection under Today
        if in_today and stripped.startswith('#### Done'):
            in_today_done = True
            continue
        if in_today and stripped.startswith('#### '):
            in_today_done = False
            continue

        # Track weekly subsection headers (### SALES, ### MARKETING, etc.)
        if in_weekly and stripped.startswith('### '):
            current_weekly_section = stripped.lstrip('#').strip()
            continue

        # Parse checkboxes in Today section
        if in_today:
            checkbox = re.match(r'^- \[([ xX])\] (.+)$', stripped)
            if checkbox:
                is_done = checkbox.group(1).lower() == 'x'
                item_text = checkbox.group(2).strip()
                if is_done or in_today_done:
                    done.append(item_text)
                else:
                    not_done.append(item_text)

        # Parse weekly priorities with "(done Weekday)" matching target date
        if in_weekly:
            checkbox = re.match(r'^- \[([ xX])\] (.+)$', stripped)
            if checkbox:
                is_done = checkbox.group(1).lower() == 'x'
                item_text = checkbox.group(2).strip()

                # Check for "(done Weekday)" annotation
                done_match = re.search(r'\(done\s+(\w+)\)', item_text, re.IGNORECASE)
                if done_match:
                    weekday_str = done_match.group(1).lower()
                    weekday_num = WEEKDAY_MAP.get(weekday_str)
                    if weekday_num == target_weekday:
                        # Strip the annotation for cleaner output
                        clean = re.sub(r'\s*\(done\s+\w+\)', '', item_text).strip()
                        prefix = f"[{current_weekly_section}] " if current_weekly_section else ""
                        done.append(f"{prefix}{clean}")
                elif is_done and not done_match:
                    # Checked but no weekday annotation — skip (could be any day)
                    pass
                elif not is_done:
                    prefix = f"[{current_weekly_section}] " if current_weekly_section else ""
                    not_done.append(f"{prefix}{item_text}")

    return {'done': done, 'not_done': not_done}


def get_tomorrows_top3(tasks_data: dict) -> list[str]:
    """Pick top 3 priorities from open Q1/Q2 items in Work Tasks.md."""
    candidates = []
    for task in tasks_data.get('q1', []):
        if not task.get('done'):
            candidates.append(task['title'])
    for task in tasks_data.get('q2', []):
        if not task.get('done'):
            candidates.append(task['title'])
    return candidates[:3]


def generate_eod(target_date: datetime = None) -> dict:
    """Generate EOD review data.

    Returns dict with: date, date_display, done, not_done, tomorrows_top3, source
    """
    if target_date is None:
        target_date = datetime.now()

    date_str = target_date.strftime('%Y-%m-%d')
    date_display = target_date.strftime('%A, %B %d').replace(' 0', ' ')
    weekday = target_date.strftime('%A')

    daily_note_path = DAILY_DIR / f"{date_str}.md"
    source = '06-Daily'

    if daily_note_path.exists():
        content = daily_note_path.read_text()
        parsed = parse_daily_note(content, target_date)
        done = parsed['done']
        not_done = parsed['not_done']
    else:
        # Fallback: Work Tasks.md done section (with staleness caveat)
        source = 'Work Tasks.md (fallback — no daily note found)'
        _, tasks_data = load_tasks()
        done = [t['title'] for t in tasks_data.get('done', [])[:8]]
        not_done = []

    # Tomorrow's top 3 from Work Tasks.md
    _, tasks_data = load_tasks()
    tomorrows_top3 = get_tomorrows_top3(tasks_data)

    return {
        'date': date_str,
        'date_display': date_display,
        'weekday': weekday,
        'done': done,
        'not_done': not_done,
        'tomorrows_top3': tomorrows_top3,
        'source': source,
    }


def format_markdown(data: dict) -> str:
    """Format EOD data as full markdown for 01-Reports file."""
    lines = [
        f"# EOD Review — {data['weekday']}, {data['date']}",
        '',
        f"_Source: {data['source']}_",
        '',
        '## Done',
    ]

    if data['done']:
        for item in data['done']:
            lines.append(f"- {item}")
    else:
        lines.append('_Nothing recorded_')

    lines.extend(['', "## Didn't Get Done"])

    if data['not_done']:
        for item in data['not_done']:
            lines.append(f"- {item}")
    else:
        lines.append('_Everything done (or nothing tracked)_')

    lines.extend(['', "## Tomorrow's Top 3"])

    if data['tomorrows_top3']:
        for i, item in enumerate(data['tomorrows_top3'], 1):
            lines.append(f"{i}. {item}")
    else:
        lines.append('_No open Q1/Q2 items_')

    lines.extend([
        '',
        f"_Generated {datetime.now().isoformat()} via eod_review.py_",
    ])

    return '\n'.join(lines)


def format_telegram(data: dict) -> str:
    """Format condensed EOD for Telegram delivery."""
    lines = [f"EOD Review — {data['weekday']}, {data['date']}", '']

    lines.append('Done:')
    if data['done']:
        for item in data['done'][:8]:
            lines.append(f"- {item}")
    else:
        lines.append('- Nothing recorded')

    lines.extend(['', 'Missed:'])
    if data['not_done']:
        for item in data['not_done'][:6]:
            lines.append(f"- {item}")
    else:
        lines.append('- All clear')

    lines.extend(['', "Tomorrow's Top 3:"])
    if data['tomorrows_top3']:
        for i, item in enumerate(data['tomorrows_top3'], 1):
            lines.append(f"{i}. {item}")
    else:
        lines.append('- TBD')

    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description='Generate EOD review from daily notes')
    parser.add_argument('--date', help='Date for review (YYYY-MM-DD), default: today')
    parser.add_argument('--json', action='store_true', help='Output as JSON')
    parser.add_argument('--telegram', action='store_true', help='Condensed Telegram format')
    parser.add_argument('--no-write', action='store_true', help='Skip writing 01-Reports file')

    args = parser.parse_args()

    target_date = None
    if args.date:
        try:
            target_date = datetime.strptime(args.date, '%Y-%m-%d')
        except ValueError:
            print(f"Invalid date format: {args.date} (expected YYYY-MM-DD)", file=sys.stderr)
            sys.exit(1)

    data = generate_eod(target_date)

    if args.json:
        print(json.dumps(data, indent=2))
        return

    if args.telegram:
        print(format_telegram(data))
        return

    # Default: write file + print
    md = format_markdown(data)

    if not args.no_write:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        out_path = OUTPUT_DIR / f"{data['date']}-eod.md"
        tmp_path = OUTPUT_DIR / f".{data['date']}-eod.md.tmp"
        tmp_path.write_text(md)
        tmp_path.rename(out_path)
        print(f"Wrote {out_path}", file=sys.stderr)

    print(md)


if __name__ == '__main__':
    main()
