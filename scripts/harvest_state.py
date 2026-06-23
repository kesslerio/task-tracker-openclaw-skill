#!/usr/bin/env python3
"""Sole reader/writer of ``harvest-state.json`` (U5 accomplishment-ledger state).

``harvest-state.json`` records one harvest window: which evidence has been seen
(so a heartbeat re-fire never re-ingests the same PR/email), the proven delivery
target the draft was pushed to, and which task ids are pending vs approved on the
open approval loop.

Design rules (mirroring ``focus_state.py`` so no unit invents its own variant):

* **Single writer.** Only this module writes ``harvest-state.json``; all writes go
  through ``utils._atomic_write``.
* **A corrupt state file never leaks and never silently erases.** A bad file is
  quarantined aside as ``.corrupt-<n>`` (forensics preserved) and treated as "no
  state", mirroring the autonomy_gate / focus_state quarantine policy.
* **Weekly reset is detected, never silent.** ``weekly_reset_check`` compares the
  stored ISO-week id against the current one; expired (unapproved) pending items
  are returned to the caller to be surfaced, never dropped on the floor.
"""

from __future__ import annotations

import json
import os
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None

import cos_config
from utils import _atomic_write

SCHEMA_VERSION = 1

WINDOW_WEEK = "week"
WINDOW_24H = "24h"
WINDOW_STANDUP = "standup"


def harvest_state_path(window: str = WINDOW_WEEK) -> Path:
    """The state file for a window kind.

    The weekly approval loop, on-demand 24h ``/done`` loop, and explicit standup
    evidence window are independent and MUST NOT share one file -- one window
    kind would otherwise clobber another's ``pending_task_ids`` / ``seen_hashes``
    and silently break ``/approve`` (spec §3.1: the 24h window "does not reset the
    weekly state"). The weekly file keeps the canonical name; other files are
    suffixed.
    """
    if window == WINDOW_WEEK:
        name = "harvest-state.json"
    elif window == WINDOW_STANDUP:
        name = "harvest-state-standup.json"
    else:
        name = "harvest-state-24h.json"
    return cos_config.state_dir() / name


def harvest_state_lock_path(window: str = WINDOW_WEEK) -> Path:
    if window == WINDOW_WEEK:
        name = "harvest-state.lock"
    elif window == WINDOW_STANDUP:
        name = "harvest-state-standup.lock"
    else:
        name = "harvest-state-24h.lock"
    return cos_config.state_dir() / name


def new_run_id() -> str:
    return str(uuid.uuid4())


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def iso_week_id(reference: date | None = None) -> str:
    """The ISO-week id (e.g. ``2026-W25``) that scopes the weekly harvest window."""
    ref = reference or cos_config.local_today()
    iso_year, iso_week, _ = ref.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def window_id(window: str, reference: date | None = None) -> str:
    """The harvest-window id for a window kind.

    The weekly window resets per ISO week (``2026-W25``); the 24h ``/done`` window
    is dated (``2026-06-20-24h``) and never resets the weekly state.
    """
    ref = reference or cos_config.local_today()
    if window == WINDOW_24H:
        return f"{ref.isoformat()}-24h"
    return iso_week_id(ref)


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


@contextmanager
def harvest_state_flock(window: str = WINDOW_WEEK) -> Iterator[None]:
    """Hold the exclusive sidecar flock over one harvest-state file."""
    cos_config.state_dir()
    lock_path = harvest_state_lock_path(window)
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


def _load_state_unlocked(window: str = WINDOW_WEEK) -> dict[str, Any] | None:
    path = harvest_state_path(window)
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


def load_state(window: str = WINDOW_WEEK) -> dict[str, Any] | None:
    """Return the parsed harvest state for ``window``, or ``None`` when missing/corrupt.

    A structurally-corrupt file (bad JSON, or a non-object document) is renamed
    aside and ``None`` is returned. A present-but-unreadable file (perms/IO) also
    returns ``None`` without being clobbered; the next write recreates it.
    """
    with harvest_state_flock(window):
        return _load_state_unlocked(window)


def _write_state_unlocked(state: dict[str, Any], window: str = WINDOW_WEEK) -> dict[str, Any]:
    """Atomically persist ``state`` for ``window`` (stamping schema/updated_at)."""
    state["schema_version"] = SCHEMA_VERSION
    state["updated_at"] = _now_iso()
    _atomic_write(harvest_state_path(window), json.dumps(state, indent=2, sort_keys=True) + "\n")
    return state


def _merge_unique(existing: list[Any], incoming: list[Any]) -> list[Any]:
    merged = list(existing)
    known = set(json.dumps(item, sort_keys=True) if isinstance(item, dict) else item for item in merged)
    for item in incoming:
        key = json.dumps(item, sort_keys=True) if isinstance(item, dict) else item
        if key not in known:
            merged.append(item)
            known.add(key)
    return merged


def _max_iso(left: Any, right: Any) -> Any:
    if left in (None, ""):
        return right
    if right in (None, ""):
        return left
    left_s = str(left)
    right_s = str(right)
    try:
        left_dt = datetime.fromisoformat(left_s.replace("Z", "+00:00"))
        right_dt = datetime.fromisoformat(right_s.replace("Z", "+00:00"))
        return left if left_dt >= right_dt else right
    except (TypeError, ValueError):
        return max(left_s, right_s)


def _merge_state(existing: dict[str, Any] | None, incoming: dict[str, Any]) -> dict[str, Any]:
    if existing is None:
        return incoming
    if existing.get("harvest_window_id") != incoming.get("harvest_window_id"):
        existing_id = existing.get("harvest_window_id")
        incoming_id = incoming.get("harvest_window_id")
        if existing_id in (None, ""):
            return incoming
        if incoming_id in (None, ""):
            return existing
        return existing if str(existing_id) > str(incoming_id) else incoming

    merged = {**existing, **incoming}
    for key in ("auto_pushed_window", "reactive_pushed_window", "draft_pushed_at", "delivery_target"):
        if incoming.get(key) is None and existing.get(key) is not None:
            merged[key] = existing[key]
    for key in ("seen_hashes", "pending_task_ids", "approved_task_ids", "rejected_candidate_ids"):
        merged[key] = _merge_unique(existing.get(key) or [], incoming.get(key) or [])

    approved = set(merged.get("approved_task_ids") or [])
    merged["pending_task_ids"] = [tid for tid in (merged.get("pending_task_ids") or []) if tid not in approved]
    merged["pending_matches"] = {
        **(existing.get("pending_matches") or {}),
        **(incoming.get("pending_matches") or {}),
    }
    merged["pending_matches"] = {
        tid: match for tid, match in merged["pending_matches"].items() if tid not in approved
    }
    # Within a matched window provider_state converges, so incoming wins on key conflict.
    merged["seen_provider_states"] = {
        **(existing.get("seen_provider_states") or {}),
        **(incoming.get("seen_provider_states") or {}),
    }
    watermarks = dict(existing.get("watermarks") or {})
    for source, value in (incoming.get("watermarks") or {}).items():
        watermarks[source] = _max_iso(watermarks.get(source), value)
    merged["watermarks"] = watermarks
    return merged


def save_state(state: dict[str, Any], window: str = WINDOW_WEEK) -> dict[str, Any]:
    """Atomically merge and persist ``state`` for ``window`` under the sidecar lock."""
    with harvest_state_flock(window):
        merged = _merge_state(_load_state_unlocked(window), state)
        return _write_state_unlocked(merged, window)


def new_window_state(harvest_window_id: str, *, run_id: str | None = None) -> dict[str, Any]:
    """Build a fresh harvest-window document (no draft pushed yet).

    The scheduled Friday digest (``auto``) and a reactive ``/ledger`` pull dedup
    INDEPENDENTLY -- each records the window id it last pushed in -- so a mid-week
    reactive run can never silently suppress the headline weekly Friday digest.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "harvest_window_id": harvest_window_id,
        "run_id": run_id or new_run_id(),
        "auto_pushed_window": None,
        "reactive_pushed_window": None,
        "draft_pushed_at": None,
        "delivery_target": None,
        "watermarks": {},
        "pending_task_ids": [],
        "pending_matches": {},
        "approved_task_ids": [],
        "rejected_candidate_ids": [],
        "seen_hashes": [],
        "seen_provider_states": {},
        "updated_at": _now_iso(),
    }


def load_or_reset(harvest_window_id: str, window: str = WINDOW_WEEK) -> tuple[dict[str, Any], list[str]]:
    """Return the live window state, resetting it when a new ISO week began.

    Reads/writes the per-``window`` state file, so the weekly approval loop and
    the 24h ``/done`` loop never clobber each other.

    Returns ``(state, expired_task_ids)``. When the stored window id differs from
    ``harvest_window_id`` a fresh window is started; any ``pending_task_ids`` that
    were never approved in the old window are returned as ``expired_task_ids`` so
    the caller can surface them (NAG-CLOSES-ONLY-ON-ACK: expired items are
    resurfaced, never silently dropped). A matching window id keeps the existing
    state and returns no expired ids.
    """
    with harvest_state_flock(window):
        return _load_or_reset_unlocked(harvest_window_id, window)


def _load_or_reset_unlocked(harvest_window_id: str, window: str = WINDOW_WEEK) -> tuple[dict[str, Any], list[str]]:
    stored = _load_state_unlocked(window)
    if stored is not None and stored.get("harvest_window_id") == harvest_window_id:
        stored.setdefault("run_id", new_run_id())
        stored.setdefault("watermarks", {})
        stored.setdefault("seen_provider_states", {})
        return stored, []
    expired: list[str] = []
    if stored is not None:
        pending = stored.get("pending_task_ids") or []
        approved = set(stored.get("approved_task_ids") or [])
        expired = [tid for tid in pending if tid not in approved]
    return new_window_state(harvest_window_id), expired


def update_window_state(
    harvest_window_id: str,
    mutator: Callable[[dict[str, Any]], None],
    *,
    window: str = WINDOW_WEEK,
) -> tuple[dict[str, Any], list[str]]:
    """Run one locked read-modify-write for a harvest window."""
    with harvest_state_flock(window):
        state, expired = _load_or_reset_unlocked(harvest_window_id, window)
        mutator(state)
        return _write_state_unlocked(state, window), expired


def is_seen(state: dict[str, Any], evidence_hash: str, provider_state: str | None = None) -> bool:
    if provider_state is None:
        return evidence_hash in set(state.get("seen_hashes") or [])
    seen_states = state.get("seen_provider_states") or {}
    return seen_states.get(evidence_hash) == provider_state


def _hash_and_state(item: str | dict[str, Any], provider_state: str | None) -> tuple[str, str | None]:
    if isinstance(item, dict):
        return str(item["evidence_hash"]), item.get("provider_state")
    return str(item), provider_state


def mark_seen(
    state: dict[str, Any],
    evidence_hashes: list[str] | list[dict[str, Any]],
    *,
    provider_state: str | None = None,
) -> dict[str, Any]:
    """Add evidence hashes to the dedup set (idempotent, order-stable)."""
    seen = list(state.get("seen_hashes") or [])
    known = set(seen)
    seen_states = dict(state.get("seen_provider_states") or {})
    for item in evidence_hashes:
        h, item_state = _hash_and_state(item, provider_state)
        if h not in known:
            seen.append(h)
            known.add(h)
        if item_state is not None and str(item_state).strip():
            seen_states[h] = str(item_state)
    state["seen_hashes"] = seen
    state["seen_provider_states"] = seen_states
    return state


def mark_watermark(state: dict[str, Any], source: str, watermark: str) -> dict[str, Any]:
    watermarks = dict(state.get("watermarks") or {})
    watermarks[source] = _max_iso(watermarks.get(source), watermark)
    state["watermarks"] = watermarks
    return state
