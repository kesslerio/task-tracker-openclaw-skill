#!/usr/bin/env python3
"""Read-only completion candidate review summaries for workflow surfaces."""

from __future__ import annotations

from datetime import date
from typing import Any

from completion_candidates import project_candidates
from task_ledger import MalformedLedgerError


def visible_completion_candidates(candidates: list[dict[str, Any]], *, include_all: bool = False) -> list[dict[str, Any]]:
    if include_all:
        return candidates
    today = date.today().isoformat()
    return [
        candidate for candidate in candidates
        if candidate.get("status") != "snoozed"
        or (candidate.get("snoozed_until") or "") <= today
    ]


def candidate_review_summary(*, personal: bool = False, limit: int = 5) -> dict[str, Any]:
    try:
        candidates = visible_completion_candidates(project_candidates(personal=personal))
    except MalformedLedgerError as exc:
        return {
            "available": False,
            "review_required": True,
            "error": {
                "code": "malformed-ledger",
                "malformed": [
                    {
                        "path": item.path,
                        "line_number": item.line_number,
                        "message": item.message,
                    }
                    for item in exc.malformed
                ],
            },
        }
    except OSError as exc:
        return {
            "available": False,
            "review_required": True,
            "error": {"code": "io-error", "message": str(exc)},
        }

    items = []
    for candidate in candidates[:limit]:
        suggested = candidate.get("suggested_match") or {}
        items.append(
            {
                "candidate_id": candidate.get("candidate_id"),
                "status": candidate.get("status"),
                "summary": candidate.get("summary"),
                "confirmable_task_id": candidate.get("confirmable_task_id"),
                "suggested_task_id": suggested.get("task_id"),
                "suggested_title": suggested.get("title"),
                "review_required": candidate.get("review_required", True),
            }
        )

    return {
        "available": True,
        "review_required": bool(candidates),
        "total": len(candidates),
        "items": items,
        "overflow": max(0, len(candidates) - len(items)),
        "instructions": "Review candidates; confirm only by candidate ID and canonical task_id.",
    }
