#!/usr/bin/env python3
"""U1 pre-ritual preflight (Decision #6, Option A).

Before a cron-triggered ritual runs, prove its environment is sound:

- HARD checks (work file present+readable, ledger writable, python imports,
  cron-state writable) -- a hard failure means the ritual CANNOT run correctly.
- SOFT checks (STANDUP_CALENDARS, gog binary, gh auth, error-log writable,
  cron delivery targets) -- degrade gracefully; the ritual still runs.

Exit-code contract (Decision #6 / OQ-1, made definitive):

    On a HARD failure, preflight EXITS 0 with a friendly stdout line that NAMES
    the exact failed check. The static cron fallback text cannot name the check,
    so exiting non-zero (and letting the relay substitute generic text) would
    lose the diagnosis. Exit 0 + friendly stdout is the reliable path: the agent
    forwards stdout verbatim. ``--strict-exit`` is available for callers that
    want the diagnostic exit code (1 hard / 0 otherwise) without changing stdout.

Option A boundaries:
- Preflight writes a status file under ``cos_config.state_dir()``
  (``preflight-state.json``) and NEVER edits the live ``HEARTBEAT.md``.
- It reports to Telegram only on a status CHANGE (pass<->fail, or first-seen
  warn) -- handled by the caller forwarding stdout; preflight emits the report
  line and records the new status so a stable status stays silent next run.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cos_config
import error_envelope

HARD = "hard"
SOFT = "soft"

PASS = "pass"
WARN = "warn"
FAIL = "fail"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def preflight_state_path() -> Path:
    raw = os.getenv("TASK_TRACKER_PREFLIGHT_STATE")
    if raw:
        return Path(raw).expanduser()
    return cos_config.state_dir() / "preflight-state.json"


# --- Individual checks -----------------------------------------------------
# Each returns (status, detail). status in {PASS, WARN, FAIL}. A HARD check that
# returns FAIL aborts the ritual; a SOFT check that returns WARN/FAIL degrades.


def _check_work_file() -> tuple[str, str]:
    raw = os.getenv("TASK_TRACKER_WORK_FILE")
    if not raw:
        return FAIL, "TASK_TRACKER_WORK_FILE is not set"
    path = Path(raw).expanduser()
    if not path.exists():
        return FAIL, "work file not found"
    if not os.access(path, os.R_OK):
        return FAIL, "work file is not readable"
    return PASS, "work file present and readable"


def _check_ledger_writable() -> tuple[str, str]:
    try:
        import task_ledger

        target = task_ledger.ledger_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8"):
            pass
    except Exception:  # noqa: BLE001 - any failure is a hard ledger fault
        return FAIL, "ledger path is not writable"
    return PASS, "ledger writable"


def _check_python_imports() -> tuple[str, str]:
    for mod in ("utils", "standup", "standup_common"):
        try:
            __import__(mod)
        except Exception:  # noqa: BLE001
            return FAIL, f"python import failed: {mod}"
    return PASS, "core modules importable"


def _check_cron_state_writable() -> tuple[str, str]:
    try:
        path = preflight_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        # Unique probe name + missing_ok so two concurrent preflight runs (boot
        # cron + manual) never race each other's unlink into a spurious FAIL.
        probe = path.parent / f".preflight-write-probe.{os.getpid()}"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except OSError:
        return FAIL, "cron-state dir is not writable"
    return PASS, "cron-state writable"


def _check_standup_calendars() -> tuple[str, str]:
    raw = os.getenv("STANDUP_CALENDARS")
    if not raw or not raw.strip():
        return WARN, "STANDUP_CALENDARS not set — calendar section disabled"
    try:
        json.loads(raw)
    except json.JSONDecodeError:
        return WARN, "STANDUP_CALENDARS is not valid JSON — calendar disabled"
    return PASS, "STANDUP_CALENDARS configured"


def _check_gog_binary() -> tuple[str, str]:
    if shutil.which("gog") is None:
        return WARN, "gog not found — calendar tool unavailable"
    return PASS, "gog found"


def _check_gh_auth() -> tuple[str, str]:
    if shutil.which("gh") is None:
        return WARN, "gh not found — GitHub harvest unavailable"
    try:
        result = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError):
        return WARN, "gh auth status could not be determined"
    if result.returncode != 0:
        return WARN, "gh not authenticated"
    return PASS, "gh authenticated"


def _check_error_log_writable() -> tuple[str, str]:
    path = error_envelope.error_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and not os.access(path, os.W_OK):
            return WARN, "error log exists but is not writable"
        if not os.access(path.parent, os.W_OK):
            return WARN, "error-log dir is not writable"
    except OSError:
        return WARN, "error-log dir is not writable"
    return PASS, "error log writable"


def _cron_jobs_json() -> list[dict[str, Any]] | None:
    """Read the active cron jobs via ``openclaw cron list --json``.

    Returns None (not an empty list) when the tool is unavailable or fails, so a
    missing ``openclaw`` binary degrades to a SOFT skip rather than a false
    "no jobs" claim.
    """
    if shutil.which("openclaw") is None:
        return None
    try:
        result = subprocess.run(
            ["openclaw", "cron", "list", "--json"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    if isinstance(data, dict):
        data = data.get("jobs", data.get("crons", []))
    return data if isinstance(data, list) else None


def _validate_delivery_targets(jobs: list[dict[str, Any]]) -> list[str]:
    """Read-only audit of each job's delivery target (U1 INV-2, reversibility-safe).

    Returns a list of human-readable warning strings for offending jobs. NEVER
    modifies a cron job (U1 reads, U2's ladder fixes).
    """
    warnings: list[str] = []
    for job in jobs:
        job_id = str(job.get("id") or job.get("job_id") or "<unknown>")
        delivery = job.get("delivery") or {}
        to = delivery.get("to") if isinstance(delivery, dict) else None
        channel = delivery.get("channel") if isinstance(delivery, dict) else None
        agent_id = job.get("agentId") or job.get("agent_id")
        if not to:
            warnings.append(f"cron {job_id}: delivery.to not set")
        # §2g: delivery.channel must be EXPLICIT. An absent/None channel is
        # equal-or-worse than the literal "last" (the gateway infers the target),
        # so both are flagged -- consistent with the falsy treatment of to/agentId.
        if not channel or channel == "last":
            warnings.append(f"cron {job_id}: delivery.channel not explicit")
        if not agent_id:
            warnings.append(f"cron {job_id}: agentId not set")
    return warnings


def _check_delivery_targets() -> tuple[str, str]:
    jobs = _cron_jobs_json()
    if jobs is None:
        return WARN, "cron list unavailable — delivery targets not audited"
    problems = _validate_delivery_targets(jobs)
    if problems:
        return WARN, "; ".join(problems)
    return PASS, f"{len(jobs)} cron job(s), all delivery targets explicit"


_CHECKS: list[tuple[str, str, Any]] = [
    ("work_file", HARD, _check_work_file),
    ("ledger_writable", HARD, _check_ledger_writable),
    ("python_imports", HARD, _check_python_imports),
    ("cron_state_writable", HARD, _check_cron_state_writable),
    ("standup_calendars", SOFT, _check_standup_calendars),
    ("gog_binary", SOFT, _check_gog_binary),
    ("gh_auth", SOFT, _check_gh_auth),
    ("error_log_writable", SOFT, _check_error_log_writable),
    ("delivery_targets", SOFT, _check_delivery_targets),
]


def run_checks() -> list[dict[str, Any]]:
    """Run every check; return ordered result rows. Never raises."""
    rows: list[dict[str, Any]] = []
    for check_id, tier, fn in _CHECKS:
        try:
            status, detail = fn()
        except Exception:  # noqa: BLE001 - a check must never crash preflight
            status, detail = (FAIL if tier == HARD else WARN), "check raised"
        rows.append({"check": check_id, "tier": tier, "status": status, "detail": detail})
    return rows


def _load_state() -> dict[str, Any]:
    path = preflight_state_path()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"last_boot_report_ts": None, "checks": {}}


def _save_state(rows: list[dict[str, Any]]) -> dict[str, str]:
    """Persist the latest status per check; return prior statuses for diffing.

    Uses the Phase 0a atomic writer so a torn write never corrupts the state on
    a concurrent heartbeat. Returns the PREVIOUS status map (before this write).
    """
    prior = _load_state()
    prior_checks = prior.get("checks", {})
    prior_statuses = {k: v.get("last_status") for k, v in prior_checks.items()}

    new_checks: dict[str, Any] = {}
    ts = _now_iso()
    for row in rows:
        new_checks[row["check"]] = {"last_status": row["status"], "last_ts": ts}
    state = {"last_boot_report_ts": ts, "checks": new_checks}

    payload = json.dumps(state, indent=2, sort_keys=True)
    path = preflight_state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError:
        # State persistence failure must not abort the ritual; it only means the
        # next boot re-reports (report-on-change degrades to report-always).
        print("[task-tracker] preflight-state write failed", file=sys.stderr)
    return prior_statuses


def _hard_failures(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in rows if r["tier"] == HARD and r["status"] == FAIL]


def _changed_rows(rows: list[dict[str, Any]], prior: dict[str, str]) -> list[dict[str, Any]]:
    """Rows whose status differs from the prior run (drives report-on-change)."""
    changed = []
    for row in rows:
        if prior.get(row["check"]) != row["status"]:
            changed.append(row)
    return changed


def _format_report(rows: list[dict[str, Any]], changed: list[dict[str, Any]]) -> str:
    icon = {PASS: "✓", WARN: "⚠️", FAIL: "✗"}
    hard_fails = _hard_failures(rows)
    lines: list[str] = []
    if hard_fails:
        names = ", ".join(r["check"] for r in hard_fails)
        lines.append(
            f"⚠️ [task-tracker preflight] Ritual cannot run — failed check: {names}."
        )
        for r in hard_fails:
            lines.append(f"  ✗ {r['check']}: {r['detail']}")
        lines.append("Fix the named check and the next run will retry.")
        return "\n".join(lines)

    # No hard failure: emit the full boot report only when something changed.
    if not changed:
        return ""
    lines.append("📋 Task-tracker preflight:")
    for r in rows:
        lines.append(f"  {icon[r['status']]} {r['check']}: {r['detail']}")
    return "\n".join(lines)


def _log_hard_fails(rows: list[dict[str, Any]], prior: dict[str, str], trigger: str) -> None:
    """Mirror a hard failure to the structured log + ledger, but only on CHANGE.

    Parallel to the report-on-change stdout gate: a check that was already failing
    on the prior run is NOT re-mirrored to the append-only ledger, so a persistent
    fault (e.g. a permanently missing work file) does not bloat the shared
    queryable history every heartbeat. A fresh transition into failure IS logged.
    """
    for r in _hard_failures(rows):
        if prior.get(r["check"]) == FAIL:
            continue
        error_envelope.log_error(
            "preflight",
            error_class=error_envelope.ENVIRONMENT,
            message=f"preflight hard failure: {r['check']}",
            raw=r["detail"],
            trigger=trigger,
            check=r["check"],
            level="preflight_fail",
            action_taken="abort+notify",
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pre-ritual preflight checks.")
    parser.add_argument("--boot", action="store_true", help="Boot mode: report on status change.")
    parser.add_argument("--json", action="store_true", help="Emit JSON results.")
    parser.add_argument("--quiet", action="store_true", help="Only print failures.")
    parser.add_argument(
        "--strict-exit",
        action="store_true",
        help="Exit 1 on a hard failure (diagnostic). Default exits 0 with friendly stdout.",
    )
    args = parser.parse_args(argv)

    rows = run_checks()
    trigger = "heartbeat" if args.boot else "user_command:/preflight"
    prior = _save_state(rows)
    hard_fails = _hard_failures(rows)
    _log_hard_fails(rows, prior, trigger)

    if args.json:
        print(json.dumps({"rows": rows, "hard_failure": bool(hard_fails)}, indent=2, sort_keys=True))
    else:
        changed = _changed_rows(rows, prior)
        report = _format_report(rows, changed)
        if report:
            print(report)
        elif not args.quiet:
            # Stable, all-pass: stay silent in boot mode; print a terse OK otherwise.
            if not args.boot:
                print("✓ preflight: all hard checks pass")

    # Exit-code contract (Decision #6): default exit 0 even on hard failure so the
    # agent forwards the named-check stdout. --strict-exit opts into diagnostics.
    if args.strict_exit and hard_fails:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(error_envelope.run_main("preflight", main, trigger="user_command:/preflight"))
