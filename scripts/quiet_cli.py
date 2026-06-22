#!/usr/bin/env python3
"""H5 reactive ``/quiet`` command: open / close / show the proactive-push quiet window.

The user TYPED this (origin-proven, reply lands in the originating topic), so it is
reactive + read/write of its OWN state only -- it never proves a delivery target,
opens a nag loop, or sends a proactive push. It only sets/clears/reads
``quiet-state.json`` via ``quiet_state``.

* ``/quiet <dur>``  -- suppress proactive pushes until ``local_now()+dur``
                       (``24h`` / ``2h`` / ``30m`` / ``1d``).
* ``/quiet off`` (alias ``/unquiet``) -- clear the window; pushes resume.
* ``/quiet`` (no arg) -- show the current window state.

Durations reuse ``nag_commands.parse_duration_minutes`` (the existing d/h/m parser
the snooze path uses) -- a fresh parser would be a second source of truth to drift.
"""

from __future__ import annotations

import argparse
import json
from datetime import timedelta
from typing import Any

import cos_config
from nag_commands import parse_duration_minutes

# The off/clear sentinels accepted as the duration argument (so `/quiet off` works
# without a separate subcommand). `/unquiet` routes here with this already filled in.
_OFF_TOKENS = frozenset({"off", "clear", "stop", "none", "0"})


def handle_quiet(duration: str | None) -> dict[str, Any]:
    """Set / clear / show the quiet window. ``duration`` None => show current state."""
    import quiet_state  # noqa: PLC0415 -- lazy so a test can patch state_dir first

    now = cos_config.local_now()

    if duration is None:
        until = quiet_state.quiet_until(now)
        if until is None:
            return {"ok": True, "quiet": False,
                    "message": "Proactive pushes are ON (no quiet window set)."}
        return {"ok": True, "quiet": True, "quiet_until": until.isoformat(),
                "message": f"Quiet until {until.isoformat()} -- proactive pushes are suppressed."}

    if duration.strip().lower() in _OFF_TOKENS:
        quiet_state.clear_quiet()
        return {"ok": True, "quiet": False,
                "message": "Quiet cleared -- proactive pushes resume."}

    minutes = parse_duration_minutes(duration)
    if minutes <= 0:
        return {"ok": False, "error": {
            "code": "invalid-duration",
            "message": f"Quiet duration must be like 30m/2h/1d, or 'off'; got {duration!r}.",
        }}

    until = now + timedelta(minutes=minutes)
    quiet_state.set_quiet(until)
    return {"ok": True, "quiet": True, "quiet_until": until.isoformat(),
            "message": (f"Quiet until {until.isoformat()} -- the proactive nag is "
                        "suppressed until then. (Body-double check-ins you started keep "
                        "running -- /cancel-session to stop one.) Reply /quiet off to resume.")}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="quiet_cli.py", description=__doc__)
    parser.add_argument("duration", nargs="?", default=None,
                        help="e.g. 30m / 2h / 1d, or 'off' to clear; omit to show state")
    args = parser.parse_args(argv)
    result = handle_quiet(args.duration)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
