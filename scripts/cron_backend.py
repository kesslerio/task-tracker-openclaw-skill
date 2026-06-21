#!/usr/bin/env python3
"""Gateway cron backend for U4 body-double check-ins.

Body-double check-ins are ephemeral one-shot crons (``deleteAfterRun: true``,
``schedule.kind: "at"``) created via the OpenClaw gateway. ``nag_commands`` builds
the descriptor (explicit proven ``delivery.to`` + ``agentId``); this module is the
thin shell-out that asks the live gateway to create / delete one.

Why a real backend (vs the test no-op): the reactive ``/body-double`` CLI path
must NOT report "Session started" while silently creating nothing. ``main`` wires
``create_cron``/``delete_cron`` to ``GatewayCronBackend`` so the documented entry
point actually schedules the check-ins, and a gateway failure surfaces as a
``CronBackendError`` (loud), never a silent no-op.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any

# The gateway CLI. Overridable for tests / alternate hosts, but never a different
# default binary baked elsewhere.
OPENCLAW_BIN = "openclaw"
_TIMEOUT_SECONDS = 20


class CronBackendError(RuntimeError):
    """A gateway cron create/delete failed. Raised LOUD so the caller never reports
    a body-double 'started' when no check-in cron was actually scheduled."""


def gateway_available() -> bool:
    """Is the ``openclaw`` CLI on PATH? Used to fail loudly with a clear message."""
    return shutil.which(OPENCLAW_BIN) is not None


def _run(args: list[str], *, stdin: str | None = None) -> subprocess.CompletedProcess:
    if not gateway_available():
        raise CronBackendError(
            f"{OPENCLAW_BIN!r} is not on PATH; cannot schedule body-double check-ins."
        )
    try:
        return subprocess.run(
            [OPENCLAW_BIN, *args], input=stdin, capture_output=True, text=True,
            timeout=_TIMEOUT_SECONDS, check=True,
        )
    except subprocess.CalledProcessError as exc:
        raise CronBackendError(
            f"gateway cron command failed (exit {exc.returncode})."
        ) from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CronBackendError("gateway cron command could not run.") from exc


def create_cron(descriptor: dict[str, Any]) -> str:
    """Create one ephemeral cron from ``descriptor`` via ``openclaw cron create``.

    The descriptor JSON is piped on stdin. Returns the created cron id parsed from
    the gateway's JSON response; raises ``CronBackendError`` on any failure.
    """
    result = _run(["cron", "create", "--json", "-"], stdin=json.dumps(descriptor))
    return _parse_cron_id(result.stdout)


def delete_cron(cron_id: str) -> None:
    """Delete a pending cron by id via ``openclaw cron delete``.

    Any non-zero gateway exit (including a benign "not found" on an already-reaped
    ``deleteAfterRun`` cron) surfaces as ``CronBackendError``. Callers that want to
    tolerate a not-found wrap this in ``nag_commands._safe_delete``, which swallows
    ``CronBackendError`` -- that is where the best-effort policy lives, so this
    primitive stays simple and honest about what it does.
    """
    _run(["cron", "delete", cron_id])


def _parse_cron_id(stdout: str) -> str:
    """Extract the created cron id from the gateway JSON response.

    Accepts ``{"id": ...}`` or ``{"cron": {"id": ...}}``. A response we cannot
    parse is a CronBackendError -- we never fabricate a fake id (which would let a
    caller believe a check-in was scheduled when it was not).
    """
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise CronBackendError("gateway returned a non-JSON cron response.") from exc
    if not isinstance(payload, dict):
        raise CronBackendError("gateway cron response was not a JSON object.")
    nested = payload.get("cron")
    cron_id = payload.get("id") or (nested.get("id") if isinstance(nested, dict) else None)
    if not cron_id:
        raise CronBackendError("gateway cron response had no id.")
    return str(cron_id)
