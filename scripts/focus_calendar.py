#!/usr/bin/env python3
"""Sole reader/writer of ``focus-calendar.json`` (U6 calendar-block state).

``focus-calendar.json`` records the agent-owned "Task Focus" calendar id and the
active focus blocks the agent has placed on it (spec §3.1). It is
agent-runtime-owned state under ``cos_config.state_dir()`` -- NOT Obsidian, not
git-tracked.

Design rules (mirrors focus_state.py so no unit invents its own variant):

* **Single writer.** Only this module writes ``focus-calendar.json``; all writes
  go through ``utils._atomic_write`` (crash-safe).
* **A corrupt state file never leaks and never silently erases.** A bad file is
  quarantined aside as ``.corrupt-<n>`` (forensics preserved) and treated as
  "no blocks", mirroring the autonomy_gate / focus_state quarantine policy. A
  torn read therefore never blocks the next cron fire.
* **U6 owns ONLY this file.** It reads ``focus-state.json`` (U3) read-only; it
  never writes it.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cos_config
from utils import _atomic_write

SCHEMA_VERSION = 1
# Keep the stored dry-run history bounded so the state file cannot grow without
# limit on a long-lived host -- the last N writes are enough for an undo audit.
MAX_DRY_RUN_HISTORY = 50


def focus_calendar_path() -> Path:
    return cos_config.state_dir() / "focus-calendar.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def _empty_state(calendar_id: str | None = None, calendar_name: str | None = None) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "agent_calendar_id": calendar_id,
        "agent_calendar_name": calendar_name,
        "created_at": _now_iso(),
        "active_blocks": [],
        "dry_run_history": [],
    }


def load_focus_calendar() -> dict[str, Any]:
    """Return the parsed focus-calendar state, or a fresh empty state.

    A missing file yields an empty state (no quarantine). A structurally-corrupt
    file (bad JSON or non-object) is renamed aside and an empty state returned --
    the safe default. A present-but-unreadable file (perms/IO) also returns an
    empty state without being clobbered; the next write recreates it. Either way
    a torn read never raises, so the next ``*/5`` cron fire always runs.
    """
    path = focus_calendar_path()
    if not path.exists():
        return _empty_state()
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        _rename_corrupt_aside(path)
        return _empty_state()
    except OSError:
        return _empty_state()
    if not isinstance(loaded, dict):
        _rename_corrupt_aside(path)
        return _empty_state()
    # Defensive shape normalisation: a state file hand-edited to drop a key must
    # not crash a later append. The list fields default to empty.
    loaded.setdefault("active_blocks", [])
    loaded.setdefault("dry_run_history", [])
    return loaded


def save_focus_calendar(state: dict[str, Any]) -> dict[str, Any]:
    """Atomically persist ``state`` after stamping ``updated_at``.

    Returns the same dict so callers can chain. ``dry_run_history`` is trimmed to
    ``MAX_DRY_RUN_HISTORY`` so the file stays bounded.
    """
    state["schema_version"] = SCHEMA_VERSION
    history = state.get("dry_run_history") or []
    if len(history) > MAX_DRY_RUN_HISTORY:
        state["dry_run_history"] = history[-MAX_DRY_RUN_HISTORY:]
    state["updated_at"] = _now_iso()
    _atomic_write(focus_calendar_path(), json.dumps(state, indent=2, sort_keys=True) + "\n")
    return state


def record_dry_run(state: dict[str, Any], op: str, request: dict[str, Any], result: dict[str, Any]) -> None:
    """Append a dry-run payload to the in-memory state (REVERSIBILITY substrate).

    The caller persists with ``save_focus_calendar``; this only mutates the dict
    so a single atomic write captures the block change AND its dry-run record.
    """
    state.setdefault("dry_run_history", []).append(
        {"timestamp": _now_iso(), "op": op, "request": request, "result": result}
    )


def find_block(state: dict[str, Any], event_id: str) -> dict[str, Any] | None:
    """Return the active block with ``event_id``, or None."""
    for block in state.get("active_blocks", []):
        if block.get("event_id") == event_id:
            return block
    return None


def block_for_task(state: dict[str, Any], task_id: str) -> dict[str, Any] | None:
    """Return the active block for ``task_id``, or None."""
    for block in state.get("active_blocks", []):
        if block.get("task_id") == task_id:
            return block
    return None
