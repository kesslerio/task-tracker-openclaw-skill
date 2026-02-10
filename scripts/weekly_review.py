#!/usr/bin/env python3
"""
Weekly Review Generator - Summarizes last week and plans this week.
"""

import argparse
import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from daily_notes import extract_completed_actions
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

    Returns:
        dict mapping ISO week labels (e.g. "2026-W06") to lists of task titles
        found under each "## Week of YYYY-MM-DD" header.
    """
    weeks: dict[str, list[str]] = {}
    if not archive_dir.exists() or not archive_dir.is_dir():
        return weeks

    for archive_file in sorted(archive_dir.glob("ARCHIVE-*.md")):
        try:
            content = archive_file.read_text()
        except (PermissionError, OSError):
            continue

        current_week_label: str | None = None
        for line in content.splitlines():
            header_match = re.match(r'^## Week of (\d{4}-\d{2}-\d{2})', line)
            if header_match:
                try:
                    week_date = datetime.strptime(header_match.group(1), '%Y-%m-%d').date()
                    iso_year, iso_week, _ = week_date.isocalendar()
                    current_week_label = f"{iso_year}-W{iso_week:02d}"
                except ValueError:
                    current_week_label = None
                continue

            if current_week_label is None:
                continue

            task_match = re.match(r'^- ✅ \*\*(.+?)\*\*', line)
            if task_match:
                if current_week_label not in weeks:
                    weeks[current_week_label] = []
                weeks[current_week_label].append(task_match.group(1).strip())

    return weeks


def _count_completed_in_range(
    tasks: list[dict], start: date, end: date
) -> int:
    """Count tasks whose completed_date falls within [start, end]."""
    count = 0
    for task in tasks:
        cd = task.get('completed_date')
        if not cd:
            continue
        try:
            completed = datetime.strptime(cd, '%Y-%m-%d').date()
        except ValueError:
            continue
        if start <= completed <= end:
            count += 1
    return count


def generate_velocity_section(
    tasks_data: dict,
    week_start: date,
    week_end: date,
    archive_dir: Path,
) -> list[str]:
    """Generate the 📊 Velocity section lines.

    Metrics:
    - Completed: tasks with completed_date in the review week
    - Added: new tasks that appeared in the current task list but weren't in
      the previous week's archive snapshot (approximated from open task count
      and archive history)
    - Net: completed - added
    - 4-week trend: completion counts per week from archive + current
    """
    lines: list[str] = []

    # --- Completed this week ---
    # Count from all tasks (done tasks are a subset of all tasks in parse_tasks)
    all_tasks = tasks_data.get('all', [])
    completed_this_week = _count_completed_in_range(all_tasks, week_start, week_end)

    # --- Archive data for trend ---
    archive_weeks = _parse_archive_weeks(archive_dir)

    # Build 4-week rolling trend: 3 previous weeks + current week
    trend_counts: list[int] = []
    for i in range(3, 0, -1):
        trend_start = week_start - timedelta(weeks=i)
        iso_year, iso_week, _ = trend_start.isocalendar()
        label = f"{iso_year}-W{iso_week:02d}"
        trend_counts.append(len(archive_weeks.get(label, [])))

    # Current week's completion count for the trend
    iso_year_cur, iso_week_cur, _ = week_start.isocalendar()
    current_label = f"{iso_year_cur}-W{iso_week_cur:02d}"
    current_archive_count = len(archive_weeks.get(current_label, []))
    # Use the higher of live count vs archive count (archive may not be written yet)
    current_week_count = max(completed_this_week, current_archive_count)

    # --- Added this week (approximation) ---
    # We can't perfectly track "added" without snapshots. Use archive weeks:
    # tasks added ≈ current open tasks + completed this week − (open tasks last week + completed last week)
    # Since we don't have last week's open count, approximate from archive:
    # just count how many tasks exist now that weren't archived.
    # Simpler approach: report "—" if we can't determine, or use archive diff.
    #
    # Best available heuristic: total open tasks + done this week vs previous week's similar count
    # For now, we estimate added = 0 if no data, and note it's approximate.
    added_this_week: int | None = None  # None = unknown

    # --- Build output ---
    lines.append("")
    lines.append("📊 **Velocity**")
    lines.append(f"  Completed: {completed_this_week} task{'s' if completed_this_week != 1 else ''}")

    if added_this_week is not None:
        lines.append(f"  Added: {added_this_week} task{'s' if added_this_week != 1 else ''}")
        net = completed_this_week - added_this_week
        net_str = f"+{net}" if net > 0 else str(net)
        lines.append(f"  Net: {net_str}")
    else:
        lines.append("  Added: — (tracking not available)")
        lines.append(f"  Net: — (need task snapshots)")

    # 4-week trend (3 previous weeks + current)
    full_trend = trend_counts + [current_week_count]
    if any(c > 0 for c in full_trend):
        trend_str = " → ".join(str(c) for c in full_trend)
        lines.append(f"  4-week trend: {trend_str}")
    else:
        lines.append("  4-week trend: — (no archive data yet)")

    lines.append("")
    return lines


def archive_done_tasks(content: str, done_tasks: list) -> str:
    """Archive done tasks and return updated content."""
    if not done_tasks:
        return content
    
    # Create archive entry
    quarter = get_current_quarter()
    archive_file = ARCHIVE_DIR / f"ARCHIVE-{quarter}.md"
    
    archive_entry = f"\n## Week of {datetime.now().strftime('%Y-%m-%d')}\n\n"
    for task in done_tasks:
        archive_entry += f"- ✅ **{task['title']}**\n"
    
    # Append to archive
    if archive_file.exists():
        archive_content = archive_file.read_text()
    else:
        archive_content = f"# Task Archive - {quarter}\n"
    
    archive_content += archive_entry
    archive_file.write_text(archive_content)
    
    # Clear done section in original content
    done_section_pattern = r'(## ✅ Done.*?\n\n).*?(\n## |\n---|\Z)'
    new_content = re.sub(
        done_section_pattern,
        r'\1_Move completed items here during daily standup_\n\n\2',
        content,
        flags=re.DOTALL
    )
    
    return new_content


def group_by_area(tasks: list[dict]) -> dict[str, list[dict]]:
    """Group tasks by area."""
    areas: dict[str, list[dict]] = {}
    for t in tasks:
        area = t.get('area') or 'Uncategorized'
        if area not in areas:
            areas[area] = []
        areas[area].append(t)
    return areas


def parse_iso_week(week: str | None) -> tuple[date, date]:
    """Parse ISO week string (YYYY-WNN) into start/end dates."""
    today = datetime.now().date()
    if not week:
        week_start = today - timedelta(days=today.weekday())
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


def generate_weekly_review(week: str | None = None, archive: bool = False) -> str:
    """Generate weekly review summary."""
    _, tasks_data = load_tasks()

    week_start, week_end = parse_iso_week(week)
    notes_dir_raw = os.getenv("TASK_TRACKER_DAILY_NOTES_DIR", None)
    notes_dir = Path(notes_dir_raw) if notes_dir_raw else None
    today = datetime.now().date()
    reference_date = week_start if week else today
    iso_year, iso_week, _ = week_start.isocalendar()
    week_label = f"{iso_year}-W{iso_week:02d}"

    lines = [f"📊 **Weekly Review — {week_label} ({week_start.strftime('%B %d')} to {week_end.strftime('%B %d')})**\n"]

    if week:
        lines.append(
            "_Note: `--week` changes the reporting window, but `Recently Completed` cannot be time-filtered "
            "because completed tasks do not store completion timestamps._"
        )
        lines.append("")

    # Recently Completed (cannot be time-filtered with current task model)
    done_tasks = tasks_data.get('done', [])
    format_area_grouped(
        lines,
        "✅ **Recently Completed**",
        done_tasks,
        lambda t: t['title'],
        "No completed tasks in ✅ Done",
    )

    # Carried Over (Misses): overdue tasks bucketed from utils and then grouped by area
    missed_buckets = get_missed_tasks_bucketed(tasks_data, reference_date=reference_date.isoformat())
    carried_over = flatten_missed_buckets(missed_buckets)
    format_area_grouped(
        lines,
        "⏳ **Carried Over (Misses)**",
        carried_over,
        lambda t: f"{t['title']} ({format_overdue(t, reference_date)}; due {t['due']})",
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
            eff = effective_priority(task, reference_date)
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

        if due_date < reference_date or due_date > week_end:
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

    untracked_wins: list[str] = []
    if notes_dir:
        # Use week_start/week_end for --week, otherwise last 7 days
        notes_start = week_start if week else (today - timedelta(days=6))
        notes_end = week_end if week else today
        notes_actions = extract_completed_actions(
            notes_dir=notes_dir,
            start_date=notes_start,
            end_date=notes_end,
        )
        done_titles = [
            task["title"].strip()
            for task in done_tasks
            if task.get("title")
        ]
        done_titles = [t.casefold() for t in done_titles if t]  # Strip then filter empty

        for action in notes_actions:
            action_lower = action.casefold()
            if any(action_lower == title for title in done_titles):
                continue
            untracked_wins.append(action)

    if untracked_wins:
        lines.append("📌 **Untracked Wins (from daily notes):**")
        for item in untracked_wins:
            lines.append(f"  • {item}")
        lines.append("")

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

    # Archive if requested
    if archive and done_tasks:
        tasks_file, format = get_tasks_file()
        content = tasks_file.read_text()
        new_content = archive_done_tasks(content, done_tasks)
        tasks_file.write_text(new_content)
        lines.append(f"📦 Archived {len(done_tasks)} completed tasks.")

    # Velocity / Burndown metrics
    velocity_lines = generate_velocity_section(
        tasks_data, week_start, week_end, ARCHIVE_DIR,
    )
    lines.extend(velocity_lines)

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
    parser.add_argument(
        '--week',
        help=(
            'ISO week to review (YYYY-WNN). Limitation: completed tasks in "Recently Completed" '
            'cannot be time-filtered because completion timestamps are not stored.'
        ),
    )
    parser.add_argument('--archive', action='store_true', help='Archive completed tasks')

    args = parser.parse_args()
    try:
        print(generate_weekly_review(week=args.week, archive=args.archive))
    except ValueError as exc:
        parser.error(str(exc))


if __name__ == '__main__':
    main()
