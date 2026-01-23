#!/usr/bin/env bash
# Telegram slash command wrapper for task-tracker skill
# Usage: telegram-commands.sh {daily|weekly|done24h|done7d}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

case "$1" in
  daily)
    python3 "$SCRIPT_DIR/standup.py"
    ;;
  weekly)
    # Show Q1 and Q2 tasks
    python3 "$SCRIPT_DIR/weekly_review.py"
    ;;
  done24h)
    # Note: Currently shows all done tasks (time filtering not implemented)
    echo "✅ **Recently Completed**"
    echo ""
    python3 "$SCRIPT_DIR/tasks.py" list --priority high 2>/dev/null | grep -A100 "✅ Done" | head -20
    ;;
  done7d)
    # Note: Currently shows all done tasks (time filtering not implemented)
    echo "✅ **Completed This Week**"
    echo ""
    python3 "$SCRIPT_DIR/tasks.py" list --priority high 2>/dev/null | grep -A100 "✅ Done" | head -50
    ;;
  *)
    echo "Usage: $0 {daily|weekly|done24h|done7d}"
    exit 1
    ;;
esac
