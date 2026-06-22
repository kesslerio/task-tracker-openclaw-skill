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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None

from cos_config import nag_send_timeout_seconds, outbox_retention_days, state_dir
from utils import _atomic_write

# The only outbox key kind today. A frozen set so a typo'd kind is caught at the
# call site rather than silently producing an un-deduped key.
_KNOWN_KINDS: frozenset[str] = frozenset({"nag"})


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
    ``(task_id, period)`` where ``period`` is the scheduled cron cycle (local
    date+hour). The identity is deliberately DURABLE: it omits the random
    ``nag_loop_id`` (which is minted before the loop is persisted), so a same-cycle
    retry dedupes to one send EVEN IF the loop state was never written -- closing the
    first-fire double-send window. A later cycle (new ``period``) is a NEW delivery,
    so the 11/14/17 re-nag cadence is preserved; only a duplicate of the SAME fire
    (same task, same cycle) is suppressed.
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
    cannot confidently age out, since a wrongly-pruned key would re-send.
    """
    if not ts:
        return False
    try:
        return datetime.fromisoformat(ts) < cutoff
    except ValueError:
        return False


def _prune_outbox(state: dict[str, Any]) -> None:
    """Drop entries older than the retention window so ``outbox.json`` stays flat.

    The outbox only needs RECENT periods to dedupe a same-cycle retry; a key from
    days ago can never collide with a current ``(task_id, date+hour)`` key, so it is
    dead weight on every read-modify-write. Pruned in place on write.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=outbox_retention_days())
    for key in [k for k, v in state.items()
                if isinstance(v, dict) and _entry_before(v.get("ts"), cutoff)]:
        del state[key]


def deliver_once(
    delivery_target: dict[str, Any],
    text: str,
    idem_key: str,
    *,
    sender: Callable[[dict[str, Any], str], dict[str, Any]],
) -> dict[str, Any]:
    """Deliver ``text`` to ``delivery_target`` AT MOST ONCE per ``idem_key``.

    The whole read-modify-write of ``outbox.json`` runs under an exclusive sidecar
    flock (the same pattern ``nag_state.transition`` uses) so two concurrent fires
    of the same logical nag cannot both pass the recorded-key check and double-send:

    * ``idem_key`` already recorded -> return the stored receipt with
      ``idempotent: True`` and DO NOT call ``sender`` (the message was already
      delivered; re-sending would spam the user).
    * unseen ``idem_key`` -> call ``sender(delivery_target, text)`` (which returns
      ``{"message_id": str}`` or RAISES on a transport failure), record
      ``{message_id, target, ts}`` atomically, and return it with
      ``idempotent: False``.

    A ``sender`` that raises propagates OUT of the lock having recorded NOTHING --
    the caller treats it as a delivery failure and leaves the nag loop OPEN. Only a
    real receipt is ever recorded, so the outbox never contains a phantom send.
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
            state = _read_outbox()
            recorded = state.get(idem_key)
            if isinstance(recorded, dict):
                return {
                    "message_id": recorded.get("message_id"),
                    "target": recorded.get("target"),
                    "ts": recorded.get("ts"),
                    "idempotent": True,
                }
            receipt = sender(delivery_target, text)
            entry = {
                "message_id": str(receipt["message_id"]),
                "target": delivery_target,
                "ts": _now_iso(),
            }
            state[idem_key] = entry
            _prune_outbox(state)  # drop stale periods so outbox.json stays flat
            _write_outbox(state)
            return {**entry, "idempotent": False}
        finally:
            if fcntl is not None:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


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


def openclaw_sender(delivery_target: dict[str, Any], text: str) -> dict[str, Any]:
    """PRODUCTION sender: ``openclaw message send`` to the PROVEN target.

    List-form args (never ``shell=True`` with interpolated text) so the message body
    can never be shell-injected. No ``--silent`` -- a nag is meant to notify. On a
    non-zero exit, missing receipt, or unparseable output this raises
    ``OpenclawSendError`` so the caller leaves the loop OPEN; it returns
    ``{"message_id": str}`` only on a proven send.
    """
    args = [
        "openclaw", "message", "send",
        "--channel", "telegram",
        "--target", str(delivery_target["chat_id"]),
        "--thread-id", str(delivery_target["topic_id"]),
        "--message", text,
        "--json",
    ]
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
