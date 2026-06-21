#!/usr/bin/env python3
"""U6 delivery seam: the SINGLE proactive-push path for every brief + debrief.

Hard Gate #4 (DELIVERY-TARGET-PROOF): no proactive push may send without proving
its exact destination FIRST. Every U6 push -- daily brief, pre-brief, debrief
prompt, slip notification, Friday proposal -- funnels through ``prove_and_gate``
here so the proof chain is identical and tested once, exactly like U4's
``nag_delivery``:

    1. ``prove_delivery_target(chat_id, topic_id)`` -- env-assembled allowlist,
       Work-group reject, env-missing block (Contract 2). An unset env / wrong
       group returns BLOCKED -- never a guessed target.
    2. ``autonomy_gate.gate(act_type, delivery_target=...)`` -- re-proves the
       target INSIDE the gate and returns an ``act_id`` bound to it.
    3. ``autonomy_gate.assert_send_target(act_id, target)`` -- the gate<->message
       seam: the gated target is the SOLE permitted destination.

Order (mustFix #5): resolve+PROVE the delivery target FIRST -- before any freebusy
or calendar work -- so a push to an unprovable target is blocked before anything
is computed, and the proven target is what every message lands on. On a proof
failure the caller logs ``delivery_target_proof_failed`` and sends NOTHING; on
success it logs ``delivery_target_resolved`` immediately before the send.

The Friday proposal targets the WEEKLY_REVIEW_PLANNING topic (4); every other push
targets the STANDUP topic (2). Both are env-sourced and proven the same way.
"""

from __future__ import annotations

import os
from typing import Any, Callable

from autonomy_gate import assert_send_target, gate
from delivery_target import prove_delivery_target
from task_ledger import append_event, new_event

CHAT_ID_ENV = "TELEGRAM_CHAT_ID_PRODUCTIVITY"
STANDUP_TOPIC_ENV = "OPENCLAW_TOPIC_PRODUCTIVITY_STANDUP"
WEEKLY_TOPIC_ENV = "OPENCLAW_TOPIC_PRODUCTIVITY_WEEKLY_REVIEW_PLANNING"
AGENT_ID = "niemand-work"

# The two U6 push surfaces -> the env var holding their topic id. The brief/
# debrief/slip stream lands on the working standup topic; the Friday proposal
# lands on the weekly-planning topic.
SURFACE_TOPIC_ENV: dict[str, str] = {
    "standup": STANDUP_TOPIC_ENV,
    "weekly": WEEKLY_TOPIC_ENV,
}


def _log(event_type: str, **metadata: Any) -> None:
    """Append a U6 delivery ledger event (append-only, flocked by append_event)."""
    append_event(
        new_event(event_type, source="agent_autonomous", actor="proactive_brief", metadata=metadata)
    )


def resolve_target(surface: str = "standup") -> dict[str, Any]:
    """Prove the U6 delivery target for ``surface`` from env (Contract 2).

    Returns the ``prove_delivery_target`` result: ``{"ok": True, "delivery_target":
    {...}}`` or ``{"ok": False, "reason": ...}``. Never a guessed target -- an unset
    ``TELEGRAM_CHAT_ID_PRODUCTIVITY`` or the surface topic env var yields
    ``reason: env_missing`` and the caller must block the push.
    """
    chat_id = os.getenv(CHAT_ID_ENV)
    topic_id = os.getenv(SURFACE_TOPIC_ENV.get(surface, STANDUP_TOPIC_ENV))
    return prove_delivery_target(chat_id, topic_id, agent_id=AGENT_ID)


def prove_and_gate(
    act_type: str,
    *,
    surface: str = "standup",
    task_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Prove the target FIRST, gate the act, and bind an authorised ``act_id``.

    Emits ``delivery_target_resolved`` on success (with the proven target) and
    ``delivery_target_proof_failed`` on any failure (proof or gate), so the audit
    trail records the proof outcome of every push attempt. Returns
    ``{"ok": True, "act_id", "delivery_target"}`` only when BOTH the env proof and
    the in-gate proof succeed; otherwise ``{"ok": False, "reason": ...}``.
    """
    proof = resolve_target(surface)
    if not proof["ok"]:
        _log("delivery_target_proof_failed", act_type=act_type, surface=surface,
             reason=proof["reason"], stage="prove")
        return {"ok": False, "reason": proof["reason"], "stage": "prove",
                "message": proof.get("message")}

    target = proof["delivery_target"]
    gated = gate(act_type, delivery_target=target, task_id=task_id, unit="U6", metadata=metadata)
    if not gated["ok"]:
        _log("delivery_target_proof_failed", act_type=act_type, surface=surface,
             reason=gated["reason"], stage="gate")
        return {"ok": False, "reason": gated["reason"], "stage": "gate",
                "proof_reason": gated.get("proof_reason"), "act_id": gated.get("act_id")}

    _log("delivery_target_resolved", act_type=act_type, surface=surface,
         delivery_target=target, act_id=gated["act_id"])
    return {"ok": True, "act_id": gated["act_id"], "delivery_target": target}


def authorised_send(
    act_id: str,
    delivery_target: dict[str, Any],
    text: str,
    *,
    send: Callable[[dict[str, Any], str], Any],
) -> dict[str, Any]:
    """Assert the gate<->message seam, then send ONLY to the gated target.

    ``assert_send_target`` confirms ``delivery_target`` is the exact target
    ``act_id`` was gated for (Decision #1). The transport ``send`` is invoked only
    after that passes; a mismatch returns ``{"ok": False, "reason":
    "target-mismatch"}`` and NOTHING is sent. ``send`` is REQUIRED -- a missing
    transport is a delivery FAILURE, never a silent success (a brief logged as
    sent but delivered nowhere would be a phantom).
    """
    check = assert_send_target(act_id, delivery_target)
    if not check["ok"]:
        return {"ok": False, "reason": check["reason"], "stage": "assert",
                "message": check.get("message")}
    send(delivery_target, text)
    return {"ok": True, "delivery_target": delivery_target}
