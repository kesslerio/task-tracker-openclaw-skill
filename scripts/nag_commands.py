#!/usr/bin/env python3
"""U4 reactive command handlers: /done /reschedule /snooze + body-double.

These run inside the user's interactive turn (origin-proven -- they reply in the
topic the command arrived in, no proactive push), so they do NOT go through the
proactive delivery seam.  Their job is to mutate the board (via the existing
``task_transitions`` write path) and then close / pause the nag loop
SYNCHRONOUSLY in the SAME turn -- the ADHD-trust requirement (Decision/Verdict
mustFix #4): if /done returned but the nag re-fired 3h later because the
background loop had not yet noticed, that is a trust kill.

Command summary:

* ``/done <id>``        -- complete the task, then close the loop (explicit_done).
* ``/reschedule <id> <date>`` -- move ``due::``, then close the loop (rescheduled).
* ``/snooze <id> <dur>`` -- pause the loop until now+dur; akrasia re-prompt + a
  hard cap of 3 snoozes (the 4th is REFUSED, loop unchanged).
* ``/body-double <id> <dur>`` -- start a focus session with two ephemeral
  (``deleteAfterRun:true``) check-in crons, each carrying an explicit proven
  ``delivery.to`` + ``agentId``.  Refuses a task not on the active board, and a
  second concurrent session for the same task.
* ``/cancel-session <id>`` -- end the active body-double session + delete its
  pending crons.

The body-double ephemeral crons use ``deleteAfterRun: true`` so the gateway reaps
them after they fire -- the agent never issues a ``cron rm`` (orphan risk if a
fire is missed).
"""

from __future__ import annotations

import argparse
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import cos_config
import nag_delivery
import nag_state
from task_ledger import append_event, new_event
from task_records import active_records, load_records
from task_transitions import complete_by_id, reschedule_by_id

# Nag durations are days/hours/minutes (the spec uses 1h / 1d / 3d / 90m). The
# shared utils.parse_duration only understands h/m (it sizes focus blocks), so we
# parse here rather than widen that helper and risk changing U3/U6 semantics.
_DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([dhm])")
_UNIT_MINUTES = {"d": 24 * 60, "h": 60, "m": 1}


def parse_duration_minutes(duration: str | None) -> int:
    """Parse a d/h/m duration into minutes (e.g. '1d'->1440, '90m'->90, '1h'->60).

    Returns 0 on empty/garbage so callers reject it with a friendly message. A
    bare number is treated as minutes (matching utils.parse_duration's fallback).
    """
    if not duration:
        return 0
    text = duration.strip().lower()
    parts = _DURATION_RE.findall(text)
    if not parts:
        try:
            return int(float(text))
        except ValueError:
            return 0
    total = 0
    for value, unit in parts:
        try:
            total += int(float(value) * _UNIT_MINUTES[unit])
        except ValueError:
            continue
    return total


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _log(event_type: str, *, task_id: str | None = None, **metadata: Any) -> None:
    append_event(
        new_event(event_type, task_id=task_id, source="user_command",
                  actor="nag_commands", metadata=metadata)
    )


def _active_record(task_id: str, *, personal: bool = False):
    """Return the active TaskRecord for ``task_id``, or None if not on the board."""
    _f, _c, records = load_records(personal=personal)
    for record in active_records(records):
        if record.canonical_id == task_id:
            return record
    return None


# --- /done + /reschedule: board op THEN synchronous close in the same turn ---

def _close_loop_after_board_op(result: dict[str, Any], task_id: str,
                               *, closed_by: str) -> dict[str, Any]:
    """Close the task's nag loop SYNCHRONOUSLY iff the preceding board op succeeded.

    The shared tail of /done and /reschedule: a successful board mutation closes
    the loop in the SAME turn (no 3h ack-lag); a FAILED board op leaves the loop
    OPEN (the task is still open -- NAG-CLOSES-ONLY-ON-ACK). Annotates ``result``
    with ``nag_closed`` and returns it unchanged on failure.
    """
    if not result.get("ok"):
        return result
    closed = nag_state.transition(
        lambda state: nag_state.close_loop(state, task_id, closed_by=closed_by)
    )
    if closed is not None:
        _log("nag_acked", task_id=task_id, nag_loop_id=closed.get("nag_loop_id"),
             closed_by=closed_by)
    result["nag_closed"] = closed is not None
    return result


def handle_done(task_id: str, *, personal: bool = False) -> dict[str, Any]:
    """Complete ``task_id`` then close its nag loop in the SAME turn (Path A)."""
    result = complete_by_id(task_id, personal=personal, source="user_command")
    return _close_loop_after_board_op(result, task_id,
                                      closed_by=nag_state.CLOSED_EXPLICIT_DONE)


def handle_reschedule(task_id: str, new_due: str, *, personal: bool = False) -> dict[str, Any]:
    """Move ``due::`` to ``new_due`` then close the loop in the SAME turn (Path C)."""
    result = reschedule_by_id(task_id, new_due, personal=personal, source="user_command")
    return _close_loop_after_board_op(result, task_id,
                                      closed_by=nag_state.CLOSED_RESCHEDULED)


# --- /snooze (akrasia asymmetry) ------------------------------------------

def handle_snooze(task_id: str, duration: str, *, block_reason: str | None = None) -> dict[str, Any]:
    """Pause the loop until now+duration, with the akrasia cap of 3 (spec §2.3).

    A snooze NEVER closes the loop (snooze != close).  The 4th snooze for the same
    loop is REFUSED: the loop is left exactly as it was, no ``nag_snoozed`` event
    is logged, and the user is told to /reschedule or /done.  The re-prompt that
    asks why it slipped (akrasia) is the ``block_reason`` captured here.
    """
    minutes = parse_duration_minutes(duration)
    if minutes <= 0:
        return {"ok": False, "error": {
            "code": "invalid-duration",
            "message": f"Snooze duration must be like 1h/1d/3d; got {duration!r}.",
        }}

    snooze_max = cos_config.nag_snooze_max()
    snoozed_until = (_now() + timedelta(minutes=minutes)).isoformat()

    # Check the cap AND apply the snooze inside ONE locked transition so two racing
    # 4th-snooze attempts cannot both slip past a stale read of snooze_count.
    def mutate(state: dict[str, Any]) -> dict[str, Any]:
        if nag_state.snooze_capped(state.get(task_id), snooze_max=snooze_max):
            return {"capped": True, "snooze_count": int((state.get(task_id) or {}).get("snooze_count") or 0)}
        entry = nag_state.apply_snooze(state, task_id, snoozed_until=snoozed_until,
                                       block_reason=block_reason)
        return {"capped": False, "entry": entry}

    outcome = nag_state.transition(mutate)
    if outcome["capped"]:
        return {"ok": False, "error": {
            "code": "snooze-cap-reached",
            "message": (f"You've snoozed this {snooze_max} times. Use /reschedule to set a "
                        "real date, or /done to close it."),
            "snooze_count": outcome["snooze_count"],
        }}

    entry = outcome["entry"]
    reprompt = "I'll remind you then -- and ask why it slipped. What's blocking this?"
    # Akrasia note: a long snooze means the task disappears for a while -- say so.
    if minutes > 3 * 24 * 60:
        reprompt += f" Note: a {minutes // (24 * 60)}-day snooze hides this until then."
    _log("nag_snoozed", task_id=task_id, nag_loop_id=entry.get("nag_loop_id"),
         snooze_count=entry.get("snooze_count"), snoozed_until=snoozed_until,
         block_reason=block_reason)
    return {"ok": True, "task_id": task_id, "snoozed_until": snoozed_until,
            "snooze_count": entry["snooze_count"], "reprompt": reprompt}


# --- /body-double ----------------------------------------------------------

# Two check-ins: halfway + at the end.  These fractions of the session duration
# size the ephemeral one-shot crons.
_CHECKIN_FRACTIONS = (0.5, 1.0)


def _checkin_cron(session_id: str, task_id: str, elapsed_min: int, fire_at: datetime,
                  delivery_target: dict[str, Any], *, is_final: bool) -> dict[str, Any]:
    """Build ONE ephemeral check-in cron descriptor.

    Every check-in cron carries an EXPLICIT ``delivery.to`` + ``agentId`` set at
    session-start time (it can never derive the target at fire time from session
    history -- Hard Gate #4), and ``deleteAfterRun: true`` so the gateway reaps it
    after firing (no agent-issued cron rm).
    """
    to = f"{delivery_target['chat_id']}:topic:{delivery_target['topic_id']}"
    kind = "session-end" if is_final else "halfway"
    return {
        "name": f"body-double {kind} {session_id}",
        "schedule": {"kind": "at", "at": fire_at.isoformat()},
        "agentId": delivery_target["agent_id"],
        "deleteAfterRun": True,
        "toolsAllow": ["exec"],
        "prompt": (f"BODY_DOUBLE_CHECKIN session={session_id} task={task_id} "
                   f"elapsed={elapsed_min}m final={is_final}"),
        "delivery": {"mode": "announce", "channel": "telegram", "to": to},
    }


def handle_body_double(
    task_id: str,
    duration: str,
    *,
    personal: bool = False,
    create_cron: Callable[[dict[str, Any]], str] | None = None,
) -> dict[str, Any]:
    """Start a body-double session: two ephemeral check-in crons + a state record.

    Refuses (spec §2.5 denied paths):

    * a task not on the active board -- "I can't body-double a task that isn't on
      your active board.";
    * a second concurrent session for the same task.

    Every check-in cron's delivery target is PROVEN first (``resolve_target``); if
    the env is unset NO crons are created and the start is blocked (a body-double
    that cannot prove its check-in destination must not silently start headless).

    ``create_cron`` is injectable (the gateway ``openclaw cron create`` in
    production); it returns the created cron id.  It defaults to a no-op id so a
    dry-run / test records the descriptors without a live gateway.
    """
    minutes = parse_duration_minutes(duration)
    if minutes <= 0:
        return {"ok": False, "error": {
            "code": "invalid-duration",
            "message": f"Body-double duration must be like 90m/1h; got {duration!r}.",
        }}

    record = _active_record(task_id, personal=personal)
    if record is None:
        return {"ok": False, "error": {
            "code": "task-not-active",
            "message": "I can't body-double a task that isn't on your active board.",
        }}

    existing = nag_state.read_state().get(task_id)
    active_session = nag_state.active_body_double_session(existing)
    if active_session is not None:
        return {"ok": False, "error": {
            "code": "session-already-active",
            "message": (f"There's already an active body-double session for this task "
                        f"(started at {active_session.get('started_at')}). "
                        f"Reply /cancel-session {task_id} to end it first."),
        }}

    proof = nag_delivery.resolve_target()
    if not proof["ok"]:
        return {"ok": False, "error": {
            "code": "delivery-target-unproven",
            "message": "Cannot prove a check-in delivery target; body-double not started.",
            "reason": proof["reason"],
        }}
    delivery_target = proof["delivery_target"]

    session_id = f"bd_{uuid.uuid4().hex[:12]}"
    started_at = _now()
    create = create_cron or (lambda _descriptor: f"cron_{uuid.uuid4().hex[:12]}")
    cron_ids: list[str] = []
    for fraction in _CHECKIN_FRACTIONS:
        elapsed = int(round(minutes * fraction))
        descriptor = _checkin_cron(
            session_id, task_id, elapsed, started_at + timedelta(minutes=elapsed),
            delivery_target, is_final=(fraction == 1.0),
        )
        cron_ids.append(create(descriptor))

    session = {
        "session_id": session_id,
        "cron_ids": cron_ids,
        "started_at": started_at.isoformat(),
        "duration_min": minutes,
        "delivery_target": delivery_target,
        "ended_at": None,
        "outcome": None,
    }
    nag_state.transition(lambda state: nag_state.add_body_double_session(state, task_id, session))
    _log("body_double_started", task_id=task_id, session_id=session_id,
         duration_min=minutes, cron_ids=cron_ids)
    return {"ok": True, "task_id": task_id, "session_id": session_id,
            "cron_ids": cron_ids, "duration_min": minutes,
            "ack": "Session started. I'll check in at the halfway and end points. "
                   "What are you aiming to finish?"}


def handle_cancel_session(
    task_id: str,
    *,
    delete_cron: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    """End the active body-double session for ``task_id`` + delete its pending crons.

    ``delete_cron`` is injectable (the gateway ``openclaw cron delete``); it
    defaults to a no-op so a test exercises the state transition without a gateway.
    The ephemeral crons are ``deleteAfterRun`` so a fired one is already gone;
    deleting cancels the ones that have NOT yet fired.
    """
    existing = nag_state.read_state().get(task_id)
    session = nag_state.active_body_double_session(existing)
    if session is None:
        return {"ok": False, "error": {
            "code": "no-active-session",
            "message": "No active body-double session for this task.",
        }}

    delete = delete_cron or (lambda _cron_id: None)
    for cron_id in session.get("cron_ids") or []:
        delete(cron_id)

    ended = nag_state.transition(
        lambda state: nag_state.end_body_double_session(
            state, task_id, session["session_id"], outcome="cancelled")
    )
    _log("body_double_ended", task_id=task_id, session_id=session["session_id"],
         outcome="cancelled")
    return {"ok": True, "task_id": task_id, "session_id": session["session_id"],
            "outcome": "cancelled", "ended": ended is not None}


# --- CLI surface (routed via telegram-commands.sh) -------------------------

_SAFE_ID = re.compile(r"^[A-Za-z0-9._:-]+$")


def _require_id(task_id: str) -> dict[str, Any] | None:
    """Block any non-canonical task_id (no title/position matching on the board)."""
    if not _SAFE_ID.match(task_id or ""):
        return {"ok": False, "error": {
            "code": "unsafe-task-id",
            "message": "A canonical task_id is required; title/position matching is blocked.",
        }}
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nag_commands.py", description=__doc__)
    parser.add_argument("--personal", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p_done = sub.add_parser("done", help="complete a task + close its nag loop")
    p_done.add_argument("task_id")

    p_res = sub.add_parser("reschedule", help="move due:: + close the nag loop")
    p_res.add_argument("task_id")
    p_res.add_argument("new_due", help="YYYY-MM-DD")

    p_snz = sub.add_parser("snooze", help="pause the nag loop (cap 3)")
    p_snz.add_argument("task_id")
    p_snz.add_argument("duration", help="e.g. 1h / 1d / 3d")
    p_snz.add_argument("--reason", default=None, help="why it slipped (akrasia note)")

    p_bd = sub.add_parser("body-double", help="start a focus session with check-ins")
    p_bd.add_argument("task_id")
    p_bd.add_argument("duration", help="e.g. 90m / 1h")

    p_cancel = sub.add_parser("cancel-session", help="end a body-double session")
    p_cancel.add_argument("task_id")

    args = parser.parse_args(argv)

    blocked = _require_id(getattr(args, "task_id", ""))
    if blocked is not None:
        result = blocked
    elif args.command == "done":
        result = handle_done(args.task_id, personal=args.personal)
    elif args.command == "reschedule":
        result = handle_reschedule(args.task_id, args.new_due, personal=args.personal)
    elif args.command == "snooze":
        result = handle_snooze(args.task_id, args.duration, block_reason=args.reason)
    elif args.command == "body-double":
        result = handle_body_double(args.task_id, args.duration, personal=args.personal)
    else:  # cancel-session
        result = handle_cancel_session(args.task_id)

    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
