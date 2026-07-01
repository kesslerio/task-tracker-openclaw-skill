#!/usr/bin/env python3
"""ID-based task state transitions."""

from __future__ import annotations

import json
import os
import re
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator

import task_records as task_record_module
from autonomy import board_snapshot, resolve_board_restore
from locks import sidecar_flock
from log_done import log_task_completed
from task_lines import line_index, remove_task_line, replace_task_line
from task_records import active_records, load_records
from task_ledger import append_event, ledger_path, new_event, read_events
from utils import next_recurrence_date, _atomic_write

INLINE_FIELD_RE = re.compile(r"\s+[A-Za-z_][A-Za-z0-9_-]*::")
RECURRENCE_RE = re.compile(r"\brecur::\s*(?!(?:\s|[A-Za-z_][A-Za-z0-9_-]*::))([^\n]+?)(?=\s+[A-Za-z_][A-Za-z0-9_-]*::|\s*[🗓️📅]|$)")
DUE_RE = re.compile(r"(?:🗓️\s*|📅\s*)(\d{4}-\d{2}-\d{2})")
# An EOD ``carry`` stamps this inline marker so the morning standup can surface the
# task as carried-from-yesterday. It is plain inline metadata (KTD-7): no new board
# status field, the task stays ACTIVE.
CARRIED_RE = re.compile(r"\s*carried::\s*\d{4}-\d{2}-\d{2}")


@contextmanager
def board_flock(target: Path) -> Iterator[None]:
    """Hold the exclusive sidecar flock guarding a board rewrite."""
    with sidecar_flock(target):
        yield


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


def _tasks_file_for_board(personal: bool) -> Path:
    tasks_file, _fmt = task_record_module.get_tasks_file(personal)
    return tasks_file


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
                "matches_found": len(matches),
                "repair_choices": ["identity-audit", "identity-repair --dry-run"],
            },
        }
    return tasks_file, content, matches[0], None


def _reverted_transition_ids(events: list[dict[str, Any]]) -> set[str]:
    reverted: set[str] = set()
    for event in events:
        if event.get("event_type") != "state_transition_reverted":
            continue
        metadata = event.get("metadata") or {}
        for key in ("completion_id", "reverted_event_id"):
            value = metadata.get(key)
            if isinstance(value, str) and value:
                reverted.add(value)
    return reverted


def _latest_unreverted_terminal_event(
    events: list[dict[str, Any]],
    task_id: str,
    terminal_states: set[str],
) -> dict[str, Any] | None:
    reverted = _reverted_transition_ids(events)
    for event in reversed(events):
        if event.get("event_type") != "state_transition":
            continue
        if event.get("event_id") in reverted:
            continue
        if event.get("task_id") != task_id:
            continue
        if event.get("next_state") in terminal_states:
            return event
    return None


def _terminal_noop_result(
    tasks_file: Path,
    task_id: str,
    personal: bool,
    terminal_states: set[str],
) -> dict | None:
    try:
        _resolved_file, _content, records = load_records(personal)
    except FileNotFoundError:
        records = []

    matches = [record for record in records if record.canonical_id == task_id]
    done_matches = [record for record in matches if record.done]
    if "done" in terminal_states and len(done_matches) == 1:
        record = done_matches[0]
        return {
            "ok": False,
            "task_id": task_id,
            "title": record.title,
            "noop": True,
            "reason": "already-done",
            "error": {
                "code": "canonical-id-resolution-failed",
                "message": f"Task {task_id} is already done; no task state was changed.",
            },
        }

    terminal_event = _latest_unreverted_terminal_event(
        read_events(ledger_path(tasks_file)),
        task_id,
        terminal_states,
    )
    if terminal_event is None:
        return None

    state = terminal_event.get("next_state") or "terminal"
    return {
        "ok": False,
        "task_id": task_id,
        "title": (terminal_event.get("metadata") or {}).get("title"),
        "noop": True,
        "reason": f"already-{state}",
        "error": {
            "code": "canonical-id-resolution-failed",
            "message": f"Task {task_id} is already {state}; no task state was changed.",
        },
    }


def _maybe_terminal_noop(
    tasks_file: Path,
    task_id: str,
    personal: bool,
    error: dict,
    terminal_states: set[str],
) -> dict | None:
    detail = error.get("error") or {}
    if (
        detail.get("code") != "canonical-id-resolution-failed"
        or detail.get("matches_found") != 0
    ):
        return None
    return _terminal_noop_result(tasks_file, task_id, personal, terminal_states)


def _same_path(left: Path, right: Path) -> bool:
    return left.expanduser().resolve(strict=False) == right.expanduser().resolve(strict=False)


def _daily_context_task_id(context_line: str) -> str | None:
    try:
        parsed = json.loads(context_line.strip())
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    value = parsed.get("task_id")
    return value if isinstance(value, str) and value else None


def _remove_exact_completion_block(
    content: str,
    action_line: str,
    context_line: str,
    task_id: str,
) -> tuple[str, int]:
    if _daily_context_task_id(context_line) != task_id:
        return content, 0

    lines = content.splitlines(keepends=True)
    matches: list[int] = []
    for index in range(0, max(0, len(lines) - 1)):
        if (
            lines[index].rstrip("\r\n") == action_line
            and lines[index + 1].rstrip("\r\n") == context_line
            and _daily_context_task_id(lines[index + 1].rstrip("\r\n")) == task_id
        ):
            matches.append(index)
    if len(matches) != 1:
        return content, len(matches)
    index = matches[0]
    del lines[index:index + 2]
    return "".join(lines), 1


def _later_unreverted_terminal_event(
    events: list[dict[str, Any]],
    completion_index: int,
    task_id: str,
) -> dict[str, Any] | None:
    reverted = _reverted_transition_ids(events)
    for event in events[completion_index + 1:]:
        event_id = event.get("event_id")
        if event.get("event_type") != "state_transition":
            continue
        if isinstance(event_id, str) and event_id in reverted:
            continue
        if event.get("task_id") != task_id:
            continue
        if event.get("next_state") in {"done", "cancelled"}:
            return event
    return None


def complete_by_id(
    task_id: str,
    personal: bool = False,
    source: str = "user_command",
    extra_events_factory: Callable[[dict], list[dict]] | None = None,
) -> dict:
    lock_file = _tasks_file_for_board(personal)
    initial_tasks_file, _initial_content, initial_record, initial_error = _resolve_by_id(task_id, personal)
    if initial_error:
        noop = _maybe_terminal_noop(
            initial_tasks_file or lock_file,
            task_id,
            personal,
            initial_error,
            {"done", "cancelled"},
        )
        return noop or initial_error
    target_raw_line = initial_record.raw_line

    with board_flock(lock_file):
        tasks_file, content, record, error = _resolve_by_id(task_id, personal)
        if error:
            noop = _maybe_terminal_noop(tasks_file or lock_file, task_id, personal, error, {"done", "cancelled"})
            return noop or error
        # Concurrency guard (recurring-safe): if a winner completed this occurrence
        # while we waited on the board lock, our pre-lock target line is either gone
        # (non-recurring removal) or rolled forward to a new due (recurring keeps the
        # same task_id active). Either way the occurrence we captured is already done,
        # so no-op instead of double-completing / advancing the recurrence twice.
        # (Verified by test_concurrent_complete_recurring_serializes_to_one_completion.)
        if record.raw_line != target_raw_line:
            noop = _terminal_noop_result(tasks_file, task_id, personal, {"done", "cancelled"})
            if noop:
                return noop
            return {
                "ok": False,
                "error": {
                    "code": "task-line-resolution-failed",
                    "message": "Target task line no longer matches the active board; active board was not changed.",
                },
            }
        ledger_error = _preflight_ledger(tasks_file)
        if ledger_error:
            return ledger_error

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
        next_line = None
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

        event = new_event(
            "state_transition",
            task_id=task_id,
            source=source,
            previous_state="active",
            next_state="done",
            reason="explicit-done-by-canonical-id",
            metadata={
                "title": record.title,
                "line_number": record.line_number,
                "board_snapshot": board_snapshot(
                    tasks_file,
                    record.raw_line,
                    record.line_number,
                    content=content,
                    post_raw_line=next_line,
                ),
            },
        )
        event["metadata"]["completion_id"] = event["event_id"]
        extra_events = extra_events_factory(event) if extra_events_factory else []

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
            event["metadata"].update(logged)

            write_stage = "board-write"
            _atomic_write(tasks_file, new_content)
            write_stage = "ledger-append"
            append_event(event, path=ledger_file)
            for extra_event in extra_events:
                append_event(extra_event, path=ledger_file)
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
            "completion_id": event["event_id"],
            "event": event,
            "extra_events": extra_events,
            # A recurring task is rolled forward (kept on the board with the same
            # canonical_id and a new due date), not removed. Callers that key state on
            # task_id (U4's nag loop) need this so they can RESET the loop for the next
            # recurrence rather than mark it terminally acked.
            "recurring": bool(recur_value),
        }


def cancel_by_id(
    task_id: str,
    personal: bool = False,
    source: str = "user_command",
) -> dict:
    lock_file = _tasks_file_for_board(personal)
    with board_flock(lock_file):
        tasks_file, content, record, error = _resolve_by_id(task_id, personal)
        if error:
            noop = _maybe_terminal_noop(tasks_file or lock_file, task_id, personal, error, {"done", "cancelled"})
            return noop or error
        ledger_error = _preflight_ledger(tasks_file)
        if ledger_error:
            return ledger_error
        if record.line_number is None:
            return {
                "ok": False,
                "error": {
                    "code": "task-line-resolution-failed",
                    "message": "Resolved task has no stable line number; active board was not changed.",
                },
            }

        new_content = remove_task_line(content, record.raw_line, record.line_number)
        if new_content is None:
            return {
                "ok": False,
                "error": {
                    "code": "task-line-resolution-failed",
                    "message": "Resolved task line no longer matches the active board; active board was not changed.",
                },
            }

        event = new_event(
            "state_transition",
            task_id=task_id,
            source=source,
            previous_state="active",
            next_state="cancelled",
            reason="cancelled-by-id",
            metadata={
                "title": record.title,
                "line_number": record.line_number,
                "raw_line": record.raw_line,
                "section": record.section,
                "area": record.area,
            },
        )
        snapshots = {tasks_file: (True, content)}
        ledger_file = ledger_path(tasks_file)
        ledger_snapshot = _snapshot_regular(ledger_file)
        if ledger_snapshot is not None:
            snapshots[ledger_file] = ledger_snapshot

        write_stage = "board-write"
        try:
            _atomic_write(tasks_file, new_content)
            write_stage = "ledger-append"
            append_event(event, path=ledger_file)
        except OSError as exc:
            restore_error = _restore_after_failure(snapshots)
            code = "ledger-append-failed" if write_stage == "ledger-append" else "task-state-write-failed"
            error = {
                "code": code,
                "message": f"Task cancellation write failed; snapshots were restored: {exc}",
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
        }


def revert_completion(completion_id: str, personal: bool = False) -> dict:
    lock_file = _tasks_file_for_board(personal)
    with board_flock(lock_file):
        ledger_file = ledger_path(lock_file)
        events = read_events(ledger_file)
        completion_index = -1
        completion: dict[str, Any] | None = None
        for index, event in enumerate(events):
            if (
                event.get("event_id") == completion_id
                and event.get("event_type") == "state_transition"
                and event.get("next_state") == "done"
            ):
                completion_index = index
                completion = event
                break
        if completion is None:
            return {
                "ok": False,
                "error": {
                    "code": "completion-not-found",
                    "message": f"No completion state_transition event found for {completion_id}.",
                },
            }

        if completion_id in _reverted_transition_ids(events):
            return {
                "ok": False,
                "error": {
                    "code": "completion-already-reverted",
                    "message": f"Completion {completion_id} has already been reverted.",
                },
            }

        metadata = completion.get("metadata") or {}
        snapshot = metadata.get("board_snapshot")
        if not isinstance(snapshot, dict):
            return {
                "ok": False,
                "error": {
                    "code": "completion-snapshot-missing",
                    "message": "Completion event has no board_snapshot metadata; nothing was changed.",
                },
            }

        completion_task_id = completion.get("task_id")
        snapshot_task_id = snapshot.get("task_id")
        if (
            not isinstance(completion_task_id, str)
            or not completion_task_id
            or snapshot_task_id != completion_task_id
        ):
            return {
                "ok": False,
                "error": {
                    "code": "revert-target-mismatch",
                    "message": "Completion snapshot task_id does not match the completion event; nothing was changed.",
                },
            }

        snapshot_file_raw = snapshot.get("file")
        tasks_file = lock_file
        if isinstance(snapshot_file_raw, str) and snapshot_file_raw:
            tasks_file = Path(snapshot_file_raw).expanduser()
            if not _same_path(tasks_file, lock_file):
                return {
                    "ok": False,
                    "error": {
                        "code": "revert-target-mismatch",
                        "message": "Completion snapshot targets a different board than the requested personal flag; nothing was changed.",
                    },
                }

        later_terminal = _later_unreverted_terminal_event(events, completion_index, completion_task_id)
        if later_terminal is not None:
            return {
                "ok": False,
                "error": {
                    "code": "revert-out-of-order",
                    "message": "revert the newer completion first",
                    "later_event_id": later_terminal.get("event_id"),
                },
            }

        daily_path_raw = metadata.get("daily_note_path")
        daily_line = metadata.get("daily_note_line")
        daily_context_line = metadata.get("daily_note_context_line")
        if (
            not isinstance(daily_path_raw, str)
            or not daily_path_raw
            or not isinstance(daily_line, str)
            or not daily_line
            or not isinstance(daily_context_line, str)
            or not daily_context_line
        ):
            return {
                "ok": False,
                "error": {
                    "code": "completion-log-metadata-missing",
                    "message": "Completion event has no daily-note block identity; nothing was changed.",
                },
            }
        daily_path = Path(daily_path_raw).expanduser()

        try:
            board_content = tasks_file.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            return {
                "ok": False,
                "error": {
                    "code": "tasks-file-missing",
                    "message": str(exc),
                },
            }
        except OSError as exc:
            return {
                "ok": False,
                "error": {
                    "code": "task-state-read-failed",
                    "message": f"Could not read active board; nothing was changed: {exc}",
                },
            }

        restore = resolve_board_restore(board_content, snapshot)
        if not restore.get("ok"):
            return {
                "ok": False,
                "error": {
                    "code": "board-restore-conflict",
                    "message": "Completion could not be reverted because the board restore is ambiguous; nothing was changed.",
                    "reason": restore.get("reason"),
                    "candidates": restore.get("candidates"),
                },
            }

        if not daily_path.exists() or not daily_path.is_file():
            return {
                "ok": False,
                "error": {
                    "code": "completion-log-line-missing",
                    "message": "Daily-note completion line is not present; nothing was changed.",
                },
            }
        try:
            daily_content = daily_path.read_text(encoding="utf-8")
        except OSError as exc:
            return {
                "ok": False,
                "error": {
                    "code": "completion-log-read-failed",
                    "message": f"Could not read daily note; nothing was changed: {exc}",
                },
            }
        new_daily_content, daily_block_matches = _remove_exact_completion_block(
            daily_content,
            daily_line,
            daily_context_line,
            completion_task_id,
        )
        if daily_block_matches != 1:
            return {
                "ok": False,
                "error": {
                    "code": "completion-log-line-ambiguous",
                    "message": "Daily-note completion block could not be unambiguously located; nothing was changed.",
                    "candidates": daily_block_matches,
                },
            }

        revert_event = new_event(
            "state_transition_reverted",
            task_id=completion_task_id,
            source="user_command",
            reason="revert_completion",
            metadata={
                "completion_id": completion_id,
                "reverted_event_id": completion_id,
                "task_id": completion_task_id,
            },
        )

        snapshots = {
            tasks_file: (True, board_content),
            daily_path: (True, daily_content),
        }
        ledger_snapshot = _snapshot_regular(ledger_file)
        if ledger_snapshot is not None:
            snapshots[ledger_file] = ledger_snapshot

        write_stage = "board-write"
        try:
            new_board_content = str(restore.get("new_content", board_content))
            if new_board_content != board_content:
                _atomic_write(tasks_file, new_board_content)
            write_stage = "completion-log"
            _atomic_write(daily_path, new_daily_content)
            write_stage = "ledger-append"
            append_event(revert_event, path=ledger_file)
        except OSError as exc:
            restore_error = _restore_after_failure(snapshots)
            code = "ledger-append-failed" if write_stage == "ledger-append" else "task-state-write-failed"
            error = {
                "code": code,
                "message": f"Completion revert write failed; snapshots were restored: {exc}",
            }
            if restore_error:
                error["restore_error"] = restore_error
            return {
                "ok": False,
                "error": error,
            }

        return {
            "ok": True,
            "completion_id": completion_id,
            "task_id": completion_task_id,
            "board_restored": bool(restore.get("restored")),
            "daily_note_line_removed": True,
            "overwrote_edit": bool(restore.get("overwrote_edit")),
            "event": revert_event,
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

    lock_file = _tasks_file_for_board(personal)
    with board_flock(lock_file):
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
    lock_file = _tasks_file_for_board(personal)
    with board_flock(lock_file):
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
    lock_file = _tasks_file_for_board(personal)
    with board_flock(lock_file):
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
