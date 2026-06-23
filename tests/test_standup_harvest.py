import json
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import cos_health
import error_envelope
import harvest_ledger
import harvest_state
import standup
import standup_harvest
import utils


WORK_BOARD = """# Work

## 🔴 Q1
- [ ] **Investigate payroll sync** https://github.com/acme/app/issues/42 task_id::tsk_exact area:: Ops
- [ ] **Build revenue attribution dashboard** task_id::tsk_fuzzy area:: Product
"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    work = tmp_path / "Work Tasks.md"
    work.write_text(WORK_BOARD)
    monkeypatch.setenv("TASK_MGMT_STATE_DIR", str(state_dir))
    monkeypatch.setenv("TASK_TRACKER_WORK_FILE", str(work))
    monkeypatch.setenv("TASK_TRACKER_LEDGER_FILE", str(state_dir / "events.jsonl"))
    monkeypatch.setenv("TASK_TRACKER_ERROR_LOG", str(state_dir / "errors.jsonl"))
    monkeypatch.setattr(utils, "OBSIDIAN_WORK", work)
    monkeypatch.delenv("STANDUP_CALENDARS", raising=False)
    return {"state_dir": state_dir, "work": work}


def _github_record(
    title: str,
    *,
    provider_id: str = "acme/app#42",
    provider_state: str = "merged:sha-1:merged",
    occurred_at: str = "2026-06-23T10:00:00-07:00",
) -> dict:
    return {
        "source_type": "pr",
        "match_title": title,
        "title": f"{title} [{provider_id}]",
        "url": "https://github.com/acme/app/pull/42",
        "provider_id": provider_id,
        "provider_state": provider_state,
        "occurred_at": occurred_at,
    }


def _gmail_record(title: str) -> dict:
    return {
        "source_type": "email",
        "match_title": title,
        "title": title,
        "url": None,
        "provider_id": "thread-1/message-1",
        "provider_state": "history-1",
        "occurred_at": "2026-06-23T13:00:00-07:00",
    }


def _stub_adapters(monkeypatch, *, github=None, gmail=None, github_failed=False, gmail_failed=False):
    def gh(_since, *, trigger, query_start=None, query_end=None):
        return [dict(item) for item in (github or [])], github_failed

    def gm(_since, *, trigger, query_start=None, query_end=None):
        return [dict(item) for item in (gmail or [])], gmail_failed

    monkeypatch.setattr(harvest_ledger, "harvest_github", gh)
    monkeypatch.setattr(harvest_ledger, "harvest_gmail", gm)


def test_exact_issue_reference_auto_associates_candidate(env, monkeypatch):
    _stub_adapters(monkeypatch, github=[_github_record("Fix payroll sync #42")])

    result = standup_harvest.harvest(target_date=date(2026, 6, 23), trigger="test")

    candidate = result["evidence_candidates"][0]
    assert candidate["source"] == "github"
    assert candidate["kind"] == "activity"
    assert candidate["auto_associated"] is True
    assert candidate["matched_task_id"] == "tsk_exact"
    assert candidate["association_status"] == "auto-associated"


def test_fuzzy_only_match_is_review_candidate_not_auto_done(env, monkeypatch):
    _stub_adapters(monkeypatch, github=[_github_record("Build revenue attribution dashboards")])

    result = standup_harvest.harvest(target_date=date(2026, 6, 23), trigger="test")

    candidate = result["evidence_candidates"][0]
    assert candidate["auto_associated"] is False
    assert candidate["decision"] == "needs-review"
    assert candidate["matched_task_id"] is None
    assert candidate["suggested_task_id"] == "tsk_fuzzy"


def test_direct_commit_without_pr_appears_as_activity_candidate(env, monkeypatch):
    class Completed:
        returncode = 0
        stderr = ""

        def __init__(self, stdout):
            self.stdout = stdout

    def fake_run(cmd, **_kwargs):
        joined = " ".join(cmd)
        if "search prs" in joined:
            return Completed("[]")
        if "search commits" in joined:
            return Completed(
                json.dumps(
                    [
                        {
                            "sha": "abc123def456",
                            "repository": {"nameWithOwner": "acme/app"},
                            "url": "https://github.com/acme/app/commit/abc123def456",
                            "commit": {
                                "message": "Fix payroll sync #42\n\nbody ignored",
                                "committer": {"date": "2026-06-23T17:00:00Z"},
                            },
                        }
                    ]
                )
            )
        if cmd[0] == "gog":
            return Completed(json.dumps({"threads": []}))
        raise AssertionError(cmd)

    monkeypatch.setattr(harvest_ledger.subprocess, "run", fake_run)

    result = standup_harvest.harvest(target_date=date(2026, 6, 23), trigger="test")

    candidate = result["evidence_candidates"][0]
    assert candidate["source"] == "github"
    assert candidate["provider_id"] == "acme/app@abc123def456"
    assert candidate["provider_state"] == "abc123def456"
    assert candidate["title"].startswith("Fix payroll sync #42")


def test_sent_email_appears_as_activity_candidate(env, monkeypatch):
    _stub_adapters(monkeypatch, gmail=[_gmail_record("Sent payroll sync follow-up #42")])

    result = standup_harvest.harvest(target_date=date(2026, 6, 23), trigger="test")

    candidate = result["evidence_candidates"][0]
    assert candidate["source"] == "gmail"
    assert candidate["provider_id"] == "thread-1/message-1"
    assert candidate["provider_state"] == "history-1"
    assert candidate["kind"] == "activity"


def test_failed_github_records_source_health_and_standup_still_exits_zero(env, monkeypatch):
    _stub_adapters(monkeypatch, github_failed=True)
    monkeypatch.setattr(standup, "get_calendar_events", lambda trigger="calendar_fetch": {})
    monkeypatch.setattr(standup, "candidate_review_summary", lambda: {})
    monkeypatch.setattr(standup, "task_audit_summary", lambda limit=3: {})

    rc = error_envelope.run_main(
        "standup",
        lambda: standup.generate_standup(
            date_str="2026-06-23",
            json_output=True,
            tasks_data={"done": [], "due_today": [], "q1": [], "q2": [], "q3": [], "team": []},
            capacity_records=[],
        ),
        trigger="test",
    )

    assert rc == 0
    receipt = cos_health.read_health()["standup"]["sources"]["github"]
    assert receipt["status"] == "failed"
    assert receipt["last_failure"]["error_class"] == "github_harvest_failed"


def test_adversarial_commit_text_keeps_match_alignment(env, monkeypatch):
    _stub_adapters(
        monkeypatch,
        github=[
            _github_record("✅\n**not a real done**", provider_id="acme/app@bad", provider_state="bad"),
            _github_record("Fix payroll sync #42", provider_id="acme/app@good", provider_state="good"),
        ],
    )

    result = standup_harvest.harvest(target_date=date(2026, 6, 23), trigger="test")

    assert len(result["evidence_candidates"]) == 2
    good = [c for c in result["evidence_candidates"] if c["provider_id"] == "acme/app@good"][0]
    assert good["matched_task_id"] == "tsk_exact"
    bad = [c for c in result["evidence_candidates"] if c["provider_id"] == "acme/app@bad"][0]
    assert bad["matched_task_id"] is None


def test_provider_state_change_resurfaces_same_identity(env, monkeypatch):
    state = {"provider_state": "merged:sha-1:merged"}

    def gh(_since, *, trigger, query_start=None, query_end=None):
        return [
            _github_record(
                "Fix payroll sync #42",
                provider_id="acme/app#42",
                provider_state=state["provider_state"],
            )
        ], False

    monkeypatch.setattr(harvest_ledger, "harvest_github", gh)
    monkeypatch.setattr(harvest_ledger, "harvest_gmail", lambda *a, **k: ([], False))

    first = standup_harvest.harvest(target_date=date(2026, 6, 23), trigger="test")
    second = standup_harvest.harvest(target_date=date(2026, 6, 23), trigger="test")
    state["provider_state"] = "merged:sha-2:merged"
    third = standup_harvest.harvest(target_date=date(2026, 6, 23), trigger="test")

    assert len(first["evidence_candidates"]) == 1
    assert second["evidence_candidates"] == []
    assert len(third["evidence_candidates"]) == 1
    standup_state = harvest_state.load_state(harvest_state.WINDOW_STANDUP)
    assert standup_state["seen_provider_states"][first["evidence_candidates"][0]["evidence_hash"]] == "merged:sha-2:merged"
