#!/usr/bin/env python3
"""Chief-of-Staff Phase 0a shared configuration.

Single home for the env-with-default tunables (Contract 6 / Decision #7) and the
state directory every Chief-of-Staff state file lives under. One module so no
unit invents its own variant of a knob.
"""

from __future__ import annotations

import os
from pathlib import Path


def _int_env(name: str, default: int) -> int:
    """Read an int env var, falling back to ``default`` on unset/empty/garbage.

    A misconfigured knob must never crash a ritual; an unparseable value degrades
    to the documented default rather than raising.
    """
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


# --- Focus / capacity knobs (Contract 6, Decision #7) ---------------------

def daily_priority_count() -> int:
    """How many must-do-today priorities the morning proposal surfaces."""
    return _int_env("DAILY_PRIORITY_COUNT", 3)


def weekly_capacity_hours() -> int:
    """Active-inventory cap ceiling: ~one week of capacity in hours."""
    return _int_env("WEEKLY_CAPACITY_HOURS", 25)


def unestimated_task_hours() -> int:
    """Hours an active task with no ``estimate::`` is counted at for the cap."""
    return _int_env("UNESTIMATED_TASK_HOURS", 2)


def active_task_hard_cap() -> int:
    """Count safety-valve on the active set for sparsely-estimated boards."""
    return _int_env("ACTIVE_TASK_HARD_CAP", 20)


# --- Undo windows (Decision #8) -------------------------------------------

def undo_window_nag_hours() -> int:
    """How long a nag act stays reversible via /undo."""
    return _int_env("UNDO_WINDOW_NAG_HOURS", 4)


def undo_window_board_hours() -> int:
    """How long a board mutation stays reversible via /undo (default 7d)."""
    return _int_env("UNDO_WINDOW_BOARD_HOURS", 168)


# --- State directory -------------------------------------------------------

def state_dir() -> Path:
    """Resolve and create the Chief-of-Staff state directory.

    Defaults to ``~/.lobster/state/task-mgmt`` (Contract 3/4 + Option A). The
    ``TASK_MGMT_STATE_DIR`` override exists for tests and alternate hosts; it is
    never hardcoded to a different path elsewhere.

    Per project security policy the directory is owner-only (``0o700``): it holds
    the autonomy log + nag state + autonomy config, none of which should be
    group/world readable. The chmod is applied on every resolve so a dir created
    before this policy is tightened on next use.
    """
    raw = os.getenv("TASK_MGMT_STATE_DIR")
    base = Path(raw).expanduser() if raw else Path.home() / ".lobster" / "state" / "task-mgmt"
    base.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(base, 0o700)
    except OSError:
        pass
    return base
