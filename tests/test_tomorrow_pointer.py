"""U6 tomorrow-pointer: behavioral invariants of the EOD write side of the loop.

These assert the invariants, not the implementation path:

* SINGLE CANONICAL POINTER: setting a #1 writes ``tomorrow-pointer.json`` with the
  task + ``source:"eod"``; re-setting a DIFFERENT task OVERWRITES it (never appends) --
  there is exactly one "tomorrow's #1".
* EXPLICIT NONE: an empty-board EOD writes an explicit ``task_id: null`` "none" pointer
  so the standup shows a clean board, NOT a stale prior-day #1.
* STALE OVERWRITE: a prior-day pointer left on disk is overwritten by the next set,
  never accumulated.
* CORRUPT/MISSING FAILS OPEN: a missing or corrupt file reads as ``None`` (no pointer),
  never a crash -- the standup degrades to "pick a #1".
* ATOMIC WRITE: writes go through the sidecar flock + temp-file swap (no torn read) and
  a single canonical file is left behind (no ``.tmp`` residue).

Public-repo hygiene: no real chat/topic ids appear here; the only ids are fake task
ids (``tsk_*``), which the CI hygiene grep does not flag.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import tomorrow_pointer  # noqa: E402


@pytest.fixture
def state(tmp_path, monkeypatch):
    """Isolate the pointer + its sidecar lock under a tmp state dir."""
    state_dir = tmp_path / "state"
    monkeypatch.setenv("TASK_MGMT_STATE_DIR", str(state_dir))
    return state_dir


# --- single canonical pointer + source ----------------------------------------


def test_set_top_writes_pointer_with_source_eod(state):
    written = tomorrow_pointer.set_top("tsk_abc123", "Re-evaluate ActiveCampaign")

    assert written["task_id"] == "tsk_abc123"
    assert written["title"] == "Re-evaluate ActiveCampaign"
    assert written["source"] == "eod"
    assert written["set_at"]  # a real timestamp was stamped

    on_disk = json.loads((state / "tomorrow-pointer.json").read_text())
    assert on_disk["task_id"] == "tsk_abc123"
    assert on_disk["source"] == "eod"


def test_read_pointer_round_trips_the_written_pointer(state):
    tomorrow_pointer.set_top("tsk_abc123", "A title")
    pointer = tomorrow_pointer.read_pointer()
    assert pointer is not None
    assert pointer["task_id"] == "tsk_abc123"
    assert pointer["title"] == "A title"


# --- re-setting a different task OVERWRITES (single pointer, never appends) ----


def test_resetting_a_different_task_overwrites_single_pointer(state):
    tomorrow_pointer.set_top("tsk_abc123", "First pick")
    tomorrow_pointer.set_top("tsk_def456", "Second pick")

    # Exactly ONE pointer remains, and it is the LAST set (single canonical pointer).
    pointer = tomorrow_pointer.read_pointer()
    assert pointer["task_id"] == "tsk_def456"
    assert pointer["title"] == "Second pick"

    # The file is a single JSON object, not an appended log of two records.
    raw = (state / "tomorrow-pointer.json").read_text()
    assert raw.count('"task_id"') == 1
    json.loads(raw)  # parses as a single object, not concatenated JSON


# --- no open tasks -> explicit "none" -----------------------------------------


def test_set_none_records_explicit_none_pointer(state):
    written = tomorrow_pointer.set_none()
    assert written["task_id"] is None
    assert written["source"] == "eod"

    pointer = tomorrow_pointer.read_pointer()
    # An explicit "none" is NOT a missing file: it's a real, dated record.
    assert pointer is not None
    assert pointer["task_id"] is None
    assert tomorrow_pointer.is_none_pointer(pointer) is True


def test_none_overwrites_a_prior_real_pointer(state):
    tomorrow_pointer.set_top("tsk_abc123", "Yesterday's #1")
    tomorrow_pointer.set_none()

    pointer = tomorrow_pointer.read_pointer()
    assert tomorrow_pointer.is_none_pointer(pointer) is True
    assert pointer["task_id"] is None


# --- a stale prior-day pointer is overwritten, never appended ------------------


def test_stale_prior_day_pointer_is_overwritten(state):
    # Simulate a stale pointer left on disk from a prior day (hand-written).
    (state).mkdir(parents=True, exist_ok=True)
    (state / "tomorrow-pointer.json").write_text(json.dumps({
        "schema_version": 1, "task_id": "tsk_stale", "title": "Stale",
        "set_at": "2026-06-01T00:00:00+00:00", "source": "eod",
    }))

    tomorrow_pointer.set_top("tsk_fresh", "Fresh #1")

    pointer = tomorrow_pointer.read_pointer()
    assert pointer["task_id"] == "tsk_fresh"
    # The stale record is gone, not merely shadowed.
    assert "tsk_stale" not in (state / "tomorrow-pointer.json").read_text()


# --- corrupt / missing fails open to no-pointer (never raises) -----------------


def test_missing_file_reads_as_none(state):
    assert tomorrow_pointer.read_pointer() is None
    # is_none_pointer over a missing file is False (that's "EOD never ran", not "none").
    assert tomorrow_pointer.is_none_pointer(None) is False


def test_corrupt_file_reads_as_none_not_a_crash(state):
    state.mkdir(parents=True, exist_ok=True)
    (state / "tomorrow-pointer.json").write_text("{ this is not json")
    assert tomorrow_pointer.read_pointer() is None


def test_non_object_file_reads_as_none(state):
    state.mkdir(parents=True, exist_ok=True)
    (state / "tomorrow-pointer.json").write_text("[1, 2, 3]")
    assert tomorrow_pointer.read_pointer() is None


# --- atomic write: a single canonical file, no torn read, no temp residue ------


def test_atomic_write_leaves_a_single_clean_file_no_temp_residue(state):
    tomorrow_pointer.set_top("tsk_abc123", "A title")
    # No leftover *.tmp from the atomic temp-file swap.
    residue = [p.name for p in state.iterdir() if p.suffix == ".tmp"]
    assert residue == []
    # The pointer file parses as one clean object every time (no torn/duplicated write).
    json.loads((state / "tomorrow-pointer.json").read_text())


def test_write_holds_the_sidecar_lock_path(state):
    # The sidecar lock lives beside the data file (the flock target), so the atomic
    # os.replace of the data file can never orphan the lock. After a write both exist.
    tomorrow_pointer.set_top("tsk_abc123", "A title")
    assert (state / "tomorrow-pointer.json").exists()
    assert (state / "tomorrow-pointer.lock").exists()
