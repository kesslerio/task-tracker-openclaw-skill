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


def test_make_idem_key_initiation_is_known_and_namespaced():
    # v0.4-C: the initiation nudge key is (focus_episode_id, stage); the
    # focus_episode_id slot carries user_scope, so the colon-joined key stays
    # namespaced distinctly from nag/checkin/ledger/eod.
    key = outbox.make_idem_key("initiation", "work:tsk_x:2026-06-24", "cold_start")
    assert key == "initiation:work:tsk_x:2026-06-24:cold_start"


def test_initiation_is_a_known_kind():
    assert "initiation" in outbox._KNOWN_KINDS


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


# --- R2 Fix A: a corrupt-ts entry can never fail a committed delivery -------

def test_deliver_once_garbage_ts_entry_does_not_raise_or_lose_send(state):
    """R2 Fix A: a pre-existing entry with a NON-STRING ts (an int -- a corrupt or
    hand-edited entry) makes ``datetime.fromisoformat`` raise TypeError inside the
    prune that runs AFTER the receipt is committed and the message SENT. Pre-fix that
    TypeError propagated out of deliver_once, the caller marked delivery_failed, and
    the SAME message re-sent next run (a double-send). Now: the send happens ONCE, the
    new receipt is durably recorded, and deliver_once does NOT raise."""
    state.mkdir(parents=True, exist_ok=True)
    # Seed the outbox with a garbage-ts entry that the prune walk will hit.
    (state / "outbox.json").write_text(json.dumps({
        "nag:garbage:2026-06-01-11": {"message_id": "x", "target": TARGET, "ts": 12345},
    }))
    calls = []

    def sender(target, text):
        calls.append(text)
        return {"message_id": FAKE_ID}

    key = outbox.make_idem_key("nag", "newtask", "2026-06-22-11")
    # The send + record must NOT raise despite the garbage-ts neighbour.
    receipt = outbox.deliver_once(TARGET, "fresh", key, sender=sender)
    assert calls == ["fresh"]  # sent exactly once -- not lost, not re-sent
    assert receipt["idempotent"] is False and receipt["message_id"] == FAKE_ID
    # The receipt is durably recorded (the at-most-once fact survives the prune).
    assert _outbox(state)[key]["message_id"] == FAKE_ID

    # And a same-key re-fire dedupes off that recorded receipt -- no double-send.
    calls2 = []
    second = outbox.deliver_once(TARGET, "fresh", key,
                                 sender=lambda t, x: calls2.append(x) or {"message_id": "z"})
    assert calls2 == []  # the sender was NOT called again
    assert second["idempotent"] is True


def test_deliver_once_prune_failure_still_leaves_the_receipt(state, monkeypatch):
    """R2 Fix A: even if pruning itself raises (any reason), the just-written receipt
    MUST survive and deliver_once MUST NOT raise -- a delivered message always leaves a
    recorded receipt so it is never re-sent."""
    state.mkdir(parents=True, exist_ok=True)

    def boom_prune(_state):
        raise RuntimeError("prune blew up")

    monkeypatch.setattr(outbox, "_prune_outbox", boom_prune)
    key = outbox.make_idem_key("nag", "t", "2026-06-22-11")
    receipt = outbox.deliver_once(TARGET, "hi", key,
                                  sender=lambda t, x: {"message_id": FAKE_ID})
    assert receipt["idempotent"] is False
    assert _outbox(state)[key]["message_id"] == FAKE_ID  # receipt committed despite prune fail


# --- R2 Fix B: get_receipt returns the stored receipt for split-brain repair --

def test_get_receipt_returns_stored_receipt_or_none(state):
    """get_receipt returns the committed {message_id, target, ts} for a delivered key
    (the durable fact a caller repairs split-brain state/ledger from), or None for an
    unseen key."""
    key = outbox.make_idem_key("nag", "tsk_x", "2026-06-22-11")
    assert outbox.get_receipt(key) is None  # never delivered
    outbox.deliver_once(TARGET, "hi", key, sender=lambda t, x: {"message_id": FAKE_ID})
    receipt = outbox.get_receipt(key)
    assert receipt is not None
    assert receipt["message_id"] == FAKE_ID
    assert receipt["target"] == TARGET
    assert "ts" in receipt
    assert outbox.get_receipt(outbox.make_idem_key("nag", "other", "2026-06-22-11")) is None


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


def test_nag_send_timeout_default_is_ten(monkeypatch):
    """R2 Fix E: the send runs under the nag-state lock that reactive /done also takes,
    so a hung gateway makes /done wait the full timeout. The default is HALVED 20 -> 10
    to cut the worst-case wait; the env override + the floor still hold."""
    import cos_config  # noqa: PLC0415
    monkeypatch.delenv("NAG_SEND_TIMEOUT_SECONDS", raising=False)
    assert cos_config.nag_send_timeout_seconds() == 10  # default halved to 10
    monkeypatch.setenv("NAG_SEND_TIMEOUT_SECONDS", "30")
    assert cos_config.nag_send_timeout_seconds() == 30  # env override honoured
    monkeypatch.setenv("NAG_SEND_TIMEOUT_SECONDS", "0")
    assert cos_config.nag_send_timeout_seconds() == 1  # floor preserved


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


# --- U1: optional inline buttons (--presentation) ---------------------------

# Mirrors the KTD-3 scheme: a button dict is {"label", "value"} with the value carrying
# the tt: callback_data. The codec that produces these is exercised in
# test_telegram_buttons.py; here we only assert the SEND/THREAD plumbing.
BUTTONS = [
    {"label": "Done", "value": "tt:done:tsk_abc"},
    {"label": "Snooze 1d", "value": "tt:snz:tsk_abc:1d"},
]


def _capture_run(stdout='{"messageId":"7"}', returncode=0, stderr=""):
    """A subprocess.run stub that records the argv it was called with."""
    captured = {}

    def _run(args, **kwargs):
        captured["args"] = args
        captured["shell"] = kwargs.get("shell", False)
        return types.SimpleNamespace(args=args, returncode=returncode,
                                     stdout=stdout, stderr=stderr)

    return captured, _run


def test_openclaw_sender_no_buttons_argv_is_byte_for_byte_unchanged(monkeypatch):
    """Characterization: the no-buttons argv carries NO --presentation flag and is
    exactly the historical plain-text send. Calling with buttons=None must equal calling
    with no buttons arg at all, so every existing caller is unaffected by U1."""
    cap_default, run_default = _capture_run()
    monkeypatch.setattr(outbox.subprocess, "run", run_default)
    outbox.openclaw_sender(TARGET, "the nag text")
    argv_default = cap_default["args"]

    cap_none, run_none = _capture_run()
    monkeypatch.setattr(outbox.subprocess, "run", run_none)
    outbox.openclaw_sender(TARGET, "the nag text", None)
    argv_none = cap_none["args"]

    cap_empty, run_empty = _capture_run()
    monkeypatch.setattr(outbox.subprocess, "run", run_empty)
    outbox.openclaw_sender(TARGET, "the nag text", [])  # empty list is also no-buttons
    argv_empty = cap_empty["args"]

    assert "--presentation" not in argv_default
    assert argv_default == argv_none == argv_empty  # byte-for-byte identical


def test_openclaw_sender_with_buttons_appends_presentation_json(monkeypatch):
    """A buttons-bearing send appends --presentation carrying the expected
    MessagePresentation: a text block then a buttons block whose button values are the
    callback_data. The plain --message body is preserved as the text fallback, and the
    proven target/thread are unchanged."""
    captured, run = _capture_run()
    monkeypatch.setattr(outbox.subprocess, "run", run)
    receipt = outbox.openclaw_sender(TARGET, "Your #1 today", buttons=BUTTONS)

    args = captured["args"]
    assert captured["shell"] is False  # still never shell=True
    assert "--presentation" in args
    # --message text fallback is preserved alongside the presentation block.
    assert "--message" in args and "Your #1 today" in args
    # Target/thread still point at the proven delivery target.
    assert "--target" in args and TARGET["chat_id"] in args
    assert "--thread-id" in args and TARGET["topic_id"] in args
    # The --presentation value parses to the documented shape.
    presentation = json.loads(args[args.index("--presentation") + 1])
    assert presentation == {
        "blocks": [
            {"type": "text", "text": "Your #1 today"},
            {"type": "buttons", "buttons": BUTTONS},
        ]
    }
    # A proven send still returns the captured receipt.
    assert receipt == {"message_id": "7"}


def test_openclaw_sender_with_buttons_raises_on_failure_no_receipt(monkeypatch):
    """A transport failure with buttons present is still a delivery FAILURE: it raises
    OpenclawSendError and fabricates no receipt (unchanged failure semantics)."""
    monkeypatch.setattr(outbox.subprocess, "run",
                        _fake_run(returncode=1, stderr="gateway down"))
    with pytest.raises(outbox.OpenclawSendError):
        outbox.openclaw_sender(TARGET, "x", buttons=BUTTONS)

    # Unparseable stdout with buttons present also raises (no messageId -> no proof).
    monkeypatch.setattr(outbox.subprocess, "run",
                        _fake_run(stdout="no json here"))
    with pytest.raises(outbox.OpenclawSendError):
        outbox.openclaw_sender(TARGET, "x", buttons=BUTTONS)


def test_openclaw_sender_unserialisable_button_raises_openclaw_send_error(monkeypatch):
    """A non-JSON-serializable button value makes the presentation build raise; it must
    surface as OpenclawSendError (the documented delivery-failure type), NOT a raw
    TypeError that would crash the caller's run. subprocess.run is never reached."""
    def _must_not_run(*a, **k):
        raise AssertionError("subprocess.run must not be called on a serialisation failure")

    monkeypatch.setattr(outbox.subprocess, "run", _must_not_run)
    bad_buttons = [{"label": "Done", "value": object()}]  # object() is not JSON-serializable
    with pytest.raises(outbox.OpenclawSendError):
        outbox.openclaw_sender(TARGET, "x", buttons=bad_buttons)


def test_deliver_once_with_buttons_sender_failure_records_nothing(state):
    """The at-most-once / no-phantom-receipt invariant must hold on the BUTTONS branch too:
    a sender that raises with buttons present propagates out, records nothing, and a later
    clean retry with the SAME key still delivers."""
    key = outbox.make_idem_key("nag", "tsk_btn", "2026-06-22-11")

    def boom(target, text, buttons=None):
        raise outbox.OpenclawSendError("gateway down")

    with pytest.raises(outbox.OpenclawSendError):
        outbox.deliver_once(TARGET, "nag", key, sender=boom, buttons=BUTTONS)
    assert key not in _outbox(state)  # no phantom receipt on the buttons path

    # A later clean retry with the same key delivers (the failure did not poison it).
    calls = []

    def ok(target, text, buttons=None):
        calls.append((text, buttons))
        return {"message_id": FAKE_ID}

    receipt = outbox.deliver_once(TARGET, "nag", key, sender=ok, buttons=BUTTONS)
    assert receipt["message_id"] == FAKE_ID and calls == [("nag", BUTTONS)]


def test_deliver_once_threads_buttons_to_sender(state):
    """deliver_once passes the buttons list to the sender as a third positional, records
    one receipt, and a same-key re-fire dedupes off it WITHOUT re-sending -- buttons are
    not part of the idem-key."""
    calls = []

    def sender(target, text, buttons=None):
        calls.append((text, buttons))
        return {"message_id": FAKE_ID}

    key = outbox.make_idem_key("nag", "tsk_abc", "2026-06-22-11")
    first = outbox.deliver_once(TARGET, "nag text", key, sender=sender, buttons=BUTTONS)
    assert first["idempotent"] is False and first["message_id"] == FAKE_ID
    assert calls == [("nag text", BUTTONS)]  # buttons threaded through

    # A re-fire with the SAME key (even different buttons) does NOT re-call the sender.
    second = outbox.deliver_once(TARGET, "nag text", key, sender=sender, buttons=[])
    assert second["idempotent"] is True
    assert len(calls) == 1  # sender called exactly once -- buttons don't change the key


def test_deliver_once_no_buttons_calls_sender_with_two_args(state):
    """When buttons is omitted, deliver_once calls the sender with the historical TWO
    positional args, so an existing two-arg sender (the common test fake) keeps working."""
    seen = []

    def two_arg_sender(target, text):  # no buttons parameter at all
        seen.append((target, text))
        return {"message_id": FAKE_ID}

    key = outbox.make_idem_key("nag", "tsk_x", "2026-06-22-11")
    receipt = outbox.deliver_once(TARGET, "hi", key, sender=two_arg_sender)
    assert receipt["message_id"] == FAKE_ID
    assert seen == [(TARGET, "hi")]  # two-arg call, no buttons positional


def test_deliver_once_empty_buttons_first_fire_calls_two_arg_sender(state):
    """An empty buttons list on a FIRST fire is "no buttons": deliver_once calls the
    two-arg sender (no third positional), matching openclaw_sender's `if buttons:` guard
    so a two-arg fake never sees a surprise third arg."""
    seen = []

    def two_arg_sender(target, text):  # would TypeError if passed a third positional
        seen.append((target, text))
        return {"message_id": FAKE_ID}

    key = outbox.make_idem_key("nag", "tsk_empty", "2026-06-22-11")
    receipt = outbox.deliver_once(TARGET, "hi", key, sender=two_arg_sender, buttons=[])
    assert receipt["message_id"] == FAKE_ID
    assert seen == [(TARGET, "hi")]  # empty list -> two-arg call, no buttons positional
