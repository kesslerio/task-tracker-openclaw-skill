#!/usr/bin/env python3
"""U1 trust boundary: the NO-RAW-ERROR-LEAK envelope.

Every tool/script failure that would otherwise surface a raw traceback,
exception class, file path, ``ModuleNotFoundError``, or a hallucinated tool name
to the Telegram channel is caught here, classified, and turned into a friendly
one-line acknowledgment ("...unavailable right now, logged..."). The full error
is recorded -- never forwarded -- to a structured JSONL error log under the
Chief-of-Staff state dir (``cos_config.state_dir()`` -> Decision #6, NOT the
OpenClaw-managed agent dir which rotates on upgrade).

Contract for every Chief-of-Staff Python script (Phase 0a/U1 §3d):

1. Wrap the ``if __name__ == "__main__":`` body in ``run_main()`` (or a manual
   top-level ``try/except Exception`` that calls ``handle_fatal``).
2. On an uncaught exception: log it here, print ONE friendly line to STDOUT
   (never stderr, never a traceback), and exit 0 so the agent relay does not see
   a non-zero exit and forward raw output.
3. Never print a Python exception class, file path, or line number to stdout.

The module is also a CLI (``error_envelope.py log-subprocess ...``) so the shell
wrapper ``telegram-commands.sh`` can log a failed subprocess's captured stderr
without ever echoing it to stdout.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None

import cos_config

# --- Tunables --------------------------------------------------------------

# The structured error log is capped; on overflow we keep the tail and discard
# the head so the file never grows unbounded but recent failures survive.
_MAX_LOG_LINES = 500
_TRIM_TO_LINES = 400
# The `raw` field keeps the real stderr/traceback for debugging only -- it is
# never forwarded to Telegram. Truncated so one giant traceback cannot bloat the
# log past the rotation budget.
_RAW_TRUNCATE = 2000

# Error classes the envelope recognises. Classification drives the friendly
# message verb and, for `transient`, a single retry.
TRANSIENT = "transient"
AUTH = "auth"
MISSING_TOOL = "missing-tool"
ENVIRONMENT = "environment"

# A missing tool that keeps failing must not loop. After this many recorded
# missing-tool failures for the same component within the breaker window, the
# breaker opens and the envelope short-circuits to the friendly line without
# re-invoking the tool.
_BREAKER_THRESHOLD = 3
_BREAKER_WINDOW_SECONDS = 600

# The Telegram slash command that routes to each single-command ritual, so the
# friendly "Retry:" hint names a command the relay actually understands
# (telegram-commands.sh routes daily->standup, weekly->weekly_review). A
# component with no UNAMBIGUOUS slash command (e.g. `tasks`, a multi-subcommand
# CLI that backs both done24h and done7d) is intentionally absent: its notice
# omits the retry verb rather than steering the user to an arbitrary command.
_RETRY_COMMAND = {
    "standup": "daily",
    "weekly_review": "weekly",
}


def error_log_path() -> Path:
    """Resolve the structured error-log path.

    Defaults to ``<state_dir>/task-tracker-errors.jsonl`` (Decision #6). The
    ``TASK_TRACKER_ERROR_LOG`` override exists for tests and alternate hosts.
    """
    raw = os.getenv("TASK_TRACKER_ERROR_LOG")
    if raw:
        return Path(raw).expanduser()
    return cos_config.state_dir() / "task-tracker-errors.jsonl"


def _has(blob: str, *patterns: str) -> bool:
    """True if any whole-word pattern matches the lowercased blob.

    Word-boundary matching keeps ``classify`` from tagging an unrelated traceback
    (e.g. ``line 403`` or a summary that merely contains ``connection``) as AUTH
    or TRANSIENT, so the on-disk ``error_class`` stays trustworthy for querying.
    """
    return any(re.search(rf"\b{p}\b", blob) for p in patterns)


def classify(exc: BaseException | None, *, stderr: str = "") -> str:
    """Classify a failure into one of the four envelope error classes.

    Classification is structural (exception type first, then anchored stderr
    fingerprints), never a loose substring guess that widens what we tell the
    user or mislabels the on-disk record.
    """
    name = type(exc).__name__ if exc is not None else ""
    blob = f"{name}\n{stderr}".lower()

    if isinstance(exc, ModuleNotFoundError) or "modulenotfounderror" in blob:
        return ENVIRONMENT
    if isinstance(exc, (FileNotFoundError,)) or "no such file or directory" in blob:
        # A missing binary surfaces as FileNotFoundError from subprocess; a
        # missing data file is also environment. Distinguish on the stderr
        # fingerprint of a command-not-found OR, for an in-process
        # subprocess.run() failure, an exc.filename that is a bare executable
        # name (no path separator) -- that is a missing tool, so the breaker can
        # open and the cron stops looping on it.
        filename = getattr(exc, "filename", None)
        # A missing executable is a bare name with no path separator AND no file
        # extension (binaries: `gog`, `gh`); a missing *data* file carries a
        # suffix (`HEARTBEAT.md`, `tasks.json`) or a path -- that stays
        # ENVIRONMENT so a misplaced data file never trips the missing-tool
        # breaker and short-circuits the component.
        bare_executable = (
            bool(filename)
            and "/" not in str(filename)
            and not Path(str(filename)).suffix
        )
        if "command not found" in blob or _has(blob, "executable") or bare_executable:
            return MISSING_TOOL
        return ENVIRONMENT
    # subprocess.TimeoutExpired is NOT a TimeoutError subclass, and its class
    # name folds to "timeoutexpired" (no \btimeout\b boundary), so it must be
    # matched explicitly -- a calendar-fetch timeout is the most common transient
    # path and the on-disk error_class must label it accurately.
    if (
        isinstance(exc, (TimeoutError, subprocess.TimeoutExpired))
        or "timed out" in blob
        or "timeoutexpired" in blob
        or _has(blob, "timeout")
    ):
        return TRANSIENT
    if isinstance(exc, (ConnectionError,)) or _has(blob, "connection", "temporarily"):
        return TRANSIENT
    if (
        "permission denied" in blob
        or _has(blob, "unauthorized", "forbidden")
        # Only treat a bare 401/403 as auth when it carries HTTP/status context;
        # a plain "line 403" in a traceback must NOT be misread as an auth error.
        or re.search(r"\b(?:http|status|code|error)\s*[:=]?\s*40[13]\b", blob)
    ):
        return AUTH
    if "command not found" in blob:
        return MISSING_TOOL
    return ENVIRONMENT


def _sanitize(component: str, error_class: str) -> str:
    """Build the human-readable `message` field: classified, never raw.

    This is the value that may be surfaced; it must not contain a traceback,
    exception class, or file path -- only the component and its error class.
    """
    return f"{component} failed ({error_class})"


def _friendly_line(component: str) -> str:
    """The single user-facing line. No exception class, no path, no tool name.

    When the component maps to one real Telegram slash command, the line ends
    with a correct retry hint (``Retry: /daily`` for standup). When it does not
    (a multi-subcommand CLI like ``tasks``), the retry verb is omitted rather
    than steering the user to an arbitrary or non-routed command.
    """
    label = component.replace("_", " ")
    base = f"⚠️ {label} is unavailable right now. Logged for review."
    retry = _RETRY_COMMAND.get(component)
    return f"{base} Retry: /{retry}" if retry else base


def degraded_notice(component: str) -> str:
    """Inline degraded-mode string for a partial failure (e.g. calendar fetch).

    Used when the ritual continues but one section could not render. The user
    sees only that the section is degraded; the real failure was already logged
    at its source (e.g. ``get_calendar_events`` -> ``log_degraded``), never here.
    """
    label = component.replace("_", " ")
    return f"\U0001f4c5 {label}: unavailable (logged)"


def _read_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []


def _record(entry: dict[str, Any]) -> bool:
    """Append one structured entry to the error log; crash-safe and flocked.

    The whole read-trim-write rotation happens while holding an exclusive flock
    on a sidecar ``.lock`` file (not the data file itself, which is swapped out
    by ``os.replace`` and would lose the lock). Two concurrent writers (heartbeat
    cron + user CLI) can never interleave a torn JSONL line or both trim against a
    stale view. The new content is written to a temp file, fsync'd, and atomically
    renamed over the log -- so a crash mid-rotation leaves the OLD log intact
    rather than an empty/truncated file.

    Returns False on any OS error (unwritable path / chmod 000) -- the caller
    never lets that failure escape to Telegram; it degrades to the friendly line
    and a local stderr note. The file is ``0o600`` (owner-only) per security
    policy.
    """
    path = error_log_path()
    rendered = json.dumps(entry, ensure_ascii=False, sort_keys=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return False
    try:
        if fcntl is not None:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            try:
                existing = path.read_text(encoding="utf-8").splitlines()
            except FileNotFoundError:
                existing = []
            existing = [ln for ln in existing if ln.strip()]
            existing.append(rendered)
            if len(existing) > _MAX_LOG_LINES:
                existing = existing[-_TRIM_TO_LINES:]
            content = "\n".join(existing) + "\n"

            fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(tmp, 0o600)
                os.replace(tmp, path)
            except OSError:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                return False
        finally:
            if fcntl is not None:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
        return True
    except OSError:
        return False
    finally:
        try:
            os.close(lock_fd)
        except OSError:
            pass


def _ledger_system_error(component: str, check: str, action_taken: str, trigger: str) -> None:
    """Mirror an aborting error into the shared JSONL ledger as `system_error`.

    Best-effort: a ledger failure here must never escalate. The ledger is the
    queryable history that joins error events with task history (U1 §3a).
    """
    try:
        import task_ledger

        source = "user_command" if trigger.startswith("user_command") else "cli"
        event = task_ledger.new_event(
            "system_error",
            actor="task-tracker-preflight",
            source=source,
            reason=_sanitize(component, check),
            metadata={
                "component": component,
                "check": check,
                "action_taken": action_taken,
                "trigger": trigger,
            },
        )
        task_ledger.append_event(event)
    except Exception:  # noqa: BLE001 - best-effort; never escalate a logging path
        pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_error(
    component: str,
    *,
    error_class: str,
    message: str,
    raw: str,
    trigger: str,
    check: str = "runtime",
    level: str = "error",
    action_taken: str = "abort+notify",
    delivery_target: str = "none",
    to_ledger: bool = True,
) -> None:
    """Write one structured error entry (and optionally mirror to the ledger).

    All callers funnel through here so the on-disk shape is uniform (U1 §3a).
    The ``raw`` field is truncated and stays on disk only; it is never returned
    to the caller for forwarding.
    """
    entry = {
        "ts": _now_iso(),
        "level": level,
        "component": component,
        "trigger": trigger,
        "check": check,
        "error_class": error_class,
        "message": message,
        "raw": (raw or "")[:_RAW_TRUNCATE],
        "action_taken": action_taken,
        "delivery_target": delivery_target,
    }
    if not _record(entry):
        # The log itself is unwritable. This must not reach Telegram; a local
        # stderr note is acceptable (stderr is swallowed by the shell wrapper).
        print(
            f"[task-tracker] error-log unwritable; dropped {component}/{check}",
            file=sys.stderr,
        )
    if to_ledger and level in ("error", "preflight_fail"):
        _ledger_system_error(component, check, action_taken, trigger)


def _recent_missing_tool_count(component: str) -> int:
    """Count recent missing-tool failures for a component within the breaker window."""
    cutoff = time.time() - _BREAKER_WINDOW_SECONDS
    count = 0
    for line in _read_lines(error_log_path()):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("component") != component:
            continue
        if rec.get("error_class") != MISSING_TOOL:
            continue
        ts = rec.get("ts")
        try:
            when = datetime.fromisoformat(str(ts)).timestamp()
        except (ValueError, TypeError):
            continue
        if when >= cutoff:
            count += 1
    return count


def breaker_open(component: str) -> bool:
    """True when the missing-tool circuit-breaker for a component is open.

    A repeatedly-missing tool must not loop (re-invoke -> fail -> retry). Once
    the breaker is open the caller short-circuits to the friendly line without
    touching the tool again.
    """
    return _recent_missing_tool_count(component) >= _BREAKER_THRESHOLD


def handle_fatal(component: str, exc: BaseException, trigger: str) -> str:
    """Top-level uncaught-exception handler. Log, return a friendly line, never raise.

    The returned string is what the script prints to STDOUT before exiting 0. It
    contains no exception class, file path, or traceback.
    """
    import traceback

    raw = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    error_class = classify(exc)
    log_error(
        component,
        error_class=error_class,
        message=_sanitize(component, error_class),
        raw=raw,
        trigger=trigger,
        check="runtime",
        level="error",
        action_taken="abort+notify",
    )
    return _friendly_line(component)


def log_subprocess_error(
    component: str,
    cmd: list[str],
    returncode: int,
    stderr: str,
    trigger: str,
) -> None:
    """Log a failed subprocess. Never echo raw stderr to stdout (U1 §6a)."""
    error_class = classify(None, stderr=stderr)
    log_error(
        component,
        error_class=error_class,
        message=_sanitize(component, error_class),
        raw=stderr,
        trigger=trigger,
        check="subprocess",
        level="error",
        action_taken="abort+notify",
    )


def log_degraded(
    component: str,
    exc: BaseException,
    *,
    trigger: str,
    check: str,
) -> None:
    """Log a soft, in-process failure that degrades a section rather than aborting.

    The symmetric public entry point for a partial failure (e.g. a calendar
    fetch raising mid-render): the ritual continues, one section renders a
    degraded notice, and the real error is recorded -- never forwarded. Callers
    pass the raw exception; classification + sanitization stay inside the
    envelope so no caller reaches into its internals. A captured subprocess
    stderr (on ``CalledProcessError``/``TimeoutExpired``) is folded into both the
    classification and the on-disk ``raw`` field so an auth/quota exit is labelled
    accurately. Not mirrored to the ledger (a degraded section is not an abort).
    """
    captured = getattr(exc, "stderr", None) or ""
    error_class = classify(exc, stderr=str(captured))
    log_error(
        component,
        error_class=error_class,
        message=_sanitize(component, error_class),
        raw=f"{type(exc).__name__}: {exc}\n{captured}".strip(),
        trigger=trigger,
        check=check,
        level="warn",
        action_taken="degraded+logged",
        to_ledger=False,
    )


def run_main(component: str, main_func, trigger: str | None = None) -> int:
    """Run a script's ``main()`` inside the envelope. Returns the exit code.

    On success: returns 0 (or whatever ``main`` returns, defaulting to 0).
    On any uncaught exception: logs it, prints ONE friendly line to stdout, and
    returns 0 so the agent relay never sees a non-zero exit or raw output.

    ``SystemExit`` from argparse (``--help``, bad args) is re-raised untouched so
    CLI ergonomics are preserved; only genuine exceptions are enveloped.

    There is no missing-tool breaker at this layer: a top-level ritual that
    raises does so as a Python exception, not a repeated missing-tool subprocess
    loop. The breaker lives where the actual loop risk is -- ``get_calendar_events``
    short-circuits on an open ``calendar_fetch`` breaker so a missing ``gog`` does
    not re-invoke every run.
    """
    resolved_trigger = trigger or f"user_command:/{component}"
    try:
        result = main_func()
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 - this IS the catch-all boundary
        print(handle_fatal(component, exc, resolved_trigger))
        return 0
    return int(result) if isinstance(result, int) else 0


# --- CLI (used by telegram-commands.sh run_with_envelope) ------------------

def _cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Structured error-log writer.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("log-subprocess", help="Log a failed subprocess.")
    sp.add_argument("--component", required=True)
    sp.add_argument("--exit-code", type=int, required=True)
    sp.add_argument("--stderr-file", required=True)
    sp.add_argument("--trigger", required=True)
    sp.add_argument("--cmd", default="")

    fp = sub.add_parser("friendly-line", help="Print the friendly notice for a component.")
    fp.add_argument("--component", required=True)

    args = parser.parse_args(argv)
    if args.cmd == "log-subprocess":
        try:
            stderr = Path(args.stderr_file).read_text(encoding="utf-8", errors="replace")
        except OSError:
            stderr = ""
        cmd_list = args.cmd.split() if args.cmd else []
        log_subprocess_error(
            args.component, cmd_list, args.exit_code, stderr, args.trigger
        )
        return 0
    if args.cmd == "friendly-line":
        # Single source of truth for the notice + retry-command mapping, so the
        # shell wrapper never drifts from the Python envelope's _RETRY_COMMAND.
        print(_friendly_line(args.component))
        return 0
    return 1


if __name__ == "__main__":
    # The CLI itself must never leak a traceback; wrap it too.
    try:
        sys.exit(_cli(sys.argv[1:]))
    except SystemExit:
        raise
    except BaseException:  # noqa: BLE001
        print("[task-tracker] error-envelope CLI failed", file=sys.stderr)
        sys.exit(0)
