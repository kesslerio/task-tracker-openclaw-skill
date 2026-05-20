#!/usr/bin/env python3
"""ID-based task state transitions."""

from __future__ import annotations

import json
import re
from datetime import datetime

from log_done import log_task_completed
from task_identity import load_records
from task_ledger import append_event, new_event
from utils import get_tasks_file, next_recurrence_date


def _remove_task_line(content: str, raw_line: str) -> str:
    lines = content.split("\n")
    try:
        target_index = lines.index(raw_line)
    except ValueError:
        return content
    target_indent = len(raw_line) - len(raw_line.lstrip(" "))
    remove_until = target_index + 1
    while remove_until < len(lines):
        line = lines[remove_until]
        if not line.strip():
            break
        indent = len(line) - len(line.lstrip(" "))
        if indent > target_indent:
            remove_until += 1
            continue
        break
    return "\n".join(lines[:target_index] + lines[remove_until:])


def _resolve_by_id(task_id: str, personal: bool = False):
    tasks_file, content, records = load_records(personal)
    matches = [record for record in records if not record.done and record.canonical_id == task_id]
    if len(matches) != 1:
        return tasks_file, content, None, {
            "ok": False,
            "error": {
                "code": "canonical-id-resolution-failed",
                "message": f"Expected exactly one active task for canonical ID {task_id}; found {len(matches)}.",
                "repair_choices": ["identity-audit", "identity-repair --dry-run"],
            },
        }
    return tasks_file, content, matches[0], None


def complete_by_id(task_id: str, personal: bool = False, source: str = "user_command") -> dict:
    tasks_file, content, record, error = _resolve_by_id(task_id, personal)
    if error:
        return error

    event = new_event(
        "state_transition",
        task_id=task_id,
        source=source,
        previous_state="active",
        next_state="done",
        reason="explicit-done-by-canonical-id",
        metadata={"title": record.title, "line_number": record.line_number},
    )

    logged = log_task_completed(
        title=record.title,
        section=record.section,
        area=record.area,
        context={"task_id": task_id, "source": source},
    )
    if not logged:
        return {
            "ok": False,
            "error": {
                "code": "completion-log-failed",
                "message": "Daily-note completion log failed; active board was not changed.",
            },
        }

    append_event(event)
    new_content = _remove_task_line(content, record.raw_line)
    tasks_file.write_text(new_content, encoding="utf-8")
    return {"ok": True, "task_id": task_id, "title": record.title, "event": event}


def transition_by_id(
    task_id: str,
    next_state: str,
    *,
    personal: bool = False,
    reason: str | None = None,
    metadata: dict | None = None,
) -> dict:
    tasks_file, content, record, error = _resolve_by_id(task_id, personal)
    if error:
        return error

    event = new_event(
        "state_transition",
        task_id=task_id,
        source="user_command",
        previous_state="active",
        next_state=next_state,
        reason=reason or f"move-to-{next_state}",
        metadata={"title": record.title, "line_number": record.line_number, **(metadata or {})},
    )
    append_event(event)

    if next_state in {"deleted", "frozen", "backlog", "delegated"}:
        tasks_file.write_text(_remove_task_line(content, record.raw_line), encoding="utf-8")
    elif next_state == "active":
        replacement = record.raw_line
        if metadata and metadata.get("due"):
            if re.search(r"🗓️\d{4}-\d{2}-\d{2}", replacement):
                replacement = re.sub(r"🗓️\d{4}-\d{2}-\d{2}", f"🗓️{metadata['due']}", replacement, count=1)
            else:
                replacement = f"{replacement.rstrip()} 🗓️{metadata['due']}"
            tasks_file.write_text(content.replace(record.raw_line, replacement, 1), encoding="utf-8")

    return {"ok": True, "task_id": task_id, "title": record.title, "event": event}


def block_unsafe_query(query: str) -> dict:
    return {
        "ok": False,
        "error": {
            "code": "unsafe-title-mutation-blocked",
            "message": "Active task mutations require a canonical task_id. Title/list-position matching is blocked.",
            "query": query,
            "repair_choices": ["identity-audit", "identity-repair --dry-run", "rerun command with task_id"],
        },
    }


def print_result(result: dict) -> None:
    print(json.dumps(result, indent=2, sort_keys=True))
