"""Merge confirmed accomplishments with harvested evidence provenance."""

from __future__ import annotations

import copy
import re
from typing import Any

from evidence_matching import normalize_title


_GENERIC_TITLES = frozenset({"update", "fix", "wip", "standup", "misc", "cleanup", "chores", "notes", "done", "review"})


def merge(user_stated: list[dict[str, Any]], evidence_candidates: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return confirmed completions enriched with matching evidence.

    ``user_stated`` is the only source of completed accomplishments here. Evidence
    records can attach provenance to a confirmed item, but unmatched evidence is
    returned unchanged for the confirm gate.
    """
    completed = _dedupe_user_stated(user_stated)
    matched_candidate_indexes: set[int] = set()

    for index, candidate in enumerate(evidence_candidates):
        for item in completed:
            if not _matches(item, candidate):
                continue
            _ensure_user_provenance(item)
            _append_unique_provenance(item, _evidence_provenance(candidate))
            matched_candidate_indexes.add(index)
            break

    remaining = [candidate for index, candidate in enumerate(evidence_candidates) if index not in matched_candidate_indexes]
    return completed, remaining


def _dedupe_user_stated(user_stated: list[dict[str, Any]]) -> list[dict[str, Any]]:
    completed: list[dict[str, Any]] = []
    by_title: dict[str, dict[str, Any]] = {}

    for item in user_stated:
        normalized = _title_key(str(item.get("title") or ""))
        if not normalized:
            continue
        existing = by_title.get(normalized)
        if existing is None:
            copied = copy.deepcopy(item)
            by_title[normalized] = copied
            completed.append(copied)
            continue

        _merge_missing_user_fields(existing, item)
        _ensure_user_provenance(existing)
        _append_unique_provenance(existing, _user_provenance(item))

    return completed


def _merge_missing_user_fields(existing: dict[str, Any], incoming: dict[str, Any]) -> None:
    for key, value in incoming.items():
        if key in {"title", "provenance"}:
            continue
        if key == "is_calendar_meeting" and value:
            existing[key] = True
            continue
        if existing.get(key) in (None, "", [], {}) and value not in (None, "", [], {}):
            existing[key] = copy.deepcopy(value)


def _matches(item: dict[str, Any], candidate: dict[str, Any]) -> bool:
    evidence_hash = str(candidate.get("evidence_hash") or "").strip()
    if evidence_hash and evidence_hash in _item_hashes(item):
        return True

    candidate_task_id = _candidate_task_id(candidate)
    if candidate_task_id and candidate_task_id in _item_task_ids(item):
        return True

    normalized_title = _title_key(str(item.get("title") or ""))
    return _is_specific_title(normalized_title) and _candidate_normalized_title(candidate) == normalized_title


def _is_specific_title(normalized: str) -> bool:
    tokens = normalized.split()
    if len(tokens) < 3:
        return False
    return not all(token in _GENERIC_TITLES for token in tokens)


def _title_key(title: str) -> str:
    """Reconcile-specific dedup/match key: normalize_title PLUS punctuation-insensitive.

    ``normalize_title`` preserves ``-`` and ``/``, so "follow up" and "follow-up"
    would otherwise stay distinct. For deduping confirmed completions and matching
    evidence by title we treat hyphen/slash as whitespace so common punctuation
    variants of the same accomplishment collapse to one (the documented contract).
    """
    normalized = normalize_title(str(title or ""))
    return re.sub(r"\s+", " ", re.sub(r"[-/]+", " ", normalized)).strip()


def _item_hashes(item: dict[str, Any]) -> set[str]:
    hashes: set[str] = set()
    evidence_hash = str(item.get("evidence_hash") or "").strip()
    if evidence_hash:
        hashes.add(evidence_hash)
    for entry in item.get("provenance") or []:
        if not isinstance(entry, dict):
            continue
        entry_hash = str(entry.get("evidence_hash") or "").strip()
        if entry_hash:
            hashes.add(entry_hash)
    return hashes


def _candidate_task_id(candidate: dict[str, Any]) -> str | None:
    for value in (
        candidate.get("matched_task_id"),
        (candidate.get("match") or {}).get("matched_task_id") if isinstance(candidate.get("match"), dict) else None,
    ):
        if value:
            return str(value)
    return None


def _item_task_ids(item: dict[str, Any]) -> set[str]:
    ids: set[str] = set()
    for key in ("task_id", "legacy_id", "id", "confirmable_task_id"):
        value = item.get(key)
        if value:
            ids.add(str(value))
    for key in ("title", "raw_line"):
        ids.update(_inline_task_ids(str(item.get(key) or "")))
    return ids


def _inline_task_ids(text: str) -> set[str]:
    if not text:
        return set()
    pattern = r"(?:^|\s)(?:task_id|id)::\s*([A-Za-z0-9._:-]*[A-Za-z0-9._-])(?=\s|$|[),.;!?])"
    return set(re.findall(pattern, text, flags=re.IGNORECASE))


def _candidate_normalized_title(candidate: dict[str, Any]) -> str:
    for key in ("normalized_title", "match_title", "title"):
        value = candidate.get(key)
        if value:
            return _title_key(str(value))
    return ""


def _ensure_user_provenance(item: dict[str, Any]) -> None:
    provenance = item.get("provenance")
    if not isinstance(provenance, list):
        item["provenance"] = []
    if not any(isinstance(entry, dict) and entry.get("kind") == "user_claim" for entry in item["provenance"]):
        item["provenance"].insert(0, _user_provenance(item))
    item["provenance"] = _dedupe_provenance(item["provenance"])


def _append_unique_provenance(item: dict[str, Any], entry: dict[str, Any]) -> None:
    provenance = item.get("provenance")
    if not isinstance(provenance, list):
        provenance = []
    provenance.append(entry)
    item["provenance"] = _dedupe_provenance(provenance)


def _user_provenance(item: dict[str, Any]) -> dict[str, Any]:
    entry = {
        "source": "user",
        "kind": "user_claim",
        "title": item.get("title"),
    }
    if item.get("completed_date"):
        entry["completed_date"] = item.get("completed_date")
    if item.get("task_id"):
        entry["task_id"] = item.get("task_id")
    return _compact(entry)


def _evidence_provenance(candidate: dict[str, Any]) -> dict[str, Any]:
    return _compact(
        {
            "source": candidate.get("source") or candidate.get("source_type"),
            "kind": candidate.get("kind"),
            "title": candidate.get("title"),
            "url": candidate.get("url"),
            "evidence_hash": candidate.get("evidence_hash"),
            "provider_id": candidate.get("provider_id"),
            "matched_task_id": _candidate_task_id(candidate),
            "auto_done_eligible": candidate.get("auto_done_eligible"),
        }
    )


def _dedupe_provenance(provenance: list[Any]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for entry in provenance:
        if not isinstance(entry, dict):
            continue
        compact = _compact(entry)
        key = (
            str(compact.get("source") or ""),
            str(compact.get("kind") or ""),
            str(compact.get("evidence_hash") or ""),
            str(compact.get("title") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(compact)
    return deduped


def _compact(entry: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in entry.items() if value is not None}
