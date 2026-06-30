#!/usr/bin/env python3
"""
Weekly Review Generator - Summarizes a completed or in-progress ISO week.
"""

import argparse
import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import error_envelope
from candidate_review import candidate_review_summary
from task_audit import task_audit_summary
from daily_notes import extract_completed_actions, extract_completed_tasks
from task_lines import remove_task_line
from utils import (
    get_tasks_file,
    ARCHIVE_DIR,
    get_current_quarter,
    get_missed_tasks_bucketed,
    effective_priority,
    load_tasks,
)


def _parse_archive_weeks(archive_dir: Path) -> dict[str, list[str]]:
    """Parse all archive files and return tasks grouped by ISO week.

    LIMITATION: Archives store task titles but not their original completion
    dates. Tasks are grouped by the archive header date (when they were
    archived), not their actual completion date. This means late archiving
    or archiving backlog tasks may misattribute completions to the wrong week.

    Returns:
        dict mapping ISO week labels (e.g. "2026-W06") to lists of task titles
        found under each "## Week of YYYY-MM-DD" or "## Archived ... (Work)"
        header.
    """
    weeks: dict[str, list[str]] = {}
    if not archive_dir.exists() or not archive_dir.is_dir():
        return weeks

    for archive_file in sorted(archive_dir.glob("ARCHIVE-*.md")):
        try:
            content = archive_file.read_text(encoding='utf-8')
        except (PermissionError, OSError, UnicodeDecodeError):
            continue

        current_header_week: str | None = None
        for line in content.splitlines():
            # Match both archive header formats (work only):
            #   "## Week of YYYY-MM-DD"          (weekly_review.py archive — always work)
            #   "## Archived YYYY-MM-DD (Work)"   (tasks.py archive — explicit work label)
            # Excludes "## Archived YYYY-MM-DD (Personal)" to avoid inflating work metrics.
            header_match = re.match(
                r'^## (?:Week of\s+(\d{4}-\d{2}-\d{2})|Archived\s+(\d{4}-\d{2}-\d{2})\s+\(Work\))',
                line,
            )
            if header_match:
                date_str = header_match.group(1) or header_match.group(2)
                try:
                    week_date = datetime.strptime(date_str, '%Y-%m-%d').date()
                    iso_year, iso_week, _ = week_date.isocalendar()
                    current_header_week = f"{iso_year}-W{iso_week:02d}"
                except ValueError:
                    current_header_week = None
                continue

            # Reset on any other ## header to avoid misattribution
            if line.startswith('## '):
                current_header_week = None
                continue

            # Look for completed task line: - ✅ **Title** ... ✅ YYYY-MM-DD
            task_match = re.match(r'^- ✅ \*\*(.+?)\*\*(.*)$', line)
            if task_match:
                title = task_match.group(1).strip()
                rest = task_match.group(2)
                
                # Priority: use completion timestamp if present (more accurate)
                # Fallback: use header week (archive date)
                task_week = current_header_week
                completed_match = re.search(r'✅\s*(\d{4}-\d{2}-\d{2})\s*$', rest)
                if completed_match:
                    try:
                        c_date = datetime.strptime(completed_match.group(1), '%Y-%m-%d').date()
                        iso_year, iso_week, _ = c_date.isocalendar()
                        task_week = f"{iso_year}-W{iso_week:02d}"
                    except ValueError:
                        pass

                if task_week:
                    if task_week not in weeks:
                        weeks[task_week] = []
                    weeks[task_week].append(title)

    return weeks


def _count_completed_in_range(
    tasks: list[dict], start: date, end: date
) -> int:
    """Count tasks whose completed_date falls within [start, end]."""
    return len(_completed_in_range(tasks, start, end))


def _completed_in_range(tasks: list[dict], start: date, end: date) -> list[dict]:
    """Return tasks whose completed_date falls within [start, end]."""
    completed_tasks = []
    for task in tasks:
        if _completion_date_in_range(task, start, end):
            completed_tasks.append(task)
    return completed_tasks


def _completion_date_in_range(task: dict, start: date, end: date) -> bool:
    """Return True when task.completed_date is a valid date inside [start, end]."""
    cd = task.get('completed_date')
    if not cd:
        return False
    try:
        completed = datetime.strptime(cd, '%Y-%m-%d').date()
    except ValueError:
        return False
    return start <= completed <= end


def generate_velocity_section(
    tasks_data: dict,
    week_start: date,
    week_end: date,
    archive_dir: Path,
    notes_dir: Path | None = None,
    completed_tasks: list[dict] | None = None,
) -> list[str]:
    """Generate the 📊 Velocity section lines.

    The current week count comes from the same windowed completion set used by
    the Completed This Week section. Trend data still uses daily notes when
    available and archive data otherwise.
    """
    lines: list[str] = []
    completed_this_week = len(completed_tasks) if completed_tasks is not None else None

    if notes_dir:
        if completed_this_week is None:
            completed_this_week = len(extract_completed_tasks(notes_dir, week_start, week_end))

        # Build 4-week rolling trend from daily notes
        trend_counts: list[int] = []
        for i in range(3, 0, -1):
            trend_start = week_start - timedelta(weeks=i)
            trend_end = trend_start + timedelta(days=6)
            trend_tasks = extract_completed_tasks(notes_dir, trend_start, trend_end)
            trend_counts.append(len(trend_tasks))
    else:
        # Fallback: archive data for trend
        archive_weeks = _parse_archive_weeks(archive_dir)

        if completed_this_week is None:
            all_tasks = tasks_data.get('done', [])
            live_completed = _count_completed_in_range(all_tasks, week_start, week_end)

            iso_year_cur, iso_week_cur, _ = week_start.isocalendar()
            current_label = f"{iso_year_cur}-W{iso_week_cur:02d}"
            current_archive_count = len(archive_weeks.get(current_label, []))

            completed_this_week = live_completed + current_archive_count

        trend_counts = []
        for i in range(3, 0, -1):
            trend_start = week_start - timedelta(weeks=i)
            iso_year, iso_week, _ = trend_start.isocalendar()
            label = f"{iso_year}-W{iso_week:02d}"
            trend_counts.append(len(archive_weeks.get(label, [])))

    current_week_count = completed_this_week

    lines.append("")
    lines.append("📊 **Velocity**")
    lines.append(f"  Completed: {completed_this_week} task{'s' if completed_this_week != 1 else ''}")

    # 4-week trend (3 previous weeks + current)
    full_trend = trend_counts + [current_week_count]
    if any(c > 0 for c in full_trend):
        trend_str = " → ".join(str(c) for c in full_trend)
        lines.append(f"  4-week trend: {trend_str}")
    else:
        lines.append("  4-week trend: — (no archive data yet)")

    lines.append("")
    return lines


def _archive_to_quarterly(done_tasks: list[dict]) -> str | None:
    """Write completed tasks to quarterly archive file.

    Idempotent: skips tasks already present in the archive (matched by
    title + completion date) so re-running is safe.

    Returns the archive filename on success, None if nothing to archive.
    """
    if not done_tasks:
        return None

    quarter = get_current_quarter()
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    archive_file = ARCHIVE_DIR / f"ARCHIVE-{quarter}.md"

    if archive_file.exists():
        archive_content = archive_file.read_text()
    else:
        archive_content = f"# Task Archive - {quarter}\n"

    # Build set of (title, completed_date) already archived
    already_archived: set[tuple[str, str]] = set()
    for line in archive_content.splitlines():
        m = re.match(r'^- ✅ \*\*(.+?)\*\*', line)
        if m:
            title_key = m.group(1).strip().casefold()
            date_m = re.search(r'✅\s*(\d{4}-\d{2}-\d{2})\s*$', line)
            date_key = date_m.group(1) if date_m else ''
            already_archived.add((title_key, date_key))

    new_tasks = [
        t for t in done_tasks
        if (t['title'].casefold(), t.get('completed_date') or '') not in already_archived
    ]
    if not new_tasks:
        return None

    archive_entry = f"\n## Week of {datetime.now().strftime('%Y-%m-%d')}\n\n"
    for task in new_tasks:
        date_suffix = f" ✅ {task['completed_date']}" if task.get('completed_date') else ""
        area_suffix = f" [{task.get('area')}]" if task.get('area') else ""
        archive_entry += f"- ✅ **{task['title']}**{area_suffix}{date_suffix}\n"

    archive_content += archive_entry
    archive_file.write_text(archive_content)
    return archive_file.name


def _clean_stale_done_lines(tasks_file: Path, done_tasks: list[dict]) -> int:
    """Remove stale [x] lines from the board. Returns count removed."""
    if not done_tasks or not tasks_file.exists():
        return 0

    content = tasks_file.read_text()
    removed = 0
    for task in sorted(done_tasks, key=lambda item: item.get('line_number') or 0, reverse=True):
        raw_line = task.get('raw_line', '')
        line_number = task.get('line_number')
        updated = remove_task_line(content, raw_line, line_number)
        if updated is not None:
            content = updated
            removed += 1

    if removed:
        tasks_file.write_text(content)
    return removed


def _board_done_tasks_with_provenance(done_tasks: list[dict]) -> list[dict]:
    """Return archived done tasks that can be safely removed from the board."""
    return [
        task for task in done_tasks
        if task.get('raw_line') and task.get('line_number')
    ]


def group_by_area(tasks: list[dict]) -> dict[str, list[dict]]:
    """Group tasks by area."""
    areas: dict[str, list[dict]] = {}
    for t in tasks:
        area = t.get('area') or 'Uncategorized'
        if area not in areas:
            areas[area] = []
        areas[area].append(t)
    return areas


def parse_iso_week(
    week: str | None,
    *,
    this_week: bool = False,
    today: date | None = None,
) -> tuple[date, date]:
    """Parse ISO week string (YYYY-WNN) into start/end dates."""
    # today= is a test-injection point; runtime callers leave it unset.
    today = today or datetime.now().date()
    if not week:
        anchor = today if this_week else today - timedelta(days=7)
        week_start = anchor - timedelta(days=anchor.weekday())
        return week_start, week_start + timedelta(days=6)

    match = re.fullmatch(r'(\d{4})-W(\d{2})', week)
    if not match:
        raise ValueError("Invalid --week format. Use YYYY-WNN (example: 2026-W07).")

    year = int(match.group(1))
    week_num = int(match.group(2))
    try:
        week_start = date.fromisocalendar(year, week_num, 1)
    except ValueError as exc:
        raise ValueError(f"Invalid ISO week: {week}") from exc

    return week_start, week_start + timedelta(days=6)


def parse_due_date(due_str: str | None) -> date | None:
    """Parse YYYY-MM-DD date string."""
    if not due_str:
        return None
    try:
        return datetime.strptime(due_str, '%Y-%m-%d').date()
    except ValueError:
        return None


def format_area_grouped(
    lines: list[str],
    title: str,
    tasks: list[dict],
    formatter,
    empty_text: str,
) -> None:
    """Append a section grouped by area with counts."""
    lines.append(f"{title} ({len(tasks)})")
    if not tasks:
        lines.append(f"  _{empty_text}_")
        lines.append("")
        return

    grouped = group_by_area(tasks)
    for area in sorted(grouped.keys()):
        area_tasks = grouped[area]
        lines.append(f"  **{area} ({len(area_tasks)}):**")
        for task in area_tasks:
            lines.append(f"    • {formatter(task)}")
    lines.append("")


def flatten_missed_buckets(missed_buckets: dict) -> list[dict]:
    """Flatten missed buckets in severity order."""
    tasks: list[dict] = []
    for bucket in ['yesterday', 'last7', 'last30', 'older']:
        tasks.extend(missed_buckets.get(bucket, []))
    return tasks


def append_candidate_review_section(lines: list[str], limit: int = 5) -> None:
    lines.append("")
    lines.append("🧾 **Completion Candidates**")
    summary = candidate_review_summary(limit=limit)
    if not summary.get("available"):
        error = summary.get("error") or {}
        lines.append(f"  Review unavailable: {error.get('code', 'unknown-error')}.")
        for item in (error.get("malformed") or [])[:3]:
            lines.append(f"  • {item.get('path')}:{item.get('line_number')} {item.get('message')}")
        return

    if not summary.get("total"):
        lines.append("  No active completion candidates.")
        return

    lines.append(f"  {summary.get('total', 0)} candidate(s) need review.")
    for candidate in summary.get("items", []):
        task_hint = candidate.get("confirmable_task_id") or candidate.get("suggested_task_id")
        suffix = f" -> {task_hint}" if task_hint else ""
        lines.append(f"  • {candidate.get('candidate_id')}: {candidate.get('summary')}{suffix}")
    if summary.get("overflow"):
        lines.append(f"  • ... and {summary['overflow']} more")
    lines.append("  Review required; do not auto-complete from this summary.")


def append_task_audit_section(lines: list[str], limit: int = 5) -> None:
    lines.append("")
    lines.append("🧹 **Task Audit**")
    summary = task_audit_summary(limit=limit)
    if not summary.get("available"):
        error = summary.get("error") or {}
        lines.append(f"  Audit unavailable: {error.get('code', 'unknown-error')}.")
        return

    if not summary.get("total"):
        lines.append("  No task-health findings.")
        return

    lines.append(f"  {summary.get('total', 0)} finding(s) need review.")
    for finding in summary.get("items", []):
        lines.append(f"  • {finding.get('severity')}: {finding.get('code')} — {finding.get('reason')}")
    if summary.get("overflow"):
        lines.append(f"  • ... and {summary['overflow']} more")
    lines.append("  Run `tasks.py task-audit`; do not mutate from audit text.")


def format_overdue(task: dict, reference_date: date) -> str:
    """Return overdue label for a task."""
    due_date = parse_due_date(task.get('due'))
    if not due_date:
        return "due date unavailable"

    overdue_days = (reference_date - due_date).days
    day_word = "day" if overdue_days == 1 else "days"
    return f"{overdue_days} {day_word} overdue"


def extract_lessons(notes_dir: Path, start_date: date, end_date: date) -> list[str]:
    """Extract lesson and insight lines from dated daily notes."""
    if not notes_dir.exists() or not notes_dir.is_dir():
        return []

    lessons: list[str] = []
    for notes_file in sorted(notes_dir.glob("*.md")):
        match = re.fullmatch(r"(\d{4}-\d{2}-\d{2})\.md", notes_file.name)
        if not match:
            continue

        try:
            note_date = datetime.strptime(match.group(1), "%Y-%m-%d").date()
        except ValueError:
            continue

        if note_date < start_date or note_date > end_date:
            continue

        try:
            content = notes_file.read_text()
        except (PermissionError, UnicodeDecodeError, OSError):
            # Skip unreadable or non-UTF8 files silently
            continue

        for raw_line in content.splitlines():
            line = raw_line.strip()
            match_line = re.search(r"\b(?:lesson|insight)::\s*(.+)", line, flags=re.IGNORECASE)
            if match_line:
                lessons.append(match_line.group(1).strip())

    return lessons


def _merge_windowed_completed_tasks(
    notes_tasks: list[dict],
    board_done: list[dict],
    week_start: date,
    week_end: date,
) -> list[dict]:
    """Merge daily-note completions with board completions dated in the window."""
    done_tasks = list(notes_tasks)
    seen = {t['title'].casefold() for t in done_tasks}
    for board_task in _completed_in_range(board_done, week_start, week_end):
        title_key = board_task['title'].casefold()
        if title_key in seen:
            continue
        seen.add(title_key)
        done_tasks.append(board_task)
    return done_tasks


def _count_rolling_7_day_completions(
    tasks_data: dict,
    notes_dir: Path | None,
    today: date,
) -> int:
    """Mirror tasks.py --completed-since 7d count for coverage comparison."""
    cutoff_date = today - timedelta(days=7)
    recent_done = _completed_in_range(tasks_data.get('done', []), cutoff_date, today)

    if notes_dir:
        notes_tasks = extract_completed_tasks(notes_dir, cutoff_date, today)
        board_titles = {task['title'].casefold() for task in recent_done}
        for notes_task in notes_tasks:
            if notes_task['title'].casefold() not in board_titles:
                recent_done.append(notes_task)

    return len(recent_done)


def _coverage_warning_lines(
    tasks_data: dict,
    notes_dir: Path | None,
    week_start: date,
    week_end: date,
    today: date,
    window_completed_count: int,
) -> list[str]:
    """Build coverage-divergence warnings without changing completion counts.

    The prior-week daily-note check fires only when the reviewed window has
    zero completions.
    """
    warnings: list[str] = []

    if notes_dir and window_completed_count == 0:
        prior_start = week_start - timedelta(days=7)
        prior_end = week_start - timedelta(days=1)
        prior_count = len(extract_completed_tasks(notes_dir, prior_start, prior_end))
        if prior_count > 0:
            warnings.append(
                f"Window completions: {window_completed_count}; "
                f"prior-week daily-note completions: {prior_count}."
            )

    rolling_count = _count_rolling_7_day_completions(tasks_data, notes_dir, today)
    if rolling_count >= window_completed_count + 2:
        warnings.append(
            f"Window completions: {window_completed_count}; "
            f"rolling-7-day completions: {rolling_count}."
        )

    undated_board_count = sum(
        1 for task in tasks_data.get('done', [])
        if not task.get('completed_date')
    )
    if undated_board_count:
        warnings.append(
            f"Board done items without completion dates: {undated_board_count}; "
            "not attributed to this window."
        )

    if not warnings:
        return []

    lines = ["", "⚠️ **Coverage Warning**"]
    lines.extend(f"  • {warning}" for warning in warnings)
    lines.append("")
    return lines


def generate_weekly_review(
    week: str | None = None,
    archive: bool = False,
    *,
    this_week: bool = False,
    today: date | None = None,
) -> str:
    """Generate weekly review summary."""
    _, tasks_data = load_tasks()

    # today= is a test-injection point; runtime callers leave it unset.
    today = today or datetime.now().date()
    week_start, week_end = parse_iso_week(week, this_week=this_week, today=today)
    notes_dir_raw = os.getenv("TASK_TRACKER_DAILY_NOTES_DIR", None)
    notes_dir = Path(notes_dir_raw) if notes_dir_raw else None
    priority_reference_date = today if this_week else week_start
    misses_reference_date = today
    upcoming_start = max(today, week_start)
    iso_year, iso_week, _ = week_start.isocalendar()
    week_label = f"{iso_year}-W{iso_week:02d}"

    lines = [f"📊 **Weekly Review — {week_label} ({week_start.strftime('%B %d')} to {week_end.strftime('%B %d')})**\n"]

    # Completed This Week: daily notes primary, board [x] fallback
    # Always use the ISO week range so the data matches the header label
    if notes_dir:
        done_tasks = extract_completed_tasks(
            notes_dir=notes_dir,
            start_date=week_start,
            end_date=week_end,
        )
        done_tasks = _merge_windowed_completed_tasks(
            done_tasks,
            tasks_data.get('done', []),
            week_start,
            week_end,
        )
    else:
        done_tasks = _completed_in_range(tasks_data.get('done', []), week_start, week_end)

    format_area_grouped(
        lines,
        "✅ **Completed This Week**",
        done_tasks,
        lambda t: t['title'],
        "No completed tasks",
    )

    lines.extend(_coverage_warning_lines(
        tasks_data,
        notes_dir,
        week_start,
        week_end,
        today,
        len(done_tasks),
    ))

    # Carried Over (Misses): overdue tasks bucketed from utils and then grouped by area
    missed_buckets = get_missed_tasks_bucketed(
        tasks_data,
        reference_date=misses_reference_date.isoformat(),
    )
    carried_over = flatten_missed_buckets(missed_buckets)
    format_area_grouped(
        lines,
        "⏳ **Carried Over (Misses)**",
        carried_over,
        lambda t: f"{t['title']} ({format_overdue(t, misses_reference_date)}; due {t['due']})",
        "No overdue tasks",
    )

    # This Week Priorities: Q1 + Q2 due this week, plus undated tasks (with escalation labels)
    priorities: list[dict] = []
    for priority_label, section in (('Q1', 'q1'), ('Q2', 'q2')):
        for task in tasks_data.get(section, []):
            due_raw = task.get('due')
            due_date_val = parse_due_date(due_raw)
            if due_raw and (not due_date_val or due_date_val < week_start or due_date_val > week_end):
                continue
            eff = effective_priority(task, priority_reference_date)
            display_label = {'q1': 'Q1', 'q2': 'Q2', 'q3': 'Q3'}.get(eff['section'], priority_label)
            indicator = f" {eff['indicator']}" if eff['indicator'] else ""
            priorities.append({**task, '_priority': display_label, '_escalation_indicator': indicator})

    format_area_grouped(
        lines,
        "🎯 **This Week Priorities (Q1 + Q2)**",
        priorities,
        lambda t: (
            f"[{t['_priority']}] {t['title']}"
            + (f" (due {t['due']})" if t.get('due') else "")
            + t.get('_escalation_indicator', '')
        ),
        "No Q1/Q2 priorities",
    )

    # Upcoming deadlines: open tasks due later in this week window
    upcoming = []
    for task in tasks_data.get('all', []):
        if task.get('done'):
            continue

        due_date = parse_due_date(task.get('due'))
        if not due_date:
            continue

        if due_date < upcoming_start or due_date > week_end:
            continue

        upcoming.append((due_date, task))

    upcoming.sort(key=lambda item: item[0])
    upcoming_tasks = [task for _, task in upcoming]
    format_area_grouped(
        lines,
        "📅 **Upcoming Deadlines**",
        upcoming_tasks,
        lambda t: f"{t['title']} (due {t['due']})",
        "No upcoming deadlines in this week",
    )

    completed_demos = [
        task for task in done_tasks
        if (task.get('type') or '').lower() == 'demo'
    ]
    upcoming_demos = [
        task for task in tasks_data.get('all', [])
        if not task.get('done') and (task.get('type') or '').lower() == 'demo'
    ]
    if completed_demos or upcoming_demos:
        lines.append("")
        lines.append("🎬 **Demo Summary**")
        completed_titles = ', '.join(task['title'] for task in completed_demos) or 'None'
        upcoming_titles = ', '.join(task['title'] for task in upcoming_demos) or 'None'
        lines.append(f"  • Completed ({len(completed_demos)}): {completed_titles}")
        lines.append(f"  • Upcoming ({len(upcoming_demos)}): {upcoming_titles}")

    # Velocity / Burndown metrics (compute BEFORE archiving to avoid double-count)
    velocity_lines = generate_velocity_section(
        tasks_data,
        week_start,
        week_end,
        ARCHIVE_DIR,
        notes_dir=notes_dir,
        completed_tasks=done_tasks,
    )
    lines.extend(velocity_lines)

    append_candidate_review_section(lines)
    append_task_audit_section(lines)

    # Archive if requested
    if archive and done_tasks:
        archive_name = _archive_to_quarterly(done_tasks)
        # Clean stale [x] lines from the board
        tasks_file, fmt = get_tasks_file()
        archived_board_done = _board_done_tasks_with_provenance(done_tasks)
        cleaned = _clean_stale_done_lines(tasks_file, archived_board_done)
        extra = f" (cleaned {cleaned} stale lines)" if cleaned else ""
        if archive_name:
            lines.append(f"📦 Archived {len(done_tasks)} completed tasks to {archive_name}{extra}.")

    lessons = extract_lessons(notes_dir, week_start, week_end) if notes_dir else []
    lines.append("")
    lines.append("📝 **Lessons & Insights**")
    if lessons:
        for lesson in lessons:
            lines.append(f"  • {lesson}")
    else:
        lines.append(
            "  No lessons captured this week. Consider: What worked? What didn't? "
            "What would you do differently?"
        )

    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description='Generate weekly review summary')
    window_group = parser.add_mutually_exclusive_group()
    window_group.add_argument(
        '--week',
        help='ISO week to review (YYYY-WNN).',
    )
    window_group.add_argument(
        '--this-week',
        action='store_true',
        help='Review the current ISO week instead of the most recent completed week.',
    )
    parser.add_argument('--archive', action='store_true', help='Archive completed tasks')

    args = parser.parse_args()
    try:
        print(generate_weekly_review(
            week=args.week,
            archive=args.archive,
            this_week=args.this_week,
        ))
    except ValueError as exc:
        parser.error(str(exc))


if __name__ == '__main__':
    sys.exit(error_envelope.run_main("weekly_review", main))
