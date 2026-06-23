"""U2 inline-button RECEIVE half: callback_dispatch behavioral invariants.

These assert the invariants, not the implementation path:

* DECODE IS THE TRUST BOUNDARY: a forged / over-budget / unknown-action / wrong-namespace
  callback never reaches a command -- it returns a clean ``undecodable`` result, not a crash.
* HAPPY PATH: a ``tt:done:<id>`` tap runs the EXISTING command and mutates the board through
  the reversible path (the task leaves the active board; an ``ok:true`` result is returned).
* STALE TAP: a tap on an already-actioned task returns a clean structured result
  (``canonical-id-resolution-failed``), NEVER a traceback -- the board is not written twice.
* ONE AUTH POINT (KTD-4): an ``appr`` tap from the WRONG topic is rejected by the downstream
  topic guard (``wrong-topic``); the dispatcher adds no bypass and authorizes nothing itself.
* RESCHEDULE: a bare ``rsch`` (no date) is ``needs_date`` (no board change); ``rsch:<date>``
  reschedules through the existing command (closes the R7 two-step-reschedule UX in one tap).
* NO RAW ERROR LEAK: a malformed args JSON surfaces as a friendly one line + exit 0, never a
  traceback (the error_envelope.run_main boundary).
* U5 DISPOSITION: a decodable ``carry`` / ``drop`` tap routes to the new nag_commands verbs and
  mutates the board through the existing reversible path (carry keeps it active + stamps carried::;
  drop moves it to the parking lot).
* U6 SET-TOP: a decodable ``top`` tap routes to the ``set-top`` verb, which writes the
  tomorrow-pointer the morning standup reads (no board mutation).

Public-repo hygiene: the only chat/topic ids here are FAKE values that do NOT match the
-100[0-9]{8,} pattern the CI hygiene grep flags. Real ids are env-sourced at runtime.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import callback_dispatch  # noqa: E402
import telegram_buttons  # noqa: E402

# Fake ids: valid shape but NOT -100xxxxxxxx, so the public-hygiene grep stays clean.
FAKE_SENDER = "-4242424242"
DONE_TOPIC = "5"
WRONG_TOPIC = "99"

WORK_BOARD = """# Work

## 🟡 Q2
- [ ] **Re-evaluate ActiveCampaign** task_id::tsk_abc123 🗓️2026-06-15 area:: Marketing
- [ ] **Camp coordination for June** task_id::tsk_def456 🗓️2026-06-14 area:: Ops
"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolate the board + every state file under tmp_path, mirroring test_harvest_ledger.

    Crucially this UNSETS the ambient productivity env so a test reflects the CI clean
    environment (this host has TELEGRAM_CHAT_ID_* / OPENCLAW_TOPIC_* set ambiently, which would
    otherwise mask an env-dependent path). Tests that need the Done topic set it explicitly.
    """
    work = tmp_path / "Work Tasks.md"
    work.write_text(WORK_BOARD)
    state = tmp_path / "state"
    daily = tmp_path / "daily"

    monkeypatch.setenv("TASK_TRACKER_WORK_FILE", str(work))
    monkeypatch.setenv("TASK_TRACKER_LEDGER_FILE", str(state / "events.jsonl"))
    monkeypatch.setenv("TASK_TRACKER_DAILY_NOTES_DIR", str(daily))
    monkeypatch.setenv("TASK_TRACKER_DONE_LOG_DIR", str(daily))
    monkeypatch.setenv("TASK_MGMT_STATE_DIR", str(state))
    monkeypatch.setenv("TASK_TRACKER_ERROR_LOG", str(state / "errors.jsonl"))
    # Start from NO productivity topic set (the clean-CI baseline). The appr-success path would
    # need a live harvest draft + the proven Done target, which is out of U2's transport scope;
    # the appr test asserts the REJECT path (the topic guard), which is U2's invariant.
    for name in ("TELEGRAM_CHAT_ID_PRODUCTIVITY", "TELEGRAM_CHAT_ID_WORK", "OPENCLAW_TOPIC_PRODUCTIVITY_DONE"):
        monkeypatch.delenv(name, raising=False)
    return {"work": work, "state": state, "daily": daily}


def _dispatch(callback_data, *, sender_id=FAKE_SENDER, topic_id=DONE_TOPIC):
    """Run the dispatcher as a SUBPROCESS (the real entry path: one argv JSON in, one JSON line
    out) and return the parsed result. Inherits the test's (monkeypatched) env, so the board the
    fixture wrote is the board the command mutates."""
    args = {"callback_data": callback_data, "sender_id": sender_id, "topic_id": topic_id}
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "callback_dispatch.py"), json.dumps(args)],
        capture_output=True,
        text=True,
        check=False,
    )
    # The dispatcher prints ONE compact JSON line; a no-raw-leak path prints a friendly line.
    return completed.stdout.strip(), completed.returncode


def _result(callback_data, **kw):
    out, code = _dispatch(callback_data, **kw)
    assert code == 0, f"dispatcher must always exit 0 (got {code}); stdout={out!r}"
    assert "Traceback" not in out, f"a raw traceback leaked to stdout: {out!r}"
    return json.loads(out)


# --- decode trust boundary ------------------------------------------------------------------

def test_forged_callback_is_undecodable_not_a_crash(env):
    res = _result("zzz:tsk_x")  # unknown action -> not in encode's image
    assert res == {"ok": False, "action": "none", "reason": "undecodable",
                   "message": "That button is no longer valid."}


def test_embedded_colon_in_id_is_rejected(env):
    # A raw value a naive split would mis-attribute (id "tsk_a", arg "b") but encode could never
    # emit for a no-arg action -> decode rejects it, so it never reaches a command.
    res = _result("done:tsk_a:b")
    assert res["ok"] is False and res["reason"] == "undecodable"


def test_over_budget_id_never_dispatches(env):
    long_id = "tsk_" + "z" * 80  # pushes tt:done:<id> well past 64 bytes
    assert telegram_buttons.encode("done", long_id) is None  # the U1 guard would have dropped it
    res = _result(f"done:{long_id}")
    assert res["ok"] is False and res["reason"] == "undecodable"


# --- happy path: a tap runs the existing command and mutates the board ----------------------

def test_done_tap_completes_task_through_existing_path(env):
    res = _result("done:tsk_abc123")
    assert res["ok"] is True
    assert res["action"] == "done"
    assert res["task_id"] == "tsk_abc123"
    # The board was mutated through the reversible command path: the task is gone from active.
    assert "tsk_abc123" not in env["work"].read_text()
    # A ledger event was recorded (the existing transition's audit trail).
    ledger = env["state"] / "events.jsonl"
    assert ledger.exists() and "tsk_abc123" in ledger.read_text()


def test_done_tap_action_matches_decoded_verb(env):
    # The decoded action ``snz`` maps to the command verb ``snooze`` so the plugin's ackText keys
    # line up; assert the verb (not the raw tap code) is the reported action.
    res = _result("snz:tsk_abc123:1d")
    assert res["action"] == "snooze"


# --- stale tap: clean structured result, never a double-write or traceback ------------------

def test_stale_done_tap_is_clean_result(env):
    first = _result("done:tsk_abc123")
    assert first["ok"] is True
    board_after_first = env["work"].read_text()
    # Tap again: the task is already gone. A clean structured result, not a crash.
    second = _result("done:tsk_abc123")
    assert second["ok"] is False
    assert second["error"]["code"] == "canonical-id-resolution-failed"
    # No second board write: the board is byte-for-byte what the first tap left.
    assert env["work"].read_text() == board_after_first


# --- one auth point (KTD-4): the topic guard, not the plugin/dispatcher ----------------------

def test_approve_from_wrong_topic_is_rejected_by_the_guard(env, monkeypatch):
    # Set the Done topic to 5; a tap whose inbound topic is 99 must be rejected -- the dispatcher
    # forwards the topic verbatim and the downstream guard (the single authority) rejects it.
    monkeypatch.setenv("OPENCLAW_TOPIC_PRODUCTIVITY_DONE", DONE_TOPIC)
    res = _result("appr:tsk_abc123", topic_id=WRONG_TOPIC)
    assert res["ok"] is False
    assert res["reason"] == "wrong-topic"
    # The board is unchanged: a rejected approve mutates nothing.
    assert "tsk_abc123" in env["work"].read_text()


def test_dispatcher_adds_no_topic_bypass(env, monkeypatch):
    # Even with the Done topic set, a MISSING inbound topic id is still rejected (the guard needs
    # a matching topic; the dispatcher does not fabricate one).
    monkeypatch.setenv("OPENCLAW_TOPIC_PRODUCTIVITY_DONE", DONE_TOPIC)
    res = _result("appr:tsk_abc123", topic_id="")
    assert res["ok"] is False and res["reason"] == "wrong-topic"


def test_empty_inbound_topic_with_empty_env_is_still_refused(env, monkeypatch):
    # The bypass that would exist WITHOUT a dispatcher-side guard: an EMPTY inbound topic AND an
    # EMPTY OPENCLAW_TOPIC_PRODUCTIVITY_DONE env would satisfy harvest_ledger.approve's
    # `str("") != str("")` check. The dispatcher refuses an empty appr topic BEFORE shelling, so
    # the guard can never become a no-op. (Narrows authorization; never widens it.)
    monkeypatch.setenv("OPENCLAW_TOPIC_PRODUCTIVITY_DONE", "")
    res = _result("appr:tsk_abc123", topic_id="")
    assert res["ok"] is False and res["reason"] == "wrong-topic"
    # The board is untouched: an empty-topic approve mutates nothing.
    assert "tsk_abc123" in env["work"].read_text()


def test_flag_shaped_task_id_is_a_literal_positional_not_an_option(env):
    # A flag-shaped task id (begins with '-') must be treated as a literal positional, never as an
    # argparse option that could trigger --help or set a downstream flag. The argv builders insert
    # '--' before the first positional. (decode also rejects it, but '--' is the defense-in-depth.)
    # We assert it resolves as a not-found task (the command ran, the id was a literal), NOT that
    # argparse printed help / errored.
    res = _result("done:-x")  # '-x' is a clean field (no ':'), so decode accepts it
    assert res["ok"] is False
    # The command ran with '--' '-x' as the task id -> not on the board -> stale-shaped result.
    assert res.get("error", {}).get("code") == "canonical-id-resolution-failed"


# --- reschedule: the two-step UX collapses to one tap (R7) ----------------------------------

def test_bare_reschedule_needs_date_no_board_change(env):
    res = _result("rsch:tsk_abc123")
    assert res["ok"] is False
    assert res["reason"] == "needs_date"
    assert res["action"] == "reschedule"
    # No board mutation: the due date is unchanged until a concrete date is tapped.
    assert "🗓️2026-06-15" in env["work"].read_text()


def test_reschedule_to_date_moves_the_due_date(env):
    res = _result("rsch:tsk_abc123:2026-07-01")
    assert res["ok"] is True
    assert res["action"] == "reschedule"
    text = env["work"].read_text()
    assert "2026-07-01" in text  # moved to the tapped date
    assert "tsk_abc123" in text  # still on the board (rescheduled, not completed)


# --- U5 disposition: carry / drop now route to the new nag_commands verbs --------------------

def test_carry_tap_keeps_task_active_and_stamps_carried(env):
    res = _result("carry:tsk_abc123")
    assert res["ok"] is True
    assert res["action"] == "carry"
    assert res["task_id"] == "tsk_abc123"
    text = env["work"].read_text()
    assert "tsk_abc123" in text  # still on the active board
    assert "carried::" in text  # marker stamped for the standup


def test_drop_tap_moves_task_to_parking_lot(env, monkeypatch):
    # drop needs a Parking Lot section to move into; give the board one.
    work = env["work"]
    work.write_text(work.read_text() + "\n## 🅿️ Parking Lot\n")
    res = _result("drop:tsk_abc123")
    assert res["ok"] is True
    assert res["action"] == "drop"
    assert res["task_id"] == "tsk_abc123"
    text = work.read_text()
    # The line is gone from active (Q2) and now lives under the Parking Lot header.
    assert text.index("tsk_abc123") > text.index("Parking Lot")


# --- U6 set-top: a top tap writes the tomorrow-pointer (no board mutation) -------------------

def test_top_tap_writes_tomorrow_pointer(env):
    res = _result("top:tsk_abc123")
    assert res["ok"] is True
    assert res["action"] == "set-top"
    assert res["task_id"] == "tsk_abc123"
    # The pointer file was written under the state dir with the tapped task + source eod.
    pointer = json.loads((env["state"] / "tomorrow-pointer.json").read_text())
    assert pointer["task_id"] == "tsk_abc123"
    assert pointer["source"] == "eod"
    # No board mutation: the task is untouched (set-top only writes the pointer).
    assert "tsk_abc123" in env["work"].read_text()


def test_stale_top_tap_is_clean_no_pointer_for_dead_id(env):
    # A tap on a task no longer active (already done first) must NOT set a dead pointer.
    _result("done:tsk_abc123")
    res = _result("top:tsk_abc123")
    assert res["ok"] is False
    assert res["reason"] == "not-active"
    assert not (env["state"] / "tomorrow-pointer.json").exists()


# --- U10 start: the priority-nag Start button routes to the H7 handle_start -----------------

def test_start_action_maps_to_the_nag_commands_start_verb():
    # The decode->command routing table maps `start` to nag_commands' `start` verb with the
    # default button form `["start", "--", <task_id>]` (the `--` argv guard before the id).
    module, build_argv = callback_dispatch._ACTION_TO_COMMAND["start"]
    assert module == "nag_commands.py"
    assert build_argv("tsk_abc123", None, "5") == ["start", "--", "tsk_abc123"]


def test_start_tap_routes_through_handle_start(env):
    """A `start` tap routes to the EXISTING handle_start (H7 initiation loop) -- not the
    `not_yet_available` stub. We assert routing via the active-board guard handle_start
    enforces: a tap on a task NOT on the board is refused with handle_start's own
    `task-not-active` code, which only that command emits -- proving the tap reached it."""
    res = _result("start:tsk_gone999")
    assert res.get("action") == "start"          # decoded `start` verb reported
    assert res.get("reason") != "not_yet_available"  # it IS wired (not a stub)
    assert res["ok"] is False
    assert res.get("error", {}).get("code") == "task-not-active"  # handle_start's own guard


def test_start_tap_uses_the_argv_dash_guard(env):
    """The `start` argv inserts `--` before the task id so a flag-shaped id is a literal
    positional (no argv-flag escape), exactly like the sibling verbs."""
    # A flag-shaped id (`-x`) is decoded fine (no `:`), routed as `start -- -x`, and treated
    # as a literal task id -> task-not-active (not parsed as an argparse option).
    res = _result("start:-x")
    assert res.get("action") == "start"
    assert res["ok"] is False
    assert res.get("error", {}).get("code") == "task-not-active"


# --- no raw error leak: a malformed args JSON is a friendly line, exit 0, no traceback -------

def test_malformed_args_json_no_raw_leak(env):
    # Pass a non-JSON argv element DIRECTLY (not via json.dumps) -- the real failure mode if the
    # plugin ever handed a corrupt args blob. _parse_args raises, the envelope catches it, prints
    # ONE friendly line, and exits 0. No traceback, no exception class, no file path.
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "callback_dispatch.py"), "this is not json"],
        capture_output=True, text=True, check=False,
    )
    out = completed.stdout.strip()
    assert completed.returncode == 0  # the envelope keeps the exit code at 0 for the relay
    assert "Traceback" not in out
    assert "Exception" not in out and ".py" not in out
    # The envelope's friendly one-liner is emitted (its reserved phrasing), not a JSON result.
    assert "unavailable" in out.lower()


def test_empty_argv_no_raw_leak(env):
    # No argv element at all -> still a friendly handled outcome, never a crash.
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "callback_dispatch.py")],
        capture_output=True, text=True, check=False,
    )
    assert completed.returncode == 0
    assert "Traceback" not in completed.stdout


# --- the dispatch() function as a unit (no subprocess) for the pure routing decisions --------

def test_dispatch_unit_decode_miss(env):
    res = callback_dispatch.dispatch({"callback_data": "garbage", "sender_id": "x", "topic_id": "5"})
    assert res["ok"] is False and res["reason"] == "undecodable"


def test_dispatch_unit_namespace_reprepended(env):
    # The plugin hands the payload WITHOUT the ``tt:`` prefix (the gateway split it off); dispatch
    # re-prepends it so decode (the single source of truth for the scheme) accepts it. ``top`` is
    # wired in U6, so its decode round-trips and routes to the ``set-top`` command verb.
    res = callback_dispatch.dispatch({"callback_data": "top:tsk_abc123", "sender_id": "x", "topic_id": "5"})
    assert res["action"] == "set-top" and res["ok"] is True
