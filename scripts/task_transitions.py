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
from task_identity import active_records, load_records
from task_ledger import append_event, ledger_path, new_event
from utils import next_recurrence_date

INLINE_FIELD_RE = re.compile(r"\s+[A-Za-z_][A-Za-z0-9_-]*::")
RECURRENCE_RE = re.compile(r"\brecur::\s*(?!(?:\s|[A-Za-z_][A-Za-z0-9_-]*::))([^\n]+?)(?=\s+[A-Za-z_][A-Za-z0-9_-]*::|\s*[🗓️📅]|$)")
DUE_RE = re.compile(r"(?:🗓️\s*|📅\s*)(\d{4}-\d{2}-\d{2})")
PRIORITY_VALUES = ("urgent", "high", "medium", "low")


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


def _extract_priority(raw_line: str) -> str | None:
    for priority in PRIORITY_VALUES:
        if re.search(rf"#{priority}\b", raw_line, re.IGNORECASE):
            return priority
    return None


def _sanitize_department(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = re.sub(r"[^A-Za-z0-9_]", "", value)
    if not cleaned:
        return None
    return cleaned[:1].upper() + cleaned[1:]


def _archive_dropped_task(record, tasks_file: Path, archive_dir: Path | None = None) -> None:
    env_archive_dir = os.getenv("TASK_TRACKER_ARCHIVE_DIR")
    target_dir = archive_dir or (Path(env_archive_dir).expanduser() if env_archive_dir else tasks_file.parent / "Done Archive")
    target_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now().date()
    archive_file = target_dir / f"ARCHIVE-{today.year}-Q{((today.month - 1) // 3) + 1}.md"
    existing = archive_file.read_text(encoding="utf-8") if archive_file.exists() else f"# Task Archive - {today.year}-Q{((today.month - 1) // 3) + 1}\n"
    entry = f"- [x] ~~{record.title}~~ (dropped) ✅ {today.isoformat()}\n"
    archive_file.write_text(existing.rstrip() + "\n\n## Dropped\n" + entry, encoding="utf-8")


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
    tasks_file, content, records = load_records(personal)
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


def complete_by_id(task_id: str, personal: bool = False, source: str = "user_command") -> dict:
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

    due_value = _extract_due_date(record.raw_line)
    recur_value = _extract_recur_value(record.raw_line) or ""
    daily_log_file = _daily_log_file()
    snapshots = {tasks_file: (True, content)}
    if daily_log_file is not None:
        snapshots[daily_log_file] = _snapshot(daily_log_file)
    ledger_file = ledger_path(tasks_file)
    ledger_snapshot = _snapshot_regular(ledger_file)
    if ledger_snapshot is not None:
        snapshots[ledger_file] = ledger_snapshot

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

    if recur_value:
        try:
            from_date = due_value or datetime.now().date().isoformat()
            next_due = next_recurrence_date(recur_value, from_date)
            next_line = _set_due_date(record.raw_line, next_due)
            new_content = _replace_task_line(content, record.raw_line, next_line)
        except ValueError:
            new_content = _remove_task_line(content, record.raw_line)
    else:
        new_content = _remove_task_line(content, record.raw_line)

    tasks_file.write_text(new_content, encoding="utf-8")
    try:
        append_event(event, path=ledger_path(tasks_file))
    except OSError as exc:
        _restore_snapshots(snapshots)
        return {
            "ok": False,
            "error": {
                "code": "ledger-append-failed",
                "message": f"Ledger append failed after board write; board was restored: {exc}",
            },
        }
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
    ledger_error = _preflight_ledger(tasks_file)
    if ledger_error:
        return ledger_error

    event = new_event(
        "state_transition",
        task_id=task_id,
        source="user_command",
        previous_state="active",
        next_state=next_state,
        reason=reason or f"move-to-{next_state}",
        metadata={"title": record.title, "line_number": record.line_number, **(metadata or {})},
    )
    snapshots = {tasks_file: (True, content)}
    ledger_file = ledger_path(tasks_file)
    ledger_snapshot = _snapshot_regular(ledger_file)
    if ledger_snapshot is not None:
        snapshots[ledger_file] = ledger_snapshot
    if next_state == "delegated":
        delegation_file = delegation.resolve_delegation_file()
        snapshots[delegation_file] = _snapshot(delegation_file)
        delegation.ensure_file(delegation_file)
        delegation.add_item(
            delegation_file,
            record.title,
            str((metadata or {}).get("assignee") or "unknown"),
            str((metadata or {}).get("followup") or datetime.now().date().isoformat()),
            _sanitize_department(record.area),
        )
        tasks_file.write_text(_remove_task_line(content, record.raw_line), encoding="utf-8")
    elif next_state == "backlog":
        priority = (metadata or {}).get("priority") or _extract_priority(record.raw_line) or "low"
        msg = add_parking_item(
            tasks_file,
            record.title,
            dept=_sanitize_department((metadata or {}).get("dept") or record.area),
            priority=priority,
            task_id=task_id,
        )
        if not str(msg).startswith("✅"):
            return {"ok": False, "error": {"code": "backlog-write-failed", "message": msg}}
        tasks_file.write_text(_remove_task_line(tasks_file.read_text(encoding="utf-8"), record.raw_line), encoding="utf-8")
    elif next_state == "deleted":
        today = datetime.now().date()
        env_archive_dir = os.getenv("TASK_TRACKER_ARCHIVE_DIR")
        archive_dir = Path(env_archive_dir).expanduser() if env_archive_dir else tasks_file.parent / "Done Archive"
        archive_file = archive_dir / f"ARCHIVE-{today.year}-Q{((today.month - 1) // 3) + 1}.md"
        snapshots[archive_file] = _snapshot(archive_file)
        _archive_dropped_task(record, tasks_file)
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

    try:
        append_event(event, path=ledger_path(tasks_file))
    except OSError as exc:
        _restore_snapshots(snapshots)
        return {
            "ok": False,
            "error": {
                "code": "ledger-append-failed",
                "message": f"Ledger append failed after state write; task state was restored: {exc}",
            },
        }
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
