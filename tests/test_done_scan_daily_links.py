import json
import os
import subprocess
from datetime import date, timedelta


def test_done_scan_json(tmp_path):
    note = tmp_path / f"{date.today().isoformat()}.md"
    note.write_text("- 10:00 ✅ Ship alpha\n")
    older = tmp_path / f"{(date.today() - timedelta(days=2)).isoformat()}.md"
    older.write_text("- 09:00 ✅ Too old\n")

    env = os.environ.copy()
    env["TASK_TRACKER_DAILY_NOTES_DIR"] = str(tmp_path)
    env["TASK_TRACKER_WORK_FILE"] = str(tmp_path / "Weekly TODOs.md")
    (tmp_path / "Weekly TODOs.md").write_text("# Weekly TODOs\n")

    proc = subprocess.run(
        ["python3", "scripts/tasks.py", "done-scan", "--window", "24h", "--json"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert payload["command"] == "done scan"
    assert payload["window"] == "24h"
    assert payload["count"] == 1


def test_daily_links_json(tmp_path):
    env = os.environ.copy()
    env["TASK_TRACKER_WORK_FILE"] = str(tmp_path / "Weekly TODOs.md")
    (tmp_path / "Weekly TODOs.md").write_text("# Weekly TODOs\n")

    proc = subprocess.run(
        ["python3", "scripts/tasks.py", "daily-links", "--window", "today", "--json"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert payload["command"] == "daily links"
    assert "today" in payload["links"]
    assert payload["links"]["today"]["deep"].startswith("obsidian://")
    assert payload["links"]["today"]["universal"].startswith("https://obsidian.md/open?")
