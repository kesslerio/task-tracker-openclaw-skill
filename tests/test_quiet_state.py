"""H5/R3 quiet-state I/O layer: flocked, atomic, OWNER-KEYED quiet leases.

Invariants pinned here:

* ``is_quiet`` is True while any live lease is in the future, False once all are
  expired, and False on a missing-or-corrupt file -- and NEVER raises (a broken
  quiet file fails toward nagging, never toward permanent silence).
* ``set_quiet`` + ``clear_quiet`` round-trip under the flock (manual-lease shims).
* ``quiet_until`` returns the MAX live-lease deadline for display, None when none.
* R3 leases: ``set_lease`` adds/replaces ONLY its owner (a concurrent peer's lease
  is never lost); ``release_lease`` removes only its owner; expired leases prune on
  read and write (bounded); a legacy scalar ``quiet_until`` reads as a manual lease.

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


# --- R3 owner-keyed leases --------------------------------------------------

def test_effective_quiet_is_max_over_live_leases(state_dir):
    """``quiet_until`` is the MAX deadline over all live leases; ``is_quiet`` True
    while any is future."""
    quiet_state.set_lease("manual", NOW + timedelta(minutes=10), now=NOW)
    quiet_state.set_lease("st_abc", NOW + timedelta(minutes=25), now=NOW)
    assert quiet_state.is_quiet(NOW) is True
    assert quiet_state.quiet_until(NOW) == NOW + timedelta(minutes=25)  # the later one


def test_set_lease_does_not_drop_a_concurrent_owners_lease(state_dir):
    """R3 HIGH-1 core: a writer adds/replaces ONLY its own owner. A manual lease
    written between a session's read and write is NOT lost -- both survive."""
    # Simulate the race: a session lease, then a manual /quiet "concurrently" -- each
    # set_lease is its own read-modify-write under the flock, so neither clobbers.
    quiet_state.set_lease("st_session", NOW + timedelta(minutes=25), now=NOW)
    quiet_state.set_lease("manual", NOW + timedelta(hours=2), now=NOW)
    leases = quiet_state._read_leases(quiet_state._read_raw())
    assert set(leases) == {"st_session", "manual"}  # both present
    # And releasing the session lease leaves the manual one intact.
    quiet_state.release_lease("st_session", now=NOW)
    leases = quiet_state._read_leases(quiet_state._read_raw())
    assert set(leases) == {"manual"}
    assert quiet_state.quiet_until(NOW) == NOW + timedelta(hours=2)


def test_release_lease_removes_only_that_owner(state_dir):
    quiet_state.set_lease("a", NOW + timedelta(minutes=10), now=NOW)
    quiet_state.set_lease("b", NOW + timedelta(minutes=20), now=NOW)
    quiet_state.release_lease("a", now=NOW)
    leases = quiet_state._read_leases(quiet_state._read_raw())
    assert set(leases) == {"b"}
    # Releasing a non-existent owner is a harmless no-op.
    quiet_state.release_lease("ghost", now=NOW)
    assert set(quiet_state._read_leases(quiet_state._read_raw())) == {"b"}


def test_expired_leases_are_pruned_on_write_so_the_set_stays_bounded(state_dir):
    """An expired lease does not accumulate: each write prunes leases past their
    deadline, so a frequently-restarted session never grows the set unbounded."""
    quiet_state.set_lease("old1", NOW - timedelta(minutes=1), now=NOW)
    quiet_state.set_lease("old2", NOW - timedelta(minutes=2), now=NOW)
    # A fresh write prunes the two already-expired leases and adds only the live one.
    quiet_state.set_lease("live", NOW + timedelta(minutes=30), now=NOW)
    leases = quiet_state._read_leases(quiet_state._read_raw())
    assert set(leases) == {"live"}  # the expired ones were pruned on write


def test_all_expired_leases_is_not_quiet(state_dir):
    """All leases expired -> NOT quiet (fail toward nagging), never raises."""
    quiet_state.set_lease("a", NOW + timedelta(minutes=10), now=NOW)
    quiet_state.set_lease("b", NOW + timedelta(minutes=20), now=NOW)
    later = NOW + timedelta(hours=1)  # past both deadlines
    assert quiet_state.is_quiet(later) is False
    assert quiet_state.quiet_until(later) is None


def test_legacy_scalar_quiet_until_is_read_as_a_manual_lease(state_dir):
    """A pre-R3 on-disk ``{"quiet_until": "<iso>"}`` (the old scalar) is honored as an
    implicit manual lease so a live quiet window survives the deploy."""
    quiet_state.quiet_state_path().parent.mkdir(parents=True, exist_ok=True)
    legacy_deadline = NOW + timedelta(hours=2)
    quiet_state.quiet_state_path().write_text(
        '{"quiet_until": "%s"}' % legacy_deadline.isoformat(), encoding="utf-8")
    assert quiet_state.is_quiet(NOW) is True
    assert quiet_state.quiet_until(NOW) == legacy_deadline
    # It reads as the "manual" owner, so /unquiet (clear_quiet) clears it.
    leases = quiet_state._read_leases(quiet_state._read_raw())
    assert set(leases) == {quiet_state.MANUAL_OWNER}
    quiet_state.clear_quiet()
    assert quiet_state.is_quiet(NOW) is False


def test_legacy_scalar_does_not_shadow_an_explicit_manual_lease(state_dir):
    """If both the new ``leases`` shape AND a stray legacy ``quiet_until`` are on disk,
    the explicit manual lease wins (the legacy migration only backfills a MISSING
    manual lease)."""
    quiet_state.quiet_state_path().parent.mkdir(parents=True, exist_ok=True)
    explicit = (NOW + timedelta(hours=3)).isoformat()
    stale_scalar = (NOW + timedelta(hours=9)).isoformat()
    quiet_state.quiet_state_path().write_text(
        '{"leases": {"manual": "%s"}, "quiet_until": "%s"}' % (explicit, stale_scalar),
        encoding="utf-8")
    leases = quiet_state._read_leases(quiet_state._read_raw())
    assert leases[quiet_state.MANUAL_OWNER].isoformat() == explicit  # not the scalar


def test_manual_shims_round_trip_through_the_manual_lease(state_dir):
    """``set_quiet`` / ``clear_quiet`` are thin shims over the manual lease and a
    session lease set alongside is untouched by ``clear_quiet`` (manual-only)."""
    quiet_state.set_quiet(NOW + timedelta(hours=1), now=NOW)
    quiet_state.set_lease("st_x", NOW + timedelta(minutes=30), now=NOW)
    quiet_state.clear_quiet(now=NOW)  # releases only "manual"; prune ref pinned to NOW
    leases = quiet_state._read_leases(quiet_state._read_raw())
    assert set(leases) == {"st_x"}  # the session lease survives a manual clear
    assert quiet_state.is_quiet(NOW) is True  # still muted by the session lease
