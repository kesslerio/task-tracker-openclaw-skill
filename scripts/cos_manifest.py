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

# R1 Fix 4: the expected-ritual registry -- the rituals that SHOULD run, with each
# one's max-age cadence in HOURS. A registered ritual that has NEVER recorded any
# health (no entry in cos-health.json at all) is flagged MISSING (a kind of STALE):
# "absent" is now LOUD, not silent. The ``health`` view iterates the registry UNION the
# recorded rituals, so a ritual that has never started is visible rather than simply not
# printed.
#
# ONLY rituals that actually RECORD a health success on a clean run belong here, or a
# healthy ritual would be permanently false-MISSING. ``standup``/``weekly_review``/
# ``eod_review`` record via ``error_envelope.run_main`` (success on a 0 return);
# ``nag_check`` and ``ledger_harvest`` record via their own ``_record_*_health`` on the
# cron fire (both run under the shell ``run_with_envelope``, not ``run_main``, and catch
# their own crash to exit 0, so they MUST record health directly or they false-green
# until STALE). H8 wired ``ledger_harvest`` health: ``--auto`` records success on a clean
# weekly harvest and failure on an ``ok:false`` result, ONLY on the cron path (a reactive
# ``/ledger`` records nothing). Its cadence is now WEEKLY (the digest fires Friday), so a
# weekly ceiling matches ``weekly_review``.
_EXPECTED_RITUALS: dict[str, int] = {
    "standup": 24,             # daily cadence
    "weekly_review": 24 * 8,   # weekly cadence + a missed run of slack
    "nag_check": 24,           # fires every ~3h in work hours; daily is a loose ceiling
    "eod_review": 24,          # end-of-day cadence
    "ledger_harvest": 24 * 8,  # weekly brag digest (Friday) + a missed run of slack
}

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


def _parse_ts(ts: str | None):
    """Parse a local-ISO timestamp into an aware ``datetime``, or None if absent/garbage.

    A naive timestamp is pinned to the local tz so two timestamps compare apples-to-apples
    (the same clock the rituals/cron stamp in). Used both by ``_age_hours`` and by the
    DEGRADED freshness check, which compares the two PARSED timestamps DIRECTLY -- never
    two independently-computed ``now``-relative ages, whose two ``local_now()`` snapshots
    drift apart and would mis-order an EQUAL failure/success pair.
    """
    if not ts:
        return None
    try:
        when = cos_config.datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=cos_config.local_tz())
    return when


def _age_hours(ts: str | None) -> float | None:
    """Hours between ``ts`` (local ISO) and now, or None if absent/unparseable."""
    when = _parse_ts(ts)
    if when is None:
        return None
    delta = cos_config.local_now() - when
    return delta.total_seconds() / 3600.0


def _ritual_flag(entry: dict[str, Any] | None, *, stale_hours: int, registered: bool) -> str:
    """Classify ONE ritual's health into a status flag.

    R1 Fix 4 precedence (loudest comparable failure wins, documented):

    * MISSING -- a REGISTERED ritual with NO health entry at all (never ran). A kind of
      STALE: "absent" is loud, not silent. (Outermost: there is nothing to compare.)
    * DEGRADED -- a FRESH failure that is comparable to a recorded success:
      ``last_failure_ts`` is newer than OR EQUAL TO ``last_success_ts``. A failure at
      least as recent as the last good run means the ritual's most recent outcome is a
      failure -- the loudest signal -- so it OUTRANKS OK and STALE: a ritual that ran and
      BROKE must not read OK just because the (older) success is still inside the stale
      window. (An OLDER failure already followed by a NEWER success is a RECOVERED ritual
      -- failure newer-than success is false -- so it is NOT degraded.)
    * STALE -- ``last_success_ts`` is absent or older than ``stale_hours`` and there is
      no fresh comparable failure to upgrade it to DEGRADED. A failure with NO recorded
      success is STALE-by-absent-success, NOT degraded: there is no good run to be newer
      than, so "the ritual never succeeded" is the staleness story, not a regression.
    * OK -- a recent success and no fresh failure.
    """
    if entry is None:
        # A registered ritual that has never recorded any health: never ran -> MISSING.
        # (An unregistered ritual never reaches here -- it always has a recorded entry.)
        return "MISSING" if registered else "STALE"

    last_success = entry.get("last_success_ts")
    success_when = _parse_ts(last_success)
    failure_ts = entry.get("last_failure_ts")
    if failure_ts is None:
        failure = entry.get("last_failure")
        failure_ts = failure.get("ts") if isinstance(failure, dict) else None
    failure_when = _parse_ts(failure_ts)

    # FRESH FAILURE (loudest, but only when COMPARABLE to a recorded success): a failure
    # at least as recent as the last good run means the most recent outcome is a failure
    # -> DEGRADED, regardless of how fresh that (older) success is. The two PARSED
    # timestamps are compared DIRECTLY (not two now-relative ages, whose separate
    # local_now() snapshots drift and would mis-order an EQUAL pair): failure >= success
    # is the documented rule. A failure with no recorded/parseable success has nothing to
    # be newer-than, so it stays STALE-by-absent-success below, not DEGRADED.
    if failure_when is not None and success_when is not None and failure_when >= success_when:
        return "DEGRADED"

    success_age = _age_hours(last_success)
    if success_age is None or success_age > stale_hours:
        return "STALE"
    return "OK"


def health_lines(*, stale_hours: int = _DEFAULT_STALE_HOURS) -> list[str]:
    """Render the per-ritual health summary, flagging stale/degraded/missing rituals.

    R1 Fix 4: the view iterates the EXPECTED-RITUAL registry UNION the recorded rituals,
    so a registered ritual that has never run shows up as MISSING rather than being
    silently absent. Per ritual (see ``_ritual_flag`` for the precedence):

    * DEGRADED -- a fresh failure (``last_failure_ts`` >= ``last_success_ts``);
    * MISSING  -- a registered ritual with no health entry at all (never ran);
    * STALE    -- ``last_success_ts`` absent/older than ``stale_hours`` (no fresh failure);
    * OK       -- a recent success and no fresh failure.

    The last failure (class + ts) is shown when present so a watchdog/operator sees both
    the last good run and the last bad one.
    """
    rituals = cos_health.read_health()
    # Union: every registered ritual is visible even with no recorded entry (MISSING),
    # plus any recorded ritual not in the registry (an ad-hoc / future ritual still gets
    # its STALE/DEGRADED/OK status). Sorted for a stable, deterministic surface.
    names = sorted(set(_EXPECTED_RITUALS) | set(rituals))
    if not names:
        return ["No ritual health recorded yet."]

    lines: list[str] = []
    for ritual in names:
        raw = rituals.get(ritual)
        entry = raw if isinstance(raw, dict) else None
        # Each REGISTERED ritual is judged against ITS OWN cadence (e.g. weekly_review =
        # 192h), so an on-cadence weekly ritual is not false-flagged STALE against the
        # global default. An unregistered / recorded-only ritual falls back to stale_hours.
        ritual_stale = _EXPECTED_RITUALS.get(ritual, stale_hours)
        flag = _ritual_flag(entry, stale_hours=ritual_stale, registered=ritual in _EXPECTED_RITUALS)
        success_part = (entry or {}).get("last_success_ts") or "never"
        failure = (entry or {}).get("last_failure")
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
