#!/usr/bin/env python3
"""H8 manual-win store: the sole reader/writer of ``manual-wins.jsonl``.

``/win <text>`` is a FRICTIONLESS capture: no board cap, no validation gate, no
matching -- the founder types one accomplishment and it is durably persisted so it
survives a crash and surfaces in the weekly brag digest. Auto-harvest over-counts
code/comms (PRs + email) while missing strategy / hiring / decisions /
relationships; ``/win`` is the manual channel that captures exactly those, routed
into the four-bucket digest by a lightweight classifier.

Design rules (mirroring ``harvest_state.py`` so no unit invents its own variant):

* **Single writer.** Only this module appends to ``manual-wins.jsonl``; the append
  is flocked + line-atomic (one JSON object per line, the SAME idiom
  ``task_ledger.append_event`` uses) so a crash mid-write never tears a line and a
  concurrent capture never interleaves.
* **Capture never blocks.** ``append_win`` performs no cap check, no board read, no
  network call -- the only failure mode is an unwritable state dir, which is the
  caller's friendly-envelope concern, not a capture gate.
* **Fail-soft read.** A missing file reads as ``[]``; a torn/corrupt line is skipped
  (best-effort forensics), never raised -- one bad line must not hide every other
  win from the digest.
"""

from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None

import cos_config

# The four digest buckets a manual win can land in. ``shipped`` / ``maintenance``
# are reachable only by an explicit leading tag (``/win shipped: ...``); a bare
# ``/win`` text classifies to ``decisions`` (a decision/hire/choice was made) or
# falls back to ``advanced`` (it moved something forward) -- the two buckets the
# auto-harvest structurally cannot fill.
DEFAULT_BUCKET = "advanced"
DECISIONS_BUCKET = "decisions"
WIN_BUCKETS = ("shipped", "advanced", "decisions", "maintenance")

# Words that mark a win as a DECISION/strategy/hiring win (the harvest's blind spot).
# Whole-word anchored so "undecided" / "advanced" do not false-trigger.
_DECISION_RE = re.compile(
    r"\b(decid(?:e|ed|es)|chose|chosen|hir(?:e|ed)|approv(?:e|ed)|"
    r"agreed|committed|signed|picked|settled)\b",
    re.IGNORECASE,
)
# An explicit leading bucket tag, e.g. ``/win shipped: cut the release``.
_TAG_RE = re.compile(r"^\s*(shipped|advanced|decisions?|maintenance)\s*[:\-]\s*", re.IGNORECASE)


def wins_path() -> Path:
    """The append-only manual-wins log under the Chief-of-Staff state dir."""
    raw = os.getenv("TASK_TRACKER_WINS_FILE")
    if raw:
        return Path(raw).expanduser()
    return cos_config.state_dir() / "manual-wins.jsonl"


def seen_wins_path() -> Path:
    """The append-only ``seen-wins`` log: one win id per line, marked on a pushed digest.

    Mirrors ``harvest_state``'s ``seen_hashes`` for evidence -- a win is the SAME
    kind of consumable as a PR/email, so it earns the same seen-on-push dedup. It
    lives in its own flocked append-only sidecar (NOT the per-window
    ``harvest-state.json``) precisely BECAUSE a win must survive the weekly window
    reset: ``harvest_state``'s seen set is scoped to one ISO week, while a win
    captured after Friday's push must still be unseen NEXT week. A win id appended
    here is durable across windows, so a delivered win never repeats and an
    undelivered one keeps surfacing.
    """
    return wins_path().with_name("seen-wins.jsonl")


def win_id(record: dict[str, Any]) -> str:
    """The stable id for one win record (used for seen-on-push dedup).

    Derived from the immutable capture facts (``ts`` + ``text`` + ``actor``) so the
    id is identical on every later read of the same line -- the SAME hash idiom
    ``harvest_ledger._evidence_hash`` uses for evidence. A pre-id legacy record (no
    ``ts``) still yields a deterministic id from its text/actor.
    """
    existing = record.get("id")
    if isinstance(existing, str) and existing:
        return existing
    basis = f"{record.get('ts', '')}:{record.get('actor', '')}:{record.get('text', '')}"
    return f"win:{sha256(basis.encode('utf-8')).hexdigest()[:24]}"


def classify_bucket(text: str) -> tuple[str, str]:
    """Resolve ``text`` into ``(bucket, cleaned_text)`` for a manual win.

    An explicit leading tag (``shipped:`` / ``decisions:`` / ...) wins and is
    stripped from the stored text; otherwise a decision/hire/choice phrase routes to
    ``decisions`` and everything else to ``advanced`` -- the two buckets the
    PR/email harvest structurally cannot populate. ``decision`` and ``decisions``
    both normalise to the ``decisions`` bucket.
    """
    tagged = _TAG_RE.match(text)
    if tagged:
        bucket = tagged.group(1).lower()
        if bucket == "decision":
            bucket = DECISIONS_BUCKET
        return bucket, text[tagged.end():].strip()
    cleaned = text.strip()
    if _DECISION_RE.search(cleaned):
        return DECISIONS_BUCKET, cleaned
    return DEFAULT_BUCKET, cleaned


def append_win(text: str, *, actor: str = "niemand-work") -> dict[str, Any]:
    """Durably append one manual win; FRICTIONLESS (no cap, no validation, no match).

    Classifies the text into a bucket, stamps a UTC timestamp + local date, and
    appends one flocked JSON line so a crash mid-write leaves the prior wins intact.
    Returns the stored record. Raises only if the state dir itself is unwritable
    (the caller's envelope turns that into the friendly notice) -- there is NO
    capture gate that can refuse a real accomplishment.
    """
    bucket, cleaned = classify_bucket(text)
    record = {
        "text": cleaned,
        "bucket": bucket,
        "actor": actor,
        "ts": datetime.now(timezone.utc).isoformat(),
        "captured_on": cos_config.local_today().isoformat(),
    }
    # Stamp the stable seen-dedup id from the immutable capture facts BEFORE writing,
    # so every later read resolves the same id and a delivered win never repeats.
    record["id"] = win_id(record)
    path = wins_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(record, ensure_ascii=False, sort_keys=True)
    with path.open("a", encoding="utf-8") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.write(rendered + "\n")
            handle.flush()
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return record


def read_wins(*, since: str | None = None) -> list[dict[str, Any]]:
    """Read every captured win, optionally only those captured on/after ``since``.

    Fail-soft: a missing file is ``[]`` and a torn/corrupt line is skipped, never
    raised -- one bad line must not hide the rest from the digest. ``since`` is the
    inclusive ``YYYY-MM-DD`` lower bound the weekly window supplies, so a win from a
    prior window does not re-surface in this week's digest.
    """
    path = wins_path()
    if not path.exists():
        return []
    wins: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue  # skip a torn line; never let it hide the others
        if not isinstance(record, dict):
            continue
        if since and (record.get("captured_on") or "") < since:
            continue
        record.setdefault("id", win_id(record))
        wins.append(record)
    return wins


def read_seen_win_ids() -> set[str]:
    """The set of win ids already delivered in a pushed digest (fail-soft).

    A missing file is an empty set; a torn/corrupt line is skipped (never raised) --
    one bad line must not make a delivered win re-surface. Mirrors ``read_wins``'s
    fail-soft posture.
    """
    path = seen_wins_path()
    if not path.exists():
        return set()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return set()
    seen: set[str] = set()
    for line in lines:
        candidate = line.strip()
        if candidate:
            seen.add(candidate)
    return seen


def read_unseen_wins() -> list[dict[str, Any]]:
    """Every captured win NOT yet delivered in a pushed digest, regardless of date.

    Deliberately UNFILTERED by capture date: an unseen win older than this week's
    Monday must still surface (the weekly ``since`` window would drop it forever, so
    seen-on-push -- not the window -- is what consumes a win). A delivered win is
    excluded by its id being in the seen set.
    """
    seen = read_seen_win_ids()
    return [w for w in read_wins() if w["id"] not in seen]


def mark_wins_seen(win_ids: list[str]) -> None:
    """Durably record ``win_ids`` as delivered (idempotent, flocked, append-only).

    Called ONLY after a digest ACTUALLY PUSHED -- the SAME success condition that
    marks evidence ``seen_hashes``. Skips ids already recorded so a re-fire never
    grows the file unbounded. Append-only + flocked, the SAME crash-safe idiom
    ``append_win`` uses. Fail-soft: a write failure is the caller's envelope concern,
    but an already-recorded id is simply not re-appended.
    """
    fresh = [wid for wid in dict.fromkeys(win_ids) if wid]
    if not fresh:
        return
    already = read_seen_win_ids()
    pending = [wid for wid in fresh if wid not in already]
    if not pending:
        return
    path = seen_wins_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.write("".join(f"{wid}\n" for wid in pending))
            handle.flush()
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
