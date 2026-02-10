#!/usr/bin/env python3
"""
Helpers for extracting completed actions from daily notes.
"""

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
