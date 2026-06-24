#!/usr/bin/env python3
"""v0.4-C initiation decision-contract: the episode slot, the expiring proposal,
and the send-time compare-and-swap (CAS).

This is the *contract* half of the initiation nudge -- the data shapes and the
read-only validity check that the C3 evaluator and the C4 dispatcher share. It
holds NO send logic (the dispatcher delivers via ``outbox.deliver_once``) and
performs NO mutation; it only defines identities and answers "is this proposal
still safe to act on?".

Three pieces:

* ``focus_episode_slot`` -- the deterministic committed-#1 EPISODE identity. A
  cold-start initiation nudge ("you said X was today's #1, it's 2pm, not started
  -- Start it?") fires BEFORE any focus session exists, so there is no
  ``nag_state`` ``session_id`` to key on at decision time. The slot
  ``<user_scope>:<task_id>:<local_date>`` exists the moment the user commits a #1
  for the day, is identical across the treatment and the 25%-holdout arms, and the
  ``nag_state`` session (when the user finally taps Start) binds to it. This is the
  ``focus_episode_id`` in the outbox key ``initiation:<focus_episode_id>:<stage>``
  -- ``user_scope`` is carried INSIDE the slot, realising the checkpoint's
  ``(user_scope, focus_episode_id, stage)`` tuple without duplicating scope as a
  separate key segment.

* ``Proposal`` -- the expiring proposal the evaluator writes (and the store
  persists). It snapshots the two CAS tokens at write time: ``cas_focus_state_rev``
  (the monotonic ``focus_state.rev`` -- the committed-#1 / priorities version) and
  ``cas_no_session_since`` (the instant after which ANY focus-session start/end for
  the task invalidates the proposal). ``arm`` is filled by C5 (the holdout); it is
  ``None`` here.

* ``cas_still_valid`` -- the pure two-dimension check the dispatcher runs INSIDE the
  outbox flock immediately before sending. It defends against STALE STATE (not
  injection -- injection is handled by the dispatcher rendering inert templates with
  escaped titles and no raw body). It compares (1) the current ``focus_state.rev``
  against the snapshot (the user re-prioritised / approved / vetoed -> abort) and
  (2) whether any focus session for the task started or ended since the baseline
  (the user already started -> abort). ``cas_still_valid_now`` reads live state and
  fails CLOSED on any read error.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

import focus_state
import nag_state
from outbox import make_idem_key

# The two intervention stages. ``cold_start`` is the first nudge for the day's
# committed-but-unstarted #1; ``cold_start_renudge`` is the single allowed
# follow-up after a snooze/dismissal (C3 "medium": one re-nudge). They are distinct
# outbox key segments so each is a separate at-most-once delivery.
STAGE_COLD_START = "cold_start"
STAGE_COLD_START_RENUDGE = "cold_start_renudge"
KNOWN_STAGES: frozenset[str] = frozenset({STAGE_COLD_START, STAGE_COLD_START_RENUDGE})
# Stages are colon-joined into the outbox key, so a stage constant carrying a ":"
# would ambiguate it. The closed KNOWN_STAGES set already rejects an arbitrary
# colon stage at runtime; this guards a FUTURE maintainer adding a bad constant.
assert all(":" not in stage for stage in KNOWN_STAGES), "stage constants must be colon-free"

# Slot/key segments are colon-joined into the outbox idem key, so a literal ":" in
# any segment would make the key ambiguous (and could collide two distinct slots).
# Real values never contain one (scope ``work``; ``tsk_...`` ids; ISO ``YYYY-MM-DD``
# dates); we reject it defensively rather than silently mint a colliding key.
_FORBIDDEN = ":"


def focus_episode_slot(user_scope: str, task_id: str, local_date: str) -> str:
    """The deterministic committed-#1 episode-slot id (the ``focus_episode_id``).

    ``<user_scope>:<task_id>:<local_date>`` -- self-contained and stable for a given
    (scope, task, day), so the same #1 on the same day always maps to the same slot
    regardless of how many times the evaluator runs, and the holdout arm assignment
    (C5) keyed on it is stable too. Raises ``ValueError`` if any segment is empty or
    contains ``":"`` (which would corrupt the colon-joined outbox key).
    """
    for name, value in (("user_scope", user_scope), ("task_id", task_id),
                        ("local_date", local_date)):
        if not value or not isinstance(value, str):
            raise ValueError(f"focus_episode_slot: {name} must be a non-empty string")
        if _FORBIDDEN in value:
            raise ValueError(f"focus_episode_slot: {name} must not contain {_FORBIDDEN!r}")
    return f"{user_scope}:{task_id}:{local_date}"


@dataclass(frozen=True)
class Proposal:
    """An expiring initiation proposal: the evaluator's decision to nudge, pending
    a send-time recheck. Immutable -- a new decision is a new proposal.
    """

    focus_episode_id: str
    task_id: str
    user_scope: str
    local_date: str
    stage: str
    reason_code: str
    created_at: str
    expires_at: str
    cas_focus_state_rev: int | None
    cas_no_session_since: str
    arm: str | None = field(default=None)

    def __post_init__(self) -> None:
        if self.stage not in KNOWN_STAGES:
            raise ValueError(f"unknown initiation stage {self.stage!r}")
        # The slot must round-trip its parts (no stray ":"), or the outbox key is
        # ambiguous. Re-derive and compare rather than trust a hand-built id.
        expected = focus_episode_slot(self.user_scope, self.task_id, self.local_date)
        if self.focus_episode_id != expected:
            raise ValueError(
                f"focus_episode_id {self.focus_episode_id!r} != slot {expected!r}")

    def idem_key(self) -> str:
        """The outbox at-most-once key: ``initiation:<focus_episode_id>:<stage>``."""
        return make_idem_key("initiation", self.focus_episode_id, self.stage)

    def is_expired(self, *, now: datetime | None = None) -> bool:
        """True if ``now`` is at/after ``expires_at`` (an unparseable expiry =>
        expired -- a proposal we cannot date must not be acted on)."""
        ref = now or datetime.now(timezone.utc)
        if ref.tzinfo is None:  # a naive ``now`` is assumed UTC, never a raise
            ref = ref.replace(tzinfo=timezone.utc)
        expires = _parse_iso(self.expires_at)
        if expires is None:
            return True
        return ref >= expires

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Proposal":
        """Reconstruct from a stored dict, ignoring unknown keys (forward-compat)."""
        fields = {
            "focus_episode_id", "task_id", "user_scope", "local_date", "stage",
            "reason_code", "created_at", "expires_at", "cas_focus_state_rev",
            "cas_no_session_since", "arm",
        }
        return cls(**{k: v for k, v in data.items() if k in fields})


def _parse_iso(raw: Any) -> datetime | None:
    """Parse an ISO timestamp to a tz-aware UTC datetime, or None on garbage.

    A naive timestamp is assumed UTC (mirrors ``nag_state._session_elapsed``) so
    comparisons against the proposal baseline never raise a naive/aware mix.
    """
    if not raw or not isinstance(raw, str):
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def cas_still_valid(
    proposal: Proposal,
    *,
    current_focus_rev: int | None,
    task_sessions: list[dict[str, Any]] | None,
) -> bool:
    """Pure send-time CAS: is ``proposal`` still safe to deliver?

    Two dimensions, BOTH must hold (else the proposal is stale -> suppress):

    1. **Task-state version.** ``current_focus_rev`` must equal the snapshot the
       proposal carries. ``None`` (the committed-#1 state is gone/corrupt) fails the
       check. A bumped ``rev`` means the user re-proposed / approved / vetoed -- the
       #1 the nudge is about may have changed, so abort.
    2. **Focus-episode version.** No focus session for the task may have started OR
       ended at/after ``cas_no_session_since``. A new ``started_at`` means the user
       already began (nudging now is a false positive); an ``ended_at`` means they
       finished/cancelled. An unparseable baseline fails closed.

    Guards STALE STATE only. Injection is handled at render time by the dispatcher.
    """
    if current_focus_rev is None or current_focus_rev != proposal.cas_focus_state_rev:
        return False
    baseline = _parse_iso(proposal.cas_no_session_since)
    if baseline is None:
        return False
    for session in task_sessions or []:
        if not isinstance(session, dict):
            continue
        for stamp in (session.get("started_at"), session.get("ended_at")):
            if stamp in (None, ""):
                continue  # absent stamp: this dimension does not constrain
            ts = _parse_iso(stamp)
            # A present-but-unparseable stamp fails CLOSED: we cannot prove the
            # session is OLDER than the baseline, so we must not nudge over it.
            if ts is None or ts >= baseline:
                return False
    return True


def task_sessions(state: dict[str, Any] | None, task_id: str) -> list[dict[str, Any]]:
    """The focus/body-double sessions recorded for ``task_id`` (empty if none)."""
    if not isinstance(state, dict):
        return []
    entry = state.get(task_id)
    if not isinstance(entry, dict):
        return []
    sessions = entry.get("body_double_sessions")
    return sessions if isinstance(sessions, list) else []


class _NagStateUnreadable(Exception):
    """nag-state.json is present but unparseable -- the focus-episode CAS dimension
    cannot be evaluated, so the caller must fail closed (suppress the send)."""


def _live_task_sessions(task_id: str) -> list[dict[str, Any]]:
    """Live focus sessions for ``task_id``, raising ``_NagStateUnreadable`` on a
    corrupt-but-present nag-state.

    ``nag_state.read_state`` QUARANTINES a corrupt file aside and returns an empty
    dict -- indistinguishable from "no sessions". Trusting that would let the CAS
    read "the user has not started" from a file we actually could not read (a fail
    OPEN, asymmetric with the focus_state side, which fails closed via ``current_rev
    -> None``). So probe the raw file FIRST: a present-but-unparseable / non-object
    nag-state raises (caught by ``cas_still_valid_now`` -> fail closed); a genuinely
    ABSENT file is a legitimate "no sessions yet".
    """
    path = nag_state.nag_state_path()
    if path.exists():
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise _NagStateUnreadable(str(path)) from exc
        if not isinstance(parsed, dict):
            raise _NagStateUnreadable(f"{path}: not a JSON object")
    return task_sessions(nag_state.read_state(), task_id)


def cas_still_valid_now(proposal: Proposal) -> bool:
    """``cas_still_valid`` against LIVE state, failing CLOSED on any read error.

    Reads ``focus_state.current_rev()`` (the task-state version) and the task's
    ``nag_state`` sessions (the focus-episode version, via ``_live_task_sessions``
    which fails closed on a corrupt nag-state). Any exception -- a missing module
    dependency, an unreadable/corrupt state file, a parse error -- returns ``False``
    (suppress the nudge): a send we cannot prove is safe is not sent.
    """
    try:
        return cas_still_valid(
            proposal,
            current_focus_rev=focus_state.current_rev(),
            task_sessions=_live_task_sessions(proposal.task_id),
        )
    except Exception:  # noqa: BLE001 -- fail closed: any read failure suppresses the send
        return False
