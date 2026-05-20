import json
import os
import subprocess


def _env(tmp_path, work):
    env = os.environ.copy()
    env["TASK_TRACKER_WORK_FILE"] = str(work)
    env["TASK_TRACKER_DAILY_NOTES_DIR"] = str(tmp_path / "daily")
    env["TASK_TRACKER_LEDGER_FILE"] = str(tmp_path / "events.jsonl")
    env["STANDUP_CALENDARS"] = "{}"
    return env


def test_done_by_canonical_id_completes_one_task_and_writes_ledger(tmp_path):
    work = tmp_path / "Work Tasks.md"
    work.write_text("""# Work

## 🔴 Q1
- [ ] **Ship milestone** task_id::tsk_ship area:: Delivery
- [ ] **Other task** task_id::tsk_other area:: Ops
""")
    env = _env(tmp_path, work)

    proc = subprocess.run(
        ["python3", "scripts/tasks.py", "done", "tsk_ship"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert payload["ok"] is True
    assert "Ship milestone" not in work.read_text()
    assert "Other task" in work.read_text()
    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert events[0]["event_type"] == "state_transition"
    assert events[0]["next_state"] == "done"
    assert events[0]["source"] == "user_command"


def test_done_by_title_is_blocked_without_writes(tmp_path):
    work = tmp_path / "Work Tasks.md"
    original = """# Work

## 🔴 Q1
- [ ] **Ship milestone** task_id::tsk_ship area:: Delivery
"""
    work.write_text(original)
    env = _env(tmp_path, work)

    proc = subprocess.run(
        ["python3", "scripts/tasks.py", "done", "Ship milestone"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert proc.returncode == 2
    payload = json.loads(proc.stdout)
    assert payload["error"]["code"] == "unsafe-title-mutation-blocked"
    assert work.read_text() == original
    assert not (tmp_path / "events.jsonl").exists()


def test_done_id_survives_title_edit_and_reorder(tmp_path):
    work = tmp_path / "Work Tasks.md"
    work.write_text("""# Work

## 🔴 Q1
- [ ] **Renamed thing** task_id::tsk_stable area:: Delivery
- [ ] **First thing** task_id::tsk_first area:: Ops
""")
    env = _env(tmp_path, work)

    proc = subprocess.run(
        ["python3", "scripts/tasks.py", "done", "tsk_stable"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert proc.returncode == 0
    assert "Renamed thing" not in work.read_text()
    assert "First thing" in work.read_text()
