# Calendar Integration

The daily standup script can optionally fetch today's calendar events via the `gog` CLI.

## Configuration

Set the `STANDUP_CALENDARS` environment variable with JSON configuration:

```bash
export STANDUP_CALENDARS='{
  "work": {
    "cmd": "gog-work",
    "calendar_id": "user@work.com",
    "account": "user@work.com",
    "label": "Work"
  },
  "personal": {
    "cmd": "gog",
    "calendar_id": "user@personal.com",
    "account": "user@personal.com",
    "label": null
  },
  "family": {
    "cmd": "gog",
    "calendar_id": "family_calendar_id@group.calendar.google.com",
    "account": "user@personal.com",
    "label": "Family"
  }
}'
```

## Configuration Fields

- **cmd**: Command to run (e.g., `gog`, `gog-work`, etc.)
- **calendar_id**: Google Calendar ID
- **account**: Account email for OAuth
- **label**: Optional label suffix for events (e.g., "(Work)", "(Family)")

## Requirements

- `gog` CLI installed and configured
- OAuth credentials for specified accounts
- Calendar access granted to OAuth app

## Behavior

- Fetches only timed events (ignores birthdays and all-day items)
- Shows events in chronological order
- Silently skips calendars that fail to fetch
- If `STANDUP_CALENDARS` is not set, no calendar integration (script works without it)

## Example Output

```
📋 Daily Standup — Wednesday, January 21

📅 Today's Calendar:
  • 10:00 AM — Team Standup (Work)
  • 11:30 AM — Q1 Planning Meeting (Work)
  • 2:15 PM — Client Demo (Work)
  • 4:45 PM — Sprint Review (Work)

🎯 #1 Priority: Complete project proposal
...
```

## Privacy Note

Calendar titles are displayed as-is from the API. If your OAuth client has limited calendar access, event titles may show as "Untitled" or be hidden entirely (depending on Google Calendar privacy settings).
