import hashlib
import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import completion_candidates
import evidence_matching
import harvest_window
import standup_harvest
import task_records
import task_transitions


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


def _write_personal_file(tmp_path):
    personal = tmp_path / "Personal Tasks.md"
    personal.write_text(
        """# Personal

## 🔴 Q1
- [ ] **Buy replacement filter** task_id::tsk_filter area:: Home
"""
    )
    return personal


def _env(tmp_path, work):
    env = os.environ.copy()
    env["TASK_TRACKER_WORK_FILE"] = str(work)
    env["TASK_TRACKER_DAILY_NOTES_DIR"] = str(tmp_path / "daily")
    env["TASK_TRACKER_DONE_LOG_DIR"] = str(tmp_path / "daily")
    env["TASK_TRACKER_LEDGER_FILE"] = str(tmp_path / "events.jsonl")
    env["STANDUP_CALENDARS"] = "{}"
    return env


def _run(args, env, *, input_text=None):
    return subprocess.run(
        ["python3", "scripts/tasks.py", *args],
        input=input_text,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def _payload(proc):
    assert "Traceback" not in proc.stdout
    assert "Traceback" not in proc.stderr
    return json.loads(proc.stdout)


def _scan_file(tmp_path, env, text):
    source = tmp_path / "done.md"
    source.write_text(text)
    proc = _run(["completion-candidates", "scan", "--file", str(source)], env)
    assert proc.returncode == 0, proc.stderr
    return _payload(proc)


def _candidate_id(payload, index=0):
    return payload["created"][index]["candidate_id"]


def _legacy_candidate_id(source, summary):
    stable = {
        "source_type": source.get("type"),
        "source_path": source.get("path"),
        "source_date": source.get("date"),
        "line_number": source.get("line_number"),
        "timestamp": source.get("timestamp"),
        "summary": " ".join((summary or "").casefold().split()),
    }
    material = json.dumps(stable, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return f"cand_{hashlib.sha256(material.encode('utf-8')).hexdigest()[:20]}"


def _ledger_events(tmp_path):
    ledger = tmp_path / "events.jsonl"
    if not ledger.exists():
        return []
    return [json.loads(line) for line in ledger.read_text().splitlines() if line.strip()]


def test_scan_dedupes_same_evidence_without_task_mutation(tmp_path):
    work = _write_work_file(tmp_path)
    original = work.read_text()
    env = _env(tmp_path, work)

    first = _scan_file(tmp_path, env, "- Ship alpha milestone task_id::tsk_ship\n")
    second = _scan_file(tmp_path, env, "- Ship alpha milestone task_id::tsk_ship\n")

    assert first["totals"]["created"] == 1
    assert second["totals"]["created"] == 0
    assert second["totals"]["existing"] == 1
    assert work.read_text() == original
    assert [event["event_type"] for event in _ledger_events(tmp_path)] == ["candidate_seen"]


def test_scan_dedupes_legacy_file_candidate_hash_without_chat_keys(tmp_path):
    work = _write_work_file(tmp_path)
    env = _env(tmp_path, work)
    source = tmp_path / "done.md"
    source.write_text("- Ship alpha milestone task_id::tsk_ship\n")
    source_pointer = {"type": "file", "path": str(source), "line_number": 1}
    summary = "Ship alpha milestone task_id::tsk_ship"
    legacy_id = _legacy_candidate_id(source_pointer, summary)
    legacy_event = {
        "event_id": "evt_legacy_file_candidate",
        "event_type": "candidate_seen",
        "timestamp": "2026-05-21T00:00:00+00:00",
        "actor": "task-tracker",
        "source": "completion_candidate_scan",
        "task_id": legacy_id,
        "previous_state": None,
        "next_state": None,
        "reason": None,
        "evidence": None,
        "metadata": {
            "candidate": {
                "candidate_id": legacy_id,
                "status": "new",
                "source": source_pointer,
                "summary": summary,
                "matched_task_id": "tsk_ship",
                "match_metadata": {
                    "matched_task_id": "tsk_ship",
                    "match_type": "exact-id-or-link",
                    "decision": "evidence-link",
                    "score": 1.0,
                },
            }
        },
    }
    (tmp_path / "events.jsonl").write_text(json.dumps(legacy_event) + "\n")

    payload = _payload(_run(["completion-candidates", "scan", "--file", str(source)], env))

    assert completion_candidates.candidate_id_for(source_pointer, summary) == legacy_id
    assert payload["totals"]["created"] == 0
    assert payload["totals"]["existing"] == 1
    assert payload["existing"][0]["candidate_id"] == legacy_id
    assert [event["event_type"] for event in _ledger_events(tmp_path)] == ["candidate_seen"]


def test_scan_preserves_original_source_line_number(tmp_path):
    work = _write_work_file(tmp_path)
    env = _env(tmp_path, work)

    payload = _scan_file(
        tmp_path,
        env,
        "# Done today\n\n- Ship alpha milestone task_id::tsk_ship\n",
    )

    assert payload["created"][0]["source"]["line_number"] == 3


def test_scheduled_calendar_becomes_candidate(tmp_path, monkeypatch):
    work = _write_work_file(tmp_path)
    original = work.read_text()
    env = _env(tmp_path, work)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(evidence_matching, "get_tasks_file", lambda personal=False: (work, "obsidian"))
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()

    payload = completion_candidates.scan_adapter_records(
        [
            {
                "source": "calendar",
                "kind": "commitment",
                "provider_id": "calendar-fixture-event",
                "provider_state": "status=confirmed;response=accepted;updated=fixture",
                "occurred_at": future,
                "match_title": "Ship alpha milestone task_id::tsk_ship",
                "title": "Ship alpha milestone task_id::tsk_ship",
            }
        ]
    )

    candidate = payload["created"][0]
    assert payload["totals"]["created"] == 1
    assert candidate["source"]["source"] == "calendar"
    assert candidate["low_trust"] is True
    assert candidate["candidate_only"] is True
    assert candidate["auto_done_eligible"] is False
    assert candidate["confirmable_task_id"] == "tsk_ship"
    assert work.read_text() == original
    assert [event["event_type"] for event in _ledger_events(tmp_path)] == ["candidate_seen"]


def test_sms_becomes_candidate(tmp_path, monkeypatch):
    work = _write_work_file(tmp_path)
    original = work.read_text()
    env = _env(tmp_path, work)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(evidence_matching, "get_tasks_file", lambda personal=False: (work, "obsidian"))

    payload = completion_candidates.scan_adapter_records(
        [
            {
                "source": "dialpad_sms",
                "kind": "activity",
                "provider_id": "sms:+15550101010:2026-07-01",
                "provider_state": "outbound=2;chars=48;sha256=fixturehash",
                "occurred_at": "2026-07-01T09:00:00+00:00",
                "match_title": "SMS thread with +1 (555) 010-1010 (2 sent)",
                "title": "SMS thread with +1 (555) 010-1010 (2 sent)",
            }
        ]
    )

    candidate = payload["created"][0]
    assert payload["totals"]["created"] == 1
    assert candidate["source"]["source"] == "dialpad_sms"
    assert candidate["summary"] == "SMS thread with <contact> (2 sent)"
    assert candidate["low_trust"] is True
    assert candidate["candidate_only"] is True
    assert candidate["auto_done_eligible"] is False
    assert work.read_text() == original
    assert [event["event_type"] for event in _ledger_events(tmp_path)] == ["candidate_seen"]


def test_sms_candidate_ids_include_provider_identity_after_masking(tmp_path, monkeypatch):
    work = _write_work_file(tmp_path)
    env = _env(tmp_path, work)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(evidence_matching, "get_tasks_file", lambda personal=False: (work, "obsidian"))

    payload = completion_candidates.scan_adapter_records(
        [
            {
                "source": "dialpad_sms",
                "kind": "activity",
                "provider_id": "sms:+15550101010:2026-07-01",
                "provider_state": "outbound=2;chars=48;sha256=fixturehash-a",
                "occurred_at": "2026-07-01T09:00:00+00:00",
                "match_title": "SMS thread with +1 (555) 010-1010 (2 sent)",
                "title": "SMS thread with +1 (555) 010-1010 (2 sent)",
            },
            {
                "source": "dialpad_sms",
                "kind": "activity",
                "provider_id": "sms:+15550102020:2026-07-01",
                "provider_state": "outbound=2;chars=48;sha256=fixturehash-b",
                "occurred_at": "2026-07-01T09:30:00+00:00",
                "match_title": "SMS thread with +1 (555) 020-2020 (2 sent)",
                "title": "SMS thread with +1 (555) 020-2020 (2 sent)",
            },
        ]
    )

    candidate_ids = {candidate["candidate_id"] for candidate in payload["created"]}
    assert payload["totals"]["created"] == 2
    assert len(candidate_ids) == 2
    assert {candidate["summary"] for candidate in payload["created"]} == {"SMS thread with <contact> (2 sent)"}
    assert [event["event_type"] for event in _ledger_events(tmp_path)] == ["candidate_seen", "candidate_seen"]


def test_no_raw_sms_body_or_phone_in_candidate(tmp_path, monkeypatch):
    work = _write_work_file(tmp_path)
    env = _env(tmp_path, work)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(evidence_matching, "get_tasks_file", lambda personal=False: (work, "obsidian"))
    raw_phone = "+1 (555) 010-2020"
    raw_body = "fixture raw body: shipped the alpha milestone"

    payload = completion_candidates.scan_adapter_records(
        [
            {
                "source": "dialpad_sms",
                "kind": "activity",
                "provider_id": f"sms:{raw_phone}:2026-07-01",
                "provider_state": "outbound=3;chars=91;sha256=fixturehash",
                "occurred_at": "2026-07-01T09:00:00+00:00",
                "match_title": f"SMS thread with {raw_phone} (3 sent)",
                "title": f"SMS thread with {raw_phone} (3 sent)",
                "body": raw_body,
                "text": raw_body,
            }
        ]
    )

    serialized_candidate = json.dumps(payload["created"][0], sort_keys=True)
    ledger_text = (tmp_path / "events.jsonl").read_text()
    assert payload["created"][0]["title"] == "SMS thread with <contact> (3 sent)"
    assert raw_phone not in serialized_candidate
    assert raw_body not in serialized_candidate
    assert raw_phone not in ledger_text
    assert raw_body not in ledger_text


def test_body_style_sms_title_is_replaced_not_passed_through(tmp_path, monkeypatch):
    work = _write_work_file(tmp_path)
    env = _env(tmp_path, work)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(evidence_matching, "get_tasks_file", lambda personal=False: (work, "obsidian"))
    raw_title = "shipped alpha at 3pm"

    payload = completion_candidates.scan_adapter_records(
        [
            {
                "source": "dialpad_sms",
                "kind": "activity",
                "provider_id": "sms:+15550103030:2026-07-01",
                "provider_state": "outbound=4;chars=91;sha256=fixturehash",
                "occurred_at": "2026-07-01T09:00:00+00:00",
                "match_title": raw_title,
                "title": raw_title,
            }
        ]
    )

    candidate = payload["created"][0]
    assert candidate["title"] == "SMS thread with <contact> (4 sent)"
    assert raw_title not in candidate["title"]
    assert raw_title not in candidate["summary"]
    assert raw_title not in candidate["raw_summary"]


def test_low_trust_calendar_classification_response_status_and_malformed_records():
    declined = {
        "source": "calendar",
        "kind": "activity",
        "provider_state": "status=confirmed;response=declined;updated=fixture",
        "title": "Declined sync",
    }
    tentative = {
        "source": "calendar",
        "kind": "activity",
        "response": "tentative",
        "title": "Tentative sync",
    }
    accepted = {
        "source": "calendar",
        "kind": "activity",
        "provider_state": "status=confirmed;response=accepted;updated=fixture",
        "title": "Accepted sync",
    }

    assert completion_candidates._is_low_trust_calendar(declined) is True
    assert completion_candidates._is_low_trust_calendar(tentative) is True
    assert completion_candidates._is_low_trust_calendar(accepted) is False
    assert completion_candidates._is_low_trust_adapter_record({}) is False
    assert completion_candidates._is_low_trust_adapter_record({"kind": "activity", "title": 123}) is False


def test_standup_harvest_persists_low_trust_adapter_candidates(tmp_path, monkeypatch):
    work = _write_work_file(tmp_path)
    env = _env(tmp_path, work)
    env["TASK_MGMT_STATE_DIR"] = str(tmp_path / "state")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(evidence_matching, "get_tasks_file", lambda personal=False: (work, "obsidian"))
    monkeypatch.setattr(standup_harvest.standup_summarizer, "summarize", lambda *_args, **_kwargs: {})
    resolved = harvest_window.resolve_standup_window(target_date=date(2026, 7, 1))

    def harvest_source(source, **_kwargs):
        if source == "dialpad_sms":
            return [
                {
                    "source": "dialpad_sms",
                    "kind": "activity",
                    "provider_id": "sms:+15550104040:2026-07-01",
                    "provider_state": "outbound=2;chars=48;sha256=fixturehash",
                    "occurred_at": resolved.evidence_start.isoformat(),
                    "match_title": "SMS thread with +1 (555) 040-4040 (2 sent)",
                    "title": "SMS thread with +1 (555) 040-4040 (2 sent)",
                }
            ], False
        return [], False

    monkeypatch.setattr(standup_harvest, "_harvest_source", harvest_source)

    result = standup_harvest.harvest(trigger="test", resolved_window=resolved)
    durable = completion_candidates.project_candidates(personal=False)

    assert len(result["evidence_candidates"]) == 1
    assert len(durable) == 1
    assert durable[0]["source"]["source"] == "dialpad_sms"
    assert durable[0]["summary"] == "SMS thread with <contact> (2 sent)"
    assert durable[0]["low_trust"] is True
    assert durable[0]["candidate_only"] is True
    assert durable[0]["auto_done_eligible"] is False


def test_title_candidate_requires_explicit_task_id_before_confirmation(tmp_path):
    work = _write_work_file(tmp_path)
    env = _env(tmp_path, work)
    scan = _scan_file(tmp_path, env, "- Ship alpha milestone\n")
    candidate_id = _candidate_id(scan)
    candidate = scan["created"][0]

    assert "confirmable_task_id" not in candidate
    assert candidate["suggested_match"]["task_id"] == "tsk_ship"
    assert candidate["review_required"] is True

    blocked = _run(["completion-candidates", "confirm", candidate_id], env)
    blocked_payload = _payload(blocked)

    assert blocked.returncode == 2
    assert blocked_payload["error"]["code"] == "explicit-task-id-required"
    assert "Ship alpha milestone" in work.read_text()

    confirmed = _run(["completion-candidates", "confirm", candidate_id, "--task-id", "tsk_ship"], env)
    confirmed_payload = _payload(confirmed)

    assert confirmed.returncode == 0
    assert confirmed_payload["ok"] is True
    assert "Ship alpha milestone" not in work.read_text()
    events = _ledger_events(tmp_path)
    assert [event["event_type"] for event in events] == [
        "candidate_seen",
        "state_transition",
        "candidate_confirmed",
    ]
    assert events[1]["source"] == "completion_candidate"


def test_exact_canonical_id_candidate_can_confirm_without_extra_task_id(tmp_path):
    work = _write_work_file(tmp_path)
    env = _env(tmp_path, work)
    scan = _scan_file(tmp_path, env, "- Fixed login timeout task_id::tsk_login\n")
    candidate = scan["created"][0]

    assert candidate["confirmable_task_id"] == "tsk_login"
    assert candidate["review_required"] is False

    proc = _run(["completion-candidates", "confirm", _candidate_id(scan)], env)
    payload = _payload(proc)

    assert proc.returncode == 0
    assert payload["ok"] is True
    assert payload["candidate"]["status"] == "confirmed"
    assert "Fix login timeout" not in work.read_text()


def test_legacy_exact_candidate_projects_confirmable_task_id(tmp_path):
    work = _write_work_file(tmp_path)
    env = _env(tmp_path, work)
    candidate_id = "cand_legacy_exact"
    legacy_event = {
        "event_id": "evt_legacy",
        "event_type": "candidate_seen",
        "timestamp": "2026-05-21T00:00:00+00:00",
        "actor": "task-tracker",
        "source": "completion_candidate_scan",
        "task_id": candidate_id,
        "previous_state": None,
        "next_state": None,
        "reason": None,
        "evidence": None,
        "metadata": {
            "candidate": {
                "candidate_id": candidate_id,
                "status": "new",
                "source": {"type": "file", "path": "/tmp/legacy.md"},
                "summary": "Fix login timeout task_id::tsk_login",
                "matched_task_id": "tsk_login",
                "match_metadata": {
                    "matched_task_id": "tsk_login",
                    "match_type": "exact-id-or-link",
                    "decision": "evidence-link",
                    "score": 1.0,
                },
            }
        },
    }
    (tmp_path / "events.jsonl").write_text(json.dumps(legacy_event) + "\n")

    shown = _payload(_run(["completion-candidates", "show", candidate_id], env))
    confirmed = _run(["completion-candidates", "confirm", candidate_id], env)

    assert shown["candidate"]["confirmable_task_id"] == "tsk_login"
    assert shown["candidate"]["review_required"] is False
    assert confirmed.returncode == 0
    assert "Fix login timeout" not in work.read_text()


def test_fallback_only_candidate_cannot_confirm_without_supplied_canonical_id(tmp_path):
    work = _write_work_file(
        tmp_path,
        """# Work

## 🔴 Q1
- [ ] **Legacy title only** area:: Delivery
""",
    )
    env = _env(tmp_path, work)
    scan = _scan_file(tmp_path, env, "- Legacy title only\n")
    candidate = scan["created"][0]

    assert "confirmable_task_id" not in candidate
    assert candidate["review_required"] is True

    proc = _run(["completion-candidates", "confirm", _candidate_id(scan)], env)
    payload = _payload(proc)

    assert proc.returncode == 2
    assert payload["error"]["code"] == "explicit-task-id-required"
    assert "Legacy title only" in work.read_text()


def test_reject_and_snooze_remove_candidates_from_default_list(tmp_path):
    work = _write_work_file(tmp_path)
    env = _env(tmp_path, work)
    scan = _scan_file(
        tmp_path,
        env,
        "- Ship alpha milestone task_id::tsk_ship\n- Fix login timeout task_id::tsk_login\n",
    )
    rejected_id = _candidate_id(scan, 0)
    snoozed_id = _candidate_id(scan, 1)

    reject = _run(["completion-candidates", "reject", rejected_id, "--reason", "not done"], env)
    snooze_until = (date.today() + timedelta(days=7)).isoformat()
    snooze = _run(["completion-candidates", "snooze", snoozed_id, "--until", snooze_until], env)
    listed = _run(["completion-candidates", "list"], env)
    listed_all = _run(["completion-candidates", "list", "--all"], env)

    assert reject.returncode == 0
    assert snooze.returncode == 0
    assert _payload(listed)["total"] == 0
    all_payload = _payload(listed_all)
    statuses = {candidate["candidate_id"]: candidate["status"] for candidate in all_payload["candidates"]}
    assert statuses[rejected_id] == "rejected"
    assert statuses[snoozed_id] == "snoozed"


def test_list_mark_shown_preserves_default_snooze_filter(tmp_path):
    work = _write_work_file(tmp_path)
    env = _env(tmp_path, work)
    scan = _scan_file(tmp_path, env, "- Ship alpha milestone task_id::tsk_ship\n")
    candidate_id = _candidate_id(scan)
    snooze_until = (date.today() + timedelta(days=7)).isoformat()

    snooze = _run(["completion-candidates", "snooze", candidate_id, "--until", snooze_until], env)
    listed = _run(["completion-candidates", "list", "--mark-shown"], env)
    listed_all = _run(["completion-candidates", "list", "--all"], env)

    assert snooze.returncode == 0
    assert _payload(listed)["total"] == 0
    all_candidates = _payload(listed_all)["candidates"]
    assert all_candidates[0]["candidate_id"] == candidate_id
    assert all_candidates[0]["status"] == "snoozed"


def test_duplicate_candidate_is_terminal_and_links_target(tmp_path):
    work = _write_work_file(tmp_path)
    env = _env(tmp_path, work)
    scan = _scan_file(
        tmp_path,
        env,
        "- Ship alpha milestone task_id::tsk_ship\n- Fix login timeout task_id::tsk_login\n",
    )
    duplicate_id = _candidate_id(scan, 1)
    target_id = _candidate_id(scan, 0)

    proc = _run(["completion-candidates", "duplicate", duplicate_id, "--of", target_id], env)
    listed = _run(["completion-candidates", "list"], env)
    listed_all = _run(["completion-candidates", "list", "--all"], env)

    assert proc.returncode == 0
    payload = _payload(proc)
    assert payload["candidate"]["status"] == "duplicate"
    assert payload["candidate"]["duplicate_of"] == target_id
    assert duplicate_id not in {item["candidate_id"] for item in _payload(listed)["candidates"]}
    assert duplicate_id in {item["candidate_id"] for item in _payload(listed_all)["candidates"]}


def test_duplicate_candidate_rejects_self_reference(tmp_path):
    work = _write_work_file(tmp_path)
    env = _env(tmp_path, work)
    scan = _scan_file(tmp_path, env, "- Ship alpha milestone task_id::tsk_ship\n")
    candidate_id = _candidate_id(scan)

    proc = _run(["completion-candidates", "duplicate", candidate_id, "--of", candidate_id], env)
    payload = _payload(proc)

    assert proc.returncode == 2
    assert payload["error"]["code"] == "self-duplicate-blocked"
    assert _payload(_run(["completion-candidates", "list"], env))["total"] == 1


def test_apply_failed_keeps_candidate_retryable_and_board_unchanged(tmp_path):
    work = _write_work_file(tmp_path)
    env = _env(tmp_path, work)
    scan = _scan_file(tmp_path, env, "- Ship alpha milestone task_id::tsk_ship\n")
    candidate_id = _candidate_id(scan)
    original = work.read_text()

    work.write_text(original.replace("- [ ] **Ship alpha milestone** task_id::tsk_ship area:: Delivery\n", ""))
    proc = _run(["completion-candidates", "confirm", candidate_id], env)
    payload = _payload(proc)

    assert proc.returncode == 2
    assert payload["error"]["code"] == "canonical-id-resolution-failed"
    assert payload["candidate"]["status"] == "apply_failed"
    assert any(event["event_type"] == "candidate_apply_failed" for event in _ledger_events(tmp_path))


def test_stale_candidate_confirm_closes_as_duplicate(tmp_path):
    work = _write_work_file(tmp_path)
    env = _env(tmp_path, work)
    scan = _scan_file(tmp_path, env, "- Ship alpha milestone task_id::tsk_ship\n")
    candidate_id = _candidate_id(scan)

    done = _run(["done", "tsk_ship"], env)
    proc = _run(["completion-candidates", "confirm", candidate_id], env)
    payload = _payload(proc)
    listed = _payload(_run(["completion-candidates", "list"], env))

    events = _ledger_events(tmp_path)
    event_types = [event["event_type"] for event in events]
    assert done.returncode == 0
    assert proc.returncode == 0
    assert payload["ok"] is True
    assert payload["candidate"]["status"] == "duplicate"
    assert payload["candidate"]["duplicate_of_task_id"] == "tsk_ship"
    assert not str(payload["candidate"].get("duplicate_of") or "").startswith("tsk_")
    assert event_types == ["candidate_seen", "state_transition", "candidate_duplicate"]
    assert "candidate_apply_failed" not in event_types
    assert listed["total"] == 0


def test_stale_cancelled_candidate_confirm_closes_as_duplicate(tmp_path):
    work = _write_work_file(tmp_path)
    env = _env(tmp_path, work)
    scan = _scan_file(tmp_path, env, "- Ship alpha milestone task_id::tsk_ship\n")
    candidate_id = _candidate_id(scan)

    cancelled = _run(["remove", "tsk_ship"], env)
    proc = _run(["completion-candidates", "confirm", candidate_id], env)
    payload = _payload(proc)
    listed = _payload(_run(["completion-candidates", "list"], env))

    events = _ledger_events(tmp_path)
    event_types = [event["event_type"] for event in events]
    assert cancelled.returncode == 0
    assert proc.returncode == 0
    assert payload["ok"] is True
    assert payload["candidate"]["status"] == "duplicate"
    assert payload["candidate"]["duplicate_of_task_id"] == "tsk_ship"
    assert not str(payload["candidate"].get("duplicate_of") or "").startswith("tsk_")
    assert event_types == ["candidate_seen", "state_transition", "candidate_duplicate"]
    assert listed["total"] == 0


def test_scan_rejects_conflicting_file_and_date_sources(tmp_path):
    work = _write_work_file(tmp_path)
    env = _env(tmp_path, work)
    source = tmp_path / "done.md"
    source.write_text("- Ship alpha milestone task_id::tsk_ship\n")

    proc = _run(
        [
            "completion-candidates",
            "scan",
            "--file",
            str(source),
            "--date",
            date.today().isoformat(),
        ],
        env,
    )
    payload = _payload(proc)

    assert proc.returncode == 2
    assert payload["error"]["code"] == "conflicting-scan-sources"
    assert not (tmp_path / "events.jsonl").exists()


def test_personal_candidate_lifecycle_uses_personal_ledger_without_override(tmp_path):
    work = _write_work_file(tmp_path)
    personal = _write_personal_file(tmp_path)
    env = _env(tmp_path, work)
    env["TASK_TRACKER_PERSONAL_FILE"] = str(personal)
    env.pop("TASK_TRACKER_LEDGER_FILE")

    scan = _run(
        [
            "--personal",
            "completion-candidates",
            "scan",
        ],
        env,
        input_text="- Buy replacement filter task_id::tsk_filter\n",
    )
    scan_payload = _payload(scan)
    candidate_id = _candidate_id(scan_payload)
    confirm = _run(["--personal", "completion-candidates", "confirm", candidate_id], env)
    list_payload = _payload(_run(["--personal", "completion-candidates", "list"], env))

    work_ledger = work.with_suffix(work.suffix + ".events.jsonl")
    personal_ledger = personal.with_suffix(personal.suffix + ".events.jsonl")
    personal_events = [
        json.loads(line) for line in personal_ledger.read_text().splitlines() if line.strip()
    ]

    assert scan.returncode == 0
    assert confirm.returncode == 0
    assert list_payload["total"] == 0
    assert not work_ledger.exists()
    assert [event["event_type"] for event in personal_events] == [
        "candidate_seen",
        "state_transition",
        "candidate_confirmed",
    ]
    assert "Buy replacement filter" not in personal.read_text()


def test_confirm_restores_task_state_when_candidate_event_append_fails(tmp_path, monkeypatch):
    work = _write_work_file(tmp_path)
    env = _env(tmp_path, work)
    scan = _scan_file(tmp_path, env, "- Ship alpha milestone task_id::tsk_ship\n")
    candidate_id = _candidate_id(scan)
    original = work.read_text()

    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(task_records, "get_tasks_file", lambda personal=False: (work, "obsidian"))

    real_append = task_transitions.append_event

    def fail_candidate_confirmed(event, path=None):
        if event.get("event_type") == "candidate_confirmed":
            raise OSError("simulated candidate event append failure")
        return real_append(event, path=path)

    monkeypatch.setattr(task_transitions, "append_event", fail_candidate_confirmed)

    result = completion_candidates.confirm_candidate(candidate_id)

    assert result["ok"] is False
    assert result["error"]["code"] == "ledger-append-failed"
    assert work.read_text() == original
    assert not list((tmp_path / "daily").glob("*.md"))
    event_types = [event["event_type"] for event in _ledger_events(tmp_path)]
    assert event_types == ["candidate_seen", "candidate_apply_failed"]


def test_malformed_ledger_blocks_candidate_projection(tmp_path):
    work = _write_work_file(tmp_path)
    env = _env(tmp_path, work)
    (tmp_path / "events.jsonl").write_text("{not json}\n")

    proc = _run(["completion-candidates", "list"], env)
    payload = _payload(proc)

    assert proc.returncode == 2
    assert payload["error"]["code"] == "malformed-ledger"
    assert payload["error"]["malformed"][0]["line_number"] == 1


def test_scan_daily_note_uses_configured_notes_directory(tmp_path):
    work = _write_work_file(tmp_path)
    env = _env(tmp_path, work)
    notes_dir = tmp_path / "daily"
    notes_dir.mkdir()
    today = date.today().isoformat()
    (notes_dir / f"{today}.md").write_text("- Write onboarding docs task_id::tsk_docs\n")

    proc = _run(["completion-candidates", "scan", "--date", today], env)
    payload = _payload(proc)

    assert proc.returncode == 0
    assert payload["totals"]["created"] == 1
    assert payload["created"][0]["source"]["type"] == "daily_note"


def test_workflow_control_wrapper_delegates_candidate_decisions(tmp_path):
    work = _write_work_file(tmp_path)
    env = _env(tmp_path, work)
    scan = _scan_file(tmp_path, env, "- Ship alpha milestone\n")
    candidate_id = _candidate_id(scan)

    listed = subprocess.run(
        ["python3", "scripts/completion_inbox_control.py", "list"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    blocked = subprocess.run(
        ["python3", "scripts/completion_inbox_control.py", "confirm", candidate_id],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    confirmed = subprocess.run(
        [
            "python3",
            "scripts/completion_inbox_control.py",
            "confirm",
            candidate_id,
            "--task-id",
            "tsk_ship",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert listed.returncode == 0
    assert json.loads(listed.stdout)["total"] == 1
    assert blocked.returncode == 2
    assert json.loads(blocked.stdout)["error"]["code"] == "explicit-task-id-required"
    assert confirmed.returncode == 0
    assert json.loads(confirmed.stdout)["ok"] is True
    assert "Ship alpha milestone" not in work.read_text()


def test_standalone_workflow_scripts_surface_candidates_without_mutation(tmp_path):
    work = _write_work_file(tmp_path)
    env = _env(tmp_path, work)
    env["EOD_DAILY_DIR"] = str(tmp_path / "daily")
    env["EOD_OUTPUT_DIR"] = str(tmp_path / "reports")
    original = work.read_text()
    scan = _scan_file(tmp_path, env, "- Ship alpha milestone\n")
    candidate_id = _candidate_id(scan)

    standup = subprocess.run(
        ["python3", "scripts/standup.py", "--compact-json", "--skip-missed"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    eod = subprocess.run(
        ["python3", "scripts/eod_review.py", "--json"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    weekly = subprocess.run(
        ["python3", "scripts/weekly_review.py"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert standup.returncode == 0
    assert eod.returncode == 0
    assert weekly.returncode == 0
    assert json.loads(standup.stdout)["completion_candidates"]["items"][0]["candidate_id"] == candidate_id
    assert json.loads(eod.stdout)["completion_candidates"]["items"][0]["candidate_id"] == candidate_id
    assert candidate_id in weekly.stdout
    assert work.read_text() == original
