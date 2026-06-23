#!/usr/bin/env python3
"""Stable Pacific-day evidence windows for standup harvests."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cos_config
import harvest_state

DEFAULT_OVERLAP_MINUTES = 10


@dataclass(frozen=True)
class HarvestWindow:
    """Resolved standup dates and evidence boundaries.

    ``plan_date`` is the day the standup plans. ``evidence_date`` is the completed
    local workday summarized by that standup. By default a morning standup plans
    today and summarizes the prior workday's Pacific calendar day.
    """

    plan_date: date
    evidence_date: date
    week_id: str
    window_id: str
    evidence_start: datetime
    evidence_end: datetime
    query_start: datetime
    overlap_minutes: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "plan_date": self.plan_date.isoformat(),
            "evidence_date": self.evidence_date.isoformat(),
            "week_id": self.week_id,
            "window_id": self.window_id,
            "evidence_start": self.evidence_start.isoformat(),
            "evidence_end": self.evidence_end.isoformat(),
            "query_start": self.query_start.isoformat(),
            "overlap_minutes": self.overlap_minutes,
        }

    def contains(self, occurred_at: str | datetime) -> bool:
        dt = parse_local_datetime(occurred_at)
        return self.evidence_start <= dt < self.evidence_end


def _parse_date(value: str | date | None) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(cos_config.local_tz()).date() if value.tzinfo else value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


def parse_local_datetime(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=cos_config.local_tz())
    return dt.astimezone(cos_config.local_tz())


def previous_workday(plan_date: date) -> date:
    offset = 3 if plan_date.weekday() == 0 else 1
    candidate = plan_date - timedelta(days=offset)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


def _local_midnight(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=cos_config.local_tz())


def resolve_standup_window(
    *,
    target_date: str | date | None = None,
    evidence_date: str | date | None = None,
    now: datetime | None = None,
    overlap_minutes: int = DEFAULT_OVERLAP_MINUTES,
) -> HarvestWindow:
    """Resolve the deterministic standup evidence window.

    ``target_date`` is an explicit evidence/label date for reruns. When omitted,
    the plan date is ``cos_config.local_today()`` (or ``now`` converted to
    Pacific in tests) and the evidence date is the prior workday.
    """
    explicit_target = target_date is not None
    if explicit_target:
        plan_date = _parse_date(target_date)
    elif now is not None:
        plan_date = parse_local_datetime(now).date()
    else:
        plan_date = cos_config.local_today()
    assert plan_date is not None

    if evidence_date is not None:
        resolved_evidence_date = _parse_date(evidence_date)
    elif explicit_target:
        resolved_evidence_date = plan_date
    else:
        resolved_evidence_date = previous_workday(plan_date)
    assert resolved_evidence_date is not None
    start = _local_midnight(resolved_evidence_date)
    end = _local_midnight(resolved_evidence_date + timedelta(days=1))
    week_id = harvest_state.iso_week_id(plan_date)
    return HarvestWindow(
        plan_date=plan_date,
        evidence_date=resolved_evidence_date,
        week_id=week_id,
        window_id=f"{week_id}:{plan_date.isoformat()}:standup",
        evidence_start=start,
        evidence_end=end,
        query_start=start - timedelta(minutes=overlap_minutes),
        overlap_minutes=overlap_minutes,
    )


def source_query_window(
    resolved: HarvestWindow,
    *,
    watermark: str | datetime | None = None,
    overlap_minutes: int | None = None,
) -> tuple[datetime, datetime]:
    """Return the overlapped query window a source adapter should request."""
    overlap = resolved.overlap_minutes if overlap_minutes is None else overlap_minutes
    start = resolved.evidence_start
    if watermark is not None:
        start = max(start, parse_local_datetime(watermark) - timedelta(minutes=overlap))
    else:
        start = start - timedelta(minutes=overlap)
    return start, resolved.evidence_end


def filter_records(records: list[dict[str, Any]], resolved: HarvestWindow) -> list[dict[str, Any]]:
    """Keep records whose ``occurred_at`` lands in the evidence window."""
    filtered: list[dict[str, Any]] = []
    for record in records:
        occurred_at = record.get("occurred_at")
        if occurred_at is None:
            filtered.append(record)
            continue
        if resolved.contains(str(occurred_at)):
            filtered.append(record)
    return filtered
