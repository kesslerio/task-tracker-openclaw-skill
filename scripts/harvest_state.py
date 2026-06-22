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
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import cos_config
from utils import _atomic_write

SCHEMA_VERSION = 1

WINDOW_WEEK = "week"
WINDOW_24H = "24h"


def harvest_state_path(window: str = WINDOW_WEEK) -> Path:
    """The state file for a window kind.

    The weekly approval loop and the on-demand 24h ``/done`` loop are independent
    and MUST NOT share one file -- a 24h run would otherwise clobber the weekly
    window's ``pending_task_ids`` / ``seen_hashes`` and silently break ``/approve``
    (spec §3.1: the 24h window "does not reset the weekly state"). The weekly file
    keeps the canonical name; the 24h file is suffixed.
    """
    name = "harvest-state.json" if window == WINDOW_WEEK else "harvest-state-24h.json"
    return cos_config.state_dir() / name


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


def load_state(window: str = WINDOW_WEEK) -> dict[str, Any] | None:
    """Return the parsed harvest state for ``window``, or ``None`` when missing/corrupt.

    A structurally-corrupt file (bad JSON, or a non-object document) is renamed
    aside and ``None`` is returned. A present-but-unreadable file (perms/IO) also
    returns ``None`` without being clobbered; the next write recreates it.
    """
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


def save_state(state: dict[str, Any], window: str = WINDOW_WEEK) -> dict[str, Any]:
    """Atomically persist ``state`` for ``window`` (stamping schema/updated_at)."""
    state["schema_version"] = SCHEMA_VERSION
    state["updated_at"] = _now_iso()
    _atomic_write(harvest_state_path(window), json.dumps(state, indent=2, sort_keys=True) + "\n")
    return state


def new_window_state(harvest_window_id: str) -> dict[str, Any]:
    """Build a fresh harvest-window document (no draft pushed yet).

    The scheduled Friday digest (``auto``) and a reactive ``/ledger`` pull dedup
    INDEPENDENTLY -- each records the window id it last pushed in -- so a mid-week
    reactive run can never silently suppress the headline weekly Friday digest.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "harvest_window_id": harvest_window_id,
        "auto_pushed_window": None,
        "reactive_pushed_window": None,
        "draft_pushed_at": None,
        "delivery_target": None,
        "pending_task_ids": [],
        "pending_matches": {},
        "approved_task_ids": [],
        "rejected_candidate_ids": [],
        "seen_hashes": [],
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
    stored = load_state(window)
    if stored is not None and stored.get("harvest_window_id") == harvest_window_id:
        return stored, []
    expired: list[str] = []
    if stored is not None:
        pending = stored.get("pending_task_ids") or []
        approved = set(stored.get("approved_task_ids") or [])
        expired = [tid for tid in pending if tid not in approved]
    return new_window_state(harvest_window_id), expired


def is_seen(state: dict[str, Any], evidence_hash: str) -> bool:
    return evidence_hash in set(state.get("seen_hashes") or [])


def mark_seen(state: dict[str, Any], evidence_hashes: list[str]) -> dict[str, Any]:
    """Add evidence hashes to the dedup set (idempotent, order-stable)."""
    seen = list(state.get("seen_hashes") or [])
    known = set(seen)
    for h in evidence_hashes:
        if h not in known:
            seen.append(h)
            known.add(h)
    state["seen_hashes"] = seen
    return state
