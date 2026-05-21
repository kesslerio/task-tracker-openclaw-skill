import json
import os
import subprocess
from datetime import datetime


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


def test_done_removes_resolved_active_line_when_inactive_raw_line_matches(tmp_path):
    raw_line = "- [ ] **Same title** task_id::tsk_same"
    work = tmp_path / "Work Tasks.md"
    work.write_text(f"""# Work

## 🅿️ Parking Lot
{raw_line}

## 🔴 Q1
{raw_line}
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
    assert content.count(raw_line) == 1
    assert content.index(raw_line) < content.index("## 🔴 Q1")


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


def test_done_recurring_task_reports_bad_rule_without_writes(tmp_path):
    work = tmp_path / "Work Tasks.md"
    original = """# Work

## 🔴 Q1
- [ ] **Send weekly update** task_id::tsk_weekly recur::weekley 🗓️2026-05-20
"""
    work.write_text(original)
    env = _env(tmp_path, work)

    proc = subprocess.run(
        ["python3", "scripts/tasks.py", "done", "tsk_weekly"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert proc.returncode == 2
    payload = json.loads(proc.stdout)
    assert payload["error"]["code"] == "recurrence-rollover-failed"
    assert work.read_text() == original
    assert not (tmp_path / "daily").exists()
    assert (tmp_path / "events.jsonl").read_text() == ""


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
