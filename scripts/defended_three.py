#!/usr/bin/env python3
"""Layer-1 Daily Top Priorities: propose / approve / veto (U3).

Each morning the agent PROPOSES ``DAILY_PRIORITY_COUNT`` (default 3) must-do-today
priorities -- a selection over the active board (surfaced + chased), never a limit
on how many tasks may exist (that is the Layer-2 cap in ``focus_core``). The user
vetoes or approves; the result is persisted by ``focus_state`` (the sole writer)
and logged to the JSONL ledger.

This module is pure proposal logic over parsed task records; it owns no file I/O
beyond delegating writes to ``focus_state`` and appends to ``task_ledger``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import cos_config
import focus_state
from focus_core import estimate_minutes_for, summarize_capacity
from task_ledger import append_event, new_event
from task_records import TaskRecord, active_records
from utils import effective_priority


@dataclass
class FocusProposal:
    """A ranked proposal: the chosen daily priorities + the demoted overflow."""

    defended: list[dict] = field(default_factory=list)
    holding_tank: list[dict] = field(default_factory=list)
    total_estimated_minutes: int = 0
    free_hours: float | None = None
    capacity_ok: bool = True


# Effective-priority ordering: escalated q1 first, then q2, then q3. Within a
# bucket we keep the regrouped order (already escalation-aware).
_SECTION_RANK = {"q1": 0, "q2": 1, "q3": 2}


def _candidate_row(record: TaskRecord, position: int, escalated: bool, section: str) -> dict:
    """Serialise an active record into a focus-state ``daily_priorities`` row."""
    return {
        "task_id": record.canonical_id or record.fallback_id,
        "title": record.title,
        "estimate_minutes": estimate_minutes_for(record),
        "section": section,
        "escalated": escalated,
        "position": position,
    }


def rank_active_records(
    records: list[TaskRecord], *, reference_date: str | None = None
) -> list[tuple[TaskRecord, dict]]:
    """Order active records by effective (escalation-aware) priority.

    Returns ``(record, effective_priority_result)`` pairs, most-important first.
    Objective-header pseudo-tasks are skipped (they are grouping lines, not
    actionable work), matching ``regroup_by_effective_priority``.
    """
    ranked: list[tuple[TaskRecord, dict]] = []
    for record in active_records(records):
        if record.is_objective:
            continue
        eff = effective_priority(
            {
                "section": record.section,
                "due": record.due,
                "done": record.done,
                "priority": record.priority,
            },
            reference_date,
        )
        ranked.append((record, eff))
    ranked.sort(key=lambda pair: _SECTION_RANK.get(pair[1]["section"], 3))
    return ranked


def propose_defended_three(
    records: list[TaskRecord],
    *,
    free_hours: float | None = None,
    reference_date: str | None = None,
) -> FocusProposal:
    """Propose the top ``DAILY_PRIORITY_COUNT`` daily priorities from active work.

    The remaining active candidates are recorded in ``holding_tank`` (demoted for
    the day, board untouched -- no force-evict). ``capacity_ok`` reflects the
    whole active board against the Layer-2 ceiling (capacity is a property of the
    inventory, not of the three chosen), so an honest "you are overcommitted"
    signal rides the proposal even when the three themselves are small.
    """
    count = max(cos_config.daily_priority_count(), 1)
    ranked = rank_active_records(records, reference_date=reference_date)

    defended: list[dict] = []
    holding_tank: list[dict] = []
    for index, (record, eff) in enumerate(ranked):
        if len(defended) < count:
            defended.append(
                _candidate_row(record, len(defended) + 1, eff["escalated"], eff["section"])
            )
        else:
            holding_tank.append(
                {
                    "task_id": record.canonical_id or record.fallback_id,
                    "title": record.title,
                    "reason": "beyond_daily_priority_count",
                }
            )

    total_estimated = sum(row["estimate_minutes"] for row in defended)
    summary = summarize_capacity(records)
    return FocusProposal(
        defended=defended,
        holding_tank=holding_tank,
        total_estimated_minutes=total_estimated,
        free_hours=free_hours,
        capacity_ok=not summary.over_cap,
    )


def _ledger_actor() -> dict:
    return {"actor": "niemand-work", "source": "agent_autonomous"}


def write_proposal(proposal: FocusProposal, *, reference_date: str | None = None) -> dict:
    """Persist a proposal as ``status="proposed"`` and log ``focus_proposed``."""
    state = focus_state.new_proposal_state(
        defended=proposal.defended,
        holding_tank=proposal.holding_tank,
        free_hours=proposal.free_hours,
        total_estimated_minutes=proposal.total_estimated_minutes,
        capacity_ok=proposal.capacity_ok,
        reference_date=reference_date,
    )
    focus_state.save_focus_state(state)
    append_event(
        new_event(
            "focus_proposed",
            **_ledger_actor(),
            metadata={
                "date": state["date"],
                "defended": [row["task_id"] for row in proposal.defended],
                "free_hours": proposal.free_hours,
                "total_estimated_minutes": proposal.total_estimated_minutes,
            },
        )
    )
    if not proposal.capacity_ok:
        append_event(
            new_event(
                "capacity_overcommit",
                **_ledger_actor(),
                metadata={
                    "date": state["date"],
                    "total_estimated_minutes": proposal.total_estimated_minutes,
                },
            )
        )
    return state


def approve_focus_state(
    state: dict, *, override_reason: str | None = None
) -> dict:
    """Approve the current proposal in place; log ``focus_approved``.

    Sets ``status="approved"`` and stamps ``approved_at``. ``override_reason``
    (``"user_explicit"`` from ``/focus-override``) is recorded when the user
    accepts an over-capacity board explicitly.
    """
    state["status"] = focus_state.STATUS_APPROVED
    state["approved_at"] = focus_state._now_iso()
    if override_reason:
        state["override_reason"] = override_reason
    focus_state.save_focus_state(state)
    append_event(
        new_event(
            "focus_approved",
            **_ledger_actor(),
            metadata={
                "date": state.get("date"),
                "defended": [row["task_id"] for row in state.get("daily_priorities", [])],
                "override_reason": state.get("override_reason"),
            },
        )
    )
    return state


def veto_and_repropose(
    state: dict,
    remove_position: int,
    records: list[TaskRecord],
    *,
    reference_date: str | None = None,
) -> dict:
    """Drop the daily priority at ``remove_position`` and promote the next candidate.

    Re-presents an updated ``status="proposed"`` state: the vetoed task is removed
    and the top holding-tank candidate (by effective priority) is promoted into
    its slot. Logs ``focus_vetoed`` with the removed + added task ids.
    """
    priorities = list(state.get("daily_priorities", []))
    removed = next((row for row in priorities if row.get("position") == remove_position), None)
    if removed is None:
        return state
    remaining = [row for row in priorities if row.get("position") != remove_position]

    # Veto is sticky for the day: a task vetoed in THIS or any earlier veto must
    # never be re-promoted, even though rank_active_records re-derives from the
    # whole board each call. The set persists on the focus-state document.
    vetoed = set(state.get("vetoed", []))
    vetoed.add(removed["task_id"])

    chosen_ids = {row["task_id"] for row in remaining}
    added_id = None
    for record, eff in rank_active_records(records, reference_date=reference_date):
        candidate_id = record.canonical_id or record.fallback_id
        if candidate_id in vetoed or candidate_id in chosen_ids:
            continue
        remaining.append(_candidate_row(record, 0, eff["escalated"], eff["section"]))
        added_id = candidate_id
        break

    # Keep the "most-important first" contract after promotion: a q1 promotion must
    # not sit below a q2 survivor just because it was appended last. Stable sort by
    # effective-section rank preserves intra-rank order.
    remaining.sort(key=lambda row: _SECTION_RANK.get(row.get("section"), 3))
    for position, row in enumerate(remaining, start=1):
        row["position"] = position

    # A promoted candidate leaves the holding tank (it is now a daily priority);
    # leaving it there would double-count it and overstate the demoted total.
    holding = [row for row in state.get("holding_tank", []) if row.get("task_id") != added_id]

    state["daily_priorities"] = remaining
    state["holding_tank"] = holding
    state["vetoed"] = sorted(vetoed)
    state["status"] = focus_state.STATUS_PROPOSED
    state["total_estimated_minutes"] = sum(row["estimate_minutes"] for row in remaining)
    focus_state.save_focus_state(state)
    append_event(
        new_event(
            "focus_vetoed",
            **_ledger_actor(),
            metadata={
                "date": state.get("date"),
                "removed_task_id": removed["task_id"],
                "added_task_id": added_id,
            },
        )
    )
    return state
