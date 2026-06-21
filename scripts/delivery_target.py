#!/usr/bin/env python3
"""Delivery-target descriptor + proof (Contract 2).

The descriptor is the dict shape every proactive push (U4 nag, U5 ledger draft,
U6 brief) names and proves its destination with:

    {"chat_id": "<TELEGRAM_CHAT_ID_PRODUCTIVITY>", "topic_id": "2",
     "agent_id": "niemand-work", "channel": "telegram"}

Proof rules (verbatim from the contract):

1. ``known_safe_targets()`` and ``prove_delivery_target()`` assemble from the env
   vars that ALREADY EXIST in secrets.conf / the container -- never hardcoded.
   The allowlist is assembled lazily at call time, with no module-level snapshot,
   so a stale import-time view can never be grabbed.
   ``TELEGRAM_CHAT_ID_PRODUCTIVITY`` plus
   ``OPENCLAW_TOPIC_PRODUCTIVITY_{STANDUP,WEEKLY_REVIEW_PLANNING,DONE,JOURNAL,IDENTITY}``
   (IDENTITY defaults to 1909, the Chief-of-Staff log topic).
2. The Work/heartbeat group (id from ``TELEGRAM_CHAT_ID_WORK``) carries the
   heartbeat; no productivity push may ride it -- when that env var is set the
   group is rejected outright, and even when it is unset the env-assembled
   allowlist still blocks it as an unknown target.
3. When the env is unset, proof returns a BLOCKED result -- never a guessed or
   stale target.
"""

from __future__ import annotations

import os
import re
import warnings
from typing import Any

DEFAULT_AGENT_ID = "niemand-work"
DEFAULT_CHANNEL = "telegram"

# Allowed delivery channels. Phase 0a is Telegram-only; an unknown channel is a
# misconfiguration, not a guess we make on the user's behalf.
KNOWN_CHANNELS: frozenset[str] = frozenset({"telegram"})

# Canonical id shapes. A Telegram chat id is a (signed) integer; a topic id is a
# non-negative integer. Anything else in the env is garbage and is skipped+warned
# rather than silently widening the allowlist.
_CHAT_ID_RE = re.compile(r"^-?\d+$")
_TOPIC_ID_RE = re.compile(r"^\d+$")


def _normalize_id(raw: object) -> str:
    """Canonicalise a chat/topic id for comparison: str, stripped, no leading '+'.

    The Work-group reject and the allowlist must both see the same canonical form
    so the integer chat id and the whitespace/``+`` string variants cannot
    sneak past as a different value.
    """
    return str(raw).strip().lstrip("+")


# Decision #3 / Contract 2: the Work (heartbeat) group. The productivity push
# surface must never resolve here -- the heartbeat fires to this group, so a nag
# riding it would land in the wrong place. The id is env-sourced
# (``TELEGRAM_CHAT_ID_WORK``) and never hardcoded; it is read live at call time
# via ``_work_group_chat_id`` so a secrets.conf/env change is honoured without a
# reload. When the env var is unset the explicit reject is skipped, and the
# env-assembled allowlist blocks the group as ``target_unknown`` instead --
# fail-closed either way.
def _work_group_chat_id() -> str | None:
    raw = os.getenv("TELEGRAM_CHAT_ID_WORK", "")
    chat_id = _normalize_id(raw)
    return chat_id if chat_id else None

# Topic env var -> default topic id. IDENTITY (the Chief-of-Staff log topic)
# defaults to 1909 per Decisions Locked; the rest have no default and are only
# safe if their env var is set.
_PRODUCTIVITY_TOPIC_ENV: dict[str, str | None] = {
    "OPENCLAW_TOPIC_PRODUCTIVITY_STANDUP": None,
    "OPENCLAW_TOPIC_PRODUCTIVITY_WEEKLY_REVIEW_PLANNING": None,
    "OPENCLAW_TOPIC_PRODUCTIVITY_DONE": None,
    "OPENCLAW_TOPIC_PRODUCTIVITY_JOURNAL": None,
    "OPENCLAW_TOPIC_PRODUCTIVITY_IDENTITY": "1909",
}


def _productivity_chat_id() -> str | None:
    raw = os.getenv("TELEGRAM_CHAT_ID_PRODUCTIVITY")
    if raw is None or not raw.strip():
        return None
    chat_id = _normalize_id(raw)
    if not _CHAT_ID_RE.match(chat_id):
        warnings.warn(
            f"TELEGRAM_CHAT_ID_PRODUCTIVITY={raw!r} is not a valid chat id "
            "(^-?\\d+$); ignoring it -- no safe target can be assembled.",
            RuntimeWarning,
            stacklevel=2,
        )
        return None
    return chat_id


def known_safe_targets() -> set[tuple[str, str]]:
    """Assemble the allowlist of ``(chat_id, topic_id)`` pairs from env at call time.

    Assembled live (not at import) -- there is intentionally no module-level
    snapshot, so a test or a secrets.conf change is honoured without a reload and
    a stale import-time view can never be grabbed. Returns an empty set when the
    productivity chat id is unset/garbage -- there is no safe target to guess.

    Each topic env value is shape-validated (``^\\d+$``); a garbage value is
    skipped with a warning rather than poisoning the allowlist with an
    unproven pair.
    """
    chat_id = _productivity_chat_id()
    if chat_id is None:
        return set()
    targets: set[tuple[str, str]] = set()
    for env_name, default in _PRODUCTIVITY_TOPIC_ENV.items():
        raw = os.getenv(env_name)
        if raw and raw.strip():
            topic_id = _normalize_id(raw)
        elif default:
            topic_id = _normalize_id(default)
        else:
            continue
        if not _TOPIC_ID_RE.match(topic_id):
            warnings.warn(
                f"{env_name}={raw!r} is not a valid topic id (^\\d+$); "
                "skipping it -- not added to the safe-target allowlist.",
                RuntimeWarning,
                stacklevel=2,
            )
            continue
        targets.add((chat_id, topic_id))
    return targets


def make_delivery_target(
    chat_id: str,
    topic_id: str,
    *,
    agent_id: str = DEFAULT_AGENT_ID,
    channel: str = DEFAULT_CHANNEL,
) -> dict[str, str]:
    """Build a descriptor dict with the canonical key order and normalised ids."""
    return {
        "chat_id": _normalize_id(chat_id),
        "topic_id": _normalize_id(topic_id),
        "agent_id": agent_id,
        "channel": channel,
    }


def prove_delivery_target(
    chat_id: str | None,
    topic_id: str | None,
    *,
    agent_id: str = DEFAULT_AGENT_ID,
    channel: str = DEFAULT_CHANNEL,
) -> dict[str, Any]:
    """Prove a delivery target against the env-assembled allowlist.

    Returns ``{"ok": True, "delivery_target": {...}}`` only when the pair is in
    ``known_safe_targets()``. Otherwise returns ``{"ok": False, "reason": ...}``
    with one of:

    - ``env_missing``     -- TELEGRAM_CHAT_ID_PRODUCTIVITY (or the topic) is unset.
    - ``work_group``      -- the Work/heartbeat group was requested.
    - ``target_unknown``  -- a chat/topic not in the productivity allowlist.
    - ``channel_unknown`` -- a channel outside the Phase 0a allowlist.

    The Work-group check normalises the requested chat id first, so the integer
    chat id and the whitespace/``+`` string variants are all rejected when
    ``TELEGRAM_CHAT_ID_WORK`` is set. When that env var is unset the explicit
    ``work_group`` reject is skipped and the env-assembled allowlist blocks the
    group as ``target_unknown`` instead -- fail-closed either way.
    Never returns a guessed or stale target.
    """
    if channel not in KNOWN_CHANNELS:
        return {
            "ok": False,
            "reason": "channel_unknown",
            "message": f"Channel {channel!r} is not in the allowlist {sorted(KNOWN_CHANNELS)}; "
            "push blocked.",
        }

    work_group_chat_id = _work_group_chat_id()
    if (
        work_group_chat_id is not None
        and chat_id is not None
        and _normalize_id(chat_id) == work_group_chat_id
    ):
        return {
            "ok": False,
            "reason": "work_group",
            "message": f"Refusing the Work/heartbeat group {work_group_chat_id}; "
            "no productivity push may ride it.",
        }

    safe = known_safe_targets()
    if not safe:
        return {
            "ok": False,
            "reason": "env_missing",
            "message": "TELEGRAM_CHAT_ID_PRODUCTIVITY is unset; cannot prove a "
            "delivery target. Push blocked rather than guessing.",
        }

    if chat_id is None or topic_id is None:
        return {
            "ok": False,
            "reason": "env_missing",
            "message": "chat_id/topic_id not supplied; cannot prove a target.",
        }

    pair = (_normalize_id(chat_id), _normalize_id(topic_id))
    if pair not in safe:
        return {
            "ok": False,
            "reason": "target_unknown",
            "message": f"Target {pair} is not in the safe-target allowlist; push blocked.",
        }

    return {
        "ok": True,
        "delivery_target": make_delivery_target(
            pair[0], pair[1], agent_id=agent_id, channel=channel
        ),
    }
