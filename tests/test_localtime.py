"""H1 local-time correctness: the UTC "overdue" bug and its fix.

The cron jobs fire at 17:00 / 17:30 America/Los_Angeles, but the container clock
runs in UTC. At Pacific evening the UTC calendar day has ALREADY rolled to
tomorrow, so a naive ``datetime.now()`` / ``date.today()`` reads the wrong day:
a task due *today* (Pacific) gets counted as 1 day overdue and a Q1 task nags a
day early.

These tests pin the INVARIANT, not the implementation path:

* At 17:01 Pacific (== 00:01 UTC the next day), a task due *today* (Pacific) is
  **0 days overdue** -- the exact failure the bug caused. 16:59 vs 17:01 is
  parametrized to prove no off-by-one across the UTC-midnight boundary.
* ``cos_config.local_today()`` returns the **Pacific** calendar date, not the
  UTC date, at a Pacific-evening instant.
* The harvest weekly window keys off the local date.

No test calls the real wall clock for "today": every reference instant is a
fixed tz-aware datetime, and where a helper reads ``now`` it is monkeypatched.
Chat ids are irrelevant here; no real ids appear (public-repo hygiene).
"""

import sys
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import cos_config  # noqa: E402
import nag_check  # noqa: E402
import harvest_state  # noqa: E402

PACIFIC = ZoneInfo("America/Los_Angeles")

# A task due on this Pacific calendar day.
DUE_TODAY = "2026-06-19"


# --- The bug: due-today must be 0 overdue across the UTC-midnight boundary ---


@pytest.mark.parametrize(
    "hour,minute,label",
    [
        (16, 59, "16:59 PT (before the cron, still 23:59 UTC same day)"),
        (17, 1, "17:01 PT (the cron instant, already 00:01 UTC next day)"),
    ],
)
def test_due_today_is_zero_overdue_across_utc_midnight(hour, minute, label):
    """A task due today (Pacific) is 0 days overdue at the Pacific-evening cron.

    17:01 PT is 00:01 UTC the NEXT day; the pre-fix code read that UTC day and
    reported 1 day overdue. With a Pacific-local ``ref`` the answer is 0 on both
    sides of the boundary -- the off-by-one is gone.
    """
    ref = datetime(2026, 6, 19, hour, minute, tzinfo=PACIFIC)
    assert nag_check._overdue_days(DUE_TODAY, ref=ref) == 0, label


def test_utc_reference_would_reintroduce_the_bug():
    """Guard the regression: the SAME instant expressed in UTC yields 1 overdue.

    This documents exactly what the old ``datetime.now(timezone.utc)`` did so a
    future refactor that reverts ``_today`` to UTC fails loudly here.
    """
    pacific_ref = datetime(2026, 6, 19, 17, 1, tzinfo=PACIFIC)
    utc_ref = pacific_ref.astimezone(timezone.utc)
    assert utc_ref.date().isoformat() == "2026-06-20"  # the UTC day has rolled
    assert nag_check._overdue_days(DUE_TODAY, ref=pacific_ref) == 0
    assert nag_check._overdue_days(DUE_TODAY, ref=utc_ref) == 1  # the bug


def test_nag_today_is_pacific_local_now(monkeypatch):
    """``nag_check._today`` returns a Pacific-aware now, so ``.date()`` is local.

    Patch only the time source (cos_config.local_now) -- never the real clock --
    to a fixed Pacific-evening instant and assert the local calendar day.
    """
    instant = datetime(2026, 6, 19, 17, 1, tzinfo=PACIFIC)
    monkeypatch.setattr(cos_config, "local_now", lambda: instant)
    ref = nag_check._today()
    assert ref.tzinfo == PACIFIC
    assert ref.date().isoformat() == DUE_TODAY  # Pacific day, not the UTC 06-20


# --- local_today() resolves the Pacific calendar day, not the UTC day --------


def test_local_today_is_pacific_not_utc_at_evening(monkeypatch):
    """At 00:01 UTC, the Pacific calendar day is still YESTERDAY.

    Build a fixed UTC instant just past UTC midnight and let ``local_today`` do
    the zone conversion through ``local_now``; it must land on the Pacific day,
    not the UTC day.
    """
    # 00:01 UTC on 2026-06-20 == 17:01 PT on 2026-06-19.
    utc_instant = datetime(2026, 6, 20, 0, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(
        cos_config, "local_now", lambda: utc_instant.astimezone(cos_config.local_tz())
    )
    assert cos_config.local_today().isoformat() == "2026-06-19"


def test_local_tz_converts_known_utc_instant_to_pacific():
    """Direct zone math: a known UTC instant maps to the expected Pacific day.

    No clock, no monkeypatch -- just assert the timezone object converts an
    explicit aware UTC datetime to the right Pacific calendar day.
    """
    utc_instant = datetime(2026, 6, 20, 0, 1, tzinfo=timezone.utc)
    pacific = utc_instant.astimezone(cos_config.local_tz())
    assert pacific.date().isoformat() == "2026-06-19"
    assert pacific.hour == 17 and pacific.minute == 1


def test_local_tz_defaults_and_degrades(monkeypatch):
    """Default is US Pacific; a garbage tz name degrades, never raises."""
    monkeypatch.delenv("COS_TIMEZONE", raising=False)
    assert cos_config.local_tz() == ZoneInfo("America/Los_Angeles")
    monkeypatch.setenv("COS_TIMEZONE", "Europe/Berlin")
    assert cos_config.local_tz() == ZoneInfo("Europe/Berlin")
    monkeypatch.setenv("COS_TIMEZONE", "Not/AZone")  # garbage -> default, no raise
    assert cos_config.local_tz() == ZoneInfo("America/Los_Angeles")
    monkeypatch.setenv("COS_TIMEZONE", "   ")  # blank -> default
    assert cos_config.local_tz() == ZoneInfo("America/Los_Angeles")


# --- harvest week-boundary keys off the local date ---------------------------


def test_harvest_iso_week_id_uses_local_today(monkeypatch):
    """``iso_week_id()`` (no arg) reads the LOCAL day for the weekly window.

    At 00:01 UTC on 2026-06-22 (a Monday, ISO week 26) it is still 17:01 PT on
    2026-06-21 (a Sunday, ISO week 25). The weekly harvest window must scope to
    the Pacific week (W25), not the UTC week (W26), or the window resets a day
    early and resurfaces still-pending items.
    """
    # 2026-06-21 is Sunday (ISO 2026-W25); 2026-06-22 is Monday (ISO 2026-W26).
    monkeypatch.setattr(cos_config, "local_today", lambda: date(2026, 6, 21))
    assert harvest_state.iso_week_id() == "2026-W25"
    # The 24h window id is likewise the local calendar day.
    assert harvest_state.window_id(harvest_state.WINDOW_24H) == "2026-06-21-24h"


def test_harvest_window_id_local_vs_utc_boundary():
    """Explicit boundary proof for the harvest week id at the UTC rollover.

    Passing the Pacific date (Sunday W25) vs the UTC date (Monday W26) for the
    same instant yields different ISO weeks; the helper must use the local one.
    """
    pacific_day = date(2026, 6, 21)  # Sunday, ISO 2026-W25
    utc_day = date(2026, 6, 22)  # Monday, ISO 2026-W26
    assert harvest_state.iso_week_id(pacific_day) == "2026-W25"
    assert harvest_state.iso_week_id(utc_day) == "2026-W26"
