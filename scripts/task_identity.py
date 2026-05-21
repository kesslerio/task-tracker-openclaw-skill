#!/usr/bin/env python3
"""Canonical task identity audit and export helpers."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from utils import get_tasks_file, parse_tasks

TASK_ID_RE = re.compile(r"\btask_id::\s*([A-Za-z0-9._:-]*[A-Za-z0-9._-])(?=\s|$|[),.;!?])")
LEGACY_ID_RE = re.compile(r"\bid::\s*([A-Za-z0-9._:-]*[A-Za-z0-9._-])(?=\s|$|[),.;!?])")


@dataclass(frozen=True)
class IdentityRecord:
    task_id: str | None
    legacy_id: str | None
    title: str
    done: bool
    section: str | None
    area: str | None
    line_number: int | None
    raw_line: str
    fallback_id: str

    @property
    def canonical_id(self) -> str | None:
        return self.task_id or self.legacy_id

    @property
    def identity_source(self) -> str:
        if self.task_id:
            return "task_id"
        if self.legacy_id:
            return "legacy_id"
        return "fallback"


def fallback_id_for(raw_line: str, line_number: int | None) -> str:
    material = f"{line_number or 0}\0{raw_line}".encode("utf-8")
    return f"fallback-{hashlib.sha1(material).hexdigest()[:12]}"


def opaque_task_id(raw_line: str, line_number: int | None) -> str:
    material = f"task-v1\0{line_number or 0}\0{raw_line}".encode("utf-8")
    return f"tsk_{hashlib.sha256(material).hexdigest()[:16]}"


def _parking_lot_line_numbers(content: str) -> set[int]:
    parking_lines: set[int] = set()
    in_parking_lot = False
    for idx, line in enumerate(content.splitlines(), start=1):
        if re.match(r"##\s+(?:🅿️\s*)?Parking Lot\b", line, re.IGNORECASE):
            in_parking_lot = True
            continue
        if in_parking_lot and re.match(r"##\s+", line):
            in_parking_lot = False
        if in_parking_lot:
            parking_lines.add(idx)
    return parking_lines


def task_records(content: str, personal: bool = False, fmt: str = "obsidian") -> list[IdentityRecord]:
    parsed = parse_tasks(content, personal=personal, format=fmt)
    parking_lot_lines = _parking_lot_line_numbers(content)
    records: list[IdentityRecord] = []
    for task in parsed.get("all", []):
        raw_line = str(task.get("raw_line") or "")
        line_number = task.get("line_number")
        line_number_int = int(line_number) if line_number else None
        section = task.get("section")
        if line_number_int in parking_lot_lines:
            section = "parking_lot"
        records.append(
            IdentityRecord(
                task_id=task.get("task_id"),
                legacy_id=task.get("legacy_id"),
                title=str(task.get("title") or ""),
                done=bool(task.get("done")),
                section=section,
                area=task.get("area") or task.get("department"),
                line_number=line_number_int,
                raw_line=raw_line,
                fallback_id=fallback_id_for(raw_line, line_number_int),
            )
        )
    return records


def load_records(personal: bool = False) -> tuple[Path, str, list[IdentityRecord]]:
    tasks_file, fmt = get_tasks_file(personal)
    if not tasks_file.exists():
        raise FileNotFoundError(f"Tasks file not found: {tasks_file}")
    content = tasks_file.read_text(encoding="utf-8")
    return tasks_file, content, task_records(content, personal=personal, fmt=fmt)


def active_records(records: Iterable[IdentityRecord]) -> list[IdentityRecord]:
    inactive_sections = {"backlog", "parking_lot"}
    return [record for record in records if not record.done and record.section not in inactive_sections]


def export_active(records: Iterable[IdentityRecord]) -> list[dict]:
    exported = []
    for record in active_records(records):
        exported.append(
            {
                "task_id": record.canonical_id,
                "identity_source": record.identity_source,
                "fallback_id": record.fallback_id,
                "title": record.title,
                "state": "active",
                "section": record.section,
                "area": record.area,
                "line_number": record.line_number,
                "raw_line": record.raw_line,
                "missing_task_id": record.task_id is None,
                "fallback_only": record.canonical_id is None,
                "checked_candidate": record.done,
            }
        )
    return exported


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
