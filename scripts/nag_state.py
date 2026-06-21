#!/usr/bin/env python3
"""U4 nag-state I/O layer (Contract 3): the single source of truth for open loops.

``nag-state.json`` (under ``cos_config.state_dir()``) is keyed by ``task_id``.
An entry with ``ack: false`` MEANS the nag loop is open; ``ack: true`` is terminal
for a loop (only a NEW ``nag_loop_id`` reactivates a task -- the old loop is moved
to ``archived_nag_loops``). This module owns every read/modify/write of that file
so both the background cron (``nag_check.py``) and the reactive command path
(``nag_commands.py``) mutate it through one flock-guarded, atomic-write code path.

Why a sidecar lockfile + atomic write (not ``.write_text``): overlapping
heartbeats / a reactive ``/done`` racing the cron would otherwise read the same
base state, each add their own entry, and the last writer's ``os.replace`` drops
every other writer's entry (lost update). ``transition()`` serialises the whole
read-modify-write cycle on a sidecar lock so every concurrent writer survives, and
``_atomic_write`` (temp+replace+fsync) prevents a torn write from corrupting the
file mid-heartbeat.

This module makes NO board write and sends NO Telegram push -- it is pure state.
The push/proof plumbing lives in ``nag_check.py``; the board mutations live in the
reactive command handlers that call ``task_transitions``.
"""

from __future__ import annotations

import json
import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None

from cos_config import state_dir

# Reuse the Phase-0a state helpers rather than reinventing them: the frozen
# Contract-3 entry shape, the corrupt-aside-quarantining read, and the atomic
# write all already live in autonomy_gate (U2 ships the /undo stub against them).
from autonomy_gate import (
    _read_nag_state,
    _write_nag_state,
    default_nag_entry,
    nag_lock_path,
    nag_state_path,
)

# Terminal close reasons (ack: true). Snooze is NOT here -- a snooze pauses, it
# never closes (the akrasia asymmetry: tightening is instant, loosening is not).
CLOSED_EXPLICIT_DONE = "explicit_done"
CLOSED_VERIFIED_DONE = "verified_done"
CLOSED_RESCHEDULED = "rescheduled"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_nag_loop_id() -> str:
    return f"nag_{uuid.uuid4().hex[:16]}"


def read_state() -> dict[str, Any]:
    """Read nag-state.json (corrupt file is quarantined aside, never destroyed)."""
    return _read_nag_state()


@contextmanager
def _locked_state() -> Iterator[dict[str, Any]]:
    """Yield the current nag-state under an exclusive sidecar flock.

    The lock is held for the whole read-modify-write so concurrent writers
    (heartbeat cron + reactive ``/done``) cannot lose each other's updates. The
    caller mutates the yielded dict in place; on a clean exit it is written back
    atomically. An exception inside the block does NOT write (the loop stays as it
    was on disk -- a crash never silently clears a nag).
    """
    state_dir()  # ensure the 0o700 dir exists before opening the lockfile
    lock_path = nag_lock_path()
    with lock_path.open("a", encoding="utf-8") as lock_handle:
        try:
            os.fchmod(lock_handle.fileno(), 0o600)
        except OSError:
            pass
        if fcntl is not None:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            state = read_state()
            yield state
            _write_nag_state(state)
        finally:
            if fcntl is not None:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def transition(mutator: Callable[[dict[str, Any]], Any]) -> Any:
    """Run ``mutator(state)`` under the lock and persist the result atomically.

    The single mutation primitive every state change funnels through. ``mutator``
    receives the live state dict, mutates it in place, and may return a value that
    ``transition`` passes back to the caller (e.g. the touched entry).
    """
    with _locked_state() as state:
        return mutator(state)


def open_loop(
    state: dict[str, Any],
    task_id: str,
    *,
    task_title: str,
    threshold_crossed: int,
    threshold_type: str,
    delivery_target: dict[str, Any],
) -> dict[str, Any]:
    """Create a FRESH open nag loop for ``task_id`` in ``state`` (in place).

    A brand-new entry is created from the frozen Contract-3 shape. If a closed
    (``ack: true``) entry already exists for this task, it is archived into
    ``archived_nag_loops`` first -- ``ack: true`` is terminal for a loop, so a
    re-nag is a DELIBERATE new loop with a new ``nag_loop_id``, never a silent
    reactivation of the old one (spec §2.3 "nag fire while ack: true").

    ``delivery_target`` is the PROOF record: an entry is only created with a
    proven target (the caller proves it first). It is stored so a re-fire can be
    checked against the live env (target_mismatch guard).
    """
    existing = state.get(task_id)
    entry = default_nag_entry(new_nag_loop_id(), delivery_target=delivery_target)
    if isinstance(existing, dict):
        entry["archived_nag_loops"] = list(existing.get("archived_nag_loops") or [])
        entry["archived_nag_loops"].append(_archive_view(existing))
    entry["task_title"] = task_title
    entry["threshold_crossed"] = threshold_crossed
    entry["threshold_type"] = threshold_type
    entry["threshold_crossed_at"] = now_iso()
    state[task_id] = entry
    return entry


def _archive_view(entry: dict[str, Any]) -> dict[str, Any]:
    """A compact snapshot of a closed loop for the archive (drops the recursive
    archive list so archives never nest unboundedly)."""
    return {k: v for k, v in entry.items() if k != "archived_nag_loops"}


def record_sent(state: dict[str, Any], task_id: str) -> dict[str, Any]:
    """Increment the loop's nag_count + stamp last_nag_ts after a successful push."""
    entry = state[task_id]
    entry["nag_count"] = int(entry.get("nag_count") or 0) + 1
    entry["last_nag_ts"] = now_iso()
    return entry


def close_loop(state: dict[str, Any], task_id: str, *, closed_by: str) -> dict[str, Any] | None:
    """Mark the task's loop acked (terminal). No-op if there is no entry.

    ``ack: true`` is the ONLY thing that closes a loop and it is set here, never by
    a background reactivation. Snooze does NOT call this (snooze != close).
    """
    entry = state.get(task_id)
    if not isinstance(entry, dict):
        return None
    entry["ack"] = True
    entry["closed_by"] = closed_by
    entry["closed_at"] = now_iso()
    return entry


def clear_loop(state: dict[str, Any], task_id: str) -> dict[str, Any] | None:
    """Remove the task's nag entry entirely (returns the removed entry, or None).

    Used when a RECURRING task is completed: the same canonical_id rolls forward to
    a new due date, so acking the loop would terminally mute it and the next
    recurrence would never nag (an acked entry is skipped by the cron). Clearing
    the entry lets the next overdue crossing open a clean, fresh loop. A
    body-double session attached to the entry is preserved by re-attaching it.
    """
    entry = state.pop(task_id, None)
    if isinstance(entry, dict):
        sessions = [s for s in (entry.get("body_double_sessions") or [])
                    if isinstance(s, dict) and not s.get("ended_at")]
        if sessions:
            fresh = default_nag_entry(new_nag_loop_id())
            fresh["body_double_sessions"] = sessions
            state[task_id] = fresh
    return entry if isinstance(entry, dict) else None


def is_genuine_nag(entry: dict[str, Any] | None) -> bool:
    """Has this entry actually nagged (an open NAG loop), vs a body-double-only stub?

    ``add_body_double_session`` may create an entry with ``nag_count == 0`` for a
    task that never crossed a nag threshold. The cron's close pass must not treat
    such a stub as an open nag loop to ack/close -- only a loop that has fired
    (``nag_count > 0``) is a genuine nag the cron owns.
    """
    return isinstance(entry, dict) and int(entry.get("nag_count") or 0) > 0


def apply_snooze(
    state: dict[str, Any],
    task_id: str,
    *,
    snoozed_until: str,
    block_reason: str | None,
) -> dict[str, Any]:
    """Pause the loop until ``snoozed_until`` and increment ``snooze_count``.

    Does NOT set ``ack`` -- a snooze pauses the nag, it never closes it (the
    akrasia asymmetry). The caller is responsible for enforcing the snooze cap
    BEFORE calling this (see ``snooze_capped``); this primitive only records.
    """
    entry = state.setdefault(task_id, default_nag_entry(new_nag_loop_id()))
    entry["snoozed_until"] = snoozed_until
    entry["snooze_count"] = int(entry.get("snooze_count") or 0) + 1
    entry["block_reason"] = block_reason
    return entry


def snooze_capped(entry: dict[str, Any] | None, *, snooze_max: int) -> bool:
    """Is this loop already at the snooze cap (akrasia limit, spec §2.3 denied)?"""
    if not isinstance(entry, dict):
        return False
    return int(entry.get("snooze_count") or 0) >= snooze_max


def add_body_double_session(
    state: dict[str, Any],
    task_id: str,
    session: dict[str, Any],
) -> dict[str, Any] | None:
    """Append a body-double session IFF no other session is active (one-per-task).

    Re-validates the no-active-session invariant UNDER the caller's lock (the
    early pre-check in the handler is outside the lock and is only fast feedback):
    if another concurrent ``/body-double`` slipped a session in first, this returns
    ``None`` and appends nothing, so the caller can roll back its just-created
    crons. On success it returns the entry. A body-double is independent of an open
    nag loop, so the entry is created if the task has none yet.
    """
    entry = state.get(task_id)
    if active_body_double_session(entry) is not None:
        return None
    entry = state.setdefault(task_id, default_nag_entry(new_nag_loop_id()))
    entry.setdefault("body_double_sessions", []).append(session)
    return entry


def active_body_double_session(entry: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return the first non-ended body-double session for the task, or None."""
    if not isinstance(entry, dict):
        return None
    for session in entry.get("body_double_sessions") or []:
        if isinstance(session, dict) and not session.get("ended_at"):
            return session
    return None


def end_body_double_session(
    state: dict[str, Any],
    task_id: str,
    session_id: str,
    *,
    outcome: str,
) -> dict[str, Any] | None:
    """Mark a body-double session ended with ``outcome``; None if not found."""
    entry = state.get(task_id)
    if not isinstance(entry, dict):
        return None
    for session in entry.get("body_double_sessions") or []:
        if isinstance(session, dict) and session.get("session_id") == session_id:
            session["ended_at"] = now_iso()
            session["outcome"] = outcome
            return session
    return None


def is_open(entry: dict[str, Any] | None) -> bool:
    """An open loop is one that exists and is not acked."""
    return isinstance(entry, dict) and not entry.get("ack", False)


def is_snoozed(entry: dict[str, Any] | None, *, now: datetime | None = None) -> bool:
    """Is the loop currently within an unexpired snooze window?

    A garbage/absent ``snoozed_until`` is treated as NOT snoozed (fail toward
    nagging, never toward silence -- a corrupt snooze must not mute a nag forever).
    """
    if not isinstance(entry, dict):
        return False
    raw = entry.get("snoozed_until")
    if not raw:
        return False
    try:
        until = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return False
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return now < until
