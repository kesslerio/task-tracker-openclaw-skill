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
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import eod_ritual
import harvest_auto
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
    personal = tmp_path / "Personal Tasks.md"
    work.write_text(WORK_BOARD)
    personal.write_text("""# Personal

## Q1
- [ ] **Buy groceries** task_id::tsk_personal area:: Home
""")
    ledger = tmp_path / "events.jsonl"
    daily = tmp_path / "daily"

    monkeypatch.setenv("TASK_MGMT_STATE_DIR", str(state_dir))
    monkeypatch.setenv("TASK_TRACKER_WORK_FILE", str(work))
    monkeypatch.setenv("TASK_TRACKER_PERSONAL_FILE", str(personal))
    monkeypatch.setenv("TASK_TRACKER_LEDGER_FILE", str(ledger))
    monkeypatch.setenv("TASK_TRACKER_DAILY_NOTES_DIR", str(daily))
    monkeypatch.setenv("TASK_TRACKER_DONE_LOG_DIR", str(daily))
    monkeypatch.setenv("TASK_TRACKER_ERROR_LOG", str(state_dir / "errors.jsonl"))
    monkeypatch.setattr(utils, "OBSIDIAN_WORK", work)
    monkeypatch.setattr(utils, "OBSIDIAN_PERSONAL", personal)
    return {"work": work, "personal": personal, "ledger": ledger, "state_dir": state_dir, "daily": daily}


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


def test_auto_completed_task_is_named_when_no_candidates_remain(env, monkeypatch):
    env["work"].write_text("""# Work

## 🔴 Q1
- [ ] **Add social updates to World Cup skill** https://github.com/kesslerio/world-cup-skill/issues/101 task_id::tsk_abc123 area:: Delivery
""")
    monkeypatch.setenv(harvest_auto.AUTO_ENV, "true")
    monkeypatch.setenv("TASK_TRACKER_GITHUB_OWNER", "niemand")
    _set_productivity_env(monkeypatch)
    _stub_sources(
        monkeypatch,
        gh_payload=[{
            "title": "Ship social updates",
            "number": 7,
            "repository": {"nameWithOwner": "kesslerio/world-cup-skill"},
            "url": "https://github.com/kesslerio/world-cup-skill/pull/7",
            "state": "MERGED",
            "mergedAt": "2026-06-23T18:00:00Z",
            "author": {"login": "niemand"},
            "closingIssuesReferences": [{
                "number": 101,
                "repository": {"nameWithOwner": "kesslerio/world-cup-skill"},
                "url": "https://github.com/kesslerio/world-cup-skill/issues/101",
            }],
        }],
        gog_payload={"threads": []},
    )

    payload = eod_ritual.build_confirm_step()

    assert payload["completed_count"] == 1
    assert payload["detection_count"] == 0
    assert payload["auto_completed"] == [{
        "task_id": "tsk_abc123",
        "title": "Add social updates to World Cup skill",
        "source": "merged_pr",
        "url": "https://github.com/kesslerio/world-cup-skill/pull/7",
    }]
    assert "Auto-completed 1 task" in payload["message"]
    assert "Add social updates to World Cup skill" in payload["message"]
    assert "Nothing auto-detected" not in payload["message"]
    assert "tsk_abc123" not in env["work"].read_text()


def test_confirm_step_threads_frozen_now_into_auto_harvest_window(env, monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[0] == "gh":
            return _FakeCompleted(0, "[]")
        if cmd[0] == "gog":
            return _FakeCompleted(0, json.dumps({"threads": []}))
        raise AssertionError(f"unexpected command {cmd!r}")

    monkeypatch.setattr(harvest_ledger.subprocess, "run", fake_run)

    payload = eod_ritual.build_confirm_step(now=datetime.fromisoformat("2026-06-23T18:00:00-07:00"))

    assert payload["harvest_window_id"] == "2026-W26:2026-06-23:standup"
    gh_cmd = next(cmd for cmd in calls if cmd[0] == "gh")
    assert any("2026-06-22..2026-06-23" in arg for arg in gh_cmd)
    gmail_cmd = next(cmd for cmd in calls if cmd[0] == "gog")
    assert any("in:sent after:2026/06/22 before:2026/06/24" in arg for arg in gmail_cmd)
    assert payload["message"].count("Nothing auto-detected") == 1


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


def test_main_json_detect_step_exits_zero_and_emits_payload(env, monkeypatch, capsys):
    # The detect/confirm preview is now behind `--step detect` (the default is the full
    # delivered run, U7). The read-only preview never delivers, so a stubbed harvest is enough.
    _set_productivity_env(monkeypatch)
    _stub_sources(monkeypatch, gh_payload=[PR_EVIDENCE])

    rc = eod_ritual.main(["--json", "--step", "detect"])
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


# --- U7: delivery seam + ## EOD Summary upsert + health + cron descriptor ---------


class _StubSender:
    """A fake receipt-returning sender: records every (target, text, buttons) call.

    Mirrors the production ``openclaw_sender`` contract -- returns ``{"message_id": ...}``
    -- so ``deliver_once`` records a real receipt and the idem-key dedup is exercised.
    """

    def __init__(self):
        self.calls = []

    def __call__(self, target, text, buttons=None):
        self.calls.append({"target": target, "text": text, "buttons": buttons})
        return {"message_id": f"msg-{len(self.calls)}"}


def test_personal_eod_does_not_auto_complete_work_board(env, monkeypatch):
    env["work"].write_text("""# Work

## 🔴 Q1
- [ ] **Add social updates to World Cup skill** https://github.com/kesslerio/world-cup-skill/issues/101 task_id::tsk_abc123 area:: Delivery
""")
    monkeypatch.setenv(harvest_auto.AUTO_ENV, "true")
    monkeypatch.setenv("TASK_TRACKER_GITHUB_OWNER", "niemand")
    _set_productivity_env(monkeypatch)
    _stub_sources(
        monkeypatch,
        gh_payload=[{
            "title": "Ship social updates",
            "number": 7,
            "repository": {"nameWithOwner": "kesslerio/world-cup-skill"},
            "url": "https://github.com/kesslerio/world-cup-skill/pull/7",
            "state": "MERGED",
            "mergedAt": "2026-06-23T18:00:00Z",
            "author": {"login": "niemand"},
            "closingIssuesReferences": [{
                "number": 101,
                "repository": {"nameWithOwner": "kesslerio/world-cup-skill"},
                "url": "https://github.com/kesslerio/world-cup-skill/issues/101",
            }],
        }],
    )

    result = eod_ritual.run(personal=True, sender=_StubSender())

    assert result["ok"] is True
    assert "tsk_abc123" in env["work"].read_text()
    assert "Buy groceries" in env["personal"].read_text()


def _read_summary(env):
    """Read the day's daily note (the ## EOD Summary lands here), or '' if absent."""
    from cos_config import local_today

    note = env["daily"] / f"{local_today().strftime('%Y-%m-%d')}.md"
    return note.read_text() if note.exists() else ""


# The per-task ACTION actions: a task's disposition row (done/carry/rsch/drop) and the
# confirm (appr). The ``top`` picker is DELIBERATELY excluded -- the tomorrow's-#1 chunk
# is one decision (pick ONE #1 among ~3 candidates), the contract's single allowed
# multi-button exception; it is not a per-task action grid.
_PER_TASK_ACTIONS = {"done", "carry", "rsch", "drop", "appr", "snz", "start"}


def _task_ids_in_buttons(buttons):
    """The DISTINCT task_ids the button rows reference, decoded via the tt: codec."""
    ids = set()
    for button in buttons or []:
        decoded = telegram_buttons.decode(button["value"])
        assert decoded is not None, f"un-decodable callback {button['value']!r}"
        ids.add(decoded[1])
    return ids


def _action_task_ids_in_buttons(buttons):
    """The DISTINCT task_ids referenced by PER-TASK ACTION buttons in a message.

    THE REGRESSION GUARD: a single message's per-task action buttons (done/carry/rsch/
    drop/appr) must reference AT MOST one task_id, or the unreadable multi-task grid (the
    shipped bug) is back. The ``top`` picker is excluded -- that one chunk is the
    deliberate single-decision exception (pick one #1 among candidates)."""
    ids = set()
    for button in buttons or []:
        decoded = telegram_buttons.decode(button["value"])
        assert decoded is not None, f"un-decodable callback {button['value']!r}"
        action, task_id, _arg = decoded
        if action in _PER_TASK_ACTIONS:
            ids.add(task_id)
    return ids


def test_full_eod_run_delivers_a_sequence_of_one_item_messages_and_upserts_summary(env, monkeypatch):
    """A full EOD run delivers a SEQUENCE of small one-item messages (NOT one mega-message),
    upserts a ## EOD Summary to the daily note, and records the end-to-end eod_summary_written
    audit event -- the nag-style happy path that fixes the unreadable single-message grid."""
    import cos_health

    _set_productivity_env(monkeypatch)
    _stub_sources(monkeypatch, gh_payload=[PR_EVIDENCE])
    sender = _StubSender()

    result = eod_ritual.run(sender=sender)

    assert result["ok"] is True
    assert result["delivered"] is True
    assert result["idempotent"] is False
    # A LIST of messages, not one: the board has 1 detection + 2 open tasks + the #1 step,
    # so the EOD does 4 receipted sends (one confirm, one disposition per task, one #1).
    assert len(sender.calls) == 4
    assert result["messages_sent"] == 4
    # Every send hit the proven DONE-thread target.
    assert all(c["target"]["chat_id"] == PRODUCTIVITY for c in sender.calls)
    assert all(c["target"]["topic_id"] == DONE_TOPIC for c in sender.calls)
    # REGRESSION GUARD (the bug): NO single message's PER-TASK ACTION buttons reference
    # more than one task_id. The shipped mega-message crammed Done/Carry/Reschedule/Drop
    # × N tasks into one grid; here each message carries at most one task's action row.
    for call in sender.calls:
        assert len(_action_task_ids_in_buttons(call["buttons"])) <= 1, \
            f"a single message carried action buttons for >1 task: {call['buttons']!r}"

    # The ## EOD Summary was upserted to the daily note (done today / still-open / #1).
    summary = _read_summary(env)
    assert summary.count("## EOD Summary") == 1
    assert "Done today" in summary and "Still open" in summary and "Tomorrow's #1" in summary

    # The end-to-end audit event carries the receipt ids + the summary path.
    types = _ledger_event_types(env["ledger"])
    assert "eod_summary_written" in types

    # eod_review health SUCCESS is recorded when the full run delivers under the envelope
    # (eod_review is already in cos_manifest.EXPECTED_RITUALS -- U7 wires the REAL signal).
    monkeypatch.setattr(eod_ritual.outbox, "openclaw_sender", sender)
    rc = eod_ritual.error_envelope.run_main("eod_review", lambda: eod_ritual.main([]), trigger="cron:eod_review")
    assert rc == 0
    assert "last_success_ts" in cos_health.read_health()["eod_review"]


def test_no_eod_message_carries_more_than_one_tasks_buttons(env, monkeypatch):
    """THE REGRESSION GUARD for the shipped bug: with several open tasks AND a detection,
    every delivered message's button rows reference AT MOST one task_id. The old EOD packed
    every task's buttons into one flat grid -- this asserts that can never happen again."""
    env["work"].write_text("""# Work

## 🔴 Q1
- [ ] **Add social updates to World Cup skill** task_id::tsk_abc123 area:: Delivery
- [ ] **Camp coordination for June** task_id::tsk_def456 🗓️2026-06-10 area:: Ops
- [ ] **Third open task** task_id::tsk_ghi789 area:: Ops
- [ ] **Fourth open task** task_id::tsk_jkl012 🗓️2026-06-01 area:: Ops
""")
    _set_productivity_env(monkeypatch)
    _stub_sources(monkeypatch, gh_payload=[PR_EVIDENCE])
    sender = _StubSender()

    result = eod_ritual.run(sender=sender)
    assert result["delivered"] is True

    # Several messages were sent (a confirm + per-task disposition messages + the #1).
    assert len(sender.calls) >= 3
    seen_action_button = False
    for call in sender.calls:
        ids = _action_task_ids_in_buttons(call["buttons"])
        assert len(ids) <= 1, f"a message carried action buttons for >1 task: {ids}"
        if ids:
            seen_action_button = True
    # The guard is non-vacuous: at least one message actually carried a per-task action row.
    assert seen_action_button
    # And the ONLY multi-task-button message is the tomorrow's-#1 picker -- whose buttons
    # are ALL the `top` action (one decision: pick one #1), never a disposition grid.
    multi = [c for c in sender.calls if len(_task_ids_in_buttons(c["buttons"])) > 1]
    for call in multi:
        actions = {telegram_buttons.decode(b["value"])[0] for b in call["buttons"]}
        assert actions == {"top"}, f"a multi-task message was not the #1 picker: {actions}"


def test_confirmed_taps_are_not_coupled_to_delivery(env, monkeypatch):
    """The board mutations U4/U5/U6 commit happen ONLY on taps; a delivery never mutates
    the board. A full run with a working sender leaves the board byte-for-byte unchanged
    (the EOD only RENDERS buttons; the user's taps -- elsewhere -- are what mutate)."""
    _set_productivity_env(monkeypatch)
    _stub_sources(monkeypatch, gh_payload=[PR_EVIDENCE])
    board_before = env["work"].read_text()

    result = eod_ritual.run(sender=_StubSender())
    assert result["delivered"] is True

    # No board mutation rode the delivery -- both tasks are still open + unchecked.
    assert env["work"].read_text() == board_before


def test_env_unset_blocks_clean_no_partial_send(env, monkeypatch):
    """Env unset -> the EOD is BLOCKED with a clear reason, NO partial send, and NO
    ## EOD Summary written. The confirmed taps (elsewhere) are untouched -- delivery
    failure is decoupled from board state."""
    # Deliberately do NOT set the productivity env; clear any inherited value.
    monkeypatch.delenv("TELEGRAM_CHAT_ID_PRODUCTIVITY", raising=False)
    monkeypatch.delenv("OPENCLAW_TOPIC_PRODUCTIVITY_DONE", raising=False)
    _stub_sources(monkeypatch, gh_payload=[PR_EVIDENCE])
    sender = _StubSender()

    result = eod_ritual.run(sender=sender)

    assert result["ok"] is False
    assert result["delivered"] is False
    assert result["reason"] == "env_missing"
    # NOTHING left: no send, no summary file, no end-to-end audit event.
    assert sender.calls == []
    assert _read_summary(env) == ""
    assert "eod_summary_written" not in _ledger_event_types(env["ledger"])


def test_forced_failure_records_eod_review_health_failure(env, monkeypatch):
    """A forced failure inside the EOD records an eod_review health FAILURE through the
    run_main envelope (so /health flags it), with no raw error leak."""
    import cos_health

    _set_productivity_env(monkeypatch)
    _stub_sources(monkeypatch, gh_payload=[PR_EVIDENCE])

    def boom():
        raise RuntimeError("forced EOD failure")

    monkeypatch.setattr(eod_ritual, "run", boom)
    rc = eod_ritual.error_envelope.run_main("eod_review", lambda: eod_ritual.main([]), trigger="cron:eod_review")
    # The envelope swallows the crash to a friendly exit 0...
    assert rc == 0
    # ...but the machine-visible health is a FAILURE.
    entry = cos_health.read_health()["eod_review"]
    assert "last_failure" in entry


def test_blocked_delivery_returns_nonzero_so_run_main_records_failure(env, monkeypatch):
    """A blocked delivery (env unset) makes main() return nonzero, which run_main turns
    into an eod_review health FAILURE -- a blocked EOD must not false-green."""
    import cos_health

    monkeypatch.delenv("TELEGRAM_CHAT_ID_PRODUCTIVITY", raising=False)
    monkeypatch.delenv("OPENCLAW_TOPIC_PRODUCTIVITY_DONE", raising=False)
    _stub_sources(monkeypatch, gh_payload=[])

    # main() returns 1 on a blocked delivery; run_main records the FAILURE and returns the
    # diagnostic code verbatim (coercing the cron-relay exit-0 is the shell wrapper's job).
    rc = eod_ritual.error_envelope.run_main("eod_review", lambda: eod_ritual.main([]), trigger="cron:eod_review")
    assert rc == 1
    assert "last_failure" in cos_health.read_health()["eod_review"]


def test_refire_does_not_double_send_or_duplicate_summary(env, monkeypatch):
    """Re-firing the EOD in the same day does NOT re-send ANY item (each item's per-item
    idem-key short-circuits to its recorded receipt) and does NOT duplicate the ## EOD
    Summary (the upsert REPLACES, never appends) -- the per-item idempotency guarantee."""
    _set_productivity_env(monkeypatch)
    _stub_sources(monkeypatch, gh_payload=[PR_EVIDENCE])
    sender = _StubSender()

    first = eod_ritual.run(sender=sender)
    sent_on_first = len(sender.calls)
    second = eod_ritual.run(sender=sender)

    assert first["delivered"] is True and first["idempotent"] is False
    # The second fire short-circuits EVERY item on its recorded receipt -- delivered, but
    # idempotent (no item re-sent).
    assert second["delivered"] is True and second["idempotent"] is True
    # The sender was called only on the FIRST fire (the whole sequence deduped on re-fire).
    assert len(sender.calls) == sent_on_first
    # Exactly ONE ## EOD Summary on disk -- the re-run replaced, never appended.
    assert _read_summary(env).count("## EOD Summary") == 1


def test_refire_with_a_new_open_task_still_sends_only_that_item(env, monkeypatch):
    """Per-item idem-keys mean a re-fire skips every already-delivered item but a NEW item
    (a task that became open since the last fire) STILL sends -- one send for the new item,
    none for the rest."""
    _set_productivity_env(monkeypatch)
    _stub_sources(monkeypatch, gh_payload=[])  # no detections, so chunks = disp + #1
    sender = _StubSender()

    first = eod_ritual.run(sender=sender)
    assert first["delivered"] is True
    sent_on_first = len(sender.calls)

    # A third task is added to the board, then the EOD re-fires the same day.
    env["work"].write_text(env["work"].read_text().replace(
        "- [ ] **Camp coordination for June** task_id::tsk_def456 area:: Ops",
        "- [ ] **Camp coordination for June** task_id::tsk_def456 area:: Ops\n"
        "- [ ] **Brand-new task** task_id::tsk_new999 area:: Ops",
    ))
    second = eod_ritual.run(sender=sender)

    assert second["delivered"] is True
    # NOT fully idempotent: a new item sent. Exactly ONE new send (the new task's
    # disposition); every prior item deduped on its recorded receipt.
    assert second["idempotent"] is False
    assert len(sender.calls) == sent_on_first + 1
    last = sender.calls[-1]
    assert _task_ids_in_buttons(last["buttons"]) == {"tsk_new999"}


def test_eod_uses_per_item_idem_keys(env, monkeypatch):
    """The EOD keys each item on its OWN idem-key (eod:<date>:disp:<id> / :conf:<id> / :top),
    so re-fires dedupe per item rather than as one all-or-nothing send."""
    import outbox
    from cos_config import local_today

    _set_productivity_env(monkeypatch)
    _stub_sources(monkeypatch, gh_payload=[PR_EVIDENCE])

    result = eod_ritual.run(sender=_StubSender())
    assert result["delivered"] is True

    day = local_today().strftime("%Y-%m-%d")
    recorded = set(outbox._read_outbox().keys())
    # Per-item keys: a confirm for the detected task, a disposition per open task, the #1.
    assert f"eod:{day}:conf:tsk_abc123" in recorded
    assert f"eod:{day}:disp:tsk_abc123" in recorded
    assert f"eod:{day}:disp:tsk_def456" in recorded
    assert f"eod:{day}:top" in recorded
    # NOT one whole-ritual key (the old single-message design).
    assert f"eod:{day}" not in recorded


def test_disposition_is_capped_and_leads_with_overdue_then_priority(env, monkeypatch):
    """A big board's disposition is CAPPED to EOD_DISPOSITION_LIMIT per-task messages and
    LEADS with overdue / high-priority (q1<q2) tasks, summarising the rest as one buttonless
    "+K more" line -- so a big board does not flood the thread."""
    monkeypatch.setenv("EOD_DISPOSITION_LIMIT", "2")
    # q1 overdue, q2 overdue (older), q1 not-due, q3 not-due. Overdue leads, then q1<q2<q3.
    env["work"].write_text("""# Work

## 🔴 Q1
- [ ] **Q1 overdue recent** task_id::tsk_q1over 🗓️2026-06-20 area:: Ops
- [ ] **Q1 not due** task_id::tsk_q1new area:: Ops

## 🟠 Q2
- [ ] **Q2 overdue old** task_id::tsk_q2over 🗓️2026-06-01 area:: Ops

## 🟢 Q3
- [ ] **Q3 not due** task_id::tsk_q3new area:: Ops
""")
    _set_productivity_env(monkeypatch)
    _stub_sources(monkeypatch, gh_payload=[])

    payload = eod_ritual.build_disposition_step(now=__import__("datetime").datetime(2026, 6, 23))
    assert payload["open_count"] == 4
    # Capped to 2 surfaced items; 2 held back.
    assert len(payload["items"]) == 2
    assert payload["remainder_count"] == 2
    # LEAD order: most-overdue first (the older Q2-overdue beats the recent Q1-overdue on
    # days-overdue), so the two surfaced are the overdue ones, worst first.
    surfaced = [item["task_id"] for item in payload["items"]]
    assert surfaced == ["tsk_q2over", "tsk_q1over"]
    # The "+K more" remainder is summarised in the preview text (no per-task flood).
    assert "+2 more" in payload["message"]


def test_disposition_remainder_is_a_single_buttonless_message(env, monkeypatch):
    """The tasks past the cap ride ONE buttonless "+K more open" message, never a
    message-per-task flood."""
    monkeypatch.setenv("EOD_DISPOSITION_LIMIT", "1")
    env["work"].write_text("""# Work

## 🔴 Q1
- [ ] **First** task_id::tsk_one 🗓️2026-06-01 area:: Ops
- [ ] **Second** task_id::tsk_two area:: Ops
- [ ] **Third** task_id::tsk_three area:: Ops
""")
    _set_productivity_env(monkeypatch)
    _stub_sources(monkeypatch, gh_payload=[])
    sender = _StubSender()

    result = eod_ritual.run(sender=sender)
    assert result["delivered"] is True

    # Find the remainder message: it mentions "more open" and carries NO buttons.
    remainder = [c for c in sender.calls if "more open" in c["text"]]
    assert len(remainder) == 1
    assert not remainder[0]["buttons"]  # the summary line references no single task
    assert "+2 more" in remainder[0]["text"]


def test_summary_upsert_is_idempotent_replaces_not_appends(env):
    """The ## EOD Summary writer REPLACES its section on a re-run (idempotent), never
    appends a second one -- proven directly on the writer (no delivery dependency)."""
    import eod_summary

    first = eod_summary.write_summary(
        done_today=["A"], still_open=["B"], tomorrow_top="C"
    )
    assert first["changed"] is True
    # Same inputs -> byte-identical file (no change, no second section).
    second = eod_summary.write_summary(
        done_today=["A"], still_open=["B"], tomorrow_top="C"
    )
    assert second["changed"] is False
    note = Path(first["path"])
    assert note.read_text().count("## EOD Summary") == 1

    # Changed inputs -> the section is REPLACED in place (still exactly one).
    third = eod_summary.write_summary(
        done_today=["A", "X"], still_open=[], tomorrow_top=None
    )
    assert third["changed"] is True
    text = note.read_text()
    assert text.count("## EOD Summary") == 1
    assert "- ✅ X" in text
    assert "_No #1 set_" in text  # empty tomorrow renders the explicit placeholder
    assert "_Board is clear_" in text  # empty still-open renders its placeholder


def test_summary_done_today_uses_daily_notes_completed_format(env):
    """Done items in ## EOD Summary are machine-readable completion evidence."""
    import eod_summary
    from daily_notes import _is_completed_action_line, extract_completed_tasks
    from cos_config import local_today

    rendered = eod_summary.render_summary(
        done_today=["Ship the parser fix"],
        still_open=[],
        tomorrow_top=None,
    )
    done_line = next(line for line in rendered.splitlines() if "Ship the parser fix" in line)
    assert done_line == "- ✅ Ship the parser fix"
    assert _is_completed_action_line(done_line)

    eod_summary.write_summary(
        done_today=["Ship the parser fix"],
        still_open=[],
        tomorrow_top=None,
    )
    tasks = extract_completed_tasks(
        notes_dir=env["daily"],
        start_date=local_today(),
        end_date=local_today(),
    )
    assert [task["title"] for task in tasks] == ["Ship the parser fix"]


def test_summary_upsert_preserves_surrounding_sections(env):
    """An existing daily note keeps its other sections; only ## EOD Summary is swapped."""
    import eod_summary
    from cos_config import local_today

    note = env["daily"] / f"{local_today().strftime('%Y-%m-%d')}.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("# Daily\n\n## Notes\n- keep me\n\n## EOD Summary\n\nold\n")

    eod_summary.write_summary(done_today=["new"], still_open=[], tomorrow_top=None)

    text = note.read_text()
    assert "## Notes" in text and "- keep me" in text  # neighbour section preserved
    assert text.count("## EOD Summary") == 1
    assert "- ✅ new" in text and "old" not in text  # the summary body was replaced


# --- U7 cron descriptor: CODE-ONLY shape (no live registration) ------------------


def test_eod_cron_descriptor_is_a_command_cron_to_the_done_thread():
    """The EOD cron descriptor is a DETERMINISTIC command cron (payload.kind == 'command')
    that runs telegram-commands.sh eod and announces to the Productivity DONE thread --
    asserted on the descriptor JSON (this is CODE-ONLY; no live openclaw cron add)."""
    desc = eod_ritual.eod_cron_descriptor()

    # Deterministic command cron -- NOT an LLM agentTurn.
    assert desc["payload"]["kind"] == "command"
    argv = desc["payload"]["argv"]
    assert argv[0] == "sh" and argv[1] == "-lc"
    assert "telegram-commands.sh eod" in argv[2]
    # Announce delivery to the Productivity DONE thread (env-var NAMES, no real ids).
    assert desc["delivery"]["mode"] == "announce"
    assert desc["delivery"]["chat_id_env"] == "TELEGRAM_CHAT_ID_PRODUCTIVITY"
    assert desc["delivery"]["topic_env"] == "OPENCLAW_TOPIC_PRODUCTIVITY_DONE"
    # No real chat id is baked into the descriptor -- only env-var names.
    assert "-100" not in json.dumps(desc)


def test_eod_cron_descriptor_carries_no_committed_chat_id():
    """The descriptor must embed env-var NAMES, never a literal -100xxxxxxxx chat id
    (public-repo hygiene): a serialised descriptor carries no production id."""
    serialised = json.dumps(eod_ritual.eod_cron_descriptor())
    assert "TELEGRAM_CHAT_ID_PRODUCTIVITY" in serialised
    assert PRODUCTIVITY not in serialised  # not even the fake test id is hardcoded
