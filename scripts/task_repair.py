#!/usr/bin/env python3
"""Safe metadata repair for missing task IDs."""

from __future__ import annotations

import json
from pathlib import Path

from task_identity import audit_identity, load_records, opaque_task_id
from task_ledger import append_event, ledger_path, new_event
from task_records import REPAIR_HINT_RE, TASK_ID_RE


def _insert_task_id(raw_line: str, task_id: str) -> str:
    if "task_id::" in raw_line:
        return raw_line
    return f"{raw_line.rstrip()} task_id::{task_id}"


def _adjacent_repair_hint_index(lines: list[str], idx: int) -> int | None:
    if idx < 0 or idx >= len(lines) or not TASK_ID_RE.search(lines[idx]):
        return None
    adjacent = idx + 1
    if adjacent < len(lines) and REPAIR_HINT_RE.match(lines[adjacent]):
        return adjacent
    return None


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


def _ledger_snapshot(tasks_file: Path) -> tuple[Path, bool, str] | None:
    target = ledger_path(tasks_file)
    if target.exists() and not target.is_file():
        return None
    return target, target.exists(), target.read_text(encoding="utf-8") if target.exists() else ""


def _restore_ledger(snapshot: tuple[Path, bool, str] | None) -> None:
    if snapshot is None:
        return
    target, existed, content = snapshot
    if existed:
        target.write_text(content, encoding="utf-8")
    elif target.exists():
        target.unlink()


def repair_missing_ids(personal: bool = False, apply: bool = False) -> dict:
    try:
        tasks_file, content, records = load_records(personal)
    except FileNotFoundError as exc:
        return {
            "schema_version": "v1",
            "command": "identity-repair",
            "applied": False,
            "blocked": True,
            "blocking_invariants": ["tasks-file-missing"],
            "proposed_repairs": [],
            "error": str(exc),
        }
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

    ledger_snapshot = _ledger_snapshot(tasks_file)
    ledger_error = _preflight_ledger(tasks_file)
    if ledger_error:
        return ledger_error

    lines = content.split("\n")
    event_objects = []
    hint_indexes: set[int] = set()
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
        hint_idx = _adjacent_repair_hint_index(lines, idx)
        if hint_idx is not None:
            hint_indexes.add(hint_idx)
        event = new_event(
            "metadata_repair",
            task_id=task_id,
            source="metadata_repair",
            reason="add-missing-task-id",
            metadata={"line_number": line_number, "title": repair.get("title")},
        )
        event_objects.append(event)

    for idx in sorted(hint_indexes, reverse=True):
        del lines[idx]

    tasks_file.write_text("\n".join(lines), encoding="utf-8")
    events = []
    try:
        for event in event_objects:
            events.append(append_event(event, path=ledger_path(tasks_file)))
    except OSError as exc:
        _restore_content(tasks_file, content)
        _restore_ledger(ledger_snapshot)
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
