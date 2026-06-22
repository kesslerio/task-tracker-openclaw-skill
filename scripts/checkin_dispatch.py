#!/usr/bin/env python3
"""V1 deterministic check-in dispatcher: fire a focus/body-double check-in as TEXT.

Oracle O3 HIGH-2 (a live gap in deployed code): ``/start`` and ``/body-double``
scheduled their halfway/end check-ins as one-shot crons carrying an ``agentId`` +
``prompt``. Per OpenClaw's cron model that is a fresh MODEL-BACKED agent turn --
``announce`` is only fallback delivery AFTER the turn, and the agent can still use
``message`` / default tools. The user's free-text resumption CUE was spliced into
that prompt by ``_disposition_prompt``, so untrusted user text entered an LLM
instruction channel = a prompt-injection surface. The cron also reused a delivery
target proven at SESSION-CREATION time (never re-validated at fire time) and could
fire AFTER the user had ``/done``'d or cancelled the task.

The fix is to schedule a deterministic COMMAND cron that runs THIS dispatcher at
fire time (no LLM turn). The session identity arrives as ARGV -- never interpolated
into any prompt. At fire time the dispatcher:

1. Reloads nag-state and looks up the session via
   ``nag_state.active_body_double_session(entry, now=...)``. If the session is NOT
   active (ended / ``/done``'d / cancelled / elapsed-expired / not found) it sends
   NOTHING and exits clean -- a post-``/done`` check-in is a no-op.
2. RE-PROVES the delivery target NOW (the same ``nag_delivery.resolve_target`` path
   ``_open_focus_session`` uses) -- it never trusts a target baked in at create
   time (DELIVERY-TARGET-PROOF, Hard Gate #4). An unprovable target records a
   health/error and sends nothing -- never a send to an unproven target.
3. Renders the check-in / end-of-session disposition as INERT templated TEXT (the
   cue appears only as displayed text, never as an instruction to a model) and
   passes it through ``redaction.redact_message`` as the H10 length backstop.
4. Delivers via ``outbox.deliver_once`` -> ``openclaw_sender`` (receipt-backed,
   idempotent), keyed on ``(session_id, halfway|end)`` so a cron RETRY of the same
   phase can never double-send.

NO-RAW-ERROR-LEAK: ``main`` runs under ``error_envelope.run_main`` so any crash is
logged + health-recorded and exits 0 with a friendly line (never a traceback).
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from typing import Any, Callable

import cos_config
import error_envelope
import nag_delivery
import nag_state
import outbox
import redaction

COMPONENT = "checkin_dispatch"

# The two check-in phases. ``end`` is the final (session-end) nudge; ``halfway`` is
# the mid-block one. The phase is part of the idempotency key, so a cron retry of
# the SAME phase dedupes to one send.
PHASE_END = "end"
PHASE_HALFWAY = "halfway"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _log(event_type: str, *, task_id: str | None = None, **metadata: Any) -> None:
    from task_ledger import append_event, new_event  # lazy: keep import surface thin

    append_event(
        new_event(event_type, task_id=task_id, source="agent_autonomous",
                  actor=COMPONENT, metadata=metadata)
    )


def _render_checkin_text(task_id: str, elapsed_min: int, *, is_final: bool,
                         cue: str | None) -> str:
    """Render the check-in / end-of-session disposition as INERT user-facing TEXT.

    This is the SAME wording the old cron ``prompt`` carried, but rendered as a
    plain Telegram message the user reads and replies to -- never an instruction to
    a model. The resumption ``cue`` is displayed (quoted) as text only; it is never
    an LLM instruction channel, which is the whole point of the V1 fix.

    The end-of-session text mirrors ``nag_commands._disposition_text`` (the
    structured done/continue/blocked/redefine choice routed to the EXISTING
    commands); the halfway text is a short body-double nudge.
    """
    if is_final:
        lines = ["⏱️ Focus block done."]
        if cue:
            lines.append(f"Resume cue was: {cue!r}.")
        lines += [
            "How did it go? Pick one:",
            f"  done → /done {task_id}",
            f"  continue → /start {task_id} (another block)",
            f"  blocked → /reschedule {task_id} <date>",
            "  redefine → just reply with the new next action.",
        ]
        return "\n".join(lines)
    nudge = f"⏳ Halfway through your focus block ({elapsed_min} min in)."
    if cue:
        nudge += f"\nStill on: {cue!r}? If you've drifted, this is the nudge back."
    else:
        nudge += "\nStill on track? If you've drifted, this is the nudge back."
    return nudge


def _record_health(*, ok: bool, error_class: str | None = None) -> None:
    """Best-effort: record this dispatch's outcome to the health substrate.

    A failed target proof (the only soft-failure that sends nothing yet is NOT a
    no-op) is a health FAILURE; an active-session send and a benign skip are
    successes. Wrapped so a broken/absent ``cos_health`` never changes the dispatch.
    """
    try:
        import cos_health  # noqa: PLC0415 -- lazy + wrapped: health is best-effort

        if ok:
            cos_health.record_success(COMPONENT)
        else:
            cos_health.record_failure(COMPONENT, error_class=error_class or "checkin_failed",
                                      trigger=f"cron:{COMPONENT}")
    except Exception:  # noqa: BLE001 -- health recording is best-effort, never fatal
        pass


def run_dispatch(
    session_id: str,
    task_id: str,
    elapsed_min: int,
    *,
    is_final: bool,
    label: str = "body-double",
    sender: Callable[[dict[str, Any], str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Fire ONE check-in for ``session_id`` at cron-fire time. Deterministic, no LLM.

    Returns a small result dict (``{"sent": bool, "reason": str, ...}``) for the CLI
    + tests. ``sender`` is injectable (tests pass a fake returning a canned receipt);
    production defaults to ``outbox.openclaw_sender`` (the real Telegram send).

    Send-NOTHING outcomes (each exits clean, no message):

    * the session is not active (ended / ``/done``'d / cancelled / elapsed / not
      found) -> ``reason: session-inactive``;
    * the delivery target cannot be proven NOW -> ``reason: target-unproven``
      (records a health failure -- never a send to an unproven target).

    Otherwise it renders inert text and delivers it AT MOST ONCE per
    ``(session_id, phase)`` via the receipt-backed outbox.
    """
    now = _now()
    # 1. Reload state and find the LIVE session by id. A session ended by /done,
    #    /cancel-session, or elapsed past ends_at is NOT active -> send nothing. We
    #    scan all entries because the dispatcher knows the session_id, not which
    #    task_id key it lives under (and the board may have moved).
    session = _find_active_session(session_id, now=now)
    if session is None:
        # The user already closed/cancelled the block, or it elapsed. A check-in for
        # a dead session must say nothing -- the post-/done no-op the V1 fix exists
        # for. Benign, so a SUCCESS health stamp (the dispatcher ran correctly).
        _record_health(ok=True)
        return {"sent": False, "reason": "session-inactive", "session_id": session_id}

    # 2. RE-PROVE the target NOW -- never trust a target baked in at create time.
    proof = nag_delivery.resolve_target()
    if not proof["ok"]:
        _log("checkin_delivery_blocked", task_id=task_id, session_id=session_id,
             reason=proof.get("reason"), phase=_phase(is_final))
        _record_health(ok=False, error_class="target-unproven")
        return {"sent": False, "reason": "target-unproven",
                "session_id": session_id, "proof_reason": proof.get("reason")}
    delivery_target = proof["delivery_target"]

    # 3. Render INERT text -- the cue is displayed text only, never an LLM instruction.
    cue = session.get("cue") if isinstance(session, dict) else None
    text = redaction.redact_message(
        _render_checkin_text(task_id, elapsed_min, is_final=is_final, cue=cue))

    # 4. Deliver AT MOST ONCE per (session_id, phase). A cron retry of the same phase
    #    short-circuits to the recorded receipt without re-sending.
    idem_key = outbox.make_idem_key("checkin", session_id, _phase(is_final))
    receipt = outbox.deliver_once(delivery_target, text, idem_key,
                                  sender=sender or outbox.openclaw_sender)
    if not receipt.get("idempotent"):
        _log("checkin_sent", task_id=task_id, session_id=session_id,
             phase=_phase(is_final), label=label,
             message_id=receipt.get("message_id"), idem_key=idem_key,
             delivery_target=delivery_target)
    _record_health(ok=True)
    return {"sent": not receipt.get("idempotent"), "reason": "delivered",
            "idempotent": bool(receipt.get("idempotent")),
            "session_id": session_id, "message_id": receipt.get("message_id"),
            "idem_key": idem_key}


def _phase(is_final: bool) -> str:
    return PHASE_END if is_final else PHASE_HALFWAY


def _find_active_session(session_id: str, *, now: datetime) -> dict[str, Any] | None:
    """The LIVE session matching ``session_id`` across all nag-state entries, or None.

    Active means ``active_body_double_session`` (not ended, not elapsed at ``now``)
    returns a session whose id matches. A session that has been ``/done``'d,
    cancelled, or has elapsed is treated as gone -> None -> the dispatcher sends
    nothing.
    """
    for entry in nag_state.read_state().values():
        session = nag_state.active_body_double_session(entry, now=now)
        if session is not None and session.get("session_id") == session_id:
            return session
    return None


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint (wired through ``telegram-commands.sh checkin-dispatch``).

    The session identity arrives as ARGV -- NEVER interpolated into any prompt. On
    success the dispatcher OWNS the send, so stdout carries only a small JSON status
    line (the cron does not re-announce it). Wrapped by ``run_main`` so any crash is
    logged + health-recorded and exits 0 with a friendly line.
    """
    parser = argparse.ArgumentParser(prog="checkin_dispatch.py", description=__doc__)
    parser.add_argument("--personal", action="store_true")
    parser.add_argument("session_id")
    parser.add_argument("task_id")
    parser.add_argument("elapsed_min", type=int)
    parser.add_argument("is_final", help="true|false: the session-end check-in")
    parser.add_argument("label", nargs="?", default="body-double")
    args = parser.parse_args(argv)

    # ``--personal`` is accepted for ARGV parity with the focus-session handlers, but
    # the dispatcher reads only the (board-agnostic) shared nag-state, so it does not
    # need to branch on it for the lookup or the send.
    result = run_dispatch(
        args.session_id, args.task_id, args.elapsed_min,
        is_final=str(args.is_final).strip().lower() in ("true", "1", "yes"),
        label=args.label,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(error_envelope.run_main(
        COMPONENT, lambda: main(), trigger=f"cron:{COMPONENT}"))
