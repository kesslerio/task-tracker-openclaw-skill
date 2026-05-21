#!/usr/bin/env python3
"""Shared completion-evidence parsing and task matching helpers."""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any

from task_records import active_records, record_to_task_dict, task_records as build_task_records
from utils import get_tasks_file

FUZZY_EVIDENCE_LINK_THRESHOLD = 0.90
FUZZY_REVIEW_THRESHOLD = 0.70


def safe_load_task_records(personal: bool = False) -> list:
    tasks_file, fmt = get_tasks_file(personal)
    if not tasks_file.exists():
        return []
    try:
        content = tasks_file.read_text()
    except OSError:
        return []
    try:
        return build_task_records(content, personal=personal, fmt=fmt)
    except Exception:
        return []


def normalize_title(title: str) -> str:
    lowered = (title or "").strip().casefold()
    lowered = re.sub(r"\[x\]|\[ \]|✅|☑️", " ", lowered)
    lowered = re.sub(r"\*\*|__|~~", "", lowered)
    lowered = re.sub(r"[^\w\s/-]", " ", lowered)
    lowered = re.sub(r"\s+", " ", lowered)
    return lowered.strip()


def extract_inline_identifiers(text: str) -> dict[str, set[str]]:
    exact_identifiers: set[str] = set()
    fallback_identifiers: set[str] = set()
    if not text:
        return {"exact": exact_identifiers, "fallback": fallback_identifiers}

    for match in re.findall(
        r"\b(?:id|task_id|task)::\s*([A-Za-z0-9._:-]*[A-Za-z0-9._-])(?=\s|$|[),.;!?])",
        text,
        flags=re.IGNORECASE,
    ):
        exact_identifiers.add(match.casefold())

    for url in re.findall(r"https?://[^\s)>\]]+", text):
        lowered_url = url.casefold()
        exact_identifiers.add(lowered_url)
        github_issue_match = re.search(
            r"^https?://(?:www\.)?github\.com/([^/\s]+)/([^/\s]+)/issues/(\d+)\b",
            lowered_url,
        )
        if github_issue_match:
            owner, repo, issue_num = github_issue_match.groups()
            exact_identifiers.add(f"gh:{owner}/{repo}#{issue_num}")
            fallback_identifiers.add(f"gh-issue-num:{issue_num}")

    for match in re.findall(r"\b#(\d+)\b", text):
        fallback_identifiers.add(f"gh-issue-num:{match}")

    return {"exact": exact_identifiers, "fallback": fallback_identifiers}


def record_identifier_bundle(record) -> dict[str, set[str]]:
    raw_identifiers = extract_inline_identifiers(record.raw_line)
    title_identifiers = extract_inline_identifiers(record.title)
    exact_identifiers = raw_identifiers["exact"] | title_identifiers["exact"]
    fallback_identifiers = raw_identifiers["fallback"] | title_identifiers["fallback"]
    if record.canonical_id:
        exact_identifiers.add(record.canonical_id.casefold())
    return {
        "exact_identifiers": exact_identifiers,
        "fallback_identifiers": fallback_identifiers,
    }


def canonical_record(record) -> dict[str, Any]:
    return record_to_task_dict(record)


def extract_done_lines(content: str) -> list[dict[str, Any]]:
    parsed: list[dict[str, Any]] = []
    for line_number, raw in enumerate(content.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue

        is_checkbox = bool(re.match(r"^\s*[-*+]\s+\[(?:x|X| )\]\s+", raw))
        is_checked = bool(re.match(r"^\s*[-*+]\s+\[(?:x|X)\]\s+", raw))

        is_plain_bullet = bool(re.match(r"^\s*[-*+]\s+", raw))
        if is_checkbox and not is_checked:
            continue
        if not is_checkbox and not is_plain_bullet and not line.startswith("✅"):
            pass

        cleaned = re.sub(r"^\s*[-*+]\s+", "", raw).strip()
        cleaned = re.sub(r"^\[(?:x|X| )\]\s+", "", cleaned)
        cleaned = re.sub(r"^\d{1,2}:\d{2}(?::\d{2})?\s+", "", cleaned)
        cleaned = re.sub(r"^✅\s*", "", cleaned)
        cleaned = re.sub(r"\s*✅\s*\d{4}-\d{2}-\d{2}\s*$", "", cleaned)
        cleaned = cleaned.strip()
        if not cleaned:
            continue

        identifiers = extract_inline_identifiers(cleaned)
        parsed.append(
            {
                "raw_line": raw.rstrip("\n"),
                "line_number": line_number,
                "title": cleaned,
                "normalized_title": normalize_title(cleaned),
                "exact_identifiers": identifiers["exact"],
                "fallback_identifiers": identifiers["fallback"],
            }
        )
    return parsed


def fuzzy_score(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left, right).ratio()


def build_task_catalog(records: list) -> list[dict[str, Any]]:
    catalog: list[dict[str, Any]] = []
    for record in active_records(records):
        bundle = record_identifier_bundle(record)
        canonical = canonical_record(record)
        catalog.append(
            {
                "record": record,
                "canonical": canonical,
                "normalized_title": normalize_title(canonical["title"]),
                "exact_identifiers": bundle["exact_identifiers"],
                "fallback_identifiers": bundle["fallback_identifiers"],
            }
        )
    return catalog


def match_evidence_line(
    line: dict[str, Any],
    catalog: list[dict[str, Any]],
    auto_threshold: float,
    review_threshold: float,
) -> dict[str, Any]:
    def sort_key(candidate: dict[str, Any]) -> str:
        canonical = candidate["canonical"]
        return canonical.get("task_id") or canonical.get("fallback_id") or canonical.get("title") or ""

    def result(
        *,
        candidate: dict[str, Any] | None,
        score: float,
        decision: str,
        match_type: str,
    ) -> dict[str, Any]:
        return {
            "raw_line": line["raw_line"],
            "parsed_title": line["title"],
            "normalized_title": line["normalized_title"],
            "canonical_task": candidate["canonical"] if candidate and decision != "no-match" else None,
            "match_metadata": {
                "matched_task_id": (
                    candidate["canonical"]["task_id"] if candidate and decision != "no-match" else None
                ),
                "score": round(float(score), 4),
                "decision": decision,
                "match_type": match_type,
            },
        }

    exact_matches = [
        candidate
        for candidate in catalog
        if line["exact_identifiers"] and (line["exact_identifiers"] & candidate["exact_identifiers"])
    ]
    if exact_matches:
        chosen = sorted(exact_matches, key=sort_key)[0]
        return result(
            candidate=chosen,
            score=1.0,
            decision="evidence-link",
            match_type="exact-id-or-link",
        )

    fallback_matches = [
        candidate
        for candidate in catalog
        if line["fallback_identifiers"] and (line["fallback_identifiers"] & candidate["fallback_identifiers"])
    ]
    if fallback_matches:
        chosen = sorted(fallback_matches, key=sort_key)[0]
        return result(
            candidate=chosen,
            score=0.6,
            decision="needs-review",
            match_type="issue-number-fallback",
        )

    exact_title_matches = [
        candidate for candidate in catalog if candidate["normalized_title"] == line["normalized_title"]
    ]
    if exact_title_matches:
        chosen = sorted(exact_title_matches, key=sort_key)[0]
        return result(
            candidate=chosen,
            score=1.0,
            decision="evidence-link",
            match_type="normalized-title",
        )

    scored = []
    for candidate in catalog:
        score = fuzzy_score(line["normalized_title"], candidate["normalized_title"])
        scored.append((score, sort_key(candidate), candidate))
    scored.sort(key=lambda item: (-item[0], item[1]))
    best_score, _, best = scored[0] if scored else (0.0, "", None)

    decision = "no-match"
    if best and best_score >= auto_threshold:
        decision = "evidence-link"
    elif best and best_score >= review_threshold:
        decision = "needs-review"

    return result(candidate=best, score=best_score, decision=decision, match_type="fuzzy")


def match_evidence_content(
    content: str,
    *,
    personal: bool = False,
    auto_threshold: float = FUZZY_EVIDENCE_LINK_THRESHOLD,
    review_threshold: float = FUZZY_REVIEW_THRESHOLD,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    parsed = extract_done_lines(content)
    records = safe_load_task_records(personal)
    catalog = build_task_catalog(records)
    matched = [
        match_evidence_line(
            line,
            catalog,
            auto_threshold=auto_threshold,
            review_threshold=review_threshold,
        )
        for line in parsed
    ]
    for line, match in zip(parsed, matched, strict=False):
        match["line_number"] = line.get("line_number")
    return parsed, matched
