#!/usr/bin/env bash
# Task tracker shortcuts for slash commands

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"

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
    # Note: Time-based filtering not yet implemented (completion dates not tracked).
    # Shows most recent completed tasks (limited to 20 lines).
    echo "✅ **Recently Completed**"
    echo ""
    python3 "$SCRIPT_DIR/tasks.py" list 2>/dev/null | grep -A100 "✅" | head -20 || echo "No completed tasks found"
    ;;
  done7d)
    # Note: Time-based filtering not yet implemented (completion dates not tracked).
    # Shows all completed tasks (limited to 50 lines).
    echo "✅ **Completed This Week**"
    echo ""
    python3 "$SCRIPT_DIR/tasks.py" list 2>/dev/null | grep -A100 "✅" | head -50 || echo "No completed tasks found"
    ;;
  *)
    echo "Usage: $0 {daily|weekly|done24h|done7d}"
    exit 1
    ;;
esac
