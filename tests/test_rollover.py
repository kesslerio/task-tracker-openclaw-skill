import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import utils  # noqa: E402
from rollover import rollover_board  # noqa: E402
from task_records import repair_hint  # noqa: E402

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


def _reverted_event(task_id: str, title: str) -> dict:
    return {
        "event_type": "state_transition_reverted",
        "task_id": task_id,
        "metadata": {"title": title},
    }


def _assert_single_canonical_board(content: str) -> None:
    assert "## 📋 All Tasks" not in content
    for header in CANONICAL_HEADERS:
        assert content.count(header) == 1


def _titles(tasks: dict, bucket: str) -> set[str]:
    return {task["title"] for task in tasks[bucket]}


def test_rollover_drops_ledger_closed_ghosts_and_emits_sectioned_board():
    board = """# Weekly TODOs — 2026-W25

## 🔴 Q1
- [ ] **Keep launch checklist** task_id::tsk_keep area:: Ops 🗓️2026-06-30 🔺
- [ ] **Closed milestone** task_id::tsk_closed area:: Ops
- [ ] **Closed bare ghost**

## 📋 All Tasks
- [ ] **Keep launch checklist** task_id::tsk_keep area:: Ops 🗓️2026-06-30 🔺
- [ ] **Closed bare ghost**
"""
    result = rollover_board(
        board,
        [
            _done_event("tsk_closed", "Closed milestone"),
            _done_event("tsk_closed_bare", "Closed bare ghost"),
        ],
        target_date="2026-06-29",
    )

    assert result.content.startswith("# Weekly TODOs — 2026-W27")
    assert "Closed milestone" not in result.content
    assert "Closed bare ghost" not in result.content
    assert result.content.count("task_id::tsk_keep") == 1
    assert "- [ ] **Keep launch checklist** task_id::tsk_keep area:: Ops 🗓️2026-06-30 🔺" in result.content
    _assert_single_canonical_board(result.content)
    q1_start = result.content.index("## 🔴 Q1: Urgent & Important")
    q2_start = result.content.index("## 🟡 Q2: Important, Not Urgent")
    assert q1_start < result.content.index("Keep launch checklist") < q2_start


def test_rollover_round_trips_priority_sections_through_load_tasks(tmp_path, monkeypatch):
    board = """# Weekly TODOs — 2026-W25

## 🔴 Q1: Urgent & Important
- [ ] **Escalate incident** task_id::tsk_q1 area:: Ops

## 🟡 Q2: Important, Not Urgent
- [ ] **Plan migration** task_id::tsk_q2 area:: Platform

## 🟠 Q3: Waiting / Blocked
- [ ] **Wait for vendor** task_id::tsk_q3 area:: Vendor
"""
    result = rollover_board(board, [], target_date="2026-06-29")
    rolled = tmp_path / "Work Tasks.md"
    rolled.write_text(result.content, encoding="utf-8")
    monkeypatch.setattr(utils, "get_tasks_file", lambda personal=False, force_legacy=False: (rolled, "obsidian"))

    _content, tasks = utils.load_tasks()

    assert "Escalate incident" in _titles(tasks, "q1")
    assert "Plan migration" in _titles(tasks, "q2")
    assert "Wait for vendor" in _titles(tasks, "q3")
    assert "Escalate incident" not in _titles(tasks, "q2") | _titles(tasks, "q3")
    assert "Plan migration" not in _titles(tasks, "q1") | _titles(tasks, "q3")
    assert "Wait for vendor" not in _titles(tasks, "q1") | _titles(tasks, "q2")


def test_rollover_is_idempotent_on_its_own_output():
    board = """# Weekly TODOs — 2026-W25

## 🔴 Q1
- [ ] **Keep launch checklist** task_id::tsk_keep area:: Ops 🗓️2026-06-30 🔺
- [ ] **Keep launch checklist** task_id::tsk_keep area:: Ops 🗓️2026-06-30 🔺
"""
    first = rollover_board(board, [], target_date="2026-06-29")
    second = rollover_board(first.content, [], target_date="2026-06-29")

    assert second.content == first.content
    _assert_single_canonical_board(first.content)


def test_rollover_keeps_same_title_tasks_with_distinct_task_ids_and_suppresses_bare_duplicate():
    board = """# Weekly TODOs — 2026-W25

## 🔴 Q1
- [ ] **Follow up with client** area:: Sales task_id::tsk_aaaa
- [ ] **Follow up with client** area:: Success task_id::tsk_bbbb
- [ ] **Follow up with client** area:: Bare
"""
    result = rollover_board(board, [], target_date="2026-06-29")

    assert "task_id::tsk_aaaa" in result.content
    assert "task_id::tsk_bbbb" in result.content
    assert "area:: Bare" not in result.content
    assert result.content.count("Follow up with client") == 2


def test_rollover_advances_checked_recurring_task_once():
    board = """# Weekly TODOs — 2026-W21

## 🟡 Q2: Important, Not Urgent
- [x] **Send weekly update** task_id::tsk_weekly recur::weekly 🗓️2026-05-20
"""
    result = rollover_board(
        board,
        [_done_event("tsk_weekly", "Send weekly update")],
        target_date="2026-05-26",
    )

    assert "- [ ] **Send weekly update** task_id::tsk_weekly recur::weekly 🗓️2026-05-27" in result.content
    q2_start = result.content.index("## 🟡 Q2: Important, Not Urgent")
    q3_start = result.content.index("## 🟠 Q3: Waiting / Blocked")
    assert q2_start < result.content.index("Send weekly update") < q3_start
    rerun = rollover_board(result.content, [_done_event("tsk_weekly", "Send weekly update")], target_date="2026-05-26")
    assert rerun.content == result.content


def test_rollover_keeps_unsupported_recurring_task_open_and_reports_skip():
    board = """# Weekly TODOs — 2026-W25

## 🟡 Q2: Important, Not Urgent
- [x] **Review vendor access** task_id::tsk_bad_recur recur:: every 2 weeks 🗓️2026-06-10
"""
    result = rollover_board(
        board,
        [_done_event("tsk_bad_recur", "Review vendor access")],
        target_date="2026-06-29",
    )

    assert "- [ ] **Review vendor access** task_id::tsk_bad_recur recur:: every 2 weeks 🗓️2026-06-10" in result.content
    assert result.skipped_recur_errors == (
        {
            "task_id": "tsk_bad_recur",
            "title": "Review vendor access",
            "recur": "every 2 weeks",
            "error": "unsupported recurrence pattern: every 2 weeks",
        },
    )


def test_rollover_carries_missing_task_id_and_flags_for_repair():
    board = """# Weekly TODOs — 2026-W25

## 🟠 Q3: Waiting / Blocked
- [ ] **Bare open task** area:: Ops 🗓️2026-06-30
"""
    result = rollover_board(board, [], target_date="2026-06-29")

    assert "- [ ] **Bare open task** area:: Ops 🗓️2026-06-30" in result.content
    assert "task_id::tsk_" not in result.content
    assert repair_hint("Bare open task") in result.content
    assert result.missing_task_ids[0]["title"] == "Bare open task"
    q3_start = result.content.index("## 🟠 Q3: Waiting / Blocked")
    team_start = result.content.index("## 👥 Team Tasks")
    assert q3_start < result.content.index("Bare open task") < team_start


def test_rollover_does_not_report_valid_task_id_as_missing():
    board = """# Weekly TODOs — 2026-W25

## 🔴 Q1
- [ ] **Has valid id** task_id::tsk_marker area:: Ops
"""
    result = rollover_board(board, [], target_date="2026-06-29")

    assert "- [ ] **Has valid id** task_id::tsk_marker area:: Ops" in result.content
    assert "<!-- repair:" not in result.content
    assert result.missing_task_ids == ()


def test_rollover_reports_malformed_task_id_marker_for_repair():
    raw_line = "- [ ] **Malformed marker** task_id:: area:: Ops"
    board = f"""# Weekly TODOs — 2026-W25

## 🔴 Q1
{raw_line}
"""
    result = rollover_board(board, [], target_date="2026-06-29")

    assert raw_line in result.content
    assert repair_hint("Malformed marker") in result.content
    assert result.missing_task_ids == (
        {
            "line_number": 4,
            "title": "Malformed marker",
            "raw_line": raw_line,
        },
    )


def test_rollover_preserves_parking_lot_section_and_does_not_backlog_parked_tasks():
    board = """# Weekly TODOs — 2026-W25

## 🔴 Q1
- [ ] **Active task** task_id::tsk_active area:: Ops

## 🅿️ Parking Lot
- [ ] **Parked task** created::2026-06-01 task_id::tsk_parked

## ⚪ Backlog
- [ ] **Backlog task** task_id::tsk_backlog
"""
    result = rollover_board(board, [], target_date="2026-06-29")

    _assert_single_canonical_board(result.content)
    assert "## 🅿️ Parking Lot" in result.content
    parking_start = result.content.index("## 🅿️ Parking Lot")
    parked_start = result.content.index("Parked task")
    assert parking_start < parked_start
    backlog_start = result.content.index("## ⚪ Backlog")
    if backlog_start < parking_start:
        assert "Parked task" not in result.content[backlog_start:parking_start]
    assert "- [ ] **Parked task** created::2026-06-01 task_id::tsk_parked" in result.content
    assert "- [ ] **Backlog task** task_id::tsk_backlog" in result.content


def test_rollover_reverted_closed_event_reopens_task():
    board = """# Weekly TODOs — 2026-W25

## 🔴 Q1
- [ ] **Reopened task** task_id::tsk_reopened area:: Ops
"""
    result = rollover_board(
        board,
        [_done_event("tsk_reopened", "Reopened task"), _reverted_event("tsk_reopened", "Reopened task")],
        target_date="2026-06-29",
    )

    assert "task_id::tsk_reopened" in result.content
    assert result.excluded_closed == ()


def test_tasks_rollover_cli_writes_tmp_board_only(tmp_path):
    work = tmp_path / "Weekly TODOs.md"
    ledger = tmp_path / "Weekly TODOs.md.events.jsonl"
    work.write_text(
        """# Weekly TODOs — 2026-W25

## 🔴 Q1
- [ ] **Open task** task_id::tsk_open area:: Ops
- [ ] **Done task** task_id::tsk_done area:: Ops
""",
        encoding="utf-8",
    )
    ledger.write_text(json.dumps(_done_event("tsk_done", "Done task")) + "\n", encoding="utf-8")
    env = os.environ.copy()
    env.update(
        {
            "TASK_TRACKER_WORK_FILE": str(work),
            "TASK_TRACKER_LEDGER_FILE": str(ledger),
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "scripts"),
        }
    )

    proc = subprocess.run(
        ["python3", "scripts/tasks.py", "rollover", "--date", "2026-06-29"],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["ok"] is True
    assert payload["week_id"] == "2026-W27"
    updated = work.read_text(encoding="utf-8")
    assert "Open task" in updated
    assert "Done task" not in updated
    assert updated.startswith("# Weekly TODOs — 2026-W27")
    _assert_single_canonical_board(updated)


def test_rollover_dry_run_writes_nothing(tmp_path):
    work = tmp_path / "Weekly TODOs.md"
    ledger = tmp_path / "Weekly TODOs.md.events.jsonl"
    original = """# Weekly TODOs — 2026-W25

## 🔴 Q1
- [ ] **Open task** task_id::tsk_open area:: Ops
- [ ] **Done task** task_id::tsk_done area:: Ops
"""
    work.write_text(original, encoding="utf-8")
    ledger.write_text(json.dumps(_done_event("tsk_done", "Done task")) + "\n", encoding="utf-8")
    env = os.environ.copy()
    env.update(
        {
            "TASK_TRACKER_WORK_FILE": str(work),
            "TASK_TRACKER_LEDGER_FILE": str(ledger),
            "TASK_MGMT_STATE_DIR": str(tmp_path / "state"),
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "scripts"),
        }
    )

    proc = subprocess.run(
        ["python3", "scripts/rollover.py", "--date", "2026-06-29", "--dry-run"],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert work.read_text(encoding="utf-8") == original
    assert proc.stdout.startswith("# Weekly TODOs — 2026-W27")
    assert "Done task" not in proc.stdout


def test_rollover_cli_uses_error_envelope_contract():
    source = (Path(__file__).resolve().parents[1] / "scripts" / "rollover.py").read_text(encoding="utf-8")

    assert 'sys.exit(error_envelope.run_main("rollover", main))' in source


def test_rollover_exports_public_reconciliation_symbols():
    import rollover

    for name in ("Candidate", "is_closed_by_ledger", "render_candidates", "normalise_title"):
        assert hasattr(rollover, name)
