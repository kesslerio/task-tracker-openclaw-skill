#!/usr/bin/env python3
"""Safe metadata repair for missing task IDs."""

from __future__ import annotations

import json
from pathlib import Path

from task_identity import audit_identity, load_records, opaque_task_id
from task_ledger import append_event, ledger_path, new_event


def _insert_task_id(raw_line: str, task_id: str) -> str:
    if "task_id::" in raw_line:
        return raw_line
    return f"{raw_line.rstrip()} task_id::{task_id}"


def _preflight_ledger(tasks_file: Path) -> dict | None:
    try:
        target = ledger_path(tasks_file)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8"):
            pass
    except OSError as exc:
        return {
            "schema_version": "v1",
            "command": "identity-repair",
            "applied": False,
            "blocked": True,
            "blocking_invariants": ["ledger-unwritable"],
            "error": f"Ledger is not writable; no task metadata was changed: {exc}",
        }
    return None


def _restore_content(tasks_file: Path, content: str) -> None:
    tasks_file.write_text(content, encoding="utf-8")


def repair_missing_ids(personal: bool = False, apply: bool = False) -> dict:
    tasks_file, content, records = load_records(personal)
    audit = audit_identity(records)
    proposed = audit.get("proposed_repairs", [])
    blocked = list(audit.get("blocking_invariants", []))
    if proposed and audit.get("ambiguous_titles"):
        blocked.append("ambiguous-title")

    if blocked:
        return {
            "schema_version": "v1",
            "command": "identity-repair",
            "applied": False,
            "blocked": True,
            "blocking_invariants": sorted(set(blocked)),
            "proposed_repairs": audit.get("proposed_repairs", []),
        }

    if not apply:
        return {
            "schema_version": "v1",
            "command": "identity-repair",
            "applied": False,
            "blocked": False,
            "proposed_repairs": proposed,
        }

    if not proposed:
        return {
            "schema_version": "v1",
            "command": "identity-repair",
            "applied": True,
            "changed": 0,
            "events": [],
        }

    ledger_error = _preflight_ledger(tasks_file)
    if ledger_error:
        return ledger_error

    lines = content.split("\n")
    event_objects = []
    for repair in proposed:
        line_number = int(repair["line_number"])
        idx = line_number - 1
        if idx < 0 or idx >= len(lines) or lines[idx] != repair["raw_line"]:
            return {
                "schema_version": "v1",
                "command": "identity-repair",
                "applied": False,
                "blocked": True,
                "blocking_invariants": ["line-changed-before-repair"],
                "failed_line": line_number,
            }
        task_id = repair.get("task_id") or opaque_task_id(repair["raw_line"], line_number)
        lines[idx] = _insert_task_id(lines[idx], task_id)
        event = new_event(
            "metadata_repair",
            task_id=task_id,
            source="metadata_repair",
            reason="add-missing-task-id",
            metadata={"line_number": line_number, "title": repair.get("title")},
        )
        event_objects.append(event)

    tasks_file.write_text("\n".join(lines), encoding="utf-8")
    events = []
    try:
        for event in event_objects:
            events.append(append_event(event, path=ledger_path(tasks_file)))
    except OSError as exc:
        _restore_content(tasks_file, content)
        return {
            "schema_version": "v1",
            "command": "identity-repair",
            "applied": False,
            "blocked": True,
            "blocking_invariants": ["ledger-append-failed"],
            "error": f"Ledger append failed after metadata write; task metadata was restored: {exc}",
        }
    return {
        "schema_version": "v1",
        "command": "identity-repair",
        "applied": True,
        "changed": len(proposed),
        "events": events,
    }


def print_json(payload: dict) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))
