"""Decision #1 gate<->message seam + Contract 3/4 substrate.

Core invariant (Decision #1): a gated delivery_target is the SOLE permitted
destination for that act_id. A send to any other target is blocked.
"""

import json
import os
import stat
import sys
import warnings
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import autonomy_gate


# Fake chat ids: valid chat-id shape (^-?\d+$) but neither starts with -100, so
# the public-hygiene grep (-100[0-9]{8,}) will not flag them. Real ids are
# env-sourced at runtime, never committed to source.
PRODUCTIVITY_CHAT = "-4242424242"
WORK_GROUP_CHAT_ID = "-5252525252"

# Hermetic productivity env so gate()'s in-gate proof of the delivery_target does
# not depend on the host's secrets.conf. STANDUP=2 and JOURNAL=6 make TOPIC_2 /
# TOPIC_6 below provable targets in the allowlist; without this the gate would
# correctly block them as unproven. TELEGRAM_CHAT_ID_WORK arms the explicit
# work-group reject so test_gate_blocks_work_group_target sees proof_reason
# "work_group" rather than the fall-through "target_unknown".
_PRODUCTIVITY_ENV = {
    "TELEGRAM_CHAT_ID_PRODUCTIVITY": PRODUCTIVITY_CHAT,
    "TELEGRAM_CHAT_ID_WORK": WORK_GROUP_CHAT_ID,
    "OPENCLAW_TOPIC_PRODUCTIVITY_STANDUP": "2",
    "OPENCLAW_TOPIC_PRODUCTIVITY_WEEKLY_REVIEW_PLANNING": "4",
    "OPENCLAW_TOPIC_PRODUCTIVITY_DONE": "5",
    "OPENCLAW_TOPIC_PRODUCTIVITY_JOURNAL": "6",
}


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_MGMT_STATE_DIR", str(tmp_path / "state"))
    for name, value in _PRODUCTIVITY_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("OPENCLAW_TOPIC_PRODUCTIVITY_IDENTITY", raising=False)
    yield


TOPIC_2 = {"chat_id": PRODUCTIVITY_CHAT, "topic_id": "2", "agent_id": "niemand-work", "channel": "telegram"}
TOPIC_6 = {"chat_id": PRODUCTIVITY_CHAT, "topic_id": "6", "agent_id": "niemand-work", "channel": "telegram"}

# Seam-mechanism probe. The gate<->message seam (Decision #1) is orthogonal to the
# v0.1 push-freeze: it must stay testable with an act that actually EXECUTES and
# BINDS a target. ``nag_sent`` is a real rung-3 push and is therefore frozen in
# v0.1 (RUNG3_PUSH_ENABLED=False); an unregistered act type defaults to rung 1
# (draft), so it executes + binds a proven target and exercises the seam without
# being a frozen push. The v0.1 push-freeze itself is asserted separately below
# (test_nag_sent_push_is_frozen_in_v0_1).
SEAM_ACT = "seam_probe_unregistered"


def test_gate_returns_act_id_bound_to_delivery_target():
    result = autonomy_gate.gate(SEAM_ACT, delivery_target=TOPIC_2, unit="U4")
    assert result["ok"] is True
    assert result["act_id"].startswith("act_")
    assert result["delivery_target"] == TOPIC_2


def test_send_to_gated_target_allowed():
    gated = autonomy_gate.gate(SEAM_ACT, delivery_target=TOPIC_2, unit="U4")
    check = autonomy_gate.assert_send_target(gated["act_id"], TOPIC_2)
    assert check["ok"] is True


def test_send_to_non_gated_target_blocked():
    # Gate topic:2, then attempt to send topic:6 -> must be blocked (Decision #1).
    gated = autonomy_gate.gate(SEAM_ACT, delivery_target=TOPIC_2, unit="U4")
    check = autonomy_gate.assert_send_target(gated["act_id"], TOPIC_6)
    assert check["ok"] is False
    assert check["reason"] == "target-mismatch"
    assert check["gated_target"] == TOPIC_2


def test_nag_sent_push_enabled_in_v0_2():
    """nag_sent is a real rung-3 push; v0.2 (U4) ships the delivery seam, so a
    PROVEN target now executes + binds. The proof is NOT relaxed -- an unproven
    target is still blocked at the in-gate prove (see test_gate_blocks_* above)."""
    assert autonomy_gate.rung_for_act_type("nag_sent") == autonomy_gate.RUNG_MONITORED_AUTO
    assert autonomy_gate.RUNG3_PUSH_ENABLED is True
    result = autonomy_gate.gate("nag_sent", delivery_target=TOPIC_2, unit="U4")
    assert result["ok"] is True
    assert result["delivery_target"] == TOPIC_2


def test_send_for_unknown_act_blocked():
    check = autonomy_gate.assert_send_target("act_doesnotexist", TOPIC_2)
    assert check["ok"] is False
    assert check["reason"] == "unknown-act"


def test_rung4_act_is_blocked():
    config = autonomy_gate.ensure_autonomy_config()
    config["act_type_rungs"] = {"email_send": autonomy_gate.RUNG_NEVER_AUTO}
    autonomy_gate._atomic_write(
        autonomy_gate.autonomy_config_path(),
        json.dumps(config, indent=2, sort_keys=True) + "\n",
    )
    result = autonomy_gate.gate("email_send", unit="U5")
    assert result["ok"] is False
    assert result["reason"] == "rung4"


def test_rung2_act_without_snapshot_blocked():
    config = autonomy_gate.ensure_autonomy_config()
    config["act_type_rungs"] = {"board_complete": autonomy_gate.RUNG_APPROVE}
    autonomy_gate._atomic_write(
        autonomy_gate.autonomy_config_path(),
        json.dumps(config, indent=2, sort_keys=True) + "\n",
    )
    result = autonomy_gate.gate("board_complete", unit="U2", reversible=True)
    assert result["ok"] is False
    assert result["reason"] == "missing-snapshot"


def test_snapshot_taken_inside_gate_not_at_proposal():
    config = autonomy_gate.ensure_autonomy_config()
    config["act_type_rungs"] = {"board_complete": autonomy_gate.RUNG_APPROVE}
    autonomy_gate._atomic_write(
        autonomy_gate.autonomy_config_path(),
        json.dumps(config, indent=2, sort_keys=True) + "\n",
    )
    calls = {"n": 0}

    def snapshot_provider():
        calls["n"] += 1
        return {"file": "board.md", "raw_line": "- [ ] x", "line_number": 5}

    result = autonomy_gate.gate(
        "board_complete", unit="U2", reversible=True, snapshot_provider=snapshot_provider
    )
    assert result["ok"] is True
    assert calls["n"] == 1  # provider invoked exactly at gate time (TOCTOU fix)
    assert result["record"]["pre_action_snapshot"]["line_number"] == 5


def test_autonomy_config_has_rung_ladder():
    config = autonomy_gate.ensure_autonomy_config()
    assert config["rungs"] == {
        "0": "read", "1": "draft", "2": "approve",
        "3": "monitored-auto", "4": "never-auto",
    }
    assert config["default_rung_for_unknown"] == 1


def test_nag_ack_stub_writes_without_crashing():
    """Contract 3 stub: /undo of a nag works before U4 exists."""
    entry = autonomy_gate.ack_nag("tsk_abc", ack_type="user_undo")
    assert entry["ack"] is True
    assert entry["ack_type"] == "user_undo"
    # Frozen Contract 3 shape is present.
    for key in ["nag_loop_id", "closed_by", "closed_at", "snoozed_until",
                "snooze_count", "block_reason", "nag_count", "delivery_target",
                "body_double_sessions", "archived_nag_loops"]:
        assert key in entry
    on_disk = json.loads(autonomy_gate.nag_state_path().read_text())
    assert on_disk["tsk_abc"]["ack"] is True


# --- Finding #1: assert_send_target must require status == "executed" ---------

def _write_config_with_override(act_type: str, rung: int) -> None:
    config = autonomy_gate.ensure_autonomy_config()
    config.setdefault("act_type_rungs", {})[act_type] = rung
    autonomy_gate._atomic_write(
        autonomy_gate.autonomy_config_path(),
        json.dumps(config, indent=2, sort_keys=True) + "\n",
    )


def test_assert_send_target_blocks_blocked_rung4_act():
    """A gated-but-BLOCKED rung-4 act is NOT a permitted send (verified-seam bypass)."""
    gated = autonomy_gate.gate("email_send", delivery_target=TOPIC_2, unit="U5")
    assert gated["ok"] is False and gated["reason"] == "rung4"
    # Even though an act_id exists in the log, the send must be refused.
    check = autonomy_gate.assert_send_target(gated["act_id"], TOPIC_2)
    assert check["ok"] is False
    assert check["reason"] == "act-not-authorised"
    assert str(check["status"]).startswith("blocked:")


def test_assert_send_target_blocks_missing_snapshot_act():
    """A rung-2 act blocked for a missing snapshot cannot greenlight a send."""
    _write_config_with_override("board_complete", autonomy_gate.RUNG_APPROVE)
    gated = autonomy_gate.gate("board_complete", delivery_target=TOPIC_2, unit="U2")
    assert gated["ok"] is False and gated["reason"] == "missing-snapshot"
    check = autonomy_gate.assert_send_target(gated["act_id"], TOPIC_2)
    assert check["ok"] is False
    assert check["reason"] == "act-not-authorised"
    assert str(check["status"]).startswith("blocked:")


def test_assert_send_target_ignores_benign_extra_keys():
    """Finding #9: extra keys (e.g. message_id) must not block a legitimate send."""
    gated = autonomy_gate.gate(SEAM_ACT, delivery_target=TOPIC_2, unit="U4")
    attempted = dict(TOPIC_2, message_id=12345)
    check = autonomy_gate.assert_send_target(gated["act_id"], attempted)
    assert check["ok"] is True


def test_assert_send_target_fails_on_missing_canonical_key():
    """Finding #9: a missing canonical key still fails (not a benign extra)."""
    gated = autonomy_gate.gate(SEAM_ACT, delivery_target=TOPIC_2, unit="U4")
    attempted = {k: v for k, v in TOPIC_2.items() if k != "topic_id"}
    check = autonomy_gate.assert_send_target(gated["act_id"], attempted)
    assert check["ok"] is False
    assert check["reason"] == "target-mismatch"


# --- Finding #2: gate() must prove the delivery_target before binding ---------


def test_gate_blocks_work_group_target():
    work_group_target = dict(TOPIC_2, chat_id=WORK_GROUP_CHAT_ID)
    result = autonomy_gate.gate("nag_sent", delivery_target=work_group_target, unit="U4")
    assert result["ok"] is False
    assert result["reason"] == "unproven-target"
    assert result["proof_reason"] == "work_group"
    assert result["record"]["delivery_target"] is None  # binds nothing


def test_gate_blocks_unknown_target():
    unknown = dict(TOPIC_2, topic_id="9999")
    result = autonomy_gate.gate("nag_sent", delivery_target=unknown, unit="U4")
    assert result["ok"] is False
    assert result["reason"] == "unproven-target"
    assert result["proof_reason"] == "target_unknown"
    assert result["record"]["delivery_target"] is None


def test_gate_blocks_when_env_missing(monkeypatch):
    for name in [
        "TELEGRAM_CHAT_ID_PRODUCTIVITY",
        "OPENCLAW_TOPIC_PRODUCTIVITY_STANDUP",
        "OPENCLAW_TOPIC_PRODUCTIVITY_WEEKLY_REVIEW_PLANNING",
        "OPENCLAW_TOPIC_PRODUCTIVITY_DONE",
        "OPENCLAW_TOPIC_PRODUCTIVITY_JOURNAL",
        "OPENCLAW_TOPIC_PRODUCTIVITY_IDENTITY",
    ]:
        monkeypatch.delenv(name, raising=False)
    result = autonomy_gate.gate("nag_sent", delivery_target=TOPIC_2, unit="U4")
    assert result["ok"] is False
    assert result["reason"] == "unproven-target"
    assert result["proof_reason"] == "env_missing"
    assert result["record"]["delivery_target"] is None


def test_gate_normalises_int_chat_id():
    """Finding #2 footgun: int chat/topic ids normalise to the canonical str target."""
    int_target = {"chat_id": int(PRODUCTIVITY_CHAT), "topic_id": 2,
                  "agent_id": "niemand-work", "channel": "telegram"}
    result = autonomy_gate.gate(SEAM_ACT, delivery_target=int_target, unit="U4")
    assert result["ok"] is True
    assert result["delivery_target"] == TOPIC_2  # str-normalised


# --- Finding #5: reversible=False is not a snapshot escape hatch --------------

def test_rung2_reversible_false_without_snapshot_is_blocked():
    _write_config_with_override("board_complete", autonomy_gate.RUNG_APPROVE)
    result = autonomy_gate.gate(
        "board_complete", delivery_target=TOPIC_2, unit="U2", reversible=False
    )
    assert result["ok"] is False
    assert result["reason"] == "missing-snapshot"


# --- Finding #3: corrupt autonomy-config fails CLOSED, anchors rung-4 ---------

def test_email_send_anchored_at_rung4_in_code():
    """With no config override at all, email_send resolves to rung 4 from code."""
    assert autonomy_gate.rung_for_act_type("email_send") == autonomy_gate.RUNG_NEVER_AUTO


def test_corrupt_config_still_resolves_email_send_rung4_and_does_not_erase():
    path = autonomy_gate.autonomy_config_path()
    autonomy_gate.state_dir()
    path.write_text("{ this is not valid json :::", encoding="utf-8")
    # Must NOT crash, must resolve email_send to rung 4 (fail closed).
    assert autonomy_gate.rung_for_act_type("email_send") == autonomy_gate.RUNG_NEVER_AUTO
    # The corrupt bytes are renamed aside, not erased.
    corrupt_siblings = list(path.parent.glob(path.name + ".corrupt-*"))
    assert corrupt_siblings, "corrupt config should be renamed aside, not destroyed"
    assert "this is not valid json" in corrupt_siblings[0].read_text()
    # A system_error was logged.
    log = autonomy_gate.read_autonomy_log()
    assert any(r.get("act_type") == "system_error" for r in log)


def test_garbage_rung_override_is_ignored_for_email_send():
    """A JSON override that tries to downgrade email_send with garbage is ignored."""
    config = autonomy_gate.ensure_autonomy_config()
    config["act_type_rungs"] = {"email_send": "not-an-int"}
    autonomy_gate._atomic_write(
        autonomy_gate.autonomy_config_path(),
        json.dumps(config, indent=2, sort_keys=True) + "\n",
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        assert autonomy_gate.rung_for_act_type("email_send") == autonomy_gate.RUNG_NEVER_AUTO


def test_out_of_range_rung_override_is_ignored():
    config = autonomy_gate.ensure_autonomy_config()
    config["act_type_rungs"] = {"email_send": 99}
    autonomy_gate._atomic_write(
        autonomy_gate.autonomy_config_path(),
        json.dumps(config, indent=2, sort_keys=True) + "\n",
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        assert autonomy_gate.rung_for_act_type("email_send") == autonomy_gate.RUNG_NEVER_AUTO


def test_valid_override_for_known_act_type_applies():
    """A valid in-range override for a known act_type does apply."""
    config = autonomy_gate.ensure_autonomy_config()
    config["act_type_rungs"] = {"nag_sent": autonomy_gate.RUNG_MONITORED_AUTO}
    autonomy_gate._atomic_write(
        autonomy_gate.autonomy_config_path(),
        json.dumps(config, indent=2, sort_keys=True) + "\n",
    )
    assert autonomy_gate.rung_for_act_type("nag_sent") == autonomy_gate.RUNG_MONITORED_AUTO


# --- Finding #4: state perms 0o700 dir / 0o600 files -------------------------

def _mode(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def test_state_dir_is_0700():
    d = autonomy_gate.state_dir()
    assert _mode(d) == 0o700


def test_autonomy_log_and_config_are_0600():
    autonomy_gate.gate(SEAM_ACT, delivery_target=TOPIC_2, unit="U4")
    autonomy_gate.ensure_autonomy_config()
    assert _mode(autonomy_gate.autonomy_log_path()) == 0o600
    assert _mode(autonomy_gate.autonomy_config_path()) == 0o600


def test_nag_state_is_0600():
    autonomy_gate.ack_nag("tsk_perm", ack_type="user_undo")
    assert _mode(autonomy_gate.nag_state_path()) == 0o600


# --- Finding #7: ack_nag concurrency (no lost update) ------------------------

def test_concurrent_ack_nag_keeps_all_entries():
    n = 40

    def ack(i: int):
        return autonomy_gate.ack_nag(f"tsk_{i:04d}", ack_type="user_undo")

    with ThreadPoolExecutor(max_workers=n) as pool:
        list(pool.map(ack, range(n)))

    state = json.loads(autonomy_gate.nag_state_path().read_text())
    assert len(state) == n
    assert all(f"tsk_{i:04d}" in state for i in range(n))
    assert all(state[f"tsk_{i:04d}"]["ack"] is True for i in range(n))


# --- Finding #8: tolerant log read + nag-state rename-aside ------------------

def test_read_autonomy_log_tolerates_torn_line():
    autonomy_gate.gate(SEAM_ACT, delivery_target=TOPIC_2, unit="U4")
    log_path = autonomy_gate.autonomy_log_path()
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write('{"act_id": "act_torn", "incomplete  \n')  # torn line
    autonomy_gate.gate(SEAM_ACT, delivery_target=TOPIC_2, unit="U4")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        records = autonomy_gate.read_autonomy_log()
    # The two good gate records survive; the torn line is skipped, not raised.
    assert len([r for r in records if r.get("act_type") == SEAM_ACT]) == 2


def test_assert_send_target_survives_torn_log_line():
    gated = autonomy_gate.gate(SEAM_ACT, delivery_target=TOPIC_2, unit="U4")
    with autonomy_gate.autonomy_log_path().open("a", encoding="utf-8") as fh:
        fh.write("{not valid json at all\n")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        check = autonomy_gate.assert_send_target(gated["act_id"], TOPIC_2)
    assert check["ok"] is True  # one torn line does not break the seam


def test_corrupt_nag_state_renamed_aside_not_destroyed():
    autonomy_gate.ack_nag("tsk_live", ack_type="user_undo")
    nag_path = autonomy_gate.nag_state_path()
    nag_path.write_text("{ totally corrupt :::", encoding="utf-8")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        state = autonomy_gate._read_nag_state()
    assert state == {}  # fresh empty, not a crash
    corrupt = list(nag_path.parent.glob(nag_path.name + ".corrupt-*"))
    assert corrupt, "corrupt nag-state must be renamed aside, not silently dropped"
    assert "totally corrupt" in corrupt[0].read_text()


# --- Finding #10: find_act binds the FIRST executed record -------------------

def test_find_act_binds_first_executed_record():
    """A later forged append for the same act_id cannot rebind the gated target."""
    gated = autonomy_gate.gate(SEAM_ACT, delivery_target=TOPIC_2, unit="U4")
    act_id = gated["act_id"]
    # Forge a later record for the same act_id pointing at a different target.
    forged = dict(gated["record"], delivery_target=dict(TOPIC_2, topic_id="6"))
    autonomy_gate._append_autonomy_log(forged)
    bound = autonomy_gate.find_act(act_id)
    assert bound["delivery_target"] == TOPIC_2  # canonical first-record binding
    # And the seam still rejects a send to the forged topic:6.
    check = autonomy_gate.assert_send_target(act_id, dict(TOPIC_2, topic_id="6"))
    assert check["ok"] is False


def test_forged_executed_cannot_override_first_blocked_record():
    """A forged later 'executed' append cannot un-block a first-blocked act_id."""
    blocked = autonomy_gate.gate("email_send", delivery_target=TOPIC_2, unit="U5")
    act_id = blocked["act_id"]
    assert blocked["ok"] is False and str(blocked["reason"]) == "rung4"
    # Forge a later executed record for the same act_id pointing at TOPIC_2.
    forged = dict(blocked["record"], status="executed", delivery_target=TOPIC_2)
    autonomy_gate._append_autonomy_log(forged)
    # The canonical (first) record is still the blocked one.
    bound = autonomy_gate.find_act(act_id)
    assert str(bound["status"]).startswith("blocked:")
    # And the seam still refuses the send -- forgery did not authorise it.
    check = autonomy_gate.assert_send_target(act_id, TOPIC_2)
    assert check["ok"] is False
    assert check["reason"] == "act-not-authorised"


def test_send_blocked_when_act_has_no_bound_target():
    """gate() with delivery_target=None executes but binds no target; any later
    send for that act_id is refused (no-gated-target), not silently allowed."""
    result = autonomy_gate.gate(SEAM_ACT, delivery_target=None, unit="U4")
    assert result["ok"] is True
    assert result["record"]["delivery_target"] is None
    check = autonomy_gate.assert_send_target(result["act_id"], TOPIC_2)
    assert check["ok"] is False
    assert check["reason"] == "no-gated-target"


def test_send_with_none_attempted_target_is_blocked():
    """A send that names no target at all is refused, not treated as a match."""
    gated = autonomy_gate.gate(SEAM_ACT, delivery_target=TOPIC_2, unit="U4")
    check = autonomy_gate.assert_send_target(gated["act_id"], None)
    assert check["ok"] is False
    assert check["reason"] == "target-mismatch"
