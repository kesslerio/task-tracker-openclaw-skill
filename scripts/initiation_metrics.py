#!/usr/bin/env python3
"""v0.4-C initiation metrics: a PURE read of the ledger for the holdout efficacy read.

Correlates each initiation DECISION (a ``initiation_sent`` for treatment, a
``initiation_suppressed_holdout`` for control) with whether the user STARTED that task
within the success window (decisions-C7: 45 min), and reports per-arm start rates plus
the false-positive material a HUMAN labels. It reads only -- no send, no mutation, no
decision: the C->B escalation (is the nudge earning its place?) stays the human's call,
made from ``valid_holdouts`` (the >=15-20 gate) + the treatment-vs-control lift + the
manually-reviewed miss candidates. The agent never decides its own promotion.

Decisions still inside their window at ``now`` are PENDING (the outcome is not yet
determinable) and are excluded from the rates rather than miscounted as misses.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

import cos_config
import task_ledger
from initiation_holdout import ARM_CONTROL, ARM_TREATMENT

_DECISION_TYPES = {"initiation_sent", "initiation_suppressed_holdout"}
_START_TYPES = {"start_session_started", "body_double_started"}


def _parse(raw: Any) -> datetime | None:
    if not raw or not isinstance(raw, str):
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _arm_of(event: dict[str, Any]) -> str:
    """The arm of a decision event -- from metadata, falling back to the event type
    (a suppressed-holdout is control by construction; a sent is treatment)."""
    arm = (event.get("metadata") or {}).get("arm")
    if arm in (ARM_TREATMENT, ARM_CONTROL):
        return arm
    return ARM_CONTROL if event.get("event_type") == "initiation_suppressed_holdout" else ARM_TREATMENT


def _local_day(dt: datetime) -> str:
    return dt.astimezone(cos_config.local_tz()).date().isoformat()


def summarize(
    *, now: datetime | None = None, path: Any = None, window_min: int | None = None
) -> dict[str, Any]:
    """Per-arm initiation-success summary over the ledger. Pure read."""
    now = now or datetime.now(timezone.utc)
    window = timedelta(minutes=window_min or cos_config.initiation_success_window_min())
    events = task_ledger.read_events(path)

    starts_by_task: dict[str | None, list[datetime]] = defaultdict(list)
    for event in events:
        if event.get("event_type") in _START_TYPES:
            ts = _parse(event.get("timestamp"))
            if ts is not None:
                starts_by_task[event.get("task_id")].append(ts)
    for stamps in starts_by_task.values():
        stamps.sort()

    arms = {ARM_TREATMENT: {"decisions": 0, "started_within": 0, "pending": 0},
            ARM_CONTROL: {"decisions": 0, "started_within": 0, "pending": 0}}
    already_started_fp: list[dict[str, Any]] = []
    miss_candidates: list[dict[str, Any]] = []

    for event in events:
        if event.get("event_type") not in _DECISION_TYPES:
            continue
        dec_ts = _parse(event.get("timestamp"))
        if dec_ts is None:
            continue
        arm = _arm_of(event)
        task_id = event.get("task_id")
        starts = starts_by_task.get(task_id, [])
        stage = (event.get("metadata") or {}).get("stage")

        # A start STRICTLY BEFORE the decision, same local day -> the nudge/hold fired
        # though the user had already engaged today (the CAS should prevent this; an
        # auto false-positive sanity check).
        if any(s < dec_ts and _local_day(s) == _local_day(dec_ts) for s in starts):
            already_started_fp.append({"task_id": task_id, "arm": arm, "stage": stage,
                                       "decision_ts": event.get("timestamp")})

        if dec_ts + window > now:
            arms[arm]["pending"] += 1  # window still open -> outcome not yet determinable
            continue
        arms[arm]["decisions"] += 1
        if any(dec_ts < s <= dec_ts + window for s in starts):
            arms[arm]["started_within"] += 1
        elif event.get("event_type") == "initiation_sent":
            # A treatment nudge with no start in the window: material for MANUAL FP
            # labelling (bad-time / not-#1 / irrelevant -- snooze/dismissal alone != FP).
            miss_candidates.append({"task_id": task_id, "stage": stage,
                                    "decision_ts": event.get("timestamp")})

    for stats in arms.values():
        stats["start_rate"] = (stats["started_within"] / stats["decisions"]
                               if stats["decisions"] else None)

    return {
        "window_min": window_min or cos_config.initiation_success_window_min(),
        "treatment": arms[ARM_TREATMENT],
        "control": arms[ARM_CONTROL],
        # The C->B escalation gate is the HOLDOUT count (decisions-C7: >=15-20 valid
        # control observations), read by a human -- never the agent.
        "valid_holdouts": arms[ARM_CONTROL]["decisions"],
        "already_started_fp": already_started_fp,
        "miss_candidates": miss_candidates,
    }
