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

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None

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


# Contract 1 -- canonical event_type registry (Chief-of-Staff Phase 0a).
# A single physical ledger (`Weekly TODOs.md.events.jsonl`) is shared by U1-U6
# and read by completion_candidates.py / full-ledger scans. Every event_type a
# unit appends MUST be registered here so a scan can recognise it instead of
# misreading it. This set is the documented contract; new_event() validates
# against it and warns on an unregistered type rather than guessing.
KNOWN_EVENT_TYPES: frozenset[str] = frozenset({
    # Pre-existing (already in the ledger today).
    "state_transition",
    "state_transition_reverted",
    "metadata_repair",
    "candidate_seen",
    "candidate_shown",
    "candidate_confirmed",
    "candidate_rejected",
    "candidate_snoozed",
    "candidate_duplicate",
    "candidate_expired",
    "candidate_apply_failed",
    # U1 -- trust foundation.
    "system_error",
    # U2 -- autonomy & audit substrate.
    "agent_action",
    "pre_action_snapshot",
    "pre_action_snapshot_cancelled",
    # U3 -- focus core.
    "focus_proposed",
    "focus_approved",
    "focus_vetoed",
    "wip_cap_enforced",
    "disposition",
    "disposition_skipped",
    "capacity_overcommit",
    # U4 -- accountability / nag engine.
    "nag_opened",
    "nag_sent",
    "nag_acked",
    "nag_snoozed",
    "nag_delivery_blocked",
    "body_double_started",
    "body_double_checkin",
    "body_double_ended",
    # U5 -- accomplishment ledger.
    "ledger_harvest_started",
    "ledger_draft_pushed",
    "evidence_link",
    "ledger_approved",
    "ledger_rejected",
    "harvest_error",
    # U6 -- proactive layer.
    "calendar_block_created",
    "calendar_block_moved",
    "calendar_block_deleted",
    "calendar_block_refused",
    "brief_sent",
    "debrief_captured",
    "commitment_task_created",
    "freebusy_check_passed",
    "freebusy_check_failed",
    "delivery_target_resolved",
    "delivery_target_proof_failed",
})

# Canonical actor sources. "agent_autonomous" is the source every agent-initiated
# (non-user-commanded) event uses, so audit replay can separate autonomous acts
# from user_command / cli ones.
KNOWN_EVENT_SOURCES: frozenset[str] = frozenset({
    "cli",
    "user_command",
    "completion_candidate_cli",
    "metadata_repair",
    "ledger_agent",
    "agent_autonomous",
})


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
    if event_type not in KNOWN_EVENT_TYPES:
        warnings.warn(
            f"Unregistered ledger event_type {event_type!r}; add it to "
            "task_ledger.KNOWN_EVENT_TYPES so ledger scans recognise it.",
            RuntimeWarning,
            stacklevel=2,
        )
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
    target = Path(path) if path is not None else ledger_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(event, ensure_ascii=False, sort_keys=True)
    # Contract 1: hold an exclusive flock for the whole append so concurrent
    # writers (heartbeat cron + user CLI + every U1-U6 caller that goes through
    # append_event) can never interleave a torn JSONL line. The lock lives HERE,
    # inside append_event, not in any per-unit wrapper -- a wrapper would leave
    # standup.py / complete_by_id() racing. flock is released when the handle is
    # closed by the `with` block, including on exception.
    with target.open("a", encoding="utf-8") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.write(rendered + "\n")
            handle.flush()
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
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
