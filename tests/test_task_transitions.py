import json
import os
import subprocess
from pathlib import Path


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


def test_done_recurring_task_rolls_forward_next_due_date(tmp_path):
    work = tmp_path / "Work Tasks.md"
    work.write_text("""# Work

## 🔴 Q1
- [ ] **Send weekly update** task_id::tsk_weekly recur::weekly 🗓️2026-05-20
""")
    env = _env(tmp_path, work)

    proc = subprocess.run(
        ["python3", "scripts/tasks.py", "done", "tsk_weekly"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert proc.returncode == 0
    content = work.read_text()
    assert "Send weekly update" in content
    assert "🗓️2026-05-27" in content


def test_done_removes_indented_block_across_blank_line(tmp_path):
    work = tmp_path / "Work Tasks.md"
    work.write_text("""# Work

## 🔴 Q1
- [ ] **Ship milestone** task_id::tsk_ship
  first note

  second note
- [ ] **Other task** task_id::tsk_other
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
    content = work.read_text()
    assert "first note" not in content
    assert "second note" not in content
    assert "Other task" in content


def test_state_delegate_backlog_drop_write_destination_records(tmp_path):
    work = tmp_path / "Work Tasks.md"
    delegated = tmp_path / "Delegated.md"
    delegated.write_text("# Delegated Tasks\n\n## Active\n\n## Awaiting Follow-up\n\n## Completed\n")
    work.write_text("""# Work

## 🔴 Q1
- [ ] **Delegate me** task_id::tsk_delegate area:: Ops
- [ ] **Backlog me** task_id::tsk_backlog area:: Sales
- [ ] **Drop me** task_id::tsk_drop area:: Dev

## 🅿️ Parking Lot
""")
    env = _env(tmp_path, work)
    env["TASK_TRACKER_DELEGATION_FILE"] = str(delegated)
    env["TASK_TRACKER_ARCHIVE_DIR"] = str(tmp_path / "archive")

    delegate = subprocess.run(
        [
            "python3",
            "scripts/tasks.py",
            "state",
            "delegate",
            "tsk_delegate",
            "--to",
            "Alex",
            "--followup",
            "2026-05-27",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    backlog = subprocess.run(
        ["python3", "scripts/tasks.py", "state", "backlog", "tsk_backlog"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    drop = subprocess.run(
        ["python3", "scripts/tasks.py", "state", "drop", "tsk_drop"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert delegate.returncode == 0, delegate.stdout + delegate.stderr
    assert backlog.returncode == 0, backlog.stdout + backlog.stderr
    assert drop.returncode == 0, drop.stdout + drop.stderr
    assert "Delegate me" in delegated.read_text()
    assert "Backlog me" in work.read_text()
    archive_text = "\n".join(path.read_text() for path in (tmp_path / "archive").glob("*.md"))
    assert "Drop me" in archive_text


def test_state_pause_without_until_persists_paused_marker(tmp_path):
    work = tmp_path / "Work Tasks.md"
    work.write_text("# Work\n\n## 🔴 Q1\n- [ ] **Pause me** task_id::tsk_pause\n")
    env = _env(tmp_path, work)

    proc = subprocess.run(
        ["python3", "scripts/tasks.py", "state", "pause", "tsk_pause"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert proc.returncode == 0
    assert "paused::" in work.read_text()
