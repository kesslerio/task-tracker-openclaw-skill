#!/usr/bin/env python3
"""Read-only task health audits for workflow surfaces."""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from completion_candidates import project_candidates
from parking_lot import audit_items
from task_identity import audit_payload
from task_ledger import MalformedLedgerError
from task_records import TaskRecord, active_records, load_records, record_to_task_dict
from utils import get_tasks_file

DEFAULT_STALE_DAYS = 14
DEFAULT_CANDIDATE_DAYS = 7


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def _parse_event_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return _parse_date(value[:10])


def _normalized_title(title: str) -> str:
    return " ".join((title or "").casefold().split())


def _safe_task_ref(record: TaskRecord) -> dict[str, Any]:
    row = record_to_task_dict(record)
    return {
        "task_id": row.get("task_id"),
        "identity_source": row.get("identity_source"),
        "fallback_id": row.get("fallback_id"),
        "fallback_only": row.get("fallback_only"),
        "missing_task_id": row.get("missing_task_id"),
        "title": row.get("title"),
        "section": row.get("section"),
        "area": row.get("area"),
        "due": row.get("due"),
        "line_number": row.get("line_number"),
    }


def _finding(
    code: str,
    severity: str,
    reason: str,
    *,
    basis: dict[str, Any],
    recommended_action: str,
    tasks: list[dict[str, Any]] | None = None,
    candidate_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    finding = {
        "code": code,
        "severity": severity,
        "reason": reason,
        "basis": basis,
        "recommended_action": recommended_action,
    }
    if tasks is not None:
        finding["tasks"] = tasks
    if candidate_id:
        finding["candidate_id"] = candidate_id
    if details:
        finding["details"] = details
    return finding


def _identity_findings(identity: dict[str, Any]) -> list[dict[str, Any]]:
    audit = identity.get("audit") or {}
    findings: list[dict[str, Any]] = []

    for item in audit.get("malformed_task_ids", []):
        findings.append(
            _finding(
                "malformed-task-id",
                "high",
                "Task line contains malformed task_id metadata.",
                basis={"source": "identity-audit", "line_number": item.get("line_number")},
                recommended_action="Run identity-audit, then repair the task line manually.",
                tasks=[item],
            )
        )

    for item in audit.get("duplicate_task_ids", []):
        findings.append(
            _finding(
                "duplicate-task-id",
                "high",
                "Multiple active tasks share the same canonical task ID.",
                basis={"source": "identity-audit", "task_id": item.get("task_id")},
                recommended_action="Run identity-audit and fix duplicate task_id:: metadata before mutating either task.",
                tasks=item.get("items") or [],
            )
        )

    for item in audit.get("missing_task_ids", []):
        findings.append(
            _finding(
                "missing-task-id",
                "medium",
                "Active task is missing canonical task_id:: metadata.",
                basis={"source": "identity-audit", "line_number": item.get("line_number")},
                recommended_action="Run identity-repair --dry-run, then identity-repair --apply if the repair is unambiguous.",
                tasks=[item],
            )
        )

    return findings


def _duplicate_title_findings(records: list[TaskRecord]) -> list[dict[str, Any]]:
    groups: dict[str, list[TaskRecord]] = {}
    for record in active_records(records):
        groups.setdefault(_normalized_title(record.title), []).append(record)

    findings = []
    for _, group in sorted(groups.items()):
        if len(group) < 2:
            continue
        findings.append(
            _finding(
                "duplicate-title",
                "medium",
                "Multiple active tasks have the same title; review before pruning or merging.",
                basis={"source": "task-records", "match": "normalized-title"},
                recommended_action="Review the canonical task IDs and decide manually; do not merge or complete by title.",
                tasks=[_safe_task_ref(record) for record in group],
            )
        )
    return findings


def _due_findings(
    records: list[TaskRecord],
    *,
    today: date,
    stale_days: int,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    stale_cutoff = today - timedelta(days=stale_days)
    for record in active_records(records):
        due_date = _parse_date(record.due)
        if not due_date:
            continue
        task = _safe_task_ref(record)
        if due_date < today:
            age_days = (today - due_date).days
            findings.append(
                _finding(
                    "overdue-task",
                    "high" if age_days >= stale_days else "medium",
                    "Active task is past its due date.",
                    basis={"source": "task-due-date", "due": record.due, "age_days": age_days},
                    recommended_action="Review the task by canonical task_id::; carry, postpone, freeze, or complete explicitly.",
                    tasks=[task],
                )
            )
        if due_date <= stale_cutoff:
            age_days = (today - due_date).days
            findings.append(
                _finding(
                    "stale-active-task",
                    "high",
                    "Active task has been overdue longer than the stale threshold.",
                    basis={
                        "source": "task-due-date",
                        "due": record.due,
                        "age_days": age_days,
                        "threshold_days": stale_days,
                    },
                    recommended_action="Decide whether to rescope, freeze, backlog, or complete by canonical task_id::.",
                    tasks=[task],
                )
            )
    return findings


def _candidate_findings(
    *,
    personal: bool,
    today: date,
    candidate_days: int,
) -> list[dict[str, Any]]:
    try:
        candidates = project_candidates(personal=personal)
    except MalformedLedgerError as exc:
        return [
            _finding(
                "malformed-ledger",
                "high",
                "Candidate ledger contains malformed JSONL and cannot be safely projected.",
                basis={"source": "completion-candidates", "malformed_count": len(exc.malformed)},
                recommended_action="Fix malformed ledger lines before trusting candidate audit output.",
                details={
                    "malformed": [
                        {
                            "path": item.path,
                            "line_number": item.line_number,
                            "message": item.message,
                        }
                        for item in exc.malformed
                    ]
                },
            )
        ]

    findings: list[dict[str, Any]] = []
    cutoff = today - timedelta(days=candidate_days)
    for candidate in candidates:
        status = candidate.get("status")
        history = candidate.get("history") or []
        first_seen = _parse_event_date(history[0].get("timestamp") if history else None)
        age_days = (today - first_seen).days if first_seen else None
        if status == "apply_failed":
            findings.append(
                _finding(
                    "candidate-apply-failed",
                    "high",
                    "Completion candidate failed to apply and is retryable.",
                    basis={"source": "candidate-history", "status": status, "age_days": age_days},
                    recommended_action=f"Inspect candidate {candidate.get('candidate_id')} and retry or reject it explicitly.",
                    candidate_id=candidate.get("candidate_id"),
                    details={"summary": candidate.get("summary"), "last_error": candidate.get("last_error")},
                )
            )
            continue
        if status == "snoozed":
            snoozed_until = _parse_date(candidate.get("snoozed_until"))
            if snoozed_until and snoozed_until > today:
                continue
            if snoozed_until and snoozed_until <= today:
                findings.append(
                    _finding(
                        "candidate-snooze-expired",
                        "medium",
                        "Snoozed completion candidate is actionable again.",
                        basis={"source": "candidate-history", "snoozed_until": candidate.get("snoozed_until")},
                        recommended_action=f"Review candidate {candidate.get('candidate_id')}; confirm, reject, duplicate, or snooze.",
                        candidate_id=candidate.get("candidate_id"),
                        details={"summary": candidate.get("summary")},
                    )
                )
        if first_seen and first_seen <= cutoff:
            findings.append(
                _finding(
                    "stale-completion-candidate",
                    "medium",
                    "Completion candidate has remained unresolved beyond the review threshold.",
                    basis={
                        "source": "candidate-history",
                        "first_seen": first_seen.isoformat(),
                        "age_days": age_days,
                        "threshold_days": candidate_days,
                    },
                    recommended_action=f"Review candidate {candidate.get('candidate_id')}; confirm only by canonical task_id::.",
                    candidate_id=candidate.get("candidate_id"),
                    details={"summary": candidate.get("summary"), "status": status},
                )
            )
    return findings


def _backlog_findings(tasks_file: Path, *, backlog_cap: int | None) -> tuple[list[dict[str, Any]], int | None]:
    try:
        audit = audit_items(tasks_file, cap=backlog_cap)
    except (OSError, ValueError) as exc:
        return (
            [
                _finding(
                    "backlog-unavailable",
                    "low",
                    "Parking Lot could not be read for backlog audit.",
                    basis={"source": "parking-lot", "error": str(exc)},
                    recommended_action="Inspect task board and parking-lot env configuration, then rerun task-audit.",
                )
            ],
            backlog_cap,
        )
    if not audit.get("available"):
        return [], audit.get("cap")

    findings: list[dict[str, Any]] = []
    cap = audit.get("cap") or 0
    total = audit.get("total") or 0
    if cap and total >= cap:
        findings.append(
            _finding(
                "backlog-cap-reached",
                "medium",
                "Parking Lot is at or above its configured cap.",
                basis={"source": "parking-lot", "total": total, "cap": cap},
                recommended_action="Review stale backlog items before adding more.",
                details={"total": total, "cap": cap},
            )
        )
    for item in audit.get("stale") or []:
        findings.append(
            _finding(
                "stale-backlog-item",
                "low",
                "Parking Lot item is older than the stale threshold.",
                basis={"source": "parking-lot", "created": item.get("created"), "age_days": item.get("age_days")},
                recommended_action="Review backlog item manually; promote or drop only through explicit backlog commands.",
                details=item,
            )
        )
    return findings, audit.get("cap")


def _summary(findings: list[dict[str, Any]], *, limit: int) -> dict[str, Any]:
    severity_counts = Counter(finding.get("severity", "unknown") for finding in findings)
    top = findings[:limit]
    return {
        "review_required": bool(findings),
        "total": len(findings),
        "by_severity": dict(sorted(severity_counts.items())),
        "items": top,
        "overflow": max(0, len(findings) - len(top)),
        "instructions": "Review audit findings; mutate tasks only through canonical task_id:: commands.",
    }


def collect_task_audit(
    *,
    personal: bool = False,
    stale_days: int = DEFAULT_STALE_DAYS,
    candidate_days: int = DEFAULT_CANDIDATE_DAYS,
    backlog_cap: int | None = None,
    limit: int = 5,
    today: date | None = None,
) -> dict[str, Any]:
    today = today or date.today()
    tasks_file, _ = get_tasks_file(personal)
    findings: list[dict[str, Any]] = []
    records: list[TaskRecord] = []

    identity = audit_payload(personal=personal)
    error = identity.get("error")
    if error:
        findings.append(
            _finding(
                "tasks-file-unavailable",
                "high",
                "Task board could not be loaded.",
                basis={"source": "task-board", "path": str(tasks_file)},
                recommended_action="Fix the task board path before running task automation.",
                details={"error": error},
            )
        )
    else:
        _, _, records = load_records(personal)
        findings.extend(_identity_findings(identity))
        findings.extend(_duplicate_title_findings(records))
        findings.extend(_due_findings(records, today=today, stale_days=stale_days))

    findings.extend(_candidate_findings(personal=personal, today=today, candidate_days=candidate_days))
    backlog_findings, effective_backlog_cap = _backlog_findings(tasks_file, backlog_cap=backlog_cap)
    findings.extend(backlog_findings)

    severity_order = {"high": 0, "medium": 1, "low": 2}
    findings = sorted(findings, key=lambda item: (severity_order.get(item.get("severity"), 9), item.get("code", "")))

    return {
        "ok": not any(finding.get("severity") == "high" for finding in findings),
        "tasks_file": str(tasks_file),
        "personal": personal,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "thresholds": {
            "stale_days": stale_days,
            "candidate_days": candidate_days,
            "backlog_cap": effective_backlog_cap,
        },
        "totals": {
            "active_tasks": len(active_records(records)),
            "findings": len(findings),
            "high": sum(1 for finding in findings if finding.get("severity") == "high"),
            "medium": sum(1 for finding in findings if finding.get("severity") == "medium"),
            "low": sum(1 for finding in findings if finding.get("severity") == "low"),
        },
        "findings": findings,
        "summary": _summary(findings, limit=limit),
    }


def task_audit_summary(*, personal: bool = False, limit: int = 5) -> dict[str, Any]:
    try:
        payload = collect_task_audit(personal=personal, limit=limit)
    except (OSError, ValueError) as exc:
        return {
            "available": False,
            "review_required": True,
            "error": {"code": "io-error", "message": str(exc)},
        }
    summary = payload.get("summary") or {}
    summary["available"] = True
    return summary
