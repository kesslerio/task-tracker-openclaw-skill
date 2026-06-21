import json
import os
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import eod_review
import standup


def _write_work_file(tmp_path: Path, content: str) -> Path:
    work = tmp_path / "Weekly TODOs.md"
    work.write_text(content)
    return work


def _env(tmp_path: Path, work: Path) -> dict:
    env = os.environ.copy()
    env["TASK_TRACKER_WORK_FILE"] = str(work)
    env["TASK_TRACKER_DAILY_NOTES_DIR"] = str(tmp_path / "daily")
    env["TASK_TRACKER_DONE_LOG_DIR"] = str(tmp_path / "daily")
    env["TASK_TRACKER_LEDGER_FILE"] = str(tmp_path / "events.jsonl")
    env["STANDUP_CALENDARS"] = "{}"
    return env


def _run(args: list[str], env: dict, *, input_text: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["python3", "scripts/tasks.py", *args],
        input=input_text,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def _payload(proc: subprocess.CompletedProcess) -> dict:
    assert "Traceback" not in proc.stdout
    assert "Traceback" not in proc.stderr
    return json.loads(proc.stdout)


def test_task_audit_reports_duplicate_missing_overdue_and_backlog_without_mutation(tmp_path):
    old_due = (date.today() - timedelta(days=20)).isoformat()
    old_created = (date.today() - timedelta(days=45)).isoformat()
    work = _write_work_file(
        tmp_path,
        f"""# Weekly TODOs

## 🔴 Q1
- [ ] **Duplicate title** task_id::tsk_one area:: Delivery
- [ ] **Duplicate title** area:: Platform
- [ ] **Overdue launch task** task_id::tsk_overdue 🗓️{old_due} area:: Delivery

## 🅿️ Parking Lot
- [ ] **Old backlog idea** task_id::tsk_backlog created::{old_created} #Dev #low
""",
    )
    original = work.read_text()
    env = _env(tmp_path, work)

    proc = _run(["task-audit", "--stale-days", "14", "--candidate-days", "7"], env)
    payload = _payload(proc)
    codes = {finding["code"] for finding in payload["findings"]}

    assert proc.returncode == 0
    assert payload["schema_version"] == "v1"
    assert payload["command"] == "task-audit"
    assert "duplicate-title" in codes
    assert "missing-task-id" in codes
    assert "overdue-task" in codes
    assert "stale-active-task" in codes
    assert "stale-backlog-item" in codes
    assert work.read_text() == original


def test_invalid_task_audit_env_defaults_do_not_break_other_commands(tmp_path):
    work = _write_work_file(
        tmp_path,
        """# Weekly TODOs

## 🔴 Q1
- [ ] **Ship alpha milestone** task_id::tsk_ship area:: Delivery
""",
    )
    env = _env(tmp_path, work)
    env["TASK_AUDIT_STALE_DAYS"] = "not-a-number"
    env["TASK_AUDIT_CANDIDATE_DAYS"] = "also-bad"

    listed = _run(["list"], env)
    audit = _run(["task-audit"], env)
    payload = _payload(audit)

    assert listed.returncode == 0
    assert audit.returncode == 0
    assert payload["thresholds"]["stale_days"] == 14
    assert payload["thresholds"]["candidate_days"] == 7


def test_task_audit_reports_effective_default_backlog_cap(tmp_path):
    work = _write_work_file(
        tmp_path,
        """# Weekly TODOs

## 🔴 Q1
- [ ] **Ship alpha milestone** task_id::tsk_ship area:: Delivery

## 🅿️ Parking Lot
- [ ] **Idea one** task_id::tsk_idea created::2026-01-01 #Dev #low
""",
    )
    env = _env(tmp_path, work)
    env["PARKING_LOT_CAP"] = "1"

    proc = _run(["task-audit"], env)
    payload = _payload(proc)

    assert proc.returncode == 0
    assert payload["thresholds"]["backlog_cap"] == 1
    assert "backlog-cap-reached" in {finding["code"] for finding in payload["findings"]}


def test_task_audit_degrades_on_invalid_parking_lot_env(tmp_path):
    work = _write_work_file(
        tmp_path,
        """# Weekly TODOs

## 🔴 Q1
- [ ] **Ship alpha milestone** task_id::tsk_ship area:: Delivery

## 🅿️ Parking Lot
- [ ] **Idea one** task_id::tsk_idea created::2026-01-01 #Dev #low
""",
    )
    env = _env(tmp_path, work)
    env["PARKING_LOT_CAP"] = "bad-cap"

    proc = _run(["task-audit"], env)
    payload = _payload(proc)

    assert proc.returncode == 0
    assert "backlog-unavailable" in {finding["code"] for finding in payload["findings"]}


def test_standup_and_eod_render_unavailable_audit_as_error(monkeypatch):
    unavailable = {
        "available": False,
        "review_required": True,
        "error": {"code": "io-error", "message": "cannot read board"},
    }

    monkeypatch.setattr(standup, "task_audit_summary", lambda limit=3: unavailable)
    monkeypatch.setattr(standup, "candidate_review_summary", lambda: {})
    monkeypatch.setattr(standup, "get_calendar_events", lambda trigger="calendar_fetch": {})
    standup_text = standup.generate_standup(
        date_str="2026-05-22",
        tasks_data={"q1": [], "q2": [], "q3": [], "team": [], "done": [], "due_today": [], "all": []},
    )
    eod_text = eod_review.format_markdown(
        {
            "weekday": "Friday",
            "date": "2026-05-22",
            "source": "test",
            "done": [],
            "not_done": [],
            "tomorrows_top3": [],
            "completion_candidates": {},
            "task_audit": unavailable,
        }
    )
    eod_telegram = eod_review.format_telegram(
        {
            "weekday": "Friday",
            "date": "2026-05-22",
            "done": [],
            "not_done": [],
            "tomorrows_top3": [],
            "completion_candidates": {},
            "task_audit": unavailable,
        }
    )

    assert "unavailable (io-error)" in standup_text
    assert "Audit unavailable: io-error." in eod_text
    assert "Unavailable: io-error" in eod_telegram


def test_task_audit_reports_stale_candidates_and_does_not_write_ledger(tmp_path):
    work = _write_work_file(
        tmp_path,
        """# Weekly TODOs

## 🔴 Q1
- [ ] **Ship alpha milestone** task_id::tsk_ship area:: Delivery
""",
    )
    env = _env(tmp_path, work)
    scan = _run(["completion-candidates", "scan"], env, input_text="- Ship alpha milestone task_id::tsk_ship\n")
    scan_payload = _payload(scan)
    assert scan.returncode == 0
    assert scan_payload["created"]

    ledger = tmp_path / "events.jsonl"
    event = json.loads(ledger.read_text().strip())
    event["timestamp"] = (date.today() - timedelta(days=10)).isoformat()
    ledger.write_text(json.dumps(event) + "\n")
    before = ledger.read_text()

    proc = _run(["task-audit", "--candidate-days", "7"], env)
    payload = _payload(proc)

    assert proc.returncode == 0
    assert "stale-completion-candidate" in {finding["code"] for finding in payload["findings"]}
    assert ledger.read_text() == before


def test_task_audit_degrades_on_malformed_ledger_but_keeps_identity_findings(tmp_path):
    work = _write_work_file(
        tmp_path,
        """# Weekly TODOs

## 🔴 Q1
- [ ] **Missing id task** area:: Delivery
""",
    )
    env = _env(tmp_path, work)
    (tmp_path / "events.jsonl").write_text("{bad-json\n")

    proc = _run(["task-audit"], env)
    payload = _payload(proc)
    codes = {finding["code"] for finding in payload["findings"]}

    assert proc.returncode == 0
    assert "malformed-ledger" in codes
    assert "missing-task-id" in codes


def test_task_audit_degrades_on_unreadable_candidate_ledger(tmp_path):
    work = _write_work_file(
        tmp_path,
        """# Weekly TODOs

## 🔴 Q1
- [ ] **Missing id task** area:: Delivery
""",
    )
    env = _env(tmp_path, work)
    ledger_dir = tmp_path / "events-as-dir"
    ledger_dir.mkdir()
    env["TASK_TRACKER_LEDGER_FILE"] = str(ledger_dir)

    proc = _run(["task-audit"], env)
    payload = _payload(proc)
    codes = {finding["code"] for finding in payload["findings"]}

    assert proc.returncode == 0
    assert "candidate-ledger-unavailable" in codes
    assert "missing-task-id" in codes


def test_task_audit_excludes_objective_headers_from_actionable_identity_findings(tmp_path):
    work = _write_work_file(
        tmp_path,
        """# Weekly TODOs

## Objectives
- [ ] Launch hiring plan #HR #high
  - [ ] Post on LinkedIn task_id::tsk_post_linkedin
- [ ] Close Fizzi deal #Sales #urgent
  - [ ] Send follow-up proposal
""",
    )
    env = _env(tmp_path, work)

    proc = _run(["task-audit"], env)
    payload = _payload(proc)
    missing = [finding for finding in payload["findings"] if finding["code"] == "missing-task-id"]
    missing_titles = {
        task["title"]
        for finding in missing
        for task in finding.get("tasks", [])
    }

    assert proc.returncode == 0
    assert payload["totals"]["active_tasks"] == 2
    assert "Launch hiring plan" not in missing_titles
    assert "Close Fizzi deal" not in missing_titles
    assert "Send follow-up proposal" in missing_titles


def test_task_audit_summary_uses_threshold_env_overrides(tmp_path):
    old_due = (date.today() - timedelta(days=20)).isoformat()
    work = _write_work_file(
        tmp_path,
        f"""# Weekly TODOs

## 🔴 Q1
- [ ] **Overdue launch task** task_id::tsk_overdue 🗓️{old_due} area:: Delivery
""",
    )
    env = _env(tmp_path, work)
    env["TASK_AUDIT_STALE_DAYS"] = "30"
    env["TASK_AUDIT_CANDIDATE_DAYS"] = "11"

    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json, sys; "
                "sys.path.insert(0, 'scripts'); "
                "import task_audit; "
                "print(json.dumps(task_audit.task_audit_summary(limit=10)))"
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    summary = json.loads(proc.stdout)
    codes = {item["code"] for item in summary["items"]}

    assert proc.returncode == 0
    assert summary["available"] is True
    assert "overdue-task" in codes
    assert "stale-active-task" not in codes


def test_task_audit_negative_summary_limit_does_not_slice_from_tail(tmp_path):
    work = _write_work_file(
        tmp_path,
        """# Weekly TODOs

## 🔴 Q1
- [ ] **Missing id task** area:: Delivery
""",
    )
    env = _env(tmp_path, work)

    proc = _run(["task-audit", "--limit", "-1"], env)
    payload = _payload(proc)

    assert proc.returncode == 0
    assert payload["summary"]["total"] >= 1
    assert payload["summary"]["items"] == []
    assert payload["summary"]["overflow"] == payload["summary"]["total"]


def test_task_audit_does_not_flag_future_snoozed_candidate_stale(tmp_path):
    work = _write_work_file(
        tmp_path,
        """# Weekly TODOs

## 🔴 Q1
- [ ] **Ship alpha milestone** task_id::tsk_ship area:: Delivery
""",
    )
    env = _env(tmp_path, work)
    scan = _run(
        ["completion-candidates", "scan"],
        env,
        input_text="- Ship alpha milestone task_id::tsk_ship\n",
    )
    candidate_id = _payload(scan)["created"][0]["candidate_id"]
    future = (date.today() + timedelta(days=7)).isoformat()
    snooze = _run(["completion-candidates", "snooze", candidate_id, "--until", future], env)
    assert snooze.returncode == 0

    ledger = tmp_path / "events.jsonl"
    events = [json.loads(line) for line in ledger.read_text().splitlines()]
    events[0]["timestamp"] = (date.today() - timedelta(days=30)).isoformat()
    ledger.write_text("\n".join(json.dumps(event) for event in events) + "\n")

    proc = _run(["task-audit", "--candidate-days", "7"], env)
    codes = {finding["code"] for finding in _payload(proc)["findings"]}

    assert proc.returncode == 0
    assert "stale-completion-candidate" not in codes
    assert "candidate-snooze-expired" not in codes


def test_task_audit_personal_uses_personal_files_and_ledger(tmp_path):
    work = _write_work_file(
        tmp_path,
        """# Work

## 🔴 Q1
- [ ] **Work task** task_id::tsk_work
""",
    )
    personal = tmp_path / "Personal Tasks.md"
    personal.write_text(
        """# Personal

## 🔴 Q1
- [ ] **Personal task** area:: Home
"""
    )
    env = _env(tmp_path, work)
    env["TASK_TRACKER_PERSONAL_FILE"] = str(personal)

    proc = _run(["--personal", "task-audit"], env)
    payload = _payload(proc)

    assert proc.returncode == 0
    assert payload["personal"] is True
    assert payload["tasks_file"] == str(personal)
    assert payload["totals"]["active_tasks"] == 1
    assert "missing-task-id" in {finding["code"] for finding in payload["findings"]}
