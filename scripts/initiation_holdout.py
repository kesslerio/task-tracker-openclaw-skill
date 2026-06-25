#!/usr/bin/env python3
"""v0.4-C initiation holdout: a deterministic 25% control arm.

The initiation nudge ships BEHIND a holdout so we can measure whether nudging actually
moves "did the user start their #1" before trusting it (the seed's "do nudges even
increase initiation?"). Each episode SLOT is assigned -- deterministically, stably -- to
``treatment`` (gets the nudge) or ``control`` (eligible, but the send is suppressed and
the counterfactual recorded). The split is a hash of the ``focus_episode_id`` (which
encodes scope + #1 + date), so the SAME episode is always the same arm -- no wall-clock,
no RNG -- and treatment/control are symmetric: both pass every eligibility gate; only the
final send differs.

The 25% default and the human-gated C->B escalation read (the holdout COUNT, not the
agent) come from the decisions doc; the assignment here is pure and deterministic.
"""

from __future__ import annotations

import hashlib

import cos_config
from initiation_contract import ARM_CONTROL, ARM_TREATMENT  # canonical arm vocabulary

__all__ = ["ARM_CONTROL", "ARM_TREATMENT", "arm_for"]


def arm_for(focus_episode_id: str) -> str:
    """Assign ``focus_episode_id`` to ``control`` (bottom ``holdout_pct`` %) or
    ``treatment``, deterministically and stably.

    A SHA-256 of the slot id -> an integer in ``[0, 100)`` -> below the holdout cut is
    control. Deterministic (no ``Math.random``/wall-clock), so the arm is identical
    across every evaluator tick for the same episode, and symmetric across runs.

    **Operational invariant: do NOT change ``INITIATION_HOLDOUT_PCT`` during an active
    experiment.** The split is stable for a FIXED pct; changing it mid-experiment can
    flip an as-yet-unrecorded slot's arm (changing the % of any running A/B test
    invalidates it). Once a slot's first decision is recorded (an outbox receipt), the
    C3 evaluator no longer re-emits it, so the recorded arm is frozen -- but set the pct
    once at the start of a holdout window and leave it.
    """
    digest = hashlib.sha256(focus_episode_id.encode("utf-8")).hexdigest()
    bucket = int(digest, 16) % 100
    return ARM_CONTROL if bucket < cos_config.initiation_holdout_pct() else ARM_TREATMENT
