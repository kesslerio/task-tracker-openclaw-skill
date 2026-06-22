"""U1 pre-ritual preflight tests (Decision #6, Option A).

Asserts:
- a HARD failure exits 0 with friendly stdout that NAMES the failed check (the
  static cron fallback cannot name it);
- a hard failure logs a preflight_fail entry to the structured error log and a
  system_error event to the ledger;
- a SOFT failure (STANDUP_CALENDARS unset) does NOT abort (exit 0) and reports
  once, then stays silent on a stable status;
- preflight writes the status file under the state dir and NEVER touches
  HEARTBEAT.md;
- --strict-exit returns 1 on a hard failure (diagnostic) while stdout is
  unchanged;
- a denied cron job (delivery.to null) surfaces a delivery-targets warning
  without modifying the job.
"""

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))

import preflight  # noqa: E402
import task_ledger  # noqa: E402


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Pin state dir + error log + ledger into tmp; remove ambient work file."""
    state = tmp_path / "state"
    monkeypatch.setenv("TASK_MGMT_STATE_DIR", str(state))
    monkeypatch.setenv("TASK_TRACKER_ERROR_LOG", str(state / "task-tracker-errors.jsonl"))
    monkeypatch.setenv("TASK_TRACKER_LEDGER_FILE", str(state / "events.jsonl"))
    monkeypatch.setenv("TASK_TRACKER_PREFLIGHT_STATE", str(state / "preflight-state.json"))
    monkeypatch.delenv("STANDUP_CALENDARS", raising=False)
    monkeypatch.delenv("TASK_TRACKER_WORK_FILE", raising=False)
    return tmp_path


def _good_work_file(tmp_path, monkeypatch) -> Path:
    wf = tmp_path / "Weekly TODOs.md"
    wf.write_text("# tasks\n")
    monkeypatch.setenv("TASK_TRACKER_WORK_FILE", str(wf))
    return wf


# --- T3: hard failure aborts loudly, exits 0, names the check ---------------


def test_hard_failure_exits_zero_and_names_check(env, monkeypatch, capsys):
    monkeypatch.setenv("TASK_TRACKER_WORK_FILE", str(env / "missing.md"))
    rc = preflight.main(["--boot"])
    out = capsys.readouterr().out
    assert rc == 0  # Decision #6: exit 0 so the agent forwards named-check stdout
    assert "preflight" in out
    assert "work_file" in out
    # never leak the raw file path
    assert str(env / "missing.md") not in out


def test_hard_failure_logs_error_and_ledger(env, monkeypatch):
    monkeypatch.setenv("TASK_TRACKER_WORK_FILE", str(env / "missing.md"))
    preflight.main(["--boot"])

    error_log = Path(env / "state" / "task-tracker-errors.jsonl")
    entries = [json.loads(ln) for ln in error_log.read_text().splitlines() if ln.strip()]
    work_fail = [e for e in entries if e["check"] == "work_file"]
    assert work_fail, "no preflight_fail logged for work_file"
    assert work_fail[0]["level"] == "preflight_fail"
    assert work_fail[0]["component"] == "preflight"

    events = task_ledger.read_events(Path(env / "state" / "events.jsonl"))
    sys_errors = [e for e in events if e["event_type"] == "system_error"]
    assert sys_errors, "no system_error event appended to ledger"
    assert sys_errors[0]["metadata"]["check"] == "work_file"
    assert sys_errors[0]["actor"] == "task-tracker-preflight"


def test_persistent_hard_failure_does_not_re_append_ledger(env, monkeypatch):
    # A stable hard fault must mirror to the append-only ledger ONCE (on the
    # transition into failure), not on every heartbeat -- otherwise a permanently
    # missing work file bloats the shared queryable history unboundedly.
    monkeypatch.setenv("TASK_TRACKER_WORK_FILE", str(env / "missing.md"))
    preflight.main(["--boot"])
    preflight.main(["--boot"])
    preflight.main(["--boot"])

    events = task_ledger.read_events(Path(env / "state" / "events.jsonl"))
    work_errors = [
        e
        for e in events
        if e["event_type"] == "system_error" and e["metadata"]["check"] == "work_file"
    ]
    assert len(work_errors) == 1, "persistent fault must mirror to the ledger only once"


def test_strict_exit_returns_one_on_hard_fail(env, monkeypatch, capsys):
    monkeypatch.setenv("TASK_TRACKER_WORK_FILE", str(env / "missing.md"))
    rc = preflight.main(["--strict-exit"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "work_file" in out


def test_preflight_writes_status_file_under_state_dir(env, monkeypatch):
    _good_work_file(env, monkeypatch)
    preflight.main(["--boot"])
    state_file = Path(env / "state" / "preflight-state.json")
    assert state_file.exists()
    data = json.loads(state_file.read_text())
    assert "checks" in data
    assert "work_file" in data["checks"]


def test_preflight_never_writes_heartbeat(env, monkeypatch, tmp_path):
    # Option A: preflight must never edit a HEARTBEAT.md. Plant one and assert it
    # is byte-identical after a run (and a hard-fail run).
    heartbeat = tmp_path / "HEARTBEAT.md"
    heartbeat.write_text("ORIGINAL HEARTBEAT\n")
    before = heartbeat.read_bytes()
    monkeypatch.setenv("TASK_TRACKER_WORK_FILE", str(env / "missing.md"))
    preflight.main(["--boot"])
    assert heartbeat.read_bytes() == before


# --- T4: soft failure degrades, reports once, then silent -------------------


def test_soft_failure_does_not_abort(env, monkeypatch, capsys):
    _good_work_file(env, monkeypatch)
    monkeypatch.delenv("STANDUP_CALENDARS", raising=False)
    rc = preflight.main(["--boot"])
    out = capsys.readouterr().out
    assert rc == 0
    # first boot reports the warn line
    assert "STANDUP_CALENDARS" in out
    state = json.loads(Path(env / "state" / "preflight-state.json").read_text())
    assert state["checks"]["standup_calendars"]["last_status"] == "warn"


def test_stable_status_is_silent_on_second_boot(env, monkeypatch, capsys):
    _good_work_file(env, monkeypatch)
    preflight.main(["--boot"])
    capsys.readouterr()  # drain first report
    rc = preflight.main(["--boot"])
    out = capsys.readouterr().out
    assert rc == 0
    # nothing changed -> boot mode stays silent (report-on-change only)
    assert out.strip() == ""


# --- T5: cron delivery-target audit is read-only, surfaces denied job -------


def test_delivery_target_audit_flags_null_to_without_modifying(monkeypatch):
    jobs = [
        {"id": "good", "delivery": {"to": "x:y:z", "channel": "telegram"}, "agentId": "niemand-work"},
        {"id": "bad", "delivery": {"to": None, "channel": "last"}, "agentId": None},
        # an absent channel is just as under-specified as the literal "last".
        {"id": "nochan", "delivery": {"to": "a:b:c"}, "agentId": "niemand-work"},
    ]
    snapshot = json.dumps(jobs, sort_keys=True)
    warnings = preflight._validate_delivery_targets(jobs)
    # job not mutated
    assert json.dumps(jobs, sort_keys=True) == snapshot
    assert any("bad" in w and "delivery.to" in w for w in warnings)
    assert any("agentId" in w for w in warnings)
    # both the literal "last" AND an absent channel are flagged as not explicit.
    assert any("bad" in w and "channel" in w for w in warnings)
    assert any("nochan" in w and "channel" in w for w in warnings)
    # the explicit-telegram "good" job produces NO channel warning.
    assert not any("good" in w and "channel" in w for w in warnings)


def test_delivery_target_audit_skips_command_crons(monkeypatch):
    """V1: a command cron (the deterministic check-in dispatcher) owns its own send,
    so it has no agentId/delivery BY DESIGN -- it must NOT be flagged as a finding."""
    jobs = [
        {"id": "checkin", "schedule": {"kind": "at", "at": "2026-06-19T09:00:00+00:00"},
         "deleteAfterRun": True,
         "payload": {"kind": "command", "argv": ["sh", "-lc", "bash telegram-commands.sh checkin-dispatch st_x tsk_x 30 true start"]}},
    ]
    warnings = preflight._validate_delivery_targets(jobs)
    assert warnings == []  # a command cron is not a delivery-target finding


def test_delivery_target_check_warns_when_cron_unavailable(env, monkeypatch):
    # No openclaw binary in a clean test env -> SOFT warn, never a hard fail.
    monkeypatch.setattr(preflight.shutil, "which", lambda name: None)
    status, detail = preflight._check_delivery_targets()
    assert status == preflight.WARN
    assert "unavailable" in detail


def test_json_output_shape(env, monkeypatch, capsys):
    _good_work_file(env, monkeypatch)
    rc = preflight.main(["--json"])
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    assert "rows" in payload and "hard_failure" in payload
    checks = {r["check"] for r in payload["rows"]}
    assert {"work_file", "ledger_writable", "python_imports"} <= checks
