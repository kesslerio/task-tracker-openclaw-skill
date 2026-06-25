#!/usr/bin/env python3
"""v0.4-C point-in-time calendar availability -- ``not_known_busy(now)``, fail CLOSED.

The U3 ``calendar_adapter`` is a WINDOW read (a day of accepted/organized events as
standup evidence). The initiation nudge needs the opposite shape: at the send instant,
"is the user in an accepted meeting RIGHT NOW?" -- a point-in-time *containment* check.

It **fails CLOSED**: any uncertainty (no calendar configured, breaker open, a ``gog``
error/timeout, unparseable output, or an accepted event we cannot time) is treated as
"busy" and SUPPRESSES the nudge. The calendar is only a weak "actually free" proxy, and
a nudge fired mid-meeting is the worst false positive -- so the safe default is silence.

The "what counts as busy" classification (timed, accepted/organized, NOT cancelled /
declined / all-day) is **reused** from ``calendar_adapter._event_allowed`` so it is
single-sourced and cannot drift between the harvest read and this one (a drift there
would be a real bug: e.g. a declined meeting silently suppressing every nudge).
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta
from typing import Any

import cos_config
import error_envelope
# Private helpers deliberately reused to SINGLE-SOURCE the busy-classification
# (accepted/organized, all-day, declined/cancelled) -- see the module docstring. Do
# not "promote to public" or re-implement; a drift here is a real nudge bug.
from adapters.calendar_adapter import (
    _calendar_configs,
    _event_allowed,
    _event_start,
    _parse_dt,
)

COMPONENT = "initiation:availability"

# How far back to look for an in-progress event that CONTAINS ``now``. A timed work
# meeting is never longer than this; querying ``[now - lookback, now + lookahead]`` and
# filtering by containment catches an event that started earlier and is still running.
_BUSY_LOOKBACK = timedelta(hours=12)
# A small margin on the QUERY upper bound for clock skew between our ``now`` and the
# calendar server; containment still uses ``start <= now < end`` so a future-starting
# event fetched by this margin is never counted as current.
_BUSY_LOOKAHEAD = timedelta(minutes=1)
_GOG_TIMEOUT_S = 10

# Event types that NEVER make the user busy for nudging, even when timed: a birthday
# or a working-location annotation is not an interruption. ``focusTime`` and
# ``outOfOffice`` are deliberately NOT here -- a focus block or an out-of-office period
# is EXACTLY when the user is unavailable, so they MUST classify as busy (via
# ``_event_allowed``). The harvest's ``_DENIED_EVENT_TYPES`` is the WRONG set to reuse
# here: it drops focusTime/OOO as "not standup evidence", the opposite of "not busy".
_NON_BLOCKING_EVENT_TYPES = {"birthday", "workingLocation"}


def _event_end(event: dict[str, Any]) -> datetime | None:
    end = event.get("end") if isinstance(event.get("end"), dict) else {}
    return _parse_dt(end.get("dateTime"))


def _query_events(
    config: dict[str, Any], start: datetime, end: datetime
) -> list[dict[str, Any]]:
    """``gog calendar list`` over ``[start, end]`` for one calendar config.

    Raises on any transport failure (non-zero exit -> ``CalledProcessError``; timeout ->
    ``TimeoutExpired``; bad JSON -> ``JSONDecodeError``) so the caller fails CLOSED.
    """
    cmd = str(config.get("cmd") or "gog")
    calendar_id = config.get("calendar_id") or config.get("calendar") or config.get("id")
    account = config.get("account")
    if not calendar_id or not account:
        # A configured-but-unqueryable calendar is UNCERTAINTY, not "no meetings": we
        # cannot rule out a meeting on it, so raise (the caller's broad except fails
        # closed). The harvest's _run_calendar returns [] here because empty evidence is
        # fine; the availability GATE must not read a missing-config as "free".
        raise ValueError("calendar config missing calendar_id/account")
    args = [
        cmd, "calendar", "list", str(calendar_id),
        "--account", str(account),
        "--from", start.isoformat(),
        "--to", end.isoformat(),
        "--json", "--max", str(config.get("max") or 100), "--all-pages",
    ]
    token_env = config.get("access_token_env") or config.get("token_env")
    if token_env:
        token = os.getenv(str(token_env))
        if token:
            args.extend(["--access-token", token])
    result = subprocess.run(args, capture_output=True, text=True, timeout=_GOG_TIMEOUT_S)
    if result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, cmd, output=result.stdout, stderr=result.stderr)
    return _strict_events(json.loads(result.stdout))


def _strict_events(payload: Any) -> list[dict[str, Any]]:
    """Extract the event list from a gog payload, RAISING on an unexpected shape.

    Unlike the harvest's lenient ``_events_from_payload`` (which coerces any odd shape
    to ``[]`` because empty harvest evidence is fine), the availability GATE must treat
    an unparseable payload as UNCERTAINTY, not "no meetings": a ``{"events": "<error>"}``
    / ``null`` / scalar / wrapper-without-events response would otherwise read as "free"
    and fire a nudge we cannot justify. So a non-list events value raises ``ValueError``
    (caught by the caller -> fail closed). A genuinely empty ``{"events": []}`` is fine.
    """
    if isinstance(payload, list):
        events = payload
    elif isinstance(payload, dict):
        events = payload.get("events")
        if events is None:
            events = payload.get("items")
    else:
        raise ValueError(f"gog payload is not a list/object: {type(payload).__name__}")
    if not isinstance(events, list):
        raise ValueError("gog payload carried no events/items list")
    return [event for event in events if isinstance(event, dict)]


def _is_busy_at(event: dict[str, Any], now: datetime, *, account: str | None) -> bool:
    """True if ``event`` is a busy-making event whose ``[start, end)`` contains ``now``.

    Busy-making = the ``_event_allowed`` set (timed, accepted/organized, not
    cancelled/declined/all-day) MINUS the non-blocking event types (birthday,
    workingLocation). A focusTime or outOfOffice block IS busy. An allowed event we
    CANNOT time (missing/garbage start or end, or a reversed ``end <= start`` interval)
    returns True -- fail closed, since we cannot prove it does not contain ``now``. A
    non-busy event (all-day, declined, non-blocking type, ...) or one whose interval
    does not contain ``now`` returns False.
    """
    if event.get("eventType") in _NON_BLOCKING_EVENT_TYPES:
        return False
    allowed, _reason = _event_allowed(event, account=account)
    if not allowed:
        return False  # all-day / declined / cancelled / no-response -> not busy
    start = _event_start(event)
    end = _event_end(event)
    if start is None or end is None or end <= start:
        return True  # an accepted event we cannot reliably time -> fail closed -> busy
    return start <= now < end


def not_known_busy(now: datetime, *, trigger: str = "initiation_availability") -> bool:
    """Is the user safe to nudge at ``now`` (NOT known to be in a meeting)?

    Returns **True** ONLY on a fresh, successful calendar read in which no accepted /
    organized timed event contains ``now`` (the nudge may proceed). Returns **False**
    -- suppress -- when an accepted event contains ``now`` (genuinely busy) OR
    availability cannot be confidently determined: no calendar configured, the breaker
    is open, a ``gog`` error/timeout, or unparseable output. **Fail CLOSED.**

    The read is ON-DEMAND (performed at ``now``), so it is inherently fresh: the 10-min
    staleness bound from the decisions doc is satisfied by construction -- the dispatcher
    re-calls this at send time and nothing caches an availability reading across minutes.
    All-day events do NOT count as busy (``_event_allowed`` excludes them).
    """
    try:
        configs = _calendar_configs()
    except (json.JSONDecodeError, ValueError):
        return False  # malformed STANDUP_CALENDARS -> cannot confirm free -> suppress
    if not configs:
        return False  # no calendar configured -> cannot confirm free -> fail closed
    if error_envelope.breaker_open(COMPONENT):
        return False

    if now.tzinfo is None:
        now = now.replace(tzinfo=cos_config.local_tz())
    now = now.astimezone(cos_config.local_tz())
    window_start = now - _BUSY_LOOKBACK
    window_end = now + _BUSY_LOOKAHEAD

    for config in configs:
        account = str(config.get("account") or "") or None
        try:
            for event in _query_events(config, window_start, window_end):
                if _is_busy_at(event, now, account=account):
                    return False  # an accepted event contains now -> busy -> suppress
        except Exception as exc:  # noqa: BLE001 -- fail CLOSED: ANY read/parse/classify
            # error means we cannot rule out a meeting -> suppress. A broad catch is the
            # correct posture for a security-relevant gate (an unexpected error must
            # never fall through to "free"); the degrade is recorded, not silent.
            error_envelope.log_degraded(COMPONENT, exc, trigger=trigger, check="availability")
            return False
    return True  # fresh read of every calendar, nothing contains now -> safe to nudge
