import json
import os
import subprocess


def test_compact_standup_sections(tmp_path):
    work = tmp_path / "Weekly TODOs.md"
    work.write_text(
        """# Weekly TODOs\n\n## 🔴 Q1\n- [ ] **Ship alpha** #Dev\n\n## 🟡 Q2\n- [ ] **Review roadmap** #Ops\n\n## Calendar Meetings\n- [ ] **Team sync** meeting::123 status::scheduled #Ops\n- [x] **Retro** meeting::124 status::done #Ops\n"""
    )
    env = os.environ.copy()
    env["TASK_TRACKER_WORK_FILE"] = str(work)
    env["TASK_TRACKER_DAILY_NOTES_DIR"] = str(tmp_path)
    env["STANDUP_CALENDARS"] = "{}"

    proc = subprocess.run(
        ["python3", "scripts/standup.py", "--compact-json", "--skip-missed"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert payload["schema_version"] == "1"
    assert "dones" in payload
    assert "calendar_dos" in payload
    assert "calendar_dones" in payload
    assert "dos" in payload
    assert "links" in payload
    assert payload["dos"]
    assert payload["calendar_dones"]
