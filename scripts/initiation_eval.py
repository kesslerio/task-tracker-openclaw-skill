#!/usr/bin/env python3
"""v0.4-C initiation state-machine evaluator -- the deterministic "should we nudge?".

A PURE, rules-only evaluator (U9 discipline: no model, fail-OPEN toward silence, gates by
explicit stored facts). It reads the committed #1, the focus-session start signal, the
point-in-time calendar, and the outbox send-history, and -- if every gate passes -- emits
an expiring ``Proposal`` (``initiation_contract``) for the C4 dispatcher to re-check and
deliver. It performs NO send and NO board mutation; its only side effect (in
``decide_and_store``) is parking the proposal in the initiation store.

Gates (ALL must pass), in cheap-to-expensive order so the calendar subprocess runs LAST:
  1. a #1 is committed for today (focus-state approved, ``daily_priorities[0]``);
  2. it has NOT been started (no focus session today -- active or already ended);
  3. the task's nag is not currently snoozed;
  4. the stage/cadence allows a send now -- cold-start after >= elapsed-min; ONE re-nudge
     after >= renudge-min past the cold-start send; both capped by the daily send budget;
  5. the calendar does not say the user is busy right now (``availability.not_known_busy``).

Fail-OPEN: any read error returns ``None`` (no nudge). For a proactive send, erring toward
silence is the safe direction -- a missed nudge is harmless, an errant one is not.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import availability
import cos_config
import focus_state
import initiation_store
import nag_state
import outbox
from initiation_contract import (
    STAGE_COLD_START,
    STAGE_COLD_START_RENUDGE,
    Proposal,
    focus_episode_slot,
)

REASON_COLD_START = "committed_unstarted"
REASON_RENUDGE = "still_unstarted_after_nudge"


def _parse_iso(raw: object) -> datetime | None:
    if not raw or not isinstance(raw, str):
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _minutes(delta: timedelta) -> float:
    return delta.total_seconds() / 60.0


def _local_date(moment: datetime) -> str:
    return moment.astimezone(cos_config.local_tz()).date().isoformat()


def _committed_first(now: datetime) -> tuple[str, datetime] | None:
    """``(task_id, committed_at)`` of today's APPROVED #1, or ``None``.

    Only an APPROVED-for-today focus state is a commitment to chase; a stale
    (yesterday's) or merely-proposed list is not (same rule the nag path uses).
    ``committed_at`` is the ``approved_at`` stamp -- the elapsed clock starts there.
    """
    state = focus_state.load_focus_state()
    ref = _local_date(now)
    if focus_state.status_for_today(state, reference_date=ref) != focus_state.STATUS_APPROVED:
        return None
    rows = (state or {}).get("daily_priorities") or []
    if not rows or not isinstance(rows[0], dict):
        return None
    task_id = rows[0].get("task_id")
    committed_at = _parse_iso((state or {}).get("approved_at"))
    # task_id must be a real string id -- do NOT str()-coerce a numeric/garbage value
    # (that would launder it past focus_episode_slot's own non-empty-string guard).
    if not isinstance(task_id, str) or not task_id or committed_at is None:
        return None
    return task_id, committed_at


def _has_started(entry: object, now: datetime, today: str) -> bool:
    """Has a focus session for the task engaged it today? If so, nothing to *initiate*.

    Engaged = an ACTIVE session, OR any session whose start / scheduled-end (``ends_at``,
    so an overnight block running into today counts) / explicit-end falls on today, OR an
    ended session we cannot date. The undateable-ended case fails CLOSED (assume engaged)
    -- mirroring ``cas_still_valid``, and so a garbage stamp on a real same-day session
    can never read as "not started" and fire a false nudge.
    """
    if not isinstance(entry, dict):
        return False
    if nag_state.active_body_double_session(entry, now=now) is not None:
        return True
    for session in entry.get("body_double_sessions") or []:
        if not isinstance(session, dict):
            continue
        for field in ("started_at", "ends_at", "ended_at"):
            stamp = _parse_iso(session.get(field))
            if stamp is not None and _local_date(stamp) == today:
                return True
        if session.get("ended_at") and _parse_iso(session.get("ended_at")) is None:
            return True  # an ended session we cannot date -> fail closed (engaged)
    return False


def _select_stage(slot: str, committed_at: datetime, now: datetime) -> tuple[str, str] | None:
    """``(stage, reason)`` for the next allowable nudge, or ``None`` if none is due.

    ``cold_start`` once (after the elapsed threshold past the commit); a single
    ``cold_start_renudge`` once (after the re-nudge gap past the cold-start SEND); both
    capped by the daily send budget. The two stages' outbox receipts ARE the send
    history -- with one committed #1 per day the per-slot count is the per-day budget.
    """
    cold = outbox.get_receipt(outbox.make_idem_key("initiation", slot, STAGE_COLD_START))
    renudge = outbox.get_receipt(outbox.make_idem_key("initiation", slot, STAGE_COLD_START_RENUDGE))
    if (1 if cold else 0) + (1 if renudge else 0) >= cos_config.initiation_daily_budget():
        return None
    if cold is None and renudge is not None:
        return None  # a re-nudge with no cold-start is corrupted state -> suppress
    if cold is None:
        if _minutes(now - committed_at) < cos_config.initiation_elapsed_min():
            return None
        return STAGE_COLD_START, REASON_COLD_START
    if renudge is None:
        cold_ts = _parse_iso(cold.get("ts"))
        if cold_ts is None or _minutes(now - cold_ts) < cos_config.initiation_renudge_after_min():
            return None
        return STAGE_COLD_START_RENUDGE, REASON_RENUDGE
    return None


def evaluate(now: datetime, *, user_scope: str = "work") -> Proposal | None:
    """Decide whether to emit an initiation proposal at ``now``. PURE read, fail-OPEN."""
    try:
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        first = _committed_first(now)
        if first is None:
            return None
        task_id, committed_at = first
        today = _local_date(now)

        entry = nag_state.read_state().get(task_id)
        if _has_started(entry, now, today) or nag_state.is_snoozed(entry, now=now):
            return None

        slot = focus_episode_slot(user_scope, task_id, today)
        selected = _select_stage(slot, committed_at, now)
        if selected is None:
            return None
        stage, reason = selected

        # The calendar is the one network read -- gate on it LAST, only once every cheap
        # gate has passed, so a normal "too soon / already started" run does no subprocess.
        if not availability.not_known_busy(now):
            return None

        created = now.isoformat()
        ttl = timedelta(minutes=cos_config.initiation_proposal_ttl_min())
        return Proposal(
            focus_episode_id=slot,
            task_id=task_id,
            user_scope=user_scope,
            local_date=today,
            stage=stage,
            reason_code=reason,
            created_at=created,
            expires_at=(now + ttl).isoformat(),
            cas_focus_state_rev=focus_state.current_rev(),
            cas_no_session_since=created,
            arm=None,
        )
    except Exception:  # noqa: BLE001 -- fail OPEN toward silence: any read error -> no nudge
        return None


def decide_and_store(now: datetime, *, user_scope: str = "work") -> Proposal | None:
    """``evaluate`` + park the proposal in the store for the C4 dispatcher. Returns it.

    The evaluator's ONLY side effect. The store is the durable hand-off so the
    dispatcher's send-time re-check (C4) can run in a separate cron tick.
    """
    proposal = evaluate(now, user_scope=user_scope)
    if proposal is None:
        return None
    try:
        # ``now`` may be naive; the store's expiry check normalizes it (naive -> UTC).
        initiation_store.write_proposal(proposal, now=now)
    except Exception:  # noqa: BLE001 -- fail OPEN: a store-write failure must not crash the cron
        return None
    return proposal


def today_slot(now: datetime, *, user_scope: str = "work") -> str | None:
    """The committed-#1 episode slot for ``now``, or None when no #1 is committed.

    The public seam the C4 dispatcher uses to locate a lingering proposal's slot when
    this tick itself emitted nothing -- single-sourcing the "today's committed #1"
    derivation with the evaluator so the tick and the evaluation always agree.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    first = _committed_first(now)
    if first is None:
        return None
    return focus_episode_slot(user_scope, first[0], _local_date(now))
