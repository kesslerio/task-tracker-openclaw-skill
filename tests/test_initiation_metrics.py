"""v0.4-C initiation metrics: pure per-arm initiation-success read over the ledger."""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import initiation_metrics as metrics  # noqa: E402
import task_ledger  # noqa: E402

# Real-now-relative so the ledger's retention prune never drops these recent events.
NOW = datetime.now(timezone.utc).replace(microsecond=0)


def _ago(minutes):
    return NOW - timedelta(minutes=minutes)


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_TRACKER_LEDGER_FILE", str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv("TASK_MGMT_STATE_DIR", str(tmp_path / "state"))

    def _append(event_type, task_id, ts, **meta):
        event = task_ledger.new_event(event_type, task_id=task_id, metadata=meta)
        event["timestamp"] = ts.isoformat()
        task_ledger.append_event(event)

    return _append


def test_per_arm_success_rates_and_pending(ledger):
    ledger("initiation_sent", "tsk_a", _ago(60), arm="treatment", stage="cold_start")
    ledger("start_session_started", "tsk_a", _ago(40))                    # 20m after -> success
    ledger("initiation_sent", "tsk_b", _ago(60), arm="treatment", stage="cold_start")  # no start -> miss
    ledger("initiation_suppressed_holdout", "tsk_c", _ago(60), arm="control", stage="cold_start")
    ledger("body_double_started", "tsk_c", _ago(30))                      # 30m after -> success
    ledger("initiation_suppressed_holdout", "tsk_d", _ago(60), arm="control", stage="cold_start")  # no start
    ledger("initiation_sent", "tsk_e", _ago(10), arm="treatment", stage="cold_start")  # window open -> pending

    s = metrics.summarize(now=NOW)
    assert s["treatment"] == {"decisions": 2, "started_within": 1, "pending": 1, "start_rate": 0.5}
    assert s["control"] == {"decisions": 2, "started_within": 1, "pending": 0, "start_rate": 0.5}
    assert s["valid_holdouts"] == 2
    assert [m["task_id"] for m in s["miss_candidates"]] == ["tsk_b"]
    assert s["already_started_fp"] == []
    assert s["window_min"] == 45


def test_start_after_window_is_not_a_success(ledger):
    ledger("initiation_sent", "tsk_a", _ago(60), arm="treatment", stage="cold_start")
    ledger("start_session_started", "tsk_a", _ago(5))  # 55m after -> outside the 45m window
    s = metrics.summarize(now=NOW)
    assert s["treatment"]["started_within"] == 0
    assert [m["task_id"] for m in s["miss_candidates"]] == ["tsk_a"]


def test_already_started_before_decision_is_flagged(ledger):
    ledger("initiation_sent", "tsk_f", _ago(60), arm="treatment", stage="cold_start")
    ledger("start_session_started", "tsk_f", _ago(65))  # BEFORE the decision -> auto FP
    s = metrics.summarize(now=NOW)
    assert [f["task_id"] for f in s["already_started_fp"]] == ["tsk_f"]


def test_empty_ledger_is_zeroed(ledger):
    s = metrics.summarize(now=NOW)
    assert s["valid_holdouts"] == 0
    assert s["treatment"]["start_rate"] is None and s["control"]["start_rate"] is None
