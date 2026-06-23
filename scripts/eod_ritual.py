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

Scope boundary: this module now spans detect + confirm (U4) AND the forced
disposition (U5). It does NOT build the tomorrow-pointer (U6) or the delivery / cron
/ Obsidian summary (U7). ``main`` produces the structured detect + confirm output
plus the disposition step; live delivery is wired by U7. ``eod_review.py`` already
parses the daily note for done/not-done; this unit reuses ``run_harvest`` rather than
re-implementing evidence detection, and leaves daily-note parsing to ``eod_review``
where the later EOD slices need it.

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
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import error_envelope
import harvest_ledger
import harvest_state
import telegram_buttons
from task_records import active_records, load_records

COMPONENT = "eod_review"
TRIGGER = "cron:eod_review"

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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="EOD ritual: detect completions (confirm) + force a disposition on every open task"
    )
    parser.add_argument("--json", action="store_true", help="Structured JSON output")
    parser.add_argument("--step", choices=["detect", "disposition"], default="detect",
                        help="which EOD step to render (default: detect)")
    args = parser.parse_args(argv)

    if args.step == "disposition":
        payload = build_disposition_step()
        render = _render_disposition_text
    else:
        payload = build_confirm_step()
        render = _render_text
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    else:
        print(render(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(error_envelope.run_main(COMPONENT, main, trigger=TRIGGER))
