#!/usr/bin/env bash
# reminder.sh — Create OpenClaw cron reminders with natural language dates
#
# Interactive or CLI mode for creating cron reminders with optional
# task integration into Weekly Objectives.md
#
# Usage:
#   # Interactive mode
#   ./reminder.sh
#
#   # CLI mode
#   ./reminder.sh "Email founder if no reply" --in 2d --agent niemand-work
#
#   # Full CLI
#   ./reminder.sh "Weekly sync prep" --every "mon 9am" --agent niemand-work \
#     --channel telegram:-5099413463 --model kimi --add-to-tasks
#
# Dependencies: openclaw CLI, python3, date (GNU coreutils)

set -euo pipefail

# --- Defaults ----------------------------------------------------------------

DEFAULT_AGENT="niemand-work"
DEFAULT_CHANNEL="telegram:-5099413463"
DEFAULT_MODEL="kimi"
DEFAULT_SESSION="isolated"
WEEKLY_OBJECTIVES="${TASK_TRACKER_WEEKLY_OBJ:-${HOME}/Obsidian/03-Areas/ShapeScale/Tasks/Weekly Objectives.md}"

# --- Parse CLI args ----------------------------------------------------------

REMINDER_TEXT=""
WHEN=""
AGENT="$DEFAULT_AGENT"
CHANNEL="$DEFAULT_CHANNEL"
MODEL="$DEFAULT_MODEL"
ADD_TO_TASKS=false
INTERACTIVE=true
DRY_RUN=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --in|--at|--every|--when)
      WHEN="$2"; INTERACTIVE=false; shift 2 ;;
    --agent)
      AGENT="$2"; shift 2 ;;
    --channel)
      CHANNEL="$2"; shift 2 ;;
    --model)
      MODEL="$2"; shift 2 ;;
    --add-to-tasks)
      ADD_TO_TASKS=true; shift ;;
    --dry-run)
      DRY_RUN=true; shift ;;
    --help|-h)
      cat <<EOF
Usage: $(basename "$0") [REMINDER_TEXT] [OPTIONS]

Create an OpenClaw cron reminder with natural language scheduling.

Arguments:
  REMINDER_TEXT          The reminder message (or omit for interactive mode)

Options:
  --in DURATION         Fire in relative time (e.g., "2d", "3h", "1w")
  --at DATETIME         Fire at specific time (e.g., "tomorrow 9am", "2026-02-15 10:00")
  --every SCHEDULE      Recurring schedule (e.g., "weekday 8am", "mon 9am", "daily 3pm")
  --when NATURAL        Natural language (e.g., "in 2 days", "next Monday 9am")
  --agent AGENT         Agent to handle reminder (default: ${DEFAULT_AGENT})
  --channel CHANNEL     Delivery channel (default: ${DEFAULT_CHANNEL})
  --model MODEL         Model for agent (default: ${DEFAULT_MODEL})
  --add-to-tasks        Add to Weekly Objectives.md
  --dry-run             Show command without executing
  -h, --help            Show this help

Examples:
  $(basename "$0") "Email founder" --in 2d
  $(basename "$0") "Weekly standup prep" --every "mon 8:30am"
  $(basename "$0")   # Interactive mode
EOF
      exit 0
      ;;
    -*)
      echo "Unknown option: $1" >&2; exit 1 ;;
    *)
      if [[ -z "$REMINDER_TEXT" ]]; then
        REMINDER_TEXT="$1"; INTERACTIVE=false
      else
        echo "Unexpected argument: $1" >&2; exit 1
      fi
      shift ;;
  esac
done

# --- Interactive mode --------------------------------------------------------

if [[ "$INTERACTIVE" == "true" || -z "$REMINDER_TEXT" ]]; then
  echo "🔔 Create New Reminder"
  echo "━━━━━━━━━━━━━━━━━━━━━"
  echo ""

  if [[ -z "$REMINDER_TEXT" ]]; then
    read -rp "📝 What's the reminder? " REMINDER_TEXT
    [[ -z "$REMINDER_TEXT" ]] && { echo "Aborted: no reminder text." >&2; exit 1; }
  fi

  if [[ -z "$WHEN" ]]; then
    echo ""
    echo "⏰ When should this fire?"
    echo "   Examples: \"in 2 days\", \"tomorrow 9am\", \"every weekday 8am\", \"next Monday 9am\""
    read -rp "   > " WHEN
    [[ -z "$WHEN" ]] && { echo "Aborted: no schedule." >&2; exit 1; }
  fi

  echo ""
  echo "🤖 Which agent? [${DEFAULT_AGENT}]"
  echo "   Options: niemand / niemand-work / niemand-family"
  read -rp "   > " input_agent
  [[ -n "$input_agent" ]] && AGENT="$input_agent"

  echo ""
  read -rp "📡 Channel? [${DEFAULT_CHANNEL}] > " input_channel
  [[ -n "$input_channel" ]] && CHANNEL="$input_channel"

  echo ""
  read -rp "🧠 Model? [${DEFAULT_MODEL}] > " input_model
  [[ -n "$input_model" ]] && MODEL="$input_model"

  echo ""
  read -rp "📋 Add to Weekly Objectives? [y/N] > " input_tasks
  [[ "$input_tasks" =~ ^[Yy] ]] && ADD_TO_TASKS=true
fi

# --- Parse natural language date ---------------------------------------------

# parse_schedule outputs: TYPE ARGS
# Where TYPE is one of: at, cron, every
# And ARGS are the openclaw cron add arguments
parse_schedule() {
  local input="$1"
  python3 -c "
import re, sys
from datetime import datetime, timedelta, timezone

input_str = '''${input}'''
now = datetime.now(timezone.utc).astimezone()  # Local timezone-aware datetime

def parse_relative(s):
    \"\"\"Parse relative durations: 'in 2 days', '3h', '1w', 'in 30m'\"\"\"
    s = s.strip().lower()
    s = re.sub(r'^in\s+', '', s)

    # Duration shorthand: 2d, 3h, 30m, 1w
    m = re.match(r'^(\d+)\s*(m|min|mins|minutes?|h|hrs?|hours?|d|days?|w|weeks?)$', s)
    if m:
        val = int(m.group(1))
        unit = m.group(2)[0]
        if unit == 'm': return now + timedelta(minutes=val)
        if unit == 'h': return now + timedelta(hours=val)
        if unit == 'd': return now + timedelta(days=val)
        if unit == 'w': return now + timedelta(weeks=val)

    # 'in X days/hours'
    m = re.match(r'^(\d+)\s+(day|hour|minute|week)s?$', s)
    if m:
        val = int(m.group(1))
        unit = m.group(2)
        if unit == 'minute': return now + timedelta(minutes=val)
        if unit == 'hour': return now + timedelta(hours=val)
        if unit == 'day': return now + timedelta(days=val)
        if unit == 'week': return now + timedelta(weeks=val)
    return None

def parse_absolute(s):
    \"\"\"Parse absolute times: 'tomorrow 9am', '2026-02-15 10:00', 'next Monday 9am'\"\"\"
    s = s.strip().lower()

    # tomorrow [time]
    m = re.match(r'^tomorrow\s*(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$', s)
    if m:
        h = int(m.group(1))
        mi = int(m.group(2)) if m.group(2) else 0
        ampm = m.group(3)
        if ampm == 'pm' and h < 12: h += 12
        if ampm == 'am' and h == 12: h = 0
        t = (now + timedelta(days=1)).replace(hour=h, minute=mi, second=0, microsecond=0)
        return t

    # tomorrow (no time -> 9am default)
    if s.strip() == 'tomorrow':
        return (now + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)

    # next <weekday> [time]
    days_map = {'monday': 0, 'tuesday': 1, 'wednesday': 2, 'thursday': 3,
                'friday': 4, 'saturday': 5, 'sunday': 6,
                'mon': 0, 'tue': 1, 'wed': 2, 'thu': 3, 'fri': 4, 'sat': 5, 'sun': 6}
    m = re.match(r'^next\s+(\w+)\s*(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$', s)
    if m:
        day_name = m.group(1)
        if day_name in days_map:
            target_dow = days_map[day_name]
            current_dow = now.weekday()
            delta = (target_dow - current_dow) % 7
            if delta == 0: delta = 7
            h = int(m.group(2))
            mi = int(m.group(3)) if m.group(3) else 0
            ampm = m.group(4)
            if ampm == 'pm' and h < 12: h += 12
            if ampm == 'am' and h == 12: h = 0
            t = (now + timedelta(days=delta)).replace(hour=h, minute=mi, second=0, microsecond=0)
            return t

    # next <weekday> (no time -> 9am)
    m = re.match(r'^next\s+(\w+)$', s)
    if m and m.group(1) in days_map:
        target_dow = days_map[m.group(1)]
        current_dow = now.weekday()
        delta = (target_dow - current_dow) % 7
        if delta == 0: delta = 7
        return (now + timedelta(days=delta)).replace(hour=9, minute=0, second=0, microsecond=0)

    # ISO date: 2026-02-15 [10:00]
    m = re.match(r'^(\d{4}-\d{2}-\d{2})\s*(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$', s)
    if m:
        d = datetime.strptime(m.group(1), '%Y-%m-%d')
        h = int(m.group(2))
        mi = int(m.group(3)) if m.group(3) else 0
        ampm = m.group(4)
        if ampm == 'pm' and h < 12: h += 12
        if ampm == 'am' and h == 12: h = 0
        return d.replace(hour=h, minute=mi)

    m = re.match(r'^(\d{4}-\d{2}-\d{2})$', s)
    if m:
        return datetime.strptime(m.group(1), '%Y-%m-%d').replace(hour=9, minute=0)

    return None

def parse_recurring(s):
    \"\"\"Parse recurring schedules: 'every weekday 8am', 'daily 3pm', 'every mon 9am'\"\"\"
    s = s.strip().lower()
    s = re.sub(r'^every\s+', '', s)

    days_map = {'monday': '1', 'tuesday': '2', 'wednesday': '3', 'thursday': '4',
                'friday': '5', 'saturday': '6', 'sunday': '0',
                'mon': '1', 'tue': '2', 'wed': '3', 'thu': '4', 'fri': '5', 'sat': '6', 'sun': '0'}

    # daily/weekday [time]
    m = re.match(r'^(daily|weekdays?|weekends?)\s*(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$', s)
    if m:
        pattern = m.group(1)
        h = int(m.group(2))
        mi = int(m.group(3)) if m.group(3) else 0
        ampm = m.group(4)
        if ampm == 'pm' and h < 12: h += 12
        if ampm == 'am' and h == 12: h = 0
        dow = '*'
        if pattern.startswith('weekday'): dow = '1-5'
        elif pattern.startswith('weekend'): dow = '0,6'
        return f'{mi} {h} * * {dow}'

    # <weekday> [time]
    m = re.match(r'^(\w+)\s*(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$', s)
    if m and m.group(1) in days_map:
        dow = days_map[m.group(1)]
        h = int(m.group(2))
        mi = int(m.group(3)) if m.group(3) else 0
        ampm = m.group(4)
        if ampm == 'pm' and h < 12: h += 12
        if ampm == 'am' and h == 12: h = 0
        return f'{mi} {h} * * {dow}'

    return None

# Try parsing in order: recurring > relative > absolute
result = None

# Check for recurring patterns first
if re.match(r'^(every\s+|daily\s+|weekday)', input_str.lower()):
    cron_expr = parse_recurring(input_str)
    if cron_expr:
        print(f'cron {cron_expr}')
        sys.exit(0)

# Try recurring without 'every' prefix (e.g., 'mon 9am')
cron_expr = parse_recurring(input_str)
if cron_expr:
    print(f'cron {cron_expr}')
    sys.exit(0)

# Try relative
dt = parse_relative(input_str)
if dt:
    # Use local timezone offset to ensure correct interpretation
    offset = dt.strftime('%z')
    iso = dt.strftime('%Y-%m-%dT%H:%M:%S') + offset
    print(f'at {iso}')
    sys.exit(0)

# Try absolute
dt = parse_absolute(input_str)
if dt:
    # Use local timezone offset to ensure correct interpretation
    offset = dt.strftime('%z')
    iso = dt.strftime('%Y-%m-%dT%H:%M:%S') + offset
    print(f'at {iso}')
    sys.exit(0)

# Fallback: try to pass through as-is if it looks like a cron expr
if re.match(r'^[\d\*\/\-\,]+\s+[\d\*\/\-\,]+\s+[\d\*\/\-\,]+\s+[\d\*\/\-\,]+\s+[\d\*\/\-\,]+$', input_str.strip()):
    print(f'cron {input_str.strip()}')
    sys.exit(0)

print('error Could not parse schedule', file=sys.stderr)
sys.exit(1)
"
}

SCHEDULE_RESULT=$(parse_schedule "$WHEN") || {
  echo "❌ Could not parse schedule: \"${WHEN}\"" >&2
  echo "   Try: \"in 2 days\", \"tomorrow 9am\", \"every weekday 8am\", \"next Monday 9am\"" >&2
  exit 1
}

SCHEDULE_TYPE="${SCHEDULE_RESULT%% *}"
SCHEDULE_ARGS="${SCHEDULE_RESULT#* }"

# --- Build openclaw command ---------------------------------------------------

CMD=(openclaw cron add)
CMD+=(--name "$REMINDER_TEXT")
CMD+=(--agent "$AGENT")
CMD+=(--model "$MODEL")
CMD+=(--session "$DEFAULT_SESSION")
CMD+=(--announce)
CMD+=(--channel "${CHANNEL%%:*}")
CMD+=(--to "${CHANNEL#*:}")

# Message payload: the reminder text
CMD+=(--message "Reminder: ${REMINDER_TEXT}")

case "$SCHEDULE_TYPE" in
  at)
    CMD+=(--at "$SCHEDULE_ARGS")
    CMD+=(--delete-after-run)
    ;;
  cron)
    CMD+=(--cron "$SCHEDULE_ARGS")
    CMD+=(--tz "America/Los_Angeles")
    ;;
  *)
    echo "❌ Unknown schedule type: $SCHEDULE_TYPE" >&2
    exit 1
    ;;
esac

CMD+=(--json)

# --- Confirm ------------------------------------------------------------------

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "📋 Reminder Summary"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  📝 Text:     ${REMINDER_TEXT}"
echo "  ⏰ Schedule: ${SCHEDULE_TYPE} ${SCHEDULE_ARGS}"
echo "  🤖 Agent:    ${AGENT}"
echo "  📡 Channel:  ${CHANNEL}"
echo "  🧠 Model:    ${MODEL}"
echo "  📋 Tasks:    $([ "$ADD_TO_TASKS" = true ] && echo "Yes" || echo "No")"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

if [[ "$DRY_RUN" == "true" ]]; then
  echo ""
  echo "[DRY RUN] Would execute:"
  echo "  ${CMD[*]}"
  exit 0
fi

# --- Create cron --------------------------------------------------------------

echo ""
echo "Creating cron job..."
RESULT=$("${CMD[@]}" 2>&1) || {
  echo "❌ Failed to create cron job:" >&2
  echo "$RESULT" >&2
  exit 1
}

# Extract cron ID from JSON response
CRON_ID=$(echo "$RESULT" | python3 -c "
import json, sys
try:
    data = json.load(sys.stdin)
    print(data.get('id', data.get('job', {}).get('id', 'unknown')))
except:
    print('unknown')
" 2>/dev/null || echo "unknown")

SHORT_ID="${CRON_ID:0:8}"

echo "✅ Reminder created (cron: ${SHORT_ID})"

# --- Add to Weekly Objectives -------------------------------------------------

if [[ "$ADD_TO_TASKS" == "true" ]]; then
  if [[ -f "$WEEKLY_OBJECTIVES" ]]; then
    # Find the ## Objectives section and append after the last task line
    TASK_LINE="- [ ] ${REMINDER_TEXT} (from cron ${SHORT_ID}) #Ops #high"

    # Append before Parking Lot section if it exists, otherwise at end of Objectives
    if grep -q "^## 🅿️ Parking Lot" "$WEEKLY_OBJECTIVES" 2>/dev/null; then
      # Insert before Parking Lot
      sed -i "/^## 🅿️ Parking Lot/i\\
${TASK_LINE}\\
" "$WEEKLY_OBJECTIVES"
    elif grep -q "^## Objectives" "$WEEKLY_OBJECTIVES" 2>/dev/null; then
      # Append to end of file
      echo "" >> "$WEEKLY_OBJECTIVES"
      echo "$TASK_LINE" >> "$WEEKLY_OBJECTIVES"
    else
      echo "⚠️  Could not find Objectives section in ${WEEKLY_OBJECTIVES}" >&2
    fi
    echo "📋 Task added to Weekly Objectives: ${TASK_LINE}"
  else
    echo "⚠️  Weekly Objectives file not found: ${WEEKLY_OBJECTIVES}" >&2
  fi
fi

echo ""
echo "🔔 Done! Cron ID: ${CRON_ID}"
