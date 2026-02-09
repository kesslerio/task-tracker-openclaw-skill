#!/usr/bin/env bash
# Task tracker shortcuts for slash commands
set -eo pipefail

# Resolve SCRIPT_DIR (supports symlinks on both GNU/Linux and macOS)
_source="${BASH_SOURCE[0]}"
while [ -L "$_source" ]; do
  _dir="$(cd "$(dirname "$_source")" && pwd)"
  _source="$(readlink "$_source")"
  [[ "$_source" != /* ]] && _source="$_dir/$_source"
done
SCRIPT_DIR="$(cd "$(dirname "$_source")" && pwd)"
unset _source _dir

case "${1:-}" in
  daily)
    export STANDUP_CALENDARS="$(cat ~/.config/task-tracker-calendars.json 2>/dev/null || echo '{}')"
    # Generate standup and split into 3 messages
    python3 "$SCRIPT_DIR/standup.py" --split > /tmp/standup_split.txt 2>&1
    csplit -s /tmp/standup_split.txt '/^---$/' '{*}' -f /tmp/standup_msg_ 2>/dev/null || true

    # Print each message with separator that Niemand can parse
    for msg_file in /tmp/standup_msg_*; do
      [ -s "$msg_file" ] || continue
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
    # Show recently completed tasks (summary view, limited to 20 lines).
    # Note: Completion timestamps are not tracked in the task format,
    # so this shows the most recent items from the Done section.
    echo "✅ **Recently Completed**"
    echo ""
    output="$(python3 "$SCRIPT_DIR/tasks.py" list 2>&1)" || {
      echo "Error: failed to list tasks" >&2
      exit 1
    }
    echo "$output" | grep -A100 "✅" | tail -n +2 | head -20 || echo "No completed tasks found"
    ;;
  done7d)
    # Show all completed tasks (full view, limited to 50 lines).
    # Note: Completion timestamps are not tracked in the task format,
    # so this shows the full Done section.
    echo "✅ **All Completed Tasks**"
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
