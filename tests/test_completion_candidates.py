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


def test_candidate_add_dedupes_by_source_pointer_summary_and_task(tmp_path):
    work = tmp_path / "Work Tasks.md"
    work.write_text("# Work\n\n## 🔴 Q1\n- [ ] **Ship milestone** task_id::tsk_ship\n")
    env = _env(tmp_path, work)

    cmd = [
        "python3",
        "scripts/tasks.py",
        "completion-candidates",
        "add",
        "--source-type",
        "daily-note",
        "--source-pointer",
        "2026-05-20.md:3",
        "--summary",
        "Ship milestone",
        "--task-id",
        "tsk_ship",
        "--confidence",
        "0.95",
    ]
    first = subprocess.run(cmd, capture_output=True, text=True, check=False, env=env)
    second = subprocess.run(cmd, capture_output=True, text=True, check=False, env=env)

    assert first.returncode == 0
    assert second.returncode == 0
    assert json.loads(first.stdout)["created"] is True
    assert json.loads(second.stdout)["created"] is False
    assert len((tmp_path / "events.jsonl").read_text().splitlines()) == 1


def test_candidate_confirmation_uses_id_transition(tmp_path):
    work = tmp_path / "Work Tasks.md"
    work.write_text("# Work\n\n## 🔴 Q1\n- [ ] **Ship milestone** task_id::tsk_ship\n")
    env = _env(tmp_path, work)

    add = subprocess.run(
        [
            "python3",
            "scripts/tasks.py",
            "completion-candidates",
            "add",
            "--source-type",
            "done-topic",
            "--source-pointer",
            "telegram:42",
            "--summary",
            "Ship milestone",
            "--task-id",
            "tsk_ship",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    key = json.loads(add.stdout)["candidate"]["evidence"]["dedupe_key"]

    decide = subprocess.run(
        ["python3", "scripts/tasks.py", "completion-candidates", "decide", key, "confirmed"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert decide.returncode == 0
    assert "Ship milestone" not in work.read_text()
    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert [event["event_type"] for event in events] == [
        "completion_candidate",
        "state_transition",
        "completion_candidate_decision",
    ]
