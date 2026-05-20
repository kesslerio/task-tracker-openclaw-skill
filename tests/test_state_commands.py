import json
import os
import subprocess
from pathlib import Path


def _run(cmd, env):
    return subprocess.run(cmd, text=True, capture_output=True, env=env, check=False)


def _env(tmp_path):
    work = tmp_path / "Weekly TODOs.md"
    work.write_text(
        """# Weekly TODOs\n\n## 🔴 Q1\n- [ ] **Ship alpha** task_id::tsk_ship #Dev\n\n## 🟡 Q2\n- [ ] **Review roadmap** task_id::tsk_roadmap #Ops\n\n## 🅿️ Parking Lot\n- [ ] **Old task** #Ops #low created::2025-01-01\n"""
    )
    delegated = tmp_path / "Delegated.md"
    delegated.write_text("# Delegated Tasks\n\n## Active\n\n## Awaiting Follow-up\n\n## Completed\n")
    env = os.environ.copy()
    env["TASK_TRACKER_WORK_FILE"] = str(work)
    env["TASK_TRACKER_DELEGATION_FILE"] = str(delegated)
    env["TASK_TRACKER_DAILY_NOTES_DIR"] = str(tmp_path)
    env["TASK_TRACKER_LEDGER_FILE"] = str(tmp_path / "events.jsonl")
    return env, work, delegated


def test_state_pause_and_backlog(tmp_path):
    env, work, _ = _env(tmp_path)
    r = _run(["python3", "scripts/tasks.py", "state", "pause", "tsk_ship", "--until", "2026-03-01"], env)
    assert r.returncode == 0
    content = work.read_text()
    assert "🗓️2026-03-01" in content

    r = _run(["python3", "scripts/tasks.py", "state", "backlog", "tsk_roadmap"], env)
    assert r.returncode == 0
    content = work.read_text().lower()
    assert "review roadmap" not in content
    assert "parking lot" in content
    events = [json.loads(line) for line in Path(env["TASK_TRACKER_LEDGER_FILE"]).read_text().splitlines()]
    assert [event["next_state"] for event in events] == ["active", "backlog"]


def test_promote_and_review_backlog(tmp_path):
    env, work, _ = _env(tmp_path)
    r = _run(["python3", "scripts/tasks.py", "review-backlog", "--stale-days", "30", "--json"], env)
    assert r.returncode == 0
    data = json.loads(r.stdout)
    assert data

    r = _run(["python3", "scripts/tasks.py", "promote-from-backlog", "--cap", "1"], env)
    assert r.returncode == 0
    assert "Promoted from Parking Lot" in r.stdout
    assert "Old task" in work.read_text()
