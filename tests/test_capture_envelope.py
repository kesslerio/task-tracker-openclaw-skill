import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import capture_envelope


SECRET = "unit-test-envelope-secret"
NOW = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)


def _unsigned_envelope():
    return {
        "v": 1,
        "sender": "sender-123",
        "channel": "telegram",
        "message_id": "msg-sign-1",
        "timestamp": NOW.isoformat(),
        "task_id": "tsk_sign",
        "intent": "complete",
    }


def test_sign_cli_round_trips_through_verify_envelope():
    env = os.environ.copy()
    env[capture_envelope.SECRET_ENV] = SECRET

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "capture_envelope.py"),
            "sign",
            "--json",
            json.dumps(_unsigned_envelope()),
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert result.returncode == 0
    assert result.stderr == ""
    signed = json.loads(result.stdout)
    assert signed["sig"]
    verified = capture_envelope.verify_envelope(signed, secret=SECRET, now=NOW)
    assert verified.ok
    assert verified.envelope == signed


def test_sign_cli_fails_closed_when_secret_unset():
    env = os.environ.copy()
    env.pop(capture_envelope.SECRET_ENV, None)

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "capture_envelope.py"),
            "sign",
            "--json",
            json.dumps(_unsigned_envelope()),
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert result.returncode != 0
    assert result.stdout == ""
    assert capture_envelope.SECRET_ENV in result.stderr
    assert SECRET not in result.stderr


def test_sign_cli_fails_closed_when_secret_empty():
    env = os.environ.copy()
    env[capture_envelope.SECRET_ENV] = ""

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "capture_envelope.py"),
            "sign",
            "--json",
            json.dumps(_unsigned_envelope()),
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert result.returncode != 0
    assert result.stdout == ""
    assert capture_envelope.SECRET_ENV in result.stderr
