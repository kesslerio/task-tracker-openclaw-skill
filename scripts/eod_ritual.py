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

Scope boundary (U4 only): this is detect + confirm ONLY. It does NOT build the
forced disposition (U5), the tomorrow-pointer (U6), or the delivery / cron /
Obsidian summary (U7). ``main`` produces the structured detect + confirm output;
live delivery is wired by U7. ``eod_review.py`` already parses the daily note for
done/not-done; this unit reuses ``run_harvest`` rather than re-implementing
evidence detection, and leaves daily-note parsing to ``eod_review`` where the later
EOD slices need it.

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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="U4 EOD ritual: detect today's completions + render confirm buttons"
    )
    parser.add_argument("--json", action="store_true", help="Structured JSON output")
    args = parser.parse_args(argv)

    payload = build_confirm_step()
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    else:
        print(_render_text(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(error_envelope.run_main(COMPONENT, main, trigger=TRIGGER))
