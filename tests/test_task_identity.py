import json
import os
import subprocess


def _env(tmp_path, work):
    env = os.environ.copy()
    env["TASK_TRACKER_WORK_FILE"] = str(work)
    env["TASK_TRACKER_DAILY_NOTES_DIR"] = str(tmp_path)
    env["TASK_TRACKER_LEDGER_FILE"] = str(tmp_path / "events.jsonl")
    env["STANDUP_CALENDARS"] = "{}"
    return env


def test_identity_audit_reports_missing_ids_without_writing(tmp_path):
    work = tmp_path / "Work Tasks.md"
    original = """# Work

## 🔴 Q1
- [ ] **Ship milestone** area:: Delivery
- [ ] **Existing ID** task_id::tsk_existing area:: Ops
"""
    work.write_text(original)

    proc = subprocess.run(
        ["python3", "scripts/tasks.py", "identity-audit"],
        capture_output=True,
        text=True,
        check=False,
        env=_env(tmp_path, work),
    )

    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert payload["command"] == "identity-audit"
    assert payload["audit"]["totals"]["missing_task_ids"] == 1
    assert payload["audit"]["proposed_repairs"][0]["title"] == "Ship milestone"
    assert work.read_text() == original


def test_identity_audit_reports_duplicate_task_ids(tmp_path):
    work = tmp_path / "Work Tasks.md"
    work.write_text("""# Work

## 🔴 Q1
- [ ] **First** task_id::tsk_same
- [ ] **Second** task_id::tsk_same
""")

    proc = subprocess.run(
        ["python3", "scripts/tasks.py", "identity-audit"],
        capture_output=True,
        text=True,
        check=False,
        env=_env(tmp_path, work),
    )

    payload = json.loads(proc.stdout)
    assert payload["audit"]["blocking_invariants"] == ["duplicate-task-id"]
    assert payload["audit"]["duplicate_task_ids"][0]["task_id"] == "tsk_same"


def test_standup_summary_prefers_task_id_over_legacy_id(tmp_path):
    work = tmp_path / "Work Tasks.md"
    work.write_text("""# Work

## 🔴 Q1
- [ ] **Ship milestone** id::legacy-1 task_id::tsk_real area:: Delivery
""")

    proc = subprocess.run(
        ["python3", "scripts/tasks.py", "standup-summary"],
        capture_output=True,
        text=True,
        check=False,
        env=_env(tmp_path, work),
    )

    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert payload["dos"][0]["task_id"] == "tsk_real"
