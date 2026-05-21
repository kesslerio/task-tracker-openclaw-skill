#!/usr/bin/env python3
"""Append-only JSONL event ledger for task state and evidence."""

from __future__ import annotations

import json
import os
import warnings
from dataclasses import dataclass
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from utils import get_tasks_file


@dataclass(frozen=True)
class MalformedLedgerLine:
    path: str
    line_number: int
    message: str
    raw_line: str


class MalformedLedgerError(ValueError):
    def __init__(self, malformed: list[MalformedLedgerLine]):
        self.malformed = malformed
        summary = ", ".join(f"{item.path}:{item.line_number}: {item.message}" for item in malformed)
        super().__init__(f"Malformed ledger JSONL line(s): {summary}")


def ledger_path(tasks_file: Path | None = None) -> Path:
    raw = os.getenv("TASK_TRACKER_LEDGER_FILE")
    if raw:
        return Path(raw).expanduser()
    if tasks_file is None:
        tasks_file, _ = get_tasks_file(False)
    return tasks_file.with_suffix(tasks_file.suffix + ".events.jsonl")


def new_event(
    event_type: str,
    *,
    task_id: str | None = None,
    actor: str = "task-tracker",
    source: str = "cli",
    previous_state: str | None = None,
    next_state: str | None = None,
    reason: str | None = None,
    evidence: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "event_id": f"evt_{uuid.uuid4().hex}",
        "event_type": event_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "actor": actor,
        "source": source,
        "task_id": task_id,
        "previous_state": previous_state,
        "next_state": next_state,
        "reason": reason,
        "evidence": evidence,
        "metadata": metadata or {},
    }


def append_event(event: dict[str, Any], path: Path | None = None) -> dict[str, Any]:
    target = path or ledger_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(event, ensure_ascii=False, sort_keys=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(rendered + "\n")
    return event


def read_events_report(path: Path | None = None) -> tuple[list[dict[str, Any]], list[MalformedLedgerLine]]:
    target = path or ledger_path()
    if not target.exists():
        return [], []
    events: list[dict[str, Any]] = []
    malformed: list[MalformedLedgerLine] = []
    for line_number, line in enumerate(target.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError as exc:
            malformed.append(
                MalformedLedgerLine(
                    path=str(target),
                    line_number=line_number,
                    message=str(exc),
                    raw_line=line,
                )
            )
    return events, malformed


def read_events(path: Path | None = None, *, strict: bool = False) -> list[dict[str, Any]]:
    events, malformed = read_events_report(path)
    if malformed:
        if strict:
            raise MalformedLedgerError(malformed)
        warnings.warn(
            f"Ignored {len(malformed)} malformed ledger JSONL line(s); "
            "use read_events_report() or strict=True for details.",
            RuntimeWarning,
            stacklevel=2,
        )
    return events
