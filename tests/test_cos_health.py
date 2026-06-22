"""H4 machine-visible health substrate (cos-health.json).

Invariants pinned here:
- record_success then read_health shows the success ts for that ritual;
- record_failure records error_class/ts/trigger AND does NOT clobber a prior
  last_success_ts (last good run and last bad run coexist);
- record_success does NOT clobber a prior last_failure;
- a missing/corrupt file reads as {} without raising (never crashes a ritual);
- concurrent writes survive the flock (a round-trip under threads).

Fake ids only; no real openclaw.
"""

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import cos_health  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_MGMT_STATE_DIR", str(tmp_path / "state"))
    yield


def _disk():
    path = cos_health.health_path()
    return json.loads(path.read_text()) if path.exists() else {}


# --- record_success --------------------------------------------------------


def test_record_success_stamps_last_success_ts():
    cos_health.record_success("standup")
    health = cos_health.read_health()
    assert "last_success_ts" in health["standup"]
    assert health["standup"]["last_success_ts"]  # a non-empty ts string
    # read_health reflects what landed on disk.
    assert _disk()["standup"]["last_success_ts"] == health["standup"]["last_success_ts"]


# --- record_failure --------------------------------------------------------


def test_record_failure_records_class_ts_trigger():
    cos_health.record_failure("nag_check", error_class="transient", trigger="cron:abc")
    entry = cos_health.read_health()["nag_check"]
    assert entry["last_failure"]["error_class"] == "transient"
    assert entry["last_failure"]["trigger"] == "cron:abc"
    assert entry["last_failure"]["ts"]
    # The top-level mirror lets a poller age the failure without descending.
    assert entry["last_failure_ts"] == entry["last_failure"]["ts"]


def test_record_failure_does_not_clobber_last_success_ts():
    """The KEY invariant: a failure preserves the record of the last good run."""
    cos_health.record_success("standup")
    success_ts = cos_health.read_health()["standup"]["last_success_ts"]
    cos_health.record_failure("standup", error_class="auth", trigger="cron:x")
    entry = cos_health.read_health()["standup"]
    assert entry["last_success_ts"] == success_ts  # NOT clobbered
    assert entry["last_failure"]["error_class"] == "auth"


def test_record_success_does_not_clobber_last_failure():
    """Symmetric: a clean run records success without erasing the last failure."""
    cos_health.record_failure("standup", error_class="environment", trigger="cron:y")
    cos_health.record_success("standup")
    entry = cos_health.read_health()["standup"]
    assert entry["last_failure"]["error_class"] == "environment"  # still there
    assert entry["last_success_ts"]


def test_record_failure_trigger_defaults_to_none():
    cos_health.record_failure("weekly_review", error_class="transient")
    assert cos_health.read_health()["weekly_review"]["last_failure"]["trigger"] is None


# --- read_health fail-soft --------------------------------------------------


def test_read_health_missing_file_is_empty_dict():
    assert cos_health.read_health() == {}


def test_read_health_corrupt_file_is_empty_dict_without_raising():
    cos_health.health_path().parent.mkdir(parents=True, exist_ok=True)
    cos_health.health_path().write_text("{ this is not json")
    # A corrupt file must NEVER crash a ritual -- it degrades to {}.
    assert cos_health.read_health() == {}


def test_read_health_non_dict_json_is_empty_dict():
    cos_health.health_path().parent.mkdir(parents=True, exist_ok=True)
    cos_health.health_path().write_text("[1, 2, 3]")
    assert cos_health.read_health() == {}


# --- concurrency ------------------------------------------------------------


def test_concurrent_records_do_not_lose_updates():
    """The sidecar flock serialises read-modify-write so concurrent records survive."""
    n = 30

    def record_one(i):
        cos_health.record_success(f"ritual_{i:03d}")

    with ThreadPoolExecutor(max_workers=n) as pool:
        list(pool.map(record_one, range(n)))

    health = cos_health.read_health()
    assert len(health) == n
    assert all(f"ritual_{i:03d}" in health for i in range(n))
