"""v0.4-C initiation dispatcher: load proposal -> send-time CAS -> re-prove target ->
inert nudge -> deliver_once (with in-flock precheck) -> clear. Externals stubbed; the
outbox + store run for real (tmp state)."""

import sys
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import cos_config  # noqa: E402
import focus_state  # noqa: E402
import initiation_contract as ic  # noqa: E402
import initiation_dispatch as disp  # noqa: E402
import initiation_store as store  # noqa: E402

TASK = "tsk_aaaaaaaaaaaaaaaa"
TITLE = "Ship the onboarding flow"
NOW = cos_config.local_now().replace(microsecond=0)
TODAY = NOW.astimezone(cos_config.local_tz()).date().isoformat()
SLOT = ic.focus_episode_slot("work", TASK, TODAY)
TARGET = {"chat_id": "-4242424242", "topic_id": "2", "agent_id": "niemand-work", "channel": "telegram"}


def _commit(*, minutes_ago=100):
    committed = (NOW - timedelta(minutes=minutes_ago)).isoformat()
    focus_state.save_focus_state({
        "schema_version": 1, "date": TODAY, "status": "approved",
        "proposed_at": committed, "approved_at": committed,
        "free_hours": 4.0, "total_estimated_minutes": 60, "capacity_ok": True,
        "override_reason": None,
        "daily_priorities": [{"task_id": TASK, "title": TITLE, "position": 1}],
        "holding_tank": [], "vetoed": [],
    })


def _park(*, stage=ic.STAGE_COLD_START):
    p = ic.Proposal(
        focus_episode_id=SLOT, task_id=TASK, user_scope="work", local_date=TODAY,
        stage=stage, reason_code="committed_unstarted",
        created_at=(NOW - timedelta(minutes=1)).isoformat(),
        expires_at=(NOW + timedelta(minutes=59)).isoformat(),
        cas_focus_state_rev=focus_state.current_rev(), cas_no_session_since=NOW.isoformat())
    store.write_proposal(p, now=NOW)
    return p


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_MGMT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(disp.nag_delivery, "resolve_target",
                        lambda: {"ok": True, "delivery_target": TARGET})
    monkeypatch.setattr(disp.availability, "not_known_busy", lambda now, **k: True)
    monkeypatch.setattr(disp.nag_state, "read_state", lambda: {})
    monkeypatch.setattr(disp.initiation_contract, "cas_still_valid_now", lambda p: True)
    sent = []

    def sender(target, text, *buttons):
        sent.append({"target": target, "text": text,
                     "buttons": buttons[0] if buttons else None})
        return {"message_id": "9001"}

    return type("E", (), {"sent": sent, "sender": staticmethod(sender), "mp": monkeypatch})


# --- happy path ------------------------------------------------------------

def test_delivers_inert_nudge_with_buttons_and_clears(env):
    _commit()
    _park()
    result = disp.run_dispatch(SLOT, now=NOW, sender=env.sender)
    assert result["sent"] is True and result["reason"] == "delivered"
    assert len(env.sent) == 1
    msg = env.sent[0]
    assert msg["target"] == TARGET
    assert TITLE in msg["text"] and "haven't started" in msg["text"]
    values = {b["value"] for b in msg["buttons"]}
    assert values == {f"tt:start:{TASK}", f"tt:done:{TASK}", f"tt:snz:{TASK}:1d"}
    # terminal -> the proposal is cleared
    assert store.read_proposal(SLOT, now=NOW) is None


def test_renudge_stage_uses_distinct_text(env):
    _commit()
    _park(stage=ic.STAGE_COLD_START_RENUDGE)
    disp.run_dispatch(SLOT, now=NOW, sender=env.sender)
    assert "Still your #1" in env.sent[0]["text"]


# --- nothing to send -------------------------------------------------------

def test_no_proposal_is_a_noop(env):
    assert disp.run_dispatch(SLOT, now=NOW, sender=env.sender)["reason"] == "no-proposal"
    assert env.sent == []


# --- stale / wait states ---------------------------------------------------

def test_cas_stale_clears_and_does_not_send(env):
    _commit()
    _park()
    env.mp.setattr(disp.initiation_contract, "cas_still_valid_now", lambda p: False)
    result = disp.run_dispatch(SLOT, now=NOW, sender=env.sender)
    assert result["reason"] == "cas-stale" and env.sent == []
    assert store.read_proposal(SLOT, now=NOW) is None  # cleared


def test_snoozed_waits_and_leaves_proposal(env):
    _commit()
    _park()
    env.mp.setattr(disp.nag_state, "read_state",
                   lambda: {TASK: {"snoozed_until": (NOW + timedelta(hours=1)).isoformat()}})
    result = disp.run_dispatch(SLOT, now=NOW, sender=env.sender)
    assert result["reason"] == "snoozed" and env.sent == []
    assert store.read_proposal(SLOT, now=NOW) is not None  # NOT cleared -> retry later


def test_calendar_busy_waits_and_leaves_proposal(env):
    _commit()
    _park()
    env.mp.setattr(disp.availability, "not_known_busy", lambda now, **k: False)
    result = disp.run_dispatch(SLOT, now=NOW, sender=env.sender)
    assert result["reason"] == "calendar-busy" and env.sent == []
    assert store.read_proposal(SLOT, now=NOW) is not None


def test_target_unproven_leaves_proposal(env):
    _commit()
    _park()
    env.mp.setattr(disp.nag_delivery, "resolve_target", lambda: {"ok": False, "reason": "env_missing"})
    result = disp.run_dispatch(SLOT, now=NOW, sender=env.sender)
    assert result["reason"] == "target-unproven" and env.sent == []
    assert store.read_proposal(SLOT, now=NOW) is not None


# --- idempotency + precheck-abort ------------------------------------------

def test_already_delivered_does_not_double_send(env):
    _commit()
    _park()
    disp.run_dispatch(SLOT, now=NOW, sender=env.sender)  # first send
    _park()  # re-park (simulate a stale proposal lingering)
    result = disp.run_dispatch(SLOT, now=NOW, sender=env.sender)
    assert len(env.sent) == 1 and result["idempotent"] is True  # outbox dedup


def test_precheck_abort_at_send_does_not_deliver(env):
    _commit()
    _park()
    # CAS passes the pre-flock gate but flips stale exactly at the in-flock precheck.
    flips = iter([True, False])  # 1st call (pre-flock) True, 2nd (precheck) False
    env.mp.setattr(disp.initiation_contract, "cas_still_valid_now", lambda p: next(flips))
    result = disp.run_dispatch(SLOT, now=NOW, sender=env.sender)
    assert result["reason"] == "cas-stale-at-send" and env.sent == []
    assert store.read_proposal(SLOT, now=NOW) is None  # cleared


def test_snooze_tapped_at_send_aborts(env):
    _commit()
    _park()
    # Not snoozed at the pre-flock gate, but a Snooze lands before the held lock: the
    # in-flock precheck's fresh snooze read must abort (no send mid-snooze).
    states = iter([{}, {TASK: {"snoozed_until": (NOW + timedelta(hours=1)).isoformat()}}])
    env.mp.setattr(disp.nag_state, "read_state", lambda: next(states))
    result = disp.run_dispatch(SLOT, now=NOW, sender=env.sender)
    assert result["reason"] == "cas-stale-at-send" and env.sent == []


# --- run_tick (evaluate + store + dispatch) --------------------------------

def test_run_tick_end_to_end_delivers(env):
    _commit()  # committed #1, 100 min ago, unstarted, not snoozed, calendar clear
    result = disp.run_tick(now=NOW, sender=env.sender)
    assert result["sent"] is True and len(env.sent) == 1
    assert env.sent[0]["text"].count(TITLE) == 1


def test_run_tick_no_committed_first(env):
    assert disp.run_tick(now=NOW, sender=env.sender)["reason"] == "no-committed-first"
    assert env.sent == []


def test_run_tick_uses_the_returned_proposal_slot(env):
    # On the hot path (decide_and_store emitted a proposal) the tick must NOT re-derive
    # the slot -- it uses the returned proposal's focus_episode_id.
    _commit()
    env.mp.setattr(disp.initiation_eval, "today_slot",
                   lambda *a, **k: (_ for _ in ()).throw(AssertionError("re-derived slot")))
    assert disp.run_tick(now=NOW, sender=env.sender)["sent"] is True


def test_run_tick_dispatches_a_lingering_proposal(env):
    # This tick emits nothing new, but a prior tick's proposal is still parked -> the
    # tick falls back to today's slot and dispatches it.
    _commit()
    _park()
    env.mp.setattr(disp.initiation_eval, "decide_and_store", lambda now, **k: None)
    result = disp.run_tick(now=NOW, sender=env.sender)
    assert result["sent"] is True and len(env.sent) == 1


# --- cron descriptor (code-only) -------------------------------------------

def test_cron_descriptor_is_a_deterministic_command_template():
    d = disp.initiation_cron_descriptor(scripts_dir="/data/x/scripts")
    assert d["payload"]["kind"] == "command"
    assert d["payload"]["argv"][:2] == ["sh", "-c"]  # U8 parity, not sh -lc
    assert "telegram-commands.sh initiation-tick" in d["payload"]["argv"][2]
    assert "delivery" not in d  # the dispatcher OWNS the send (no cron announce)
    # no real chat id committed anywhere in the template
    assert "-100" not in repr(d)
