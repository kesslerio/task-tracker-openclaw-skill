#!/usr/bin/env python3
"""U2 inline-button RECEIVE half: decode a ``tt:`` tap and run the existing command.

The OpenClaw ``task-tracker-interactive`` plugin (``scripts/openclaw-plugins/``) shells
``callback_run.sh`` -> this module with a SINGLE argv element: a JSON object
``{"callback_data": "<action>:<task_id>[:<arg>]", "sender_id": "<id>", "topic_id": "<id>"}``.
``callback_data`` is the payload AFTER the ``tt`` namespace (the gateway split it off the
first ``:``), so we re-prepend ``tt:`` and decode it with the SAME U1 codec
(``telegram_buttons.decode``) -- one source of truth for the scheme on both sides.

What this module IS: a thin transport that turns a decoded tap into ONE invocation of an
EXISTING command (``done`` / ``snooze`` / ``reschedule`` / ``carry`` / ``drop`` / ``approve`` /
``set-top``) and prints ONE compact JSON result line. It adds no board semantics: every mutation
goes through the existing reversible, gated, receipted command path.

It invokes the underlying command MODULE directly (``nag_commands.py`` / ``harvest_ledger.py``)
rather than the ``telegram-commands.sh`` front-end. They are the SAME commands -- the shell is
just one front-end -- but the shell wrapper intentionally MASKS a non-``ok`` result behind a
friendly one-line notice (its job, for a human relay), which would discard the very structured
signal a tap needs: a STALE tap (``canonical-id-resolution-failed``) or a topic-guard rejection
(``wrong-topic``) must reach the plugin as data, not as a generic "unavailable". So the dispatcher
calls the module, which prints clean JSON on every path, and applies its own no-raw-error-leak
boundary via ``run_main``.

What this module is NOT: it is NOT an authorization point (KTD-4). ``sender_id`` is forwarded
but never trusted here; the downstream command + topic guard are the single authority (e.g.
``harvest_ledger.approve`` hard-rejects a tap whose ``topic_id`` != the Productivity Done topic).
The plugin added no bypass; this module adds none either.

NO RAW ERROR LEAK: the whole body runs inside ``error_envelope.run_main``, so a decode miss, a
bad args JSON, or a crashing command surfaces as ONE friendly line + exit 0 -- never a traceback
to Telegram. A STALE tap (the task is already done / no longer on the board / no open nag loop /
already harvested) is NOT an error: the downstream command reports it as a structured non-``ok``
result, which we pass through verbatim so the plugin can edit the message to a clean "already
actioned" -- the board was already in the desired state.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import error_envelope  # noqa: E402
import telegram_buttons  # noqa: E402

COMPONENT = "callback_dispatch"

# A hard upper bound on a single command invocation so a wedged downstream (a held board flock, a
# stuck mount) cannot pin the gateway's interactive-handler slot indefinitely. A timeout surfaces
# as the same clean ``command_unavailable`` failure result the plugin already handles.
_COMMAND_TIMEOUT_S = 30.0

# Decoded action -> (command MODULE, argv builder). ``module`` is the script under ``scripts/``;
# the builder takes the decoded ``(task_id, arg, topic_id)`` and returns the argv AFTER the script
# path. The module is the SAME command the shell front-end runs; calling it directly preserves the
# structured JSON the shell would mask behind a friendly notice on a non-``ok`` result (stale /
# wrong-topic). ``snz``/``rsch`` map to the verbs ``snooze``/``reschedule`` so the result's
# ``action`` (``command_argv[0]``) matches the plugin's ackText keys.
#
# ``carry`` / ``drop`` are wired in U5 (their nag_commands verbs exist). ``top`` (set tomorrow's
# #1) is wired in U6: it maps to the ``set-top`` verb, which writes the tomorrow-pointer (no board
# mutation -- a STALE tap on an already-actioned task is refused as a structured non-ok result, not
# silently pointed at a dead id). Every action in the KTD-3 table is now mapped; an unknown/forged
# action still returns a clean ``ok:false`` (``decode`` is the trust boundary).
ArgvBuilder = Callable[[str, "str | None", str], "list[str]"]


# Every builder inserts ``--`` before the FIRST positional so a flag-shaped value (e.g. a task id
# that begins with ``-``) is treated as a literal positional by argparse, never as an option --
# regardless of any option a downstream command may add later. ``decode`` already constrains the
# fields (no ``:``), but ``--`` is the cheap, future-proof guard against an argv-flag escape.
def _done_argv(task_id: str, arg: str | None, topic_id: str) -> list[str]:
    return ["done", "--", task_id]


def _start_argv(task_id: str, arg: str | None, topic_id: str) -> list[str]:
    # ``start -- <task_id>``: the default button form (no duration, no cue) -> handle_start
    # uses the configured default block + the ``Work on: <title>`` cue. The ``start``
    # subparser slurps ``rest`` with nargs="*", so the ``--`` guard makes a flag-shaped id a
    # literal positional, and the lone task_id is parsed as the start target.
    return ["start", "--", task_id]


def _snooze_argv(task_id: str, arg: str | None, topic_id: str) -> list[str]:
    return ["snooze", "--", task_id, arg or ""]


def _reschedule_argv(task_id: str, arg: str | None, topic_id: str) -> list[str]:
    return ["reschedule", "--", task_id, arg or ""]


def _carry_argv(task_id: str, arg: str | None, topic_id: str) -> list[str]:
    return ["carry", "--", task_id]


def _drop_argv(task_id: str, arg: str | None, topic_id: str) -> list[str]:
    return ["drop", "--", task_id]


def _set_top_argv(task_id: str, arg: str | None, topic_id: str) -> list[str]:
    return ["set-top", "--", task_id]


def _approve_argv(task_id: str, arg: str | None, topic_id: str) -> list[str]:
    # ``approve --topic-id <inbound topic> -- <task_id>``: the inbound topic id is forwarded so the
    # downstream topic guard (the authority) can accept or reject. We never gate it here. The
    # ``--topic-id`` option precedes ``--`` (it is a real option); ``--`` then protects the task id.
    return ["approve", "--topic-id", topic_id, "--", task_id]


_ACTION_TO_COMMAND: dict[str, tuple[str, ArgvBuilder]] = {
    "done": ("nag_commands.py", _done_argv),
    # U10 priority nag: the ``▶️ Start`` button routes to the EXISTING H7 ``start`` verb
    # (handle_start) -- the initiation lever for today's committed priorities. No new
    # initiation logic; a stale tap (task not on the active board) is refused by
    # handle_start as a structured non-ok result, not pointed at a dead id.
    "start": ("nag_commands.py", _start_argv),
    "snz": ("nag_commands.py", _snooze_argv),
    "rsch": ("nag_commands.py", _reschedule_argv),
    "appr": ("harvest_ledger.py", _approve_argv),
    # U5 EOD forced disposition: carry (keep active, stamp carried::) and drop (move to
    # the parking lot) route to the new nag_commands verbs. ``top`` (set tomorrow's #1)
    # is U6: it routes to the ``set-top`` verb, which writes the tomorrow-pointer the
    # morning standup reads (no board mutation -- a stale tap is refused, not pointed at
    # a dead id). The ``--`` guard protects the task id exactly as the sibling verbs do.
    "carry": ("nag_commands.py", _carry_argv),
    "drop": ("nag_commands.py", _drop_argv),
    "top": ("nag_commands.py", _set_top_argv),
}


def _parse_args(raw: str) -> dict[str, str]:
    """Parse the single argv JSON. A malformed value is a friendly failure, not a crash."""
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError(f"args JSON is not valid: {exc}") from exc
    if not isinstance(obj, dict):
        raise ValueError("args JSON must be an object")
    return {
        "callback_data": str(obj.get("callback_data") or ""),
        "sender_id": str(obj.get("sender_id") or ""),
        "topic_id": str(obj.get("topic_id") or ""),
    }


def _run_command(module: str, command_argv: list[str]) -> dict:
    """Run ``python3 scripts/<module> <argv...>`` and recover its JSON result.

    The command module prints its structured result as JSON on EVERY path (success, stale,
    topic-rejected) and exits non-zero only as an internal signal -- so we ignore the exit code
    and parse stdout. We scan for the LAST parseable JSON object; if there is none (the module
    crashed before printing) we synthesize a clean failure result so the plugin always gets a
    structured object -- never a traceback (a crash's stderr is captured, NOT forwarded).
    """
    try:
        completed = subprocess.run(  # noqa: S603 - list-form argv, no shell=True, fixed module path
            [sys.executable, str(SCRIPT_DIR / module), *command_argv],
            cwd=str(SCRIPT_DIR),
            capture_output=True,
            text=True,
            check=False,
            timeout=_COMMAND_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        # The command wedged (a held lock, a stuck mount). Surface a clean failure -- the board
        # mutation, if any, is the command's own concern; we do NOT report a success we can't prove.
        return {
            "ok": False,
            "reason": "command_timeout",
            "message": "That action timed out. Try the typed command.",
        }
    result = _last_json_object(completed.stdout)
    if result is not None:
        return result
    # No JSON => the module produced no parseable result (it crashed before printing). Treat it as
    # a handled failure; do NOT echo its stdout/stderr, which could carry a traceback.
    return {
        "ok": False,
        "reason": "command_unavailable",
        "message": "That action could not be completed right now. Try the typed command.",
    }


def _last_json_object(text: str) -> dict | None:
    """Return the LAST line of ``text`` that parses as a JSON object, else ``None``.

    The underlying command prints its result as a (possibly multi-line, indented) JSON object,
    but the wrapper may prepend log noise. Scanning whole lines from the end finds a compact
    single-line object; for the indented multi-line case we also try parsing the whole stdout.
    """
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            obj = json.loads(stripped)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
    for line in reversed(stripped.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def dispatch(args: dict[str, str]) -> dict:
    """Decode the tap and run the matching command; return a structured result.

    The result always carries ``ok`` (bool) and ``action`` (the decoded action) so the plugin
    can map it to an acknowledgement. A decode miss (forged / over-budget / unknown action /
    wrong namespace) is a clean ``ok:false`` result, NOT a crash -- ``decode`` is the trust
    boundary, so anything it rejects never reaches a command.
    """
    raw = args["callback_data"]
    # The gateway split ``tt`` off the front; re-prepend it so we decode with the SAME codec
    # the sender used (one source of truth). A value that already carries the namespace (belt
    # and suspenders for a future caller) is decoded as-is.
    candidate = raw if raw.startswith(f"{telegram_buttons.NAMESPACE}:") else f"{telegram_buttons.NAMESPACE}:{raw}"
    decoded = telegram_buttons.decode(candidate)
    if decoded is None:
        return {"ok": False, "action": "none", "reason": "undecodable",
                "message": "That button is no longer valid."}
    action, task_id, arg = decoded

    if action == "rsch" and arg is None:
        # A bare reschedule is an "open date options" intent, handled by the plugin as an edit;
        # it should never reach dispatch as a run. If it does, do NOTHING (no board change) and
        # report it so the caller can open the date keyboard rather than reschedule blindly.
        return {"ok": False, "action": "reschedule", "reason": "needs_date",
                "task_id": task_id, "message": "Pick a date to reschedule to."}

    if action == "appr" and not args["topic_id"]:
        # DEFENSE-IN-DEPTH for the topic guard (KTD-4): an ``appr`` tap MUST carry a non-empty
        # inbound topic id. harvest_ledger.approve compares ``str(inbound) != str(expected)``, so an
        # EMPTY inbound topic combined with an EMPTY ``OPENCLAW_TOPIC_PRODUCTIVITY_DONE`` env would
        # satisfy the guard (``"" == ""``). We refuse the empty case here, BEFORE shelling, so the
        # guard can never be a no-op. This narrows, never widens, authorization (no bypass).
        return {"ok": False, "action": "approve", "task_id": task_id, "reason": "wrong-topic",
                "message": "That confirmation must come from the Done topic."}

    mapping = _ACTION_TO_COMMAND.get(action)
    if mapping is None:
        # A decodable action whose command does not exist yet (carry / drop / top land in U5/U6).
        # A clean, non-crashing result; the rows that emit these actions ship with their command.
        return {"ok": False, "action": action, "task_id": task_id, "reason": "not_yet_available",
                "message": "That action isn't available yet."}
    module, build_argv = mapping
    command_argv = build_argv(task_id, arg, args["topic_id"])

    result = _run_command(module, command_argv)
    # Stamp the decoded action/task_id so the plugin always has them even if the command's JSON
    # omitted one (the command's own keys win where present). ``action`` is the decoded tap action
    # mapped to its command verb so the plugin's ackText keys match (e.g. ``snz`` -> ``snooze``).
    result.setdefault("action", command_argv[0])
    result.setdefault("task_id", task_id)
    return result


def main() -> int:
    raw = sys.argv[1] if len(sys.argv) > 1 else ""
    args = _parse_args(raw)
    result = dispatch(args)
    # ONE compact JSON line -- the plugin reads the last JSON object on stdout.
    print(json.dumps(result, separators=(",", ":"), sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(error_envelope.run_main(COMPONENT, main, trigger="callback_tap"))
