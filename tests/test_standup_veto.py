import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import cos_config  # noqa: E402
import standup  # noqa: E402
import task_records  # noqa: E402
import task_transitions  # noqa: E402
from task_ledger import KNOWN_EVENT_TYPES  # noqa: E402

NOW = datetime.fromisoformat("2026-07-01T08:00:00-07:00")
OLD_COMPLETION_ID = "evt_" + "0" * 32
AUTO_COMPLETION_ID = "evt_" + "1" * 32
WORK_COMPLETION_ID = "evt_" + "2" * 32
PERSONAL_COMPLETION_ID = "evt_" + "3" * 32


def _tasks_data() -> dict:
    return {
        "done": [],
        "due_today": [],
        "q1": [],
        "q2": [],
        "q3": [],
        "team": [],
        "all": [],
    }


def _event(
    event_id: str,
    event_type: str,
    timestamp: str,
    *,
    task_id: str | None = None,
    source: str = "chat_capture",
    next_state: str | None = None,
    metadata: dict | None = None,
) -> dict:
    return {
        "event_id": event_id,
        "event_type": event_type,
        "timestamp": timestamp,
        "actor": "test",
        "source": source,
        "task_id": task_id,
        "previous_state": "active",
        "next_state": next_state,
        "reason": "test",
        "evidence": None,
        "metadata": metadata or {},
    }


def _write_events(ledger: Path, events: list[dict]) -> None:
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text("\n".join(json.dumps(event) for event in events) + "\n")


def _snapshot_metadata(path: Path, completion_id: str, task_id: str, title: str) -> dict:
    return {
        "title": title,
        "completion_id": completion_id,
        "board_snapshot": {
            "file": str(path),
            "task_id": task_id,
            "raw_line": f"- [ ] **{title}** task_id::{task_id}",
        },
    }


def _standup_env(tmp_path, monkeypatch):
    state = tmp_path / "state"
    ledger = state / "events.jsonl"
    work = tmp_path / "Work Tasks.md"
    work.write_text("# Work\n\n## 🔴 Q1\n")
    monkeypatch.setenv("TASK_MGMT_STATE_DIR", str(state))
    monkeypatch.setenv("TASK_TRACKER_WORK_FILE", str(work))
    monkeypatch.setenv("TASK_TRACKER_LEDGER_FILE", str(ledger))
    monkeypatch.setenv("TASK_TRACKER_MORNING_VETO_SINCE_HOUR", "6")
    monkeypatch.setenv("STANDUP_CALENDARS", "{}")
    monkeypatch.setattr(cos_config, "local_now", lambda: NOW)
    monkeypatch.setattr(standup, "get_calendar_events", lambda trigger="calendar_fetch": {})
    monkeypatch.setattr(standup, "candidate_review_summary", lambda: {})
    monkeypatch.setattr(standup, "task_audit_summary", lambda limit=3: {})
    monkeypatch.setattr(
        standup,
        "_standup_harvest_result",
        lambda date_str, *, trigger, resolved_window=None: {
            "evidence_candidates": [],
            "health": {},
            "window": resolved_window.as_dict() if resolved_window else None,
        },
    )
    return {"ledger": ledger, "work": work, "state": state}


def _apply_transition_env(monkeypatch, env: dict[str, str], work: Path) -> None:
    for key in (
        "TASK_TRACKER_WORK_FILE",
        "TASK_TRACKER_DAILY_NOTES_DIR",
        "TASK_TRACKER_DONE_LOG_DIR",
        "TASK_TRACKER_LEDGER_FILE",
        "STANDUP_CALENDARS",
    ):
        monkeypatch.setenv(key, env[key])
    monkeypatch.setattr(task_records, "get_tasks_file", lambda personal=False: (work, "obsidian"))


def _plugin_ack_text(result: dict) -> str:
    plugin_url = (ROOT / "scripts" / "openclaw-plugins" / "task-tracker-interactive" / "index.js").as_uri()
    code = (
        f"import {{ ackText }} from {json.dumps(plugin_url)};\n"
        f"console.log(ackText({json.dumps(result)}));\n"
    )
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", code],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def _render_standup_json() -> dict:
    return standup.generate_standup(
        date_str="2026-07-01",
        json_output=True,
        tasks_data=_tasks_data(),
        capacity_records=[],
    )


def _subprocess_env(tmp_path: Path, work: Path) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not (
            key.startswith("TASK_TRACKER_")
            or key.startswith("TELEGRAM_CHAT_ID_")
            or key.startswith("OPENCLAW_TOPIC_")
            or key in {"TASK_MGMT_STATE_DIR", "DIALPAD_SMS_DB", "STANDUP_CALENDARS"}
        )
    }
    env.update(
        {
            "TASK_MGMT_STATE_DIR": str(tmp_path / "state"),
            "TASK_TRACKER_WORK_FILE": str(work),
            "TASK_TRACKER_DAILY_NOTES_DIR": str(tmp_path / "daily"),
            "TASK_TRACKER_DONE_LOG_DIR": str(tmp_path / "daily"),
            "TASK_TRACKER_LEDGER_FILE": str(tmp_path / "events.jsonl"),
            "STANDUP_CALENDARS": "{}",
        }
    )
    return env


def _run_tasks(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "tasks.py"), *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_standup_shows_pending_auto_completions(tmp_path, monkeypatch):
    env = _standup_env(tmp_path, monkeypatch)
    _write_events(
        env["ledger"],
        [
            _event(
                OLD_COMPLETION_ID,
                "state_transition",
                "2026-06-30T08:15:00-07:00",
                task_id="tsk_old",
                source="chat_capture",
                next_state="done",
                metadata=_snapshot_metadata(
                    env["work"], OLD_COMPLETION_ID, "tsk_old", "Yesterday auto"
                ),
            ),
            _event(
                AUTO_COMPLETION_ID,
                "state_transition",
                "2026-07-01T07:15:00-07:00",
                task_id="tsk_auto",
                source="chat_capture",
                next_state="done",
                metadata=_snapshot_metadata(
                    env["work"], AUTO_COMPLETION_ID, "tsk_auto", "Auto-captured task"
                ),
            ),
        ],
    )

    output = _render_standup_json()

    section = output["auto_completions"]
    assert section["window"]["since"].startswith("2026-07-01T06:00:00")
    assert [item["completion_id"] for item in section["items"]] == [AUTO_COMPLETION_ID]
    assert section["items"][0]["task_id"] == "tsk_auto"
    assert section["items"][0]["title"] == "Auto-captured task"
    assert section["items"][0]["completed_time"] == "7:15 AM"
    assert section["items"][0]["action"]["callback_data"] == f"tt:undo:{AUTO_COMPLETION_ID}"
    assert "tsk_auto" not in section["items"][0]["action"]["callback_data"]

    text = standup.generate_standup(
        date_str="2026-07-01",
        tasks_data=_tasks_data(),
        capacity_records=[],
    )
    assert "Recent auto-completions" in text
    assert "Auto-captured task" in text
    assert "tt:undo:" not in text
    assert "Yesterday auto" not in text


def test_standup_auto_completions_are_work_board_only(tmp_path, monkeypatch):
    env = _standup_env(tmp_path, monkeypatch)
    personal = tmp_path / "Personal Tasks.md"
    personal.write_text("# Personal\n\n## 🔴 Q1\n")
    monkeypatch.setenv("TASK_TRACKER_PERSONAL_FILE", str(personal))
    _write_events(
        env["ledger"],
        [
            _event(
                WORK_COMPLETION_ID,
                "state_transition",
                "2026-07-01T07:10:00-07:00",
                task_id="tsk_work",
                source="chat_capture",
                next_state="done",
                metadata=_snapshot_metadata(env["work"], WORK_COMPLETION_ID, "tsk_work", "Work auto"),
            ),
            _event(
                PERSONAL_COMPLETION_ID,
                "state_transition",
                "2026-07-01T07:15:00-07:00",
                task_id="tsk_personal",
                source="chat_capture",
                next_state="done",
                metadata=_snapshot_metadata(
                    personal, PERSONAL_COMPLETION_ID, "tsk_personal", "Personal auto"
                ),
            ),
        ],
    )

    section = _render_standup_json()["auto_completions"]

    assert [item["completion_id"] for item in section["items"]] == [WORK_COMPLETION_ID]
    assert section["items"][0]["title"] == "Work auto"
    assert all(item["task_id"] != "tsk_personal" for item in section["items"])


def test_revert_command_fully_reverts(tmp_path):
    work = tmp_path / "Work Tasks.md"
    original = """# Work

## 🔴 Q1
- [ ] **Ship milestone** task_id::tsk_ship area:: Delivery
"""
    work.write_text(original)
    env = _subprocess_env(tmp_path, work)

    done = _run_tasks(["done", "tsk_ship"], env)
    assert done.returncode == 0, done.stderr
    done_payload = json.loads(done.stdout)
    completion_id = done_payload["completion_id"]
    assert "Ship milestone" not in work.read_text()
    assert "✅ Ship milestone" in "\n".join(path.read_text() for path in (tmp_path / "daily").glob("*.md"))

    reverted = _run_tasks(["revert", completion_id], env)
    assert reverted.returncode == 0, reverted.stderr
    payload = json.loads(reverted.stdout)

    assert payload["ok"] is True
    assert payload["action"] == "revert"
    assert payload["reason"] == "reverted"
    assert work.read_text() == original
    daily_text = "\n".join(path.read_text() for path in (tmp_path / "daily").glob("*.md"))
    assert "✅ Ship milestone" not in daily_text
    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    reverts = [event for event in events if event["event_type"] == "state_transition_reverted"]
    assert len(reverts) == 1
    assert reverts[0]["metadata"]["completion_id"] == completion_id


def test_undo_idempotent(tmp_path):
    work = tmp_path / "Work Tasks.md"
    original = """# Work

## 🔴 Q1
- [ ] **Ship milestone** task_id::tsk_ship area:: Delivery
"""
    work.write_text(original)
    env = _subprocess_env(tmp_path, work)
    done = _run_tasks(["done", "tsk_ship"], env)
    completion_id = json.loads(done.stdout)["completion_id"]
    first = _run_tasks(["revert", completion_id], env)
    assert first.returncode == 0, first.stderr

    second = _run_tasks(["revert", completion_id], env)
    payload = json.loads(second.stdout)

    assert second.returncode == 2
    assert payload["ok"] is False
    assert payload["reason"] == "completion-already-reverted"
    assert payload["error"]["code"] == "completion-already-reverted"
    assert work.read_text() == original
    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    reverts = [event for event in events if event["event_type"] == "state_transition_reverted"]
    assert len(reverts) == 1


def test_revert_command_rejects_invalid_completion_ids(tmp_path):
    work = tmp_path / "Work Tasks.md"
    work.write_text("# Work\n\n## 🔴 Q1\n")
    env = _subprocess_env(tmp_path, work)

    for bad_id in ("", f"{AUTO_COMPLETION_ID}:extra"):
        result = _run_tasks(["revert", bad_id], env)
        payload = json.loads(result.stdout)

        assert result.returncode == 2
        assert payload["ok"] is False
        assert payload["action"] == "revert"
        assert payload["reason"] == "unsafe-completion-id"
        assert payload["error"]["code"] == "unsafe-completion-id"


def test_revert_outside_board_undo_window_changes_nothing(tmp_path, monkeypatch):
    work = tmp_path / "Work Tasks.md"
    original = """# Work

## 🔴 Q1
- [ ] **Ship milestone** task_id::tsk_ship area:: Delivery
"""
    work.write_text(original)
    env = _subprocess_env(tmp_path, work)
    _apply_transition_env(monkeypatch, env, work)
    monkeypatch.setenv("UNDO_WINDOW_BOARD_HOURS", "168")
    monkeypatch.setattr(cos_config, "local_now", lambda: NOW)

    completed = task_transitions.complete_by_id("tsk_ship")
    assert completed["ok"] is True
    completion_id = completed["completion_id"]
    daily_path = Path(completed["event"]["metadata"]["daily_note_path"])
    board_after_complete = work.read_text()
    daily_after_complete = daily_path.read_text()

    ledger = Path(env["TASK_TRACKER_LEDGER_FILE"])
    events = [json.loads(line) for line in ledger.read_text().splitlines()]
    for event in events:
        if event["event_id"] == completion_id:
            event["timestamp"] = (NOW - timedelta(hours=169)).isoformat()
    _write_events(ledger, events)

    result = task_transitions.revert_completion(completion_id)

    assert result["ok"] is False
    assert result["error"]["code"] == "completion-out-of-window"
    assert result["error"]["window_hours"] == 168
    assert work.read_text() == board_after_complete
    assert daily_path.read_text() == daily_after_complete
    final_events = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert [event["event_type"] for event in final_events].count("state_transition_reverted") == 0


def test_revert_overwrote_edit_warning_reaches_plugin_ack(tmp_path, monkeypatch):
    work = tmp_path / "Work Tasks.md"
    original = """# Work

## 🔴 Q1
- [ ] **Send weekly update** task_id::tsk_weekly recur::weekly 🗓️2026-06-24
"""
    work.write_text(original)
    env = _subprocess_env(tmp_path, work)
    _apply_transition_env(monkeypatch, env, work)
    monkeypatch.setattr(cos_config, "local_now", lambda: NOW)

    completed = task_transitions.complete_by_id("tsk_weekly")
    assert completed["ok"] is True
    work.write_text(work.read_text().replace("**Send weekly update**", "**Send weekly update edited**"))

    result = task_transitions.revert_completion(completed["completion_id"])

    assert result["ok"] is True
    assert result["overwrote_edit"] is True
    assert "edited since the completion" in result["message"]
    ack = _plugin_ack_text({"ok": True, "action": "revert", "overwrote_edit": True})
    assert "Reverted" in ack
    assert "edited since the completion" in ack


def test_recall_report_counts(tmp_path, monkeypatch):
    env = _standup_env(tmp_path, monkeypatch)
    _write_events(
        env["ledger"],
        [
            _event(
                AUTO_COMPLETION_ID,
                "state_transition",
                "2026-07-01T07:00:00-07:00",
                task_id="tsk_auto",
                next_state="done",
                metadata=_snapshot_metadata(
                    env["work"], AUTO_COMPLETION_ID, "tsk_auto", "Auto-captured task"
                ),
            ),
            _event("evt_shown_1", "auto_completion_shown", "2026-07-01T07:01:00-07:00"),
            _event("evt_shown_2", "auto_completion_shown", "2026-07-01T07:02:00-07:00"),
            _event("evt_accepted", "accepted", "2026-07-01T07:03:00-07:00"),
            _event("evt_expired", "expired", "2026-07-01T07:04:00-07:00"),
            _event("evt_miss", "capture_miss", "2026-07-01T07:05:00-07:00"),
        ],
    )

    recall = _render_standup_json()["auto_completions"]["recall"]

    assert recall["auto"] == 2
    assert recall["shown"] == 2
    assert recall["accepted"] == 1
    assert recall["tapped"] == 1
    assert recall["expired"] == 1
    assert recall["missed_captures"] == 1
    assert recall["line"] == "Recall: auto 2; tapped 1; expired 1; missed captures 1"


def test_reverted_completion_not_listed(tmp_path, monkeypatch):
    env = _standup_env(tmp_path, monkeypatch)
    _write_events(
        env["ledger"],
        [
            _event(
                AUTO_COMPLETION_ID,
                "state_transition",
                "2026-07-01T07:15:00-07:00",
                task_id="tsk_auto",
                source="chat_capture",
                next_state="done",
                metadata=_snapshot_metadata(
                    env["work"], AUTO_COMPLETION_ID, "tsk_auto", "Auto-captured task"
                ),
            ),
            _event(
                "evt_revert_1",
                "state_transition_reverted",
                "2026-07-01T07:20:00-07:00",
                task_id="tsk_auto",
                source="user_command",
                metadata={
                    "completion_id": AUTO_COMPLETION_ID,
                    "reverted_event_id": AUTO_COMPLETION_ID,
                },
            ),
        ],
    )

    assert _render_standup_json()["auto_completions"] is None


def test_veto_event_types_are_registered():
    for event_type in (
        "auto_completion_shown",
        "auto_completion_accepted",
        "auto_completion_expired",
        "accepted",
        "expired",
        "capture_miss",
    ):
        assert event_type in KNOWN_EVENT_TYPES
