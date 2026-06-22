#!/usr/bin/env python3
"""U5 accomplishment ledger: harvest -> match -> draft push -> approve -> auto-mark.

Replaces the broken ``/done24h`` / ``/done7d`` commands. It pulls evidence of
shipped work from GitHub (merged PRs via ``gh``) and Gmail (sent mail via ``gog``;
calendar harvest is DEFERRED to v0.3 per Decisions #4), matches it against the
active task board, and assembles a brag-doc draft. The draft is pushed to the
Productivity Done topic ONLY through the proven delivery seam, and on an explicit
``/approve`` the matched task is marked done -- reversibly.

Invariant (one line): every autonomous act that pushes output to Telegram must
prove its delivery target before sending, and every auto-mark must be reversible
by the owner in one step.

Hard seams enforced here:

* **DELIVERY-TARGET-PROOF.** The draft push resolves its target from env via
  ``delivery_target.prove_delivery_target`` and binds it through
  ``autonomy_gate.gate`` (returning an ``act_id``). The ``message()`` send is then
  asserted against that gated target with ``autonomy_gate.assert_send_target`` --
  a Work-group / unknown / unset-env target binds nothing and nothing is sent.
* **TOPIC GUARD on /approve.** A reactive ``/approve`` proves its *origin* topic
  automatically, but origin != correctness -- ``approve`` rejects when the inbound
  topic id != the Productivity Done topic.
* **REVERSIBILITY.** Before each ``complete_by_id`` call a ``pre_action_snapshot``
  event is appended to the ledger; if the board write then fails a
  ``pre_action_snapshot_cancelled`` compensating event is appended so audit replay
  stays consistent, and ``approved_task_ids`` is left untouched.
* **NO RAW ERROR LEAK.** Every harvest subprocess failure is caught, classified,
  and logged through ``error_envelope`` (the canonical error log) -- never echoed.

The script does NOT call the Telegram ``message`` tool itself; it emits the proven
``delivery_target`` + draft text as structured output and the agent relays it. The
push proof (gate + assert) is exercised here so a buggy relay cannot send to an
unproven target.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

import autonomy_gate
import cos_config
import delivery_target
import error_envelope
import harvest_state
import win_store
from evidence_matching import extract_done_lines, match_evidence_content
from task_ledger import append_event, ledger_path, new_event
from task_transitions import complete_by_id

COMPONENT = "ledger_harvest"
ACTOR = "niemand-work"
LEDGER_SOURCE = "ledger_agent"
DRAFT_ACT_TYPE = "ledger_draft_pushed"

_SUBPROCESS_FAILURES = (
    subprocess.TimeoutExpired,
    subprocess.CalledProcessError,
    json.JSONDecodeError,
    FileNotFoundError,
    OSError,
)


# --- evidence model --------------------------------------------------------


def _evidence_hash(source_type: str, canonical_id: str) -> str:
    """A stable dedup hash for an evidence item (source + canonical id)."""
    digest = sha256(f"{source_type}:{canonical_id}".encode("utf-8")).hexdigest()[:24]
    return f"sha256:{source_type}:{digest}"


def _evidence(
    source_type: str,
    match_title: str,
    canonical_id: str,
    url: str | None,
    *,
    display_suffix: str = "",
) -> dict[str, Any]:
    """Build an evidence item.

    ``match_title`` is the clean title matched against the board (no annotation,
    so the ``[repo#N]`` reference never dilutes the fuzzy score). ``title`` is the
    human-facing display line that appends ``display_suffix`` (e.g. the PR ref).
    """
    return {
        "source_type": source_type,
        "match_title": match_title,
        "title": f"{match_title} {display_suffix}".rstrip(),
        "url": url,
        "evidence_hash": _evidence_hash(source_type, canonical_id),
    }


# --- harvest sources -------------------------------------------------------
# Every source funnels through _harvest, which owns the breaker + no-raw-leak
# error handling ONCE, so github/gmail never copy-paste a try/except.


def _harvest(
    source: str,
    cmd: list[str],
    parse: Callable[[dict], list[dict[str, Any]]],
    *,
    trigger: str,
    timeout: int = 15,
) -> tuple[list[dict[str, Any]], bool]:
    """Run one harvest subprocess and parse it; return ``(evidence, failed)``.

    A missing/broken tool that has failed repeatedly trips the circuit breaker --
    the subprocess is skipped entirely so a daily cron does not loop on it. Any
    failure (nonzero exit / exception / timeout / a tripped breaker) is logged
    through ``error_envelope`` (classified, never echoed) and yields
    ``([], failed=True)``; this function NEVER raises.

    The ``failed`` flag is what distinguishes a SOURCE ERROR (couldn't harvest --
    the cron path records a health FAILURE) from a legitimately-empty source (ran
    clean, nothing to report -- a quiet week stays healthy). A tripped breaker is a
    failure too: the source is unprovenly skipped, NOT confirmed empty. Without this
    flag a gh/gog subprocess failure silently swallows to ``[]`` and false-greens.
    """
    component = f"{COMPONENT}:{source}"
    if error_envelope.breaker_open(component):
        return [], True
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            raise subprocess.CalledProcessError(
                result.returncode, cmd[0], output=result.stdout, stderr=result.stderr
            )
        return parse(json.loads(result.stdout)), False
    except _SUBPROCESS_FAILURES as exc:
        error_envelope.log_degraded(component, exc, trigger=trigger, check=source)
        return [], True


def _since_date(window: str, since_override: str | None, reference: date | None = None) -> str:
    """The harvest-window start date (YYYY-MM-DD)."""
    if since_override:
        return since_override
    ref = reference or date.today()
    if window == harvest_state.WINDOW_24H:
        return (ref - timedelta(days=1)).isoformat()
    # Weekly window: start of the current ISO week (Monday).
    return (ref - timedelta(days=ref.weekday())).isoformat()


def harvest_github(since: str, *, trigger: str) -> tuple[list[dict[str, Any]], bool]:
    """Harvest merged PRs authored by the user since ``since`` (``gh``).

    Returns ``(evidence, failed)`` -- ``failed`` is True when the ``gh`` subprocess
    errored (so a source error is not mistaken for a clean-empty week)."""

    def parse(payload: Any) -> list[dict[str, Any]]:
        items = payload if isinstance(payload, list) else payload.get("items", [])
        evidence: list[dict[str, Any]] = []
        for pr in items:
            repo = pr.get("repository") or {}
            repo_name = repo.get("nameWithOwner") or repo.get("name") or ""
            number = pr.get("number")
            url = pr.get("url")
            title = pr.get("title") or ""
            if not title or number is None:
                continue
            canonical = f"{repo_name}#{number}"
            evidence.append(_evidence("pr", title, canonical, url, display_suffix=f"[{canonical}]"))
        return evidence

    cmd = [
        "gh", "search", "prs", "--author", "@me", "--merged",
        "--merged-at", f">={since}",
        "--json", "title,closedAt,repository,url,number",
        "--limit", "100",
    ]
    return _harvest("github", cmd, parse, trigger=trigger)


def harvest_gmail(since: str, *, trigger: str) -> tuple[list[dict[str, Any]], bool]:
    """Harvest sent-mail subjects since ``since`` (``gog``).

    Returns ``(evidence, failed)`` -- ``failed`` is True when the ``gog`` subprocess
    errored (so a source error is not mistaken for a clean-empty week)."""

    def parse(payload: Any) -> list[dict[str, Any]]:
        threads = payload.get("threads", []) if isinstance(payload, dict) else []
        evidence: list[dict[str, Any]] = []
        for thread in threads:
            thread_id = thread.get("id")
            subject = thread.get("subject") or ""
            if not thread_id or not subject:
                continue
            evidence.append(_evidence("email", subject, str(thread_id), None))
        return evidence

    gmail_after = since.replace("-", "/")
    cmd = ["gog", "gmail", "search", f"in:sent after:{gmail_after}", "--max", "50", "--json"]
    return _harvest("gmail", cmd, parse, trigger=trigger)


def harvest_all(since: str, *, trigger: str) -> tuple[list[dict[str, Any]], int, bool]:
    """Harvest every (non-deferred) source. Returns ``(evidence, sources_tried, source_error)``.

    Calendar harvest is DEFERRED to v0.3 (``STANDUP_CALENDARS`` not in container),
    so only GitHub + Gmail are tried in v0.2. ``source_error`` is True when ANY
    source could not be harvested (subprocess error / tripped breaker), so the cron
    path can record a health FAILURE rather than mistaking an unharvested source for
    a quiet week.
    """
    gh_evidence, gh_failed = harvest_github(since, trigger=trigger)
    gmail_evidence, gmail_failed = harvest_gmail(since, trigger=trigger)
    return gh_evidence + gmail_evidence, 2, gh_failed or gmail_failed


# --- matching --------------------------------------------------------------


# A title that the done-line extractor would split (embedded newline) or clean to
# empty (whitespace / a lone checkmark / a stripped time prefix) would break the
# 1:1 evidence<->match alignment -- by yielding zero or multiple parsed lines. We
# normalise to a single line and substitute a stable, unmatchable placeholder when
# the canonical extractor would not yield EXACTLY one line, so every evidence item
# yields exactly one parsed line and positional alignment is guaranteed.
_UNMATCHABLE_PLACEHOLDER = "ledger-no-match-placeholder"
_WHITESPACE_RE = re.compile(r"\s+")


def _synthetic_line_title(match_title: str) -> str:
    """The single-line, never-dropped title for one evidence item's synthetic line.

    Collapses all whitespace (including newlines, which would otherwise split the
    synthetic content into multiple lines) to one space, then PROBES the canonical
    ``extract_done_lines`` on the candidate line: if it does not yield exactly one
    parsed line (the title is stripped to empty -- e.g. a lone ``✅`` or a
    ``HH:MM ✅`` time-only subject), it falls back to an unmatchable placeholder.
    Delegating the emptiness decision to the real extractor (rather than
    re-implementing its strip rules) means this guard can never drift from it.
    """
    collapsed = _WHITESPACE_RE.sub(" ", match_title).strip()
    if collapsed and len(extract_done_lines(f"- [x] {collapsed}")) == 1:
        return collapsed
    return _UNMATCHABLE_PLACEHOLDER


def _synthetic_content(evidence: list[dict[str, Any]]) -> str:
    """Build the synthetic markdown ``match_evidence_content`` parses.

    Each evidence ``match_title`` becomes a checked-bullet line so it flows
    through the existing done-line extractor + matcher unchanged (no bespoke
    matching). The display annotation (``[repo#N]``) is deliberately excluded so
    it never dilutes the fuzzy score.
    """
    return "\n".join(f"- [x] {_synthetic_line_title(item['match_title'])}" for item in evidence)


def match_evidence(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Match harvested evidence against the active board via the shared matcher.

    Returns one enriched record per evidence item, carrying its source metadata
    plus the matcher's decision (``evidence-link`` / ``needs-review`` / ``no-match``)
    and matched task id. Each evidence item maps to exactly one synthetic
    done-line (placeholder-guarded), so the matcher's per-line output aligns 1:1
    with ``evidence`` -- this is asserted, never assumed.
    """
    if not evidence:
        return []
    content = _synthetic_content(evidence)
    _parsed, matched = match_evidence_content(content)
    if len(matched) != len(evidence):
        # The placeholder guard makes this unreachable; assert rather than risk
        # silent wrong-task attribution from a positional zip over misaligned lists.
        raise AssertionError(
            f"evidence/match misalignment: {len(evidence)} items vs {len(matched)} matches"
        )
    enriched: list[dict[str, Any]] = []
    for item, match in zip(evidence, matched, strict=True):
        meta = match.get("match_metadata", {})
        enriched.append({
            **item,
            "decision": meta.get("decision", "no-match"),
            "matched_task_id": meta.get("matched_task_id"),
            "score": meta.get("score", 0.0),
            "match_type": meta.get("match_type"),
        })
    return enriched


def _pending_match_index(matches: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Index match provenance by task id, so a reactive ``/approve`` can rebuild
    the ``evidence_link`` (source_type / url / score / match_type) without the
    caller having to re-supply it -- the inbound Telegram command carries only the
    task id, not the original match.
    """
    index: dict[str, dict[str, Any]] = {}
    for m in matches:
        task_id = m.get("matched_task_id")
        if task_id and m["decision"] in ("evidence-link", "needs-review"):
            index[task_id] = {
                "source_type": m.get("source_type"),
                "url": m.get("url"),
                "score": m.get("score"),
                "match_type": m.get("match_type"),
            }
    return index


# --- draft assembly --------------------------------------------------------


def _bucket(matches: list[dict[str, Any]], decision: str) -> list[dict[str, Any]]:
    return [m for m in matches if m["decision"] == decision]


# H8 four-bucket digest. The Oracle finding: the auto-harvest over-counts code +
# comms (PR + email) while missing strategy / hiring / decisions / relationships.
# So harvested evidence is CLASSIFIED into shipped / advanced / maintenance (it can
# never produce a "decisions" item -- a PR is not a decision), and the manual /win
# channel fills the gap, routed by win_store.classify_bucket. Each bucket carries a
# human heading for the rendered digest.
_BUCKET_HEADINGS: dict[str, str] = {
    "shipped": "Shipped",
    "advanced": "Advanced",
    "decisions": "Decisions",
    "maintenance": "Maintenance",
}


def _classify_match_bucket(match: dict[str, Any]) -> str:
    """Route ONE harvested+matched evidence item into a digest bucket.

    * ``shipped`` -- a PR that evidence-links a board task (code that shipped + closes a loop).
    * ``advanced`` -- a fuzzy ``needs-review`` match (work in flight, not yet confirmed done),
      and any non-email evidence with no confident task link (it moved something forward).
    * ``maintenance`` -- email/comms and other upkeep with no task match (the harvest's
      over-counted comms volume lands here rather than inflating "shipped").
    """
    decision = match.get("decision")
    source_type = match.get("source_type")
    if decision == "evidence-link" and source_type == "pr":
        return "shipped"
    if decision == "needs-review":
        return "advanced"
    if source_type == "email":
        return "maintenance"
    # An evidence-link from a non-PR source (rare) still shipped a closed loop; a
    # no-match non-email item advanced something without closing a task.
    return "shipped" if decision == "evidence-link" else "advanced"


def bucketise(matches: list[dict[str, Any]], wins: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Group harvested matches + manual wins into the four named digest buckets.

    Harvested items keep their ``/approve`` provenance (``matched_task_id`` /
    ``score``); manual wins carry only ``text``. Both share a ``line`` the renderer
    prints, plus a ``task_id``/``score`` when the item is an approvable match.
    """
    buckets: dict[str, list[dict[str, Any]]] = {name: [] for name in _BUCKET_HEADINGS}
    for m in matches:
        bucket = _classify_match_bucket(m)
        buckets[bucket].append({
            "line": m["title"],
            "decision": m["decision"],
            "matched_task_id": m.get("matched_task_id"),
            "score": m.get("score"),
        })
    for win in wins:
        bucket = win.get("bucket") if win.get("bucket") in buckets else win_store.DEFAULT_BUCKET
        buckets[bucket].append({"line": win.get("text", ""), "decision": "manual_win",
                                "matched_task_id": None, "score": None})
    return buckets


def digest_has_content(buckets: dict[str, list[dict[str, Any]]]) -> bool:
    """True iff ANY bucket holds at least one item (the empty-silent gate input)."""
    return any(buckets.values())


def build_draft(matches: list[dict[str, Any]], harvest_window_id: str,
                wins: list[dict[str, Any]] | None = None) -> str:
    """Assemble the four-bucket brag-doc digest (Shipped / Advanced / Decisions / Maintenance).

    Harvested evidence is classified into shipped/advanced/maintenance and manual
    ``/win`` items fold into their classified bucket (decisions/advanced as the
    harvest cannot). A shipped/advanced item that confidently closes a task still
    advertises the ``/approve <task_id>`` reply; manual wins and maintenance items
    are informational. An empty bucket is omitted -- the caller's Friday+content
    gate decides whether to send at all, so a non-empty digest never renders blank.
    """
    buckets = bucketise(matches, wins or [])
    lines = [f"Accomplishment Ledger — {harvest_window_id}", ""]
    for name, heading in _BUCKET_HEADINGS.items():
        items = buckets[name]
        if not items:
            continue
        lines.append(f"{heading}:")
        for item in items:
            lines.append(f"• {item['line']}")
            task_id = item["matched_task_id"]
            if task_id and item["decision"] in ("evidence-link", "needs-review"):
                verb = "mark done" if item["decision"] == "evidence-link" else "confirm"
                lines.append(f"  Reply /approve {task_id} to {verb}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# --- the proven draft push (DELIVERY-TARGET-PROOF) -------------------------


def _resolve_push_target() -> dict[str, Any]:
    """Prove the Productivity Done topic from env (Contract 2 / Decision #3).

    Reads ``TELEGRAM_CHAT_ID_PRODUCTIVITY`` + ``OPENCLAW_TOPIC_PRODUCTIVITY_DONE``
    (the phantom ``PRODUCTIVITY_GROUP_ID`` is deleted everywhere). Returns the
    ``prove_delivery_target`` result; ``ok: False`` means the env is unset/garbage
    and NO push may happen.
    """
    chat_id = os.getenv("TELEGRAM_CHAT_ID_PRODUCTIVITY")
    topic_id = os.getenv("OPENCLAW_TOPIC_PRODUCTIVITY_DONE")
    return delivery_target.prove_delivery_target(chat_id, topic_id, agent_id=ACTOR)


def prove_and_gate_push(
    *, pending_task_ids: list[str], evidence_count: int, harvest_window_id: str
) -> dict[str, Any]:
    """Prove + gate the draft push, returning the gated target and act_id.

    DELIVERY-TARGET-PROOF: the target is proven from env, bound through the gate
    (which re-proves it and rejects a Work-group/unknown/unset target), and the
    returned ``act_id`` is the SOLE token a later ``message()`` send may use. The
    push is logged as a ``ledger_draft_pushed`` ledger event with the proven
    target -- so a replay can verify the original destination was correct.

    Returns ``{"ok": True, "delivery_target", "act_id"}`` or
    ``{"ok": False, "reason"}`` (nothing is sent on a False result).
    """
    proof = _resolve_push_target()
    if not proof["ok"]:
        return {"ok": False, "reason": proof.get("reason", "env_missing"), "message": proof.get("message")}

    gated = autonomy_gate.gate(
        DRAFT_ACT_TYPE,
        delivery_target=proof["delivery_target"],
        unit="U5",
        agent_id=ACTOR,
        metadata={"harvest_window_id": harvest_window_id, "evidence_count": evidence_count},
    )
    if not gated["ok"]:
        return {"ok": False, "reason": gated.get("reason", "gate_blocked")}

    target = gated["delivery_target"]
    # The send seam: assert the (only) destination we are allowed to send to is
    # the gated one. A relay that aimed elsewhere would be blocked here.
    send_ok = autonomy_gate.assert_send_target(gated["act_id"], target)
    if not send_ok["ok"]:
        return {"ok": False, "reason": send_ok.get("reason", "target-mismatch")}

    append_event(
        new_event(
            "ledger_draft_pushed",
            actor=ACTOR,
            source=LEDGER_SOURCE,
            metadata={
                "harvest_window_id": harvest_window_id,
                "delivery_target": target,
                "pending_task_ids": pending_task_ids,
                "evidence_count": evidence_count,
                "act_id": gated["act_id"],
            },
        )
    )
    return {"ok": True, "delivery_target": target, "act_id": gated["act_id"]}


# --- the weekly + empty-silent auto-send gate ------------------------------


def is_digest_day(now: datetime | None = None) -> bool:
    """True on the weekly digest day (Friday, in the user's local zone).

    The auto-harvest moved from a daily push to a WEEKLY brag digest: a daily
    auto-queue is "another thing to service" and the harvest mis-weights what it
    surfaces, so the proactive send only fires on Friday. ``weekday() == 4`` is
    Friday. The reactive ``/ledger`` path ignores this -- it works any day.
    """
    ref = now or cos_config.local_now()
    return ref.weekday() == cos_config.ledger_digest_weekday()


def _auto_send_allowed(*, auto: bool, has_content: bool, now: datetime | None) -> bool:
    """Whether the PROACTIVE push may fire: forced/on-demand, OR Friday-with-content.

    The two-part gate the Oracle finding mandates: a daily auto-fire is suppressed
    unless it is the digest day AND there is something to report. An on-demand
    (``auto=False``) call bypasses the day gate entirely (it still requires content
    of its own to have a draft to push). An EMPTY digest sends NOTHING on either
    path -- no blank "nothing happened" message ever leaves.
    """
    if not has_content:
        return False
    if not auto:
        return True
    return is_digest_day(now)


# --- harvest orchestration -------------------------------------------------


def run_harvest(window: str, *, since_override: str | None, dry_run: bool, trigger: str,
                auto: bool = False, now: datetime | None = None) -> dict[str, Any]:
    """Harvest, dedup, match, fold in manual wins, and (unless dry-run) push a digest.

    ``auto`` marks the SCHEDULED (cron) path: it is gated to Friday-with-content, so
    a non-Friday auto-run -- or a Friday auto-run with an empty digest -- pushes
    NOTHING (no blank message). A reactive ``/ledger`` (``auto=False``) works any day
    and only needs content of its own. Returns the structured result + draft text +
    the proven ``delivery_target`` (or a blocked reason). NEVER raises -- the
    ``main`` wrapper classifies any unhandled exception into the friendly fallback.
    """
    harvest_window_id = harvest_state.window_id(window)
    state, expired = harvest_state.load_or_reset(harvest_window_id, window)

    since = _since_date(window, since_override)
    append_event(
        new_event(
            "ledger_harvest_started",
            actor=ACTOR,
            source=LEDGER_SOURCE,
            metadata={"harvest_window_id": harvest_window_id, "window": window, "since": since},
        )
    )

    evidence, sources_tried, source_error = harvest_all(since, trigger=trigger)
    # Dedup against this window's seen set BEFORE matching, so a heartbeat re-fire
    # never re-ingests the same PR/email.
    fresh = [item for item in evidence if not harvest_state.is_seen(state, item["evidence_hash"])]
    matches = match_evidence(fresh)
    # Manual /win captures count as digest content alongside harvested evidence --
    # they are the strategy/decision items the harvest misses. Selected by the SAME
    # seen-on-push dedup evidence uses (NOT the weekly ``since`` window): an unseen
    # win older than this week's Monday must still surface (a win captured after
    # Friday's push, or before a mid-week reactive push, would otherwise vanish when
    # ``since`` advances). A delivered win is excluded by being in the seen set.
    wins = win_store.read_unseen_wins()
    has_content = bool(matches) or bool(wins)

    pending_task_ids = sorted({
        m["matched_task_id"]
        for m in matches
        if m["decision"] in ("evidence-link", "needs-review") and m["matched_task_id"]
    })
    draft = build_draft(matches, harvest_window_id, wins)

    # EMPTY-SILENT + WEEKLY gate: the proactive push only fires forced/on-demand, or
    # on Friday-with-content. A suppressed auto-run consumes NOTHING (no evidence
    # marked seen) so the items still surface on the next allowed fire.
    if not _auto_send_allowed(auto=auto, has_content=has_content, now=now):
        reason = "no_new_evidence" if not has_content else "not_digest_day"
        return {
            "ok": True,
            "draft_pushed": False,
            "reason": reason,
            "harvest_window_id": harvest_window_id,
            "expired": expired,
            # A source error means "no content" is unproven -- carry the signal so the
            # cron path records a FAILURE rather than false-greening an empty digest
            # that may simply have failed to harvest.
            "source_error": source_error,
            # Silent on the auto path: no message is surfaced when nothing should send.
            "message": None if auto else f"Nothing new to report for {harvest_window_id}.",
        }

    push: dict[str, Any] = {"ok": False, "reason": "dry_run"}
    if not dry_run:
        # Kind-aware duplicate-push guard. The SCHEDULED Friday digest (``auto``) and
        # a reactive ``/ledger`` pull track their last-pushed window SEPARATELY, so a
        # mid-week reactive run never preempts the headline weekly Friday digest (and
        # a Friday digest never blocks a later reactive pull). Back-compat: a pre-kind
        # state recorded a single ``draft_pushed`` flag -- treat that as a REACTIVE
        # push for its window (bias toward letting the Friday auto digest still fire),
        # so the upgrade never silently suppresses a digest.
        kind = "auto" if auto else "reactive"
        pushed_key = f"{kind}_pushed_window"
        legacy_reactive = (kind == "reactive" and bool(state.get("draft_pushed"))
                           and state.get("harvest_window_id") == harvest_window_id)
        if state.get(pushed_key) == harvest_window_id or legacy_reactive:
            # Duplicate-push guard: a draft of THIS kind is already out for this window.
            return {
                "ok": True,
                "draft_pushed": False,
                "reason": "already_pushed",
                "harvest_window_id": harvest_window_id,
                "delivery_target": state.get("delivery_target"),
                "expired": expired,
                "source_error": source_error,
                "message": "Ledger draft already sent for this window.",
            }
        push = prove_and_gate_push(
            pending_task_ids=pending_task_ids,
            evidence_count=len(fresh),
            harvest_window_id=harvest_window_id,
        )
        # Evidence is consumed (marked seen) ONLY once the push target is PROVEN
        # and the draft is logged -- never on a BLOCKED push (env unset /
        # Work-group / gate rejection), where the dedup set is left untouched so
        # the next fire re-attempts delivery once the target is fixed. (The final
        # Telegram send is performed by the agent relay; if that relay step fails
        # the draft can be re-emitted from the logged ledger_draft_pushed event --
        # proof success, not Telegram receipt, is what marks evidence consumed.)
        if push["ok"]:
            state[pushed_key] = harvest_window_id
            state["draft_pushed_at"] = datetime.now(timezone.utc).isoformat()
            state["delivery_target"] = push["delivery_target"]
            state["pending_task_ids"] = pending_task_ids
            state["pending_matches"] = _pending_match_index(matches)
            harvest_state.mark_seen(state, [item["evidence_hash"] for item in fresh])
            harvest_state.save_state(state, window)
            # Consume the wins on the SAME success condition as the evidence: a win
            # is marked seen ONLY when it was included in a digest that actually
            # PUSHED, so a win captured after this push stays unseen for the next
            # one, and a delivered win never repeats. (A suppressed/blocked/no-push
            # run never reaches here, so it consumes no wins -- never silently lost.)
            win_store.mark_wins_seen([win["id"] for win in wins])

    return {
        "ok": True,
        "draft_pushed": bool(push["ok"]),
        "harvest_window_id": harvest_window_id,
        "since": since,
        "sources_tried": sources_tried,
        "evidence_count": len(fresh),
        "win_count": len(wins),
        "matches": matches,
        "pending_task_ids": pending_task_ids,
        "draft": draft,
        "delivery_target": push.get("delivery_target") if push["ok"] else None,
        "push_blocked_reason": None if push["ok"] else push.get("reason"),
        "source_error": source_error,
        "expired": expired,
    }


# --- /approve (REVERSIBILITY + TOPIC GUARD) --------------------------------


def _evidence_link_event(task_id: str, match: dict[str, Any] | None) -> dict[str, Any]:
    """Build the ``evidence_link`` event carried alongside the completion."""
    return new_event(
        "evidence_link",
        task_id=task_id,
        actor=ACTOR,
        source=LEDGER_SOURCE,
        evidence={
            "source_type": (match or {}).get("source_type"),
            "source_url": (match or {}).get("url"),
            "match_score": (match or {}).get("score"),
            "match_type": (match or {}).get("match_type"),
        },
    )


def _find_pending_window(task_id: str) -> tuple[dict[str, Any], str] | None:
    """Locate which window's state holds ``task_id`` as pending.

    Either harvest window (weekly ``/ledger`` or 24h ``/done``) can produce a
    pending item, and they live in separate state files. Checking both keeps
    ``/approve`` working regardless of which harvest surfaced the task -- the
    weekly loop is checked first (the canonical persistent loop).
    """
    for window in (harvest_state.WINDOW_WEEK, harvest_state.WINDOW_24H):
        state = harvest_state.load_state(window)
        if state and task_id in (state.get("pending_task_ids") or []):
            return state, window
    return None


def approve(task_id: str, *, inbound_topic_id: str | None, match: dict[str, Any] | None = None) -> dict[str, Any]:
    """Approve one matched task: topic-guard, snapshot, then reversible complete.

    Guards, in order:

    * **TOPIC GUARD.** A reactive ``/approve`` proves its origin topic, but origin
      != correctness. Reject when ``inbound_topic_id`` != the Productivity Done
      topic (``OPENCLAW_TOPIC_PRODUCTIVITY_DONE``).
    * **STALE/UNKNOWN.** The task id must be pending in one of the live harvest
      windows; otherwise it is not part of any live draft.

    On success a ``pre_action_snapshot`` is appended BEFORE the board write. If
    ``complete_by_id`` fails, a ``pre_action_snapshot_cancelled`` compensating
    event is appended and ``approved_task_ids`` is left untouched (REVERSIBILITY).
    """
    expected_topic = os.getenv("OPENCLAW_TOPIC_PRODUCTIVITY_DONE")
    if expected_topic is None or inbound_topic_id is None or str(inbound_topic_id) != str(expected_topic):
        return {
            "ok": False,
            "reason": "wrong-topic",
            "message": "/approve is only honoured in the Done topic; origin proof is not correctness proof.",
        }

    found = _find_pending_window(task_id)
    if found is None:
        return {
            "ok": False,
            "reason": "stale-approval",
            "message": "That task id is not in the current ledger draft. Run /ledger to generate a fresh one.",
        }
    state, window = found
    # The reactive /approve carries only the task id; rebuild the evidence-link
    # provenance from the match stored at push time when the caller didn't supply
    # it, so the ledger evidence_link is not recorded with null source/url/score.
    if match is None:
        match = (state.get("pending_matches") or {}).get(task_id)

    snapshot_event = new_event(
        "pre_action_snapshot",
        task_id=task_id,
        actor=ACTOR,
        source=LEDGER_SOURCE,
        metadata={"about": "ledger-approve-complete", "harvest_window_id": state.get("harvest_window_id")},
    )
    append_event(snapshot_event, path=ledger_path())

    result = complete_by_id(
        task_id,
        source=LEDGER_SOURCE,
        extra_events_factory=lambda _e: [_evidence_link_event(task_id, match)],
    )
    if not result.get("ok"):
        # Compensating event so audit replay stays consistent (the snapshot we
        # wrote above did NOT lead to a board mutation). approved_task_ids stays
        # untouched -- the open loop remains open, nothing is silently lost.
        append_event(
            new_event(
                "pre_action_snapshot_cancelled",
                task_id=task_id,
                actor=ACTOR,
                source=LEDGER_SOURCE,
                reason="complete_by_id-failed",
                metadata={"cancels_event_id": snapshot_event["event_id"], "error": result.get("error")},
            ),
            path=ledger_path(),
        )
        error = result.get("error") or {}
        message = (
            "Task is already marked done or was not found on the active board."
            if error.get("code") == "canonical-id-resolution-failed"
            else "Could not mark the task done; the board was left unchanged. Try again."
        )
        return {"ok": False, "reason": "complete-failed", "error": error, "message": message}

    pending = [tid for tid in (state.get("pending_task_ids") or []) if tid != task_id]
    approved = list(state.get("approved_task_ids") or [])
    if task_id not in approved:
        approved.append(task_id)
    state["pending_task_ids"] = pending
    state["approved_task_ids"] = approved
    harvest_state.save_state(state, window)

    append_event(
        new_event(
            "ledger_approved",
            task_id=task_id,
            actor=ACTOR,
            source=LEDGER_SOURCE,
            metadata={"approved_by": ACTOR, "harvest_window_id": state.get("harvest_window_id"), "task_ids": [task_id]},
        ),
        path=ledger_path(),
    )
    return {"ok": True, "task_id": task_id, "title": result.get("title")}


# --- /win (frictionless capture) -------------------------------------------


def capture_win(text: str) -> dict[str, Any]:
    """Append one manual win durably and return a structured confirmation.

    FRICTIONLESS: no board cap, no validation gate, no matching -- a real
    accomplishment is never blocked. Persists through ``win_store`` (a flocked
    append-only jsonl that survives a crash) and surfaces in the next digest's
    classified bucket. An empty/whitespace text is the only refusal (there is no
    win to record).
    """
    cleaned = (text or "").strip()
    if not cleaned:
        return {"ok": False, "reason": "empty", "message": "Tell me what you won, e.g. /win shipped the pricing deck."}
    record = win_store.append_win(cleaned)
    return {
        "ok": True,
        "bucket": record["bucket"],
        "text": record["text"],
        "message": f"🏆 Logged to {record['bucket']}: {record['text']}",
    }


# --- R1 health (cron path only) --------------------------------------------


def _ledger_failure_class(result: dict[str, Any]) -> str | None:
    """Classify the SCHEDULED harvest result into a health failure class, or ``None``.

    The harvest returns ``ok:True`` on EVERY path, so ``ok`` alone is useless as a
    health signal. A REAL failure is one of:

    * ``source_error`` -- a gh/gog subprocess errored (couldn't harvest), so an
      "empty" digest is unproven (it may simply have failed to fetch).
    * ``push_blocked_reason`` -- the digest had content but the push was BLOCKED
      (env unset / unprovable target / gate rejection), so nothing was delivered.
    * an explicit ``ok:False`` (a synthetic/legacy degraded result).

    A legitimately-empty week (sources ran clean, nothing to report, no blocked
    push) is NOT a failure -- a quiet week is healthy. Source-error outranks a
    blocked push so the most upstream cause is the recorded class.
    """
    if result.get("source_error"):
        return "harvest_source_error"
    if result.get("push_blocked_reason"):
        return "push_blocked"
    if not result.get("ok"):
        return str(result.get("reason") or "harvest_failed")
    return None


def _record_ledger_health(result: dict[str, Any]) -> None:
    """Best-effort: record the SCHEDULED harvest's outcome to the health substrate.

    Mirrors ``nag_check._record_nag_health`` -- ``harvest_ledger`` runs under the
    shell ``run_with_envelope`` (not ``error_envelope.run_main``) and catches its own
    crash to exit 0, so without recording here a broken weekly harvest would
    false-green until STALE. Called ONLY from the cron (``--auto``) path, never from
    a reactive ``/ledger`` run, so the two are not conflated. A real FAILURE (a
    source subprocess error, or a blocked push that delivered nothing) records a
    health failure; a legitimately-empty week records success (a quiet week is
    healthy). Wrapped so a broken/absent ``cos_health`` can never change the harvest
    outcome.
    """
    try:
        import cos_health  # noqa: PLC0415 -- lazy + wrapped: health is best-effort

        failure_class = _ledger_failure_class(result)
        if failure_class is None:
            cos_health.record_success(COMPONENT)
        else:
            cos_health.record_failure(
                COMPONENT,
                error_class=failure_class,
                trigger="cron:ledger_harvest",
            )
    except Exception:  # noqa: BLE001 -- health recording is best-effort, never fatal
        pass


# --- CLI -------------------------------------------------------------------


def _emit(payload: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
        return
    if payload.get("draft"):
        print(payload["draft"])
    elif payload.get("message"):
        print(payload["message"])
    if payload.get("expired"):
        print("\nExpired (never approved last window): " + ", ".join(payload["expired"]))


def _run_harvest_cli(args: argparse.Namespace) -> int:
    # ``--auto`` is the SCHEDULED (cron) fire: it gates the push to Friday-with-content
    # AND records ritual health. A reactive ``/ledger`` omits it, so it works any day
    # and records NO health (no cron-vs-reactive conflation). The trigger keys off the
    # same flag, not the window (a reactive /ledger also uses --window week).
    trigger = "cron:ledger_harvest" if args.auto else "user_command:/ledger"
    record_health = args.auto and not args.dry_run
    try:
        result = run_harvest(
            args.window,
            since_override=args.since,
            dry_run=args.dry_run,
            trigger=trigger,
            auto=args.auto,
        )
    except Exception as exc:  # noqa: BLE001 -- record health on a crash, then re-raise
        # A HARD crash mid-harvest is the worst silently-broken-cron case: the
        # _cli_entry no-raw-leak boundary catches it and exits 0, so without
        # recording here a crashing weekly harvest would false-green until STALE.
        # Record a FAILURE before re-raising -- but ONLY on the scheduled (--auto)
        # path, never a reactive /ledger (no cron-vs-reactive conflation). Mirrors
        # nag_check.main's except -> _record_nag_health(crashed=...).
        if record_health:
            _record_ledger_health({"ok": False, "reason": type(exc).__name__})
        raise
    if record_health:
        _record_ledger_health(result)
    _emit(result, as_json=args.json)
    return 0


def _run_win_cli(args: argparse.Namespace) -> int:
    # Frictionless capture: a handled refusal (empty text) is still exit 0 with a
    # structured message so the U1 envelope relays it, not a generic tool failure.
    result = capture_win(" ".join(args.text))
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
    else:
        print(result["message"])
    return 0


def _run_approve_cli(args: argparse.Namespace) -> int:
    # A guard rejection (wrong-topic / stale-approval / complete-failed) is a
    # HANDLED outcome with a user-facing structured message, not a tool failure --
    # exit 0 so the U1 envelope relays the message instead of masking it as a
    # generic "unavailable" notice. Only an unhandled crash (caught in _cli_entry)
    # is a real failure.
    result = approve(args.task_id, inbound_topic_id=args.topic_id)
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="U5 accomplishment ledger")
    sub = parser.add_subparsers(dest="cmd")

    harvest = sub.add_parser("harvest", help="Harvest + match + (optionally) push a draft")
    harvest.add_argument("--window", choices=[harvest_state.WINDOW_WEEK, harvest_state.WINDOW_24H],
                         default=harvest_state.WINDOW_WEEK)
    harvest.add_argument("--since", help="Override window start (YYYY-MM-DD)")
    harvest.add_argument("--dry-run", action="store_true", help="Harvest + match only; do not push or write state")
    harvest.add_argument("--json", action="store_true", help="Structured JSON output")
    harvest.add_argument("--auto", action="store_true",
                         help="Scheduled fire: gate the push to Friday-with-content and record ritual health")

    approve_p = sub.add_parser("approve", help="Approve one matched task (topic-guarded)")
    approve_p.add_argument("task_id")
    approve_p.add_argument("--topic-id", dest="topic_id", required=True,
                           help="The inbound Telegram topic id (proven origin)")

    win_p = sub.add_parser("win", help="Capture a manual win (frictionless; surfaces in the digest)")
    win_p.add_argument("text", nargs="+", help="The accomplishment text (free-form)")
    win_p.add_argument("--json", action="store_true", help="Structured JSON output")

    # A bare invocation (no subcommand) is the scheduled cron path: default to the
    # weekly harvest in --auto mode with structured JSON output, which the agent
    # then narrates. The reactive /ledger passes ``harvest`` explicitly (no --auto).
    parser.set_defaults(cmd="harvest", window=harvest_state.WINDOW_WEEK,
                        since=None, dry_run=False, json=True, auto=True)

    args = parser.parse_args(argv)
    if args.cmd == "approve":
        return _run_approve_cli(args)
    if args.cmd == "win":
        return _run_win_cli(args)
    return _run_harvest_cli(args)


def _cli_entry() -> int:
    """Top-level entry with the no-raw-leak envelope around an unhandled crash."""
    try:
        return main()
    except Exception as exc:  # noqa: BLE001 -- last-resort no-raw-leak boundary
        friendly = error_envelope.handle_fatal(COMPONENT, exc, trigger="cron:/ledger")
        print(json.dumps({"ok": False, "draft_pushed": False, "message": friendly}, indent=2))
        return 0


if __name__ == "__main__":
    raise SystemExit(_cli_entry())
