#!/usr/bin/env python3
"""U4 delivery seam: the SINGLE proactive-push path for every nag + check-in.

Hard Gate #4 (DELIVERY-TARGET-PROOF): no proactive/background push may send
without proving its exact destination first. Every U4 push -- a nag re-fire and a
body-double check-in -- funnels through ``prove_and_gate`` here so the proof chain
is identical and tested once:

    1. ``prove_delivery_target(chat_id, topic_id)`` -- env-assembled allowlist,
       Work-group reject, env-missing block (Contract 2).  An unset env / wrong
       group returns BLOCKED -- never a guessed target.
    2. ``autonomy_gate.gate(act_type, delivery_target=...)`` -- re-proves the
       target INSIDE the gate and returns an ``act_id`` bound to it.
    3. ``autonomy_gate.assert_send_target(act_id, target)`` -- the gate<->message
       seam: the gated target is the SOLE permitted destination.  A buggy caller
       that gates topic:2 and sends topic:6 is blocked here, not delivered.

This module proves the target and AUTHORISES it (``authorise_target`` asserts the
seam); the caller performs the I/O only after ``ok`` is True. As of H3 the actual
Telegram transport is owned by the script, not the cron: the caller delivers the
authorised target through ``outbox.deliver_once`` (receipt-captured + idempotent),
so the proven target is the thing that actually sends -- closing the pre-H3 seam
where the proof validated a target the blind cron ``--announce`` never used.

Binding boundary (by design): the proof gates WHETHER a nag is delivered (an unset
/ work-group env blocks BEFORE any send, so nothing leaves) and binds the in-process
gate<->message seam. The bytes then leave through ``outbox.openclaw_sender``, which
sends to the EXACT proven ``chat_id`` + ``topic_id`` -- the same env pair
(``TELEGRAM_CHAT_ID_PRODUCTIVITY`` + ``OPENCLAW_TOPIC_PRODUCTIVITY_STANDUP``) the
proof reads. There is no out-of-process descriptor to diverge from anymore: the
sender's destination IS the proven target.

When the proof or seam fails, the caller MUST treat it as ``nag_delivery_blocked``
and leave the nag OPEN -- the env var being unset never clears a loop.
"""

from __future__ import annotations

import os
from typing import Any

from autonomy_gate import assert_send_target, gate
from delivery_target import prove_delivery_target

# The env var names U4 reads its target from. Decision #3: there is NO phantom
# PRODUCTIVITY_GROUP_ID -- the chat id is TELEGRAM_CHAT_ID_PRODUCTIVITY and the
# topic is OPENCLAW_TOPIC_PRODUCTIVITY_STANDUP (topic 2, the working-standup
# surface, per OQ2). Read live (not at import) so a secrets.conf change is honoured.
CHAT_ID_ENV = "TELEGRAM_CHAT_ID_PRODUCTIVITY"
TOPIC_ID_ENV = "OPENCLAW_TOPIC_PRODUCTIVITY_STANDUP"
AGENT_ID = "niemand-work"


def resolve_target() -> dict[str, Any]:
    """Prove the U4 nag delivery target from env (Contract 2).

    Returns the ``prove_delivery_target`` result: ``{"ok": True, "delivery_target":
    {...}}`` or ``{"ok": False, "reason": ...}``. Never a guessed target -- an unset
    ``TELEGRAM_CHAT_ID_PRODUCTIVITY`` or ``OPENCLAW_TOPIC_PRODUCTIVITY_STANDUP``
    yields ``reason: env_missing`` and the caller must block the push (nag stays
    open).
    """
    chat_id = os.getenv(CHAT_ID_ENV)
    topic_id = os.getenv(TOPIC_ID_ENV)
    return prove_delivery_target(chat_id, topic_id, agent_id=AGENT_ID)


def prove_and_gate(
    act_type: str,
    *,
    task_id: str | None = None,
    unit: str = "U4",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Prove the target, gate the act, and bind an authorised ``act_id``.

    Returns ``{"ok": True, "act_id", "delivery_target"}`` only when BOTH the env
    proof and the in-gate proof succeed AND the act executes (rung allows the
    push). Otherwise ``{"ok": False, "reason": ...}`` where ``reason`` is one of:

    * ``env_missing`` / ``work_group`` / ``target_unknown`` -- the env proof failed.
    * ``unproven-target`` / ``push-disabled`` / ``rung4`` -- the gate refused.

    The caller logs ``nag_delivery_blocked`` and leaves the loop OPEN on any
    non-ok result.
    """
    proof = resolve_target()
    if not proof["ok"]:
        return {"ok": False, "reason": proof["reason"],
                "stage": "prove", "message": proof.get("message")}

    target = proof["delivery_target"]
    gated = gate(act_type, delivery_target=target, task_id=task_id, unit=unit,
                 metadata=metadata)
    if not gated["ok"]:
        return {"ok": False, "reason": gated["reason"], "stage": "gate",
                "proof_reason": gated.get("proof_reason"),
                "act_id": gated.get("act_id")}

    return {"ok": True, "act_id": gated["act_id"], "delivery_target": target}


def authorise_target(
    act_id: str,
    delivery_target: dict[str, Any],
) -> dict[str, Any]:
    """Assert the gate<->message seam: confirm ``delivery_target`` is the SOLE
    permitted destination for ``act_id``, returning it authorised for delivery.

    ``assert_send_target`` is the Decision #1 seam: it confirms ``delivery_target``
    is the exact target ``act_id`` was gated for. A mismatch returns ``{"ok": False,
    "reason": "target-mismatch"}`` and the caller must NOT send.

    H3 boundary: this asserts the seam but does NOT itself perform the transport.
    The caller delivers through ``outbox.deliver_once`` only after this returns
    ``ok`` -- so the send is receipt-captured and idempotent, while the gate<->message
    binding is still proven here BEFORE any byte leaves. Splitting assert from send
    keeps the proof identical and the actual delivery owned by the receipt layer.
    """
    check = assert_send_target(act_id, delivery_target)
    if not check["ok"]:
        return {"ok": False, "reason": check["reason"], "stage": "assert",
                "message": check.get("message")}
    return {"ok": True, "delivery_target": delivery_target}
