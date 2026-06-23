#!/usr/bin/env bash
# Bridge wrapper: run the task-tracker inline-button callback dispatcher with JSON args supplied
# by the OpenClaw interactive-handler plugin (scripts/openclaw-plugins/task-tracker-interactive/).
#
# $1 = args JSON, e.g. {"callback_data":"done:tsk_abc","sender_id":"<id>","topic_id":"<id>"}
#   - callback_data is the <payload> AFTER the "tt" namespace ("<action>:<task_id>[:<arg>]");
#     callback_dispatch.py re-prepends "tt:" and decodes with the SAME U1 codec.
#   - sender_id / topic_id are forwarded VERBATIM. This wrapper performs NO authorization; the
#     downstream command + topic guard are the single authority (KTD-4). The args JSON is handed
#     to callback_dispatch.py as a SINGLE argv element — never interpolated into a shell command —
#     so a callback_data carrying any characters can never reach a shell.
set -euo pipefail

ARGS_JSON="${1:?usage: callback_run.sh <args-json>}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Resolve python3 to an ABSOLUTE path — do NOT rely on PATH. The OpenClaw gateway spawns this
# wrapper with an environment whose PATH may resolve a bare `python3` to a bogus entry. Prefer an
# explicit override, then a PATH lookup as a last resort (we do NOT strip PATH here — that can
# deadlock system wrappers on this host).
if [ -n "${TASK_TRACKER_PYTHON:-}" ] && [ -x "${TASK_TRACKER_PYTHON}" ]; then
  PYTHON_BIN="${TASK_TRACKER_PYTHON}"
elif PYTHON_BIN="$(command -v python3 2>/dev/null)" && [ -n "$PYTHON_BIN" ]; then
  :
else
  echo "callback_run.sh: python3 not found (set TASK_TRACKER_PYTHON to an absolute path)" >&2
  exit 127
fi

cd "$SCRIPT_DIR"
# callback_dispatch.py wraps its body in error_envelope.run_main, so even a fatal error prints ONE
# friendly line and exits 0 — never a traceback to Telegram. The wrapper therefore does not need
# its own error handling beyond the python-not-found guard above.
exec "$PYTHON_BIN" "$SCRIPT_DIR/callback_dispatch.py" "$ARGS_JSON"
