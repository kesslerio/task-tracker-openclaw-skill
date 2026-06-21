"""U4 gateway cron backend: real create/delete, loud failure (no silent no-op).

The body-double CLI path must NOT report "started" while creating nothing -- it
either schedules the real check-in crons or fails loudly.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import cron_backend  # noqa: E402
import nag_commands  # noqa: E402
import nag_state  # noqa: E402
import utils  # noqa: E402

PRODUCTIVITY = "-4242424242"


@pytest.fixture
def env(tmp_path, monkeypatch):
    board = tmp_path / "Work Tasks.md"
    board.write_text(
        "# Work\n\n## 🟡 Q2\n- [ ] **AC** task_id::tsk_abc123 🗓️2026-06-15 area:: M\n",
        encoding="utf-8")
    state = tmp_path / "state"
    monkeypatch.setenv("TASK_TRACKER_WORK_FILE", str(board))
    monkeypatch.setenv("TASK_TRACKER_LEDGER_FILE", str(state / "events.jsonl"))
    monkeypatch.setenv("TASK_MGMT_STATE_DIR", str(state))
    monkeypatch.setenv("TELEGRAM_CHAT_ID_PRODUCTIVITY", PRODUCTIVITY)
    monkeypatch.setenv("TELEGRAM_CHAT_ID_WORK", "-5252525252")
    monkeypatch.setenv("OPENCLAW_TOPIC_PRODUCTIVITY_STANDUP", "2")
    monkeypatch.setattr(utils, "OBSIDIAN_WORK", Path(board))
    return board, state


def test_create_cron_unavailable_gateway_raises(monkeypatch):
    monkeypatch.setattr(cron_backend, "gateway_available", lambda: False)
    with pytest.raises(cron_backend.CronBackendError):
        cron_backend.create_cron({"name": "x"})


def test_create_cron_parses_id_from_gateway_json(monkeypatch):
    monkeypatch.setattr(cron_backend, "gateway_available", lambda: True)

    class _Result:
        stdout = '{"id": "cron_realid"}'

    monkeypatch.setattr(cron_backend, "_run", lambda *a, **k: _Result())
    assert cron_backend.create_cron({"name": "x"}) == "cron_realid"


def test_create_cron_rejects_response_with_no_id(monkeypatch):
    monkeypatch.setattr(cron_backend, "gateway_available", lambda: True)

    class _Result:
        stdout = '{"status": "ok"}'  # no id -- must NOT fabricate one

    monkeypatch.setattr(cron_backend, "_run", lambda *a, **k: _Result())
    with pytest.raises(cron_backend.CronBackendError):
        cron_backend.create_cron({"name": "x"})


@pytest.mark.parametrize("stdout", [
    '{"cron": "created"}',   # non-dict 'cron' must not AttributeError
    '"a string payload"',    # non-object JSON
    'not json at all',       # invalid JSON
])
def test_parse_cron_id_malformed_response_raises_cron_backend_error(monkeypatch, stdout):
    monkeypatch.setattr(cron_backend, "gateway_available", lambda: True)

    class _Result:
        pass

    _Result.stdout = stdout
    monkeypatch.setattr(cron_backend, "_run", lambda *a, **k: _Result())
    with pytest.raises(cron_backend.CronBackendError):
        cron_backend.create_cron({"name": "x"})


def test_create_cron_parses_nested_cron_id(monkeypatch):
    monkeypatch.setattr(cron_backend, "gateway_available", lambda: True)

    class _Result:
        stdout = '{"cron": {"id": "cron_nested"}}'

    monkeypatch.setattr(cron_backend, "_run", lambda *a, **k: _Result())
    assert cron_backend.create_cron({"name": "x"}) == "cron_nested"


def test_body_double_reports_failure_when_backend_errors(env):
    """The default production path: if the gateway cron create fails, the body-
    double is NOT reported as started and no session is recorded."""
    board, state = env

    def boom(_descriptor):
        raise cron_backend.CronBackendError("gateway down")

    result = nag_commands.handle_body_double("tsk_abc123", "90m", create_cron=boom)
    assert result["ok"] is False
    assert result["error"]["code"] == "checkin-cron-failed"
    # No half-started session recorded.
    on_disk = nag_state.read_state().get("tsk_abc123")
    assert nag_state.active_body_double_session(on_disk) is None


def test_body_double_rolls_back_first_cron_when_second_fails(env, monkeypatch):
    """If the 2nd check-in cron fails, the 1st already-created cron is deleted.

    The rollback delete runs through ``_safe_delete`` -> ``cron_backend.delete_cron``;
    patch that backend so no real gateway is touched."""
    board, state = env
    created = []
    deleted = []

    def create(_descriptor):
        created.append(_descriptor)
        if len(created) == 2:
            raise cron_backend.CronBackendError("second failed")
        return f"cron_{len(created)}"

    monkeypatch.setattr(cron_backend, "delete_cron", lambda cron_id: deleted.append(cron_id))
    result = nag_commands.handle_body_double("tsk_abc123", "90m", create_cron=create)
    assert result["ok"] is False
    assert deleted == ["cron_1"]  # the first cron was rolled back
