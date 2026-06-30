#!/usr/bin/env python3
"""
Daily Standup Generator - Creates a concise summary of today's priorities.
"""

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).parent))
from candidate_review import candidate_review_summary
from task_audit import task_audit_summary
from daily_notes import extract_completed_actions, extract_completed_tasks
from defended_three import propose_defended_three
import harvest_window
import pushback
import standup_harvest
from standup_common import (
    calendar_error,
    flatten_calendar_events,
    format_missed_tasks_block,
    format_time,
    get_calendar_events,
    resolve_standup_date,
)
import error_envelope
import reconcile
from task_records import fallback_id_for
from utils import (
    load_tasks,
    get_missed_tasks_bucketed,
    regroup_by_effective_priority,
    summarize_objective_progress,
    escalation_suffix,
    recurrence_suffix,
    parse_duration,
    format_duration,
    dependency_suffix,
    sprint_suffix,
)


def tomorrow_pointer_state(records=None) -> dict:
    """Resolve the EOD-set #1 pointer into the single standup #1 state."""
    try:
        import tomorrow_pointer

        if records is None:
            from task_records import load_records

            _, _, records = load_records(personal=False)
        resolved = tomorrow_pointer.resolve_to_record(records)
    except Exception:
        return {
            "status": "unavailable",
            "task_id": None,
            "title": "",
            "line": "🎯 **No #1 set — pick one today.**",
        }

    status = resolved.get("status")
    if status == tomorrow_pointer.STATUS_ACTIVE:
        title = str(resolved.get("title") or "")
        return {
            "status": status,
            "task_id": resolved.get("task_id"),
            "title": title,
            "line": f"🎯 **Today's #1 (set last night):** {title}",
        }
    if status == tomorrow_pointer.STATUS_STALE:
        return {
            "status": status,
            "task_id": resolved.get("task_id"),
            "title": "",
            "line": "🎯 **Last night's #1 is done — pick a fresh one.**",
        }
    return {
        "status": status,
        "task_id": None,
        "title": "",
        "line": "🎯 **No #1 set — pick one today.**",
    }


def capacity_line(records=None):
    """Build the Layer-2 capacity ceiling line for the standup, or None.

    Reuses ``focus_core`` so the standup display and the ``add_task`` write-time
    gate agree on one calculation. ``records`` is the parsed work-task record
    list; when omitted it is loaded from the work file. Any failure degrades to
    None (no capacity line) rather than breaking the whole standup.
    """
    try:
        from focus_core import capacity_display, summarize_capacity

        if records is None:
            from task_records import load_records

            _, _, records = load_records(personal=False)
        return capacity_display(summarize_capacity(records))
    except Exception:
        return None


def tomorrow_pointer_line(records=None) -> str:
    """The standup's OPENING line: tomorrow's #1, resolved from the U6 pointer.

    This is the read side of the standup<->EOD daily loop (KTD-6 / R2). The EOD sets
    "tomorrow's #1" in ``tomorrow-pointer.json``; this line resolves that pointer against
    the LIVE active board and opens the standup with it. Four outcomes, mirroring
    ``tomorrow_pointer.resolve_to_record``:

    * a still-active pointer -> ``🎯 **Today's #1 (set last night):** <live title>``,
    * an explicit "none" pointer or no pointer file -> ``🎯 **No #1 set — pick one today.**``
      (the EOD ran on an empty board, or never ran),
    * a since-completed/dropped pointer -> ``🎯 **Last night's #1 is done — pick a fresh
      one.**`` (degrade cleanly; NEVER resurface a dead #1).

    NEVER crashes the standup: any resolution failure degrades to the "pick one" line
    rather than raising, so a broken pointer can never blank the morning standup.
    """
    return tomorrow_pointer_state(records=records)["line"]


def _task_key(task: dict) -> tuple[str, str]:
    for field in ("task_id", "legacy_id", "fallback_id", "raw_line"):
        value = task.get(field)
        if value:
            return (field, str(value))
    return ("title", str(task.get("title") or "").casefold())


def _dedupe_tasks(tasks: list[dict], *, seen: set[tuple[str, str]] | None = None) -> list[dict]:
    seen = seen if seen is not None else set()
    deduped = []
    for task in tasks or []:
        key = _task_key(task)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(task)
    return deduped


def _load_focus_records(capacity_records):
    if capacity_records is not None:
        return capacity_records
    try:
        from task_records import load_records

        _, _, records = load_records(personal=False)
        return records
    except Exception:
        return []


def _daily_top_priorities(records, *, reference_date: date) -> list[dict]:
    try:
        proposal = propose_defended_three(records or [], reference_date=reference_date.isoformat())
    except Exception:
        return []
    return proposal.defended[:3]


def _find_task_by_id(tasks_data: dict, task_id: str | None) -> dict | None:
    if not task_id:
        return None
    for task in tasks_data.get("all", []) or []:
        if task.get("task_id") == task_id or task.get("legacy_id") == task_id:
            return task
    for section in ("q1", "q2", "q3", "due_today", "team"):
        for task in tasks_data.get(section, []) or []:
            if task.get("task_id") == task_id or task.get("legacy_id") == task_id:
                return task
    return None


def _render_daily_top_priorities(lines: list[str], rows: list[dict], *, indent: str = "  ") -> None:
    if not rows:
        return
    lines.append("🎯 **Daily Top Priorities:**")
    for row in rows[:3]:
        est_minutes = int(row.get("estimate_minutes") or 0)
        est = f" ({format_duration(est_minutes)})" if est_minutes > 0 else ""
        escalated = " 🔺" if row.get("escalated") else ""
        lines.append(f"{indent}{row.get('position', len(lines))}. {row.get('title')}{escalated}{est}")
    lines.append("")


# --- U8 deterministic cron descriptor (CODE-ONLY -- no live registration) -------
#
# The 8am morning standup runs the task-tracker ``daily`` command as a DETERMINISTIC
# command cron (``payload.kind == "command"``), replacing the legacy Lobster
# ``Daily Interactive Work Standup`` agentTurn. This is the deterministic-standup half
# of R2: the entry the 8am cron runs is the same ``telegram-commands.sh daily`` a user
# runs by hand -- no LLM relay. The descriptor below is a CODE-ONLY TEMPLATE the operator
# hands to ``openclaw cron add``; nothing here registers a live cron, edits
# ``openclaw.json``, or restarts the gateway (the cron swap + the legacy-cron deletion
# are DEFERRED OPERATOR steps, gated on the U8 parity check). The env-var NAMES (not
# values) are embedded so the operator resolves the live target at registration time --
# no real chat id is committed (public-repo hygiene).

# The 8am morning-standup command-cron HOUR (local).
STANDUP_CRON_HOUR = 8

# The standup announces to the Productivity STANDUP thread (topic 2) -- the working
# standup surface, NOT the DONE thread the EOD posts to. Env-var NAMES only.
STANDUP_CHAT_ID_ENV = "TELEGRAM_CHAT_ID_PRODUCTIVITY"
STANDUP_TOPIC_ID_ENV = "OPENCLAW_TOPIC_PRODUCTIVITY_STANDUP"


def standup_cron_descriptor(
    *, chat_id_env: str = STANDUP_CHAT_ID_ENV, topic_env: str = STANDUP_TOPIC_ID_ENV,
    scripts_dir: str = "/data/.openclaw/skills/task-tracker/scripts",
) -> dict:
    """The deterministic-command-cron descriptor for the 8am standup (CODE-ONLY template).

    Mirrors the U4-nag / U7-EOD cron shape: ``payload.kind == "command"`` (a deterministic
    argv, NOT an LLM agentTurn), running ``telegram-commands.sh daily`` in the skill's
    scripts dir, with ``delivery.mode == "announce"`` to the Productivity STANDUP thread.
    This is a TEMPLATE the operator hands to ``openclaw cron add``; nothing here registers
    a live cron, edits ``openclaw.json``, or restarts the gateway (a deferred OPERATOR
    step, gated on the parity check). The env-var NAMES (not values) are embedded so the
    operator resolves the live target at registration time -- no real chat id is committed.
    """
    return {
        "schedule": {"kind": "daily", "hour": STANDUP_CRON_HOUR, "minute": 0},
        "payload": {
            "kind": "command",
            "argv": [
                "sh", "-c",
                f"cd {scripts_dir} && bash telegram-commands.sh daily",
            ],
        },
        "delivery": {
            "mode": "announce",
            "chat_id_env": chat_id_env,
            "topic_env": topic_env,
        },
    }


def group_by_area(tasks):
    """Group tasks by area (falls back to department for objectives format tasks)."""
    areas = {}
    for t in tasks:
        area = t.get('area') or t.get('department') or 'Uncategorized'
        if area not in areas:
            areas[area] = []
        areas[area].append(t)
    return areas


def _identity_fields(task: dict) -> dict:
    raw_line = str(task.get("raw_line") or "")
    line_number = task.get("line_number")
    try:
        line_number_int = int(line_number) if line_number else None
    except (TypeError, ValueError):
        line_number_int = None
    task_id = task.get("task_id") or task.get("legacy_id")
    fallback_id = fallback_id_for(raw_line, line_number_int) if raw_line else None
    return {
        "task_id": task_id,
        "fallback_id": fallback_id,
        "missing_task_id": task.get("task_id") is None,
        "fallback_only": task_id is None,
    }


def _standup_harvest_result(
    date_str: str | None,
    *,
    trigger: str,
    resolved_window: harvest_window.HarvestWindow | None = None,
) -> dict:
    try:
        return standup_harvest.harvest(
            target_date=date_str,
            trigger=trigger,
            resolved_window=resolved_window,
        )
    except Exception as exc:  # noqa: BLE001 -- standup must degrade, not blank
        error_envelope.log_degraded("standup:harvest", exc, trigger=trigger, check="evidence_harvest")
        return {"evidence_candidates": [], "health": {"status": "failed"}, "window": None}


def _candidate_task_suffix(candidate: dict) -> str:
    task_id = candidate.get("matched_task_id") or candidate.get("suggested_task_id")
    return f" -> {task_id}" if task_id else ""


def _completed_board_items(items: list[dict]) -> list[dict]:
    completed: list[dict] = []
    for item in items:
        copied = dict(item)
        if "meeting::" in str(copied.get("raw_line") or ""):
            copied["is_calendar_meeting"] = True
        completed.append(copied)
    return completed


def _draft_summary_lines(summary: dict | None, *, indent: str = "  ") -> list[str]:
    if not summary or not summary.get("bullets"):
        return []
    lines = ["**Draft summary (unconfirmed):**"]
    disclosure = summary.get("disclosure")
    if disclosure:
        lines.append(f"{indent}_{disclosure}_")
    grouped: dict[str, list[dict]] = {}
    for bullet in summary.get("bullets") or []:
        area = str(bullet.get("area") or "unclassified")
        grouped.setdefault(area, []).append(bullet)
    for area in sorted(grouped):
        lines.append(f"{indent}{area}:")
        for bullet in grouped[area]:
            lines.append(f"{indent}  • {bullet.get('bullet')}")
    lines.append(f"{indent}Read-only draft; not recorded as completed.")
    return lines


def format_split_standup(output: dict, date_display: str) -> list:
    """Format standup as 3 separate messages.
    
    Returns list of 3 strings:
    1. Completed items (by category)
    2. Calendar events
    3. Active todos (by priority + category)
    """
    messages = []
    
    # Message 1: Completed items
    msg1_lines = [f"✅ **Completed — {date_display}**\n"]
    if output['completed']:
        by_area = group_by_area(output['completed'])
        for cat in sorted(by_area.keys()):
            msg1_lines.append(f"**{cat}:**")
            for t in by_area[cat]:
                rec = recurrence_suffix(t)
                msg1_lines.append(f"  • {t['title']}{rec}")
            msg1_lines.append("")
    else:
        msg1_lines.append("_No completed items_")
    if output.get("evidence_candidates"):
        msg1_lines.append("")
        msg1_lines.append("**Evidence candidates:**")
        for candidate in output["evidence_candidates"][:5]:
            msg1_lines.append(
                f"  • {candidate.get('title')}{_candidate_task_suffix(candidate)}"
            )
    summary_lines = _draft_summary_lines((output.get("evidence_harvest") or {}).get("summary"))
    if summary_lines:
        msg1_lines.append("")
        msg1_lines.extend(summary_lines)
    messages.append('\n'.join(msg1_lines).strip())
    
    # Message 2: Calendar events
    msg2_lines = [f"📅 **Calendar — {date_display}**\n"]
    cal_err = calendar_error(output['calendar'])
    all_events = flatten_calendar_events(output['calendar'])
    if cal_err:
        msg2_lines.append(error_envelope.degraded_notice("Calendar"))
    elif all_events:
        for event in all_events:
            time_str = format_time(event['start'])
            msg2_lines.append(f"• {time_str} — {event['summary']}")
    else:
        msg2_lines.append("_No calendar events today_")
    messages.append('\n'.join(msg2_lines).strip())
    
    # Message 3: Active todos
    msg3_lines = [f"📋 **Todos — {date_display}**\n"]

    msg3_lines.append(output.get('tomorrow_pointer_line') or "🎯 **No #1 set — pick one today.**")
    msg3_lines.append("")

    rendered_seen: set[tuple[str, str]] = set()
    pointer_state = output.get("tomorrow_pointer_state") or {}
    if pointer_state.get("task_id"):
        rendered_seen.add(("task_id", str(pointer_state["task_id"])))

    top_priorities = _dedupe_tasks(output.get("daily_top_priorities") or [], seen=rendered_seen)
    _render_daily_top_priorities(msg3_lines, top_priorities)
    
    # Due today
    due_today = _dedupe_tasks(output['due_today'], seen=rendered_seen)
    if due_today:
        msg3_lines.append("⏰ **Due Today:**")
        for t in due_today:
            rec = recurrence_suffix(t)
            msg3_lines.append(f"  • {t['title']}{rec}")
        msg3_lines.append("")
    
    # Q2 - Important, Not Urgent
    q2 = _dedupe_tasks(output.get('q2') or [], seen=rendered_seen)
    if q2:
        msg3_lines.append("🟡 **Important, Not Urgent (Q2):**")
        by_area = group_by_area(q2)
        for area in sorted(by_area.keys()):
            msg3_lines.append(f"  **{area}:**")
            for t in by_area[area]:
                due_str = f" (🗓️{t['due']})" if t.get('due') else ""
                rec = recurrence_suffix(t)
                msg3_lines.append(f"    • {t['title']}{due_str}{rec}")
        msg3_lines.append("")
    
    # Q3 - Waiting/Blocked
    q3 = _dedupe_tasks(output.get('q3') or [], seen=rendered_seen)
    if q3:
        msg3_lines.append("🟠 **Waiting/Blocked (Q3):**")
        for t in q3:
            blocks_str = f" → {t['blocks']}" if t.get('blocks') else ""
            esc = escalation_suffix(t)
            rec = recurrence_suffix(t)
            msg3_lines.append(f"  • {t['title']}{blocks_str}{esc}{rec}")
        msg3_lines.append("")
    
    # Team tasks
    team = _dedupe_tasks(output.get('team') or [], seen=rendered_seen)
    if team:
        msg3_lines.append("👥 **Team Tasks:**")
        for t in team:
            owner_str = f" ({t['owner']})" if t.get('owner') else ""
            rec = recurrence_suffix(t)
            msg3_lines.append(f"  • {t['title']}{rec}{owner_str}")
        msg3_lines.append("")

    objective_progress = output.get('objective_progress') or {}
    if objective_progress.get('total_objectives', 0) > 0:
        msg3_lines.append("🎯 **Objective Progress:**")
        msg3_lines.append(
            "  • On track: "
            f"{objective_progress['on_track_objectives']}/{objective_progress['total_objectives']}"
        )
        at_risk = objective_progress.get('at_risk_objectives', [])
        if at_risk:
            msg3_lines.append("  • At risk (0%):")
            for objective in at_risk:
                msg3_lines.append(f"    • {objective['title']}")
        else:
            msg3_lines.append("  • At risk (0%): none")
    
    messages.append('\n'.join(msg3_lines).strip())
    
    return messages


def _build_daily_note_links(anchor_date: str | None = None) -> dict:
    """Build Obsidian universal+deep links for standup date and previous day."""
    notes_dir = os.getenv("TASK_TRACKER_DAILY_NOTES_DIR")
    vault = os.getenv("TASK_TRACKER_OBSIDIAN_VAULT", "Obsidian")
    if not notes_dir:
        return {}

    relative_dir = os.getenv("TASK_TRACKER_DAILY_NOTES_RELATIVE_DIR", "01-TODOs/Daily").strip("/")

    from datetime import date
    import cos_config
    # Local (Pacific) day, not the container's UTC day, so the fallback daily-note
    # name matches the user's calendar day at Pacific evening.
    base_date = cos_config.local_today()
    if anchor_date:
        try:
            base_date = date.fromisoformat(anchor_date)
        except ValueError:
            pass

    def mk_link(day_offset: int) -> dict:
        note_date = base_date + timedelta(days=day_offset)
        note_name = f"{note_date.isoformat()}.md"
        rel_path = f"{relative_dir}/{note_name}"
        encoded_vault = quote(vault, safe="")
        encoded_file = quote(rel_path, safe="")
        return {
            "universal": f"https://obsidian.md/open?vault={encoded_vault}&file={encoded_file}",
            "deep": f"obsidian://open?vault={encoded_vault}&file={encoded_file}",
        }

    return {
        "today_daily_note": mk_link(0),
        "yesterday_daily_note": mk_link(-1),
    }


def build_compact_standup_sections(output: dict) -> dict:
    """Compact standup payload schema v1 for automation clients."""
    done = [t.get('title', '') for t in (output.get('completed') or [])[:12]]
    calendar_dos = [
        {
            "quick_id": f"c{idx}",
            "title": t.get('title', ''),
            "status": "scheduled",
            **_identity_fields(t),
        }
        for idx, t in enumerate(output.get('due_today') or [], start=1)
    ]

    completed = output.get('completed') or []
    calendar_dones = [
        {
            "quick_id": f"cd{idx}",
            "title": t.get('title', ''),
            "status": "done",
            **_identity_fields(t),
        }
        for idx, t in enumerate(
            [
                t for t in completed
                if t.get("is_calendar_meeting") or "meeting::" in str(t.get("raw_line") or "")
            ],
            start=1,
        )
    ]

    dos = []
    stack = (
        [("q1", t) for t in (output.get('q1') or [])]
        + [("q2", t) for t in (output.get('q2') or [])]
        + [("q3", t) for t in (output.get('q3') or [])]
    )
    for idx, (section, t) in enumerate(stack[:20], start=1):
        dos.append(
            {
                "quick_id": f"d{idx}",
                "title": t.get('title', ''),
                "section": section,
                **_identity_fields(t),
            }
        )

    return {
        "schema_version": "1",
        "dones": done,
        "calendar_dos": calendar_dos,
        "calendar_dones": calendar_dones,
        "dos": dos,
        "completion_candidates": output.get("completion_candidates") or {},
        "evidence_candidates": output.get("evidence_candidates") or [],
        "links": _build_daily_note_links(output.get("date")),
    }


def generate_standup(
    date_str: str = None,
    json_output: bool = False,
    split_output: bool = False,
    tasks_data: dict | None = None,
    notes_dir: Path | None = None,
    capacity_records=None,
) -> str | dict | list:
    """Generate daily standup summary.

    Args:
        date_str: Optional date string (YYYY-MM-DD) for standup
        json_output: If True, return dict instead of markdown
        notes_dir: Path to daily notes directory for completion data

    Returns:
        String summary (default) or dict if json_output=True
    """
    if tasks_data is None:
        _, tasks_data = load_tasks()

    # Only a VALID explicit date selects an explicit (target-date) window; a missing
    # OR malformed date_str falls through to the implicit prior-workday standup window
    # (a typo must NOT silently retarget the evidence window to today).
    requested_date: date | None = None
    if date_str:
        try:
            requested_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            requested_date = None
    standup_window = harvest_window.resolve_standup_window(target_date=requested_date)
    standup_date = standup_window.plan_date
    date_display = standup_date.strftime("%A, %B %d")

    # Build output using new task structure (q1, q2, q3, team, backlog)
    output = {
        'date': str(standup_date),
        'date_display': date_display,
        'week_id': standup_window.week_id,
        'window_id': standup_window.window_id,
        'standup_window': standup_window.as_dict(),
        'calendar': get_calendar_events(trigger="user_command:/standup"),
        'priority': None,
        'tomorrow_pointer_state': {},
        'tomorrow_pointer_line': "",
        'daily_top_priorities': [],
        'due_today': [],
        'q1': [],  # Urgent & Important
        'q2': [],  # Important, Not Urgent
        'q3': [],  # Waiting/Blocked
        'team': [],  # Team tasks to monitor
        'completed': [],
        'evidence_candidates': [],
        'evidence_harvest': {},
        'objective_progress': {},
        'capacity': None,
        'capacity_pushback': None,
    }

    # Layer-2 capacity ceiling line (U3): estimate-sum over active work vs ~1
    # week of capacity. Degrades to None on any failure rather than breaking the
    # standup. Skipped for personal standups (the cap governs the work board).
    output['capacity'] = capacity_line(records=capacity_records)
    # U9: deterministic capacity push-back (rules only). When the active board is over
    # the Layer-2 ceiling, surface the most-overdue candidates + a cut/defer/edit ask.
    # Pure read + render: never chooses, mutates the board, or sends. None when within cap.
    output['capacity_pushback'] = pushback.capacity_pushback(capacity_records)

    # Apply display-only priority escalation
    regrouped = regroup_by_effective_priority(tasks_data, reference_date=standup_date)

    focus_records = _load_focus_records(capacity_records)

    # U6: the only #1 is the EOD-set tomorrow pointer resolved against the live
    # board. When absent/stale, the standup prompts for a fresh pick and does not
    # invent a second #1 from Q1 escalation.
    pointer_state = tomorrow_pointer_state(records=focus_records)
    output['tomorrow_pointer_state'] = pointer_state
    output['tomorrow_pointer_line'] = pointer_state["line"]
    output['priority'] = _find_task_by_id(tasks_data, pointer_state.get("task_id"))
    output['daily_top_priorities'] = _daily_top_priorities(
        focus_records,
        reference_date=standup_date,
    )

    # Due today
    output['due_today'] = _dedupe_tasks(tasks_data.get('due_today', []))

    # Q1 - Urgent & Important (includes escalated tasks)
    output['q1'] = _dedupe_tasks(regrouped['q1'])

    # Q2 - Important, Not Urgent
    output['q2'] = _dedupe_tasks(regrouped['q2'])

    # Q3 - Waiting/Blocked (includes escalated tasks)
    output['q3'] = _dedupe_tasks(regrouped['q3'])

    # Team tasks
    output['team'] = _dedupe_tasks(tasks_data.get('team', []))

    harvest_result = _standup_harvest_result(
        str(requested_date) if requested_date else None,
        trigger="user_command:/standup",
        resolved_window=standup_window,
    )
    evidence_candidates = harvest_result.get("evidence_candidates") or []

    # Completed: confirmed user claims only; evidence can enrich but never promote.
    yesterday = standup_date - timedelta(days=1)
    if notes_dir:
        notes_completed = extract_completed_tasks(
            notes_dir=notes_dir,
            start_date=yesterday,
            end_date=standup_date,
        )
        user_stated = [*notes_completed, *_completed_board_items(tasks_data.get('done', []))]
    else:
        user_stated = _completed_board_items(tasks_data.get('done', []))

    output['completed'], output['evidence_candidates'] = reconcile.merge(
        user_stated,
        evidence_candidates,
    )
    output['evidence_harvest'] = {
        "health": harvest_result.get("health") or {},
        "window": harvest_result.get("window") or standup_window.as_dict(),
        "run_id": harvest_result.get("run_id"),
        "summary": harvest_result.get("summary"),
    }

    output['objective_progress'] = summarize_objective_progress(tasks_data)
    output['completion_candidates'] = candidate_review_summary()
    output['task_audit'] = task_audit_summary(limit=3)

    if json_output:
        return output
    
    if split_output:
        return format_split_standup(output, date_display)
    
    # Format as markdown (single message)
    lines = [f"📋 **Daily Standup — {date_display}**\n"]

    # U8: open with tomorrow's #1 (set the prior evening at the EOD), the first content
    # line of the standup -- the read side of the daily loop.
    lines.append(output['tomorrow_pointer_line'])
    lines.append("")

    rendered_seen: set[tuple[str, str]] = set()
    pointer_state = output.get("tomorrow_pointer_state") or {}
    if pointer_state.get("task_id"):
        rendered_seen.add(("task_id", str(pointer_state["task_id"])))

    # Calendar events
    cal_err = calendar_error(output['calendar'])
    all_events = flatten_calendar_events(output['calendar'])
    if cal_err:
        lines.append(error_envelope.degraded_notice("Calendar"))
        lines.append("")
    elif all_events:
        lines.append("📅 **Today's Calendar:**")
        for event in all_events:
            time_str = format_time(event['start'])
            lines.append(f"  • {time_str} — {event['summary']}")
        lines.append("")

    top_priorities = _dedupe_tasks(output.get("daily_top_priorities") or [], seen=rendered_seen)
    _render_daily_top_priorities(lines, top_priorities)

    if output.get('capacity'):
        lines.append(output['capacity'])
        if output.get('capacity_pushback'):
            lines.append(output['capacity_pushback'])
        lines.append("")

    due_today = _dedupe_tasks(output['due_today'], seen=rendered_seen)
    if due_today:
        total_est = sum(parse_duration(t.get('estimate')) for t in due_today)
        est_str = f" [{format_duration(total_est)}]" if total_est > 0 else ""
        lines.append(f"⏰ **Due Today:{est_str}**")
        for t in due_today:
            rec = recurrence_suffix(t)
            est = f" ({t['estimate']})" if t.get('estimate') else ""
            lines.append(f"  • {t['title']}{rec}{est}")
        lines.append("")

    # Q2 - Important, Not Urgent
    q2 = _dedupe_tasks(output['q2'], seen=rendered_seen)
    if q2:
        total_est = sum(parse_duration(t.get('estimate')) for t in q2)
        est_str = f" [{format_duration(total_est)}]" if total_est > 0 else ""
        lines.append(f"🟡 **Important, Not Urgent (Q2):{est_str}**")
        by_area = group_by_area(q2)
        for cat in sorted(by_area.keys()):
            lines.append(f"  **{cat}:**")
            for t in by_area[cat]:
                due_str = f" (🗓️{t['due']})" if t.get('due') else ""
                rec = recurrence_suffix(t)
                est = f" ({t['estimate']})" if t.get('estimate') else ""
                dep = dependency_suffix(t)
                spr = sprint_suffix(t)
                lines.append(f"    • {t['title']}{due_str}{rec}{est}{dep}{spr}")
        lines.append("")
    
    # Q3 - Waiting/Blocked
    q3 = _dedupe_tasks(output['q3'], seen=rendered_seen)
    if q3:
        lines.append("🟠 **Waiting/Blocked (Q3):**")
        for t in q3:
            blocks_str = f" → {t['blocks']}" if t.get('blocks') else ""
            esc = escalation_suffix(t)
            rec = recurrence_suffix(t)
            dep = dependency_suffix(t)
            lines.append(f"  • {t['title']}{blocks_str}{esc}{rec}{dep}")
        lines.append("")
    
    # Team tasks
    team = _dedupe_tasks(output['team'], seen=rendered_seen)
    if team:
        lines.append("👥 **Team Tasks:**")
        for t in team:
            owner_str = f" ({t['owner']})" if t.get('owner') else ""
            rec = recurrence_suffix(t)
            lines.append(f"  • {t['title']}{rec}{owner_str}")
        lines.append("")
    
    if output['completed']:
        lines.append(f"✅ **Recently Completed:** ({len(output['completed'])} items)")
        for t in output['completed'][:5]:  # Limit to 5
            rec = recurrence_suffix(t)
            lines.append(f"  • {t['title']}{rec}")
        if len(output['completed']) > 5:
            lines.append(f"  • ... and {len(output['completed']) - 5} more")

    if output.get('evidence_candidates'):
        lines.append("")
        lines.append(f"🧾 **Evidence Candidates:** ({len(output['evidence_candidates'])} items)")
        for candidate in output['evidence_candidates'][:5]:
            status = candidate.get("association_status") or candidate.get("decision")
            lines.append(
                f"  • [{candidate.get('source')}] {candidate.get('title')}"
                f"{_candidate_task_suffix(candidate)} ({status})"
            )
        if len(output['evidence_candidates']) > 5:
            lines.append(f"  • ... and {len(output['evidence_candidates']) - 5} more")

    summary_lines = _draft_summary_lines((output.get("evidence_harvest") or {}).get("summary"))
    if summary_lines:
        lines.append("")
        lines.extend(summary_lines)

    candidates = output.get('completion_candidates') or {}
    if candidates.get('review_required'):
        lines.append("")
        lines.append(f"🧾 **Completion Candidates:** {candidates.get('total', 0)} review required")
        for candidate in candidates.get('items', [])[:3]:
            task_hint = candidate.get('confirmable_task_id') or candidate.get('suggested_task_id')
            suffix = f" → {task_hint}" if task_hint else ""
            lines.append(f"  • {candidate.get('candidate_id')}: {candidate.get('summary')}{suffix}")
        if candidates.get('overflow'):
            lines.append(f"  • ... and {candidates['overflow']} more")
        lines.append("  Review required; do not auto-complete from this summary.")

    audit = output.get('task_audit') or {}
    if audit.get('review_required') and not audit.get('available', True):
        error = audit.get('error') or {}
        lines.append("")
        lines.append(f"🧹 **Task Audit:** unavailable ({error.get('code', 'unknown-error')})")
    elif audit.get('review_required'):
        lines.append("")
        lines.append(f"🧹 **Task Audit:** {audit.get('total', 0)} finding(s) need review")
        for finding in audit.get('items', [])[:3]:
            lines.append(f"  • {finding.get('severity')}: {finding.get('code')} — {finding.get('reason')}")
        if audit.get('overflow'):
            lines.append(f"  • ... and {audit['overflow']} more")
        lines.append("  Run `tasks.py task-audit`; do not mutate from audit text.")

    objective_progress = output.get('objective_progress') or {}
    if objective_progress.get('total_objectives', 0) > 0:
        lines.append("")
        lines.append("🎯 **Objective Progress:**")
        lines.append(
            "  • On track: "
            f"{objective_progress['on_track_objectives']}/{objective_progress['total_objectives']}"
        )
        at_risk = objective_progress.get('at_risk_objectives', [])
        if at_risk:
            lines.append("  • At risk (0%):")
            for objective in at_risk:
                lines.append(f"    • {objective['title']}")
        else:
            lines.append("  • At risk (0%): none")

    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description='Generate daily standup summary')
    parser.add_argument('--date', help='Date for standup (YYYY-MM-DD)')
    parser.add_argument('--json', action='store_true', help='Output as JSON')
    parser.add_argument('--split', action='store_true', help='Split into 3 messages (completed/calendar/todos)')
    parser.add_argument('--skip-missed', action='store_true', help='Skip missed tasks section')
    parser.add_argument('--compact-json', action='store_true', help='Output compact DONEs/Calendar DOs/DOs JSON')

    args = parser.parse_args()

    _, tasks_data = load_tasks()
    missed_buckets = None
    if not args.skip_missed:
        missed_buckets = get_missed_tasks_bucketed(tasks_data, reference_date=args.date)

    notes_dir_raw = os.getenv("TASK_TRACKER_DAILY_NOTES_DIR")
    notes_dir = Path(notes_dir_raw) if notes_dir_raw else None

    result = generate_standup(
        date_str=args.date,
        json_output=(args.json or args.compact_json),
        split_output=args.split,
        tasks_data=tasks_data,
        notes_dir=notes_dir,
    )

    missed_block = ""
    if not (args.json or args.compact_json):
        missed_block = format_missed_tasks_block(missed_buckets)
        if missed_block:
            if args.split:
                result = [f"{missed_block}{result[0]}"] + result[1:]
            else:
                result = f"{missed_block}{result}"

    if args.compact_json:
        print(json.dumps(build_compact_standup_sections(result), indent=2))
    elif args.json:
        print(json.dumps(result, indent=2))
    elif args.split:
        for i, msg in enumerate(result, 1):
            print(msg)
            if i < len(result):
                print("\n---\n")
    else:
        print(result)


if __name__ == '__main__':
    sys.exit(error_envelope.run_main("standup", main))
