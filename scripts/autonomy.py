#!/usr/bin/env python3
"""U2 autonomy core: /undo + /audit over the Phase-0a act log.

Phase 0a (``autonomy_gate.py``) lands the gate, the act log, the delivery seam,
and the nag-state stub. U2 builds the *reactive surface* on top of it:

* ``list_acts(...)`` -- the read model behind ``/audit`` (recent acts, newest first).
* ``find_act_detail(...)`` -- the single-act detail behind ``/audit act_<id>``.
* ``undo_act(act_id)`` -- the reversal behind ``/undo act_<id>``.

REVERSIBILITY is the invariant this module upholds. ``undo_act`` reverses an act
by KIND:

* A nag act (rung 3, ``nag_*``) is reversed by marking the task acked in
  ``nag-state.json`` (``ack_type="user_undo"``) -- the Contract-3 stub
  ``autonomy_gate.ack_nag`` -- and recording the reversal in both ledgers.
* A board mutation (an act carrying a ``pre_action_snapshot`` with a ``raw_line``)
  is reversed by restoring that exact ``raw_line`` to its board file via
  CONTENT SEARCH -- not a line-number guess. The board file shifts under other
  edits between the act and the undo, so a stored ``line_number`` is stale; we
  re-insert the line only if its exact text is not already present (idempotent),
  near the recorded ``line_number`` when it still fits, else appended in-section.

Every reversal is gated by the tiered undo window (Decision #8): 4h for nag acts,
7d for board mutations (both env-tunable via ``cos_config``). An act already
reverted, past its window, or unknown is refused with a structured reason -- never
a traceback (NO-RAW-ERROR-LEAK; the caller wraps this through U1's envelope).
"""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None

from cos_config import state_dir, undo_window_board_hours, undo_window_nag_hours
from task_ledger import append_event, ledger_path, new_event
from utils import _atomic_write

from autonomy_gate import (
    RUNG_READ,
    ack_nag,
    find_act,
    read_autonomy_log,
    _log_act,
)

# Act-type prefixes that the nag undo path owns. A nag act is reversed by acking
# the task's nag loop, not by touching the board (a nag makes no board mutation).
_NAG_ACT_PREFIX = "nag_"

# The two undo "kinds" an act resolves to. Kept as named constants so the
# dispatch in undo_act() reads as a closed set rather than scattered strings.
_KIND_NAG = "nag"
_KIND_BOARD = "board"
_KIND_NONE = "none"  # nothing reversible to do (e.g. a blocked act)


def _parse_iso(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp to an aware UTC datetime, or None on garbage.

    A malformed/absent timestamp must not crash the undo or audit path; the
    caller treats ``None`` as "cannot prove this is inside the window" and fails
    closed (refuses the undo) rather than guessing.
    """
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _act_kind(record: dict[str, Any]) -> str:
    """Classify an act into the reversal kind that owns it.

    A board mutation is identified by a non-empty ``raw_line`` in its
    ``pre_action_snapshot`` -- that is the ONLY thing ``undo_act`` can restore by
    content. A ``nag_*`` act is a nag reversal. Everything else has nothing to
    reverse.
    """
    snapshot = record.get("pre_action_snapshot")
    if isinstance(snapshot, dict) and str(snapshot.get("raw_line") or "").strip():
        return _KIND_BOARD
    if str(record.get("act_type") or "").startswith(_NAG_ACT_PREFIX):
        return _KIND_NAG
    return _KIND_NONE


def _undo_window_hours(record: dict[str, Any]) -> int:
    """The tiered undo window (Decision #8) for this act, in hours.

    Board mutations get the long window (7d default); nag acts get the short one
    (4h default). The kind drives the window, so a nag act and a board act read
    from the same log are each held to their own policy.
    """
    if _act_kind(record) == _KIND_BOARD:
        return undo_window_board_hours()
    return undo_window_nag_hours()


def _within_window(record: dict[str, Any], *, now: datetime | None = None) -> bool:
    """Is this act still inside its tiered undo window?

    Fails closed: an unparseable/absent timestamp returns False (not undoable)
    rather than treating a missing timestamp as "always fresh".
    """
    stamped = _parse_iso(record.get("timestamp"))
    if stamped is None:
        return False
    now = now or datetime.now(timezone.utc)
    age_hours = (now - stamped).total_seconds() / 3600.0
    return age_hours <= _undo_window_hours(record)


def _already_reverted(act_id: str) -> bool:
    """Has a ``reverted`` record already been appended for this act_id?

    Undo is idempotent-by-refusal: a second ``/undo`` of the same act is refused
    rather than acked-again / re-inserting the board line twice.
    """
    for entry in read_autonomy_log():
        if entry.get("act_id") == act_id and entry.get("status") == "reverted":
            return True
    return False


# --- /audit read model -----------------------------------------------------

def list_acts(*, since_hours: int | None = None, limit: int = 20) -> list[dict[str, Any]]:
    """Return recent gated acts, newest first, for ``/audit``.

    Only canonical (first-per-act_id) gate records are returned -- a forged later
    append for an act_id cannot inject a phantom audit row -- and ``reverted``
    bookkeeping records are folded into their original act as ``reverted: True``
    rather than listed as separate acts. Capped at ``limit``.

    The default window is the BOARD undo window (7d), not an arbitrary 48h: the
    documented flow is ``/audit`` to discover an act_id, then ``/undo`` it, so
    every act ``/undo`` will still accept (board acts stay reversible 7d) must be
    discoverable through the default listing. A shorter audit window would hide a
    still-undoable act for two-thirds of its life. An explicit ``since_hours``
    narrows it.
    """
    if since_hours is None:
        since_hours = undo_window_board_hours()
    now = datetime.now(timezone.utc)
    canonical: dict[str, dict[str, Any]] = {}
    reverted_ids: set[str] = set()
    for entry in read_autonomy_log():
        act_id = entry.get("act_id")
        if not isinstance(act_id, str):
            continue
        if entry.get("status") == "reverted":
            reverted_ids.add(act_id)
            continue
        canonical.setdefault(act_id, entry)  # first record per act_id wins

    rows: list[dict[str, Any]] = []
    for act_id, entry in canonical.items():
        stamped = _parse_iso(entry.get("timestamp"))
        if stamped is None or (now - stamped).total_seconds() / 3600.0 > since_hours:
            continue
        row = dict(entry)
        row["reverted"] = act_id in reverted_ids
        rows.append(row)

    rows.sort(key=lambda r: str(r.get("timestamp") or ""), reverse=True)
    return rows[:limit]


def find_act_detail(act_id: str) -> dict[str, Any] | None:
    """Full canonical record for one act (``/audit act_<id>``), or None.

    Returns the canonical (first) gate record augmented with ``reverted`` so the
    detail view shows whether the act was already undone.
    """
    record = find_act(act_id)
    if record is None:
        return None
    detail = dict(record)
    detail["reverted"] = _already_reverted(act_id)
    return detail


# --- /undo -----------------------------------------------------------------

def _refuse(act_id: str, reason: str, message: str) -> dict[str, Any]:
    return {"ok": False, "act_id": act_id, "reason": reason, "message": message}


@contextmanager
def _undo_lock():
    """Hold an exclusive flock for the duration of one undo cycle.

    Mirrors ``autonomy_gate.ack_nag``'s sidecar-lockfile pattern so the
    `_already_reverted` check, the board/nag mutation, and the reverted-marker
    write happen atomically with respect to any other concurrent undo. The lock
    is process-wide (single ``undo.lock`` sidecar) rather than per-act, which
    keeps the lockfile set bounded for a reactive command. A POSIX-less host
    (``fcntl is None``) degrades to a no-op lock -- those platforms do not run the
    concurrent gateway this guards.
    """
    state_dir()  # ensure the 0o700 dir exists before opening the lockfile
    lock_path = state_dir() / "undo.lock"
    with lock_path.open("a", encoding="utf-8") as handle:
        try:
            os.fchmod(handle.fileno(), 0o600)
        except OSError:
            pass
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def undo_act(act_id: str) -> dict[str, Any]:
    """Reverse a gated act by kind, gated by the tiered undo window.

    Returns a structured dict (never raises to the caller): ``{"ok": True, ...}``
    on a completed reversal, or ``{"ok": False, "reason": ..., "message": ...}``
    on any refusal (unknown act, already reverted, window expired, not executed,
    nothing-to-undo, or an internal error). The caller surfaces ``message``; the
    raw reason is for the audit trail.
    """
    try:
        # Serialize the whole check-mutate-mark cycle under an exclusive lock so
        # the `_already_reverted` guard is atomic with the mutation + the reverted
        # marker write. Without it, two concurrent `/undo act_X` calls both pass
        # the guard and each append a duplicate `state_transition_reverted` event;
        # a retry after a partial failure has the same hazard. The board restore is
        # idempotent so state never corrupts, but the audit ledger would gain
        # duplicate revert events. One process-wide lock (not per-act) keeps the
        # lockfile set bounded and is fine for a reactive, low-frequency command.
        with _undo_lock():
            return _undo_act_inner(act_id)
    except Exception as exc:  # noqa: BLE001 -- this IS the no-leak boundary
        # NO-RAW-ERROR-LEAK: any fault on the undo path (IO on a board/state/ledger
        # write, or a malformed-state ValueError/KeyError/JSONDecodeError) returns a
        # structured error, never a traceback. A bare OSError catch would let a
        # non-IO fault escape and contradict the "never raises" contract.
        #
        # The breadcrumb write itself can raise (the fault may BE an unwritable
        # autonomy log) -- guard it best-effort so a failed breadcrumb can never
        # turn the refusal into a re-raise. The structured refusal is always
        # returned regardless.
        try:
            _log_undo_outcome(act_id, "error", reason="error:internal",
                              detail=type(exc).__name__)
        except Exception:  # noqa: BLE001 -- breadcrumb is best-effort, never fatal
            pass
        return _refuse(act_id, "error:internal",
                       "Undo could not complete due to an internal error; logged for review.")


def _undo_act_inner(act_id: str) -> dict[str, Any]:
    record = find_act(act_id)
    if record is None:
        return _refuse(act_id, "unknown-act", f"No gated act {act_id} to undo.")

    if record.get("status") != "executed":
        # A blocked act made no change; there is nothing to reverse.
        return _refuse(act_id, "act-not-executed",
                       f"Act {act_id} was not executed ({record.get('status')}); nothing to undo.")

    if _already_reverted(act_id):
        return _refuse(act_id, "already-reverted", f"Act {act_id} was already undone.")

    if not _within_window(record):
        return _refuse(act_id, "undo-window-expired",
                       f"Act {act_id} is past its undo window; it can no longer be undone.")

    kind = _act_kind(record)
    if kind == _KIND_NAG:
        return _undo_nag(act_id, record)
    if kind == _KIND_BOARD:
        return _undo_board(act_id, record)
    return _refuse(act_id, "nothing-to-undo", f"Act {act_id} has no reversible effect.")


def _undo_nag(act_id: str, record: dict[str, Any]) -> dict[str, Any]:
    """Reverse a nag act: ack the task's nag loop + record the reversal.

    Writes ``ack_type="user_undo"`` into ``nag-state.json`` (Contract 3 stub) so
    the U4 nag loop will not re-fire, then appends the cross-ledger reversal
    record. A nag act makes NO board mutation, so there is no board restore.
    """
    task_id = record.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        return _refuse(act_id, "nag-missing-task",
                       f"Nag act {act_id} has no task_id; cannot ack a nag loop.")
    ack_nag(task_id, ack_type="user_undo")
    self_event = _append_ledger_revert(act_id, record, board_restored=False)
    self_record = _log_undo_outcome(act_id, "reverted", reason="nag-acked",
                                    task_id=task_id, board_restored=False)
    return {
        "ok": True,
        "act_id": act_id,
        "kind": _KIND_NAG,
        "task_id": task_id,
        "board_restored": False,
        "message": f"Undid {act_id}: nag loop for {task_id} silenced (acked).",
        "ledger_event": self_event,
        "log_record": self_record,
    }


def _undo_board(act_id: str, record: dict[str, Any]) -> dict[str, Any]:
    """Reverse a board mutation: restore the snapshot ``raw_line`` by CONTENT SEARCH.

    The board file shifts under other edits between the act and the undo, so the
    stored ``line_number`` is only a HINT for placement -- the restore is keyed on
    the exact line TEXT, not the number. Two mutation kinds are reversed correctly:

    * If the snapshot carries ``post_raw_line`` (what the act WROTE -- e.g. a
      ``[ ]`` -> ``[x]`` checkbox toggle), the undo REPLACES that post-action line
      with the original ``raw_line`` in place, so the board never ends up with two
      copies of the task.
    * Otherwise the act removed a line; the undo RE-INSERTS the original
      ``raw_line`` near the hinted position.

    Both are idempotent: if the board already shows the original line (and not the
    post-action one), the restore is a no-op.
    """
    snapshot = record["pre_action_snapshot"]
    raw_line = str(snapshot.get("raw_line") or "")
    board_file = snapshot.get("file")
    if not board_file:
        return _refuse(act_id, "snapshot-no-file",
                       f"Act {act_id} snapshot names no board file; cannot restore.")
    path = Path(board_file)
    if not path.exists():
        return _refuse(act_id, "board-file-missing",
                       f"Board file for {act_id} is missing; cannot restore the line.")

    content = path.read_text(encoding="utf-8")
    new_content, restored = restore_line_by_content(
        content, raw_line,
        post_raw_line=snapshot.get("post_raw_line"),
        line_number_hint=snapshot.get("line_number"),
    )
    if restored:
        _atomic_write(path, new_content)

    self_event = _append_ledger_revert(act_id, record, board_restored=restored,
                                        task_id=record.get("task_id"))
    self_record = _log_undo_outcome(act_id, "reverted", reason="board-restored",
                                    task_id=record.get("task_id"), board_restored=restored)
    note = ("restored to the board" if restored
            else "was already on the board (no change needed)")
    return {
        "ok": True,
        "act_id": act_id,
        "kind": _KIND_BOARD,
        "task_id": record.get("task_id"),
        "board_restored": restored,
        "message": f"Undid {act_id}: task line {note}.",
        "ledger_event": self_event,
        "log_record": self_record,
    }


def _split_keep_trailing(content: str) -> tuple[list[str], bool]:
    """Split into lines, separating the trailing-newline marker from the content.

    ``"a\\nb\\n".split("\\n")`` yields a spurious ``""`` tail that, after re-join,
    silently turns into a blank line + a lost trailing newline. We instead split on
    line content only and remember whether the file ended in ``\\n`` so ``_join``
    can reproduce the exact terminator. Returns ``(lines, had_trailing_newline)``.
    """
    if content.endswith("\n"):
        return content[:-1].split("\n"), True
    return content.split("\n"), False


def _join_keep_trailing(lines: list[str], had_trailing_newline: bool) -> str:
    text = "\n".join(lines)
    return text + "\n" if had_trailing_newline else text


def restore_line_by_content(
    content: str,
    raw_line: str,
    *,
    post_raw_line: Any = None,
    line_number_hint: Any = None,
) -> tuple[str, bool]:
    """Restore ``raw_line`` to ``content`` by content, idempotently, by mutation kind.

    Returns ``(new_content, restored)``. The match is on EXACT line text, never a
    line number; the hint only positions a fresh insert. The file's trailing
    newline is preserved (no blank-line injection). An empty ``raw_line`` is a
    no-op.

    * REPLACE (in-place edit): when ``post_raw_line`` is given and present in the
      file, that line is replaced with ``raw_line`` -- this reverses a checkbox
      toggle without leaving a duplicate. If ``post_raw_line`` is already gone but
      ``raw_line`` is present, it is a no-op (already reversed).
    * RE-INSERT (deletion): when there is no ``post_raw_line`` to replace, insert
      ``raw_line`` only if absent, near ``line_number_hint`` (clamped into range),
      else append. If it is already present, no-op.
    """
    if not raw_line.strip():
        return content, False

    lines, had_nl = _split_keep_trailing(content)

    post = str(post_raw_line) if post_raw_line is not None else ""
    if post.strip() and post in lines:
        # In-place reversal: swap the act's written line back to the original.
        lines[lines.index(post)] = raw_line
        return _join_keep_trailing(lines, had_nl), True

    if raw_line in lines:
        return content, False  # already restored: idempotent

    insert_at = len(lines)
    try:
        hint = int(line_number_hint) - 1  # snapshot line_number is 1-based
    except (TypeError, ValueError):
        hint = None
    if hint is not None:
        insert_at = max(0, min(hint, len(lines)))

    lines.insert(insert_at, raw_line)
    return _join_keep_trailing(lines, had_nl), True


def _ledger_revert_exists(act_id: str) -> bool:
    """Is there already a ``state_transition_reverted`` event for this act_id?

    The undo cycle writes two files sequentially (the task-ledger revert event,
    then the autonomy-log ``reverted`` marker) and the double-undo guard reads only
    the marker. If the marker write fails AFTER the ledger event committed, a retry
    would re-append the ledger event. This check makes the ledger append itself
    idempotent on ``reverted_act_id`` so the ledger holds at most one revert per
    act regardless of where a partial failure landed.
    """
    from task_ledger import read_events
    for event in read_events(path=ledger_path()):
        if (event.get("event_type") == "state_transition_reverted"
                and (event.get("metadata") or {}).get("reverted_act_id") == act_id):
            return True
    return False


def _append_ledger_revert(
    act_id: str,
    record: dict[str, Any],
    *,
    board_restored: bool,
    task_id: str | None = None,
) -> dict[str, Any] | None:
    """Append a ``state_transition_reverted`` event to the task ledger, idempotently.

    Links back to the original act via ``metadata.reverted_act_id`` (Contract 1)
    and carries ``source="agent_autonomous"`` since the original mutation was an
    agent-initiated act. Returns ``None`` (without appending) when a revert event
    for this act already exists -- so a partial-failure retry cannot double-log a
    revert. A ledger fault is surfaced by the caller's exception handling.
    """
    if _ledger_revert_exists(act_id):
        return None
    event = new_event(
        "state_transition_reverted",
        task_id=task_id if task_id is not None else record.get("task_id"),
        source="agent_autonomous",
        reason="undo_act",
        metadata={
            "reverted_act_id": act_id,
            "act_type": record.get("act_type"),
            "board_restored": board_restored,
        },
    )
    append_event(event, path=ledger_path())
    return event


def _log_undo_outcome(
    act_id: str,
    status: str,
    *,
    reason: str | None = None,
    task_id: str | None = None,
    board_restored: bool | None = None,
    detail: str | None = None,
) -> dict[str, Any]:
    """Append a reversal bookkeeping record to the autonomy log.

    A ``reverted`` record marks the original ``act_id`` as undone -- this is what
    ``_already_reverted`` reads to refuse a double-undo and what ``list_acts``
    folds into the audit view. It is a SECOND record for the same ``act_id`` and
    never replaces the canonical first one (``find_act`` always binds the first).

    Built through the canonical ``autonomy_gate._log_act`` so the audit log has a
    single record shape -- not a second hand-rolled copy of the Contract-4 fields.
    The reversal carries a ``reversal_id`` and the outcome reason in metadata.
    """
    metadata: dict[str, Any] = {"reversal_id": f"rev_{uuid.uuid4().hex[:12]}"}
    if reason is not None:
        metadata["reason"] = reason
    if board_restored is not None:
        metadata["board_restored"] = board_restored
    if detail is not None:
        metadata["detail"] = detail
    return _log_act(
        act_id, "undo", RUNG_READ, status,
        task_id=task_id, unit="U2", agent_id="task-tracker",
        reversible=False, metadata=metadata,
    )
