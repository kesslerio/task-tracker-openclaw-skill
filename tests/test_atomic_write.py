"""Atomic board writes (Contract 4 / board).

Invariant: _atomic_write round-trips identical content, and a partial/interrupted
write never leaves a truncated destination -- temp+replace means the old file
survives intact on failure.
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from utils import _atomic_write


def test_round_trips_identical_content(tmp_path):
    target = tmp_path / "Weekly TODOs.md"
    content = "# board\n\n## 🔴 Q1\n- [ ] **Ship** task_id::tsk_a\n" * 50
    _atomic_write(target, content)
    assert target.read_text(encoding="utf-8") == content


def test_unicode_preserved_byte_for_byte(tmp_path):
    target = tmp_path / "board.md"
    content = "## 🅿️ Parking Lot\n- 🎯 émojis and ünïcode\n"
    _atomic_write(target, content)
    assert target.read_bytes() == content.encode("utf-8")


def test_preserves_existing_mode(tmp_path):
    target = tmp_path / "board.md"
    target.write_text("old\n")
    os.chmod(target, 0o600)
    _atomic_write(target, "new\n")
    assert (os.stat(target).st_mode & 0o777) == 0o600
    assert target.read_text() == "new\n"


def test_fresh_file_defaults_to_0600(tmp_path):
    """Finding #4/#13: a FRESH file (no existing mode) is owner-only by default,
    never the wider mkstemp inherited mode -- secret state must not leak."""
    target = tmp_path / "secret-state.json"
    assert not target.exists()
    _atomic_write(target, '{"k": "v"}\n')
    assert (os.stat(target).st_mode & 0o777) == 0o600


def test_interrupted_write_leaves_original_intact(tmp_path, monkeypatch):
    """If os.replace fails mid-write, the original board is untouched (no truncation)
    and the temp file is cleaned up -- never a half-written board."""
    import utils

    target = tmp_path / "board.md"
    original = "ORIGINAL BOARD CONTENT\n" * 20
    target.write_text(original)

    def boom(src, dst):
        raise OSError("simulated crash during replace")

    monkeypatch.setattr(utils.os, "replace", boom)

    with pytest.raises(OSError):
        _atomic_write(target, "NEW CONTENT THAT MUST NOT LAND\n")

    # Destination is byte-for-byte the original -- never truncated/partial.
    assert target.read_text() == original
    # No stray temp files left behind.
    assert [p.name for p in tmp_path.iterdir()] == ["board.md"]
