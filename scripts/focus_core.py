#!/usr/bin/env python3
"""Layer-2 capacity model: the active-inventory cap (Contract 6 / Decision #7).

The single home for "how loaded is the active board?" so the write-time gate in
``tasks.py``, the capacity display in ``standup.py``, and the morning proposal in
``defended_three.py`` all agree on one calculation. No unit re-derives the cap.

The model (two-layer; Layer 1 = daily priorities lives in ``defended_three.py``):

* Active set = ``task_records.active_records()`` -- every non-done, non-backlog,
  non-parking_lot task, INCLUDING ``section=None`` ("All Tasks") tasks. This is
  the single source of truth; ``focus-state.holding_tank`` members are a
  focus-state flag on active tasks, not a board section, so they are already
  counted here (they ARE active work, per the U3 mustFix).
* Estimate sum = ``sum(parse_duration(estimate::))`` over the active set, with
  unestimated tasks counted at ``UNESTIMATED_TASK_HOURS`` (default 2h) so a
  sparsely-estimated board is not silently under the ceiling.
* The cap is breached when EITHER the estimate-sum exceeds
  ``WEEKLY_CAPACITY_HOURS`` (default 25h) OR the active count exceeds
  ``ACTIVE_TASK_HARD_CAP`` (default 20). Breaching nudges a backlog move; it
  NEVER force-evicts existing tasks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import cos_config
from task_records import TaskRecord, active_records, task_records
from utils import format_duration, parse_duration


@dataclass(frozen=True)
class CapacitySummary:
    """A read-only snapshot of the active board's load against the Layer-2 cap."""

    active_count: int
    estimated_minutes: int
    unestimated_count: int
    capacity_minutes: int
    hard_cap: int

    @property
    def estimate_exceeded(self) -> bool:
        return self.estimated_minutes > self.capacity_minutes

    @property
    def count_exceeded(self) -> bool:
        return self.active_count > self.hard_cap

    @property
    def over_cap(self) -> bool:
        """True when EITHER ceiling is breached -- a new add would over-fill."""
        return self.estimate_exceeded or self.count_exceeded

    @property
    def projected_minutes(self) -> int:
        """Active load if one more (unestimated) task were added."""
        return self.estimated_minutes + _unestimated_minutes()

    @property
    def projected_count(self) -> int:
        """Active count if one more task were added."""
        return self.active_count + 1

    def would_exceed_with_one_more(self) -> bool:
        """True if adding ONE more (unestimated) task would breach either ceiling.

        The write-time gate asks this BEFORE the board write: a board that is
        already at-or-over the ceiling, or that a single new task would push
        over, blocks the add. A new task is sized at ``UNESTIMATED_TASK_HOURS``
        (the add path supplies no estimate), so the projected load is honest.
        """
        return (
            self.projected_minutes > self.capacity_minutes
            or self.projected_count > self.hard_cap
        )


def active_work_records(records: list[TaskRecord]) -> list[TaskRecord]:
    """Active tasks that are actual WORK -- objective-header lines excluded.

    ``active_records()`` excludes done/backlog/parking_lot but still includes
    ``is_objective`` grouping headers (parent lines like "Hiring", not actionable
    tasks). Both the Layer-1 proposal (``rank_active_records``) and the standup
    DO-list already skip those headers; the Layer-2 cap MUST agree, or N objective
    headers each add +1 to the count and +UNESTIMATED_TASK_HOURS to the estimate,
    falsely flagging overcommit. This is the single active-work definition both
    layers share.
    """
    active = [record for record in active_records(records) if not record.is_objective]
    return _distinct_active_records(active)


def _normalised_title(record: TaskRecord) -> str:
    return re.sub(r"\s+", " ", record.title).strip().casefold()


def _distinct_active_records(records: list[TaskRecord]) -> list[TaskRecord]:
    """Collapse duplicate active records before they hit count/hour caps.

    Identity wins first: repeated physical lines with the same canonical id are
    one task. The remaining rows are then collapsed by normalized title so bare
    duplicate/phantom lines without ``task_id::`` do not consume extra capacity.
    """
    by_id: list[TaskRecord] = []
    seen_ids: set[str] = set()
    for record in records:
        if record.canonical_id:
            if record.canonical_id in seen_ids:
                continue
            seen_ids.add(record.canonical_id)
        by_id.append(record)

    distinct: list[TaskRecord] = []
    seen_titles: set[str] = set()
    for record in by_id:
        title_key = _normalised_title(record)
        if title_key and title_key in seen_titles:
            continue
        if title_key:
            seen_titles.add(title_key)
        distinct.append(record)
    return distinct


def _unestimated_minutes() -> int:
    return max(cos_config.unestimated_task_hours(), 0) * 60


def estimate_minutes_for(record: TaskRecord) -> int:
    """Minutes a single active record contributes to the cap.

    A task with a parseable ``estimate::`` contributes its parsed duration; one
    with no (or an unparseable) estimate contributes ``UNESTIMATED_TASK_HOURS``
    so the ceiling is never silently undercounted by missing estimates.
    """
    parsed = parse_duration(getattr(record, "estimate", None))
    return parsed if parsed > 0 else _unestimated_minutes()


def summarize_capacity(records: list[TaskRecord]) -> CapacitySummary:
    """Build the active-board capacity snapshot from parsed task records.

    ``records`` is the FULL record list (as from ``task_records()``); the active
    set is derived here via ``active_work_records()`` so callers cannot pass a
    pre-filtered set that drops ``section=None`` or holding-tank tasks and skew
    the count, nor one that includes objective-header grouping lines.
    """
    active = active_work_records(records)
    estimated = 0
    unestimated = 0
    for record in active:
        parsed = parse_duration(getattr(record, "estimate", None))
        if parsed > 0:
            estimated += parsed
        else:
            unestimated += 1
            estimated += _unestimated_minutes()
    return CapacitySummary(
        active_count=len(active),
        estimated_minutes=estimated,
        unestimated_count=unestimated,
        capacity_minutes=max(cos_config.weekly_capacity_hours(), 0) * 60,
        hard_cap=max(cos_config.active_task_hard_cap(), 0),
    )


def count_active_tasks(records: list[TaskRecord]) -> int:
    """Active-task count for the Layer-2 cap.

    section=None tasks are included; objective-header grouping lines are not
    (they are not work) -- matching Layer-1 and the standup DO-list.
    """
    return len(active_work_records(records))


def capacity_display(summary: CapacitySummary) -> str:
    """One human-readable capacity line for the standup capacity ceiling.

    ``✅ Capacity OK`` when within ceiling; ``⚠️ Overcommitted`` when over.
    Always shows estimate-sum vs ~1 week of capacity, plus the count vs hard cap
    when that is the breached dimension.
    """
    est = format_duration(summary.estimated_minutes) or "0m"
    cap = format_duration(summary.capacity_minutes) or "0m"
    unestimated = (
        f" ({summary.unestimated_count} unestimated @ "
        f"{cos_config.unestimated_task_hours()}h)"
        if summary.unestimated_count
        else ""
    )
    if not summary.over_cap:
        return f"✅ Capacity OK: {est} of {cap} active load{unestimated}"

    reasons = []
    if summary.estimate_exceeded:
        reasons.append(f"{est} estimated > {cap} capacity")
    if summary.count_exceeded:
        reasons.append(f"{summary.active_count} active > {summary.hard_cap} cap")
    return f"⚠️ Overcommitted: {'; '.join(reasons)}{unestimated}"


def projected_breach_reason(summary: CapacitySummary) -> str:
    """Explain which ceiling a single new task would breach (for the add denial).

    Names the PROJECTED dimension (load/count after one more), so the user sees
    why the add is blocked even when the current board is exactly at the cap.
    """
    reasons = []
    if summary.projected_minutes > summary.capacity_minutes:
        projected = format_duration(summary.projected_minutes) or "0m"
        cap = format_duration(summary.capacity_minutes) or "0m"
        reasons.append(f"would reach {projected} of active load (cap {cap})")
    if summary.projected_count > summary.hard_cap:
        reasons.append(
            f"would reach {summary.projected_count} active tasks (cap {summary.hard_cap})"
        )
    return "; ".join(reasons) if reasons else "would exceed the active-inventory cap"


# --- The write-time add gate (Layer-2 enforcement) -------------------------

@dataclass(frozen=True)
class AddGateDecision:
    """The Layer-2 gate's verdict on a single ``tasks add`` against the cap.

    ``allowed`` is the only thing the caller must branch on; ``summary`` and
    ``denial_message`` ride along so the caller never re-computes either. A
    summary that could not be built (unparseable board) yields ``allowed=True``
    with ``summary=None`` -- the gate fails OPEN rather than wedging the user out
    of their own board on a transient parse error.
    """

    allowed: bool
    summary: CapacitySummary | None
    denial_message: str | None = None


def evaluate_add(
    content: str,
    fmt: str,
    title: str,
    *,
    destination_active: bool = True,
    personal: bool = False,
) -> AddGateDecision:
    """Decide whether a new task may be added without breaching the Layer-2 cap.

    The board is parsed and summarised exactly ONCE here. The gate blocks when
    adding one more task would push the active board past either ceiling, and
    builds the friendly denial message (nudge a backlog move; never force-evict).
    A parse failure degrades to ``allowed=True`` (fail open).

    ``destination_active`` is False when the add targets an INACTIVE section
    (e.g. ``--priority low`` -> Backlog): such a task adds zero active load and
    is always allowed, since the cap governs the active inventory only.

    ``personal`` is threaded into ``task_records`` so a personal-format board is
    parsed correctly; the work-vs-personal SCOPE decision (the cap governs the
    work board only) is the caller's, made before calling this.
    """
    if not destination_active:
        return AddGateDecision(allowed=True, summary=None)

    try:
        summary = summarize_capacity(task_records(content, personal=personal, fmt=fmt))
    except Exception:
        return AddGateDecision(allowed=True, summary=None)

    if not summary.would_exceed_with_one_more():
        return AddGateDecision(allowed=True, summary=summary)

    # Describe the PROJECTED breach only -- the gate fires on the projected state,
    # so splicing in the current-state capacity_display() line (which can read
    # "✅ Capacity OK" at a boundary load) would contradict the "❌" header.
    message = (
        "❌ Active-inventory cap reached.\n"
        f"Adding this task {projected_breach_reason(summary)}.\n\n"
        f'To add "{title}", first free up capacity:\n'
        f'  tasks parking-lot add "{title}"  → send to parking lot for later\n'
        "  complete or reschedule an active task → frees a slot\n\n"
        "Override with: tasks add --force-parking (auto-sends to the parking lot)"
    )
    return AddGateDecision(allowed=False, summary=summary, denial_message=message)
