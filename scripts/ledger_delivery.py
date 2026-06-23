#!/usr/bin/env python3
"""V2 receipt-backed delivery + consume for the SCHEDULED (``--auto``) U5 brag digest.

O3 HIGH 1 closed the same "proves intent, not delivery" seam for the weekly digest
that H3 closed for the nag (``nag_check`` + ``outbox``). Before V2 the scheduled
Friday digest PROVED its Done-topic target, then marked evidence + wins seen, logged
``ledger_draft_pushed`` and recorded a SUCCESS -- but the actual Telegram send was
left to the agent relay (the command cron announced the draft on stdout). A relay
crash / no-send / wrong-target then LOST the digest AND false-greened the ritual.

This module makes the AUTO digest OWN its send: it delivers the proven draft itself
through the receipt-backed outbox (``outbox.deliver_once`` -> ``openclaw_sender``),
captures the gateway message-id receipt, and consumes state ONLY when a receipt comes
back. A transport failure (sender raises) consumes NOTHING and leaves no pushed-window,
so the next fire re-attempts delivery; the idempotency key
``("ledger", harvest_window_id, kind)`` makes a re-fire after a SUCCESSFUL send a no-op
(``deliver_once`` short-circuits on the recorded receipt).

The REACTIVE ``/ledger`` path is NOT routed through ``deliver_auto_digest``: it
delivers to the user's DYNAMIC originating topic via the agent relay (interactive,
immediately visible), which is not the false-green risk O3 flagged -- there is no fixed
proven target to receipt-back -- so it consumes on PROOF (still through
``push_and_consume`` here, with ``auto=False``, which skips the receipt-backed send).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

import harvest_state
import outbox
import win_store
from task_ledger import append_event, new_event

# The kind component of the ledger idem-key: only the scheduled Friday digest is
# receipt-backed (the reactive pull delivers via the relay to a dynamic topic).
AUTO_KIND = "auto"

# The NON-draft status keys the auto-path announce may carry (operational only).
_AUTO_STATUS_KEYS = ("ok", "draft_pushed", "reason", "harvest_window_id",
                     "evidence_count", "win_count", "push_blocked_reason")


def auto_status_line(payload: dict[str, Any]) -> str:
    """A compact, NON-draft status line for the SCHEDULED (``--auto``) digest stdout.

    The auto digest OWNS its delivery (it already sent the draft via the receipt-backed
    outbox above), so the cron's blind ``announce`` of stdout must NOT re-announce the
    draft -- that would double-send. Emits only an operational status (no draft / no
    delivery_target) so the announce carries nothing user-facing -- the same shape
    ``checkin_dispatch`` uses for its already-delivered check-in.
    """
    return json.dumps({k: payload.get(k) for k in _AUTO_STATUS_KEYS},
                      sort_keys=True, default=str)


@dataclass(frozen=True)
class DigestPush:
    """The artifacts of ONE harvest run that are ready to deliver + consume.

    Bundles the harvest ``state`` plus the run's computed ``draft`` / ``fresh``
    evidence / ``wins`` / ``match_index`` / ``pending_task_ids`` so the delivery+consume
    seam takes one cohesive payload instead of a dozen positional locals threaded out of
    ``run_harvest``. ``pushed_key`` is the kind-aware dedup field (``auto_pushed_window``
    / ``reactive_pushed_window``).
    """

    state: dict[str, Any]
    window: str
    pushed_key: str
    harvest_window_id: str
    match_index: dict[str, dict[str, Any]]
    pending_task_ids: list[str]
    fresh: list[dict[str, Any]]
    wins: list[dict[str, Any]]
    draft: str


def deliver_auto_digest(
    delivery_target: dict[str, Any],
    draft: str,
    harvest_window_id: str,
    *,
    sender: Callable[[dict[str, Any], str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Deliver the AUTO digest AT MOST ONCE per ``(harvest_window_id, AUTO_KIND)``.

    The proven ``delivery_target`` is the thing that actually sends: the draft is
    handed to ``outbox.deliver_once`` (idempotent + receipt-capturing), keyed on the
    DURABLE window+kind identity so a cron retry after a successful send never
    double-sends the same Friday digest.

    Returns ``{"ok": True, "message_id", "idem_key", "idempotent": bool}`` on a receipt
    (a fresh send OR an idempotent short-circuit of a prior successful send -- both mean
    DELIVERED), or ``{"ok": False, "reason", "idem_key"}`` when the transport FAILED
    (the sender raised). NEVER raises -- a transport failure is caught and returned so
    the caller's consume-on-receipt branch is the single decision point.
    """
    idem_key = outbox.make_idem_key("ledger", harvest_window_id, AUTO_KIND)
    try:
        receipt = outbox.deliver_once(
            delivery_target, draft, idem_key, sender=sender or outbox.openclaw_sender
        )
    except Exception as exc:  # noqa: BLE001 -- a send failure is a delivery block, not
        # a crash: consume NOTHING, leave no pushed-window, record a health FAILURE.
        return {"ok": False, "reason": type(exc).__name__, "message": str(exc),
                "idem_key": idem_key}
    return {
        "ok": True,
        "message_id": receipt.get("message_id"),
        "idem_key": idem_key,
        "idempotent": bool(receipt.get("idempotent")),
    }


def _log_draft_pushed(
    proof: dict[str, Any], push: DigestPush, *, actor: str, source: str,
    message_id: str | None, idem_key: str | None,
) -> None:
    """Append the ``ledger_draft_pushed`` proof-of-DELIVERY event.

    Called ONLY after the digest is confirmed delivered -- a RECEIPT on the auto path
    (``message_id`` / ``idem_key`` carried so a replay can verify the gateway receipt),
    the relay handoff on the reactive path. So the presence of this event means the
    digest WAS delivered, never merely proven (the O3 HIGH 1 invariant).
    """
    metadata: dict[str, Any] = {
        "harvest_window_id": push.harvest_window_id,
        "delivery_target": proof["delivery_target"],
        "pending_task_ids": push.pending_task_ids,
        "evidence_count": len(push.fresh),
        "act_id": proof["act_id"],
    }
    if message_id is not None:
        metadata["message_id"] = message_id
    if idem_key is not None:
        metadata["idem_key"] = idem_key
    append_event(new_event("ledger_draft_pushed", actor=actor, source=source, metadata=metadata))


def _consume(proof: dict[str, Any], push: DigestPush, *, auto: bool) -> None:
    """Consume the digest's state: set the pushed-window, merge pending-approval ids,
    and (CANONICAL delivery only) mark evidence + wins seen, then persist. Called ONLY
    after the digest is confirmed DELIVERED -- a RECEIPT on the auto path, PROOF on the
    reactive (relay) path.

    Both paths record the push (``pushed_key``) and MERGE the pending-approval ids -- an
    id a reactive digest advertised as approvable ("/approve A") survives the Friday push
    so the user can still approve it. But ONLY the scheduled Friday digest (``auto``)
    marks evidence + wins SEEN: the auto digest is the canonical weekly record, so its
    items must not repeat. The reactive ``/ledger`` is a PREVIEW/pull -- if it consumed
    the week's evidence + wins, Friday's headline digest would render thin or empty
    (V-P1b). So a reactive pull never marks seen; Friday still delivers the FULL week.

    Assumption (the auto digest is the SOLE win consumer): the scheduled Friday
    ``ledger-cron --auto`` must fire weekly to consume wins -- ``read_unseen_wins`` is
    date-UNFILTERED (the H8 "never silently lost" rule), so if that digest were
    persistently blocked the reactive preview would re-render every accumulated win.
    That is bounded by the deployed Friday cron and is observable (a persistently
    blocked auto digest records a ``push_blocked`` health FAILURE, V2) -- and nothing is
    lost, the wins simply deliver once the block clears. A windowed preview read would
    be the mitigation if win accumulation ever became a real problem.
    """
    state = push.state
    state[push.pushed_key] = push.harvest_window_id
    state["draft_pushed_at"] = datetime.now(timezone.utc).isoformat()
    state["delivery_target"] = proof["delivery_target"]
    approved = set(state.get("approved_task_ids") or [])
    merged_ids = (set(state.get("pending_task_ids") or []) | set(push.pending_task_ids)) - approved
    state["pending_task_ids"] = sorted(merged_ids)
    state["pending_matches"] = {
        tid: match for tid, match in {
            **(state.get("pending_matches") or {}),
            **push.match_index,
        }.items() if tid not in approved
    }
    if auto:  # CANONICAL weekly delivery -- the reactive PREVIEW must not consume.
        harvest_state.mark_seen(state, push.fresh)
    harvest_state.save_state(state, push.window)
    if auto:
        win_store.mark_wins_seen([win["id"] for win in push.wins])


def push_and_consume(
    proof: dict[str, Any], push: DigestPush, *, auto: bool, actor: str, source: str,
    sender: Callable[[dict[str, Any], str], dict[str, Any]] | None,
) -> dict[str, Any]:
    """Deliver the digest and consume state ONLY on a confirmed delivery.

    Returns the ``push`` result the caller threads into its response:
    ``{"ok", "delivery_target", "reason"}``.

    * proof BLOCKED (env unset / Work-group / gate rejection): nothing is delivered or
      consumed; carries the proof reason as ``push_blocked_reason``.
    * AUTO + proof ok: the script OWNS the send via the receipt-backed outbox. Evidence
      + wins are consumed and ``ledger_draft_pushed`` is logged ONLY when the transport
      returns a RECEIPT. A transport failure consumes NOTHING, sets no pushed-window,
      and surfaces ``reason`` so the cron path records a health FAILURE -- the digest is
      never lost-and-false-greened (O3 HIGH 1). The idem-key makes a re-fire after a
      successful send a no-op (``deliver_once`` short-circuits on the recorded receipt).
    * REACTIVE + proof ok: the agent relay delivers to the user's dynamic originating
      topic, so the proof IS the delivery confirmation -- consume on proof.
    """
    if not proof["ok"]:
        return {"ok": False, "reason": proof.get("reason", "gate_blocked")}

    message_id: str | None = None
    idem_key: str | None = None
    if auto:
        # AUTO digest: the script owns the send. Consume ONLY on a real receipt.
        delivered = deliver_auto_digest(proof["delivery_target"], push.draft,
                                        push.harvest_window_id, sender=sender)
        if not delivered["ok"]:
            # Transport failed: consume NOTHING, set no pushed-window. The reason flows
            # to push_blocked_reason so the cron path records a FAILURE (no false-green).
            return {"ok": False, "reason": f"delivery_failed:{delivered['reason']}"}
        message_id, idem_key = delivered.get("message_id"), delivered.get("idem_key")
    # Delivered (auto: a receipt; reactive: the relay handoff). Log the proof-of-delivery
    # push event and consume state -- but only the canonical AUTO digest marks evidence +
    # wins seen (the reactive PREVIEW must not gut Friday's full-week digest -- V-P1b).
    _log_draft_pushed(proof, push, actor=actor, source=source,
                      message_id=message_id, idem_key=idem_key)
    _consume(proof, push, auto=auto)
    return {"ok": True, "delivery_target": proof["delivery_target"]}
