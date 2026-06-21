"""U3 focus_commands CLI layer: the /focus* user-facing surface.

Drives focus_commands.py end-to-end via subprocess to cover the command guards
(stale-date "run /focus first", over-capacity approve refusal, override) that the
unit tests of defended_three/focus_state do not exercise.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))


def _focus(tmp_path, board, *cmd, env_overrides=None):
    env = os.environ.copy()
    env["TASK_TRACKER_WORK_FILE"] = str(board)
    env["TASK_MGMT_STATE_DIR"] = str(tmp_path / "state")
    env["TASK_TRACKER_LEDGER_FILE"] = str(tmp_path / "events.jsonl")
    env["WEEKLY_CAPACITY_HOURS"] = "25"
    env["DAILY_PRIORITY_COUNT"] = "3"
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        ["python3", str(SCRIPTS / "focus_commands.py"), *cmd],
        capture_output=True,
        text=True,
        env=env,
    )


def _board(tmp_path, body):
    board = tmp_path / "Work Tasks.md"
    board.write_text(body)
    return board


def _state(tmp_path):
    return json.loads((tmp_path / "state" / "focus-state.json").read_text())


_UNDER_CAP = (
    "# Work\n\n"
    "## 🔴 Q1\n"
    "- [ ] **A** estimate:: 2h task_id::tsk_aaaaaaaaaaaaaaaa\n"
    "- [ ] **B** estimate:: 1h task_id::tsk_bbbbbbbbbbbbbbbb\n"
    "## 🟡 Q2\n"
    "- [ ] **C** estimate:: 30m task_id::tsk_cccccccccccccccc\n"
    "- [ ] **D** estimate:: 1h task_id::tsk_dddddddddddddddd\n"
    "## 🅿️ Parking Lot\n"
)


def test_focus_propose_then_approve(tmp_path):
    board = _board(tmp_path, _UNDER_CAP)
    propose = _focus(tmp_path, board, "focus")
    assert propose.returncode == 0
    assert "Today's Daily Priorities" in propose.stdout
    assert _state(tmp_path)["status"] == "proposed"

    approve = _focus(tmp_path, board, "approve")
    assert approve.returncode == 0
    assert "locked" in approve.stdout
    assert _state(tmp_path)["status"] == "approved"


def test_focus_veto_then_status(tmp_path):
    board = _board(tmp_path, _UNDER_CAP)
    _focus(tmp_path, board, "focus")
    veto = _focus(tmp_path, board, "veto", "2")
    assert veto.returncode == 0
    # The vetoed task id is persisted sticky.
    assert len(_state(tmp_path)["vetoed"]) == 1

    status = _focus(tmp_path, board, "status")
    assert status.returncode == 0
    assert "Capacity OK" in status.stdout


def test_approve_without_proposal_is_guarded(tmp_path):
    board = _board(tmp_path, _UNDER_CAP)
    # No /focus run first.
    approve = _focus(tmp_path, board, "approve")
    assert approve.returncode == 0
    assert "No current proposal" in approve.stdout
    assert not (tmp_path / "state" / "focus-state.json").exists()


def test_stale_proposal_is_not_approvable(tmp_path):
    board = _board(tmp_path, _UNDER_CAP)
    _focus(tmp_path, board, "focus")
    # Make the persisted state stale (yesterday's date).
    path = tmp_path / "state" / "focus-state.json"
    state = json.loads(path.read_text())
    state["date"] = "2020-01-01"
    path.write_text(json.dumps(state))

    approve = _focus(tmp_path, board, "approve")
    assert "No current proposal" in approve.stdout
    # Status must NOT have flipped to approved on a stale proposal.
    assert json.loads(path.read_text())["status"] == "proposed"


def test_over_capacity_approve_refused_until_override(tmp_path):
    over = (
        "# Work\n\n"
        "## 🔴 Q1\n"
        "- [ ] **Big1** estimate:: 13h task_id::tsk_aaaaaaaaaaaaaaaa\n"
        "- [ ] **Big2** estimate:: 13h task_id::tsk_bbbbbbbbbbbbbbbb\n"
        "## 🅿️ Parking Lot\n"
    )
    board = _board(tmp_path, over)
    _focus(tmp_path, board, "focus")
    assert _state(tmp_path)["capacity_ok"] is False

    approve = _focus(tmp_path, board, "approve")
    assert "over capacity" in approve.stdout.lower()
    assert _state(tmp_path)["status"] == "proposed"  # refused

    override = _focus(tmp_path, board, "override")
    assert override.returncode == 0
    final = _state(tmp_path)
    assert final["status"] == "approved"
    assert final["override_reason"] == "user_explicit"
    events = [
        json.loads(l)
        for l in (tmp_path / "events.jsonl").read_text().splitlines()
        if l.strip()
    ]
    assert any(e["event_type"] == "capacity_overcommit" for e in events)


def test_focus_command_never_leaks_traceback_on_missing_board(tmp_path):
    # No work file at all -> the envelope must print a friendly line, not a trace.
    missing = tmp_path / "nope.md"
    proc = _focus(tmp_path, missing, "focus")
    assert "Traceback" not in proc.stdout
    assert "Traceback" not in proc.stderr
