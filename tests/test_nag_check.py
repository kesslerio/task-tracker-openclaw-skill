"""U4 nag engine: NAG-CLOSES-ONLY-ON-ACK + DELIVERY-TARGET-PROOF + REVERSIBILITY.

These tests assert the unit INVARIANTS on the denied paths, not just the happy
path:

* NAG-CLOSES-ONLY-ON-ACK -- a crash mid-run leaves the loop open; a delivery block
  (unset env / work group) leaves the loop OPEN and never silently clears; a
  snooze pauses but does not close; only /done /reschedule / verified-done close.
* DELIVERY-TARGET-PROOF -- no push without a proven, gated, asserted target; an
  unset env => nag_delivery_blocked:env_missing, zero sends.
* REVERSIBILITY -- the board file is never written by the nag engine (mtime
  unchanged after every run).

Fake chat ids: valid chat-id shape (^-?\\d+$) but NOT matching the public-hygiene
grep (-100[0-9]{8,}); real ids are env-sourced, never committed.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import utils  # noqa: E402
import task_records  # noqa: E402
import task_ledger  # noqa: E402
import nag_state  # noqa: E402
import nag_check  # noqa: E402

PRODUCTIVITY = "-4242424242"
WORK_GROUP = "-5252525252"
REF = datetime(2026, 6, 19, tzinfo=timezone.utc)

BOARD = """# Work

## 🟡 Q2
- [ ] **Re-evaluate ActiveCampaign** task_id::tsk_abc123 🗓️2026-06-15 area:: Marketing
"""


def _set_env(monkeypatch, board_path, state_dir, *, productivity=PRODUCTIVITY):
    monkeypatch.setenv("TASK_TRACKER_WORK_FILE", str(board_path))
    monkeypatch.setenv("TASK_TRACKER_LEDGER_FILE", str(state_dir / "events.jsonl"))
    monkeypatch.setenv("TASK_MGMT_STATE_DIR", str(state_dir))
    monkeypatch.setenv("TELEGRAM_CHAT_ID_PRODUCTIVITY", productivity)
    monkeypatch.setenv("TELEGRAM_CHAT_ID_WORK", WORK_GROUP)
    monkeypatch.setenv("OPENCLAW_TOPIC_PRODUCTIVITY_STANDUP", "2")
    # OBSIDIAN_WORK is resolved at import time; rebind it to the test board.
    monkeypatch.setattr(utils, "OBSIDIAN_WORK", Path(board_path))


@pytest.fixture
def harness(tmp_path, monkeypatch):
    board = tmp_path / "Work Tasks.md"
    board.write_text(BOARD, encoding="utf-8")
    state = tmp_path / "state"
    _set_env(monkeypatch, board, state)
    monkeypatch.setattr(nag_check, "_today", lambda: REF)
    return board, state


def _run(harness):
    """Run one nag-check pass; return (counts, sends-for-THIS-run-only).

    A fresh send list per call so a test can assert what a SINGLE run pushed,
    independent of prior runs.
    """
    sent: list[tuple] = []
    counts = nag_check.run_nag_check(send=lambda target, text: sent.append((target, text)))
    return counts, sent


def _events(state):
    return task_ledger.read_events(state / "events.jsonl")


def _state(state):
    path = state / "nag-state.json"
    return json.loads(path.read_text()) if path.exists() else {}


# --- T1: nag fires on first threshold crossing -----------------------------

def test_t1_nag_fires_on_first_threshold_crossing(harness):
    board, state = harness
    counts, sent = _run(harness)
    assert counts["sent"] == 1
    assert len(sent) == 1
    target, _text = sent[0]
    assert target == {"chat_id": PRODUCTIVITY, "topic_id": "2",
                      "agent_id": "niemand-work", "channel": "telegram"}
    nag = _state(state)["tsk_abc123"]
    assert nag["ack"] is False
    types = [e["event_type"] for e in _events(state)]
    assert "nag_opened" in types and "nag_sent" in types


def test_t1_board_mtime_unchanged(harness):
    """REVERSIBILITY (T5): nag-check NEVER writes the board."""
    board, state = harness
    before = board.stat().st_mtime_ns
    before_text = board.read_text()
    _run(harness)
    assert board.stat().st_mtime_ns == before
    assert board.read_text() == before_text


# --- T2: nag does NOT close without ack (invariant) ------------------------

def test_t2_nag_refires_without_ack(harness):
    board, state = harness
    _run(harness)
    counts2, sent2 = _run(harness)
    assert counts2["sent"] == 1
    nag = _state(state)["tsk_abc123"]
    assert nag["ack"] is False
    assert nag["nag_count"] == 2  # fired twice, never closed
    sent_events = [e for e in _events(state) if e["event_type"] == "nag_sent"]
    assert len(sent_events) == 2


# --- T3: verified-done closes the loop (Path B) ----------------------------

def test_t3_verified_done_closes_without_push(harness):
    board, state = harness
    _run(harness)
    # Task disappears from the board (completed elsewhere).
    board.write_text("# Work\n\n## 🟡 Q2\n", encoding="utf-8")
    counts2, sent2 = _run(harness)
    assert sent2 == []  # no push on close
    assert counts2["closed"] == 1
    nag = _state(state)["tsk_abc123"]
    assert nag["ack"] is True
    assert nag["closed_by"] == "verified_done"
    acked = [e for e in _events(state) if e["event_type"] == "nag_acked"]
    assert acked and acked[-1]["metadata"]["closed_by"] == "verified_done"


# --- T4: DENIED -- delivery target not provable (DELIVERY-TARGET-PROOF) -----

def test_t4_env_missing_blocks_push_and_leaves_loop_open(tmp_path, monkeypatch):
    board = tmp_path / "Work Tasks.md"
    board.write_text(BOARD, encoding="utf-8")
    state = tmp_path / "state"
    _set_env(monkeypatch, board, state)
    monkeypatch.delenv("TELEGRAM_CHAT_ID_PRODUCTIVITY", raising=False)  # unset
    monkeypatch.setattr(nag_check, "_today", lambda: REF)

    sent = []
    counts = nag_check.run_nag_check(send=lambda t, x: sent.append((t, x)))

    assert sent == []  # zero Telegram messages
    assert counts["blocked"] == 1 and counts["sent"] == 0
    blocked = [e for e in task_ledger.read_events(state / "events.jsonl")
               if e["event_type"] == "nag_delivery_blocked"]
    assert blocked and blocked[0]["metadata"]["reason"] == "env_missing"
    # The nag STAYS OPEN -- env_missing never clears a loop (no entry was created,
    # so there is nothing acked; on the next fire with env set it opens).
    on_disk = _state(state)
    assert "tsk_abc123" not in on_disk or on_disk["tsk_abc123"]["ack"] is False


def test_t4_open_loop_stays_open_when_env_drops_mid_life(harness, monkeypatch):
    """An already-open loop must NOT be cleared when a later fire loses the env."""
    board, state = harness
    _run(harness)  # open the loop with env set
    assert _state(state)["tsk_abc123"]["ack"] is False
    monkeypatch.delenv("TELEGRAM_CHAT_ID_PRODUCTIVITY", raising=False)
    sent2 = []
    nag_check.run_nag_check(send=lambda t, x: sent2.append((t, x)))
    assert sent2 == []
    assert _state(state)["tsk_abc123"]["ack"] is False  # still open


def test_t4_work_group_target_blocks_push(tmp_path, monkeypatch):
    """The Work group is rejected -- a productivity nag must not ride it."""
    board = tmp_path / "Work Tasks.md"
    board.write_text(BOARD, encoding="utf-8")
    state = tmp_path / "state"
    # Point the productivity chat env AT the work group: prove_delivery_target
    # must reject it as work_group, blocking the push.
    _set_env(monkeypatch, board, state, productivity=WORK_GROUP)
    monkeypatch.setattr(nag_check, "_today", lambda: REF)
    sent = []
    counts = nag_check.run_nag_check(send=lambda t, x: sent.append((t, x)))
    assert sent == []
    assert counts["blocked"] == 1
    blocked = [e for e in task_ledger.read_events(state / "events.jsonl")
               if e["event_type"] == "nag_delivery_blocked"]
    assert blocked and blocked[0]["metadata"]["reason"] == "work_group"


# --- T9 / NO-RAW-ERROR-LEAK + crash leaves loop open -----------------------

def test_t9_corrupt_state_via_main_emits_safe_envelope(tmp_path, monkeypatch, capsys):
    board = tmp_path / "Work Tasks.md"
    board.write_text(BOARD, encoding="utf-8")
    state = tmp_path / "state"
    _set_env(monkeypatch, board, state)
    monkeypatch.setattr(nag_check, "_today", lambda: REF)
    # Force a crash deep in the run AFTER state is read but before any push.
    monkeypatch.setattr(nag_check, "load_records",
                        lambda personal=False: (_ for _ in ()).throw(RuntimeError("boom")))
    rc = nag_check.main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert nag_check.SAFE_ENVELOPE in out
    assert "Traceback" not in out and "RuntimeError" not in out


def test_crash_mid_run_leaves_open_loop_open(harness, monkeypatch):
    """NAG-CLOSES-ONLY-ON-ACK: a crash after state-read but before push must not
    close an open loop; the next clean run finds it open and re-fires."""
    board, state = harness
    _run(harness)  # loop is open
    assert _state(state)["tsk_abc123"]["ack"] is False

    # Inject a crash inside the push so the run aborts mid-cycle. Restore ONLY the
    # push (not the env/board monkeypatches) so the recovery run stays isolated.
    real_push = nag_check._push_nag

    def boom(*a, **k):
        raise RuntimeError("mid-run crash")

    monkeypatch.setattr(nag_check, "_push_nag", boom)
    with pytest.raises(RuntimeError):
        nag_check.run_nag_check()
    # State on disk is unchanged -- the loop is still open.
    assert _state(state)["tsk_abc123"]["ack"] is False

    # Recovery: a clean run re-fires the still-open loop.
    monkeypatch.setattr(nag_check, "_push_nag", real_push)
    sent2 = []
    nag_check.run_nag_check(send=lambda t, x: sent2.append((t, x)))
    assert len(sent2) == 1
    assert _state(state)["tsk_abc123"]["ack"] is False


# --- Race: a /done that acks under the lock is NOT clobbered into a reopen ---

def test_concurrent_done_under_lock_is_not_reopened_by_cron(harness, monkeypatch):
    """If a reactive /done acks the loop in the window between the cron's snapshot
    read and the locked fire, the cron must NOT re-open it (the re-nag trust kill).
    We simulate the race by acking inside the push, just before _persist_fire."""
    board, state = harness
    _run(harness)  # open loop, nag_count=1

    real_push = nag_check._push_nag

    def push_then_ack(record, section, overdue, *, dry_run, send):
        result = real_push(record, section, overdue, dry_run=dry_run, send=send)
        # A /done lands right after the push, before the locked _persist_fire.
        nag_state.transition(lambda s: nag_state.close_loop(
            s, "tsk_abc123", closed_by="explicit_done"))
        return result

    monkeypatch.setattr(nag_check, "_push_nag", push_then_ack)
    nag_check.run_nag_check(send=lambda t, x: None)
    nag = _state(state)["tsk_abc123"]
    assert nag["ack"] is True  # the /done close survived
    assert nag["closed_by"] == "explicit_done"  # NOT reopened/archived
    assert nag["archived_nag_loops"] == []  # no spurious archive+reopen


# --- Body-double-only entry must not be spuriously acked by the cron --------

def test_body_double_only_entry_not_acked_by_cron(tmp_path, monkeypatch):
    """A body-double on a NON-overdue task creates a nag_count==0 stub entry; the
    cron's close pass must not treat it as an open nag loop and ack it."""
    board = tmp_path / "Work Tasks.md"
    # Task due in the FUTURE -- not overdue, so it would never cross a threshold.
    board.write_text(
        "# Work\n\n## 🟡 Q2\n- [ ] **Soon** task_id::tsk_future 🗓️2026-07-15 area:: Ops\n",
        encoding="utf-8")
    state = tmp_path / "state"
    _set_env(monkeypatch, board, state)
    monkeypatch.setattr(nag_check, "_today", lambda: REF)
    # Attach a body-double-only entry (nag_count stays 0).
    session = {"session_id": "bd_1", "cron_ids": ["c"], "started_at": "x", "ended_at": None}
    nag_state.transition(lambda s: nag_state.add_body_double_session(s, "tsk_future", session))

    counts = nag_check.run_nag_check(send=lambda t, x: None)
    assert counts["closed"] == 0  # the stub is NOT a nag loop to close
    nag = _state(state)["tsk_future"]
    assert nag["ack"] is False  # not spuriously acked


# --- T10: ack:true is terminal; a re-nag is a NEW loop ---------------------

def test_t10_acked_loop_not_reactivated_in_background(harness):
    """An acked loop is NOT re-fired by the background even if still on the board."""
    board, state = harness
    _run(harness)
    # Externally ack the loop (as /done would), but leave the task on the board.
    nag_state.transition(lambda s: nag_state.close_loop(s, "tsk_abc123",
                                                        closed_by="explicit_done"))
    sent2 = []
    counts = nag_check.run_nag_check(send=lambda t, x: sent2.append((t, x)))
    assert sent2 == []  # acked loop stays silent
    assert counts["sent"] == 0
    assert _state(state)["tsk_abc123"]["ack"] is True


# --- Q1-aware threshold (mustFix #2) ---------------------------------------

def test_q1_task_nags_at_one_day_overdue(tmp_path, monkeypatch):
    board = tmp_path / "Work Tasks.md"
    board.write_text(
        "# Work\n\n## 🔴 Q1\n- [ ] **Fire** task_id::tsk_q1 🗓️2026-06-18 area:: Ops\n",
        encoding="utf-8",
    )
    state = tmp_path / "state"
    _set_env(monkeypatch, board, state)
    monkeypatch.setattr(nag_check, "_today", lambda: REF)  # 1 day overdue
    sent = []
    counts = nag_check.run_nag_check(send=lambda t, x: sent.append((t, x)))
    assert counts["sent"] == 1  # Q1 nags at 1 day, off the scalar overdue_days


def test_q2_task_overdue_four_days_nags(tmp_path, monkeypatch):
    """A q2 task 4 days overdue nags at the q2 threshold -- NOT held to the q3
    threshold that effective_priority's escalation would (incorrectly) impose."""
    board = tmp_path / "Work Tasks.md"
    board.write_text(BOARD, encoding="utf-8")  # tsk_abc123 due 2026-06-15
    state = tmp_path / "state"
    _set_env(monkeypatch, board, state)
    monkeypatch.setattr(nag_check, "_today",
                        lambda: datetime(2026, 6, 19, tzinfo=timezone.utc))  # 4d
    sent = []
    nag_check.run_nag_check(send=lambda t, x: sent.append((t, x)))
    assert len(sent) == 1


def test_q3_task_overdue_four_days_does_not_nag(tmp_path, monkeypatch):
    board = tmp_path / "Work Tasks.md"
    board.write_text(
        "# Work\n\n## 🟠 Q3\n- [ ] **Low** task_id::tsk_q3 🗓️2026-06-15 area:: Ops\n",
        encoding="utf-8",
    )
    state = tmp_path / "state"
    _set_env(monkeypatch, board, state)
    monkeypatch.setattr(nag_check, "_today", lambda: REF)  # 4 days overdue
    sent = []
    counts = nag_check.run_nag_check(send=lambda t, x: sent.append((t, x)))
    assert sent == [] and counts["sent"] == 0  # q3 needs 7 days


# --- Dry-run never writes state or sends -----------------------------------

def test_dry_run_writes_no_state_and_sends_nothing(harness):
    board, state = harness
    sent: list[tuple] = []
    counts = nag_check.run_nag_check(dry_run=True,
                                     send=lambda t, x: sent.append((t, x)))
    assert sent == []
    assert counts["open"] == 1  # would open one
    assert not (state / "nag-state.json").exists()  # no state written


def test_dry_run_does_not_append_to_autonomy_audit_log(harness):
    """A --dry-run must NOT gate() (which appends an executed act) -- it only proves
    the target. Otherwise it manufactures a phantom undoable nag never sent."""
    import autonomy_gate  # noqa: PLC0415 -- local import keeps the harness imports lean
    board, state = harness
    nag_check.run_nag_check(dry_run=True, send=lambda t, x: None)
    assert read_autonomy_log_for(state, autonomy_gate) == []
    assert not (state / "autonomy-log.jsonl").exists()


def read_autonomy_log_for(state, autonomy_gate):
    path = autonomy_gate.autonomy_log_path()
    return autonomy_gate.read_autonomy_log() if path.exists() else []
