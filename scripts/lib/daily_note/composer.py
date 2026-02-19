"""Daily note composer."""

from datetime import datetime


def format_calendar_section(events: list[dict]) -> str:
    """Format calendar events as markdown list."""
    if not events:
        return "_No calendar events_"
    
    lines = []
    for event in events:
        if isinstance(event, dict):
            summary = event.get("summary", "Untitled")
            start = event.get("start", {}).get("dateTime", event.get("start", {}).get("date", ""))
            # Try to extract time from ISO format
            if start and "T" in start:
                try:
                    dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
                    time_str = dt.strftime("%H:%M")
                    lines.append(f"- {time_str} — {summary}")
                except ValueError:
                    lines.append(f"- {summary}")
            else:
                lines.append(f"- {summary}")
        else:
            lines.append(f"- {event}")
    
    return "\n".join(lines)


def compose_daily_note(
    date_str: str,
    calendar_events: list[dict],
    top_3: list[str],
    carried: list[str],
) -> str:
    """
    Compose the daily note content.
    
    Sections:
    - ## Calendar
    - ## Top 3
    - ## Open/Carried
    - ## Done
    """
    lines = [
        f"# {date_str}",
        "",
        "## Calendar",
        "",
        format_calendar_section(calendar_events),
        "",
        "## Top 3",
        "",
    ]
    
    # Top 3 tasks
    if top_3:
        for task in top_3:
            lines.append(f"- [ ] {task}")
    else:
        lines.append("- [ ] _Add your top priority_")
        lines.append("- [ ] _Add your second priority_")
        lines.append("- [ ] _Add your third priority_")
    
    lines.extend([
        "",
        "## Open/Carried",
        "",
    ])
    
    # Carried tasks
    if carried:
        for task in carried:
            lines.append(f"- [ ] {task}")
    else:
        lines.append("_No carried tasks_")
    
    lines.extend([
        "",
        "## Done",
        "",
        "_Log completed items here_",
        "",
    ])
    
    return "\n".join(lines)
