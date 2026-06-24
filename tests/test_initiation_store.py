"""v0.4-C initiation proposal store: write/read/clear/supersede/expire, with
expired entries pruned on read and write and a corrupt file read as empty."""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import initiation_contract as ic  # noqa: E402
import initiation_store as store  # noqa: E402

TASK = "tsk_aaaaaaaaaaaaaaaa"
DATE = "2026-06-24"
SLOT = "work:tsk_aaaaaaaaaaaaaaaa:2026-06-24"


def _proposal(*, stage=ic.STAGE_COLD_START, expires="2026-06-24T19:00:00+00:00",
              task=TASK, date=DATE):
    slot = ic.focus_episode_slot("work", task, date)
    return ic.Proposal(
        focus_episode_id=slot, task_id=task, user_scope="work", local_date=date,
        stage=stage, reason_code="committed_unstarted",
        created_at="2026-06-24T18:00:00+00:00", expires_at=expires,
        cas_focus_state_rev=1, cas_no_session_since="2026-06-24T18:00:00+00:00")


def _at(hhmm: str) -> datetime:
    return datetime.fromisoformat(f"2026-06-24T{hhmm}:00+00:00")


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_MGMT_STATE_DIR", str(tmp_path / "state"))
    return tmp_path / "state"


def test_write_then_read_round_trips(state):
    p = _proposal()
    store.write_proposal(p, now=_at("18:00"))
    got = store.read_proposal(SLOT, now=_at("18:30"))
    assert got == p


def test_read_missing_is_none(state):
    assert store.read_proposal(SLOT, now=_at("18:30")) is None


def test_write_supersedes_same_slot(state):
    store.write_proposal(_proposal(stage=ic.STAGE_COLD_START), now=_at("18:00"))
    store.write_proposal(_proposal(stage=ic.STAGE_COLD_START_RENUDGE), now=_at("18:40"))
    got = store.read_proposal(SLOT, now=_at("18:45"))
    assert got.stage == ic.STAGE_COLD_START_RENUDGE


def test_expired_proposal_reads_as_none_and_is_pruned(state):
    store.write_proposal(_proposal(expires="2026-06-24T18:15:00+00:00"), now=_at("18:00"))
    assert store.read_proposal(SLOT, now=_at("18:30")) is None  # past expiry
    # the prune-on-read dropped it from disk
    import json
    on_disk = json.loads(store.store_path().read_text())
    assert SLOT not in on_disk


def test_write_prunes_other_expired_entries(state):
    other = _proposal(task="tsk_bbbbbbbbbbbbbbbb", expires="2026-06-24T18:05:00+00:00")
    store.write_proposal(other, now=_at("18:00"))
    # a later write (after the other's expiry) prunes the stale sibling
    store.write_proposal(_proposal(), now=_at("18:30"))
    import json
    on_disk = json.loads(store.store_path().read_text())
    assert other.focus_episode_id not in on_disk
    assert SLOT in on_disk


def test_clear_removes_the_proposal(state):
    store.write_proposal(_proposal(), now=_at("18:00"))
    store.clear_proposal(SLOT)
    assert store.read_proposal(SLOT, now=_at("18:30")) is None


def test_clear_missing_is_noop(state):
    store.clear_proposal(SLOT)  # must not raise


def test_corrupt_store_reads_as_empty(state):
    store.store_path().parent.mkdir(parents=True, exist_ok=True)
    store.store_path().write_text("{ not json", encoding="utf-8")
    assert store.read_proposal(SLOT, now=_at("18:30")) is None


def test_store_file_and_lock_are_owner_only(state):
    store.write_proposal(_proposal(), now=_at("18:00"))
    assert (store.store_path().stat().st_mode & 0o777) == 0o600
    assert (store.store_lock_path().stat().st_mode & 0o777) == 0o600


def test_unreconstructable_entry_reads_as_none(state):
    # A non-expired but structurally-broken entry (focus_episode_id no longer
    # matches its parts -> Proposal.__post_init__ raises) must read as None (and is
    # pruned), never reconstructed into an actionable proposal.
    store.write_proposal(_proposal(), now=_at("18:00"))
    raw = json.loads(store.store_path().read_text())
    raw[SLOT]["focus_episode_id"] = "work:tsk_tampered:2026-06-24"  # mismatches parts
    store.store_path().write_text(json.dumps(raw), encoding="utf-8")
    assert store.read_proposal(SLOT, now=_at("18:30")) is None
