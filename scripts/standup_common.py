#!/usr/bin/env python3
"""
Shared helpers for standup scripts.
"""

import json
import os
import subprocess
from datetime import date, datetime


def get_calendar_events() -> dict:
    """Fetch today's calendar events via gog CLI."""
    config_str = os.getenv("STANDUP_CALENDARS")
    if not config_str:
        return {}

    try:
        calendars_config = json.loads(config_str)
    except json.JSONDecodeError:
        return {}

    events = {}

    for key, config in calendars_config.items():
        events[key] = []
        cmd = config.get("cmd", "gog")
        calendar_id = config.get("calendar_id")
        account = config.get("account")
        label = config.get("label")

        if not calendar_id or not account:
            continue

        try:
            result = subprocess.run(
                [
                    cmd,
                    "calendar",
                    "list",
                    calendar_id,
                    "--account",
                    account,
                    "--today",
                    "--json",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                data = json.loads(result.stdout)
                for event in data.get("events", []):
                    if event.get("eventType") == "birthday":
                        continue
                    if "dateTime" not in event.get("start", {}):
                        continue

                    summary = event.get("summary", "Untitled")
                    if label:
                        summary = f"{summary} ({label})"

                    events[key].append(
                        {
                            "summary": summary,
                            "start": event["start"].get("dateTime"),
                            "end": event["end"].get("dateTime"),
                        }
                    )
        except (
            subprocess.TimeoutExpired,
            subprocess.CalledProcessError,
            json.JSONDecodeError,
            FileNotFoundError,
        ):
            pass

    return events


def format_time(iso_time: str) -> str:
    """Format ISO datetime to human-readable time."""
    try:
        dt = datetime.fromisoformat(iso_time.replace("Z", "+00:00"))
        return dt.strftime("%I:%M %p").lstrip("0")
    except Exception:
        return iso_time


def resolve_standup_date(date_str: str | None) -> date:
    """Parse standup date from YYYY-MM-DD, defaulting to today."""
    today = datetime.now().date()
    if not date_str:
        return today

    try:
        return datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return today


def flatten_calendar_events(calendar_events: dict) -> list[dict]:
    """Flatten calendar dict into a single sorted event list."""
    if not calendar_events:
        return []
    all_events = []
    for key in sorted(calendar_events.keys()):
        all_events.extend(calendar_events[key])
    return all_events


def format_missed_tasks_block(missed_buckets: dict | None) -> str:
    """Build markdown block for missed tasks buckets."""
    if not missed_buckets:
        return ""

    has_missed = any(
        missed_buckets.get(key) for key in ["yesterday", "last7", "last30", "older"]
    )
    if not has_missed:
        return ""

    missed_lines = ["🔴 **Missed Tasks:**"]

    if missed_buckets.get("yesterday"):
        missed_lines.append("\n  **Yesterday:**")
        for task in missed_buckets["yesterday"]:
            title = task.get("title", "")
            missed_lines.append(
                f'    • {title} — say "done {title}" to mark complete'
            )

    if missed_buckets.get("last7"):
        missed_lines.append("\n  **Last 7 Days:**")
        for task in missed_buckets["last7"]:
            title = task.get("title", "")
            missed_lines.append(f"    • {title}")

    if missed_buckets.get("last30"):
        missed_lines.append("\n  **Last 30 Days:**")
        for task in missed_buckets["last30"]:
            title = task.get("title", "")
            missed_lines.append(f"    • {title}")

    if missed_buckets.get("older"):
        missed_lines.append("\n  **Older than 30 Days:**")
        for task in missed_buckets["older"]:
            title = task.get("title", "")
            missed_lines.append(f"    • {title}")

    missed_lines.append("")
    return "\n".join(missed_lines)
