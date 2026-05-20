import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


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


def test_personal_candidate_confirmation_uses_personal_board_and_ledger(tmp_path):
    work = tmp_path / "Work Tasks.md"
    personal = tmp_path / "Personal Tasks.md"
    work.write_text("# Work\n\n## 🔴 Q1\n- [ ] **Work task** task_id::tsk_same\n")
    personal.write_text("# Personal\n\n## 🔴 Q1\n- [ ] **Personal task** task_id::tsk_same\n")
    env = _env(tmp_path, work)
    env.pop("TASK_TRACKER_LEDGER_FILE")
    env["TASK_TRACKER_PERSONAL_FILE"] = str(personal)

    add = subprocess.run(
        [
            "python3",
            "scripts/tasks.py",
            "--personal",
            "completion-candidates",
            "add",
            "--source-type",
            "done-topic",
            "--source-pointer",
            "telegram:99",
            "--summary",
            "Personal task",
            "--task-id",
            "tsk_same",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    key = json.loads(add.stdout)["candidate"]["evidence"]["dedupe_key"]

    decide = subprocess.run(
        ["python3", "scripts/tasks.py", "--personal", "completion-candidates", "decide", key, "confirmed"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert decide.returncode == 0
    assert "Personal task" not in personal.read_text()
    assert "Work task" in work.read_text()
    assert personal.with_suffix(".md.events.jsonl").exists()
    assert not work.with_suffix(".md.events.jsonl").exists()


def test_candidate_decision_returns_structured_error_when_ledger_append_fails(monkeypatch, tmp_path):
    import completion_candidates

    work = tmp_path / "Work Tasks.md"
    work.write_text("# Work\n\n## 🔴 Q1\n- [ ] **Ship milestone** task_id::tsk_ship\n")
    monkeypatch.setenv("TASK_TRACKER_WORK_FILE", str(work))
    monkeypatch.setenv("TASK_TRACKER_LEDGER_FILE", str(tmp_path / "events.jsonl"))

    created = completion_candidates.create_candidate(
        source_type="done-topic",
        source_pointer="telegram:42",
        summary="Ship milestone",
        matched_task_id="tsk_ship",
    )
    key = created["candidate"]["evidence"]["dedupe_key"]

    def fail_append(event, path=None):
        raise OSError("simulated ledger failure")

    monkeypatch.setattr(completion_candidates, "append_event", fail_append)

    payload = completion_candidates.decide_candidate(key, "rejected")

    assert payload["ok"] is False
    assert payload["error"]["code"] == "candidate-decision-ledger-failed"
