#!/usr/bin/env python3
"""U6 tomorrow-pointer: the EOD write side of the standup<->EOD daily loop.

The EOD ritual sets/confirms "tomorrow's #1" and persists it HERE, where the
morning standup (U8, not built yet) reads it as its opening line (KTD-6). This
module is the SOLE reader/writer of ``tomorrow-pointer.json`` under
``cos_config.state_dir()``.

Design rules (mirroring ``harvest_state.py`` / ``quiet_state.py`` so no unit
invents its own variant):

* **Single writer, atomic write.** Every write goes through ``utils._atomic_write``
  under a SIDECAR flock (``tomorrow-pointer.lock``), so a concurrent read can never
  see a half-written file and two writes cannot lose each other's update. The
  sidecar-lock + temp-file-swap idiom is the same one ``quiet_state``/``outbox`` use.

* **A SINGLE canonical pointer.** There is exactly one "tomorrow's #1": re-setting
  OVERWRITES it, never appends. A stale pointer from a prior day is overwritten on
  the next EOD, never accumulated -- the standup always resolves the LATEST decision.

* **An explicit "none" is a first-class value, not a missing file.** When the board
  has no open task to nominate, the EOD records an explicit ``task_id: null``
  pointer so the morning standup shows a CLEAN board ("no #1 set -- pick one")
  rather than silently resurfacing yesterday's stale #1. "No #1 today" must be a
  deliberate, dated record, distinguishable from "the EOD never ran".

* **A corrupt/missing file fails toward 'no pointer'.** A bad file never crashes the
  standup and is treated as no pointer (the next EOD rewrites it) -- the same
  fail-open posture ``quiet_state``/``harvest_state`` take. ``read_pointer`` never
  raises.

Schema (the contract the standup reads): ``{schema_version, task_id, title,
set_at, source}``. ``source`` is ``"eod"`` for the EOD-set pointer (the only
writer today). ``task_id`` is ``null`` for the explicit "none" record.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None

import cos_config
from utils import _atomic_write

SCHEMA_VERSION = 1

# The only writer today is the EOD ritual; ``source`` records provenance so a future
# writer (e.g. a manual override) is distinguishable in the standup + audit.
SOURCE_EOD = "eod"


def pointer_path() -> Path:
    return cos_config.state_dir() / "tomorrow-pointer.json"


def pointer_lock_path() -> Path:
    return cos_config.state_dir() / "tomorrow-pointer.lock"


@contextmanager
def _pointer_flock() -> Iterator[None]:
    """Hold the exclusive SIDECAR flock over ``tomorrow-pointer.json`` for the block.

    The lock lives on a separate ``tomorrow-pointer.lock`` file (the same pattern
    ``quiet_state._quiet_flock`` / ``outbox._outbox_flock`` use), NOT on the data
    file's own fd -- so a write's atomic ``os.replace`` (temp-file swap) can never
    orphan the lock, and the whole read-modify-write of a write runs serialised. A
    lock-free read (``read_pointer`` takes no lock) sees the whole old OR whole new
    file, never a torn middle -- the atomic swap guarantees no partial read.
    """
    cos_config.state_dir()  # ensure the 0o700 dir exists before opening the lockfile
    lock_path = pointer_lock_path()
    with lock_path.open("a", encoding="utf-8") as lock_handle:
        try:
            os.fchmod(lock_handle.fileno(), 0o600)
        except OSError:
            pass
        if fcntl is not None:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _build(task_id: str | None, title: str, *, source: str) -> dict[str, Any]:
    """Assemble the canonical pointer document (the standup's read contract)."""
    return {
        "schema_version": SCHEMA_VERSION,
        "task_id": task_id,
        "title": title,
        "set_at": _now_iso(),
        "source": source,
    }


def _write(pointer: dict[str, Any]) -> dict[str, Any]:
    """Atomically OVERWRITE the single canonical pointer under the flock.

    A write replaces the whole file (never appends): there is exactly one
    "tomorrow's #1", so a re-set or a stale prior-day pointer is overwritten, not
    accumulated. ``_atomic_write`` swaps a temp file in, so a reader sees the whole
    old or whole new pointer -- never a torn record.
    """
    with _pointer_flock():
        _atomic_write(
            pointer_path(),
            json.dumps(pointer, indent=2, sort_keys=True) + "\n",
        )
    return pointer


def set_top(task_id: str, title: str, *, source: str = SOURCE_EOD) -> dict[str, Any]:
    """Set tomorrow's #1 to ``task_id`` (OVERWRITE the single canonical pointer).

    The EOD calls this when the user taps "Set as tomorrow's #1" on a proposed task.
    Re-tapping a DIFFERENT task overwrites the pointer (single canonical pointer,
    never appended). Returns the written document.
    """
    return _write(_build(task_id, title or "", source=source))


def set_none(*, source: str = SOURCE_EOD) -> dict[str, Any]:
    """Record an explicit "no #1 tomorrow" pointer (``task_id: null``).

    Used when the board has no open task to nominate: the standup then shows a clean
    "no #1 set" rather than resurfacing a stale prior-day pointer. This is a
    deliberate dated record, distinct from a missing file (the EOD never ran).
    """
    return _write(_build(None, "", source=source))


def read_pointer() -> dict[str, Any] | None:
    """Return the parsed pointer, or ``None`` when missing/corrupt -- NEVER raises.

    A missing file (the EOD has not run) and a structurally-corrupt one both return
    ``None`` so the standup degrades to "pick a #1" rather than crashing. An explicit
    "none" record (``task_id`` is ``null``) is NOT ``None``: it returns a real dict so
    the caller can tell "the EOD ran and there was nothing to set" (clean board) from
    "the EOD never ran" (missing file).
    """
    path = pointer_path()
    if not path.exists():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(loaded, dict):
        return None
    return loaded


def is_none_pointer(pointer: dict[str, Any] | None) -> bool:
    """True iff ``pointer`` is the explicit "no #1" record (present, ``task_id`` null).

    Distinguishes the three standup states: ``None`` (no file -> EOD never ran),
    ``is_none_pointer`` True (explicit none -> clean board), or a real ``task_id``.
    """
    return pointer is not None and pointer.get("task_id") is None


# --- U8 read side: resolve the pointer to a live board task -------------------
#
# The morning standup (U8) opens with tomorrow's #1. The pointer is the EOD's
# decision; resolving it against the LIVE board is the read side -- a since-completed
# (or dropped/rescheduled-off) pointer must NOT show a dead #1, it degrades to
# "pick one". These statuses are the only contract the standup branches on.

# No pointer file (the EOD never ran) -> standup says "no #1 set -- pick one".
STATUS_NO_POINTER = "no_pointer"
# Explicit ``task_id: null`` record (the EOD ran on an empty board) -> "no #1 set".
STATUS_NONE = "none"
# The pointer's task is still active on the board -> show it as today's #1.
STATUS_ACTIVE = "active"
# The pointer's task is no longer active (done/dropped/rescheduled off) -> degrade
# to "pick a fresh #1" rather than resurfacing a since-completed item.
STATUS_STALE = "stale"


def resolve_to_record(records: Any) -> dict[str, Any]:
    """Resolve the persisted pointer against the LIVE active board -- NEVER raises.

    ``records`` is the parsed work-task record list (``task_records.active_records``
    input). Returns ``{"status", "task_id", "title"}`` where ``status`` is one of the
    four ``STATUS_*`` constants. This is the standup's read side of the daily loop:

    * ``STATUS_NO_POINTER`` -- no pointer file (the EOD never ran).
    * ``STATUS_NONE``       -- explicit "none" pointer (EOD ran, empty board).
    * ``STATUS_ACTIVE``     -- the pointer's task is still active; ``title`` is the
      LIVE board title (not the stale stored one), so a renamed task shows correctly.
    * ``STATUS_STALE``      -- the pointer names a task that is no longer active (it was
      completed / dropped / rescheduled off the board since the EOD set it); the standup
      degrades to "pick a fresh #1" rather than resurfacing a dead #1.

    A corrupt/missing pointer fails toward ``STATUS_NO_POINTER`` (``read_pointer`` already
    never raises). A board that cannot be resolved (no records) falls through to ``STALE``
    for a real pointer -- never a crash and never a confident dead #1.
    """
    pointer = read_pointer()
    if pointer is None:
        return {"status": STATUS_NO_POINTER, "task_id": None, "title": ""}
    if is_none_pointer(pointer):
        return {"status": STATUS_NONE, "task_id": None, "title": ""}

    task_id = pointer.get("task_id")
    try:
        from task_records import active_records

        for record in active_records(records or []):
            if record.canonical_id == task_id:
                # The LIVE title wins over the stored one (a since-renamed task).
                return {"status": STATUS_ACTIVE, "task_id": task_id, "title": record.title}
    except Exception:
        # A board read/parse failure must never crash the standup: treat an
        # unresolvable pointer as stale (the standup prompts for a fresh #1).
        pass

    return {
        "status": STATUS_STALE,
        "task_id": task_id,
        "title": str(pointer.get("title") or ""),
    }
