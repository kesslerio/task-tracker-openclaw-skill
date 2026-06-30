#!/usr/bin/env python3
"""
EOD Review Generator - Aggregates daily note + work tasks into a structured EOD review.

Reads from:
1. TASK_TRACKER_DAILY_NOTES_DIR/{date}.md — canonical daily-note completion evidence
2. Work Tasks.md — fallback completed items and open Q1/Q2 items for "tomorrow's top 3"

Outputs:
- Default: writes 01-Reports/{date}-eod.md AND prints to stdout
- --json: JSON dict for programmatic use
- --telegram: condensed markdown for Telegram delivery
"""

import argparse
import json
import os
import sys
from datetime import datetime, date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import error_envelope
from candidate_review import candidate_review_summary
from daily_notes import extract_completed_tasks
from task_audit import task_audit_summary
from utils import load_tasks

OBSIDIAN_VAULT = Path(os.getenv('OBSIDIAN_VAULT', Path.home() / "Obsidian"))
_DAILY_NOTES_DEFAULT = OBSIDIAN_VAULT / "01-TODOs" / "Daily"
OUTPUT_DIR = Path(os.getenv('EOD_OUTPUT_DIR', OBSIDIAN_VAULT / "01-Reports"))


def daily_notes_dir() -> Path:
    raw = os.getenv("TASK_TRACKER_DAILY_NOTES_DIR")
    return (Path(raw) if raw else _DAILY_NOTES_DEFAULT).expanduser()


def read_canonical_daily_note(target_day: date) -> dict:
    """Read completion evidence from the canonical daily-note directory."""
    notes_dir = daily_notes_dir()
    note_path = notes_dir / f"{target_day.isoformat()}.md"
    completed = extract_completed_tasks(
        notes_dir=notes_dir,
        start_date=target_day,
        end_date=target_day,
    )
    return {
        "done": [task["title"] for task in completed],
        "not_done": [],
        "path": note_path,
        "exists": note_path.exists(),
    }


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

    parsed = read_canonical_daily_note(target_date.date())
    source = 'TASK_TRACKER_DAILY_NOTES_DIR'
    _, tasks_data = load_tasks()

    if parsed["exists"]:
        done = parsed['done']
        not_done = parsed['not_done']
    else:
        # Fallback: Work Tasks.md done section (with staleness caveat)
        source = 'Work Tasks.md (fallback — no canonical daily note found)'
        done = [t['title'] for t in tasks_data.get('done', [])[:8]]
        not_done = []

    # Tomorrow's top 3 from Work Tasks.md
    tomorrows_top3 = get_tomorrows_top3(tasks_data)

    return {
        'date': date_str,
        'date_display': date_display,
        'weekday': weekday,
        'done': done,
        'not_done': not_done,
        'tomorrows_top3': tomorrows_top3,
        'completion_candidates': candidate_review_summary(),
        'task_audit': task_audit_summary(limit=3),
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

    candidates = data.get('completion_candidates') or {}
    lines.extend(['', '## Completion Candidates'])
    if candidates.get('review_required'):
        lines.append(f"{candidates.get('total', 0)} candidate(s) need review.")
        for candidate in candidates.get('items', [])[:5]:
            task_hint = candidate.get('confirmable_task_id') or candidate.get('suggested_task_id')
            suffix = f" -> {task_hint}" if task_hint else ""
            lines.append(f"- {candidate.get('candidate_id')}: {candidate.get('summary')}{suffix}")
        lines.append("")
        lines.append("Review required; do not auto-complete from this summary.")
    else:
        lines.append('_No active completion candidates_')

    audit = data.get('task_audit') or {}
    lines.extend(['', '## Task Audit'])
    if audit.get('review_required') and not audit.get('available', True):
        error = audit.get('error') or {}
        lines.append(f"Audit unavailable: {error.get('code', 'unknown-error')}.")
    elif audit.get('review_required'):
        lines.append(f"{audit.get('total', 0)} task-health finding(s) need review.")
        for finding in audit.get('items', [])[:3]:
            lines.append(f"- {finding.get('severity')}: {finding.get('code')} — {finding.get('reason')}")
        if audit.get('overflow'):
            lines.append(f"- ... and {audit['overflow']} more")
        lines.append("")
        lines.append("Run `tasks.py task-audit`; do not mutate from audit text.")
    else:
        lines.append('_No task-health findings_')

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

    candidates = data.get('completion_candidates') or {}
    lines.extend(['', 'Completion candidates:'])
    if candidates.get('review_required'):
        lines.append(f"- {candidates.get('total', 0)} need review")
        for candidate in candidates.get('items', [])[:3]:
            lines.append(f"- {candidate.get('candidate_id')}: {candidate.get('summary')}")
    else:
        lines.append('- None')

    audit = data.get('task_audit') or {}
    lines.extend(['', 'Task audit:'])
    if audit.get('review_required') and not audit.get('available', True):
        error = audit.get('error') or {}
        lines.append(f"- Unavailable: {error.get('code', 'unknown-error')}")
    elif audit.get('review_required'):
        lines.append(f"- {audit.get('total', 0)} need review")
        for finding in audit.get('items', [])[:3]:
            lines.append(f"- {finding.get('severity')}: {finding.get('code')}")
    else:
        lines.append('- None')

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
    sys.exit(error_envelope.run_main("eod_review", main))
