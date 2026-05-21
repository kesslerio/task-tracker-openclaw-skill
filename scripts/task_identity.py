#!/usr/bin/env python3
"""Canonical task identity audit and export helpers."""

from __future__ import annotations

import json
from typing import Iterable

from task_records import (
    IdentityRecord,
    TASK_ID_RE,
    active_records,
    export_active,
    load_records,
    opaque_task_id,
    task_records,
)
from utils import get_tasks_file


def audit_identity(records: Iterable[IdentityRecord]) -> dict:
    active = active_records(records)
    by_id: dict[str, list[IdentityRecord]] = {}
    by_title: dict[str, list[IdentityRecord]] = {}
    missing: list[IdentityRecord] = []
    proposed_repairs: list[dict] = []
    malformed: list[dict] = []

    for record in active:
        if record.canonical_id:
            by_id.setdefault(record.canonical_id, []).append(record)
        else:
            missing.append(record)
            proposed_repairs.append(
                {
                    "line_number": record.line_number,
                    "title": record.title,
                    "task_id": opaque_task_id(record.raw_line, record.line_number),
                    "raw_line": record.raw_line,
                }
            )
        by_title.setdefault(record.title.casefold(), []).append(record)
        if "task_id::" in record.raw_line and not TASK_ID_RE.search(record.raw_line):
            malformed.append({"line_number": record.line_number, "title": record.title})

    duplicate_ids = [
        {
            "task_id": task_id,
            "items": [
                {"line_number": r.line_number, "title": r.title, "raw_line": r.raw_line}
                for r in group
            ],
        }
        for task_id, group in sorted(by_id.items())
        if len(group) > 1
    ]
    ambiguous_titles = [
        {
            "title": group[0].title,
            "items": [
                {"line_number": r.line_number, "task_id": r.canonical_id, "raw_line": r.raw_line}
                for r in group
            ],
        }
        for _, group in sorted(by_title.items())
        if len(group) > 1
    ]

    blocking = []
    if duplicate_ids:
        blocking.append("duplicate-task-id")
    if malformed:
        blocking.append("malformed-task-id")

    return {
        "missing_task_ids": [
            {"line_number": r.line_number, "title": r.title, "raw_line": r.raw_line}
            for r in missing
        ],
        "duplicate_task_ids": duplicate_ids,
        "ambiguous_titles": ambiguous_titles,
        "malformed_task_ids": malformed,
        "proposed_repairs": proposed_repairs,
        "blocking_invariants": blocking,
        "totals": {
            "active": len(active),
            "missing_task_ids": len(missing),
            "duplicate_task_ids": len(duplicate_ids),
            "ambiguous_titles": len(ambiguous_titles),
            "malformed_task_ids": len(malformed),
        },
    }


def audit_payload(personal: bool = False) -> dict:
    tasks_file, _ = get_tasks_file(personal)
    try:
        _, _, records = load_records(personal)
    except FileNotFoundError as exc:
        return {
            "schema_version": "v1",
            "command": "identity-audit",
            "tasks_file": str(tasks_file),
            "active": [],
            "audit": {
                "missing_task_ids": [],
                "duplicate_task_ids": [],
                "ambiguous_titles": [],
                "malformed_task_ids": [],
                "proposed_repairs": [],
                "blocking_invariants": ["tasks-file-missing"],
                "totals": {
                    "active": 0,
                    "missing_task_ids": 0,
                    "duplicate_task_ids": 0,
                    "ambiguous_titles": 0,
                    "malformed_task_ids": 0,
                },
            },
            "error": str(exc),
        }
    return {
        "schema_version": "v1",
        "command": "identity-audit",
        "tasks_file": str(tasks_file),
        "active": export_active(records),
        "audit": audit_identity(records),
    }


def print_json(payload: dict) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))
