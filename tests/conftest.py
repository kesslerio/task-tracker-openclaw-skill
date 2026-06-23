import os

import pytest


@pytest.fixture(autouse=True)
def isolate_unwritable_ambient_state_dir(tmp_path, monkeypatch):
    """Keep subprocess tests from inheriting a host-local read-only state dir."""
    raw = os.getenv("TASK_MGMT_STATE_DIR", "")
    if raw.startswith("/data/"):
        monkeypatch.setenv("TASK_MGMT_STATE_DIR", str(tmp_path / "state"))
