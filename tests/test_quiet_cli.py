"""H5 reactive /quiet command: set / clear / show the proactive-push quiet window.

Reactive (the user typed it): it read/writes ONLY its own quiet-state.json -- it
proves no delivery target, opens no nag loop, and sends no push. These tests pin
the set / off / show round-trip and the duration validation.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import cos_config  # noqa: E402
import quiet_cli  # noqa: E402
import quiet_state  # noqa: E402

# A FIXED local-now so the deadline math is deterministic.
NOW = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_MGMT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(cos_config, "local_now", lambda: NOW)
    return tmp_path / "state"


def test_quiet_set_window(cli_env):
    result = quiet_cli.handle_quiet("2h")
    assert result["ok"] is True and result["quiet"] is True
    # The window is now active when read from state.
    assert quiet_state.is_quiet(NOW) is True
    until = quiet_state.quiet_until(NOW)
    assert until is not None and (until - NOW).total_seconds() == 2 * 3600


def test_quiet_off_clears_window(cli_env):
    quiet_cli.handle_quiet("1d")
    assert quiet_state.is_quiet(NOW) is True
    result = quiet_cli.handle_quiet("off")
    assert result["ok"] is True and result["quiet"] is False
    assert quiet_state.is_quiet(NOW) is False


def test_quiet_unquiet_alias_clears(cli_env):
    """`/unquiet` routes here with 'off' -- same clear as `/quiet off`."""
    quiet_cli.handle_quiet("30m")
    quiet_cli.handle_quiet("off")  # what the unquiet case dispatches
    assert quiet_state.is_quiet(NOW) is False


def test_quiet_no_arg_shows_state(cli_env):
    # No window set -> reports pushes ON.
    off = quiet_cli.handle_quiet(None)
    assert off["ok"] is True and off["quiet"] is False
    # After setting, the no-arg query reports the active window.
    quiet_cli.handle_quiet("4h")
    on = quiet_cli.handle_quiet(None)
    assert on["ok"] is True and on["quiet"] is True and "quiet_until" in on


def test_quiet_invalid_duration_rejected(cli_env):
    result = quiet_cli.handle_quiet("banana")
    assert result["ok"] is False
    assert result["error"]["code"] == "invalid-duration"
    # A rejected duration sets NO window.
    assert quiet_state.is_quiet(NOW) is False


def test_quiet_main_returns_zero_on_ok_nonzero_on_error(cli_env, capsys):
    assert quiet_cli.main(["1h"]) == 0
    capsys.readouterr()
    assert quiet_cli.main(["banana"]) == 2  # invalid duration -> nonzero
