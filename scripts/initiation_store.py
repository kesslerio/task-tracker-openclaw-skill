#!/usr/bin/env python3
"""v0.4-C initiation proposal store: the sidecar where the evaluator parks an
expiring ``Proposal`` for the dispatcher to pick up.

One JSON file (``initiation-proposals.json``) keyed by ``focus_episode_id`` -- at
most one PENDING proposal per episode slot at a time; a later stage (a re-nudge)
supersedes the earlier one for that slot. Reads/writes go through an exclusive
sidecar flock + ``utils._atomic_write``, the SAME pattern ``outbox`` and
``nag_state.transition`` use, so a concurrent evaluator run and dispatcher read
cannot see a half-written file or lose an entry. Expired entries are dropped on
every read and write so the file stays flat.

This module persists; it never sends and never evaluates. ``Proposal`` validity at
SEND time is the dispatcher's job (``initiation_contract.cas_still_valid``); expiry
here is the coarse "this proposal is simply too old to act on" backstop.
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

from cos_config import state_dir
from initiation_contract import Proposal
from utils import _atomic_write


def store_path() -> Path:
    return state_dir() / "initiation-proposals.json"


def store_lock_path() -> Path:
    return state_dir() / "initiation-proposals.lock"


def _now() -> datetime:
    return datetime.now(timezone.utc)


@contextmanager
def _store_flock() -> Iterator[None]:
    """Hold the exclusive sidecar flock over ``initiation-proposals.json`` for the
    block (mirrors ``outbox._outbox_flock``)."""
    state_dir()  # ensure the 0o700 dir exists before opening the lockfile
    lock_path = store_lock_path()
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


def _read() -> dict[str, Any]:
    """Read the store, treating a missing/corrupt file as empty.

    A corrupt store fails toward "no pending proposal" (no send) rather than
    crashing the dispatcher -- a missed nudge is recoverable; a crashed cron is a
    silent gap. The next clean write rebuilds the file.
    """
    path = store_path()
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _write(state: dict[str, Any]) -> None:
    _atomic_write(store_path(), json.dumps(state, indent=2, sort_keys=True) + "\n")


def _is_expired(entry: Any, now: datetime) -> bool:
    """True if a stored proposal dict is expired (or unparseable -> drop it)."""
    if not isinstance(entry, dict):
        return True
    try:
        return Proposal.from_dict(entry).is_expired(now=now)
    except (TypeError, ValueError):
        return True


def _prune(state: dict[str, Any], now: datetime) -> None:
    for key in [k for k, v in state.items() if _is_expired(v, now)]:
        del state[key]


def write_proposal(proposal: Proposal, *, now: datetime | None = None) -> None:
    """Persist ``proposal`` under its ``focus_episode_id`` (supersedes any pending
    proposal for that slot), pruning expired entries in the same write."""
    ref = now or _now()
    with _store_flock():
        state = _read()
        _prune(state, ref)
        state[proposal.focus_episode_id] = proposal.to_dict()
        _write(state)


def read_proposal(
    focus_episode_id: str, *, now: datetime | None = None
) -> Proposal | None:
    """The pending, non-expired ``Proposal`` for ``focus_episode_id``, or ``None``.

    Expired or unparseable entries return ``None`` (and are pruned from the file on
    this read), so the dispatcher never acts on a stale proposal.
    """
    ref = now or _now()
    with _store_flock():
        state = _read()
        had = len(state)
        _prune(state, ref)
        if len(state) != had:
            _write(state)  # persist the prune so the file stays flat
        entry = state.get(focus_episode_id)
        if not isinstance(entry, dict):
            return None
        try:
            return Proposal.from_dict(entry)
        except (TypeError, ValueError):
            return None


def clear_proposal(focus_episode_id: str) -> None:
    """Drop the pending proposal for ``focus_episode_id`` (a no-op if absent).

    The dispatcher clears a proposal once it has been delivered (or definitively
    abandoned) so a later cron tick does not re-evaluate a spent slot.
    """
    with _store_flock():
        state = _read()
        if focus_episode_id in state:
            del state[focus_episode_id]
            _write(state)
