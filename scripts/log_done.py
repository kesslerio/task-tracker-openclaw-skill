#!/usr/bin/env python3
"""
Append completion events to a daily markdown log file.
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

KNOWN_ACTIONS = {
    "email_sent",
    "sms_sent",
    "crm_update",
    "calendar_update",
    "deal_update",
}


def _env_path(name: str) -> Path | None:
    raw_value = os.getenv(name)
    if not raw_value:
        return None
    cleaned = raw_value.strip()
    if not cleaned:
        return None
    return Path(cleaned).expanduser()


def _resolve_log_dir(log_path: str | Path | None) -> Path | None:
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


def _format_context(context: dict | None) -> str:
    if not context:
        return ""

    parts: list[str] = []
    for key in sorted(context.keys()):
        value = context[key]
        if value is None:
            continue
        if isinstance(value, (dict, list)):
            rendered = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        else:
            rendered = str(value)
        parts.append(f"{key}={rendered}")

    if not parts:
        return ""

    return f"  details: {', '.join(parts)}\n"


def log_done(
    action: str,
    summary: str,
    context: dict | None = None,
    log_path: str | Path | None = None,
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

    normalized_action = action.strip()
    normalized_summary = summary.strip()

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
        # Open in append mode for safer concurrent writes.
        with log_file.open("a+", encoding="utf-8") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(f"## {date_str}\n\n")
            handle.write(f"- {timestamp} ✅ {normalized_summary}\n")
            if context_line:
                handle.write(context_line)
    except (PermissionError, OSError) as exc:
        print(f"Error: cannot write log file '{log_file}': {exc}", file=sys.stderr)
        return False

    return True


def _merge_context(base: dict | None, extra: dict) -> dict:
    merged: dict = {}
    if base:
        merged.update(base)
    merged.update(extra)
    return merged


def log_email_sent(recipient: str, subject: str | None = None, context: dict | None = None) -> bool:
    details = {"recipient": recipient}
    summary = f"Sent email to {recipient}"
    if subject:
        details["subject"] = subject
        summary += f" ({subject})"
    return log_done(
        action="email_sent",
        summary=summary,
        context=_merge_context(context, details),
    )


def log_sms_sent(recipient: str, summary: str | None = None, context: dict | None = None) -> bool:
    effective_summary = summary.strip() if summary else f"Sent SMS to {recipient}"
    return log_done(
        action="sms_sent",
        summary=effective_summary,
        context=_merge_context(context, {"recipient": recipient}),
    )


def log_crm_update(record: str, action_detail: str, context: dict | None = None) -> bool:
    summary = f"Updated CRM record {record}: {action_detail}"
    return log_done(
        action="crm_update",
        summary=summary,
        context=_merge_context(context, {"record": record, "action_detail": action_detail}),
    )


def log_deal_update(deal: str, stage: str | None = None, context: dict | None = None) -> bool:
    if stage:
        summary = f"Updated deal {deal} to stage {stage}"
        extra = {"deal": deal, "stage": stage}
    else:
        summary = f"Updated deal {deal}"
        extra = {"deal": deal}
    return log_done(
        action="deal_update",
        summary=summary,
        context=_merge_context(context, extra),
    )


def _parse_context(context_raw: str | None) -> dict | None:
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
