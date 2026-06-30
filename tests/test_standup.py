import json
import os
import re
import shlex
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import standup
import cos_config
import utils


SCRIPTS = ROOT / "scripts"

WORK_BOARD = """# Work

## 🔴 Q1
- [ ] **Investigate payroll sync** https://github.com/acme/app/issues/42 task_id::tsk_exact area:: Ops

## ✅ Done
- [x] **User stated DONE** task_id::tsk_done area:: Ops
"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    work = tmp_path / "Work Tasks.md"
    work.write_text(WORK_BOARD)
    monkeypatch.setenv("TASK_MGMT_STATE_DIR", str(state_dir))
    monkeypatch.setenv("TASK_TRACKER_WORK_FILE", str(work))
    monkeypatch.setattr(utils, "OBSIDIAN_WORK", work)
    monkeypatch.delenv("STANDUP_CALENDARS", raising=False)
    monkeypatch.setattr(standup, "get_calendar_events", lambda trigger="calendar_fetch": {})
    monkeypatch.setattr(standup, "candidate_review_summary", lambda: {})
    monkeypatch.setattr(standup, "task_audit_summary", lambda limit=3: {})
    monkeypatch.setattr(standup, "tomorrow_pointer_line", lambda records=None: "No #1 set")
    return {"state_dir": state_dir, "work": work}


def _tasks_data():
    return {
        "done": [{"title": "User stated DONE", "area": "Ops", "raw_line": "- [x] **User stated DONE**"}],
        "due_today": [],
        "q1": [{"title": "Investigate payroll sync", "area": "Ops", "task_id": "tsk_exact"}],
        "q2": [],
        "q3": [],
        "team": [],
        "all": [],
    }


def test_rendered_priority_rows_are_deduplicated(env, monkeypatch):
    monkeypatch.setattr(
        standup,
        "_standup_harvest_result",
        lambda date_str, *, trigger, resolved_window=None: {
            "evidence_candidates": [],
            "health": {},
            "window": resolved_window.as_dict() if resolved_window else None,
        },
    )
    duplicated = {
        "done": [],
        "due_today": [],
        "q1": [],
        "q2": [
            {"title": "Deduplicate me", "area": "Ops", "task_id": "tsk_dupe"},
            {"title": "Deduplicate me", "area": "Ops", "task_id": "tsk_dupe"},
        ],
        "q3": [],
        "team": [],
        "all": [],
    }

    text = standup.generate_standup(
        date_str="2026-06-23",
        tasks_data=duplicated,
        capacity_records=[],
    )

    assert text.count("Deduplicate me") == 1


def _candidate():
    return {
        "schema_version": 1,
        "source": "github",
        "source_type": "github",
        "kind": "activity",
        "provider_id": "acme/app#42",
        "provider_state": "merged:sha-1:merged",
        "evidence_hash": "sha256:github:test",
        "occurred_at": "2026-06-23T10:00:00-07:00",
        "match_title": "Fix payroll sync #42",
        "title": "Fix payroll sync #42 [acme/app#42]",
        "url": "https://github.com/acme/app/pull/42",
        "match": {"decision": "evidence-link"},
        "auto_done_eligible": True,
        "decision": "evidence-link",
        "matched_task_id": "tsk_exact",
        "suggested_task_id": "tsk_exact",
        "association_status": "auto-associated",
    }


def _candidate_for_confirmed_done():
    candidate = _candidate()
    candidate.update(
        {
            "match_title": "User stated DONE",
            "title": "Merged PR title that should not overwrite the user claim",
            "evidence_hash": "sha256:github:done",
            "matched_task_id": "tsk_done",
            "suggested_task_id": "tsk_done",
            "match": {
                "decision": "evidence-link",
                "match_type": "exact-id-or-link",
                "matched_task_id": "tsk_done",
                "suggested_task_id": "tsk_done",
            },
        }
    )
    return candidate


def _write_fake_harvest_tools(bin_dir: Path) -> None:
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text("#!/usr/bin/env bash\nprintf '[]\\n'\n")
    gh.chmod(0o755)
    gog = bin_dir / "gog"
    gog.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \"$1\" == \"gmail\" ]]; then\n"
        "  printf '{\"threads\": []}\\n'\n"
        "else\n"
        "  printf '{\"events\": []}\\n'\n"
        "fi\n"
    )
    gog.chmod(0o755)


def _standup_subprocess_env(tmp_path: Path, *, state_name: str) -> dict[str, str]:
    state_dir = tmp_path / state_name
    work = tmp_path / f"{state_name}-Work Tasks.md"
    daily = tmp_path / f"{state_name}-daily"
    fake_bin = tmp_path / "fake-bin"
    if not fake_bin.exists():
        _write_fake_harvest_tools(fake_bin)
    daily.mkdir(exist_ok=True)
    work.write_text(WORK_BOARD)

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
            "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
            "TASK_MGMT_STATE_DIR": str(state_dir),
            "TASK_TRACKER_WORK_FILE": str(work),
            "TASK_TRACKER_DAILY_NOTES_DIR": str(daily),
            "TASK_TRACKER_DONE_LOG_DIR": str(daily),
            "TASK_TRACKER_LEDGER_FILE": str(state_dir / "events.jsonl"),
            "TASK_TRACKER_ERROR_LOG": str(state_dir / "errors.jsonl"),
            "STANDUP_CALENDARS": "{}",
            "STANDUP_SUMMARIZER_ENABLED": "0",
            "COS_TIMEZONE": "America/Los_Angeles",
        }
    )
    return env


def _run_entrypoint(argv: list[str], env: dict[str, str]) -> bytes:
    proc = subprocess.run(
        argv,
        cwd=ROOT,
        env=env,
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", errors="replace")
    return proc.stdout


def _health_sources(env: dict[str, str]) -> dict:
    health_path = Path(env["TASK_MGMT_STATE_DIR"]) / "cos-health.json"
    return json.loads(health_path.read_text())["standup"]["sources"]


def test_evidence_candidates_do_not_change_completed_bytes(env, monkeypatch):
    monkeypatch.setattr(
        standup,
        "_standup_harvest_result",
        lambda date_str, *, trigger, resolved_window=None: {"evidence_candidates": [], "health": {}, "window": None},
    )
    before = standup.generate_standup(
        date_str="2026-06-23",
        json_output=True,
        tasks_data=_tasks_data(),
        capacity_records=[],
    )
    completed_before = json.dumps(before["completed"], sort_keys=True)

    monkeypatch.setattr(
        standup,
        "_standup_harvest_result",
        lambda date_str, *, trigger, resolved_window=None: {
            "evidence_candidates": [_candidate()],
            "health": {"github": {"status": "ok"}},
            "window": {"window_id": "2026-W26:2026-06-23:standup"},
            "run_id": "run-1",
        },
    )
    after = standup.generate_standup(
        date_str="2026-06-23",
        json_output=True,
        tasks_data=_tasks_data(),
        capacity_records=[],
    )

    assert json.dumps(after["completed"], sort_keys=True) == completed_before
    assert after["evidence_candidates"] == [_candidate()]
    assert after["evidence_harvest"]["health"]["github"]["status"] == "ok"


def test_typed_standup_and_cron_descriptor_resolve_to_byte_identical_compact_json(tmp_path):
    structured_args = ["--compact-json", "--skip-missed", "--date", "2026-06-23"]
    typed_argv = ["bash", str(SCRIPTS / "telegram-commands.sh"), "daily", *structured_args]

    desc = standup.standup_cron_descriptor(scripts_dir=str(SCRIPTS))
    assert desc["payload"]["kind"] == "command"
    cron_argv = list(desc["payload"]["argv"])
    assert cron_argv[:2] == ["sh", "-c"]
    assert "bash telegram-commands.sh daily" in cron_argv[2]
    assert "--cron" not in cron_argv[2]
    cron_argv[2] = f"{cron_argv[2]} {' '.join(shlex.quote(arg) for arg in structured_args)}"

    serialised = json.dumps(desc, sort_keys=True)
    assert desc["delivery"]["chat_id_env"] == "TELEGRAM_CHAT_ID_PRODUCTIVITY"
    assert desc["delivery"]["topic_env"] == "OPENCLAW_TOPIC_PRODUCTIVITY_STANDUP"
    assert "TELEGRAM_CHAT_ID_PRODUCTIVITY" in serialised
    assert "OPENCLAW_TOPIC_PRODUCTIVITY_STANDUP" in serialised
    assert not re.search(r"-100\d{8,}", serialised)
    assert "-4242424242" not in serialised

    typed_env = _standup_subprocess_env(tmp_path, state_name="typed-state")
    cron_env = _standup_subprocess_env(tmp_path, state_name="cron-state")
    typed_once = _run_entrypoint(typed_argv, typed_env)
    cron_once = _run_entrypoint(cron_argv, cron_env)
    assert typed_once == cron_once

    payload = json.loads(typed_once)
    assert payload["schema_version"] == "1"
    assert payload["dos"][0]["title"] == "Investigate payroll sync"
    assert payload["evidence_candidates"] == []

    expected_sources = {"github", "gmail", "calendar", "dialpad_sms"}
    for run_env in (typed_env, cron_env):
        sources = _health_sources(run_env)
        assert set(sources) == expected_sources
        assert all(receipt["status"] == "ok" for receipt in sources.values())
        assert {receipt["trigger"] for receipt in sources.values()} == {"user_command:/standup"}

    typed_twice = _run_entrypoint(typed_argv, typed_env)
    assert typed_twice == typed_once


def test_matching_evidence_enriches_confirmed_done_and_is_not_rendered_twice(env, monkeypatch):
    candidate = _candidate_for_confirmed_done()
    monkeypatch.setattr(
        standup,
        "_standup_harvest_result",
        lambda date_str, *, trigger, resolved_window=None: {
            "evidence_candidates": [candidate],
            "health": {"github": {"status": "ok"}},
            "window": {"window_id": "2026-W26:2026-06-23:standup"},
            "run_id": "run-1",
        },
    )

    output = standup.generate_standup(
        date_str="2026-06-23",
        json_output=True,
        tasks_data=_tasks_data(),
        capacity_records=[],
    )

    assert output["evidence_candidates"] == []
    assert len(output["completed"]) == 1
    assert output["completed"][0]["title"] == "User stated DONE"
    assert output["completed"][0]["provenance"][1]["source"] == "github"
    assert output["completed"][0]["provenance"][1]["evidence_hash"] == "sha256:github:done"


def test_harvested_candidates_render_in_read_only_section(env, monkeypatch):
    monkeypatch.setattr(
        standup,
        "_standup_harvest_result",
        lambda date_str, *, trigger, resolved_window=None: {
            "evidence_candidates": [_candidate()],
            "health": {"github": {"status": "ok"}},
            "window": {"window_id": "2026-W26:2026-06-23:standup"},
            "run_id": "run-1",
        },
    )

    text = standup.generate_standup(
        date_str="2026-06-23",
        json_output=False,
        tasks_data=_tasks_data(),
        capacity_records=[],
    )

    assert "Evidence Candidates" in text
    assert "Fix payroll sync #42" in text
    assert "Recently Completed" in text
    assert "User stated DONE" in text


def test_draft_summary_renders_read_only_and_does_not_change_completed(env, monkeypatch):
    summary = {
        "bullets": [
            {
                "evidence_id": "sha256:github:test",
                "area": "eng",
                "bullet": "Shipped payroll sync fix",
            }
        ],
        "translated": True,
        "model": "qwen3-coder-next:cloud",
        "prompt_version": "test",
        "disclosure": None,
        "draft": True,
        "confirmed": False,
    }
    monkeypatch.setattr(
        standup,
        "_standup_harvest_result",
        lambda date_str, *, trigger, resolved_window=None: {
            "evidence_candidates": [_candidate()],
            "summary": summary,
            "health": {"github": {"status": "ok"}},
            "window": {"window_id": "2026-W26:2026-06-23:standup"},
            "run_id": "run-1",
        },
    )

    output = standup.generate_standup(
        date_str="2026-06-23",
        json_output=True,
        tasks_data=_tasks_data(),
        capacity_records=[],
    )
    text = standup.generate_standup(
        date_str="2026-06-23",
        json_output=False,
        tasks_data=_tasks_data(),
        capacity_records=[],
    )

    assert output["completed"] == _tasks_data()["done"]
    assert output["evidence_harvest"]["summary"] == summary
    assert "Draft summary (unconfirmed)" in text
    assert "Shipped payroll sync fix" in text
    assert "Read-only draft; not recorded as completed." in text


def test_tuesday_standup_label_and_week_are_from_pacific_window(env, monkeypatch):
    captured = {}

    def harvest(date_str, *, trigger, resolved_window=None):
        captured["date_str"] = date_str
        captured["resolved_window"] = resolved_window
        return {"evidence_candidates": [], "health": {}, "window": resolved_window.as_dict()}

    monkeypatch.setattr(cos_config, "local_now", lambda: datetime.fromisoformat("2026-06-23T00:05:00-07:00"))
    monkeypatch.setattr(standup, "_standup_harvest_result", harvest)

    output = standup.generate_standup(
        json_output=True,
        tasks_data=_tasks_data(),
        capacity_records=[],
    )

    assert output["date"] == "2026-06-23"
    assert output["date_display"] == "Tuesday, June 23"
    assert output["week_id"] == "2026-W26"
    assert output["window_id"] == "2026-W26:2026-06-23:standup"
    assert output["standup_window"]["evidence_date"] == "2026-06-22"
    assert output["evidence_harvest"]["window"]["window_id"] == output["window_id"]
    assert captured["date_str"] is None
    assert captured["resolved_window"].plan_date == date(2026, 6, 23)


def test_explicit_target_date_controls_label_week_and_harvest_window(env, monkeypatch):
    captured = {}

    def harvest(date_str, *, trigger, resolved_window=None):
        captured["date_str"] = date_str
        captured["resolved_window"] = resolved_window
        return {"evidence_candidates": [], "health": {}, "window": resolved_window.as_dict()}

    monkeypatch.setattr(cos_config, "local_now", lambda: datetime.fromisoformat("2030-01-01T08:00:00-08:00"))
    monkeypatch.setattr(standup, "_standup_harvest_result", harvest)

    output = standup.generate_standup(
        date_str="2021-01-04",
        json_output=True,
        tasks_data=_tasks_data(),
        capacity_records=[],
    )

    assert output["date_display"] == "Monday, January 04"
    assert output["week_id"] == "2021-W01"
    assert output["window_id"] == "2021-W01:2021-01-04:standup"
    assert output["standup_window"]["evidence_date"] == "2021-01-04"
    assert captured["date_str"] == "2021-01-04"
    assert captured["resolved_window"].week_id == "2021-W01"


@pytest.mark.parametrize(
    ("target", "expected_label", "expected_week"),
    [
        ("2026-03-09", "Monday, March 09", "2026-W11"),
        ("2026-11-02", "Monday, November 02", "2026-W45"),
        ("2020-12-31", "Thursday, December 31", "2020-W53"),
        ("2021-01-01", "Friday, January 01", "2020-W53"),
    ],
)
def test_standup_labels_dst_and_iso_week_boundaries(env, monkeypatch, target, expected_label, expected_week):
    monkeypatch.setattr(
        standup,
        "_standup_harvest_result",
        lambda date_str, *, trigger, resolved_window=None: {
            "evidence_candidates": [],
            "health": {},
            "window": resolved_window.as_dict(),
        },
    )

    output = standup.generate_standup(
        date_str=target,
        json_output=True,
        tasks_data=_tasks_data(),
        capacity_records=[],
    )

    assert output["date_display"] == expected_label
    assert output["week_id"] == expected_week
    assert output["evidence_harvest"]["window"]["week_id"] == expected_week


def test_harvest_degrade_still_exposes_deterministic_week_window(env, monkeypatch):
    monkeypatch.setattr(
        standup,
        "_standup_harvest_result",
        lambda date_str, *, trigger, resolved_window=None: {
            "evidence_candidates": [],
            "health": {"status": "failed"},
            "window": None,
        },
    )

    output = standup.generate_standup(
        date_str="2026-06-23",
        json_output=True,
        tasks_data=_tasks_data(),
        capacity_records=[],
    )

    assert output["date_display"] == "Tuesday, June 23"
    assert output["week_id"] == "2026-W26"
    assert output["evidence_harvest"]["window"]["window_id"] == "2026-W26:2026-06-23:standup"


def test_invalid_date_string_uses_implicit_prior_workday_window(env, monkeypatch):
    # A typo'd date (/standup 2026-99-99) must NOT silently retarget the evidence
    # window to today; it falls through to the implicit prior-workday standup window.
    captured = {}

    def harvest(date_str, *, trigger, resolved_window=None):
        captured["resolved_window"] = resolved_window
        return {"evidence_candidates": [], "health": {}, "window": resolved_window.as_dict()}

    # Tuesday 2026-06-23 Pacific -> implicit evidence window = Monday 2026-06-22.
    monkeypatch.setattr(cos_config, "local_now", lambda: datetime.fromisoformat("2026-06-23T08:00:00-07:00"))
    monkeypatch.setattr(standup, "_standup_harvest_result", harvest)

    output = standup.generate_standup(
        date_str="2026-99-99",
        json_output=True,
        tasks_data=_tasks_data(),
        capacity_records=[],
    )

    assert output["date_display"] == "Tuesday, June 23"
    assert output["standup_window"]["plan_date"] == "2026-06-23"
    # The implicit window summarizes the PRIOR workday, not today (the bug would be today).
    assert output["standup_window"]["evidence_date"] == "2026-06-22"
    assert captured["resolved_window"].evidence_date.isoformat() == "2026-06-22"


_PUSHBACK_OVER_CAP_BOARD = """# Work

## 🔴 Q1
- [ ] **Ship payroll sync** 🗓️2026-05-01 estimate:: 13h task_id::tsk_aaaaaaaaaaaaaaaa
- [ ] **Fix onboarding** 🗓️2026-06-20 estimate:: 13h task_id::tsk_bbbbbbbbbbbbbbbb

## 🅿️ Parking Lot
"""


def test_over_cap_standup_renders_capacity_pushback_after_capacity_line(env, monkeypatch):
    from task_records import task_records

    over_cap = task_records(_PUSHBACK_OVER_CAP_BOARD)
    monkeypatch.setattr(cos_config, "local_today", lambda: date(2026, 6, 24))
    monkeypatch.setattr(cos_config, "local_now", lambda: datetime.fromisoformat("2026-06-24T08:00:00-07:00"))
    monkeypatch.setattr(
        standup, "_standup_harvest_result",
        lambda *a, **k: {"evidence_candidates": [], "health": {}, "window": None},
    )

    out = standup.generate_standup(json_output=True, tasks_data=_tasks_data(), capacity_records=over_cap)
    assert out["capacity_pushback"] is not None
    assert "Cut / defer / edit" in out["capacity_pushback"]
    assert "Ship payroll sync" in out["capacity_pushback"]

    text = standup.generate_standup(tasks_data=_tasks_data(), capacity_records=over_cap)
    assert out["capacity"] in text
    # The push-back is rendered AFTER the capacity line.
    assert text.index(out["capacity"]) < text.index("Cut / defer / edit")


def test_under_cap_standup_has_no_pushback(env, monkeypatch):
    monkeypatch.setattr(
        standup, "_standup_harvest_result",
        lambda *a, **k: {"evidence_candidates": [], "health": {}, "window": None},
    )
    out = standup.generate_standup(json_output=True, tasks_data=_tasks_data(), capacity_records=[])
    assert out["capacity_pushback"] is None


def test_pushback_renders_when_capacity_records_not_supplied(env, monkeypatch):
    # The live /standup CLI calls generate_standup WITHOUT capacity_records (None);
    # the push-back must load the work board itself, not silently no-op (regression guard).
    env["work"].write_text(_PUSHBACK_OVER_CAP_BOARD)
    monkeypatch.setattr(cos_config, "local_today", lambda: date(2026, 6, 24))
    monkeypatch.setattr(cos_config, "local_now", lambda: datetime.fromisoformat("2026-06-24T08:00:00-07:00"))
    monkeypatch.setattr(
        standup, "_standup_harvest_result",
        lambda *a, **k: {"evidence_candidates": [], "health": {}, "window": None},
    )
    out = standup.generate_standup(json_output=True, tasks_data=_tasks_data())  # no capacity_records
    assert out["capacity_pushback"] is not None
    assert "Ship payroll sync" in out["capacity_pushback"]
