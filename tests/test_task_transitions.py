import json
import os
import subprocess
from datetime import datetime
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import task_transitions
import task_records


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
