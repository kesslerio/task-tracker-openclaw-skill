#!/usr/bin/env python3
"""Autonomy gate + audit-log substrate (Contracts 3 & 4, Decision #1).

Phase 0a scaffolding. This module lands the *contracts* every autonomous writer
(U2-U6) codes against, plus the security-grade gate<->message seam (Decision #1):

* ``gate(...)`` records an act in the autonomy log and returns an ``act_id`` bound
  to a single proven ``delivery_target``. The pre-action snapshot is taken INSIDE
  ``gate()`` immediately before the write is authorised (TOCTOU fix), not at
  proposal time.
* ``assert_send_target(act_id, attempted_target)`` is the seam: a later send for
  that ``act_id`` MUST use the identical gated target or it is blocked. Full
  ``message()`` wiring is U4; here we land the helper + the denied-path contract.

State files (all under ``cos_config.state_dir()`` == ~/.lobster/state/task-mgmt):

* ``autonomy-log.jsonl``  -- append-only act log (Contract 4 shape).
* ``autonomy-config.json``-- the rung ladder + default_rung_for_unknown.
* ``nag-state.json``      -- frozen Contract 3 shape; minimal safe stub so a future
  ``/undo`` of a nag won't crash before U4 exists.

Every writer here is atomic (utils._atomic_write) and flock-guarded.
"""

from __future__ import annotations

import copy
import json
import os
import uuid
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None

from cos_config import state_dir
from delivery_target import make_delivery_target, prove_delivery_target
from utils import _atomic_write

# --- Rung ladder (Contract 4) ---------------------------------------------
# Reversibility-keyed. Higher rung == less reversible == less autonomy.
RUNG_READ = 0          # read-only
RUNG_DRAFT = 1         # draft-only (default for unknown act types)
RUNG_APPROVE = 2       # execute-with-approval
RUNG_MONITORED_AUTO = 3  # monitored auto-execute (reversible)
RUNG_NEVER_AUTO = 4    # never auto (irreversible: email send, real-event delete)

DEFAULT_RUNG_FOR_UNKNOWN = RUNG_DRAFT
RUNG_MIN = RUNG_READ
RUNG_MAX = RUNG_NEVER_AUTO

# v0.2 ships the proactive-push delivery seam (U4 nag engine). A rung-3
# monitored-auto act that names a PROVEN delivery_target is now permitted to
# execute and bind its target -- the gate<->message seam (assert_send_target)
# still enforces that the SAME proven target is the sole permitted destination,
# so flipping this on does NOT relax the delivery-target proof: an unproven /
# work-group / env-missing target is still blocked upstream at
# ``blocked:unproven-target`` (the in-gate prove), and a send to any other target
# is still blocked at ``target-mismatch``. This was the explicit v0.2 gate paired
# with U4's delivery wiring; v0.1 shipped it False (board-only). A rung-3 act with
# NO delivery_target (a pure board mutation) was always unaffected.
RUNG3_PUSH_ENABLED = True

# Acts that are a proactive PUSH and make NO board write, anchored IN CODE. These
# are the ONLY acts exempt from the pre-action board snapshot requirement: their
# reversal is the ack (e.g. /undo acks the nag loop), not a board-line restore, so
# there is nothing on the board to snapshot. The exemption is keyed on this
# explicit allowlist -- NOT inferred from (rung, has-target) -- so a FUTURE rung-3
# act that names a delivery_target AND rewrites a board line is NOT silently
# exempted and keeps its mandatory undo snapshot. To add a new push act here, it
# must genuinely make no board mutation.
PUSH_NO_BOARD_WRITE_ACTS: frozenset[str] = frozenset({
    "nag_sent",            # U4 nag re-fire -- reversal is the ack, not a board edit
    "body_double_checkin",  # U4 body-double check-in -- pure push
})

# Irreversible-act rungs anchored IN CODE (Finding #3b). These are the acts that
# must never auto-execute regardless of what a JSON override claims; a corrupt or
# tampered autonomy-config.json can only ever fail CLOSED to these, never below
# them. A JSON override may adjust a KNOWN act_type to another valid rung, but the
# in-code floor for these irreversible acts is the safe default.
DEFAULT_ACT_TYPE_RUNGS: dict[str, int] = {
    # Irreversible acts -- anchored at rung 4 so a corrupt config can only ever
    # fail CLOSED to these, never below them.
    "email_send": RUNG_NEVER_AUTO,
    "calendar_block_deleted": RUNG_NEVER_AUTO,
    "focus_deleted": RUNG_NEVER_AUTO,
    # The reversibility-keyed ladder (spec-U2 §3.1). U2 lands the real rungs so the
    # system has genuine rung-3 push acts to disable in v0.1 (RUNG3_PUSH_ENABLED).
    # A nag is reversible (ack+silence) -> rung 3 (monitored-auto), but its PUSH is
    # frozen in v0.1. Board mutations are execute-with-approval -> rung 2.
    "nag_sent": RUNG_MONITORED_AUTO,
    "nag_acked": RUNG_MONITORED_AUTO,
    "body_double_checkin": RUNG_MONITORED_AUTO,  # U4 push, no board write
    "wip_cap_enforced": RUNG_APPROVE,
    "task_marked_done": RUNG_APPROVE,
    "focus_set": RUNG_APPROVE,
    "focus_updated": RUNG_APPROVE,
    "email_draft": RUNG_DRAFT,
}

DEFAULT_AUTONOMY_CONFIG: dict[str, Any] = {
    "rungs": {
        "0": "read",
        "1": "draft",
        "2": "approve",
        "3": "monitored-auto",
        "4": "never-auto",
    },
    "act_type_rungs": dict(DEFAULT_ACT_TYPE_RUNGS),
    "default_rung_for_unknown": DEFAULT_RUNG_FOR_UNKNOWN,
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def autonomy_log_path() -> Path:
    return state_dir() / "autonomy-log.jsonl"


def autonomy_config_path() -> Path:
    return state_dir() / "autonomy-config.json"


def nag_state_path() -> Path:
    return state_dir() / "nag-state.json"


# --- autonomy-config.json --------------------------------------------------

def _rename_corrupt_aside(path: Path) -> Path | None:
    """Rename a corrupt state file aside as ``<name>.corrupt-<n>`` (never erase it).

    Preserves the bad bytes for forensics rather than silently rewriting over
    them. Returns the destination path, or None if the rename failed.
    """
    for n in range(1, 1000):
        candidate = path.with_name(f"{path.name}.corrupt-{n}")
        if not candidate.exists():
            try:
                os.replace(path, candidate)
                return candidate
            except OSError:
                return None
    return None


def _log_system_error(reason: str, **fields: Any) -> None:
    """Append a ``system_error`` record to the autonomy log (best-effort).

    A failure here (e.g. the log itself is unwritable) must never crash the
    caller -- a fail-closed config resolution is the priority, not the audit
    breadcrumb.
    """
    try:
        _log_act(
            f"act_{uuid.uuid4().hex[:16]}", "system_error", RUNG_READ, "system_error",
            agent_id="task-tracker", metadata={"reason": reason, **fields},
        )
    except OSError:
        pass


# Sentinel: a state file that is present but unreadable (perms/IO), as distinct
# from missing or corrupt. The caller falls back in-memory WITHOUT clobbering it.
_STATE_UNREADABLE = object()


def _load_state_dict(path: Path, corrupt_reason: str):
    """Read a JSON-object state file, quarantining a corrupt one.

    Returns the parsed ``dict`` when valid; ``None`` when the file is missing or
    structurally corrupt (a corrupt file is renamed aside as ``.corrupt-<n>`` and
    a ``system_error`` logged -- never silently erased, which would destroy the
    operator's tampered/corrupt evidence); or the ``_STATE_UNREADABLE`` sentinel
    when present but unreadable, so the caller can fail closed in-memory without
    overwriting it. The single home for the read+quarantine policy both
    ``ensure_autonomy_config`` and ``_read_nag_state`` share.
    """
    if not path.exists():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            return loaded
        # A non-dict JSON document is structurally corrupt for our purposes.
        raise json.JSONDecodeError(f"{path.name} is not a JSON object", "", 0)
    except json.JSONDecodeError as exc:
        moved = _rename_corrupt_aside(path)
        _log_system_error(
            corrupt_reason,
            path=str(path),
            moved_to=str(moved) if moved else None,
            error=str(exc),
        )
        return None
    except OSError:
        return _STATE_UNREADABLE


def ensure_autonomy_config() -> dict[str, Any]:
    """Materialise/return autonomy-config.json; fail CLOSED to in-code defaults.

    A corrupt config is quarantined (never silently rewritten over) and a fresh
    safe default is written; a present-but-unreadable config is left untouched and
    the in-code default is used in memory. Either way the irreversible-act rungs
    are anchored in code, so resolution can never fail open.
    """
    path = autonomy_config_path()
    loaded = _load_state_dict(path, "autonomy-config-corrupt")
    if isinstance(loaded, dict):
        return loaded
    # Deep-copy the defaults so a caller that mutates ``config["act_type_rungs"]``
    # (e.g. to register a rung) can never poison the module-level default for the
    # next caller -- a shallow ``dict(...)`` shares the nested act_type_rungs dict.
    if loaded is _STATE_UNREADABLE:
        return copy.deepcopy(DEFAULT_AUTONOMY_CONFIG)
    _atomic_write(path, json.dumps(DEFAULT_AUTONOMY_CONFIG, indent=2, sort_keys=True) + "\n")
    return copy.deepcopy(DEFAULT_AUTONOMY_CONFIG)


def _coerce_rung(value: Any) -> int | None:
    """Coerce a config rung value to a valid int in [RUNG_MIN, RUNG_MAX].

    Mirrors cos_config._int_env: int() in try/except, never crashes. Returns None
    for garbage / out-of-range / wrong-type so the caller can fall back to the
    in-code default instead of trusting a poisoned override.
    """
    try:
        rung = int(value)
    except (TypeError, ValueError):
        return None
    if RUNG_MIN <= rung <= RUNG_MAX:
        return rung
    return None


def rung_for_act_type(act_type: str, config: dict[str, Any] | None = None) -> int:
    """Resolve the rung for an act type; never crash, never silently fail open.

    Resolution order, with the in-code default as the safe floor:

    * A JSON ``act_type_rungs`` override applies ONLY for a KNOWN act_type and ONLY
      when its value is a valid int in ``[0, 4]``. A garbage / out-of-range /
      wrong-type override is ignored with a warning and the in-code default is
      used -- so a corrupt config can never downgrade ``email_send`` from rung 4.
    * Otherwise the in-code ``DEFAULT_ACT_TYPE_RUNGS`` (irreversible acts anchored
      at rung 4) wins.
    * Otherwise ``default_rung_for_unknown`` (validated the same way), else
      ``DEFAULT_RUNG_FOR_UNKNOWN``.
    """
    cfg = config or ensure_autonomy_config()
    overrides = cfg.get("act_type_rungs")
    if not isinstance(overrides, dict):
        overrides = {}

    if act_type in overrides:
        coerced = _coerce_rung(overrides[act_type])
        if coerced is not None:
            return coerced
        warnings.warn(
            f"autonomy-config.json act_type_rungs[{act_type!r}]="
            f"{overrides[act_type]!r} is not a valid rung in [0,4]; "
            "ignoring it and using the in-code default.",
            RuntimeWarning,
            stacklevel=2,
        )

    if act_type in DEFAULT_ACT_TYPE_RUNGS:
        return DEFAULT_ACT_TYPE_RUNGS[act_type]

    coerced_default = _coerce_rung(cfg.get("default_rung_for_unknown"))
    if coerced_default is not None:
        return coerced_default
    return DEFAULT_RUNG_FOR_UNKNOWN


# --- autonomy-log.jsonl ----------------------------------------------------

def _append_autonomy_log(record: dict[str, Any]) -> None:
    # state_dir() owns the 0o700 parent-dir guarantee; calling it here ensures the
    # directory exists with owner-only perms before we open the log inside it.
    state_dir()
    path = autonomy_log_path()
    rendered = json.dumps(record, ensure_ascii=False, sort_keys=True)
    with path.open("a", encoding="utf-8") as handle:
        # The audit log holds delivery targets + act metadata: owner-only (0o600).
        try:
            os.fchmod(handle.fileno(), 0o600)
        except OSError:
            pass
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.write(rendered + "\n")
            handle.flush()
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def read_autonomy_log() -> list[dict[str, Any]]:
    """Read the autonomy log, tolerating torn/malformed JSONL lines.

    A single torn JSONL line (e.g. a crash mid-append) must NOT break every
    ``assert_send_target`` that reads the log -- that would fail OPEN by making the
    seam unenforceable. Malformed lines are skipped and collected into a warning,
    mirroring ``task_ledger.read_events``.

    A genuine I/O fault (``OSError`` from the read) is intentionally NOT swallowed:
    silently returning ``[]`` would make ``find_act`` treat a transiently
    unreadable log as empty and block legitimate sends with ``unknown-act`` -- a
    real fault should surface, not be masked into a fail-quiet empty log.
    """
    path = autonomy_log_path()
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    malformed: list[tuple[int, str]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as exc:
            malformed.append((line_number, str(exc)))
    if malformed:
        summary = ", ".join(f"{path}:{ln}: {msg}" for ln, msg in malformed)
        warnings.warn(
            f"Ignored {len(malformed)} malformed autonomy-log line(s): {summary}",
            RuntimeWarning,
            stacklevel=2,
        )
    return records


def find_act(act_id: str) -> dict[str, Any] | None:
    """Return the FIRST log record for an ``act_id`` -- the canonical binding.

    ``gate()`` writes exactly ONE record per ``act_id`` (a fresh uuid4 per call),
    so the FIRST record is the authoritative one and its status (``executed`` or
    ``blocked:*``) is the truth. Binding to the FIRST record -- regardless of
    status -- fully closes the forge-by-later-append vector: a later append for
    the same act_id (e.g. a forged ``executed`` record over a first ``blocked:*``
    one, or a different delivery_target) can NEVER override the canonical record.
    Scanning forward for an ``executed`` record would re-open that vector, so we
    deliberately do not.
    """
    for record in read_autonomy_log():
        if record.get("act_id") == act_id:
            return record
    return None


# --- The gate (Contract 4 + Decision #1 seam) ------------------------------

def gate(
    act_type: str,
    *,
    delivery_target: dict[str, Any] | None = None,
    task_id: str | None = None,
    unit: str | None = None,
    agent_id: str = "niemand-work",
    reversible: bool = True,
    snapshot_provider=None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Record an autonomous act and return its act_id bound to ``delivery_target``.

    The pre-action snapshot is taken HERE, immediately before the act is logged as
    ``executed`` -- not at proposal time -- so an async approval that takes minutes
    cannot stale it (Contract 4 TOCTOU fix). ``snapshot_provider`` is a zero-arg
    callable returning ``{"file", "raw_line", "line_number"}``; it is invoked at
    gate time.

    A rung-4 (never-auto / irreversible) act is blocked. A rung-2+ act that needs a
    snapshot but supplies none is blocked REGARDLESS of ``reversible`` (a snapshot
    is required to undo any execute-with-approval act; ``reversible`` only governs
    the undo *window*, never whether a snapshot is taken). The returned ``act_id``
    is the token the caller MUST pass to its later ``message()`` send; the gated
    ``delivery_target`` -- PROVEN here -- is the SOLE permitted destination
    (enforced by ``assert_send_target``).

    The caller-supplied ``delivery_target`` is re-proven through
    ``prove_delivery_target`` INSIDE the gate before binding: a Work-group /
    unknown-chat / env-missing target binds NOTHING and blocks
    (``reason: "unproven-target"``). This also normalises int-vs-str ids so the
    bound target is canonical.
    """
    config = ensure_autonomy_config()
    rung = rung_for_act_type(act_type, config)
    act_id = f"act_{uuid.uuid4().hex[:16]}"

    # Fields shared by every act this gate logs (status/target/snapshot vary).
    common = dict(task_id=task_id, unit=unit, agent_id=agent_id,
                  reversible=reversible, metadata=metadata)

    # Prove + normalise the target BEFORE binding. An act that names a destination
    # may only bind a proven one; if proof fails it binds nothing and blocks.
    proven_target: dict[str, Any] | None = None
    if delivery_target is not None:
        proof = _prove_supplied_target(delivery_target)
        if not proof["ok"]:
            record = _log_act(act_id, act_type, rung, "blocked:unproven-target", **common)
            return {
                "ok": False,
                "reason": "unproven-target",
                "proof_reason": proof.get("reason"),
                "act_id": act_id,
                "record": record,
            }
        proven_target = proof["delivery_target"]

    if rung >= RUNG_NEVER_AUTO:
        record = _log_act(act_id, act_type, rung, "blocked:rung4",
                          delivery_target=proven_target, **common)
        return {"ok": False, "reason": "rung4", "act_id": act_id, "record": record}

    # v0.1 board-only: a rung-3 act that names a delivery_target is a proactive
    # Telegram push, and the delivery seam (U4/U5/U6) has not shipped. Block it at
    # the gate -- even a perfectly proven target -- so no rung-3 push can execute
    # before its owning unit lands. Board-only rung-3 acts (no delivery_target)
    # pass through normally.
    if rung >= RUNG_MONITORED_AUTO and proven_target is not None and not RUNG3_PUSH_ENABLED:
        record = _log_act(act_id, act_type, rung, "blocked:push-disabled",
                          delivery_target=proven_target, **common)
        return {"ok": False, "reason": "push-disabled", "act_id": act_id, "record": record}

    # TOCTOU: snapshot taken now, right before authorising the write. A pre-action
    # snapshot is the undo substrate for a BOARD MUTATION (an act that rewrites a
    # markdown line); it is mandatory at rung >= APPROVE irrespective of reversible
    # -- reversible=False is NOT an escape hatch for skipping it.
    #
    # A proactive PUSH that makes NO board write (the explicit
    # PUSH_NO_BOARD_WRITE_ACTS allowlist -- e.g. U4 nag_sent / body_double_checkin)
    # has nothing on the board to snapshot; its reversal is the ack (`/undo` acks
    # the nag loop, not a line restore). So those acts are exempt from the snapshot
    # requirement -- the delivery-target proof + the gate<->message seam are THEIR
    # safety substrate. The exemption is keyed on the explicit allowlist, NOT
    # inferred from (rung, has-target): a rung-2 board mutation still needs its
    # snapshot, and a future rung-3 act that DOES write the board is not exempted.
    snapshot = snapshot_provider() if snapshot_provider is not None else None
    is_push = act_type in PUSH_NO_BOARD_WRITE_ACTS and proven_target is not None
    if rung >= RUNG_APPROVE and snapshot is None and not is_push:
        record = _log_act(act_id, act_type, rung, "blocked:missing-snapshot",
                          delivery_target=proven_target, **common)
        return {"ok": False, "reason": "missing-snapshot", "act_id": act_id, "record": record}

    record = _log_act(act_id, act_type, rung, "executed",
                      delivery_target=proven_target, snapshot=snapshot, **common)
    return {"ok": True, "act_id": act_id, "delivery_target": proven_target, "record": record}


def _prove_supplied_target(delivery_target: dict[str, Any]) -> dict[str, Any]:
    """Re-prove a caller-supplied delivery_target descriptor through Contract 2.

    Accepts the descriptor dict shape; extracts ``chat_id``/``topic_id`` and any
    ``agent_id``/``channel`` overrides, then runs ``prove_delivery_target`` so the
    bound target is both proven (work-group/unknown/env rejected) and normalised.
    """
    chat_id = delivery_target.get("chat_id")
    topic_id = delivery_target.get("topic_id")
    agent_id = delivery_target.get("agent_id") or "niemand-work"
    channel = delivery_target.get("channel") or "telegram"
    return prove_delivery_target(chat_id, topic_id, agent_id=agent_id, channel=channel)


def _log_act(
    act_id: str,
    act_type: str,
    rung: int,
    status: str,
    *,
    task_id: str | None = None,
    unit: str | None = None,
    agent_id: str = "niemand-work",
    delivery_target: dict[str, Any] | None = None,
    reversible: bool = True,
    snapshot: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the Contract-4 act record, append it to the audit log, and return it.

    Every record built here IS appended -- there is no build-without-log path --
    so construction and the single append live together. Everything past the four
    positional essentials is keyword-only, so a caller can never transpose
    ``delivery_target`` / ``snapshot`` / ``metadata`` (the old 11-positional
    footgun).
    """
    record = {
        "act_id": act_id,
        "act_type": act_type,
        "rung": rung,
        "status": status,
        "timestamp": _now_iso(),
        "agent_id": agent_id,
        "task_id": task_id,
        "unit": unit,
        "delivery_target": delivery_target,
        "reversible": reversible,
        "undo_command": f"/undo {act_id}",
        "pre_action_snapshot": snapshot,
        "metadata": metadata or {},
    }
    _append_autonomy_log(record)
    return record


_CANONICAL_TARGET_KEYS = ("chat_id", "topic_id", "agent_id", "channel")


def _canonical_target(target: dict[str, Any] | None) -> tuple | None:
    """Project a target onto the canonical comparison subset.

    Returns ``None`` if any canonical key is missing -- a target that cannot
    name all four canonical fields is not comparable and must fail. Extra keys
    (e.g. ``message_id``) are intentionally ignored so a benign extra field does
    not spuriously block a legitimate send.
    """
    if target is None:
        return None
    if any(key not in target for key in _CANONICAL_TARGET_KEYS):
        return None
    return tuple(target[key] for key in _CANONICAL_TARGET_KEYS)


def assert_send_target(act_id: str, attempted_target: dict[str, Any] | None) -> dict[str, Any]:
    """Decision #1 seam: a send for ``act_id`` MUST use the gated, AUTHORISED target.

    Returns ``{"ok": True}`` only if ALL hold:

    * the act exists in the log, and
    * its bound record has ``status == "executed"`` -- a gated-but-BLOCKED act is
      NOT a permitted send (a ``blocked:*`` status returns ``act-not-authorised``),
      closing the verified-seam-bypass where a blocked act greenlit a send, and
    * the canonical subset ``(chat_id, topic_id, agent_id, channel)`` of
      ``attempted_target`` equals the gated target's. Benign extra keys (e.g.
      ``message_id``) are ignored; a missing canonical key fails.

    This is the assertion that a buggy/malicious U4 can neither gate topic:2 then
    send topic:6, nor send for an act the gate refused.
    """
    record = find_act(act_id)
    if record is None:
        return {"ok": False, "reason": "unknown-act", "message": f"No gated act {act_id}."}

    status = record.get("status")
    if status != "executed":
        return {
            "ok": False,
            "reason": "act-not-authorised",
            "status": status,
            "message": f"Act {act_id} status {status!r} is not executed; send blocked.",
        }

    gated = record.get("delivery_target")
    if gated is None:
        return {
            "ok": False,
            "reason": "no-gated-target",
            "message": f"Act {act_id} has no bound delivery_target; send blocked.",
        }

    gated_canonical = _canonical_target(gated)
    attempted_canonical = _canonical_target(attempted_target)
    if attempted_canonical is None or attempted_canonical != gated_canonical:
        return {
            "ok": False,
            "reason": "target-mismatch",
            "message": f"Send target {attempted_target} != gated target {gated}; blocked.",
            "gated_target": gated,
        }
    return {"ok": True, "delivery_target": gated}


# --- nag-state.json (Contract 3 frozen shape + minimal stub) ---------------

def default_nag_entry(nag_loop_id: str, delivery_target: dict[str, Any] | None = None) -> dict[str, Any]:
    """The frozen Contract 3 per-task nag entry shape (U4 owns the logic)."""
    return {
        "nag_loop_id": nag_loop_id,
        "ack": False,
        "closed_by": None,
        "closed_at": None,
        "snoozed_until": None,
        "snooze_count": 0,
        "block_reason": None,
        "nag_count": 0,
        "delivery_target": delivery_target,
        "body_double_sessions": [],
        "archived_nag_loops": [],
    }


def nag_lock_path() -> Path:
    return state_dir() / "nag-state.lock"


def _read_nag_state() -> dict[str, Any]:
    """Read nag-state.json; on corruption rename aside + log, never destroy it.

    Silently returning ``{}`` on a JSONDecodeError would let the NEXT write erase
    every live nag entry. ``_load_state_dict`` quarantines a corrupt file aside
    (``.corrupt-<n>``) so the bad bytes survive for forensics and we rebuild from
    a fresh empty state; a present-but-unreadable file is treated as empty in
    memory without being clobbered.
    """
    loaded = _load_state_dict(nag_state_path(), "nag-state-corrupt")
    return loaded if isinstance(loaded, dict) else {}


def _write_nag_state(state: dict[str, Any]) -> None:
    _atomic_write(nag_state_path(), json.dumps(state, indent=2, sort_keys=True) + "\n")


def ack_nag(task_id: str, *, ack_type: str = "user_undo") -> dict[str, Any]:
    """Minimal stub so /undo of a nag works before U4 ships.

    Marks the task's nag entry acked (terminal for the loop) and records the
    ``ack_type``. If no entry exists yet, a default stub entry is created so the
    undo path never raises.

    The whole read-modify-write of ``nag-state.json`` is wrapped in an exclusive
    ``flock`` on a sidecar lockfile: concurrent acks otherwise read the same base
    state, each add their own entry, and the last writer's ``os.replace`` drops
    every other writer's entry (lost update). The lock serialises the cycle so
    every concurrent ack survives.
    """
    state_dir()  # ensure the 0o700 dir exists before opening the lockfile
    lock_path = nag_lock_path()
    with lock_path.open("a", encoding="utf-8") as lock_handle:
        try:
            os.fchmod(lock_handle.fileno(), 0o600)
        except OSError:
            pass
        if fcntl is not None:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            state = _read_nag_state()
            entry = state.get(task_id) or default_nag_entry(f"nag_{uuid.uuid4().hex[:12]}")
            entry["ack"] = True
            entry["closed_by"] = ack_type
            entry["closed_at"] = _now_iso()
            entry["ack_type"] = ack_type
            state[task_id] = entry
            _write_nag_state(state)
            return entry
        finally:
            if fcntl is not None:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
