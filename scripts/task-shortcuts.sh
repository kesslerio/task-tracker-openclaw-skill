#!/usr/bin/env bash
# Task tracker shortcuts for slash commands
set -euo pipefail

# Resolve SCRIPT_DIR (supports symlinks on both GNU/Linux and macOS)
_source="${BASH_SOURCE[0]}"
while [ -L "$_source" ]; do
  _dir="$(cd "$(dirname "$_source")" && pwd)"
  _source="$(readlink "$_source")"
  [[ "$_source" != /* ]] && _source="$_dir/$_source"
done
SCRIPT_DIR="$(cd "$(dirname "$_source")" && pwd)"
unset _source _dir

case "$1" in
  daily)
    export STANDUP_CALENDARS="$(cat ~/.config/task-tracker-calendars.json 2>/dev/null || echo '{}')"
    # Generate standup and split into 3 messages
    python3 "$SCRIPT_DIR/standup.py" --split > /tmp/standup_split.txt 2>&1
    csplit -s -z /tmp/standup_split.txt '/^---$/' '{*}' -f /tmp/standup_msg_

    # Print each message with separator that Niemand can parse
    for msg_file in /tmp/standup_msg_*; do
      cat "$msg_file"
      echo "___SPLIT_MESSAGE___"
    done

    # Cleanup
    rm -f /tmp/standup_split.txt /tmp/standup_msg_*
    ;;
  weekly)
    python3 "$SCRIPT_DIR/weekly_review.py"
    ;;
  done24h)
    # Show recently completed tasks.
    # Note: True 24h filtering requires completion timestamps (not yet tracked).
    # Current approach: show the Done section, limited to 20 lines.
    echo "✅ **Recently Completed (last 24h)**"
    echo ""
    output="$(python3 "$SCRIPT_DIR/tasks.py" list 2>&1)" || {
      echo "Error: failed to list tasks" >&2
      exit 1
    }
    echo "$output" | grep -A100 "✅" | tail -n +2 | head -20 || echo "No completed tasks found"
    ;;
  done7d)
    # Show completed tasks for the week.
    # Note: True 7d filtering requires completion timestamps (not yet tracked).
    # Current approach: show the full Done section, limited to 50 lines.
    echo "✅ **Completed This Week (last 7d)**"
    echo ""
    output="$(python3 "$SCRIPT_DIR/tasks.py" list 2>&1)" || {
      echo "Error: failed to list tasks" >&2
      exit 1
    }
    echo "$output" | grep -A100 "✅" | tail -n +2 | head -50 || echo "No completed tasks found"
    ;;
  *)
    echo "Usage: $0 {daily|weekly|done24h|done7d}"
    exit 1
    ;;
esac
