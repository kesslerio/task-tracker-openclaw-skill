#!/usr/bin/env python3
"""ID-based task state transitions."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Callable

from log_done import log_task_completed
from task_lines import remove_task_line, replace_task_line
from task_records import active_records, load_records
from task_ledger import append_event, ledger_path, new_event
from utils import next_recurrence_date

INLINE_FIELD_RE = re.compile(r"\s+[A-Za-z_][A-Za-z0-9_-]*::")
RECURRENCE_RE = re.compile(r"\brecur::\s*(?!(?:\s|[A-Za-z_][A-Za-z0-9_-]*::))([^\n]+?)(?=\s+[A-Za-z_][A-Za-z0-9_-]*::|\s*[🗓️📅]|$)")
DUE_RE = re.compile(r"(?:🗓️\s*|📅\s*)(\d{4}-\d{2}-\d{2})")


def _set_due_date(raw_line: str, due_date: str) -> str:
    marker = f"🗓️{due_date}"
    if re.search(r"🗓️\s*\d{4}-\d{2}-\d{2}", raw_line):
        return re.sub(r"🗓️\s*\d{4}-\d{2}-\d{2}", marker, raw_line, count=1)
    if re.search(r"📅\s*\d{4}-\d{2}-\d{2}", raw_line):
        return re.sub(r"📅\s*\d{4}-\d{2}-\d{2}", f"📅 {due_date}", raw_line, count=1)
    match = INLINE_FIELD_RE.search(raw_line)
    if match:
        return f"{raw_line[:match.start()]} {marker}{raw_line[match.start():]}"
    return f"{raw_line.rstrip()} {marker}"


def _extract_due_date(raw_line: str) -> str | None:
    match = DUE_RE.search(raw_line)
    return match.group(1) if match else None


def _extract_recur_value(raw_line: str) -> str | None:
    match = RECURRENCE_RE.search(raw_line)
    return match.group(1).strip() if match else None


def _snapshot(path: Path) -> tuple[bool, str]:
    return (path.exists(), path.read_text(encoding="utf-8") if path.exists() else "")


def _snapshot_regular(path: Path) -> tuple[bool, str] | None:
    if path.exists() and not path.is_file():
        return None
    return _snapshot(path)


def _restore_snapshots(snapshots: dict[Path, tuple[bool, str]]) -> None:
    for path, (existed, content) in snapshots.items():
        if existed:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        elif path.exists():
            path.unlink()


def _restore_after_failure(snapshots: dict[Path, tuple[bool, str]]) -> str | None:
    try:
        _restore_snapshots(snapshots)
    except OSError as exc:
        return str(exc)
    return None


def _daily_log_file() -> Path | None:
    raw_dir = os.getenv("TASK_TRACKER_DONE_LOG_DIR") or os.getenv("TASK_TRACKER_DAILY_NOTES_DIR")
    if not raw_dir:
        return None
    return Path(raw_dir).expanduser() / f"{datetime.now().strftime('%Y-%m-%d')}.md"


def _preflight_ledger(tasks_file: Path) -> dict | None:
    try:
        target = ledger_path(tasks_file)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8"):
            pass
    except OSError as exc:
        return {
            "ok": False,
            "error": {
                "code": "ledger-unwritable",
                "message": f"Ledger is not writable; no task state was changed: {exc}",
            },
        }
    return None


def _resolve_by_id(task_id: str, personal: bool = False):
    try:
        tasks_file, content, records = load_records(personal)
    except FileNotFoundError as exc:
        return None, "", None, {
            "ok": False,
            "error": {
                "code": "tasks-file-missing",
                "message": str(exc),
                "repair_choices": ["check TASK_TRACKER_WORK_FILE/TASK_TRACKER_PERSONAL_FILE"],
            },
        }
    matches = [record for record in active_records(records) if record.canonical_id == task_id]
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


def complete_by_id(
    task_id: str,
    personal: bool = False,
    source: str = "user_command",
    extra_events_factory: Callable[[dict], list[dict]] | None = None,
) -> dict:
    tasks_file, content, record, error = _resolve_by_id(task_id, personal)
    if error:
        return error
    ledger_error = _preflight_ledger(tasks_file)
    if ledger_error:
        return ledger_error

    event = new_event(
        "state_transition",
        task_id=task_id,
        source=source,
        previous_state="active",
        next_state="done",
        reason="explicit-done-by-canonical-id",
        metadata={"title": record.title, "line_number": record.line_number},
    )
    extra_events = extra_events_factory(event) if extra_events_factory else []

    due_value = _extract_due_date(record.raw_line)
    recur_value = _extract_recur_value(record.raw_line) or ""
    if record.line_number is None:
        return {
            "ok": False,
            "error": {
                "code": "task-line-resolution-failed",
                "message": "Resolved task has no stable line number; active board was not changed.",
            },
        }
    if recur_value:
        try:
            from_date = due_value or datetime.now().date().isoformat()
            next_due = next_recurrence_date(recur_value, from_date)
            next_line = _set_due_date(record.raw_line, next_due)
            new_content = replace_task_line(content, record.raw_line, next_line, record.line_number)
        except ValueError as exc:
            return {
                "ok": False,
                "error": {
                    "code": "recurrence-rollover-failed",
                    "message": f"Recurring task could not be rolled forward; active board was not changed: {exc}",
                },
            }
    else:
        new_content = remove_task_line(content, record.raw_line, record.line_number)
    if new_content is None:
        return {
            "ok": False,
            "error": {
                "code": "task-line-resolution-failed",
                "message": "Resolved task line no longer matches the active board; active board was not changed.",
            },
        }

    daily_log_file = _daily_log_file()
    snapshots = {tasks_file: (True, content)}
    if daily_log_file is not None:
        daily_snapshot = _snapshot_regular(daily_log_file)
        if daily_snapshot is not None:
            snapshots[daily_log_file] = daily_snapshot
    ledger_file = ledger_path(tasks_file)
    ledger_snapshot = _snapshot_regular(ledger_file)
    if ledger_snapshot is not None:
        snapshots[ledger_file] = ledger_snapshot

    write_stage = "completion-log"
    try:
        logged = log_task_completed(
            title=record.title,
            section=record.section,
            area=record.area,
            due=due_value,
            recur=recur_value or None,
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

        write_stage = "board-write"
        tasks_file.write_text(new_content, encoding="utf-8")
        write_stage = "ledger-append"
        append_event(event, path=ledger_path(tasks_file))
        for extra_event in extra_events:
            append_event(extra_event, path=ledger_path(tasks_file))
    except OSError as exc:
        restore_error = _restore_after_failure(snapshots)
        code = "ledger-append-failed" if write_stage == "ledger-append" else "task-state-write-failed"
        error = {
            "code": code,
            "message": f"Task completion write failed; snapshots were restored: {exc}",
        }
        if restore_error:
            error["restore_error"] = restore_error
        return {
            "ok": False,
            "error": error,
        }
    return {
        "ok": True,
        "task_id": task_id,
        "title": record.title,
        "event": event,
        "extra_events": extra_events,
    }


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
