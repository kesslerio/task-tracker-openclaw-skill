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
import delivery_target
import error_envelope
import harvest_state
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
) -> list[dict[str, Any]]:
    """Run one harvest subprocess and parse it; return ``[]`` on any failure.

    A missing/broken tool that has failed repeatedly trips the circuit breaker --
    the subprocess is skipped entirely so a daily cron does not loop on it. Any
    failure is logged through ``error_envelope`` (classified, never echoed) and an
    empty list is returned; this function NEVER raises.
    """
    component = f"{COMPONENT}:{source}"
    if error_envelope.breaker_open(component):
        return []
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            raise subprocess.CalledProcessError(
                result.returncode, cmd[0], output=result.stdout, stderr=result.stderr
            )
        return parse(json.loads(result.stdout))
    except _SUBPROCESS_FAILURES as exc:
        error_envelope.log_degraded(component, exc, trigger=trigger, check=source)
        return []


def _since_date(window: str, since_override: str | None, reference: date | None = None) -> str:
    """The harvest-window start date (YYYY-MM-DD)."""
    if since_override:
        return since_override
    ref = reference or date.today()
    if window == harvest_state.WINDOW_24H:
        return (ref - timedelta(days=1)).isoformat()
    # Weekly window: start of the current ISO week (Monday).
    return (ref - timedelta(days=ref.weekday())).isoformat()


def harvest_github(since: str, *, trigger: str) -> list[dict[str, Any]]:
    """Harvest merged PRs authored by the user since ``since`` (``gh``)."""

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


def harvest_gmail(since: str, *, trigger: str) -> list[dict[str, Any]]:
    """Harvest sent-mail subjects since ``since`` (``gog``)."""

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


def harvest_all(since: str, *, trigger: str) -> tuple[list[dict[str, Any]], int]:
    """Harvest every (non-deferred) source. Returns ``(evidence, sources_tried)``.

    Calendar harvest is DEFERRED to v0.3 (``STANDUP_CALENDARS`` not in container),
    so only GitHub + Gmail are tried in v0.2.
    """
    evidence = harvest_github(since, trigger=trigger) + harvest_gmail(since, trigger=trigger)
    return evidence, 2


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


def build_draft(matches: list[dict[str, Any]], harvest_window_id: str) -> str:
    """Assemble the brag-doc draft text (Shipped / For review / Logged).

    Only the ``/approve <task_id>`` command this unit implements is advertised.
    The wider ``/reject`` / ``/approve-all`` surface is out of U5's scope, so the
    draft never instructs the user to type a command that does nothing.
    """
    lines = [f"Accomplishment Ledger — {harvest_window_id}", ""]
    shipped = _bucket(matches, "evidence-link")
    review = _bucket(matches, "needs-review")
    logged = _bucket(matches, "no-match")

    if shipped:
        lines.append("Shipped:")
        for m in shipped:
            lines.append(f"• {m['title']} → closes {m['matched_task_id']}")
            lines.append(f"  Reply /approve {m['matched_task_id']} to mark done")
        lines.append("")
    if review:
        lines.append("For review (fuzzy match — confirm?):")
        for m in review:
            lines.append(
                f"• {m['title']} → possible match {m['matched_task_id']} (score {m['score']})"
            )
            lines.append(f"  Reply /approve {m['matched_task_id']} to confirm")
        lines.append("")
    if logged:
        lines.append("Logged (no task match):")
        for m in logged:
            lines.append(f"• {m['title']}")
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


# --- harvest orchestration -------------------------------------------------


def run_harvest(window: str, *, since_override: str | None, dry_run: bool, trigger: str) -> dict[str, Any]:
    """Harvest, dedup, match, and (unless dry-run) prove+gate the draft push.

    Returns a structured result with the draft text, match buckets, the proven
    ``delivery_target`` (or a blocked reason), and any ``expired`` task ids the
    weekly reset surfaced. NEVER raises -- the ``main`` wrapper additionally
    classifies any unhandled exception into the friendly fallback.
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

    evidence, sources_tried = harvest_all(since, trigger=trigger)
    # Dedup against this window's seen set BEFORE matching, so a heartbeat re-fire
    # never re-ingests the same PR/email.
    fresh = [item for item in evidence if not harvest_state.is_seen(state, item["evidence_hash"])]

    if not fresh:
        # No new evidence: either all sources failed/empty, or everything was
        # already seen. Either way no draft is pushed (guard: all-signals-empty).
        result = {
            "ok": True,
            "draft_pushed": False,
            "reason": "no_new_evidence",
            "harvest_window_id": harvest_window_id,
            "expired": expired,
            "message": f"Nothing new to report for {harvest_window_id}.",
        }
        return result

    matches = match_evidence(fresh)
    pending_task_ids = sorted({
        m["matched_task_id"]
        for m in matches
        if m["decision"] in ("evidence-link", "needs-review") and m["matched_task_id"]
    })
    draft = build_draft(matches, harvest_window_id)

    push: dict[str, Any] = {"ok": False, "reason": "dry_run"}
    if not dry_run:
        if state.get("draft_pushed") and state.get("harvest_window_id") == harvest_window_id:
            # Duplicate-push guard: a draft is already out for this window.
            return {
                "ok": True,
                "draft_pushed": False,
                "reason": "already_pushed",
                "harvest_window_id": harvest_window_id,
                "delivery_target": state.get("delivery_target"),
                "expired": expired,
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
            state["draft_pushed"] = True
            state["draft_pushed_at"] = datetime.now(timezone.utc).isoformat()
            state["delivery_target"] = push["delivery_target"]
            state["pending_task_ids"] = pending_task_ids
            state["pending_matches"] = _pending_match_index(matches)
            harvest_state.mark_seen(state, [item["evidence_hash"] for item in fresh])
            harvest_state.save_state(state, window)

    return {
        "ok": True,
        "draft_pushed": bool(push["ok"]),
        "harvest_window_id": harvest_window_id,
        "since": since,
        "sources_tried": sources_tried,
        "evidence_count": len(fresh),
        "matches": matches,
        "pending_task_ids": pending_task_ids,
        "draft": draft,
        "delivery_target": push.get("delivery_target") if push["ok"] else None,
        "push_blocked_reason": None if push["ok"] else push.get("reason"),
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
    trigger = f"{'cron' if args.window == harvest_state.WINDOW_WEEK else 'user_command'}:/ledger"
    result = run_harvest(
        args.window,
        since_override=args.since,
        dry_run=args.dry_run,
        trigger=trigger,
    )
    _emit(result, as_json=args.json)
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

    approve_p = sub.add_parser("approve", help="Approve one matched task (topic-guarded)")
    approve_p.add_argument("task_id")
    approve_p.add_argument("--topic-id", dest="topic_id", required=True,
                           help="The inbound Telegram topic id (proven origin)")

    # A bare invocation (no subcommand) is the daily cron path: default to the
    # weekly harvest with structured JSON output, which the agent then narrates.
    parser.set_defaults(cmd="harvest", window=harvest_state.WINDOW_WEEK,
                        since=None, dry_run=False, json=True)

    args = parser.parse_args(argv)
    if args.cmd == "approve":
        return _run_approve_cli(args)
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
