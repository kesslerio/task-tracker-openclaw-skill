#!/usr/bin/env python3
"""U4 nag engine (cron): chase overdue tasks until acknowledged.

Runs every ~3h in work hours under ``agentId=niemand-work`` (spec §6.6). On each
fire it:

1. Reads the board (READ-ONLY -- it NEVER writes ``Weekly TODOs.md``; §3.4) and
   computes ``overdue_days`` per active task.
2. For the WORST-overdue tasks (capped at ``cos_config.nag_display_limit()``, so the
   ADHD surface gets the top few, not an 85-line dump) whose overdue age crosses its
   section threshold (Q1=1, Q2=3, Q3=7 days -- Q1-aware off the SCALAR
   ``overdue_days`` because ``effective_priority`` short-circuits non-q2/q3 to
   ``escalated=False``):
   - opens a fresh nag loop if none is open,
   - re-fires an existing open loop (unless acked or snoozed),
   - closes a loop whose task is gone (verified_done) or no longer overdue.
   Tasks past the cap defer (the push shows a "+K more" pointer; ``/nag all`` lists
   them in full). The close/recycle resolve pass always runs over every loop.
3. Every push goes through the delivery seam (``prove_delivery_target`` ->
   ``gate`` -> ``assert_send_target``) and is then DELIVERED + RECEIPTED by
   ``outbox.deliver_once`` (idempotent, message-id captured). The script OWNS the
   send (H3): the proven target is the thing that actually sends, the gateway
   message-id is recorded on the ``nag_sent`` event, and a re-fire of the same loop
   on the same day is deduped by the outbox idem-key -- so stdout is no longer the
   delivery channel and the cron ``--announce`` of stdout cannot double-send. An
   unset env => ``nag_delivery_blocked`` with ``reason: env_missing`` and the nag
   STAYS OPEN; a transport FAILURE (the sender raises) likewise leaves the loop OPEN
   and records NO ``nag_sent`` -- it is never silently cleared or phantom-sent.

CADENCE COUPLING (HARD DEPENDENCY): the outbox idem-key period is the local date +
HOUR (``%Y-%m-%d-%H``), so the same-fire dedup window assumes >= 1h spacing between
scheduled fires. This holds for the 11/14/17 cadence (each lands in a distinct clock
hour). If the schedule is EVER tightened to sub-hourly (two fires in one hour), the
second would silently dedupe to the same key -- a DROPPED nag with NO log. A
sub-hourly schedule MUST coarsen or re-key the period (e.g. include the minute)
before tightening. The invariant is restated at the idem-key site in ``_fire_locked``;
keep both halves in sync.

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
from datetime import datetime
from typing import Any, Callable

import cos_config
import error_envelope
import nag_delivery
import nag_state
import outbox
import task_ledger
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
    # Local (Pacific) now, not UTC: the cron fires at 17:00/17:30 Pacific, when
    # the UTC day has already rolled over, so ``ref.date()`` in ``_overdue_days``
    # must read the local calendar day or a due-today task counts as 1d overdue.
    return cos_config.local_now()


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


# Worst-first tiebreak when two tasks are equally overdue: the more urgent
# quadrant leads, then the canonical id for a stable, deterministic order.
_SECTION_RANK: dict[str, int] = {"q1": 0, "q2": 1, "q3": 2}


def _sorted_crossed(active: dict[str, Any], *, ref: datetime
                    ) -> list[tuple[str, Any, str, int]]:
    """All threshold-crossed tasks as ``(task_id, record, section, overdue)``,
    worst-overdue first. The single source of crossing order for both the capped
    cron fire and the read-only ``/nag all`` list, so the two never disagree."""
    crossed = []
    for task_id, record in active.items():
        hit = _crossed(record, ref=ref)
        if hit is not None:
            section, overdue = hit
            crossed.append((task_id, record, section, overdue))
    # Every section here came from _crossed() returning a real q1/q2/q3, so the
    # rank lookup is total -- no fallback needed.
    crossed.sort(key=lambda t: (-t[3], _SECTION_RANK[t[2]], t[0]))
    return crossed


def _firable(entry: dict[str, Any] | None, *, ref: datetime) -> bool:
    """Would this crossed task actually fire this cycle, or is it paused/closed?

    False for an acked (terminal) or snoozed (paused) loop. Such tasks must NOT
    consume a cap slot: if a snoozed leader held its slot, snoozing the worst-N
    would starve every task below them -- the surface would go silent while real
    work aged (the snooze-starvation hole). Mirrors the under-lock skip in
    ``_fire_locked``; the lock stays the authority for the actual fire decision,
    this is only slot allocation against a best-effort snapshot.
    """
    if isinstance(entry, dict) and entry.get("ack"):
        return False
    return not nag_state.is_snoozed(entry, now=ref)


def _log(event_type: str, *, task_id: str | None = None, **metadata: Any) -> None:
    """Append a U4 ledger event (append-only, flocked by append_event)."""
    append_event(
        new_event(event_type, task_id=task_id, source="agent_autonomous",
                  actor="nag_check", metadata=metadata)
    )


def _nag_sent_already_logged(idem_key: str) -> bool:
    """Has a ``nag_sent`` carrying ``idem_key`` already been appended to the ledger?

    The split-brain REPAIR (Fix B) catches the ledger up to a delivered fact -- but the
    only state it needs to repair is whatever is MISSING. The ledger (events.jsonl) is
    append-only and written by a different path than nag-state.json, so the two can
    drift independently: a fire can append nag_opened+nag_sent and then lose
    nag-state.json later, leaving the ledger already-caught-up. Without this guard the
    repair would emit a SECOND nag_sent for the same delivered idem_key, double-counting
    one delivery (nag_sent counts DELIVERED nags). Keyed on the idem_key the genuine
    delivery stamped, this makes the repair's ledger emit idempotent against a surviving
    ledger. A read failure degrades to False (re-emit) -- a duplicate audit line is
    recoverable; a silently-missing one is the gap the repair exists to close.
    """
    try:
        events = task_ledger.read_events()
    except Exception:  # noqa: BLE001 -- a ledger read must never fail the repair
        return False
    return any(e.get("event_type") == "nag_sent"
               and (e.get("metadata") or {}).get("idem_key") == idem_key
               for e in events)


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


def _authorise_nag(record, section: str, overdue: int, *, dry_run: bool
                   ) -> dict[str, Any]:
    """Prove+gate+assert ONE nag, returning the authorised target + text.

    This is the proof chain that MUST pass before any byte leaves: an env-missing /
    work-group / seam-mismatch result returns ``{"ok": False, ...}`` and the caller
    logs ``nag_delivery_blocked`` + leaves the loop OPEN. The actual delivery is NOT
    done here -- it is the caller's ``outbox.deliver_once`` (receipt-captured +
    idempotent), invoked only on ``ok``. Splitting authorise from send is the H3
    change: the proven target is the thing that actually sends.

    DRY-RUN: only PROVES the target (Contract 2) -- it does NOT call ``gate()``,
    which would append an ``executed`` act to the append-only autonomy audit log
    and manufacture a phantom undoable nag that was never sent. A dry-run touches
    no append-only state, honouring its documented "preview, no write" contract.
    """
    text = _nag_text(record, section, overdue)
    if dry_run:
        proof = nag_delivery.resolve_target()
        if not proof["ok"]:
            return {"ok": False, "reason": proof["reason"], "stage": "prove"}
        return {"ok": True, "dry_run": True,
                "delivery_target": proof["delivery_target"], "text": text}

    task_id = record.canonical_id
    gated = nag_delivery.prove_and_gate("nag_sent", task_id=task_id,
                                        metadata={"section": section, "overdue_days": overdue})
    if not gated["ok"]:
        return {"ok": False, "reason": gated["reason"], "stage": gated.get("stage")}

    authorised = nag_delivery.authorise_target(gated["act_id"], gated["delivery_target"])
    if not authorised["ok"]:
        # The gate FIRED (it logged an ``executed`` act under this act_id) but the
        # gate<->message seam asserted out, so nothing was delivered. Carry the act_id
        # (Fix C) so the caller can emit a nag_gate_act_undelivered reconciliation
        # event -- a gated-but-asserted-out nag must not leave an executed act with NO
        # reconciliation. The PROVE-stage blocks above carry no act_id because gate()
        # never fired there (target not proven), so they need no reconciliation.
        return {"ok": False, "reason": authorised["reason"], "stage": "assert",
                "act_id": gated["act_id"]}
    return {"ok": True, "act_id": gated["act_id"],
            "delivery_target": gated["delivery_target"], "text": text}


def run_nag_check(*, dry_run: bool = False, limit: int | None = None,
                  sender: Callable[[dict[str, Any], str], dict[str, Any]] | None = None
                  ) -> dict[str, int]:
    """One nag-check pass.  Returns counts ``{open, sent, closed, blocked, deferred}``.

    Board reads are READ-ONLY.  State writes go through ``nag_state.transition``
    (flock + atomic).  A delivery block leaves the loop open.

    ``limit`` caps how many of the worst-overdue tasks fire this cycle (``None`` ==
    no cap). The cap is a FIRING bound, not a mute: deferred tasks open no loop and
    push nothing this cycle, but keep their place and surface as the leaders are
    cleared -- ``counts['deferred']`` is how many were held back, which ``main``
    turns into the "+K more" pointer and ``/nag all`` shows in full. The resolve
    pass (close/recycle) always runs over every loop, uncapped.

    ``sender`` is the receipt-returning transport every nag is delivered through via
    ``outbox.deliver_once`` -- ``sender(delivery_target, text) -> {"message_id": str}``
    (or raises on a transport failure). It is REQUIRED for a real run (a missing
    sender would gate + log nag_sent while delivering nothing). A dry-run never
    sends, so it may be omitted there. ``main`` wires ``outbox.openclaw_sender`` (the
    REAL Telegram send); tests inject a fake returning canned message ids.
    """
    if not dry_run and sender is None:
        raise ValueError("run_nag_check requires a sender transport for a real run.")
    ref = _today()
    _tasks_file, _content, records = load_records(personal=False)
    active = {r.canonical_id: r for r in active_records(records) if r.canonical_id}
    counts = {"open": 0, "sent": 0, "closed": 0, "blocked": 0, "deferred": 0}

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

    # 2. Open / re-fire loops for the worst-overdue tasks that are actually FIRABLE
    #    this cycle (not acked, not snoozed), capped at ``limit``. Filtering before
    #    the slice is what makes the cap a top-N-FIRABLE bound rather than
    #    top-N-CROSSED: a snoozed/acked leader yields its slot so the next real task
    #    surfaces instead of the surface going silent (the snooze-starvation hole).
    #    Tasks past the cap are deferred (counted for the "+K more" pointer); paused/
    #    closed ones are simply not slotted (the user already snoozed/acked them).
    #    ``max(0, ...)`` keeps a stray negative ``limit`` from slicing off the tail.
    snapshot = nag_state.read_state()
    firable = [c for c in _sorted_crossed(active, ref=ref)
               if _firable(snapshot.get(c[0]), ref=ref)]
    to_fire = firable if limit is None else firable[:max(0, limit)]
    counts["deferred"] = len(firable) - len(to_fire)
    # Fix D: the "+K more" pointer rides the LAST fired nag's gated, receipted send
    # instead of a separate ungated push. It is computed here (deferred count is known)
    # and appended ONLY to the final task's text. If nothing fires, no pointer. A
    # dry-run never delivers, so it appends no pointer (the preview is unchanged).
    pointer = (_more_pointer_line(counts["deferred"])
               if counts["deferred"] > 0 and to_fire and not dry_run else None)
    for index, (task_id, record, section, overdue) in enumerate(to_fire):
        suffix = pointer if index == len(to_fire) - 1 else None
        _fire_one(task_id, record, section, overdue, counts,
                  ref=ref, dry_run=dry_run, sender=sender, more_pointer=suffix)

    return counts


def _fire_one(task_id, record, section, overdue, counts, *,
              ref: datetime, dry_run: bool,
              sender: Callable[[dict[str, Any], str], dict[str, Any]] | None,
              more_pointer: str | None = None) -> None:
    """Fire (or skip) ONE task's nag, deciding ack/snooze UNDER the lock.

    The whole decision -- re-check ack/snooze, prove+gate+assert, the
    receipt-captured send, persist -- happens inside ONE locked transition so a
    reactive ``/done`` that acks the loop can never be raced: if the loop is
    acked/snoozed when the lock is held, NO message is sent, NO gate act is logged,
    and state is untouched (closing the 'said /done, got nagged again' trust window
    AND the phantom-gate-act audit hole).

    A dry-run takes no lock and never gates/sends -- it only previews. To stay a
    FAITHFUL preview it applies the same ack/snooze skip the real fire does, so a
    snoozed or acked-on-board loop is not over-counted as "would push".
    """
    if dry_run:
        current = nag_state.read_state().get(task_id)
        if (isinstance(current, dict) and current.get("ack")) or \
                nag_state.is_snoozed(current, now=ref):
            return  # the real run would skip this -- preview must too
        outcome = _authorise_nag(record, section, overdue, dry_run=True)
        if outcome["ok"]:
            counts["open"] += 1
        else:
            counts["blocked"] += 1
        return

    result = nag_state.transition(
        lambda live: _fire_locked(live, task_id, record, section, overdue,
                                  ref=ref, sender=sender, more_pointer=more_pointer))
    _apply_fire_result(result, task_id, section, counts)


def _fire_locked(live, task_id, record, section, overdue, *, ref, sender,
                 more_pointer=None):
    """The under-lock fire decision for ONE task (runs inside nag_state.transition).

    Returns a small result dict the caller turns into ledger events + counts. The
    whole chain -- ack/snooze re-check, outbox peek, prove+gate+assert, the
    receipt-captured send, and the state persist -- happens UNDER the lock, so a
    reactive ``/done`` that acks the loop before this takes the lock means NO message
    is sent and NO gate act is logged. The send goes through ``outbox.deliver_once``
    (idempotent + receipt), keyed on (task_id, period), so a re-fire of the SAME loop
    in the SAME cycle never double-sends even across retries.

    Ordering (audit-honest): peek -> [skip | gate->assert->deliver] -> reconcile.
    The idem-key is computed FIRST and the outbox PEEKED before any gate() call. A
    key already recorded this cycle short-circuits to ``already_delivered`` WITHOUT
    proving/gating/asserting, so a same-cycle idempotent retry logs NO phantom
    ``executed`` autonomy-gate act -- mirroring how the acked/snoozed SKIP path above
    also avoids gate(). Only an unrecorded key proceeds to ``_authorise_nag``
    (prove->gate->assert, which logs the executed act) and then ``deliver_once``,
    whose own under-flock recorded-key check stays the AUTHORITATIVE dedup for the
    rare TOCTOU where another fire records between this peek and that flock.

    State is mutated ONLY AFTER a proven, delivered send. A delivery failure (the
    sender raises) returns ``delivery_failed`` having mutated ``live`` not at all, so
    ``nag_state.transition`` persists nothing -- the loop stays exactly as it was
    (OPEN), never a phantom "sent"; the result carries the gate ``act_id`` so the
    caller can reconcile the now-undelivered executed act in the ledger. A post-gate
    ``idempotent`` short-circuit (the rare TOCTOU race) returns ``already_delivered``
    likewise carrying the ``act_id`` to reconcile, having mutated nothing: no message
    was delivered, so nag_count is not bumped and no second receipt is written.
    nag_count counts DELIVERED nags.
    """
    current = live.get(task_id)
    if (isinstance(current, dict) and current.get("ack")) or \
            nag_state.is_snoozed(current, now=ref):
        return {"status": "skipped"}  # raced with a close/snooze -- do nothing

    # CADENCE COUPLING (P3): the period is local date + HOUR, so the dedup window
    # assumes >= 1h spacing between scheduled fires (the 11/14/17 cadence -- each
    # lands in a distinct clock hour). If the schedule is ever tightened to
    # sub-hourly (two fires in one hour), the second would silently dedupe here to
    # the same key -- a DROPPED nag with no log. A sub-hourly schedule MUST coarsen
    # or re-key this period (e.g. include the minute) before tightening. See the
    # module docstring; do NOT change the granularity without changing both halves.
    idem_key = outbox.make_idem_key("nag", task_id, ref.strftime("%Y-%m-%d-%H"))

    # PEEK BEFORE GATE: if this cycle's delivery is already recorded, this is a
    # same-cycle idempotent retry. Short-circuit BEFORE _authorise_nag so gate() is
    # never called and NO phantom ``executed`` act is logged for an undelivered fire.
    # deliver_once re-checks under its own flock, so this peek is an optimisation, not
    # the authority -- the rare post-peek TOCTOU is still caught + reconciled below.
    receipt = outbox.get_receipt(idem_key)
    if receipt is not None:
        # The message WAS delivered this cycle (a committed outbox receipt proves it).
        # Two sub-cases:
        #   * the loop is ALREADY a genuine open nag loop for this task -> the normal
        #     same-cycle retry: state+ledger already reflect the delivery, so no-op.
        #   * the loop is NOT a genuine open loop (e.g. the FIRST fire wrote the outbox
        #     receipt but the process died before nag_state persisted the loop, then
        #     nag-state.json was lost) -> REPAIR split-brain: open the loop from the
        #     receipt's stored target and emit the missing nag_sent carrying the stored
        #     message_id + idem_key, so state+ledger catch up to the delivered fact.
        # The repair is idempotent: once it opens the loop, a later same-cycle run sees
        # a genuine open loop and no-ops -- no double-open, no double nag_sent. It does
        # NOT gate() (no new send happened), so it carries no act_id and emits no
        # autonomy act -- it only reconciles state/ledger to a delivery that already
        # occurred.
        if nag_state.is_open(current) and nag_state.is_genuine_nag(current):
            return {"status": "already_delivered"}
        stored_target = receipt.get("target")
        opened = nag_state.open_loop(
            live, task_id, task_title=record.title,
            threshold_crossed=overdue, threshold_type=section,
            delivery_target=stored_target, nag_loop_id=nag_state.new_nag_loop_id(),
        )
        entry = nag_state.record_sent(live, task_id)
        return {"status": "repaired", "entry": entry,
                "delivery_target": stored_target, "overdue": overdue,
                "message_id": receipt.get("message_id"), "idem_key": idem_key,
                "nag_loop_id": opened.get("nag_loop_id")}

    authorised = _authorise_nag(record, section, overdue, dry_run=False)
    if not authorised["ok"]:
        # Carry the act_id + stage through (Fix C): an ASSERT-stage block means gate()
        # already fired (executed act logged) but the seam asserted out, so the caller
        # must reconcile that executed-but-undelivered act. A PROVE-stage block carries
        # no act_id (gate never fired) and needs no reconciliation.
        return {"status": "blocked", "reason": authorised["reason"],
                "stage": authorised.get("stage"), "act_id": authorised.get("act_id")}
    # _authorise_nag's gate() has now logged an ``executed`` act under this act_id. If
    # delivery does NOT happen below, the caller reconciles that act in the ledger.
    act_id = authorised["act_id"]

    # Open a fresh loop unless there is ALREADY a genuine open nag loop (one that
    # has fired). An absent entry, an acked one, or a body-double-only STUB
    # (nag_count==0) is not a genuine loop, so we open_loop -- which backfills the
    # threshold/delivery_target metadata and emits nag_opened -- rather than
    # silently promoting a stub to a firing loop with delivery_target:null.
    opened = not (nag_state.is_open(current) and nag_state.is_genuine_nag(current))
    # The loop id is for the loop STATE + ledger only -- an existing genuine loop
    # reuses its id (a re-fire is the SAME logical nag), a fresh loop mints one that
    # open_loop will then persist. It is DELIBERATELY NOT part of the idem-key: a
    # fresh loop mints a RANDOM id BEFORE the loop is persisted, so keying on it means
    # a process death between the outbox write and the state persist mints a new id
    # next run -> a new key -> a missed dedup -> a double-send. The idem-key keys only
    # on DURABLE identity instead (task_id + period, computed above).
    nag_loop_id = (current.get("nag_loop_id") if not opened and isinstance(current, dict)
                   else nag_state.new_nag_loop_id())

    target = authorised["delivery_target"]
    # Fix D: fold the "+K more" pointer into THIS nag's text (only the last fired nag
    # carries a non-None suffix) so it rides the one gated, receipted, idempotent send
    # -- no separate ungated pointer push. Appended here so the pointer is part of the
    # exact bytes deliver_once sends and records under the idem-key.
    text = authorised["text"] + (f"\n\n{more_pointer}" if more_pointer else "")
    try:
        receipt = outbox.deliver_once(target, text, idem_key, sender=sender)
    except Exception as exc:  # noqa: BLE001 -- a send failure is a per-task block,
        # not a run crash: leave the loop OPEN (state untouched), log it, move on.
        # The gate already logged an executed act; carry act_id so the caller can
        # emit nag_gate_act_undelivered and keep the audit trail reconcilable.
        return {"status": "delivery_failed", "reason": type(exc).__name__,
                "message": str(exc), "act_id": act_id}

    # A post-gate idempotent short-circuit (the rare TOCTOU: another fire recorded
    # the key between our peek and deliver_once's flock) means deliver_once did NOT
    # call the sender: the message was already delivered THIS cycle, so this duplicate
    # fire must not inflate nag_count or write a phantom second receipt. nag_count
    # counts DELIVERED nags; this fire is a no-op -- no open_loop, no record_sent, no
    # nag_sent. State is left untouched (transition persists nothing new). But gate()
    # already logged an executed act, so carry act_id so the caller reconciles it.
    if receipt.get("idempotent"):
        return {"status": "already_delivered", "act_id": act_id,
                "reason": "idempotent_after_gate"}

    if opened:
        nag_state.open_loop(
            live, task_id, task_title=record.title,
            threshold_crossed=overdue, threshold_type=section,
            delivery_target=target, nag_loop_id=nag_loop_id,
        )
    entry = nag_state.record_sent(live, task_id)
    return {"status": "sent", "entry": entry, "opened": opened,
            "delivery_target": target, "overdue": overdue,
            "message_id": receipt.get("message_id"), "idem_key": idem_key}


def _apply_fire_result(result, task_id, section, counts) -> None:
    """Translate the under-lock fire result into ledger events + counters."""
    status = result["status"]
    if status == "skipped":
        return
    if status == "already_delivered":
        # A same-cycle retry: deliver_once short-circuited on the recorded receipt
        # WITHOUT calling the sender, so no message went out this fire. The genuine
        # delivery already logged its one nag_sent; logging again would write a
        # phantom second receipt for a single delivery and inflate the delivered
        # count. So the nag_sent ledger is untouched -- only an observability counter
        # ticks. EXCEPT in the rare post-gate TOCTOU case (an act_id is present): the
        # peek missed the dup, so _authorise_nag's gate() DID log an executed act for
        # a fire that delivered nothing. Reconcile that act so the audit trail stays
        # honest -- every gated-but-undelivered fire is reconcilable from the ledger.
        counts["idempotent"] = counts.get("idempotent", 0) + 1
        if result.get("act_id"):
            _log("nag_gate_act_undelivered", task_id=task_id,
                 act_id=result["act_id"], reason=result.get("reason", "idempotent_after_gate"))
        return
    if status == "blocked":
        _log("nag_delivery_blocked", task_id=task_id, reason=result["reason"],
             section=section)
        # Fix C: an ASSERT-stage block carries the gate act_id -- the gate FIRED
        # (executed act logged) but the seam asserted out, so the gated-but-undelivered
        # act needs a reconciliation event, exactly like the delivery_failed path. A
        # PROVE-stage block (env-missing / work-group) carries NO act_id (gate never
        # fired) and emits only the nag_delivery_blocked above -- nothing to reconcile.
        if result.get("act_id"):
            _log("nag_gate_act_undelivered", task_id=task_id, act_id=result["act_id"],
                 reason=result["reason"], stage=result.get("stage", "assert"))
        counts["blocked"] += 1
        return
    if status == "delivery_failed":
        # The proof + seam passed but the transport itself failed (sender raised).
        # State was NOT mutated, so the loop stays OPEN and no nag_sent is recorded
        # -- never a phantom "sent". Logged as a delivery block so the operator sees
        # the failure and the next cycle retries.
        _log("nag_delivery_blocked", task_id=task_id, reason=result["reason"],
             section=section, stage="send", message=result.get("message"))
        # gate() already logged an executed act for this fire, but nothing was
        # delivered. Reconcile that act in the append-only ledger (we do NOT touch
        # autonomy_gate's authoritative executed record -- the FIRST record stays the
        # truth) so /undo + audit counting can tie this executed act to its
        # non-delivery and not overstate what was pushed.
        if result.get("act_id"):
            _log("nag_gate_act_undelivered", task_id=task_id, act_id=result["act_id"],
                 reason=result["reason"], stage="send")
        counts["blocked"] += 1
        return
    if status == "repaired":
        # Split-brain repair (Fix B): a prior fire DELIVERED (committed outbox receipt)
        # but crashed before persisting the loop. This fire found the receipt at the peek
        # and reopened the loop -- WITHOUT calling the sender (no double-send) and WITHOUT
        # gate() (no new autonomy act). Catch the LEDGER up to the delivered fact too,
        # but only if it is actually missing: events.jsonl is append-only and drifts
        # independently of nag-state.json, so the genuine fire may have already logged
        # nag_opened+nag_sent for this idem_key before state was lost. Emitting again
        # would double-count one delivery (nag_sent counts DELIVERED nags), so the ledger
        # emit is GUARDED on the idem_key already being absent. The state repair (loop
        # reopened) always stands; only the audit emit is conditional. Counts reflect the
        # delivered nag once regardless, since the cron run did process one delivery.
        entry, target = result["entry"], result["delivery_target"]
        if not _nag_sent_already_logged(result["idem_key"]):
            _log("nag_opened", task_id=task_id, nag_loop_id=entry.get("nag_loop_id"),
                 overdue_days=result["overdue"], threshold_type=section,
                 delivery_target=target, repaired=True)
            _log("nag_sent", task_id=task_id, nag_loop_id=entry.get("nag_loop_id"),
                 nag_count=entry.get("nag_count"), delivery_target=target,
                 message_id=result.get("message_id"), idem_key=result.get("idem_key"),
                 repaired=True)
        counts["open"] += 1
        counts["sent"] += 1
        return
    entry, target = result["entry"], result["delivery_target"]
    if result["opened"]:
        _log("nag_opened", task_id=task_id, nag_loop_id=entry.get("nag_loop_id"),
             overdue_days=result["overdue"], threshold_type=section,
             delivery_target=target)
        counts["open"] += 1
    # The nag_sent event now carries the gateway message-id RECEIPT and the
    # idem-key: the audit record proves the message was delivered (not merely
    # intended) and to which destination, and ties it to the idempotency key that
    # stops a duplicate on a re-fire.
    _log("nag_sent", task_id=task_id, nag_loop_id=entry.get("nag_loop_id"),
         nag_count=entry.get("nag_count"), delivery_target=target,
         message_id=result.get("message_id"), idem_key=result.get("idem_key"))
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


def _print_full_list() -> None:
    """``/nag all``: print EVERY overdue nag, worst first, READ-ONLY.

    The escape hatch the capped cron push points at. It proves no target, opens no
    loop, gates nothing, and writes no state -- it only echoes the same ``_nag_text``
    bodies the cron would fire, in the same worst-first order, so the reactive reply
    in-thread is a faithful full view of what the cap held back.
    """
    _tasks_file, _content, records = load_records(personal=False)
    active = {r.canonical_id: r for r in active_records(records) if r.canonical_id}
    crossed = _sorted_crossed(active, ref=_today())
    if not crossed:
        print("✅ No overdue tasks past their nag threshold. All caught up.")
        return
    for _task_id, record, section, overdue in crossed:
        print(_nag_text(record, section, overdue))
        print()
    plural = "s" if len(crossed) != 1 else ""
    print(f"{len(crossed)} overdue task{plural} total.")


def _more_pointer_line(deferred: int) -> str:
    """The "+K more … /nag all" pointer line (wording frozen).

    Fix D folds this pointer INTO the LAST fired nag's gated, receipted, idempotent
    send rather than pushing it as a separate ungated message: it is appended to that
    nag's text so it rides the one proven, dedup-keyed delivery. The wording is
    unchanged from the prior standalone pointer.
    """
    plural = "s" if deferred != 1 else ""
    return f"+{deferred} more overdue task{plural} — reply /nag all to see them."


def _record_nag_health(*, blocked: int = 0, crashed: str | None = None) -> None:
    """Best-effort: record this cron fire's outcome to the machine-visible health map.

    A HARD crash (``crashed`` = the exception class) or a swallowed delivery failure
    (``blocked > 0``) is a health FAILURE; an otherwise-clean real run is a success.
    Recorded DIRECTLY (not via the exit code) because the cron's shell wrapper would turn
    a nonzero exit into a user-facing "unavailable" announce -- and because nag_check
    catches its own crash and returns 0, so the shell's log_subprocess_error never fires
    either; without recording here a crashing nag_check would false-green until STALE.
    Wrapped so a broken/absent ``cos_health`` can never change the nag run.
    """
    try:
        import cos_health  # noqa: PLC0415 -- lazy + wrapped: health is best-effort
        if crashed is not None:
            cos_health.record_failure("nag_check", error_class=crashed, trigger="cron:nag_check")
        elif blocked > 0:
            cos_health.record_failure("nag_check", error_class="nag_delivery_blocked",
                                      trigger="cron:nag_check")
        else:
            cos_health.record_success("nag_check")
    except Exception:  # noqa: BLE001 -- health recording is best-effort, never fatal
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nag_check.py", description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be pushed without writing state or sending")
    parser.add_argument("--all", dest="all_nags", action="store_true",
                        help="fire every overdue nag this cycle (no top-N display cap)")
    parser.add_argument("--list", dest="list_only", action="store_true",
                        help="read-only: print ALL overdue nags (the /nag all reply); no fire, no state")
    args = parser.parse_args(argv)
    try:
        if args.list_only:
            _print_full_list()
            return 0
        # H3: the SCRIPT owns the send. Each proven, gated, asserted nag is delivered
        # by ``outbox.openclaw_sender`` (the REAL Telegram send), receipt-captured and
        # idempotent. Because the send happens here -- not via the cron announcing
        # stdout -- STDOUT stays EMPTY for delivered nags so the cron's blind
        # ``--announce`` of stdout is a no-op and CANNOT double-send. A dry-run never
        # sends, so its sender is None and nothing leaves; the operator preview is the
        # footer on stdout. The operational footer always rides STDERR (captured by
        # the run_with_envelope boundary, never delivered) so an idle cycle announces
        # nothing and the ADHD-focused surface is not spammed (spec §2.1 habituation).
        limit = None if args.all_nags else cos_config.nag_display_limit()
        sender = None if args.dry_run else outbox.openclaw_sender
        counts = run_nag_check(dry_run=args.dry_run, limit=limit, sender=sender)
        # Fix D: the "+K more" pointer is now FOLDED into the last fired nag's gated,
        # receipted send inside run_nag_check (it rides one proven, idempotent delivery
        # instead of a separate ungated push). main() no longer sends it -- when nags
        # fired and the cap held others back, the pointer already went out on the last
        # nag; an idle cycle fires nothing and carries no pointer.
        footer = (f"NAG_CHECK_DONE: {counts['open']} open loops, {counts['sent']} sent, "
                  f"{counts['closed']} closed, {counts['blocked']} blocked, "
                  f"{counts['deferred']} deferred")
        print(footer, file=sys.stdout if args.dry_run else sys.stderr)
        # R1: a SWALLOWED per-task delivery failure (run_nag_check absorbs every transport
        # failure into ``counts['blocked']``, leaving the loops OPEN) must be machine-
        # visible WITHOUT a user-facing regression. The cron runs this via the shell
        # ``run_with_envelope``, which turns a NONZERO exit into a "nag_check is
        # unavailable" notice it blind-announces -- so a partial-delivery cycle (most nags
        # sent) must NOT exit nonzero. Instead record the outcome to the health substrate
        # DIRECTLY and return 0: a blocked run is a health FAILURE (-> DEGRADED), an
        # otherwise-clean real run is a success, and the cron announces only the real nags.
        # (The reactive ``/nag`` path returns early at ``--list`` above, so only the cron
        # fire records ``nag_check`` health -- no reactive-run conflation.) A dry-run never
        # delivers, so it records nothing.
        if not args.dry_run:
            _record_nag_health(blocked=counts["blocked"])
        return 0
    except Exception as exc:  # noqa: BLE001 -- top-level NO-RAW-ERROR-LEAK boundary
        error_envelope.log_error(
            "nag_check", error_class=type(exc).__name__,
            message="nag-check run failed", raw=repr(exc),
            trigger="cron:nag_check",
        )
        # R1: a HARD crash is the worst silently-broken-cron case -- record a health
        # FAILURE (best-effort) so it shows DEGRADED, not false-green-until-STALE.
        _record_nag_health(crashed=type(exc).__name__)
        print(SAFE_ENVELOPE)
        return 0  # exit 0 so cron does not treat as failure


if __name__ == "__main__":
    raise SystemExit(main())
