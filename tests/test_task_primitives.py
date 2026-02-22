import json
import os
import subprocess
from datetime import date, timedelta


def _write_work_file(tmp_path):
    work = tmp_path / "Weekly TODOs.md"
    work.write_text(
        """# Weekly TODOs

## 🔴 Q1
- [ ] **Ship alpha milestone** id::A-1 area:: Delivery
- [ ] **Fix login timeout** https://github.com/acme/proj/issues/42 area:: Platform

## 🟡 Q2
- [ ] **Write onboarding docs** area:: Docs
"""
    )
    return work


def _env(tmp_path, work):
    env = os.environ.copy()
    env["TASK_TRACKER_WORK_FILE"] = str(work)
    env["TASK_TRACKER_DAILY_NOTES_DIR"] = str(tmp_path)
    env["STANDUP_CALENDARS"] = "{}"
    return env


def test_primitive_schema_shape(tmp_path):
    work = _write_work_file(tmp_path)
    today_note = tmp_path / f"{date.today().isoformat()}.md"
    today_note.write_text("- 09:15 ✅ Ship alpha milestone\n")
    env = _env(tmp_path, work)

    standup = subprocess.run(
        ["python3", "scripts/tasks.py", "standup-summary"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert standup.returncode == 0
    standup_payload = json.loads(standup.stdout)
    assert standup_payload["schema_version"] == "v1"
    assert standup_payload["command"] == "standup-summary"
    assert "dones" in standup_payload
    assert "dos" in standup_payload
    assert "overdue" in standup_payload
    assert "carryover_suggestions" in standup_payload

    week_start = date.today() - timedelta(days=date.today().weekday())
    week_end = week_start + timedelta(days=6)
    weekly = subprocess.run(
        [
            "python3",
            "scripts/tasks.py",
            "weekly-review-summary",
            "--start",
            week_start.isoformat(),
            "--end",
            week_end.isoformat(),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert weekly.returncode == 0
    weekly_payload = json.loads(weekly.stdout)
    assert weekly_payload["schema_version"] == "v1"
    assert weekly_payload["command"] == "weekly-review-summary"
    assert "DONE" in weekly_payload
    assert "DO" in weekly_payload
    assert "by_area" in weekly_payload["DONE"]
    assert "by_category" in weekly_payload["DO"]

    cal = subprocess.run(
        ["python3", "scripts/tasks.py", "calendar-sync"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert cal.returncode == 0
    cal_payload = json.loads(cal.stdout)
    assert cal_payload["schema_version"] == "v1"
    assert cal_payload["command"] == "calendar-sync"
    assert "lifecycle_map" in cal_payload


def test_ingest_daily_log_plain_bullets_and_exact_matching(tmp_path):
    work = _write_work_file(tmp_path)
    env = _env(tmp_path, work)
    ingest_file = tmp_path / "done-log.md"
    ingest_file.write_text(
        """- Ship alpha milestone
- Fixed login timeout https://github.com/acme/proj/issues/42
"""
    )

    proc = subprocess.run(
        ["python3", "scripts/tasks.py", "ingest-daily-log", "--file", str(ingest_file)],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert payload["schema_version"] == "v1"
    assert payload["command"] == "ingest-daily-log"
    assert payload["totals"]["parsed_done_lines"] == 2
    assert payload["totals"]["auto_linked"] == 2
    assert payload["items"][0]["match_metadata"]["decision"] == "auto-link"
    assert payload["items"][0]["match_metadata"]["match_type"] in {"normalized-title", "exact-id-or-link"}
    assert payload["items"][1]["match_metadata"]["match_type"] == "exact-id-or-link"


def test_ingest_daily_log_checkbox_and_fuzzy_threshold_bands(tmp_path):
    work = _write_work_file(tmp_path)
    env = _env(tmp_path, work)

    proc = subprocess.run(
        [
            "python3",
            "scripts/tasks.py",
            "ingest-daily-log",
            "--auto-threshold",
            "0.98",
            "--review-threshold",
            "0.70",
        ],
        input="- [x] Writ onboarding docs\n- [x] Completely unrelated objective\n- [ ] Not done\n",
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert payload["totals"]["parsed_done_lines"] == 2
    decisions = [item["match_metadata"]["decision"] for item in payload["items"]]
    assert "needs-review" in decisions
    assert "no-match" in decisions
    first = payload["items"][0]["match_metadata"]
    assert first["score"] >= 0.70
    assert first["score"] < 0.98
    assert payload["items"][1]["match_metadata"]["matched_task_id"] is None
