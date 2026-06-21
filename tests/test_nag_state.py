"""U4 nag-state layer: Contract-3 shape, terminal ack, archive-on-reopen, locks.

Unit tests for the single state I/O layer that both nag_check (cron) and
nag_commands (reactive) mutate through.
"""

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import nag_state  # noqa: E402

TARGET = {"chat_id": "-4242424242", "topic_id": "2",
          "agent_id": "niemand-work", "channel": "telegram"}


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_MGMT_STATE_DIR", str(tmp_path / "state"))
    yield


def _open(task_id="tsk_a", **kw):
    return nag_state.transition(lambda s: nag_state.open_loop(
        s, task_id, task_title=kw.get("title", "t"),
        threshold_crossed=kw.get("days", 4), threshold_type=kw.get("section", "q2"),
        delivery_target=TARGET))


def _disk():
    path = nag_state.nag_state_path()
    return json.loads(path.read_text()) if path.exists() else {}


def test_open_loop_writes_frozen_contract3_shape():
    _open("tsk_a")
    entry = _disk()["tsk_a"]
    for key in ["nag_loop_id", "ack", "closed_by", "closed_at", "snoozed_until",
                "snooze_count", "block_reason", "nag_count", "delivery_target",
                "body_double_sessions", "archived_nag_loops"]:
        assert key in entry
    assert entry["ack"] is False
    assert entry["delivery_target"] == TARGET


def test_ack_is_terminal_and_reopen_archives_old_loop():
    """ack:true is terminal for a loop; a re-open is a NEW loop_id and the old
    closed loop moves to archived_nag_loops (never silently reactivated)."""
    _open("tsk_a")
    first_loop = _disk()["tsk_a"]["nag_loop_id"]
    nag_state.transition(lambda s: nag_state.close_loop(s, "tsk_a", closed_by="rescheduled"))
    assert _disk()["tsk_a"]["ack"] is True

    _open("tsk_a")  # deliberate re-open
    entry = _disk()["tsk_a"]
    assert entry["ack"] is False
    assert entry["nag_loop_id"] != first_loop  # fresh loop id
    archived = entry["archived_nag_loops"]
    assert len(archived) == 1
    assert archived[0]["nag_loop_id"] == first_loop
    assert archived[0]["closed_by"] == "rescheduled"


def test_record_sent_increments_count_and_stamps_ts():
    _open("tsk_a")
    nag_state.transition(lambda s: nag_state.record_sent(s, "tsk_a"))
    nag_state.transition(lambda s: nag_state.record_sent(s, "tsk_a"))
    entry = _disk()["tsk_a"]
    assert entry["nag_count"] == 2
    assert entry["last_nag_ts"] is not None


def test_snooze_does_not_set_ack():
    _open("tsk_a")
    until = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    nag_state.transition(lambda s: nag_state.apply_snooze(s, "tsk_a",
                                                          snoozed_until=until,
                                                          block_reason="blocked on X"))
    entry = _disk()["tsk_a"]
    assert entry["ack"] is False  # snooze != close
    assert entry["snooze_count"] == 1
    assert entry["block_reason"] == "blocked on X"


def test_is_snoozed_true_within_window_false_after():
    until = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    entry = {"snoozed_until": until}
    assert nag_state.is_snoozed(entry) is True
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    assert nag_state.is_snoozed({"snoozed_until": past}) is False


def test_is_snoozed_treats_garbage_as_not_snoozed():
    """A corrupt snoozed_until must NOT mute a nag forever -- fail toward nagging."""
    assert nag_state.is_snoozed({"snoozed_until": "not-a-date"}) is False
    assert nag_state.is_snoozed({"snoozed_until": None}) is False
    assert nag_state.is_snoozed({}) is False


def test_close_loop_noop_on_missing_entry():
    assert nag_state.transition(
        lambda s: nag_state.close_loop(s, "tsk_missing", closed_by="verified_done")) is None


def test_body_double_session_lifecycle():
    session = {"session_id": "bd_1", "cron_ids": ["c1", "c2"],
               "started_at": nag_state.now_iso(), "ended_at": None}
    nag_state.transition(lambda s: nag_state.add_body_double_session(s, "tsk_a", session))
    entry = _disk()["tsk_a"]
    active = nag_state.active_body_double_session(entry)
    assert active is not None and active["session_id"] == "bd_1"

    nag_state.transition(lambda s: nag_state.end_body_double_session(
        s, "tsk_a", "bd_1", outcome="cancelled"))
    entry = _disk()["tsk_a"]
    assert nag_state.active_body_double_session(entry) is None
    assert entry["body_double_sessions"][0]["outcome"] == "cancelled"


def test_concurrent_transitions_do_not_lose_updates():
    """The sidecar flock serialises read-modify-write so concurrent opens survive."""
    n = 30

    def open_one(i):
        return _open(f"tsk_{i:03d}")

    with ThreadPoolExecutor(max_workers=n) as pool:
        list(pool.map(open_one, range(n)))

    disk = _disk()
    assert len(disk) == n
    assert all(f"tsk_{i:03d}" in disk for i in range(n))
