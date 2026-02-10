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

    # Create temp dir (portable: -t template works on GNU and BSD/macOS)
    _tmpdir="$(mktemp -d -t task-tracker.XXXXXX)"
    _split_file="$_tmpdir/standup_split.txt"

    # Cleanup on exit (success or failure)
    cleanup() { rm -rf "$_tmpdir"; }
    trap cleanup EXIT

    # Generate standup and split into 3 messages
    if ! python3 "$SCRIPT_DIR/standup.py" --split > "$_split_file" 2>&1; then
      echo "Error: standup.py failed" >&2
      cat "$_split_file" >&2 || true
      exit 1
    fi

    # Split on message separator (stderr to /dev/null, not stdout)
    if ! csplit -s "$_split_file" '/^---$/' '{*}' -f "$_tmpdir/msg_" 2>/dev/null; then
      # If no separators found, output the whole file as one message
      cat "$_split_file"
      exit 0
    fi

    # Print each message with separator that Niemand can parse
    for msg_file in "$_tmpdir/msg_"*; do
      [ -s "$msg_file" ] || continue
      cat "$msg_file"
      echo "___SPLIT_MESSAGE___"
    done
    ;;
  weekly)
    python3 "$SCRIPT_DIR/weekly_review.py"
    ;;
  done24h)
    # Show recently completed tasks (summary view, limited to 20 lines).
    # Note: Completion timestamps are not tracked in the task format.
    echo "✅ **Recently Completed**"
    echo ""
    if ! output="$(python3 "$SCRIPT_DIR/tasks.py" list 2>&1)"; then
      echo "Error: failed to list tasks" >&2
      exit 1
    fi
    # Extract only lines with ✅ (completed tasks), not surrounding context
    echo "$output" | grep "✅" | head -20 || echo "No completed tasks found"
    ;;
  done7d)
    # Show all completed tasks (full view, limited to 50 lines).
    # Note: Completion timestamps are not tracked in the task format.
    echo "✅ **All Completed Tasks**"
    echo ""
    if ! output="$(python3 "$SCRIPT_DIR/tasks.py" list 2>&1)"; then
      echo "Error: failed to list tasks" >&2
      exit 1
    fi
    # Extract only lines with ✅ (completed tasks), not surrounding context
    echo "$output" | grep "✅" | head -50 || echo "No completed tasks found"
    ;;
  *)
    echo "Usage: $0 {daily|weekly|done24h|done7d}"
    exit 1
    ;;
esac
