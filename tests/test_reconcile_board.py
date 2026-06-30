import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import utils  # noqa: E402
from reconcile_board import reconcile_board  # noqa: E402


CANONICAL_HEADERS = (
    "## 🔴 Q1: Urgent & Important",
    "## 🟡 Q2: Important, Not Urgent",
    "## 🟠 Q3: Waiting / Blocked",
    "## 👥 Team Tasks",
    "## ⚪ Backlog",
)


def _done_event(task_id: str, title: str) -> dict:
    return {
        "event_type": "state_transition",
        "task_id": task_id,
        "previous_state": "active",
        "next_state": "done",
        "metadata": {"title": title},
    }


def _assert_single_canonical_board(content: str) -> None:
    assert "## 📋 All Tasks" not in content
    for header in CANONICAL_HEADERS:
        assert content.count(header) == 1


def _titles(tasks: dict, bucket: str) -> set[str]:
    return {task["title"] for task in tasks[bucket]}


def _active_title_keys(content: str) -> list[str]:
    parsed = utils.parse_tasks(content)
    return [task["title"].casefold() for task in parsed["all"] if not task["done"]]


def test_reconcile_collapses_dual_board_strikes_closed_and_reports_actions(tmp_path, monkeypatch):
    board = """# Weekly TODOs — 2026-W25

## 🔴 Q1
- [ ] **Ship launch checklist** task_id::tsk_launch area:: Ops 🗓️2026-07-01 🔺
- [ ] **Closed milestone** task_id::tsk_closed area:: Ops

## 🟡 Q2
- [ ] **Draft customer memo** task_id::tsk_memo area:: GTM 🗓️2026-07-02

## 🟠 Q3
- [ ] **Waiting on procurement** area:: Ops 🗓️2026-07-03

## 📋 All Tasks
- [ ] **Ship launch checklist**
- [ ] **Closed bare ghost**
"""
    result = reconcile_board(
        board,
        [
            _done_event("tsk_closed", "Closed milestone"),
            _done_event("tsk_closed_bare", "Closed bare ghost"),
        ],
        target_date="2026-06-29",
    )

    assert result.content.startswith("# Weekly TODOs — 2026-W27")
    _assert_single_canonical_board(result.content)
    assert "Closed milestone" not in result.content
    assert "Closed bare ghost" not in result.content
    assert result.content.count("Ship launch checklist") == 1
    assert "- [ ] **Ship launch checklist** task_id::tsk_launch area:: Ops 🗓️2026-07-01 🔺" in result.content
    assert "- [ ] **Draft customer memo** task_id::tsk_memo area:: GTM 🗓️2026-07-02" in result.content
    assert "- [ ] **Waiting on procurement** area:: Ops 🗓️2026-07-03" in result.content
    assert "task_id::tsk_memo" in result.content
    assert '<!-- repair: missing task_id:: for "Waiting on procurement" -->' in result.content

    q1_start = result.content.index("## 🔴 Q1: Urgent & Important")
    q2_start = result.content.index("## 🟡 Q2: Important, Not Urgent")
    q3_start = result.content.index("## 🟠 Q3: Waiting / Blocked")
    team_start = result.content.index("## 👥 Team Tasks")
    assert q1_start < result.content.index("Ship launch checklist") < q2_start
    assert q2_start < result.content.index("Draft customer memo") < q3_start
    assert q3_start < result.content.index("Waiting on procurement") < team_start

    report = result.report
    assert [item["title"] for item in report["struck_closed"]] == ["Closed milestone", "Closed bare ghost"]
    merge = report["merged_duplicates"][0]
    assert merge["title"] == "Ship launch checklist"
    assert merge["kept"]["task_id"] == "tsk_launch"
    assert merge["dropped"]["raw_line"] == "- [ ] **Ship launch checklist**"
    assert report["still_missing_task_id"][0]["title"] == "Waiting on procurement"

    assert len(_active_title_keys(result.content)) == len(set(_active_title_keys(result.content)))
    rolled = tmp_path / "Work Tasks.md"
    rolled.write_text(result.content, encoding="utf-8")
    monkeypatch.setattr(utils, "get_tasks_file", lambda personal=False, force_legacy=False: (rolled, "obsidian"))
    _content, tasks = utils.load_tasks()
    assert "Ship launch checklist" in _titles(tasks, "q1")
    assert "Draft customer memo" in _titles(tasks, "q2")
    assert "Waiting on procurement" in _titles(tasks, "q3")


def test_reconcile_dry_run_cli_writes_nothing(tmp_path):
    work = tmp_path / "Weekly TODOs.md"
    ledger = tmp_path / "Weekly TODOs.md.events.jsonl"
    original = """# Weekly TODOs — 2026-W25

## 🔴 Q1
- [ ] **Open task** task_id::tsk_open area:: Ops
- [ ] **Closed task** task_id::tsk_closed area:: Ops
"""
    work.write_text(original, encoding="utf-8")
    ledger.write_text(json.dumps(_done_event("tsk_closed", "Closed task")) + "\n", encoding="utf-8")
    env = os.environ.copy()
    env.update(
        {
            "TASK_TRACKER_WORK_FILE": str(work),
            "TASK_TRACKER_LEDGER_FILE": str(ledger),
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "scripts"),
        }
    )

    proc = subprocess.run(
        ["python3", "scripts/reconcile_board.py", "--date", "2026-06-29"],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert work.read_text(encoding="utf-8") == original
    assert "# Weekly TODOs — 2026-W27" in proc.stdout
    assert "Closed task" not in proc.stdout.split("--- reconcile report ---", 1)[0]
    assert '"applied": false' in proc.stdout


def test_reconcile_apply_cli_writes_cleaned_board(tmp_path):
    work = tmp_path / "Weekly TODOs.md"
    ledger = tmp_path / "Weekly TODOs.md.events.jsonl"
    work.write_text(
        """# Weekly TODOs — 2026-W25

## 🔴 Q1
- [ ] **Open task** task_id::tsk_open area:: Ops
- [ ] **Closed task** task_id::tsk_closed area:: Ops
""",
        encoding="utf-8",
    )
    ledger.write_text(json.dumps(_done_event("tsk_closed", "Closed task")) + "\n", encoding="utf-8")
    env = os.environ.copy()
    env.update(
        {
            "TASK_TRACKER_WORK_FILE": str(work),
            "TASK_TRACKER_LEDGER_FILE": str(ledger),
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "scripts"),
        }
    )

    proc = subprocess.run(
        ["python3", "scripts/reconcile_board.py", "--apply", "--date", "2026-06-29"],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["applied"] is True
    updated = work.read_text(encoding="utf-8")
    assert "Open task" in updated
    assert "Closed task" not in updated
    _assert_single_canonical_board(updated)


def test_reconcile_keeps_non_duplicate_similar_titles():
    board = """# Weekly TODOs — 2026-W25

## 🔴 Q1
- [ ] **Review vendor contract** task_id::tsk_contract area:: Legal

## 📋 All Tasks
- [ ] **Review vendor contract draft** area:: Legal
"""

    result = reconcile_board(board, [], target_date="2026-06-29")

    assert "Review vendor contract" in result.content
    assert "Review vendor contract draft" in result.content
    assert result.report["merged_duplicates"] == []
