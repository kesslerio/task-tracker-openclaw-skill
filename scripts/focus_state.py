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
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import cos_config
from utils import _atomic_write

SCHEMA_VERSION = 1

STATUS_PROPOSED = "proposed"
STATUS_APPROVED = "approved"
STATUS_CLOSED = "closed"
STATUS_SKIPPED = "skipped"


def focus_state_path() -> Path:
    return cos_config.state_dir() / "focus-state.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def today_str() -> str:
    return date.today().isoformat()


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


def save_focus_state(state: dict[str, Any]) -> dict[str, Any]:
    """Atomically persist ``state`` after stamping ``updated_at``.

    Returns the same dict so callers can chain. The directory + 0o700 perms are
    owned by ``state_dir()``; the file inherits 0o600 from ``_atomic_write`` on
    first write.
    """
    state["schema_version"] = SCHEMA_VERSION
    state["updated_at"] = _now_iso()
    _atomic_write(focus_state_path(), json.dumps(state, indent=2, sort_keys=True) + "\n")
    return state


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
