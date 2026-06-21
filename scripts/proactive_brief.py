#!/usr/bin/env python3
"""U6 proactive brief/debrief + focus-block cron entry point.

One module, four flows, ALL sharing one ironclad order (mustFix #5):

    resolve+PROVE delivery target FIRST  ->  do calendar/task work  ->  push to
    the PROVEN target only (gate<->message seam).

A push to an unprovable target is blocked before any freebusy or brief assembly
runs, and ``delivery_target_proof_failed`` is logged; nothing is sent.

Flows (selected by ``--mode``):

* ``brief``   -- daily morning context brief (idempotent via proactive-state).
* ``prebrief``-- ``*/5`` scan: for each upcoming event in the lead window, send a
  pre-brief ONCE (idempotent), and re-prompt any OPEN debrief loop (closes only
  on capture/skip, never by time).
* ``slip``    -- check active focus blocks; slide a slipped agent-owned block to
  the next free window via ``gog calendar update`` (NEVER delete+create).
* ``friday``  -- Friday next-week priority proposal to the weekly topic. It NEVER
  writes U3 ``focus-state.json`` -- it only proposes.

Brief CONTENT is built by small pure helpers (testable without a gateway); the
calendar I/O is the injectable ``calendar_blocks`` seam; the delivery proof is
``proactive_delivery``. NO-RAW-ERROR-LEAK: ``main`` wraps the whole run and prints
a safe envelope on any failure, exiting 0 so cron does not treat it as a failure.
"""

from __future__ import annotations

import argparse
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import autonomy_gate
import calendar_blocks
import cos_config
import error_envelope
import focus_calendar
import focus_state
import proactive_delivery
import proactive_state
from standup_common import flatten_calendar_events, get_calendar_events
from task_ledger import append_event, new_event
from task_records import active_records, load_records

# How far ahead the `*/5` cron scans for upcoming events to pre-brief (spec OQ-4).
PRE_BRIEF_LEAD_WINDOW_MINUTES = 15
SAFE_ENVELOPE = "PROACTIVE_BRIEF_ERROR: internal error logged, no push this cycle"
# add_task prints "✅ Added ... (<task_id>)"; capture the id from the parens.
_ADDED_TASK_ID_RE = re.compile(r"\(([A-Za-z0-9._:-]+)\)\s*$")

Send = Callable[[dict[str, Any], str], Any]
# A subprocess boundary for the canonical ``tasks.py add`` CLI; injectable so the
# debrief-capture test runs without spawning a real subprocess.
ShellRunner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]

COMMITMENT_ADD_TIMEOUT_SECONDS = 20


def _default_shell_runner(cmd: list[str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(cmd, capture_output=True, text=True,
                          timeout=COMMITMENT_ADD_TIMEOUT_SECONDS)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _log(event_type: str, *, task_id: str | None = None, **metadata: Any) -> None:
    """Append a U6 ledger event (append-only, flocked by append_event)."""
    append_event(
        new_event(event_type, task_id=task_id, source="agent_autonomous",
                  actor="proactive_brief", metadata=metadata)
    )


# --- Pure brief-content helpers (no I/O, unit-testable) --------------------

def _parse_event_start(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _event_has_ended(entry: dict[str, Any], *, ref: datetime) -> bool:
    """True if a pre-brief entry's event has fully ENDED by ``ref``.

    A debrief is meaningful only AFTER the event ends -- nudging mid-meeting makes
    no sense. Uses ``event_end`` when present; falls back to ``event_start`` for an
    entry written before end was tracked (a started-but-untimed event is treated as
    ended, the prior behaviour). An entry with no parseable time is treated as
    ended (better to offer a debrief than to silently never close the loop).
    """
    end = _parse_event_start(entry.get("event_end")) or _parse_event_start(entry.get("event_start"))
    return end is None or end <= ref


def upcoming_events(events: list[dict[str, Any]], *, now: datetime, lead_window_minutes: int) -> list[dict[str, Any]]:
    """Events starting within the next ``lead_window_minutes`` (sorted by start).

    An event with no parseable start is skipped -- the pre-brief only fires for an
    event it can time. The ``event_id`` is the gateway event id; we fall back to a
    summary+start composite so an event with no id still de-duplicates per day.
    """
    horizon = now + timedelta(minutes=lead_window_minutes)
    upcoming: list[tuple[datetime, dict[str, Any]]] = []
    for ev in events:
        start = _parse_event_start(ev.get("start"))
        if start is None or start < now or start > horizon:
            continue
        upcoming.append((start, ev))
    upcoming.sort(key=lambda pair: pair[0])
    return [ev for _start, ev in upcoming]


def event_key(event: dict[str, Any]) -> str:
    """A stable per-day identity for an event (its id, or summary@start fallback)."""
    return str(event.get("event_id") or event.get("id") or f"{event.get('summary')}@{event.get('start')}")


def daily_brief_text(events: list[dict[str, Any]], active: list[Any]) -> str:
    """Compose the morning daily-brief body from today's calendar + active tasks."""
    lines = ["🌅 Good morning. Today's context:"]
    meeting_count = len(events)
    lines.append(f"  Calendar: {meeting_count} event{'s' if meeting_count != 1 else ''} today.")
    overdue = _most_overdue(active)
    if overdue is not None:
        lines.append(f'  Most pressing: "{overdue.title}" [{overdue.canonical_id}].')
    lines.append(f"  Active tasks: {len(active)}.")
    return "\n".join(lines)


def pre_brief_text(event: dict[str, Any]) -> str:
    """Compose a pre-brief body for an upcoming event."""
    summary = event.get("summary") or "(untitled event)"
    start = event.get("start") or ""
    return (
        f'📋 Pre-brief for "{summary}" (starts {start}).\n'
        f"Capture commitments after with: /debrief {summary}"
    )


def debrief_followup_text(entry: dict[str, Any]) -> str:
    """Gentle re-prompt for an OPEN debrief loop (NAG-CLOSES-ONLY-ON-ACK)."""
    summary = entry.get("event_summary") or "(your last event)"
    return (
        f'📝 Did you capture commitments from "{summary}"? '
        "Reply with notes (\"I will X by DATE\") or 'skip' to close."
    )


def slip_notice_text(block: dict[str, Any], new_start: str, new_end: str) -> str:
    """Notice that a slipped focus block was auto-slid to a new free window."""
    title = block.get("task_title") or block.get("task_id") or "(focus block)"
    return (
        f'⏱️ "{title}" focus block slipped (still open at its start). '
        f"Moved to {new_start}–{new_end} (next free window, no conflicts)."
    )


def friday_proposal_text(active: list[Any]) -> str:
    """Compose the Friday next-week priority proposal (proposal only -- no write)."""
    top = _ranked_active(active)[:3]
    lines = ["🔮 Chief-of-Staff: next-week priority proposal.", "  Proposed Defended Three:"]
    for idx, record in enumerate(top, start=1):
        lines.append(f'  {idx}. "{record.title}" [{record.canonical_id}]')
    if not top:
        lines.append("  (no active tasks to propose)")
    lines.append('  Approve with "approve", or "adjust [task-id]".')
    return "\n".join(lines)


def _overdue_days(due: str | None, *, ref: datetime) -> int:
    if not due:
        return -10_000  # no due date sorts last
    try:
        due_date = datetime.strptime(due, "%Y-%m-%d").date()
    except ValueError:
        return -10_000
    return (ref.date() - due_date).days


def _ranked_active(active: list[Any], *, ref: datetime | None = None) -> list[Any]:
    """Active tasks ordered by overdue-ness (most overdue first), stable by title."""
    reference = ref or _now()
    return sorted(active, key=lambda r: (-_overdue_days(r.due, ref=reference), r.title or ""))


def _most_overdue(active: list[Any]) -> Any | None:
    ranked = _ranked_active(active)
    return ranked[0] if ranked else None


def parse_commitments(notes: str) -> list[dict[str, str]]:
    """Parse debrief free-text into structured commitment task specs.

    Recognises the spec's quick format -- one commitment per sentence/line, e.g.
    "I will send Q3 budget draft by 2026-06-30. Martin will review by 2026-07-02".
    Each spec is ``{"title", "due"}`` (``due`` may be empty). This is a pure parser
    so the debrief capture is testable without a board write; the caller turns each
    spec into a task and logs ``commitment_task_created``.

    Splits on newlines and on a sentence boundary (a period FOLLOWED BY whitespace),
    NOT on every ``.`` -- so a date (``2026-06-30``) or an abbreviation (``Inc.``) is
    not chopped mid-title. A trailing sentence period is then stripped.
    """
    commitments: list[dict[str, str]] = []
    for chunk in re.split(r"\n|(?<=\.)\s+", notes):
        chunk = chunk.strip().rstrip(".").strip()
        if not chunk:
            continue
        if not re.search(r"\bwill\b", chunk, re.IGNORECASE):
            continue
        due = ""
        date_match = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", chunk)
        if date_match:
            due = date_match.group(1)
        # Drop a trailing "by <date>" so the title reads cleanly.
        title = re.sub(r"\s+by\s+\d{4}-\d{2}-\d{2}\s*$", "", chunk, flags=re.IGNORECASE).strip()
        commitments.append({"title": title, "due": due})
    return commitments


# --- Debrief capture (reactive: user notes -> commitment tasks) ------------

def _create_commitment_task(spec: dict[str, str], *, runner: "ShellRunner | None") -> str | None:
    """Add ONE commitment task via the canonical ``tasks.py add`` CLI; return its id.

    The board write goes through the existing add path (which honours U3's capacity
    cap and atomic write) -- U6 never reimplements a board writer. ``--force-parking``
    is deliberately NOT used: a parking-lot add prints ``✅ Added to Parking Lot: ...
    [n/cap]`` (no trailing ``(task_id)``), which is unparseable, so a force-parked
    commitment would be misread as a failure -- the loop would never close and a
    retry would duplicate it. Instead an over-cap add fails (the CLI exits non-zero),
    which the caller treats as a real failure that keeps the loop OPEN so the user
    is nudged to make room -- never silently lost, never duplicated. Returns the new
    task id parsed from the ``✅ Added ... (task_id)`` output, or None on any failure.
    """
    run = runner or _default_shell_runner
    cmd = ["python3", str(Path(__file__).resolve().parent / "tasks.py"), "add", spec["title"]]
    if spec.get("due"):
        cmd += ["--due", spec["due"]]
    result = run(cmd)
    if result.returncode != 0:
        return None
    match = _ADDED_TASK_ID_RE.search((result.stdout or "").strip())
    return match.group(1) if match else None


def run_debrief_capture(reference: str, notes: str, *,
                        runner: "ShellRunner | None" = None) -> dict[str, Any]:
    """Capture a user's debrief notes into commitment tasks and close the loop.

    The reactive ``/debrief <reference>`` path (spec §2.4). ``reference`` is what
    the user typed -- the event SUMMARY the pre-brief advertised, or the raw stored
    key -- so the OPEN loop is resolved by summary-or-key
    (``resolve_open_debrief``), and ALL state ops use that loop's actual stored
    ``event_id``. This closes two holes: the loop is always the one closed (no
    endless re-prompts from a key mismatch), and a reference with no matching OPEN
    loop REFUSES -- it never silently creates tasks against a phantom loop and so
    cannot duplicate commitments on a retry.

    On a match: parse the notes into commitments, create each as a task, record the
    new task ids (sets ``debrief_captured_at`` -> CLOSED), and emit
    ``commitment_task_created`` + ``debrief_captured``. A "skip" closes via skip.
    Notes that parse to ZERO commitments (and are not "skip") do NOT close the loop
    -- the user wrote something that was not understood as a commitment, so the loop
    stays open to be retried rather than silently dropping it. Runs UNDER the
    proactive-state lock so a concurrent cron re-prompt cannot race the close.
    Returns ``{captured, task_ids[, reason]}``.
    """
    return proactive_state.transition(
        lambda state: _capture_under_lock(state, reference, notes, runner))


def _capture_under_lock(state: dict[str, Any], reference: str, notes: str,
                        runner: "ShellRunner | None") -> dict[str, Any]:
    """The locked body of run_debrief_capture: resolve, create commitments, close."""
    entry = proactive_state.resolve_open_debrief(state, reference)
    if entry is None:
        # No OPEN loop for this reference: it was already captured/skipped, or the
        # reference is unknown. Refuse -- never create tasks against a phantom loop.
        return {"captured": False, "task_ids": [], "reason": "no_open_debrief"}
    event_id = entry["event_id"]

    if notes.strip().lower() == "skip":
        proactive_state.skip_debrief(state, event_id)
        return {"captured": False, "task_ids": []}

    commitments = parse_commitments(notes)
    if not commitments:
        # The user wrote notes that parsed to no commitment (e.g. no "will" phrasing).
        # Do NOT close the loop -- a real commitment would be silently lost. Keep it
        # open and ask the user to rephrase on the next nudge.
        return {"captured": False, "task_ids": [], "reason": "no_commitment_parsed"}

    # A retry after a partial failure re-submits the SAME notes, so skip any
    # commitment whose title was already created on a prior attempt (recorded on the
    # entry) -- the dedup that stops a retry from duplicating board tasks while still
    # letting the previously-failed ones through.
    already = set(entry.get("created_commitment_titles") or [])
    task_ids: list[str] = []
    failed: list[str] = []
    for spec in commitments:
        if spec["title"] in already:
            continue  # already created on a prior attempt -- do not duplicate
        task_id = _create_commitment_task(spec, runner=runner)
        if task_id is None:
            failed.append(spec["title"])
            continue
        task_ids.append(task_id)
        already.add(spec["title"])
        _log("commitment_task_created", task_id=task_id, title=spec["title"], due=spec.get("due"))

    # NEVER lose a commitment: if ANY commitment failed to create, the loop stays
    # OPEN so the user can retry. Created ids + titles are recorded so a retry dedups.
    if failed:
        proactive_state.record_partial_debrief(state, event_id, task_ids, sorted(already))
        _log("debrief_captured", event_key=event_id, commitments_task_ids=task_ids,
             failed_commitments=failed, partial=True)
        return {"captured": False, "task_ids": task_ids, "reason": "commitment_create_failed",
                "failed": failed}

    proactive_state.capture_debrief(state, event_id, task_ids)
    _log("debrief_captured", event_key=event_id, commitments_task_ids=task_ids)
    return {"captured": True, "task_ids": task_ids}


# --- Flows (orchestration; calendar/delivery are injected seams) -----------

def _load_active(personal: bool = False) -> list[Any]:
    """Read the active task set (READ-ONLY; degrade to empty on a missing board)."""
    try:
        _file, _content, records = load_records(personal=personal)
    except FileNotFoundError:
        return []
    return list(active_records(records))


def _gate_calendar_write(act_type: str, task_id: str, snapshot: dict[str, Any]) -> dict[str, Any]:
    """Record an autonomous calendar write in the autonomy audit log via the gate.

    A focus-block create/move is a rung-3 (monitored-auto) reversible act: its
    reversal substrate is NOT a board line but the event record itself (event_id +
    calendar_id + window), which is what ``snapshot`` carries -- so the act is
    logged with a real undo payload and the rung config is actually exercised. No
    ``delivery_target`` is bound (the Telegram notice is a SEPARATE gated push).
    Returns the gate result; a blocked gate refuses the write upstream.
    """
    return autonomy_gate.gate(act_type, task_id=task_id, unit="U6",
                              snapshot_provider=lambda: snapshot)


def _record_calendar_write_audit(act_type: str, task_id: str, snapshot: dict[str, Any],
                                 ledger_event: str, **ledger_meta: Any) -> None:
    """Best-effort audit (autonomy gate + ledger) for a SUCCESSFUL calendar write.

    Called AFTER the block has been appended to the locked ``cal_state`` and the gog
    write has succeeded, so the block is already tracked when this runs. Any failure
    here (a transient ledger/audit-log I/O error) is swallowed: it must NOT abort the
    enclosing ``focus_calendar.transition`` and discard the block append, which would
    orphan a real calendar event with no record. The audit is a breadcrumb; the
    tracked block is the source of truth for reversibility.
    """
    try:
        _gate_calendar_write(act_type, task_id, snapshot)
        _log(ledger_event, task_id=task_id, **ledger_meta)
    except OSError:
        pass


def _push(act_type: str, text: str, *, surface: str, send: Send | None,
          dry_run: bool, task_id: str | None = None,
          metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    """Prove FIRST, gate, assert, send -- the one push path every flow uses.

    On a dry-run only the proof runs (no gate act is logged, no send). On a real
    run the full seam runs and ``send`` is required.
    """
    if dry_run:
        proof = proactive_delivery.resolve_target(surface)
        return {"sent": False, "reason": "dry_run" if proof["ok"] else proof["reason"],
                "text": text}
    gated = proactive_delivery.prove_and_gate(act_type, surface=surface,
                                              task_id=task_id, metadata=metadata)
    if not gated["ok"]:
        return {"sent": False, "reason": gated["reason"], "stage": gated.get("stage")}
    sent = proactive_delivery.authorised_send(gated["act_id"], gated["delivery_target"],
                                              text, send=send)
    if not sent["ok"]:
        return {"sent": False, "reason": sent["reason"], "stage": "assert"}
    return {"sent": True, "act_id": gated["act_id"],
            "delivery_target": gated["delivery_target"], "text": text}


def run_daily_brief(*, now: datetime | None = None, dry_run: bool = False,
                    send: Send | None = None) -> dict[str, Any]:
    """Send today's daily brief once (idempotent). Returns ``{sent, reason}``.

    The due-check + push + mark run UNDER the proactive-state lock so two cron modes
    firing in the same minute cannot both send (the idempotency claim is atomic).
    """
    if dry_run:
        return _push("brief_sent", "", surface="standup", send=send, dry_run=True)

    def _claim_and_send(state: dict[str, Any]) -> dict[str, Any]:
        if not proactive_state.daily_brief_due(state):
            return {"sent": False, "reason": "already_sent"}
        events = flatten_calendar_events(get_calendar_events(trigger="proactive_brief"))
        text = daily_brief_text(events, _load_active())
        result = _push("brief_sent", text, surface="standup", send=send, dry_run=False)
        if result["sent"]:
            proactive_state.mark_daily_brief_sent(state)
            _log("brief_sent", brief_type="daily", delivery_target=result["delivery_target"])
        return result

    return proactive_state.transition(_claim_and_send)


def run_pre_brief_scan(*, now: datetime | None = None, dry_run: bool = False,
                       send: Send | None = None) -> dict[str, int]:
    """The ``*/5`` scan: pre-brief upcoming events once + re-prompt open debriefs.

    Idempotency is mandatory (spec §4.6): a pre-brief is sent at most once per
    event per day, gated on ``proactive-state.json``; an open debrief loop is
    re-prompted until capture/skip but never closed by time. The whole scan runs
    UNDER the proactive-state lock so two `*/5` fires in the same minute cannot
    both send the same brief (the idempotency claim is atomic).
    """
    ref = now or _now()
    counts = {"briefed": 0, "debrief_reprompts": 0, "blocked": 0}
    events = flatten_calendar_events(get_calendar_events(trigger="proactive_brief"))
    upcoming = upcoming_events(events, now=ref, lead_window_minutes=PRE_BRIEF_LEAD_WINDOW_MINUTES)

    def _scan(state: dict[str, Any]) -> None:
        for event in upcoming:
            _maybe_pre_brief(state, event, ref=ref, dry_run=dry_run, send=send, counts=counts)
        _reprompt_open_debriefs(state, ref=ref, dry_run=dry_run, send=send, counts=counts)

    if dry_run:
        _scan(proactive_state.load_proactive_state())  # no lock/persist on a dry-run
    else:
        proactive_state.transition(_scan)
    return counts


def _maybe_pre_brief(state, event, *, ref, dry_run, send, counts) -> None:
    """Send a pre-brief for ONE upcoming event if it has not been briefed today."""
    key = event_key(event)
    if not proactive_state.pre_brief_due(state, key):
        return
    result = _push("brief_sent", pre_brief_text(event), surface="standup",
                   send=send, dry_run=dry_run)
    if result["sent"]:
        proactive_state.mark_pre_brief_sent(
            state, key, event.get("summary") or "",
            event.get("start") or "", event.get("end") or "")
        proactive_state.open_debrief(state, key)  # debrief loop opens with the pre-brief
        _log("brief_sent", brief_type="pre_brief", event_key=key,
             delivery_target=result["delivery_target"])
        counts["briefed"] += 1
    elif not dry_run:
        counts["blocked"] += 1


def _reprompt_open_debriefs(state, *, ref, dry_run, send, counts) -> None:
    """Re-prompt every OPEN debrief loop whose event has ENDED, PACED per interval.

    The wait is on the event END (not its start), so a long meeting never gets a
    mid-meeting "capture commitments" prompt; the pacing keeps an ignored loop to
    at most one nudge per interval (NAG-CLOSES-ONLY-ON-ACK -- never closed by time).
    """
    interval = cos_config.debrief_reprompt_interval_minutes()
    for entry in state.get("pre_briefs", []):
        if not proactive_state.is_debrief_open(entry):
            continue
        if not _event_has_ended(entry, ref=ref):
            continue  # event is upcoming or still in progress -- nothing to debrief yet
        if not proactive_state.debrief_reprompt_due(entry, now=ref, interval_minutes=interval):
            continue  # nudged recently -- respect the pacing interval
        result = _push("brief_sent", debrief_followup_text(entry), surface="standup",
                       send=send, dry_run=dry_run)
        if result["sent"]:
            proactive_state.mark_debrief_reprompted(entry, now=ref)
            counts["debrief_reprompts"] += 1
        elif not dry_run:
            counts["blocked"] += 1


def run_friday_proposal(*, now: datetime | None = None, dry_run: bool = False,
                        send: Send | None = None) -> dict[str, Any]:
    """Send the Friday next-week proposal once (idempotent). NEVER writes U3 state.

    The due-check + push + mark run UNDER the proactive-state lock so a concurrent
    fire cannot double-send.
    """
    if dry_run:
        return _push("brief_sent", "", surface="weekly", send=send, dry_run=True)

    def _claim_and_send(state: dict[str, Any]) -> dict[str, Any]:
        if not proactive_state.friday_proposal_due(state):
            return {"sent": False, "reason": "already_sent"}
        text = friday_proposal_text(_load_active())
        result = _push("brief_sent", text, surface="weekly", send=send, dry_run=False)
        if result["sent"]:
            proactive_state.mark_friday_proposal_sent(state)
            _log("brief_sent", brief_type="friday_proposal", delivery_target=result["delivery_target"])
        return result

    return proactive_state.transition(_claim_and_send)


def _block_has_slipped(block: dict[str, Any], *, ref: datetime, active_ids: set[str]) -> bool:
    """True if ``block`` has slipped: its window has fully ENDED and the task is
    still active.

    Keys off the block END, not its start, so a block currently in its active
    window (started but not yet ended) is NOT moved out from under the user
    mid-session. Falls back to start for an untimed end. A block whose task is no
    longer active (done/parked) has not slipped -- it is just stale and left alone.
    """
    end = _parse_event_start(block.get("end")) or _parse_event_start(block.get("start"))
    return end is not None and end <= ref and block.get("task_id") in active_ids


def _block_duration(block: dict[str, Any]) -> timedelta:
    """A block's original duration, defaulting to 1h when its window is unparseable."""
    start = _parse_event_start(block.get("start"))
    end = _parse_event_start(block.get("end"))
    return (end - start) if (start and end and end > start) else timedelta(hours=1)


def _recover_one_block(cal_state: dict[str, Any], block: dict[str, Any], calendar_id: str,
                       fb_ids: list[str], *, ref: datetime, new_start: str, new_end: str,
                       runner: calendar_blocks.Runner | None) -> dict[str, Any]:
    """Move ONE slipped block to [new_start, new_end] via UPDATE, mutating LOCKED state.

    Returns ``{"status": "moved", "new_start", "new_end"}`` or ``{"status":
    "refused"}``. Mutates ``cal_state`` in place; the surrounding
    ``focus_calendar.transition`` persists once on exit. The move is a ``gog calendar
    update`` so the block keeps its id (reversible); an overlap/unknown freebusy or a
    non-agent event refuses the move (block left in place, ``calendar_block_refused``
    logged) -- NEVER-OVERBOOK-EXTERNAL holds even during recovery. ``new_start`` /
    ``new_end`` are supplied by the caller's cursor so multiple slipped blocks in one
    pass do not all stack on the same window.

    The autonomy gate is recorded AFTER the gog write succeeds (with the real
    event_id), so a freebusy/external refusal leaves NO phantom ``executed`` audit
    entry and the recorded snapshot always has an event_id to undo against.
    """
    task_id = block.get("task_id")
    try:
        moved = calendar_blocks.move_focus_block(
            calendar_id, block["event_id"], task_id, new_start, new_end,
            freebusy_calendar_ids=fb_ids, runner=runner, trigger="proactive_brief")
    except (calendar_blocks.OverbookError, calendar_blocks.ExternalEventError) as exc:
        reason = getattr(exc, "reason", "external_event")
        focus_calendar.record_dry_run(cal_state, "calendar.move_refused",
                                      {"event_id": block.get("event_id"), "task_id": task_id},
                                      {"reason": reason})
        _log("calendar_block_refused", task_id=task_id, reason=reason, event_id=block.get("event_id"))
        return {"status": "refused"}
    # The move succeeded: UPDATE the tracked block's window FIRST (its event_id is
    # unchanged) so state matches the calendar, THEN record the audit best-effort
    # with the OLD window as the undo substrate (move it back). A logging failure
    # must not abort the transition and revert the tracked window.
    old_start, old_end = block.get("start"), block.get("end")
    block["start"], block["end"] = moved["start"], moved["end"]
    block["slip_count"] = (block.get("slip_count") or 0) + 1
    block["last_slipped_at"] = ref.isoformat()
    focus_calendar.record_dry_run(cal_state, "calendar.update", moved["request"], moved)
    _record_calendar_write_audit(
        "calendar_block_moved", task_id,
        {"calendar_id": calendar_id, "event_id": block["event_id"],
         "old_start": old_start, "old_end": old_end},
        "calendar_block_moved", event_id=block["event_id"],
        new_start=moved["start"], new_end=moved["end"])
    return {"status": "moved", "new_start": moved["start"], "new_end": moved["end"]}


def _local_day_start(ref: datetime, *, day_start_hour: int, tz_offset_hours: int) -> datetime:
    """The day-start anchor as a tz-aware timestamp in the user's LOCAL clock.

    ``ref`` may be UTC (the cron is UTC-scheduled); this converts it to
    ``UTC+tz_offset_hours``, pins it to ``day_start_hour:00`` LOCAL, and returns the
    tz-aware result. So a 09:00 local anchor lands at the user's morning regardless
    of the cron's clock -- the UTC-anchor bug. A fixed offset (no tz database) is
    good enough for a focus-block start hint.
    """
    local_tz = timezone(timedelta(hours=tz_offset_hours))
    local_ref = ref.astimezone(local_tz)
    return local_ref.replace(hour=day_start_hour, minute=0, second=0, microsecond=0)


def _create_cursor_start(cal_state: dict[str, Any], *, day_start: datetime, ref: datetime) -> datetime:
    """The earliest start for a new block: after the day-start, NOW, and every
    remaining block's end.

    Clamps to ``max(day_start, ref)`` so a create firing after the anchor hour never
    places a block in the past, then advances past the latest end of any existing
    block so a NEW priority on a re-run does not overlap one already placed today
    (the agent never overbooks its OWN calendar). Past-day blocks have already been
    pruned, so every remaining block is today-or-future.
    """
    cursor = max(day_start, ref)
    for block in cal_state.get("active_blocks", []):
        end = _parse_event_start(block.get("end"))
        if end is not None and end > cursor:
            cursor = end
    return cursor


def run_create_blocks(*, now: datetime | None = None, dry_run: bool = False,
                      send: Send | None = None,
                      runner: calendar_blocks.Runner | None = None,
                      day_start_hour: int | None = None,
                      tz_offset_hours: int | None = None) -> dict[str, int]:
    """Create freebusy-gated focus blocks for today's Defended Three.

    Reads the day's priorities from U3's ``focus-state.json`` (READ-ONLY -- U6
    never writes it), sizes each block from ``estimate_minutes``, and places them
    back-to-back starting at the user's LOCAL morning -- each via the freebusy-gated
    ``create_focus_block`` (NEVER-OVERBOOK-EXTERNAL: an overlap/unknown freebusy
    refuses that block; the others still place). Created blocks are recorded in
    ``focus-calendar.json`` and a confirmation notice is pushed through the proven
    delivery seam. Idempotent: a priority that already has an active block is
    skipped. Degrades silently when no focus calendar is configured.

    The morning anchor is the user's LOCAL clock: ``day_start_hour`` (default from
    ``FOCUS_BLOCK_DAY_START_HOUR``) interpreted in ``UTC + tz_offset_hours`` (default
    from ``FOCUS_TZ_OFFSET_HOURS``), so a UTC-scheduled cron still lands blocks in
    the morning rather than at UTC midnight.
    """
    ref = now or _now()
    start_hour = day_start_hour if day_start_hour is not None else cos_config.focus_block_day_start_hour()
    offset = tz_offset_hours if tz_offset_hours is not None else cos_config.focus_tz_offset_hours()
    today_local = _local_day_start(ref, day_start_hour=start_hour, tz_offset_hours=offset).date().isoformat()
    counts = {"created": 0, "refused": 0, "skipped": 0}
    if not focus_calendar.load_focus_calendar().get("agent_calendar_id"):
        return counts  # no focus calendar configured -- degrade silently
    # Only place blocks for an APPROVED, CURRENT (today's) Defended Three -- the same
    # is_current + STATUS_APPROVED gate every other focus-state consumer applies. A
    # stale (yesterday's) or merely-proposed/unapproved plan must NOT drive calendar
    # writes. ``today_local`` is the placement date, so the staleness check uses it.
    fs = focus_state.load_focus_state()
    if focus_state.status_for_today(fs, reference_date=today_local) != focus_state.STATUS_APPROVED:
        return counts  # no approved plan for today -- nothing to place
    priorities = (fs or {}).get("daily_priorities") or []
    fb_ids = calendar_blocks.external_calendar_ids()
    created_titles: list[str] = []

    day_start = _local_day_start(ref, day_start_hour=start_hour, tz_offset_hours=offset)

    def _create_under_lock(cal_state: dict[str, Any]) -> None:
        calendar_id = cal_state.get("agent_calendar_id")
        # Prune past-day blocks so active_blocks does not grow unbounded and a stale
        # block never suppresses today's placement.
        focus_calendar.prune_blocks_before(cal_state, today_local)
        # The cursor starts no earlier than NOW (never place a block in the past when
        # the cron fires after the anchor hour) and after every existing same-day
        # block's end (so a NEW priority on a re-run never overlaps an already-placed
        # block on the agent's own focus calendar).
        cursor = _create_cursor_start(cal_state, day_start=day_start, ref=ref)
        for row in priorities:
            task_id = row.get("task_id")
            # Date-scoped idempotency: only a block placed TODAY suppresses a re-place.
            if not task_id or focus_calendar.block_for_task_on_date(cal_state, task_id, today_local):
                counts["skipped"] += 1
                continue
            minutes = int(row.get("estimate_minutes") or 60)
            start_iso, end_iso = cursor.isoformat(), (cursor + timedelta(minutes=minutes)).isoformat()
            cursor = cursor + timedelta(minutes=minutes)
            if dry_run:
                counts["created"] += 1
                continue
            outcome = _place_one_block(cal_state, calendar_id, task_id, row.get("title") or "",
                                       start_iso, end_iso, fb_ids, ref=ref, runner=runner)
            counts[outcome] += 1
            if outcome == "created":
                created_titles.append(row.get("title") or task_id)

    if dry_run:
        _create_under_lock(focus_calendar.load_focus_calendar())  # no lock/persist on dry-run
    else:
        focus_calendar.transition(_create_under_lock)

    if created_titles and not dry_run:
        notice = "🗓️ Created focus blocks: " + ", ".join(f'"{t}"' for t in created_titles)
        _push("brief_sent", notice, surface="standup", send=send, dry_run=False)
    return counts


def _place_one_block(cal_state, calendar_id, task_id, title, start_iso, end_iso, fb_ids,
                     *, ref: datetime, runner) -> str:
    """Create ONE freebusy-gated focus block, mutating the LOCKED state. -> created|refused.

    Mutates ``cal_state`` in place; the surrounding ``focus_calendar.transition``
    persists once on exit (this helper never writes the file itself). The freebusy
    gate inside ``create_focus_block`` is the overbook guard; the autonomy gate is
    recorded AFTER the write succeeds (with the real event_id), so a freebusy
    refusal leaves NO phantom ``executed`` audit entry and the snapshot always has
    an event_id to undo against.
    """
    try:
        block = calendar_blocks.create_focus_block(
            calendar_id, task_id, title, start_iso, end_iso,
            freebusy_calendar_ids=fb_ids, runner=runner, trigger="proactive_brief")
    except calendar_blocks.OverbookError as exc:
        focus_calendar.record_dry_run(cal_state, "calendar.create_refused",
                                      {"task_id": task_id, "start": start_iso}, {"reason": exc.reason})
        _log("calendar_block_refused", task_id=task_id, reason=exc.reason)
        return "refused"
    # The write succeeded: TRACK the block in the locked state FIRST so the event is
    # never orphaned, THEN record the audit best-effort (a logging failure must not
    # abort the transition and discard this append).
    cal_state.setdefault("active_blocks", []).append({
        "event_id": block["event_id"], "task_id": task_id, "task_title": title,
        "start": block["start"], "end": block["end"], "created_at": ref.isoformat(),
        "slip_count": 0, "last_slipped_at": None,
    })
    focus_calendar.record_dry_run(cal_state, "calendar.create", block["request"], block)
    _record_calendar_write_audit(
        "calendar_block_created", task_id,
        {"calendar_id": calendar_id, "task_id": task_id, "event_id": block["event_id"],
         "start": block["start"], "end": block["end"]},
        "calendar_block_created", event_id=block["event_id"],
        start=block["start"], end=block["end"])
    return "created"


def run_slip_recovery(*, now: datetime | None = None, dry_run: bool = False,
                      send: Send | None = None,
                      runner: calendar_blocks.Runner | None = None) -> dict[str, int]:
    """Slide every slipped agent-owned focus block to the next free window + notify.

    Recovery is a ``gog calendar update`` (NEVER delete+create). The moves run UNDER
    the focus-calendar lock so an overlapping create/slip run cannot lose the update.
    After the lock releases, a slip notice for each moved block is pushed through the
    proven delivery seam, so the user is told their focus block was rescheduled
    rather than having it silently moved out from under them. Degrades silently when
    no focus calendar is configured. A dry-run counts the slipped blocks without
    touching the calendar or sending.
    """
    ref = now or _now()
    counts = {"moved": 0, "refused": 0, "notified": 0}
    if not focus_calendar.load_focus_calendar().get("agent_calendar_id"):
        return counts  # no focus calendar configured -- degrade silently
    active_ids = {r.canonical_id for r in _load_active() if r.canonical_id}
    # Freebusy-check the slid window against EXTERNAL (human) calendars only -- the
    # block being moved still occupies its old slot on the focus calendar, which
    # FreeBusy cannot exclude per-event, so including the focus calendar would
    # always self-overlap and refuse the move.
    fb_ids = calendar_blocks.external_calendar_ids()
    notices: list[tuple[str, str | None]] = []  # (text, task_id) collected under the lock

    def _slip_under_lock(cal_state: dict[str, Any]) -> None:
        calendar_id = cal_state.get("agent_calendar_id")
        # Cursor for the next free window: blocks slid in one pass are placed
        # back-to-back from ref+1h so multiple slipped blocks do not stack/overlap.
        cursor = ref + timedelta(hours=1)
        for block in list(cal_state.get("active_blocks", [])):
            if not _block_has_slipped(block, ref=ref, active_ids=active_ids):
                continue
            duration = _block_duration(block)
            new_start, new_end = cursor.isoformat(), (cursor + duration).isoformat()
            if dry_run:
                counts["moved"] += 1
                cursor = cursor + duration
                continue
            result = _recover_one_block(cal_state, block, calendar_id, fb_ids, ref=ref,
                                        new_start=new_start, new_end=new_end, runner=runner)
            if result["status"] != "moved":
                counts["refused"] += 1
                continue
            cursor = cursor + duration  # advance past this block so the next does not overlap
            counts["moved"] += 1
            notices.append((slip_notice_text(block, result["new_start"], result["new_end"]),
                            block.get("task_id")))

    if dry_run:
        _slip_under_lock(focus_calendar.load_focus_calendar())  # no lock/persist on dry-run
    else:
        focus_calendar.transition(_slip_under_lock)

    # Push the slip notices AFTER releasing the focus-calendar lock (the push touches
    # the autonomy log + ledger, not the focus calendar) so no lock is held across I/O.
    for text, task_id in notices:
        if _push("brief_sent", text, surface="standup", send=send, dry_run=False,
                 task_id=task_id)["sent"]:
            counts["notified"] += 1
    return counts


# Cron/scheduled modes -> the name of the flow function on this module. Stored as
# NAMES (resolved via getattr at call time), not function objects, so a test can
# monkeypatch a flow and the dispatcher honours it. Every one takes the uniform
# ``(dry_run, send)`` signature (calendar flows default ``runner`` to the real
# subprocess). The reactive ``debrief-capture`` mode is handled separately -- it
# takes user-supplied notes, not the uniform signature.
_MODE_FLOWS: dict[str, str] = {
    "brief": "run_daily_brief",
    "prebrief": "run_pre_brief_scan",
    "slip": "run_slip_recovery",
    "friday": "run_friday_proposal",
    "create": "run_create_blocks",
}
DEBRIEF_CAPTURE_MODE = "debrief-capture"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="proactive_brief.py", description=__doc__)
    parser.add_argument("--mode", choices=sorted([*_MODE_FLOWS, DEBRIEF_CAPTURE_MODE]),
                        required=True, help="which proactive flow to run")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be pushed without sending or writing state")
    parser.add_argument("--event-key", help="debrief-capture: the event whose loop to close")
    parser.add_argument("--notes", help="debrief-capture: the user's commitment notes (or 'skip')")
    args = parser.parse_args(argv)
    try:
        # The reactive debrief-capture path closes a loop from user notes; it does
        # not push text via the cron announce, so it is handled before the uniform
        # send-collector flows.
        if args.mode == DEBRIEF_CAPTURE_MODE:
            if not args.event_key or args.notes is None:
                parser.error("--event-key and --notes are required for debrief-capture")
            result = run_debrief_capture(args.event_key, args.notes)
            print(f"DEBRIEF_CAPTURE: captured={result['captured']} "
                  f"tasks={len(result['task_ids'])}")
            return 0
        # Every cron flow takes the same (dry_run, send) signature; the cron
        # announces the collected payloads to its explicit delivery.to.
        payloads: list[str] = []
        send = None if args.dry_run else (lambda _target, text: payloads.append(text))
        flow = globals()[_MODE_FLOWS[args.mode]]
        flow(dry_run=args.dry_run, send=send)
        for text in payloads:
            print(text)
            print()
        return 0
    except Exception as exc:  # noqa: BLE001 -- top-level NO-RAW-ERROR-LEAK boundary
        error_envelope.log_error(
            "proactive_brief", error_class=type(exc).__name__,
            message="proactive-brief run failed", raw=repr(exc),
            trigger=f"cron:proactive_brief:{args.mode}",
        )
        print(SAFE_ENVELOPE)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
