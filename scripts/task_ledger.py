#!/usr/bin/env python3
"""Append-only JSONL event ledger for task state and evidence."""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from utils import get_tasks_file


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


def read_events(path: Path | None = None) -> list[dict[str, Any]]:
    target = path or ledger_path()
    if not target.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in target.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events
