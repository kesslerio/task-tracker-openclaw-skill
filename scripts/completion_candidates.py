#!/usr/bin/env python3
"""Completion candidate inbox backed by the task ledger."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from task_ledger import append_event, ledger_path, new_event, read_events
from task_transitions import complete_by_id
from utils import get_tasks_file


def dedupe_key(source_type: str, source_pointer: str, summary: str, task_id: str | None = None) -> str:
    material = "\0".join([source_type, source_pointer, summary.strip().casefold(), task_id or ""])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def create_candidate(
    *,
    source_type: str,
    source_pointer: str,
    summary: str,
    matched_task_id: str | None = None,
    confidence: float = 0.0,
    actor: str = "task-tracker",
    personal: bool = False,
) -> dict[str, Any]:
    tasks_file, _ = get_tasks_file(personal)
    event_path = ledger_path(tasks_file)
    key = dedupe_key(source_type, source_pointer, summary, matched_task_id)
    try:
        events = read_events(event_path)
    except OSError as exc:
        return {
            "created": False,
            "ok": False,
            "error": {
                "code": "candidate-ledger-read-failed",
                "message": f"Completion candidate ledger could not be read; no candidate was created: {exc}",
            },
        }
    for event in events:
        evidence = event.get("evidence") or {}
        if evidence.get("dedupe_key") == key and event.get("event_type") == "completion_candidate":
            return {"created": False, "candidate": event}

    evidence = {
        "source_type": source_type,
        "source_pointer": source_pointer,
        "source_timestamp": datetime.now(timezone.utc).isoformat(),
        "redacted_summary": summary.strip(),
        "matched_task_id": matched_task_id,
        "confidence": confidence,
        "actor": actor,
        "decision": "new",
        "dedupe_key": key,
    }
    event = new_event(
        "completion_candidate",
        task_id=matched_task_id,
        actor=actor,
        source=source_type,
        next_state="candidate",
        evidence=evidence,
        metadata={"candidate_status": "new"},
    )
    try:
        append_event(event, path=event_path)
    except OSError as exc:
        return {
            "created": False,
            "ok": False,
            "error": {
                "code": "candidate-ledger-append-failed",
                "message": f"Completion candidate ledger append failed; no candidate was created: {exc}",
            },
        }
    return {"created": True, "candidate": event}


def list_candidates(personal: bool = False) -> list[dict[str, Any]] | dict[str, Any]:
    tasks_file, _ = get_tasks_file(personal)
    event_path = ledger_path(tasks_file)
    statuses: dict[str, str] = {}
    candidates: dict[str, dict[str, Any]] = {}
    try:
        events = read_events(event_path)
    except OSError as exc:
        return {
            "ok": False,
            "error": {
                "code": "candidate-ledger-read-failed",
                "message": f"Completion candidate ledger could not be read: {exc}",
            },
        }
    for event in events:
        evidence = event.get("evidence") or {}
        key = evidence.get("dedupe_key")
        if not key:
            continue
        if event.get("event_type") == "completion_candidate":
            candidates[key] = event
            statuses.setdefault(key, "new")
        elif event.get("event_type") == "completion_candidate_decision":
            statuses[key] = event.get("metadata", {}).get("candidate_status", "decided")
    return [
        {**event, "candidate_status": statuses.get(key, "new")}
        for key, event in candidates.items()
        if statuses.get(key, "new") in {"new", "shown", "snoozed", "apply_failed"}
    ]


def decide_candidate(
    dedupe_key_value: str,
    decision: str,
    task_id: str | None = None,
    personal: bool = False,
) -> dict[str, Any]:
    tasks_file, _ = get_tasks_file(personal)
    event_path = ledger_path(tasks_file)
    listed = list_candidates(personal=personal)
    if isinstance(listed, dict) and not listed.get("ok", True):
        return listed
    candidates = {
        (event.get("evidence") or {}).get("dedupe_key"): event
        for event in listed
    }
    candidate = candidates.get(dedupe_key_value)
    if not candidate:
        return {"ok": False, "error": {"code": "candidate-not-found"}}

    matched_task_id = task_id or (candidate.get("evidence") or {}).get("matched_task_id")
    if decision == "confirmed" and not matched_task_id:
        return {"ok": False, "error": {"code": "candidate-missing-task-id"}}
    event = new_event(
        "completion_candidate_decision",
        task_id=matched_task_id,
        source="candidate_inbox",
        next_state="done" if decision == "confirmed" else "candidate",
        evidence=candidate.get("evidence"),
        metadata={"candidate_status": decision, "dedupe_key": dedupe_key_value},
    )
    try:
        append_event(event, path=event_path)
    except OSError as exc:
        return {
            "ok": False,
            "error": {
                "code": "candidate-decision-ledger-failed",
                "message": f"Candidate decision ledger append failed; task was not changed: {exc}",
            },
        }
    if decision == "confirmed":
        applied = complete_by_id(matched_task_id, personal=personal, source="completion_candidate")
        if not applied.get("ok"):
            failure_event = new_event(
                "completion_candidate_decision",
                task_id=matched_task_id,
                source="candidate_inbox",
                next_state="candidate",
                evidence=candidate.get("evidence"),
                metadata={"candidate_status": "apply_failed", "dedupe_key": dedupe_key_value},
            )
            try:
                append_event(failure_event, path=event_path)
            except OSError:
                pass
            return applied
    return {"ok": True, "decision": decision, "event": event}


def print_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))
