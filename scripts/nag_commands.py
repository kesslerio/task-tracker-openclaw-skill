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
* ``/start <id> [<min>] [next: <cue>]`` (H7) -- the initiation loop: REUSES the
  body-double focus-session machinery (``_open_focus_session``) and layers on a
  resumption CUE stored on the session, a QUIET window for the session duration
  (H5), and an end-of-session done/continue/blocked/redefine DISPOSITION prompt.
  ``/start`` (no task) / ``/start status`` shows the active session's cue.
* ``/cancel-session <id>`` -- end the active focus/body-double session + delete its
  pending crons, and clear THIS session's quiet (only if it still matches -- a
  longer manual ``/quiet`` is left intact).

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
import cron_backend
import nag_delivery
import nag_state
import quiet_state
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


def _safe_delete(cron_id: str, *, delete: Callable[[str], Any] | None = None) -> None:
    """Best-effort delete of a pending cron; a backend failure is swallowed.

    Used when rolling back a partially-created body-double and when cancelling a
    session: a transient gateway error must not block the state cleanup, and a
    deleteAfterRun cron may already be gone. ``delete`` resolves to the live
    backend at CALL time (not bound at def time) so a test can patch it.
    """
    delete = delete or cron_backend.delete_cron
    try:
        delete(cron_id)
    except cron_backend.CronBackendError:
        pass


# --- /done + /reschedule: board op THEN synchronous close in the same turn ---

def _recycle_loop(result: dict[str, Any], task_id: str, *, closed_by: str) -> dict[str, Any]:
    """Clear (recycle) the task's nag loop and annotate ``result`` accordingly.

    Used by the recurring-/done and reschedule paths. The loop is NOT terminally
    acked -- it is reset so a future overdue crossing re-opens a fresh loop. The
    ledger event is ``nag_acked`` (the registered close event) but carries
    ``recycled: true`` in its metadata so an audit consumer can tell a recycle from
    a terminal ack and is not surprised when the loop re-nags later.
    """
    cleared = nag_state.transition(lambda state: nag_state.clear_loop(state, task_id))
    if cleared is not None:
        _log("nag_acked", task_id=task_id, nag_loop_id=cleared.get("nag_loop_id"),
             closed_by=closed_by, recycled=True)
    result["nag_closed"] = False  # recycled, not terminally closed
    result["nag_recycled"] = cleared is not None
    return result


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
    """Complete ``task_id`` then close its nag loop in the SAME turn (Path A).

    A one-shot task is removed from the board, so its loop is terminally acked. A
    RECURRING task is rolled forward (same canonical_id, new future due date); its
    loop is CLEARED instead of acked -- an acked entry is terminal and the cron
    skips it, so acking would mute every future recurrence. Clearing lets the next
    overdue crossing open a clean fresh loop.
    """
    result = complete_by_id(task_id, personal=personal, source="user_command")
    if not result.get("ok"):
        return result
    if result.get("recurring"):
        return _recycle_loop(result, task_id, closed_by=nag_state.CLOSED_EXPLICIT_DONE)
    return _close_loop_after_board_op(result, task_id,
                                      closed_by=nag_state.CLOSED_EXPLICIT_DONE)


def handle_reschedule(task_id: str, new_due: str, *, personal: bool = False) -> dict[str, Any]:
    """Move ``due::`` then RECYCLE the nag loop in the SAME turn (Path C / spec T10).

    A reschedule always CLEARS the loop rather than acking it -- never sets the
    terminal ``ack: true``. Acking would permanently mute the task: when the new
    due date later passes and the task is overdue again, the cron skips acked
    entries forever (the same accountability hole the recurring-/done path
    deliberately avoids by clearing). Clearing lets the next overdue crossing open
    a fresh loop with a new nag_loop_id:

    * future date -> off the overdue set now; re-nags only if/when it lapses again;
    * still-overdue date (T10) -> the next nag-check opens a fresh loop right away.
    """
    result = reschedule_by_id(task_id, new_due, personal=personal, source="user_command")
    if not result.get("ok"):
        return result
    return _recycle_loop(result, task_id, closed_by=nag_state.CLOSED_RESCHEDULED)


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

    # Validate membership + cap AND apply the snooze inside ONE locked transition so
    # two racing 4th-snooze attempts cannot both slip past a stale read of
    # snooze_count. A snooze PAUSES an existing open nag loop -- it must not
    # materialise a phantom snoozed entry for a task that was never nagged (which
    # would pre-suppress a future first nag and leave an unreclaimed stub). So a
    # snooze with no genuine open loop is refused, mirroring the sibling handlers'
    # board-membership validation.
    def mutate(state: dict[str, Any]) -> dict[str, Any]:
        current = state.get(task_id)
        if not (nag_state.is_open(current) and nag_state.is_genuine_nag(current)):
            return {"refused": "no-open-loop"}
        if nag_state.snooze_capped(current, snooze_max=snooze_max):
            return {"refused": "cap", "snooze_count": int(current.get("snooze_count") or 0)}
        entry = nag_state.apply_snooze(state, task_id, snoozed_until=snoozed_until,
                                       block_reason=block_reason)
        return {"entry": entry}

    outcome = nag_state.transition(mutate)
    if outcome.get("refused") == "no-open-loop":
        return {"ok": False, "error": {
            "code": "no-open-nag",
            "message": ("No open nag loop for this task to snooze. /snooze pauses an "
                        "active nag; nothing is firing for this task."),
        }}
    if outcome.get("refused") == "cap":
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
                  delivery_target: dict[str, Any], *, is_final: bool,
                  label: str = "body-double", prompt: str | None = None) -> dict[str, Any]:
    """Build ONE ephemeral check-in cron descriptor.

    Every check-in cron carries an EXPLICIT ``delivery.to`` + ``agentId`` set at
    session-start time (it can never derive the target at fire time from session
    history -- Hard Gate #4), and ``deleteAfterRun: true`` so the gateway reaps it
    after firing (no agent-issued cron rm).

    ``label`` names the session kind in the cron name (``body-double`` or ``start``)
    and ``prompt`` lets the caller override the default check-in prompt -- H7's
    ``/start`` final check-in carries the done/continue/blocked/redefine disposition
    text instead of the body-double check-in marker. When ``prompt`` is omitted the
    default body-double marker is used, so ``/body-double`` is unchanged.
    """
    to = f"{delivery_target['chat_id']}:topic:{delivery_target['topic_id']}"
    kind = "session-end" if is_final else "halfway"
    return {
        "name": f"{label} {kind} {session_id}",
        "schedule": {"kind": "at", "at": fire_at.isoformat()},
        "agentId": delivery_target["agent_id"],
        "deleteAfterRun": True,
        "toolsAllow": ["exec"],
        "prompt": prompt or (f"BODY_DOUBLE_CHECKIN session={session_id} task={task_id} "
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

    ``create_cron`` is injectable (a test passes a recording stub); it returns the
    created cron id. It DEFAULTS to the real gateway backend
    (``cron_backend.create_cron``) so the production CLI path actually schedules
    the check-ins -- never a silent no-op that reports "started" while creating
    nothing. A backend failure raises ``CronBackendError`` and is reported as a
    structured error (no half-started session is recorded).
    """
    started = _open_focus_session(
        task_id, duration,
        personal=personal,
        id_prefix="bd_",
        label="body-double",
        invalid_duration_msg=f"Body-double duration must be like 90m/1h; got {duration!r}.",
        unproven_msg="Cannot prove a check-in delivery target; body-double not started.",
        create_cron=create_cron,
    )
    if not started["ok"]:
        return started["error"]

    session_id = started["session_id"]
    _log("body_double_started", task_id=task_id, session_id=session_id,
         duration_min=started["duration_min"], cron_ids=started["cron_ids"])
    return {"ok": True, "task_id": task_id, "session_id": session_id,
            "cron_ids": started["cron_ids"], "duration_min": started["duration_min"],
            "ack": "Session started. I'll check in at the halfway and end points. "
                   "What are you aiming to finish?"}


def _open_focus_session(
    task_id: str,
    duration: str | None,
    *,
    personal: bool,
    id_prefix: str,
    label: str,
    invalid_duration_msg: str,
    unproven_msg: str,
    create_cron: Callable[[dict[str, Any]], str] | None,
    default_minutes: int | None = None,
    cue: str | None = None,
    final_prompt: Callable[[str], str] | None = None,
    extra_session: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Shared focus-session core for ``/body-double`` and ``/start`` (DRY).

    Validates duration + active board + one-session-per-task, proves the delivery
    target, schedules the ephemeral check-in cron pair (each with the explicit
    proven ``delivery.to`` + ``agentId``), and appends the session under the lock
    (rolling its crons back if a concurrent session won the race). Returns
    ``{"ok": True, "session_id", "cron_ids", "duration_min", "session"}`` on success
    or ``{"ok": False, "error": <handler-result>}``.

    ``default_minutes`` lets ``/start`` accept an OMITTED duration and fall back to
    the configured default; ``cue`` / ``extra_session`` are stored ON the session so
    they survive a crash (in nag-state.json); ``final_prompt`` overrides the final
    check-in cron's prompt (``/start``'s disposition text). ``/body-double`` passes
    none of these, so its behaviour is unchanged.
    """
    minutes = parse_duration_minutes(duration)
    if minutes <= 0 and default_minutes is not None and not (duration or "").strip():
        minutes = default_minutes
    if minutes <= 0:
        return {"ok": False, "error": {"ok": False, "error": {
            "code": "invalid-duration",
            "message": invalid_duration_msg,
        }}}
    if _active_record(task_id, personal=personal) is None:
        return {"ok": False, "error": {"ok": False, "error": {
            "code": "task-not-active",
            "message": "I can't body-double a task that isn't on your active board.",
        }}}
    # ``now`` makes an ELAPSED prior session (its block already over -- the final
    # check-in cron fired and was reaped, but nothing marked the session ended)
    # auto-expire here, so a new /start on that task is no longer blocked until a
    # manual /cancel-session. A genuinely ACTIVE (not-yet-elapsed) session still
    # refuses below.
    now = _now()
    active_session = nag_state.active_body_double_session(
        nag_state.read_state().get(task_id), now=now)
    if active_session is not None:
        return {"ok": False, "error": _session_active_error(task_id, active_session.get("started_at"))}

    proof = nag_delivery.resolve_target()
    if not proof["ok"]:
        return {"ok": False, "error": {"ok": False, "error": {
            "code": "delivery-target-unproven",
            "message": unproven_msg,
            "reason": proof["reason"],
        }}}

    session_id = f"{id_prefix}{uuid.uuid4().hex[:12]}"
    started_at = _now()
    final_prompt_text = final_prompt(session_id) if final_prompt is not None else None
    created = _create_checkin_crons(session_id, task_id, minutes, started_at,
                                    proof["delivery_target"],
                                    create=create_cron or cron_backend.create_cron,
                                    label=label, final_prompt=final_prompt_text)
    if not created["ok"]:
        return {"ok": False, "error": created["error"]}

    # ends_at marks when the block elapses (started_at + the parsed duration). It
    # lets active_body_double_session(now=...) auto-expire a session whose final
    # check-in cron has already fired (the cron only delivers text, it never marks
    # the session ended), so the task frees up for a fresh /start without a manual
    # /cancel-session.
    ends_at = (started_at + timedelta(minutes=minutes)).isoformat()
    session = {"session_id": session_id, "cron_ids": created["cron_ids"],
               "started_at": started_at.isoformat(), "ends_at": ends_at,
               "duration_min": minutes,
               "delivery_target": proof["delivery_target"], "ended_at": None,
               "outcome": None, "cue": cue}
    if extra_session:
        session.update(extra_session)
    # The append re-validates the one-session-per-task invariant UNDER the lock; if
    # a concurrent (or stale-elapsed, via now) session won the race it returns None
    # and we roll back the crons we just created (the early pre-check above is only
    # fast feedback).
    if nag_state.transition(
            lambda s: nag_state.add_body_double_session(s, task_id, session, now=now)) is None:
        for cron_id in created["cron_ids"]:
            _safe_delete(cron_id)
        return {"ok": False, "error": _session_active_error(task_id)}

    return {"ok": True, "session_id": session_id, "cron_ids": created["cron_ids"],
            "duration_min": minutes, "session": session}


def _session_active_error(task_id: str, started_at: str | None = None) -> dict[str, Any]:
    """The 'a session is already active' refusal (early pre-check + under-lock race).

    Shared by ``/body-double`` and ``/start`` -- one focus/body-double session per
    task at a time (``active_body_double_session``'s guard)."""
    when = f" (started at {started_at})" if started_at else ""
    return {"ok": False, "error": {
        "code": "session-already-active",
        "message": (f"There's already an active focus session for this task{when}. "
                    f"Reply /cancel-session {task_id} to end it first."),
    }}


def _create_checkin_crons(session_id, task_id, minutes, started_at, delivery_target,
                          *, create: Callable[[dict[str, Any]], str],
                          label: str = "body-double",
                          final_prompt: str | None = None) -> dict[str, Any]:
    """Create the check-in cron pair; roll back partial creation on failure.

    Returns ``{"ok": True, "cron_ids": [...]}`` on success, or ``{"ok": False,
    "error": <handler-result>}`` where ``error`` is the full structured result the
    caller returns. A backend failure deletes any cron already created so no
    half-started session is left behind (the caller must not record a session or
    report "started").

    ``label`` names the cron kind; ``final_prompt`` overrides the FINAL check-in
    cron's prompt (H7's ``/start`` disposition). Both default to the body-double
    behaviour so ``/body-double`` is unchanged.
    """
    cron_ids: list[str] = []
    try:
        for fraction in _CHECKIN_FRACTIONS:
            elapsed = int(round(minutes * fraction))
            is_final = fraction == 1.0
            descriptor = _checkin_cron(
                session_id, task_id, elapsed, started_at + timedelta(minutes=elapsed),
                delivery_target, is_final=is_final, label=label,
                prompt=final_prompt if (is_final and final_prompt) else None,
            )
            cron_ids.append(create(descriptor))
    except cron_backend.CronBackendError as exc:
        for cron_id in cron_ids:
            _safe_delete(cron_id)
        return {"ok": False, "error": {"ok": False, "error": {
            "code": "checkin-cron-failed",
            "message": "Could not schedule the body-double check-ins; session not started.",
            "reason": str(exc),
        }}}
    return {"ok": True, "cron_ids": cron_ids}


# --- /start: the initiation loop (reuses the focus-session machinery) -------

# The end-of-session disposition prompt (H7 step 4). It becomes the FINAL check-in
# cron's text, so the gateway fires the structured done/continue/blocked/redefine
# choice at the session end -- directing to the EXISTING commands (no new /continue
# or /redefine commands: continue == /start again, redefine == a plain reply).
def _disposition_prompt(session_id: str, task_id: str, cue: str) -> str:
    return (
        f"START_SESSION_END session={session_id} task={task_id}\n"
        f"Focus block done. Resume cue was: {cue!r}.\n"
        "How did it go? Pick one:\n"
        f"  done -> /done {task_id}\n"
        f"  continue -> /start {task_id} (another block)\n"
        f"  blocked -> /reschedule {task_id} <date>\n"
        "  redefine -> just reply with the new next action."
    )


def handle_start(
    task_id: str,
    duration: str | None = None,
    cue: str | None = None,
    *,
    personal: bool = False,
    create_cron: Callable[[dict[str, Any]], str] | None = None,
) -> dict[str, Any]:
    """``/start <task> [<minutes>] [next: <cue>]`` -- the initiation loop (H7).

    A task list surfaces tasks but doesn't help you START. ``/start`` reuses the
    body-double focus-session machinery (DRY: ``_open_focus_session`` -- same
    session record, same ephemeral check-in crons each carrying the explicit proven
    ``delivery.to`` + ``agentId``) and LAYERS on:

    * a resumption CUE stored ON the session (so it survives a crash via
      nag-state.json): the user's ``next:`` text, else ``Work on: <task title>``;
    * a QUIET window for the session duration (``quiet_state.set_quiet``) so the nag
      is muted while the user focuses -- recorded on the session (``quiet_set``,
      ``quiet_until``) so ``/cancel-session`` can clear ONLY this session's quiet and
      never clobber a longer manual ``/quiet``;
    * an end-of-session check-in whose text is the structured
      done/continue/blocked/redefine DISPOSITION (the final check-in cron's prompt).

    A duration default of ``cos_config.start_session_minutes()`` (25, floored at 1)
    applies when the user omits it. ``/body-double`` is untouched.
    """
    record = _active_record(task_id, personal=personal)
    if cue is None:
        title = record.title if record is not None else task_id
        cue = f"Work on: {title}"

    started = _open_focus_session(
        task_id, duration,
        personal=personal,
        id_prefix="st_",
        label="start",
        invalid_duration_msg=(f"Start duration must be like 25 / 45m / 1h; got {duration!r}. "
                              "Omit it for the default block."),
        unproven_msg="Cannot prove a check-in delivery target; focus session not started.",
        create_cron=create_cron,
        default_minutes=cos_config.start_session_minutes(),
        cue=cue,
        final_prompt=lambda sid: _disposition_prompt(sid, task_id, cue),
        extra_session={"kind": "start"},
    )
    if not started["ok"]:
        return started["error"]

    session_id = started["session_id"]
    minutes = started["duration_min"]
    started_at = datetime.fromisoformat(started["session"]["started_at"])

    # Mute the nag for the focus block (H5 reuse). Record on the session that THIS
    # /start set quiet + the exact deadline, so /cancel-session can clear ONLY this
    # session's quiet and never cut a longer manual /quiet short (the guard below).
    quiet_set = False
    session_deadline = started_at + timedelta(minutes=minutes)
    quiet_until_iso = session_deadline.isoformat()
    existing_quiet = quiet_state.quiet_until(_now())
    if existing_quiet is None or existing_quiet <= session_deadline:
        # No manual quiet, or a manual quiet that ends no later than this session --
        # set the session window (extending a shorter manual quiet is harmless and
        # the deadline match below still lets cancel clear it).
        quiet_state.set_quiet(session_deadline)
        quiet_set = True
    nag_state.transition(lambda s: _annotate_quiet(s, task_id, session_id,
                                                   quiet_set, quiet_until_iso))

    _log("start_session_started", task_id=task_id, session_id=session_id,
         duration_min=minutes, cron_ids=started["cron_ids"], cue=cue,
         quiet_set=quiet_set)
    return {"ok": True, "task_id": task_id, "session_id": session_id,
            "cron_ids": started["cron_ids"], "duration_min": minutes, "cue": cue,
            "quiet_set": quiet_set, "quiet_until": quiet_until_iso if quiet_set else None,
            "ack": (f"Started a {minutes}-min focus block on {task_id}. Nag muted until "
                    f"then. Next action: {cue}. I'll check in at the halfway and end "
                    "points and ask done/continue/blocked/redefine.")}


def _annotate_quiet(state: dict[str, Any], task_id: str, session_id: str,
                    quiet_set: bool, quiet_until_iso: str) -> None:
    """Stamp ``quiet_set`` + ``quiet_until`` onto the just-created session (under lock).

    Stored ON the session so a crash-restart still knows whether to clear quiet on
    cancel, and which deadline must match before clearing (so /start's auto-quiet
    never clobbers a longer manual /quiet)."""
    entry = state.get(task_id)
    if not isinstance(entry, dict):
        return
    for session in entry.get("body_double_sessions") or []:
        if isinstance(session, dict) and session.get("session_id") == session_id:
            session["quiet_set"] = quiet_set
            session["quiet_until"] = quiet_until_iso
            return


def handle_start_status(*, personal: bool = False) -> dict[str, Any]:
    """``/start`` (no task) / ``/start status`` -- show the active session + its cue.

    A context-switched user has lost the thread; this surfaces the live focus
    session and its resumption cue (``Resume: <cue>``) so they can pick back up. It
    is read-only over nag-state.json -- no board read, no target proof, no push.
    Scans every task's entry because the cue/session live keyed by task_id.

    ``now`` is passed so an ELAPSED session (its block already over) is treated as
    not active -- we don't advertise a stale ``Resume:`` cue for a block that has
    already ended; it reports "no active session" instead.
    """
    now = _now()
    state = nag_state.read_state()
    for task_id, entry in state.items():
        session = nag_state.active_body_double_session(entry, now=now)
        if session is None:
            continue
        cue = session.get("cue")
        resume = f"Resume: {cue}" if cue else "No resumption cue saved for this session."
        return {"ok": True, "active": True, "task_id": task_id,
                "session_id": session.get("session_id"), "cue": cue,
                "started_at": session.get("started_at"),
                "duration_min": session.get("duration_min"),
                "message": (f"Active focus session on {task_id} "
                            f"(started {session.get('started_at')}). {resume}")}
    return {"ok": True, "active": False,
            "message": "No active focus session. /start <task_id> to begin one."}


def handle_cancel_session(
    task_id: str,
    *,
    delete_cron: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    """End the active body-double / focus session for ``task_id`` + delete its crons.

    ``delete_cron`` is injectable (a test passes a recording stub); it DEFAULTS to
    the real gateway backend (``cron_backend.delete_cron``). The ephemeral crons
    are ``deleteAfterRun`` so a fired one is already gone; deleting cancels the
    ones that have NOT yet fired. A delete failure is swallowed (best-effort) so a
    transient gateway hiccup cannot block ending the session in state.

    H7: if THIS session set a quiet window (``/start``'s auto-mute), clear it on
    cancel -- but ONLY when the live quiet deadline still matches the session's
    ``quiet_until``. A user who ran a longer ``/quiet 24h`` independently (or after
    the session started) keeps it: ending a 25-min focus block must never cut a
    manual day-long quiet short.
    """
    existing = nag_state.read_state().get(task_id)
    session = nag_state.active_body_double_session(existing)
    if session is None:
        return {"ok": False, "error": {
            "code": "no-active-session",
            "message": "No active body-double session for this task.",
        }}

    delete = delete_cron or cron_backend.delete_cron
    for cron_id in session.get("cron_ids") or []:
        _safe_delete(cron_id, delete=delete)

    quiet_cleared = _clear_session_quiet(session)

    ended = nag_state.transition(
        lambda state: nag_state.end_body_double_session(
            state, task_id, session["session_id"], outcome="cancelled")
    )
    _log("body_double_ended", task_id=task_id, session_id=session["session_id"],
         outcome="cancelled", quiet_cleared=quiet_cleared)
    return {"ok": True, "task_id": task_id, "session_id": session["session_id"],
            "outcome": "cancelled", "ended": ended is not None,
            "quiet_cleared": quiet_cleared}


def _clear_session_quiet(session: dict[str, Any]) -> bool:
    """Clear quiet IFF this session set it AND the live deadline still matches it.

    The guard that stops /start's auto-quiet from clobbering a longer manual
    /quiet: clears only when ``session["quiet_set"]`` is true and the current live
    ``quiet_until`` equals the session's stored ``quiet_until``. A user who set a
    LONGER /quiet (different/later deadline) keeps it; a window that already
    expired/was-cleared is a no-op. Returns True only when it actually cleared.
    """
    if not session.get("quiet_set"):
        return False
    stored = session.get("quiet_until")
    if not stored:
        return False
    try:
        session_deadline = datetime.fromisoformat(str(stored))
    except (TypeError, ValueError):
        return False
    live = quiet_state.quiet_until(_now())
    if live is not None and live == session_deadline:
        quiet_state.clear_quiet()
        return True
    return False


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

    p_start = sub.add_parser("start", help="begin a focus block: cue + timer + muted nag")
    # Everything after `start` is free-form so we can accept the optional
    # `[<minutes>] [next: <cue text>]` tail (and the no-arg / `status` form).
    p_start.add_argument("rest", nargs="*", help="[<task_id>] [<minutes>] [next: <cue>]")

    p_cancel = sub.add_parser("cancel-session", help="end a focus/body-double session")
    p_cancel.add_argument("task_id")

    args = parser.parse_args(argv)

    if args.command == "start":
        result = _dispatch_start(args.rest, personal=args.personal)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result.get("ok") else 2

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


def parse_start_tail(rest: list[str]) -> dict[str, Any]:
    """Parse ``/start`` args into ``{task_id, duration, cue}`` (or a status request).

    Grammar: ``[<task_id>] [<minutes>] [next: <cue words...>]``.

    * No tokens, or a lone ``status`` token -> ``{"status": True}`` (the no-arg /
      ``/start status`` form that shows the active session's cue).
    * First token is the task_id. An optional second token that looks like a
      duration (``25`` / ``45m`` / ``1h``) is the minutes. Everything from a ``next:``
      token onward is the resumption cue text (joined back into one line).
    """
    tokens = [t for t in rest if t != ""]
    if not tokens or (len(tokens) == 1 and tokens[0].lower() == "status"):
        return {"status": True}

    task_id = tokens[0]
    duration: str | None = None
    cue: str | None = None
    idx = 1
    # Find a `next:` marker (the cue), so the words after it are not mistaken for a
    # duration. Accept `next:` as its own token or as a `next:foo` prefix.
    next_at = None
    for i, tok in enumerate(tokens[1:], start=1):
        if tok.lower() == "next:" or tok.lower().startswith("next:"):
            next_at = i
            break
    cue_end = next_at if next_at is not None else len(tokens)
    # An optional duration sits between the task_id and the cue (or end).
    if idx < cue_end and parse_duration_minutes(tokens[idx]) > 0:
        duration = tokens[idx]
    if next_at is not None:
        cue_tokens = tokens[next_at:]
        # Strip the leading `next:` marker (whole token or prefix).
        first = cue_tokens[0]
        remainder = first[len("next:"):] if len(first) > len("next:") else ""
        cue_words = ([remainder] if remainder else []) + cue_tokens[1:]
        cue = " ".join(cue_words).strip() or None
    return {"status": False, "task_id": task_id, "duration": duration, "cue": cue}


def _dispatch_start(rest: list[str], *, personal: bool) -> dict[str, Any]:
    """Route a parsed ``/start`` invocation to status-show or session-start."""
    parsed = parse_start_tail(rest)
    if parsed.get("status"):
        return handle_start_status(personal=personal)
    blocked = _require_id(parsed["task_id"])
    if blocked is not None:
        return blocked
    return handle_start(parsed["task_id"], parsed.get("duration"), parsed.get("cue"),
                        personal=personal)


if __name__ == "__main__":
    raise SystemExit(main())
