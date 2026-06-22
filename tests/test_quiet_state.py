"""H5 quiet-state I/O layer: the flocked, atomic quiet window.

Invariants pinned here:

* ``is_quiet`` is True inside the window, False once expired, and False on a
  missing-or-corrupt file -- and NEVER raises (a broken quiet file fails toward
  nagging, never toward permanent silence).
* ``set_quiet`` + ``clear_quiet`` round-trip under the flock.
* ``quiet_until`` returns the future deadline for display, None when not quiet.

Fake values only -- no real chat ids or paths (TASK_MGMT_STATE_DIR is tmp_path).
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import quiet_state  # noqa: E402

NOW = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_MGMT_STATE_DIR", str(tmp_path / "state"))
    return tmp_path / "state"


def test_missing_file_is_not_quiet(state_dir):
    """No quiet-state.json at all -> not quiet (fail toward nagging), never raises."""
    assert quiet_state.is_quiet(NOW) is False
    assert quiet_state.quiet_until(NOW) is None


def test_inside_window_is_quiet(state_dir):
    quiet_state.set_quiet(NOW + timedelta(hours=2))
    assert quiet_state.is_quiet(NOW) is True
    until = quiet_state.quiet_until(NOW)
    assert until == NOW + timedelta(hours=2)


def test_expired_window_is_not_quiet(state_dir):
    """A past deadline is NOT quiet -- the window self-expires, no clear needed."""
    quiet_state.set_quiet(NOW - timedelta(minutes=1))
    assert quiet_state.is_quiet(NOW) is False
    assert quiet_state.quiet_until(NOW) is None  # expired -> nothing to display


def test_boundary_exactly_at_deadline_is_not_quiet(state_dir):
    """At the exact deadline the window is OVER (``now < until`` is strict)."""
    quiet_state.set_quiet(NOW)
    assert quiet_state.is_quiet(NOW) is False


def test_corrupt_file_is_not_quiet(state_dir):
    """A corrupt/garbage quiet-state.json -> not quiet, never raises (the next clean
    write rebuilds it; a broken file must never mute the nag engine forever)."""
    quiet_state.set_quiet(NOW + timedelta(hours=2))  # create the dir + file first
    quiet_state.quiet_state_path().write_text("{ this is not json", encoding="utf-8")
    assert quiet_state.is_quiet(NOW) is False  # does not raise
    assert quiet_state.quiet_until(NOW) is None


def test_garbage_quiet_until_value_is_not_quiet(state_dir):
    """A non-timestamp ``quiet_until`` (a hand-edited list/number) -> not quiet, no raise."""
    quiet_state.quiet_state_path().parent.mkdir(parents=True, exist_ok=True)
    quiet_state.quiet_state_path().write_text('{"quiet_until": [1, 2, 3]}', encoding="utf-8")
    assert quiet_state.is_quiet(NOW) is False


def test_set_then_clear_round_trip(state_dir):
    """set_quiet + clear_quiet round-trip under the flock."""
    quiet_state.set_quiet(NOW + timedelta(hours=3))
    assert quiet_state.is_quiet(NOW) is True
    quiet_state.clear_quiet()
    assert quiet_state.is_quiet(NOW) is False
    assert quiet_state.quiet_until(NOW) is None


def test_naive_until_is_stored_as_utc(state_dir):
    """A naive deadline is stamped UTC so it round-trips to a tz-aware comparison."""
    quiet_state.set_quiet(datetime(2026, 6, 22, 14, 0, 0))  # naive
    until = quiet_state.quiet_until(NOW)
    assert until is not None and until.tzinfo is not None


def test_quiet_state_file_is_owner_only(state_dir):
    """The quiet-state file holds a user attention preference under the 0o700 state dir
    -- _atomic_write leaves a FRESH file at 0o600 (no group/world read)."""
    quiet_state.set_quiet(NOW + timedelta(hours=1))
    mode = quiet_state.quiet_state_path().stat().st_mode & 0o777
    assert mode == 0o600
