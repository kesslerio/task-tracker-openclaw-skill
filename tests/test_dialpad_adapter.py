import json
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import standup
import standup_harvest
import utils
from adapters import dialpad_adapter
import harvest_window

CONTACT = "-4242424242"
RAW_BODY = "raw fixture body that must stay private"


def _resolved(day: date = date(2026, 6, 23)):
    return harvest_window.resolve_standup_window(target_date=day)


def _epoch(value: str) -> int:
    return int(datetime.fromisoformat(value).astimezone(timezone.utc).timestamp())


def _db(tmp_path: Path) -> Path:
    path = tmp_path / "sms.db"
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE messages (
            dialpad_id INTEGER UNIQUE,
            contact_number TEXT,
            direction TEXT,
            timestamp INTEGER,
            text TEXT
        )
        """
    )
    conn.commit()
    conn.close()
    return path


def _insert(path: Path, dialpad_id: int, direction: str, timestamp: str, text: str, contact: str = CONTACT) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO messages (dialpad_id, contact_number, direction, timestamp, text) VALUES (?, ?, ?, ?, ?)",
        (dialpad_id, contact, direction, _epoch(timestamp), text),
    )
    conn.commit()
    conn.close()


def test_three_outbound_messages_emit_one_metadata_only_activity(tmp_path, monkeypatch):
    path = _db(tmp_path)
    monkeypatch.setenv("DIALPAD_SMS_DB", str(path))
    _insert(path, 1, "outbound", "2026-06-23T09:00:00-07:00", f"First {RAW_BODY}")
    _insert(path, 2, "outbound", "2026-06-23T10:00:00-07:00", "Second useful reply")
    _insert(path, 3, "outbound", "2026-06-23T11:00:00-07:00", "Third useful reply")
    _insert(path, 4, "inbound", "2026-06-23T11:30:00-07:00", "Customer reply")

    records, failed = dialpad_adapter.harvest(resolved=_resolved(), trigger="test")

    assert failed is False
    assert len(records) == 1
    record = records[0]
    assert record["kind"] == "activity"
    assert record["provider_id"] == f"sms:{CONTACT}:2026-06-23"
    assert record["title"] == f"SMS thread with {CONTACT} (3 sent)"
    assert RAW_BODY not in json.dumps(record)


def test_long_outbound_thread_is_substantive_by_character_threshold(tmp_path, monkeypatch):
    path = _db(tmp_path)
    monkeypatch.setenv("DIALPAD_SMS_DB", str(path))
    _insert(path, 1, "outbound", "2026-06-23T09:00:00-07:00", "substantive context " * 12)

    records, failed = dialpad_adapter.harvest(resolved=_resolved(), trigger="test")

    assert failed is False
    assert len(records) == 1
    assert "(1 sent)" in records[0]["title"]


def test_auto_reply_and_single_token_thread_is_dropped(tmp_path, monkeypatch):
    path = _db(tmp_path)
    monkeypatch.setenv("DIALPAD_SMS_DB", str(path))
    _insert(path, 1, "outbound", "2026-06-23T09:00:00-07:00", "OK")
    _insert(path, 2, "outbound", "2026-06-23T10:00:00-07:00", "STOP")
    _insert(path, 3, "outbound", "2026-06-23T11:00:00-07:00", "This is an automated reply, do not reply")

    records, failed = dialpad_adapter.harvest(resolved=_resolved(), trigger="test")

    assert failed is False
    assert records == []


def test_provider_state_changes_when_thread_changes(tmp_path, monkeypatch):
    path = _db(tmp_path)
    monkeypatch.setenv("DIALPAD_SMS_DB", str(path))
    _insert(path, 1, "outbound", "2026-06-23T09:00:00-07:00", "substantive context " * 12)

    first, _failed = dialpad_adapter.harvest(resolved=_resolved(), trigger="test")
    _insert(path, 2, "outbound", "2026-06-23T10:00:00-07:00", "Additional substantive context")
    second, _failed = dialpad_adapter.harvest(resolved=_resolved(), trigger="test")

    assert first[0]["provider_state"] != second[0]["provider_state"]


def test_missing_db_degrades_as_failed_source(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_TRACKER_ERROR_LOG", str(tmp_path / "errors.jsonl"))
    monkeypatch.setenv("DIALPAD_SMS_DB", str(tmp_path / "missing.db"))

    records, failed = dialpad_adapter.harvest(resolved=_resolved(), trigger="test")

    assert records == []
    assert failed is True


def test_db_is_opened_read_only(tmp_path, monkeypatch):
    path = _db(tmp_path)
    monkeypatch.setenv("DIALPAD_SMS_DB", str(path))
    _insert(path, 1, "outbound", "2026-06-23T09:00:00-07:00", "substantive context " * 12)
    original = dialpad_adapter.sqlite3.connect
    seen = {}

    def wrapped(database, **kwargs):
        seen["database"] = database
        seen["uri"] = kwargs.get("uri")
        return original(database, **kwargs)

    monkeypatch.setattr(dialpad_adapter.sqlite3, "connect", wrapped)

    records, failed = dialpad_adapter.harvest(resolved=_resolved(), trigger="test")

    assert failed is False
    assert len(records) == 1
    assert seen["uri"] is True
    assert "mode=ro" in seen["database"]


def test_sms_candidate_never_enters_completed_or_plan(tmp_path, monkeypatch):
    path = _db(tmp_path)
    work = tmp_path / "Work Tasks.md"
    state = tmp_path / "state"
    work.write_text("# Work\n\n## \U0001f534 Q1\n- [ ] **Existing task** task_id::tsk_existing\n")
    monkeypatch.setenv("DIALPAD_SMS_DB", str(path))
    monkeypatch.setenv("TASK_MGMT_STATE_DIR", str(state))
    monkeypatch.setenv("TASK_TRACKER_WORK_FILE", str(work))
    monkeypatch.setattr(utils, "OBSIDIAN_WORK", work)
    monkeypatch.setattr(standup_harvest, "SOURCES", ("dialpad_sms",))
    monkeypatch.setattr(standup, "get_calendar_events", lambda trigger="calendar_fetch": {})
    monkeypatch.setattr(standup, "candidate_review_summary", lambda: {})
    monkeypatch.setattr(standup, "task_audit_summary", lambda limit=3: {})
    _insert(path, 1, "outbound", "2026-06-23T09:00:00-07:00", f"First {RAW_BODY}")
    _insert(path, 2, "outbound", "2026-06-23T10:00:00-07:00", "Second useful reply")
    _insert(path, 3, "outbound", "2026-06-23T11:00:00-07:00", "Third useful reply")

    payload = standup.generate_standup(date_str="2026-06-23", json_output=True)

    assert payload["completed"] == []
    assert [item["title"] for item in payload["q1"]] == ["Existing task"]
    assert len(payload["evidence_candidates"]) == 1
    assert payload["evidence_candidates"][0]["source"] == "dialpad_sms"
