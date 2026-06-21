#!/usr/bin/env python3
"""U6 calendar-write seam: agent-owned focus blocks, freebusy-gated.

This is the ONLY path that writes the agent-owned "Task Focus" calendar, and it
owns the NEVER-OVERBOOK-EXTERNAL invariant:

* **Freebusy before every write.** ``create_focus_block`` / ``move_focus_block``
  call ``get_freebusy`` across the known calendars first. Any overlap with a busy
  slot raises ``OverbookError`` -- no ``gog calendar create`` is made. An UNKNOWN
  freebusy state (subprocess timeout / tool missing / bad JSON) is treated as
  BUSY (conservative refuse), never as "free" (spec T7).
* **agent_created guard on every mutate.** ``delete_focus_block`` /
  ``move_focus_block`` first read the event's
  ``extendedProperties.private.agent_created``; if it is absent or not
  ``"task-tracker"`` the op raises ``ExternalEventError`` and NOTHING is touched.
  The agent never deletes or moves a human's event.
* **Slip is an UPDATE, never delete+create.** ``move_focus_block`` issues one
  ``gog calendar update`` so the slid block keeps its id and is reversible
  (move back); a delete+create would lose the id irreversibly (mustFix #3).
* **Delete is rung-4 (irreversible).** ``delete_focus_block`` refuses to execute
  without an explicit ``approved=True`` from the caller; even then it enforces
  the agent_created guard.
* **NO RAW ERROR LEAK.** Every ``gog`` subprocess call is wrapped; a failure logs
  to the structured error file and surfaces a typed result, never a traceback.
* **Degrade silently when absent.** ``gog`` and the focus calendar are not in the
  container today (Decision #4/#5). A missing tool / unset ``STANDUP_CALENDARS``
  is not an error here -- freebusy returns "unknown" (treated as busy -> refuse),
  so a block is simply not placed rather than placed unsafely.

Pure-policy helpers (``intervals_overlap``, ``slot_is_free``) take parsed data so
they are unit-testable without a live ``gog``; the subprocess boundary is a thin
injectable seam (``runner``) so tests assert "created vs refused" deterministically.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime
from typing import Any, Callable

import error_envelope

GOG_TIMEOUT_SECONDS = 10
AGENT_CREATED_PROP = "agent_created"
AGENT_CREATED_VALUE = "task-tracker"

# A subprocess boundary that returns a ``CompletedProcess``-like object. Injectable
# so tests run without a live gog binary; production passes the real subprocess.
Runner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


class OverbookError(Exception):
    """A focus-block write was refused because the slot is busy (or freebusy is
    unknown, treated as busy). NEVER-OVERBOOK-EXTERNAL: the write never happened."""

    def __init__(self, message: str, *, reason: str):
        super().__init__(message)
        self.reason = reason


class ExternalEventError(Exception):
    """A mutate (delete/move) was refused because the target event is NOT
    agent-created. The agent may only touch its own Task Focus events."""

    def __init__(self, message: str, *, event_id: str):
        super().__init__(message)
        self.event_id = event_id


def _default_runner(cmd: list[str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(cmd, capture_output=True, text=True, timeout=GOG_TIMEOUT_SECONDS)


def _gog_json(cmd: list[str], *, runner: Runner | None, trigger: str) -> dict[str, Any] | None:
    """Run a ``gog`` command and parse its JSON, or return None on ANY failure.

    None means "unknown" -- the caller decides the safe interpretation (for
    freebusy, unknown == busy == refuse). Every failure mode (tool missing,
    timeout, non-zero exit, bad JSON) is caught and logged via the U1 structured
    error path; a raw traceback never escapes (NO RAW ERROR LEAK).
    """
    run = runner or _default_runner
    try:
        result = run(cmd)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        error_envelope.log_degraded("calendar_blocks", exc, trigger=trigger, check="gog")
        return None
    if result.returncode != 0:
        error_envelope.log_degraded(
            "calendar_blocks",
            subprocess.CalledProcessError(result.returncode, cmd, output=result.stdout, stderr=result.stderr),
            trigger=trigger,
            check="gog",
        )
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        error_envelope.log_degraded("calendar_blocks", exc, trigger=trigger, check="gog")
        return None


# --- Pure interval policy (unit-testable without gog) ----------------------

def _parse_dt(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def intervals_overlap(a_start: str, a_end: str, b_start: str, b_end: str) -> bool:
    """True if [a_start, a_end) overlaps [b_start, b_end).

    Adjacency (a_end == b_start) is NOT an overlap. A garbage/unparseable bound is
    treated as overlapping (conservative: an interval we cannot reason about must
    not be assumed free).
    """
    a0, a1, b0, b1 = (_parse_dt(a_start), _parse_dt(a_end), _parse_dt(b_start), _parse_dt(b_end))
    if None in (a0, a1, b0, b1):
        return True
    return a0 < b1 and b0 < a1


def slot_is_free(busy: list[dict[str, str]], start: str, end: str) -> bool:
    """True iff [start, end) overlaps NO busy interval."""
    return not any(
        intervals_overlap(start, end, b.get("start", ""), b.get("end", ""))
        for b in busy
    )


# --- gog wrappers (the subprocess boundary) --------------------------------

def get_freebusy(
    calendar_ids: list[str],
    time_min: str,
    time_max: str,
    *,
    runner: Runner | None = None,
    trigger: str = "calendar_blocks",
) -> dict[str, Any]:
    """Query freebusy across ``calendar_ids`` for [time_min, time_max).

    Returns ``{"ok": True, "busy": [{"start","end"}, ...]}`` on a successful query,
    or ``{"ok": False, "reason": "unknown"}`` when the query could not be made
    (tool missing / timeout / non-zero / bad JSON) OR no calendars are configured.
    A False ``ok`` is the signal the caller MUST treat as busy (refuse the write):
    an unknown freebusy state is never assumed free (T7).
    """
    if not calendar_ids:
        return {"ok": False, "reason": "unknown"}
    cmd = ["gog", "calendar", "freebusy", "--from", time_min, "--to", time_max, "--json"]
    for cal_id in calendar_ids:
        cmd += ["--calendar", cal_id]
    data = _gog_json(cmd, runner=runner, trigger=trigger)
    # A None (tool failed) OR a response with no ``calendars`` key is UNKNOWN: an
    # empty ``{}`` from gog is more plausibly a malformed/empty response than a
    # true "every calendar is free", so it is treated as busy (refuse), never free.
    if data is None or "calendars" not in data:
        return {"ok": False, "reason": "unknown"}
    calendars = data.get("calendars", {})
    busy: list[dict[str, str]] = []
    for cal_id in calendar_ids:
        cal = calendars.get(cal_id)
        # A requested calendar that is ABSENT from the response, or whose entry
        # carries an ``errors`` field (inaccessible / not found / rate-limited), has
        # an UNKNOWN free/busy state -- never assume it is free, or the agent could
        # overbook a calendar it cannot read. Treat the whole check as unknown ->
        # busy -> refuse (NEVER-OVERBOOK-EXTERNAL).
        if cal is None or cal.get("errors"):
            return {"ok": False, "reason": "unknown"}
        for slot in cal.get("busy", []):
            start, end = slot.get("start"), slot.get("end")
            if start and end:
                busy.append({"start": start, "end": end})
    return {"ok": True, "busy": busy}


def _get_event(calendar_id: str, event_id: str, *, runner: Runner | None, trigger: str) -> dict[str, Any] | None:
    """Read one event (``gog calendar event``) to inspect its extended properties."""
    cmd = ["gog", "calendar", "event", calendar_id, event_id, "--json"]
    return _gog_json(cmd, runner=runner, trigger=trigger)


def _is_agent_created(event: dict[str, Any]) -> bool:
    private = (event.get("extendedProperties") or {}).get("private") or {}
    return private.get(AGENT_CREATED_PROP) == AGENT_CREATED_VALUE


def _assert_free_or_refuse(
    calendar_ids: list[str], start: str, end: str, task_id: str, op: str,
    *, runner: Runner | None, trigger: str,
) -> list[dict[str, str]]:
    """Freebusy-gate a write: return the busy list if free, else raise OverbookError.

    The single home for the freebusy guard both create and move share -- so the
    "unknown == busy == refuse" rule cannot drift between them.
    """
    fb = get_freebusy(calendar_ids, start, end, runner=runner, trigger=trigger)
    if not fb["ok"]:
        raise OverbookError(
            f"Cannot {op} focus block for {task_id}: freebusy check unavailable "
            f"(treated as busy). No block written.",
            reason="freebusy_unknown",
        )
    if not slot_is_free(fb["busy"], start, end):
        raise OverbookError(
            f"Cannot {op} focus block for {task_id} at {start}: slot is busy "
            "(external meeting). No block written.",
            reason="freebusy_overlap",
        )
    return fb["busy"]


def create_focus_block(
    calendar_id: str,
    task_id: str,
    task_title: str,
    start: str,
    end: str,
    *,
    freebusy_calendar_ids: list[str] | None = None,
    runner: Runner | None = None,
    trigger: str = "calendar_blocks",
) -> dict[str, Any]:
    """Create an agent-owned focus block, freebusy-gated.

    Runs freebusy across ``freebusy_calendar_ids`` (the focus calendar plus any
    configured external calendars) FIRST; raises ``OverbookError`` on overlap or
    unknown freebusy -- in which case ``gog calendar create`` is NEVER called.
    On success the event carries the ``agent_created=task-tracker`` private
    property so it is recognisable (and the only thing this unit may later
    move/delete). Returns ``{"event_id", "start", "end", "request"}``.
    """
    # ``None`` means "not supplied" -> default to the EXTERNAL (human) calendars,
    # never the agent's own focus calendar (NEVER-OVERBOOK-EXTERNAL is about human
    # meetings; including the focus calendar would self-overlap a move). An explicit
    # ``[]`` is honoured as "no calendars to check" -> unknown -> refuse.
    fb_calendars = external_calendar_ids() if freebusy_calendar_ids is None else freebusy_calendar_ids
    _assert_free_or_refuse(fb_calendars, start, end, task_id, "place", runner=runner, trigger=trigger)
    cmd = [
        "gog", "calendar", "create", calendar_id,
        "--summary", task_title,
        "--from", start, "--to", end,
        "--event-type", "focus-time",
        "--private-prop", f"{AGENT_CREATED_PROP}={AGENT_CREATED_VALUE}",
        "--private-prop", f"task_id={task_id}",
        "--no-input", "--json",
    ]
    data = _gog_json(cmd, runner=runner, trigger=trigger)
    if data is None:
        raise OverbookError(
            f"Focus-block create for {task_id} failed at the calendar tool. No block written.",
            reason="create_failed",
        )
    event_id = data.get("id") or data.get("event_id")
    if not event_id:
        # A create that returns no id leaves a block we can never slide or delete --
        # reversibility is broken. Treat it as a failure rather than storing a
        # null-id block.
        raise OverbookError(
            f"Focus-block create for {task_id} returned no event id; refusing to "
            "store an un-reversible block.",
            reason="no_event_id",
        )
    return {
        "event_id": event_id,
        "start": start,
        "end": end,
        "request": {"calendar_id": calendar_id, "task_id": task_id, "start": start, "end": end},
    }


def move_focus_block(
    calendar_id: str,
    event_id: str,
    task_id: str,
    new_start: str,
    new_end: str,
    *,
    freebusy_calendar_ids: list[str] | None = None,
    runner: Runner | None = None,
    trigger: str = "calendar_blocks",
) -> dict[str, Any]:
    """Slide an agent-owned block to a new free window via ``gog calendar update``.

    This is the slip-recovery path. It is an UPDATE -- the block KEEPS its
    ``event_id`` (reversible; a delete+create would lose it irreversibly,
    mustFix #3). It enforces the agent_created guard FIRST (a non-agent event
    raises ``ExternalEventError``), then freebusy-gates the new window
    (``OverbookError`` on overlap/unknown -- the block is NOT moved), then issues
    the single update. Returns ``{"event_id", "start", "end", "request"}``.
    """
    event = _get_event(calendar_id, event_id, runner=runner, trigger=trigger)
    if event is None:
        raise ExternalEventError(
            f"Cannot move {event_id}: event could not be read to verify it is "
            "agent-created. Refusing to touch it.",
            event_id=event_id,
        )
    if not _is_agent_created(event):
        raise ExternalEventError(
            f"Cannot move {event_id}: not an agent-created event "
            f"(no {AGENT_CREATED_PROP}={AGENT_CREATED_VALUE}). Refusing.",
            event_id=event_id,
        )
    fb_calendars = external_calendar_ids() if freebusy_calendar_ids is None else freebusy_calendar_ids
    _assert_free_or_refuse(fb_calendars, new_start, new_end, task_id, "move", runner=runner, trigger=trigger)
    cmd = [
        "gog", "calendar", "update", calendar_id, event_id,
        "--from", new_start, "--to", new_end,
        "--no-input", "--json",
    ]
    data = _gog_json(cmd, runner=runner, trigger=trigger)
    if data is None:
        raise OverbookError(
            f"Focus-block move for {task_id} failed at the calendar tool. Block not moved.",
            reason="update_failed",
        )
    return {
        "event_id": event_id,
        "start": new_start,
        "end": new_end,
        "request": {"calendar_id": calendar_id, "event_id": event_id, "start": new_start, "end": new_end},
    }


def delete_focus_block(
    calendar_id: str,
    event_id: str,
    *,
    approved: bool = False,
    runner: Runner | None = None,
    trigger: str = "calendar_blocks",
) -> dict[str, Any]:
    """Delete an agent-owned block -- IRREVERSIBLE, requires explicit approval.

    Two gates, both mandatory:

    * ``approved`` MUST be True. A delete is rung-4 (irreversible in the Google
      Calendar API). Without approval this returns ``{"ok": False,
      "reason": "needs_approval"}`` and NO ``gog`` call is made.
    * the target event MUST be agent-created. A non-agent event raises
      ``ExternalEventError`` -- the agent never deletes a human's meeting, even
      when approved.

    Returns ``{"ok": True, "event_id"}`` on a successful delete.
    """
    if not approved:
        return {
            "ok": False,
            "reason": "needs_approval",
            "message": f"Deleting {event_id} is irreversible and needs explicit approval.",
        }
    event = _get_event(calendar_id, event_id, runner=runner, trigger=trigger)
    if event is None or not _is_agent_created(event):
        raise ExternalEventError(
            f"Cannot delete {event_id}: not a verified agent-created event. Refusing.",
            event_id=event_id,
        )
    cmd = ["gog", "calendar", "delete", calendar_id, event_id, "--no-input", "--json"]
    data = _gog_json(cmd, runner=runner, trigger=trigger)
    if data is None:
        return {"ok": False, "reason": "delete_failed",
                "message": f"Delete of {event_id} failed at the calendar tool."}
    return {"ok": True, "event_id": event_id}


def external_calendar_ids() -> list[str]:
    """The HUMAN/external calendar ids to freebusy-check before a write, env-sourced.

    NEVER-OVERBOOK-EXTERNAL is about never overlapping a *human* meeting, so the
    freebusy gate checks the EXTERNAL calendars (those configured in
    ``STANDUP_CALENDARS``) and NOT the agent-owned focus calendar
    (``TASK_TRACKER_FOCUS_CALENDAR_ID``). Including the focus calendar would make a
    MOVE self-overlap: the block being slid still occupies its old slot on the
    focus calendar, which FreeBusy cannot exclude per-event, so the move would
    always be refused. The agent owns and serialises its own focus-block writes, so
    the focus calendar does not need a freebusy guard against itself.

    All external calendars are absent in the container today (Decision #4/#5); an
    empty list makes ``get_freebusy`` return "unknown" -> treated as busy -> the
    agent simply does not place/move a block (degrade silently, never overbook).
    """
    ids: list[str] = []
    raw = os.getenv("STANDUP_CALENDARS")
    if raw and raw.strip():
        try:
            for cfg in json.loads(raw).values():
                cal_id = cfg.get("calendar_id") if isinstance(cfg, dict) else None
                if cal_id:
                    ids.append(str(cal_id))
        except (json.JSONDecodeError, AttributeError):
            pass
    # The agent's own focus calendar is explicitly EXCLUDED (see docstring); de-dup
    # while preserving order in case a calendar appears twice in STANDUP_CALENDARS.
    focus_id = (os.getenv("TASK_TRACKER_FOCUS_CALENDAR_ID") or "").strip()
    seen: set[str] = set()
    return [cid for cid in ids if cid != focus_id and not (cid in seen or seen.add(cid))]
