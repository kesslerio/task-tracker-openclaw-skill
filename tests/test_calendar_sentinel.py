"""U1 calendar-fetch sentinel tests (fixes silent-failure G9).

get_calendar_events() must:
- return {} when not configured (renders "not configured", not an error);
- return a {"_error": ...} sentinel on a real fetch failure, logged, never a
  bare pass/swallow;
- both standup callers guard the sentinel via calendar_error() and render a
  degraded notice instead of letting the section silently vanish, with NO raw
  exception name in the output.
"""

import json
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))

import standup_common  # noqa: E402
import error_envelope  # noqa: E402


@pytest.fixture()
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_MGMT_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("TASK_TRACKER_ERROR_LOG", str(tmp_path / "errors.jsonl"))
    monkeypatch.setenv("TASK_TRACKER_LEDGER_FILE", str(tmp_path / "events.jsonl"))
    return tmp_path


def test_not_configured_returns_empty_not_error(state, monkeypatch):
    monkeypatch.delenv("STANDUP_CALENDARS", raising=False)
    result = standup_common.get_calendar_events()
    assert result == {}
    assert standup_common.calendar_error(result) is None


def test_invalid_json_config_returns_empty(state, monkeypatch):
    monkeypatch.setenv("STANDUP_CALENDARS", "{not json")
    assert standup_common.get_calendar_events() == {}


def test_fetch_failure_returns_sentinel_and_logs(state, monkeypatch):
    # A configured calendar whose binary does not exist -> FileNotFoundError ->
    # sentinel, NOT a swallow, and logged.
    config = {
        "work": {
            "cmd": "/nonexistent/gog-binary-xyz",
            "calendar_id": "cal@example.com",
            "account": "acct",
        }
    }
    monkeypatch.setenv("STANDUP_CALENDARS", json.dumps(config))
    result = standup_common.get_calendar_events()
    assert standup_common.calendar_error(result) == "calendar_unavailable"

    entries = [
        json.loads(ln)
        for ln in (state / "errors.jsonl").read_text().splitlines()
        if ln.strip()
    ]
    assert entries, "calendar fetch failure not logged"
    assert entries[-1]["component"] == "calendar_fetch"
    assert entries[-1]["action_taken"] == "degraded+logged"


def test_partial_failure_keeps_succeeding_calendars(state, monkeypatch):
    # Two configured calendars: one succeeds, one raises. The succeeding
    # calendar's events must survive (no sentinel), and the failure is logged.
    import json as _json
    import subprocess as _subprocess
    import types

    config = {
        "work": {"cmd": "gog", "calendar_id": "work@example.com", "account": "w"},
        "home": {"cmd": "gog", "calendar_id": "home@example.com", "account": "h"},
    }
    monkeypatch.setenv("STANDUP_CALENDARS", _json.dumps(config))

    ok_payload = _json.dumps(
        {"events": [{"summary": "Standup", "start": {"dateTime": "2026-06-21T09:00:00Z"}, "end": {"dateTime": "2026-06-21T09:15:00Z"}}]}
    )

    def fake_run(cmd, **kwargs):
        # cmd = [bin, "calendar", "list", <calendar_id>, ...]
        calendar_id = cmd[3]
        if calendar_id == "home@example.com":
            raise _subprocess.TimeoutExpired(cmd, 10)
        return types.SimpleNamespace(returncode=0, stdout=ok_payload, stderr="")

    monkeypatch.setattr(standup_common.subprocess, "run", fake_run)
    result = standup_common.get_calendar_events()

    # Not a sentinel: at least one calendar succeeded.
    assert standup_common.calendar_error(result) is None
    assert any(ev["summary"] == "Standup" for ev in result.get("work", []))
    # The failure was still logged.
    entries = [
        json.loads(ln)
        for ln in (state / "errors.jsonl").read_text().splitlines()
        if ln.strip()
    ]
    assert any(e["component"] == "calendar_fetch" for e in entries)


def test_all_calendars_failing_returns_sentinel(state, monkeypatch):
    import json as _json
    import subprocess as _subprocess

    config = {
        "work": {"cmd": "gog", "calendar_id": "work@example.com", "account": "w"},
        "home": {"cmd": "gog", "calendar_id": "home@example.com", "account": "h"},
    }
    monkeypatch.setenv("STANDUP_CALENDARS", _json.dumps(config))

    def fake_run(cmd, **kwargs):
        raise _subprocess.TimeoutExpired(cmd, 10)

    monkeypatch.setattr(standup_common.subprocess, "run", fake_run)
    result = standup_common.get_calendar_events()
    assert standup_common.calendar_error(result) == "calendar_unavailable"


def test_nonzero_exit_is_logged_not_silently_empty(state, monkeypatch):
    # A gog non-zero exit (auth expired) must be counted + logged + surfaced as
    # the sentinel, NOT rendered as an empty "no events" section (silent G9).
    import types

    config = {"work": {"cmd": "gog", "calendar_id": "work@example.com", "account": "w"}}
    monkeypatch.setenv("STANDUP_CALENDARS", json.dumps(config))

    def fake_run(cmd, **kwargs):
        return types.SimpleNamespace(
            returncode=1, stdout="", stderr="ERROR: 401 unauthorized: token expired"
        )

    monkeypatch.setattr(standup_common.subprocess, "run", fake_run)
    result = standup_common.get_calendar_events()
    assert standup_common.calendar_error(result) == "calendar_unavailable"

    entries = [
        json.loads(ln)
        for ln in (state / "errors.jsonl").read_text().splitlines()
        if ln.strip()
    ]
    cal = [e for e in entries if e["component"] == "calendar_fetch"]
    assert cal, "non-zero calendar exit was silently swallowed"
    # The captured stderr drives accurate classification (auth, not a bare fail).
    assert cal[-1]["error_class"] == error_envelope.AUTH


def test_open_breaker_skips_subprocess_and_degrades(state, monkeypatch):
    # Once the missing-tool breaker for calendar_fetch is open (a repeatedly
    # missing gog), get_calendar_events must NOT invoke the subprocess again --
    # it short-circuits to the degraded sentinel so the cron stops looping.
    config = {"work": {"cmd": "gog", "calendar_id": "c@example.com", "account": "a"}}
    monkeypatch.setenv("STANDUP_CALENDARS", json.dumps(config))

    # Seed the breaker open: _BREAKER_THRESHOLD missing-tool entries for the
    # calendar_fetch component.
    for _ in range(error_envelope._BREAKER_THRESHOLD):
        error_envelope.log_error(
            "calendar_fetch",
            error_class=error_envelope.MISSING_TOOL,
            message="calendar_fetch failed (missing-tool)",
            raw="command not found",
            trigger="user_command:/standup",
            to_ledger=False,
        )
    assert error_envelope.breaker_open("calendar_fetch") is True

    called = {"n": 0}

    def fake_run(cmd, **kwargs):
        called["n"] += 1
        raise AssertionError("subprocess must not run when breaker is open")

    monkeypatch.setattr(standup_common.subprocess, "run", fake_run)
    result = standup_common.get_calendar_events()
    assert called["n"] == 0
    assert standup_common.calendar_error(result) == "calendar_unavailable"


def test_calendar_error_detects_sentinel():
    assert standup_common.calendar_error({"_error": "calendar_unavailable"}) == "calendar_unavailable"
    assert standup_common.calendar_error({"work": []}) is None
    assert standup_common.calendar_error({}) is None
    assert standup_common.calendar_error(None) is None


def test_flatten_handles_sentinel_without_crashing():
    # The sentinel must not be iterated as event data.
    assert standup_common.flatten_calendar_events({"_error": "calendar_unavailable"}) == []


def test_degraded_notice_has_no_raw_exception():
    notice = error_envelope.degraded_notice("Calendar")
    assert "FileNotFoundError" not in notice
    assert "CalledProcessError" not in notice
    assert "Traceback" not in notice
    assert "unavailable" in notice
