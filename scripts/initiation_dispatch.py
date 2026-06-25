#!/usr/bin/env python3
"""v0.4-C initiation dispatcher: the "hands deliver" half of the initiation nudge.

A generalization of ``checkin_dispatch`` from body-double check-ins to initiation. A
recurring deterministic COMMAND cron (no LLM turn) runs ``run_tick`` each tick, which:

1. runs the C3 evaluator (``initiation_eval.decide_and_store``) -- if a nudge is due it
   parks an expiring ``Proposal`` in the store;
2. dispatches the pending proposal for today's committed-#1 slot (the one just written,
   OR a prior tick's that did not deliver) via ``run_dispatch``.

``run_dispatch`` mirrors the check-in dispatcher's discipline:
* reload the proposal; if absent/expired -> send nothing, clean exit;
* re-run the send-time CAS (``cas_still_valid_now`` -- the #1/priorities version + the
  focus-episode lifecycle). A STALE proposal is cleared and dropped;
* honor snooze + the point-in-time calendar -- both leave the proposal for a later tick
  within its TTL (waiting for a free, un-snoozed moment is the adaptive "good moment");
* RE-PROVE the delivery target NOW (``nag_delivery.resolve_target`` -- Hard Gate #4);
* render INERT templated text (a fixed "Start it?" nudge, escaped #1 title, NO raw task
  body) + the shipped ``priority_nag_row`` (Start / Done / Snooze);
* deliver AT MOST ONCE per ``initiation:<slot>:<stage>`` via ``outbox.deliver_once``,
  passing the CAS as an in-flock ``precheck`` -- the atomic claim: a proposal that goes
  stale between this re-check and the held lock aborts WITHOUT sending;
* clear the proposal once it is terminal (delivered / already-delivered / aborted-stale).

NO send escapes the outbox; ``main`` runs under ``error_envelope.run_main`` so any crash
is logged + health-recorded and exits 0 with a friendly line.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from typing import Any, Callable

import availability
import cos_config
import error_envelope
import focus_state
import initiation_contract
import initiation_eval
import initiation_store
import nag_delivery
import nag_state
import outbox
import redaction
import telegram_buttons

COMPONENT = "initiation_dispatch"
DEFAULT_SCRIPTS_DIR = "/data/.openclaw/skills/task-tracker/scripts"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _log(event_type: str, *, task_id: str | None = None, **metadata: Any) -> None:
    from task_ledger import append_event, new_event  # lazy: keep import surface thin

    append_event(new_event(event_type, task_id=task_id, source="agent_autonomous",
                           actor=COMPONENT, metadata=metadata))


def _record_health(*, ok: bool, error_class: str | None = None) -> None:
    try:
        import cos_health  # noqa: PLC0415 -- lazy + wrapped: health is best-effort

        if ok:
            cos_health.record_success(COMPONENT)
        else:
            cos_health.record_failure(COMPONENT, error_class=error_class or "initiation_failed",
                                      trigger=f"cron:{COMPONENT}")
    except Exception:  # noqa: BLE001 -- health recording is best-effort, never fatal
        pass


def _committed_title(task_id: str) -> str | None:
    """The committed #1's display title (single line, capped), matched to ``task_id``.

    Read fresh from focus-state so the nudge shows the user's own words. Sanitised to a
    single capped line so a multi-line/oversized title can never break the template; the
    whole rendered message also passes ``redaction.redact_message``. Returns None (the
    nudge falls back to a generic phrasing) if the current #1 does not match -- the CAS
    has already ensured it does, so a mismatch means do not guess a title.
    """
    state = focus_state.load_focus_state()
    for row in (state or {}).get("daily_priorities") or []:
        if isinstance(row, dict) and row.get("task_id") == task_id:
            title = row.get("title")
            return " ".join(title.split())[:120] if isinstance(title, str) and title.strip() else None
    return None


def _render_initiation_text(proposal: initiation_contract.Proposal) -> str:
    """INERT, fixed-template "Start it?" nudge. The title is the only variable, escaped
    to a single capped line; no raw task body, so there is no instruction channel."""
    title = _committed_title(proposal.task_id)
    head = "👋 You committed this as today's #1 — and you haven't started it yet."
    focus = f"\n  ▶️ {title}" if title else ""
    if proposal.stage == initiation_contract.STAGE_COLD_START_RENUDGE:
        head = "🔁 Still your #1, still not started."
    tail = "\nStart a focus block now? Or Done / Snooze below."
    return redaction.redact_message(head + focus + tail)


def run_dispatch(
    focus_episode_id: str,
    *,
    now: datetime | None = None,
    sender: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Dispatch the pending proposal for ``focus_episode_id`` at fire time. No LLM."""
    now = now or _now()
    proposal = initiation_store.read_proposal(focus_episode_id, now=now)
    if proposal is None:
        _record_health(ok=True)  # nothing pending / expired -> benign no-op
        return {"sent": False, "reason": "no-proposal", "focus_episode_id": focus_episode_id}

    # Send-time CAS: a stale proposal (the #1 changed, or the user already started) is
    # dead -- clear it and stop. (Re-checked again, atomically, in the outbox flock below.)
    if not initiation_contract.cas_still_valid_now(proposal):
        initiation_store.clear_proposal(focus_episode_id)
        _record_health(ok=True)
        return {"sent": False, "reason": "cas-stale", "focus_episode_id": focus_episode_id}

    # Snooze + calendar are "wait" states, NOT staleness: leave the proposal parked for a
    # later tick within its TTL (the adaptive "is now a good moment?").
    entry = nag_state.read_state().get(proposal.task_id)
    if nag_state.is_snoozed(entry, now=now):
        _record_health(ok=True)
        return {"sent": False, "reason": "snoozed", "focus_episode_id": focus_episode_id}
    if not availability.not_known_busy(now):
        _record_health(ok=True)
        return {"sent": False, "reason": "calendar-busy", "focus_episode_id": focus_episode_id}

    # Re-prove the target NOW -- never trust a baked-in target. Transient: leave parked.
    proof = nag_delivery.resolve_target()
    if not proof["ok"]:
        _log("initiation_delivery_blocked", task_id=proposal.task_id,
             focus_episode_id=focus_episode_id, stage=proposal.stage, reason=proof.get("reason"))
        _record_health(ok=False, error_class="target-unproven")
        return {"sent": False, "reason": "target-unproven",
                "focus_episode_id": focus_episode_id, "proof_reason": proof.get("reason")}

    text = _render_initiation_text(proposal)
    buttons = telegram_buttons.priority_nag_row(proposal.task_id)
    receipt = outbox.deliver_once(
        proof["delivery_target"], text, proposal.idem_key(),
        sender=sender or outbox.openclaw_sender, buttons=buttons,
        precheck=lambda: initiation_contract.cas_still_valid_now(proposal),
    )

    # Delivered / already-delivered / aborted-stale are all TERMINAL for this proposal.
    initiation_store.clear_proposal(focus_episode_id)
    _record_health(ok=True)
    if receipt.get("aborted"):
        return {"sent": False, "reason": "cas-stale-at-send", "focus_episode_id": focus_episode_id}
    if not receipt.get("idempotent"):
        _log("initiation_sent", task_id=proposal.task_id, focus_episode_id=focus_episode_id,
             stage=proposal.stage, reason_code=proposal.reason_code,
             message_id=receipt.get("message_id"), idem_key=proposal.idem_key())
    return {"sent": not receipt.get("idempotent"), "reason": "delivered",
            "idempotent": bool(receipt.get("idempotent")), "stage": proposal.stage,
            "focus_episode_id": focus_episode_id, "message_id": receipt.get("message_id")}


def _today_slot(now: datetime, *, user_scope: str = "work") -> str | None:
    """The committed-#1 episode slot for ``now``, or None when no #1 is committed.

    Single-sources the "today's committed #1" derivation with the evaluator so the
    dispatch tick and the evaluation agree on the slot.
    """
    first = initiation_eval._committed_first(now)
    if first is None:
        return None
    return initiation_contract.focus_episode_slot(
        user_scope, first[0], initiation_eval._local_date(now))


def run_tick(
    *, now: datetime | None = None, sender: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """The recurring cron entry: evaluate + store, then dispatch the pending proposal."""
    now = now or _now()
    initiation_eval.decide_and_store(now)
    slot = _today_slot(now)
    if slot is None:
        _record_health(ok=True)
        return {"sent": False, "reason": "no-committed-first"}
    return run_dispatch(slot, now=now, sender=sender)


def initiation_cron_descriptor(*, scripts_dir: str = DEFAULT_SCRIPTS_DIR) -> dict[str, Any]:
    """The recurring deterministic-COMMAND cron descriptor (CODE-ONLY template).

    Mirrors the standup/EOD/check-in cron shape: ``payload.kind == "command"`` (a
    deterministic ``sh -c`` argv -- the U8 parity form, NOT ``sh -lc``, and NOT an LLM
    agentTurn), running ``telegram-commands.sh initiation-tick`` in the skill scripts
    dir. The dispatcher OWNS the send (via the receipt-backed outbox), so there is NO
    ``delivery.announce`` block -- the cron does not re-announce stdout.

    This is a TEMPLATE the operator hands to ``openclaw cron add``; nothing here
    registers a live cron, edits ``openclaw.json``, or restarts the gateway (a deferred
    OPERATOR step, gated on the C5 holdout existing). The schedule CADENCE and kind are
    the operator's to confirm against the live gateway -- the load-bearing, security-
    relevant part is the deterministic command payload, not the interval.
    """
    return {
        "name": "initiation-nudge-tick",
        "schedule": {"kind": "interval", "minutes": cos_config.initiation_tick_minutes()},
        "payload": {
            "kind": "command",
            "argv": ["sh", "-c", f"cd {scripts_dir} && bash telegram-commands.sh initiation-tick"],
        },
    }


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint (wired through ``telegram-commands.sh initiation-tick``).

    No positional identity: the recurring tick derives today's slot itself. The
    dispatcher OWNS the send, so stdout carries only a small JSON status line.
    """
    parser = argparse.ArgumentParser(prog="initiation_dispatch.py", description=__doc__)
    parser.add_argument("--personal", action="store_true")
    parser.parse_args(argv)
    print(json.dumps(run_tick(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(error_envelope.run_main(
        COMPONENT, lambda: main(), trigger=f"cron:{COMPONENT}"))
