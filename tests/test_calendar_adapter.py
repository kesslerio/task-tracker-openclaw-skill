import json
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import cos_health
import harvest_window
import standup_harvest
from adapters import calendar_adapter


def _resolved(day: date = date(2026, 6, 23)):
    return harvest_window.resolve_standup_window(target_date=day)


def _event(
    event_id: str,
    summary: str,
    start: str,
    *,
    status: str = "confirmed",
    response: str = "accepted",
    organizer_self: bool = False,
    all_day: bool = False,
    recurring_id: str | None = None,
    event_type: str | None = None,
    ical_uid: str | None = None,
) -> dict:
    event = {
        "id": event_id,
        "summary": summary,
        "status": status,
        "start": {"date": start[:10]} if all_day else {"dateTime": start},
        "end": {"date": start[:10]} if all_day else {"dateTime": start},
        "attendees": [{"email": "owner@example.test", "self": True, "responseStatus": response}],
        "organizer": {"email": "owner@example.test", "self": organizer_self},
        "htmlLink": f"https://calendar.example.test/{event_id}",
        "updated": "2026-06-23T12:00:00Z",
    }
    if recurring_id:
        event["recurringEventId"] = recurring_id
        event["originalStartTime"] = {"dateTime": start}
    if event_type:
        event["eventType"] = event_type
    if ical_uid:
        event["iCalUID"] = ical_uid
    return event


def _configure(monkeypatch, events, *, returncode=0):
    monkeypatch.setenv(
        "STANDUP_CALENDARS",
        json.dumps(
            {
                "work": {
                    "cmd": "gog",
                    "calendar_id": "cal_fixture",
                    "account": "owner@example.test",
                }
            }
        ),
    )
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True
        if returncode:
            return subprocess.CompletedProcess(cmd, returncode, stdout="", stderr="fixture failure")
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"events": events}), stderr="")

    monkeypatch.setattr(calendar_adapter.subprocess, "run", fake_run)
    monkeypatch.setattr(
        calendar_adapter.cos_config,
        "local_now",
        lambda: datetime.fromisoformat("2026-06-23T12:00:00-07:00"),
    )
    return calls


def test_calendar_queries_u1_window_without_today(monkeypatch):
    calls = _configure(monkeypatch, [])

    records, failed = calendar_adapter.harvest(resolved=_resolved(), trigger="test")

    assert records == []
    assert failed is False
    cmd = calls[0]
    assert "--today" not in cmd
    assert "--from" in cmd
    assert "--to" in cmd
    assert "2026-06-23T00:00:00-07:00" in cmd
    assert "2026-06-24T00:00:00-07:00" in cmd


def test_past_accepted_event_is_activity(monkeypatch):
    _configure(monkeypatch, [_event("evt_1", "Planning review", "2026-06-23T09:00:00-07:00")])

    records, failed = calendar_adapter.harvest(resolved=_resolved(), trigger="test")

    assert failed is False
    assert records[0]["kind"] == "activity"
    assert records[0]["provider_id"] == "evt_1"
    assert "response=accepted" in records[0]["provider_state"]


def test_past_organized_event_is_activity(monkeypatch):
    event = _event(
        "evt_organized",
        "Roadmap sync",
        "2026-06-23T10:00:00-07:00",
        response="needsAction",
        organizer_self=True,
    )
    event["attendees"] = []
    _configure(monkeypatch, [event])

    records, _failed = calendar_adapter.harvest(resolved=_resolved(), trigger="test")

    assert len(records) == 1
    assert records[0]["kind"] == "activity"
    assert "response=organized" in records[0]["provider_state"]


def test_declined_cancelled_and_all_day_events_are_excluded(monkeypatch):
    _configure(
        monkeypatch,
        [
            _event("evt_declined", "Declined", "2026-06-23T09:00:00-07:00", response="declined"),
            _event("evt_cancelled", "Cancelled", "2026-06-23T10:00:00-07:00", status="cancelled"),
            _event("evt_all_day", "All day", "2026-06-23T00:00:00-07:00", all_day=True),
        ],
    )

    records, failed = calendar_adapter.harvest(resolved=_resolved(), trigger="test")

    assert failed is False
    assert records == []


def test_tentative_needs_action_event_is_excluded(monkeypatch):
    event = _event("evt_tentative", "Maybe sync", "2026-06-23T09:00:00-07:00", response="needsAction")
    event["organizer"] = {"email": "other@example.test"}
    _configure(
        monkeypatch,
        [event],
    )

    records, failed = calendar_adapter.harvest(resolved=_resolved(), trigger="test")

    assert failed is False
    assert records == []


def test_focus_time_event_is_excluded(monkeypatch):
    _configure(
        monkeypatch,
        [
            _event(
                "evt_focus",
                "Focus block",
                "2026-06-23T09:00:00-07:00",
                organizer_self=True,
                event_type="focusTime",
            )
        ],
    )

    records, failed = calendar_adapter.harvest(resolved=_resolved(), trigger="test")

    assert failed is False
    assert records == []


def test_upcoming_accepted_event_is_commitment(monkeypatch):
    _configure(monkeypatch, [_event("evt_future", "Customer call", "2026-06-23T15:00:00-07:00")])

    records, _failed = calendar_adapter.harvest(resolved=_resolved(), trigger="test")

    assert records[0]["kind"] == "commitment"
    assert records[0]["match_title"] == "Customer call"


def test_recurring_occurrences_keep_distinct_provider_ids(monkeypatch):
    _configure(
        monkeypatch,
        [
            _event("series_abc_20260623", "Daily check", "2026-06-23T09:00:00-07:00", recurring_id="series_abc"),
            _event("series_abc_20260624", "Daily check", "2026-06-23T10:00:00-07:00", recurring_id="series_abc"),
        ],
    )

    records, _failed = calendar_adapter.harvest(resolved=_resolved(), trigger="test")

    assert {record["provider_id"] for record in records} == {"series_abc_20260623", "series_abc_20260624"}


def test_recurring_occurrences_without_ids_use_original_start_provider_ids(monkeypatch):
    _configure(
        monkeypatch,
        [
            _event("", "Daily check", "2026-06-23T09:00:00-07:00", recurring_id="series_abc"),
            _event("", "Daily check", "2026-06-23T10:00:00-07:00", recurring_id="series_abc"),
        ],
    )

    records, _failed = calendar_adapter.harvest(resolved=_resolved(), trigger="test")

    assert {record["provider_id"] for record in records} == {
        "series_abc:2026-06-23T09:00:00-07:00",
        "series_abc:2026-06-23T10:00:00-07:00",
    }


def test_ical_uid_only_occurrences_include_start_in_provider_id(monkeypatch):
    _configure(
        monkeypatch,
        [
            _event("", "Daily check", "2026-06-23T09:00:00-07:00", ical_uid="uid_abc"),
            _event("", "Daily check", "2026-06-23T10:00:00-07:00", ical_uid="uid_abc"),
        ],
    )

    records, _failed = calendar_adapter.harvest(resolved=_resolved(), trigger="test")

    assert {record["provider_id"] for record in records} == {
        "uid_abc:2026-06-23T09:00:00-07:00",
        "uid_abc:2026-06-23T10:00:00-07:00",
    }


def test_dst_overnight_event_lands_on_start_day(monkeypatch):
    day = date(2026, 3, 8)
    _configure(monkeypatch, [_event("evt_dst", "DST overnight", "2026-03-08T23:30:00-07:00")])
    monkeypatch.setattr(
        calendar_adapter.cos_config,
        "local_now",
        lambda: datetime.fromisoformat("2026-03-09T08:00:00-07:00"),
    )

    records, _failed = calendar_adapter.harvest(resolved=_resolved(day), trigger="test")

    assert records[0]["occurred_at"].startswith("2026-03-08T23:30:00-07:00")


def test_gog_non_zero_records_failed_source_health_without_crashing(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_MGMT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("TASK_TRACKER_ERROR_LOG", str(tmp_path / "errors.jsonl"))
    _configure(monkeypatch, [], returncode=1)
    monkeypatch.setattr(standup_harvest, "SOURCES", ("calendar",))

    result = standup_harvest.harvest(target_date=date(2026, 6, 23), trigger="test")

    assert result["evidence_candidates"] == []
    assert result["health"]["calendar"]["status"] == "failed"
    receipt = cos_health.read_health()["standup"]["sources"]["calendar"]
    assert receipt["status"] == "failed"


def test_gog_timeout_error_log_redacts_access_token(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_MGMT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("TASK_TRACKER_ERROR_LOG", str(tmp_path / "errors.jsonl"))
    monkeypatch.setenv("GOG_ACCESS_TOKEN", "SENTINELTOKEN123")
    monkeypatch.setenv(
        "STANDUP_CALENDARS",
        json.dumps(
            {
                "work": {
                    "cmd": "gog",
                    "calendar_id": "cal_fixture",
                    "account": "owner@example.test",
                    "access_token_env": "GOG_ACCESS_TOKEN",
                }
            }
        ),
    )

    def fake_run(cmd, **_kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=10)

    monkeypatch.setattr(calendar_adapter.subprocess, "run", fake_run)

    records, failed = calendar_adapter.harvest(resolved=_resolved(), trigger="test")

    assert records == []
    assert failed is True
    raw_log = (tmp_path / "errors.jsonl").read_text()
    assert "SENTINELTOKEN123" not in raw_log
    assert "<redacted>" in raw_log
