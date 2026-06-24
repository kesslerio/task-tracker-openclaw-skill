"""v0.4-C initiation decision-contract: the slot id, the Proposal record, and the
two-dimension send-time CAS (pure core + fail-closed live wrapper)."""

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import focus_state  # noqa: E402
import initiation_contract as ic  # noqa: E402
import nag_state  # noqa: E402

TASK = "tsk_aaaaaaaaaaaaaaaa"
DATE = "2026-06-24"
SLOT = "work:tsk_aaaaaaaaaaaaaaaa:2026-06-24"
BASE = "2026-06-24T18:00:00+00:00"


def _proposal(**over):
    base = dict(
        focus_episode_id=SLOT,
        task_id=TASK,
        user_scope="work",
        local_date=DATE,
        stage=ic.STAGE_COLD_START,
        reason_code="committed_unstarted",
        created_at=BASE,
        expires_at="2026-06-24T19:00:00+00:00",
        cas_focus_state_rev=1,
        cas_no_session_since=BASE,
    )
    base.update(over)
    return ic.Proposal(**base)


def _at(hhmm: str) -> datetime:
    return datetime.fromisoformat(f"2026-06-24T{hhmm}:00+00:00")


# --- focus_episode_slot ----------------------------------------------------

def test_slot_is_deterministic_and_self_contained():
    assert ic.focus_episode_slot("work", TASK, DATE) == SLOT
    assert ic.focus_episode_slot("work", TASK, DATE) == ic.focus_episode_slot("work", TASK, DATE)
    assert ic.focus_episode_slot("work", TASK, "2026-06-25") != SLOT


@pytest.mark.parametrize("scope,task,date", [
    ("work", "tsk:bad", DATE),     # colon in a segment would corrupt the key
    ("", TASK, DATE),               # empty segment
    ("work", TASK, ""),
])
def test_slot_rejects_colon_or_empty_segments(scope, task, date):
    with pytest.raises(ValueError):
        ic.focus_episode_slot(scope, task, date)


# --- Proposal --------------------------------------------------------------

def test_proposal_round_trips_and_keys():
    p = _proposal()
    assert p.idem_key() == f"initiation:{SLOT}:cold_start"
    assert ic.Proposal.from_dict(p.to_dict()) == p
    # arm defaults to None (C5 fills it) and survives the round trip
    assert p.arm is None


def test_proposal_from_dict_ignores_unknown_keys():
    data = _proposal().to_dict()
    data["future_field"] = "ignored"
    assert ic.Proposal.from_dict(data).task_id == TASK


def test_proposal_rejects_unknown_stage():
    with pytest.raises(ValueError):
        _proposal(stage="nope")


def test_proposal_rejects_mismatched_episode_id():
    with pytest.raises(ValueError):
        _proposal(focus_episode_id="work:other:2026-06-24")


def test_proposal_expiry():
    p = _proposal()
    assert p.is_expired(now=_at("18:30")) is False
    assert p.is_expired(now=_at("19:00")) is True   # at expiry == expired
    assert p.is_expired(now=_at("19:30")) is True
    assert _proposal(expires_at="not-a-date").is_expired(now=_at("18:30")) is True


# --- cas_still_valid (pure) -------------------------------------------------

def test_cas_valid_when_rev_matches_and_no_sessions():
    assert ic.cas_still_valid(_proposal(), current_focus_rev=1, task_sessions=[]) is True


def test_cas_invalid_when_rev_advanced_or_missing():
    p = _proposal(cas_focus_state_rev=1)
    assert ic.cas_still_valid(p, current_focus_rev=2, task_sessions=[]) is False
    assert ic.cas_still_valid(p, current_focus_rev=None, task_sessions=[]) is False


def test_cas_invalid_when_a_session_started_after_baseline():
    started = [{"session_id": "st_x", "started_at": "2026-06-24T18:30:00+00:00"}]
    assert ic.cas_still_valid(_proposal(), current_focus_rev=1, task_sessions=started) is False


def test_cas_valid_when_only_a_pre_baseline_session_exists():
    # A session that started AND ended before the proposal (e.g. a cancelled earlier
    # attempt) does not invalidate -- the user is not currently in/past this episode.
    old = [{"session_id": "st_old", "started_at": "2026-06-24T17:00:00+00:00",
            "ended_at": "2026-06-24T17:30:00+00:00"}]
    assert ic.cas_still_valid(_proposal(), current_focus_rev=1, task_sessions=old) is True


def test_cas_invalid_when_a_session_ended_after_baseline():
    ended = [{"session_id": "st_y", "started_at": "2026-06-24T17:00:00+00:00",
              "ended_at": "2026-06-24T18:30:00+00:00"}]
    assert ic.cas_still_valid(_proposal(), current_focus_rev=1, task_sessions=ended) is False


def test_cas_invalid_when_baseline_unparseable():
    assert ic.cas_still_valid(
        _proposal(cas_no_session_since="garbage"), current_focus_rev=1, task_sessions=[]) is False


def test_cas_tolerates_naive_session_timestamps():
    # A naive (tz-less) started_at after the baseline still invalidates -- assumed UTC.
    naive = [{"session_id": "st_z", "started_at": "2026-06-24T18:30:00"}]
    assert ic.cas_still_valid(_proposal(), current_focus_rev=1, task_sessions=naive) is False


# --- cas_still_valid_now (live, fail-closed) --------------------------------

@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_MGMT_STATE_DIR", str(tmp_path / "state"))
    return tmp_path / "state"


def _seed_focus_rev(rev_target):
    """Save focus-state enough times to reach rev == rev_target; return the rev."""
    s = focus_state.new_proposal_state(
        defended=[{"task_id": TASK, "title": "X", "position": 1}],
        holding_tank=[], free_hours=4.0, total_estimated_minutes=60, capacity_ok=True)
    for _ in range(rev_target):
        focus_state.save_focus_state(s)
    return focus_state.current_rev()


def test_cas_now_valid_against_real_focus_state(state, monkeypatch):
    rev = _seed_focus_rev(1)
    monkeypatch.setattr(nag_state, "read_state", lambda: {})
    assert ic.cas_still_valid_now(_proposal(cas_focus_state_rev=rev)) is True


def test_cas_now_invalid_when_focus_rev_moved(state, monkeypatch):
    _seed_focus_rev(1)
    monkeypatch.setattr(nag_state, "read_state", lambda: {})
    p = _proposal(cas_focus_state_rev=1)
    focus_state.save_focus_state(focus_state.load_focus_state())  # bump rev to 2
    assert ic.cas_still_valid_now(p) is False


def test_cas_now_fail_closed_when_no_focus_state(state, monkeypatch):
    monkeypatch.setattr(nag_state, "read_state", lambda: {})
    assert ic.cas_still_valid_now(_proposal()) is False  # current_rev() is None


def test_cas_now_invalid_when_live_session_started(state, monkeypatch):
    rev = _seed_focus_rev(1)
    monkeypatch.setattr(nag_state, "read_state", lambda: {
        TASK: {"body_double_sessions": [
            {"session_id": "st_live", "started_at": "2026-06-24T18:30:00+00:00"}]}})
    assert ic.cas_still_valid_now(_proposal(cas_focus_state_rev=rev)) is False


def test_cas_now_fail_closed_on_read_error(state, monkeypatch):
    _seed_focus_rev(1)

    def boom():
        raise RuntimeError("nag-state unreadable")

    monkeypatch.setattr(nag_state, "read_state", boom)
    assert ic.cas_still_valid_now(_proposal(cas_focus_state_rev=1)) is False
