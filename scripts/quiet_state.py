#!/usr/bin/env python3
"""H5 quiet-window state: suppress PROACTIVE pushes for a user-set window.

``/quiet <dur>`` is the ADHD attention-budget escape hatch (external review:
"the nag is too aggressive -- alert fatigue"). It sets a deadline in
``quiet-state.json`` (under ``cos_config.state_dir()``); while ``now`` is before
that deadline the nag cron suppresses every PROACTIVE push -- no nag is proved,
gated, or sent, and no loop is opened. The read-only RESOLVE pass (which sends
nothing) and the user-initiated ``/nag --list`` read still work: quiet mutes
PROACTIVE output, not user-initiated reads.

A single small flag (``quiet_until``) -- so this reuses the ``outbox.py`` /
``proactive_state.py`` flock + ``utils._atomic_write`` idiom rather than inventing
a new locking scheme. Every read tolerates a missing/corrupt file and an expired
window by reporting NOT quiet: a broken quiet file must FAIL TOWARD nagging, never
toward permanent silence (the same fail-open posture ``nag_state.is_snoozed`` and
the outbox use). ``is_quiet`` / ``quiet_until`` therefore NEVER raise.
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


def quiet_state_path() -> Path:
    return cos_config.state_dir() / "quiet-state.json"


def quiet_lock_path() -> Path:
    return cos_config.state_dir() / "quiet-state.lock"


@contextmanager
def _quiet_flock() -> Iterator[None]:
    """Hold the exclusive sidecar flock over ``quiet-state.json`` (the same pattern
    ``outbox._outbox_flock`` / ``nag_state.transition`` use) for the block.

    Both the writers (``set_quiet`` / ``clear_quiet``) and a consistent read
    acquire the lock through HERE, so a read can never see a half-written file and
    two concurrent ``/quiet`` writes cannot lose each other's update.
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


def _parse_until(raw: dict[str, Any]) -> datetime | None:
    """Extract a tz-aware ``quiet_until`` from a raw state dict, or None.

    A missing/garbage value yields None (not quiet) -- never raises. A naive
    timestamp is read as UTC so a hand-edited window without an offset still
    compares sanely.
    """
    value = raw.get("quiet_until")
    if not value:
        return None
    try:
        until = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    return until


def set_quiet(until: datetime) -> None:
    """Set the quiet window deadline (under the flock, atomic write).

    Stores ``until`` as an ISO timestamp. A naive ``until`` is stamped UTC so the
    stored value always round-trips to a tz-aware deadline.
    """
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    with _quiet_flock():
        _atomic_write(
            quiet_state_path(),
            json.dumps({"quiet_until": until.isoformat()}, indent=2, sort_keys=True) + "\n",
        )


def clear_quiet() -> None:
    """Clear any quiet window (under the flock).

    Writes an empty state rather than deleting the file so the path's owner-only
    (``0o600``) mode is preserved by ``_atomic_write`` and a concurrent reader
    under the lock always sees a well-formed (empty) file.
    """
    with _quiet_flock():
        _atomic_write(quiet_state_path(), json.dumps({}, indent=2, sort_keys=True) + "\n")


def quiet_until(now: datetime | None = None) -> datetime | None:
    """The active quiet deadline if one is in the FUTURE, else None (for display).

    Reads under the flock for a consistent snapshot. An expired or missing/corrupt
    window yields None. Never raises.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    with _quiet_flock():
        until = _parse_until(_read_raw())
    if until is None:
        return None
    return until if now < until else None


def is_quiet(now: datetime) -> bool:
    """Is ``now`` within an active quiet window?

    True only when a valid, future ``quiet_until`` is stored. A missing/corrupt
    file or an expired window is NOT quiet (fail toward nagging). Never raises.
    """
    return quiet_until(now) is not None
