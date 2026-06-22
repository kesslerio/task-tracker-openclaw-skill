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
import error_envelope  # noqa: E402

PRODUCTIVITY = "-4242424242"
WORK_GROUP = "-5252525252"
REF = datetime(2026, 6, 19, tzinfo=timezone.utc)

# Fake gateway message id the FAKE sender returns -- never a real send. Valid id
# shape but does not match the public-hygiene -100[0-9]{8,} grep.
FAKE_MESSAGE_ID = "-4242424242"


def fake_sender(record=None, *, message_id=FAKE_MESSAGE_ID):
    """A deliver_once-shaped fake sender: records (target, text) and returns a canned
    ``{"message_id": ...}`` receipt. NEVER calls real openclaw."""
    calls = record if record is not None else []

    def _send(target, text):
        calls.append((target, text))
        return {"message_id": message_id}

    return _send

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
    counts = nag_check.run_nag_check(sender=fake_sender(sent))
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

def test_t2_nag_refires_without_ack(harness, monkeypatch):
    from datetime import timedelta
    board, state = harness
    _run(harness)
    # Advance to the NEXT scheduled cycle so the un-acked loop genuinely re-fires
    # (a same-cycle re-run would be an idempotent no-op; the invariant under test is
    # that the loop is NEVER closed without an ack, so it must re-deliver each cycle).
    monkeypatch.setattr(nag_check, "_today", lambda: REF + timedelta(hours=3))
    counts2, sent2 = _run(harness)
    assert counts2["sent"] == 1
    nag = _state(state)["tsk_abc123"]
    assert nag["ack"] is False
    assert nag["nag_count"] == 2  # fired in two cycles, never closed
    sent_events = [e for e in _events(state) if e["event_type"] == "nag_sent"]
    assert len(sent_events) == 2


# --- H3: receipt-backed, idempotent delivery -------------------------------

def test_h3_nag_sent_event_carries_message_id_and_idem_key(harness):
    """After a nag fires, the nag_sent ledger event records the gateway message-id
    RECEIPT and the idem_key -- proving delivery (not mere intent) and to which key."""
    board, state = harness
    sent = []
    nag_check.run_nag_check(sender=fake_sender(sent, message_id="1915"))
    sent_events = [e for e in _events(state) if e["event_type"] == "nag_sent"]
    assert len(sent_events) == 1
    meta = sent_events[0]["metadata"]
    assert meta["message_id"] == "1915"  # the captured receipt
    assert meta["idem_key"].startswith("nag:tsk_abc123:")
    assert meta["idem_key"].endswith(":2026-06-19-00")  # period = the run's REF cycle (date+hour)


def test_h3_sender_receives_the_proven_gated_target(harness):
    """The fake sender is handed the PROVEN gated target (chat_id + topic), not a
    discarded/default one -- the proven target is the thing that actually sends."""
    board, state = harness
    sent = []
    nag_check.run_nag_check(sender=fake_sender(sent))
    assert len(sent) == 1
    target, text = sent[0]
    assert target == {"chat_id": PRODUCTIVITY, "topic_id": "2",
                      "agent_id": "niemand-work", "channel": "telegram"}
    assert "tsk_abc123" in text


def test_h3_same_cycle_retry_sender_called_once(harness):
    """Idempotency at the nag level: two fires of the SAME loop within the SAME cron
    cycle (same ref instant -- a retry) call the sender EXACTLY once (the outbox
    idem-key dedupes the duplicate delivery). nag_count counts DELIVERED nags, so the
    same-cycle retry is a NO-OP: nag_count stays 1, exactly ONE nag_sent is logged,
    and the loop stays open (it re-fires next CYCLE, not on a same-cycle retry)."""
    board, state = harness
    sent = []
    sender = fake_sender(sent)
    nag_check.run_nag_check(sender=sender)
    nag_check.run_nag_check(sender=sender)  # same REF -> same cycle -> a retry, no-op
    assert len(sent) == 1  # the real send happened ONCE -- no duplicate to the user
    nag = _state(state)["tsk_abc123"]
    assert nag["nag_count"] == 1 and nag["ack"] is False  # retry did NOT bump count
    sent_events = [e for e in _events(state) if e["event_type"] == "nag_sent"]
    assert len(sent_events) == 1  # the retry logged NO second receipt


def test_h3_different_cycle_same_day_redelivers(harness, monkeypatch):
    """The re-nag cadence is PRESERVED: the SAME loop at a DIFFERENT scheduled cycle
    (a later hour, same day) is a NEW delivery -- only a same-cycle retry dedupes.
    H3 must not silently collapse the 11/14/17 nags into one/day (that is H5's job)."""
    from datetime import timedelta
    board, state = harness
    sent = []
    sender = fake_sender(sent)
    nag_check.run_nag_check(sender=sender)  # cycle 1 (REF hour)
    monkeypatch.setattr(nag_check, "_today", lambda: REF + timedelta(hours=3))  # next cycle, same day
    nag_check.run_nag_check(sender=sender)
    assert len(sent) == 2  # each scheduled cycle delivers -- the re-nag cadence stands
    nag = _state(state)["tsk_abc123"]
    assert nag["nag_count"] == 2 and nag["ack"] is False  # each cycle is a delivered nag
    sent_events = [e for e in _events(state) if e["event_type"] == "nag_sent"]
    assert len(sent_events) == 2  # one receipt per delivered cycle


def test_h3_first_fire_death_then_retry_no_double_send(harness):
    """Fix 1 (durable idem-key): if the process dies AFTER the outbox records the
    delivery but BEFORE the loop state is persisted, the next same-cycle run must NOT
    double-send. The idem-key keys on DURABLE (task_id, period) -- not a per-loop
    random id minted before persist -- so the recorded outbox entry dedupes the retry
    even though nag-state.json was never written.

    We simulate the crash window by firing once (which writes BOTH outbox.json and
    nag-state.json), then wiping nag-state.json on disk while KEEPING outbox.json --
    exactly the state a death between the two writes would leave. The same-period
    re-run must find the durable idem-key already recorded and call the sender ZERO
    more times."""
    board, state = harness
    sent = []
    nag_check.run_nag_check(sender=fake_sender(sent))
    assert len(sent) == 1  # first fire delivered to the outbox
    # The loop state did NOT survive (process died before nag_state.transition wrote
    # it); the outbox receipt DID. Reproduce that on disk.
    (state / "nag-state.json").unlink()
    assert (state / "outbox.json").exists()
    # Same period (same REF) -> durable idem-key already recorded -> NO second send.
    sent_retry = []
    nag_check.run_nag_check(sender=fake_sender(sent_retry))
    assert sent_retry == []  # the user is NOT double-messaged


def test_h3_idempotent_retry_logs_no_second_receipt(harness):
    """Fix 2 (idempotent short-circuit is a no-op): a same-cycle retry adds NO new
    nag_sent ledger event and does NOT bump nag_count. nag_count counts DELIVERED
    nags; the retry delivered nothing (deliver_once short-circuited on the recorded
    receipt without calling the sender), so it must write neither a phantom second
    receipt nor an inflated count."""
    board, state = harness
    sender = fake_sender([])
    nag_check.run_nag_check(sender=sender)
    sent_before = [e for e in _events(state) if e["event_type"] == "nag_sent"]
    count_before = _state(state)["tsk_abc123"]["nag_count"]
    assert len(sent_before) == 1 and count_before == 1

    nag_check.run_nag_check(sender=sender)  # same cycle -> idempotent no-op
    sent_after = [e for e in _events(state) if e["event_type"] == "nag_sent"]
    assert len(sent_after) == 1  # NO second nag_sent receipt
    assert _state(state)["tsk_abc123"]["nag_count"] == 1  # count NOT bumped


def test_h3_idempotent_retry_via_peek_logs_no_gate_act(harness):
    """Fix A (peek before gate): a same-cycle retry short-circuits at the outbox PEEK
    -- before _authorise_nag -- so gate() is NEVER called and NO new ``executed``
    autonomy-gate act is appended for the undelivered duplicate fire. We assert the
    autonomy-log length is UNCHANGED across the second (retry) fire: the phantom
    executed-but-undelivered act the pre-fix ordering manufactured is gone."""
    import autonomy_gate  # noqa: PLC0415
    board, state = harness
    sender = fake_sender([])
    nag_check.run_nag_check(sender=sender)  # genuine fire: gate logs ONE executed act
    log_before = len(autonomy_gate.read_autonomy_log())

    nag_check.run_nag_check(sender=sender)  # same cycle -> peek short-circuits, no gate()
    # No new autonomy-log act for the deduped retry (the peek skipped gate entirely),
    # and -- since nothing was gated -- no reconciliation event either.
    assert len(autonomy_gate.read_autonomy_log()) == log_before
    undel = [e for e in _events(state) if e["event_type"] == "nag_gate_act_undelivered"]
    assert undel == []  # nothing gated this fire -> nothing to reconcile


def test_r2_fixb_peek_repairs_state_and_ledger_from_receipt(harness):
    """R2 Fix B: a FIRST fire delivered (outbox receipt written) but crashed before
    nag_state persisted the loop, and nag-state.json was then lost. The next same-cycle
    run peeks the recorded receipt -- and instead of returning blind, REPAIRS the loop
    from the stored receipt: the sender is NOT called again (no double-send), the loop
    is reopened, and a nag_sent carrying the STORED message_id is emitted so state +
    ledger catch up to the delivered fact."""
    board, state = harness
    sent = []
    nag_check.run_nag_check(sender=fake_sender(sent, message_id="9001"))
    assert len(sent) == 1  # first fire delivered + wrote the outbox receipt
    # Reproduce the TRUE crash window: the process died inside the locked fire AFTER
    # deliver_once committed the outbox receipt but BEFORE nag_state persisted the loop
    # and BEFORE the nag_sent ledger event was appended -- so the outbox receipt is the
    # ONLY durable trace. Wipe nag-state.json + events.jsonl, keep outbox.json.
    (state / "nag-state.json").unlink()
    (state / "events.jsonl").unlink()
    assert (state / "outbox.json").exists()

    sent_retry = []
    nag_check.run_nag_check(sender=fake_sender(sent_retry, message_id="9001"))
    assert sent_retry == []  # NOT re-sent -- the receipt deduped the delivery
    # The loop is REPAIRED (reopened) from the stored receipt.
    nag = _state(state)["tsk_abc123"]
    assert nag["ack"] is False  # open loop, not split-brain
    assert nag["nag_count"] == 1  # the delivered nag is now reflected in state
    # The missing nag_sent is emitted carrying the STORED message_id (ledger caught up).
    sent_events = [e for e in _events(state) if e["event_type"] == "nag_sent"]
    assert len(sent_events) == 1
    assert sent_events[0]["metadata"]["message_id"] == "9001"  # the stored receipt id
    assert sent_events[0]["metadata"].get("repaired") is True


def test_r2_fixb_repair_is_idempotent(harness):
    """R2 Fix B: repairing twice does not double-open or double-emit. After the first
    repair the loop is a genuine open loop, so a further same-cycle run is the normal
    no-op retry -- no new nag_sent, count unchanged."""
    board, state = harness
    nag_check.run_nag_check(sender=fake_sender([], message_id="42"))
    (state / "nag-state.json").unlink()  # lose state, keep outbox
    nag_check.run_nag_check(sender=fake_sender([], message_id="42"))  # repair #1
    count_after_repair = _state(state)["tsk_abc123"]["nag_count"]
    sent_after_repair = len([e for e in _events(state) if e["event_type"] == "nag_sent"])

    nag_check.run_nag_check(sender=fake_sender([], message_id="42"))  # would-be repair #2
    assert _state(state)["tsk_abc123"]["nag_count"] == count_after_repair  # not double-bumped
    assert len([e for e in _events(state)
                if e["event_type"] == "nag_sent"]) == sent_after_repair  # no second emit


def test_r2_fixb_repair_does_not_double_emit_when_ledger_survives(harness):
    """R2 Fix B idempotency vs. a SURVIVING ledger: events.jsonl is append-only and
    drifts independently of nag-state.json. If the genuine fire already wrote
    nag_opened+nag_sent to the ledger and ONLY nag-state.json was later lost, the repair
    must reopen the loop but NOT emit a SECOND nag_sent for the same delivered idem_key
    (nag_sent counts DELIVERED nags -- a double emit over-counts one delivery)."""
    board, state = harness
    nag_check.run_nag_check(sender=fake_sender([], message_id="77"))
    sent_before = [e for e in _events(state) if e["event_type"] == "nag_sent"]
    assert len(sent_before) == 1  # the genuine fire logged its one nag_sent
    # Lose ONLY the loop state; the ledger (and outbox receipt) survive.
    (state / "nag-state.json").unlink()
    assert (state / "events.jsonl").exists() and (state / "outbox.json").exists()

    sent_retry = []
    nag_check.run_nag_check(sender=fake_sender(sent_retry, message_id="77"))
    assert sent_retry == []  # not re-sent
    # The loop is reopened (state repaired)...
    assert _state(state)["tsk_abc123"]["ack"] is False
    # ...but the ledger is NOT double-counted: still exactly one nag_sent for the
    # delivered message.
    sent_after = [e for e in _events(state) if e["event_type"] == "nag_sent"]
    assert len(sent_after) == 1  # no phantom second receipt for one delivery


def test_r2_fixc_assert_failure_reconciles_gate_act(harness, monkeypatch):
    """R2 Fix C: a gate fires (executed act logged) but the gate<->message SEAM asserts
    out (gate target != send target). Pre-fix _authorise_nag dropped the act_id on the
    assert path, leaving an executed act with NO reconciliation. Now: the loop stays
    OPEN, no nag_sent, AND a nag_gate_act_undelivered event carries the gate act_id."""
    import nag_delivery  # noqa: PLC0415
    import autonomy_gate  # noqa: PLC0415
    board, state = harness
    # Force the seam to fail AFTER gate() already logged its executed act.
    monkeypatch.setattr(nag_delivery, "authorise_target",
                        lambda act_id, target: {"ok": False, "reason": "target-mismatch",
                                                "stage": "assert"})
    sent = []
    counts = nag_check.run_nag_check(sender=fake_sender(sent))
    assert sent == [] and counts["sent"] == 0 and counts["blocked"] == 1
    # The gate logged exactly one executed act; we reconcile against it.
    executed = [r for r in autonomy_gate.read_autonomy_log()
                if r.get("act_type") == "nag_sent" and r.get("status") == "executed"]
    assert len(executed) == 1
    gate_act_id = executed[0]["act_id"]
    undel = [e for e in _events(state) if e["event_type"] == "nag_gate_act_undelivered"]
    assert len(undel) == 1
    assert undel[0]["metadata"]["act_id"] == gate_act_id  # ties back to the gate act
    assert undel[0]["metadata"]["stage"] == "assert"
    # Loop stays OPEN, no phantom nag_sent.
    on_disk = _state(state)
    assert "tsk_abc123" not in on_disk or on_disk["tsk_abc123"]["ack"] is False
    assert [e for e in _events(state) if e["event_type"] == "nag_sent"] == []


def test_r2_fixc_env_missing_emits_no_reconciliation(tmp_path, monkeypatch):
    """R2 Fix C: an env-missing block is a PROVE-stage block -- the gate NEVER fired, so
    there is no executed act to reconcile. It emits ONLY nag_delivery_blocked, never a
    nag_gate_act_undelivered (no phantom reconciliation for a gate that did not fire)."""
    board = tmp_path / "Work Tasks.md"
    board.write_text(BOARD, encoding="utf-8")
    state = tmp_path / "state"
    _set_env(monkeypatch, board, state)
    monkeypatch.delenv("TELEGRAM_CHAT_ID_PRODUCTIVITY", raising=False)  # prove-stage block
    monkeypatch.setattr(nag_check, "_today", lambda: REF)
    counts = nag_check.run_nag_check(sender=fake_sender([]))
    assert counts["blocked"] == 1 and counts["sent"] == 0
    blocked = [e for e in task_ledger.read_events(state / "events.jsonl")
               if e["event_type"] == "nag_delivery_blocked"]
    assert blocked and blocked[0]["metadata"]["reason"] == "env_missing"
    undel = [e for e in task_ledger.read_events(state / "events.jsonl")
             if e["event_type"] == "nag_gate_act_undelivered"]
    assert undel == []  # gate never fired -> nothing to reconcile


def test_h3_delivery_failure_reconciles_gate_act(harness):
    """Fix B (reconcile the gate act on non-delivery): when the sender RAISES after
    the gate already logged an ``executed`` act, a nag_gate_act_undelivered ledger
    event is appended carrying that gate act_id -- so the executed-but-undelivered act
    is reconcilable from the ledger. The loop stays OPEN and no nag_sent is written."""
    import autonomy_gate  # noqa: PLC0415
    board, state = harness

    def boom(target, text):
        raise nag_check.outbox.OpenclawSendError("gateway unreachable")

    counts = nag_check.run_nag_check(sender=boom)
    assert counts["sent"] == 0 and counts["blocked"] == 1
    # The gate logged exactly one executed nag_sent act (its authoritative record is
    # untouched -- we never forge a status over it); we tie our reconciliation to it.
    executed = [r for r in autonomy_gate.read_autonomy_log()
                if r.get("act_type") == "nag_sent" and r.get("status") == "executed"]
    assert len(executed) == 1
    gate_act_id = executed[0]["act_id"]
    undel = [e for e in _events(state) if e["event_type"] == "nag_gate_act_undelivered"]
    assert len(undel) == 1
    meta = undel[0]["metadata"]
    assert meta["act_id"] == gate_act_id  # reconciliation ties back to the gate act
    assert meta["reason"] == "OpenclawSendError" and meta["stage"] == "send"
    # Loop stays OPEN, no phantom nag_sent ledger receipt.
    on_disk = _state(state)
    assert "tsk_abc123" not in on_disk or on_disk["tsk_abc123"]["ack"] is False
    assert [e for e in _events(state) if e["event_type"] == "nag_sent"] == []


def test_h3_sender_failure_leaves_loop_open_and_logs_block(harness):
    """A sender that RAISES is a delivery FAILURE: the loop is NOT closed, NO phantom
    nag_sent is recorded, and a delivery-failure (nag_delivery_blocked) is logged."""
    board, state = harness

    def boom(target, text):
        raise nag_check.outbox.OpenclawSendError("gateway unreachable")

    counts = nag_check.run_nag_check(sender=boom)
    assert counts["sent"] == 0 and counts["blocked"] == 1
    # No nag loop was persisted as sent -- it stays OPEN (or absent, never acked).
    on_disk = _state(state)
    assert "tsk_abc123" not in on_disk or on_disk["tsk_abc123"]["ack"] is False
    sent_events = [e for e in _events(state) if e["event_type"] == "nag_sent"]
    assert sent_events == []  # no phantom "sent"
    blocked = [e for e in _events(state) if e["event_type"] == "nag_delivery_blocked"]
    assert blocked and blocked[-1]["metadata"]["stage"] == "send"


def test_h3_failed_send_then_clean_retry_delivers(harness):
    """After a transport failure leaves the loop open, the NEXT cycle delivers
    cleanly (the failed send recorded no idem-key, so the retry is not deduped)."""
    board, state = harness

    def boom(target, text):
        raise nag_check.outbox.OpenclawSendError("transient")

    nag_check.run_nag_check(sender=boom)  # fails, loop left open
    sent = []
    counts = nag_check.run_nag_check(sender=fake_sender(sent))  # same day, retry
    assert counts["sent"] == 1 and len(sent) == 1  # retry delivered
    assert _state(state)["tsk_abc123"]["ack"] is False


# --- Production transport: main() DELIVERS via the sender, stdout stays empty -----

def test_main_delivers_via_sender_and_stdout_is_empty(harness, capsys, monkeypatch):
    """H3: the script OWNS the send. main() delivers the proven nag text through the
    sender (here a FAKE that records the delivery), leaving STDOUT EMPTY so the cron's
    blind --announce of stdout cannot double-send. The operational footer is on STDERR."""
    board, state = harness
    delivered = []
    monkeypatch.setattr(nag_check.outbox, "openclaw_sender", fake_sender(delivered))
    rc = nag_check.main([])
    captured = capsys.readouterr()
    assert rc == 0
    # The nag text went to the SENDER, not stdout.
    assert len(delivered) == 1
    target, text = delivered[0]
    assert "tsk_abc123" in text and "Overdue task still open" in text
    assert target == {"chat_id": PRODUCTIVITY, "topic_id": "2",
                      "agent_id": "niemand-work", "channel": "telegram"}
    assert captured.out.strip() == ""  # stdout empty: --announce of it is a no-op
    assert "NAG_CHECK_DONE" not in captured.out  # footer NOT in the announced output
    assert "NAG_CHECK_DONE: 1 open loops, 1 sent" in captured.err  # footer on stderr


def test_main_idle_cycle_announces_nothing(tmp_path, monkeypatch, capsys):
    """No overdue task -> stdout is empty (the cron announces nothing this cycle)."""
    board = tmp_path / "Work Tasks.md"
    board.write_text(
        "# Work\n\n## 🟡 Q2\n- [ ] **Soon** task_id::tsk_future 🗓️2026-07-15 area:: Ops\n",
        encoding="utf-8")
    state = tmp_path / "state"
    _set_env(monkeypatch, board, state)
    monkeypatch.setattr(nag_check, "_today", lambda: REF)
    rc = nag_check.main([])
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out.strip() == ""  # nothing announced on an idle cycle
    assert "NAG_CHECK_DONE: 0 open loops" in captured.err


def test_run_nag_check_requires_transport_for_real_run(harness):
    """A real run with no transport raises rather than silently delivering nothing."""
    with pytest.raises(ValueError):
        nag_check.run_nag_check()  # no send, not dry-run


# --- R1 Fix 2: a swallowed delivery failure surfaces as a NONZERO main() code -----

def test_r1_blocked_run_records_health_failure_returns_zero(tmp_path, monkeypatch, capsys):
    """A swallowed per-task transport failure (counts['blocked']>0, loop left OPEN) is
    recorded as a health FAILURE for nag_check DIRECTLY, and main() returns 0 -- so the
    cron's shell wrapper never turns it into a user-facing "unavailable" announce. Env
    unset => the nag is delivery-blocked at the prove stage without any real send."""
    import cos_health  # noqa: PLC0415
    board = tmp_path / "Work Tasks.md"
    board.write_text(BOARD, encoding="utf-8")
    state = tmp_path / "state"
    _set_env(monkeypatch, board, state)
    monkeypatch.delenv("TELEGRAM_CHAT_ID_PRODUCTIVITY", raising=False)  # delivery-blocked
    monkeypatch.setattr(nag_check, "_today", lambda: REF)
    rc = nag_check.main([])
    err = capsys.readouterr().err
    assert rc == 0  # NO nonzero -> the shell wrapper does not announce "unavailable"
    assert "1 blocked" in err  # the footer still reports the blocked count (unchanged)
    entry = cos_health.read_health()["nag_check"]
    assert entry["last_failure"]["error_class"] == "nag_delivery_blocked"
    assert "last_success_ts" not in entry  # a blocked run is NOT recorded healthy
    on_disk = _state(state)
    assert "tsk_abc123" not in on_disk or on_disk["tsk_abc123"]["ack"] is False  # loop OPEN


def test_r1_sender_failure_records_health_failure_returns_zero(harness, monkeypatch, capsys):
    """A transport FAILURE (the sender raises) is swallowed into counts['blocked'] with the
    loop OPEN; main() records a health failure directly and returns 0 (no user-facing
    'unavailable' announce on the cron path)."""
    import cos_health  # noqa: PLC0415
    board, state = harness
    def boom(target, text):
        raise nag_check.outbox.OpenclawSendError("gateway unreachable")
    monkeypatch.setattr(nag_check.outbox, "openclaw_sender", boom)
    rc = nag_check.main([])
    err = capsys.readouterr().err
    assert rc == 0
    assert "0 sent" in err and "1 blocked" in err
    assert cos_health.read_health()["nag_check"]["last_failure"]["error_class"] == "nag_delivery_blocked"


def test_r1_clean_run_records_health_success_returns_zero(harness, monkeypatch, capsys):
    """A clean real run (a nag delivered, nothing blocked) records a health SUCCESS and
    returns 0."""
    import cos_health  # noqa: PLC0415
    board, state = harness
    monkeypatch.setattr(nag_check.outbox, "openclaw_sender", fake_sender([]))
    rc = nag_check.main([])
    err = capsys.readouterr().err
    assert rc == 0
    assert "1 sent" in err and "0 blocked" in err
    entry = cos_health.read_health()["nag_check"]
    assert "last_success_ts" in entry and "last_failure" not in entry


def test_r1_idle_run_records_health_success(tmp_path, monkeypatch, capsys):
    """An idle cycle (nothing overdue) is a HEALTHY run -- record success, return 0 (the
    cron fired fine, there was just nothing to nag)."""
    import cos_health  # noqa: PLC0415
    board = tmp_path / "Work Tasks.md"
    board.write_text(
        "# Work\n\n## 🟡 Q2\n- [ ] **Soon** task_id::tsk_future 🗓️2026-07-15 area:: Ops\n",
        encoding="utf-8")
    state = tmp_path / "state"
    _set_env(monkeypatch, board, state)
    monkeypatch.setattr(nag_check, "_today", lambda: REF)
    rc = nag_check.main([])
    capsys.readouterr()
    assert rc == 0
    assert "last_success_ts" in cos_health.read_health()["nag_check"]


def test_r1_dry_run_records_no_health(harness, monkeypatch):
    """A --dry-run NEVER delivers, so it records NO health (neither success nor failure)
    and returns 0 -- a preview is not a real cron outcome."""
    import cos_health  # noqa: PLC0415
    board, state = harness
    monkeypatch.delenv("TELEGRAM_CHAT_ID_PRODUCTIVITY", raising=False)  # preview is blocked
    rc = nag_check.main(["--dry-run"])
    assert rc == 0
    assert "nag_check" not in cos_health.read_health()  # dry-run records nothing


def test_r1_crash_records_health_failure(harness, monkeypatch):
    """A HARD crash (an unexpected exception -- worse than a swallowed block) is recorded
    as a health FAILURE too. nag_check catches its own crash and returns 0, so the shell
    log_subprocess_error never fires; without recording here a crashing cron would
    false-green until STALE."""
    import cos_health  # noqa: PLC0415
    board, state = harness
    monkeypatch.setattr(nag_check, "run_nag_check",
                        lambda **k: (_ for _ in ()).throw(RuntimeError("boom")))
    rc = nag_check.main([])
    assert rc == 0  # the crash is enveloped (exit 0), SAFE_ENVELOPE printed
    assert cos_health.read_health()["nag_check"]["last_failure"]["error_class"] == "RuntimeError"


# --- Background recycle: no-longer-overdue clears (never terminally acks) ------

def test_background_no_longer_overdue_recycles_not_acks(harness, monkeypatch):
    """A still-on-board task that drops below threshold is RECYCLED (cleared), not
    terminally acked -- so a future lapse past threshold nags again (no mute hole)."""
    board, state = harness
    _run(harness)  # open loop for tsk_abc123 (due 2026-06-15)
    # The due date is edited forward so the task is no longer overdue past threshold.
    board.write_text(
        "# Work\n\n## 🟡 Q2\n"
        "- [ ] **Re-evaluate ActiveCampaign** task_id::tsk_abc123 🗓️2026-06-30 area:: Marketing\n",
        encoding="utf-8")
    counts, _sent = _run(harness)
    assert counts["closed"] == 1
    # The entry is cleared (recycled), NOT a lingering acked entry.
    assert "tsk_abc123" not in _state(state) or _state(state)["tsk_abc123"].get("ack") is not True
    # When the new date later lapses past threshold, a fresh loop nags.
    monkeypatch.setattr(nag_check, "_today",
                        lambda: datetime(2026, 7, 10, tzinfo=timezone.utc))
    sent = []
    nag_check.run_nag_check(sender=fake_sender(sent))
    assert len(sent) == 1  # re-nags -- never permanently muted


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
    counts = nag_check.run_nag_check(sender=fake_sender(sent))

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
    nag_check.run_nag_check(sender=fake_sender(sent2))
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
    counts = nag_check.run_nag_check(sender=fake_sender(sent))
    assert sent == []
    assert counts["blocked"] == 1
    blocked = [e for e in task_ledger.read_events(state / "events.jsonl")
               if e["event_type"] == "nag_delivery_blocked"]
    assert blocked and blocked[0]["metadata"]["reason"] == "work_group"


def test_t4_seam_mismatch_blocks_before_send(harness, monkeypatch):
    """The gate<->message seam (assert_send_target) is the last guard BEFORE delivery:
    a target-mismatch must block, the fake sender is NEVER called, and the loop stays
    OPEN. We force the seam to fail to exercise the assert stage in isolation."""
    import nag_delivery  # noqa: PLC0415
    board, state = harness
    monkeypatch.setattr(nag_delivery, "authorise_target",
                        lambda act_id, target: {"ok": False, "reason": "target-mismatch",
                                                "stage": "assert"})
    sent = []
    counts = nag_check.run_nag_check(sender=fake_sender(sent))
    assert sent == []  # the seam blocked BEFORE any send
    assert counts["sent"] == 0 and counts["blocked"] == 1
    on_disk = _state(state)
    assert "tsk_abc123" not in on_disk or on_disk["tsk_abc123"]["ack"] is False
    blocked = [e for e in _events(state) if e["event_type"] == "nag_delivery_blocked"]
    assert blocked and blocked[-1]["metadata"]["reason"] == "target-mismatch"


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
    from datetime import timedelta
    board, state = harness
    _run(harness)  # loop is open (recorded the idem-key for the REF cycle)
    assert _state(state)["tsk_abc123"]["ack"] is False

    # Inject a crash inside the prove/authorise step so the run aborts mid-cycle.
    # Restore ONLY authorise (not the env/board monkeypatches) so the recovery run
    # stays isolated. A raised authorise (unlike a controlled sender failure) is an
    # uncaught fault: it propagates out, transition() persists nothing, loop stays open.
    # The crash run must land in a FRESH cycle so the outbox peek does NOT short-circuit
    # on the REF cycle's recorded delivery before _authorise_nag is reached -- otherwise
    # we would be exercising the dedup peek, not the mid-authorise crash this pins.
    monkeypatch.setattr(nag_check, "_today", lambda: REF + timedelta(hours=3))
    real_authorise = nag_check._authorise_nag

    def boom(*a, **k):
        raise RuntimeError("mid-run crash")

    monkeypatch.setattr(nag_check, "_authorise_nag", boom)
    with pytest.raises(RuntimeError):
        nag_check.run_nag_check(sender=fake_sender())
    # State on disk is unchanged -- the loop is still open.
    assert _state(state)["tsk_abc123"]["ack"] is False

    # Recovery: a clean run re-fires the still-open loop. We advance the clock again
    # to a fresh cycle so the receipt idem-key is unseen and the recovery genuinely
    # re-delivers -- proving the crashed loop was processed, not lost (in the crashed
    # cycle nothing was ever recorded, so this is a clean new delivery).
    monkeypatch.setattr(nag_check, "_authorise_nag", real_authorise)
    monkeypatch.setattr(nag_check, "_today",
                        lambda: datetime(2026, 6, 20, tzinfo=timezone.utc))
    sent2 = []
    nag_check.run_nag_check(sender=fake_sender(sent2))
    assert len(sent2) == 1
    assert _state(state)["tsk_abc123"]["ack"] is False


# --- Race: an ack landing before the locked fire suppresses send + reopen -----

def test_acked_loop_under_lock_sends_nothing_and_is_not_reopened(harness):
    """The fire (ack re-check + gate + send + persist) is ONE locked transition, so
    a reactive /done that acks the loop before the cron's fire takes the lock means
    NO message is sent, NO gate act is logged, and the close is not clobbered into
    a reopen. We assert the gated outcome directly: with the loop acked, the cron
    leaves it acked and sends nothing."""
    import autonomy_gate  # noqa: PLC0415
    board, state = harness
    _run(harness)  # open loop, nag_count=1
    # The /done landed: the loop is acked before the next cron fire.
    nag_state.transition(lambda s: nag_state.close_loop(
        s, "tsk_abc123", closed_by="explicit_done"))
    log_before = len(autonomy_gate.read_autonomy_log())

    sent = []
    counts = nag_check.run_nag_check(sender=fake_sender(sent))
    assert sent == []  # no message after /done
    assert counts["sent"] == 0
    nag = _state(state)["tsk_abc123"]
    assert nag["ack"] is True and nag["closed_by"] == "explicit_done"
    assert nag["archived_nag_loops"] == []  # not clobbered into a reopen
    # And no phantom gate act was appended for the suppressed fire.
    assert len(autonomy_gate.read_autonomy_log()) == log_before


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

    counts = nag_check.run_nag_check(sender=fake_sender())
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
    counts = nag_check.run_nag_check(sender=fake_sender(sent2))
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
    counts = nag_check.run_nag_check(sender=fake_sender(sent))
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
    nag_check.run_nag_check(sender=fake_sender(sent))
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
    counts = nag_check.run_nag_check(sender=fake_sender(sent))
    assert sent == [] and counts["sent"] == 0  # q3 needs 7 days


# --- Dry-run never writes state or sends -----------------------------------

def test_dry_run_writes_no_state_and_sends_nothing(harness):
    board, state = harness
    sent: list[tuple] = []
    counts = nag_check.run_nag_check(dry_run=True,
                                     sender=fake_sender(sent))
    assert sent == []
    assert counts["open"] == 1  # would open one
    assert not (state / "nag-state.json").exists()  # no state written


def test_dry_run_preview_skips_snoozed_loop_like_a_real_run(harness):
    """--dry-run is a faithful preview: a snoozed on-board overdue loop is NOT
    counted as 'would push', matching what the real cron pass would do."""
    from datetime import timedelta
    board, state = harness
    _run(harness)  # open the loop
    # Snooze it past the nag-check clock (REF + 1 day).
    until = (REF + timedelta(days=1)).isoformat()
    nag_state.transition(lambda s: nag_state.apply_snooze(
        s, "tsk_abc123", snoozed_until=until, block_reason=None))
    counts = nag_check.run_nag_check(dry_run=True)
    assert counts["open"] == 0  # snoozed -> not previewed as a push


def test_dry_run_does_not_resolve_a_preexisting_open_loop(harness):
    """--dry-run must not terminally ack/clear a real open loop or write the ledger
    in the resolve pass (pass 1), even against live state with loops present."""
    import task_ledger
    board, state = harness
    _run(harness)  # open a real loop for tsk_abc123
    # Task leaves the board -> a real run would close it (verified_done).
    board.write_text("# Work\n\n## 🟡 Q2\n", encoding="utf-8")
    state_before = (state / "nag-state.json").read_text()
    events_before = len(task_ledger.read_events(state / "events.jsonl"))

    counts = nag_check.run_nag_check(dry_run=True)
    assert counts["closed"] == 1  # previewed, but...
    assert (state / "nag-state.json").read_text() == state_before  # ...state unchanged
    assert len(task_ledger.read_events(state / "events.jsonl")) == events_before
    assert _state(state)["tsk_abc123"]["ack"] is False  # loop NOT resolved


def test_body_double_stub_promoted_to_genuine_nag_opens_loop(tmp_path, monkeypatch):
    """A body-double stub (nag_count==0, delivery_target=None) that later crosses
    threshold must be promoted via open_loop -- emitting nag_opened and backfilling
    delivery_target -- not silently fired as a loop with delivery_target:null."""
    board = tmp_path / "Work Tasks.md"
    board.write_text(
        "# Work\n\n## 🟡 Q2\n- [ ] **AC** task_id::tsk_abc123 🗓️2026-06-15 area:: M\n",
        encoding="utf-8")
    state = tmp_path / "state"
    _set_env(monkeypatch, board, state)
    monkeypatch.setattr(nag_check, "_today", lambda: REF)  # already 4 days overdue
    # Attach a body-double-only stub BEFORE any nag fires.
    session = {"session_id": "bd_1", "cron_ids": ["c"], "started_at": "x", "ended_at": None}
    nag_state.transition(lambda s: nag_state.add_body_double_session(s, "tsk_abc123", session))
    assert _state(state)["tsk_abc123"]["nag_count"] == 0  # a stub

    sent = []
    nag_check.run_nag_check(sender=fake_sender(sent))
    assert len(sent) == 1
    nag = _state(state)["tsk_abc123"]
    assert nag["nag_count"] == 1
    assert nag["delivery_target"] is not None  # backfilled, not null
    # The active body-double session survives the promotion.
    assert nag_state.active_body_double_session(nag) is not None
    opened = [e for e in _events(state) if e["event_type"] == "nag_opened"]
    assert opened  # nag_opened emitted for the promoted loop


def test_dry_run_does_not_append_to_autonomy_audit_log(harness):
    """A --dry-run must NOT gate() (which appends an executed act) -- it only proves
    the target. Otherwise it manufactures a phantom undoable nag never sent."""
    import autonomy_gate  # noqa: PLC0415 -- local import keeps the harness imports lean
    board, state = harness
    nag_check.run_nag_check(dry_run=True)
    assert read_autonomy_log_for(state, autonomy_gate) == []
    assert not (state / "autonomy-log.jsonl").exists()


def read_autonomy_log_for(state, autonomy_gate):
    path = autonomy_gate.autonomy_log_path()
    return autonomy_gate.read_autonomy_log() if path.exists() else []


# --- U4.1: top-N display cap + /nag all read-only escape hatch --------------

# Five Q1 tasks (threshold 1 day) at DISTINCT overdue ages so the worst-first
# order is deterministic: a(18d) > b(14d) > c(9d) > d(5d) > e(2d) at REF.
MULTI_BOARD = """# Work

## 🔴 Q1
- [ ] **Alpha** task_id::tsk_a 🗓️2026-06-01 area:: Ops
- [ ] **Bravo** task_id::tsk_b 🗓️2026-06-05 area:: Ops
- [ ] **Charlie** task_id::tsk_c 🗓️2026-06-10 area:: Ops
- [ ] **Delta** task_id::tsk_d 🗓️2026-06-14 area:: Ops
- [ ] **Echo** task_id::tsk_e 🗓️2026-06-17 area:: Ops
"""


@pytest.fixture
def multi(tmp_path, monkeypatch):
    board = tmp_path / "Work Tasks.md"
    board.write_text(MULTI_BOARD, encoding="utf-8")
    state = tmp_path / "state"
    _set_env(monkeypatch, board, state)
    monkeypatch.setattr(nag_check, "_today", lambda: REF)
    return board, state


def test_cap_fires_only_top_n_most_overdue(multi):
    """limit=3 fires the 3 WORST-overdue tasks and DEFERS the rest -- the deferred
    ones open no loop and push nothing this cycle."""
    board, state = multi
    sent = []
    counts = nag_check.run_nag_check(limit=3, sender=fake_sender(sent))
    assert counts["sent"] == 3 and counts["deferred"] == 2
    fired_ids = " ".join(text for _target, text in sent)
    for worst in ("tsk_a", "tsk_b", "tsk_c"):
        assert worst in fired_ids
    for deferred in ("tsk_d", "tsk_e"):
        assert deferred not in fired_ids
    # A deferred task opens NO loop this cycle (cap is a firing bound).
    on_disk = _state(state)
    assert "tsk_d" not in on_disk and "tsk_e" not in on_disk


def test_no_limit_fires_everything(multi):
    """The default (limit=None) is uncapped -- every crossed task fires, deferred 0.
    Preserves the pre-cap contract for direct run_nag_check callers."""
    board, state = multi
    sent = []
    counts = nag_check.run_nag_check(sender=fake_sender(sent))
    assert counts["sent"] == 5 and counts["deferred"] == 0


def test_main_caps_and_delivers_more_pointer(multi, capsys, monkeypatch):
    """The cron CLI path caps at NAG_DISPLAY_LIMIT (3). Post-H3/Fix D the 3 nags are
    DELIVERED through the sender (stdout stays empty) and the '+2 more … /nag all'
    pointer is FOLDED into the LAST fired nag's text -- one send per fired nag, NO
    separate pointer message; the operational footer on stderr carries the deferred
    count."""
    board, state = multi
    delivered = []
    monkeypatch.setattr(nag_check.outbox, "openclaw_sender", fake_sender(delivered))
    rc = nag_check.main([])  # limit defaults to nag_display_limit() == 3
    captured = capsys.readouterr()
    out, err = captured.out, captured.err
    assert rc == 0
    blob = " ".join(text for _target, text in delivered)
    # The 3 worst nags were DELIVERED; the deferred 2 were not.
    assert "tsk_a" in blob and "tsk_b" in blob and "tsk_c" in blob
    assert "tsk_d" not in blob and "tsk_e" not in blob
    # The pointer rides the LAST fired nag's body -- NOT a separate message.
    assert "+2 more overdue" in blob and "/nag all" in blob
    assert len(delivered) == 3  # exactly one send per fired nag, NO extra pointer send
    last_text = delivered[-1][1]
    assert "+2 more overdue" in last_text  # the pointer is on the last nag's text
    assert out.strip() == ""  # nothing on stdout: the send is the channel now
    # NAG_CHECK_DONE footer (stderr, not announced) reports the deferred count.
    assert "3 sent" in err and "2 deferred" in err


def test_r2_fixd_pointer_rides_last_nag_no_separate_send(multi):
    """R2 Fix D: with deferred>0 and >=1 nag fired, the '+K more' pointer is FOLDED into
    the LAST fired nag's delivered text and rides that one gated, receipted, idempotent
    send -- there is NO separate pointer send (exactly one send per fired nag, none
    extra)."""
    board, state = multi
    sent = []
    counts = nag_check.run_nag_check(limit=3, sender=fake_sender(sent))
    assert counts["sent"] == 3 and counts["deferred"] == 2
    assert len(sent) == 3  # one send per fired nag -- NO extra pointer message
    last_text = sent[-1][1]
    assert "+2 more overdue tasks — reply /nag all to see them." in last_text
    # The pointer is on EXACTLY one (the last) nag, not duplicated across nags.
    assert sum(1 for _t, text in sent if "more overdue" in text) == 1
    # The earlier nags carry no pointer.
    for _t, text in sent[:-1]:
        assert "more overdue" not in text


def test_r2_fixd_no_pointer_when_nothing_deferred(multi):
    """R2 Fix D: when nothing is deferred (no cap held anything back), no pointer is
    appended to any nag -- the wording only appears when there is a '+K more'."""
    board, state = multi
    sent = []
    counts = nag_check.run_nag_check(sender=fake_sender(sent))  # uncapped -> 0 deferred
    assert counts["deferred"] == 0
    assert all("more overdue" not in text for _t, text in sent)


def test_main_all_flag_fires_everything_no_pointer(multi, capsys, monkeypatch):
    """`--all` removes the cap: every overdue is DELIVERED and there is NO '+more'
    pointer (nothing deferred). Stdout stays empty -- the sender is the channel."""
    board, state = multi
    delivered = []
    monkeypatch.setattr(nag_check.outbox, "openclaw_sender", fake_sender(delivered))
    rc = nag_check.main(["--all"])
    captured = capsys.readouterr()
    out, err = captured.out, captured.err
    assert rc == 0
    blob = " ".join(text for _target, text in delivered)
    for tid in ("tsk_a", "tsk_b", "tsk_c", "tsk_d", "tsk_e"):
        assert tid in blob
    assert len(delivered) == 5  # all 5 nags, NO pointer message
    assert "more overdue" not in blob
    assert out.strip() == ""
    assert "5 sent" in err and "0 deferred" in err


def test_nag_all_list_is_read_only_full_view(multi, capsys):
    """`--list` (the /nag all reply) prints EVERY overdue nag worst-first but writes
    NO state, sends nothing, gates nothing, and appends no ledger event."""
    import autonomy_gate  # noqa: PLC0415
    board, state = multi
    before = board.stat().st_mtime_ns
    rc = nag_check.main(["--list"])
    out = capsys.readouterr().out
    assert rc == 0
    # Full worst-first list, all five, with a total line.
    for tid in ("tsk_a", "tsk_b", "tsk_c", "tsk_d", "tsk_e"):
        assert tid in out
    assert out.index("tsk_a") < out.index("tsk_e")  # worst-first order
    assert "5 overdue tasks total" in out
    # READ-ONLY: no nag state, no ledger, no autonomy log, board untouched.
    assert not (state / "nag-state.json").exists()
    assert not (state / "events.jsonl").exists()
    assert read_autonomy_log_for(state, autonomy_gate) == []
    assert board.stat().st_mtime_ns == before


def test_snoozed_leaders_do_not_starve_lower_tasks(multi):
    """D1 regression: snoozing the top-N must NOT mute the surface. A snoozed leader
    yields its cap slot so the next-worst FIRABLE task fires -- the cap is a
    top-N-FIRABLE bound, not top-N-CROSSED. Without this, snoozing the worst 3
    would silently black-hole everything below them until the snoozes expire."""
    from datetime import timedelta
    board, state = multi
    # Cycle 1: top-3 (a, b, c) fire and open loops.
    nag_check.run_nag_check(limit=3, sender=fake_sender())
    # The user snoozes all three leaders (ADHD breathing room) past the clock.
    until = (REF + timedelta(days=11)).isoformat()
    for tid in ("tsk_a", "tsk_b", "tsk_c"):
        nag_state.transition(lambda s, tid=tid: nag_state.apply_snooze(
            s, tid, snoozed_until=until, block_reason=None))
    # Cycle 2: the snoozed leaders must NOT hold cap slots -- d and e fire instead.
    sent = []
    counts = nag_check.run_nag_check(limit=3, sender=fake_sender(sent))
    fired = " ".join(text for _target, text in sent)
    assert "tsk_d" in fired and "tsk_e" in fired
    for snoozed in ("tsk_a", "tsk_b", "tsk_c"):
        assert snoozed not in fired  # paused leaders did not consume slots
    assert counts["sent"] == 2  # only d and e were firable and within cap
    assert counts["deferred"] == 0  # nothing FIRABLE was held back by the cap


def test_nag_display_limit_floors_at_one(monkeypatch):
    """D2: a 0/negative NAG_DISPLAY_LIMIT (misconfig) must not silently mute the
    engine -- the knob can shrink the push but never switch it off."""
    import cos_config  # noqa: PLC0415
    for bad in ("0", "-1", "-9", "  -3 "):
        monkeypatch.setenv("NAG_DISPLAY_LIMIT", bad)
        assert cos_config.nag_display_limit() == 1
    monkeypatch.setenv("NAG_DISPLAY_LIMIT", "5")
    assert cos_config.nag_display_limit() == 5


def test_nag_all_list_empty_board_says_caught_up(tmp_path, monkeypatch, capsys):
    """`/nag all` on a board with nothing overdue past threshold is a clean 'caught up',
    not an empty reply."""
    board = tmp_path / "Work Tasks.md"
    board.write_text(
        "# Work\n\n## 🟡 Q2\n- [ ] **Soon** task_id::tsk_future 🗓️2026-07-15 area:: Ops\n",
        encoding="utf-8")
    state = tmp_path / "state"
    _set_env(monkeypatch, board, state)
    monkeypatch.setattr(nag_check, "_today", lambda: REF)
    rc = nag_check.main(["--list"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "caught up" in out.lower()
