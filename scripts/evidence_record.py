#!/usr/bin/env python3
"""Canonical evidence records for standup harvest adapters.

Adapters emit only activity/commitment candidates. Confirmed accomplishments are
reserved for the human confirm gate so source evidence never silently becomes a
DONE.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Literal

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cos_config
from harvest_ledger import _evidence, _evidence_hash

SCHEMA_VERSION = 1

Source = Literal["github", "gmail", "calendar", "dialpad_sms"]
AdapterKind = Literal["activity", "commitment"]
RecordKind = Literal["activity", "commitment", "accomplishment"]

ADAPTER_KINDS: set[str] = {"activity", "commitment"}
GATE_ONLY_KIND = "accomplishment"
AUTO_DONE_NEVER_SOURCES: set[str] = {"calendar", "dialpad_sms"}
_DISPLAY_REF_RE = re.compile(r"\s*\[[^\]]+#\d+\]\s*$")


@dataclass(frozen=True)
class EvidenceRecordInput:
    source: Source
    kind: AdapterKind
    provider_id: str
    provider_state: str
    occurred_at: str | datetime | date
    match_title: str
    title: str | None = None
    url: str | None = None
    match: dict[str, Any] | None = None
    auto_done_eligible: bool | None = None
    run_id: str | None = None


def _clean_match_title(value: str) -> str:
    return _DISPLAY_REF_RE.sub("", value).strip()


def _occurred_at_iso(value: str | datetime | date) -> str:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime.combine(value, time.min, tzinfo=cos_config.local_tz())
    else:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("occurred_at must be an ISO datetime/date") from exc

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=cos_config.local_tz())
    return dt.astimezone(cos_config.local_tz()).isoformat()


def _base_record(
    *,
    source: Source,
    kind: RecordKind,
    provider_id: str,
    provider_state: str,
    occurred_at: str | datetime | date,
    match_title: str,
    title: str | None,
    url: str | None,
    match: dict[str, Any] | None,
    auto_done_eligible: bool | None,
    run_id: str | None,
) -> dict[str, Any]:
    if not provider_id.strip():
        raise ValueError("provider_id is required")
    if not provider_state.strip():
        raise ValueError("provider_state is required")
    clean_title = _clean_match_title(match_title)
    if not clean_title:
        raise ValueError("match_title is required")

    record = _evidence(source, clean_title, provider_id, url)
    record.update(
        {
            "schema_version": SCHEMA_VERSION,
            "source": source,
            "source_type": source,
            "kind": kind,
            "provider_id": provider_id,
            "provider_state": provider_state,
            "evidence_hash": _evidence_hash(source, provider_id),
            "occurred_at": _occurred_at_iso(occurred_at),
            "match_title": clean_title,
            "title": (title or clean_title).strip(),
            "url": url,
            "match": match,
            "auto_done_eligible": (
                source not in AUTO_DONE_NEVER_SOURCES
                if auto_done_eligible is None
                else bool(auto_done_eligible)
            ),
        }
    )
    if source in AUTO_DONE_NEVER_SOURCES:
        record["auto_done_eligible"] = False
    if run_id is not None:
        record["run_id"] = run_id
    return record


def adapter_record(
    *,
    source: Source,
    kind: AdapterKind,
    provider_id: str,
    provider_state: str,
    occurred_at: str | datetime | date,
    match_title: str,
    title: str | None = None,
    url: str | None = None,
    match: dict[str, Any] | None = None,
    auto_done_eligible: bool | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Build an adapter-facing evidence candidate.

    ``kind="accomplishment"`` is deliberately unavailable here; only the confirm
    gate can mint that kind.
    """
    if kind not in ADAPTER_KINDS:
        raise ValueError("adapter records must be activity or commitment")
    return _base_record(
        source=source,
        kind=kind,
        provider_id=provider_id,
        provider_state=provider_state,
        occurred_at=occurred_at,
        match_title=match_title,
        title=title,
        url=url,
        match=match,
        auto_done_eligible=auto_done_eligible,
        run_id=run_id,
    )


def accomplishment_record(
    *,
    source: Source,
    provider_id: str,
    provider_state: str,
    occurred_at: str | datetime | date,
    match_title: str,
    title: str | None = None,
    url: str | None = None,
    match: dict[str, Any] | None = None,
    auto_done_eligible: bool | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Build the confirm-gate-only accomplishment record."""
    return _base_record(
        source=source,
        kind=GATE_ONLY_KIND,
        provider_id=provider_id,
        provider_state=provider_state,
        occurred_at=occurred_at,
        match_title=match_title,
        title=title,
        url=url,
        match=match,
        auto_done_eligible=auto_done_eligible,
        run_id=run_id,
    )


def from_input(record_input: EvidenceRecordInput) -> dict[str, Any]:
    return adapter_record(**record_input.__dict__)
