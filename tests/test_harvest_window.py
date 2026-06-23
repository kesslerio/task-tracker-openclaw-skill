import sys
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import harvest_window


def test_explicit_target_date_resolves_independent_of_wall_clock():
    resolved = harvest_window.resolve_standup_window(
        target_date=date(2026, 6, 23),
        now=datetime(2030, 1, 1, 9, 0, tzinfo=timezone.utc),
    )

    assert resolved.plan_date == date(2026, 6, 23)
    assert resolved.evidence_date == date(2026, 6, 23)
    assert resolved.week_id == "2026-W26"
    assert resolved.window_id == "2026-W26:2026-06-23:standup"
    assert resolved.evidence_start.isoformat() == "2026-06-23T00:00:00-07:00"
    assert resolved.evidence_end.isoformat() == "2026-06-24T00:00:00-07:00"


def test_after_midnight_run_uses_new_pacific_day_and_rerun_can_target_yesterday():
    after_midnight = harvest_window.resolve_standup_window(
        now=datetime.fromisoformat("2026-06-23T00:05:00-07:00")
    )
    rerun = harvest_window.resolve_standup_window(target_date=date(2026, 6, 22))

    assert after_midnight.plan_date == date(2026, 6, 23)
    assert after_midnight.window_id == "2026-W26:2026-06-23:standup"
    assert rerun.plan_date == date(2026, 6, 22)
    assert rerun.window_id == "2026-W26:2026-06-22:standup"


def test_dst_spring_forward_window_has_23_utc_hours():
    resolved = harvest_window.resolve_standup_window(
        target_date=date(2026, 3, 9),
        evidence_date=date(2026, 3, 8),
    )

    assert resolved.evidence_date == date(2026, 3, 8)
    assert resolved.evidence_start.isoformat() == "2026-03-08T00:00:00-08:00"
    assert resolved.evidence_end.isoformat() == "2026-03-09T00:00:00-07:00"
    delta = resolved.evidence_end.astimezone(timezone.utc) - resolved.evidence_start.astimezone(timezone.utc)
    assert delta.total_seconds() == 23 * 60 * 60


def test_dst_fall_back_window_has_25_utc_hours():
    resolved = harvest_window.resolve_standup_window(
        target_date=date(2026, 11, 2),
        evidence_date=date(2026, 11, 1),
    )

    assert resolved.evidence_date == date(2026, 11, 1)
    assert resolved.evidence_start.isoformat() == "2026-11-01T00:00:00-07:00"
    assert resolved.evidence_end.isoformat() == "2026-11-02T00:00:00-08:00"
    delta = resolved.evidence_end.astimezone(timezone.utc) - resolved.evidence_start.astimezone(timezone.utc)
    assert delta.total_seconds() == 25 * 60 * 60


def test_iso_week_year_boundary_uses_plan_date():
    resolved = harvest_window.resolve_standup_window(now=datetime.fromisoformat("2021-01-04T08:00:00-08:00"))

    assert resolved.evidence_date == date(2021, 1, 1)
    assert resolved.week_id == "2021-W01"


def test_morning_standup_evidence_is_prior_workday_not_today():
    resolved = harvest_window.resolve_standup_window(now=datetime.fromisoformat("2026-06-23T08:00:00-07:00"))

    assert resolved.contains("2026-06-22T09:00:00-07:00")
    assert resolved.contains("2026-06-22T23:59:59-07:00")
    assert not resolved.contains("2026-06-23T00:01:00-07:00")
    assert not resolved.contains("2026-06-21T23:59:59-07:00")


def test_source_query_window_uses_watermark_with_overlap():
    resolved = harvest_window.resolve_standup_window(
        target_date=date(2026, 6, 23),
        evidence_date=date(2026, 6, 22),
    )
    start, end = harvest_window.source_query_window(
        resolved,
        watermark="2026-06-22T15:00:00-07:00",
    )

    assert start.isoformat() == "2026-06-22T14:50:00-07:00"
    assert end == resolved.evidence_end


def test_source_query_window_clamps_future_watermark_to_evidence_end():
    resolved = harvest_window.resolve_standup_window(
        target_date=date(2026, 6, 23),
        evidence_date=date(2026, 6, 22),
    )

    start, end = harvest_window.source_query_window(
        resolved,
        watermark=datetime.fromisoformat("2026-06-24T01:00:00-07:00"),
    )

    assert start == resolved.evidence_end
    assert end == resolved.evidence_end
