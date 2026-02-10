#!/usr/bin/env python3
"""
Append completion events to a daily markdown log file.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Union

KNOWN_ACTIONS = {
    "email_sent",
    "sms_sent",
    "crm_update",
    "calendar_update",
    "deal_update",
}


def _sanitize_line(text: str) -> str:
    """Strip newlines and control chars to prevent markdown injection."""
    return text.replace("\r", "").replace("\n", " ").strip()


def _env_path(name: str) -> Optional[Path]:
    raw_value = os.getenv(name)
    if not raw_value:
        return None
    cleaned = raw_value.strip()
    if not cleaned:
        return None
    return Path(cleaned).expanduser()


def _resolve_log_dir(log_path: Optional[Union[str, Path]] = None) -> Optional[Path]:
    if log_path:
        return Path(log_path).expanduser()

    from_done_log_dir = _env_path("TASK_TRACKER_DONE_LOG_DIR")
    if from_done_log_dir:
        return from_done_log_dir

    from_daily_notes_dir = _env_path("TASK_TRACKER_DAILY_NOTES_DIR")
    if from_daily_notes_dir:
        return from_daily_notes_dir

    print(
        "Warning: done logging skipped (set TASK_TRACKER_DONE_LOG_DIR or TASK_TRACKER_DAILY_NOTES_DIR).",
        file=sys.stderr,
    )
    return None


def _format_context(context: Optional[Dict[str, object]]) -> str:
    """Serialize context as a JSON object on an indented line.

    Uses JSON for unambiguous round-tripping (no delimiter collisions).
    """
    if not context:
        return ""

    # Filter out None values and coerce keys to str for safe sorting
    cleaned = {
        str(k): v for k, v in context.items() if v is not None
    }
    if not cleaned:
        return ""

    rendered = json.dumps(cleaned, ensure_ascii=False, separators=(", ", ": "), sort_keys=True)
    # Sanitize to single line in case values contain newlines
    rendered = _sanitize_line(rendered)
    return f"  {rendered}\n"


def log_done(
    action: str,
    summary: str,
    context: Optional[Dict[str, object]] = None,
    log_path: Optional[Union[str, Path]] = None,
) -> bool:
    """
    Log a completed action to today's markdown file.

    Returns True when the entry is written, otherwise False.
    """
    if not isinstance(action, str) or not action.strip():
        raise ValueError("action must be a non-empty string")
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("summary must be a non-empty string")
    if context is not None and not isinstance(context, dict):
        raise ValueError("context must be a dict when provided")

    normalized_action = _sanitize_line(action)
    normalized_summary = _sanitize_line(summary)

    if normalized_action not in KNOWN_ACTIONS:
        print(
            f"Warning: unknown action '{normalized_action}', logging anyway.",
            file=sys.stderr,
        )

    log_dir = _resolve_log_dir(log_path)
    if log_dir is None:
        return False

    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except (PermissionError, OSError) as exc:
        print(f"Error: cannot create log directory '{log_dir}': {exc}", file=sys.stderr)
        return False

    now = datetime.now()
    date_str = now.strftime("%Y-%m-%d")
    timestamp = now.strftime("%H:%M")
    log_file = log_dir / f"{date_str}.md"
    context_line = _format_context(context)

    try:
        # Build the full entry as a single string so append is atomic-ish
        entry = f"- {timestamp} ✅ {normalized_summary}\n"
        if context_line:
            entry += context_line

        with log_file.open("a", encoding="utf-8") as handle:
            handle.write(entry)
    except (PermissionError, OSError) as exc:
        print(f"Error: cannot write log file '{log_file}': {exc}", file=sys.stderr)
        return False

    return True


def _merge_context(base: Optional[Dict[str, object]], extra: dict) -> dict:
    merged: dict = {}
    if base:
        merged.update(base)
    merged.update(extra)
    return merged


def log_email_sent(
    recipient: str, subject: Optional[str] = None, context: Optional[Dict[str, object]] = None
) -> bool:
    details: dict = {"recipient": recipient}
    summary = f"Sent email to {recipient}"
    if subject:
        details["subject"] = subject
        summary += f" ({subject})"
    return log_done(
        action="email_sent",
        summary=summary,
        context=_merge_context(context, details),
    )


def log_sms_sent(
    recipient: str, summary: Optional[str] = None, context: Optional[Dict[str, object]] = None
) -> bool:
    effective_summary = summary.strip() if summary and summary.strip() else f"Sent SMS to {recipient}"
    return log_done(
        action="sms_sent",
        summary=effective_summary,
        context=_merge_context(context, {"recipient": recipient}),
    )


def log_crm_update(
    record: str, action_detail: str, context: Optional[Dict[str, object]] = None
) -> bool:
    summary = f"Updated CRM record {record}: {action_detail}"
    return log_done(
        action="crm_update",
        summary=summary,
        context=_merge_context(context, {"record": record, "action_detail": action_detail}),
    )


def log_deal_update(
    deal: str, stage: Optional[str] = None, context: Optional[Dict[str, object]] = None
) -> bool:
    if stage:
        summary = f"Updated deal {deal} to stage {stage}"
        extra: dict = {"deal": deal, "stage": stage}
    else:
        summary = f"Updated deal {deal}"
        extra = {"deal": deal}
    return log_done(
        action="deal_update",
        summary=summary,
        context=_merge_context(context, extra),
    )


def _parse_context(context_raw: Optional[str]) -> Optional[dict]:
    if not context_raw:
        return None
    try:
        parsed = json.loads(context_raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"--context must be valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("--context must decode to a JSON object")
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser(description="Log completed actions to daily notes")
    parser.add_argument("--action", required=True, help="Action type (e.g. email_sent)")
    parser.add_argument("--summary", required=True, help="Human-readable summary")
    parser.add_argument(
        "--context",
        help="Optional JSON object with metadata, e.g. '{\"deal_id\":123}'",
    )
    parser.add_argument(
        "--log-path",
        help="Optional directory override for done log files",
    )
    args = parser.parse_args()

    try:
        context = _parse_context(args.context)
    except ValueError as exc:
        parser.error(str(exc))

    did_log = log_done(
        action=args.action,
        summary=args.summary,
        context=context,
        log_path=args.log_path,
    )
    return 0 if did_log else 1


if __name__ == "__main__":
    raise SystemExit(main())
