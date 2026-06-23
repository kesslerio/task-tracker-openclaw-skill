import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import harvest_state


def test_pre_u1_state_loads_defaults_and_round_trips_seen_hashes(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_MGMT_STATE_DIR", str(tmp_path))
    wid = "2026-W26"
    legacy = {
        "schema_version": 1,
        "harvest_window_id": wid,
        "seen_hashes": ["sha256:github:legacy"],
    }
    (tmp_path / "harvest-state.json").write_text(json.dumps(legacy, indent=2) + "\n", encoding="utf-8")

    state, expired = harvest_state.load_or_reset(wid)

    assert expired == []
    assert state["run_id"]
    assert state["watermarks"] == {}
    assert state["seen_provider_states"] == {}
    assert state["seen_hashes"] == ["sha256:github:legacy"]

    harvest_state.save_state(state)
    saved = harvest_state.load_state()
    assert saved["seen_hashes"] == ["sha256:github:legacy"]
    assert saved["watermarks"] == {}
    assert saved["seen_provider_states"] == {}


def test_new_window_state_has_run_id_watermarks_and_state_dedup_map():
    state = harvest_state.new_window_state("2026-W26:2026-06-23:standup", run_id="run-fixed")

    assert state["run_id"] == "run-fixed"
    assert state["watermarks"] == {}
    assert state["seen_provider_states"] == {}


def test_seen_is_provider_state_aware():
    state = harvest_state.new_window_state("2026-W26")
    harvest_state.mark_seen(
        state,
        [{"evidence_hash": "sha256:calendar:abc", "provider_state": "accepted"}],
    )

    assert harvest_state.is_seen(state, "sha256:calendar:abc", "accepted") is True
    assert harvest_state.is_seen(state, "sha256:calendar:abc", "cancelled") is False
    assert harvest_state.is_seen(state, "sha256:calendar:abc") is True


def test_overlap_reread_same_identity_same_state_is_skipped():
    state = harvest_state.new_window_state("2026-W26")
    item = {"evidence_hash": "sha256:github:abc", "provider_state": "merged:1"}
    harvest_state.mark_seen(state, [item])

    fresh = [item for item in [item] if not harvest_state.is_seen(state, item["evidence_hash"], item["provider_state"])]

    assert fresh == []


def test_window_mismatch_save_keeps_newer_on_disk_state(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_MGMT_STATE_DIR", str(tmp_path))
    newer = harvest_state.new_window_state("2026-W27", run_id="newer")
    newer["seen_hashes"] = ["sha256:github:newer"]
    harvest_state.save_state(newer)

    older = harvest_state.new_window_state("2026-W26", run_id="older")
    older["seen_hashes"] = ["sha256:github:older"]
    harvest_state.save_state(older)

    saved = harvest_state.load_state()
    assert saved["harvest_window_id"] == "2026-W27"
    assert saved["seen_hashes"] == ["sha256:github:newer"]
    assert saved["run_id"] == "newer"


def test_none_scalar_does_not_overwrite_existing_value(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_MGMT_STATE_DIR", str(tmp_path))
    wid = "2026-W26"
    existing = harvest_state.new_window_state(wid)
    existing["auto_pushed_window"] = wid
    harvest_state.save_state(existing)

    incoming = harvest_state.new_window_state(wid)
    incoming["auto_pushed_window"] = None
    harvest_state.save_state(incoming)

    assert harvest_state.load_state()["auto_pushed_window"] == wid


def test_max_iso_compares_instants_not_lexical_strings():
    assert (
        harvest_state._max_iso(
            "2026-06-22T23:00:00-07:00",
            "2026-06-23T01:00:00-08:00",
        )
        == "2026-06-23T01:00:00-08:00"
    )


def test_mark_seen_ignores_empty_provider_state():
    state = harvest_state.new_window_state("2026-W26")

    harvest_state.mark_seen(
        state,
        [{"evidence_hash": "sha256:x:y", "provider_state": ""}],
    )

    assert state["seen_hashes"] == ["sha256:x:y"]
    assert state["seen_provider_states"] == {}


def test_concurrent_saves_merge_seen_hashes_and_watermarks(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_MGMT_STATE_DIR", str(tmp_path))
    wid = "2026-W26:2026-06-23:standup"
    first, _ = harvest_state.load_or_reset(wid)
    second, _ = harvest_state.load_or_reset(wid)

    harvest_state.mark_seen(first, [{"evidence_hash": "sha256:github:a", "provider_state": "merged"}])
    harvest_state.mark_watermark(first, "github", "2026-06-22T10:00:00-07:00")
    harvest_state.mark_seen(second, [{"evidence_hash": "sha256:gmail:b", "provider_state": "sent"}])
    harvest_state.mark_watermark(second, "gmail", "2026-06-22T11:00:00-07:00")

    harvest_state.save_state(first)
    harvest_state.save_state(second)

    saved = harvest_state.load_state()
    assert sorted(saved["seen_hashes"]) == ["sha256:github:a", "sha256:gmail:b"]
    assert saved["seen_provider_states"] == {
        "sha256:github:a": "merged",
        "sha256:gmail:b": "sent",
    }
    assert saved["watermarks"] == {
        "github": "2026-06-22T10:00:00-07:00",
        "gmail": "2026-06-22T11:00:00-07:00",
    }
    assert (tmp_path / "harvest-state.json").exists()
    assert not list(tmp_path.glob("harvest-state.json.corrupt-*"))


def test_locked_update_window_state_merges_cron_and_manual(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_MGMT_STATE_DIR", str(tmp_path))
    wid = "2026-W26:2026-06-23:standup"

    def write_github() -> None:
        def mutate(state):
            harvest_state.mark_seen(state, [{"evidence_hash": "sha256:github:a", "provider_state": "merged"}])
            harvest_state.mark_watermark(state, "github", "2026-06-22T10:00:00-07:00")

        harvest_state.update_window_state(wid, mutate)

    def write_gmail() -> None:
        def mutate(state):
            harvest_state.mark_seen(state, [{"evidence_hash": "sha256:gmail:b", "provider_state": "sent"}])
            harvest_state.mark_watermark(state, "gmail", "2026-06-22T11:00:00-07:00")

        harvest_state.update_window_state(wid, mutate)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda fn: fn(), [write_github, write_gmail]))

    saved = json.loads((tmp_path / "harvest-state.json").read_text(encoding="utf-8"))
    assert sorted(saved["seen_hashes"]) == ["sha256:github:a", "sha256:gmail:b"]
    assert saved["watermarks"]["github"] == "2026-06-22T10:00:00-07:00"
    assert saved["watermarks"]["gmail"] == "2026-06-22T11:00:00-07:00"
    assert len(list(tmp_path.glob("harvest-state.json"))) == 1
