#!/usr/bin/env python3
"""Calendar evidence adapter for the U1 standup window."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cos_config
import error_envelope
import harvest_window

COMPONENT = "standup_harvest:calendar"
_DENIED_EVENT_TYPES = {"birthday", "focusTime", "outOfOffice", "workingLocation"}
_SUBPROCESS_FAILURES = (
    subprocess.TimeoutExpired,
    subprocess.CalledProcessError,
    json.JSONDecodeError,
    FileNotFoundError,
    OSError,
)


def _calendar_configs() -> list[dict[str, Any]]:
    raw = json.loads(os.getenv("STANDUP_CALENDARS", "{}"))
    if isinstance(raw, dict):
        return [dict(value, key=key) for key, value in raw.items() if isinstance(value, dict)]
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    return []


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=cos_config.local_tz())
    return dt.astimezone(cos_config.local_tz())


def _event_start(event: dict[str, Any]) -> datetime | None:
    start = event.get("start") if isinstance(event.get("start"), dict) else {}
    return _parse_dt(start.get("dateTime"))


def _is_all_day(event: dict[str, Any]) -> bool:
    start = event.get("start") if isinstance(event.get("start"), dict) else {}
    return bool(start.get("date")) and not start.get("dateTime")


def _self_response_status(event: dict[str, Any], account: str | None) -> str:
    for attendee in event.get("attendees") or []:
        if not isinstance(attendee, dict):
            continue
        email = str(attendee.get("email") or "").strip().lower()
        is_self = bool(attendee.get("self"))
        if not is_self and account:
            is_self = email == account.strip().lower()
        if is_self:
            return str(attendee.get("responseStatus") or attendee.get("response_status") or "").strip().lower()
    return str(event.get("selfResponseStatus") or event.get("responseStatus") or "").strip().lower()


def _is_organized_by_self(event: dict[str, Any], account: str | None) -> bool:
    for key in ("organizer", "creator"):
        value = event.get(key)
        if not isinstance(value, dict):
            continue
        if value.get("self"):
            return True
        email = str(value.get("email") or "").strip().lower()
        if account and email == account.strip().lower():
            return True
    return False


def _organizer_self_flag(event: dict[str, Any]) -> bool:
    for key in ("organizer", "creator"):
        value = event.get(key)
        if isinstance(value, dict) and value.get("self") is True:
            return True
    return False


def _event_allowed(event: dict[str, Any], *, account: str | None) -> tuple[bool, str]:
    status = str(event.get("status") or "").strip().lower()
    if status == "cancelled":
        return False, "cancelled"
    if _is_all_day(event):
        return False, "all_day"
    response = _self_response_status(event, account)
    organized = _is_organized_by_self(event, account)
    if response == "declined":
        return False, "declined"
    if response == "accepted" or organized:
        return True, response or "organized"
    return False, response or "missing_response"


def _provider_id(event: dict[str, Any]) -> str:
    event_id = str(event.get("id") or "").strip()
    if event_id:
        return event_id
    recurring_id = str(event.get("recurringEventId") or "").strip()
    original = event.get("originalStartTime") if isinstance(event.get("originalStartTime"), dict) else {}
    original_start = original.get("dateTime") or original.get("date") or ""
    if recurring_id and original_start:
        return f"{recurring_id}:{original_start}"
    ical_uid = str(event.get("iCalUID") or "").strip()
    start = event.get("start") if isinstance(event.get("start"), dict) else {}
    start_at = str(start.get("dateTime") or start.get("date") or "").strip()
    if ical_uid and start_at:
        return f"{ical_uid}:{start_at}"
    return ical_uid


def _provider_state(event: dict[str, Any], *, account: str | None) -> str:
    status = str(event.get("status") or "confirmed").strip().lower()
    response = _self_response_status(event, account)
    if not response and _is_organized_by_self(event, account):
        response = "organized"
    if not response:
        response = "unknown"
    updated = str(event.get("updated") or event.get("etag") or "").strip()
    return f"status={status};response={response};updated={updated or 'unknown'}"


def _events_from_payload(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        events = payload.get("events") or payload.get("items") or []
    else:
        events = payload
    return [event for event in events if isinstance(event, dict)] if isinstance(events, list) else []


def _run_calendar(config: dict[str, Any], resolved: harvest_window.HarvestWindow) -> list[dict[str, Any]]:
    cmd = str(config.get("cmd") or "gog")
    calendar_id = config.get("calendar_id") or config.get("calendar") or config.get("id")
    account = config.get("account")
    if not calendar_id or not account:
        return []

    args = [
        cmd,
        "calendar",
        "list",
        str(calendar_id),
        "--account",
        str(account),
        "--from",
        resolved.evidence_start.isoformat(),
        "--to",
        resolved.evidence_end.isoformat(),
        "--json",
        "--max",
        str(config.get("max") or 100),
        "--all-pages",
    ]
    token_env = config.get("access_token_env") or config.get("token_env")
    if token_env:
        token = os.getenv(str(token_env))
        if token:
            args.extend(["--access-token", token])

    result = subprocess.run(args, capture_output=True, text=True, timeout=10)
    if result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, cmd, output=result.stdout, stderr=result.stderr)
    return _events_from_payload(json.loads(result.stdout))


def _record_from_event(
    event: dict[str, Any],
    *,
    account: str | None,
    now: datetime,
) -> dict[str, Any] | None:
    if event.get("eventType") in _DENIED_EVENT_TYPES:
        return None
    start = _event_start(event)
    if start is None:
        return None
    allowed, _reason = _event_allowed(event, account=account)
    if not allowed:
        return None
    provider_id = _provider_id(event)
    title = str(event.get("summary") or "Untitled").strip()
    if not provider_id or not title:
        return None
    return {
        "source": "calendar",
        "kind": "commitment" if start > now else "activity",
        "provider_id": provider_id,
        "provider_state": _provider_state(event, account=account),
        "occurred_at": start.isoformat(),
        "match_title": title,
        "title": title,
        "organizer_self": _organizer_self_flag(event),
        "url": event.get("htmlLink"),
    }


def harvest(
    *,
    resolved: harvest_window.HarvestWindow,
    trigger: str,
    query_start: datetime | None = None,
    query_end: datetime | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """Return calendar evidence candidates for ``resolved``.

    ``query_start``/``query_end`` are accepted for the orchestrator interface; the
    calendar source intentionally queries the exact U1 evidence window.
    """
    del query_start, query_end
    try:
        configs = _calendar_configs()
    except json.JSONDecodeError:
        return [], False
    if not configs:
        return [], False
    if error_envelope.breaker_open(COMPONENT):
        return [], True

    records: list[dict[str, Any]] = []
    failed = 0
    now = cos_config.local_now()
    for config in configs:
        try:
            for event in _run_calendar(config, resolved):
                record = _record_from_event(event, account=str(config.get("account") or ""), now=now)
                if record:
                    records.append(record)
        except _SUBPROCESS_FAILURES as exc:
            failed += 1
            error_envelope.log_degraded(COMPONENT, exc, trigger=trigger, check="calendar")
    # ANY per-calendar failure -> failed=True, so the orchestrator records a degraded
    # calendar source AND does not advance the calendar watermark past events the failed
    # calendar would have returned (the U2 watermark-safety invariant).
    return records, failed > 0
