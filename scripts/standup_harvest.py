#!/usr/bin/env python3
"""Deterministic standup evidence harvest orchestration.

The weekly ledger still owns the brag digest and approval loop. This module owns
the morning-standup DONES candidate harvest: resolve the stable U1 window, call
the existing GitHub/Gmail adapters outside the state lock, wrap their output into
canonical evidence records, match them, persist idempotency state, and return a
read-only candidate section for ``standup.py``.
"""

from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cos_health
import evidence_record
import harvest_ledger
import harvest_state
import harvest_window
import standup_summarizer
from adapters import calendar_adapter, dialpad_adapter

STANDUP_RITUAL = "standup"
SOURCES = ("github", "gmail", "calendar", "dialpad_sms")


def _source_query(
    resolved: harvest_window.HarvestWindow,
    state: dict[str, Any],
    source: str,
) -> tuple[datetime, datetime]:
    return harvest_window.source_query_window(
        resolved,
        watermark=(state.get("watermarks") or {}).get(source),
    )


def _adapter_source_type(source: str, raw: dict[str, Any]) -> str:
    if source == "github":
        return "github"
    if source == "gmail":
        return "gmail"
    return str(raw.get("source") or source)


def _wrap_adapter_records(
    source: str,
    raw_records: list[dict[str, Any]],
    *,
    run_id: str,
) -> tuple[list[dict[str, Any]], int]:
    wrapped: list[dict[str, Any]] = []
    invalid = 0
    for raw in raw_records:
        try:
            provider_id = str(raw.get("provider_id") or "")
            provider_state = str(raw.get("provider_state") or "")
            occurred_at = raw.get("occurred_at")
            match_title = str(raw.get("match_title") or raw.get("title") or "")
            wrapped.append(
                evidence_record.adapter_record(
                    source=_adapter_source_type(source, raw),  # type: ignore[arg-type]
                    kind=str(raw.get("kind") or "activity"),  # type: ignore[arg-type]
                    provider_id=provider_id,
                    provider_state=provider_state,
                    occurred_at=occurred_at,
                    match_title=match_title,
                    title=str(raw.get("title") or match_title),
                    url=raw.get("url"),
                    run_id=run_id,
                )
            )
        except (TypeError, ValueError):
            invalid += 1
    return wrapped, invalid


def _record_source_health(
    source: str,
    *,
    failed: bool,
    invalid_count: int,
    trigger: str,
) -> dict[str, Any]:
    status = "failed" if failed else "partial" if invalid_count else "ok"
    error_class = None
    if failed:
        error_class = f"{source}_harvest_failed"
    elif invalid_count:
        error_class = f"{source}_invalid_records"
    receipt = {
        "status": status,
        "failed": failed,
        "invalid_records": invalid_count,
    }
    try:
        cos_health.record_source_status(
            STANDUP_RITUAL,
            source,
            status,
            error_class=error_class,
            trigger=trigger,
        )
    except Exception:  # noqa: BLE001 -- health receipts are best-effort
        receipt["receipt_error"] = True
    return receipt


def _latest_occurred_at(records: list[dict[str, Any]], source: str) -> str | None:
    latest: datetime | None = None
    latest_raw: str | None = None
    for record in records:
        if record.get("source") != source:
            continue
        occurred_at = record.get("occurred_at")
        if not occurred_at:
            continue
        try:
            parsed = harvest_window.parse_local_datetime(str(occurred_at))
        except ValueError:
            continue
        if latest is None or parsed > latest:
            latest = parsed
            latest_raw = str(occurred_at)
    return latest_raw


def _apply_attribution_policy(candidate: dict[str, Any]) -> dict[str, Any]:
    """Enforce ADR-05 on top of the shared matcher.

    The shared matcher can mark normalized-title or high-fuzzy matches as
    ``evidence-link``. For the standup candidate surface, only explicit
    identifiers/links auto-associate. Everything else
    remains a standalone review candidate with only a suggested task id.
    """
    matched_task_id = candidate.get("matched_task_id")
    match_type = candidate.get("match_type")
    explicit = bool(matched_task_id and match_type == "exact-id-or-link")

    candidate["suggested_task_id"] = matched_task_id
    candidate["auto_associated"] = explicit
    candidate["association_status"] = "auto-associated" if explicit else "needs-review" if matched_task_id else "no-match"
    if explicit:
        candidate["decision"] = "evidence-link"
    elif matched_task_id:
        candidate["decision"] = "needs-review"
        candidate["matched_task_id"] = None
    candidate["match"] = {
        "decision": candidate.get("decision"),
        "match_type": match_type,
        "score": candidate.get("score"),
        "matched_task_id": candidate.get("matched_task_id"),
        "suggested_task_id": candidate.get("suggested_task_id"),
    }
    return candidate


def _harvest_source(
    source: str,
    *,
    resolved: harvest_window.HarvestWindow,
    since: str,
    trigger: str,
    query_start: datetime,
    query_end: datetime,
) -> tuple[list[dict[str, Any]], bool]:
    if source == "github":
        return harvest_ledger.harvest_github(
            since,
            trigger=trigger,
            query_start=query_start,
            query_end=query_end,
            harvest_commits=True,
        )
    if source == "gmail":
        return harvest_ledger.harvest_gmail(
            since,
            trigger=trigger,
            query_start=query_start,
            query_end=query_end,
        )
    if source == "calendar":
        return calendar_adapter.harvest(
            resolved=resolved,
            trigger=trigger,
            query_start=query_start,
            query_end=query_end,
        )
    if source == "dialpad_sms":
        return dialpad_adapter.harvest(
            resolved=resolved,
            trigger=trigger,
            query_start=query_start,
            query_end=query_end,
        )
    raise ValueError(f"unsupported standup harvest source: {source}")


def _github_summary_metadata(candidates: list[dict[str, Any]]) -> list[dict[str, str]]:
    minimal: list[dict[str, str]] = []
    for candidate in candidates:
        if candidate.get("source") != "github" or candidate.get("kind") != "activity":
            continue
        evidence_hash = str(candidate.get("evidence_hash") or "")
        match_title = str(candidate.get("match_title") or "")
        if evidence_hash and match_title:
            minimal.append({"evidence_hash": evidence_hash, "match_title": match_title})
    return minimal


def harvest(*, target_date: str | date | None = None, trigger: str) -> dict[str, Any]:
    """Return fresh standup evidence candidates for the stable U1 window."""
    resolved = harvest_window.resolve_standup_window(target_date=target_date)
    state, expired = harvest_state.load_or_reset(
        resolved.window_id,
        harvest_state.WINDOW_STANDUP,
    )
    run_id = harvest_state.new_run_id()
    since = resolved.evidence_start.date().isoformat()

    all_records: list[dict[str, Any]] = []
    health: dict[str, Any] = {}
    failed_sources: set[str] = set()
    for source in SOURCES:
        query_start, query_end = _source_query(resolved, state, source)
        raw_records, failed = _harvest_source(
            source,
            resolved=resolved,
            since=since,
            trigger=trigger,
            query_start=query_start,
            query_end=query_end,
        )
        if failed:
            failed_sources.add(source)
        wrapped, invalid = _wrap_adapter_records(source, raw_records, run_id=run_id)
        all_records.extend(wrapped)
        health[source] = _record_source_health(
            source,
            failed=failed,
            invalid_count=invalid,
            trigger=trigger,
        )

    filtered = harvest_window.filter_records(all_records, resolved)
    matched = [
        _apply_attribution_policy(candidate)
        for candidate in harvest_ledger.match_evidence(filtered)
    ]

    fresh: list[dict[str, Any]] = [
        candidate
        for candidate in matched
        if not harvest_state.is_seen(state, candidate["evidence_hash"], candidate.get("provider_state"))
    ]
    persisted_fresh: list[dict[str, Any]] = []
    # Only advance a source's watermark when that source's harvest fully succeeded
    # (failed=False). A partial failure -- e.g. PR search ok but commit search failed
    # -- must NOT advance the watermark, or the next run starts past the records the
    # failed query missed and skips them permanently. is_seen dedups the re-query.
    watermarks = {
        source: watermark
        for source in SOURCES
        if source not in failed_sources
        and (watermark := _latest_occurred_at(filtered, source)) is not None
    }

    def mutate(live_state: dict[str, Any]) -> None:
        live_state["run_id"] = run_id
        live_fresh = [
            candidate
            for candidate in fresh
            if not harvest_state.is_seen(
                live_state,
                candidate["evidence_hash"],
                candidate.get("provider_state"),
            )
        ]
        if live_fresh:
            harvest_state.mark_seen(live_state, live_fresh)
            persisted_fresh.extend(live_fresh)
        for source, watermark in watermarks.items():
            harvest_state.mark_watermark(live_state, source, watermark)

    harvest_state.update_window_state(
        resolved.window_id,
        mutate,
        window=harvest_state.WINDOW_STANDUP,
    )

    summary = standup_summarizer.summarize(_github_summary_metadata(persisted_fresh))

    return {
        "evidence_candidates": persisted_fresh,
        "summary": summary,
        "health": health,
        "window": resolved.as_dict(),
        "run_id": run_id,
        "expired": expired,
    }
