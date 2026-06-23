#!/usr/bin/env bash
# Telegram slash command wrapper for task-tracker skill
# Usage: telegram-commands.sh {daily|eod|weekly|promote|swap|done|reschedule|snooze|body-double|start|cancel-session|
#                              done24h|done7d|ledger|ledger-cron|win|approve|nag-check|checkin-dispatch|nag|quiet|unquiet|audit|undo}
#
# U1 NO-RAW-ERROR-LEAK boundary: every python3 invocation goes through
# run_with_envelope, which captures stdout AND stderr. On a non-zero exit the
# captured stderr is written to the structured error log (NEVER echoed to
# stdout) and a friendly one-line notice is printed instead. The user/agent
# relay therefore never sees a Python traceback, exception class, or file path.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# The reserved envelope notice phrase. error_envelope._friendly_line() emits this
# exact substring on any handled failure; it is NOT a bare ⚠️ glyph (which
# tasks.py reuses for unrelated messages), so callers may key off it as a safe
# failure sentinel rather than the exit status.
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
    # U8 consolidated morning standup: the CANONICAL deterministic cron entry the 8am
    # standup runs (a deterministic command cron, replacing the legacy Lobster
    # `Daily Interactive Work Standup` agentTurn -- see standup.standup_cron_descriptor).
    # The standup OPENS with tomorrow's #1 (the EOD-set tomorrow-pointer, resolved against
    # the live board), then the board/priorities/blockers. No LLM relay; same command a
    # user runs by hand. The cron swap + the legacy-cron deletion are DEFERRED OPERATOR
    # steps, gated on the U8 parity check.
    run_with_envelope "standup" python3 "$SCRIPT_DIR/standup.py"
    ;;
  eod)
    # U7 EOD ritual (the 18:00 deterministic command cron points here). Assembles the
    # detect/confirm + forced-disposition + tomorrow's-#1 steps and DELIVERS the whole
    # ritual through the receipt-backed seam (prove -> gate -> assert -> deliver_once,
    # button-capable, idem-keyed on the local date so a same-day re-fire never double-
    # sends), then upserts the human-readable ## EOD Summary to the Obsidian daily note.
    # Board mutations happen ONLY on the user's later button taps -- never on this send.
    # eod_review.py wraps main() in run_main, which records the real eod_review health.
    run_with_envelope "eod_review" python3 "$SCRIPT_DIR/eod_ritual.py"
    ;;
  weekly)
    # Show Q1 and Q2 tasks
    run_with_envelope "weekly_review" python3 "$SCRIPT_DIR/weekly_review.py"
    ;;
  done24h)
    # U5: replaces the broken done24h. Harvests the last 24h of shipped work
    # (merged PRs + sent mail), matches it against the active board, and prints a
    # brag-doc draft. Reactive: the agent relays the draft to the originating
    # topic, and an explicit /approve marks a matched task done.
    run_with_envelope "ledger_harvest" python3 "$SCRIPT_DIR/harvest_ledger.py" harvest --window 24h
    ;;
  done7d|ledger)
    # U5: replaces the broken done7d. Harvests the current ISO week and proves
    # the Done-topic delivery target before the draft is pushed. REACTIVE (no
    # --auto): works any day, sends a "nothing to report" line when empty, and
    # records NO ledger_harvest health (a user-asked run is not a cron heartbeat).
    run_with_envelope "ledger_harvest" python3 "$SCRIPT_DIR/harvest_ledger.py" harvest --window week
    ;;
  ledger-cron)
    # H8 + V2: the SCHEDULED weekly brag digest (the U5 cron points here). --auto
    # engages the Friday+content gate (silent on non-Friday and when empty) AND records
    # ledger_harvest health, so a silently-broken weekly harvest shows up in the
    # manifest instead of false-greening. V2 (O3 HIGH 1): the --auto digest now OWNS its
    # delivery through the receipt-backed outbox (openclaw message send -> message-id
    # receipt) and consumes evidence/wins ONLY on a real receipt -- a transport failure
    # records a health FAILURE and re-attempts next fire (never lost-and-false-greened).
    # Because the script owns the send, stdout carries ONLY a compact status line (no
    # draft), so this cron's announce of stdout delivers nothing and cannot double-send.
    # Every reactive path stays un-auto'd (it consumes on proof + relays the draft).
    run_with_envelope "ledger_harvest" python3 "$SCRIPT_DIR/harvest_ledger.py" harvest --window week --auto
    ;;
  approve)
    # U5: approve one matched ledger task (`approve <task_id> <inbound_topic_id>`).
    # Reactive: the relay passes the inbound topic id, and harvest_ledger.py's
    # topic guard rejects it unless it equals the Done topic -- origin proof is not
    # correctness proof.
    run_with_envelope "ledger_harvest" python3 "$SCRIPT_DIR/harvest_ledger.py" \
      approve "$2" --topic-id "$3"
    ;;
  win)
    # H8: `/win <text>` -- frictionless manual win capture. No board cap, no
    # validation gate (capture must never block); the win is durably appended and
    # surfaces in the next weekly brag digest's classified bucket. The free-form
    # tail is forwarded verbatim. Reactive: the reply lands in the originating topic.
    shift
    run_with_envelope "win" python3 "$SCRIPT_DIR/harvest_ledger.py" win "$@"
    ;;
  audit)
    # U2: list recent autonomous acts, or detail one with `audit act_<id>`.
    # Surfaced in the 🧭 Identity topic (1909); reactive + read-only (rung 0).
    shift
    run_with_envelope "audit" python3 "$SCRIPT_DIR/autonomy_cli.py" audit "$@"
    ;;
  undo)
    # U2: reverse a prior gated act (`undo act_<id>`). Reactive; restores the
    # board line by content-search or acks a nag loop, inside the undo window.
    shift
    run_with_envelope "undo" python3 "$SCRIPT_DIR/autonomy_cli.py" undo "$@"
    ;;
  promote)
    # H6 capture+promote-gate: move a parked task onto the active board. The cap
    # gates PROMOTION (not capture) -- a full committed set refuses with a /swap
    # hint. Reactive board mutation; reply lands in the originating topic.
    run_with_envelope "promote" python3 "$SCRIPT_DIR/tasks.py" promote "$2"
    ;;
  swap)
    # H6 swap: park out_id (active->parking) AND promote in_id (parking->active) so
    # a full committed set can take a new task without going over-cap. Park-out runs
    # first to free a slot; a bad out/in id refuses with no partial move.
    run_with_envelope "swap" python3 "$SCRIPT_DIR/tasks.py" swap "$2" "$3"
    ;;
  done|reschedule|snooze|carry|drop|body-double|cancel-session)
    # U4 reactive nag commands + U5 EOD dispositions (carry/drop). Each mutates the
    # board (where applicable) and then closes/recycles the nag loop SYNCHRONOUSLY in
    # the same turn (origin-proven, no proactive push). carry keeps the task active and
    # stamps a carried:: marker; drop moves it to the parking lot (both /undo-reversible
    # via a gated pre-action snapshot). The subcommand name is $1; the rest are its args.
    sub="$1"; shift
    run_with_envelope "$sub" python3 "$SCRIPT_DIR/nag_commands.py" "$sub" "$@"
    ;;
  start)
    # H7 initiation loop: `/start <task_id> [<minutes>] [next: <cue>]` begins a focus
    # block -- REUSES the body-double focus-session machinery (a cue stored on the
    # session, ephemeral check-in crons with the explicit proven delivery target),
    # mutes the nag (H5 quiet) for the duration, and ends with a structured
    # done/continue/blocked/redefine disposition. `/start` (no arg) or `/start status`
    # shows the active session's resumption cue. Reactive: reply lands in the
    # originating topic. The free-form tail (incl. the multi-word `next:` cue) is
    # forwarded verbatim; nag_commands parses it.
    shift
    run_with_envelope "start" python3 "$SCRIPT_DIR/nag_commands.py" start "$@"
    ;;
  nag-check)
    # U4 nag engine (cron). Proactive push: every nag goes through
    # prove_delivery_target + the gated act_id + assert_send_target. An unset env
    # blocks the push and leaves the loop open (never silently clears). The push is
    # capped at NAG_DISPLAY_LIMIT worst-overdue tasks (the rest defer to `/nag`).
    shift
    run_with_envelope "nag_check" python3 "$SCRIPT_DIR/nag_check.py" "$@"
    ;;
  checkin-dispatch)
    # V1 deterministic focus/body-double check-in (cron-driven, NOT an LLM turn).
    # The `/start` + `/body-double` ephemeral check-in crons are COMMAND crons that
    # run this at fire time: it reloads state (skip-if-ended), RE-PROVES the delivery
    # target, renders INERT text, and sends via the receipt-backed outbox. The session
    # identity arrives as ARGV (session_id task_id elapsed_min is_final [label]) --
    # never interpolated into any prompt, so the user's resumption cue can never reach
    # an LLM instruction channel. The dispatcher OWNS the send; the cron does not
    # re-announce its stdout.
    shift
    run_with_envelope "checkin_dispatch" python3 "$SCRIPT_DIR/checkin_dispatch.py" "$@"
    ;;
  nag)
    # U4 read-only escape hatch: `/nag` (or `/nag all`) prints the FULL overdue
    # list the capped cron push points at. Reactive + read-only (rung 0) -- no
    # fire, no push, no state write; the reply lands in the originating topic.
    run_with_envelope "nag" python3 "$SCRIPT_DIR/nag_check.py" --list
    ;;
  quiet)
    # H5 attention budget: `/quiet <dur>` suppresses the PROACTIVE nag (nags only;
    # body-double check-ins the user started keep running) until local now+dur;
    # `/quiet off` clears it; `/quiet` (no arg)
    # shows the current window. Reactive (the user typed it) -- it read/writes only
    # its own quiet-state.json, proves no target, opens no loop, sends no push.
    shift
    run_with_envelope "quiet" python3 "$SCRIPT_DIR/quiet_cli.py" "$@"
    ;;
  unquiet)
    # H5 alias: `/unquiet` == `/quiet off` -- resume proactive pushes immediately.
    run_with_envelope "quiet" python3 "$SCRIPT_DIR/quiet_cli.py" off
    ;;
  health)
    # H4 read-only observability: per-ritual last_success/last_failure, flagging a
    # ritual whose last success is stale. Reactive + read-only (rung 0) -- no state
    # write; the reply lands in the originating topic.
    run_with_envelope "manifest" python3 "$SCRIPT_DIR/cos_manifest.py" health
    ;;
  manifest)
    # H4: emit cos-manifest.json (enabled units + ritual health) and print it. A
    # derived artifact a watchdog can poll; the command itself is rung-0 read-only
    # over the board.
    run_with_envelope "manifest" python3 "$SCRIPT_DIR/cos_manifest.py" manifest
    ;;
  *)
    echo "Usage: $0 {daily|eod|weekly|promote|swap|done|reschedule|snooze|body-double|start|cancel-session|done24h|done7d|ledger|ledger-cron|win|approve|nag-check|checkin-dispatch|nag|quiet|unquiet|health|manifest|audit|undo}"
    exit 1
    ;;
esac

# A handled ritual ALWAYS exits 0: the friendly notice is the user-facing output
# and a non-zero exit would let the cron relay substitute static fallback text
# for it. run_with_envelope's return code is only an INTERNAL signal a caller may
# branch on, never the script's exit status. The unknown-command `*)` branch
# above exits 1 before reaching here.
exit 0
