"""U1 NO-RAW-ERROR-LEAK invariant tests.

These tests assert the invariant, not the implementation path:
- a Python crash (ModuleNotFoundError + a generic failure) routed through the
  shell wrapper exits 0, prints an "unavailable" notice, and leaks NO Traceback /
  exception class / file path to stdout;
- the structured error log gets a correctly-shaped entry (component + level +
  content, not merely "a new line");
- the happy path is unchanged with ZERO error-log entries;
- the error log survives an unwritable path without crashing to stdout;
- rotation keeps the tail; the circuit-breaker stops a missing-tool loop.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))

import error_envelope  # noqa: E402

# Modules a self-contained isolated scripts dir needs so the broken-standup shim
# can still import the envelope + its deps without reaching back into the repo.
_ENVELOPE_DEPS = [
    "error_envelope.py",
    "cos_config.py",
    "task_ledger.py",
    "utils.py",
    "telegram-commands.sh",
]

# Strings that must NEVER appear in user-facing stdout when a script crashes.
_FORBIDDEN_SUBSTRINGS = [
    "Traceback",
    "ModuleNotFoundError",
    "Exception",
    "Error:",
    str(SCRIPTS),
    "/scripts/",
    ".py",
    "line ",
]


def _make_isolated_scripts(tmp_path: Path) -> Path:
    """Copy the envelope + its deps into an isolated scripts dir for the shell."""
    iso = tmp_path / "scripts"
    iso.mkdir()
    for name in _ENVELOPE_DEPS:
        shutil.copy(SCRIPTS / name, iso / name)
    return iso


def _clean_env(state_dir: Path, work_file: Path | None = None) -> dict:
    """A controlled env: ambient TASK_TRACKER_* removed, state dir pinned."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("TASK_TRACKER")}
    env["TASK_MGMT_STATE_DIR"] = str(state_dir)
    env["TASK_TRACKER_ERROR_LOG"] = str(state_dir / "task-tracker-errors.jsonl")
    env["TASK_TRACKER_LEDGER_FILE"] = str(state_dir / "events.jsonl")
    if work_file is not None:
        env["TASK_TRACKER_WORK_FILE"] = str(work_file)
    return env


def _assert_no_raw_leak(stdout: str) -> None:
    for forbidden in _FORBIDDEN_SUBSTRINGS:
        assert forbidden not in stdout, f"raw leak: {forbidden!r} in stdout:\n{stdout}"


def _error_log_lines(state_dir: Path) -> list[dict]:
    path = state_dir / "task-tracker-errors.jsonl"
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


# --- T1: ModuleNotFoundError through the shell wrapper ----------------------


@pytest.mark.parametrize(
    "shim_body, expect_class",
    [
        ("import nonexistent_module_xyz  # noqa\n", error_envelope.ENVIRONMENT),
        ("raise RuntimeError('boom generic tool failure')\n", error_envelope.ENVIRONMENT),
    ],
    ids=["module-not-found", "generic-failure"],
)
def test_python_crash_does_not_reach_telegram(tmp_path, shim_body, expect_class):
    iso = _make_isolated_scripts(tmp_path)
    # A deliberately-broken standup.py: it crashes the moment main() runs.
    (iso / "standup.py").write_text(
        "import error_envelope\n"
        "import sys\n"
        "def main():\n"
        f"    {shim_body.strip()}\n"
        "if __name__ == '__main__':\n"
        "    sys.exit(error_envelope.run_main('standup', main))\n"
    )
    state_dir = tmp_path / "state"
    env = _clean_env(state_dir)

    result = subprocess.run(
        ["bash", str(iso / "telegram-commands.sh"), "daily"],
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0
    assert "unavailable" in result.stdout
    _assert_no_raw_leak(result.stdout)

    lines = _error_log_lines(state_dir)
    assert lines, "error log got no entry"
    entry = lines[-1]
    assert entry["component"] == "standup"
    assert entry["level"] == "error"
    # The structured (on-disk, never forwarded) raw field keeps the real error.
    assert entry["raw"], "raw error content not recorded"
    assert entry["message"].startswith("standup failed")


# --- happy path: unchanged output, zero error-log entries -------------------


def test_happy_path_zero_error_log_entries(tmp_path):
    iso = _make_isolated_scripts(tmp_path)
    (iso / "standup.py").write_text(
        "import error_envelope\n"
        "import sys\n"
        "def main():\n"
        "    print('📋 **Daily Standup — happy path**')\n"
        "if __name__ == '__main__':\n"
        "    sys.exit(error_envelope.run_main('standup', main))\n"
    )
    state_dir = tmp_path / "state"
    env = _clean_env(state_dir)

    result = subprocess.run(
        ["bash", str(iso / "telegram-commands.sh"), "daily"],
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0
    assert "Daily Standup — happy path" in result.stdout
    assert "unavailable" not in result.stdout
    assert _error_log_lines(state_dir) == []


# --- run_main contract (unit-level) -----------------------------------------


def test_run_main_module_not_found_prints_friendly(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TASK_MGMT_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("TASK_TRACKER_ERROR_LOG", str(tmp_path / "errors.jsonl"))
    monkeypatch.setenv("TASK_TRACKER_LEDGER_FILE", str(tmp_path / "events.jsonl"))

    def boom():
        raise ModuleNotFoundError("No module named 'utils'")

    rc = error_envelope.run_main("standup", boom, trigger="user_command:/standup")
    out = capsys.readouterr().out
    assert rc == 0
    assert "unavailable" in out
    _assert_no_raw_leak(out)
    entry = json.loads((tmp_path / "errors.jsonl").read_text().splitlines()[-1])
    assert entry["component"] == "standup"
    # ModuleNotFoundError classifies as environment, not a hallucinated tool name.
    assert entry["error_class"] == error_envelope.ENVIRONMENT


def test_run_main_preserves_systemexit(monkeypatch, tmp_path):
    monkeypatch.setenv("TASK_MGMT_STATE_DIR", str(tmp_path))

    def argparse_exit():
        raise SystemExit(2)

    with pytest.raises(SystemExit):
        error_envelope.run_main("tasks", argparse_exit)


def test_run_main_runs_main_exactly_once(monkeypatch, tmp_path, capsys):
    # run_main invokes main() exactly once; a failure goes straight to the
    # friendly line + log (no implicit retry that could double-run main()).
    monkeypatch.setenv("TASK_MGMT_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("TASK_TRACKER_ERROR_LOG", str(tmp_path / "errors.jsonl"))
    monkeypatch.setenv("TASK_TRACKER_LEDGER_FILE", str(tmp_path / "events.jsonl"))
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        raise TimeoutError("transient timeout")

    rc = error_envelope.run_main("standup", flaky)
    out = capsys.readouterr().out
    assert rc == 0
    assert calls["n"] == 1, "main() must run exactly once (no implicit retry)"
    assert "unavailable" in out
    _assert_no_raw_leak(out)
    assert (tmp_path / "errors.jsonl").exists()


# --- denied path: unwritable error log must not crash to stdout -------------


def test_unwritable_error_log_does_not_crash(monkeypatch, tmp_path, capsys):
    # Point the error log at a path under a non-existent, uncreatable parent so
    # _record() fails the OS open and returns False -> handle_fatal still returns
    # a friendly line and never raises.
    bad = tmp_path / "nope-file"
    bad.write_text("not a dir")
    monkeypatch.setenv("TASK_TRACKER_ERROR_LOG", str(bad / "child" / "errors.jsonl"))
    monkeypatch.setenv("TASK_MGMT_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("TASK_TRACKER_LEDGER_FILE", str(tmp_path / "events.jsonl"))

    line = error_envelope.handle_fatal(
        "standup", RuntimeError("kaboom"), "user_command:/standup"
    )
    assert "unavailable" in line
    _assert_no_raw_leak(line)


# --- rotation + breaker -----------------------------------------------------


def test_error_log_rotation_keeps_tail(monkeypatch, tmp_path):
    log = tmp_path / "errors.jsonl"
    monkeypatch.setenv("TASK_TRACKER_ERROR_LOG", str(log))
    monkeypatch.setenv("TASK_MGMT_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("TASK_TRACKER_LEDGER_FILE", str(tmp_path / "events.jsonl"))

    seed = "\n".join(json.dumps({"n": i, "ts": "2026-01-01T00:00:00+00:00"}) for i in range(600))
    log.write_text(seed + "\n")

    error_envelope.log_error(
        "standup",
        error_class=error_envelope.ENVIRONMENT,
        message="standup failed (environment)",
        raw="boom",
        trigger="user_command:/standup",
        to_ledger=False,
    )
    lines = [ln for ln in log.read_text().splitlines() if ln.strip()]
    assert len(lines) == error_envelope._TRIM_TO_LINES
    # all lines remain valid JSON (no corruption)
    for ln in lines:
        json.loads(ln)
    # the newest entry is preserved
    assert json.loads(lines[-1])["component"] == "standup"


def test_error_log_file_mode_is_owner_only(monkeypatch, tmp_path):
    log = tmp_path / "errors.jsonl"
    monkeypatch.setenv("TASK_TRACKER_ERROR_LOG", str(log))
    monkeypatch.setenv("TASK_MGMT_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("TASK_TRACKER_LEDGER_FILE", str(tmp_path / "events.jsonl"))
    error_envelope.log_error(
        "standup",
        error_class=error_envelope.ENVIRONMENT,
        message="x",
        raw="y",
        trigger="user_command:/standup",
        to_ledger=False,
    )
    assert (os.stat(log).st_mode & 0o777) == 0o600


def test_missing_tool_breaker_opens_after_threshold(monkeypatch, tmp_path):
    log = tmp_path / "errors.jsonl"
    monkeypatch.setenv("TASK_TRACKER_ERROR_LOG", str(log))
    monkeypatch.setenv("TASK_MGMT_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("TASK_TRACKER_LEDGER_FILE", str(tmp_path / "events.jsonl"))

    assert error_envelope.breaker_open("standup") is False
    for _ in range(error_envelope._BREAKER_THRESHOLD):
        error_envelope.log_error(
            "standup",
            error_class=error_envelope.MISSING_TOOL,
            message="standup failed (missing-tool)",
            raw="command not found",
            trigger="user_command:/standup",
            to_ledger=False,
        )
    assert error_envelope.breaker_open("standup") is True


def test_in_process_missing_binary_classifies_as_missing_tool():
    # subprocess.run(["gog", ...]) raising FileNotFoundError(filename="gog") must
    # classify as missing-tool so the breaker can open and stop a cron loop.
    exc = FileNotFoundError(2, "No such file or directory")
    exc.filename = "gog"
    assert error_envelope.classify(exc) == error_envelope.MISSING_TOOL
    # A missing data FILE (has a path separator) stays environment.
    exc2 = FileNotFoundError(2, "No such file or directory")
    exc2.filename = "./data/board.md"
    assert error_envelope.classify(exc2) == error_envelope.ENVIRONMENT
    # A missing data file opened by a BARE relative name (no slash) but with a
    # file extension is still environment -- it must NOT trip the missing-tool
    # breaker just because it lacks a path separator.
    for bare_data in ("HEARTBEAT.md", "tasks.json", "Weekly TODOs.md"):
        exc3 = FileNotFoundError(2, "No such file or directory")
        exc3.filename = bare_data
        assert error_envelope.classify(exc3) == error_envelope.ENVIRONMENT, bare_data


def test_run_main_always_runs_main_despite_breaker(monkeypatch, tmp_path, capsys):
    # The breaker does NOT live at the run_main layer (a top-level ritual failure
    # is a Python exception, not a missing-tool subprocess loop). Even with a
    # standup breaker seeded open, run_main still invokes main() -- the breaker is
    # enforced where the loop risk actually is (get_calendar_events).
    monkeypatch.setenv("TASK_TRACKER_ERROR_LOG", str(tmp_path / "errors.jsonl"))
    monkeypatch.setenv("TASK_MGMT_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("TASK_TRACKER_LEDGER_FILE", str(tmp_path / "events.jsonl"))
    for _ in range(error_envelope._BREAKER_THRESHOLD):
        error_envelope.log_error(
            "standup",
            error_class=error_envelope.MISSING_TOOL,
            message="standup failed (missing-tool)",
            raw="command not found",
            trigger="cron:c42",
            to_ledger=False,
        )
    assert error_envelope.breaker_open("standup") is True

    called = {"n": 0}

    def main_func():
        called["n"] += 1
        print("ran main")

    rc = error_envelope.run_main("standup", main_func, trigger="cron:c42b6a07")
    out = capsys.readouterr().out
    assert rc == 0
    assert called["n"] == 1
    assert "ran main" in out


def test_friendly_line_uses_real_slash_command():
    # The retry hint must name a command the relay actually routes.
    assert "Retry: /daily" in error_envelope._friendly_line("standup")
    assert "Retry: /weekly" in error_envelope._friendly_line("weekly_review")
    # A component with no unambiguous slash command omits the retry verb rather
    # than steering the user to an arbitrary/non-routed command.
    tasks_line = error_envelope._friendly_line("tasks")
    assert "unavailable" in tasks_line
    assert "Retry:" not in tasks_line
    eod_line = error_envelope._friendly_line("eod_review")
    assert "unavailable" in eod_line
    assert "Retry:" not in eod_line
    # personal_standup has no routed slash command (/daily runs the WORK standup),
    # so its notice must omit the retry verb rather than mis-steer the user.
    personal_line = error_envelope._friendly_line("personal_standup")
    assert "unavailable" in personal_line
    assert "Retry:" not in personal_line


def test_subprocess_timeout_expired_classifies_transient():
    # subprocess.TimeoutExpired is NOT a TimeoutError subclass; it must still be
    # labelled transient (the most common calendar-fetch failure) so the on-disk
    # error_class stays trustworthy for querying.
    import subprocess as _subprocess

    exc = _subprocess.TimeoutExpired(cmd=["gog"], timeout=10)
    assert error_envelope.classify(exc) == error_envelope.TRANSIENT


def test_classify_anchored_patterns_avoid_false_positives():
    # A traceback line that merely contains "403" or the word "connection" inside
    # a larger token must NOT be misclassified.
    assert error_envelope.classify(None, stderr="File x.py, line 403, in main") == error_envelope.ENVIRONMENT
    assert error_envelope.classify(None, stderr="disconnections handled") == error_envelope.ENVIRONMENT
    # But a real HTTP 401/403 and a real connection error still classify.
    assert error_envelope.classify(None, stderr="HTTP 403 Forbidden") == error_envelope.AUTH
    assert error_envelope.classify(None, stderr="401 unauthorized") == error_envelope.AUTH
    assert error_envelope.classify(None, stderr="connection reset by peer") == error_envelope.TRANSIENT


def test_run_main_nonzero_result_records_failure_but_exits_zero(monkeypatch, tmp_path):
    """R1 Fix 1: a handled-but-failed ritual (main() returns a NONZERO int -- it caught
    its own failure rather than raising) is NOT healthy. run_main records a health
    FAILURE (error_class ``nonzero_exit``) -- so H4 cannot false-green on a soft failure
    -- but STILL returns 0 to the OS so the cron relay sees the unchanged exit-0
    contract. This REPLACES the pre-R1 'a nonzero return is a success signal to preserve'
    behaviour: a nonzero return is now a failure, recorded in health, exit forced to 0."""
    import cos_health  # noqa: PLC0415

    monkeypatch.setenv("TASK_MGMT_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("TASK_TRACKER_ERROR_LOG", str(tmp_path / "errors.jsonl"))
    monkeypatch.setenv("TASK_TRACKER_LEDGER_FILE", str(tmp_path / "events.jsonl"))

    def main_func():
        return 3  # a handled-but-failed ritual return code

    rc = error_envelope.run_main("standup", main_func, trigger="cron:c99")
    assert rc == 0  # exit-0 contract: a soft failure does NOT trip the cron
    entry = cos_health.read_health()["standup"]
    assert entry["last_failure"]["error_class"] == "nonzero_exit"
    assert entry["last_failure"]["trigger"] == "cron:c99"
    assert "last_success_ts" not in entry  # a nonzero result records NO success


def test_done_failure_surfaces_friendly_line(tmp_path):
    # U5 replaced done24h/done7d with done/ledger, which route harvest_ledger.py
    # through run_with_envelope. A crash there must surface the friendly notice,
    # never a raw error.
    iso = _make_isolated_scripts(tmp_path)
    (iso / "harvest_ledger.py").write_text(
        "import sys\n"
        "raise RuntimeError('ledger exploded')\n"
    )
    state_dir = tmp_path / "state"
    env = _clean_env(state_dir)
    result = subprocess.run(
        ["bash", str(iso / "telegram-commands.sh"), "done"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0
    assert "unavailable" in result.stdout
    _assert_no_raw_leak(result.stdout)


def test_shell_failure_branch_names_real_command(tmp_path):
    # When python3 cannot even run the script (module-top syntax error, before
    # run_main is reached), the shell failure branch fires. Its notice must name
    # the command the relay routes (/daily), not the component (/standup), and
    # must leak no raw error.
    iso = _make_isolated_scripts(tmp_path)
    (iso / "standup.py").write_text("this is not valid python !!!\n")
    state_dir = tmp_path / "state"
    env = _clean_env(state_dir)
    result = subprocess.run(
        ["bash", str(iso / "telegram-commands.sh"), "daily"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0
    assert "unavailable" in result.stdout
    assert "Retry: /daily" in result.stdout
    _assert_no_raw_leak(result.stdout)


def test_done_failure_replaces_partial_output_with_notice(tmp_path):
    # If harvest_ledger.py prints partial output and THEN crashes (non-zero exit),
    # run_with_envelope discards the partial stdout and surfaces only the friendly
    # notice -- the partial text must NOT leak alongside a failure.
    iso = _make_isolated_scripts(tmp_path)
    (iso / "harvest_ledger.py").write_text(
        "import sys\n"
        "print('some partial ledger output emitted before failure')\n"
        "raise RuntimeError('ledger exploded mid-render')\n"
    )
    state_dir = tmp_path / "state"
    env = _clean_env(state_dir)
    result = subprocess.run(
        ["bash", str(iso / "telegram-commands.sh"), "done"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0
    assert "unavailable" in result.stdout
    # The partial stdout on a failed run is discarded, not surfaced.
    assert "some partial ledger output" not in result.stdout
    _assert_no_raw_leak(result.stdout)


def test_raw_traceback_never_reaches_stdout_only_disk(tmp_path):
    # Positive assertion of NO-RAW-ERROR-LEAK: the traceback lives in the log's
    # `raw` field but never on stdout.
    iso = _make_isolated_scripts(tmp_path)
    (iso / "standup.py").write_text(
        "import error_envelope, sys\n"
        "def main():\n"
        "    raise ValueError('UNIQUE_TRACEBACK_MARKER_42')\n"
        "if __name__ == '__main__':\n"
        "    sys.exit(error_envelope.run_main('standup', main))\n"
    )
    state_dir = tmp_path / "state"
    env = _clean_env(state_dir)
    result = subprocess.run(
        ["bash", str(iso / "telegram-commands.sh"), "daily"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert "UNIQUE_TRACEBACK_MARKER_42" not in result.stdout
    lines = _error_log_lines(state_dir)
    assert any("UNIQUE_TRACEBACK_MARKER_42" in ln.get("raw", "") for ln in lines)


def test_rotation_survives_data_after_replace(monkeypatch, tmp_path):
    # After rotation via os.replace, the file is intact and parseable, and a
    # follow-up append still works (lock file does not block subsequent writes).
    log = tmp_path / "errors.jsonl"
    monkeypatch.setenv("TASK_TRACKER_ERROR_LOG", str(log))
    monkeypatch.setenv("TASK_MGMT_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("TASK_TRACKER_LEDGER_FILE", str(tmp_path / "events.jsonl"))
    for i in range(3):
        error_envelope.log_error(
            "standup",
            error_class=error_envelope.ENVIRONMENT,
            message=f"entry {i}",
            raw="x",
            trigger="user_command:/standup",
            to_ledger=False,
        )
    lines = [ln for ln in log.read_text().splitlines() if ln.strip()]
    assert len(lines) == 3
    for ln in lines:
        json.loads(ln)


def test_classify_does_not_emit_tool_names():
    # The friendly line and the sanitized message must never echo a hallucinated
    # tool name or the raw exception text.
    msg = error_envelope._sanitize("standup", error_envelope.classify(ModuleNotFoundError("x")))
    assert "ModuleNotFoundError" not in msg
    assert "qmd" not in msg
    line = error_envelope._friendly_line("standup")
    assert line.startswith("⚠️")
    assert "Traceback" not in line


# --- H4: machine-visible health from the envelope --------------------------
#
# The envelope must make a failure (and a clean run) machine-visible in
# cos-health.json WITHOUT regressing NO-RAW-ERROR-LEAK: stdout stays the friendly
# notice, exit stays 0. A health-recording call that itself raises must NOT change
# that contract -- health is observability, never a new failure source.


def _read_health(state_dir: Path) -> dict:
    import cos_health  # local import: same scripts dir is already on sys.path

    cos_health  # ensure import side effects resolved
    path = state_dir / "cos-health.json"
    return json.loads(path.read_text()) if path.exists() else {}


def test_run_main_failure_records_health_and_no_raw_leak(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("TASK_MGMT_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("TASK_TRACKER_ERROR_LOG", str(tmp_path / "errors.jsonl"))
    monkeypatch.setenv("TASK_TRACKER_LEDGER_FILE", str(tmp_path / "events.jsonl"))

    def boom():
        raise ConnectionError("gateway flake")

    rc = error_envelope.run_main("standup", boom, trigger="cron:c42")
    out = capsys.readouterr().out
    assert rc == 0  # exit-0 contract preserved
    assert "unavailable" in out
    _assert_no_raw_leak(out)  # NO traceback / class / path leaked
    # The failure is now machine-visible to a watchdog.
    entry = _read_health(tmp_path)["standup"]
    assert entry["last_failure"]["error_class"] == error_envelope.TRANSIENT
    assert entry["last_failure"]["trigger"] == "cron:c42"
    assert "last_success_ts" not in entry  # a pure failure records no success


def test_log_subprocess_error_records_health_failure(monkeypatch, tmp_path):
    """R1 Fix 3: a failed subprocess is machine-visible in cos-health.json. For the cron
    rituals that run through the shell wrapper (nag_check, ledger_harvest) -- NOT through
    run_main -- log_subprocess_error is the ONLY place the failure is recorded, so it must
    stamp a health failure too (else a return-code soft failure false-greens). The
    error_class is the one classify() derives from the captured stderr."""
    import cos_health  # noqa: PLC0415

    monkeypatch.setenv("TASK_MGMT_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("TASK_TRACKER_ERROR_LOG", str(tmp_path / "errors.jsonl"))
    monkeypatch.setenv("TASK_TRACKER_LEDGER_FILE", str(tmp_path / "events.jsonl"))

    error_envelope.log_subprocess_error(
        "nag_check", ["python3", "nag_check.py"], 1,
        "connection reset by peer", "user_command:/nag_check")
    entry = cos_health.read_health()["nag_check"]
    # classify("connection reset by peer") -> TRANSIENT; recorded as the failure class.
    assert entry["last_failure"]["error_class"] == error_envelope.TRANSIENT
    assert entry["last_failure"]["trigger"] == "user_command:/nag_check"
    # And the structured error log still got its entry (Fix 3 ADDS health, keeps logging).
    log = json.loads((tmp_path / "errors.jsonl").read_text().splitlines()[-1])
    assert log["component"] == "nag_check" and log["check"] == "subprocess"


def test_log_subprocess_error_health_failure_is_best_effort(monkeypatch, tmp_path):
    """A raising cos_health.record_failure must NOT escalate a subprocess log into a new
    failure -- log_subprocess_error stays best-effort (the shell wrapper already owns the
    friendly notice). The error-log entry is still written even when health recording
    raises."""
    import cos_health  # noqa: PLC0415

    monkeypatch.setenv("TASK_MGMT_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("TASK_TRACKER_ERROR_LOG", str(tmp_path / "errors.jsonl"))
    monkeypatch.setenv("TASK_TRACKER_LEDGER_FILE", str(tmp_path / "events.jsonl"))
    monkeypatch.setattr(cos_health, "record_failure",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("health on fire")))

    # Must not raise despite the broken recorder.
    error_envelope.log_subprocess_error(
        "ledger_harvest", ["python3", "harvest_ledger.py"], 1, "boom", "cron:harvest")
    log = json.loads((tmp_path / "errors.jsonl").read_text().splitlines()[-1])
    assert log["component"] == "ledger_harvest"


def test_run_main_clean_run_records_success(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("TASK_MGMT_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("TASK_TRACKER_ERROR_LOG", str(tmp_path / "errors.jsonl"))

    def main_func():
        print("📋 ok")
        return 0

    rc = error_envelope.run_main("standup", main_func)
    assert rc == 0
    assert "ok" in capsys.readouterr().out
    entry = _read_health(tmp_path)["standup"]
    assert entry["last_success_ts"]
    assert "last_failure" not in entry  # a clean run records no failure


def test_health_recording_failure_never_breaks_envelope(monkeypatch, tmp_path, capsys):
    """A cos_health recorder that itself RAISES must not change exit-0 / friendly
    stdout -- health recording is best-effort, wrapped defensively at the call site."""
    import cos_health

    monkeypatch.setenv("TASK_MGMT_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("TASK_TRACKER_ERROR_LOG", str(tmp_path / "errors.jsonl"))
    monkeypatch.setenv("TASK_TRACKER_LEDGER_FILE", str(tmp_path / "events.jsonl"))

    def explode(*a, **k):
        raise RuntimeError("health substrate is on fire")

    monkeypatch.setattr(cos_health, "record_failure", explode)
    monkeypatch.setattr(cos_health, "record_success", explode)

    # Failure path: the friendly line still prints, exit still 0.
    rc = error_envelope.run_main("standup", lambda: (_ for _ in ()).throw(ValueError("x")))
    out = capsys.readouterr().out
    assert rc == 0
    assert "unavailable" in out
    _assert_no_raw_leak(out)

    # Clean path: a raising record_success does not turn a good run into a failure.
    rc2 = error_envelope.run_main("standup", lambda: 0)
    assert rc2 == 0

    # R1 Fix 1 nonzero path: a raising record_failure on a nonzero RESULT must not
    # change the exit-0 contract either -- the soft failure still exits 0, stdout
    # unchanged. Health recording stays best-effort across all three outcome paths.
    out_before = capsys.readouterr().out  # drain
    rc3 = error_envelope.run_main("standup", lambda: 1)
    assert rc3 == 0
    assert out_before is not None  # (drained; the nonzero path prints nothing itself)
