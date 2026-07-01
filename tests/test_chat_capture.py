import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import capture_envelope
import chat_capture
import utils
from evidence_matching import build_task_catalog, match_evidence_all, resolve_for_auto
from task_records import task_records


SECRET = "test-capture-envelope-secret"
NOW = "2026-06-30T12:00:00+00:00"


def _write_work_file(tmp_path, content=None):
    work = tmp_path / "Work Tasks.md"
    work.write_text(
        content
        or """# Work

## 🔴 Q1
- [ ] **Ship alpha milestone** task_id::tsk_ship area:: Delivery
- [ ] **Fix login timeout** task_id::tsk_login area:: Platform
- [ ] **Write onboarding docs** task_id::tsk_docs area:: Docs
"""
    )
    return work


def _apply_env(monkeypatch, tmp_path, work, *, autowrite="false", secret=SECRET, now=NOW):
    monkeypatch.setenv("TASK_TRACKER_WORK_FILE", str(work))
    monkeypatch.setenv("TASK_TRACKER_DAILY_NOTES_DIR", str(tmp_path / "daily"))
    monkeypatch.setenv("TASK_TRACKER_DONE_LOG_DIR", str(tmp_path / "daily"))
    monkeypatch.setenv("TASK_TRACKER_LEDGER_FILE", str(tmp_path / "events.jsonl"))
    monkeypatch.setenv("STANDUP_CALENDARS", "{}")
    monkeypatch.setenv("TASK_TRACKER_CAPTURE_NOW", now)
    if autowrite is None:
        monkeypatch.delenv("TASK_TRACKER_CAPTURE_AUTOWRITE_ENABLED", raising=False)
    else:
        monkeypatch.setenv("TASK_TRACKER_CAPTURE_AUTOWRITE_ENABLED", autowrite)
    if secret is None:
        monkeypatch.delenv("TASK_TRACKER_CAPTURE_ENVELOPE_SECRET", raising=False)
    else:
        monkeypatch.setenv("TASK_TRACKER_CAPTURE_ENVELOPE_SECRET", secret)
    monkeypatch.delenv("TASK_TRACKER_CAPTURE_OWNER_ID", raising=False)
    monkeypatch.delenv("TASK_TRACKER_CAPTURE_GATEWAY_TOKEN", raising=False)
    monkeypatch.setattr(utils, "OBSIDIAN_WORK", work)
    monkeypatch.setattr(utils, "LEGACY_WORK", tmp_path / "missing-legacy.md")


def _events(tmp_path):
    ledger = tmp_path / "events.jsonl"
    if not ledger.exists():
        return []
    return [json.loads(line) for line in ledger.read_text().splitlines() if line.strip()]


def _event_types(tmp_path):
    return [event["event_type"] for event in _events(tmp_path)]


def _seen_events(tmp_path):
    return [event for event in _events(tmp_path) if event["event_type"] == capture_envelope.SEEN_EVENT_TYPE]


def _envelope_json(
    *,
    task_id="tsk_ship",
    channel="telegram",
    message_id="msg-1",
    timestamp=NOW,
    intent="complete",
    secret=SECRET,
):
    envelope = {
        "v": 1,
        "sender": "sender-123",
        "channel": channel,
        "message_id": message_id,
        "timestamp": timestamp,
        "task_id": task_id,
        "intent": intent,
    }
    return json.dumps(capture_envelope.sign_envelope(envelope, secret))


def _candidate_actions(payload):
    return [action for action in payload["actions"] if action["action"] == "candidate"]


def test_match_evidence_all_exposes_identical_title_runner_up(tmp_path):
    work = _write_work_file(
        tmp_path,
        """# Work

## 🔴 Q1
- [ ] **Fix login timeout** task_id::tsk_a area:: Platform
- [ ] **Fix login timeout** task_id::tsk_b area:: Platform
""",
    )
    records = task_records(work.read_text(), fmt="obsidian")
    catalog = build_task_catalog(records)
    line = {
        "raw_line": "finished Fix login timeout",
        "title": "Fix login timeout",
        "normalized_title": "fix login timeout",
        "exact_identifiers": set(),
        "fallback_identifiers": set(),
    }

    payload = match_evidence_all(line, catalog)

    assert [match["matched_task_id"] for match in payload["matches"][:2]] == ["tsk_a", "tsk_b"]
    assert [match["score"] for match in payload["matches"][:2]] == [1.0, 1.0]
    assert all("normalized-title" in match["match_types"] for match in payload["matches"][:2])


def test_resolve_for_auto_is_exact_active_task_id_only(tmp_path):
    work = _write_work_file(tmp_path)
    catalog = build_task_catalog(task_records(work.read_text(), fmt="obsidian"))

    assert resolve_for_auto("tsk_ship", catalog).canonical_id == "tsk_ship"
    assert resolve_for_auto("  tsk_ship  ", catalog).canonical_id == "tsk_ship"
    assert resolve_for_auto("Ship alpha milestone", catalog) is None
    assert resolve_for_auto("ship alpha milestone", catalog) is None
    assert resolve_for_auto("tsk_missing", catalog) is None

    # A legacy id:: is NOT a valid auto-complete target: canonical_id falls back
    # to it, but resolve_for_auto matches the record's real task_id:: only.
    legacy_catalog = build_task_catalog(
        task_records(
            "# Work\n\n## \U0001F534 Q1\n- [ ] **Legacy only** id::legacy_x\n",
            fmt="obsidian",
        )
    )
    assert resolve_for_auto("legacy_x", legacy_catalog) is None


def test_valid_signed_envelope_autowrites_exact_non_recurring_task(tmp_path, monkeypatch):
    work = _write_work_file(tmp_path)
    _apply_env(monkeypatch, tmp_path, work, autowrite="true")

    payload = chat_capture.capture_text(envelope=_envelope_json())

    assert payload["action"] == "auto"
    assert payload["envelope_verified"] is True
    assert payload["task_id"] == "tsk_ship"
    assert payload["completion_id"].startswith("evt_")
    assert "Ship alpha milestone" not in work.read_text()
    events = _events(tmp_path)
    assert [event["event_type"] for event in events] == ["state_transition", "capture_envelope_seen"]
    assert events[0]["source"] == "chat_capture"
    assert events[0]["metadata"]["completion_id"] == payload["completion_id"]
    assert events[1]["metadata"]["message_id"] == "msg-1"
    assert events[1]["metadata"]["channel"] == "telegram"
    assert events[1]["metadata"]["completion_event_id"] == payload["completion_id"]


def test_same_message_id_on_different_channels_both_auto_complete(tmp_path, monkeypatch):
    work = _write_work_file(tmp_path)
    _apply_env(monkeypatch, tmp_path, work, autowrite="true")

    first = chat_capture.capture_text(
        envelope=_envelope_json(task_id="tsk_ship", channel="telegram", message_id="shared-gateway-id"),
    )
    second = chat_capture.capture_text(
        envelope=_envelope_json(task_id="tsk_login", channel="slack", message_id="shared-gateway-id"),
    )

    assert first["action"] == "auto"
    assert first["envelope_verified"] is True
    assert first["task_id"] == "tsk_ship"
    assert second["action"] == "auto"
    assert second["envelope_verified"] is True
    assert second["task_id"] == "tsk_login"
    assert "Ship alpha milestone" not in work.read_text()
    assert "Fix login timeout" not in work.read_text()
    assert _event_types(tmp_path) == [
        "state_transition",
        capture_envelope.SEEN_EVENT_TYPE,
        "state_transition",
        capture_envelope.SEEN_EVENT_TYPE,
    ]
    seen_events = _seen_events(tmp_path)
    assert [event["metadata"]["message_id"] for event in seen_events] == [
        "shared-gateway-id",
        "shared-gateway-id",
    ]
    assert [event["metadata"]["channel"] for event in seen_events] == ["telegram", "slack"]


def test_valid_signed_envelope_strips_task_id_before_auto_complete(tmp_path, monkeypatch):
    work = _write_work_file(tmp_path)
    _apply_env(monkeypatch, tmp_path, work, autowrite="true")

    payload = chat_capture.capture_text(
        envelope=_envelope_json(task_id="  tsk_ship  ", message_id="task-ws"),
    )

    assert payload["action"] == "auto"
    assert payload["envelope_verified"] is True
    assert payload["task_id"] == "tsk_ship"
    assert "Ship alpha milestone" not in work.read_text()
    events = _events(tmp_path)
    assert events[1]["task_id"] == "tsk_ship"


@pytest.mark.parametrize(
    ("case", "expected_reason"),
    [
        ("bad-signature", "invalid-signature"),
        ("secret-unset", "secret-unset"),
        ("invalid-intent", "invalid-intent"),
        ("stale-timestamp", "stale-timestamp"),
        ("flag-off", None),
    ],
)
def test_rejected_or_disabled_envelope_routes_to_candidate_without_auto(
    tmp_path,
    monkeypatch,
    case,
    expected_reason,
):
    work = _write_work_file(tmp_path)
    original = work.read_text()
    autowrite = "false" if case == "flag-off" else "true"
    secret = None if case == "secret-unset" else SECRET
    _apply_env(monkeypatch, tmp_path, work, autowrite=autowrite, secret=secret)

    if case == "bad-signature":
        envelope = json.loads(_envelope_json())
        envelope["sig"] = "0" * 64
        raw = json.dumps(envelope)
    elif case == "invalid-intent":
        raw = _envelope_json(intent="cancel")
    elif case == "stale-timestamp":
        raw = _envelope_json(timestamp="2026-06-30T11:00:00+00:00")
    else:
        raw = _envelope_json()

    payload = chat_capture.capture_text(envelope=raw)

    assert payload["action"] == "candidate"
    assert payload["task_id"] == "tsk_ship"
    assert payload["envelope_verified"] is (case == "flag-off")
    assert payload["envelope_reason"] == expected_reason
    assert payload["decision_reason"] == (expected_reason or "autowrite-disabled")
    assert work.read_text() == original
    expected_events = ["candidate_seen", capture_envelope.SEEN_EVENT_TYPE] if case == "flag-off" else ["candidate_seen"]
    assert _event_types(tmp_path) == expected_events
    if case == "flag-off":
        [seen] = _seen_events(tmp_path)
        assert seen["metadata"]["message_id"] == "msg-1"
        assert seen["metadata"]["outcome"] == "autowrite-disabled"
    else:
        assert _seen_events(tmp_path) == []


def test_verified_disabled_envelope_consumes_message_id_before_replay(tmp_path, monkeypatch):
    work = _write_work_file(tmp_path)
    original = work.read_text()
    _apply_env(monkeypatch, tmp_path, work, autowrite="false")
    raw = _envelope_json(message_id="disabled-replay")

    first = chat_capture.capture_text(envelope=raw)
    monkeypatch.setenv("TASK_TRACKER_CAPTURE_AUTOWRITE_ENABLED", "true")
    second = chat_capture.capture_text(envelope=raw)

    assert first["action"] == "candidate"
    assert first["envelope_verified"] is True
    assert first["decision_reason"] == "autowrite-disabled"
    assert second["action"] == "candidate"
    assert second["envelope_verified"] is False
    assert second["envelope_reason"] == "replayed-message-id"
    assert second["decision_reason"] == "replayed-message-id"
    assert work.read_text() == original
    assert _event_types(tmp_path) == ["candidate_seen", capture_envelope.SEEN_EVENT_TYPE]
    [seen] = _seen_events(tmp_path)
    assert seen["metadata"]["message_id"] == "disabled-replay"
    assert seen["metadata"]["outcome"] == "autowrite-disabled"


def test_verified_task_not_found_envelope_consumes_message_id_before_replay(tmp_path, monkeypatch):
    work = _write_work_file(tmp_path)
    original = work.read_text()
    _apply_env(monkeypatch, tmp_path, work, autowrite="true")
    raw = _envelope_json(task_id="Ship alpha milestone", message_id="missing-replay")

    first = chat_capture.capture_text(envelope=raw)
    second = chat_capture.capture_text(envelope=raw)

    assert first["action"] == "candidate"
    assert first["envelope_verified"] is True
    assert first["decision_reason"] == "auto-task-not-found"
    assert second["action"] == "candidate"
    assert second["envelope_verified"] is False
    assert second["envelope_reason"] == "replayed-message-id"
    assert second["decision_reason"] == "replayed-message-id"
    assert work.read_text() == original
    assert _event_types(tmp_path) == ["candidate_seen", capture_envelope.SEEN_EVENT_TYPE]
    [seen] = _seen_events(tmp_path)
    assert seen["metadata"]["message_id"] == "missing-replay"
    assert seen["metadata"]["outcome"] == "auto-task-not-found"


def test_invalid_signature_envelope_does_not_consume_message_id(tmp_path, monkeypatch):
    work = _write_work_file(tmp_path)
    _apply_env(monkeypatch, tmp_path, work, autowrite="true")
    envelope = json.loads(_envelope_json(message_id="bad-then-valid"))
    envelope["sig"] = "0" * 64

    first = chat_capture.capture_text(envelope=json.dumps(envelope))

    assert first["action"] == "candidate"
    assert first["envelope_verified"] is False
    assert first["envelope_reason"] == "invalid-signature"
    assert _seen_events(tmp_path) == []

    second = chat_capture.capture_text(envelope=_envelope_json(message_id="bad-then-valid"))

    assert second["action"] == "auto"
    assert second["envelope_verified"] is True
    assert second["task_id"] == "tsk_ship"
    assert "Ship alpha milestone" not in work.read_text()
    assert _event_types(tmp_path) == ["candidate_seen", "state_transition", capture_envelope.SEEN_EVENT_TYPE]
    [seen] = _seen_events(tmp_path)
    assert seen["metadata"]["message_id"] == "bad-then-valid"
    assert "outcome" not in seen["metadata"]


def test_prior_verified_envelope_message_id_blocks_replay(tmp_path, monkeypatch):
    work = _write_work_file(tmp_path)
    original = work.read_text()
    _apply_env(monkeypatch, tmp_path, work, autowrite="true")
    raw = _envelope_json(message_id="replay-me")
    seen_event = {
        "event_id": "evt_prior_verified_envelope",
        "event_type": capture_envelope.SEEN_EVENT_TYPE,
        "timestamp": "2026-06-30T11:59:00+00:00",
        "actor": "task-tracker",
        "source": "chat_capture",
        "task_id": "tsk_ship",
        "previous_state": None,
        "next_state": None,
        "reason": None,
        "evidence": None,
        "metadata": {"channel": "telegram", "message_id": "replay-me"},
    }
    (tmp_path / "events.jsonl").write_text(json.dumps(seen_event) + "\n")

    payload = chat_capture.capture_text(envelope=raw)

    assert payload["action"] == "candidate"
    assert payload["envelope_verified"] is False
    assert payload["envelope_reason"] == "replayed-message-id"
    assert payload["candidate_created"] is True
    assert work.read_text() == original
    assert _event_types(tmp_path) == [capture_envelope.SEEN_EVENT_TYPE, "candidate_seen"]


def test_unrelated_receipt_message_id_does_not_block_verified_envelope(tmp_path, monkeypatch):
    work = _write_work_file(tmp_path)
    _apply_env(monkeypatch, tmp_path, work, autowrite="true")
    unrelated_event = {
        "event_id": "evt_nag_receipt",
        "event_type": "nag_sent",
        "timestamp": "2026-06-30T11:59:00+00:00",
        "actor": "task-tracker",
        "source": "agent_autonomous",
        "task_id": "tsk_ship",
        "previous_state": None,
        "next_state": None,
        "reason": None,
        "evidence": None,
        "metadata": {"message_id": "tg-1"},
    }
    (tmp_path / "events.jsonl").write_text(json.dumps(unrelated_event) + "\n")

    payload = chat_capture.capture_text(envelope=_envelope_json(message_id="tg-1"))

    assert payload["action"] == "auto"
    assert payload["envelope_verified"] is True
    assert payload["task_id"] == "tsk_ship"
    assert "Ship alpha milestone" not in work.read_text()
    assert _event_types(tmp_path) == ["nag_sent", "state_transition", capture_envelope.SEEN_EVENT_TYPE]


def test_raw_lane_b_candidate_message_id_does_not_block_later_envelope(tmp_path, monkeypatch):
    work = _write_work_file(tmp_path)
    _apply_env(monkeypatch, tmp_path, work, autowrite="true")

    raw_capture = chat_capture.capture_text(
        "finished Ship alpha milestone",
        sender="sender-123",
        channel="telegram",
        message_id="tg-1",
    )
    envelope_capture = chat_capture.capture_text(envelope=_envelope_json(message_id="tg-1"))

    assert raw_capture["action"] == "candidate"
    assert envelope_capture["action"] == "auto"
    assert envelope_capture["envelope_verified"] is True
    assert "Ship alpha milestone" not in work.read_text()
    assert _event_types(tmp_path) == ["candidate_seen", "state_transition", capture_envelope.SEEN_EVENT_TYPE]


def test_raw_lane_b_miss_message_id_does_not_block_later_envelope(tmp_path, monkeypatch):
    work = _write_work_file(tmp_path)
    _apply_env(monkeypatch, tmp_path, work, autowrite="true")

    raw_capture = chat_capture.capture_text("finished Reticulate splines", message_id="tg-1")
    envelope_capture = chat_capture.capture_text(envelope=_envelope_json(message_id="tg-1"))

    assert raw_capture["action"] == "miss"
    assert envelope_capture["action"] == "auto"
    assert envelope_capture["envelope_verified"] is True
    assert "Ship alpha milestone" not in work.read_text()
    assert _event_types(tmp_path) == ["capture_miss", "state_transition", capture_envelope.SEEN_EVENT_TYPE]


def test_envelope_seen_message_id_is_stripped_and_blocks_whitespace_replay(tmp_path, monkeypatch):
    work = _write_work_file(tmp_path)
    _apply_env(monkeypatch, tmp_path, work, autowrite="true")
    raw = _envelope_json(message_id="  msg-ws  ")

    first = chat_capture.capture_text(envelope=raw)
    second = chat_capture.capture_text(envelope=raw)
    events = _events(tmp_path)

    assert first["action"] == "auto"
    assert second["envelope_verified"] is False
    assert second["envelope_reason"] == "replayed-message-id"
    assert events[1]["event_type"] == capture_envelope.SEEN_EVENT_TYPE
    assert events[1]["metadata"]["message_id"] == "msg-ws"
    assert _event_types(tmp_path).count("state_transition") == 1


def test_malformed_capture_now_falls_back_without_crashing_envelope_capture(tmp_path, monkeypatch):
    work = _write_work_file(tmp_path)
    _apply_env(monkeypatch, tmp_path, work, autowrite="true", now="not-iso")
    timestamp = datetime.now(timezone.utc).isoformat()

    payload = chat_capture.capture_text(envelope=_envelope_json(message_id="bad-now", timestamp=timestamp))

    assert payload["action"] == "auto"
    assert payload["envelope_verified"] is True
    assert payload["task_id"] == "tsk_ship"
    assert "Ship alpha milestone" not in work.read_text()


def test_envelope_title_or_fuzzy_match_does_not_authorize_auto(tmp_path, monkeypatch):
    work = _write_work_file(tmp_path)
    original = work.read_text()
    _apply_env(monkeypatch, tmp_path, work, autowrite="true")

    payload = chat_capture.capture_text(
        envelope=_envelope_json(task_id="Ship alpha milestone", message_id="title-id"),
    )

    assert payload["action"] == "candidate"
    assert payload["decision_reason"] == "auto-task-not-found"
    assert payload["task_id"] == "tsk_ship"
    assert work.read_text() == original
    assert _event_types(tmp_path) == ["candidate_seen", capture_envelope.SEEN_EVENT_TYPE]
    [seen] = _seen_events(tmp_path)
    assert seen["metadata"]["message_id"] == "title-id"
    assert seen["metadata"]["outcome"] == "auto-task-not-found"


def test_quoted_raw_text_is_suppressed_to_miss_not_confirmable_candidate(tmp_path, monkeypatch):
    work = _write_work_file(tmp_path)
    _apply_env(monkeypatch, tmp_path, work, autowrite="true")

    payload = chat_capture.capture_text(
        "Bob: finished Ship alpha milestone",
        message_id="quote-1",
    )

    # A forwarded/quoted message that DOES match a task must not become a
    # confirmable candidate — the quality gate suppresses it to a miss.
    assert payload["action"] == "miss"
    assert payload["decision_reason"] == "quoted-or-forwarded"
    assert "candidate_seen" not in _event_types(tmp_path)


def test_raw_chat_retry_same_message_id_dedupes_candidate(tmp_path, monkeypatch):
    work = _write_work_file(tmp_path)
    _apply_env(monkeypatch, tmp_path, work, autowrite="false")

    first = chat_capture.capture_text("finished Ship alpha milestone", message_id="tg-77")
    # A gateway retry of the SAME message a moment later (different capture time)
    # must dedupe on message_id, not spawn a second candidate.
    monkeypatch.setenv("TASK_TRACKER_CAPTURE_NOW", "2999-01-01T00:00:00+00:00")
    second = chat_capture.capture_text("finished Ship alpha milestone", message_id="tg-77")

    assert first["action"] == "candidate"
    assert second["action"] == "candidate"
    assert _event_types(tmp_path).count("candidate_seen") == 1


def test_recurring_task_in_envelope_becomes_candidate_never_auto(tmp_path, monkeypatch):
    work = _write_work_file(
        tmp_path,
        """# Work

## 🔴 Q1
- [ ] **Send weekly update** task_id::tsk_weekly recur::weekly 🗓️2026-05-20
""",
    )
    original = work.read_text()
    _apply_env(monkeypatch, tmp_path, work, autowrite="true")

    payload = chat_capture.capture_text(
        envelope=_envelope_json(task_id="tsk_weekly", message_id="recurring"),
    )

    assert payload["action"] == "candidate"
    assert payload["decision_reason"] == "recurring-task"
    assert payload["task_id"] == "tsk_weekly"
    assert work.read_text() == original
    assert _event_types(tmp_path) == ["candidate_seen", capture_envelope.SEEN_EVENT_TYPE]
    [seen] = _seen_events(tmp_path)
    assert seen["metadata"]["message_id"] == "recurring"
    assert seen["metadata"]["outcome"] == "recurring-task"


def test_raw_chat_finished_statement_is_candidate_not_auto_even_when_flag_on(tmp_path, monkeypatch):
    work = _write_work_file(tmp_path)
    original = work.read_text()
    _apply_env(monkeypatch, tmp_path, work, autowrite="true")

    payload = chat_capture.capture_text(
        "finished Ship alpha milestone",
        sender="sender-123",
        channel="telegram",
        message_id="raw-1",
    )

    assert payload["action"] == "candidate"
    assert payload["envelope_verified"] is False
    assert payload["task_id"] == "tsk_ship"
    assert payload["candidate"]["source"]["sender"] == "sender-123"
    assert payload["candidate"]["source"]["channel"] == "telegram"
    assert payload["candidate"]["source"]["message_id"] == "raw-1"
    assert work.read_text() == original
    assert _event_types(tmp_path) == ["candidate_seen"]


def test_raw_negated_completion_statement_creates_miss_not_candidate(tmp_path, monkeypatch):
    work = _write_work_file(tmp_path)
    original = work.read_text()
    _apply_env(monkeypatch, tmp_path, work, autowrite="true")

    payload = chat_capture.capture_text("didn't finish Ship alpha milestone", sender="sender-123")

    assert payload["action"] == "miss"
    assert payload["decision_reason"] == "negated-or-hedged"
    assert _candidate_actions(payload) == []
    assert work.read_text() == original
    assert _event_types(tmp_path) == ["capture_miss"]


def test_raw_no_match_records_capture_miss(tmp_path, monkeypatch):
    work = _write_work_file(tmp_path)
    original = work.read_text()
    _apply_env(monkeypatch, tmp_path, work, autowrite="true")

    payload = chat_capture.capture_text("finished Reticulate splines")

    assert payload["action"] == "miss"
    assert payload["event_id"].startswith("evt_")
    assert work.read_text() == original
    events = _events(tmp_path)
    assert [event["event_type"] for event in events] == ["capture_miss"]
    assert events[0]["source"] == "chat_capture"
    assert events[0]["metadata"]["reason"] == "no-match"


@pytest.mark.parametrize(
    ("text", "task_id"),
    [
        ("scratch that, finished Ship alpha milestone task_id::tsk_ship", "tsk_ship"),
        ("> finished Ship alpha milestone task_id::tsk_ship", "tsk_ship"),
        ("Bob said finished Ship alpha milestone task_id::tsk_ship", "tsk_ship"),
        ("done thinking about Ship alpha milestone task_id::tsk_ship", "tsk_ship"),
        ("finished Ship alpha milestone task_id::tsk_ship and grabbed coffee", "tsk_ship"),
        ("starting on Fix login timeout task_id::tsk_login", "tsk_login"),
    ],
)
def test_old_prose_auto_bypass_vectors_are_candidate_only(tmp_path, monkeypatch, text, task_id):
    work = _write_work_file(tmp_path)
    original = work.read_text()
    _apply_env(monkeypatch, tmp_path, work, autowrite="true")

    payload = chat_capture.capture_text(text, sender="sender-123")

    assert payload["action"] == "candidate"
    assert payload["task_id"] == task_id
    assert "completion_id" not in payload
    assert work.read_text() == original
    assert _event_types(tmp_path) == ["candidate_seen"]


def test_capture_refuses_personal_scope_without_mutating_personal_board(tmp_path):
    work = _write_work_file(tmp_path)
    personal = tmp_path / "Personal Tasks.md"
    personal.write_text(
        """# Personal

## 🔴 Q1
- [ ] **Ship alpha milestone** task_id::tsk_ship area:: Personal
"""
    )
    env = os.environ.copy()
    env["TASK_TRACKER_WORK_FILE"] = str(work)
    env["TASK_TRACKER_PERSONAL_FILE"] = str(personal)
    env["TASK_TRACKER_LEDGER_FILE"] = str(tmp_path / "events.jsonl")
    env["TASK_TRACKER_CAPTURE_AUTOWRITE_ENABLED"] = "true"
    env["TASK_TRACKER_CAPTURE_ENVELOPE_SECRET"] = SECRET
    env["TASK_TRACKER_CAPTURE_NOW"] = NOW
    original_personal = personal.read_text()

    proc = subprocess.run(
        [
            "python3",
            str(ROOT / "scripts" / "tasks.py"),
            "--personal",
            "capture",
            "--text",
            "finished Ship alpha milestone",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert proc.returncode == 2
    payload = json.loads(proc.stdout)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "personal-capture-refused"
    assert personal.read_text() == original_personal


def test_capture_cli_accepts_verified_envelope_for_auto(tmp_path):
    work = _write_work_file(tmp_path)
    env = os.environ.copy()
    env["TASK_TRACKER_WORK_FILE"] = str(work)
    env["TASK_TRACKER_LEDGER_FILE"] = str(tmp_path / "events.jsonl")
    env["TASK_TRACKER_DAILY_NOTES_DIR"] = str(tmp_path / "daily")
    env["TASK_TRACKER_DONE_LOG_DIR"] = str(tmp_path / "daily")
    env["TASK_TRACKER_CAPTURE_AUTOWRITE_ENABLED"] = "true"
    env["TASK_TRACKER_CAPTURE_ENVELOPE_SECRET"] = SECRET
    env["TASK_TRACKER_CAPTURE_NOW"] = NOW
    env["STANDUP_CALENDARS"] = "{}"

    proc = subprocess.run(
        [
            "python3",
            str(ROOT / "scripts" / "tasks.py"),
            "capture",
            "--envelope",
            _envelope_json(message_id="cli-msg"),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert payload["action"] == "auto"
    assert payload["task_id"] == "tsk_ship"
    assert "Ship alpha milestone" not in work.read_text()
