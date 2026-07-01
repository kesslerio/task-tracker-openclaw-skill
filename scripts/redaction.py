#!/usr/bin/env python3
"""H10 redaction layer: keep references, strip raw content (minimum-necessary).

The Oracle finding: proving the destination doesn't prove the CONTENT is
appropriate. This skill harvests work (PR titles, sent-mail subjects) and pushes
proactive messages (nags, the weekly brag digest), recording events to the
append-only ledger (``events.jsonl``). A future harvest source -- or a careless
event payload -- could carry a raw sensitive BODY (an email body, a meeting
transcript, customer/health specifics) into a durable log or a proactive message.

This module is the redact-by-default seam that keeps raw free-form content OUT of
both surfaces. It is:

* **Reference-aware, not a blind deep-strip.** The ledger's own audit trail is
  made of REFERENCES (subject/title, stable id, url, source_type, task_id,
  timestamps, status, scores, match metadata). Those are the product's signal --
  the digest must still show what shipped by title + link, and
  ``completion_candidates`` round-trips ``summary`` / ``raw_summary`` / ``raw_line``
  out of the ledger. So an allowlisted reference field ALWAYS passes through, even
  if long.
* **Deny-by-name for known content carriers.** A field named like a free-form body
  (``body``, ``snippet``, ``content``, ``description``, ``text`` blobs, a mail/message
  ``html``/``plain`` part, ...) is the classic leak vector. It is dropped to a
  redaction marker regardless of length -- so a future source that adds a ``body``
  cannot leak it into the ledger or a push.
* **Redact-by-default for the unknown.** An UNKNOWN string field longer than a sane
  cap is treated as sensitive and truncated -- an unknown large free-text field is
  presumed to be content, not a reference. Short unknown strings (ids, urls, short
  titles) pass through.

The redactor is PURE and TOTAL: it never mutates its input, never raises (a
malformed/None payload returns a safe value -- fail-open toward delivering a
redacted-but-valid event rather than crashing the append/push), and a missing
field is fine.
"""

from __future__ import annotations

from typing import Any

# A field whose NAME marks it as raw free-form content. Dropped to the marker no
# matter how short -- a one-line "body" is still a body, and the point is that a
# future source adding any of these cannot leak it. Names are matched
# case-insensitively. ``text`` is included so a hypothetical email/message text
# BLOB is stripped; legitimate SHORT ``text`` references (a manual ``/win`` line, a
# nag's short text) survive because they are passed as the message string through
# ``redact_message`` / are kept short, and because the ledger events themselves
# carry the win under ``text`` only via the win store (not the ledger). A ``text``
# field is neither denied nor exempt: it falls through to the unknown-field length
# cap, so a SHORT win line survives while a LONG blob routed through it is truncated.
_CONTENT_FIELD_NAMES: frozenset[str] = frozenset({
    "body",
    "snippet",
    "content",
    "description",
    "html",
    "html_body",
    "plain",
    "plain_body",
    "text_body",
    "message_body",
    "email_body",
    "transcript",
    "notes",
    "note",
    "comment",
    "preview",
    "excerpt",
    "quote",
    "raw_content",
})

# Reference fields that are the audit trail's SIGNAL and must pass through intact
# even when long: a clean PR/email title, a board task line, a url. Stripping
# these would make the digest blank and break the candidate-inbox round-trip, so
# they are exempt from the unknown-field length cap. Matched case-insensitively.
_REFERENCE_FIELD_NAMES: frozenset[str] = frozenset({
    "title",
    "match_title",
    "subject",
    "summary",
    "raw_summary",
    "raw_line",
    "post_raw_line",
    "daily_note_path",
    "daily_note_line",
    "daily_note_context_line",
    "normalized_summary",
    "normalized_title",
    "parsed_title",
    "line",
    "url",
    "source_url",
    "message",  # a friendly, already-sanitised user-facing line (never raw)
    "reason",  # a short classification reason, not free-form content
})

# The sane cap for an unknown free-text field. A string
# longer than this is presumed content and truncated; shorter strings (ids, urls,
# short titles, a one-line win) pass through. A title/subject is rarely longer than
# this; an email body / transcript is far longer, so the cap separates the two
# without a content-aware heuristic.
_MAX_FREETEXT_LEN = 512

# The cap on a fully-ASSEMBLED proactive message body. A digest is built from many
# reference lines (one per bucket item) so it is legitimately longer than a single
# field; this bound is generous enough never to clip a real digest, but still
# stops an accidentally-huge body (a raw transcript spliced into the text) from
# being blasted to Telegram. It is a backstop -- the real defence is stripping the
# body at the event/field level (``redact_event``) before it ever reaches the text.
_MAX_MESSAGE_LEN = 8192

# What a stripped/truncated field is replaced with. The marker is stable and
# obviously-not-content so a reader (and a test) can see the field was redacted
# rather than empty.
REDACTED_MARKER = "[redacted]"
_TRUNCATED_SUFFIX = " …[redacted]"

# Guard against a pathological/cyclic payload: never recurse deeper than this. A
# real event is shallow (event -> metadata -> a small dict); anything deeper is
# treated as content at the limit and replaced with the marker rather than risking
# unbounded recursion (totality > completeness).
_MAX_DEPTH = 12


def _is_content_field(name: str) -> bool:
    return name.casefold() in _CONTENT_FIELD_NAMES


def _is_reference_field(name: str) -> bool:
    return name.casefold() in _REFERENCE_FIELD_NAMES


def _redact_string(value: str, *, exempt_length: bool) -> str:
    """Pass a reference-length string through; truncate an over-cap free-text one.

    ``exempt_length`` is True only for an allowlisted reference field (a title/url
    that may legitimately be long). Every other string is capped at
    ``_MAX_FREETEXT_LEN`` -- the redact-by-default rule for unknown large text.
    """
    if exempt_length or len(value) <= _MAX_FREETEXT_LEN:
        return value
    return value[:_MAX_FREETEXT_LEN] + _TRUNCATED_SUFFIX


def _redact_value(name: str, value: Any, depth: int) -> Any:
    """Redact one (field-name, value) pair. Pure; never raises.

    A content-named field is dropped to the marker regardless of type/length. A
    reference-named field passes through (strings exempt from the length cap;
    nested structure still walked so a content field nested inside a reference dict
    is not a bypass). Everything else is redact-by-default: strings are
    length-capped, dicts/lists are walked.
    """
    if depth > _MAX_DEPTH:
        return REDACTED_MARKER
    if _is_content_field(name):
        return REDACTED_MARKER
    if isinstance(value, dict):
        return _redact_mapping(value, depth + 1)
    if isinstance(value, (list, tuple)):
        return [_redact_value(name, item, depth + 1) for item in value]
    if isinstance(value, str):
        return _redact_string(value, exempt_length=_is_reference_field(name))
    # Non-string scalars (int/float/bool/None) carry no free-form body.
    return value


def _redact_mapping(payload: dict[str, Any], depth: int) -> dict[str, Any]:
    if depth > _MAX_DEPTH:
        return {}
    redacted: dict[str, Any] = {}
    for key, value in payload.items():
        name = str(key)
        redacted[name] = _redact_value(name, value, depth)
    return redacted


def redact_payload(payload: Any) -> Any:
    """Redact an arbitrary event/evidence/metadata payload (pure, total).

    Returns a NEW structure with raw content stripped/truncated and references
    kept. A non-dict/list top-level value is returned through the same per-value
    rules (with no field name, so it is treated as unknown free-text). Never
    mutates the input and never raises -- a malformed payload (None, a weird type,
    a cyclic-looking nest) degrades to a safe redacted value rather than crashing
    the append/push that called it.
    """
    try:
        if isinstance(payload, dict):
            return _redact_mapping(payload, 0)
        if isinstance(payload, (list, tuple)):
            return [_redact_value("", item, 0) for item in payload]
        if isinstance(payload, str):
            return _redact_string(payload, exempt_length=False)
        return payload
    except Exception:  # noqa: BLE001 -- redaction is total; fail toward a safe marker
        return REDACTED_MARKER


def redact_event(event: dict[str, Any]) -> dict[str, Any]:
    """Redact a ledger event before it is persisted (the append-only seam).

    The top-level event envelope is itself walked: ``evidence`` and ``metadata``
    are the dicts that can carry a body/snippet/content field (today they hold only
    references, but a future source could add one), so they are redacted in place.
    The fixed envelope fields (``event_id`` / ``event_type`` / ``timestamp`` /
    ``actor`` / ``source`` / ``task_id`` / ``previous_state`` / ``next_state``) are
    references; ``reason`` is a short classification string -- all pass through the
    reference rules. Non-dict input is returned untouched-but-safe (the append path
    only ever passes a dict; this keeps the function total).
    """
    if not isinstance(event, dict):
        return event
    return _redact_mapping(event, 0)


def redact_message(text: Any) -> str:
    """Defensively cap a proactive message body before it is sent.

    Proactive text is ASSEMBLED from subjects/titles/ids (not raw bodies) -- that
    reference-only assembly is the real content guarantee. This is the belt-and-braces
    LENGTH backstop, wired at the proactive send seams: ``harvest_ledger.build_draft``
    (the brag digest, relayed by the agent) and ``proactive_delivery.authorised_send``
    (the gated-send choke point the proactive brief routes through). The nag path emits
    short reference-only task lines through the outbox and is not length-wrapped here.
    A non-string is coerced; the result is length-capped at the MESSAGE bound (generous
    enough never to clip a real multi-line digest) so an accidentally-huge value spliced
    into a line is truncated rather than blasted to Telegram.
    """
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    if len(text) <= _MAX_MESSAGE_LEN:
        return text
    return text[:_MAX_MESSAGE_LEN] + _TRUNCATED_SUFFIX
