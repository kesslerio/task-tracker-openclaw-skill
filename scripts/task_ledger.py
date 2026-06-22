#!/usr/bin/env python3
"""Append-only JSONL event ledger for task state and evidence."""

from __future__ import annotations

import json
import os
import warnings
from dataclasses import dataclass
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None

import cos_config
import redaction
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
    # H6 -- capture never blocks: promotion gate + swap.
    "task_promoted",
    "task_swapped",
    "disposition",
    "disposition_skipped",
    "capacity_overcommit",
    # U4 -- accountability / nag engine.
    "nag_opened",
    "nag_sent",
    "nag_acked",
    "nag_snoozed",
    "nag_delivery_blocked",
    "nag_gate_act_undelivered",
    "body_double_started",
    "body_double_checkin",
    "body_double_ended",
    # H7 -- /start initiation loop (reuses the body-double session machinery).
    "start_session_started",
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


# --- H10 retention: prune stale entries on append, undo-window-safe ----------


def _prune_cutoff(now: datetime | None = None) -> datetime:
    """The age boundary below which an event may be pruned (H10 Part B).

    The cutoff is ``now - max(ledger_retention_days, board undo window)``. Taking
    the MAX is the load-bearing safety rule: ``events.jsonl`` is the audit/undo
    substrate (``/audit`` + ``/undo`` resolve a board mutation by replaying its
    ``pre_action_snapshot`` / ``evidence_link`` / revert events, and a pending
    ``/approve`` references the harvest events), so an event INSIDE the board undo
    window (7d default) must never be pruned even if a misconfigured retention is
    shorter. Retention can shrink the kept history but NEVER below the window in
    which an event is still operationally needed.
    """
    now = now or datetime.now(timezone.utc)
    retention_hours = cos_config.ledger_retention_days() * 24
    floor_hours = cos_config.undo_window_board_hours()
    keep_hours = max(retention_hours, floor_hours)
    return now - timedelta(hours=keep_hours)


def _event_timestamp(line: str) -> datetime | None:
    """Parse the ``timestamp`` out of one rendered JSONL line, or None on garbage.

    A line we cannot parse (torn/corrupt) or whose timestamp is missing/garbage is
    treated as un-ageable: ``None`` means "do not prune". We never drop a line we
    cannot confidently date -- a wrongly-pruned audit/undo event is unrecoverable,
    a lingering one is harmless.
    """
    try:
        record = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    ts = record.get("timestamp") if isinstance(record, dict) else None
    if not isinstance(ts, str):
        return None
    try:
        parsed = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _prune_stale_locked(target: Path, *, now: datetime | None = None) -> None:
    """Drop ledger lines older than the undo-window-safe cutoff. IN PLACE, under the lock.

    Rewrites the data file IN PLACE (``r+`` seek-0 / write / truncate) -- NOT via a
    temp-file ``os.replace``. That matters: ``append_event`` holds its exclusive flock
    on the DATA file's inode, so swapping the inode out with ``os.replace`` would
    orphan that lock and let a waiting writer append to the unlinked old inode (a lost
    append). An in-place rewrite keeps the one inode the lock guards, so the whole
    read-trim-write is serialised by the SAME flock the caller already holds (external
    writers open the same path and block on it) -- no torn line, no lost append, no new
    locking scheme.

    Best-effort: any error is swallowed by the caller. The rewrite only happens when a
    line is CONFIDENTLY datable as older than the cutoff (``_event_timestamp`` returns
    None for a torn/undateable line, which is always kept) -- so a quiet ledger never
    pays the rewrite cost, and an event inside the undo window is never dropped.
    """
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    cutoff = _prune_cutoff(now)
    kept: list[str] = []
    dropped = False
    for line in lines:
        if not line.strip():
            continue
        stamped = _event_timestamp(line)
        if stamped is not None and stamped < cutoff:
            dropped = True
            continue
        kept.append(line)
    if not dropped:
        return  # nothing stale: skip the rewrite so the common path stays append-only
    content = "\n".join(kept) + ("\n" if kept else "")
    with target.open("r+", encoding="utf-8") as rewriter:
        rewriter.seek(0)
        rewriter.write(content)
        rewriter.truncate()
        rewriter.flush()


def append_event(event: dict[str, Any], path: Path | None = None) -> dict[str, Any]:
    target = Path(path) if path is not None else ledger_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    # H10 Part A: redact BEFORE persisting so NO caller can bypass the redaction --
    # the append-only ledger stores REFERENCES (subject/title, id, url, source_type,
    # task_id, timestamps, status, scores), never a raw body/snippet/content. A
    # future harvest source that adds a body field cannot leak it into this durable
    # log. ``redact_event`` is pure + total (never raises), so a malformed payload
    # degrades to a safe redacted event rather than crashing the append.
    safe_event = redaction.redact_event(event)
    rendered = json.dumps(safe_event, ensure_ascii=False, sort_keys=True)
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
            # H10 Part B: APPEND FIRST, prune SECOND (best-effort) under the SAME
            # flock -- mirroring outbox.deliver_once. The just-written event MUST
            # survive; pruning only drops entries OLDER than the undo-window-safe
            # cutoff, so it can never delete a fresh append or an in-window
            # audit/undo/approval event. Any prune fault is contained here and never
            # propagates out of append_event: the new line on disk stands.
            try:
                _prune_stale_locked(target)
            except Exception:  # noqa: BLE001 -- prune is best-effort; the append is committed
                pass
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
