#!/usr/bin/env python3
"""
Shared helpers for standup scripts.
"""

import json
import os
import subprocess
import sys
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cos_config
import error_envelope


def get_calendar_events(trigger: str = "calendar_fetch") -> dict:
    """Fetch today's calendar events via gog CLI.

    ``trigger`` attributes a fetch failure to its caller in the structured error
    log (U1 §3a) -- standup, personal_standup, weekly_review, or a heartbeat --
    so the log is not misattributed to a single ritual. Defaults to a neutral
    ``"calendar_fetch"`` rather than baking in one caller's name.

    Returns one of:
    - ``{}``                              -- not configured (STANDUP_CALENDARS
      unset/invalid). Renders as "not configured", never an error.
    - ``{"<key>": [events...]}``          -- success.
    - ``{"_error": "<reason>", ...}``     -- a real fetch failure (subprocess
      timeout / tool missing / bad JSON). The U1 sentinel: the caller renders a
      degraded notice instead of silently dropping the section, and the failure
      is logged. NEVER a bare ``pass``/swallow (U1 §2c, fixes silent-failure G9).
    """
    config_str = os.getenv("STANDUP_CALENDARS")
    if not config_str:
        return {}

    try:
        calendars_config = json.loads(config_str)
    except json.JSONDecodeError:
        return {}

    # If a missing/broken calendar tool has failed repeatedly, the circuit
    # breaker is open: skip the subprocess entirely and render degraded, so a
    # missing `gog` does not loop (re-invoke -> fail) on every standup. This is
    # the breaker's primary motivating scenario and must be checked HERE, since
    # calendar failures log under the "calendar_fetch" component, not the
    # wrapping ritual's name.
    if error_envelope.breaker_open("calendar_fetch"):
        return {"_error": "calendar_unavailable", "reason": "breaker_open"}

    events: dict = {}
    attempted = 0
    failed = 0

    for key, config in calendars_config.items():
        events[key] = []
        cmd = config.get("cmd", "gog")
        calendar_id = config.get("calendar_id")
        account = config.get("account")
        label = config.get("label")

        if not calendar_id or not account:
            continue

        attempted += 1
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
            if result.returncode != 0:
                # A non-zero gog exit (auth expired, calendar not found, quota)
                # is a real failure, NOT an empty calendar -- raise so it flows
                # through the same no-swallow logging path as an exception
                # (U1 §2c: never silently render an empty section on error).
                raise subprocess.CalledProcessError(
                    result.returncode, cmd, output=result.stdout, stderr=result.stderr
                )
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
        ) as exc:
            # U1: do NOT swallow. Log the real error and KEEP GOING so a single
            # failing calendar does not blank the calendars that succeeded.
            failed += 1
            error_envelope.log_degraded(
                "calendar_fetch",
                exc,
                trigger=trigger,
                check="calendar_fetch",
            )

    # Only surface the degraded sentinel when EVERY attempted calendar failed --
    # otherwise the section renders the calendars that succeeded (U1 §2c: the
    # section never silently vanishes, but a multi-calendar setup is not
    # over-degraded by one transient failure).
    if attempted and failed == attempted:
        return {"_error": "calendar_unavailable", "reason": "all_calendars_failed"}

    return events


def calendar_error(calendar_events: dict | None) -> str | None:
    """Return the degraded-notice reason if the calendar result is a sentinel.

    Both standup callers guard on this: a truthy return means render a one-line
    degraded notice and DO NOT treat the dict as event data.
    """
    if isinstance(calendar_events, dict) and "_error" in calendar_events:
        return str(calendar_events.get("_error") or "calendar_unavailable")
    return None


def format_time(iso_time: str) -> str:
    """Format ISO datetime to human-readable time."""
    try:
        dt = datetime.fromisoformat(iso_time.replace("Z", "+00:00"))
        return dt.strftime("%I:%M %p").lstrip("0")
    except (ValueError, TypeError):
        return iso_time


def resolve_standup_date(date_str: str | None) -> date:
    """Parse standup date from YYYY-MM-DD, defaulting to today.

    "Today" is the LOCAL (Pacific) calendar day: this date feeds the standup's
    overdue/effective-priority regrouping, so a UTC day (rolled over by the
    17:00/17:30 Pacific cron) would mis-classify due-today tasks as overdue.
    """
    today = cos_config.local_today()
    if not date_str:
        return today

    try:
        return datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return today


def flatten_calendar_events(calendar_events: dict) -> list[dict]:
    """Flatten calendar dict into a single sorted event list.

    A ``{"_error": ...}`` sentinel flattens to an empty list -- callers detect
    the degraded state via ``calendar_error()``, not by inspecting this output.
    """
    if not calendar_events or "_error" in calendar_events:
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
            task_id = task.get("task_id") or task.get("legacy_id")
            if task_id:
                missed_lines.append(f'    • {title} — say "done {task_id}" to mark complete')
            else:
                missed_lines.append(f"    • {title} — missing task_id::; repair identity before completion")

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
