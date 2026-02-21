import json
import os
import subprocess
from datetime import date


def test_calendar_sync_json(tmp_path):
    work = tmp_path / "Weekly TODOs.md"
    work.write_text(
        """# Weekly TODOs

## 🔴 Q1
- [ ] **Team sync** meeting::123 status::scheduled #Ops
- [ ] **Private 1:1** meeting::124 status::scheduled #private
- [ ] **Buffer block** meeting::125 status::blocked #Ops
"""
    )

    env = os.environ.copy()
    env["TASK_TRACKER_WORK_FILE"] = str(work)
    env["STANDUP_CALENDARS"] = "{}"

    proc = subprocess.run(
        ["python3", "scripts/tasks.py", "calendar", "sync", "--json"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert payload["command"] == "calendar sync"
    assert payload["idempotent"] is True
    assert len(payload["meetings"]) == 3
    blocked = next(item for item in payload["meetings"] if item["title"] == "Buffer block")
    assert blocked["status"] == "blocked"


def test_calendar_resolve_json(tmp_path):
    work = tmp_path / "Weekly TODOs.md"
    work.write_text(
        """# Weekly TODOs

## 🔴 Q1
- [ ] **Team sync** meeting::123 status::scheduled #Ops
- [ ] **Blocked sync** meeting::122 status::blocked #Ops
- [ ] **Canceled sync** meeting::124 status::canceled #Ops
"""
    )
    daily = tmp_path / f"{date.today().isoformat()}.md"
    daily.write_text("- 09:30 ✅ Team sync\n")

    env = os.environ.copy()
    env["TASK_TRACKER_WORK_FILE"] = str(work)
    env["TASK_TRACKER_DAILY_NOTES_DIR"] = str(tmp_path)

    proc = subprocess.run(
        ["python3", "scripts/tasks.py", "calendar", "resolve", "--window", "today", "--json"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert payload["command"] == "calendar resolve"
    assert payload["window"] == "today"
    assert payload["idempotent"] is True
    assert any(item["status"] == "canceled" for item in payload["resolved"])
    assert any(item["title"] == "Blocked sync" and item["status"] == "blocked" for item in payload["resolved"])
