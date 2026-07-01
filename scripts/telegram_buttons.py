#!/usr/bin/env python3
"""U1 inline-button send seam: the ``tt:`` ``callback_data`` codec + row-builders.

This is the SEND half of the v0.3 inline-button seam (the receive half -- gateway
plugin + dispatcher -- is U2, which imports ``decode`` from here so the scheme has one
source of truth on both sides). Nothing here mutates the board, authorises a tap, or
sends a message: it is pure transport that turns ``(action, task_id, arg)`` into the
compact ``callback_data`` Telegram round-trips, and assembles the button rows a caller
hands to ``outbox.deliver_once(.., buttons=...)``.

The scheme (KTD-3): ``tt:<action>:<task_id>[:<arg>]``, hard-capped at 64 UTF-8 BYTES
(Telegram's ``callback_data`` limit). ``encode`` counts BYTES, not characters, and
returns ``None`` -- never a malformed value, never raising -- when:

* the action is not a known action,
* the ``task_id`` is empty / not a string / contains the ``:`` field separator,
* the ``arg`` (when given) is empty / not a string / contains ``:``,
* or the assembled value exceeds 64 UTF-8 bytes.

A ``None`` from ``encode`` makes the row-builder OMIT that button, so the caller's
existing text command stays in the message body (graceful drop-fallback, mirroring
``dialpad/webhook_server.py:build_telegram_callback_data``). A dropped button degrades
UX; it never fails a delivery.

``decode`` is the EXACT inverse of ``encode`` for ALL input, not just well-formed input:
it splits a candidate triple off the value, then re-``encode``s it and accepts only when
the re-encode reproduces the input byte-for-byte. That round-trip check rejects any raw
value carrying an embedded ``:`` in a field (which a naive split would mis-attribute),
an over-budget length, an unknown action, or a wrong namespace -- so a downstream
dispatcher (U2 imports this) can never be handed a tuple ``encode`` could not have
produced. Note (U2's responsibility, not U1's): a structurally-valid decode does NOT
prove the ``task_id`` resolves to a real board task or that the tapping user owns it --
the dispatcher must still validate existence and re-authorise the action (KTD-4).
"""

from __future__ import annotations

from typing import Any

# The gateway splits the callback namespace on the first ``:`` (mirrors reply-watcher's
# ``rw``); ``tt`` is the task-tracker namespace this whole seam owns.
NAMESPACE = "tt"

# Telegram caps ``callback_data`` at 64 UTF-8 bytes. Measured in BYTES, not characters,
# so a multibyte task_id/arg cannot silently overflow the wire limit.
MAX_BYTES = 64

# The field separator. ``task_id`` and ``arg`` may not contain it, so a value always has
# a fixed field count and ``decode`` is unambiguous.
_SEP = ":"

# The known actions (KTD-3 table) mapped to their ARG POLICY. ``encode`` enforces the
# policy so a value carrying an arg the action never takes (or missing a required one) is
# rejected -- the codec emits ONLY the canonical shapes the row builders produce, and
# ``decode`` (which U2 imports as the trust-boundary shape check) rejects any forged
# callback_data outside that set. An unknown/typo'd action is rejected outright.
#
# Policies:
# * ``"none"``     -> the action is task-only; an arg is forbidden.
# * ``"required"`` -> the action is meaningless without its arg.
# * ``"optional"`` -> the arg may be present or absent (two distinct canonical forms).
#
# * ``done``  none      -> mark a task done                       (nag, EOD disposition)
# * ``start`` none      -> begin a focus block (the H7 initiation) (PRIORITY nag, U10)
# * ``snz``   required  -> snooze; arg is the span, e.g. ``1d``    (nag)
# * ``rsch``  optional  -> reschedule; no arg = open date options, arg = a target date
# * ``carry`` none      -> carry to tomorrow                       (EOD disposition)
# * ``drop``  none      -> drop to the parking lot                 (EOD disposition)
# * ``appr``  none      -> confirm a detected completion           (EOD confirm-gate)
# * ``top``   none      -> set as tomorrow's #1                    (EOD)
# * ``undo``  none      -> revert an auto completion by completion_id (standup veto)
_ARG_POLICY: dict[str, str] = {
    "done": "none",
    "start": "none",
    "snz": "required",
    "rsch": "optional",
    "carry": "none",
    "drop": "none",
    "appr": "none",
    "top": "none",
    "undo": "none",
}

# The known actions, derived from the policy map so the two can never drift.
KNOWN_ACTIONS: frozenset[str] = frozenset(_ARG_POLICY)


def encode(action: str, task_id: str, arg: str | None = None) -> str | None:
    """Build a ``tt:<action>:<task_id>[:<arg>]`` callback value, or ``None`` if invalid.

    Returns ``None`` (never raises, never a malformed ``tt:`` value) when the action is
    unknown, the ``task_id``/``arg`` is empty / not a string / contains ``:``, the
    action's ARG POLICY is violated (an arg on a task-only action, or a missing required
    arg), or the assembled value exceeds 64 UTF-8 BYTES. A ``None`` tells the row-builder
    to drop the button and keep the text command (the drop-fallback).
    """
    # Validate ``action`` is a clean string BEFORE indexing the policy map: an unhashable
    # garbage action (``[]``, ``{}``, ``set()``) would otherwise raise ``TypeError`` from
    # ``dict.get`` and break the documented never-raise contract. ``_is_clean_field`` also
    # rejects a ``:``-bearing action, which could never name a real action anyway.
    if not _is_clean_field(action):
        return None
    policy = _ARG_POLICY.get(action)
    if policy is None:  # unknown action
        return None
    if not _is_clean_field(task_id):
        return None
    if arg is None:
        if policy == "required":  # e.g. snooze without its span is meaningless
            return None
    else:
        if policy == "none":  # e.g. done/carry/drop/appr/top never carry an arg
            return None
        if not _is_clean_field(arg):
            return None
    parts = [NAMESPACE, action, task_id]
    if arg is not None:
        parts.append(arg)
    data = _SEP.join(parts)
    if len(data.encode("utf-8")) > MAX_BYTES:
        return None
    return data


def decode(data: str | None) -> tuple[str, str, str | None] | None:
    """The EXACT inverse of ``encode``: a value -> ``(action, task_id, arg)`` ONLY when
    ``encode`` could have produced that exact value, else ``None``.

    A naive ``split(_SEP, 3)`` is NOT a true inverse for hostile input: a raw
    ``tt:done:tsk_a:b`` would split into ``("done", "tsk_a", "b")`` even though the real
    id was ``tsk_a:b`` -- a tuple ``encode`` could never emit, which a downstream
    dispatcher (U2 imports this as the single source of truth for the scheme) would then
    act on with the WRONG task. So after splitting we re-``encode`` the decoded triple and
    require it equals the input byte-for-byte: that rejects any value carrying an embedded
    ``:`` in a field, an over-budget length, an unknown action, a wrong namespace, or a
    missing ``task_id`` -- i.e. anything outside ``encode``'s image. The split arithmetic
    only proposes a candidate; the round-trip check is the authority, so ``decode`` is a
    genuine inverse for ALL input, not just well-formed input.
    """
    if not isinstance(data, str) or not data:
        return None
    parts = data.split(_SEP, 3)
    if len(parts) < 3:
        return None
    namespace, action, task_id = parts[0], parts[1], parts[2]
    if namespace != NAMESPACE:
        return None
    arg = parts[3] if len(parts) == 4 else None
    # Authority: the candidate is valid only if re-encoding it reproduces the input
    # exactly. This is what makes decode a true inverse for hostile/raw values, not just
    # for strings encode itself produced (e.g. an embedded ':' in the arg is rejected here
    # even though the naive split accepted it into the arg field).
    if encode(action, task_id, arg) != data:
        return None
    return action, task_id, arg


def _is_clean_field(value: object) -> bool:
    """A field is clean iff it is a non-empty string with no ``:`` separator."""
    return isinstance(value, str) and bool(value) and _SEP not in value


def _button(label: str, action: str, task_id: str, arg: str | None = None) -> dict[str, Any] | None:
    """A single ``{"label","value"}`` button, or ``None`` when the value cannot be encoded.

    The ``value`` carries the ``callback_data`` the gateway routes back through the
    channel's interaction path (per the openclaw presentation contract). ``None`` here
    propagates the drop-fallback up to the row-builder, which omits the button.
    """
    value = encode(action, task_id, arg)
    if value is None:
        return None
    return {"label": label, "value": value}


def _row(*buttons: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Assemble a button list, dropping any button whose value could not be encoded.

    An over-budget or invalid button is silently omitted so the rest of the row still
    sends and the caller's text command remains the fallback for the dropped action.
    """
    return [button for button in buttons if button is not None]


def done_button(task_id: str) -> dict[str, Any] | None:
    """``tt:done:<id>`` -- mark the task done (nag + EOD disposition)."""
    return _button("Done", "done", task_id)


def start_button(task_id: str) -> dict[str, Any] | None:
    """``tt:start:<id>`` -- begin a focus block on the task (U10 priority-nag initiation).

    The PRIMARY action on a priority nag: a tap routes through the existing H7
    ``handle_start`` (cue + timer + muted nag), turning the nag from a guilt-reminder
    into an initiation lever. ``▶️ Start`` leads the priority row before Done/Snooze.
    """
    return _button("▶️ Start", "start", task_id)


def snooze_button(task_id: str, span: str = "1d") -> dict[str, Any] | None:
    """``tt:snz:<id>:<span>`` -- snooze the task (nag); default span ``1d``."""
    return _button(f"Snooze {span}", "snz", task_id, span)


def reschedule_button(task_id: str) -> dict[str, Any] | None:
    """``tt:rsch:<id>`` -- open reschedule date options (nag + EOD)."""
    return _button("Reschedule", "rsch", task_id)


def reschedule_date_button(task_id: str, date: str, label: str | None = None) -> dict[str, Any] | None:
    """``tt:rsch:<id>:<YYYY-MM-DD>`` -- reschedule to a specific date."""
    return _button(label or date, "rsch", task_id, date)


def carry_button(task_id: str) -> dict[str, Any] | None:
    """``tt:carry:<id>`` -- carry the task to tomorrow (EOD disposition)."""
    return _button("Carry", "carry", task_id)


def drop_button(task_id: str) -> dict[str, Any] | None:
    """``tt:drop:<id>`` -- drop the task to the parking lot (EOD disposition)."""
    return _button("Drop", "drop", task_id)


def approve_button(task_id: str) -> dict[str, Any] | None:
    """``tt:appr:<id>`` -- confirm a detected completion (EOD confirm-gate)."""
    return _button("Confirm", "appr", task_id)


def set_top_button(task_id: str) -> dict[str, Any] | None:
    """``tt:top:<id>`` -- set the task as tomorrow's #1 (EOD)."""
    return _button("Set as #1", "top", task_id)


def undo_button(completion_id: str) -> dict[str, Any] | None:
    """``tt:undo:<completion_id>`` -- revert an auto completion (standup veto)."""
    return _button("UNDO", "undo", completion_id)


def nag_row(task_id: str, *, snooze_span: str = "1d") -> list[dict[str, Any]]:
    """The overdue nag action row: Done / Snooze / Reschedule. Drops any over-budget button."""
    return _row(
        done_button(task_id),
        snooze_button(task_id, snooze_span),
        reschedule_button(task_id),
    )


def priority_nag_row(task_id: str, *, snooze_span: str = "1d") -> list[dict[str, Any]]:
    """The PRIORITY nag row (U10): ``▶️ Start`` FIRST (initiation), then Done / Snooze.

    Used for today's committed priorities (the ``focus_state`` daily 2-3 + the
    tomorrow-pointer #1). Start leads because the lever the user needs is INITIATION,
    not another overdue reminder (KTD-8). A dropped (over-budget) button degrades to the
    text command exactly as ``nag_row`` does."""
    return _row(
        start_button(task_id),
        done_button(task_id),
        snooze_button(task_id, snooze_span),
    )


def disposition_row(task_id: str) -> list[dict[str, Any]]:
    """The EOD forced-disposition row: Done / Carry / Reschedule / Drop."""
    return _row(
        done_button(task_id),
        carry_button(task_id),
        reschedule_button(task_id),
        drop_button(task_id),
    )


def reschedule_date_row(task_id: str, dates: list[tuple[str, str]]) -> list[dict[str, Any]]:
    """A reschedule date-option row from ``(label, YYYY-MM-DD)`` pairs (nag/EOD picker)."""
    return _row(
        *(reschedule_date_button(task_id, date, label) for label, date in dates)
    )
