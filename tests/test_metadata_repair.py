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


def test_identity_repair_aborts_when_ledger_unwritable(tmp_path):
    work = tmp_path / "Work Tasks.md"
    original = """# Work

## 🔴 Q1
- [ ] **Ship milestone** area:: Delivery
"""
    work.write_text(original)
    ledger_dir = tmp_path / "ledger-is-dir"
    ledger_dir.mkdir()
    env = _env(tmp_path, work)
    env["TASK_TRACKER_LEDGER_FILE"] = str(ledger_dir)

    proc = subprocess.run(
        ["python3", "scripts/tasks.py", "identity-repair", "--apply"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert proc.returncode == 2
    payload = json.loads(proc.stdout)
    assert "ledger-unwritable" in payload["blocking_invariants"]
    assert work.read_text() == original


def test_personal_identity_repair_uses_personal_sidecar_by_default(tmp_path):
    work = tmp_path / "Work Tasks.md"
    personal = tmp_path / "Personal Tasks.md"
    work.write_text("# Work\n\n## 🔴 Q1\n- [ ] **Work task** area:: Ops\n")
    personal.write_text("# Personal\n\n## 🔴 Q1\n- [ ] **Personal task** area:: Home\n")
    env = os.environ.copy()
    env["TASK_TRACKER_WORK_FILE"] = str(work)
    env["TASK_TRACKER_PERSONAL_FILE"] = str(personal)

    proc = subprocess.run(
        ["python3", "scripts/tasks.py", "--personal", "identity-repair", "--apply"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert proc.returncode == 0
    assert "task_id::" in personal.read_text()
    assert personal.with_suffix(".md.events.jsonl").exists()
    assert not work.with_suffix(".md.events.jsonl").exists()
