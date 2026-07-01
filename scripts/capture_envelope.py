#!/usr/bin/env python3
"""Verified gateway envelope support for chat capture auto-completion."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

SECRET_ENV = "TASK_TRACKER_CAPTURE_ENVELOPE_SECRET"
NOW_ENV = "TASK_TRACKER_CAPTURE_NOW"
FRESHNESS_SECONDS = 300
SEEN_EVENT_TYPE = "capture_envelope_seen"
ReplayMessageKey = tuple[str, str]


@dataclass(frozen=True)
class EnvelopeVerification:
    ok: bool
    envelope: dict[str, Any] | None = None
    reason: str | None = None


def canonical_payload(envelope: dict[str, Any]) -> bytes:
    unsigned = {key: value for key, value in envelope.items() if key != "sig"}
    return json.dumps(unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def signature_for(envelope: dict[str, Any], secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), canonical_payload(envelope), hashlib.sha256).hexdigest()


def sign_envelope(envelope: dict[str, Any], secret: str) -> dict[str, Any]:
    signed = dict(envelope)
    signed["sig"] = signature_for(signed, secret)
    return signed


def parse_envelope(raw: str | dict[str, Any]) -> dict[str, Any] | None:
    if isinstance(raw, dict):
        return dict(raw)
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def current_time() -> datetime:
    override = os.getenv(NOW_ENV)
    if override:
        parsed = parse_timestamp(override)
        if parsed is not None:
            return parsed
    return datetime.now(timezone.utc)


def envelope_message_id(envelope: dict[str, Any] | None) -> str | None:
    message_id = (envelope or {}).get("message_id")
    if isinstance(message_id, str) and message_id.strip():
        return message_id.strip()
    return None


def envelope_channel(envelope: dict[str, Any] | None) -> str:
    channel = (envelope or {}).get("channel")
    if isinstance(channel, str):
        return channel.strip()
    return ""


def envelope_replay_key(envelope: dict[str, Any] | None) -> ReplayMessageKey | None:
    message_id = envelope_message_id(envelope)
    if message_id is None:
        return None
    return (envelope_channel(envelope), message_id)


def message_ids_from_events(events: list[dict[str, Any]]) -> set[ReplayMessageKey]:
    seen: set[ReplayMessageKey] = set()
    for event in events:
        if event.get("event_type") != SEEN_EVENT_TYPE:
            continue
        metadata = event.get("metadata") or {}
        replay_key = envelope_replay_key(metadata)
        if replay_key is not None:
            seen.add(replay_key)
    return seen


def verify_envelope(
    raw: str | dict[str, Any],
    *,
    seen_message_ids: set[ReplayMessageKey] | None = None,
    now: datetime | None = None,
    secret: str | None = None,
    freshness_seconds: int = FRESHNESS_SECONDS,
) -> EnvelopeVerification:
    envelope = parse_envelope(raw)
    if envelope is None:
        return EnvelopeVerification(False, reason="malformed-envelope")

    if envelope.get("v") != 1:
        return EnvelopeVerification(False, envelope=envelope, reason="unsupported-version")

    message_id = envelope_message_id(envelope)
    if not message_id:
        return EnvelopeVerification(False, envelope=envelope, reason="missing-message-id")
    replay_key = (envelope_channel(envelope), message_id)
    if replay_key in (seen_message_ids or set()):
        return EnvelopeVerification(False, envelope=envelope, reason="replayed-message-id")

    key = secret if secret is not None else os.getenv(SECRET_ENV)
    if not key:
        return EnvelopeVerification(False, envelope=envelope, reason="secret-unset")

    supplied_sig = envelope.get("sig")
    if not isinstance(supplied_sig, str) or not supplied_sig:
        return EnvelopeVerification(False, envelope=envelope, reason="missing-signature")
    expected_sig = signature_for(envelope, key)
    if not hmac.compare_digest(supplied_sig, expected_sig):
        return EnvelopeVerification(False, envelope=envelope, reason="invalid-signature")

    if envelope.get("intent") != "complete":
        return EnvelopeVerification(False, envelope=envelope, reason="invalid-intent")

    task_id = envelope.get("task_id")
    if not isinstance(task_id, str) or not task_id.strip():
        return EnvelopeVerification(False, envelope=envelope, reason="missing-task-id")

    timestamp = parse_timestamp(envelope.get("timestamp"))
    if timestamp is None:
        return EnvelopeVerification(False, envelope=envelope, reason="invalid-timestamp")
    reference_now = now or current_time()
    if abs((reference_now - timestamp).total_seconds()) > freshness_seconds:
        return EnvelopeVerification(False, envelope=envelope, reason="stale-timestamp")

    return EnvelopeVerification(True, envelope=envelope)
