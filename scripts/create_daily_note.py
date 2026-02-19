#!/usr/bin/env python3
"""
Daily note creator with carry-forward from yesterday and weekly backlog.

Usage:
    python3 scripts/create_daily_note.py [--date YYYY-MM-DD] [--dry-run] < calendar.json
"""

import argparse
import json
import logging
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

# Add lib directory to path for imports
_SCRIPT_DIR = Path(__file__).parent.resolve()
if str(_SCRIPT_DIR / "lib") not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR / "lib"))

from daily_note.parser import parse_open_tasks, parse_top_priority_tasks
from daily_note.deduper import merge_tasks
from daily_note.composer import compose_daily_note

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# Default paths (overridable via env)
OBSIDIAN_VAULT = Path(os.environ.get("OBSIDIAN_VAULT", "~/Obsidian")).expanduser()
DAILY_DIR = OBSIDIAN_VAULT / "01-TODOs" / "Daily"
WEEKLY_FILE = OBSIDIAN_VAULT / "01-TODOs" / "Weekly TODOs.md"


def get_yesterday_date(today: datetime) -> datetime:
    """Get yesterday's date, handling Sunday -> Friday for work weeks."""
    yesterday = today - timedelta(days=1)
    # If today is Monday, yesterday is Friday (skip weekend)
    if today.weekday() == 0:  # Monday
        yesterday = today - timedelta(days=3)
    return yesterday


def read_yesterday_tasks(yesterday_date: datetime) -> list[str]:
    """Read open tasks from yesterday's daily note."""
    yesterday_path = DAILY_DIR / f"{yesterday_date.strftime('%Y-%m-%d')}.md"
    if not yesterday_path.exists():
        logger.warning(f"Yesterday's note not found: {yesterday_path}")
        return []
    try:
        content = yesterday_path.read_text(encoding="utf-8")
        return parse_open_tasks(content)
    except Exception as e:
        logger.warning(f"Failed to read yesterday's note: {e}")
        return []


def read_weekly_tasks() -> list[str]:
    """Read top-priority open items from weekly file."""
    if not WEEKLY_FILE.exists():
        logger.warning(f"Weekly file not found: {WEEKLY_FILE}")
        return []
    try:
        content = WEEKLY_FILE.read_text(encoding="utf-8")
        return parse_top_priority_tasks(content)
    except Exception as e:
        logger.warning(f"Failed to read weekly file: {e}")
        return []


def read_calendar_data() -> list[dict]:
    """Read calendar data from stdin (JSON array of events)."""
    try:
        if sys.stdin.isatty():
            return []
        data = sys.stdin.read().strip()
        if not data:
            return []
        events = json.loads(data)
        if isinstance(events, list):
            return events
        return []
    except json.JSONDecodeError:
        logger.warning("Invalid JSON in stdin, skipping calendar")
        return []
    except Exception as e:
        logger.warning(f"Failed to read calendar data: {e}")
        return []


def write_daily_note(date_str: str, content: str) -> Path:
    """Atomically write daily note to file."""
    DAILY_DIR.mkdir(parents=True, exist_ok=True)
    note_path = DAILY_DIR / f"{date_str}.md"
    
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{note_path.name}.", suffix=".tmp", dir=str(DAILY_DIR)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            tmp.write(content)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_name, note_path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    
    return note_path


def main():
    parser = argparse.ArgumentParser(description="Create daily note with carry-forward")
    parser.add_argument("--date", help="Target date (YYYY-MM-DD), default: today")
    parser.add_argument("--dry-run", action="store_true", help="Print to stdout instead of writing")
    args = parser.parse_args()

    # Determine target date
    if args.date:
        try:
            today = datetime.strptime(args.date, "%Y-%m-%d")
        except ValueError:
            logger.error(f"Invalid date format: {args.date}")
            sys.exit(1)
    else:
        today = datetime.now()
    
    date_str = today.strftime("%Y-%m-%d")
    
    # Check for idempotency - don't overwrite existing note
    note_path = DAILY_DIR / f"{date_str}.md"
    if note_path.exists() and not args.dry_run:
        logger.warning(f"Daily note already exists: {note_path}")
        print(f"Note already exists: {note_path}", file=sys.stderr)
        sys.exit(0)

    # Gather data
    yesterday = get_yesterday_date(today)
    yesterday_tasks = read_yesterday_tasks(yesterday)
    weekly_tasks = read_weekly_tasks()
    calendar_events = read_calendar_data()

    # Merge tasks (weekly is source of truth)
    all_tasks = merge_tasks(weekly_tasks, yesterday_tasks)

    # Split into top 3 and carried
    top_3 = all_tasks[:3] if len(all_tasks) >= 3 else all_tasks
    carried = all_tasks[3:] if len(all_tasks) > 3 else []

    # Compose note
    note_content = compose_daily_note(
        date_str=date_str,
        calendar_events=calendar_events,
        top_3=top_3,
        carried=carried,
    )

    if args.dry_run:
        print(note_content)
    else:
        written_path = write_daily_note(date_str, note_content)
        logger.info(f"Created daily note: {written_path}")
        print(f"Created: {written_path}")


if __name__ == "__main__":
    main()
