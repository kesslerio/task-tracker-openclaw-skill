"""U5 accomplishment ledger -- behavioral invariant tests.

These assert the invariants, not the implementation path:

* DELIVERY-TARGET-PROOF on the draft push (DENIED path: unset env / Work-group
  target => blocked, nothing is sent).
* REVERSIBILITY on the auto-mark (DENIED path: board write fails mid-approve =>
  approved_task_ids unchanged, pre_action_snapshot + pre_action_snapshot_cancelled
  both in the ledger).
* TOPIC GUARD, stale-approval, task-already-done guards.
* NO RAW ERROR LEAK from a failed harvest source.
* Weekly-reset surfaces expired (never silently dropped) + dedup.

Public-repo hygiene: every chat id here is a FAKE that does NOT match the
-100[0-9]{8,} pattern the CI hygiene grep flags. Real ids are env-sourced at
runtime, never committed.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import autonomy_gate
import harvest_ledger
import harvest_state
import task_transitions
import utils

# Fake ids: valid chat-id shape but not -100xxxxxxxx, so the hygiene grep is clean.
PRODUCTIVITY = "-4242424242"
WORK_GROUP = "-5252525252"
DONE_TOPIC = "5"

WORK_BOARD = """# Work

## 🔴 Q1
- [ ] **Add social updates to World Cup skill** task_id::tsk_abc123 area:: Delivery
- [ ] **Camp coordination for June** task_id::tsk_def456 area:: Ops
"""


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
    # utils freezes the board path into module-level constants at import time, so
    # an in-process test must repoint them (mirrors how the codebase resolves the
    # board; the subprocess tests instead pass env= and re-import).
    monkeypatch.setattr(utils, "OBSIDIAN_WORK", work)
    return {"work": work, "ledger": ledger, "state_dir": state_dir, "daily": daily}


def _set_productivity_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID_PRODUCTIVITY", PRODUCTIVITY)
    monkeypatch.setenv("TELEGRAM_CHAT_ID_WORK", WORK_GROUP)
    monkeypatch.setenv("OPENCLAW_TOPIC_PRODUCTIVITY_DONE", DONE_TOPIC)


def _clear_productivity_env(monkeypatch):
    for name in (
        "TELEGRAM_CHAT_ID_PRODUCTIVITY",
        "TELEGRAM_CHAT_ID_WORK",
        "OPENCLAW_TOPIC_PRODUCTIVITY_DONE",
    ):
        monkeypatch.delenv(name, raising=False)


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


# --- T1: happy path -- PR matches a task, dry-run surfaces the match ---------


def test_harvest_matches_pr_to_task(env, monkeypatch):
    _set_productivity_env(monkeypatch)
    _stub_sources(
        monkeypatch,
        gh_payload=[{
            "title": "Add social updates to World Cup skill",
            "number": 7,
            "repository": {"nameWithOwner": "kesslerio/world-cup-soccer-openclaw-skill"},
            "url": "https://github.com/kesslerio/world-cup-soccer-openclaw-skill/pull/7",
        }],
    )
    result = harvest_ledger.run_harvest("week", since_override=None, dry_run=True, trigger="t")
    links = [m for m in result["matches"] if m["decision"] == "evidence-link"]
    assert len(links) == 1
    assert links[0]["matched_task_id"] == "tsk_abc123"
    assert links[0]["score"] >= 0.90
    # Dry-run never writes state.
    assert not (env["state_dir"] / "harvest-state.json").exists()


# --- DELIVERY-TARGET-PROOF: denied path (mandated by the unit gate) ----------


def test_push_blocked_when_env_unset(env, monkeypatch):
    """Unset productivity env => the draft is NOT pushed and no target leaks."""
    _clear_productivity_env(monkeypatch)
    _stub_sources(
        monkeypatch,
        gh_payload=[{
            "title": "Add social updates to World Cup skill",
            "number": 7,
            "repository": {"nameWithOwner": "kesslerio/world-cup-soccer-openclaw-skill"},
            "url": "https://example.test/pr/7",
        }],
    )
    result = harvest_ledger.run_harvest("week", since_override=None, dry_run=False, trigger="t")
    assert result["draft_pushed"] is False
    assert result["delivery_target"] is None
    assert result["push_blocked_reason"] == "env_missing"
    # No ledger_draft_pushed event ever written.
    assert "ledger_draft_pushed" not in _ledger_event_types(env["ledger"])


def test_blocked_push_does_not_consume_evidence(env, monkeypatch):
    """A BLOCKED push must NOT mark evidence seen -- once the env is fixed the
    next fire re-attempts delivery (accomplishments never silently dropped)."""
    payload = [{
        "title": "Add social updates to World Cup skill",
        "number": 7,
        "repository": {"nameWithOwner": "kesslerio/world-cup-soccer-openclaw-skill"},
        "url": "https://example.test/pr/7",
    }]
    # First fire with the env UNSET => push blocked.
    _clear_productivity_env(monkeypatch)
    _stub_sources(monkeypatch, gh_payload=payload)
    blocked = harvest_ledger.run_harvest("week", since_override=None, dry_run=False, trigger="t")
    assert blocked["draft_pushed"] is False
    # The dedup set was NOT persisted with this evidence.
    state = harvest_state.load_state()
    assert state is None or not state.get("seen_hashes")

    # Fix the env and re-fire: the SAME PR is harvested again and now delivered.
    _set_productivity_env(monkeypatch)
    _stub_sources(monkeypatch, gh_payload=payload)
    delivered = harvest_ledger.run_harvest("week", since_override=None, dry_run=False, trigger="t")
    assert delivered["draft_pushed"] is True
    assert delivered["evidence_count"] == 1


def test_push_blocked_when_target_is_work_group(env, monkeypatch):
    """If the configured Done topic resolves to the Work group it is rejected."""
    _set_productivity_env(monkeypatch)
    # Point the productivity chat id AT the work group: prove + gate must reject it.
    monkeypatch.setenv("TELEGRAM_CHAT_ID_PRODUCTIVITY", WORK_GROUP)
    monkeypatch.setenv("TELEGRAM_CHAT_ID_WORK", WORK_GROUP)
    _stub_sources(
        monkeypatch,
        gh_payload=[{
            "title": "Add social updates to World Cup skill",
            "number": 7,
            "repository": {"nameWithOwner": "kesslerio/world-cup-soccer-openclaw-skill"},
            "url": "https://example.test/pr/7",
        }],
    )
    result = harvest_ledger.run_harvest("week", since_override=None, dry_run=False, trigger="t")
    assert result["draft_pushed"] is False
    assert result["delivery_target"] is None
    assert result["push_blocked_reason"] in ("work_group", "env_missing", "target_unknown", "unproven-target")
    assert "ledger_draft_pushed" not in _ledger_event_types(env["ledger"])


def test_push_proven_and_asserted_when_env_set(env, monkeypatch):
    """Allowed path: proven target is gated, send-asserted, and logged."""
    _set_productivity_env(monkeypatch)
    _stub_sources(
        monkeypatch,
        gh_payload=[{
            "title": "Add social updates to World Cup skill",
            "number": 7,
            "repository": {"nameWithOwner": "kesslerio/world-cup-soccer-openclaw-skill"},
            "url": "https://example.test/pr/7",
        }],
    )
    result = harvest_ledger.run_harvest("week", since_override=None, dry_run=False, trigger="t")
    assert result["draft_pushed"] is True
    assert result["delivery_target"]["chat_id"] == PRODUCTIVITY
    assert result["delivery_target"]["topic_id"] == DONE_TOPIC
    assert "ledger_draft_pushed" in _ledger_event_types(env["ledger"])
    # The gated act binds the SAME target the send used (the seam is closed).
    pushed = [
        json.loads(line)
        for line in env["ledger"].read_text().splitlines()
        if line.strip() and json.loads(line)["event_type"] == "ledger_draft_pushed"
    ][0]
    act_id = pushed["metadata"]["act_id"]
    assert autonomy_gate.assert_send_target(act_id, result["delivery_target"])["ok"] is True
    # A send to a DIFFERENT topic for the same act is blocked.
    bad = dict(result["delivery_target"], topic_id="6")
    assert autonomy_gate.assert_send_target(act_id, bad)["ok"] is False


# --- REVERSIBILITY: denied path (mandated by the unit gate) ------------------


def _push_and_get_state(env, monkeypatch):
    _set_productivity_env(monkeypatch)
    _stub_sources(
        monkeypatch,
        gh_payload=[{
            "title": "Add social updates to World Cup skill",
            "number": 7,
            "repository": {"nameWithOwner": "kesslerio/world-cup-soccer-openclaw-skill"},
            "url": "https://example.test/pr/7",
        }],
    )
    harvest_ledger.run_harvest("week", since_override=None, dry_run=False, trigger="t")
    return harvest_state.load_state()


def test_approve_board_write_failure_is_reversible(env, monkeypatch):
    """Board write fails mid-approve => approved_task_ids unchanged, snapshot +
    cancelled compensating event both in the ledger, no raw traceback."""
    state = _push_and_get_state(env, monkeypatch)
    assert "tsk_abc123" in state["pending_task_ids"]

    def boom(path_obj, content):
        raise OSError("simulated board write failure")

    monkeypatch.setattr(task_transitions, "_atomic_write", boom)

    result = harvest_ledger.approve("tsk_abc123", inbound_topic_id=DONE_TOPIC)
    assert result["ok"] is False
    assert result["reason"] == "complete-failed"
    # The friendly error carries a structured code, NOT a raw traceback.
    assert "Traceback" not in json.dumps(result)

    after = harvest_state.load_state()
    assert "tsk_abc123" not in (after.get("approved_task_ids") or [])
    assert "tsk_abc123" in after["pending_task_ids"]

    types = _ledger_event_types(env["ledger"])
    assert "pre_action_snapshot" in types
    assert "pre_action_snapshot_cancelled" in types
    assert "ledger_approved" not in types


def test_approve_happy_path_marks_done_and_reversible(env, monkeypatch):
    """Allowed path: approve marks the task done, records the evidence link and a
    pre_action_snapshot, and moves the id from pending to approved."""
    state = _push_and_get_state(env, monkeypatch)
    result = harvest_ledger.approve(
        "tsk_abc123",
        inbound_topic_id=DONE_TOPIC,
        match={"source_type": "pr", "url": "https://example.test/pr/7", "score": 1.0, "match_type": "fuzzy"},
    )
    assert result["ok"] is True
    after = harvest_state.load_state()
    assert "tsk_abc123" in after["approved_task_ids"]
    assert "tsk_abc123" not in after["pending_task_ids"]
    types = _ledger_event_types(env["ledger"])
    assert "pre_action_snapshot" in types
    assert "evidence_link" in types
    assert "ledger_approved" in types
    # The task is gone from the active board.
    assert "tsk_abc123" not in env["work"].read_text()


# --- TOPIC GUARD: /approve in the wrong topic is rejected --------------------


def test_approve_cli_exits_zero_on_guard_rejection(env, monkeypatch):
    """A topic-guard / stale-approval rejection is a HANDLED outcome: the CLI
    exits 0 and prints the structured message, so the U1 envelope relays it
    instead of masking it as a generic 'unavailable' tool failure."""
    import argparse as _argparse

    _set_productivity_env(monkeypatch)
    # No pending state => stale-approval rejection.
    args = _argparse.Namespace(task_id="tsk_abc123", topic_id=DONE_TOPIC)
    rc = harvest_ledger._run_approve_cli(args)
    assert rc == 0


def test_reactive_approve_records_evidence_provenance(env, monkeypatch):
    """A reactive /approve (no in-process match arg) still records the PR url +
    score on the evidence_link, rebuilt from the match stored at push time."""
    _push_and_get_state(env, monkeypatch)
    # Approve WITHOUT passing match= (mirrors the inbound Telegram CLI path).
    result = harvest_ledger.approve("tsk_abc123", inbound_topic_id=DONE_TOPIC)
    assert result["ok"] is True
    evidence_links = [
        json.loads(line)
        for line in env["ledger"].read_text().splitlines()
        if line.strip() and json.loads(line)["event_type"] == "evidence_link"
    ]
    assert len(evidence_links) == 1
    ev = evidence_links[0]["evidence"]
    assert ev["source_type"] == "pr"
    assert ev["source_url"] == "https://example.test/pr/7"
    assert ev["match_score"] is not None


def test_approve_wrong_topic_rejected(env, monkeypatch):
    _push_and_get_state(env, monkeypatch)
    result = harvest_ledger.approve("tsk_abc123", inbound_topic_id="6")  # Journal topic, not Done
    assert result["ok"] is False
    assert result["reason"] == "wrong-topic"
    # No completion happened.
    assert "tsk_abc123" in env["work"].read_text()
    assert "ledger_approved" not in _ledger_event_types(env["ledger"])


# --- stale approval + already-done guards -----------------------------------


def test_approve_stale_id_rejected(env, monkeypatch):
    _set_productivity_env(monkeypatch)
    # No harvest run => no pending state => any approve is stale.
    result = harvest_ledger.approve("tsk_abc123", inbound_topic_id=DONE_TOPIC)
    assert result["ok"] is False
    assert result["reason"] == "stale-approval"


def test_approve_task_already_done_surfaces_structured_error(env, monkeypatch):
    state = _push_and_get_state(env, monkeypatch)
    # Remove the task from the board so complete_by_id cannot resolve it.
    env["work"].write_text("# Work\n\n## 🔴 Q1\n- [ ] **Other** task_id::tsk_other area:: Ops\n")
    result = harvest_ledger.approve("tsk_abc123", inbound_topic_id=DONE_TOPIC)
    assert result["ok"] is False
    assert result["reason"] == "complete-failed"
    assert result["error"]["code"] == "canonical-id-resolution-failed"
    assert "tsk_abc123" not in (harvest_state.load_state().get("approved_task_ids") or [])


# --- NO RAW ERROR LEAK: a failed source returns [] and logs, never raises -----


def test_failed_github_source_logs_and_returns_empty(env, monkeypatch):
    _set_productivity_env(monkeypatch)
    _stub_sources(monkeypatch, gh_rc=1, gog_payload={"threads": []})
    result = harvest_ledger.run_harvest("week", since_override=None, dry_run=True, trigger="t")
    # No new evidence => no draft, no raw error text anywhere in the output.
    assert result["draft_pushed"] is False
    assert "Traceback" not in json.dumps(result, default=str)
    # The failure was logged to the structured error log under the github component.
    log = env["state_dir"] / "errors.jsonl"
    entries = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    assert any(e["component"] == "ledger_harvest:github" for e in entries)


def test_all_sources_empty_no_push(env, monkeypatch):
    _set_productivity_env(monkeypatch)
    _stub_sources(monkeypatch, gh_payload=[], gog_payload={"threads": []})
    result = harvest_ledger.run_harvest("week", since_override=None, dry_run=False, trigger="t")
    assert result["draft_pushed"] is False
    assert result["reason"] == "no_new_evidence"
    assert "ledger_draft_pushed" not in _ledger_event_types(env["ledger"])


def test_weekly_harvest_does_not_ingest_commits_alongside_prs(env, monkeypatch):
    class Completed:
        returncode = 0
        stderr = ""

        def __init__(self, stdout):
            self.stdout = stdout

    calls = {"commits": 0}

    def fake_run(cmd, **_kwargs):
        joined = " ".join(cmd)
        if "search prs" in joined:
            return Completed(json.dumps([{
                "title": "Add social updates to World Cup skill",
                "number": 7,
                "repository": {"nameWithOwner": "kesslerio/world-cup-soccer-openclaw-skill"},
                "url": "https://example.test/pr/7",
            }]))
        if "search commits" in joined:
            calls["commits"] += 1
            return Completed(json.dumps([{
                "sha": "abc123def456",
                "repository": {"nameWithOwner": "kesslerio/world-cup-soccer-openclaw-skill"},
                "url": "https://example.test/commit/abc123def456",
                "commit": {
                    "message": "Add social updates to World Cup skill\n\nPR commit",
                    "committer": {"date": "2026-06-23T17:00:00Z"},
                },
            }]))
        if cmd[0] == "gog":
            return Completed(json.dumps({"threads": []}))
        raise AssertionError(cmd)

    monkeypatch.setattr(harvest_ledger.subprocess, "run", fake_run)

    result = harvest_ledger.run_harvest("week", since_override=None, dry_run=True, trigger="t")

    assert calls["commits"] == 0
    assert result["evidence_count"] == 1
    assert [match["source_type"] for match in result["matches"]] == ["pr"]


def test_harvest_parsers_tolerate_bare_string_payload(env, monkeypatch):
    class Completed:
        returncode = 0
        stderr = ""
        stdout = json.dumps("not-a-container")

    monkeypatch.setattr(harvest_ledger.subprocess, "run", lambda *_args, **_kwargs: Completed())

    github, github_failed = harvest_ledger.harvest_github(
        "2026-06-23",
        trigger="t",
        harvest_commits=True,
    )
    gmail, gmail_failed = harvest_ledger.harvest_gmail("2026-06-23", trigger="t")

    assert github == []
    assert gmail == []
    assert github_failed is False
    assert gmail_failed is False


def test_gmail_messages_without_real_ids_are_skipped(env, monkeypatch):
    class Completed:
        returncode = 0
        stderr = ""
        stdout = json.dumps({
            "threads": [{
                "id": "thread-1",
                "subject": "Thread subject",
                "messages": [
                    {"subject": "First", "internalDate": "1782200000000"},
                    {"subject": "Second", "internalDate": "1782200000001"},
                ],
            }]
        })

    monkeypatch.setattr(harvest_ledger.subprocess, "run", lambda *_args, **_kwargs: Completed())

    evidence, failed = harvest_ledger.harvest_gmail("2026-06-23", trigger="t")

    assert evidence == []
    assert failed is False


# --- dedup + weekly reset ----------------------------------------------------


def test_seen_hash_dedup_prevents_reingest(env, monkeypatch):
    _set_productivity_env(monkeypatch)
    payload = [{
        "title": "Add social updates to World Cup skill",
        "number": 7,
        "repository": {"nameWithOwner": "kesslerio/world-cup-soccer-openclaw-skill"},
        "url": "https://example.test/pr/7",
    }]
    _stub_sources(monkeypatch, gh_payload=payload)
    first = harvest_ledger.run_harvest("week", since_override=None, dry_run=False, trigger="t")
    assert first["draft_pushed"] is True
    # A second fire in the same window: the same PR is already seen, so there is
    # no new evidence and no second push.
    second = harvest_ledger.run_harvest("week", since_override=None, dry_run=False, trigger="t")
    assert second["draft_pushed"] is False
    assert second["reason"] in ("no_new_evidence", "already_pushed")


def test_weekly_reset_surfaces_expired(env, monkeypatch):
    """A new ISO week resets the window; unapproved pending ids surface as expired."""
    _set_productivity_env(monkeypatch)
    # Seed last week's state with an unapproved pending id.
    state_dir = env["state_dir"]
    state_dir.mkdir(parents=True, exist_ok=True)
    prior = harvest_state.new_window_state("2026-W24")
    prior["pending_task_ids"] = ["tsk_stale"]
    prior["draft_pushed"] = True
    (state_dir / "harvest-state.json").write_text(json.dumps(prior, indent=2, sort_keys=True) + "\n")

    _stub_sources(monkeypatch, gh_payload=[], gog_payload={"threads": []})
    result = harvest_ledger.run_harvest("week", since_override=None, dry_run=False, trigger="t")
    assert "tsk_stale" in result["expired"]


@pytest.mark.parametrize(
    "degenerate_title",
    ["   ", "✅", "✅ 2026-06-21", "line one\nline two", "12:30 ✅", "09:15 ✅ 2026-06-21"],
)
def test_degenerate_title_evidence_keeps_alignment(env, monkeypatch, degenerate_title):
    """An evidence item whose title cleans to empty OR contains a newline must not
    shift the matcher's per-line alignment (else a later PR is attributed to the
    wrong task, or the whole harvest raises an AssertionError)."""
    _set_productivity_env(monkeypatch)
    _stub_sources(
        monkeypatch,
        gh_payload=[
            {"title": degenerate_title, "number": 1,
             "repository": {"nameWithOwner": "kesslerio/x"}, "url": "https://example.test/1"},
            {"title": "Add social updates to World Cup skill", "number": 7,
             "repository": {"nameWithOwner": "kesslerio/world-cup-soccer-openclaw-skill"},
             "url": "https://example.test/7"},
        ],
    )
    result = harvest_ledger.run_harvest("week", since_override=None, dry_run=True, trigger="t")
    assert len(result["matches"]) == 2
    # The real PR (second item) is correctly attributed -- NOT shifted onto the
    # degenerate first item, and the harvest did not crash.
    by_id = {m["matched_task_id"]: m for m in result["matches"]}
    assert by_id.get("tsk_abc123") is not None
    assert by_id["tsk_abc123"]["decision"] == "evidence-link"


def test_24h_done_does_not_clobber_weekly_approval_loop(env, monkeypatch):
    """A 24h /done run must NOT overwrite the weekly window's pending/seen state;
    /approve of a weekly item must still succeed after a /done fires."""
    _set_productivity_env(monkeypatch)
    payload = [{
        "title": "Add social updates to World Cup skill",
        "number": 7,
        "repository": {"nameWithOwner": "kesslerio/world-cup-soccer-openclaw-skill"},
        "url": "https://example.test/pr/7",
    }]
    # Weekly harvest + push: tsk_abc123 becomes pending in the WEEKLY state.
    _stub_sources(monkeypatch, gh_payload=payload)
    weekly = harvest_ledger.run_harvest("week", since_override=None, dry_run=False, trigger="t")
    assert weekly["draft_pushed"] is True
    assert "tsk_abc123" in harvest_state.load_state("week")["pending_task_ids"]

    # A 24h /done fires with DIFFERENT evidence -- it must write only the 24h file.
    _stub_sources(monkeypatch, gh_payload=[{
        "title": "Camp coordination for June", "number": 9,
        "repository": {"nameWithOwner": "kesslerio/x"}, "url": "https://example.test/9",
    }])
    harvest_ledger.run_harvest("24h", since_override=None, dry_run=False, trigger="t")
    # The weekly state is untouched: tsk_abc123 still pending.
    assert "tsk_abc123" in harvest_state.load_state("week")["pending_task_ids"]

    # And /approve of the weekly item still works.
    result = harvest_ledger.approve("tsk_abc123", inbound_topic_id=DONE_TOPIC)
    assert result["ok"] is True
    assert "tsk_abc123" in harvest_state.load_state("week")["approved_task_ids"]


def test_load_or_reset_keeps_same_window(env, monkeypatch):
    state_dir = env["state_dir"]
    state_dir.mkdir(parents=True, exist_ok=True)
    wid = harvest_state.iso_week_id()
    seeded = harvest_state.new_window_state(wid)
    seeded["pending_task_ids"] = ["tsk_keep"]
    (state_dir / "harvest-state.json").write_text(json.dumps(seeded, indent=2, sort_keys=True) + "\n")
    state, expired = harvest_state.load_or_reset(wid)
    assert state["pending_task_ids"] == ["tsk_keep"]
    assert expired == []


def test_explicit_standup_window_since_is_stable_when_24h_rolling_cutoff_moves():
    from datetime import date
    import harvest_window

    resolved = harvest_window.resolve_standup_window(
        target_date=date(2026, 6, 23),
        evidence_date=date(2026, 6, 22),
    )

    assert harvest_ledger._since_date("24h", None, reference=date(2026, 6, 23)) == "2026-06-22"
    assert harvest_ledger._since_date("24h", None, reference=date(2026, 6, 24)) == "2026-06-23"
    assert harvest_ledger._since_date_for_window(resolved) == "2026-06-22"


def test_late_rerun_for_same_explicit_window_counts_evidence_once(env, monkeypatch):
    from datetime import date, datetime
    import harvest_window

    _set_productivity_env(monkeypatch)
    resolved = harvest_window.resolve_standup_window(
        target_date=date(2026, 6, 23),
        evidence_date=date(2026, 6, 22),
    )
    evidence = [{
        "source_type": "github",
        "match_title": "Add social updates to World Cup skill",
        "title": "Add social updates to World Cup skill [repo#31]",
        "url": "https://example.test/pr/31",
        "evidence_hash": harvest_ledger._evidence_hash("github", "repo#31"),
        "provider_state": "merged:2026-06-22T10:00:00-07:00",
        "occurred_at": "2026-06-22T10:00:00-07:00",
    }]

    def fake_harvest_all_for_window(window, *, trigger):
        assert window == resolved
        return [dict(item) for item in evidence], 1, False

    monkeypatch.setattr(harvest_ledger, "harvest_all_for_window", fake_harvest_all_for_window)

    def sender(_target, _message):
        return {"ok": True, "message_id": "m-1", "idem_key": "idem-1"}

    first = harvest_ledger.run_harvest(
        "week",
        since_override=None,
        dry_run=False,
        trigger="t",
        auto=True,
        now=datetime(2026, 6, 26, 9, 0),
        evidence_window=resolved,
        sender=sender,
    )
    second = harvest_ledger.run_harvest(
        "week",
        since_override=None,
        dry_run=False,
        trigger="t",
        auto=True,
        now=datetime(2026, 6, 26, 9, 0),
        evidence_window=resolved,
        sender=sender,
    )

    assert first["evidence_count"] == 1
    assert first["draft_pushed"] is True
    assert second["draft_pushed"] is False
    assert second["reason"] == "no_new_evidence"


def test_explicit_standup_window_writes_only_standup_state_file(env, monkeypatch):
    from datetime import date, datetime
    import harvest_window

    _set_productivity_env(monkeypatch)
    resolved = harvest_window.resolve_standup_window(
        target_date=date(2026, 6, 23),
        evidence_date=date(2026, 6, 22),
    )
    evidence = [{
        "source_type": "github",
        "match_title": "Add social updates to World Cup skill",
        "title": "Add social updates to World Cup skill [repo#31]",
        "url": "https://example.test/pr/31",
        "evidence_hash": harvest_ledger._evidence_hash("github", "repo#31"),
        "provider_state": "merged:2026-06-22T10:00:00-07:00",
        "occurred_at": "2026-06-22T10:00:00-07:00",
    }]

    def fake_harvest_all_for_window(window, *, trigger):
        assert window == resolved
        return [dict(item) for item in evidence], 1, False

    monkeypatch.setattr(harvest_ledger, "harvest_all_for_window", fake_harvest_all_for_window)

    def sender(_target, _message):
        return {"ok": True, "message_id": "m-standup", "idem_key": "idem-standup"}

    result = harvest_ledger.run_harvest(
        "week",
        since_override=None,
        dry_run=False,
        trigger="t",
        auto=True,
        now=datetime(2026, 6, 26, 9, 0),
        evidence_window=resolved,
        sender=sender,
    )

    assert result["draft_pushed"] is True
    assert (env["state_dir"] / "harvest-state-standup.json").exists()
    assert not (env["state_dir"] / "harvest-state.json").exists()
    assert not (env["state_dir"] / "harvest-state-24h.json").exists()

    standup_state = harvest_state.load_state(harvest_state.WINDOW_STANDUP)
    assert standup_state["harvest_window_id"] == resolved.window_id
    assert standup_state["seen_hashes"] == [harvest_ledger._evidence_hash("github", "repo#31")]
