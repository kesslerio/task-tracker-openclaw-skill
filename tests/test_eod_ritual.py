"""U4 EOD ritual (detect + confirm-gate) -- behavioral invariant tests.

These assert the U4 invariants, not the implementation path:

* DETECT: the ``done24h`` harvest matches a merged PR to an open board task and U4
  renders a ``tt:appr:<task_id>`` Confirm button for it.
* NO BOARD CHANGE WITHOUT A TAP: running detect/build leaves the board AND the
  ledger byte-for-byte unchanged -- U4 marks nothing done; only a later tap ->
  ``harvest_ledger.approve`` mutates anything.
* CLEAN ZERO-DETECTION PATH: no detections renders a single "nothing auto-detected"
  line, NOT an empty confirm prompt, and carries no buttons.
* SOURCE FAILURE IS ABSORBED: a non-zero ``gh``/``gog`` trips the existing circuit
  breaker inside the harvest; U4 still completes the detect step and flags it.

Fixtures mirror ``test_harvest_ledger.py`` (``env`` tmp board+ledger+state,
``_set_productivity_env``, ``_stub_sources``) so U4 detection is exercised over the
real harvest, not a mock of it.

Public-repo hygiene: the only chat id here is the FAKE ``-4242424242`` (does NOT
match the ``-100[0-9]{8,}`` pattern the CI hygiene grep flags). Real ids are
env-sourced at runtime, never committed.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import eod_ritual
import harvest_ledger
import harvest_state
import nag_commands
import telegram_buttons
import tomorrow_pointer
import utils

# Fake id: valid chat-id shape but not -100xxxxxxxx, so the hygiene grep is clean.
PRODUCTIVITY = "-4242424242"
DONE_TOPIC = "5"

WORK_BOARD = """# Work

## 🔴 Q1
- [ ] **Add social updates to World Cup skill** task_id::tsk_abc123 area:: Delivery
- [ ] **Camp coordination for June** task_id::tsk_def456 area:: Ops
"""

PR_EVIDENCE = {
    "title": "Add social updates to World Cup skill",
    "number": 7,
    "repository": {"nameWithOwner": "kesslerio/world-cup-soccer-openclaw-skill"},
    "url": "https://example.test/pr/7",
}


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolate every state file + the board + the ledger under tmp_path."""
    state_dir = tmp_path / "state"
    work = tmp_path / "Work Tasks.md"
    work.write_text(WORK_BOARD)
    ledger = tmp_path / "events.jsonl"
    daily = tmp_path / "daily"

    monkeypatch.setenv("TASK_MGMT_STATE_DIR", str(state_dir))
    monkeypatch.setenv("TASK_TRACKER_WORK_FILE", str(work))
    monkeypatch.setenv("TASK_TRACKER_LEDGER_FILE", str(ledger))
    monkeypatch.setenv("TASK_TRACKER_DAILY_NOTES_DIR", str(daily))
    monkeypatch.setenv("TASK_TRACKER_DONE_LOG_DIR", str(daily))
    monkeypatch.setenv("TASK_TRACKER_ERROR_LOG", str(state_dir / "errors.jsonl"))
    monkeypatch.setattr(utils, "OBSIDIAN_WORK", work)
    return {"work": work, "ledger": ledger, "state_dir": state_dir, "daily": daily}


def _set_productivity_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID_PRODUCTIVITY", PRODUCTIVITY)
    monkeypatch.setenv("OPENCLAW_TOPIC_PRODUCTIVITY_DONE", DONE_TOPIC)


class _FakeCompleted:
    def __init__(self, returncode, stdout):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


def _stub_sources(monkeypatch, *, gh_payload=None, gog_payload=None, gh_rc=0, gog_rc=0):
    """Stub the harvest subprocesses by intercepting subprocess.run in the module."""

    def fake_run(cmd, **kwargs):
        if cmd[0] == "gh":
            return _FakeCompleted(gh_rc, json.dumps(gh_payload if gh_payload is not None else []))
        if cmd[0] == "gog":
            return _FakeCompleted(gog_rc, json.dumps(gog_payload if gog_payload is not None else {"threads": []}))
        raise AssertionError(f"unexpected command {cmd!r}")

    monkeypatch.setattr(harvest_ledger.subprocess, "run", fake_run)


def _ledger_event_types(ledger_path: Path) -> list[str]:
    if not ledger_path.exists():
        return []
    return [json.loads(line)["event_type"] for line in ledger_path.read_text().splitlines() if line.strip()]


# --- DETECT: a PR matching an open task renders a tt:appr Confirm button ------


def test_detect_renders_appr_confirm_button_for_matched_pr(env, monkeypatch):
    _set_productivity_env(monkeypatch)
    _stub_sources(monkeypatch, gh_payload=[PR_EVIDENCE])

    payload = eod_ritual.build_confirm_step()

    assert payload["detection_count"] == 1
    det = payload["detections"][0]
    assert det["task_id"] == "tsk_abc123"
    # Exactly one Confirm button, carrying the tt:appr:<id> callback value.
    assert len(det["buttons"]) == 1
    assert det["buttons"][0]["value"] == telegram_buttons.encode("appr", "tsk_abc123")
    assert det["buttons"][0]["value"] == "tt:appr:tsk_abc123"
    # The decoded callback round-trips to the appr action on the same task.
    assert telegram_buttons.decode(det["buttons"][0]["value"]) == ("appr", "tsk_abc123", None)
    # The confirm text names the detected work and the no-change-until-tap guarantee.
    assert "Add social updates to World Cup skill" in payload["message"]
    assert "until you tap" in payload["message"]


# --- NO BOARD CHANGE WITHOUT A TAP -------------------------------------------


def test_detect_does_not_touch_board_or_ledger(env, monkeypatch):
    """Detect/build RENDERS the confirm step but mutates nothing: the board is
    unchanged and no completion/approval event is written. Only a later tap ->
    harvest_ledger.approve marks anything done."""
    _set_productivity_env(monkeypatch)
    _stub_sources(monkeypatch, gh_payload=[PR_EVIDENCE])

    board_before = env["work"].read_text()
    payload = eod_ritual.build_confirm_step()
    # A detection was rendered (so this is the meaningful no-mutation assertion, not
    # a vacuous one over an empty detect).
    assert payload["detection_count"] == 1

    # The board is byte-for-byte unchanged: the task is still open and unchecked.
    assert env["work"].read_text() == board_before
    assert "- [ ] **Add social updates to World Cup skill**" in env["work"].read_text()

    # No completion/approval event leaked from the detect step.
    types = _ledger_event_types(env["ledger"])
    assert "ledger_approved" not in types
    assert "task_completed" not in types
    assert "evidence_link" not in types

    # And the harvest ran DRY: no 24h state file was written (detection consumes
    # nothing, so the same PR re-surfaces next run until the user taps Confirm).
    assert not (env["state_dir"] / "harvest-state-24h.json").exists()


def test_detect_is_repeatable_without_consuming(env, monkeypatch):
    """Because detect is dry-run, the SAME detection surfaces on a second run --
    nothing was consumed, so an un-tapped completion is never silently dropped."""
    _set_productivity_env(monkeypatch)
    _stub_sources(monkeypatch, gh_payload=[PR_EVIDENCE])

    first = eod_ritual.build_confirm_step()
    second = eod_ritual.build_confirm_step()
    assert first["detection_count"] == 1
    assert second["detection_count"] == 1
    assert second["detections"][0]["task_id"] == "tsk_abc123"


# --- CLEAN ZERO-DETECTION PATH -----------------------------------------------


def test_zero_detections_is_a_clean_path_not_an_empty_prompt(env, monkeypatch):
    _set_productivity_env(monkeypatch)
    _stub_sources(monkeypatch, gh_payload=[], gog_payload={"threads": []})

    payload = eod_ritual.build_confirm_step()

    assert payload["detection_count"] == 0
    assert payload["detections"] == []
    # A single clean line, NOT an empty confirm prompt.
    assert "Nothing auto-detected" in payload["message"]
    # No Confirm button anywhere, and no "until you tap" prompt for absent items.
    assert "tt:appr" not in payload["message"]
    assert "Tap Confirm" not in payload["message"]


# --- A no-match item is reported but NOT confirmable (no spurious button) -----


def test_unmatched_evidence_is_not_confirmable(env, monkeypatch):
    """Detected work that does not confidently link to one open task carries NO
    Confirm button (there is no single task for approve to act on)."""
    _set_productivity_env(monkeypatch)
    _stub_sources(
        monkeypatch,
        gh_payload=[{
            "title": "Totally unrelated drive-by fix in some other repo",
            "number": 99,
            "repository": {"nameWithOwner": "kesslerio/unrelated"},
            "url": "https://example.test/pr/99",
        }],
    )

    payload = eod_ritual.build_confirm_step()

    assert payload["detection_count"] == 0
    assert payload["other_evidence_count"] >= 1
    # No board task was confirmed; nothing mutated.
    assert "ledger_approved" not in _ledger_event_types(env["ledger"])


# --- SOURCE FAILURE IS ABSORBED; U4 still completes --------------------------


def test_broken_harvest_source_still_completes(env, monkeypatch):
    """A non-zero gh trips the harvest's circuit breaker / source-error path; the
    detect step still completes and flags harvest_unavailable (no raw error leak)."""
    _set_productivity_env(monkeypatch)
    # gh fails (rc=1); gog returns clean-empty.
    _stub_sources(monkeypatch, gh_rc=1, gog_payload={"threads": []})

    payload = eod_ritual.build_confirm_step()

    assert payload["ok"] is True
    assert payload["harvest_unavailable"] is True
    # No detections from a failed source, but the step did not abort.
    assert payload["detection_count"] == 0
    # The degraded note is surfaced, with NO raw traceback/exception text.
    assert "unavailable" in payload["message"]
    assert "Traceback" not in json.dumps(payload, default=str)
    # The underlying gh failure was logged to the structured error log.
    log = env["state_dir"] / "errors.jsonl"
    entries = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    assert any(e["component"] == "ledger_harvest:github" for e in entries)


# --- CLI: main() emits the structured payload and exits 0 --------------------


def test_main_json_exits_zero_and_emits_payload(env, monkeypatch, capsys):
    _set_productivity_env(monkeypatch)
    _stub_sources(monkeypatch, gh_payload=[PR_EVIDENCE])

    rc = eod_ritual.main(["--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["step"] == "detect_confirm"
    assert out["detection_count"] == 1
    assert out["detections"][0]["buttons"][0]["value"] == "tt:appr:tsk_abc123"


# --- U5 forced disposition: every open task gets a button row, board UNCHANGED ----


def test_disposition_renders_a_four_button_row_per_open_task(env):
    payload = eod_ritual.build_disposition_step()

    # Both open tasks are surfaced for a forced disposition.
    assert payload["step"] == "disposition"
    assert payload["open_count"] == 2
    ids = {item["task_id"] for item in payload["items"]}
    assert ids == {"tsk_abc123", "tsk_def456"}

    item = next(i for i in payload["items"] if i["task_id"] == "tsk_abc123")
    values = [b["value"] for b in item["buttons"]]
    # The KTD-3 disposition row: done / carry / reschedule / drop.
    assert values == [
        "tt:done:tsk_abc123",
        "tt:carry:tsk_abc123",
        "tt:rsch:tsk_abc123",
        "tt:drop:tsk_abc123",
    ]
    # Every open task is reported as needing a disposition (no decision until a tap).
    assert item["needs_disposition"] is True
    assert "need a disposition" in payload["message"]


def test_disposition_does_not_mutate_the_board(env):
    """The disposition step RENDERS the buttons but mutates nothing -- mirroring the
    no-change-without-confirm invariant. Only a later tap changes the board."""
    board_before = env["work"].read_text()
    ledger_before = env["ledger"].read_text() if env["ledger"].exists() else ""

    payload = eod_ritual.build_disposition_step()
    assert payload["open_count"] == 2  # a meaningful (non-vacuous) no-mutation assertion

    assert env["work"].read_text() == board_before
    after = env["ledger"].read_text() if env["ledger"].exists() else ""
    assert after == ledger_before
    # No disposition events leaked from the read-only render step.
    assert "eod_disposition_carry" not in _ledger_event_types(env["ledger"])
    assert "eod_disposition_drop" not in _ledger_event_types(env["ledger"])


def test_disposition_untapped_task_is_reported_needs_disposition_board_unchanged(env):
    """An un-tapped task is REPORTED (needs_disposition_count > 0), never silently
    carried or dropped -- the board is byte-for-byte unchanged."""
    board_before = env["work"].read_text()
    payload = eod_ritual.build_disposition_step()

    assert payload["needs_disposition_count"] == 2
    assert all(item["needs_disposition"] for item in payload["items"])
    assert env["work"].read_text() == board_before


def test_disposition_empty_board_is_a_clean_no_op(env):
    """An empty board is a clean no-op (open_count 0), proceeding to tomorrow's #1
    (U6) rather than rendering an empty prompt."""
    env["work"].write_text("# Work\n\n## 🔴 Q1\n")

    payload = eod_ritual.build_disposition_step()

    assert payload["open_count"] == 0
    assert payload["items"] == []
    assert payload["needs_disposition_count"] == 0
    assert "Nothing open" in payload["message"]


def test_disposition_main_step_flag_emits_payload(env, capsys):
    rc = eod_ritual.main(["--json", "--step", "disposition"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["step"] == "disposition"
    assert out["open_count"] == 2
    assert out["items"][0]["buttons"][0]["value"].startswith("tt:done:")


def test_recurring_done_disposition_spawns_next_not_a_carried_dup(env, monkeypatch):
    """A recurring task dispositioned DONE rolls forward its next occurrence via the
    existing recurrence path -- it is NOT a carried duplicate (a done disposition reuses
    complete_by_id, whose recurrence handling spawns the next due date)."""
    import nag_commands

    env["work"].write_text("""# Work

## 🔴 Q1
- [ ] **Send weekly update** task_id::tsk_weekly recur::weekly 🗓️2026-05-20
""")
    result = nag_commands.handle_done("tsk_weekly")
    assert result["ok"] is True
    assert result.get("recurring") is True
    content = env["work"].read_text()
    # The recurrence rolled forward to the next week (NOT a carried:: marker, NOT a dup).
    assert content.count("Send weekly update") == 1
    assert "🗓️2026-05-27" in content
    assert "carried::" not in content


# --- U6 set tomorrow's #1: propose (read-only) + tap writes the single pointer ----


def test_tomorrow_step_proposes_a_top_with_a_set_button(env):
    """The EOD proposes a #1 from the open board, rendered with a tt:top Set-as-#1
    button plus alternatives. Proposing writes NOTHING (the pointer is set on a tap)."""
    payload = eod_ritual.build_tomorrow_step()

    assert payload["step"] == "tomorrow_top"
    assert payload["has_open"] is True
    # The top pick carries a tt:top:<id> button.
    top = payload["top"]
    assert top["task_id"] in {"tsk_abc123", "tsk_def456"}
    assert top["buttons"][0]["value"] == telegram_buttons.encode("top", top["task_id"])
    assert top["buttons"][0]["value"].startswith("tt:top:")
    # Alternatives are offered (both open tasks are candidates).
    ids = {c["task_id"] for c in payload["candidates"]}
    assert ids == {"tsk_abc123", "tsk_def456"}
    # Proposing wrote NO pointer (no change until a tap).
    assert tomorrow_pointer.read_pointer() is None
    assert "tomorrow's #1" in payload["message"]


def test_tapping_a_proposed_top_writes_the_pointer_source_eod(env, monkeypatch):
    """Tapping a proposed #1 (-> set-top command) writes tomorrow-pointer.json with the
    task + source:'eod'. This is the U2-dispatcher-resolved write side."""
    payload = eod_ritual.build_tomorrow_step()
    top_id = payload["top"]["task_id"]

    result = nag_commands.handle_set_top(top_id)
    assert result["ok"] is True
    assert result["source"] == "eod"

    pointer = tomorrow_pointer.read_pointer()
    assert pointer["task_id"] == top_id
    assert pointer["source"] == "eod"
    # The ledger recorded WHICH task became tomorrow's #1.
    assert "eod_tomorrow_top_set" in _ledger_event_types(env["ledger"])
    # set-top writes only the pointer -- the board is untouched.
    assert top_id in env["work"].read_text()


def test_retapping_a_different_task_overwrites_single_pointer(env):
    """Re-tapping a DIFFERENT task overwrites the pointer (single canonical pointer,
    never appended)."""
    nag_commands.handle_set_top("tsk_abc123")
    nag_commands.handle_set_top("tsk_def456")

    pointer = tomorrow_pointer.read_pointer()
    assert pointer["task_id"] == "tsk_def456"
    # One canonical record on disk, not an appended pair.
    raw = (env["state_dir"] / "tomorrow-pointer.json").read_text()
    assert raw.count('"task_id"') == 1


def test_no_open_tasks_writes_explicit_none_pointer(env):
    """An empty board records an explicit 'none' pointer so the standup shows a clean
    board, not a stale prior-day #1."""
    env["work"].write_text("# Work\n\n## 🔴 Q1\n")

    payload = eod_ritual.build_tomorrow_step()
    assert payload["has_open"] is False
    assert payload["wrote_none"] is True
    assert "board is clear" in payload["message"]

    pointer = tomorrow_pointer.read_pointer()
    assert tomorrow_pointer.is_none_pointer(pointer) is True
    assert pointer["task_id"] is None


def test_empty_board_none_overwrites_a_stale_prior_pointer(env):
    """A stale prior-day pointer is overwritten by the empty-board 'none' -- never a
    leftover stale #1 the standup would resurface."""
    # A real #1 is set first (a prior day).
    nag_commands.handle_set_top("tsk_abc123")
    assert tomorrow_pointer.read_pointer()["task_id"] == "tsk_abc123"

    # Next EOD runs against an empty board.
    env["work"].write_text("# Work\n\n## 🔴 Q1\n")
    eod_ritual.build_tomorrow_step()

    pointer = tomorrow_pointer.read_pointer()
    assert tomorrow_pointer.is_none_pointer(pointer) is True
    assert "tsk_abc123" not in (env["state_dir"] / "tomorrow-pointer.json").read_text()


def test_set_top_stale_tap_refused_no_dead_pointer(env):
    """A tap to set a task that is no longer active (already done) is refused -- no dead
    pointer the standup would resolve to nothing."""
    nag_commands.handle_done("tsk_abc123")
    result = nag_commands.handle_set_top("tsk_abc123")
    assert result["ok"] is False
    assert result["reason"] == "not-active"
    assert tomorrow_pointer.read_pointer() is None


def test_tomorrow_main_step_flag_emits_payload(env, capsys):
    rc = eod_ritual.main(["--json", "--step", "tomorrow"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["step"] == "tomorrow_top"
    assert out["has_open"] is True
    assert out["top"]["buttons"][0]["value"].startswith("tt:top:")
