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
from task_lines import line_index, remove_task_line, replace_task_line
from task_records import active_records, load_records
from task_ledger import append_event, ledger_path, new_event
from utils import next_recurrence_date, _atomic_write

INLINE_FIELD_RE = re.compile(r"\s+[A-Za-z_][A-Za-z0-9_-]*::")
RECURRENCE_RE = re.compile(r"\brecur::\s*(?!(?:\s|[A-Za-z_][A-Za-z0-9_-]*::))([^\n]+?)(?=\s+[A-Za-z_][A-Za-z0-9_-]*::|\s*[🗓️📅]|$)")
DUE_RE = re.compile(r"(?:🗓️\s*|📅\s*)(\d{4}-\d{2}-\d{2})")
# An EOD ``carry`` stamps this inline marker so the morning standup can surface the
# task as carried-from-yesterday. It is plain inline metadata (KTD-7): no new board
# status field, the task stays ACTIVE.
CARRIED_RE = re.compile(r"\s*carried::\s*\d{4}-\d{2}-\d{2}")


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


def _set_carried_marker(raw_line: str, carried_date: str) -> str:
    """Stamp (or refresh) a ``carried::<date>`` marker on the task line.

    Idempotent re-stamp: an existing ``carried::`` is replaced in place rather than
    duplicated, so carrying a task on two consecutive EODs leaves exactly one marker
    with the latest date. The marker is appended at the end of the line (after any
    inline fields) -- it is metadata for the standup, not a positional field.
    """
    stripped = CARRIED_RE.sub("", raw_line).rstrip()
    return f"{stripped} carried::{carried_date}"


def _extract_carried(raw_line: str) -> str | None:
    match = re.search(r"\bcarried::\s*(\d{4}-\d{2}-\d{2})", raw_line)
    return match.group(1) if match else None


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
    # The rollback must be at least as crash-safe as the forward path it restores:
    # route every restore through _atomic_write (temp+replace+fsync) so a crash
    # mid-restore cannot leave a board half-rewritten with the snapshot content.
    for path, (existed, content) in snapshots.items():
        if existed:
            path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(path, content)
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
        _atomic_write(tasks_file, new_content)
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
        # A recurring task is rolled forward (kept on the board with the same
        # canonical_id and a new due date), not removed. Callers that key state on
        # task_id (U4's nag loop) need this so they can RESET the loop for the next
        # recurrence rather than mark it terminally acked.
        "recurring": bool(recur_value),
    }


def reschedule_by_id(
    task_id: str,
    new_due: str,
    personal: bool = False,
    source: str = "user_command",
) -> dict:
    """Move a task's ``due::`` to ``new_due`` (YYYY-MM-DD), atomically + reversibly.

    Shares the resolve / ledger-preflight / snapshot-restore scaffold with
    ``complete_by_id``: the board is snapshotted before the write, the write is
    atomic, and a failure restores the snapshot.  Used by the reactive
    ``/reschedule`` handler -- moving the due date out (to a future date) takes the
    task off the overdue set so the next nag-check closes the loop (Path C), and
    the handler also closes the nag-state loop SYNCHRONOUSLY in the same turn.
    """
    try:
        datetime.strptime(new_due, "%Y-%m-%d")
    except ValueError:
        return {"ok": False, "error": {
            "code": "invalid-due-date",
            "message": f"Due date must be YYYY-MM-DD; got {new_due!r}.",
        }}

    tasks_file, content, record, error = _resolve_by_id(task_id, personal)
    if error:
        return error
    ledger_error = _preflight_ledger(tasks_file)
    if ledger_error:
        return ledger_error
    if record.line_number is None:
        return {"ok": False, "error": {
            "code": "task-line-resolution-failed",
            "message": "Resolved task has no stable line number; active board was not changed.",
        }}

    new_line = _set_due_date(record.raw_line, new_due)
    new_content = replace_task_line(content, record.raw_line, new_line, record.line_number)
    if new_content is None:
        return {"ok": False, "error": {
            "code": "task-line-resolution-failed",
            "message": "Resolved task line no longer matches the active board; active board was not changed.",
        }}

    old_due = _extract_due_date(record.raw_line)
    event = new_event(
        "state_transition",
        task_id=task_id,
        source=source,
        previous_state="active",
        next_state="active",
        reason="rescheduled-by-canonical-id",
        metadata={"title": record.title, "line_number": record.line_number,
                  "previous_due": old_due, "new_due": new_due},
    )
    snapshots = {tasks_file: (True, content)}
    ledger_file = ledger_path(tasks_file)
    ledger_snapshot = _snapshot_regular(ledger_file)
    if ledger_snapshot is not None:
        snapshots[ledger_file] = ledger_snapshot

    try:
        _atomic_write(tasks_file, new_content)
        append_event(event, path=ledger_file)
    except OSError as exc:
        restore_error = _restore_after_failure(snapshots)
        error = {"code": "task-state-write-failed",
                 "message": f"Reschedule write failed; snapshots were restored: {exc}"}
        if restore_error:
            error["restore_error"] = restore_error
        return {"ok": False, "error": error}
    return {"ok": True, "task_id": task_id, "title": record.title,
            "previous_due": old_due, "new_due": new_due, "event": event}


def carry_by_id(
    task_id: str,
    *,
    personal: bool = False,
    source: str = "user_command",
    carried_date: str | None = None,
) -> dict:
    """EOD ``carry``: keep the task ACTIVE, stamp a ``carried::<date>`` marker.

    A carry is the "not done, but still mine -- chase it again tomorrow" disposition.
    The task stays on the active board (no removal, no parking, no done) and is only
    annotated with an inline ``carried::<today>`` marker so the morning standup can
    surface it as carried-from-yesterday (KTD-7: disposition lives in inline metadata
    + the ledger, NOT a new board status field).

    Mirrors ``reschedule_by_id`` exactly: resolve by canonical id, preflight the
    ledger, snapshot the board (and ledger), replace the line atomically, append the
    ledger event, and restore the snapshot on any write failure. Re-carrying is
    idempotent -- ``_set_carried_marker`` refreshes the date rather than stacking
    markers. The ledger event is the registered ``eod_disposition_carry``.
    """
    tasks_file, content, record, error = _resolve_by_id(task_id, personal)
    if error:
        return error
    ledger_error = _preflight_ledger(tasks_file)
    if ledger_error:
        return ledger_error
    if record.line_number is None:
        return {"ok": False, "error": {
            "code": "task-line-resolution-failed",
            "message": "Resolved task has no stable line number; active board was not changed.",
        }}

    carried_date = carried_date or datetime.now().date().isoformat()
    new_line = _set_carried_marker(record.raw_line, carried_date)
    new_content = replace_task_line(content, record.raw_line, new_line, record.line_number)
    if new_content is None:
        return {"ok": False, "error": {
            "code": "task-line-resolution-failed",
            "message": "Resolved task line no longer matches the active board; active board was not changed.",
        }}

    previous_carried = _extract_carried(record.raw_line)
    event = new_event(
        "eod_disposition_carry",
        task_id=task_id,
        source=source,
        previous_state="active",
        next_state="active",
        reason="eod-disposition-carry",
        metadata={"title": record.title, "line_number": record.line_number,
                  "carried_date": carried_date, "previous_carried": previous_carried},
    )
    snapshots = {tasks_file: (True, content)}
    ledger_file = ledger_path(tasks_file)
    ledger_snapshot = _snapshot_regular(ledger_file)
    if ledger_snapshot is not None:
        snapshots[ledger_file] = ledger_snapshot

    try:
        _atomic_write(tasks_file, new_content)
        append_event(event, path=ledger_file)
    except OSError as exc:
        restore_error = _restore_after_failure(snapshots)
        error = {"code": "task-state-write-failed",
                 "message": f"Carry write failed; snapshots were restored: {exc}"}
        if restore_error:
            error["restore_error"] = restore_error
        return {"ok": False, "error": error}
    return {"ok": True, "task_id": task_id, "title": record.title,
            "carried_date": carried_date, "event": event}


def drop_by_id(
    task_id: str,
    *,
    personal: bool = False,
    source: str = "user_command",
) -> dict:
    """EOD ``drop``: move the task off the active board into the 🅿️ Parking Lot.

    A drop is the "let it go (for now)" disposition: the task is removed from the
    active board and re-inserted under the Parking Lot section in ONE atomic board
    write, so a reader never sees the task absent from both. The snapshot captures the
    WHOLE board before the edit, so a failed write restores it byte-for-byte -- and the
    ``pre_action_snapshot`` the EOD caller records (mirroring ``harvest_ledger.approve``)
    carries the original active ``raw_line``, so ``/undo`` restores the task to the
    active board by stable id within the undo window (REVERSIBILITY).

    Mirrors ``complete_by_id``'s reversible scaffold (resolve / ledger-preflight /
    snapshot / atomic write / restore-on-failure). A recurring task is dropped as-is
    (no rollover): dropping is an explicit decision to stop chasing this occurrence,
    not a completion, so it must NOT spawn a next occurrence. The ledger event is the
    registered ``eod_disposition_drop``.
    """
    tasks_file, content, record, error = _resolve_by_id(task_id, personal)
    if error:
        return error
    ledger_error = _preflight_ledger(tasks_file)
    if ledger_error:
        return ledger_error
    if record.line_number is None:
        return {"ok": False, "error": {
            "code": "task-line-resolution-failed",
            "message": "Resolved task has no stable line number; active board was not changed.",
        }}

    dropped = _drop_to_parking(content, record.raw_line, record.line_number)
    if dropped is None:
        return {"ok": False, "error": {
            "code": "parking-lot-missing",
            "message": "No 🅿️ Parking Lot section to drop into, or the task line no "
                       "longer matches; active board was not changed.",
        }}
    new_content, parked_line = dropped

    event = new_event(
        "eod_disposition_drop",
        task_id=task_id,
        source=source,
        previous_state="active",
        next_state="parked",
        reason="eod-disposition-drop",
        metadata={"title": record.title, "line_number": record.line_number},
    )
    snapshots = {tasks_file: (True, content)}
    ledger_file = ledger_path(tasks_file)
    ledger_snapshot = _snapshot_regular(ledger_file)
    if ledger_snapshot is not None:
        snapshots[ledger_file] = ledger_snapshot

    try:
        _atomic_write(tasks_file, new_content)
        append_event(event, path=ledger_file)
    except OSError as exc:
        restore_error = _restore_after_failure(snapshots)
        error = {"code": "task-state-write-failed",
                 "message": f"Drop write failed; snapshots were restored: {exc}"}
        if restore_error:
            error["restore_error"] = restore_error
        return {"ok": False, "error": error}
    return {"ok": True, "task_id": task_id, "title": record.title,
            "destination": "parking_lot", "parked_line": parked_line,
            "raw_line": record.raw_line, "line_number": record.line_number,
            "event": event}


def _parked_line(active_line: str) -> str:
    """Render the active task line as a parking-lot line (drop the ``carried::`` marker,
    add the parking ``created::<today>`` marker so the parked item is self-describing).

    Keeps the SAME ``task_id`` so ``/undo`` can resolve and reverse the move by stable
    id (restore-by-task-id), and so a later ``/promote`` round-trips the task. The
    parked form differs from the active form (the ``created::`` marker + no
    ``carried::``), which is exactly what makes the undo an in-place id swap back to the
    original active line.
    """
    base = CARRIED_RE.sub("", active_line).rstrip()
    today = datetime.now().date().isoformat()
    if re.search(r"\bcreated::", base):
        return base
    return f"{base} created::{today}"


def _drop_to_parking(content: str, raw_line: str, line_number: int) -> tuple[str, str] | None:
    """Remove the active task line and re-insert it (parked form) under the 🅿️ section.

    Returns ``(new_content, parked_line)`` -- the rewritten board plus the parked line
    text (the caller stamps it as the gate snapshot's ``post_raw_line`` so ``/undo`` can
    swap it back to the original active line by stable id) -- or ``None`` when there is
    no Parking Lot section or the active line no longer matches (caller refuses, board
    untouched). Done in ONE pass over a single ``lines`` list so the result is one
    atomic write: the active line is removed and its parked form is inserted under the
    Parking Lot header.
    """
    from parking_lot import _find_parking_lot_bounds, _item_block_end

    lines = content.split("\n")
    if line_index(lines, raw_line, line_number) is None:
        return None
    start, _end = _find_parking_lot_bounds(lines)
    if start == -1:
        return None

    index = line_number - 1
    block_end = _item_block_end(lines, index)
    parked_line = _parked_line(lines[index])
    block = [parked_line] + lines[index + 1:block_end]
    del lines[index:block_end]

    # The removal shifts every line after it up by len(block); recompute the parking
    # header position so the insert lands under the (possibly shifted) header.
    start, _ = _find_parking_lot_bounds(lines)
    if start == -1:  # the parking header was inside the removed block -- impossible for an active task, fail closed
        return None
    insert_at = start + 1
    lines[insert_at:insert_at] = block
    return "\n".join(lines), parked_line


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
