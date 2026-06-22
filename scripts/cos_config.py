#!/usr/bin/env python3
"""Chief-of-Staff Phase 0a shared configuration.

Single home for the env-with-default tunables (Contract 6 / Decision #7) and the
state directory every Chief-of-Staff state file lives under. One module so no
unit invents its own variant of a knob.
"""

from __future__ import annotations

import os
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


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


# --- Local-time canonical helpers -----------------------------------------
#
# WHY: the cron jobs fire in America/Los_Angeles, but the container clock runs in
# UTC. A naive ``datetime.now()`` / ``date.today()`` therefore reads the UTC
# calendar day, which has ALREADY rolled to tomorrow by the 17:00 / 17:30 Pacific
# runs. That makes a task due *today* (Pacific) look 1 day overdue and nags a Q1
# task a day early. Every "today" / "overdue" comparison must derive its calendar
# day from these helpers so the whole skill agrees on one local day.

DEFAULT_TIMEZONE = "America/Los_Angeles"


def local_tz() -> ZoneInfo:
    """The user's local timezone from ``COS_TIMEZONE`` (default US Pacific).

    Mirrors ``_int_env`` robustness: an unknown/garbage tz name degrades to the
    documented default rather than crashing a ritual.
    """
    name = (os.getenv("COS_TIMEZONE") or "").strip() or DEFAULT_TIMEZONE
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(DEFAULT_TIMEZONE)


def local_now() -> datetime:
    """Tz-aware ``now`` in the user's local zone.

    Use this instead of ``datetime.now(timezone.utc)`` wherever a calendar-day
    comparison against a due date follows: at Pacific evening the UTC day is
    already tomorrow, so a UTC ``now`` mis-classifies due-today as overdue.
    """
    return datetime.now(local_tz())


def local_today() -> date:
    """Today's calendar date in the user's local zone.

    The single source of truth for "today" in due-date / overdue logic. Replaces
    naive ``date.today()`` / ``datetime.now().date()``, both of which read the UTC
    day inside the container and roll a day early at Pacific evening.
    """
    return local_now().date()


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


# --- Nag engine knobs (U4, spec §6.5) -------------------------------------

def nag_q1_threshold_days() -> int:
    """Days overdue before a Q1 (urgent) task triggers a nag (default 1).

    Q1 is the section ``effective_priority()`` short-circuits to ``escalated=False``,
    so the nag engine reads the scalar ``overdue_days`` and applies this threshold
    itself rather than relying on the display escalation.
    """
    return _int_env("NAG_Q1_THRESHOLD_DAYS", 1)


def nag_q2_threshold_days() -> int:
    """Days overdue before a Q2 task triggers a nag (default 3, matches escalation)."""
    return _int_env("NAG_Q2_THRESHOLD_DAYS", 3)


def nag_q3_threshold_days() -> int:
    """Days overdue before a Q3 task triggers a nag (default 7)."""
    return _int_env("NAG_Q3_THRESHOLD_DAYS", 7)


def nag_snooze_max() -> int:
    """Akrasia cap: how many times a single nag loop may be snoozed (default 3)."""
    return _int_env("NAG_SNOOZE_MAX", 3)


def start_session_minutes() -> int:
    """Default ``/start`` focus-session length in minutes (default 25, floored at 1).

    H7's ``/start <task>`` opens a short focus block (Pomodoro-ish) when the user
    gives no explicit duration. Floored at 1 so a 0/negative misconfig still yields
    a real, schedulable session rather than a zero-length one whose check-ins fire
    immediately.
    """
    return max(1, _int_env("START_SESSION_MINUTES", 25))


def nag_disposition_after_snoozes() -> int:
    """Snoozes after which the nag STOPS re-asking the same way and asks for a
    disposition instead (default 2, env ``NAG_DISPOSITION_AFTER_SNOOZES``).

    External review (ADHD-overwhelm surface): "after two snoozes, stop tightening
    the interval and ask whether the task is blocked, unclear, too large, or no
    longer important." There is no interval tightening to remove (the cadence is the
    fixed cron schedule, not a per-loop shrink), so the escalation is purely the
    DISPOSITION prompt: once ``snooze_count >= this``, the nag's TEXT becomes the
    disposition question rather than the normal overdue nag (same gated/receipted
    send -- just different wording).

    Floored at 1: a 0/negative value (a misconfig typo) would make EVERY nag a
    disposition prompt from the first snooze (or before), so the knob can raise the
    bar but never drop below one snooze.
    """
    return max(1, _int_env("NAG_DISPOSITION_AFTER_SNOOZES", 2))


def nag_display_limit() -> int:
    """Most-overdue nags pushed per cron cycle; the rest defer to ``/nag all``.

    An ADHD-focused surface drowns under an unbounded overdue dump, so the cron
    push shows only the N worst and a one-line "+K more" pointer. The cap is a
    DISPLAY/firing bound only: deferred tasks keep their place and surface as the
    leaders are cleared. ``/nag all`` (read-only) always shows the full list.

    Floored at 1: a 0 or negative value (a misconfig typo) would silently mute the
    whole nag engine, so the knob can shrink the push but never switch it off.
    """
    return max(1, _int_env("NAG_DISPLAY_LIMIT", 3))


def nag_send_timeout_seconds() -> int:
    """Hard bound on a single ``openclaw message send`` (default 10s, floored at 1).

    H3 makes the nag send in-process, UNDER the nag-state lock; an unbounded hang
    would wedge the cron run AND reactive ``/done`` (which takes the same lock). A
    timeout turns a hung gateway into a clean delivery FAILURE that leaves the loop
    open and releases the lock, instead of blocking forever. R2 halves the default
    (20 -> 10) so a hung gateway makes reactive ``/done`` wait at most ~10s for the
    lock, not 20.
    """
    return max(1, _int_env("NAG_SEND_TIMEOUT_SECONDS", 10))


def outbox_retention_days() -> int:
    """Days of delivered-receipt idem-keys to keep in ``outbox.json`` (default 7).

    The outbox only needs RECENT periods to dedupe a same-cycle retry; an entry from
    days ago can never collide with a current ``(task_id, date+hour)`` key. Old
    entries are pruned on write so the file and the per-run read-modify-write cost
    stay flat. Floored at 1 so a typo cannot disable dedupe within the day.
    """
    return max(1, _int_env("OUTBOX_RETENTION_DAYS", 7))


# --- Proactive layer knobs (U6) -------------------------------------------

def focus_block_day_start_hour() -> int:
    """Local-clock hour the day's focus blocks start at (default 09:00)."""
    return _int_env("FOCUS_BLOCK_DAY_START_HOUR", 9)


def focus_tz_offset_hours() -> int:
    """Fixed UTC offset (hours) for the user's local clock when placing focus blocks.

    Focus blocks anchor to the user's LOCAL morning, but a UTC-scheduled cron passes
    a UTC ``now``. This offset converts: the day-start hour is applied in
    ``UTC+offset``, then the result is expressed as a tz-aware timestamp. Default -7
    (US Pacific daylight) matches the spec's PT cron schedule; set
    ``FOCUS_TZ_OFFSET_HOURS`` for another zone. A fixed offset avoids a tz database
    dependency; DST drift is acceptable for a focus-block start hint.
    """
    return _int_env("FOCUS_TZ_OFFSET_HOURS", -7)


def debrief_reprompt_interval_minutes() -> int:
    """Minimum minutes between debrief follow-up re-prompts for one open loop.

    The ``*/5`` pre-brief scan would otherwise re-prompt an ignored debrief every
    five minutes (dozens of messages a day). This paces it: an open loop is nudged
    at most once per interval, matching the U4 nag engine's habituation-aware
    pacing rather than spamming the ADHD-focused surface (default 120 min).
    """
    return _int_env("DEBRIEF_REPROMPT_INTERVAL_MINUTES", 120)


# --- Accomplishment ledger knobs (U5 / H8) --------------------------------

def ledger_digest_weekday() -> int:
    """Weekday the weekly brag digest auto-fires on (0=Mon .. 6=Sun, default 4=Fri).

    H8 moves the auto-harvest from a daily push to a WEEKLY digest: a daily
    auto-queue is "another thing to service" and the harvest mis-weights what it
    surfaces. The proactive (cron) path only sends on this weekday AND only when the
    digest has content -- a reactive ``/ledger`` ignores it and works any day.
    Clamped to 0..6 so a misconfig (e.g. 9) degrades to Friday rather than a weekday
    that never matches and silently mutes the digest forever.
    """
    day = _int_env("LEDGER_DIGEST_WEEKDAY", 4)
    return day if 0 <= day <= 6 else 4


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
