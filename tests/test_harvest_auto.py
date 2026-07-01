import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import eod_ritual
import harvest_auto
import harvest_ledger
import utils

WORK_BOARD = """# Work

## Q1
- [ ] **Add social updates to World Cup skill** https://github.com/kesslerio/world-cup-skill/issues/101 task_id::tsk_abc123 area:: Delivery
- [ ] **Camp coordination for June** task_id::tsk_def456 area:: Ops
"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    work = tmp_path / "Work Tasks.md"
    work.write_text(WORK_BOARD)
    ledger = tmp_path / "events.jsonl"
    daily = tmp_path / "daily"

    monkeypatch.setenv("TASK_MGMT_STATE_DIR", str(state_dir))
    monkeypatch.setenv("TASK_TRACKER_WORK_FILE", str(work))
    monkeypatch.setenv("TASK_TRACKER_LEDGER_FILE", str(ledger))
    monkeypatch.setenv("TASK_TRACKER_DAILY_NOTES_DIR", str(daily))
    monkeypatch.setenv("TASK_TRACKER_DONE_LOG_DIR", str(daily))
    monkeypatch.setenv("TASK_TRACKER_ERROR_LOG", str(state_dir / "errors.jsonl"))
    monkeypatch.setenv("TASK_TRACKER_GITHUB_OWNER", "niemand")
    monkeypatch.delenv(harvest_auto.AUTO_ENV, raising=False)
    monkeypatch.delenv("STANDUP_CALENDARS", raising=False)
    monkeypatch.setattr(utils, "OBSIDIAN_WORK", work)
    return {"work": work, "ledger": ledger, "state_dir": state_dir, "daily": daily}


def _enable_auto(monkeypatch):
    monkeypatch.setenv(harvest_auto.AUTO_ENV, "true")


def _pr(
    title: str,
    number: int = 7,
    *,
    closes: int | None = None,
    repo: str = "kesslerio/world-cup-skill",
    author: str = "niemand",
    state: str = "MERGED",
    merged_at: str | None = "2026-06-23T18:00:00Z",
) -> dict:
    closing = []
    if closes is not None:
        closing.append({
            "number": closes,
            "repository": {"nameWithOwner": repo},
            "url": f"https://github.com/{repo}/issues/{closes}",
        })
    return {
        "title": title,
        "number": number,
        "repository": {"nameWithOwner": repo},
        "url": f"https://github.com/{repo}/pull/{number}",
        "mergedAt": merged_at,
        "state": state,
        "author": {"login": author},
        "closingIssuesReferences": closing,
    }


def _gmail_subject(subject: str, message_id: str = "msg-1") -> dict:
    return {
        "threads": [{
            "id": "thread-1",
            "subject": subject,
            "messages": [{
                "id": message_id,
                "subject": subject,
                "internalDate": "1782200000000",
            }],
        }]
    }


def _event(
    event_id: str,
    summary: str,
    start: str,
    *,
    response: str = "accepted",
    organizer_self: bool = False,
) -> dict:
    return {
        "id": event_id,
        "summary": summary,
        "status": "confirmed",
        "start": {"dateTime": start},
        "end": {"dateTime": start},
        "attendees": [{"email": "owner@example.test", "self": True, "responseStatus": response}],
        "organizer": {"email": "owner@example.test", "self": organizer_self},
        "htmlLink": f"https://calendar.example.test/{event_id}",
        "updated": "2026-06-23T12:00:00Z",
    }


class _Completed:
    def __init__(self, returncode: int, stdout: str, stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _stub_subprocesses(
    monkeypatch,
    *,
    gh_payload=None,
    gmail_payload=None,
    calendar_events=None,
    calendar_returncode: int = 0,
    calendar_exc: Exception | None = None,
):
    def fake_run(cmd, **kwargs):
        if cmd[0] == "gh":
            return _Completed(0, json.dumps(gh_payload if gh_payload is not None else []))
        if cmd[0] == "gog" and len(cmd) > 1 and cmd[1] == "gmail":
            return _Completed(0, json.dumps(gmail_payload if gmail_payload is not None else {"threads": []}))
        if cmd[0] == "gog" and len(cmd) > 1 and cmd[1] == "calendar":
            if calendar_exc is not None:
                raise calendar_exc
            if calendar_returncode:
                return _Completed(calendar_returncode, "", "calendar fixture failure")
            return _Completed(0, json.dumps({"events": calendar_events or []}))
        raise AssertionError(f"unexpected command {cmd!r}")

    monkeypatch.setattr(subprocess, "run", fake_run)


def _configure_calendar(monkeypatch, events, *, now="2026-06-23T12:00:00-07:00"):
    monkeypatch.setenv(
        "STANDUP_CALENDARS",
        json.dumps({"work": {"cmd": "gog", "calendar_id": "cal_fixture", "account": "owner@example.test"}}),
    )
    monkeypatch.setattr(
        harvest_auto.calendar_adapter.cos_config,
        "local_now",
        lambda: datetime.fromisoformat(now),
    )
    _stub_subprocesses(monkeypatch, calendar_events=events)


def _run_auto(**kwargs):
    return harvest_auto.run_auto_harvest(
        "24h",
        since_override="2026-06-23",
        trigger="test",
        **kwargs,
    )


def _events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_flag_off_exact_merged_pr_goes_to_candidates(env, monkeypatch):
    _stub_subprocesses(
        monkeypatch,
        gh_payload=[_pr("Add social updates to World Cup skill", closes=101)],
    )

    result = _run_auto()

    assert result["completed"] == []
    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["matched_task_id"] == "tsk_abc123"
    assert "tsk_abc123" in env["work"].read_text()


def test_flag_on_pr_that_mentions_task_id_without_closing_issue_stays_candidate(env, monkeypatch):
    _enable_auto(monkeypatch)
    _stub_subprocesses(
        monkeypatch,
        gh_payload=[_pr("Add social updates to World Cup skill task_id::tsk_abc123")],
    )

    result = _run_auto()

    assert result["completed"] == []
    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["matched_task_id"] == "tsk_abc123"
    assert "tsk_abc123" in env["work"].read_text()


def test_flag_on_pr_title_url_match_without_closing_issue_stays_candidate(env, monkeypatch):
    _enable_auto(monkeypatch)
    env["work"].write_text("""# Work

## Q1
- [ ] **Unrelated URL-tracked task** https://github.com/kesslerio/world-cup-skill/issues/202 task_id::tsk_url area:: Ops
""")
    _stub_subprocesses(
        monkeypatch,
        gh_payload=[_pr("Tests still fail for https://github.com/kesslerio/world-cup-skill/issues/202")],
    )

    result = _run_auto()

    assert result["completed"] == []
    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["matched_task_id"] == "tsk_url"
    assert "tsk_url" in env["work"].read_text()


def test_flag_on_pr_that_closes_tracked_issue_auto_completes_non_recurring_task(env, monkeypatch):
    _enable_auto(monkeypatch)
    _stub_subprocesses(
        monkeypatch,
        gh_payload=[_pr("Ship social updates", closes=101)],
    )

    result = _run_auto()

    assert result["completed"] == [{
        "task_id": "tsk_abc123",
        "ok": True,
        "source": "merged_pr",
        "url": "https://github.com/kesslerio/world-cup-skill/pull/7",
        "completion_id": result["completed"][0]["completion_id"],
    }]
    assert result["candidates"] == []
    assert "tsk_abc123" not in env["work"].read_text()

    events = _events(env["ledger"])
    transition = next(event for event in events if event["event_type"] == "state_transition")
    evidence = next(event for event in events if event["event_type"] == "evidence_link")
    assert transition["source"] == "merged_pr"
    assert transition["task_id"] == "tsk_abc123"
    assert evidence["source"] == "merged_pr"
    assert evidence["evidence"]["source_type"] == "pr"
    assert evidence["evidence"]["match_type"] == "closed-issue-reference"
    assert evidence["metadata"]["high_trust_auto"] is True


def test_flag_on_cross_repo_bare_issue_number_stays_candidate_not_auto(env, monkeypatch):
    _enable_auto(monkeypatch)
    env["work"].write_text("""# Work

## Q1
- [ ] **Investigate ranking regressions #12** task_id::tsk_rank area:: Delivery
""")
    _stub_subprocesses(
        monkeypatch,
        gh_payload=[_pr(
            "Investigate ranking regressions #12",
            closes=12,
            repo="kesslerio/other-repo",
        )],
    )

    result = _run_auto()

    assert result["completed"] == []
    assert len(result["candidates"]) == 1
    candidate = result["candidates"][0]
    assert candidate["matched_task_id"] == "tsk_rank"
    assert candidate["decision"] == "needs-review"
    assert candidate["match_type"] == "issue-number-fallback"
    assert candidate["score"] == 0.6
    assert "tsk_rank" in env["work"].read_text()


def test_flag_on_owner_env_unset_matching_closure_stays_candidate(env, monkeypatch):
    _enable_auto(monkeypatch)
    monkeypatch.delenv("TASK_TRACKER_GITHUB_OWNER", raising=False)
    _stub_subprocesses(
        monkeypatch,
        gh_payload=[_pr("Add social updates to World Cup skill", closes=101)],
    )

    result = _run_auto()

    assert result["completed"] == []
    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["matched_task_id"] == "tsk_abc123"
    assert "tsk_abc123" in env["work"].read_text()


def test_flag_on_pr_from_non_owner_author_does_not_auto_complete(env, monkeypatch):
    _enable_auto(monkeypatch)
    _stub_subprocesses(
        monkeypatch,
        gh_payload=[_pr("Ship social updates", closes=101, author="dependabot")],
    )

    result = _run_auto()

    assert result["completed"] == []
    assert len(result["candidates"]) == 1
    assert "tsk_abc123" in env["work"].read_text()


def test_flag_on_closed_not_merged_pr_does_not_auto_complete(env, monkeypatch):
    _enable_auto(monkeypatch)
    _stub_subprocesses(
        monkeypatch,
        gh_payload=[_pr("Ship social updates", closes=101, state="CLOSED", merged_at=None)],
    )

    result = _run_auto()

    assert result["completed"] == []
    assert len(result["candidates"]) == 1
    assert "tsk_abc123" in env["work"].read_text()


def test_auto_completed_task_removes_same_task_email_candidate(env, monkeypatch):
    _enable_auto(monkeypatch)
    _stub_subprocesses(
        monkeypatch,
        gh_payload=[_pr("Ship social updates", closes=101)],
        gmail_payload=_gmail_subject("Add social updates to World Cup skill"),
    )

    result = _run_auto()

    assert [item["task_id"] for item in result["completed"] if item["ok"]] == ["tsk_abc123"]
    assert result["candidates"] == []
    assert "tsk_abc123" not in env["work"].read_text()


def test_dry_run_does_not_append_harvest_started_or_complete(env, monkeypatch):
    _enable_auto(monkeypatch)
    _stub_subprocesses(
        monkeypatch,
        gh_payload=[_pr("Ship social updates", closes=101)],
    )

    result = _run_auto(dry_run=True)

    assert result["dry_run"] is True
    assert result["completed"] == []
    assert len(result["candidates"]) == 1
    assert _events(env["ledger"]) == []
    assert "tsk_abc123" in env["work"].read_text()


def test_seen_auto_evidence_does_not_recomplete_manually_revived_task(env, monkeypatch):
    _enable_auto(monkeypatch)
    _stub_subprocesses(
        monkeypatch,
        gh_payload=[_pr("Ship social updates", closes=101)],
    )

    first = _run_auto()
    revived_line = "- [ ] **Add social updates to World Cup skill** https://github.com/kesslerio/world-cup-skill/issues/101 task_id::tsk_abc123 area:: Delivery\n"
    env["work"].write_text("# Work\n\n## Q1\n" + revived_line)
    second = _run_auto()

    assert first["completed"][0]["ok"] is True
    assert second["completed"] == []
    assert second["candidates"] == []
    assert "tsk_abc123" in env["work"].read_text()


def test_flag_on_pr_title_only_match_stays_candidate(env, monkeypatch):
    _enable_auto(monkeypatch)
    _stub_subprocesses(monkeypatch, gh_payload=[_pr("Add social updates to World Cup skill")])

    result = _run_auto()

    assert result["completed"] == []
    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["match_type"] == "normalized-title"
    assert "tsk_abc123" in env["work"].read_text()


def test_flag_on_calendar_past_accepted_exact_title_external_organizer_stays_candidate(env, monkeypatch):
    _enable_auto(monkeypatch)
    env["work"].write_text("""# Work

## Q1
- [ ] **Planning review** task_id::tsk_cal area:: Ops
""")
    _configure_calendar(
        monkeypatch,
        [_event("evt_1", "Planning review", "2026-06-23T09:00:00-07:00")],
    )

    result = _run_auto()

    assert result["completed"] == []
    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["matched_task_id"] == "tsk_cal"
    assert "tsk_cal" in env["work"].read_text()


def test_flag_on_calendar_past_exact_title_organized_by_self_auto_completes(env, monkeypatch):
    _enable_auto(monkeypatch)
    env["work"].write_text("""# Work

## Q1
- [ ] **Planning review** task_id::tsk_cal area:: Ops
""")
    _configure_calendar(
        monkeypatch,
        [_event("evt_1", "Planning review", "2026-06-23T09:00:00-07:00", organizer_self=True)],
    )

    result = _run_auto()

    assert result["completed"][0]["task_id"] == "tsk_cal"
    assert result["completed"][0]["source"] == "calendar"
    assert result["candidates"] == []
    assert "tsk_cal" not in env["work"].read_text()
    evidence = next(event for event in _events(env["ledger"]) if event["event_type"] == "evidence_link")
    assert evidence["source"] == "calendar"
    assert evidence["evidence"]["source_type"] == "calendar"
    assert evidence["evidence"]["match_type"] == "normalized-title"


def test_flag_on_calendar_declined_or_future_event_does_not_auto_complete(env, monkeypatch):
    _enable_auto(monkeypatch)
    env["work"].write_text("""# Work

## Q1
- [ ] **Planning review** task_id::tsk_cal area:: Ops
- [ ] **Customer call** task_id::tsk_future area:: Ops
""")
    _configure_calendar(
        monkeypatch,
        [
            _event("evt_declined", "Planning review", "2026-06-23T09:00:00-07:00", response="declined"),
            _event("evt_future", "Customer call", "2026-06-23T15:00:00-07:00"),
        ],
    )

    result = _run_auto()

    assert result["completed"] == []
    assert [candidate["matched_task_id"] for candidate in result["candidates"]] == ["tsk_future"]
    assert "tsk_cal" in env["work"].read_text()
    assert "tsk_future" in env["work"].read_text()


def test_flag_on_recurring_exact_match_stays_candidate(env, monkeypatch):
    _enable_auto(monkeypatch)
    env["work"].write_text("""# Work

## Q1
- [ ] **Send weekly update** https://github.com/kesslerio/world-cup-skill/issues/303 task_id::tsk_weekly recur::weekly area:: Ops
""")
    _stub_subprocesses(monkeypatch, gh_payload=[_pr("Send weekly update", closes=303)])

    result = _run_auto()

    assert result["completed"] == []
    assert len(result["candidates"]) == 1
    assert "tsk_weekly" in env["work"].read_text()


def test_flag_on_duplicate_title_collision_stays_candidate(env, monkeypatch):
    _enable_auto(monkeypatch)
    env["work"].write_text("""# Work

## Q1
- [ ] **Planning review** task_id::tsk_one area:: Ops
- [ ] **Planning review** task_id::tsk_two area:: Ops
""")
    _configure_calendar(
        monkeypatch,
        [_event("evt_1", "Planning review", "2026-06-23T09:00:00-07:00", organizer_self=True)],
    )

    result = _run_auto()

    assert result["completed"] == []
    assert len(result["candidates"]) == 1
    assert "tsk_one" in env["work"].read_text()
    assert "tsk_two" in env["work"].read_text()


def test_auto_complete_already_done_noop_does_not_abort_other_completion(env, monkeypatch):
    _enable_auto(monkeypatch)
    env["work"].write_text("""# Work

## Q1
- [ ] **Already done** https://github.com/kesslerio/world-cup-skill/issues/401 task_id::tsk_done area:: Ops
- [ ] **Still active** https://github.com/kesslerio/world-cup-skill/issues/402 task_id::tsk_active area:: Ops
""")
    matches = [
        {
            "source_type": "pr",
            "url": "https://example.test/pr/1",
            "state": "MERGED",
            "merged_at": "2026-06-23T18:00:00Z",
            "author_login": "niemand",
            "closes_issues": ["kesslerio/world-cup-skill#401"],
        },
        {
            "source_type": "pr",
            "url": "https://example.test/pr/2",
            "state": "MERGED",
            "merged_at": "2026-06-23T18:00:00Z",
            "author_login": "niemand",
            "closes_issues": ["kesslerio/world-cup-skill#402"],
        },
    ]

    def fake_complete(task_id, **_kwargs):
        if task_id == "tsk_done":
            return {
                "ok": False,
                "noop": True,
                "reason": "already-done",
                "error": {"code": "canonical-id-resolution-failed"},
            }
        return {"ok": True, "completion_id": "evt-active"}

    monkeypatch.setattr(harvest_auto, "complete_by_id", fake_complete)
    result = harvest_auto.auto_complete(matches, personal=False)

    by_id = {item["task_id"]: item for item in result}
    assert by_id["tsk_done"]["ok"] is False
    assert by_id["tsk_done"]["noop"] is True
    assert by_id["tsk_active"]["ok"] is True


def test_auto_eligible_completion_failure_falls_back_to_candidate(env, monkeypatch):
    _enable_auto(monkeypatch)
    _stub_subprocesses(
        monkeypatch,
        gh_payload=[_pr("Ship social updates", closes=101)],
    )

    def fail_complete(task_id, **_kwargs):
        return {"ok": False, "error": {"code": "forced-failure"}, "task_id": task_id}

    monkeypatch.setattr(harvest_auto, "complete_by_id", fail_complete)

    result = _run_auto()

    assert result["completed"] == [{
        "task_id": "tsk_abc123",
        "ok": False,
        "source": "merged_pr",
        "url": "https://github.com/kesslerio/world-cup-skill/pull/7",
        "error": {"code": "forced-failure"},
    }]
    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["matched_task_id"] == "tsk_abc123"
    assert "tsk_abc123" in env["work"].read_text()


def test_auto_complete_refuses_handcrafted_ineligible_match(env, monkeypatch):
    _enable_auto(monkeypatch)
    match = {
        "matched_task_id": "tsk_abc123",
        "source_type": "pr",
        "url": "https://example.test/pr/99",
        "state": "MERGED",
        "merged_at": "2026-06-23T18:00:00Z",
        "author_login": "niemand",
    }

    result = harvest_auto.auto_complete([match], personal=False)

    assert result == [{
        "task_id": "tsk_abc123",
        "ok": False,
        "source": "merged_pr",
        "url": "https://example.test/pr/99",
        "reason": "ineligible",
        "skipped": True,
    }]
    assert "tsk_abc123" in env["work"].read_text()


def test_gog_unavailable_calendar_noops_while_pr_path_still_completes(env, monkeypatch):
    _enable_auto(monkeypatch)
    monkeypatch.setenv(
        "STANDUP_CALENDARS",
        json.dumps({"work": {"cmd": "gog", "calendar_id": "cal_fixture", "account": "owner@example.test"}}),
    )
    _stub_subprocesses(
        monkeypatch,
        gh_payload=[_pr("Add social updates to World Cup skill", closes=101)],
        calendar_exc=FileNotFoundError("gog"),
    )

    result = _run_auto()

    assert result["completed"][0]["task_id"] == "tsk_abc123"
    assert result["source_error"] is True
    assert "tsk_abc123" not in env["work"].read_text()


def test_eod_excludes_auto_completed_task_from_rendered_buttons(env, monkeypatch):
    _enable_auto(monkeypatch)
    _stub_subprocesses(
        monkeypatch,
        gh_payload=[_pr("Add social updates to World Cup skill", closes=101)],
    )

    payload = eod_ritual.build_confirm_step()

    assert payload["completed_count"] == 1
    assert payload["detection_count"] == 0
    assert payload["detections"] == []
    assert "tt:appr:tsk_abc123" not in json.dumps(payload)
    assert "tsk_abc123" not in env["work"].read_text()
