"""CLI surface for /audit + /undo: exit codes + JSON contract.

A non-zero exit on a refusal is what lets telegram-commands.sh's run_with_envelope
log the detail and print a friendly notice; a zero exit on a real reversal is what
lets the relay forward the success line. Both are asserted here.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import autonomy_cli
import autonomy_gate

PRODUCTIVITY_CHAT = "-4242424242"

_ENV = {
    "TELEGRAM_CHAT_ID_PRODUCTIVITY": PRODUCTIVITY_CHAT,
    "OPENCLAW_TOPIC_PRODUCTIVITY_STANDUP": "2",
    "OPENCLAW_TOPIC_PRODUCTIVITY_IDENTITY": "1909",
}


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_MGMT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("TASK_TRACKER_LEDGER_FILE", str(tmp_path / "ledger.events.jsonl"))
    for name, value in _ENV.items():
        monkeypatch.setenv(name, value)
    yield


def _gate_board_act(tmp_path):
    board = tmp_path / "Weekly TODOs.md"
    board.write_text("# Board\n", encoding="utf-8")
    snapshot = {"file": str(board), "raw_line": "- [ ] Restore me", "line_number": 2}
    config = autonomy_gate.ensure_autonomy_config()
    config.setdefault("act_type_rungs", {})["wip_cap_enforced"] = autonomy_gate.RUNG_APPROVE
    autonomy_gate._atomic_write(
        autonomy_gate.autonomy_config_path(),
        json.dumps(config, indent=2, sort_keys=True) + "\n",
    )
    return board, autonomy_gate.gate("wip_cap_enforced", task_id="tsk_cli", unit="U3",
                                     snapshot_provider=lambda: snapshot)


def test_audit_list_exit_zero(tmp_path, capsys):
    _gate_board_act(tmp_path)
    rc = autonomy_cli.main(["audit", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["ok"] is True
    assert len(out["acts"]) == 1


def test_audit_detail_unknown_act_exit_nonzero(capsys):
    rc = autonomy_cli.main(["audit", "act_nope", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert out["ok"] is False
    assert out["reason"] == "unknown-act"


def test_undo_success_exit_zero(tmp_path, capsys):
    board, gated = _gate_board_act(tmp_path)
    rc = autonomy_cli.main(["undo", gated["act_id"], "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["ok"] is True
    assert "- [ ] Restore me" in board.read_text(encoding="utf-8")


def test_undo_unknown_act_exit_nonzero(capsys):
    rc = autonomy_cli.main(["undo", "act_missing", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert out["ok"] is False
    assert out["reason"] == "unknown-act"


def test_undo_human_output_has_no_traceback(tmp_path, capsys):
    """The default (non-JSON) refusal line is friendly: no 'Traceback', no class."""
    rc = autonomy_cli.main(["undo", "act_missing"])
    captured = capsys.readouterr().out
    assert rc == 1
    assert "Traceback" not in captured
    assert "Error" not in captured  # no exception class name leaked
    assert "act_missing" in captured


def _gate_drifted_duplicate_act(tmp_path):
    """Gate a board act, then drift the board so two lines share its task_id."""
    import autonomy
    board = tmp_path / "Weekly TODOs.md"
    raw_line = "- [ ] Pay invoice task_id::tsk_dup"
    at_act_time = f"# Board\n{raw_line}\n"
    board.write_text(at_act_time, encoding="utf-8")
    snapshot = autonomy.board_snapshot(board, raw_line, 2, content=at_act_time,
                                       post_raw_line="- [x] Pay invoice task_id::tsk_dup")
    board.write_text("# Board\n- [x] Pay invoice task_id::tsk_dup\n"
                     "- [ ] Pay invoice AGAIN task_id::tsk_dup\n", encoding="utf-8")
    config = autonomy_gate.ensure_autonomy_config()
    config.setdefault("act_type_rungs", {})["task_marked_done"] = autonomy_gate.RUNG_APPROVE
    autonomy_gate._atomic_write(
        autonomy_gate.autonomy_config_path(),
        json.dumps(config, indent=2, sort_keys=True) + "\n",
    )
    return board, autonomy_gate.gate("task_marked_done", task_id="tsk_dup", unit="U5",
                                     snapshot_provider=lambda: snapshot)


def test_undo_conflict_exit_nonzero_and_writes_nothing(tmp_path, capsys):
    """A board-undo CONFLICT (duplicate id) is a non-zero exit with a human message,
    and the board is left untouched."""
    board, gated = _gate_drifted_duplicate_act(tmp_path)
    before = board.read_text(encoding="utf-8")
    rc = autonomy_cli.main(["undo", gated["act_id"], "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert out["ok"] is False
    assert out["reason"] == "conflict-duplicate"
    assert board.read_text(encoding="utf-8") == before  # nothing written


def test_undo_conflict_human_message_no_traceback(tmp_path, capsys):
    """The conflict's default human line is reviewable and leaks no traceback."""
    board, gated = _gate_drifted_duplicate_act(tmp_path)
    rc = autonomy_cli.main(["undo", gated["act_id"]])
    captured = capsys.readouterr().out
    assert rc == 1
    assert "Traceback" not in captured
    assert "Cannot undo" in captured and "tsk_dup" in captured
