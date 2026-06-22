"""H3 outbox: idempotent, receipt-capturing send layer.

Invariants pinned here:
- deliver_once calls the sender EXACTLY ONCE per idem_key (no double-send); a
  repeat returns the recorded receipt with idempotent=True.
- a sender that RAISES propagates out, records nothing, and leaves no phantom entry.
- openclaw_sender extracts messageId from possibly-noisy JSON stdout; a non-zero
  exit / missing messageId / unparseable output RAISES (a delivery failure, never a
  silent phantom success).

The fake sender returns canned message ids only (never a real openclaw call).
"""

import json
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import outbox  # noqa: E402

TARGET = {"chat_id": "-4242424242", "topic_id": "2",
          "agent_id": "niemand-work", "channel": "telegram"}
FAKE_ID = "1915"
FAKE_ID_2 = "2020"


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_MGMT_STATE_DIR", str(tmp_path / "state"))
    return tmp_path / "state"


def _outbox(state):
    path = state / "outbox.json"
    return json.loads(path.read_text()) if path.exists() else {}


# --- make_idem_key ---------------------------------------------------------

def test_make_idem_key_is_stable_and_namespaced():
    key = outbox.make_idem_key("nag", "tsk_x", "nag_loop_y", "2026-06-22")
    assert key == "nag:tsk_x:nag_loop_y:2026-06-22"


def test_make_idem_key_rejects_unknown_kind():
    with pytest.raises(ValueError):
        outbox.make_idem_key("bogus", "tsk_x")


# --- deliver_once idempotency ----------------------------------------------

def test_deliver_once_calls_sender_exactly_once_per_key(state):
    calls = []

    def sender(target, text):
        calls.append((target, text))
        return {"message_id": FAKE_ID}

    key = outbox.make_idem_key("nag", "tsk_x", "loop_1", "2026-06-22")
    first = outbox.deliver_once(TARGET, "hello", key, sender=sender)
    second = outbox.deliver_once(TARGET, "hello again", key, sender=sender)

    assert len(calls) == 1  # the sender ran ONCE -- no double-send
    assert first["idempotent"] is False and first["message_id"] == FAKE_ID
    assert second["idempotent"] is True  # 2nd returns the recorded receipt
    assert second["message_id"] == FAKE_ID
    assert second["target"] == TARGET
    # The text of the 2nd call was NOT sent (the recorded receipt stands).
    assert calls[0] == (TARGET, "hello")


def test_deliver_once_distinct_keys_each_send(state):
    calls = []

    def sender(target, text):
        calls.append(text)
        return {"message_id": str(len(calls))}

    k1 = outbox.make_idem_key("nag", "tsk_x", "loop_1", "2026-06-22")
    k2 = outbox.make_idem_key("nag", "tsk_x", "loop_1", "2026-06-23")  # next day
    outbox.deliver_once(TARGET, "day1", k1, sender=sender)
    outbox.deliver_once(TARGET, "day2", k2, sender=sender)
    assert calls == ["day1", "day2"]  # different keys -> two sends


def test_deliver_once_records_receipt_on_disk(state):
    key = outbox.make_idem_key("nag", "tsk_x", "loop_1", "2026-06-22")
    outbox.deliver_once(TARGET, "hi", key, sender=lambda t, x: {"message_id": FAKE_ID})
    recorded = _outbox(state)[key]
    assert recorded["message_id"] == FAKE_ID
    assert recorded["target"] == TARGET
    assert "ts" in recorded


def test_deliver_once_sender_failure_records_nothing(state):
    """A sender that RAISES propagates out and records NO phantom entry, so the
    caller can leave the loop open and a later retry can deliver cleanly."""
    key = outbox.make_idem_key("nag", "tsk_x", "loop_1", "2026-06-22")

    def boom(target, text):
        raise outbox.OpenclawSendError("gateway down")

    with pytest.raises(outbox.OpenclawSendError):
        outbox.deliver_once(TARGET, "hi", key, sender=boom)
    assert key not in _outbox(state)  # nothing recorded -- no phantom send

    # A later clean retry with the SAME key delivers (the failure did not poison it).
    calls = []
    outbox.deliver_once(TARGET, "hi", key,
                        sender=lambda t, x: calls.append(x) or {"message_id": FAKE_ID})
    assert calls == ["hi"]
    assert _outbox(state)[key]["message_id"] == FAKE_ID


# --- is_recorded peek ------------------------------------------------------

def test_is_recorded_true_after_delivery_false_for_unseen_key(state):
    """is_recorded peeks the outbox under the same flock as deliver_once: True once a
    key has a recorded receipt, False for a key never delivered. This is the peek the
    nag engine uses to skip gating a same-cycle duplicate fire."""
    key = outbox.make_idem_key("nag", "tsk_x", "2026-06-22-11")
    assert outbox.is_recorded(key) is False  # never delivered -> not recorded
    outbox.deliver_once(TARGET, "hi", key, sender=lambda t, x: {"message_id": FAKE_ID})
    assert outbox.is_recorded(key) is True  # recorded after a delivery
    # An unrelated, never-delivered key is still unseen.
    assert outbox.is_recorded(outbox.make_idem_key("nag", "tsk_other", "2026-06-22-11")) is False


# --- openclaw_sender parsing -----------------------------------------------

def _fake_run(stdout="", returncode=0, stderr=""):
    """Build a stand-in subprocess.run that returns a canned CompletedProcess-like."""
    def _run(args, **kwargs):
        return types.SimpleNamespace(args=args, returncode=returncode,
                                     stdout=stdout, stderr=stderr)
    return _run


def test_openclaw_sender_extracts_message_id_from_noisy_stdout(monkeypatch):
    noisy = 'Config warnings\n{"messageId":"1915","payload":{"messageId":"1915"}}'
    monkeypatch.setattr(outbox.subprocess, "run", _fake_run(stdout=noisy))
    receipt = outbox.openclaw_sender(TARGET, "the nag text")
    assert receipt == {"message_id": "1915"}


def test_openclaw_sender_extracts_message_id_before_trailing_object(monkeypatch):
    """Fix 3: a SECOND JSON object after the receipt must not defeat extraction.

    A greedy ``{.*}`` span would run from the first ``{`` to the LAST ``}`` (across
    both objects) and fail to parse -- treating a DELIVERED message as a failure, so
    the loop re-sends next cycle with no idempotency protection. The raw_decode scan
    parses the FIRST complete object carrying messageId and stops there."""
    stdout = '{"messageId":"1915","ok":true}\n{"summary":"sent 1 message"}'
    monkeypatch.setattr(outbox.subprocess, "run", _fake_run(stdout=stdout))
    receipt = outbox.openclaw_sender(TARGET, "x")
    assert receipt == {"message_id": "1915"}


def test_openclaw_sender_extracts_message_id_past_brace_warning_line(monkeypatch):
    """Fix 3: a warning line that itself contains a ``{`` (but is not the receipt)
    must be skipped; the scan tries each ``{`` until it finds the receipt object."""
    stdout = ('WARN config { headroom } note\n'
              'not-json {oops\n'
              '{"messageId":"2020","payload":{"k":"v"}}')
    monkeypatch.setattr(outbox.subprocess, "run", _fake_run(stdout=stdout))
    receipt = outbox.openclaw_sender(TARGET, "x")
    assert receipt == {"message_id": "2020"}


def test_openclaw_sender_raises_when_no_object_carries_message_id(monkeypatch):
    """Fix 3: multiple parseable objects but NONE carries messageId -> still a
    failure (no receipt, no proof of delivery to record)."""
    stdout = '{"warn":"x"}\n{"payload":{"messageId":"buried"}}'
    monkeypatch.setattr(outbox.subprocess, "run", _fake_run(stdout=stdout))
    with pytest.raises(outbox.OpenclawSendError):
        outbox.openclaw_sender(TARGET, "x")


def test_openclaw_sender_builds_listform_args_no_shell(monkeypatch):
    captured = {}

    def _run(args, **kwargs):
        captured["args"] = args
        captured["shell"] = kwargs.get("shell", False)
        return types.SimpleNamespace(returncode=0,
                                     stdout='{"messageId":"7"}', stderr="")

    monkeypatch.setattr(outbox.subprocess, "run", _run)
    outbox.openclaw_sender(TARGET, "body; rm -rf /")
    args = captured["args"]
    assert captured["shell"] is False  # never shell=True with interpolated text
    assert args[:3] == ["openclaw", "message", "send"]
    assert "--silent" not in args  # a nag MUST notify
    assert "--target" in args and TARGET["chat_id"] in args
    assert "--thread-id" in args and TARGET["topic_id"] in args
    # The message body is passed as ONE list arg, never interpolated into a shell.
    assert "body; rm -rf /" in args


def test_openclaw_sender_raises_on_nonzero_exit(monkeypatch):
    monkeypatch.setattr(outbox.subprocess, "run",
                        _fake_run(returncode=1, stderr="boom"))
    with pytest.raises(outbox.OpenclawSendError):
        outbox.openclaw_sender(TARGET, "x")


def test_openclaw_sender_raises_on_missing_message_id(monkeypatch):
    monkeypatch.setattr(outbox.subprocess, "run",
                        _fake_run(stdout='{"payload":{"ok":true}}'))
    with pytest.raises(outbox.OpenclawSendError):
        outbox.openclaw_sender(TARGET, "x")


def test_openclaw_sender_raises_on_unparseable_stdout(monkeypatch):
    monkeypatch.setattr(outbox.subprocess, "run",
                        _fake_run(stdout="no json here at all"))
    with pytest.raises(outbox.OpenclawSendError):
        outbox.openclaw_sender(TARGET, "x")


# --- review finding 4: bounded send (no unbounded hang under the lock) ------

def test_openclaw_sender_passes_a_positive_timeout(monkeypatch):
    """The send must be BOUNDED: subprocess.run is called with a positive timeout so
    a hung gateway cannot block forever while the nag-state lock is held."""
    captured = {}

    def _run(args, **kwargs):
        captured["timeout"] = kwargs.get("timeout")
        return types.SimpleNamespace(returncode=0, stdout='{"messageId":"7"}', stderr="")

    monkeypatch.setattr(outbox.subprocess, "run", _run)
    outbox.openclaw_sender(TARGET, "x")
    assert isinstance(captured["timeout"], (int, float)) and captured["timeout"] >= 1


def test_openclaw_sender_raises_on_timeout(monkeypatch):
    """A subprocess timeout (hung gateway) becomes an OpenclawSendError -- a delivery
    FAILURE the caller treats as a loop-stays-open block, never a phantom send. This
    is the reliability guard: the send runs under the nag-state lock that /done needs."""
    def _raise_timeout(args, **kwargs):
        raise outbox.subprocess.TimeoutExpired(cmd="openclaw", timeout=kwargs.get("timeout", 20))

    monkeypatch.setattr(outbox.subprocess, "run", _raise_timeout)
    with pytest.raises(outbox.OpenclawSendError):
        outbox.openclaw_sender(TARGET, "x")


# --- review finding 5: outbox.json stays flat (stale periods pruned) --------

def test_deliver_once_prunes_stale_periods(state, monkeypatch):
    """A delivered-receipt key older than the retention window is dropped on the next
    write, so outbox.json (and the per-run read-modify-write) never grows unbounded;
    recent keys and the new one survive."""
    from datetime import datetime, timedelta, timezone
    monkeypatch.setenv("OUTBOX_RETENTION_DAYS", "7")
    state.mkdir(parents=True, exist_ok=True)
    old_ts = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    recent_ts = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    (state / "outbox.json").write_text(json.dumps({
        "nag:old:2026-05-01-11": {"message_id": "1", "target": TARGET, "ts": old_ts},
        "nag:recent:2026-06-21-11": {"message_id": "2", "target": TARGET, "ts": recent_ts},
    }))
    outbox.deliver_once(TARGET, "new", outbox.make_idem_key("nag", "newtask", "2026-06-22-11"),
                        sender=lambda t, x: {"message_id": "3"})
    box = _outbox(state)
    assert "nag:old:2026-05-01-11" not in box       # stale dropped
    assert "nag:recent:2026-06-21-11" in box         # recent kept
    assert "nag:newtask:2026-06-22-11" in box        # new recorded
