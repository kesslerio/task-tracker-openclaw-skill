#!/usr/bin/env python3
"""H5 quiet-window state: suppress PROACTIVE pushes for a user-set window.

``/quiet <dur>`` is the ADHD attention-budget escape hatch (external review:
"the nag is too aggressive -- alert fatigue"). It sets a deadline in
``quiet-state.json`` (under ``cos_config.state_dir()``); while ``now`` is before
that deadline the nag cron returns early: no nag is proved, gated, or sent, no
loop is opened, AND the resolve/close pass is skipped too (it sends nothing, so
suppressing it is harmless -- open loops simply reconcile on the first cron cycle
after quiet ends). Only the user-initiated ``/nag --list`` read still works:
quiet mutes the PROACTIVE cron, not user-initiated reads.

OWNER-KEYED LEASES (R3): the quiet state is a SET of leases keyed by owner, each
with its own future deadline (``{"leases": {"<owner>": "<iso-deadline>", ...}}``).
The effective mute is the MAX deadline over all live (future) leases, so every
writer sets/releases ONLY its own lease and a concurrent writer's lease is never
lost. The manual ``/quiet`` owns a fixed ``"manual"`` sentinel; a focus block
(H7 ``/start``) owns its ``session_id``. This replaces the old single scalar +
restore-stack: a ``/cancel-session`` just releases its OWN lease, so a shorter or
longer manual ``/quiet`` (its own lease) survives the block's end for free, with
no explicit "restore" step that could clobber a concurrent mute.

This reuses the ``outbox.py`` / ``proactive_state.py`` flock + ``utils._atomic_write``
idiom rather than inventing a new locking scheme. Every read tolerates a
missing/corrupt file and prunes expired leases by reporting NOT quiet when all are
expired: a broken quiet file must FAIL TOWARD nagging, never toward permanent
silence (the same fail-open posture ``nag_state.is_snoozed`` and the outbox use).
``is_quiet`` / ``quiet_until`` therefore NEVER raise.

Legacy migration: an on-disk ``{"quiet_until": "<iso>"}`` (the old scalar) is read
as an implicit ``"manual"`` lease so a live quiet window survives the deploy.
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

# The fixed sentinel owner for a manual ``/quiet`` lease. A focus block owns its
# ``session_id`` instead, so the two never collide and each writer touches only
# its own lease.
MANUAL_OWNER = "manual"


def quiet_state_path() -> Path:
    return cos_config.state_dir() / "quiet-state.json"


def quiet_lock_path() -> Path:
    return cos_config.state_dir() / "quiet-state.lock"


@contextmanager
def _quiet_flock() -> Iterator[None]:
    """Hold the exclusive sidecar flock over ``quiet-state.json`` (the same pattern
    ``outbox._outbox_flock`` / ``nag_state.transition`` use) for the block.

    Both the writers (``set_lease`` / ``release_lease``) and a consistent read
    acquire the lock through HERE, so a read can never see a half-written file and
    two concurrent lease writes cannot lose each other's update -- the whole
    read-modify-write of a lease runs under this lock.
    """
    cos_config.state_dir()  # ensure the 0o700 dir exists before opening the lockfile
    lock_path = quiet_lock_path()
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


def _read_raw() -> dict[str, Any]:
    """Read quiet-state.json, treating a missing/corrupt file as empty (no window).

    A corrupt quiet file must fail toward NOT quiet (nag fires) rather than crash
    the nag run or mute it forever -- a missed quiet window is recoverable (the
    user re-runs ``/quiet``); a silently-muted nag engine is the accountability
    gap the whole unit guards against. Never raises.
    """
    path = quiet_state_path()
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _parse_deadline(value: Any) -> datetime | None:
    """Parse one lease deadline into a tz-aware datetime, or None on garbage.

    A naive timestamp is read as UTC so a hand-edited deadline without an offset
    still compares sanely. Never raises (a garbage deadline is dropped, not fatal).
    """
    if not value:
        return None
    try:
        until = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    return until


def _read_leases(raw: dict[str, Any]) -> dict[str, datetime]:
    """Extract the live (parseable) leases from a raw state dict as owner->deadline.

    Reads BOTH the lease shape (``{"leases": {...}}``) AND the legacy scalar
    (``{"quiet_until": "<iso>"}``, migrated to an implicit ``"manual"`` lease so a
    live window survives the deploy). A garbage owner key or deadline is dropped --
    never raises, fails toward fewer/no leases (toward nagging).
    """
    leases: dict[str, datetime] = {}
    stored = raw.get("leases")
    if isinstance(stored, dict):
        for owner, value in stored.items():
            if not isinstance(owner, str):
                continue
            deadline = _parse_deadline(value)
            if deadline is not None:
                leases[owner] = deadline
    # Legacy scalar migration: an old single ``quiet_until`` is the user's manual
    # lease. Only applied if the new shape did not already carry a manual lease.
    if MANUAL_OWNER not in leases:
        legacy = _parse_deadline(raw.get("quiet_until"))
        if legacy is not None:
            leases[MANUAL_OWNER] = legacy
    return leases


def _live_leases(leases: dict[str, datetime], now: datetime) -> dict[str, datetime]:
    """Keep only leases whose deadline is still in the FUTURE (prune expired)."""
    return {owner: until for owner, until in leases.items() if now < until}


def _write_leases(leases: dict[str, datetime]) -> None:
    """Atomically persist the lease set (owner -> ISO deadline) under the flock.

    Caller holds ``_quiet_flock``. An empty set still writes a well-formed
    ``{"leases": {}}`` (rather than deleting the file) so the path's owner-only
    (``0o600``) mode is preserved and a concurrent reader under the lock always
    sees a clean file.
    """
    payload = {"leases": {owner: until.isoformat() for owner, until in leases.items()}}
    _atomic_write(
        quiet_state_path(),
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
    )


def _resolve_now(now: datetime | None) -> datetime:
    """The reference instant for pruning, defaulting to wall-clock UTC. A caller
    threads its own clock (e.g. ``nag_commands._now()``) so the prune uses the SAME
    instant the lease deadlines were computed against -- never a skewed wall clock
    that could prune a still-future lease."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now


def set_lease(owner: str, until: datetime, *, now: datetime | None = None) -> None:
    """Set/replace ONLY ``owner``'s quiet lease to ``until`` (read-modify-write).

    Runs under the flock: reads the current lease set, prunes expired leases, then
    adds/replaces THIS owner's deadline WITHOUT dropping any other owner's lease --
    so a concurrent writer's lease (a manual ``/quiet`` between a session's read and
    write, or another session's lease) is never lost. A naive ``until`` is stamped
    UTC so the stored value always round-trips to a tz-aware deadline. ``now`` is the
    prune reference (defaults to wall-clock UTC); a caller threads its own clock so a
    still-future peer lease is not pruned by clock skew.
    """
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    ref = _resolve_now(now)
    with _quiet_flock():
        leases = _live_leases(_read_leases(_read_raw()), ref)
        leases[owner] = until
        _write_leases(leases)


def release_lease(owner: str, *, now: datetime | None = None) -> None:
    """Remove ONLY ``owner``'s lease (read-modify-write under the flock).

    Every other owner's lease (the manual ``/quiet`` and any other session's lease)
    remains -- this is what makes "the shorter/longer manual quiet survives a
    session's end" fall out for free, with no explicit restore step. Expired leases
    are pruned in the same pass so the set stays bounded. A no-op if ``owner`` has
    no lease. ``now`` is the prune reference (defaults to wall-clock UTC).
    """
    ref = _resolve_now(now)
    with _quiet_flock():
        leases = _live_leases(_read_leases(_read_raw()), ref)
        leases.pop(owner, None)
        _write_leases(leases)


# --- manual ``/quiet`` shims (Part A back-compat) --------------------------
# ``/quiet`` + ``/unquiet`` (quiet_cli.py) drive the manual lease through these
# thin wrappers, so the manual path keeps working with no behaviour change.

def set_quiet(until: datetime, *, now: datetime | None = None) -> None:
    """Set the manual ``/quiet`` lease (thin shim = ``set_lease("manual", until)``).

    ``now`` is the expired-lease prune reference, threaded through to ``set_lease`` for
    parity with the rest of the lease API (defaults to wall-clock UTC).
    """
    set_lease(MANUAL_OWNER, until, now=now)


def clear_quiet(*, now: datetime | None = None) -> None:
    """Clear the manual ``/quiet`` lease (thin shim = ``release_lease("manual")``).

    Releases ONLY the manual lease -- a focus block's session lease is untouched.
    ``now`` is the prune reference threaded through to ``release_lease`` (defaults to
    wall-clock UTC): without it, releasing the manual lease prunes every OTHER still-
    live-at-``now`` session lease against real wall-clock, which a caller replaying a
    fixed clock (a test, or a deterministic batch) must be able to pin.
    """
    release_lease(MANUAL_OWNER, now=now)


def quiet_until(now: datetime | None = None) -> datetime | None:
    """The effective quiet deadline -- the MAX over all live (future) leases -- else None.

    Reads under the flock for a consistent snapshot, prunes expired leases, and
    returns the latest deadline still in effect (for display + the cron footer). All
    leases expired / missing / corrupt yields None. Never raises.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    with _quiet_flock():
        leases = _live_leases(_read_leases(_read_raw()), now)
    return max(leases.values()) if leases else None


def is_quiet(now: datetime) -> bool:
    """Is ``now`` within an active quiet window (any live lease)?

    True only when at least one lease's deadline is still in the FUTURE. A
    missing/corrupt file or all-expired leases is NOT quiet (fail toward nagging).
    Never raises.
    """
    return quiet_until(now) is not None
