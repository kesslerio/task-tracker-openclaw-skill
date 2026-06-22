#!/usr/bin/env python3
"""H4 machine-visible health substrate: ``cos-health.json``.

Every ritual runs inside ``error_envelope`` which prints a friendly notice and
exits 0 (so the cron relay never sees a non-zero status). Good UX, but it means a
silently-broken cron looks healthy -- there is no machine-readable signal an
external watchdog can poll. This module is that signal: a small, flocked, atomic
map under ``cos_config.state_dir()`` recording, per ritual, when it last SUCCEEDED
and when it last FAILED (with the classified ``error_class``).

The file is a flat ``{ritual: {...}}`` dict. ``record_success`` / ``record_failure``
each run one read-modify-write under an exclusive sidecar flock -- the SAME idiom
``outbox._outbox_flock`` / ``nag_state`` use -- so a cron run and a reactive command
writing concurrently cannot lose each other's updates. ``_atomic_write`` (temp +
replace + fsync) keeps a torn write from corrupting the file mid-heartbeat.

Best-effort by contract: health recording is wired into the envelope's failure
path, so it must NEVER become a new failure source. ``read_health`` treats a
missing/corrupt file as ``{}`` and never raises; the recorders are wrapped
defensively at the call site (``error_envelope.run_main``). A success NEVER clobbers
a prior ``last_failure`` and a failure NEVER clobbers a prior ``last_success_ts`` --
the two facts coexist so a watchdog sees both "last good run" and "last bad run".

Timestamps are local-zone ISO via ``cos_config.local_now()`` (the same clock the
rituals/cron reason in), so a STALE-age comparison against ``local_now()`` in
``cos_manifest`` is apples-to-apples.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None

import cos_config
from utils import _atomic_write


def health_path() -> Path:
    return cos_config.state_dir() / "cos-health.json"


def health_lock_path() -> Path:
    return cos_config.state_dir() / "cos-health.lock"


def _now_iso() -> str:
    """Local-zone ISO timestamp (matches the clock rituals/cron reason in)."""
    return cos_config.local_now().isoformat()


def _read_health() -> dict[str, Any]:
    """Read cos-health.json, treating a missing/corrupt file as empty.

    A corrupt health file must NEVER crash a ritual -- the worst case is a watchdog
    momentarily sees an empty map; the next clean write rebuilds the file. Mirrors
    ``outbox._read_outbox``'s fail-soft read.
    """
    path = health_path()
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _write_health(state: dict[str, Any]) -> None:
    _atomic_write(health_path(), json.dumps(state, indent=2, sort_keys=True) + "\n")


@contextmanager
def _health_flock() -> Iterator[None]:
    """Hold the exclusive sidecar flock over ``cos-health.json`` for the block.

    The same sidecar-lockfile pattern ``outbox._outbox_flock`` uses: both recorders
    serialise their read-modify-write here so a concurrent cron + reactive write
    cannot lose an entry or read a half-written file.
    """
    cos_config.state_dir()  # ensure the 0o700 dir exists before opening the lockfile
    lock_path = health_lock_path()
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


def _entry(state: dict[str, Any], ritual: str) -> dict[str, Any]:
    """Get-or-create the per-ritual dict, tolerating a non-dict legacy value."""
    entry = state.get(ritual)
    if not isinstance(entry, dict):
        entry = {}
        state[ritual] = entry
    return entry


def record_success(ritual: str) -> None:
    """Stamp ``ritual``'s ``last_success_ts`` = now (local ISO).

    Does NOT touch ``last_failure`` / ``last_failure_ts``: a clean run records that a
    good run happened WITHOUT erasing the record of the last bad one, so a watchdog
    can still see a recent failure followed by a recovery.
    """
    with _health_flock():
        state = _read_health()
        _entry(state, ritual)["last_success_ts"] = _now_iso()
        _write_health(state)


def record_failure(ritual: str, *, error_class: str, trigger: str | None = None) -> None:
    """Record ``ritual``'s most recent failure (class + ts + trigger).

    Does NOT clobber ``last_success_ts``: the last good run and the last bad run are
    independent facts a watchdog needs both of. ``last_failure_ts`` is mirrored at the
    top level of the entry so a poller can age the failure without descending into the
    nested object.
    """
    ts = _now_iso()
    with _health_flock():
        state = _read_health()
        entry = _entry(state, ritual)
        entry["last_failure"] = {"error_class": error_class, "ts": ts, "trigger": trigger}
        entry["last_failure_ts"] = ts
        _write_health(state)


def read_health() -> dict[str, Any]:
    """The current per-ritual health map (missing/corrupt file -> ``{}``; never raises)."""
    with _health_flock():
        return _read_health()
