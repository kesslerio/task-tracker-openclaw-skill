#!/usr/bin/env bash
# sync-cron-reminders.sh — Sync OpenClaw cron reminders to Obsidian
#
# Fetches all cron jobs from OpenClaw, filters for reminder-type jobs,
# and writes a formatted Markdown table to Obsidian.
#
# Output: ~/Obsidian/03-Areas/ShapeScale/Tasks/Cron Reminders.md
# Schedule: Daily ~3am PT (via OpenClaw cron)
#
# Filter strategy:
#   1. Tags: jobs with "reminder" tag (preferred, future-proof)
#   2. Name heuristics: jobs matching reminder/follow-up patterns
#   3. Config: explicit include/exclude lists in config file
#
# Usage:
#   ./sync-cron-reminders.sh              # Normal sync
#   ./sync-cron-reminders.sh --dry-run    # Preview without writing
#   ./sync-cron-reminders.sh --all        # Include disabled jobs
#   ./sync-cron-reminders.sh --verbose    # Show debug output

set -euo pipefail

# --- Configuration -----------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/cron-parser.sh
source "${SCRIPT_DIR}/lib/cron-parser.sh"

# Output path (configurable via env)
OUTPUT_FILE="${CRON_REMINDERS_FILE:-${HOME}/Obsidian/03-Areas/ShapeScale/Tasks/Cron Reminders.md}"

# Config file for include/exclude overrides
CONFIG_FILE="${CRON_REMINDERS_CONFIG:-${SCRIPT_DIR}/config.json}"

# Workspaces to scan
WORKSPACES=("niemand" "niemand-work")

# How many days to keep in "Recently Completed" section
RECENT_DAYS=30

# --- Flags -------------------------------------------------------------------

DRY_RUN=false
INCLUDE_DISABLED=false
VERBOSE=false

for arg in "$@"; do
  case "$arg" in
    --dry-run)  DRY_RUN=true ;;
    --all)      INCLUDE_DISABLED=true ;;
    --verbose)  VERBOSE=true ;;
    --help|-h)
      echo "Usage: $(basename "$0") [--dry-run] [--all] [--verbose]"
      echo ""
      echo "Options:"
      echo "  --dry-run   Preview output without writing to file"
      echo "  --all       Include disabled jobs"
      echo "  --verbose   Show debug output"
      exit 0
      ;;
    *) echo "Unknown option: $arg" >&2; exit 1 ;;
  esac
done

log() { [[ "$VERBOSE" == "true" ]] && echo "[DEBUG] $*" >&2 || true; }

# --- Fetch cron data ---------------------------------------------------------

log "Fetching cron jobs from OpenClaw..."
CRON_JSON=$(openclaw cron list --all --json 2>/dev/null) || {
  echo "ERROR: Failed to fetch cron list from OpenClaw" >&2
  exit 1
}

TOTAL_JOBS=$(echo "$CRON_JSON" | python3 -c "import json,sys; print(len(json.load(sys.stdin)['jobs']))")
log "Total cron jobs: ${TOTAL_JOBS}"

# --- Load config (if exists) -------------------------------------------------

INCLUDE_IDS=""
EXCLUDE_IDS=""
INCLUDE_PATTERNS=""

if [[ -f "$CONFIG_FILE" ]]; then
  log "Loading config from ${CONFIG_FILE}"
  INCLUDE_IDS=$(python3 -c "
import json, sys
try:
    cfg = json.load(open('$CONFIG_FILE'))
    print(','.join(cfg.get('include_ids', [])))
except: pass
" 2>/dev/null || echo "")
  EXCLUDE_IDS=$(python3 -c "
import json, sys
try:
    cfg = json.load(open('$CONFIG_FILE'))
    print(','.join(cfg.get('exclude_ids', [])))
except: pass
" 2>/dev/null || echo "")
  INCLUDE_PATTERNS=$(python3 -c "
import json, sys
try:
    cfg = json.load(open('$CONFIG_FILE'))
    print('|'.join(cfg.get('include_name_patterns', [])))
except: pass
" 2>/dev/null || echo "")

  # Load workspaces and recent_days from config (fallback to defaults)
  CONFIG_WORKSPACES=$(python3 -c "
import json, sys
try:
    cfg = json.load(open('$CONFIG_FILE'))
    ws = cfg.get('workspaces', [])
    print(' '.join(ws) if ws else '')
except: pass
" 2>/dev/null || echo "")
  if [[ -n "$CONFIG_WORKSPACES" ]]; then
    WORKSPACES=($CONFIG_WORKSPACES)
  fi

  CONFIG_RECENT_DAYS=$(python3 -c "
import json, sys
try:
    cfg = json.load(open('$CONFIG_FILE'))
    rd = cfg.get('recent_days')
    print(rd if rd else '')
except: pass
" 2>/dev/null || echo "")
  if [[ -n "$CONFIG_RECENT_DAYS" ]]; then
    RECENT_DAYS=$CONFIG_RECENT_DAYS
  fi
fi

# --- Filter and format -------------------------------------------------------

log "Filtering and formatting reminder crons..."

# Use Python for JSON processing (much cleaner than jq for complex logic)
FILTERED_OUTPUT=$(echo "$CRON_JSON" | python3 -c "
import json, sys, re
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

data = json.load(sys.stdin)
jobs = data.get('jobs', [])

# Config
workspaces = set('${WORKSPACES[*]}'.split())
include_disabled = '${INCLUDE_DISABLED}' == 'true'
include_ids = set(filter(None, '${INCLUDE_IDS}'.split(',')))
exclude_ids = set(filter(None, '${EXCLUDE_IDS}'.split(',')))
include_patterns_str = '${INCLUDE_PATTERNS}'
include_patterns = include_patterns_str.split('|') if include_patterns_str else []

# Default name patterns that indicate a reminder/follow-up
DEFAULT_PATTERNS = [
    r'(?i)remind',
    r'(?i)follow[\s-]?up',
    r'(?i)check[\s-]?in',
    r'(?i)nag',
    r'(?i)nudge',
    r'(?i)ping\b',
]

all_patterns = DEFAULT_PATTERNS + [p for p in include_patterns if p]

def is_reminder(job):
    \"\"\"Determine if a cron job is a reminder-type job.\"\"\"
    jid = job.get('id', '')
    name = job.get('name', '')
    agent = job.get('agentId', '')
    tags = job.get('tags', [])
    payload = job.get('payload', {})
    text = payload.get('text', '') or payload.get('message', '')

    # Explicit exclude
    if jid in exclude_ids:
        return False

    # Explicit include (bypass workspace filter)
    if jid in include_ids:
        return True

    # Workspace filter - must pass first
    if agent not in workspaces and agent:
        return False

    # Tag-based (future-proof) - after workspace check
    if isinstance(tags, list) and 'reminder' in tags:
        return True

    # Name/text pattern matching
    for pat in all_patterns:
        if re.search(pat, name) or re.search(pat, text):
            return True

    return False

# Filter
filtered = []
for job in jobs:
    if not include_disabled and not job.get('enabled', True):
        continue
    if is_reminder(job):
        filtered.append(job)

# Sort chronologically by next due date (soonest first), disabled last
def sort_key(j):
    enabled = j.get('enabled', True)
    state = j.get('state', {})
    schedule = j.get('schedule', {})
    # Primary: next run time (use nextRunAtMs, fall back to 'at' schedule, then max int)
    next_ms = state.get('nextRunAtMs') or 0
    if not next_ms and schedule.get('kind') == 'at':
        try:
            from datetime import datetime
            at_str = schedule.get('at', '')
            dt = datetime.fromisoformat(at_str.replace('Z', '+00:00'))
            next_ms = int(dt.timestamp() * 1000)
        except:
            next_ms = 0
    if not next_ms:
        next_ms = float('inf')
    return (0 if enabled else 1, next_ms)

filtered.sort(key=sort_key)

# Output as JSON for bash to process
print(json.dumps(filtered))
")

log "Filtered jobs count: $(echo "$FILTERED_OUTPUT" | python3 -c "import json,sys; print(len(json.load(sys.stdin)))")"

# --- Generate Markdown --------------------------------------------------------

generate_markdown() {
  local now_epoch
  now_epoch=$(date +%s)
  local cutoff_epoch=$((now_epoch - RECENT_DAYS * 86400))

  cat <<'HEADER'
# Cron Reminders

> Auto-generated by `sync-cron-reminders.sh` — do not edit manually.
> Last synced: SYNC_TIMESTAMP

HEADER

  # Replace timestamp
  local sync_time
  sync_time=$(TZ="America/Los_Angeles" date "+%Y-%m-%d %I:%M%P PT")

  # Section headers and tables are generated by the Python block below

  # Active reminders
  echo "$FILTERED_OUTPUT" | python3 -c "
import json, sys, subprocess, os
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

# Recent days cutoff passed from bash
cutoff_epoch = $cutoff_epoch

jobs = json.load(sys.stdin)

# Source the cron-parser for schedule formatting
def ms_to_date(ms):
    if not ms or ms == 'null':
        return '-'
    try:
        secs = int(ms) // 1000
        from datetime import datetime
        from zoneinfo import ZoneInfo
        pt = ZoneInfo('America/Los_Angeles')
        dt = datetime.fromtimestamp(secs, tz=pt)
        return dt.strftime('%Y-%m-%d')
    except:
        return '-'

def ms_to_datetime(ms):
    if not ms or ms == 'null':
        return '-'
    try:
        secs = int(ms) // 1000
        pt = ZoneInfo('America/Los_Angeles')
        dt = datetime.fromtimestamp(secs, tz=pt)
        return dt.strftime('%Y-%m-%d %I:%M%P')
    except:
        return '-'

def cron_to_human(expr, tz='America/Los_Angeles'):
    parts = expr.split()
    if len(parts) != 5:
        return expr
    minute, hour, dom, month, dow = parts

    tz_map = {
        'America/Los_Angeles': 'PT',
        'America/New_York': 'ET',
        'America/Chicago': 'CT',
        'UTC': 'UTC',
    }
    tz_abbrev = tz_map.get(tz, tz)

    # Format time
    if hour == '*':
        time_str = 'every hour'
    else:
        h = int(hour)
        m = minute if minute != '*' else '00'
        m = f'{int(m):02d}'
        period = 'am'
        dh = h
        if h == 0: dh = 12
        elif h == 12: period = 'pm'
        elif h > 12: dh = h - 12; period = 'pm'
        time_str = f'{dh}{period}' if m == '00' else f'{dh}:{m}{period}'

    # Pattern
    if dom == '*' and month == '*':
        dow_map = {
            '*': 'Daily', '1-5': 'Weekdays', '0,6': 'Weekends',
            '1': 'Mon', '2': 'Tue', '3': 'Wed', '4': 'Thu',
            '5': 'Fri', '6': 'Sat', '0': 'Sun', '7': 'Sun',
        }
        day_str = dow_map.get(dow, dow)
        return f'{day_str} {time_str} {tz_abbrev}'
    elif dow == '*' and month == '*':
        return f'Monthly (day {dom}) {time_str} {tz_abbrev}'
    return f'{expr} ({tz_abbrev})'

def format_status(enabled, last_status, kind):
    if not enabled:
        return 'Disabled'
    if kind == 'at':
        return 'Fired ✅' if last_status and last_status != 'null' else 'Pending'
    status_map = {'ok': 'Active ✅', 'error': 'Error ❌'}
    return status_map.get(last_status or '', 'Pending')

recurring_rows = []
oneshot_rows = []
completed_rows = []

for job in jobs:
    jid = job['id'][:8]
    name = job.get('name', 'Unnamed')
    agent = job.get('agentId', '?')
    enabled = job.get('enabled', True)
    schedule = job.get('schedule', {})
    state = job.get('state', {})
    kind = schedule.get('kind', '?')

    # Schedule string
    if kind == 'cron':
        sched_str = cron_to_human(schedule.get('expr', '?'), schedule.get('tz', 'America/Los_Angeles'))
    elif kind == 'at':
        at_str = schedule.get('at', '')
        try:
            from datetime import datetime
            from zoneinfo import ZoneInfo
            dt = datetime.fromisoformat(at_str.replace('Z', '+00:00'))
            pt = ZoneInfo('America/Los_Angeles')
            sched_str = dt.astimezone(pt).strftime('%Y-%m-%d %I:%M%P PT')
        except:
            sched_str = at_str
    else:
        sched_str = str(schedule)

    next_due = ms_to_date(state.get('nextRunAtMs'))
    last_fired = ms_to_date(state.get('lastRunAtMs'))
    last_status = state.get('lastStatus', '')
    status = format_status(enabled, last_status, kind)

    # Separate one-shot fired jobs:
    # - keep successful ones in recently completed (cutoff filtered)
    # - always keep errored ones in active until resolved
    if kind == 'at' and last_status and last_status != 'null':
        if last_status == 'error':
            oneshot_rows.append((jid, name, agent, sched_str, next_due, last_fired, 'Error ❌'))
        elif last_status == 'ok':
            last_ms = state.get('lastRunAtMs', 0)
            if last_ms and last_ms >= cutoff_epoch * 1000:
                completed_rows.append((jid, name, ms_to_datetime(last_ms), 'Fired'))
        else:
            oneshot_rows.append((jid, name, agent, sched_str, next_due, last_fired, status))
    elif kind == 'at':
        oneshot_rows.append((jid, name, agent, sched_str, next_due, last_fired, status))
    else:
        recurring_rows.append((jid, name, agent, sched_str, next_due, last_fired, status))

def print_table(rows):
    print('| ID | Reminder | Source | Schedule | Next Due | Last Fired | Status |')
    print('|----|----------|--------|----------|----------|------------|--------|')
    for row in rows:
        jid, name, agent, sched, nxt, last, status = row
        name = name.replace('|', r'\|')
        print(f'| {jid} | {name} | {agent} | {sched} | {nxt} | {last} | {status} |')

print('## Recurring Reminders')
print()
if recurring_rows:
    print_table(recurring_rows)
else:
    print('*No recurring reminders.*')

print()
print('## One-Time Reminders')
print()
if oneshot_rows:
    print_table(oneshot_rows)
else:
    print('*No pending one-time reminders.*')

# Print completed section
if completed_rows:
    print()
    print('## Recently Completed (Last ${RECENT_DAYS} Days)')
    print()
    print('| ID | Reminder | Completed | Result |')
    print('|----|----------|-----------|--------|')
    for jid, name, completed, result in completed_rows:
        name = name.replace('|', r'\|')
        print(f'| {jid} | {name} | {completed} | {result} |')
"

  echo ""
  echo "---"
  echo ""
  echo "*${sync_time} — $(echo "$FILTERED_OUTPUT" | python3 -c "import json,sys; print(len(json.load(sys.stdin)))") reminders tracked across ${#WORKSPACES[@]} workspaces*"
}

# --- Write output -------------------------------------------------------------

MARKDOWN_OUTPUT=$(generate_markdown)

# Replace the placeholder timestamp
SYNC_TIME=$(TZ="America/Los_Angeles" date "+%Y-%m-%d %I:%M%P PT")
MARKDOWN_OUTPUT="${MARKDOWN_OUTPUT//SYNC_TIMESTAMP/$SYNC_TIME}"

if [[ "$DRY_RUN" == "true" ]]; then
  echo "$MARKDOWN_OUTPUT"
  echo ""
  echo "[DRY RUN] Would write to: ${OUTPUT_FILE}"
else
  # Ensure output directory exists
  mkdir -p "$(dirname "$OUTPUT_FILE")"
  echo "$MARKDOWN_OUTPUT" > "$OUTPUT_FILE"
  echo "✅ Synced to ${OUTPUT_FILE}"
  log "Wrote $(wc -l < "$OUTPUT_FILE") lines"
fi
