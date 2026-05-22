import json
import os
import subprocess
from datetime import date, timedelta
from pathlib import Path


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
