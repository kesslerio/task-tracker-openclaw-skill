import json
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import standup
import cos_config
import utils


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
