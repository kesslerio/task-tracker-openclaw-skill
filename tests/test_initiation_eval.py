"""v0.4-C initiation evaluator: the pure rules-only gate chain that emits (or withholds)
an initiation Proposal. Dependencies (nag_state, availability, outbox receipts) are
stubbed; focus_state is exercised for real (it backs the CAS rev)."""

import sys
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import cos_config  # noqa: E402
import focus_state  # noqa: E402
import initiation_contract as ic  # noqa: E402
import initiation_eval as ev  # noqa: E402
import initiation_store as store  # noqa: E402

TASK = "tsk_aaaaaaaaaaaaaaaa"
NOW = cos_config.local_now().replace(microsecond=0)
TODAY = NOW.astimezone(cos_config.local_tz()).date().isoformat()
SLOT = ic.focus_episode_slot("work", TASK, TODAY)


def _commit(*, minutes_ago=100, task_id=TASK, status="approved"):
    """Write an APPROVED focus-state with ``task_id`` as #1, committed ``minutes_ago``."""
    committed = (NOW - timedelta(minutes=minutes_ago)).isoformat()
    focus_state.save_focus_state({
        "schema_version": 1, "date": TODAY, "status": status,
        "proposed_at": committed, "approved_at": committed,
        "free_hours": 4.0, "total_estimated_minutes": 60, "capacity_ok": True,
        "override_reason": None,
        "daily_priorities": [{"task_id": task_id, "title": "Ship X", "position": 1}],
        "holding_tank": [], "vetoed": [],
    })


def _key(stage):
    return ic.make_idem_key("initiation", SLOT, stage)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_MGMT_STATE_DIR", str(tmp_path / "state"))
    # Defaults: calendar clear, no nag entry (no sessions/snooze), no prior sends.
    monkeypatch.setattr(ev.availability, "not_known_busy", lambda now, **k: True)
    monkeypatch.setattr(ev.nag_state, "read_state", lambda: {})
    monkeypatch.setattr(ev.outbox, "get_receipt", lambda key: None)

    def _set_nag(entry):
        monkeypatch.setattr(ev.nag_state, "read_state", lambda: {TASK: entry})

    def _set_receipts(mapping):
        monkeypatch.setattr(ev.outbox, "get_receipt", lambda key: mapping.get(key))

    def _set_calendar(value):
        monkeypatch.setattr(ev.availability, "not_known_busy", lambda now, **k: value)

    return type("Env", (), {"nag": staticmethod(_set_nag),
                            "receipts": staticmethod(_set_receipts),
                            "calendar": staticmethod(_set_calendar),
                            "mp": monkeypatch})


# --- happy path ------------------------------------------------------------

def test_emits_cold_start_when_all_gates_pass(env):
    _commit()
    p = ev.evaluate(NOW)
    assert p is not None
    assert p.stage == ic.STAGE_COLD_START
    assert p.task_id == TASK and p.focus_episode_id == SLOT and p.user_scope == "work"
    assert p.reason_code == ev.REASON_COLD_START
    assert p.cas_focus_state_rev == focus_state.current_rev()
    assert p.cas_no_session_since == p.created_at == NOW.isoformat()
    # C5: the holdout arm is stamped deterministically from the slot.
    import initiation_holdout
    assert p.arm == initiation_holdout.arm_for(SLOT)
    assert p.arm in ("treatment", "control")


# --- gate 1: committed #1 --------------------------------------------------

def test_no_focus_state_no_nudge(env):
    assert ev.evaluate(NOW) is None


def test_proposed_not_approved_no_nudge(env):
    _commit(status="proposed")
    assert ev.evaluate(NOW) is None


# --- gate 2: not started ---------------------------------------------------

def test_active_session_no_nudge(env):
    _commit()
    env.nag({"body_double_sessions": [{"session_id": "st_x",
             "started_at": NOW.isoformat()}]})
    assert ev.evaluate(NOW) is None


def test_started_and_ended_today_no_nudge(env):
    _commit()
    env.nag({"body_double_sessions": [{"session_id": "st_x",
             "started_at": (NOW - timedelta(minutes=30)).isoformat(),
             "ended_at": (NOW - timedelta(minutes=5)).isoformat()}]})
    assert ev.evaluate(NOW) is None


def test_overnight_session_ended_today_no_nudge(env):
    # Started yesterday-local, ended today-local (straddles local midnight) -> engaged.
    _commit()
    env.nag({"body_double_sessions": [{"session_id": "st_x",
             "started_at": (NOW - timedelta(days=1)).isoformat(),
             "ended_at": (NOW - timedelta(minutes=20)).isoformat()}]})
    assert ev.evaluate(NOW) is None


def test_overnight_session_scheduled_into_today_no_nudge(env):
    # Started yesterday, scheduled end (ends_at) is today, never explicitly closed.
    _commit()
    env.nag({"body_double_sessions": [{"session_id": "st_x",
             "started_at": (NOW - timedelta(days=1)).isoformat(),
             "ends_at": (NOW - timedelta(minutes=20)).isoformat()}]})
    assert ev.evaluate(NOW) is None


def test_ended_session_with_undateable_stamp_fails_closed_no_nudge(env):
    # An ended session we cannot date might be today's -> fail closed (engaged), never
    # a false nudge (mirrors the CAS posture on unparseable stamps).
    _commit()
    env.nag({"body_double_sessions": [{"session_id": "st_x",
             "started_at": "not-a-timestamp", "ended_at": "also-garbage"}]})
    assert ev.evaluate(NOW) is None


def test_non_string_task_id_no_nudge(env):
    # A malformed focus_state with a numeric task_id must not be laundered into a slot.
    _commit(task_id=12345)
    assert ev.evaluate(NOW) is None


# --- gate 3: snooze --------------------------------------------------------

def test_snoozed_no_nudge(env):
    _commit()
    env.nag({"snoozed_until": (NOW + timedelta(hours=1)).isoformat()})
    assert ev.evaluate(NOW) is None


# --- gate 4: cadence / budget ----------------------------------------------

def test_too_soon_no_nudge(env):
    _commit(minutes_ago=80)  # < 90
    assert ev.evaluate(NOW) is None


def test_renudge_after_gap(env):
    _commit(minutes_ago=260)
    env.receipts({_key(ic.STAGE_COLD_START): {"ts": (NOW - timedelta(minutes=130)).isoformat()}})
    p = ev.evaluate(NOW)
    assert p is not None and p.stage == ic.STAGE_COLD_START_RENUDGE
    assert p.reason_code == ev.REASON_RENUDGE


def test_renudge_too_soon_no_nudge(env):
    _commit(minutes_ago=200)
    env.receipts({_key(ic.STAGE_COLD_START): {"ts": (NOW - timedelta(minutes=60)).isoformat()}})
    assert ev.evaluate(NOW) is None  # 60 < 120 re-nudge gap


def test_budget_exhausted_no_nudge(env):
    _commit(minutes_ago=400)
    env.receipts({
        _key(ic.STAGE_COLD_START): {"ts": (NOW - timedelta(minutes=300)).isoformat()},
        _key(ic.STAGE_COLD_START_RENUDGE): {"ts": (NOW - timedelta(minutes=130)).isoformat()},
    })
    assert ev.evaluate(NOW) is None


def test_renudge_receipt_without_cold_start_suppresses(env):
    # Corrupted send history (a re-nudge with no cold-start) must not emit a cold-start.
    _commit(minutes_ago=400)
    env.receipts({_key(ic.STAGE_COLD_START_RENUDGE): {"ts": (NOW - timedelta(minutes=130)).isoformat()}})
    assert ev.evaluate(NOW) is None


def test_budget_zero_disables_nudges(env):
    _commit()
    env.mp.setenv("INITIATION_DAILY_BUDGET", "0")
    assert ev.evaluate(NOW) is None


def test_elapsed_boundary_is_inclusive(env):
    _commit(minutes_ago=89)
    assert ev.evaluate(NOW) is None        # 89 < 90 -> too soon
    _commit(minutes_ago=90)
    assert ev.evaluate(NOW) is not None     # exactly 90 -> fires


# --- decide_and_store fail-open --------------------------------------------

def test_decide_and_store_fails_open_on_write_error(env):
    _commit()

    def boom(*a, **k):
        raise OSError("disk full")

    env.mp.setattr(ev.initiation_store, "write_proposal", boom)
    assert ev.decide_and_store(NOW) is None  # must not propagate / crash the cron


# --- gate 5: calendar (evaluated LAST) -------------------------------------

def test_calendar_busy_no_nudge(env):
    _commit()
    env.calendar(False)
    assert ev.evaluate(NOW) is None


def test_calendar_not_consulted_when_a_cheap_gate_fails(env):
    _commit(minutes_ago=80)  # too soon -> must short-circuit BEFORE the calendar read

    def boom(now, **k):
        raise AssertionError("calendar must not be read when a cheap gate already failed")

    env.mp.setattr(ev.availability, "not_known_busy", boom)
    assert ev.evaluate(NOW) is None


# --- fail OPEN -------------------------------------------------------------

def test_read_error_fails_open_to_no_nudge(env):
    _commit()

    def boom():
        raise RuntimeError("nag-state unreadable")

    env.mp.setattr(ev.nag_state, "read_state", boom)
    assert ev.evaluate(NOW) is None


# --- decide_and_store ------------------------------------------------------

def test_decide_and_store_parks_the_proposal(env):
    _commit()
    p = ev.decide_and_store(NOW)
    assert p is not None
    stored = store.read_proposal(SLOT, now=NOW + timedelta(minutes=1))
    assert stored == p


def test_decide_and_store_writes_nothing_when_withheld(env):
    _commit(minutes_ago=80)  # too soon -> no proposal
    assert ev.decide_and_store(NOW) is None
    assert store.read_proposal(SLOT, now=NOW) is None
