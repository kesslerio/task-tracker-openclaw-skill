#!/usr/bin/env python3
"""U4 receipt-backed, idempotent outbox: the script OWNS the gated send.

H3 closes the "proves intent, not delivery" seam. Before H3 the nag engine proved
a delivery target, then handed the proven text to an injected ``send`` that, in
production, merely COLLECTED the text for the cron's blind ``--announce`` of stdout
-- the proven target was discarded, the actual transport was the cron, no Telegram
message-id receipt was captured, and nothing was idempotent. This module makes the
script send the gated message itself, capture the gateway's message-id receipt, and
record it so a re-fire of the SAME logical nag (same task + loop + period) can never
double-send.

Two layers:

* ``deliver_once`` -- the idempotency + receipt-recording barrier. Under the nag
  outbox flock it checks ``outbox.json`` for the ``idem_key``: a recorded key short
  -circuits to the stored receipt WITHOUT calling ``sender`` (no double-send); an
  unseen key calls the INJECTED ``sender``, atomically records the receipt, and
  returns it. ``sender`` is injectable so tests pass a fake; production passes
  ``openclaw_sender``.
* ``openclaw_sender`` -- the production transport. It shells out to
  ``openclaw message send ... --json`` (list-form args, never ``shell=True``),
  extracts the JSON receipt from possibly-noisy stdout, and returns the
  ``message_id``. ANY failure (non-zero exit, unparseable output, missing
  ``messageId``) RAISES -- the caller MUST treat a raised sender as a delivery
  FAILURE: log it and leave the nag loop OPEN. A phantom "sent" is never recorded.

The outbox file reuses the Phase-0a atomic-write + flock helpers (``_atomic_write``
and the sidecar-lockfile pattern ``nag_state.transition`` already uses) rather than
inventing a new locking scheme -- one read-modify-write of ``outbox.json`` is
serialised per delivery so a concurrent fire cannot lose an entry or double-send.
"""

from __future__ import annotations

import json
import os
import subprocess
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None

from cos_config import nag_send_timeout_seconds, outbox_retention_days, state_dir
from utils import _atomic_write

# The outbox key kinds. A frozen set so a typo'd kind is caught at the call site
# rather than silently producing an un-deduped key.
#
# * ``nag`` -- a nag re-fire, keyed on (task_id, period).
# * ``checkin`` -- a V1 focus/body-double check-in, keyed on (session_id, phase)
#   where phase is ``halfway`` | ``end``. The phase (not a clock period) is the
#   dedupe identity: each check-in is a single logical send for its session phase,
#   so a deterministic-dispatcher cron RETRY (the command cron re-runs) can never
#   double-send the same halfway/end nudge.
# * ``ledger`` -- a U5 weekly brag digest, keyed on (harvest_window_id, kind) where
#   kind is ``auto`` (the scheduled Friday push). The window+kind (not a clock
#   period) is the dedupe identity: one digest per window per kind, so a cron retry
#   AFTER a successful send short-circuits to the recorded receipt and never
#   double-sends the same Friday digest.
# * ``eod`` -- a U7 EOD ritual delivery, keyed on the local CALENDAR DATE. One EOD per
#   day: a same-day cron retry (or a manual re-fire before midnight) short-circuits to
#   the recorded receipt and never double-sends the evening ritual.
# * ``initiation`` -- a v0.4 initiation nudge ("you said X was today's #1, it's 2pm,
#   not started -- Start it?"), keyed on (focus_episode_id, stage) where
#   ``focus_episode_id`` is the deterministic committed-#1 episode SLOT
#   (``<user_scope>:<task_id>:<local_date>`` -- see ``initiation_contract``) and
#   ``stage`` is ``cold_start`` | ``cold_start_renudge``. The slot+stage (NOT a clock
#   period, and NOT a focus-session id -- a cold-start nudge fires BEFORE any focus
#   session exists) is the dedupe identity: a deterministic-dispatcher cron RETRY can
#   never double-send the same stage's nudge for the day's #1.
_KNOWN_KINDS: frozenset[str] = frozenset({"nag", "checkin", "ledger", "eod", "initiation"})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def outbox_path() -> Path:
    return state_dir() / "outbox.json"


def outbox_lock_path() -> Path:
    return state_dir() / "outbox.lock"


def make_idem_key(kind: str, *parts: str) -> str:
    """Stable idempotency key, e.g. ``nag:tsk_x:2026-06-22-11``.

    ``kind`` must be a known kind (today only ``"nag"``); ``parts`` are the
    identity that makes one logical delivery unique -- for a nag that is
    ``(task_id, period)`` where ``period`` is the scheduled cron SLOT the fire falls
    into (``date + slot-hour``; see ``nag_check._nag_slot_period``), NOT the raw
    wall-clock hour. The identity is deliberately DURABLE: it omits the random
    ``nag_loop_id`` (which is minted before the loop is persisted), so a same-cycle
    retry dedupes to one send EVEN IF the loop state was never written -- closing the
    first-fire double-send window. A later slot (new ``period``) is a NEW delivery, so
    the 11/16 re-nag cadence is preserved; every fire of the SAME slot (the cron fire,
    a retry, an out-of-band manual run before the next slot) is suppressed to one send.
    """
    if kind not in _KNOWN_KINDS:
        raise ValueError(f"unknown outbox idem-key kind {kind!r}")
    return ":".join((kind, *parts))


def _read_outbox() -> dict[str, Any]:
    """Read outbox.json, treating a missing/corrupt file as empty.

    A corrupt outbox must fail toward "not yet delivered" (re-send) rather than
    crash the nag run; a duplicate Telegram message is recoverable, a crashed nag
    cron is a silent accountability gap. The next clean write rebuilds the file.
    """
    path = outbox_path()
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _write_outbox(state: dict[str, Any]) -> None:
    _atomic_write(outbox_path(), json.dumps(state, indent=2, sort_keys=True) + "\n")


def _entry_before(ts: str | None, cutoff: datetime) -> bool:
    """True if a receipt's timestamp is older than ``cutoff``.

    A missing/garbage ts is treated as NOT-stale (kept) -- we never drop an entry we
    cannot confidently age out, since a wrongly-pruned key would re-send. A
    non-string ``ts`` (an int, a list -- a corrupt/hand-edited entry) makes
    ``fromisoformat`` raise ``TypeError``, not ``ValueError``; both are caught here so
    a single garbage entry can NEVER raise out of pruning (which runs AFTER the
    receipt is committed and the message sent -- a raise there would re-send).
    """
    if not ts:
        return False
    try:
        return datetime.fromisoformat(ts) < cutoff
    except (ValueError, TypeError):
        return False


def _prune_outbox(state: dict[str, Any]) -> None:
    """Drop entries older than the retention window so ``outbox.json`` stays flat.

    The outbox only needs RECENT periods to dedupe a same-cycle retry; a key from
    days ago can never collide with a current ``(task_id, date+slot)`` key, so it is
    dead weight on every read-modify-write. Pruned in place on write.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=outbox_retention_days())
    for key in [k for k, v in state.items()
                if isinstance(v, dict) and _entry_before(v.get("ts"), cutoff)]:
        del state[key]


@contextmanager
def _outbox_flock() -> Iterator[None]:
    """Hold the exclusive sidecar flock over ``outbox.json`` (the same pattern
    ``nag_state.transition`` uses) for the duration of the block.

    Both ``deliver_once`` (read-modify-write) and ``is_recorded`` (read-only peek)
    acquire the lock through HERE, so the peek sees a consistent snapshot relative
    to a concurrent delivery -- it can never read a half-written ``outbox.json``,
    and a delivery cannot land a new key inside a single peek's read.
    """
    state_dir()  # ensure the 0o700 dir exists before opening the lockfile
    lock_path = outbox_lock_path()
    with lock_path.open("a", encoding="utf-8") as lock_handle:
        try:
            os.fchmod(lock_handle.fileno(), 0o600)
        except OSError:
            pass
        if fcntl is not None:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def get_receipt(idem_key: str) -> dict[str, Any] | None:
    """Read-only: the stored ``{message_id, target, ts}`` for ``idem_key``, or None.

    Taken under the SAME sidecar flock ``deliver_once`` uses so the read is consistent
    with a concurrent delivery -- it can never read a half-written ``outbox.json``, and
    a delivery cannot land a new key inside this read. The nag engine PEEKS this BEFORE
    gating: a recorded receipt short-circuits a same-cycle duplicate fire (avoiding the
    phantom ``executed`` autonomy-gate act that gating-then-discovering-the-dup would
    log) AND lets the caller REPAIR split-brain state+ledger from what was actually
    delivered (a first fire that wrote the receipt but crashed before persisting the
    loop). It is an OPTIMISATION, never the authority: ``deliver_once`` re-checks the
    recorded key under its own flock, so the rare TOCTOU (another fire records between
    this peek and that flock) is still caught and deduped there. A returned dict is the
    committed proof of delivery; None means no receipt.
    """
    with _outbox_flock():
        recorded = _read_outbox().get(idem_key)
        return dict(recorded) if isinstance(recorded, dict) else None


def is_recorded(idem_key: str) -> bool:
    """Read-only peek: is ``idem_key`` already recorded (a receipt exists)?

    A thin bool over ``get_receipt`` (same flock, same snapshot semantics) for callers
    that only need existence, not the stored receipt.
    """
    return get_receipt(idem_key) is not None


def deliver_once(
    delivery_target: dict[str, Any],
    text: str,
    idem_key: str,
    *,
    sender: Callable[..., dict[str, Any]],
    buttons: list[dict[str, Any]] | None = None,
    precheck: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Deliver ``text`` to ``delivery_target`` AT MOST ONCE per ``idem_key``.

    The whole read-modify-write of ``outbox.json`` runs under an exclusive sidecar
    flock (the same pattern ``nag_state.transition`` uses) so two concurrent fires
    of the same logical nag cannot both pass the recorded-key check and double-send:

    * ``idem_key`` already recorded -> return the stored receipt with
      ``idempotent: True`` and DO NOT call ``sender`` (the message was already
      delivered; re-sending would spam the user).
    * unseen ``idem_key`` -> call ``sender(delivery_target, text, buttons)`` (which
      returns ``{"message_id": str}`` or RAISES on a transport failure), record
      ``{message_id, target, ts}`` atomically, and return it with
      ``idempotent: False``.

    ``buttons`` (U1) is an OPTIONAL list of presentation button dicts. It is NOT part of
    the idem-key, because a button message and its text-only twin for the same logical
    send are the SAME delivery -- adding buttons to the key would let an identical
    re-fire double-send. When ``buttons`` is empty (``None`` or ``[]``) the sender is
    called with the SAME two positional args as before (``sender(target, text)``) -- so
    every existing two-arg ``sender`` keeps working untouched; only a NON-EMPTY buttons
    list passes the third positional, which the production ``openclaw_sender`` accepts.

    A ``sender`` that raises propagates OUT of the lock having recorded NOTHING --
    the caller treats it as a delivery failure and leaves the nag loop OPEN. Only a
    real receipt is ever recorded, so the outbox never contains a phantom send. This
    under-flock recorded-key check is the AUTHORITATIVE dedup; an upstream
    ``get_receipt`` / ``is_recorded`` peek is only an optimisation.

    ``precheck`` (v0.4-C) is an OPTIONAL last-instant send-time guard evaluated INSIDE
    the flock, AFTER the recorded-key dedup and immediately BEFORE the sender. It is the
    "atomic claim" seam for the initiation CAS: the dispatcher passes a thunk that
    re-reads the proposal's task-state/focus-episode versions, and a ``False`` ABORTS
    the send (returns ``{idempotent: False, aborted: True}`` -- no sender call, no
    receipt recorded), so a proposal that went stale between evaluation and the held
    lock never delivers. ``None`` (every existing caller) keeps the historical behaviour.
    A caller that passes ``precheck`` MUST check ``receipt.get("aborted")`` BEFORE
    treating ``idempotent: False`` as a fresh send -- an abort is NEITHER sent nor a
    recorded duplicate, so a caller that only branches on ``idempotent`` would log a
    phantom send. (Callers that pass no ``precheck`` can never see the abort shape.)
    """
    with _outbox_flock():
        state = _read_outbox()
        recorded = state.get(idem_key)
        if isinstance(recorded, dict):
            return {
                "message_id": recorded.get("message_id"),
                "target": recorded.get("target"),
                "ts": recorded.get("ts"),
                "idempotent": True,
            }
        # Last-instant CAS, inside the lock and after the dedup: a stale claim aborts
        # WITHOUT sending or recording, so the slot stays open for a corrected re-fire.
        if precheck is not None and not precheck():
            return {"message_id": None, "target": None, "ts": None,
                    "idempotent": False, "aborted": True}
        # A no-buttons send (``None`` or an empty list) stays the historical two-arg call
        # ``sender(target, text)`` so existing two-arg senders and test fakes are
        # unaffected; only a non-empty buttons list passes the third positional. The
        # truthiness guard matches ``openclaw_sender``'s own ``if buttons:`` so ``[]``
        # means "no buttons" identically at both layers.
        receipt = (
            sender(delivery_target, text, buttons)
            if buttons
            else sender(delivery_target, text)
        )
        entry = {
            "message_id": str(receipt["message_id"]),
            "target": delivery_target,
            "ts": _now_iso(),
        }
        state[idem_key] = entry
        # RECEIPT FIRST, prune SECOND (best-effort). The just-sent message MUST leave
        # a recorded receipt -- that durable at-most-once fact is what stops the
        # caller's delivery_failed handler from re-sending the SAME message next run.
        # So write the receipt-bearing state BEFORE pruning; then prune-and-rewrite as
        # a swallowed best-effort step. If pruning (or its rewrite) ever raises -- a
        # garbage ts, a transient write error -- the exception is contained here and
        # NEVER propagates out of deliver_once: the receipt already on disk stands, the
        # message is not re-sent, and the only cost is a stale entry lingering one more
        # cycle (it ages out on the next clean write).
        _write_outbox(state)
        try:
            _prune_outbox(state)  # drop stale periods so outbox.json stays flat
            _write_outbox(state)
        except Exception:  # noqa: BLE001 -- prune is best-effort; the receipt is committed
            pass
        return {**entry, "idempotent": False}


class OpenclawSendError(RuntimeError):
    """The ``openclaw message send`` transport failed or returned no receipt.

    The caller MUST treat this as a delivery FAILURE -- log it, leave the nag loop
    OPEN, and record NO ``nag_sent``. It is never swallowed into a phantom success.
    """


def _extract_message_id(stdout: str) -> str:
    """Pull ``messageId`` out of the gateway's possibly-noisy JSON stdout.

    The gateway prefixes the receipt with warning/headroom lines (which can contain
    ``{``) and may print further objects AFTER it (a second JSON object, a trailing
    summary). A greedy ``{.*}`` span would run from the first ``{`` to the LAST ``}``
    across all of that and fail to parse. Instead we walk the TOP-LEVEL ``{`` starts
    and use ``json.JSONDecoder().raw_decode`` to parse the FIRST complete object that
    is a dict carrying a top-level ``messageId`` -- tolerating noisy prefixes AND
    trailing objects. When an object parses, we skip PAST its end (not into its
    interior) so a ``messageId`` nested in some object's ``payload`` is never mistaken
    for the receipt; when a ``{`` does not start a valid object (a warning brace), we
    advance one char. A run yielding no such object (no JSON, garbage, or no top-level
    ``messageId`` anywhere) raises -- no receipt, no proof of delivery to record.
    """
    text = stdout or ""
    decoder = json.JSONDecoder()
    index = text.find("{")
    while index != -1:
        try:
            obj, end = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            index = text.find("{", index + 1)  # a brace that starts no object (noise)
            continue
        if isinstance(obj, dict) and obj.get("messageId") is not None:
            return str(obj["messageId"])
        index = text.find("{", end)  # skip PAST this object -- don't probe its interior
    raise OpenclawSendError("openclaw send response carried no messageId")


def _presentation_json(text: str, buttons: list[dict[str, Any]]) -> str:
    """Build the ``--presentation`` MessagePresentation JSON: a text block + buttons row.

    Per the openclaw presentation contract (docs/plugins/message-presentation): a
    ``MessagePresentation`` is ``{title?, tone?, blocks:[...]}`` where each button block
    is ``{type:"buttons", buttons:[{label, value, ...}]}`` and a button's ``value`` is
    the ``callback_data`` routed back through the channel. The plain ``--message`` text
    stays the fallback the channel degrades to; this layers the inline buttons on top.
    """
    presentation = {
        "blocks": [
            {"type": "text", "text": text},
            {"type": "buttons", "buttons": buttons},
        ]
    }
    return json.dumps(presentation)


def openclaw_sender(
    delivery_target: dict[str, Any],
    text: str,
    buttons: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """PRODUCTION sender: ``openclaw message send`` to the PROVEN target.

    List-form args (never ``shell=True`` with interpolated text) so the message body
    can never be shell-injected. No ``--silent`` -- a nag is meant to notify. On a
    non-zero exit, missing receipt, or unparseable output this raises
    ``OpenclawSendError`` so the caller leaves the loop OPEN; it returns
    ``{"message_id": str}`` only on a proven send.

    ``buttons`` (U1) is an OPTIONAL list of presentation button dicts. When it is a
    NON-EMPTY list, a ``--presentation <json>`` flag carrying a text+buttons
    MessagePresentation is appended so Telegram renders inline action buttons; the plain
    ``--message`` body is kept as the channel's text fallback. When ``buttons`` is
    ``None`` or empty, the argv is BYTE-FOR-BYTE the historical plain-text send (no
    ``--presentation`` flag at all), so every existing caller is unaffected.
    """
    args = [
        "openclaw", "message", "send",
        "--channel", "telegram",
        "--target", str(delivery_target["chat_id"]),
        "--thread-id", str(delivery_target["topic_id"]),
        "--message", text,
        "--json",
    ]
    if buttons:
        # ``_presentation_json`` -> ``json.dumps`` raises ``TypeError`` on a
        # non-serializable button ``value`` (a stray datetime/set a future hand-built
        # caller could pass). Convert it to ``OpenclawSendError`` so the documented
        # delivery-failure contract holds: a caller catching ``OpenclawSendError`` leaves
        # the loop OPEN instead of taking an unhandled ``TypeError`` that crashes the run.
        # This runs before ``subprocess.run`` so no phantom receipt can be recorded.
        try:
            presentation = _presentation_json(text, buttons)
        except (TypeError, ValueError) as exc:
            raise OpenclawSendError(f"buttons could not be serialised: {exc}") from exc
        args += ["--presentation", presentation]
    try:
        result = subprocess.run(args, capture_output=True, text=True, check=False,
                                timeout=nag_send_timeout_seconds())
    except subprocess.TimeoutExpired as exc:
        # The send runs UNDER the nag-state lock; reactive /done takes the same lock,
        # so an unbounded hang would wedge the user's ability to ack (the exact trust
        # window the design protects). A timeout is a delivery FAILURE: raise so the
        # caller leaves the loop OPEN and the lock is released.
        raise OpenclawSendError(
            f"openclaw send timed out after {nag_send_timeout_seconds()}s") from exc
    except (OSError, ValueError) as exc:
        raise OpenclawSendError(f"openclaw send could not be launched: {exc}") from exc
    if result.returncode != 0:
        raise OpenclawSendError(
            f"openclaw send exited {result.returncode}: {(result.stderr or '').strip()}"
        )
    return {"message_id": _extract_message_id(result.stdout)}
