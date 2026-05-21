#!/usr/bin/env python3
"""Workflow-safe controls for the completion candidate inbox.

This script is intentionally a thin wrapper over completion_candidates.py. It is
for Telegram, Lobster, and other workflow shells that need list/show/decision
actions without importing the CLI module or calling task completion directly.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from completion_candidates import (  # noqa: E402
    confirm_candidate,
    get_candidate,
    mark_shown,
    project_candidates,
    reject_candidate,
    snooze_candidate,
)
from task_ledger import MalformedLedgerError  # noqa: E402


def _payload(command: str, **fields) -> dict:
    payload = {"schema_version": "v1", "command": command}
    payload.update(fields)
    return payload


def _print(payload: dict, *, exit_on_error: bool = True) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))
    if exit_on_error and payload.get("ok") is False:
        sys.exit(2)


def _visible(candidates: list[dict], *, include_all: bool = False) -> list[dict]:
    if include_all:
        return candidates
    today = date.today().isoformat()
    return [
        candidate for candidate in candidates
        if candidate.get("status") != "snoozed"
        or (candidate.get("snoozed_until") or "") <= today
    ]


def _handle(args: argparse.Namespace) -> dict:
    if args.command == "list":
        candidates = _visible(
            project_candidates(include_terminal=args.all, personal=args.personal),
            include_all=args.all,
        )
        if args.mark_shown:
            for candidate in candidates:
                if candidate.get("status") == "new":
                    mark_shown(candidate["candidate_id"], personal=args.personal)
            candidates = _visible(
                project_candidates(include_terminal=args.all, personal=args.personal),
                include_all=args.all,
            )
        return _payload("completion-inbox-control list", candidates=candidates, total=len(candidates))

    if args.command == "show":
        candidate = get_candidate(args.candidate_id, include_terminal=True, personal=args.personal)
        if candidate is None:
            return _payload(
                "completion-inbox-control show",
                ok=False,
                error={"code": "candidate-not-found"},
            )
        if args.mark_shown and candidate.get("status") == "new":
            result = mark_shown(args.candidate_id, personal=args.personal)
            candidate = result.get("candidate")
        return _payload("completion-inbox-control show", candidate=candidate)

    if args.command == "reject":
        return _payload(
            "completion-inbox-control reject",
            **reject_candidate(args.candidate_id, reason=args.reason, personal=args.personal),
        )

    if args.command == "snooze":
        return _payload(
            "completion-inbox-control snooze",
            **snooze_candidate(args.candidate_id, until=args.until, personal=args.personal),
        )

    if args.command == "confirm":
        return _payload(
            "completion-inbox-control confirm",
            **confirm_candidate(args.candidate_id, task_id=args.task_id, personal=args.personal),
        )

    return _payload("completion-inbox-control", ok=False, error={"code": "unknown-command"})


def main() -> None:
    parser = argparse.ArgumentParser(description="Workflow-safe completion inbox controls")
    parser.add_argument("--personal", action="store_true", help="Use Personal Tasks instead of Work Tasks")
    sub = parser.add_subparsers(dest="command", required=True)

    list_parser = sub.add_parser("list", help="List active completion candidates")
    list_parser.add_argument("--all", action="store_true", help="Include terminal and future-snoozed candidates")
    list_parser.add_argument("--mark-shown", action="store_true", help="Record shown events for new candidates")

    show_parser = sub.add_parser("show", help="Show one completion candidate")
    show_parser.add_argument("candidate_id")
    show_parser.add_argument("--mark-shown", action="store_true", help="Record a shown event for a new candidate")

    reject_parser = sub.add_parser("reject", help="Reject a completion candidate")
    reject_parser.add_argument("candidate_id")
    reject_parser.add_argument("--reason")

    snooze_parser = sub.add_parser("snooze", help="Snooze a completion candidate")
    snooze_parser.add_argument("candidate_id")
    snooze_parser.add_argument("--until", required=True, help="Date to resurface candidate, YYYY-MM-DD")

    confirm_parser = sub.add_parser("confirm", help="Confirm candidate through ID-only completion")
    confirm_parser.add_argument("candidate_id")
    confirm_parser.add_argument("--task-id", help="Canonical task_id required unless exact ID/link evidence")

    args = parser.parse_args()
    try:
        _print(_handle(args))
    except MalformedLedgerError as exc:
        _print(
            _payload(
                f"completion-inbox-control {args.command}",
                ok=False,
                error={
                    "code": "malformed-ledger",
                    "malformed": [
                        {
                            "path": item.path,
                            "line_number": item.line_number,
                            "message": item.message,
                            "raw_line": item.raw_line,
                        }
                        for item in exc.malformed
                    ],
                },
            )
        )
    except OSError as exc:
        _print(
            _payload(
                f"completion-inbox-control {args.command}",
                ok=False,
                error={"code": "io-error", "message": str(exc)},
            )
        )


if __name__ == "__main__":
    main()
