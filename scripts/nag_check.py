#!/usr/bin/env python3
"""U4 nag engine (cron): chase overdue tasks until acknowledged.

Runs every ~3h in work hours under ``agentId=niemand-work`` (spec §6.6). On each
fire it:

1. Reads the board (READ-ONLY -- it NEVER writes ``Weekly TODOs.md``; §3.4) and
   computes ``overdue_days`` per active task.
2. For each task whose overdue age crosses its section threshold (Q1=1, Q2=3,
   Q3=7 days -- Q1-aware off the SCALAR ``overdue_days`` because
   ``effective_priority`` short-circuits non-q2/q3 to ``escalated=False``):
   - opens a fresh nag loop if none is open,
   - re-fires an existing open loop (unless acked or snoozed),
   - closes a loop whose task is gone (verified_done) or no longer overdue.
3. Every push goes through the delivery seam (``prove_delivery_target`` ->
   ``gate`` -> ``assert_send_target``).  An unset env => ``nag_delivery_blocked``
   with ``reason: env_missing`` and the nag STAYS OPEN -- it is never silently
   cleared.

Hard invariants enforced here:

* NAG-CLOSES-ONLY-ON-ACK -- a loop closes only on verified_done / rescheduled (or
  the reactive /done /reschedule path); a crash, a missing env, or a snooze never
  closes it.
* DELIVERY-TARGET-PROOF -- no push without a proven, gated, asserted target.
* REVERSIBILITY -- the board is never written; only ``nag-state.json`` and the
  append-only ledger are touched.
* NO-RAW-ERROR-LEAK -- the whole run is wrapped; a failure logs to the error file
  and prints a safe envelope line, never a traceback, and exits 0.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from typing import Any, Callable

import cos_config
import error_envelope
import nag_delivery
import nag_state
from task_ledger import append_event, new_event
from task_records import active_records, load_records

# Sections we recognise for thresholds. A task whose section normalises to none of
# these (e.g. an objective header with no usable priority) is not nagged.
_THRESHOLD_FOR_SECTION: dict[str, Callable[[], int]] = {
    "q1": cos_config.nag_q1_threshold_days,
    "q2": cos_config.nag_q2_threshold_days,
    "q3": cos_config.nag_q3_threshold_days,
}

# Map a non-q section to a quadrant by the task's priority. Mirrors the fallback
# in effective_priority() so an objectives/today task lands in a consistent
# quadrant -- but WITHOUT any overdue escalation (that is the whole point of
# bypassing effective_priority here; see _threshold_section).
_PRIORITY_TO_SECTION: dict[str, str] = {
    "urgent": "q1",
    "high": "q1",
    "medium": "q2",
    "low": "q3",
}

SAFE_ENVELOPE = "NAG_CHECK_ERROR: internal error logged, no nag pushed this cycle"


def _today() -> datetime:
    return datetime.now(timezone.utc)


def _overdue_days(due: str | None, *, ref: datetime) -> int | None:
    """Scalar days overdue (positive == overdue), or None if no/garbage due date."""
    if not due:
        return None
    try:
        due_date = datetime.strptime(due, "%Y-%m-%d").date()
    except ValueError:
        return None
    return (ref.date() - due_date).days


def _threshold_section(record) -> str | None:
    """The ORIGINAL quadrant whose threshold governs this task, Q1-aware.

    We deliberately do NOT use ``effective_priority`` here. That function escalates
    an overdue q2 task's DISPLAY section (q2 overdue >3d -> q3, >7d -> q1); keying
    the nag threshold off that escalated section is a bug -- a q2 task 4 days
    overdue would display as q3 and then need the 7-day q3 threshold to fire, so it
    would never nag at its own 3-day q2 threshold (spec T1). The nag must key off
    the task's ORIGINAL quadrant and apply that quadrant's day threshold against
    the raw ``overdue_days`` scalar.

    A q1/q2/q3 section is used directly; a non-q section (objectives/today) is
    normalised to a quadrant by priority -- this is the Q1-awareness: an
    urgent/high task lands in q1 and nags at the 1-day threshold even though
    ``effective_priority`` would short-circuit it to ``escalated=False``.
    """
    section = record.section
    if section in _THRESHOLD_FOR_SECTION:
        return section
    return _PRIORITY_TO_SECTION.get((record.priority or "medium").lower())


def _crossed(record, *, ref: datetime) -> tuple[str, int] | None:
    """If this task is overdue past its section threshold, return (section, days)."""
    overdue = _overdue_days(record.due, ref=ref)
    if overdue is None or overdue <= 0:
        return None
    section = _threshold_section(record)
    if section is None:
        return None
    if overdue >= _THRESHOLD_FOR_SECTION[section]():
        return section, overdue
    return None


def _log(event_type: str, *, task_id: str | None = None, **metadata: Any) -> None:
    """Append a U4 ledger event (append-only, flocked by append_event)."""
    append_event(
        new_event(event_type, task_id=task_id, source="agent_autonomous",
                  actor="nag_check", metadata=metadata)
    )


def _nag_text(record, section: str, overdue: int) -> str:
    """Fallback wording for a nag push. Deterministic here; the cron prompt
    LLM-varies the phrasing per fire to reduce habituation (spec §2.1) -- this is
    the fallback / dry-run body and the audit record of what was pushed."""
    title = record.title or record.canonical_id or "(untitled)"
    plural = "s" if overdue != 1 else ""
    return (
        f"⚠️ Overdue task still open ({overdue} day{plural}) [{section.upper()}]\n\n"
        f'"{title}" [{record.canonical_id}] is {overdue} day(s) overdue.\n'
        f"Reply /done {record.canonical_id}, /reschedule {record.canonical_id} <date>, "
        f"or /snooze {record.canonical_id} 1d to clear this."
    )


def _push_nag(record, section: str, overdue: int, *, dry_run: bool,
              send: Callable[[dict[str, Any], str], Any] | None) -> dict[str, Any]:
    """Prove+gate+assert+send one nag push.  Returns the outcome dict.

    On a delivery block (env missing / work group / seam mismatch) NOTHING is
    sent and the caller logs ``nag_delivery_blocked`` + leaves the loop OPEN.

    DRY-RUN: only PROVES the target (Contract 2) -- it does NOT call ``gate()``,
    which would append an ``executed`` act to the append-only autonomy audit log
    and manufacture a phantom undoable nag that was never sent. A dry-run touches
    no append-only state, honouring its documented "preview, no write" contract.
    """
    text = _nag_text(record, section, overdue)
    if dry_run:
        proof = nag_delivery.resolve_target()
        if not proof["ok"]:
            return {"sent": False, "reason": proof["reason"], "stage": "prove"}
        return {"sent": False, "reason": "dry_run",
                "delivery_target": proof["delivery_target"], "text": text}

    task_id = record.canonical_id
    gated = nag_delivery.prove_and_gate("nag_sent", task_id=task_id,
                                        metadata={"section": section, "overdue_days": overdue})
    if not gated["ok"]:
        return {"sent": False, "reason": gated["reason"], "stage": gated.get("stage")}

    sent = nag_delivery.authorised_send(gated["act_id"], gated["delivery_target"],
                                        text, send=send)
    if not sent["ok"]:
        return {"sent": False, "reason": sent["reason"], "stage": "assert"}
    return {"sent": True, "act_id": gated["act_id"],
            "delivery_target": gated["delivery_target"], "text": text}


def run_nag_check(*, dry_run: bool = False,
                  send: Callable[[dict[str, Any], str], Any] | None = None) -> dict[str, int]:
    """One nag-check pass.  Returns counts ``{open, sent, closed, blocked}``.

    Board reads are READ-ONLY.  State writes go through ``nag_state.transition``
    (flock + atomic).  A delivery block leaves the loop open.

    ``send`` is the transport every nag is delivered through; it is REQUIRED for a
    real run (a missing transport would gate + log nag_sent while delivering
    nothing). A dry-run never sends, so it may be omitted there. ``main`` wires a
    collector transport whose payloads it emits for the cron ``delivery.to``
    announce; tests inject a recording stub.
    """
    if not dry_run and send is None:
        raise ValueError("run_nag_check requires a send transport for a real run.")
    ref = _today()
    _tasks_file, _content, records = load_records(personal=False)
    active = {r.canonical_id: r for r in active_records(records) if r.canonical_id}
    counts = {"open": 0, "sent": 0, "closed": 0, "blocked": 0}

    # 1. Resolve GENUINE open nag loops (those that have actually fired). No push
    #    on resolve. A body-double-only stub (nag_count==0) is NOT a nag loop and
    #    must not be touched here. Two outcomes:
    #    * task GONE from the active board -> terminally acked (verified_done): a
    #      task off the board is genuinely done/parked, a terminal close is correct.
    #    * task still on the board but no longer overdue past threshold -> RECYCLED
    #      (cleared), NOT terminally acked. Acking would permanently mute the task:
    #      if its due date later lapses past threshold again, an acked entry is
    #      skipped forever. Clearing lets that future lapse open a fresh loop -- the
    #      same recycle the reactive /reschedule + recurring-/done paths use.
    #    A dry-run PREVIEWS the resolve (counts it) but writes nothing -- it must
    #    not terminally ack / clear a real loop or append a ledger event.
    for task_id, entry in list(nag_state.read_state().items()):
        if not (nag_state.is_open(entry) and nag_state.is_genuine_nag(entry)):
            continue
        record = active.get(task_id)
        if record is None:
            if not dry_run:
                _close(task_id, nag_state.CLOSED_VERIFIED_DONE, entry)
            counts["closed"] += 1
        elif _crossed(record, ref=ref) is None:
            if not dry_run:
                _recycle(task_id, entry)
            counts["closed"] += 1

    # 2. Open / re-fire loops for tasks that crossed their threshold.
    for task_id, record in active.items():
        crossed = _crossed(record, ref=ref)
        if crossed is None:
            continue
        section, overdue = crossed
        _fire_one(task_id, record, section, overdue, counts,
                  ref=ref, dry_run=dry_run, send=send)

    return counts


def _fire_one(task_id, record, section, overdue, counts, *,
              ref: datetime, dry_run: bool,
              send: Callable[[dict[str, Any], str], Any] | None) -> None:
    """Fire (or skip) ONE task's nag, deciding ack/snooze UNDER the lock.

    The whole decision -- re-check ack/snooze, prove+gate, send, persist -- happens
    inside ONE locked transition so a reactive ``/done`` that acks the loop can
    never be raced: if the loop is acked/snoozed when the lock is held, NO message
    is sent, NO gate act is logged, and state is untouched (closing the 'said
    /done, got nagged again' trust window AND the phantom-gate-act audit hole).

    A dry-run takes no lock and never gates/sends -- it only previews.
    """
    if dry_run:
        outcome = _push_nag(record, section, overdue, dry_run=True, send=send)
        if outcome["sent"] is False and outcome["reason"] == "dry_run":
            counts["open"] += 1
        else:
            counts["blocked"] += 1
        return

    result = nag_state.transition(
        lambda live: _fire_locked(live, task_id, record, section, overdue,
                                  ref=ref, send=send))
    _apply_fire_result(result, task_id, section, counts)


def _fire_locked(live, task_id, record, section, overdue, *, ref, send):
    """The under-lock fire decision for ONE task (runs inside nag_state.transition).

    Returns a small result dict the caller turns into ledger events + counts. The
    gate+send happen here, AFTER the ack/snooze re-check, so a raced-out fire never
    sends a message or logs a gate act.
    """
    current = live.get(task_id)
    if (isinstance(current, dict) and current.get("ack")) or \
            nag_state.is_snoozed(current, now=ref):
        return {"status": "skipped"}  # raced with a close/snooze -- do nothing

    outcome = _push_nag(record, section, overdue, dry_run=False, send=send)
    if not outcome["sent"]:
        return {"status": "blocked", "reason": outcome["reason"]}

    # Open a fresh loop unless there is ALREADY a genuine open nag loop (one that
    # has fired). An absent entry, an acked one, or a body-double-only STUB
    # (nag_count==0) is not a genuine loop, so we open_loop -- which backfills the
    # threshold/delivery_target metadata and emits nag_opened -- rather than
    # silently promoting a stub to a firing loop with delivery_target:null.
    opened = not (nag_state.is_open(current) and nag_state.is_genuine_nag(current))
    if opened:
        nag_state.open_loop(
            live, task_id, task_title=record.title,
            threshold_crossed=overdue, threshold_type=section,
            delivery_target=outcome["delivery_target"],
        )
    entry = nag_state.record_sent(live, task_id)
    return {"status": "sent", "entry": entry, "opened": opened,
            "delivery_target": outcome["delivery_target"], "overdue": overdue}


def _apply_fire_result(result, task_id, section, counts) -> None:
    """Translate the under-lock fire result into ledger events + counters."""
    status = result["status"]
    if status == "skipped":
        return
    if status == "blocked":
        _log("nag_delivery_blocked", task_id=task_id, reason=result["reason"],
             section=section)
        counts["blocked"] += 1
        return
    entry, target = result["entry"], result["delivery_target"]
    if result["opened"]:
        _log("nag_opened", task_id=task_id, nag_loop_id=entry.get("nag_loop_id"),
             overdue_days=result["overdue"], threshold_type=section,
             delivery_target=target)
        counts["open"] += 1
    _log("nag_sent", task_id=task_id, nag_loop_id=entry.get("nag_loop_id"),
         nag_count=entry.get("nag_count"), delivery_target=target)
    counts["sent"] += 1


def _close(task_id: str, closed_by: str, entry: dict[str, Any]) -> None:
    """Terminally ack a loop in state + append the nag_acked ledger event.

    Used only when the task is GONE from the board (verified_done) -- a terminal
    close is correct because the task is genuinely done/parked.
    """
    nag_state.transition(lambda live: nag_state.close_loop(live, task_id, closed_by=closed_by))
    _log("nag_acked", task_id=task_id, nag_loop_id=entry.get("nag_loop_id"),
         closed_by=closed_by)


def _recycle(task_id: str, entry: dict[str, Any]) -> None:
    """Recycle (clear) a loop whose task is no longer overdue past threshold.

    The loop is NOT terminally acked: the entry is removed so a FUTURE lapse past
    threshold opens a fresh loop. The nag_acked event carries ``recycled: true`` so
    the audit trail distinguishes this reset from a terminal ack (matching the
    reactive recycle paths).
    """
    nag_state.transition(lambda live: nag_state.clear_loop(live, task_id))
    _log("nag_acked", task_id=task_id, nag_loop_id=entry.get("nag_loop_id"),
         closed_by=nag_state.CLOSED_RESCHEDULED, recycled=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nag_check.py", description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be pushed without writing state or sending")
    args = parser.parse_args(argv)
    try:
        # The cron job announces this script's stdout to its explicit delivery.to
        # (topic 2). So the transport is a collector: it accumulates each proven,
        # gated, asserted nag text, and main() PRINTS the collected payloads as the
        # deliverable output the gateway announce delivers. This is not a silent
        # no-op -- a nag is only counted as sent once its text is collected for
        # delivery, and the gate<->message seam still binds every payload to its
        # proven target before it is collected.
        payloads: list[str] = []
        counts = run_nag_check(dry_run=args.dry_run,
                               send=None if args.dry_run else
                               (lambda _target, text: payloads.append(text)))
        for text in payloads:
            print(text)
            print()  # blank line between nags in the announced output
        print(f"NAG_CHECK_DONE: {counts['open']} open loops, {counts['sent']} sent, "
              f"{counts['closed']} closed, {counts['blocked']} blocked")
        return 0
    except Exception as exc:  # noqa: BLE001 -- top-level NO-RAW-ERROR-LEAK boundary
        error_envelope.log_error(
            "nag_check", error_class=type(exc).__name__,
            message="nag-check run failed", raw=repr(exc),
            trigger="cron:nag_check",
        )
        print(SAFE_ENVELOPE)
        return 0  # exit 0 so cron does not treat as failure


if __name__ == "__main__":
    raise SystemExit(main())
