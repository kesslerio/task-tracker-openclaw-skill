#!/usr/bin/env python3
"""Completion evidence candidate inbox backed by the task ledger."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

from evidence_matching import (
    FUZZY_EVIDENCE_LINK_THRESHOLD,
    FUZZY_REVIEW_THRESHOLD,
    build_task_catalog,
    extract_done_lines,
    match_evidence_line,
    safe_load_task_records,
)
from task_ledger import append_event, ledger_path, new_event, read_events
from task_transitions import complete_by_id
from utils import get_tasks_file

ACTIVE_STATUSES = {"new", "shown", "snoozed", "apply_failed"}
TERMINAL_STATUSES = {"confirmed", "rejected", "duplicate", "expired"}
DECISION_EVENTS = {
    "candidate_seen",
    "candidate_shown",
    "candidate_confirmed",
    "candidate_rejected",
    "candidate_snoozed",
    "candidate_duplicate",
    "candidate_expired",
    "candidate_apply_failed",
}


def _candidate_ledger_path(personal: bool = False) -> Path:
    tasks_file, _ = get_tasks_file(personal)
    return ledger_path(tasks_file)


def _normalize_summary(value: str) -> str:
    normalized = " ".join((value or "").casefold().split())
    return normalized


def candidate_id_for(source: dict[str, Any], summary: str) -> str:
    stable = {
        "source_type": source.get("type"),
        "source_path": source.get("path"),
        "source_date": source.get("date"),
        "line_number": source.get("line_number"),
        "timestamp": source.get("timestamp"),
        "summary": _normalize_summary(summary),
    }
    for key in ("channel", "sender", "message_id"):
        value = source.get(key)
        if value is not None:
            stable[key] = value
    material = json.dumps(stable, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return f"cand_{hashlib.sha256(material.encode('utf-8')).hexdigest()[:20]}"


def _candidate_from_seen(event: dict[str, Any]) -> dict[str, Any]:
    metadata = event.get("metadata") or {}
    candidate = dict(metadata.get("candidate") or {})
    candidate.setdefault("candidate_id", event.get("task_id"))
    candidate.setdefault("status", "new")
    candidate.setdefault("history", [])
    match_metadata = candidate.get("match_metadata") or {}
    matched_task_id = candidate.get("matched_task_id") or match_metadata.get("matched_task_id")
    if match_metadata.get("match_type") == "exact-id-or-link" and matched_task_id:
        candidate.setdefault("confirmable_task_id", matched_task_id)
        candidate.setdefault("review_required", False)
    else:
        candidate.setdefault("review_required", True)
    return candidate


def project_candidates(
    path: Path | None = None,
    *,
    include_terminal: bool = False,
    personal: bool = False,
) -> list[dict[str, Any]]:
    events = read_events(path or _candidate_ledger_path(personal), strict=True)
    candidates: dict[str, dict[str, Any]] = {}

    for event in events:
        event_type = event.get("event_type")
        if event_type not in DECISION_EVENTS:
            continue
        candidate_id = event.get("task_id")
        if not candidate_id:
            continue

        if event_type == "candidate_seen":
            candidate = candidates.get(candidate_id) or _candidate_from_seen(event)
            candidate.update(_candidate_from_seen(event))
            candidates[candidate_id] = candidate
        else:
            candidate = candidates.setdefault(
                candidate_id,
                {
                    "candidate_id": candidate_id,
                    "status": "new",
                    "history": [],
                },
            )

        metadata = event.get("metadata") or {}
        history_row = {
            "event_id": event.get("event_id"),
            "event_type": event_type,
            "timestamp": event.get("timestamp"),
            "metadata": metadata,
        }
        candidate.setdefault("history", []).append(history_row)

        if event_type == "candidate_shown" and candidate.get("status") in ACTIVE_STATUSES:
            candidate["status"] = "shown"
        elif event_type == "candidate_confirmed":
            candidate["status"] = "confirmed"
            candidate["confirmed_task_id"] = metadata.get("task_id")
            candidate["transition_event_id"] = metadata.get("transition_event_id")
        elif event_type == "candidate_rejected":
            candidate["status"] = "rejected"
            candidate["reason"] = metadata.get("reason")
        elif event_type == "candidate_snoozed":
            candidate["status"] = "snoozed"
            candidate["snoozed_until"] = metadata.get("until")
        elif event_type == "candidate_duplicate":
            candidate["status"] = "duplicate"
            candidate["duplicate_of"] = metadata.get("duplicate_of")
        elif event_type == "candidate_expired":
            candidate["status"] = "expired"
            candidate["reason"] = metadata.get("reason")
        elif event_type == "candidate_apply_failed":
            candidate["status"] = "apply_failed"
            candidate["last_error"] = metadata.get("error")

    projected = list(candidates.values())
    if not include_terminal:
        projected = [candidate for candidate in projected if candidate.get("status") not in TERMINAL_STATUSES]
    return sorted(projected, key=lambda item: item.get("candidate_id", ""))


def get_candidate(
    candidate_id: str,
    *,
    include_terminal: bool = True,
    personal: bool = False,
) -> dict[str, Any] | None:
    for candidate in project_candidates(include_terminal=include_terminal, personal=personal):
        if candidate.get("candidate_id") == candidate_id:
            return candidate
    return None


def _build_candidate_from_match(item: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    source_pointer = dict(source)
    if item.get("line_number") is not None:
        source_pointer["line_number"] = item.get("line_number")
    summary = item.get("parsed_title") or item.get("raw_line") or ""
    candidate_id = candidate_id_for(source_pointer, summary)
    match_metadata = dict(item.get("match_metadata") or {})
    canonical_task = item.get("canonical_task")
    matched_task_id = match_metadata.get("matched_task_id")
    is_confirmable = match_metadata.get("match_type") == "exact-id-or-link" and bool(matched_task_id)
    candidate = {
        "candidate_id": candidate_id,
        "status": "new",
        "source": source_pointer,
        "raw_summary": item.get("raw_line"),
        "summary": summary,
        "normalized_summary": item.get("normalized_title") or _normalize_summary(summary),
        "suggested_match": canonical_task,
        "match_metadata": match_metadata,
        "matched_task_id": matched_task_id,
        "review_required": not is_confirmable,
    }
    if is_confirmable:
        candidate["confirmable_task_id"] = matched_task_id
    return candidate


def _source_from_file(path: Path) -> tuple[str, dict[str, Any]]:
    content = path.read_text(encoding="utf-8")
    return content, {"type": "file", "path": str(path)}


def _source_from_daily_note(notes_dir: Path, day: date) -> tuple[str, dict[str, Any]]:
    path = notes_dir / f"{day.isoformat()}.md"
    content = path.read_text(encoding="utf-8")
    return content, {"type": "daily_note", "path": str(path), "date": day.isoformat()}


def _match_evidence_lines(content: str, source: dict[str, Any], *, personal: bool = False) -> list[dict[str, Any]]:
    parsed = extract_done_lines(content)
    records = safe_load_task_records(personal)
    catalog = build_task_catalog(records)
    matched = []
    for line in parsed:
        match = match_evidence_line(
            line,
            catalog,
            auto_threshold=FUZZY_EVIDENCE_LINK_THRESHOLD,
            review_threshold=FUZZY_REVIEW_THRESHOLD,
        )
        match["line_number"] = line.get("line_number")
        if (match.get("match_metadata") or {}).get("decision") == "no-match":
            continue
        matched.append(match)
    candidates = []
    for index, item in enumerate(matched, start=1):
        candidate_source = dict(source)
        candidate_source.setdefault("line_number", index)
        candidates.append(_build_candidate_from_match(item, candidate_source))
    return candidates


def scan_content(content: str, source: dict[str, Any], *, personal: bool = False) -> dict[str, Any]:
    candidates = _match_evidence_lines(content, source, personal=personal)
    existing = {
        candidate["candidate_id"]: candidate
        for candidate in project_candidates(include_terminal=True, personal=personal)
    }
    created: list[dict[str, Any]] = []
    already_seen: list[dict[str, Any]] = []

    for candidate in candidates:
        if candidate["candidate_id"] in existing:
            already_seen.append(existing[candidate["candidate_id"]])
            continue
        event = new_event(
            "candidate_seen",
            task_id=candidate["candidate_id"],
            source="completion_candidate_scan",
            metadata={"candidate": candidate},
        )
        append_event(event, path=_candidate_ledger_path(personal))
        created.append(candidate)

    return {
        "created": created,
        "existing": already_seen,
        "totals": {
            "parsed_evidence": len(candidates),
            "created": len(created),
            "existing": len(already_seen),
        },
    }


def scan_file(path: Path, *, personal: bool = False) -> dict[str, Any]:
    content, source = _source_from_file(path)
    return scan_content(content, source, personal=personal)


def scan_daily_note(notes_dir: Path, day: date, *, personal: bool = False) -> dict[str, Any]:
    content, source = _source_from_daily_note(notes_dir, day)
    return scan_content(content, source, personal=personal)


def mark_shown(candidate_id: str, *, personal: bool = False) -> dict[str, Any]:
    candidate = get_candidate(candidate_id, personal=personal)
    if candidate is None:
        return {"ok": False, "error": {"code": "candidate-not-found"}}
    if candidate.get("status") in TERMINAL_STATUSES:
        return {"ok": False, "error": {"code": "candidate-terminal", "status": candidate.get("status")}}
    append_event(
        new_event("candidate_shown", task_id=candidate_id, source="completion_candidate_cli"),
        path=_candidate_ledger_path(personal),
    )
    return {"ok": True, "candidate": get_candidate(candidate_id, personal=personal)}


def reject_candidate(candidate_id: str, *, reason: str | None = None, personal: bool = False) -> dict[str, Any]:
    candidate = get_candidate(candidate_id, personal=personal)
    if candidate is None:
        return {"ok": False, "error": {"code": "candidate-not-found"}}
    if candidate.get("status") in TERMINAL_STATUSES:
        return {"ok": False, "error": {"code": "candidate-terminal", "status": candidate.get("status")}}
    append_event(
        new_event(
            "candidate_rejected",
            task_id=candidate_id,
            source="completion_candidate_cli",
            metadata={"reason": reason},
        ),
        path=_candidate_ledger_path(personal),
    )
    return {"ok": True, "candidate": get_candidate(candidate_id, include_terminal=True, personal=personal)}


def duplicate_candidate(candidate_id: str, *, duplicate_of: str, personal: bool = False) -> dict[str, Any]:
    candidate = get_candidate(candidate_id, personal=personal)
    if candidate is None:
        return {"ok": False, "error": {"code": "candidate-not-found"}}
    if candidate.get("status") in TERMINAL_STATUSES:
        return {"ok": False, "error": {"code": "candidate-terminal", "status": candidate.get("status")}}
    if candidate_id == duplicate_of:
        return {"ok": False, "error": {"code": "self-duplicate-blocked"}}
    if not get_candidate(duplicate_of, include_terminal=True, personal=personal):
        return {"ok": False, "error": {"code": "duplicate-target-not-found"}}
    append_event(
        new_event(
            "candidate_duplicate",
            task_id=candidate_id,
            source="completion_candidate_cli",
            metadata={"duplicate_of": duplicate_of},
        ),
        path=_candidate_ledger_path(personal),
    )
    return {"ok": True, "candidate": get_candidate(candidate_id, include_terminal=True, personal=personal)}


def snooze_candidate(candidate_id: str, *, until: str, personal: bool = False) -> dict[str, Any]:
    try:
        datetime.strptime(until, "%Y-%m-%d")
    except ValueError:
        return {"ok": False, "error": {"code": "invalid-snooze-date"}}
    candidate = get_candidate(candidate_id, personal=personal)
    if candidate is None:
        return {"ok": False, "error": {"code": "candidate-not-found"}}
    if candidate.get("status") in TERMINAL_STATUSES:
        return {"ok": False, "error": {"code": "candidate-terminal", "status": candidate.get("status")}}
    append_event(
        new_event(
            "candidate_snoozed",
            task_id=candidate_id,
            source="completion_candidate_cli",
            metadata={"until": until},
        ),
        path=_candidate_ledger_path(personal),
    )
    return {"ok": True, "candidate": get_candidate(candidate_id, personal=personal)}


def _candidate_task_id(candidate: dict[str, Any], explicit_task_id: str | None) -> tuple[str | None, dict[str, Any] | None]:
    if explicit_task_id:
        return explicit_task_id, None
    match_metadata = candidate.get("match_metadata") or {}
    match_type = match_metadata.get("match_type")
    if match_type != "exact-id-or-link":
        return None, {
            "code": "explicit-task-id-required",
            "message": "Only exact canonical ID/link evidence can be confirmed without --task-id.",
        }
    task_id = candidate.get("confirmable_task_id")
    if not task_id:
        # Compatibility with 108C candidate rows projected before
        # confirmable_task_id existed.
        task_id = candidate.get("matched_task_id")
    if not task_id:
        return None, {"code": "canonical-task-id-missing"}
    return task_id, None


def confirm_candidate(candidate_id: str, *, task_id: str | None = None, personal: bool = False) -> dict[str, Any]:
    candidate = get_candidate(candidate_id, personal=personal)
    if candidate is None:
        return {"ok": False, "error": {"code": "candidate-not-found"}}
    if candidate.get("status") in TERMINAL_STATUSES:
        return {"ok": False, "error": {"code": "candidate-terminal", "status": candidate.get("status")}}

    resolved_task_id, error = _candidate_task_id(candidate, task_id)
    if error:
        return {"ok": False, "error": error}

    def confirmed_events(transition_event: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            new_event(
                "candidate_confirmed",
                task_id=candidate_id,
                source="completion_candidate_cli",
                metadata={
                    "task_id": resolved_task_id,
                    "transition_event_id": transition_event.get("event_id"),
                },
            )
        ]

    result = complete_by_id(
        resolved_task_id,
        personal=personal,
        source="completion_candidate",
        extra_events_factory=confirmed_events,
    )
    if not result.get("ok"):
        append_event(
            new_event(
                "candidate_apply_failed",
                task_id=candidate_id,
                source="completion_candidate_cli",
                metadata={"task_id": resolved_task_id, "error": result.get("error")},
            ),
            path=_candidate_ledger_path(personal),
        )
        return {
            "ok": False,
            "candidate": get_candidate(candidate_id, personal=personal),
            "error": result.get("error"),
        }

    return {
        "ok": True,
        "candidate": get_candidate(candidate_id, include_terminal=True, personal=personal),
        "completion": result,
    }
