#!/usr/bin/env python3
"""Deterministic weekly board rollover.

The weekly board is regenerated from the current board plus the append-only
ledger. It deliberately emits one canonical sectioned board so completed tasks
cannot re-enter through a second rendered representation.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

import cos_config
import error_envelope
from task_ledger import ledger_path, read_events
from task_records import TaskRecord, repair_hint, task_records
from task_transitions import _extract_due_date, _set_due_date
from utils import (
    PRIORITY_TO_SECTION,
    _atomic_write,
    get_section_display_name,
    get_tasks_file,
    load_tasks,
    next_recurrence_date,
)


CLOSED_STATES = frozenset({"done", "closed", "complete", "completed", "cancelled"})
OPEN_STATES = frozenset({"active", "open"})


@dataclass(frozen=True)
class LedgerClosedIndex:
    task_ids: frozenset[str]
    titles: frozenset[str]


@dataclass(frozen=True)
class RolloverResult:
    content: str
    week_id: str
    open_count: int
    excluded_closed: tuple[str, ...]
    missing_task_ids: tuple[dict[str, Any], ...]
    duplicate_count: int
    advanced_recurring: tuple[dict[str, Any], ...]
    skipped_recur_errors: tuple[dict[str, Any], ...] = ()

    def payload(self, *, tasks_file: Path | None = None) -> dict[str, Any]:
        payload = {
            "ok": True,
            "week_id": self.week_id,
            "open_count": self.open_count,
            "excluded_closed": list(self.excluded_closed),
            "missing_task_ids": list(self.missing_task_ids),
            "duplicate_count": self.duplicate_count,
            "advanced_recurring": list(self.advanced_recurring),
            "skipped_recur_errors": list(self.skipped_recur_errors),
        }
        if tasks_file is not None:
            payload["tasks_file"] = str(tasks_file)
        return payload


@dataclass(frozen=True)
class Candidate:
    record: TaskRecord
    raw_line: str
    task_id: str | None
    title_key: str
    missing_task_id: bool
    advanced_recurring: bool = False
    next_due: str | None = None
    skipped_recur_error: dict[str, Any] | None = None


def week_id_for(target_date: date | datetime | str | None = None) -> str:
    if target_date is None:
        ref = cos_config.local_today()
    elif isinstance(target_date, datetime):
        ref = target_date.date()
    elif isinstance(target_date, date):
        ref = target_date
    elif isinstance(target_date, str):
        ref = datetime.strptime(target_date, "%Y-%m-%d").date()
    else:
        raise ValueError("target_date must be a date/datetime object or YYYY-MM-DD string")
    iso_year, iso_week, _ = ref.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def normalise_title(title: str) -> str:
    return re.sub(r"\s+", " ", title).strip().casefold()


def ledger_closed_index(events: Iterable[dict[str, Any]]) -> LedgerClosedIndex:
    """Return task ids/titles whose latest ledger transition is terminal done."""
    state_by_id: dict[str, str] = {}
    state_by_title: dict[str, str] = {}

    for event in events:
        event_type = str(event.get("event_type") or "")
        task_id = _event_task_id(event)
        title_key = normalise_title(str((event.get("metadata") or {}).get("title") or ""))
        if event_type == "state_transition_reverted":
            if task_id:
                state_by_id[task_id] = "open"
            if title_key:
                state_by_title[title_key] = "open"
            continue
        if event_type != "state_transition":
            continue

        next_state = str(event.get("next_state") or "").strip().casefold()
        if next_state in CLOSED_STATES:
            if task_id:
                state_by_id[task_id] = "closed"
            if title_key:
                state_by_title[title_key] = "closed"
        elif next_state in OPEN_STATES:
            if task_id:
                state_by_id[task_id] = "open"
            if title_key:
                state_by_title[title_key] = "open"

    return LedgerClosedIndex(
        task_ids=frozenset(task_id for task_id, state in state_by_id.items() if state == "closed"),
        titles=frozenset(title for title, state in state_by_title.items() if state == "closed"),
    )


def _event_task_id(event: dict[str, Any]) -> str | None:
    value = event.get("task_id")
    if isinstance(value, str) and value:
        return value
    metadata = event.get("metadata") or {}
    for key in ("task_id", "canonical_id"):
        value = metadata.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def rollover_board(
    content: str,
    events: Iterable[dict[str, Any]],
    *,
    target_date: date | datetime | str | None = None,
    personal: bool = False,
    fmt: str = "obsidian",
) -> RolloverResult:
    week_id = week_id_for(target_date)
    ref_date = _target_date_value(target_date)
    closed = ledger_closed_index(events)
    records = task_records(content, personal=personal, fmt=fmt)

    candidates: list[Candidate] = []
    excluded_closed: list[str] = []
    advanced_recurring: list[dict[str, Any]] = []
    skipped_recur_errors: list[dict[str, Any]] = []
    parking_lot_lines = _parking_lot_section_lines(content)

    for record in records:
        candidate = _candidate_for_record(record, ref_date)
        if candidate is None:
            continue
        if is_closed_by_ledger(candidate, closed):
            excluded_closed.append(candidate.task_id or f"title:{record.title}")
            continue
        candidates.append(candidate)
        if candidate.skipped_recur_error:
            skipped_recur_errors.append(candidate.skipped_recur_error)
        if candidate.advanced_recurring:
            advanced_recurring.append(
                {
                    "task_id": candidate.task_id,
                    "title": record.title,
                    "next_due": candidate.next_due,
                    "line_number": record.line_number,
                }
            )

    rendered, missing_task_ids, duplicate_count = render_candidates(
        candidates,
        week_id,
        personal=personal,
        parking_lot_lines=parking_lot_lines,
    )
    return RolloverResult(
        content=rendered,
        week_id=week_id,
        open_count=sum(1 for line in rendered.splitlines() if re.match(r"^\s*- \[ \] ", line)),
        excluded_closed=tuple(excluded_closed),
        missing_task_ids=tuple(missing_task_ids),
        duplicate_count=duplicate_count,
        advanced_recurring=tuple(advanced_recurring),
        skipped_recur_errors=tuple(skipped_recur_errors),
    )


def _target_date_value(target_date: date | datetime | str | None) -> date:
    if target_date is None:
        return cos_config.local_today()
    if isinstance(target_date, datetime):
        return target_date.date()
    if isinstance(target_date, date):
        return target_date
    if isinstance(target_date, str):
        return datetime.strptime(target_date, "%Y-%m-%d").date()
    raise ValueError("target_date must be a date/datetime object or YYYY-MM-DD string")


def _candidate_for_record(record: TaskRecord, ref_date: date) -> Candidate | None:
    if record.section == "parking_lot" or record.is_objective:
        return None

    task_id = record.canonical_id
    title_key = normalise_title(record.title)
    if record.done:
        if not record.recur:
            return None
        try:
            next_line, next_due = _advance_recurring_line(record, ref_date)
        except ValueError as exc:
            return Candidate(
                record=record,
                raw_line=_open_checked_line(record.raw_line),
                task_id=task_id,
                title_key=title_key,
                missing_task_id=record.task_id is None,
                skipped_recur_error={
                    "task_id": task_id,
                    "title": record.title,
                    "recur": record.recur,
                    "error": str(exc),
                },
            )
        return Candidate(
            record=record,
            raw_line=next_line,
            task_id=task_id,
            title_key=title_key,
            missing_task_id=record.task_id is None,
            advanced_recurring=True,
            next_due=next_due,
        )
    return Candidate(
        record=record,
        raw_line=record.raw_line,
        task_id=task_id,
        title_key=title_key,
        missing_task_id=record.task_id is None,
    )


def _open_checked_line(raw_line: str) -> str:
    return re.sub(r"^(\s*)- \[[xX]\] ", r"\1- [ ] ", raw_line, count=1)


def _advance_recurring_line(record: TaskRecord, ref_date: date) -> tuple[str, str]:
    due_value = _extract_due_date(record.raw_line)
    from_date = due_value or ref_date.isoformat()
    next_due = next_recurrence_date(record.recur or "", from_date)
    open_line = _open_checked_line(record.raw_line)
    return _set_due_date(open_line, next_due), next_due


def is_closed_by_ledger(candidate: Candidate, closed: LedgerClosedIndex) -> bool:
    if candidate.advanced_recurring or candidate.record.recur:
        return False
    if candidate.task_id and candidate.task_id in closed.task_ids:
        return True
    if candidate.task_id is None and candidate.title_key in closed.titles:
        return True
    return False


SECTION_ORDER = ("q1", "q2", "q3", "team", "backlog")
PERSONAL_SECTION_ORDER = ("q1", "q2", "q3", "backlog")


def render_candidates(
    candidates: list[Candidate],
    week_id: str,
    *,
    personal: bool = False,
    parking_lot_lines: list[str] | None = None,
) -> tuple[str, list[dict[str, Any]], int]:
    id_titles: set[str] = set()
    seen_ids_for_titles: set[str] = set()
    for candidate in candidates:
        if candidate.task_id and candidate.task_id not in seen_ids_for_titles:
            seen_ids_for_titles.add(candidate.task_id)
            if candidate.title_key:
                id_titles.add(candidate.title_key)

    seen_ids: set[str] = set()
    seen_bare_titles: set[str] = set()
    kept: list[Candidate] = []
    duplicate_count = 0

    for candidate in candidates:
        if candidate.task_id:
            if candidate.task_id in seen_ids:
                duplicate_count += 1
                continue
            seen_ids.add(candidate.task_id)
            kept.append(candidate)
            continue

        if candidate.title_key in id_titles or candidate.title_key in seen_bare_titles:
            duplicate_count += 1
            continue
        if candidate.title_key:
            seen_bare_titles.add(candidate.title_key)
        kept.append(candidate)

    grouped: dict[str, list[Candidate]] = {
        bucket: [] for bucket in (PERSONAL_SECTION_ORDER if personal else SECTION_ORDER)
    }
    for candidate in kept:
        grouped.setdefault(_bucket_for_candidate(candidate), []).append(candidate)

    section_order = PERSONAL_SECTION_ORDER if personal else SECTION_ORDER
    lines = [f"# Weekly TODOs — {week_id}"]
    missing: list[dict[str, Any]] = []
    for bucket in section_order:
        lines.extend(["", f"## {get_section_display_name(bucket, personal=personal)}"])
        for candidate in grouped.get(bucket, []):
            lines.append(candidate.raw_line)
            if candidate.missing_task_id:
                missing.append(
                    {
                        "line_number": candidate.record.line_number,
                        "title": candidate.record.title,
                        "raw_line": candidate.record.raw_line,
                    }
                )
                lines.append(repair_hint(candidate.record.title))
    if parking_lot_lines is not None:
        lines.extend(["", *parking_lot_lines])
    return "\n".join(lines).rstrip() + "\n", missing, duplicate_count


def _bucket_for_candidate(candidate: Candidate) -> str:
    section = candidate.record.section
    if section in SECTION_ORDER:
        return section
    if candidate.record.priority:
        mapped_section = PRIORITY_TO_SECTION.get(candidate.record.priority)
        if mapped_section in SECTION_ORDER:
            return mapped_section
    return "backlog"


def _parking_lot_section_lines(content: str) -> list[str] | None:
    from parking_lot import _find_parking_lot_bounds

    lines = content.splitlines()
    start, end = _find_parking_lot_bounds(lines)
    if start == -1:
        return None
    return lines[start:end] or [f"## {get_section_display_name('parking_lot')}"]


def run_rollover(
    *,
    personal: bool = False,
    target_date: str | None = None,
    dry_run: bool = False,
) -> RolloverResult:
    tasks_file, fmt = get_tasks_file(personal)
    content, _tasks = load_tasks(personal)
    events = read_events(ledger_path(tasks_file))
    result = rollover_board(
        content,
        events,
        target_date=target_date,
        personal=personal,
        fmt=fmt,
    )
    if not dry_run:
        _atomic_write(tasks_file, result.content)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Regenerate the weekly task board deterministically")
    parser.add_argument("--personal", action="store_true", help="Use Personal Tasks instead of Work Tasks")
    parser.add_argument("--date", help="Target date for ISO week header (YYYY-MM-DD)")
    parser.add_argument("--dry-run", action="store_true", help="Print result without writing the board")
    args = parser.parse_args(argv)

    tasks_file, _fmt = get_tasks_file(args.personal)
    result = run_rollover(personal=args.personal, target_date=args.date, dry_run=args.dry_run)
    if args.dry_run:
        print(result.content, end="")
    else:
        print(json.dumps(result.payload(tasks_file=tasks_file), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(error_envelope.run_main("rollover", main))
