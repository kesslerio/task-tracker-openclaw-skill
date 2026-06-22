#!/usr/bin/env python3
"""Append-only JSONL event ledger for task state and evidence."""

from __future__ import annotations

import json
import os
import warnings
from contextlib import contextmanager
from dataclasses import dataclass
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None

import cos_config
import redaction
from utils import _atomic_write, get_tasks_file


def _ledger_lock_path(target: Path) -> Path:
    return target.with_name(target.name + ".lock")


@contextmanager
def _ledger_flock(target: Path) -> Iterator[None]:
    """Hold the exclusive SIDECAR flock guarding ``target`` for the block.

    The lock lives on a separate ``<ledger>.lock`` file (the same pattern
    ``outbox._outbox_flock`` / ``quiet_state._quiet_flock`` use), NOT on the ledger
    data file's own fd. That is precisely what lets the retention prune rewrite the
    ledger via an atomic ``os.replace`` (temp-file swap) without orphaning the lock:
    swapping the DATA file's inode leaves the sidecar lock's inode untouched, so a
    waiting writer blocks on the sidecar and can never append to an unlinked old inode
    (no lost append), while a lock-free reader (``read_events`` takes no lock) sees the
    whole old OR whole new file -- never the torn/duplicated middle an in-place rewrite
    would expose. ``append_event`` is the SOLE writer, so the sidecar fully serialises
    every write.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    with _ledger_lock_path(target).open("a", encoding="utf-8") as lock_handle:
        if fcntl is not None:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


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

    Known edge (v0.3): the completion-candidate inbox projects a candidate's summary
    from its seed ``candidate_seen`` event. A candidate snoozed UNBOUNDEDLY (past the
    retention window, default 90d) can have its seed pruned while a newer snooze event
    survives, so it resurfaces with no summary -- degraded display, never data loss or
    a leak. A complete fix (carry the summary forward on each snooze, or a
    candidate-aware prune) is deferred to v0.3 rather than coupling this prune to live
    candidate state; 90d of un-acted snooze is itself the more pressing signal.
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


def _oldest_datable_timestamp(target: Path) -> datetime | None:
    """The timestamp of the first datable ledger line (the oldest), or None.

    Events append in time order, so the first non-empty line is the oldest. Reads only
    that one line (O(1)), so the prune hot-path can skip the full scan when nothing is
    stale. Returns None when the file is unreadable OR its head line is undateable
    (torn) -- both force the caller to fall back to the full scan rather than wrongly
    short-circuit.
    """
    try:
        with target.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                return _event_timestamp(line)
    except OSError:
        return None
    return None


def _prune_stale_locked(target: Path, *, now: datetime | None = None) -> None:
    """Drop ledger lines older than the undo-window-safe cutoff, under the sidecar lock.

    Rewrites the data file via an ATOMIC ``os.replace`` (``utils._atomic_write`` writes
    a temp file + renames), NOT an in-place ``r+`` truncate. Because the caller holds the
    SIDECAR lock (``_ledger_flock``), not a flock on the data file's own fd, the inode
    swap can never orphan the lock or lose a concurrent append -- and a lock-free reader
    sees the whole old OR whole new file, never the torn/duplicated middle an in-place
    rewrite exposes during its write->truncate window (and a crash mid-rewrite leaves the
    intact old file rather than corrupting the audit ledger).

    Best-effort: any error is swallowed by the caller. The rewrite only happens when a
    line is CONFIDENTLY datable as older than the cutoff (``_event_timestamp`` returns
    None for a torn/undateable line, which is always kept) -- so a quiet ledger never
    pays the rewrite cost, and an event inside the undo window is never dropped.

    Hot path: ``append_event`` calls this on EVERY append, so the common case (nothing
    stale) must stay cheap. Events append in time order, so the FIRST datable line is
    the OLDEST; peeking just it short-circuits the full O(n) read+parse when the oldest
    event is still within the cutoff (only a genuinely-stale head pays the full scan).
    """
    cutoff = _prune_cutoff(now)
    oldest = _oldest_datable_timestamp(target)
    if oldest is not None and oldest >= cutoff:
        return  # the oldest event is still in-window -> nothing prunable, skip the scan
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
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
    _atomic_write(target, "\n".join(kept) + ("\n" if kept else ""))


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
    # Contract 1: hold the exclusive SIDECAR flock for the whole append+prune so
    # concurrent writers (heartbeat cron + user CLI + every U1-U6 caller that goes
    # through append_event -- the sole writer) can never interleave a torn JSONL line.
    # The lock is on <ledger>.lock, NOT the data fd, so the H10 retention prune can
    # atomically os.replace the data file without orphaning the lock or losing an
    # append. flock is released when the `with` block exits, including on exception.
    with _ledger_flock(target):
        with target.open("a", encoding="utf-8") as handle:
            handle.write(rendered + "\n")
            handle.flush()
        # H10 Part B: APPEND FIRST, prune SECOND (best-effort) under the SAME lock --
        # mirroring outbox.deliver_once. The just-written event MUST survive; pruning
        # only drops entries OLDER than the undo-window-safe cutoff, so it can never
        # delete a fresh append or an in-window audit/undo/approval event. Any prune
        # fault is contained here and never propagates out of append_event.
        try:
            _prune_stale_locked(target)
        except Exception:  # noqa: BLE001 -- prune is best-effort; the append is committed
            pass
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
