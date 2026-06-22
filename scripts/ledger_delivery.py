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

from datetime import datetime, timezone
from typing import Any, Callable

import harvest_state
import outbox
import win_store
from task_ledger import append_event, new_event

# The kind component of the ledger idem-key: only the scheduled Friday digest is
# receipt-backed (the reactive pull delivers via the relay to a dynamic topic).
AUTO_KIND = "auto"


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


def log_draft_pushed(
    proof: dict[str, Any], *, harvest_window_id: str, pending_task_ids: list[str],
    evidence_count: int, actor: str, source: str,
    message_id: str | None = None, idem_key: str | None = None,
) -> None:
    """Append the ``ledger_draft_pushed`` proof-of-DELIVERY event.

    Called ONLY after the digest is confirmed delivered -- a RECEIPT on the auto path
    (``message_id`` / ``idem_key`` carried so a replay can verify the gateway receipt),
    the relay handoff on the reactive path. So the presence of this event means the
    digest WAS delivered, never merely proven (the O3 HIGH 1 invariant).
    """
    metadata: dict[str, Any] = {
        "harvest_window_id": harvest_window_id,
        "delivery_target": proof["delivery_target"],
        "pending_task_ids": pending_task_ids,
        "evidence_count": evidence_count,
        "act_id": proof["act_id"],
    }
    if message_id is not None:
        metadata["message_id"] = message_id
    if idem_key is not None:
        metadata["idem_key"] = idem_key
    append_event(new_event("ledger_draft_pushed", actor=actor, source=source, metadata=metadata))


def consume_pushed_state(
    proof: dict[str, Any], *, state: dict[str, Any], window: str, pushed_key: str,
    harvest_window_id: str, match_index: dict[str, dict[str, Any]],
    pending_task_ids: list[str], fresh: list[dict[str, Any]], wins: list[dict[str, Any]],
) -> None:
    """Consume the digest's state: set the pushed-window, merge pending-approval ids,
    mark evidence + wins seen, and persist. Called ONLY after the digest is confirmed
    DELIVERED -- a RECEIPT on the auto path, PROOF on the reactive (relay) path.

    Kind-aware dedup allows a reactive + a Friday auto push in one window, so the
    pending-approval state is MERGED across same-window pushes, never overwritten: an
    id the reactive digest advertised as approvable ("/approve A") survives the Friday
    push (whose fresh matches differ, or are wins-only -> empty). Evidence + wins are
    marked seen on the SAME success condition so a win/PR captured after this push stays
    unseen for the next one and a delivered item never repeats.
    """
    state[pushed_key] = harvest_window_id
    state["draft_pushed_at"] = datetime.now(timezone.utc).isoformat()
    state["delivery_target"] = proof["delivery_target"]
    approved = set(state.get("approved_task_ids") or [])
    merged_ids = (set(state.get("pending_task_ids") or []) | set(pending_task_ids)) - approved
    state["pending_task_ids"] = sorted(merged_ids)
    state["pending_matches"] = {
        tid: match for tid, match in {
            **(state.get("pending_matches") or {}),
            **match_index,
        }.items() if tid not in approved
    }
    harvest_state.mark_seen(state, [item["evidence_hash"] for item in fresh])
    harvest_state.save_state(state, window)
    win_store.mark_wins_seen([win["id"] for win in wins])


def push_and_consume(
    proof: dict[str, Any], *, state: dict[str, Any], window: str, pushed_key: str,
    harvest_window_id: str, match_index: dict[str, dict[str, Any]],
    pending_task_ids: list[str], fresh: list[dict[str, Any]], wins: list[dict[str, Any]],
    draft: str, auto: bool, actor: str, source: str,
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
        delivered = deliver_auto_digest(proof["delivery_target"], draft,
                                        harvest_window_id, sender=sender)
        if not delivered["ok"]:
            # Transport failed: consume NOTHING, set no pushed-window. The reason flows
            # to push_blocked_reason so the cron path records a FAILURE (no false-green).
            return {"ok": False, "reason": f"delivery_failed:{delivered['reason']}"}
        message_id, idem_key = delivered.get("message_id"), delivered.get("idem_key")
    # Delivered (auto: a receipt; reactive: the relay handoff). Log the
    # proof-of-delivery push event and consume evidence + wins.
    log_draft_pushed(proof, harvest_window_id=harvest_window_id,
                     pending_task_ids=pending_task_ids, evidence_count=len(fresh),
                     actor=actor, source=source, message_id=message_id, idem_key=idem_key)
    consume_pushed_state(
        proof, state=state, window=window, pushed_key=pushed_key,
        harvest_window_id=harvest_window_id, match_index=match_index,
        pending_task_ids=pending_task_ids, fresh=fresh, wins=wins,
    )
    return {"ok": True, "delivery_target": proof["delivery_target"]}
