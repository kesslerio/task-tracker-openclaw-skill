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

    def _set(events, *, returncode=0, exc=None, raw=None):
        def fake_run(cmd, **kwargs):
            if exc is not None:
                raise exc
            if returncode:
                return subprocess.CompletedProcess(cmd, returncode, stdout="", stderr="fail")
            stdout = raw if raw is not None else json.dumps({"events": events})
            return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")
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


def test_focus_time_block_is_busy(configured):
    # A focusTime block is EXACTLY 'do not interrupt' -> must suppress (the harvest
    # denylists it as non-evidence; availability must NOT treat it as free).
    configured([_containing(event_type="focusTime")])
    assert availability.not_known_busy(NOW) is False


def test_out_of_office_is_busy(configured):
    configured([_containing(event_type="outOfOffice")])
    assert availability.not_known_busy(NOW) is False


def test_birthday_is_not_busy(configured):
    configured([_containing(event_type="birthday")])
    assert availability.not_known_busy(NOW) is True


def test_working_location_is_not_busy(configured):
    configured([_containing(event_type="workingLocation")])
    assert availability.not_known_busy(NOW) is True


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


@pytest.mark.parametrize("config", [
    {"cmd": "gog", "account": "owner@example.test"},        # no calendar_id
    {"cmd": "gog", "calendar_id": "cal_fixture"},            # no account
])
def test_incomplete_calendar_config_fails_closed(tmp_path, monkeypatch, config):
    # A structurally-valid config missing calendar_id/account is unqueryable ->
    # uncertainty -> suppress, never silently read as 'no meetings' (free).
    monkeypatch.setenv("TASK_MGMT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("STANDUP_CALENDARS", json.dumps({"work": config}))
    monkeypatch.setattr(availability.error_envelope, "breaker_open", lambda component: False)
    # subprocess must never be reached; if it is, this would raise loudly.
    monkeypatch.setattr(availability.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("gog called")))
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


def test_reversed_interval_fails_closed_busy(configured):
    # end < start (malformed) -> cannot reliably time -> fail closed (busy).
    configured([_event(start=_iso(NOW + timedelta(minutes=30)), end=_iso(NOW - timedelta(minutes=30)))])
    assert availability.not_known_busy(NOW) is False


def test_non_list_events_payload_fails_closed(configured):
    # gog exits 0 but the events value is not a list -> unparseable -> suppress, NOT
    # silently read as "no meetings" (the fail-OPEN the lenient harvest parser allowed).
    configured([], raw=json.dumps({"events": "rate-limited but exit 0"}))
    assert availability.not_known_busy(NOW) is False


def test_payload_without_events_key_fails_closed(configured):
    configured([], raw=json.dumps({"ok": True, "warning": "no calendar matched"}))
    assert availability.not_known_busy(NOW) is False


def test_scalar_payload_fails_closed(configured):
    configured([], raw="null")
    assert availability.not_known_busy(NOW) is False


def test_bare_list_payload_is_parsed(configured):
    # A top-level list (no wrapper) is a legitimate gog shape -> containment still applies.
    configured([], raw=json.dumps([_containing()]))
    assert availability.not_known_busy(NOW) is False


def test_unexpected_classification_error_fails_closed(configured, monkeypatch):
    configured([_containing()])

    def boom(*a, **k):
        raise RuntimeError("unexpected event shape")

    monkeypatch.setattr(availability, "_is_busy_at", boom)
    assert availability.not_known_busy(NOW) is False


def test_access_token_is_redacted_in_the_degraded_log(tmp_path, monkeypatch):
    # A gog timeout carries the full argv (incl. --access-token <secret>) on the
    # exception; the degraded-log redaction must scrub it -- no token on disk.
    monkeypatch.setenv("TASK_MGMT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("GOG_TOKEN", "SUPER_SECRET_TOKEN_XYZ")
    monkeypatch.setenv("STANDUP_CALENDARS", json.dumps({"work": {
        "cmd": "gog", "calendar_id": "c", "account": "owner@example.test",
        "access_token_env": "GOG_TOKEN"}}))
    monkeypatch.setattr(availability.error_envelope, "breaker_open", lambda component: False)

    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=10)

    monkeypatch.setattr(availability.subprocess, "run", fake_run)
    assert availability.not_known_busy(NOW) is False
    log = availability.error_envelope.error_log_path()
    contents = log.read_text() if log.exists() else ""
    assert "SUPER_SECRET_TOKEN_XYZ" not in contents


# --- naive now -------------------------------------------------------------

def test_naive_now_is_assumed_local(configured):
    configured([_containing()])
    naive = NOW.replace(tzinfo=None)
    assert availability.not_known_busy(naive) is False
