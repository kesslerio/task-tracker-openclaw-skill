#!/usr/bin/env python3
"""High-trust harvest auto-complete path.

This is intentionally narrower than the one-tap candidate path. It only writes for
structured, externally verified evidence that resolves to exactly one active,
non-recurring task. Everything else remains a candidate.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cos_config
import completion_candidates
import error_envelope
import harvest_ledger
import harvest_state
import harvest_window
from adapters import calendar_adapter, dialpad_adapter
from evidence_matching import (
    build_task_catalog,
    extract_inline_identifiers,
    normalize_title,
    safe_load_task_records,
)
from task_ledger import append_event, new_event
from task_transitions import complete_by_id

AUTO_ENV = "TASK_TRACKER_HIGH_TRUST_AUTO_ENABLED"
TRUE_VALUES = {"1", "true", "yes", "on", "enabled"}
ACTOR = "niemand-work"
CALENDAR_COMPONENT = "ledger_harvest:calendar"
SMS_COMPONENT = "ledger_harvest:dialpad_sms"
RECURRING_RE = re.compile(r"\brecur\s*::", re.IGNORECASE)


def auto_enabled() -> bool:
    raw = os.getenv(AUTO_ENV)
    return bool(raw and raw.strip().casefold() in TRUE_VALUES)


def _matched_task_id(match: dict[str, Any]) -> str | None:
    value = match.get("matched_task_id") or match.get("task_id")
    return value if isinstance(value, str) and value.strip() else None


def _source_type(match: dict[str, Any]) -> str | None:
    value = match.get("source_type") or match.get("source")
    return str(value).strip().casefold() if value else None


def _author_login(match: dict[str, Any]) -> str | None:
    value = match.get("author_login")
    if isinstance(value, str) and value.strip():
        return value.strip()
    author = match.get("author")
    if isinstance(author, dict):
        login = author.get("login")
        return str(login).strip() if login else None
    if author:
        return str(author).strip()
    return None


def _configured_github_owner() -> str | None:
    raw = os.getenv("TASK_TRACKER_GITHUB_OWNER")
    if not raw:
        return None
    cleaned = raw.strip().lstrip("@")
    return cleaned or None


def _score(match: dict[str, Any]) -> float:
    try:
        return float(match.get("score") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _real_task_entries(task_id: str | None, catalog: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not task_id:
        return []
    return [
        entry for entry in catalog
        if getattr(entry.get("record"), "task_id", None) == task_id
    ]


def _closed_issue_identifiers(match: dict[str, Any]) -> dict[str, set[str]]:
    exact: set[str] = set()
    fallback: set[str] = set()
    for value in match.get("closes_issues") or []:
        identifiers = extract_inline_identifiers(str(value))
        exact |= identifiers["exact"]
        fallback |= identifiers["fallback"]
    return {"exact": exact, "fallback": fallback}


def _closed_issue_entries(match: dict[str, Any], catalog: list[dict[str, Any]]) -> list[dict[str, Any]]:
    identifiers = _closed_issue_identifiers(match)
    exact = identifiers["exact"]
    if not exact:
        return []
    entries: list[dict[str, Any]] = []
    for entry in catalog:
        entry_exact = set(entry.get("exact_identifiers") or set())
        if exact & entry_exact:
            entries.append(entry)
    return entries


def _title_entries(match: dict[str, Any], catalog: list[dict[str, Any]]) -> list[dict[str, Any]]:
    title = str(match.get("match_title") or match.get("title") or "")
    normalized = normalize_title(title)
    if not normalized:
        return []
    return [
        entry for entry in catalog
        if entry.get("normalized_title") == normalized
    ]


def _record_is_recurring(record: Any) -> bool:
    return bool(
        getattr(record, "recur", None)
        or RECURRING_RE.search(str(getattr(record, "raw_line", "") or ""))
    )


def _single_resolved_record(match: dict[str, Any], catalog: list[dict[str, Any]]) -> Any | None:
    task_id = _matched_task_id(match)
    entries = _real_task_entries(task_id, catalog)
    if len(entries) != 1:
        return None
    return entries[0]["record"]


def _record_task_id(record: Any) -> str | None:
    value = getattr(record, "task_id", None)
    return value if isinstance(value, str) and value.strip() else None


def _pr_is_merged(match: dict[str, Any]) -> bool:
    return str(match.get("state") or "").strip().casefold() == "merged" and bool(match.get("merged_at"))


def _pr_author_allowed(match: dict[str, Any]) -> bool:
    owner = _configured_github_owner()
    if owner is None:
        # AUTO fails closed when the configured owner is absent. The evidence still
        # routes through the candidate lane; it just cannot perform a board write.
        return False
    author = _author_login(match)
    return bool(author and author.casefold() == owner.casefold())


def _pr_resolved_record(match: dict[str, Any], catalog: list[dict[str, Any]]) -> Any | None:
    if not _pr_is_merged(match):
        return None
    if not _pr_author_allowed(match):
        return None
    entries = _closed_issue_entries(match, catalog)
    if len(entries) != 1:
        return None
    record = entries[0]["record"]
    return record if _record_task_id(record) else None


def _calendar_unambiguous(match: dict[str, Any], catalog: list[dict[str, Any]]) -> bool:
    # Backlog: multi-day/ongoing calendar events with generic-title collisions remain
    # candidate-lane only; hardening that low-probability P3 is intentionally deferred.
    return (
        match.get("match_type") == "normalized-title"
        and _score(match) >= 1.0
        and match.get("organizer_self") is True
        and len(_title_entries(match, catalog)) == 1
    )


def _auto_resolved_record(match: dict[str, Any], catalog: list[dict[str, Any]]) -> Any | None:
    source_type = _source_type(match)
    if source_type == "pr":
        return _pr_resolved_record(match, catalog)
    if source_type == "calendar":
        if match.get("kind") != "activity":
            return None
        if not _calendar_unambiguous(match, catalog):
            return None
        return _single_resolved_record(match, catalog)
    return None


def _with_auto_task_id(match: dict[str, Any], catalog: list[dict[str, Any]]) -> dict[str, Any]:
    record = _auto_resolved_record(match, catalog)
    task_id = _record_task_id(record) if record is not None else None
    if not task_id or _matched_task_id(match) == task_id:
        return match
    updated = dict(match)
    updated["matched_task_id"] = task_id
    updated["decision"] = "evidence-link"
    updated["score"] = 1.0
    updated["match_type"] = "closed-issue-reference"
    return updated


def is_high_trust_auto_eligible(match: dict[str, Any], *, catalog: list[dict[str, Any]]) -> bool:
    """Return True only for the high-trust auto-complete envelope."""
    source_type = _source_type(match)
    if source_type != "pr" and match.get("decision") and match.get("decision") != "evidence-link":
        return False
    if source_type == "pr":
        record = _pr_resolved_record(match, catalog)
    elif source_type == "calendar":
        record = _auto_resolved_record(match, catalog)
    else:
        return False

    return bool(record is not None and not _record_is_recurring(record))


def partition_matches(
    matches: list[dict[str, Any]],
    *,
    catalog: list[dict[str, Any]],
    personal: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    del personal
    if not auto_enabled():
        return [], list(matches)
    auto_eligible: list[dict[str, Any]] = []
    remaining: list[dict[str, Any]] = []
    for match in matches:
        if is_high_trust_auto_eligible(match, catalog=catalog):
            auto_eligible.append(_with_auto_task_id(match, catalog))
        else:
            remaining.append(match)
    return auto_eligible, remaining


def _completion_source(match: dict[str, Any]) -> str:
    return "calendar" if _source_type(match) == "calendar" else "merged_pr"


def _evidence_link_event(task_id: str, match: dict[str, Any], source: str, completion: dict[str, Any]) -> dict[str, Any]:
    return new_event(
        "evidence_link",
        task_id=task_id,
        actor=ACTOR,
        source=source,
        evidence={
            "source_type": _source_type(match),
            "source_url": match.get("url"),
            "match_score": match.get("score"),
            "match_type": match.get("match_type"),
        },
        metadata={
            "high_trust_auto": True,
            "completion_id": completion.get("event_id"),
        },
    )


def auto_complete(
    auto_eligible: list[dict[str, Any]],
    *,
    personal: bool,
    catalog: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    live_catalog = catalog if catalog is not None else build_task_catalog(safe_load_task_records(personal))
    for match in auto_eligible:
        if not auto_enabled():
            results.append({
                "task_id": _matched_task_id(match),
                "ok": False,
                "source": _completion_source(match),
                "url": match.get("url"),
                "reason": "auto-disabled",
                "skipped": True,
            })
            continue
        if not is_high_trust_auto_eligible(match, catalog=live_catalog):
            results.append({
                "task_id": _matched_task_id(match),
                "ok": False,
                "source": _completion_source(match),
                "url": match.get("url"),
                "reason": "ineligible",
                "skipped": True,
            })
            continue
        match = _with_auto_task_id(match, live_catalog)
        task_id = _matched_task_id(match)
        source = _completion_source(match)
        url = match.get("url")
        if not task_id:
            results.append({"task_id": None, "ok": False, "source": source, "url": url})
            continue
        # Backlog: if a task is deleted between partition and complete_by_id, the
        # existing failure-to-candidate path is non-wrong-task UX noise, not an AUTO
        # authorization gap.
        try:
            result = complete_by_id(
                task_id,
                personal=personal,
                source=source,
                extra_events_factory=lambda event, m=match, tid=task_id, src=source: [
                    _evidence_link_event(tid, m, src, event)
                ],
            )
        except Exception as exc:  # noqa: BLE001 - one bad task must not abort the batch.
            results.append({
                "task_id": task_id,
                "ok": False,
                "source": source,
                "url": url,
                "error": {"code": type(exc).__name__, "message": str(exc)},
            })
            continue
        entry = {
            "task_id": task_id,
            "ok": bool(result.get("ok")),
            "source": source,
            "url": url,
        }
        if result.get("noop"):
            entry["noop"] = True
            entry["reason"] = result.get("reason")
        if result.get("error"):
            entry["error"] = result.get("error")
        if result.get("completion_id"):
            entry["completion_id"] = result.get("completion_id")
        results.append(entry)
    return results


def _terminal_done_result(result: dict[str, Any]) -> bool:
    return bool(result.get("noop") and str(result.get("reason") or "").startswith("already-"))


def _completed_task_ids(results: list[dict[str, Any]]) -> set[str]:
    completed: set[str] = set()
    for result in results:
        task_id = result.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            continue
        if result.get("ok") or _terminal_done_result(result):
            completed.add(task_id)
    return completed


def _dedupe_candidates(
    matches: list[dict[str, Any]],
    *,
    exclude_task_ids: set[str],
) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen_task_ids: set[str] = set()
    for match in matches:
        task_id = _matched_task_id(match)
        if task_id and task_id in exclude_task_ids:
            continue
        if task_id:
            if task_id in seen_task_ids:
                continue
            seen_task_ids.add(task_id)
        deduped.append(match)
    return deduped


def _calendar_target_date(since_override: str | None) -> date:
    if since_override:
        try:
            return date.fromisoformat(since_override)
        except ValueError:
            pass
    return cos_config.local_today()


def _calendar_evidence(
    *,
    since_override: str | None,
    trigger: str,
    evidence_window: harvest_window.HarvestWindow | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    resolved = evidence_window or harvest_window.resolve_standup_window(
        target_date=_calendar_target_date(since_override)
    )
    try:
        # Live calendar depends on gog being available in-container; until that
        # operator/U7 enabler is present, the adapter degrades to no evidence.
        records, failed = calendar_adapter.harvest(resolved=resolved, trigger=trigger)
    except Exception as exc:  # noqa: BLE001 - calendar is additive, never a blocker.
        error_envelope.log_degraded(CALENDAR_COMPONENT, exc, trigger=trigger, check="calendar")
        return [], True

    evidence: list[dict[str, Any]] = []
    for record in records:
        provider_id = str(record.get("provider_id") or record.get("url") or record.get("title") or "")
        if not provider_id:
            continue
        item = dict(record)
        item.setdefault("source", "calendar")
        item.setdefault("source_type", "calendar")
        item.setdefault("evidence_hash", harvest_ledger._evidence_hash("calendar", provider_id))
        evidence.append(item)
    return evidence, failed


def _dialpad_evidence(
    *,
    since_override: str | None,
    trigger: str,
    evidence_window: harvest_window.HarvestWindow | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    resolved = evidence_window or harvest_window.resolve_standup_window(
        target_date=_calendar_target_date(since_override)
    )
    try:
        records, failed = dialpad_adapter.harvest(resolved=resolved, trigger=trigger)
    except Exception as exc:  # noqa: BLE001 - SMS is additive, never a blocker.
        error_envelope.log_degraded(SMS_COMPONENT, exc, trigger=trigger, check="dialpad_sms")
        return [], True

    evidence: list[dict[str, Any]] = []
    for record in records:
        provider_id = str(record.get("provider_id") or record.get("title") or "")
        if not provider_id:
            continue
        item = dict(record)
        item.setdefault("source", "dialpad_sms")
        item.setdefault("source_type", "dialpad_sms")
        item.setdefault("evidence_hash", harvest_ledger._evidence_hash("dialpad_sms", provider_id))
        evidence.append(item)
    return evidence, failed


def _persist_low_trust_candidates(
    records: list[dict[str, Any]],
    *,
    personal: bool,
    dry_run: bool,
) -> dict[str, Any]:
    low_trust = [
        record for record in records
        if completion_candidates._is_low_trust_adapter_record(record)
    ]
    if dry_run or not low_trust:
        return {
            "created": [],
            "existing": [],
            "totals": {
                "parsed_evidence": len(low_trust),
                "created": 0,
                "existing": 0,
            },
        }
    return completion_candidates.scan_adapter_records(low_trust, personal=personal)


def _auto_evidence_window(now: datetime | None) -> harvest_window.HarvestWindow | None:
    if now is None:
        return None
    local_now = harvest_window.parse_local_datetime(now)
    return harvest_window.resolve_standup_window(target_date=local_now.date())


def _harvest_all_for_auto_window(
    resolved: harvest_window.HarvestWindow,
    *,
    trigger: str,
) -> tuple[list[dict[str, Any]], int, bool, str]:
    since = harvest_ledger._since_date_for_window(resolved)
    query_start, query_end = harvest_window.source_query_window(resolved)
    gh_evidence, gh_failed = harvest_ledger.harvest_github(
        since,
        trigger=trigger,
        query_start=query_start,
        query_end=query_end,
    )
    gmail_evidence, gmail_failed = harvest_ledger.harvest_gmail(
        since,
        trigger=trigger,
        query_start=query_start,
        query_end=query_end,
    )
    evidence = harvest_window.filter_records(gh_evidence + gmail_evidence, resolved)
    return evidence, 2, bool(gh_failed or gmail_failed), since


def _auto_completed_items(
    auto_eligible: list[dict[str, Any]],
    completed: list[dict[str, Any]],
    *,
    catalog: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for match, result in zip(auto_eligible, completed, strict=False):
        if not (result.get("ok") or _terminal_done_result(result)):
            continue
        record = _auto_resolved_record(match, catalog)
        items.append({
            "task_id": result.get("task_id"),
            "title": getattr(record, "title", None) or match.get("title") or result.get("task_id"),
            "source": result.get("source"),
            "url": result.get("url"),
        })
    return items


def run_auto_harvest(
    window: str,
    *,
    since_override: str | None = None,
    trigger: str,
    personal: bool = False,
    dry_run: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    evidence_window = _auto_evidence_window(now)
    since = (
        harvest_ledger._since_date_for_window(evidence_window)
        if evidence_window is not None
        else harvest_ledger._since_date(window, since_override)
    )
    run_id = harvest_state.new_run_id()
    harvest_window_id = evidence_window.window_id if evidence_window is not None else harvest_state.window_id(window)
    state_window = (
        harvest_state.WINDOW_STANDUP
        if evidence_window is not None
        else harvest_state.WINDOW_24H if window == harvest_state.WINDOW_24H else window
    )
    state, _expired = harvest_state.load_or_reset(harvest_window_id, state_window)

    if evidence_window is not None:
        evidence, sources_tried, source_error, since = _harvest_all_for_auto_window(
            evidence_window,
            trigger=trigger,
        )
    else:
        evidence, sources_tried, source_error = harvest_ledger.harvest_all(since, trigger=trigger)
    calendar_evidence, calendar_failed = _calendar_evidence(
        since_override=since_override,
        trigger=trigger,
        evidence_window=evidence_window,
    )
    dialpad_evidence, dialpad_failed = _dialpad_evidence(
        since_override=since_override,
        trigger=trigger,
        evidence_window=evidence_window,
    )
    adapter_evidence = calendar_evidence + dialpad_evidence
    low_trust_scan = _persist_low_trust_candidates(
        adapter_evidence,
        personal=personal,
        dry_run=dry_run,
    )
    high_trust_adapter_evidence = [
        item for item in adapter_evidence
        if not completion_candidates._is_low_trust_adapter_record(item)
    ]
    evidence = list(evidence) + high_trust_adapter_evidence
    for item in evidence:
        item.setdefault("run_id", run_id)
    fresh = [
        item for item in evidence
        if not harvest_state.is_seen(state, item["evidence_hash"], item.get("provider_state"))
    ]

    matches = harvest_ledger.match_evidence(fresh, personal=personal)
    catalog = build_task_catalog(safe_load_task_records(personal))
    auto_eligible, remaining = partition_matches(matches, catalog=catalog, personal=personal)
    completed: list[dict[str, Any]] = []
    if not dry_run and auto_enabled():
        append_event(
            new_event(
                "ledger_harvest_started",
                actor=harvest_ledger.ACTOR,
                source=harvest_ledger.LEDGER_SOURCE,
                metadata={
                    "harvest_window_id": harvest_window_id,
                    "window": window,
                    "since": since,
                    "run_id": run_id,
                    "high_trust_auto": True,
                },
            )
        )
        completed = auto_complete(auto_eligible, personal=personal, catalog=catalog)
        seen_auto_matches = [
            match for match, result in zip(auto_eligible, completed, strict=False)
            if result.get("ok") or _terminal_done_result(result)
        ]
        # Backlog: calendar seen-state replay after event edits plus manual task revive
        # is a niche P3; successful AUTO evidence remains consumed for this window.
        if seen_auto_matches:
            harvest_state.mark_seen(state, seen_auto_matches)
            harvest_state.save_state(state, state_window)
    auto_completed = _auto_completed_items(auto_eligible, completed, catalog=catalog)

    completed_task_ids = _completed_task_ids(completed)
    if dry_run:
        failed_auto_candidates = list(auto_eligible)
    else:
        failed_auto_candidates = [
            match for match, result in zip(auto_eligible, completed, strict=False)
            if not result.get("ok") and not _terminal_done_result(result)
        ]
    candidates = _dedupe_candidates(
        failed_auto_candidates + remaining,
        exclude_task_ids=completed_task_ids,
    )

    return {
        "completed": completed,
        "auto_completed": auto_completed,
        "candidates": candidates,
        "low_trust_candidates": low_trust_scan,
        "dry_run": dry_run,
        "auto_eligible_count": len(auto_eligible),
        "source_error": bool(source_error or calendar_failed or dialpad_failed),
        "sources_tried": sources_tried + 2,
        "evidence_count": len(fresh),
        "harvest_window_id": harvest_window_id,
        "run_id": run_id,
        "since": since,
    }


def _render_text(result: dict[str, Any]) -> str:
    completed = result.get("completed") or []
    candidates = result.get("candidates") or []
    return (
        f"High-trust auto harvest: {len(completed)} completed, "
        f"{len(candidates)} candidate(s), dry_run={bool(result.get('dry_run'))}."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run high-trust harvest auto-complete")
    parser.add_argument("--window", default=harvest_state.WINDOW_24H)
    parser.add_argument("--since", dest="since_override", default=None)
    parser.add_argument("--personal", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    result = run_auto_harvest(
        args.window,
        since_override=args.since_override,
        trigger="cli:harvest_auto",
        personal=args.personal,
        dry_run=args.dry_run,
    )
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
    else:
        print(_render_text(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
