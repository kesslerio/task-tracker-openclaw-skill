"""v0.4-C point-in-time calendar availability: not_known_busy(now), fail CLOSED.

True  = fresh successful read, no accepted event contains now -> safe to nudge.
False = an accepted event contains now (busy) OR any uncertainty (no config, gog
        error/timeout, malformed config, breaker open, untimeable accepted event).
"""

import json
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import availability  # noqa: E402
import cos_config  # noqa: E402

NOW = cos_config.local_now().replace(microsecond=0)


def _event(*, start, end, response="accepted", status="confirmed", all_day=False,
           organizer_self=False, event_type=None, recurring=False):
    ev = {
        "id": "ev_fixture",
        "summary": "Fixture meeting",
        "status": status,
        "attendees": [{"email": "owner@example.test", "self": True, "responseStatus": response}],
        # Organizer is someone ELSE by default (a different email + self=False), so an
        # event is "organized by me" only when organizer_self=True -- otherwise busy-ness
        # comes solely from the self attendee's responseStatus.
        "organizer": {"email": "organizer@example.test", "self": organizer_self},
        "updated": "2026-06-24T00:00:00Z",
    }
    if all_day:
        ev["start"] = {"date": "2026-06-24"}
        ev["end"] = {"date": "2026-06-25"}
    else:
        ev["start"] = {"dateTime": start} if start is not None else {}
        ev["end"] = {"dateTime": end} if end is not None else {}
    if event_type:
        ev["eventType"] = event_type
    if recurring:
        ev["recurringEventId"] = "rec_1"
        ev["originalStartTime"] = {"dateTime": start}
    return ev


def _iso(dt):
    return dt.isoformat()


def _containing(**over):
    # An event whose [start, end) brackets NOW.
    return _event(start=_iso(NOW - timedelta(minutes=30)),
                  end=_iso(NOW + timedelta(minutes=30)), **over)


@pytest.fixture
def configured(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_MGMT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("STANDUP_CALENDARS", json.dumps(
        {"work": {"cmd": "gog", "calendar_id": "cal_fixture", "account": "owner@example.test"}}))
    monkeypatch.setattr(availability.error_envelope, "breaker_open", lambda component: False)

    def _set(events, *, returncode=0, exc=None):
        def fake_run(cmd, **kwargs):
            if exc is not None:
                raise exc
            if returncode:
                return subprocess.CompletedProcess(cmd, returncode, stdout="", stderr="fail")
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"events": events}), stderr="")
        monkeypatch.setattr(availability.subprocess, "run", fake_run)
    return _set


# --- busy / free containment -----------------------------------------------

def test_accepted_event_containing_now_is_busy(configured):
    configured([_containing()])
    assert availability.not_known_busy(NOW) is False


def test_past_event_leaves_user_free(configured):
    configured([_event(start=_iso(NOW - timedelta(hours=2)), end=_iso(NOW - timedelta(hours=1)))])
    assert availability.not_known_busy(NOW) is True


def test_future_event_leaves_user_free(configured):
    configured([_event(start=_iso(NOW + timedelta(hours=1)), end=_iso(NOW + timedelta(hours=2)))])
    assert availability.not_known_busy(NOW) is True


def test_no_events_is_free(configured):
    configured([])
    assert availability.not_known_busy(NOW) is True


# --- classification: these do NOT make the user busy -----------------------

def test_all_day_event_is_not_busy(configured):
    configured([_containing(all_day=True)])
    assert availability.not_known_busy(NOW) is True


def test_declined_event_is_not_busy(configured):
    configured([_containing(response="declined")])
    assert availability.not_known_busy(NOW) is True


def test_cancelled_event_is_not_busy(configured):
    configured([_containing(status="cancelled")])
    assert availability.not_known_busy(NOW) is True


def test_no_response_event_is_not_busy(configured):
    # Not accepted, not organized -> not a busy commitment.
    configured([_containing(response="needsAction")])
    assert availability.not_known_busy(NOW) is True


def test_organized_event_is_busy_even_without_accept(configured):
    configured([_containing(response="", organizer_self=True)])
    assert availability.not_known_busy(NOW) is False


def test_recurring_instance_containing_now_is_busy(configured):
    configured([_containing(recurring=True)])
    assert availability.not_known_busy(NOW) is False


# --- fail CLOSED -----------------------------------------------------------

def test_no_calendar_configured_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_MGMT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("STANDUP_CALENDARS", raising=False)
    assert availability.not_known_busy(NOW) is False


def test_malformed_calendar_config_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_MGMT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("STANDUP_CALENDARS", "{ not json")
    assert availability.not_known_busy(NOW) is False


def test_gog_nonzero_exit_fails_closed(configured):
    configured([], returncode=1)
    assert availability.not_known_busy(NOW) is False


def test_gog_timeout_fails_closed(configured):
    configured([], exc=subprocess.TimeoutExpired(cmd="gog", timeout=10))
    assert availability.not_known_busy(NOW) is False


def test_breaker_open_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_MGMT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("STANDUP_CALENDARS", json.dumps(
        {"work": {"cmd": "gog", "calendar_id": "c", "account": "owner@example.test"}}))
    monkeypatch.setattr(availability.error_envelope, "breaker_open", lambda component: True)
    assert availability.not_known_busy(NOW) is False


def test_accepted_event_with_missing_end_fails_closed_busy(configured):
    # An accepted event we cannot time cannot be ruled out -> busy (suppress).
    configured([_event(start=_iso(NOW - timedelta(minutes=10)), end=None)])
    assert availability.not_known_busy(NOW) is False


# --- naive now -------------------------------------------------------------

def test_naive_now_is_assumed_local(configured):
    configured([_containing()])
    naive = NOW.replace(tzinfo=None)
    assert availability.not_known_busy(naive) is False
