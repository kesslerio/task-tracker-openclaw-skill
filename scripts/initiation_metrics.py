#!/usr/bin/env python3
"""v0.4-C initiation metrics: a PURE read of the ledger for the holdout efficacy read.

Aggregates per EPISODE (``focus_episode_id``), not per raw decision event: an episode's
outcome is "did the user START that task within the success window (decisions-C7: 45 min)
of its FIRST nudge/hold decision?". Per-episode framing makes the read robust -- a slot
re-decided across ticks, or a cold_start + re-nudge pair, counts as ONE observation, so
``valid_holdouts`` is the number of DISTINCT held episodes (the >=15-20 escalation gate),
not an inflated decision count.

It reads only -- no send, no mutation, no decision: the C->B escalation (is the nudge
earning its place?) stays the human's call, made from ``valid_holdouts`` + the
treatment-vs-control lift + the manually-reviewed miss candidates. The agent never decides
its own promotion.

A "start" is a focus session opened via ``/start`` or ``/body-double``
(``start_session_started`` / ``body_double_started``). A user who begins work WITHOUT
opening a session is invisible here, so ``miss_candidates`` is a strict UPPER bound the
human filters. Episodes still inside their window at ``now`` are PENDING (outcome not yet
determinable) and excluded from the rates rather than miscounted as misses.
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


def _starts_by_task(events: list[dict[str, Any]]) -> dict[str, list[datetime]]:
    """``task_id -> sorted start timestamps`` for the within-window correlation.

    Events without a parseable timestamp or a task_id are skipped, so a task_id-less
    start can never cross-correlate against a task_id-less decision.
    """
    by_task: dict[str, list[datetime]] = defaultdict(list)
    for event in events:
        if event.get("event_type") in _START_TYPES:
            task_id = event.get("task_id")
            ts = _parse(event.get("timestamp"))
            if task_id and ts is not None:
                by_task[task_id].append(ts)
    for stamps in by_task.values():
        stamps.sort()
    return by_task


def _episodes(events: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    """Fold decision events into one record per ``(focus_episode_id, arm)``, anchored at
    the episode's FIRST (earliest) decision instant."""
    episodes: dict[tuple[str, str], dict[str, Any]] = {}
    for event in events:
        if event.get("event_type") not in _DECISION_TYPES:
            continue
        meta = event.get("metadata") or {}
        slot = meta.get("focus_episode_id")
        task_id = event.get("task_id")
        ts = _parse(event.get("timestamp"))
        if not slot or not task_id or ts is None:
            continue
        arm = _arm_of(event)
        key = (slot, arm)
        current = episodes.get(key)
        if current is None or ts < current["first_ts"]:
            episodes[key] = {"slot": slot, "arm": arm, "task_id": task_id,
                             "first_ts": ts, "stage": meta.get("stage")}
    return episodes


def summarize(
    *, now: datetime | None = None, path: Any = None, window_min: int | None = None
) -> dict[str, Any]:
    """Per-arm, per-EPISODE initiation-success summary over the ledger. Pure read."""
    now = now or datetime.now(timezone.utc)
    window = timedelta(minutes=window_min or cos_config.initiation_success_window_min())
    events = task_ledger.read_events(path)
    starts_by_task = _starts_by_task(events)

    arms = {ARM_TREATMENT: {"episodes": 0, "started_within": 0, "pending": 0},
            ARM_CONTROL: {"episodes": 0, "started_within": 0, "pending": 0}}
    already_started_fp: list[dict[str, Any]] = []
    miss_candidates: list[dict[str, Any]] = []

    for episode in _episodes(events).values():
        arm, ts, task_id = episode["arm"], episode["first_ts"], episode["task_id"]
        starts = starts_by_task.get(task_id, [])
        row = {"task_id": task_id, "focus_episode_id": episode["slot"],
               "stage": episode["stage"], "decision_ts": ts.isoformat()}

        if any(s < ts and _local_day(s) == _local_day(ts) for s in starts):
            already_started_fp.append({**row, "arm": arm})

        started = any(ts < s <= ts + window for s in starts)
        if not started and ts + window > now:
            arms[arm]["pending"] += 1  # window still open -> outcome not yet determinable
            continue
        arms[arm]["episodes"] += 1
        if started:
            arms[arm]["started_within"] += 1
        elif arm == ARM_TREATMENT:
            # A treatment episode with no start in window: material for MANUAL FP
            # labelling (bad-time / not-#1 / irrelevant -- snooze/dismissal alone != FP).
            miss_candidates.append(row)

    for stats in arms.values():
        stats["start_rate"] = (stats["started_within"] / stats["episodes"]
                               if stats["episodes"] else None)

    return {
        "window_min": window_min or cos_config.initiation_success_window_min(),
        "treatment": arms[ARM_TREATMENT],
        "control": arms[ARM_CONTROL],
        # The C->B escalation gate is the HOLDOUT count -- distinct held EPISODES
        # (decisions-C7: >=15-20), read by a human, never the agent.
        "valid_holdouts": arms[ARM_CONTROL]["episodes"],
        "already_started_fp": already_started_fp,
        "miss_candidates": miss_candidates,
    }
