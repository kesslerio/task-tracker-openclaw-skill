#!/usr/bin/env bash
# Telegram slash command wrapper for task-tracker skill
# Usage: telegram-commands.sh {daily|weekly|done24h|done7d}
#
# U1 NO-RAW-ERROR-LEAK boundary: every python3 invocation goes through
# run_with_envelope, which captures stdout AND stderr. On a non-zero exit the
# captured stderr is written to the structured error log (NEVER echoed to
# stdout) and a friendly one-line notice is printed instead. The user/agent
# relay therefore never sees a Python traceback, exception class, or file path.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# The reserved envelope notice phrase. error_envelope._friendly_line() emits this
# exact substring on any handled failure; it is NOT a bare ⚠️ glyph (which
# tasks.py reuses for unrelated messages), so it is a safe failure sentinel for
# the done24h/done7d grep filter.
ENVELOPE_NOTICE="is unavailable right now. Logged for review."

# run_with_envelope LABEL CMD [ARGS...]
#   LABEL    component name for the friendly notice + error log.
#   CMD ARGS the command to run (python3 <script> ... or any tool).
# Prints the command's stdout on success, or a friendly notice on failure (the
# raw stderr is logged, never echoed). RETURNS the real success/failure status
# (0 = ok, non-zero = failed) so a caller can branch on the status rather than
# scanning the rendered output for a glyph -- which is fragile because ⚠️ is not
# reserved for the envelope and can appear in normal task output.
run_with_envelope() {
  local label="$1"; shift
  local tmpout tmperr
  tmpout="$(mktemp)"
  tmperr="$(mktemp)"
  local exit_code
  if "$@" >"$tmpout" 2>"$tmperr"; then
    cat "$tmpout"
    rm -f "$tmpout" "$tmperr"
    return 0
  else
    # Capture the wrapped command's real exit code INSIDE the else branch -- a
    # `$?` read after the `if` would reflect the compound `if` status (0), not
    # the failed command.
    exit_code=$?
  fi
  # Log captured stderr to the structured error log. Best-effort; its own
  # stderr is swallowed and a failure here never blocks the friendly notice.
  python3 "$SCRIPT_DIR/error_envelope.py" log-subprocess \
    --component "$label" \
    --exit-code "$exit_code" \
    --stderr-file "$tmperr" \
    --trigger "user_command:/$label" >/dev/null 2>&1 || true
  # The envelope owns the notice + retry-command mapping (single source of
  # truth, so the shell never names a command the relay does not route). Fall
  # back to a generic inline notice only if python3 itself cannot run.
  local notice
  notice="$(python3 "$SCRIPT_DIR/error_envelope.py" friendly-line --component "$label" 2>/dev/null)"
  if [[ -n "$notice" ]]; then
    echo "$notice"
  else
    echo "⚠️ ${label//_/ } $ENVELOPE_NOTICE"
  fi
  rm -f "$tmpout" "$tmperr"
  return "$exit_code"
}

case "$1" in
  daily)
    run_with_envelope "standup" python3 "$SCRIPT_DIR/standup.py"
    ;;
  weekly)
    # Show Q1 and Q2 tasks
    run_with_envelope "weekly_review" python3 "$SCRIPT_DIR/weekly_review.py"
    ;;
  done24h)
    # Note: Currently shows all done tasks (time filtering not implemented)
    echo "✅ **Recently Completed**"
    echo ""
    # A wrapped script exits 0 even when it FAILS (run_main prints its friendly
    # line and exits 0 so the relay never forwards a raw error), so the exit
    # status alone cannot distinguish failure. Key off the envelope's full,
    # reserved notice phrase ($ENVELOPE_NOTICE) -- NOT a bare ⚠️ glyph, which
    # tasks.py also uses for unrelated messages. On the notice, surface it
    # verbatim; otherwise filter to the completed-tasks view.
    out="$(run_with_envelope "tasks" python3 "$SCRIPT_DIR/tasks.py" list --priority high)"
    if [[ "$out" == *"$ENVELOPE_NOTICE"* ]]; then
      echo "$out"
    else
      printf '%s\n' "$out" | grep -A100 "✅ Done" | head -20
    fi
    ;;
  done7d)
    # Note: Currently shows all done tasks (time filtering not implemented)
    echo "✅ **Completed This Week**"
    echo ""
    out="$(run_with_envelope "tasks" python3 "$SCRIPT_DIR/tasks.py" list --priority high)"
    if [[ "$out" == *"$ENVELOPE_NOTICE"* ]]; then
      echo "$out"
    else
      printf '%s\n' "$out" | grep -A100 "✅ Done" | head -50
    fi
    ;;
  *)
    echo "Usage: $0 {daily|weekly|done24h|done7d}"
    exit 1
    ;;
esac

# A handled ritual ALWAYS exits 0: the friendly notice is the user-facing output
# and a non-zero exit would let the cron relay substitute static fallback text
# for it. run_with_envelope's return code is only an INTERNAL signal (used by the
# done24h/done7d branches), never the script's exit status. The unknown-command
# `*)` branch above exits 1 before reaching here.
exit 0
