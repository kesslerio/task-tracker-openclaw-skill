#!/usr/bin/env python3
"""
eod_sync.py — Auto-sync EOD completions from daily notes to Weekly TODOs.

For each completed item in the daily note's ✅ Done section, fuzzy-match
against unchecked tasks in Weekly TODOs and mark them complete.

Thresholds:
  ≥ 80%  → auto-sync (mark done)
  60-79% → log as uncertain (manual review needed)
  < 60%  → skip

Configuration via environment variables:
  TASK_TRACKER_WEEKLY_TODOS   Path to Weekly TODOs file
  TASK_TRACKER_DAILY_NOTES_DIR  Directory containing YYYY-MM-DD.md files

Usage:
  python3 eod_sync.py                   # sync today
  python3 eod_sync.py --dry-run         # preview without writing
  python3 eod_sync.py --date 2026-02-18 # sync a specific day
  python3 eod_sync.py --verbose         # show all match scores
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime, date
from difflib import SequenceMatcher
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

# ---------------------------------------------------------------------------
# Threshold constants
# ---------------------------------------------------------------------------

AUTO_SYNC_THRESHOLD = 0.80   # ≥ 80% → auto-sync
UNCERTAIN_THRESHOLD = 0.60   # 60-79% → log uncertain

# ---------------------------------------------------------------------------
# Text normalisation
# ---------------------------------------------------------------------------

# Emojis used as task-plugin priority / date markers to strip
_EMOJI_STRIP_RE = re.compile(
    r"[📅🗓️🔺⏫🔼🔽⏬✅☑️]",
    re.UNICODE,
)

# Date patterns: YYYY-MM-DD and "Feb 18", "Feb. 18" style
_DATE_STRIP_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}"
    r"|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\.?\s+\d{1,2}",
    re.IGNORECASE,
)

# Tags: #sales, #ops, etc.
_TAG_STRIP_RE = re.compile(r"#\w+")

# Checkbox markers: - [ ] / - [x]
_CHECKBOX_RE = re.compile(r"^-\s*\[[ xX]\]\s*")

# Leading "- " (plain list items)
_LIST_MARKER_RE = re.compile(r"^-\s+")

# Multiple whitespace
_MULTI_WS_RE = re.compile(r"\s{2,}")

# Trailing parenthetical: " (Feb 18)" " (follow-ups)" etc.
# We strip only if the paren contains a date reference (to handle slight rewording)
_TRAILING_DATE_PAREN_RE = re.compile(
    r"\s*\([^)]*(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec|\d{4})[^)]*\)\s*$",
    re.IGNORECASE,
)


def normalize(text: str) -> str:
    """Return a canonical lowercase string for fuzzy comparison."""
    t = _CHECKBOX_RE.sub("", text)
    t = _LIST_MARKER_RE.sub("", t)
    t = _EMOJI_STRIP_RE.sub("", t)
    t = _TRAILING_DATE_PAREN_RE.sub("", t)  # strip "(Feb 18)" style before date pass
    t = _DATE_STRIP_RE.sub("", t)
    t = _TAG_STRIP_RE.sub("", t)
    # Remove empty parens left after stripping dates, e.g. "()" or "( )"
    t = re.sub(r"\(\s*\)", "", t)
    t = _MULTI_WS_RE.sub(" ", t).strip().lower()
    return t


def similarity(a: str, b: str) -> float:
    """Return SequenceMatcher ratio between two normalized strings."""
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    return SequenceMatcher(None, na, nb).ratio()


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

_DONE_SECTION_RE = re.compile(
    r"^##\s+(?:✅\s*)?Done\s*$",
    re.IGNORECASE,
)
_SECTION_RE = re.compile(r"^##\s+")


def parse_done_items(daily_note: str) -> list[str]:
    """Extract raw task lines from the ✅ Done section of a daily note."""
    lines = daily_note.splitlines()
    in_done = False
    items: list[str] = []

    for line in lines:
        if _DONE_SECTION_RE.match(line):
            in_done = True
            continue
        if in_done:
            # Stop at next ## section
            if _SECTION_RE.match(line) and not _DONE_SECTION_RE.match(line):
                break
            # Collect ONLY checked checkbox items (not unchecked [- ])
            # Must be - [x] or - [X], not - [ ]
            stripped = line.strip()
            if re.match(r"^-\s*\[[xX]\]", stripped) or re.match(r"^-\s+\S", stripped):
                # Skip placeholder text
                if stripped.lower() in {"- (update as day progresses)", "- (none today)"}:
                    continue
                items.append(stripped)

    return items


_UNCHECKED_RE = re.compile(r"^(\s*)- \[ \] (.+)$")


def parse_weekly_open_tasks(weekly_content: str) -> list[dict]:
    """Return list of unchecked tasks from Weekly TODOs with position info."""
    tasks = []
    lines = weekly_content.splitlines(keepends=True)

    for idx, line in enumerate(lines):
        m = _UNCHECKED_RE.match(line.rstrip("\n"))
        if m:
            indent = m.group(1)
            body = m.group(2)
            tasks.append(
                {
                    "line_idx": idx,
                    "indent": indent,
                    "body": body,
                    "raw": line,
                }
            )

    return tasks


# ---------------------------------------------------------------------------
# Sync logic
# ---------------------------------------------------------------------------

SyncResult = dict  # typed alias for readability


def build_sync_plan(
    done_items: list[str],
    open_tasks: list[dict],
    sync_date: str,
    verbose: bool = False,
) -> list[SyncResult]:
    """
    For each done_item, find the best-matching open task.

    Returns list of result dicts:
      status: 'sync' | 'uncertain' | 'skip'
      done_item: original done item text
      match: matched task dict or None
      score: float 0-1
    """
    results: list[SyncResult] = []
    matched_task_indices: set[int] = set()

    # Greedy matching: process each done_item in order, assign to best available task.
    # Tasks can only be matched once (matched_task_indices prevents duplicates).
    # This maximizes total matches though not guaranteed globally optimal.
    for done_item in done_items:
        best_score = 0.0
        best_task = None
        best_idx = -1

        for task in open_tasks:
            if task["line_idx"] in matched_task_indices:
                continue
            score = similarity(done_item, task["body"])
            if score > best_score:
                best_score = score
                best_task = task
                best_idx = task["line_idx"]

        if best_score >= AUTO_SYNC_THRESHOLD and best_task is not None:
            matched_task_indices.add(best_idx)
            results.append(
                {
                    "status": "sync",
                    "done_item": done_item,
                    "match": best_task,
                    "score": best_score,
                }
            )
        elif best_score >= UNCERTAIN_THRESHOLD and best_task is not None:
            results.append(
                {
                    "status": "uncertain",
                    "done_item": done_item,
                    "match": best_task,
                    "score": best_score,
                }
            )
        else:
            results.append(
                {
                    "status": "skip",
                    "done_item": done_item,
                    "match": best_task,
                    "score": best_score,
                }
            )

    return results


def apply_sync_plan(
    weekly_lines: list[str],
    plan: list[SyncResult],
    sync_date: str,
) -> list[str]:
    """
    Return new lines with matched tasks marked complete.
    Mutates nothing; returns a new list.
    """
    updated = list(weekly_lines)

    for result in plan:
        if result["status"] != "sync":
            continue
        task = result["match"]
        idx = task["line_idx"]
        indent = task["indent"]
        body = task["body"]

        # Preserve the full line including checkbox prefix, just swap [ ] to [x]
        # and append completion date.
        # Pattern: "- [ ] Task title 📅 2026-02-19 ⏫ #tag" -> "- [x] Task title 📅 2026-02-19 ⏫ #tag ✅ 2026-02-19"
        # Use raw line (includes checkbox) not body (already stripped)
        raw_line = task["raw"].rstrip("\n")
        # Replace unchecked checkbox with checked
        new_line = raw_line.replace("- [ ]", "- [x]")
        # Remove any existing completion date to avoid duplicates
        new_line = re.sub(r"\s+✅\s*\d{4}-\d{2}-\d{2}\s*$", "", new_line)
        new_line = f"{new_line} ✅ {sync_date}\n"

        updated[idx] = new_line

    return updated


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def print_report(plan: list[SyncResult], dry_run: bool, verbose: bool = False) -> None:
    """Print a human-readable sync report."""
    synced = [r for r in plan if r["status"] == "sync"]
    uncertain = [r for r in plan if r["status"] == "uncertain"]
    skipped = [r for r in plan if r["status"] == "skip"]

    mode = "DRY RUN — " if dry_run else ""
    print(f"\n{mode}EOD Sync Report")
    print("=" * 50)

    if verbose:
        # Show all match attempts with scores
        print("\n📊 All match attempts:")
        for r in plan:
            pct = int(r["score"] * 100)
            status_icon = {"sync": "✅", "uncertain": "⚠️", "skip": "⏭️"}[r["status"]]
            weekly_title = r["match"]["body"] if r["match"] else "(no match)"
            print(f"  {status_icon} [{pct}%] {normalize(r['done_item'])}")
            if r["match"]:
                print(f"         → {normalize(weekly_title)}")
    else:
        # Default: show only synced, uncertain, skipped summaries
        if synced:
            print(f"\n✅ Auto-synced ({len(synced)}):")
            for r in synced:
                pct = int(r["score"] * 100)
                weekly_title = r["match"]["body"] if r["match"] else "?"
                print(f"  [{pct}%] {normalize(r['done_item'])!r}")
                print(f"         → {normalize(weekly_title)!r}")

        if uncertain:
            print(f"\n⚠️  Uncertain — manual review needed ({len(uncertain)}):")
            for r in uncertain:
                pct = int(r["score"] * 100)
                weekly_title = r["match"]["body"] if r["match"] else "?"
                print(f"  [{pct}%] {normalize(r['done_item'])!r}")
                print(f"         ~  {normalize(weekly_title)!r}")

        if skipped:
            print(f"\n⏭️  Skipped — no match found ({len(skipped)}):")
            for r in skipped:
                pct = int(r["score"] * 100)
                label = f"[{pct}%] " if r["match"] else "[---] "
                print(f"  {label}{normalize(r['done_item'])!r}")

    print()
    action = "Would sync" if dry_run else "Synced"
    print(
        f"Summary: {action} {len(synced)}, uncertain {len(uncertain)}, "
        f"skipped {len(skipped)}"
    )
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sync EOD completions from daily notes to Weekly TODOs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--date",
        default=None,
        help="Date to sync (YYYY-MM-DD). Defaults to today.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview what would change without writing files.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show all match scores, including low-confidence ones.",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=None,
        help="Override auto-sync threshold (0-1, default 0.80).",
    )
    args = parser.parse_args()

    # Resolve date
    if args.date:
        try:
            sync_date_obj = datetime.strptime(args.date, "%Y-%m-%d").date()
        except ValueError:
            print(f"❌ Invalid date format: {args.date!r} — expected YYYY-MM-DD", file=sys.stderr)
            sys.exit(1)
    else:
        sync_date_obj = date.today()

    sync_date = sync_date_obj.strftime("%Y-%m-%d")

    # Override threshold if requested
    global AUTO_SYNC_THRESHOLD
    if args.threshold is not None:
        AUTO_SYNC_THRESHOLD = max(0.0, min(1.0, args.threshold))

    # Load daily note
    daily_note_path = DAILY_NOTES_DIR / f"{sync_date}.md"
    if not daily_note_path.exists():
        print(f"❌ Daily note not found: {daily_note_path}", file=sys.stderr)
        sys.exit(1)

    daily_content = daily_note_path.read_text(encoding="utf-8")
    done_items = parse_done_items(daily_content)

    if not done_items:
        print(f"ℹ️  No completed items found in {daily_note_path.name} ✅ Done section.")
        sys.exit(0)

    print(f"📓 Daily note: {daily_note_path.name} — {len(done_items)} completed item(s)")

    # Load Weekly TODOs
    if not WEEKLY_TODOS_PATH.exists():
        print(f"❌ Weekly TODOs not found: {WEEKLY_TODOS_PATH}", file=sys.stderr)
        sys.exit(1)

    weekly_content = WEEKLY_TODOS_PATH.read_text(encoding="utf-8")
    weekly_lines = weekly_content.splitlines(keepends=True)
    open_tasks = parse_weekly_open_tasks(weekly_content)

    print(f"📋 Weekly TODOs: {len(open_tasks)} open task(s)")

    if not open_tasks:
        print("ℹ️  No open tasks in Weekly TODOs — nothing to sync.")
        sys.exit(0)

    # Build sync plan
    plan = build_sync_plan(done_items, open_tasks, sync_date, verbose=args.verbose)

    # Print report
    print_report(plan, dry_run=args.dry_run, verbose=args.verbose)

    # Apply plan (unless dry run)
    if not args.dry_run:
        new_lines = apply_sync_plan(weekly_lines, plan, sync_date)
        new_content = "".join(new_lines)

        if new_content == weekly_content:
            print("ℹ️  No changes to write.")
        else:
            WEEKLY_TODOS_PATH.write_text(new_content, encoding="utf-8")
            synced_count = sum(1 for r in plan if r["status"] == "sync")
            print(f"✅ Wrote {WEEKLY_TODOS_PATH.name} ({synced_count} task(s) marked done)")


if __name__ == "__main__":
    main()
