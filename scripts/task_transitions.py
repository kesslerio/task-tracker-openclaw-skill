#!/usr/bin/env python3
"""ID-based task state transitions."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

import delegation
from log_done import log_task_completed
from parking_lot import add_item as add_parking_item
from task_identity import load_records
from task_ledger import append_event, ledger_path, new_event
from utils import next_recurrence_date

INLINE_FIELD_RE = re.compile(r"\s+[A-Za-z_][A-Za-z0-9_-]*::")


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
            lookahead = remove_until + 1
            while lookahead < len(lines) and not lines[lookahead].strip():
                lookahead += 1
            if lookahead < len(lines):
                next_indent = len(lines[lookahead]) - len(lines[lookahead].lstrip(" "))
                if next_indent > target_indent:
                    remove_until += 1
                    continue
            break
        indent = len(line) - len(line.lstrip(" "))
        if indent > target_indent:
            remove_until += 1
            continue
        break
    return "\n".join(lines[:target_index] + lines[remove_until:])


def _replace_task_line(content: str, raw_line: str, replacement: str) -> str:
    return content.replace(raw_line, replacement, 1)


def _set_due_date(raw_line: str, due_date: str) -> str:
    marker = f"🗓️{due_date}"
    if re.search(r"🗓️\d{4}-\d{2}-\d{2}", raw_line):
        return re.sub(r"🗓️\d{4}-\d{2}-\d{2}", marker, raw_line, count=1)
    match = INLINE_FIELD_RE.search(raw_line)
    if match:
        return f"{raw_line[:match.start()]} {marker}{raw_line[match.start():]}"
    return f"{raw_line.rstrip()} {marker}"


def _archive_dropped_task(record, archive_dir: Path | None = None) -> None:
    target_dir = archive_dir or Path(os.getenv("TASK_TRACKER_ARCHIVE_DIR", "Done Archive"))
    target_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now().date()
    archive_file = target_dir / f"ARCHIVE-{today.year}-Q{((today.month - 1) // 3) + 1}.md"
    existing = archive_file.read_text(encoding="utf-8") if archive_file.exists() else f"# Task Archive - {today.year}-Q{((today.month - 1) // 3) + 1}\n"
    entry = f"- [x] ~~{record.title}~~ (dropped) ✅ {today.isoformat()}\n"
    archive_file.write_text(existing.rstrip() + "\n\n## Dropped\n" + entry, encoding="utf-8")


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

    recur_value = ""
    recur_match = re.search(r"\brecur::\s*([^\s]+)", record.raw_line)
    if recur_match:
        recur_value = recur_match.group(1).strip()

    if recur_value:
        try:
            from_date_match = re.search(r"🗓️(\d{4}-\d{2}-\d{2})", record.raw_line)
            from_date = from_date_match.group(1) if from_date_match else datetime.now().date().isoformat()
            next_due = next_recurrence_date(recur_value, from_date)
            next_line = _set_due_date(record.raw_line, next_due)
            new_content = _replace_task_line(content, record.raw_line, next_line)
        except ValueError:
            new_content = _remove_task_line(content, record.raw_line)
    else:
        new_content = _remove_task_line(content, record.raw_line)

    tasks_file.write_text(new_content, encoding="utf-8")
    append_event(event, path=ledger_path(tasks_file))
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
    if next_state == "delegated":
        delegation_file = delegation.resolve_delegation_file()
        delegation.ensure_file(delegation_file)
        delegation.add_item(
            delegation_file,
            record.title,
            str((metadata or {}).get("assignee") or "unknown"),
            str((metadata or {}).get("followup") or datetime.now().date().isoformat()),
            record.area,
        )
        tasks_file.write_text(_remove_task_line(content, record.raw_line), encoding="utf-8")
    elif next_state == "backlog":
        msg = add_parking_item(
            tasks_file,
            record.title,
            dept=(metadata or {}).get("dept") or record.area,
            priority=(metadata or {}).get("priority") or "low",
        )
        if not str(msg).startswith("✅"):
            return {"ok": False, "error": {"code": "backlog-write-failed", "message": msg}}
        tasks_file.write_text(_remove_task_line(tasks_file.read_text(encoding="utf-8"), record.raw_line), encoding="utf-8")
    elif next_state == "deleted":
        _archive_dropped_task(record)
        tasks_file.write_text(_remove_task_line(content, record.raw_line), encoding="utf-8")
    elif next_state == "frozen":
        tasks_file.write_text(_remove_task_line(content, record.raw_line), encoding="utf-8")
    elif next_state == "active":
        replacement = record.raw_line
        if metadata and metadata.get("paused_at") and "paused::" not in replacement:
            replacement = f"{replacement.rstrip()} paused::{metadata['paused_at']}"
        if metadata and metadata.get("due"):
            replacement = _set_due_date(replacement, str(metadata["due"]))
            if re.search(r"\bpause_until::\d{4}-\d{2}-\d{2}", replacement):
                replacement = re.sub(
                    r"\bpause_until::\d{4}-\d{2}-\d{2}",
                    f"pause_until::{metadata['due']}",
                    replacement,
                    count=1,
                )
            else:
                replacement = f"{replacement.rstrip()} pause_until::{metadata['due']}"
        if replacement != record.raw_line:
            tasks_file.write_text(content.replace(record.raw_line, replacement, 1), encoding="utf-8")

    append_event(event, path=ledger_path(tasks_file))
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
