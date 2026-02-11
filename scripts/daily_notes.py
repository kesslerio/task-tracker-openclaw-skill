#!/usr/bin/env python3
"""
Helpers for extracting completed actions from daily notes.
"""

import json
import re
from datetime import date, datetime
from pathlib import Path


ACTION_VERBS = (
    "Completed",
    "Closed",
    "Shipped",
    "Fixed",
    "Resolved",
    "Launched",
    "Sent",
    "Created",
    "Built",
    "Deployed",
)
ACTION_VERB_RE = re.compile(
    rf"^(?:{'|'.join(ACTION_VERBS)})\b",
    flags=re.IGNORECASE,
)
NOTES_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})\.md$")


def _clean_action_line(line: str) -> str:
    """Strip common completion markers and bullet prefixes."""
    cleaned = line.strip()
    cleaned = re.sub(r"^\s*(?:[-*+•]\s*)*", "", cleaned)
    cleaned = re.sub(r"^(?:\[[xX]\]\s*)*", "", cleaned)
    cleaned = re.sub(r"^(?:✅\s*)*", "", cleaned)
    cleaned = re.sub(r"^\s*(?:[-*+•]\s*)*", "", cleaned)
    return cleaned.strip()


def _is_completed_action_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False

    # Must start with bullet/checkbox markers to be an action
    if not re.match(r"^\s*[-*+•]\s*", stripped):
        return False

    if "✅" in stripped:
        return True

    if re.search(r"\[[xX]\]", stripped):
        return True

    cleaned = _clean_action_line(stripped)
    return bool(ACTION_VERB_RE.match(cleaned))


def extract_completed_actions(
    notes_dir: Path,
    start_date: date,
    end_date: date,
) -> list[str]:
    """
    Extract completed action lines from YYYY-MM-DD markdown daily notes.

    Returns a deduplicated list preserving first-seen order.
    """
    if start_date > end_date:
        start_date, end_date = end_date, start_date

    if not notes_dir.exists() or not notes_dir.is_dir():
        return []

    completed_actions: list[str] = []
    seen: set[str] = set()

    for notes_file in sorted(notes_dir.glob("*.md")):
        match = NOTES_DATE_RE.fullmatch(notes_file.name)
        if not match:
            continue

        try:
            file_date = datetime.strptime(match.group(1), "%Y-%m-%d").date()
        except ValueError:
            continue

        if file_date < start_date or file_date > end_date:
            continue

        try:
            content = notes_file.read_text()
        except (PermissionError, UnicodeDecodeError, OSError):
            continue

        for raw_line in content.splitlines():
            if not _is_completed_action_line(raw_line):
                continue

            action = _clean_action_line(raw_line)
            if not action:
                continue

            dedupe_key = action.casefold()
            if dedupe_key in seen:
                continue

            seen.add(dedupe_key)
            completed_actions.append(action)

    return completed_actions


# Regex for timestamp-prefixed completion lines written by log_done():
#   "- HH:MM ✅ Task title"
_TIMESTAMPED_RE = re.compile(r"^-\s+(\d{2}:\d{2})\s+✅\s+(.+)")


def extract_completed_tasks(
    notes_dir: Path,
    start_date: date,
    end_date: date,
) -> list[dict]:
    """Extract completed tasks from daily notes as rich dicts.

    Each dict has keys: title, done, completed_date, timestamp, section,
    area, priority, due, recur.  Metadata is recovered from the JSON
    context line that log_done() writes directly below the action line.

    Returns a deduplicated list preserving first-seen order.
    Deduplicates by (title, completed_date) so recurring tasks completed
    on different days are counted separately.
    """
    if start_date > end_date:
        start_date, end_date = end_date, start_date

    if not notes_dir.exists() or not notes_dir.is_dir():
        return []

    results: list[dict] = []
    seen: set[tuple[str, str]] = set()  # (title_casefolded, date)

    for notes_file in sorted(notes_dir.glob("*.md")):
        match = NOTES_DATE_RE.fullmatch(notes_file.name)
        if not match:
            continue

        try:
            file_date = datetime.strptime(match.group(1), "%Y-%m-%d").date()
        except ValueError:
            continue

        if file_date < start_date or file_date > end_date:
            continue

        try:
            lines = notes_file.read_text().splitlines()
        except (PermissionError, UnicodeDecodeError, OSError):
            continue

        i = 0
        while i < len(lines):
            ts_match = _TIMESTAMPED_RE.match(lines[i])
            if not ts_match:
                # Fall back to generic completed-action detection
                if _is_completed_action_line(lines[i]):
                    title = _clean_action_line(lines[i])
                    if title:
                        dedupe_key = (title.casefold(), match.group(1))
                        if dedupe_key not in seen:
                            seen.add(dedupe_key)
                            results.append({
                                "title": title,
                                "done": True,
                                "completed_date": match.group(1),
                                "timestamp": None,
                                "section": None,
                                "area": None,
                                "priority": None,
                                "due": None,
                                "recur": None,
                            })
                i += 1
                continue

            timestamp = ts_match.group(1)
            title = ts_match.group(2).strip()

            # Look ahead for JSON context on the next indented line
            context: dict = {}
            if i + 1 < len(lines) and lines[i + 1].startswith("  "):
                candidate = lines[i + 1].strip()
                try:
                    parsed = json.loads(candidate)
                    if isinstance(parsed, dict):
                        context = parsed
                        i += 1  # skip the context line
                except (json.JSONDecodeError, ValueError):
                    pass

            dedupe_key = (title.casefold(), match.group(1))
            if dedupe_key not in seen:
                seen.add(dedupe_key)
                results.append({
                    "title": title,
                    "done": True,
                    "completed_date": match.group(1),
                    "timestamp": timestamp,
                    "section": context.get("section"),
                    "area": context.get("area"),
                    "priority": None,
                    "due": context.get("due"),
                    "recur": context.get("recur"),
                })

            i += 1

    return results
