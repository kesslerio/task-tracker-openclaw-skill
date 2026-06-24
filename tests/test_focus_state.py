"""v0.4-C: focus_state.rev -- a monotonic CAS token that never regresses, even
across a fresh morning re-propose, plus the current_rev() reader.

Pre-rev behaviour (updated_at stamping, corrupt quarantine, stale-date) is covered
by the focus_core/focus_commands suites; this file pins only the rev addition.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import focus_state  # noqa: E402


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_MGMT_STATE_DIR", str(tmp_path / "state"))
    return tmp_path / "state"


def _proposal(**over):
    base = dict(
        defended=[{"task_id": "tsk_aaaaaaaaaaaaaaaa", "title": "X", "position": 1}],
        holding_tank=[],
        free_hours=4.0,
        total_estimated_minutes=120,
        capacity_ok=True,
    )
    base.update(over)
    return focus_state.new_proposal_state(**base)


def test_rev_starts_at_one_and_increments_on_each_save(state):
    s = _proposal()
    assert "rev" not in s  # the fresh proposal carries no rev
    focus_state.save_focus_state(s)
    assert s["rev"] == 1

    reloaded = focus_state.load_focus_state()
    focus_state.save_focus_state(reloaded)
    assert reloaded["rev"] == 2
    assert focus_state.current_rev() == 2


def test_rev_does_not_regress_on_fresh_re_propose(state):
    # Drive rev up, then save a BRAND-NEW proposal document (no rev field, as the
    # morning re-propose builds). rev must continue from disk, not reset to 1.
    s = _proposal()
    for _ in range(5):
        focus_state.save_focus_state(s)
    assert s["rev"] == 5

    fresh = _proposal(reference_date="2026-06-25")  # a new day's proposal, no rev
    assert "rev" not in fresh
    focus_state.save_focus_state(fresh)
    assert fresh["rev"] == 6  # 5 (on disk) + 1, NOT 1
    assert focus_state.current_rev() == 6


def test_current_rev_is_none_without_state(state):
    assert focus_state.current_rev() is None


def test_rev_floors_against_passed_value_when_disk_unreadable(state):
    # A corrupt on-disk file reads as rev 0 inside save, but the passed-in dict's
    # own rev still floors the next value -- rev never goes backwards.
    focus_state.save_focus_state(_proposal())  # rev 1
    s = focus_state.load_focus_state()
    focus_state.save_focus_state(s)  # rev 2
    assert s["rev"] == 2

    focus_state.focus_state_path().write_text("{ not json", encoding="utf-8")
    # s still carries rev 2; a re-save floors max(2, on_disk=0) + 1 = 3.
    focus_state.save_focus_state(s)
    assert s["rev"] == 3


def test_save_takes_the_focus_state_flock(state):
    # The rev bump is a read-then-write CAS token, so save must serialise under the
    # sidecar flock (the lockfile is created on first save).
    focus_state.save_focus_state(_proposal())
    assert focus_state.focus_state_lock_path().exists()


def test_two_writers_from_the_same_rev_get_distinct_revs(state):
    # The invariant the flock + read-on-disk-floor defends: two writers that both
    # loaded the state at the SAME rev (the concurrent-writer race) must mint
    # DISTINCT, strictly increasing revs -- never the same rev with different
    # content (which would let a stale CAS snapshot falsely pass). The flock
    # serialises the two saves; this asserts that, so serialised, each save reads
    # the other's on-disk rev and increments past it rather than re-minting it.
    focus_state.save_focus_state(_proposal())          # rev 1 on disk
    a = focus_state.load_focus_state()                 # both writers load rev 1
    b = focus_state.load_focus_state()
    a["override_reason"], b["override_reason"] = "A", "B"  # divergent content
    focus_state.save_focus_state(a)                    # rev 2
    focus_state.save_focus_state(b)                    # rev 3, NOT a second rev 2
    assert a["rev"] == 2 and b["rev"] == 3
    assert focus_state.current_rev() == 3


def test_current_rev_zero_for_legacy_state_without_rev(state):
    # A hand-written/legacy state file with no rev field reads as 0 (a valid
    # baseline), not None and not a crash.
    focus_state.focus_state_path().parent.mkdir(parents=True, exist_ok=True)
    focus_state.focus_state_path().write_text(
        json.dumps({"date": "2026-06-24", "status": "proposed"}), encoding="utf-8")
    assert focus_state.current_rev() == 0
