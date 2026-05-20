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


def test_identity_repair_dry_run_writes_nothing(tmp_path):
    work = tmp_path / "Work Tasks.md"
    original = """# Work

## 🔴 Q1
- [ ] **Ship milestone** area:: Delivery
"""
    work.write_text(original)

    proc = subprocess.run(
        ["python3", "scripts/tasks.py", "identity-repair"],
        capture_output=True,
        text=True,
        check=False,
        env=_env(tmp_path, work),
    )

    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert payload["applied"] is False
    assert payload["proposed_repairs"][0]["title"] == "Ship milestone"
    assert work.read_text() == original
    assert not (tmp_path / "events.jsonl").exists()


def test_identity_repair_apply_adds_task_id_and_ledger_event(tmp_path):
    work = tmp_path / "Work Tasks.md"
    work.write_text("""# Work

## 🔴 Q1
- [ ] **Ship milestone** area:: Delivery
""")

    proc = subprocess.run(
        ["python3", "scripts/tasks.py", "identity-repair", "--apply"],
        capture_output=True,
        text=True,
        check=False,
        env=_env(tmp_path, work),
    )

    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert payload["applied"] is True
    assert payload["changed"] == 1
    assert " task_id::tsk_" in work.read_text()
    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert events[0]["event_type"] == "metadata_repair"


def test_identity_repair_blocks_duplicate_titles_without_writes(tmp_path):
    work = tmp_path / "Work Tasks.md"
    original = """# Work

## 🔴 Q1
- [ ] **Same title** area:: Delivery
- [ ] **Same title** area:: Ops
"""
    work.write_text(original)

    proc = subprocess.run(
        ["python3", "scripts/tasks.py", "identity-repair", "--apply"],
        capture_output=True,
        text=True,
        check=False,
        env=_env(tmp_path, work),
    )

    assert proc.returncode == 2
    payload = json.loads(proc.stdout)
    assert "ambiguous-title" in payload["blocking_invariants"]
    assert work.read_text() == original
    assert not (tmp_path / "events.jsonl").exists()
