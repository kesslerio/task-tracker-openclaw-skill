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

import calendar_blocks
import cos_config
import error_envelope
import focus_calendar
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
    """
    commitments: list[dict[str, str]] = []
    for chunk in re.split(r"[.\n]", notes):
        chunk = chunk.strip()
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

    The board write goes through the existing add path (which honours U3's
    capacity cap and atomic write) -- U6 never reimplements a board writer.
    ``--force-parking`` routes an over-cap commitment to the parking lot rather
    than dropping it (a commitment must never be silently lost). Returns the new
    task id parsed from the CLI output, or None if the add failed.
    """
    run = runner or _default_shell_runner
    cmd = ["python3", str(Path(__file__).resolve().parent / "tasks.py"), "add",
           spec["title"], "--force-parking"]
    if spec.get("due"):
        cmd += ["--due", spec["due"]]
    result = run(cmd)
    if result.returncode != 0:
        return None
    match = _ADDED_TASK_ID_RE.search((result.stdout or "").strip())
    return match.group(1) if match else None


def run_debrief_capture(event_key: str, notes: str, *,
                        runner: "ShellRunner | None" = None) -> dict[str, Any]:
    """Capture a user's debrief notes into commitment tasks and close the loop.

    The reactive ``/debrief`` path (spec §2.4): parse the notes into commitments,
    create each as a task, record the new task ids in ``proactive-state.json``
    (which sets ``debrief_captured_at`` -> the loop is CLOSED), and emit
    ``commitment_task_created`` + ``debrief_captured`` ledger events. A "skip" (no
    commitments) closes the loop via skip instead. Returns ``{captured, task_ids}``.

    Idempotent: a re-invocation for a loop that is ALREADY closed (captured or
    skipped) is a no-op -- it never re-parses the notes or re-adds the commitment
    tasks, so a retry / double-submit cannot duplicate tasks (mirrors the
    ``pre_brief_due`` guard the cron flows use). Returns
    ``{captured: False, reason: "already_closed"}`` in that case.
    """
    state = proactive_state.load_proactive_state()
    entry = proactive_state.find_pre_brief(state, event_key)
    if entry is not None and not proactive_state.is_debrief_open(entry):
        return {"captured": False, "task_ids": [], "reason": "already_closed"}

    if notes.strip().lower() == "skip":
        proactive_state.skip_debrief(state, event_key)
        proactive_state.save_proactive_state(state)
        return {"captured": False, "task_ids": []}

    task_ids: list[str] = []
    for spec in parse_commitments(notes):
        task_id = _create_commitment_task(spec, runner=runner)
        if task_id is None:
            continue
        task_ids.append(task_id)
        _log("commitment_task_created", task_id=task_id, title=spec["title"], due=spec.get("due"))

    proactive_state.capture_debrief(state, event_key, task_ids)
    proactive_state.save_proactive_state(state)
    _log("debrief_captured", event_key=event_key, commitments_task_ids=task_ids)
    return {"captured": True, "task_ids": task_ids}


# --- Flows (orchestration; calendar/delivery are injected seams) -----------

def _load_active(personal: bool = False) -> list[Any]:
    """Read the active task set (READ-ONLY; degrade to empty on a missing board)."""
    try:
        _file, _content, records = load_records(personal=personal)
    except FileNotFoundError:
        return []
    return list(active_records(records))


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
    """Send today's daily brief once (idempotent). Returns ``{sent, reason}``."""
    ref = now or _now()
    state = proactive_state.load_proactive_state()
    if not proactive_state.daily_brief_due(state):
        return {"sent": False, "reason": "already_sent"}
    events = flatten_calendar_events(get_calendar_events(trigger="proactive_brief"))
    text = daily_brief_text(events, _load_active())
    result = _push("brief_sent", text, surface="standup", send=send, dry_run=dry_run)
    if result["sent"]:
        proactive_state.mark_daily_brief_sent(state)
        proactive_state.save_proactive_state(state)
        _log("brief_sent", brief_type="daily", delivery_target=result["delivery_target"])
    return result


def run_pre_brief_scan(*, now: datetime | None = None, dry_run: bool = False,
                       send: Send | None = None) -> dict[str, int]:
    """The ``*/5`` scan: pre-brief upcoming events once + re-prompt open debriefs.

    Idempotency is mandatory (spec §4.6): a pre-brief is sent at most once per
    event per day, gated on ``proactive-state.json``; an open debrief loop is
    re-prompted until capture/skip but never closed by time.
    """
    ref = now or _now()
    state = proactive_state.load_proactive_state()
    counts = {"briefed": 0, "debrief_reprompts": 0, "blocked": 0}

    events = flatten_calendar_events(get_calendar_events(trigger="proactive_brief"))
    for event in upcoming_events(events, now=ref, lead_window_minutes=PRE_BRIEF_LEAD_WINDOW_MINUTES):
        key = event_key(event)
        if not proactive_state.pre_brief_due(state, key):
            continue
        result = _push("brief_sent", pre_brief_text(event), surface="standup",
                       send=send, dry_run=dry_run)
        if result["sent"]:
            entry = proactive_state.mark_pre_brief_sent(
                state, key, event.get("summary") or "", event.get("start") or "")
            proactive_state.open_debrief(state, key)  # debrief loop opens with the pre-brief
            proactive_state.save_proactive_state(state)
            _log("brief_sent", brief_type="pre_brief", event_key=key,
                 delivery_target=result["delivery_target"])
            counts["briefed"] += 1
        elif not dry_run:
            counts["blocked"] += 1

    # Re-prompt every OPEN debrief loop whose event has ended (NAG-CLOSES-ONLY-ON-ACK),
    # PACED so an ignored loop is nudged at most once per interval rather than every
    # `*/5` scan (autoreview P3: no dozens-of-messages-a-day spam).
    interval = cos_config.debrief_reprompt_interval_minutes()
    for entry in state.get("pre_briefs", []):
        if not proactive_state.is_debrief_open(entry):
            continue
        start = _parse_event_start(entry.get("event_start"))
        if start is not None and start > ref:
            continue  # event has not happened yet -- nothing to debrief
        if not proactive_state.debrief_reprompt_due(entry, now=ref, interval_minutes=interval):
            continue  # nudged recently -- respect the pacing interval
        result = _push("brief_sent", debrief_followup_text(entry), surface="standup",
                       send=send, dry_run=dry_run)
        if result["sent"]:
            proactive_state.mark_debrief_reprompted(entry, now=ref)
            proactive_state.save_proactive_state(state)
            counts["debrief_reprompts"] += 1
        elif not dry_run:
            counts["blocked"] += 1

    return counts


def run_friday_proposal(*, now: datetime | None = None, dry_run: bool = False,
                        send: Send | None = None) -> dict[str, Any]:
    """Send the Friday next-week proposal once (idempotent). NEVER writes U3 state."""
    state = proactive_state.load_proactive_state()
    if not proactive_state.friday_proposal_due(state):
        return {"sent": False, "reason": "already_sent"}
    text = friday_proposal_text(_load_active())
    result = _push("brief_sent", text, surface="weekly", send=send, dry_run=dry_run)
    if result["sent"]:
        proactive_state.mark_friday_proposal_sent(state)
        proactive_state.save_proactive_state(state)
        _log("brief_sent", brief_type="friday_proposal", delivery_target=result["delivery_target"])
    return result


def _block_has_slipped(block: dict[str, Any], *, ref: datetime, active_ids: set[str]) -> bool:
    """True if ``block`` has slipped: started in the past and its task is still active."""
    start = _parse_event_start(block.get("start"))
    return start is not None and start <= ref and block.get("task_id") in active_ids


def _next_window(block: dict[str, Any], *, ref: datetime) -> tuple[str, str]:
    """The next free window to try for a slipped block: start in 1h, same duration."""
    start = _parse_event_start(block.get("start"))
    end = _parse_event_start(block.get("end"))
    duration = (end - start) if (start and end and end > start) else timedelta(hours=1)
    new_start = ref + timedelta(hours=1)
    return new_start.isoformat(), (new_start + duration).isoformat()


def _recover_one_block(cal_state: dict[str, Any], block: dict[str, Any], calendar_id: str,
                       fb_ids: list[str], *, ref: datetime,
                       runner: calendar_blocks.Runner | None) -> str:
    """Move ONE slipped block via UPDATE, persisting + logging the outcome.

    Returns ``"moved"`` or ``"refused"``. The move is a ``gog calendar update`` so
    the block keeps its id (reversible); an overlap/unknown freebusy or a
    non-agent event refuses the move (block left in place, ``calendar_block_refused``
    logged) -- NEVER-OVERBOOK-EXTERNAL holds even during recovery.
    """
    task_id = block.get("task_id")
    new_start, new_end = _next_window(block, ref=ref)
    try:
        moved = calendar_blocks.move_focus_block(
            calendar_id, block["event_id"], task_id, new_start, new_end,
            freebusy_calendar_ids=fb_ids, runner=runner, trigger="proactive_brief")
    except (calendar_blocks.OverbookError, calendar_blocks.ExternalEventError) as exc:
        reason = getattr(exc, "reason", "external_event")
        focus_calendar.record_dry_run(cal_state, "calendar.move_refused",
                                      {"event_id": block.get("event_id"), "task_id": task_id},
                                      {"reason": reason})
        focus_calendar.save_focus_calendar(cal_state)
        _log("calendar_block_refused", task_id=task_id, reason=reason, event_id=block.get("event_id"))
        return "refused"
    block["start"], block["end"] = moved["start"], moved["end"]
    block["slip_count"] = (block.get("slip_count") or 0) + 1
    block["last_slipped_at"] = ref.isoformat()
    focus_calendar.record_dry_run(cal_state, "calendar.update", moved["request"], moved)
    focus_calendar.save_focus_calendar(cal_state)
    _log("calendar_block_moved", task_id=task_id, event_id=block["event_id"],
         new_start=moved["start"], new_end=moved["end"])
    return "moved"


def run_slip_recovery(*, now: datetime | None = None, dry_run: bool = False,
                      send: Send | None = None,
                      runner: calendar_blocks.Runner | None = None) -> dict[str, int]:
    """Slide every slipped agent-owned focus block to the next free window.

    Recovery is a ``gog calendar update`` (NEVER delete+create). Degrades silently
    when no focus calendar is configured. A dry-run counts the slipped blocks
    without touching the calendar.
    """
    ref = now or _now()
    counts = {"moved": 0, "refused": 0}
    cal_state = focus_calendar.load_focus_calendar()
    calendar_id = cal_state.get("agent_calendar_id")
    if not calendar_id:
        return counts  # no focus calendar configured -- degrade silently
    active_ids = {r.canonical_id for r in _load_active() if r.canonical_id}
    # Freebusy-check the slid window against EXTERNAL (human) calendars only -- the
    # block being moved still occupies its old slot on the focus calendar, which
    # FreeBusy cannot exclude per-event, so including the focus calendar would
    # always self-overlap and refuse the move.
    fb_ids = calendar_blocks.external_calendar_ids()

    for block in list(cal_state.get("active_blocks", [])):
        if not _block_has_slipped(block, ref=ref, active_ids=active_ids):
            continue
        if dry_run:
            counts["moved"] += 1
            continue
        counts[_recover_one_block(cal_state, block, calendar_id, fb_ids, ref=ref, runner=runner)] += 1
    return counts


# Mode -> the name of the flow function on this module. Stored as NAMES (resolved
# via getattr at call time), not function objects, so a test can monkeypatch a
# flow and the dispatcher honours it -- and the indirection stays a thin lookup.
_MODE_FLOWS: dict[str, str] = {
    "brief": "run_daily_brief",
    "prebrief": "run_pre_brief_scan",
    "slip": "run_slip_recovery",
    "friday": "run_friday_proposal",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="proactive_brief.py", description=__doc__)
    parser.add_argument("--mode", choices=sorted(_MODE_FLOWS), required=True,
                        help="which proactive flow to run")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be pushed without sending or writing state")
    args = parser.parse_args(argv)
    try:
        # Every flow takes the same (dry_run, send) signature; the cron announces
        # the collected payloads to its explicit delivery.to. slip is a calendar
        # flow that collects no Telegram text, so its announced output is empty.
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
