"""U1 telegram_buttons: the ``tt:`` callback_data codec + KTD-3 row-builders.

Invariants pinned here:
- ``encode``/``decode`` round-trip the ``tt:<action>:<task_id>[:<arg>]`` scheme.
- the 64-byte guard counts UTF-8 BYTES, not characters; an over-budget value -> ``None``.
- empty / garbage / unknown-action / ``:``-bearing input -> ``None`` (never a malformed
  ``tt:`` value, never a raise).
- a row-builder OMITS any button whose ``encode`` returned ``None`` (the drop-fallback),
  keeping the rest of the row.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import telegram_buttons as tb  # noqa: E402

TASK = "tsk_370c9723f3f84241"  # a FAKE id with the real tsk_+16hex shape (20 bytes)


# --- encode / decode round-trips ------------------------------------------

def test_encode_done_round_trips():
    data = tb.encode("done", TASK)
    assert data == f"tt:done:{TASK}"
    assert tb.decode(data) == ("done", TASK, None)


def test_encode_reschedule_with_date_round_trips():
    data = tb.encode("rsch", TASK, "2026-06-24")
    assert data == f"tt:rsch:{TASK}:2026-06-24"
    assert tb.decode(data) == ("rsch", TASK, "2026-06-24")


def test_encode_snooze_span_round_trips():
    data = tb.encode("snz", TASK, "1d")
    assert data == f"tt:snz:{TASK}:1d"
    assert tb.decode(data) == ("snz", TASK, "1d")


def test_every_known_action_round_trips():
    # snz REQUIRES an arg (its span); the rest round-trip in their no-arg canonical form.
    for action in tb.KNOWN_ACTIONS:
        arg = "1d" if action == "snz" else None
        data = tb.encode(action, TASK, arg)
        assert data is not None
        assert tb.decode(data) == (action, TASK, arg)


# --- byte guard (BYTES, not chars) ----------------------------------------

def test_encode_over_64_bytes_returns_none():
    # A task_id long enough to push the value past 64 bytes -> dropped.
    long_id = "tsk_" + "a" * 80
    assert len(f"tt:done:{long_id}".encode("utf-8")) > tb.MAX_BYTES
    assert tb.encode("done", long_id) is None


def test_encode_counts_utf8_bytes_not_characters():
    # 'é' is 2 UTF-8 bytes. Pick a task_id whose CHARACTER count fits in 64 but whose
    # BYTE count overflows, to prove the guard measures bytes.
    prefix = "tt:done:"  # 8 bytes
    budget_chars = tb.MAX_BYTES - len(prefix)  # chars that WOULD fit if counted as chars
    multibyte_id = "é" * budget_chars  # budget_chars characters, 2x bytes each
    value = f"{prefix}{multibyte_id}"
    assert len(value) <= tb.MAX_BYTES  # char length is within budget
    assert len(value.encode("utf-8")) > tb.MAX_BYTES  # byte length overflows
    assert tb.encode("done", multibyte_id) is None  # rejected on BYTES


def test_encode_at_exactly_64_bytes_is_accepted():
    # Build a value that is exactly 64 bytes -> accepted (boundary inclusive).
    prefix = "tt:done:"  # 8 bytes
    task_id = "x" * (tb.MAX_BYTES - len(prefix))  # fills to exactly 64 bytes
    value = tb.encode("done", task_id)
    assert value is not None
    assert len(value.encode("utf-8")) == tb.MAX_BYTES


# --- empty / garbage / malformed input ------------------------------------

def test_encode_unknown_action_returns_none():
    assert tb.encode("bogus", TASK) is None


def test_encode_never_raises_on_garbage_action():
    """The never-raise contract holds even for an UNHASHABLE garbage action: encode must
    return None, not let a TypeError from indexing the policy map escape."""
    for bad in ([], {}, set(), 42, None, "", "do:ne"):
        assert tb.encode(bad, TASK) is None  # type: ignore[arg-type]


# --- per-action arg policy (encode emits only canonical KTD-3 shapes) -------

def test_encode_task_only_action_rejects_an_arg():
    """done/carry/drop/appr/top are task-only -- an arg is not a shape any row builder
    emits, so encode rejects it (and decode therefore rejects the raw value too)."""
    for action in ("done", "carry", "drop", "appr", "top"):
        assert tb.encode(action, TASK) is not None        # the canonical no-arg form is valid
        assert tb.encode(action, TASK, "x") is None        # an arg is rejected
        assert tb.decode(f"tt:{action}:{TASK}:x") is None   # round-trip guard rejects it


def test_encode_snooze_requires_an_arg():
    """snz is meaningless without its span -- a bare tt:snz:<id> is not a canonical form."""
    assert tb.encode("snz", TASK, "1d") is not None  # with a span: valid
    assert tb.encode("snz", TASK) is None             # without a span: rejected
    assert tb.decode(f"tt:snz:{TASK}") is None         # round-trip guard rejects the raw value


def test_encode_reschedule_arg_is_optional():
    """rsch has two canonical forms: open picker (no arg) and a target date (arg)."""
    assert tb.encode("rsch", TASK) is not None                # open picker
    assert tb.encode("rsch", TASK, "2026-06-24") is not None  # to a date
    assert tb.decode(f"tt:rsch:{TASK}") == ("rsch", TASK, None)
    assert tb.decode(f"tt:rsch:{TASK}:2026-06-24") == ("rsch", TASK, "2026-06-24")


def test_encode_empty_action_returns_none():
    assert tb.encode("", TASK) is None


def test_encode_empty_task_id_returns_none():
    assert tb.encode("done", "") is None


def test_encode_non_string_task_id_returns_none():
    assert tb.encode("done", None) is None  # type: ignore[arg-type]


def test_encode_task_id_with_separator_returns_none():
    # A ``:`` in the task_id would corrupt the field layout -> rejected.
    assert tb.encode("done", "tsk_a:b") is None


def test_encode_arg_with_separator_returns_none():
    assert tb.encode("rsch", TASK, "2026:06:24") is None


def test_encode_empty_arg_returns_none():
    assert tb.encode("rsch", TASK, "") is None


# --- decode rejects malformed values --------------------------------------

def test_decode_rejects_empty_and_non_string():
    assert tb.decode("") is None
    assert tb.decode(None) is None  # type: ignore[arg-type]


def test_decode_rejects_wrong_namespace():
    assert tb.decode(f"rw:done:{TASK}") is None


def test_decode_rejects_unknown_action():
    assert tb.decode(f"tt:bogus:{TASK}") is None


def test_decode_rejects_too_few_fields():
    assert tb.decode("tt:done") is None
    assert tb.decode("tt") is None


def test_decode_rejects_missing_task_id():
    assert tb.decode("tt:done:") is None


# --- decode is a TRUE inverse for hostile / raw input ----------------------

def test_decode_rejects_arg_on_task_only_action():
    """The round-trip guard + arg-policy: a task-only action (done/carry/drop/appr/top)
    never carries an arg, so tt:done:tsk_a:b is NOT an encode output -> decode rejects it
    (it would otherwise mis-split a ':'-bearing id into task_id+arg). This is what closes
    the mis-parse for the no-arg actions."""
    assert tb.encode("done", "tsk_a", "b") is None  # done never carries an arg
    assert tb.decode("tt:done:tsk_a:b") is None      # so decode rejects the raw value
    assert tb.decode("tt:carry:tsk_a:x") is None


def test_decode_rejects_value_with_extra_colons_in_arg():
    """tt:rsch:tsk_x:2026:06:24 splits arg to '2026:06:24' (a ':'-bearing arg encode
    forbids); the round-trip guard rejects it rather than mis-parsing into a valid-looking
    tuple a dispatcher would act on."""
    assert tb.decode("tt:rsch:tsk_x:2026:06:24") is None
    # Confirm the guard's mechanism: that candidate arg is not re-encodable.
    assert tb.encode("rsch", "tsk_x", "2026:06:24") is None


def test_decode_rejects_over_budget_value():
    """A raw value longer than 64 bytes is outside encode's image -> None (even though
    the naive split would otherwise succeed)."""
    long_id = "tsk_" + "a" * 80
    raw = f"tt:done:{long_id}"
    assert len(raw.encode("utf-8")) > tb.MAX_BYTES
    assert tb.decode(raw) is None


def test_decode_round_trips_every_encode_output():
    """Property: for every value encode emits, decode returns the originating triple.
    This locks decode as the exact inverse over encode's image."""
    cases = [
        ("done", TASK, None),
        ("snz", TASK, "1d"),
        ("rsch", TASK, "2026-06-24"),
        ("appr", TASK, None),
    ]
    for action, task_id, arg in cases:
        value = tb.encode(action, task_id, arg)
        assert value is not None
        assert tb.decode(value) == (action, task_id, arg)


# --- row-builders: drop-fallback ------------------------------------------

def test_done_button_shape():
    assert tb.done_button(TASK) == {"label": "Done", "value": f"tt:done:{TASK}"}


def test_nag_row_has_all_three_buttons():
    row = tb.nag_row(TASK)
    values = [b["value"] for b in row]
    assert values == [f"tt:done:{TASK}", f"tt:snz:{TASK}:1d", f"tt:rsch:{TASK}"]


def test_disposition_row_has_four_buttons():
    row = tb.disposition_row(TASK)
    actions = [tb.decode(b["value"])[0] for b in row]
    assert actions == ["done", "carry", "rsch", "drop"]


def test_row_builder_omits_over_budget_button_keeps_siblings():
    # Size the id so ``tt:done:<id>`` is exactly 64 bytes (fits) but the longer
    # ``tt:snz:<id>:1d`` overflows -> the snooze button drops while done survives.
    # ``tt:rsch:<id>`` shares done's 8-byte prefix with no suffix, so it also fits and
    # is KEPT -- the point is that the row drops ONLY the over-budget button, not its
    # siblings.
    base = "tt:done:"
    task_id = "y" * (tb.MAX_BYTES - len(base))  # done value == exactly 64 bytes
    assert tb.encode("done", task_id) is not None
    assert tb.encode("snz", task_id, "1d") is None     # snooze overflows -> dropped
    assert tb.encode("rsch", task_id) is not None       # reschedule fits -> kept
    row = tb.nag_row(task_id)
    values = [b["value"] for b in row]
    assert any(v.startswith("tt:done:") for v in values)   # done kept
    assert any(v.startswith("tt:rsch:") for v in values)   # reschedule kept
    assert all(not v.startswith("tt:snz:") for v in values)  # only the over-budget one dropped


def test_row_builder_drops_all_when_every_button_overflows():
    long_id = "z" * 200  # every encoded value overflows
    assert tb.nag_row(long_id) == []
    assert tb.disposition_row(long_id) == []


def test_reschedule_date_row_builds_labelled_buttons():
    row = tb.reschedule_date_row(
        TASK, [("Today", "2026-06-22"), ("Tomorrow", "2026-06-23")]
    )
    assert row == [
        {"label": "Today", "value": f"tt:rsch:{TASK}:2026-06-22"},
        {"label": "Tomorrow", "value": f"tt:rsch:{TASK}:2026-06-23"},
    ]
