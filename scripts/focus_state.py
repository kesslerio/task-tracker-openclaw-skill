#!/usr/bin/env python3
"""Sole reader/writer of ``focus-state.json`` (U3 Layer-1 state).

``focus-state.json`` records the morning Daily Top Priorities proposal and its
approval status. It is agent-runtime-owned state under
``cos_config.state_dir()`` (NOT Obsidian / not git-tracked).

Design rules honoured here:

* **Single writer.** Only this module writes ``focus-state.json``; every other
  unit reads it (U6 read-only). All writes go through ``utils._atomic_write``.
* **Stale-date is date-independent for the cap.** ``status_for_today()`` treats a
  state whose ``date != today`` as "not set" so Layer-1 priorities are
  re-proposed each morning. The Layer-2 capacity cap does NOT depend on this
  state at all (it governs total active load), so a skipped morning never
  silences the cap.
* **A corrupt state file never leaks and never silently erases.** A bad file is
  quarantined aside as ``.corrupt-<n>`` (forensics preserved) and treated as
  "no state", mirroring the autonomy_gate quarantine policy.
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

STATUS_PROPOSED = "proposed"
STATUS_APPROVED = "approved"
STATUS_CLOSED = "closed"
STATUS_SKIPPED = "skipped"


def focus_state_path() -> Path:
    return cos_config.state_dir() / "focus-state.json"


def focus_state_lock_path() -> Path:
    return cos_config.state_dir() / "focus-state.lock"


@contextmanager
def _focus_state_flock() -> Iterator[None]:
    """Hold the exclusive sidecar flock over ``focus-state.json`` for the block.

    The ``rev`` bump is a read-on-disk-then-write-plus-one, and v0.4-C uses ``rev``
    as a compare-and-swap token -- so the read-modify-write MUST be serialised across
    processes or two near-simultaneous writers (the morning proposer cron and a user
    ``/approve`` / ``/veto``, which all call ``save_focus_state`` from separate
    processes) could both read ``rev=N`` and both write ``rev=N+1`` with DIFFERENT
    content, making a stale proposal's CAS falsely pass on the coincident ``rev``.
    Mirrors ``outbox._outbox_flock`` / ``nag_state._locked_state``.
    """
    cos_config.state_dir()  # ensure the 0o700 dir exists before opening the lockfile
    lock_path = focus_state_lock_path()
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


def today_str() -> str:
    # Local (Pacific) calendar day, not the container's UTC day: this stamps the
    # focus-state ``date`` and drives the stale-date "re-propose each morning"
    # check, which a UTC day would roll a day early at Pacific evening.
    return cos_config.local_today().isoformat()


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


def load_focus_state() -> dict[str, Any] | None:
    """Return the parsed focus state, or ``None`` when missing/corrupt.

    A structurally-corrupt file (bad JSON, or a non-object document) is renamed
    aside and ``None`` is returned -- the caller treats that as "no approved
    three", which is the safe default. A present-but-unreadable file (perms/IO)
    also returns ``None`` without being clobbered; the next write recreates it.
    """
    path = focus_state_path()
    if not path.exists():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        _rename_corrupt_aside(path)
        return None
    except OSError:
        return None
    if not isinstance(loaded, dict):
        _rename_corrupt_aside(path)
        return None
    return loaded


def _as_int(value: Any, default: int = 0) -> int:
    """``int(value)`` or ``default`` -- never raises on garbage/None."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _current_on_disk_rev() -> int:
    """The ``rev`` currently persisted on disk, or 0 when absent/unreadable.

    Read directly (not via ``load_focus_state``) so a corrupt file is NOT
    quarantined here -- this runs inside ``save_focus_state``, which is about to
    overwrite the file anyway. Any read/parse problem yields 0; the passed-in
    state's own ``rev`` still floors the next value (see ``save_focus_state``), so
    a transient read failure can never make ``rev`` go backwards.
    """
    path = focus_state_path()
    if not path.exists():
        return 0
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return 0
    return _as_int(loaded.get("rev")) if isinstance(loaded, dict) else 0


def save_focus_state(state: dict[str, Any]) -> dict[str, Any]:
    """Atomically persist ``state`` after stamping ``updated_at`` and bumping ``rev``.

    Returns the same dict so callers can chain. The directory + 0o700 perms are
    owned by ``state_dir()``; the file inherits 0o600 from ``_atomic_write`` on
    first write.

    ``rev`` is a **monotonic integer** bumped on every write -- the v0.4 initiation
    CAS token (a counter, not the coarse 1-second ``updated_at`` timestamp). The
    next value floors the passed-in ``rev`` against the value currently ON DISK and
    adds one, so it never regresses even when a caller builds a FRESH state document
    (``new_proposal_state`` carries no ``rev``): the morning re-propose still reads
    the prior day's on-disk ``rev`` and increments past it. The read-on-disk + bump +
    write runs under ``_focus_state_flock`` so concurrent writers (the proposer cron
    vs a user ``/approve``/``/veto``) cannot both mint the same ``rev`` with different
    content. An additive field: a pre-``rev`` file reads as 0 and its first write
    becomes ``rev: 1``.
    """
    state["schema_version"] = SCHEMA_VERSION
    state["updated_at"] = _now_iso()
    with _focus_state_flock():
        state["rev"] = max(_as_int(state.get("rev")), _current_on_disk_rev()) + 1
        _atomic_write(
            focus_state_path(), json.dumps(state, indent=2, sort_keys=True) + "\n")
    return state


def current_rev() -> int | None:
    """The persisted monotonic ``rev``, or ``None`` when no readable state exists.

    The v0.4 initiation CAS snapshots this when the evaluator writes a proposal and
    re-reads it at send time: an advanced ``rev`` means the committed-#1 / priorities
    snapshot moved (a re-propose, an approve, a veto) -> the proposal is stale and is
    suppressed. ``None`` (missing/corrupt state -- the snapshot the proposal was built
    against is gone) is likewise treated as invalid by the CAS (fail-closed). A
    present-but-pre-``rev`` legacy file reads as 0. Uses ``load_focus_state`` so a
    corrupt file is quarantined and surfaced as ``None`` consistently with every reader.
    """
    state = load_focus_state()
    if not isinstance(state, dict):
        return None
    return _as_int(state.get("rev"))


def is_current(state: dict[str, Any] | None, *, reference_date: str | None = None) -> bool:
    """True if ``state`` is for today's date (stale-date check, U3 mustFix #4).

    A state whose ``date`` differs from today is stale: Layer-1 priorities from
    it must be re-proposed, so callers treat a non-current state as "no approved
    three". The Layer-2 cap never consults this -- it is date-independent.
    """
    if not state:
        return False
    ref = reference_date or today_str()
    return state.get("date") == ref


def status_for_today(
    state: dict[str, Any] | None, *, reference_date: str | None = None
) -> str | None:
    """The effective status for today: the stored status only if state is current.

    Returns ``None`` for missing/stale/corrupt state so the cap-approval check in
    ``tasks.py`` and the proposal re-run in ``defended_three.py`` both see a clean
    "needs a fresh proposal" signal without each re-implementing the stale-date
    rule.
    """
    if not is_current(state, reference_date=reference_date):
        return None
    status = state.get("status")
    return status if isinstance(status, str) else None


def new_proposal_state(
    *,
    defended: list[dict[str, Any]],
    holding_tank: list[dict[str, Any]],
    free_hours: float | None,
    total_estimated_minutes: int,
    capacity_ok: bool,
    reference_date: str | None = None,
) -> dict[str, Any]:
    """Build a fresh ``status="proposed"`` focus-state document.

    ``approved_at`` / ``override_reason`` start unset; ``approve`` and the
    override path fill them in. ``holding_tank`` records the agent-proposed
    candidates demoted out of the daily three -- their board entries are
    untouched (no force-evict).
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "date": reference_date or today_str(),
        "status": STATUS_PROPOSED,
        "proposed_at": _now_iso(),
        "approved_at": None,
        "free_hours": free_hours,
        "total_estimated_minutes": total_estimated_minutes,
        "capacity_ok": capacity_ok,
        "override_reason": None,
        "daily_priorities": defended,
        "holding_tank": holding_tank,
        # Task ids the user vetoed today; kept sticky so a later veto in the same
        # chain never re-promotes a task the user already removed.
        "vetoed": [],
    }
