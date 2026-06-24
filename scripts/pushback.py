#!/usr/bin/env python3
"""Deterministic capacity push-back for the morning standup (v0.3.1 U9).

Rules only, no model. When the active board is over the existing Layer-2 ceiling
(``summarize_capacity(...).over_cap``), this STATES that the board is over capacity
and surfaces the active candidates ordered by an explicit stored fact -- their
``due`` date, most-overdue first (undated tasks last) -- then asks the user to
cut / defer / edit.

Hard boundaries (ADR-10, seed change #6):
- It NEVER chooses for the user (it lists candidates by an objective fact; it does
  not pick one).
- It NEVER mutates the board (pure read + render; it returns a string).
- It NEVER sends anything proactively (the text is part of the standup the user
  already requested/scheduled; there is no separate delivery here).
- No LLM. Fail-open: any parse/compute failure -> ``None`` (no push-back), so a
  malformed board never breaks the standup (mirrors ``focus_core.evaluate_add``).
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cos_config
from focus_core import (
    active_work_records,
    capacity_display,
    summarize_capacity,
)
from task_records import TaskRecord

# How many candidates to surface; the rest are summarised as "+N more" so the ask
# stays actionable rather than dumping the whole over-cap board.
MAX_CANDIDATES = 5


def _parse_due(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _canonical_id(record: TaskRecord) -> str:
    return record.task_id or record.legacy_id or record.fallback_id or ""


def _sort_key(record: TaskRecord) -> tuple[int, date, str]:
    """Dated tasks first (oldest/most-overdue due ascending); undated last.

    Ties (same due date, or both undated) break deterministically by canonical id.
    """
    due = _parse_due(record.due)
    if due is None:
        return (1, date.max, _canonical_id(record))
    return (0, due, _canonical_id(record))


def _candidate_line(record: TaskRecord, *, today: date, stale_days: int) -> str:
    # Collapse any embedded whitespace/newlines so a multi-line board title can't
    # split the bullet list or corrupt the surrounding standup block.
    title = " ".join((record.title or "").split()) or "(untitled)"
    canonical = _canonical_id(record)
    id_part = f" [{canonical}]" if canonical else ""
    due = _parse_due(record.due)
    if due is None:
        return f"  - {title}{id_part} (no due date)"
    overdue_days = (today - due).days
    if overdue_days > stale_days:
        return f"  - {title}{id_part} (due {due.isoformat()}, {overdue_days}d overdue - stale)"
    if overdue_days > 0:
        return f"  - {title}{id_part} (due {due.isoformat()}, {overdue_days}d overdue)"
    return f"  - {title}{id_part} (due {due.isoformat()})"


def capacity_pushback(
    records: list[TaskRecord] | None,
    *,
    today: date | None = None,
    stale_days: int | None = None,
) -> str | None:
    """Return the deterministic push-back block, or ``None`` when within cap.

    ``records`` is the FULL parsed record list (as passed to the standup capacity
    line). ``None`` means "not supplied" -- the live ``/standup`` CLI calls
    ``generate_standup`` without ``capacity_records``, so we load the work board the
    SAME way ``capacity_line`` does, or the push-back would be dead in production. An
    EMPTY list means "no active tasks" -> no push-back. The active set is derived via
    ``active_work_records`` so the candidate list matches the capacity count exactly.
    """
    try:
        if records is None:
            from task_records import load_records

            _, _, records = load_records(personal=False)
        if not records:
            return None
        summary = summarize_capacity(records)
        if not summary.over_cap:
            return None
        active = active_work_records(records)
    except Exception:  # noqa: BLE001 -- fail open; never break the standup
        return None
    if not active:
        return None

    today = today or cos_config.local_today()
    stale_days = cos_config.pushback_stale_days() if stale_days is None else stale_days

    ordered = sorted(active, key=_sort_key)
    shown = ordered[:MAX_CANDIDATES]

    lines = [
        f"{capacity_display(summary)} - consider trimming the active plan.",
        "Cut / defer / edit (most overdue first):",
    ]
    lines.extend(_candidate_line(record, today=today, stale_days=stale_days) for record in shown)
    remaining = len(ordered) - len(shown)
    if remaining > 0:
        lines.append(f"  - ... and {remaining} more active")
    lines.append("Reply to cut / defer / edit one - I won't change the board for you.")
    return "\n".join(lines)
