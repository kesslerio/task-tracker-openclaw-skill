from pathlib import Path
from types import SimpleNamespace
import json
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import tasks
from utils import parse_tasks, summarize_objective_progress
from test_objective_parser import OBJECTIVES_CONTENT


def test_cmd_objectives_text_output(monkeypatch, capsys):
    tasks_data = parse_tasks(OBJECTIVES_CONTENT)
    monkeypatch.setattr(
        tasks,
        "load_tasks",
        lambda personal=False: (OBJECTIVES_CONTENT, tasks_data),
    )

    tasks.cmd_objectives(SimpleNamespace(personal=False, json=False, at_risk=False))

    out = capsys.readouterr().out
    assert "Launch job posting" in out
    assert "66.7%" in out
    assert "#HR" in out
    assert "#high" in out
    assert "✅ Draft job description" in out
    assert "⬜ Post on LinkedIn" in out


def test_cmd_objectives_json_output(monkeypatch, capsys):
    tasks_data = parse_tasks(OBJECTIVES_CONTENT)
    monkeypatch.setattr(
        tasks,
        "load_tasks",
        lambda personal=False: (OBJECTIVES_CONTENT, tasks_data),
    )

    tasks.cmd_objectives(SimpleNamespace(personal=False, json=True, at_risk=False))

    output = json.loads(capsys.readouterr().out)
    assert isinstance(output, list)
    assert len(output) == 2

    launch = next(item for item in output if item["title"] == "Launch job posting")
    assert launch["department"] == "HR"
    assert launch["priority"] == "high"
    assert launch["total_tasks"] == 3
    assert launch["completed_tasks"] == 2
    assert launch["completion_pct"] == pytest.approx(66.7)
    assert launch["tasks"][0] == {"title": "Draft job description", "done": True}

    deal = next(item for item in output if item["title"] == "Close Fizzi deal")
    assert deal["total_tasks"] == 2
    assert deal["completed_tasks"] == 0
    assert deal["completion_pct"] == pytest.approx(0.0)


def test_cmd_objectives_at_risk_filter(monkeypatch, capsys):
    tasks_data = parse_tasks(OBJECTIVES_CONTENT)
    monkeypatch.setattr(
        tasks,
        "load_tasks",
        lambda personal=False: (OBJECTIVES_CONTENT, tasks_data),
    )

    tasks.cmd_objectives(SimpleNamespace(personal=False, json=False, at_risk=True))

    out = capsys.readouterr().out
    assert "Close Fizzi deal" in out
    assert "Launch job posting" not in out


def test_cmd_objectives_graceful_skip_legacy(monkeypatch, capsys):
    legacy_content = """## 🔴 Q1: Urgent & Important
- [ ] **Ship release**
"""
    tasks_data = parse_tasks(legacy_content, format="obsidian")
    monkeypatch.setattr(
        tasks,
        "load_tasks",
        lambda personal=False: (legacy_content, tasks_data),
    )

    tasks.cmd_objectives(SimpleNamespace(personal=False, json=False, at_risk=False))

    out = capsys.readouterr().out
    assert "Objective tracking is only available for Objectives format files." in out


def test_objective_progress_summary():
    tasks_data = parse_tasks(OBJECTIVES_CONTENT)
    summary = summarize_objective_progress(tasks_data)

    assert summary["total_objectives"] == 2
    assert summary["on_track_objectives"] == 1
    at_risk_titles = [item["title"] for item in summary["at_risk_objectives"]]
    assert at_risk_titles == ["Close Fizzi deal"]
