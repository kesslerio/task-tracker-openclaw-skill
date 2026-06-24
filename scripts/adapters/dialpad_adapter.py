#!/usr/bin/env python3
"""Dialpad SMS evidence adapter backed by the local read-only SQLite store."""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cos_config
import error_envelope
import harvest_window

COMPONENT = "standup_harvest:dialpad_sms"
_AUTOMATED_PATTERNS = (
    re.compile(r"\bauto(?:mated)?[- ]?reply\b", re.IGNORECASE),
    re.compile(r"\bout of office\b", re.IGNORECASE),
    re.compile(r"\bdo not reply\b", re.IGNORECASE),
    re.compile(r"\bthis is an automated\b", re.IGNORECASE),
    re.compile(r"\bmessage generated automatically\b", re.IGNORECASE),
    re.compile(r"\bunsubscribe\b", re.IGNORECASE),
)


@dataclass(frozen=True)
class Message:
    dialpad_id: str
    contact_number: str
    direction: str
    timestamp: int
    text: str


def _db_path() -> Path | None:
    # Operator-configured via DIALPAD_SMS_DB (no hardcoded private path in a public
    # repo). Unset -> the source is "not configured" and contributes no candidates.
    raw = os.getenv("DIALPAD_SMS_DB")
    return Path(raw).expanduser() if raw else None


def _readonly_connection(path: Path) -> sqlite3.Connection:
    uri = f"file:{quote(str(path), safe='/')}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=1.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=1000")
    return conn


def _epoch_bounds(resolved: harvest_window.HarvestWindow) -> tuple[int, int, int, int]:
    start = int(resolved.evidence_start.astimezone(timezone.utc).timestamp())
    end = int(resolved.evidence_end.astimezone(timezone.utc).timestamp())
    return start, end, start * 1000, end * 1000


def _read_messages(path: Path, resolved: harvest_window.HarvestWindow) -> list[Message]:
    start_s, end_s, start_ms, end_ms = _epoch_bounds(resolved)
    with _readonly_connection(path) as conn:
        rows = conn.execute(
            """
            SELECT dialpad_id, contact_number, direction, timestamp, text
            FROM messages
            WHERE (
                (timestamp >= ? AND timestamp < ?)
                OR (timestamp >= ? AND timestamp < ?)
            )
            ORDER BY contact_number, timestamp, dialpad_id
            """,
            (start_s, end_s, start_ms, end_ms),
        ).fetchall()
    messages: list[Message] = []
    for row in rows:
        contact = str(row["contact_number"] or "").strip()
        direction = str(row["direction"] or "").strip().lower()
        timestamp = row["timestamp"]
        if not contact or direction not in {"inbound", "outbound"} or timestamp is None:
            continue
        messages.append(
            Message(
                dialpad_id=str(row["dialpad_id"] or ""),
                contact_number=contact,
                direction=direction,
                timestamp=int(timestamp),
                text=str(row["text"] or ""),
            )
        )
    return messages


def _timestamp_to_local(value: int) -> datetime:
    raw = value // 1000 if value > 10_000_000_000 else value
    return datetime.fromtimestamp(raw, tz=timezone.utc).astimezone(cos_config.local_tz())


def _is_boilerplate(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return True
    if len(stripped.split()) <= 1:
        return True
    return any(pattern.search(stripped) for pattern in _AUTOMATED_PATTERNS)


def _thread_digest(messages: list[Message]) -> str:
    digest = hashlib.sha256()
    for message in messages:
        digest.update(
            "\x1f".join(
                [
                    message.dialpad_id,
                    message.direction,
                    str(message.timestamp),
                    message.text,
                ]
            ).encode()
        )
        digest.update(b"\x1e")
    return digest.hexdigest()


def _thread_record(
    contact: str,
    messages: list[Message],
    *,
    resolved: harvest_window.HarvestWindow,
) -> dict[str, Any] | None:
    outbound = [message for message in messages if message.direction == "outbound"]
    substantive_outbound = [message for message in outbound if not _is_boilerplate(message.text)]
    sent_chars = sum(len(message.text.strip()) for message in substantive_outbound)
    sent_count = len(substantive_outbound)
    if (
        sent_count < cos_config.dialpad_sms_pushback_message_threshold()
        and sent_chars < cos_config.dialpad_sms_pushback_char_threshold()
    ):
        return None
    latest = max(substantive_outbound, key=lambda message: message.timestamp)
    evidence_date = resolved.evidence_date.isoformat()
    return {
        "source": "dialpad_sms",
        "kind": "activity",
        "provider_id": f"sms:{contact}:{evidence_date}",
        "provider_state": f"outbound={sent_count};chars={sent_chars};sha256={_thread_digest(messages)[:16]}",
        "occurred_at": _timestamp_to_local(latest.timestamp).isoformat(),
        "match_title": f"SMS thread with {contact} ({sent_count} sent)",
        "title": f"SMS thread with {contact} ({sent_count} sent)",
        "url": None,
    }


def _group_by_contact(messages: list[Message]) -> dict[str, list[Message]]:
    threads: dict[str, list[Message]] = {}
    for message in messages:
        threads.setdefault(message.contact_number, []).append(message)
    return threads


def harvest(
    *,
    resolved: harvest_window.HarvestWindow,
    trigger: str,
    query_start: datetime | None = None,
    query_end: datetime | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """Return one metadata-only evidence candidate per substantive SMS thread."""
    del query_start, query_end
    path = _db_path()
    if path is None:
        return [], False  # DIALPAD_SMS_DB not configured -> no candidates, not a failure
    try:
        messages = _read_messages(path, resolved)
    except (sqlite3.Error, OSError) as exc:
        error_envelope.log_degraded(COMPONENT, exc, trigger=trigger, check="dialpad_sms")
        return [], True

    records: list[dict[str, Any]] = []
    for contact, thread in _group_by_contact(messages).items():
        record = _thread_record(contact, thread, resolved=resolved)
        if record:
            records.append(record)
    return records, False
