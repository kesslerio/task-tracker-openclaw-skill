#!/usr/bin/env python3
"""U4 EOD ritual -- the detect + button-confirm slice (first of the EOD units).

This is the thin orchestration layer for the evening ritual's FIRST step: detect
what got done today and render each detected completion with a tappable Confirm
button. It owns NO new detection logic and NO new board semantics -- it reuses the
existing ``done24h`` harvest (``harvest_ledger.run_harvest`` -- merged PRs + sent
mail matched to the active board + manual ``/win`` captures) and the existing
``tt:appr:<task_id>`` confirm-gate (U1's ``telegram_buttons`` builder; the U2
plugin + dispatcher already route ``appr`` -> ``harvest_ledger.approve``, which is
topic-guarded + reversible + ledger-writing).

Invariant (mirrors ``/approve``): **NO board change without a tap.** U4 only
DETECTS + RENDERS the confirm step; it marks NOTHING done. The harvest runs in
``dry_run`` mode so it neither pushes a digest nor consumes/writes any state --
detection is read-only. The actual confirmation happens later, when the user taps
a Confirm button -> the U2 dispatcher invokes the existing ``harvest_ledger.approve``
through the topic-guarded, reversible path. ``eod_ritual`` never auto-approves.

Scope boundary: this module spans detect + confirm (U4), the forced disposition (U5),
setting tomorrow's #1 (U6), AND -- as of U7 -- the live DELIVERY of the assembled ritual
through the receipt-backed seam, the human-readable Obsidian ``## EOD Summary``, the
``eod_review`` health record, and the deterministic-cron descriptor. It does NOT build
the morning-standup reader (U8). ``eod_review.py`` already parses the daily note for
done/not-done; this unit reuses ``run_harvest`` rather than re-implementing evidence
detection, and leaves daily-note parsing to ``eod_review`` where the later EOD slices
need it.

U7 -- delivery + summary + health + cron (KTD-1, KTD-5): the assembled EOD (the detect,
disposition, and tomorrow's-#1 steps' text + buttons) is delivered through the SAME
prove -> gate -> assert -> ``outbox.deliver_once`` seam the weekly digest uses
(``ledger_delivery.deliver_auto_digest``), keyed on the local DATE so a same-day re-fire
never double-sends. The board mutations U4/U5/U6 commit happen ONLY on the user's taps and
are NOT coupled to this send: an env-unset / gate-blocked delivery returns a clean reason
with NO partial send, and the confirmed taps already stand on the board regardless. On a
delivered EOD the human-readable ``## EOD Summary`` (done today / still-open / tomorrow's
#1) is upserted to the Obsidian daily note (``eod_summary``; IDEMPOTENT -- a re-run
REPLACES the section, never appends). ``run_main`` records the REAL ``eod_review`` health
success/failure (the key is already in ``cos_manifest.EXPECTED_RITUALS``). The
deterministic cron is a CODE-ONLY descriptor template (``eod_cron_descriptor``) +
shape-asserting test; live ``openclaw cron add`` registration is a deferred OPERATOR step.

U6 -- set tomorrow's #1 (the loop's WRITE side, KTD-6): the EOD proposes a #1 from the
board's priority/capacity and renders it with a "Set as tomorrow's #1" button
(``tt:top:<id>``) plus a couple of alternatives. A tap routes through the U2 dispatcher
to the ``set-top`` command, which writes ``tomorrow-pointer.json`` (the morning standup,
U8, reads it). When the board has NO open task to nominate, the EOD writes an EXPLICIT
"none" pointer here directly (there is nothing to tap), so the standup shows a clean
board rather than a stale prior-day #1. Proposing is otherwise READ-ONLY -- no pointer
is written until the user taps (mirroring the no-change-without-confirm invariant), with
the one deliberate exception of the empty-board "none" record.

U5 -- forced disposition: every task still open at EOD is rendered with a disposition
button row (``tt:done`` / ``tt:carry`` / ``tt:rsch`` / ``tt:drop``). NOTHING is
auto-mutated -- mirroring the no-change-without-confirm invariant, an un-tapped task
is REPORTED as "needs disposition", never silently carried or dropped. A tap routes
through the U2 dispatcher to the existing reversible, gated command path (``done`` /
``carry`` / ``reschedule`` / ``drop``).

Robustness: a broken harvest source (``gh``/``gog`` non-zero, a tripped circuit
breaker) is absorbed inside ``run_harvest`` -- it returns ``source_error: True`` and
yields whatever evidence the surviving sources produced (possibly none). U4 reports
that as a one-line "harvest unavailable" note and STILL completes the detect step,
so a flaky source never aborts the EOD (later slices proceed to disposition).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

import autonomy_gate
import cos_config
import delivery_target
import eod_summary
import error_envelope
import harvest_ledger
import harvest_state
import outbox
import telegram_buttons
import tomorrow_pointer
from task_ledger import append_event, new_event
from task_records import active_records, load_records

COMPONENT = "eod_review"
TRIGGER = "cron:eod_review"
ACTOR = "niemand-work"

# The act type the EOD send is gated under: a reversible, message-only push that makes
# NO board write (the board changes only on the user's later taps). Registered in
# autonomy_gate.DEFAULT_ACT_TYPE_RUNGS (rung 3) + PUSH_NO_BOARD_WRITE_ACTS.
EOD_ACT_TYPE = "eod_review_sent"

# The outbox idem-key kind + the env vars the EOD delivery target is proven from. The
# EOD posts to the Productivity DONE thread (KTD-5 / plan U7: the DONE=DAILY thread is
# the deploy-time default), the SAME target the weekly ledger digest proves -- so the
# delivery seam reuses harvest_ledger's exact prove pattern.
DELIVERY_KIND = "eod"
CHAT_ID_ENV = "TELEGRAM_CHAT_ID_PRODUCTIVITY"
TOPIC_ID_ENV = "OPENCLAW_TOPIC_PRODUCTIVITY_DONE"

# The confirm-gate accepts a detection only when the harvest CONFIDENTLY linked it
# to an open board task -- an ``evidence-link`` (a PR/mail that closes a tracked
# loop). A ``needs-review`` fuzzy match or a ``no-match`` item is detected work but
# has no single task to mark done on a tap, so it is reported (visible) but carries
# no Confirm button (there is nothing for ``approve`` to act on). This mirrors the
# ledger digest, where only an ``evidence-link`` advertises ``/approve <task_id>``.
_CONFIRMABLE_DECISION = "evidence-link"


def _confirmable(match: dict[str, Any]) -> bool:
    """True iff a harvested match can be CONFIRMED on a tap (links one open task)."""
    return (
        match.get("decision") == _CONFIRMABLE_DECISION
        and bool(match.get("matched_task_id"))
    )


def _detection(match: dict[str, Any]) -> dict[str, Any]:
    """Render one confirmable detection into a confirm-step record.

    The record carries the task id + a human display line + its evidence url and a
    single Confirm button row (``tt:appr:<task_id>``). The button is built through
    U1's ``approve_button``, which drops the button (returns ``None``) only if the
    callback value would overflow 64 bytes -- in which case the detection is still
    reported, the text-command confirm path remains the fallback, and no malformed
    callback is ever emitted. Nothing here mutates the board.
    """
    task_id = match["matched_task_id"]
    button = telegram_buttons.approve_button(task_id)
    return {
        "task_id": task_id,
        "title": match.get("title") or "",
        "source_type": match.get("source_type"),
        "evidence_url": match.get("url"),
        "score": match.get("score"),
        # A list so the renderer/sender (U7) treats this uniformly with multi-button
        # rows; an over-budget value yields an empty row (button dropped, text path
        # is the fallback) rather than a malformed entry.
        "buttons": [button] if button is not None else [],
    }


def detect(*, trigger: str = TRIGGER, now=None) -> dict[str, Any]:
    """Detect today's completions via the ``done24h`` harvest -- READ-ONLY.

    Runs ``harvest_ledger.run_harvest`` on the 24h window in ``dry_run`` mode: the
    harvest matches merged PRs + sent mail + manual wins against the active board
    but pushes NO digest and writes NO state (detection never consumes evidence or
    mutates anything). Returns a structured detect result:

    * ``detections`` -- the confirmable completions (each with a ``tt:appr`` Confirm
      button); EMPTY when nothing auto-detected (the caller renders a clean
      "nothing auto-detected" path, never an empty confirm prompt).
    * ``harvest_unavailable`` -- True when a harvest SOURCE errored (``source_error``
      from a non-zero ``gh``/``gog`` or a tripped breaker); the detect step still
      completes on whatever the surviving sources produced.
    * ``other_evidence_count`` -- detected work that did NOT confidently link to one
      open task (``needs-review``/``no-match``); reported for visibility, but not
      confirmable (no single task for ``approve`` to act on).

    NEVER raises -- a harvest subprocess failure is already caught inside
    ``run_harvest`` (it returns ``source_error`` rather than propagating); the
    ``main`` envelope classifies any other unhandled exception.
    """
    result = harvest_ledger.run_harvest(
        harvest_state.WINDOW_24H,
        since_override=None,
        dry_run=True,
        trigger=trigger,
        now=now,
    )
    # ``run_harvest`` returns two result shapes: the full shape (with ``matches``)
    # when there was content, and an early "nothing/blocked" shape (no ``matches``
    # key) when the source was empty or the push was gated off. ``.get`` over both
    # keeps detect agnostic to which path the harvest took.
    matches = result.get("matches") or []
    detections = [_detection(m) for m in matches if _confirmable(m)]
    other_evidence_count = sum(1 for m in matches if not _confirmable(m))
    return {
        "ok": True,
        "detections": detections,
        "detection_count": len(detections),
        "other_evidence_count": other_evidence_count,
        "harvest_unavailable": bool(result.get("source_error")),
        "harvest_window_id": result.get("harvest_window_id"),
    }


def _confirm_message(detect_result: dict[str, Any]) -> str:
    """The user-facing confirm-step text (no button JSON; buttons ride the send).

    A zero-detection EOD shows a single clean "nothing auto-detected" line and the
    later disposition step (U5) takes over -- it NEVER renders an empty confirm
    prompt. A harvest-source error appends a one-line "harvest unavailable" note so
    the user knows detection was partial, without any raw error text.
    """
    detections = detect_result["detections"]
    lines = ["EOD — detected completions"]
    if detections:
        lines.append("")
        lines.append("Tap Confirm to mark each done (nothing changes until you tap):")
        for det in detections:
            suffix = f" [{det['source_type']}]" if det.get("source_type") else ""
            lines.append(f"• {det['title']}{suffix}")
    else:
        lines.append("")
        lines.append("Nothing auto-detected today.")
    if detect_result.get("harvest_unavailable"):
        lines.append("")
        lines.append(error_envelope.degraded_notice("harvest"))
    return "\n".join(lines)


def build_confirm_step(*, trigger: str = TRIGGER, now=None) -> dict[str, Any]:
    """Assemble the EOD detect + confirm-step output (structured, no delivery).

    This is the U4 deliverable: a structured payload carrying the confirm-step
    ``message`` text and the per-detection Confirm buttons. It performs NO live
    send (U7 wires the receipt-backed delivery) and mutates NOTHING -- a tap on a
    rendered ``tt:appr`` button later drives ``harvest_ledger.approve`` through the
    existing reversible, topic-guarded path.
    """
    detected = detect(trigger=trigger, now=now)
    return {
        "ok": True,
        "step": "detect_confirm",
        "message": _confirm_message(detected),
        "detections": detected["detections"],
        "detection_count": detected["detection_count"],
        "other_evidence_count": detected["other_evidence_count"],
        "harvest_unavailable": detected["harvest_unavailable"],
        "harvest_window_id": detected["harvest_window_id"],
    }


def _open_tasks(*, personal: bool = False) -> list[Any]:
    """The active (open) board tasks the disposition step must force a decision on.

    READ-ONLY: this only lists; the disposition step renders buttons and reports, it
    NEVER mutates the board (a tap does, later, through the existing command path). A
    missing board degrades to an empty list -- an empty board is a clean no-op, never
    a crash.
    """
    try:
        _file, _content, records = load_records(personal)
    except FileNotFoundError:
        return []
    return list(active_records(records))


def _disposition_item(record: Any) -> dict[str, Any]:
    """Render one open task into a disposition record + its 4-button row.

    The row is ``tt:done`` / ``tt:carry`` / ``tt:rsch`` / ``tt:drop`` built through
    U1's ``disposition_row`` (each button drops gracefully if its callback would
    overflow 64 bytes, leaving the text command as the fallback). ``needs_disposition``
    is True for EVERY open task: nothing is decided until the user taps, so an
    un-tapped task is REPORTED (visible) with the board UNCHANGED -- the no-silent-carry,
    no-silent-drop invariant.
    """
    task_id = record.canonical_id
    return {
        "task_id": task_id,
        "title": record.title,
        "due": record.due,
        "section": record.section,
        "needs_disposition": True,
        "buttons": telegram_buttons.disposition_row(task_id) if task_id else [],
    }


def disposition(*, personal: bool = False) -> dict[str, Any]:
    """List every open task with a forced-disposition button row -- READ-ONLY.

    Returns the open tasks (each with a ``tt:done``/``tt:carry``/``tt:rsch``/``tt:drop``
    row) plus ``needs_disposition_count``. An EMPTY board is a clean no-op
    (``open_count == 0``), proceeding to tomorrow's #1 (U6). NOTHING is mutated here:
    the board changes only when the user taps a button (which the U2 dispatcher routes
    to the existing reversible, gated command). NEVER raises -- a missing board yields
    an empty list, and ``main``'s envelope classifies any other unhandled exception.
    """
    open_tasks = _open_tasks(personal=personal)
    items = [_disposition_item(record) for record in open_tasks]
    return {
        "ok": True,
        "open_count": len(items),
        "items": items,
        # Every open task needs a decision; an un-tapped one stays in this count so the
        # caller can REPORT "N still need disposition" without mutating anything.
        "needs_disposition_count": len(items),
    }


def _disposition_message(disposition_result: dict[str, Any]) -> str:
    """The user-facing disposition-step text (buttons ride the send, not the text).

    An empty board shows a single clean "nothing open" line -- never an empty prompt.
    Otherwise each open task is listed with its due marker so the user can decide; the
    "needs disposition" framing makes the no-change-until-tap invariant explicit.
    """
    items = disposition_result["items"]
    if not items:
        return "EOD — disposition\n\nNothing open — your board is clear."
    lines = ["EOD — disposition",
             "",
             f"{len(items)} open task(s) need a disposition "
             "(Done / Carry / Reschedule / Drop). Nothing changes until you tap:"]
    for item in items:
        due = f" 🗓️{item['due']}" if item.get("due") else ""
        lines.append(f"• {item['title']}{due}")
    return "\n".join(lines)


def build_disposition_step(*, personal: bool = False) -> dict[str, Any]:
    """Assemble the EOD forced-disposition step output (structured, no delivery).

    The U5 deliverable: a structured payload carrying the disposition-step ``message``
    text and the per-task disposition button rows. It performs NO live send (U7 wires
    delivery) and mutates NOTHING -- a tap on a rendered ``tt:done``/``carry``/``rsch``/
    ``drop`` button later drives the existing reversible, gated command path.
    """
    result = disposition(personal=personal)
    return {
        "ok": True,
        "step": "disposition",
        "message": _disposition_message(result),
        "items": result["items"],
        "open_count": result["open_count"],
        "needs_disposition_count": result["needs_disposition_count"],
    }


# --- U6: set tomorrow's #1 (the loop's write side) -------------------------

# Section rank for proposing tomorrow's #1: the most urgent open work first. q1
# (urgent & important) outranks q2, q2 outranks q3; anything else (team/today/etc.)
# falls to the back. The PROPOSAL is a hint -- the user taps the actual choice (or an
# alternative), so a coarse ranking is enough; we do NOT re-implement the full standup
# capacity model here (U8 owns the morning surface).
_SECTION_RANK: dict[str, int] = {"q1": 0, "q2": 1, "q3": 2}
_DEFAULT_SECTION_RANK = 9

# How many proposal candidates the EOD surfaces: the top pick plus a couple of
# alternatives, so the user can tap a different #1 without typing an id. Kept small so
# the button surface stays tappable (the ADHD-focused UX), not a wall of choices.
_TOP_PROPOSAL_COUNT = 3


def _proposal_key(record: Any) -> tuple[int, str, str]:
    """Rank an open task for the tomorrow's-#1 proposal: section, then due, then id.

    Most-urgent section first (q1<q2<q3<other); within a section the EARLIEST due date
    first (a task with no due date sorts after dated ones via the high sentinel); the
    canonical id is the final stable tie-break so the proposal is deterministic.
    """
    section_rank = _SECTION_RANK.get(record.section, _DEFAULT_SECTION_RANK)
    due = record.due or "9999-99-99"
    return (section_rank, due, record.canonical_id or "")


def propose_tomorrow_top(*, personal: bool = False) -> dict[str, Any]:
    """Propose tomorrow's #1 from the open board -- READ-ONLY (no pointer written).

    Ranks the open tasks (most-urgent section, then earliest due) and returns the top
    pick plus a couple of alternatives, each carrying a ``tt:top:<id>`` "Set as #1"
    button. NOTHING is written here: the pointer is set only when the user TAPS a button
    (which the U2 dispatcher routes to ``set-top``). An EMPTY board yields no candidates
    (``has_open == False``); the caller (``build_tomorrow_step``) then writes the
    explicit "none" pointer, since there is nothing to tap. NEVER raises -- a missing
    board degrades to no candidates.
    """
    open_tasks = _open_tasks(personal=personal)
    ranked = sorted(open_tasks, key=_proposal_key)
    candidates: list[dict[str, Any]] = []
    for record in ranked[:_TOP_PROPOSAL_COUNT]:
        task_id = record.canonical_id
        if not task_id:
            continue
        button = telegram_buttons.set_top_button(task_id)
        candidates.append({
            "task_id": task_id,
            "title": record.title,
            "section": record.section,
            "due": record.due,
            # A list (uniform with the other steps' rows); an over-budget callback
            # drops the button and leaves the text command as the fallback.
            "buttons": [button] if button is not None else [],
        })
    return {
        "ok": True,
        "has_open": bool(candidates),
        "candidates": candidates,
        "top": candidates[0] if candidates else None,
    }


def _tomorrow_message(proposal: dict[str, Any], *, none_written: bool) -> str:
    """The user-facing tomorrow's-#1 text (buttons ride the send, not the text).

    With candidates: name the proposed #1 + alternatives and make the tap-to-set
    contract explicit. With no open task: a single clean "board is clear" line -- the
    EOD has already recorded the explicit "none" pointer, so the standup opens clean.
    """
    if not proposal["has_open"]:
        return ("EOD — tomorrow's #1\n\n"
                "No open tasks to set as tomorrow's #1 — your board is clear. "
                "The morning standup will start fresh.")
    top = proposal["top"]
    lines = ["EOD — tomorrow's #1",
             "",
             f"Proposed #1: {top['title']}",
             "Tap to set it as tomorrow's #1 (or pick an alternative):"]
    for alt in proposal["candidates"][1:]:
        lines.append(f"• {alt['title']}")
    return "\n".join(lines)


def build_tomorrow_step(*, personal: bool = False) -> dict[str, Any]:
    """Assemble the EOD set-tomorrow's-#1 step (structured, no live delivery).

    The U6 deliverable: a structured payload carrying the proposal ``message`` text and
    the per-candidate ``tt:top`` buttons. With open tasks it writes NOTHING (the pointer
    is set on a TAP -> ``set-top``). With an EMPTY board it writes the EXPLICIT "none"
    pointer here -- there is nothing to tap, and the standup must see a deliberate "no #1"
    record, not a stale prior-day pointer (single canonical pointer, OVERWRITTEN never
    appended). Returns ``wrote_none`` so the caller/audit can see the empty-board write.
    """
    proposal = propose_tomorrow_top(personal=personal)
    wrote_none = False
    if not proposal["has_open"]:
        tomorrow_pointer.set_none(source=tomorrow_pointer.SOURCE_EOD)
        wrote_none = True
    return {
        "ok": True,
        "step": "tomorrow_top",
        "message": _tomorrow_message(proposal, none_written=wrote_none),
        "candidates": proposal["candidates"],
        "has_open": proposal["has_open"],
        "top": proposal["top"],
        "wrote_none": wrote_none,
    }


# --- U7: assemble the full ritual, deliver it, summarise, record health --------


def _assemble(*, personal: bool = False) -> dict[str, Any]:
    """Run the three EOD steps and bundle their text + buttons into ONE ritual payload.

    Reuses the U4/U5/U6 builders verbatim -- this only composes their structured output.
    The disposition + tomorrow's-#1 steps may write the empty-board "none" pointer (U6),
    but NO board task is mutated here: every board change still requires a user tap.
    """
    confirm = build_confirm_step()
    disposition = build_disposition_step(personal=personal)
    tomorrow = build_tomorrow_step(personal=personal)
    message = "\n\n".join(
        step["message"] for step in (confirm, disposition, tomorrow)
    )
    # The buttons that ride the send: every detection's Confirm + every open task's
    # disposition row + the tomorrow's-#1 proposal buttons, in ritual order.
    buttons: list[dict[str, Any]] = []
    for det in confirm["detections"]:
        buttons.extend(det["buttons"])
    for item in disposition["items"]:
        buttons.extend(item["buttons"])
    for cand in tomorrow["candidates"]:
        buttons.extend(cand["buttons"])
    return {
        "confirm": confirm,
        "disposition": disposition,
        "tomorrow": tomorrow,
        "message": message,
        "buttons": buttons,
    }


def _summary_inputs(assembled: dict[str, Any]) -> dict[str, Any]:
    """Derive the ``## EOD Summary`` groups from the assembled ritual (no I/O).

    * done today -- the confirmable detections (the work the harvest linked to an open
      task; the canonical wins live in the ledger, this is the human-readable echo).
    * still open -- every task the disposition step lists (the board's open set at EOD).
    * tomorrow's #1 -- the proposed top, or ``None`` on an empty board.
    """
    done_today = [det["title"] for det in assembled["confirm"]["detections"]]
    still_open = [item["title"] for item in assembled["disposition"]["items"]]
    top = assembled["tomorrow"]["top"]
    tomorrow_top = top["title"] if top else None
    return {"done_today": done_today, "still_open": still_open,
            "tomorrow_top": tomorrow_top}


def _resolve_target() -> dict[str, Any]:
    """Prove the EOD delivery target (the Productivity DONE thread) from env.

    Mirrors ``harvest_ledger._resolve_push_target``: an unset/garbage
    ``TELEGRAM_CHAT_ID_PRODUCTIVITY`` / ``OPENCLAW_TOPIC_PRODUCTIVITY_DONE`` returns a
    BLOCKED result and NO send happens -- never a guessed target.
    """
    chat_id = os.getenv(CHAT_ID_ENV)
    topic_id = os.getenv(TOPIC_ID_ENV)
    return delivery_target.prove_delivery_target(chat_id, topic_id, agent_id=ACTOR)


def _prove_gate_authorise() -> dict[str, Any]:
    """Prove -> gate -> assert the EOD delivery target, returning an authorised target.

    The same proof chain ``nag_delivery``/``harvest_ledger`` use: env proof, then
    ``autonomy_gate.gate`` (re-proves the target inside the gate, binds an ``act_id``),
    then ``assert_send_target`` (the gated target is the SOLE permitted destination).
    Returns ``{"ok": True, "delivery_target", "act_id"}`` or ``{"ok": False, "reason"}``
    -- a non-ok result means the caller delivers NOTHING (no partial send).
    """
    proof = _resolve_target()
    if not proof["ok"]:
        return {"ok": False, "reason": proof.get("reason", "env_missing")}
    gated = autonomy_gate.gate(
        EOD_ACT_TYPE, delivery_target=proof["delivery_target"], unit="U7",
        agent_id=ACTOR,
    )
    if not gated["ok"]:
        return {"ok": False, "reason": gated.get("reason", "gate_blocked")}
    target = gated["delivery_target"]
    asserted = autonomy_gate.assert_send_target(gated["act_id"], target)
    if not asserted["ok"]:
        return {"ok": False, "reason": asserted.get("reason", "target-mismatch")}
    return {"ok": True, "delivery_target": target, "act_id": gated["act_id"]}


def _eod_idem_key(now=None) -> str:
    """The outbox idem-key for one EOD: ``eod:<YYYY-MM-DD>`` in the local zone.

    Keyed on the local calendar DATE so one EOD delivers per day: a same-day cron retry
    (or a manual re-fire before midnight) short-circuits to the recorded receipt and
    never double-sends the evening ritual.
    """
    day = (now or cos_config.local_now()).strftime("%Y-%m-%d")
    return outbox.make_idem_key(DELIVERY_KIND, day)


def deliver(
    assembled: dict[str, Any],
    *,
    sender: Callable[..., dict[str, Any]] | None = None,
    now=None,
) -> dict[str, Any]:
    """Deliver the assembled EOD through the receipt-backed seam AT MOST ONCE per day.

    Proves + gates + asserts the DONE-thread target, then hands the ritual text + buttons
    to ``outbox.deliver_once`` (idempotent, receipt-capturing). A blocked proof/gate
    returns ``{"ok": False, "reason"}`` with NO send (no partial delivery). A transport
    failure (the sender raises) is caught and returned as ``{"ok": False,
    "reason": "delivery_failed:..."}`` -- the board mutations the user already confirmed
    via taps are NEVER coupled to this send. A successful send (fresh OR an idempotent
    short-circuit) returns ``{"ok": True, "message_id", "idempotent"}``.
    """
    authorised = _prove_gate_authorise()
    if not authorised["ok"]:
        return {"ok": False, "reason": authorised["reason"]}
    idem_key = _eod_idem_key(now=now)
    try:
        receipt = outbox.deliver_once(
            authorised["delivery_target"], assembled["message"], idem_key,
            sender=sender or outbox.openclaw_sender, buttons=assembled["buttons"],
        )
    except Exception as exc:  # noqa: BLE001 -- a send failure is a delivery block, not a
        # crash: nothing partial leaves, the confirmed taps already stand on the board.
        return {"ok": False, "reason": f"delivery_failed:{type(exc).__name__}",
                "message": str(exc)}
    return {
        "ok": True,
        "message_id": receipt.get("message_id"),
        "idempotent": bool(receipt.get("idempotent")),
        "delivery_target": authorised["delivery_target"],
    }


def run(*, personal: bool = False, sender: Callable[..., dict[str, Any]] | None = None,
        now=None) -> dict[str, Any]:
    """The full U7 EOD: assemble -> deliver -> upsert ## EOD Summary -> audit.

    Returns ``{"ok", "delivered", "reason"?, "summary_path", "message_id"?}``. The board
    mutations from U4/U5/U6 already committed ONLY on the user's taps and are independent
    of delivery: a blocked/failed send leaves ``ok: False`` (so ``run_main`` records an
    ``eod_review`` health FAILURE) but never partially sends and never touches those
    confirmed taps. The ``## EOD Summary`` is upserted ONLY on a delivered EOD (idempotent
    -- a re-fire replaces the section, never appends), and the ``eod_summary_written``
    audit event carries the receipt id + the note path so a replay can prove the ritual
    ran end-to-end.
    """
    assembled = _assemble(personal=personal)
    delivered = deliver(assembled, sender=sender, now=now)
    if not delivered["ok"]:
        return {"ok": False, "delivered": False, "reason": delivered["reason"]}

    summary_inputs = _summary_inputs(assembled)
    written = eod_summary.write_summary(
        done_today=summary_inputs["done_today"],
        still_open=summary_inputs["still_open"],
        tomorrow_top=summary_inputs["tomorrow_top"],
    )
    append_event(new_event(
        "eod_summary_written", actor=ACTOR, source="agent_autonomous",
        metadata={
            "message_id": delivered.get("message_id"),
            "summary_path": written["path"],
            "idempotent_send": delivered["idempotent"],
            "done_count": len(summary_inputs["done_today"]),
            "open_count": len(summary_inputs["still_open"]),
        },
    ))
    return {
        "ok": True,
        "delivered": True,
        "idempotent": delivered["idempotent"],
        "message_id": delivered.get("message_id"),
        "summary_path": written["path"],
        "summary_changed": written["changed"],
    }


# --- U7 deterministic cron descriptor (CODE-ONLY -- no live registration) -------

# The 18:00 EOD command-cron HOUR (local). A documented template only; the OPERATOR
# registers the live cron with ``openclaw cron add`` -- this code never calls it.
EOD_CRON_HOUR = 18


def eod_cron_descriptor(
    *, chat_id_env: str = CHAT_ID_ENV, topic_env: str = TOPIC_ID_ENV,
    scripts_dir: str = "/data/.openclaw/skills/task-tracker/scripts",
) -> dict[str, Any]:
    """The deterministic-command-cron descriptor for the EOD (CODE-ONLY template).

    Mirrors the U4-nag cron shape: ``payload.kind == "command"`` (a deterministic argv,
    NOT an LLM agentTurn), running ``telegram-commands.sh eod`` in the skill's scripts
    dir, with ``delivery.mode == "announce"`` to the Productivity DONE thread. This is a
    TEMPLATE the operator hands to ``openclaw cron add``; nothing here registers a live
    cron, edits ``openclaw.json``, or restarts the gateway (a deferred OPERATOR step). The
    env-var NAMES (not values) are embedded so the operator resolves the live target at
    registration time -- no real chat id is committed.
    """
    return {
        "schedule": {"kind": "daily", "hour": EOD_CRON_HOUR, "minute": 0},
        "payload": {
            "kind": "command",
            "argv": [
                "sh", "-lc",
                f"cd {scripts_dir} && bash telegram-commands.sh eod",
            ],
        },
        "delivery": {
            "mode": "announce",
            "chat_id_env": chat_id_env,
            "topic_env": topic_env,
        },
    }


def _render_run_text(payload: dict[str, Any]) -> str:
    """Plain-text rendering for the full EOD run (the delivery + summary outcome)."""
    if not payload["delivered"]:
        return error_envelope.degraded_notice("eod review")
    summary = "summary updated" if payload.get("summary_changed") else "summary unchanged"
    dedup = " (already delivered today)" if payload.get("idempotent") else ""
    return f"EOD delivered{dedup}; {summary}."


def _render_text(payload: dict[str, Any]) -> str:
    """The plain-text rendering for a non-JSON CLI run (the message + a count line)."""
    lines = [payload["message"]]
    if payload["detection_count"]:
        lines.append("")
        lines.append(
            f"{payload['detection_count']} completion(s) await confirmation "
            "(tap a Confirm button)."
        )
    return "\n".join(lines)


def _render_disposition_text(payload: dict[str, Any]) -> str:
    """Plain-text rendering for a non-JSON disposition run (message + a count line)."""
    lines = [payload["message"]]
    if payload["needs_disposition_count"]:
        lines.append("")
        lines.append(
            f"{payload['needs_disposition_count']} task(s) need a disposition "
            "(tap a button)."
        )
    return "\n".join(lines)


def _render_tomorrow_text(payload: dict[str, Any]) -> str:
    """Plain-text rendering for a non-JSON tomorrow's-#1 run (message + a hint line)."""
    lines = [payload["message"]]
    if payload["has_open"]:
        lines.append("")
        lines.append("Tap a Set-as-#1 button to set tomorrow's #1.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="EOD ritual: deliver the full evening ritual (default), or preview one step"
    )
    parser.add_argument("--json", action="store_true", help="Structured JSON output")
    parser.add_argument(
        "--step", choices=["detect", "disposition", "tomorrow"], default=None,
        help="preview ONE step (read-only, no delivery); default is the full delivered run",
    )
    args = parser.parse_args(argv)

    if args.step == "disposition":
        payload = build_disposition_step()
        render = _render_disposition_text
    elif args.step == "tomorrow":
        payload = build_tomorrow_step()
        render = _render_tomorrow_text
    elif args.step == "detect":
        payload = build_confirm_step()
        render = _render_text
    else:
        # The default (the `eod` command): assemble + DELIVER the full ritual, upsert the
        # ## EOD Summary, and let run_main record eod_review health. A blocked/failed
        # delivery returns ok:False -> a nonzero exit -> run_main records a health FAILURE
        # (the confirmed taps already stand on the board; nothing partial leaves).
        payload = run()
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True, default=str))
        else:
            print(_render_run_text(payload))
        return 0 if payload["ok"] else 1
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    else:
        print(render(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(error_envelope.run_main(COMPONENT, main, trigger=TRIGGER))
