#!/usr/bin/env python3
"""Sole reader/writer of ``proactive-state.json`` (U6 brief/debrief idempotency).

The ``*/5`` pre-brief scan fires every five minutes during work hours. Without a
single source of truth for "did I already brief this event?", a torn read would
let two consecutive fires both send the same pre-brief -- the duplicate-brief bug
the spec calls out (§3.2, mustFix #7). This module is that source of truth:

* **Atomic + torn-read safe.** Reads tolerate a corrupt/torn/unreadable file by
  re-initialising from an empty state (quarantining the bad file aside, never
  erasing it) so the NEXT cron fire always runs. Writes go through
  ``utils._atomic_write``.
* **Date-scoped.** State is for ONE day; on a new date the daily-brief and
  Friday-proposal flags reset and the pre-brief list starts empty, so yesterday's
  "already briefed" never suppresses today.
* **Single writer.** Only this module writes ``proactive-state.json``.

The idempotency check itself (``daily_brief_due`` / ``pre_brief_due`` /
``friday_proposal_due``) lives here so every caller asks the same question; the
brief CONTENT lives in proactive_brief.py.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import cos_config
from utils import _atomic_write

SCHEMA_VERSION = 1


def proactive_state_path() -> Path:
    return cos_config.state_dir() / "proactive-state.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def today_str(reference_date: str | None = None) -> str:
    return reference_date or date.today().isoformat()


def _rename_corrupt_aside(path: Path) -> Path | None:
    """Move a corrupt state file aside as ``<name>.corrupt-<n>``; never erase it."""
    for n in range(1, 1000):
        candidate = path.with_name(f"{path.name}.corrupt-{n}")
        if not candidate.exists():
            try:
                os.replace(path, candidate)
                return candidate
            except OSError:
                return None
    return None


def _empty_state(reference_date: str | None = None) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "date": today_str(reference_date),
        "daily_brief_sent": False,
        "daily_brief_sent_at": None,
        "pre_briefs": [],
        "friday_proposal_sent": False,
        "friday_proposal_sent_at": None,
        "updated_at": _now_iso(),
    }


def load_proactive_state(reference_date: str | None = None) -> dict[str, Any]:
    """Return today's proactive state, re-initialising on a stale date or corruption.

    A missing/corrupt/unreadable file yields a fresh empty state for today; a
    corrupt file is quarantined aside (forensics preserved). A state file whose
    ``date`` is not today is also reset to a fresh empty state -- the previous
    day's "already sent" flags must never suppress today's briefs. A torn read
    therefore never raises and never carries stale idempotency forward.
    """
    ref = today_str(reference_date)
    path = proactive_state_path()
    if not path.exists():
        return _empty_state(ref)
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        _rename_corrupt_aside(path)
        return _empty_state(ref)
    except OSError:
        return _empty_state(ref)
    if not isinstance(loaded, dict) or loaded.get("date") != ref:
        if not isinstance(loaded, dict):
            _rename_corrupt_aside(path)
        return _empty_state(ref)
    loaded.setdefault("pre_briefs", [])
    return loaded


def save_proactive_state(state: dict[str, Any]) -> dict[str, Any]:
    """Atomically persist ``state`` after stamping ``updated_at``."""
    state["schema_version"] = SCHEMA_VERSION
    state["updated_at"] = _now_iso()
    _atomic_write(proactive_state_path(), json.dumps(state, indent=2, sort_keys=True) + "\n")
    return state


def find_pre_brief(state: dict[str, Any], event_id: str) -> dict[str, Any] | None:
    """Return the pre-brief entry for ``event_id``, or None.

    Public so a caller can inspect a loop's open/closed state (e.g. the reactive
    debrief-capture idempotency guard) without re-implementing the lookup.
    """
    for entry in state.get("pre_briefs", []):
        if entry.get("event_id") == event_id:
            return entry
    return None


def daily_brief_due(state: dict[str, Any]) -> bool:
    """True if today's daily brief has NOT been sent yet (idempotency gate)."""
    return not state.get("daily_brief_sent", False)


def mark_daily_brief_sent(state: dict[str, Any]) -> None:
    state["daily_brief_sent"] = True
    state["daily_brief_sent_at"] = _now_iso()


def friday_proposal_due(state: dict[str, Any]) -> bool:
    """True if this Friday's proposal has NOT been sent yet (idempotency gate)."""
    return not state.get("friday_proposal_sent", False)


def mark_friday_proposal_sent(state: dict[str, Any]) -> None:
    state["friday_proposal_sent"] = True
    state["friday_proposal_sent_at"] = _now_iso()


def pre_brief_due(state: dict[str, Any], event_id: str) -> bool:
    """True if no pre-brief has been sent for ``event_id`` today.

    The mandatory ``*/5`` idempotency check (spec §4.6): a fire reads this before
    sending so a second fire within the lead window does not double-brief.
    """
    entry = find_pre_brief(state, event_id)
    return not (entry and entry.get("brief_sent"))


def mark_pre_brief_sent(state: dict[str, Any], event_id: str, event_summary: str,
                        event_start: str, event_end: str = "") -> dict[str, Any]:
    """Record that a pre-brief was sent for ``event_id``; returns the entry.

    ``event_end`` is stored so the debrief loop can wait for the event to END
    before nudging (a mid-meeting "capture commitments" prompt makes no sense).
    """
    entry = find_pre_brief(state, event_id)
    if entry is None:
        entry = {
            "event_id": event_id,
            "event_summary": event_summary,
            "event_start": event_start,
            "event_end": event_end,
            "brief_sent": False,
            "brief_sent_at": None,
            "debrief_requested": False,
            "debrief_requested_at": None,
            "debrief_captured_at": None,
            "debrief_skipped_at": None,
            "commitments_task_ids": [],
        }
        state.setdefault("pre_briefs", []).append(entry)
    entry["brief_sent"] = True
    entry["brief_sent_at"] = _now_iso()
    return entry


def resolve_open_debrief(state: dict[str, Any], reference: str) -> dict[str, Any] | None:
    """Find the OPEN debrief entry a user's ``/debrief <reference>`` refers to.

    The reactive ``/debrief`` command forwards what the pre-brief told the user --
    the event SUMMARY -- but the loop is stored under its ``event_id`` key (which,
    for a calendar event with no id, is ``summary@start``). So a lookup by the bare
    summary must still find the loop. Resolution order, restricted to OPEN loops so
    a closed/captured loop is never re-matched:

    1. exact ``event_id`` match (the stored key), then
    2. exact ``event_summary`` match.

    Returns the entry or None. None means "no open debrief for that reference" and
    the caller MUST refuse rather than silently create tasks against a phantom loop.
    """
    open_entries = [e for e in state.get("pre_briefs", []) if is_debrief_open(e)]
    for entry in open_entries:
        if entry.get("event_id") == reference:
            return entry
    for entry in open_entries:
        if entry.get("event_summary") == reference:
            return entry
    return None


def open_debrief(state: dict[str, Any], event_id: str) -> dict[str, Any] | None:
    """Mark a debrief as requested (open loop) for ``event_id``; returns the entry.

    Returns None when no pre-brief entry exists for the event. Re-requesting an
    already-open debrief is idempotent on the ``debrief_requested`` flag but the
    caller may still re-send the gentle follow-up: a debrief loop closes ONLY on
    capture or skip, never by time (spec §5.5).
    """
    entry = find_pre_brief(state, event_id)
    if entry is None:
        return None
    if not entry.get("debrief_requested"):
        entry["debrief_requested"] = True
        entry["debrief_requested_at"] = _now_iso()
    return entry


def is_debrief_open(entry: dict[str, Any]) -> bool:
    """A debrief loop is OPEN iff requested and neither captured nor skipped."""
    return bool(
        entry.get("debrief_requested")
        and not entry.get("debrief_captured_at")
        and not entry.get("debrief_skipped_at")
    )


def debrief_reprompt_due(entry: dict[str, Any], *, now: datetime, interval_minutes: int) -> bool:
    """True if this open debrief loop is due for a follow-up re-prompt.

    The first re-prompt (no ``debrief_last_reprompt_at`` yet) is always due; after
    that, a re-prompt fires only once ``interval_minutes`` have elapsed since the
    last one -- so the ``*/5`` scan paces nudges rather than spamming every cycle.
    A garbage timestamp degrades to "due" (better to nudge than to go silent).
    """
    last = entry.get("debrief_last_reprompt_at")
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(str(last))
    except ValueError:
        return True
    return (now - last_dt) >= timedelta(minutes=interval_minutes)


def mark_debrief_reprompted(entry: dict[str, Any], *, now: datetime) -> None:
    """Stamp the re-prompt time so the next scan respects the pacing interval.

    Uses the flow's ``now`` (the cron's notion of the current time) -- not
    wall-clock -- so the pacing is computed against the same clock
    ``debrief_reprompt_due`` reads, keeping the interval check consistent.
    """
    entry["debrief_last_reprompt_at"] = now.isoformat()


def capture_debrief(state: dict[str, Any], event_id: str, commitment_task_ids: list[str]) -> dict[str, Any] | None:
    """Close a debrief loop by capturing commitments (sets ``debrief_captured_at``)."""
    entry = find_pre_brief(state, event_id)
    if entry is None:
        return None
    entry["debrief_captured_at"] = _now_iso()
    entry["commitments_task_ids"] = list(commitment_task_ids)
    return entry


def skip_debrief(state: dict[str, Any], event_id: str) -> dict[str, Any] | None:
    """Close a debrief loop by user skip (sets ``debrief_skipped_at``)."""
    entry = find_pre_brief(state, event_id)
    if entry is None:
        return None
    entry["debrief_skipped_at"] = _now_iso()
    return entry
