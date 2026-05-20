import json
import os
import subprocess
from datetime import datetime
from pathlib import Path


def _env(tmp_path, work):
    env = os.environ.copy()
    env["TASK_TRACKER_WORK_FILE"] = str(work)
    env["TASK_TRACKER_DAILY_NOTES_DIR"] = str(tmp_path / "daily")
    env["TASK_TRACKER_DONE_LOG_DIR"] = str(tmp_path / "daily")
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


def test_done_restricts_canonical_resolution_to_active_sections(tmp_path):
    work = tmp_path / "Work Tasks.md"
    work.write_text("""# Work

## 🔴 Q1
- [ ] **Active task** task_id::tsk_same area:: Delivery

## 🅿️ Parking Lot
- [ ] **Backlog task** task_id::tsk_same #Ops #low created::2026-01-01
""")
    env = _env(tmp_path, work)

    proc = subprocess.run(
        ["python3", "scripts/tasks.py", "done", "tsk_same"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert proc.returncode == 0
    content = work.read_text()
    assert "Active task" not in content
    assert "Backlog task" in content


def test_done_resolves_plain_task_line_canonical_id(tmp_path):
    work = tmp_path / "Work Tasks.md"
    work.write_text("""# Work

## 🔴 Q1
- [ ] Plain task task_id::tsk_plain
""")
    env = _env(tmp_path, work)

    proc = subprocess.run(
        ["python3", "scripts/tasks.py", "done", "tsk_plain"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert proc.returncode == 0
    assert "Plain task" not in work.read_text()


def test_done_aborts_before_board_write_when_ledger_unwritable(tmp_path):
    work = tmp_path / "Work Tasks.md"
    original = """# Work

## 🔴 Q1
- [ ] **Ship milestone** task_id::tsk_ship area:: Delivery
"""
    work.write_text(original)
    ledger_dir = tmp_path / "ledger-is-dir"
    ledger_dir.mkdir()
    env = _env(tmp_path, work)
    env["TASK_TRACKER_LEDGER_FILE"] = str(ledger_dir)

    proc = subprocess.run(
        ["python3", "scripts/tasks.py", "done", "tsk_ship"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert proc.returncode == 2
    payload = json.loads(proc.stdout)
    assert payload["error"]["code"] == "ledger-unwritable"
    assert work.read_text() == original


def test_done_reports_missing_tasks_file_as_json(tmp_path):
    missing = tmp_path / "Missing Tasks.md"
    env = _env(tmp_path, missing)

    proc = subprocess.run(
        ["python3", "scripts/tasks.py", "done", "tsk_missing"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert proc.returncode == 2
    payload = json.loads(proc.stdout)
    assert payload["error"]["code"] == "tasks-file-missing"


def test_done_restores_board_and_completion_log_when_ledger_append_fails(tmp_path):
    if not os.path.exists("/dev/full"):
        return
    work = tmp_path / "Work Tasks.md"
    original = """# Work

## 🔴 Q1
- [ ] **Ship milestone** task_id::tsk_ship area:: Delivery
"""
    work.write_text(original)
    env = _env(tmp_path, work)
    env["TASK_TRACKER_LEDGER_FILE"] = "/dev/full"

    proc = subprocess.run(
        ["python3", "scripts/tasks.py", "done", "tsk_ship"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert proc.returncode == 2
    payload = json.loads(proc.stdout)
    assert payload["error"]["code"] == "ledger-append-failed"
    assert work.read_text() == original
    assert not list((tmp_path / "daily").glob("*.md"))


def test_done_handles_date_named_daily_log_directory(tmp_path):
    work = tmp_path / "Work Tasks.md"
    original = """# Work

## 🔴 Q1
- [ ] **Ship milestone** task_id::tsk_ship area:: Delivery
"""
    work.write_text(original)
    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    (daily_dir / f"{datetime.now().strftime('%Y-%m-%d')}.md").mkdir()
    env = _env(tmp_path, work)

    proc = subprocess.run(
        ["python3", "scripts/tasks.py", "done", "tsk_ship"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert proc.returncode == 2
    payload = json.loads(proc.stdout)
    assert payload["error"]["code"] == "completion-log-failed"
    assert work.read_text() == original


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


def test_done_recurring_task_replaces_spaced_due_marker(tmp_path):
    work = tmp_path / "Work Tasks.md"
    work.write_text("""# Work

## 🔴 Q1
- [ ] **Send weekly update** task_id::tsk_weekly recur::weekly 🗓️ 2026-05-20
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
    assert "🗓️2026-05-27" in content
    assert "🗓️ 2026-05-20" not in content


def test_done_recurring_task_preserves_multi_word_rule(tmp_path):
    work = tmp_path / "Work Tasks.md"
    work.write_text("""# Work

## 🔴 Q1
- [ ] **Send monday update** task_id::tsk_monday recur::every monday 📅 2026-05-19
""")
    env = _env(tmp_path, work)

    proc = subprocess.run(
        ["python3", "scripts/tasks.py", "done", "tsk_monday"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert proc.returncode == 0
    content = work.read_text()
    assert "Send monday update" in content
    assert "📅 2026-05-25" in content


def test_done_recurring_task_inserts_due_before_inline_fields(tmp_path):
    work = tmp_path / "Work Tasks.md"
    work.write_text("""# Work

## 🔴 Q1
- [ ] **Send weekly update** task_id::tsk_weekly recur::weekly area:: Delivery
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
    line = next(line for line in work.read_text().splitlines() if "Send weekly update" in line)
    assert "🗓️" in line
    assert line.index("🗓️") < line.index("area::")


def test_done_by_canonical_id_logs_due_and_recur_context(tmp_path):
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
    daily_notes = list((tmp_path / "daily").glob("*.md"))
    assert daily_notes
    note = daily_notes[0].read_text()
    assert '"due": "2026-05-20"' in note
    assert '"recur": "weekly"' in note


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


def test_state_transition_aborts_when_ledger_unwritable(tmp_path):
    work = tmp_path / "Work Tasks.md"
    original = "# Work\n\n## 🔴 Q1\n- [ ] **Pause me** task_id::tsk_pause\n"
    work.write_text(original)
    ledger_dir = tmp_path / "ledger-is-dir"
    ledger_dir.mkdir()
    env = _env(tmp_path, work)
    env["TASK_TRACKER_LEDGER_FILE"] = str(ledger_dir)

    proc = subprocess.run(
        ["python3", "scripts/tasks.py", "state", "pause", "tsk_pause"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert proc.returncode == 2
    payload = json.loads(proc.stdout)
    assert payload["error"]["code"] == "ledger-unwritable"
    assert work.read_text() == original


def test_state_reports_missing_tasks_file_as_json(tmp_path):
    missing = tmp_path / "Missing Tasks.md"
    env = _env(tmp_path, missing)

    proc = subprocess.run(
        ["python3", "scripts/tasks.py", "state", "pause", "tsk_missing"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert proc.returncode == 2
    payload = json.loads(proc.stdout)
    assert payload["error"]["code"] == "tasks-file-missing"


def test_state_transition_restores_board_when_ledger_append_fails(tmp_path):
    if not os.path.exists("/dev/full"):
        return
    work = tmp_path / "Work Tasks.md"
    original = "# Work\n\n## 🔴 Q1\n- [ ] **Pause me** task_id::tsk_pause\n"
    work.write_text(original)
    env = _env(tmp_path, work)
    env["TASK_TRACKER_LEDGER_FILE"] = "/dev/full"

    proc = subprocess.run(
        ["python3", "scripts/tasks.py", "state", "pause", "tsk_pause"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert proc.returncode == 2
    payload = json.loads(proc.stdout)
    assert payload["error"]["code"] == "ledger-append-failed"
    assert work.read_text() == original


def test_state_drop_defaults_archive_next_to_tasks_file(tmp_path):
    work = tmp_path / "nested" / "Work Tasks.md"
    work.parent.mkdir()
    work.write_text("# Work\n\n## 🔴 Q1\n- [ ] **Drop me** task_id::tsk_drop area:: Dev\n")
    env = _env(tmp_path, work)
    env.pop("TASK_TRACKER_ARCHIVE_DIR", None)

    proc = subprocess.run(
        ["python3", str(Path.cwd() / "scripts" / "tasks.py"), "state", "drop", "tsk_drop"],
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
        env=env,
    )

    assert proc.returncode == 0
    archive_files = list((work.parent / "Done Archive").glob("*.md"))
    assert archive_files
    assert "Drop me" in archive_files[0].read_text()


def test_state_backlog_preserves_priority_and_sanitizes_department(tmp_path):
    work = tmp_path / "Work Tasks.md"
    work.write_text("""# Work

## 🔴 Q1
- [ ] **Backlog me** task_id::tsk_backlog area:: Customer Success #high

## 🅿️ Parking Lot
""")
    env = _env(tmp_path, work)

    proc = subprocess.run(
        ["python3", "scripts/tasks.py", "state", "backlog", "tsk_backlog"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert proc.returncode == 0
    content = work.read_text()
    assert "#high" in content
    assert "#CustomerSuccess" in content
    assert "#Customer Success" not in content
    assert "task_id::tsk_backlog" in content


def test_state_backlog_priority_inference_is_deterministic(tmp_path):
    work = tmp_path / "Work Tasks.md"
    work.write_text("""# Work

## 🔴 Q1
- [ ] **Backlog me** task_id::tsk_backlog area:: Ops #urgent #high

## 🅿️ Parking Lot
""")
    env = _env(tmp_path, work)

    proc = subprocess.run(
        ["python3", "scripts/tasks.py", "state", "backlog", "tsk_backlog"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert proc.returncode == 0
    content = work.read_text()
    assert "#urgent" in content


def test_state_delegate_sanitizes_department(tmp_path):
    work = tmp_path / "Work Tasks.md"
    delegated = tmp_path / "Delegated.md"
    delegated.write_text("# Delegated Tasks\n\n## Active\n\n## Awaiting Follow-up\n\n## Completed\n")
    work.write_text("# Work\n\n## 🔴 Q1\n- [ ] **Delegate me** task_id::tsk_delegate area:: Customer Success\n")
    env = _env(tmp_path, work)
    env["TASK_TRACKER_DELEGATION_FILE"] = str(delegated)

    proc = subprocess.run(
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

    assert proc.returncode == 0
    content = delegated.read_text()
    assert "#CustomerSuccess" in content
    assert "#Customer Success" not in content


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


def test_state_pause_updates_existing_pause_until(tmp_path):
    work = tmp_path / "Work Tasks.md"
    work.write_text(
        "# Work\n\n## 🔴 Q1\n- [ ] **Pause me** task_id::tsk_pause paused::2026-05-01 pause_until::2026-05-10 🗓️2026-05-10\n"
    )
    env = _env(tmp_path, work)

    proc = subprocess.run(
        ["python3", "scripts/tasks.py", "state", "pause", "tsk_pause", "--until", "2026-05-20"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert proc.returncode == 0
    content = work.read_text()
    assert "pause_until::2026-05-20" in content
    assert "pause_until::2026-05-10" not in content
    assert "🗓️2026-05-20" in content
