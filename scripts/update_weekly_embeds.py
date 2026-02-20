#!/usr/bin/env python3
"""
update_weekly_embeds.py — Refresh the 📊 Daily Progress section in Weekly TODOs.

Calculates the current week's Monday–Friday dates and updates (or creates)
the `## 📊 Daily Progress` section with Obsidian transclusion links:

  ![[01-TODOs/Daily/2026-02-17#Done]]

Call this at the start of each week or after the weekly note is recreated.

Configuration via environment variables:
  TASK_TRACKER_WEEKLY_TODOS      Path to Weekly TODOs file
  TASK_TRACKER_DAILY_NOTES_DIR   Directory containing YYYY-MM-DD.md files
                                 (used to derive the vault-relative path prefix)

Usage:
  python3 update_weekly_embeds.py            # update for current week
  python3 update_weekly_embeds.py --week 2026-02-17  # any Monday date
  python3 update_weekly_embeds.py --dry-run  # preview without writing
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime, timedelta, date
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

WEEKLY_TODOS_DEFAULT = Path.home() / "Obsidian" / "01-TODOs" / "Weekly TODOs.md"
DAILY_NOTES_DEFAULT = Path.home() / "Obsidian" / "01-TODOs" / "Daily"

WEEKLY_TODOS_PATH = Path(
    os.getenv("TASK_TRACKER_WEEKLY_TODOS", str(WEEKLY_TODOS_DEFAULT))
).expanduser()

DAILY_NOTES_DIR = Path(
    os.getenv(
        "TASK_TRACKER_DAILY_NOTES_DIR",
        str(DAILY_NOTES_DEFAULT),
    )
).expanduser()

# Obsidian vault-relative prefix for transclusion links.
# Derived from DAILY_NOTES_DIR by stripping everything up to and including
# the Obsidian vault root (the first component named "Obsidian" or its parent).
def _vault_relative_prefix(daily_dir: Path) -> str:
    """Return vault-relative path prefix, e.g. '01-TODOs/Daily'."""
    parts = daily_dir.parts
    # Find the Obsidian vault root: first part named "Obsidian"
    for i, part in enumerate(parts):
        if part == "Obsidian":
            # Everything after "Obsidian/" is vault-relative
            return "/".join(parts[i + 1 :])
    # Fallback: use the last two path components
    return "/".join(parts[-2:]) if len(parts) >= 2 else str(daily_dir)


VAULT_PREFIX = _vault_relative_prefix(DAILY_NOTES_DIR)  # e.g. "01-TODOs/Daily"

# Section header that we manage
PROGRESS_SECTION_HEADER = "## 📊 Daily Progress"

# Anchor used in transclusion links (matches the Done section header in daily notes)
# Supports both "## Done" and "## ✅ Done" formats
DONE_ANCHOR = "Done"

DAYS_OF_WEEK = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def get_week_monday(reference: date) -> date:
    """Return the Monday of the week containing *reference*."""
    return reference - timedelta(days=reference.weekday())


def build_progress_section(monday: date) -> str:
    """Build the full ## 📊 Daily Progress markdown block for the given week."""
    lines = [PROGRESS_SECTION_HEADER, ""]
    for i, day_name in enumerate(DAYS_OF_WEEK):
        day = monday + timedelta(days=i)
        date_str = day.strftime("%Y-%m-%d")
        lines.append(f"### {day_name}")
        lines.append(f"![[{VAULT_PREFIX}/{date_str}#{DONE_ANCHOR}]]")
        lines.append("")
    return "\n".join(lines)


# Regex to find the ## 📊 Daily Progress section (greedy to end of file or
# until the next ## heading).
_PROGRESS_SECTION_RE = re.compile(
    r"(^## 📊 Daily Progress\s*\n)"  # opening header
    r"(.*?)"                          # section body (non-greedy)
    r"(?=^##\s|\Z)",                  # lookahead: next ## or EOF
    re.MULTILINE | re.DOTALL,
)


def update_or_append_progress_section(content: str, new_section: str) -> str:
    """Replace existing progress section or append at end of file."""
    replacement = new_section + "\n\n"
    if _PROGRESS_SECTION_RE.search(content):
        return _PROGRESS_SECTION_RE.sub(replacement, content, count=1)
    # Not found — append before the Tasks Query block if present, else at end
    tasks_query_marker = "## 📋 Tasks Query"
    if tasks_query_marker in content:
        idx = content.index(tasks_query_marker)
        return content[:idx] + replacement + content[idx:]
    return content.rstrip() + "\n\n" + replacement


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Refresh the 📊 Daily Progress section in Weekly TODOs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--week",
        default=None,
        metavar="YYYY-MM-DD",
        help="Any date in the target week (defaults to current week).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the generated section without writing the file.",
    )
    args = parser.parse_args()

    # Resolve the reference date
    if args.week:
        try:
            ref_date = datetime.strptime(args.week, "%Y-%m-%d").date()
        except ValueError:
            print(
                f"❌ Invalid date format: {args.week!r} — expected YYYY-MM-DD",
                file=sys.stderr,
            )
            sys.exit(1)
    else:
        ref_date = date.today()

    monday = get_week_monday(ref_date)
    week_str = monday.strftime("%Y-W%V")

    print(f"📅 Week: {week_str} (Mon {monday.strftime('%Y-%m-%d')})")

    new_section = build_progress_section(monday)

    if args.dry_run:
        print("\n--- Generated section (dry run) ---\n")
        print(new_section)
        print("\n--- End ---")
        return

    if not WEEKLY_TODOS_PATH.exists():
        print(f"❌ Weekly TODOs not found: {WEEKLY_TODOS_PATH}", file=sys.stderr)
        sys.exit(1)

    original = WEEKLY_TODOS_PATH.read_text(encoding="utf-8")
    updated = update_or_append_progress_section(original, new_section)

    if updated == original:
        print("ℹ️  No changes needed — section already up to date.")
        return

    WEEKLY_TODOS_PATH.write_text(updated, encoding="utf-8")
    print(f"✅ Updated {WEEKLY_TODOS_PATH.name} with Daily Progress for week {week_str}")


if __name__ == "__main__":
    main()
