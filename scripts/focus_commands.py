#!/usr/bin/env python3
"""CLI entry for the ``/focus*`` commands (U3 Layer-1 + capacity display).

Subcommands:

* ``focus``            -- (re)run the morning Daily Top Priorities proposal.
* ``focus-approve``    -- lock the current proposal.
* ``focus-veto N``     -- drop priority N and re-propose.
* ``focus-override``   -- approve an over-capacity board explicitly (reason logged).
* ``focus-status``     -- show the current focus state + capacity line.

Every path runs inside the U1 error envelope (``run_main``) so a failure surfaces
a friendly one-line notice, never a traceback.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import error_envelope
import focus_state
from defended_three import (
    approve_focus_state,
    propose_defended_three,
    veto_and_repropose,
    write_proposal,
)
from focus_core import capacity_display, summarize_capacity
from task_records import load_records
from utils import format_duration


def _load_work_records():
    # Focus Core is work-board-only by design (the cap knobs are work-tuned and
    # focus-state.json is a single non-namespaced file), so there is no
    # --personal flag -- a personal run would otherwise overwrite the work
    # proposal with a work-sized capacity verdict.
    _, _, records = load_records(personal=False)
    return records


def _print_capacity_line(records) -> None:
    """Print a blank line + the Layer-2 capacity line for the given records."""
    print("")
    print(capacity_display(summarize_capacity(records)))


def _render_proposal(state: dict) -> str:
    """Render a proposed/approved focus state as a Telegram-friendly block."""
    lines: list[str] = []
    status = state.get("status")
    header = "🎯 Today's Daily Priorities"
    if status == focus_state.STATUS_APPROVED:
        header = "✅ Daily Priorities locked"
    lines.append(f"{header} — {state.get('date')}")
    lines.append("")

    total = state.get("total_estimated_minutes") or 0
    est_str = format_duration(total) or "0m"
    cap_flag = "✅ within capacity" if state.get("capacity_ok") else "⚠️ over capacity"
    lines.append(f"Total estimated: {est_str} ({cap_flag})")
    lines.append("")

    for row in state.get("daily_priorities", []):
        est = format_duration(row.get("estimate_minutes") or 0)
        est_part = f" (estimate:: {est})" if est else ""
        esc = " ⬆️ escalated" if row.get("escalated") else ""
        section = row.get("section") or ""
        section_part = f" [{section}]" if section else ""
        lines.append(f"{row.get('position')}. {row.get('title')}{est_part}{section_part}{esc}")

    holding = state.get("holding_tank") or []
    if holding:
        lines.append("")
        lines.append(f"{len(holding)} more active task(s) demoted for today.")

    if status != focus_state.STATUS_APPROVED:
        lines.append("")
        lines.append(
            "Reply /focus-veto <N> to swap, /focus-approve to lock"
            + (", /focus-override to accept overcommit." if not state.get("capacity_ok") else ".")
        )
    return "\n".join(lines)


def cmd_focus(args) -> int:
    """(Re)propose the daily priorities. Always re-proposes (stale-date safe)."""
    records = _load_work_records()
    state = write_proposal(propose_defended_three(records))
    print(_render_proposal(state))
    _print_capacity_line(records)
    return 0


def _current_proposal(verb: str) -> dict | None:
    """Return today's PROPOSED focus state, or print guidance + return None.

    The three mutating commands (approve / override / veto) all require a current
    proposal; this is the single guard so the "run /focus first" message and the
    stale-date rule live in one place.
    """
    state = focus_state.load_focus_state()
    if focus_state.status_for_today(state) != focus_state.STATUS_PROPOSED:
        print(f"⚠️ No current proposal to {verb}. Run /focus to propose today's priorities.")
        return None
    return state


def cmd_focus_approve(args) -> int:
    state = _current_proposal("approve")
    if state is None:
        return 0
    if not state.get("capacity_ok"):
        print(
            "⚠️ Board is over capacity. Use /focus-override to accept the overcommit "
            "explicitly, or move a task to the parking lot first."
        )
        return 0
    print(_render_proposal(approve_focus_state(state)))
    return 0


def cmd_focus_override(args) -> int:
    state = _current_proposal("override")
    if state is None:
        return 0
    print(_render_proposal(approve_focus_state(state, override_reason="user_explicit")))
    print("")
    print("⚠️ Overcommit accepted explicitly (logged for the weekly retro).")
    return 0


def cmd_focus_veto(args) -> int:
    state = _current_proposal("veto")
    if state is None:
        return 0
    updated = veto_and_repropose(state, args.position, _load_work_records())
    print(_render_proposal(updated))
    return 0


def cmd_focus_status(args) -> int:
    state = focus_state.load_focus_state()
    if focus_state.status_for_today(state) is None:
        print("No Daily Priorities set for today. Run /focus to propose them.")
    else:
        print(_render_proposal(state))
    _print_capacity_line(_load_work_records())
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Daily Priorities (Defended Three) commands")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("focus", help="Propose today's daily priorities").set_defaults(func=cmd_focus)
    sub.add_parser("approve", help="Lock the current proposal").set_defaults(func=cmd_focus_approve)
    sub.add_parser(
        "override", help="Accept an over-capacity board explicitly"
    ).set_defaults(func=cmd_focus_override)

    veto = sub.add_parser("veto", help="Drop priority N and re-propose")
    veto.add_argument("position", type=int, help="Priority position to remove (1-based)")
    veto.set_defaults(func=cmd_focus_veto)

    sub.add_parser("status", help="Show current focus state").set_defaults(func=cmd_focus_status)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(error_envelope.run_main("focus", main))
