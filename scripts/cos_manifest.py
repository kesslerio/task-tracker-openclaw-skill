#!/usr/bin/env python3
"""H4 manifest + health surface: what's running, and when each ritual last ran.

Two read-only views over the machine-visible health substrate (``cos_health``):

* ``manifest`` -- writes ``cos-manifest.json`` (and prints it): a snapshot of the
  deployed skill (``skill_version`` from an optional ``VERSION``/``DEPLOY_STAMP``
  stamp file), the static list of ``enabled_units`` (U1..U5 live, U6 dormant), and
  the ``rituals`` health map (per-ritual last_success / last_failure). An external
  watchdog can poll this one file to learn the shape of the running skill.
* ``health`` -- prints a human-readable per-ritual summary, flagging any ritual whose
  ``last_success_ts`` is older than a staleness threshold (default 36h) as STALE.

Both run inside ``error_envelope.run_main`` so this script obeys the same
NO-RAW-ERROR-LEAK contract as every other ritual: a crash logs + prints one friendly
line + exits 0, never a traceback. The views are READ-ONLY over the board/state (they
only WRITE ``cos-manifest.json``, a derived artifact), so they are safe rung-0
surfaces to route from Telegram.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import cos_config
import cos_health
import error_envelope

# Static map of the live deploy. U1..U5 are live; U6 (proactive layer) is dormant.
# Encoded as DATA (not derived from the filesystem) so the manifest states the
# DEPLOY decision, not whatever scripts happen to be present in a worktree.
ENABLED_UNITS: list[dict[str, Any]] = [
    {"unit": "U1", "name": "error-envelope", "status": "live"},
    {"unit": "U2", "name": "autonomy-gate", "status": "live"},
    {"unit": "U3", "name": "standup-rituals", "status": "live"},
    {"unit": "U4", "name": "nag-engine", "status": "live"},
    {"unit": "U5", "name": "ledger-harvest", "status": "live"},
    {"unit": "U6", "name": "proactive-layer", "status": "dormant"},
]

# Default staleness threshold: a ritual whose last success is older than this is
# flagged STALE. 36h spans a full daily cadence plus a missed run before alarming.
_DEFAULT_STALE_HOURS = 36

# Stamp files checked (in order) for the deployed skill version. Best-effort: the
# first that exists wins; absent -> "unknown" (a worktree has no deploy stamp).
_STAMP_FILES = ("VERSION", "DEPLOY_STAMP")


def manifest_path() -> Path:
    return cos_config.state_dir() / "cos-manifest.json"


def _skill_root() -> Path:
    """The skill root (parent of this ``scripts/`` dir)."""
    return Path(__file__).resolve().parents[1]


def skill_version() -> str:
    """Best-effort deployed version from a stamp file in the skill root.

    Reads the first present of ``VERSION`` / ``DEPLOY_STAMP``; an absent/unreadable
    stamp degrades to ``"unknown"`` rather than failing the manifest -- the version is
    a nice-to-have label, never a hard dependency.
    """
    root = _skill_root()
    for name in _STAMP_FILES:
        stamp = root / name
        try:
            text = stamp.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if text:
            return text
    return "unknown"


def build_manifest() -> dict[str, Any]:
    """Assemble the manifest dict (does not write it)."""
    return {
        "generated_ts": cos_config.local_now().isoformat(),
        "skill_version": skill_version(),
        "enabled_units": ENABLED_UNITS,
        "rituals": cos_health.read_health(),
    }


def write_manifest() -> dict[str, Any]:
    """Build the manifest, write ``cos-manifest.json`` atomically, return the dict."""
    from utils import _atomic_write

    manifest = build_manifest()
    _atomic_write(manifest_path(), json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def _age_hours(ts: str | None) -> float | None:
    """Hours between ``ts`` (local ISO) and now, or None if absent/unparseable."""
    if not ts:
        return None
    try:
        when = cos_config.datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=cos_config.local_tz())
    delta = cos_config.local_now() - when
    return delta.total_seconds() / 3600.0


def health_lines(*, stale_hours: int = _DEFAULT_STALE_HOURS) -> list[str]:
    """Render the per-ritual health summary, flagging stale rituals.

    A ritual whose ``last_success_ts`` is older than ``stale_hours`` (or has none) is
    flagged STALE; a recent success is OK. The last failure (class + ts) is shown when
    present so a watchdog/operator sees both the last good run and the last bad one.
    """
    rituals = cos_health.read_health()
    if not rituals:
        return ["No ritual health recorded yet."]

    lines: list[str] = []
    for ritual in sorted(rituals):
        entry = rituals[ritual] if isinstance(rituals[ritual], dict) else {}
        last_success = entry.get("last_success_ts")
        age = _age_hours(last_success)
        if age is None or age > stale_hours:
            flag = "STALE"
        else:
            flag = "OK"
        success_part = last_success or "never"
        failure = entry.get("last_failure")
        if isinstance(failure, dict):
            fail_part = f" | last_failure: {failure.get('error_class')} @ {failure.get('ts')}"
        else:
            fail_part = ""
        lines.append(f"{flag} {ritual}: last_success {success_part}{fail_part}")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Chief-of-Staff manifest + health surface.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("manifest", help="Write cos-manifest.json and print it.")
    hp = sub.add_parser("health", help="Print the per-ritual health summary.")
    hp.add_argument("--stale-hours", type=int, default=_DEFAULT_STALE_HOURS,
                    help="Flag a ritual whose last success is older than this as STALE.")

    args = parser.parse_args(argv)
    if args.cmd == "manifest":
        manifest = write_manifest()
        print(json.dumps(manifest, indent=2, sort_keys=True))
        return 0
    if args.cmd == "health":
        for line in health_lines(stale_hours=args.stale_hours):
            print(line)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(error_envelope.run_main("manifest", lambda: main(sys.argv[1:])))
