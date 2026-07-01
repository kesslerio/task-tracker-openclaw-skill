#!/usr/bin/env python3
"""Two-lane chat completion capture.

Lane A auto-completes only a gateway-verified command envelope.
Lane B treats all free-form chat as untrusted evidence for candidates or misses.
"""

from __future__ import annotations

import os
import re
from datetime import timedelta
from typing import Any

from capture_envelope import (
    SEEN_EVENT_TYPE,
    current_time,
    envelope_channel,
    envelope_message_id,
    message_ids_from_events,
    parse_timestamp,
    verify_envelope,
)
from completion_candidates import candidate_id_for, project_candidates
from evidence_matching import (
    FUZZY_REVIEW_THRESHOLD,
    build_task_catalog,
    extract_inline_identifiers,
    match_evidence_all,
    normalize_title,
    resolve_for_auto,
    safe_load_task_records,
)
from task_ledger import append_event, ledger_path, new_event, read_events
from task_transitions import complete_by_id
from utils import get_tasks_file

AUTOWRITE_ENV = "TASK_TRACKER_CAPTURE_AUTOWRITE_ENABLED"
FUZZY_MATCH_LIMIT = 5
MAX_TEXT_CHARS = 4096
MAX_STORED_PHRASE_CHARS = 512
MISS_DEDUPE_WINDOW = timedelta(hours=1)
TRUE_VALUES = {"1", "true", "yes", "on", "enabled"}

# Candidate denoising/ranking heuristics only; never use them as trust gates or
# authorization to write.
NEGATED_OR_HEDGED_RE = re.compile(
    r"\b(?:didn['’]?t|did\s+not|not\s+done|not\s+finished|not\s+complete(?:d)?|"
    r"not\s+yet|haven['’]?t|have\s+not|almost|still|will\s+finish|going\s+to|"
    r"plan\s+to|hoping\s+to|unable\s+to|can['’]?t|cannot|won['’]?t)\b",
    re.IGNORECASE,
)
QUOTE_OR_FORWARD_RE = re.compile(
    r"^\s*(?:>+|[\"'“”]|forwarded(?:\s+from)?|fwd|quote|quoted)\b|"
    r"^\s*[<]?[\w .@-]{1,40}[>]?\s*:(?!:)|"
    r"\b(?:forwarded(?:\s+from)?|fwd|quote|quoted)\b|\b(?:wrote|said):|»",
    re.IGNORECASE,
)
LEADING_EVIDENCE_PREFIX_RE = re.compile(
    r"^\s*(?:(?:i|we)\s+)?(?:just\s+|finally\s+|also\s+)?"
    r"(?:done(?:\s+with)?|finished|finish|wrapped\s+up|shipped|completed|"
    r"complete|did|closed\s+out|close\s+out|closed|sent|merged|resolved)\b\s*",
    re.IGNORECASE,
)
RECURRING_MARKER_RE = re.compile(r"\brecur\s*::", re.IGNORECASE)


def autowrite_enabled() -> bool:
    raw = os.getenv(AUTOWRITE_ENV)
    return bool(raw and raw.strip().casefold() in TRUE_VALUES)


def _capture_ledger_path(personal: bool = False):
    tasks_file, _fmt = get_tasks_file(personal)
    return ledger_path(tasks_file)


def _stored_phrase(text: str) -> str:
    phrase = " ".join((text or "").split()).strip()
    if len(phrase) > MAX_STORED_PHRASE_CHARS:
        phrase = phrase[:MAX_STORED_PHRASE_CHARS].rstrip()
    return phrase


def _collapse_statement(text: str) -> str:
    return " ".join(line.strip() for line in text.splitlines() if line.strip()).strip()


def _strip_transport_markers(text: str) -> str:
    lines: list[str] = []
    for line in text.splitlines():
        stripped = re.sub(r"^\s*>+\s?", "", line).strip()
        stripped = re.sub(r"^\s*(?:forwarded(?:\s+from)?|fwd|quote|quoted)\b[:\s-]*", "", stripped, flags=re.I)
        stripped = stripped.strip("\"'“” ")
        if stripped:
            lines.append(stripped)
    return " ".join(lines).strip()


def _phrase_for_matching(text: str) -> str:
    bounded = (text or "")[:MAX_TEXT_CHARS]
    cleaned = _collapse_statement(_strip_transport_markers(bounded))
    cleaned = re.sub(r"^\s*scratch\s+that[,.;:-]?\s*", "", cleaned, flags=re.I)
    cleaned = re.sub(r"^\s*\[[^\]\n]{1,40}\]\s+", "", cleaned, flags=re.I)
    cleaned = re.sub(
        r"^\s*(?:per|via|from|according\s+to)\s+[A-Za-z][\w.@/-]*(?:\s+[A-Za-z][\w.@/-]*){0,3}\s*[,:\-]\s*",
        "",
        cleaned,
        flags=re.I,
    )
    cleaned = re.sub(
        r"^\s*(?!(?:i|we|you)\b)[A-Za-z][\w.@-]*(?:\s+[A-Za-z][\w.@-]*){0,3}\s+"
        r"(?:said|told\s+me|wrote)\b\s*",
        "",
        cleaned,
        flags=re.I,
    )
    cleaned = re.sub(r"^\s*[<]?[\w .@-]{1,40}[>]?\s*:(?!:)\s*", "", cleaned, flags=re.I)
    cleaned = LEADING_EVIDENCE_PREFIX_RE.sub("", cleaned, count=1)
    return _stored_phrase(cleaned.strip(" \t\r\n-:;,.!✅"))


def _line_for_phrase(phrase: str, raw_text: str) -> dict[str, Any]:
    identifiers = extract_inline_identifiers(raw_text)
    identifiers_from_phrase = extract_inline_identifiers(phrase)
    exact_identifiers = identifiers["exact"] | identifiers_from_phrase["exact"]
    fallback_identifiers = identifiers["fallback"] | identifiers_from_phrase["fallback"]
    return {
        "raw_line": _stored_phrase(phrase),
        "title": phrase,
        "normalized_title": normalize_title(phrase),
        "exact_identifiers": exact_identifiers,
        "fallback_identifiers": fallback_identifiers,
    }


def _is_reviewable_match(match: dict[str, Any]) -> bool:
    match_types = set(match.get("match_types") or [match.get("match_type")])
    return bool(
        match_types & {"exact-id-or-link", "issue-number-fallback", "normalized-title"}
        or float(match.get("score") or 0.0) >= FUZZY_REVIEW_THRESHOLD
    )


def _best_reviewable(matches: list[dict[str, Any]]) -> dict[str, Any] | None:
    for match in matches:
        if _is_reviewable_match(match):
            return match
    return None


def _match_text(text: str, catalog: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    phrase = _phrase_for_matching(text)
    if not phrase:
        phrase = _stored_phrase(text)
    if not phrase:
        return "", []
    match_payload = match_evidence_all(
        _line_for_phrase(phrase, text),
        catalog,
        fuzzy_limit=FUZZY_MATCH_LIMIT,
    )
    return phrase, match_payload["matches"]


def _record_is_recurring(record: Any) -> bool:
    return bool(getattr(record, "recur", None) or RECURRING_MARKER_RE.search(getattr(record, "raw_line", "") or ""))


def _candidate_payload(
    *,
    phrase: str,
    source: dict[str, Any],
    matches: list[dict[str, Any]],
    decision_reason: str,
) -> dict[str, Any]:
    best = _best_reviewable(matches)
    best_task = (best or {}).get("canonical_task") or {}
    safe_phrase = _stored_phrase(best_task.get("title") or phrase)
    match_metadata: dict[str, Any] = {
        "matched_task_id": None,
        "score": 0.0,
        "decision": "needs-review",
        "match_type": "none",
        "decision_reason": decision_reason,
    }
    suggested_match = None
    if best:
        suggested_match = best.get("canonical_task")
        match_metadata.update(
            {
                "matched_task_id": best.get("matched_task_id"),
                "score": best.get("score"),
                "match_type": best.get("match_type"),
                "match_types": best.get("match_types") or [best.get("match_type")],
            }
        )

    candidate_id = candidate_id_for(source, safe_phrase)
    candidate = {
        "candidate_id": candidate_id,
        "status": "new",
        "source": source,
        "raw_summary": safe_phrase,
        "summary": safe_phrase,
        "normalized_summary": normalize_title(safe_phrase),
        "suggested_match": suggested_match,
        "matches": matches,
        "match_metadata": match_metadata,
        "matched_task_id": match_metadata.get("matched_task_id"),
        "review_required": True,
    }
    if match_metadata.get("match_type") == "exact-id-or-link" and match_metadata.get("matched_task_id"):
        candidate["confirmable_task_id"] = match_metadata["matched_task_id"]
        # Skips match review; still requires user confirmation; never auto-writes.
        candidate["review_required"] = False
    return candidate


def _record_candidate(
    *,
    phrase: str,
    source: dict[str, Any],
    matches: list[dict[str, Any]],
    decision_reason: str,
    personal: bool,
) -> dict[str, Any]:
    candidate = _candidate_payload(
        phrase=phrase,
        source=source,
        matches=matches,
        decision_reason=decision_reason,
    )
    existing = {
        item["candidate_id"]: item
        for item in project_candidates(include_terminal=True, personal=personal)
    }
    candidate_id = candidate["candidate_id"]
    if candidate_id in existing:
        return {"candidate": existing[candidate_id], "created": False}

    append_event(
        new_event(
            "candidate_seen",
            task_id=candidate_id,
            source="chat_capture",
            metadata={"candidate": candidate},
        ),
        path=_capture_ledger_path(personal),
    )
    return {"candidate": candidate, "created": True}


def _event_timestamp(event: dict[str, Any]):
    timestamp = str(event.get("timestamp") or "")
    parsed = parse_timestamp(timestamp)
    if parsed is None:
        raise ValueError("invalid event timestamp")
    return parsed


def _record_capture_miss(
    *,
    phrase: str,
    source: dict[str, Any],
    matches: list[dict[str, Any]],
    personal: bool,
    reason: str,
) -> dict[str, Any]:
    safe_phrase = _stored_phrase(phrase)
    normalized_phrase = normalize_title(safe_phrase)
    now = current_time()
    for event in reversed(read_events(_capture_ledger_path(personal), strict=True)):
        if event.get("event_type") != "capture_miss":
            continue
        metadata = event.get("metadata") or {}
        if metadata.get("normalized_phrase") != normalized_phrase:
            continue
        try:
            event_time = _event_timestamp(event)
        except ValueError:
            continue
        if now - event_time <= MISS_DEDUPE_WINDOW:
            return event

    event = new_event(
        "capture_miss",
        source="chat_capture",
        metadata={
            "source": source,
            "phrase": safe_phrase,
            "normalized_phrase": normalized_phrase,
            "matches": matches,
            "reason": reason,
        },
    )
    append_event(event, path=_capture_ledger_path(personal))
    return event


def _source_pointer(
    *,
    source: str,
    sender: str | None,
    channel: str | None,
    message_id: str | None,
    timestamp: str | None,
) -> dict[str, Any]:
    pointer: dict[str, Any] = {
        "type": "chat",
        "channel": channel or source,
        # Chat candidates keep line_number for parity with file-source candidate schemas.
        "line_number": 1,
        "timestamp": timestamp or current_time().isoformat(),
    }
    if sender:
        pointer["sender"] = sender
    if message_id:
        pointer["message_id"] = message_id
    return pointer


def _rollup_action(actions: list[dict[str, Any]]) -> str:
    if not actions:
        return "miss"
    if len(actions) == 1:
        return str(actions[0]["action"])
    if all(action["action"] == "auto" for action in actions):
        return "auto"
    if all(action["action"] == "miss" for action in actions):
        return "miss"
    return "candidate"


def _quality_reason(text: str) -> str | None:
    if NEGATED_OR_HEDGED_RE.search(text or ""):
        return "negated-or-hedged"
    if QUOTE_OR_FORWARD_RE.search(text or ""):
        return "quoted-or-forwarded"
    return None


def _candidate_or_miss_action(
    *,
    text: str,
    catalog: list[dict[str, Any]],
    source_pointer: dict[str, Any],
    decision_reason: str,
    personal: bool,
    suppress_negated_candidate: bool = True,
) -> dict[str, Any]:
    phrase, matches = _match_text(text, catalog)
    reason = _quality_reason(text) or decision_reason
    best = _best_reviewable(matches)

    if not phrase:
        phrase = "unparsed chat capture"
    if best is None or (suppress_negated_candidate and reason in ("negated-or-hedged", "quoted-or-forwarded")):
        miss = _record_capture_miss(
            phrase=phrase,
            source=source_pointer,
            matches=matches,
            personal=personal,
            reason=reason if best is not None else "no-match",
        )
        return {
            "action": "miss",
            "phrase": phrase,
            "event_id": miss["event_id"],
            "matches": matches,
            "decision_reason": reason if best is not None else "no-match",
        }

    recorded = _record_candidate(
        phrase=phrase,
        source=source_pointer,
        matches=matches,
        decision_reason=reason,
        personal=personal,
    )
    candidate = recorded["candidate"]
    return {
        "action": "candidate",
        "phrase": phrase,
        "task_id": candidate.get("matched_task_id"),
        "candidate_id": candidate["candidate_id"],
        "candidate_created": recorded["created"],
        "candidate": candidate,
        "matches": matches,
        "decision_reason": reason,
    }


def _envelope_candidate_text(envelope: dict[str, Any] | None, fallback_text: str | None) -> str:
    task_id = (envelope or {}).get("task_id")
    if isinstance(task_id, str) and task_id.strip():
        stripped = task_id.strip()
        if re.fullmatch(r"[A-Za-z0-9._:-]+", stripped):
            return f"task_id::{stripped}"
        return stripped
    return fallback_text or ""


def _envelope_seen_event(
    envelope: dict[str, Any],
    completion_event: dict[str, Any] | None = None,
    *,
    outcome: str | None = None,
) -> dict[str, Any]:
    metadata = {
        "message_id": envelope_message_id(envelope),
        "sender": envelope.get("sender"),
        "channel": envelope_channel(envelope),
        "envelope_timestamp": envelope.get("timestamp"),
    }
    if completion_event is not None:
        metadata["completion_event_id"] = completion_event.get("event_id")
    if outcome is not None:
        metadata["outcome"] = outcome
    return new_event(
        SEEN_EVENT_TYPE,
        task_id=str(envelope.get("task_id") or "").strip(),
        source="chat_capture",
        metadata=metadata,
    )


def _merge_single_action(payload: dict[str, Any], actions: list[dict[str, Any]]) -> None:
    if len(actions) != 1:
        return
    for key, value in actions[0].items():
        payload[key] = value


def _handle_envelope(
    *,
    text: str | None,
    envelope: str | dict[str, Any],
    sender: str | None,
    source: str,
    channel: str | None,
    message_id: str | None,
    catalog: list[dict[str, Any]],
    write_enabled: bool,
    ledger_events: list[dict[str, Any]],
    personal: bool,
) -> tuple[Any, dict[str, Any]]:
    verification = verify_envelope(envelope, seen_message_ids=message_ids_from_events(ledger_events))
    verified = verification.ok
    parsed_envelope = verification.envelope
    source_pointer = _source_pointer(
        source=source,
        sender=(parsed_envelope or {}).get("sender") or sender,
        channel=(parsed_envelope or {}).get("channel") or channel,
        message_id=(parsed_envelope or {}).get("message_id") or message_id,
        timestamp=(parsed_envelope or {}).get("timestamp"),
    )

    fallback_reason: str | None = None
    if verified and write_enabled:
        task_id = str((parsed_envelope or {}).get("task_id") or "").strip()
        record = resolve_for_auto(task_id, catalog)
        if record is not None and not _record_is_recurring(record):
            task_id = str(getattr(record, "canonical_id", None) or task_id)
            completion = complete_by_id(
                task_id,
                personal=personal,
                source="chat_capture",
                extra_events_factory=lambda event: [_envelope_seen_event(parsed_envelope or {}, event)],
            )
            if completion.get("ok"):
                return verification, {
                    "action": "auto",
                    "task_id": completion.get("task_id"),
                    "completion_id": completion.get("completion_id"),
                    "envelope_message_id": (parsed_envelope or {}).get("message_id"),
                }
            fallback_reason = "auto-complete-failed"
        else:
            fallback_reason = "recurring-task" if record is not None else "auto-task-not-found"
    elif verified:
        fallback_reason = "autowrite-disabled"
    else:
        fallback_reason = verification.reason or "unverified-envelope"

    action = _candidate_or_miss_action(
        text=_envelope_candidate_text(parsed_envelope, text),
        catalog=catalog,
        source_pointer=source_pointer,
        decision_reason=fallback_reason,
        personal=personal,
        suppress_negated_candidate=False,
    )
    if verified:
        append_event(
            _envelope_seen_event(parsed_envelope or {}, outcome=fallback_reason),
            path=_capture_ledger_path(personal),
        )
    return verification, action


def capture_text(
    text: str | None = None,
    *,
    envelope: str | dict[str, Any] | None = None,
    sender: str | None = None,
    source: str = "chat",
    channel: str | None = None,
    message_id: str | None = None,
    personal: bool = False,
) -> dict[str, Any]:
    catalog = build_task_catalog(safe_load_task_records(personal))
    write_enabled = autowrite_enabled()
    ledger_events = read_events(_capture_ledger_path(personal), strict=True)
    verification = None
    actions: list[dict[str, Any]] = []

    if envelope is not None:
        verification, action = _handle_envelope(
            text=text,
            envelope=envelope,
            sender=sender,
            source=source,
            channel=channel,
            message_id=message_id,
            catalog=catalog,
            write_enabled=write_enabled,
            ledger_events=ledger_events,
            personal=personal,
        )
        actions.append(action)
    else:
        source_pointer = _source_pointer(
            source=source,
            sender=sender,
            channel=channel,
            message_id=message_id,
            timestamp=None,
        )
        actions.append(
            _candidate_or_miss_action(
                text=text or "",
                catalog=catalog,
                source_pointer=source_pointer,
                decision_reason="raw-chat",
                personal=personal,
            )
        )

    payload: dict[str, Any] = {
        "ok": True,
        "action": _rollup_action(actions),
        "autowrite_enabled": write_enabled,
        "envelope_verified": bool(verification and verification.ok),
        "envelope_reason": verification.reason if verification else None,
        "actions": actions,
    }
    _merge_single_action(payload, actions)
    return payload
