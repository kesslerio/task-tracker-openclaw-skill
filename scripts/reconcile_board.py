#!/usr/bin/env python3
"""One-time weekly board reconciliation.

This command collapses the historical dual board representation into the same
single priority-sectioned shape emitted by rollover.py. It is intentionally a
maintenance tool: dry-run is the default and the pure reconcile_board() entry
point takes board text plus ledger events so tests never need a live board.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable

import error_envelope
from rollover import (
    Candidate,
    is_closed_by_ledger,
    ledger_closed_index,
    normalise_title,
    render_candidates,
    week_id_for,
)
from task_ledger import ledger_path, read_events
from task_records import TaskRecord, task_records
from utils import PRIORITY_TO_SECTION, _atomic_write, get_section_display_name, get_tasks_file, load_tasks

try:
    from utils import SECTION_ORDER
except ImportError:
    SECTION_ORDER = ("q1", "q2", "q3", "team", "backlog")


TASK_LINE_RE = re.compile(r"^\s*- \[ \] ")
REPORT_MARKER = "--- reconcile report ---"


@dataclass(frozen=True)
class ReconcileResult:
    content: str
    report: dict[str, Any]
    week_id: str
    open_count: int

    def payload(
        self,
        *,
        tasks_file: Path | None = None,
        applied: bool = False,
        repair_result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": True,
            "schema_version": "v1",
            "command": "reconcile-board",
            "applied": applied,
            "week_id": self.week_id,
            "open_count": self.open_count,
            "report": self.report,
        }
        if tasks_file is not None:
            payload["tasks_file"] = str(tasks_file)
        if repair_result is not None:
            payload["repair"] = repair_result
        return payload


@dataclass(frozen=True)
class _BoardItem:
    record: TaskRecord
    source_section: str | None
    original_index: int

    @property
    def title_key(self) -> str:
        return normalise_title(self.record.title)

    @property
    def task_id(self) -> str | None:
        return self.record.canonical_id


def reconcile_board(
    content: str,
    events: Iterable[dict[str, Any]],
    *,
    target_date: str | None = None,
    personal: bool = False,
    fmt: str = "obsidian",
) -> ReconcileResult:
    """Return a cleaned canonical board and a report of reconciliation actions."""
    week_id = week_id_for(target_date)
    closed = ledger_closed_index(events)
    parking_lot_lines = _parking_lot_section_lines(content)
    items = [item for item in _board_items(content, personal=personal, fmt=fmt) if not item.record.done]

    open_items: list[_BoardItem] = []
    struck_closed: list[dict[str, Any]] = []
    for item in items:
        candidate = _candidate_for_item(item)
        if is_closed_by_ledger(candidate, closed):
            struck_closed.append(
                {
                    **_report_item(item),
                    "matched_by": _closed_match_by(candidate, closed),
                }
            )
            continue
        open_items.append(item)

    deduped, merged_duplicates = _dedupe_items(open_items)
    candidates = [_candidate_for_item(item) for item in deduped]
    rendered, missing_task_ids, renderer_duplicate_count = render_candidates(
        candidates,
        week_id,
        personal=personal,
        parking_lot_lines=parking_lot_lines,
    )
    report: dict[str, Any] = {
        "struck_closed": struck_closed,
        "merged_duplicates": merged_duplicates,
        "still_missing_task_id": missing_task_ids,
    }
    if renderer_duplicate_count:
        report["renderer_duplicate_count"] = renderer_duplicate_count

    return ReconcileResult(
        content=rendered,
        report=report,
        week_id=week_id,
        open_count=sum(1 for line in rendered.splitlines() if TASK_LINE_RE.match(line)),
    )


def run_reconcile(
    *,
    personal: bool = False,
    target_date: str | None = None,
    apply: bool = False,
    repair: bool = False,
) -> tuple[ReconcileResult, Path, dict[str, Any] | None]:
    tasks_file, fmt = get_tasks_file(personal)
    if not tasks_file.exists():
        raise FileNotFoundError(f"Tasks file not found: {tasks_file}")
    content, _tasks = load_tasks(personal)
    events = read_events(ledger_path(tasks_file))
    result = reconcile_board(
        content,
        events,
        target_date=target_date,
        personal=personal,
        fmt=fmt,
    )

    repair_result = None
    if apply:
        _atomic_write(tasks_file, result.content)
        if repair:
            from task_repair import repair_missing_ids

            repair_result = repair_missing_ids(personal=personal, apply=True)
    return result, tasks_file, repair_result


def _board_items(content: str, *, personal: bool, fmt: str) -> list[_BoardItem]:
    source_by_line, section_by_line = _scan_task_sections(content, personal=personal)
    items: list[_BoardItem] = []
    for index, record in enumerate(task_records(content, personal=personal, fmt=fmt)):
        if record.section == "parking_lot" or record.is_objective:
            continue
        source_section = source_by_line.get(record.line_number)
        section = section_by_line.get(record.line_number)
        if section is None and record.line_number not in section_by_line and record.section in SECTION_ORDER:
            section = record.section
        items.append(
            _BoardItem(
                record=replace(record, section=section),
                source_section=source_section,
                original_index=index,
            )
        )
    return items


def _scan_task_sections(content: str, *, personal: bool) -> tuple[dict[int | None, str | None], dict[int | None, str | None]]:
    source_by_line: dict[int | None, str | None] = {}
    section_by_line: dict[int | None, str | None] = {}
    current_section: str | None = None

    for line_number, line in enumerate(content.splitlines(), start=1):
        if line.startswith("## "):
            current_section = _section_for_header(line, personal=personal)
            continue
        if TASK_LINE_RE.match(line):
            source_by_line[line_number] = current_section
            if current_section in SECTION_ORDER:
                section_by_line[line_number] = current_section
            else:
                section_by_line[line_number] = None
    return source_by_line, section_by_line


def _section_for_header(line: str, *, personal: bool) -> str | None:
    heading = re.sub(r"\s+", " ", line.removeprefix("##").strip()).casefold()
    if heading.startswith("📋 all tasks") or heading.startswith("all tasks"):
        return "all_tasks"
    if heading.startswith("🔴") or re.match(r"q1\b", heading):
        return "q1"
    if heading.startswith("🟡") or re.match(r"q2\b", heading):
        return "q2"
    if heading.startswith("🟠") or re.match(r"q3\b", heading):
        return "q3"
    if not personal and (heading.startswith("👥") or heading.startswith("team")):
        return "team"
    if heading.startswith("⚪") or heading.startswith("backlog"):
        return "backlog"
    if heading.startswith("✅") or heading.startswith("done"):
        return "done"
    return None


def _parking_lot_section_lines(content: str) -> list[str] | None:
    from parking_lot import _find_parking_lot_bounds

    lines = content.splitlines()
    start, end = _find_parking_lot_bounds(lines)
    if start == -1:
        return None
    return lines[start:end] or [f"## {get_section_display_name('parking_lot')}"]


def _candidate_for_item(item: _BoardItem) -> Candidate:
    return Candidate(
        record=item.record,
        raw_line=item.record.raw_line,
        task_id=item.record.canonical_id,
        title_key=item.title_key,
        missing_task_id=item.record.task_id is None,
    )


def _dedupe_items(items: list[_BoardItem]) -> tuple[list[_BoardItem], list[dict[str, Any]]]:
    merged: list[dict[str, Any]] = []
    by_id: dict[str, list[_BoardItem]] = {}
    without_id: list[_BoardItem] = []
    for item in items:
        if item.task_id:
            by_id.setdefault(item.task_id, []).append(item)
        else:
            without_id.append(item)

    kept: list[_BoardItem] = []
    id_title_owner: dict[str, _BoardItem] = {}
    for task_id in sorted(by_id, key=lambda key: min(item.original_index for item in by_id[key])):
        group = by_id[task_id]
        selected = min(group, key=_keep_preference)
        kept.append(selected)
        if selected.title_key:
            previous = id_title_owner.get(selected.title_key)
            if previous is None or selected.original_index < previous.original_index:
                id_title_owner[selected.title_key] = selected
        if len(group) == 1:
            continue
        for dropped in sorted((item for item in group if item is not selected), key=lambda item: item.original_index):
            merged.append(
                {
                    "reason": "duplicate-task-id",
                    "title": selected.record.title,
                    "kept": _report_item(selected),
                    "dropped": _report_item(dropped),
                }
            )

    seen_bare_titles: dict[str, _BoardItem] = {}
    for item in sorted(without_id, key=lambda item: item.original_index):
        id_bearing_item = id_title_owner.get(item.title_key)
        if id_bearing_item is not None:
            merged.append(
                {
                    "reason": "bare-duplicate-of-task-id",
                    "title": id_bearing_item.record.title,
                    "kept": _report_item(id_bearing_item),
                    "dropped": _report_item(item),
                }
            )
            continue
        if item.title_key in seen_bare_titles:
            bare_kept = seen_bare_titles[item.title_key]
            merged.append(
                {
                    "reason": "duplicate-bare-title",
                    "title": bare_kept.record.title,
                    "kept": _report_item(bare_kept),
                    "dropped": _report_item(item),
                }
            )
            continue
        if item.title_key:
            seen_bare_titles[item.title_key] = item
        kept.append(item)

    final = sorted(kept, key=lambda item: item.original_index)
    return final, sorted(merged, key=lambda row: row["dropped"]["line_number"] or 0)


def _keep_preference(item: _BoardItem) -> tuple[int, int, int, int]:
    section_rank = SECTION_ORDER.index(item.record.section) if item.record.section in SECTION_ORDER else len(SECTION_ORDER)
    mapped_priority = PRIORITY_TO_SECTION.get(item.record.priority or "")
    priority_rank = SECTION_ORDER.index(mapped_priority) if mapped_priority in SECTION_ORDER else len(SECTION_ORDER)
    return (
        0 if item.task_id else 1,
        0 if item.source_section in SECTION_ORDER else 1,
        min(section_rank, priority_rank),
        item.original_index,
    )


def _closed_match_by(candidate: Candidate, closed: Any) -> str:
    if candidate.task_id and candidate.task_id in closed.task_ids:
        return "task_id"
    if candidate.task_id is None and candidate.title_key in closed.titles:
        return "title"
    return "unknown"


def _report_item(item: _BoardItem) -> dict[str, Any]:
    return {
        "line_number": item.record.line_number,
        "title": item.record.title,
        "task_id": item.record.canonical_id,
        "source_section": item.source_section,
        "output_section": item.record.section,
        "raw_line": item.record.raw_line,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reconcile a corrupted weekly board into canonical sections")
    parser.add_argument("--personal", action="store_true", help="Use Personal Tasks instead of Work Tasks")
    parser.add_argument("--date", help="Target date for ISO week header (YYYY-MM-DD)")
    parser.add_argument("--apply", action="store_true", help="Write the cleaned board atomically")
    parser.add_argument("--repair", action="store_true", help="After --apply, run identity repair for missing task_id::")
    parser.add_argument("--dry-run", action="store_true", help="Accepted for clarity; dry-run is the default")
    args = parser.parse_args(argv)

    if args.apply and args.dry_run:
        parser.error("--apply and --dry-run are mutually exclusive")
    if args.repair and not args.apply:
        parser.error("--repair requires --apply")

    result, tasks_file, repair_result = run_reconcile(
        personal=args.personal,
        target_date=args.date,
        apply=args.apply,
        repair=args.repair,
    )
    payload = result.payload(tasks_file=tasks_file, applied=args.apply, repair_result=repair_result)
    if not args.apply:
        print(result.content, end="")
        print(REPORT_MARKER)
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    print(json.dumps(payload, indent=2, sort_keys=True))
    if repair_result and repair_result.get("blocked"):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(error_envelope.run_main("reconcile_board", main))
