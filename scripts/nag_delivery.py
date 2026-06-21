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

The actual Telegram transport is the cron ``delivery.to`` field / the reactive
session reply -- this module proves the target and authorises it; the caller does
the I/O only after ``ok`` is True.  ``send`` is injectable so tests can assert
"sent vs blocked" without a live gateway.

Transport-binding boundary (by design): the production ``send`` collects the
proven nag text for the cron to announce; the bytes themselves leave via the
gateway cron descriptor's ``delivery.to``, which is templated from the SAME env
vars this module proves against (``TELEGRAM_CHAT_ID_PRODUCTIVITY`` +
``OPENCLAW_TOPIC_PRODUCTIVITY_STANDUP``). The proof therefore gates WHETHER text is
emitted (an unset / work-group env blocks emission, so nothing is announced) and
binds the in-process seam; it cannot reach into the out-of-process gateway
descriptor to re-assert its ``delivery.to`` at fire time. Keeping both surfaces
sourced from one env pair is the binding -- the cron descriptor's ``delivery.to``
MUST stay env-templated, never a literal, so the two never diverge.

When the proof or seam fails, the caller MUST treat it as ``nag_delivery_blocked``
and leave the nag OPEN -- the env var being unset never clears a loop.
"""

from __future__ import annotations

import os
from typing import Any, Callable

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


def authorised_send(
    act_id: str,
    delivery_target: dict[str, Any],
    text: str,
    *,
    send: Callable[[dict[str, Any], str], Any],
) -> dict[str, Any]:
    """Assert the gate<->message seam, then send ONLY to the gated target.

    ``assert_send_target`` is the Decision #1 seam: it confirms ``delivery_target``
    is the exact target ``act_id`` was gated for. The transport ``send`` is invoked
    only after that passes; a mismatch returns ``{"ok": False, "reason":
    "target-mismatch"}`` and NOTHING is sent.

    ``send`` is REQUIRED (not optional): a missing transport is a delivery FAILURE,
    not a silent success. Otherwise the production path would gate + log ``nag_sent``
    while delivering nothing -- the nag would be inert but recorded as sent. The
    caller is responsible for passing a real transport (or, for the cron, a
    collector whose payloads main() emits for the gateway ``delivery.to`` announce).
    """
    check = assert_send_target(act_id, delivery_target)
    if not check["ok"]:
        return {"ok": False, "reason": check["reason"], "stage": "assert",
                "message": check.get("message")}
    send(delivery_target, text)
    return {"ok": True, "delivery_target": delivery_target}
