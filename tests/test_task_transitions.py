import json
import os
import subprocess
import textwrap
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import task_transitions
import task_records
from locks import sidecar_flock
from rollover import rollover_board


def _env(tmp_path, work):
    env = os.environ.copy()
    env["TASK_TRACKER_WORK_FILE"] = str(work)
    env["TASK_TRACKER_DAILY_NOTES_DIR"] = str(tmp_path / "daily")
    env["TASK_TRACKER_DONE_LOG_DIR"] = str(tmp_path / "daily")
    env["TASK_TRACKER_LEDGER_FILE"] = str(tmp_path / "events.jsonl")
    env["STANDUP_CALENDARS"] = "{}"
    return env


def _apply_env(monkeypatch, env, work):
    for key in (
        "TASK_TRACKER_WORK_FILE",
        "TASK_TRACKER_DAILY_NOTES_DIR",
        "TASK_TRACKER_DONE_LOG_DIR",
        "TASK_TRACKER_LEDGER_FILE",
        "STANDUP_CALENDARS",
    ):
        value = env[key]
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(task_records, "get_tasks_file", lambda personal=False: (work, "obsidian"))


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


def test_cancel_by_canonical_id_removes_task_and_writes_cancelled_ledger(tmp_path):
    work = tmp_path / "Work Tasks.md"
    original = """# Work

## 🔴 Q1
- [ ] **Cancel this** task_id::tsk_cancel area:: Delivery
- [ ] **Keep this** task_id::tsk_keep area:: Ops
"""
    work.write_text(original)
    env = _env(tmp_path, work)

    assert "Cancel this" in work.read_text()
    proc = subprocess.run(
        ["python3", "scripts/tasks.py", "remove", "tsk_cancel"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert payload["ok"] is True
    content = work.read_text()
    assert "Cancel this" not in content
    assert "Keep this" in content
    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert len(events) == 1
    assert events[0]["event_type"] == "state_transition"
    assert events[0]["previous_state"] == "active"
    assert events[0]["next_state"] == "cancelled"
    assert events[0]["next_state"] != "done"
    assert events[0]["reason"] == "cancelled-by-id"
    assert events[0]["metadata"]["raw_line"] == "- [ ] **Cancel this** task_id::tsk_cancel area:: Delivery"


def test_remove_writes_no_completion_log_or_done_count(tmp_path):
    work = tmp_path / "Work Tasks.md"
    work.write_text("""# Work

## 🔴 Q1
- [ ] **Cancel this** task_id::tsk_cancel area:: Delivery
""")
    env = _env(tmp_path, work)

    proc = subprocess.run(
        ["python3", "scripts/tasks.py", "remove", "tsk_cancel"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert proc.returncode == 0
    daily_notes = list((tmp_path / "daily").glob("*.md"))
    assert daily_notes == []

    done_proc = subprocess.run(
        ["python3", "scripts/tasks.py", "list", "--status", "done", "--completed-since", "7d"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert done_proc.returncode == 0
    assert "Cancel this" not in done_proc.stdout
    assert "✅" not in done_proc.stdout


def test_cancel_recurring_task_does_not_spawn_next_occurrence(tmp_path):
    work = tmp_path / "Work Tasks.md"
    work.write_text("""# Work

## 🔴 Q1
- [ ] **Send weekly update** task_id::tsk_weekly recur::weekly 🗓️2026-05-20
""")
    env = _env(tmp_path, work)

    proc = subprocess.run(
        ["python3", "scripts/tasks.py", "remove", "tsk_weekly"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert proc.returncode == 0
    content = work.read_text()
    assert content.count("Send weekly update") == 0
    assert "🗓️2026-05-27" not in content


def test_cancel_nonexistent_task_id_returns_error_without_writes(tmp_path):
    work = tmp_path / "Work Tasks.md"
    original = """# Work

## 🔴 Q1
- [ ] **Keep this** task_id::tsk_keep area:: Ops
"""
    work.write_text(original)
    env = _env(tmp_path, work)

    proc = subprocess.run(
        ["python3", "scripts/tasks.py", "remove", "tsk_missing"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert proc.returncode == 2
    payload = json.loads(proc.stdout)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "canonical-id-resolution-failed"
    assert "found 0" in payload["error"]["message"]
    assert work.read_text() == original
    assert not (tmp_path / "events.jsonl").exists()


def test_cancel_duplicate_task_id_refuses_without_writes(tmp_path):
    work = tmp_path / "Work Tasks.md"
    original = """# Work

## 🔴 Q1
- [ ] **First copy** task_id::tsk_dup area:: Delivery
- [ ] **Second copy** task_id::tsk_dup area:: Ops
"""
    work.write_text(original)
    env = _env(tmp_path, work)

    proc = subprocess.run(
        ["python3", "scripts/tasks.py", "remove", "tsk_dup"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert proc.returncode == 2
    payload = json.loads(proc.stdout)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "canonical-id-resolution-failed"
    assert "found 2" in payload["error"]["message"]
    assert work.read_text() == original
    assert not (tmp_path / "events.jsonl").exists()


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


def test_remove_by_title_is_blocked_without_writes(tmp_path):
    work = tmp_path / "Work Tasks.md"
    original = """# Work

## 🔴 Q1
- [ ] **Some Title With Spaces** task_id::tsk_remove area:: Delivery
"""
    work.write_text(original)
    env = _env(tmp_path, work)

    proc = subprocess.run(
        ["python3", "scripts/tasks.py", "remove", "Some Title With Spaces"],
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


def test_cancel_refuses_parking_lot_and_backlog_task_ids_without_writes(tmp_path):
    work = tmp_path / "Work Tasks.md"
    original = """# Work

## 🅿️ Parking Lot
- [ ] **Parked task** task_id::tsk_parked created::2026-01-01

## ⚪ Backlog
- [ ] **Backlog task** task_id::tsk_backlog area:: Ops
"""
    work.write_text(original)
    env = _env(tmp_path, work)

    for task_id in ("tsk_parked", "tsk_backlog"):
        proc = subprocess.run(
            ["python3", "scripts/tasks.py", "remove", task_id],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )

        assert proc.returncode == 2
        payload = json.loads(proc.stdout)
        assert payload["error"]["code"] == "canonical-id-resolution-failed"
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


def test_cancel_aborts_before_board_write_when_ledger_unwritable(tmp_path):
    work = tmp_path / "Work Tasks.md"
    original = """# Work

## 🔴 Q1
- [ ] **Cancel milestone** task_id::tsk_cancel area:: Delivery
"""
    work.write_text(original)
    ledger_dir = tmp_path / "ledger-is-dir"
    ledger_dir.mkdir()
    env = _env(tmp_path, work)
    env["TASK_TRACKER_LEDGER_FILE"] = str(ledger_dir)

    proc = subprocess.run(
        ["python3", "scripts/tasks.py", "remove", "tsk_cancel"],
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


def test_cancel_restores_board_when_ledger_append_fails(tmp_path):
    if not os.path.exists("/dev/full"):
        return
    work = tmp_path / "Work Tasks.md"
    original = """# Work

## 🔴 Q1
- [ ] **Cancel milestone** task_id::tsk_cancel area:: Delivery
"""
    work.write_text(original)
    env = _env(tmp_path, work)
    env["TASK_TRACKER_LEDGER_FILE"] = "/dev/full"

    proc = subprocess.run(
        ["python3", "scripts/tasks.py", "remove", "tsk_cancel"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert proc.returncode == 2
    payload = json.loads(proc.stdout)
    assert payload["error"]["code"] == "ledger-append-failed"
    assert work.read_text() == original


def test_done_restores_completion_log_when_board_write_fails(tmp_path, monkeypatch):
    work = tmp_path / "Work Tasks.md"
    original = """# Work

## 🔴 Q1
- [ ] **Ship milestone** task_id::tsk_ship area:: Delivery
"""
    work.write_text(original)
    env = _env(tmp_path, work)
    monkeypatch.setenv("TASK_TRACKER_WORK_FILE", env["TASK_TRACKER_WORK_FILE"])
    monkeypatch.setenv("TASK_TRACKER_DAILY_NOTES_DIR", env["TASK_TRACKER_DAILY_NOTES_DIR"])
    monkeypatch.setenv("TASK_TRACKER_DONE_LOG_DIR", env["TASK_TRACKER_DONE_LOG_DIR"])
    monkeypatch.setenv("TASK_TRACKER_LEDGER_FILE", env["TASK_TRACKER_LEDGER_FILE"])
    monkeypatch.setattr(task_records, "get_tasks_file", lambda personal=False: (work, "obsidian"))

    # The board write now goes through utils._atomic_write (temp file + os.replace),
    # and the rollback restore goes through it too (atomic restore). Fail ONLY the
    # forward board write; let the atomic restore proceed so the invariant still
    # holds: a failed board write restores snapshots and reports
    # task-state-write-failed.
    real_atomic_write = task_transitions._atomic_write
    state = {"forward_failed": False}

    def fail_board_write(path_obj, content):
        if Path(path_obj) == work and not state["forward_failed"]:
            state["forward_failed"] = True
            raise OSError("simulated board write failure")
        return real_atomic_write(path_obj, content)

    monkeypatch.setattr(task_transitions, "_atomic_write", fail_board_write)

    result = task_transitions.complete_by_id("tsk_ship")

    assert result["ok"] is False
    assert result["error"]["code"] == "task-state-write-failed"
    assert work.read_text() == original
    assert not list((tmp_path / "daily").glob("*.md"))
    assert (tmp_path / "events.jsonl").read_text() == ""


def test_cancel_restores_board_when_board_write_fails(tmp_path, monkeypatch):
    work = tmp_path / "Work Tasks.md"
    original = """# Work

## 🔴 Q1
- [ ] **Cancel milestone** task_id::tsk_cancel area:: Delivery
"""
    work.write_text(original)
    env = _env(tmp_path, work)
    monkeypatch.setenv("TASK_TRACKER_WORK_FILE", env["TASK_TRACKER_WORK_FILE"])
    monkeypatch.setenv("TASK_TRACKER_DAILY_NOTES_DIR", env["TASK_TRACKER_DAILY_NOTES_DIR"])
    monkeypatch.setenv("TASK_TRACKER_DONE_LOG_DIR", env["TASK_TRACKER_DONE_LOG_DIR"])
    monkeypatch.setenv("TASK_TRACKER_LEDGER_FILE", env["TASK_TRACKER_LEDGER_FILE"])
    monkeypatch.setattr(task_records, "get_tasks_file", lambda personal=False: (work, "obsidian"))

    real_atomic_write = task_transitions._atomic_write
    state = {"forward_failed": False}

    def fail_board_write(path_obj, content):
        if Path(path_obj) == work and not state["forward_failed"]:
            state["forward_failed"] = True
            raise OSError("simulated board write failure")
        return real_atomic_write(path_obj, content)

    monkeypatch.setattr(task_transitions, "_atomic_write", fail_board_write)

    result = task_transitions.cancel_by_id("tsk_cancel")

    assert result["ok"] is False
    assert result["error"]["code"] == "task-state-write-failed"
    assert work.read_text() == original
    assert (tmp_path / "events.jsonl").read_text() == ""


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


def test_done_recurring_task_sequential_double_complete_advances_two_occurrences(tmp_path):
    work = tmp_path / "Work Tasks.md"
    work.write_text("""# Work

## 🔴 Q1
- [ ] **Send daily update** task_id::tsk_daily recur::daily 🗓️2026-05-20
""")
    env = _env(tmp_path, work)

    for _ in range(2):
        proc = subprocess.run(
            ["python3", "scripts/tasks.py", "done", "tsk_daily"],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
        assert proc.returncode == 0
        assert json.loads(proc.stdout)["ok"] is True

    content = work.read_text()
    assert "Send daily update" in content
    assert "🗓️2026-05-22" in content
    assert "🗓️2026-05-21" not in content
    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    transitions = [event for event in events if event["event_type"] == "state_transition"]
    assert len(transitions) == 2


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


def test_restore_snapshots_is_atomic(tmp_path, monkeypatch):
    """Finding #6: rollback must be at least as crash-safe as the forward path.

    Assert _restore_snapshots routes existing-file restores through the atomic
    writer (temp+replace) rather than a non-atomic write_text, and that a crash
    mid-restore leaves the destination untouched (no truncation)."""
    import utils

    target = tmp_path / "board.md"
    target.write_text("CURRENT (post-failed-write) CONTENT\n")
    snapshot_content = "ORIGINAL SNAPSHOT CONTENT\n" * 20

    # Capture the writer used for the restore.
    calls = []
    real_atomic_write = utils._atomic_write

    def tracking_atomic_write(path_obj, content):
        calls.append(Path(path_obj))
        return real_atomic_write(path_obj, content)

    monkeypatch.setattr(task_transitions, "_atomic_write", tracking_atomic_write)
    task_transitions._restore_snapshots({target: (True, snapshot_content)})

    assert calls == [target], "restore must go through the atomic writer"
    assert target.read_text() == snapshot_content


# --- U8a board mutation lock -------------------------------------------------


def test_concurrent_complete_by_id_serializes_to_one_completion(tmp_path):
    work = tmp_path / "Work Tasks.md"
    work.write_text("""# Work

## 🔴 Q1
- [ ] **Race task** task_id::tsk_race area:: Delivery
""")
    env = _env(tmp_path, work)
    ready_dir = tmp_path / "ready"
    ready_dir.mkdir()
    env["RACE_READY_DIR"] = str(ready_dir)
    scripts_dir = Path(__file__).resolve().parents[1] / "scripts"
    worker = textwrap.dedent(f"""
        import json
        import os
        import sys
        import time
        from pathlib import Path

        sys.path.insert(0, {str(scripts_dir)!r})
        ready_dir = Path(os.environ["RACE_READY_DIR"])
        (ready_dir / f"{{os.getpid()}}.ready").write_text("ready", encoding="utf-8")
        deadline = time.time() + 5
        while len(list(ready_dir.glob("*.ready"))) < 2:
            if time.time() > deadline:
                print(json.dumps({{"ok": False, "error": {{"code": "barrier-timeout"}}}}))
                sys.exit(3)
            time.sleep(0.01)

        import task_transitions
        print(json.dumps(task_transitions.complete_by_id("tsk_race"), sort_keys=True))
    """)

    procs = [
        subprocess.Popen(
            ["python3", "-c", worker],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=Path(__file__).resolve().parents[1],
            env=env,
        )
        for _ in range(2)
    ]
    payloads = []
    for proc in procs:
        stdout, stderr = proc.communicate(timeout=10)
        assert proc.returncode == 0, stderr
        payloads.append(json.loads(stdout))

    assert sum(1 for payload in payloads if payload.get("ok") and not payload.get("noop")) == 1
    assert sum(1 for payload in payloads if not payload.get("ok") and payload.get("noop")) == 1
    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    transitions = [event for event in events if event["event_type"] == "state_transition"]
    assert len(transitions) == 1
    daily_text = "\n".join(path.read_text() for path in (tmp_path / "daily").glob("*.md"))
    assert daily_text.count("✅ Race task") == 1
    assert "Race task" not in work.read_text()


def test_concurrent_complete_recurring_serializes_to_one_completion(tmp_path):
    work = tmp_path / "Work Tasks.md"
    work.write_text("""# Work

## 🔴 Q1
- [ ] **Daily race task** task_id::tsk_daily_race recur::daily 🗓️2026-05-20
""")
    env = _env(tmp_path, work)
    ready_dir = tmp_path / "ready"
    ready_dir.mkdir()
    env["RACE_READY_DIR"] = str(ready_dir)
    scripts_dir = Path(__file__).resolve().parents[1] / "scripts"
    worker = textwrap.dedent(f"""
        import json
        import os
        import sys
        from contextlib import contextmanager
        from pathlib import Path

        sys.path.insert(0, {str(scripts_dir)!r})
        ready_dir = Path(os.environ["RACE_READY_DIR"])

        import task_transitions
        real_board_flock = task_transitions.board_flock

        @contextmanager
        def tracking_board_flock(path):
            (ready_dir / f"{{os.getpid()}}.ready").write_text("ready", encoding="utf-8")
            with real_board_flock(path):
                yield

        task_transitions.board_flock = tracking_board_flock
        print(json.dumps(task_transitions.complete_by_id("tsk_daily_race"), sort_keys=True))
    """)

    with sidecar_flock(work):
        procs = [
            subprocess.Popen(
                ["python3", "-c", worker],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=Path(__file__).resolve().parents[1],
                env=env,
            )
            for _ in range(2)
        ]
        deadline = datetime.now().timestamp() + 10
        while len(list(ready_dir.glob("*.ready"))) < 2:
            assert datetime.now().timestamp() < deadline
            for proc in procs:
                assert proc.poll() is None
            time.sleep(0.01)

    payloads = []
    for proc in procs:
        stdout, stderr = proc.communicate(timeout=10)
        assert proc.returncode == 0, stderr
        payloads.append(json.loads(stdout))

    assert sum(1 for payload in payloads if payload.get("ok") and not payload.get("noop")) == 1
    assert sum(1 for payload in payloads if not payload.get("ok") and payload.get("noop")) == 1
    noop = next(payload for payload in payloads if payload.get("noop"))
    assert noop["reason"] == "already-done"

    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    transitions = [event for event in events if event["event_type"] == "state_transition"]
    assert len(transitions) == 1
    daily_text = "\n".join(path.read_text() for path in (tmp_path / "daily").glob("*.md"))
    assert daily_text.count("✅ Daily race task") == 1
    content = work.read_text()
    assert "Daily race task" in content
    assert "🗓️2026-05-21" in content
    assert "🗓️2026-05-22" not in content


def test_reschedule_carry_drop_hold_board_lock_around_board_write(tmp_path, monkeypatch):
    work = tmp_path / "Work Tasks.md"
    work.write_text("""# Work

## 🔴 Q1
- [ ] **Reschedule me** task_id::tsk_reschedule area:: Delivery 🗓️2026-06-01
- [ ] **Carry me** task_id::tsk_carry area:: Delivery
- [ ] **Drop me** task_id::tsk_drop area:: Delivery

## 🅿️ Parking Lot
""")
    env = _env(tmp_path, work)
    _apply_env(monkeypatch, env, work)

    real_board_flock = task_transitions.board_flock
    real_atomic_write = task_transitions._atomic_write
    lock_depth = {"value": 0}
    entered = []
    board_writes = []

    @contextmanager
    def tracking_board_flock(path):
        entered.append(Path(path))
        lock_depth["value"] += 1
        with real_board_flock(path):
            yield
        lock_depth["value"] -= 1

    def checking_atomic_write(path_obj, content):
        if Path(path_obj) == work:
            assert lock_depth["value"] == 1
            board_writes.append(content)
        return real_atomic_write(path_obj, content)

    monkeypatch.setattr(task_transitions, "board_flock", tracking_board_flock)
    monkeypatch.setattr(task_transitions, "_atomic_write", checking_atomic_write)

    assert task_transitions.reschedule_by_id("tsk_reschedule", "2026-06-08")["ok"] is True
    assert task_transitions.carry_by_id("tsk_carry", carried_date="2026-06-30")["ok"] is True
    assert task_transitions.drop_by_id("tsk_drop")["ok"] is True

    assert entered == [work, work, work]
    assert len(board_writes) == 3


# --- U5 EOD disposition: carry_by_id + drop_by_id ----------------------------


def _events(tmp_path):
    ledger = tmp_path / "events.jsonl"
    if not ledger.exists():
        return []
    return [json.loads(line) for line in ledger.read_text().splitlines() if line.strip()]


def test_carry_keeps_task_active_and_stamps_carried(tmp_path):
    work = tmp_path / "Work Tasks.md"
    work.write_text("""# Work

## 🔴 Q1
- [ ] **Ship milestone** task_id::tsk_ship area:: Delivery
""")
    env = _env(tmp_path, work)

    proc = subprocess.run(
        ["python3", "scripts/nag_commands.py", "carry", "tsk_ship"],
        capture_output=True, text=True, check=False, env=env,
    )

    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert payload["ok"] is True
    content = work.read_text()
    # Carry KEEPS the task active and stamps the carried:: marker (no done, no parking).
    assert "Ship milestone" in content
    assert "carried::" in content
    # The disposition event is the registered eod_disposition_carry type.
    types = [e["event_type"] for e in _events(tmp_path)]
    assert "eod_disposition_carry" in types


def test_carry_is_idempotent_single_marker(tmp_path):
    work = tmp_path / "Work Tasks.md"
    work.write_text("""# Work

## 🔴 Q1
- [ ] **Ship milestone** task_id::tsk_ship area:: Delivery
""")
    env = _env(tmp_path, work)

    for _ in range(2):
        subprocess.run(["python3", "scripts/nag_commands.py", "carry", "tsk_ship"],
                       capture_output=True, text=True, check=False, env=env)

    # Re-carrying refreshes the date rather than stacking markers: exactly one carried::.
    assert work.read_text().count("carried::") == 1


def test_drop_moves_task_to_parking_lot_and_logs_event(tmp_path):
    work = tmp_path / "Work Tasks.md"
    work.write_text("""# Work

## 🔴 Q1
- [ ] **Ship milestone** task_id::tsk_ship area:: Delivery

## 🅿️ Parking Lot
""")
    env = _env(tmp_path, work)

    proc = subprocess.run(
        ["python3", "scripts/nag_commands.py", "drop", "tsk_ship"],
        capture_output=True, text=True, check=False, env=env,
    )

    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert payload["ok"] is True
    content = work.read_text()
    # The task left the active Q1 section and now lives under the Parking Lot header.
    assert content.index("tsk_ship") > content.index("Parking Lot")
    types = [e["event_type"] for e in _events(tmp_path)]
    assert "eod_disposition_drop" in types


def test_drop_without_parking_lot_refuses_and_leaves_board_unchanged(tmp_path):
    work = tmp_path / "Work Tasks.md"
    original = """# Work

## 🔴 Q1
- [ ] **Ship milestone** task_id::tsk_ship area:: Delivery
"""
    work.write_text(original)
    env = _env(tmp_path, work)

    proc = subprocess.run(
        ["python3", "scripts/nag_commands.py", "drop", "tsk_ship"],
        capture_output=True, text=True, check=False, env=env,
    )

    assert proc.returncode == 2
    payload = json.loads(proc.stdout)
    assert payload["error"]["code"] == "parking-lot-missing"
    # Board untouched, no disposition event written.
    assert work.read_text() == original
    assert "eod_disposition_drop" not in [e["event_type"] for e in _events(tmp_path)]


def test_drop_is_reversible_via_undo(tmp_path):
    """A drop records a pre-action board snapshot through the autonomy gate, so /undo
    restores the original active line by stable id (restore-by-task-id) within the undo
    window: the parked line is swapped back to its original active form -- one copy, no
    parking-lot ``created::`` marker, no duplicate."""
    work = tmp_path / "Work Tasks.md"
    active_line = "- [ ] **Ship milestone** task_id::tsk_ship area:: Delivery"
    work.write_text(f"""# Work

## 🔴 Q1
{active_line}

## 🅿️ Parking Lot
""")
    env = _env(tmp_path, work)
    state = tmp_path / "state"
    env["TASK_MGMT_STATE_DIR"] = str(state)

    drop = subprocess.run(["python3", "scripts/nag_commands.py", "drop", "tsk_ship"],
                          capture_output=True, text=True, check=False, env=env)
    assert drop.returncode == 0
    parked = work.read_text()
    # The task moved to the parking lot (below the header) and carries the parking marker.
    assert parked.index("tsk_ship") > parked.index("Parking Lot")
    assert "created::" in parked

    # Find the gated act_id for the drop, then /undo it.
    audit = subprocess.run(["python3", "scripts/autonomy_cli.py", "audit"],
                           capture_output=True, text=True, check=False, env=env)
    act_line = next(line for line in audit.stdout.splitlines() if "act_" in line)
    import re as _re
    act_id = _re.search(r"act_[0-9a-f]+", act_line).group(0)

    undo = subprocess.run(["python3", "scripts/autonomy_cli.py", "undo", act_id],
                          capture_output=True, text=True, check=False, env=env)
    assert undo.returncode == 0
    content = work.read_text()
    # The line is restored to its ORIGINAL active form by stable id (restore-by-task-id):
    # exactly one copy, the parking ``created::`` marker gone (no parked duplicate).
    assert content.count("task_id::tsk_ship") == 1
    assert active_line in content
    assert "created::" not in content


def test_drop_recurring_task_does_not_spawn_next_occurrence(tmp_path):
    """Dropping a recurring task is an explicit stop-chasing decision, NOT a
    completion: it must move to the parking lot as-is, never roll forward a dup."""
    work = tmp_path / "Work Tasks.md"
    work.write_text("""# Work

## 🔴 Q1
- [ ] **Send weekly update** task_id::tsk_weekly recur::weekly 🗓️2026-05-20

## 🅿️ Parking Lot
""")
    env = _env(tmp_path, work)

    proc = subprocess.run(["python3", "scripts/nag_commands.py", "drop", "tsk_weekly"],
                          capture_output=True, text=True, check=False, env=env)
    assert proc.returncode == 0
    content = work.read_text()
    # Exactly ONE occurrence, now parked (no rolled-forward 2026-05-27 dup on the board).
    assert content.count("Send weekly update") == 1
    assert content.index("Send weekly update") > content.index("Parking Lot")
    assert "🗓️2026-05-27" not in content


def test_rollover_excludes_reintroduced_cancelled_task(tmp_path):
    work = tmp_path / "Work Tasks.md"
    original = """# Work

## 🔴 Q1
- [ ] **Cancelled relapse** task_id::tsk_cancel area:: Delivery
- [ ] **Keep this** task_id::tsk_keep area:: Ops
"""
    work.write_text(original)
    env = _env(tmp_path, work)

    proc = subprocess.run(
        ["python3", "scripts/tasks.py", "remove", "tsk_cancel"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert proc.returncode == 0

    work.write_text(original)
    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    result = rollover_board(work.read_text(), events, target_date="2026-06-29")

    assert "Cancelled relapse" not in result.content
    assert "Keep this" in result.content
    assert result.excluded_closed == ("tsk_cancel",)


def test_restore_snapshots_crash_mid_restore_leaves_target_intact(tmp_path, monkeypatch):
    """If os.replace fails during the atomic restore, the destination is not
    truncated -- the same crash-safety the forward path has."""
    import utils

    target = tmp_path / "board.md"
    current = "CURRENT CONTENT THAT MUST SURVIVE A FAILED RESTORE\n" * 10
    target.write_text(current)

    def boom(src, dst):
        raise OSError("simulated crash during restore replace")

    monkeypatch.setattr(utils.os, "replace", boom)

    # _restore_after_failure swallows the OSError and reports it; the destination
    # must be byte-for-byte the pre-restore content (never half-written).
    err = task_transitions._restore_after_failure({target: (True, "SNAPSHOT\n")})
    assert err is not None
    assert target.read_text() == current
