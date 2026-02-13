#!/usr/bin/env bash
# cron-parser.sh — Parse cron expressions to human-readable format
# Used by sync-cron-reminders.sh
#
# Supports:
#   - 5-field cron expressions (min hour dom month dow)
#   - Common patterns: daily, weekday, weekly, monthly
#   - Timezone-aware display

set -euo pipefail

# Convert cron expression to human-readable schedule
# Args: $1 = cron expr (5-field), $2 = timezone (optional)
# Output: Human-readable string like "Daily 8:00am PT"
cron_to_human() {
  local expr="${1:?cron expression required}"
  local tz="${2:-America/Los_Angeles}"
  local tz_abbrev

  # Map timezone to abbreviation
  case "$tz" in
    America/Los_Angeles) tz_abbrev="PT" ;;
    America/New_York)    tz_abbrev="ET" ;;
    America/Chicago)     tz_abbrev="CT" ;;
    America/Denver)      tz_abbrev="MT" ;;
    UTC|Etc/UTC)         tz_abbrev="UTC" ;;
    *)                   tz_abbrev="$tz" ;;
  esac

  # Parse fields
  local min hour dom month dow
  read -r min hour dom month dow <<< "$expr"

  # Format time
  local time_str
  time_str=$(format_time "$hour" "$min")

  # Determine pattern
  if [[ "$dom" == "*" && "$month" == "*" ]]; then
    case "$dow" in
      "*")     echo "Daily ${time_str} ${tz_abbrev}" ;;
      "1-5")   echo "Weekdays ${time_str} ${tz_abbrev}" ;;
      "0,6")   echo "Weekends ${time_str} ${tz_abbrev}" ;;
      "1")     echo "Mon ${time_str} ${tz_abbrev}" ;;
      "2")     echo "Tue ${time_str} ${tz_abbrev}" ;;
      "3")     echo "Wed ${time_str} ${tz_abbrev}" ;;
      "4")     echo "Thu ${time_str} ${tz_abbrev}" ;;
      "5")     echo "Fri ${time_str} ${tz_abbrev}" ;;
      "6")     echo "Sat ${time_str} ${tz_abbrev}" ;;
      "0"|"7") echo "Sun ${time_str} ${tz_abbrev}" ;;
      *)       echo "$(dow_to_days "$dow") ${time_str} ${tz_abbrev}" ;;
    esac
  elif [[ "$dow" == "*" && "$month" == "*" ]]; then
    echo "Monthly (day ${dom}) ${time_str} ${tz_abbrev}"
  else
    # Fallback: raw expression
    echo "${expr} (${tz_abbrev})"
  fi
}

# Format hour:minute into 12h time
# Args: $1 = hour (0-23 or *), $2 = minute (0-59 or *)
format_time() {
  local hour="${1}"
  local min="${2}"

  if [[ "$hour" == "*" ]]; then
    echo "every hour"
    return
  fi

  local h=$((10#$hour))
  local m="${min}"
  [[ "$m" == "*" ]] && m="00"
  m=$(printf "%02d" "$((10#$m))")

  local period="am"
  local display_h="$h"

  if (( h == 0 )); then
    display_h=12
  elif (( h == 12 )); then
    period="pm"
  elif (( h > 12 )); then
    display_h=$((h - 12))
    period="pm"
  fi

  if [[ "$m" == "00" ]]; then
    echo "${display_h}${period}"
  else
    echo "${display_h}:${m}${period}"
  fi
}

# Convert dow field to day names
# Args: $1 = dow field (e.g., "1,3,5" or "1-5")
dow_to_days() {
  local dow="$1"
  local names=("Sun" "Mon" "Tue" "Wed" "Thu" "Fri" "Sat")
  local result=""

  # Handle ranges like "1-5"
  if [[ "$dow" =~ ^([0-7])-([0-7])$ ]]; then
    local start="${BASH_REMATCH[1]}"
    local end="${BASH_REMATCH[2]}"
    for (( i=start; i<=end; i++ )); do
      [[ -n "$result" ]] && result+=","
      result+="${names[$((i % 7))]}"
    done
    echo "$result"
    return
  fi

  # Handle lists like "1,3,5"
  IFS=',' read -ra parts <<< "$dow"
  for part in "${parts[@]}"; do
    [[ -n "$result" ]] && result+=","
    result+="${names[$((part % 7))]}"
  done
  echo "$result"
}

# Convert ISO timestamp (ms) to human-readable date
# Args: $1 = timestamp in milliseconds
ms_to_date() {
  local ms="${1:?timestamp required}"
  if [[ "$ms" == "null" || "$ms" == "" ]]; then
    echo "-"
    return
  fi
  local secs=$((ms / 1000))
  TZ="America/Los_Angeles" date -d "@${secs}" "+%Y-%m-%d %I:%M%P" 2>/dev/null || echo "-"
}

# Convert ISO timestamp (ms) to short date only
# Args: $1 = timestamp in milliseconds
ms_to_short_date() {
  local ms="${1:?timestamp required}"
  if [[ "$ms" == "null" || "$ms" == "" ]]; then
    echo "-"
    return
  fi
  local secs=$((ms / 1000))
  TZ="America/Los_Angeles" date -d "@${secs}" "+%Y-%m-%d" 2>/dev/null || echo "-"
}

# Calculate next run from cron expression
# Args: $1 = nextRunAtMs from state (if available)
# Falls back to "-" if not available
next_run_date() {
  local ms="${1:-}"
  if [[ -z "$ms" || "$ms" == "null" ]]; then
    echo "-"
    return
  fi
  ms_to_short_date "$ms"
}

# Format status from cron state
# Args: $1 = enabled (true/false), $2 = lastStatus, $3 = schedule kind
format_status() {
  local enabled="${1:-true}"
  local last_status="${2:-}"
  local kind="${3:-cron}"

  if [[ "$enabled" != "true" ]]; then
    echo "Disabled"
    return
  fi

  if [[ "$kind" == "at" ]]; then
    if [[ -n "$last_status" && "$last_status" != "null" ]]; then
      echo "Fired ✅"
    else
      echo "Pending"
    fi
    return
  fi

  case "$last_status" in
    ok)    echo "Active ✅" ;;
    error) echo "Error ❌" ;;
    ""|null) echo "Pending" ;;
    *)     echo "$last_status" ;;
  esac
}
