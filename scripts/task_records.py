#!/usr/bin/env python3
"""Shared canonical task record helpers."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from utils import get_tasks_file, parse_tasks

TASK_ID_RE = re.compile(r"\btask_id::\s*([A-Za-z0-9._:-]*[A-Za-z0-9._-])(?=\s|$|[),.;!?])")
LEGACY_ID_RE = re.compile(r"\bid::\s*([A-Za-z0-9._:-]*[A-Za-z0-9._-])(?=\s|$|[),.;!?])")


@dataclass(frozen=True)
class TaskRecord:
    task_id: str | None
    legacy_id: str | None
    title: str
    done: bool
    section: str | None
    area: str | None
    line_number: int | None
    raw_line: str
    fallback_id: str
    department: str | None = None
    priority: str | None = None
    due: str | None = None
    owner: str | None = None
    goal: str | None = None
    recur: str | None = None
    estimate: str | None = None
    parent_objective: str | None = None
    is_objective: bool = False

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

    @property
    def missing_task_id(self) -> bool:
        return self.task_id is None

    @property
    def fallback_only(self) -> bool:
        return self.canonical_id is None


IdentityRecord = TaskRecord


def fallback_id_for(raw_line: str, line_number: int | None) -> str:
    material = f"{line_number or 0}\0{raw_line}".encode("utf-8")
    return f"fallback-{hashlib.sha1(material).hexdigest()[:12]}"


def opaque_task_id(raw_line: str, line_number: int | None) -> str:
    material = f"task-v1\0{line_number or 0}\0{raw_line}".encode("utf-8")
    return f"tsk_{hashlib.sha256(material).hexdigest()[:16]}"


def parking_lot_line_numbers(content: str) -> set[int]:
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


def task_records(content: str, personal: bool = False, fmt: str = "obsidian") -> list[TaskRecord]:
    parsed = parse_tasks(content, personal=personal, format=fmt)
    parking_lot_lines = parking_lot_line_numbers(content)
    records: list[TaskRecord] = []
    for task in parsed.get("all", []):
        raw_line = str(task.get("raw_line") or "")
        line_number = task.get("line_number")
        line_number_int = int(line_number) if line_number else None
        section = task.get("section")
        if line_number_int in parking_lot_lines:
            section = "parking_lot"
        area = task.get("area") or task.get("department")
        records.append(
            TaskRecord(
                task_id=task.get("task_id"),
                legacy_id=task.get("legacy_id"),
                title=str(task.get("title") or ""),
                done=bool(task.get("done")),
                section=section,
                area=area,
                line_number=line_number_int,
                raw_line=raw_line,
                fallback_id=fallback_id_for(raw_line, line_number_int),
                department=task.get("department"),
                priority=task.get("priority"),
                due=task.get("due"),
                owner=task.get("owner"),
                goal=task.get("goal"),
                recur=task.get("recur"),
                estimate=task.get("estimate"),
                parent_objective=task.get("parent_objective"),
                is_objective=bool(task.get("is_objective")),
            )
        )
    return records


def load_records(personal: bool = False) -> tuple[Path, str, list[TaskRecord]]:
    tasks_file, fmt = get_tasks_file(personal)
    if not tasks_file.exists():
        raise FileNotFoundError(f"Tasks file not found: {tasks_file}")
    content = tasks_file.read_text(encoding="utf-8")
    return tasks_file, content, task_records(content, personal=personal, fmt=fmt)


# Sections excluded from the active set: a parked or backlogged task is not
# active work. The single source of truth shared by active_records() and the
# Layer-2 add-gate's "does this destination count as active?" check.
INACTIVE_SECTIONS: frozenset[str] = frozenset({"backlog", "parking_lot"})


def active_records(records: Iterable[TaskRecord]) -> list[TaskRecord]:
    return [
        record
        for record in records
        if not record.done and record.section not in INACTIVE_SECTIONS
    ]


def record_to_task_dict(record: TaskRecord) -> dict:
    return {
        "task_id": record.canonical_id,
        "identity_source": record.identity_source,
        "fallback_id": record.fallback_id,
        "missing_task_id": record.missing_task_id,
        "fallback_only": record.fallback_only,
        "title": record.title,
        "done": record.done,
        "section": record.section,
        "area": record.area or record.department or "Uncategorized",
        "department": record.department,
        "priority": record.priority,
        "due": record.due,
        "owner": record.owner,
        "goal": record.goal,
        "recur": record.recur,
        "parent_objective": record.parent_objective,
        "is_objective": record.is_objective,
        "line_number": record.line_number,
        "raw_line": record.raw_line,
    }


def export_active(records: Iterable[TaskRecord]) -> list[dict]:
    exported = []
    for record in active_records(records):
        row = record_to_task_dict(record)
        row["state"] = "active"
        row["checked_candidate"] = record.done
        exported.append(row)
    return exported
