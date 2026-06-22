"""H8 weekly brag digest + /win capture -- behavioral invariant tests.

Pins the four DONE invariants of H8, asserting the behavior (not the code path):

1. AUTO-HARVEST IS WEEKLY + SILENT WHEN EMPTY. The proactive (cron) push fires only
   on Friday AND only when the digest has content; an empty digest sends NOTHING (no
   blank message). On-demand /ledger works any day.
2. /win FRICTIONLESS CAPTURE. A win is appended with no cap/validation gate, persists
   durably (survives a crash -- a fresh read sees it), and surfaces in a later digest.
3. FOUR-BUCKET DIGEST. The digest renders shipped / advanced / decisions / maintenance,
   classifying harvested evidence and routing manual /win items into the right bucket.
4. R1 HEALTH WIRING. A cron (--auto) harvest records ledger_harvest health (success on
   a clean run, failure on ok:false), the manifest shows it as recently-succeeded (not
   MISSING); a reactive /ledger records NO health (no cron-vs-reactive conflation).

Public-repo hygiene: the only chat ids here are the FAKEs -4242424242 / -5252525252,
which do NOT match the -100[0-9]{8,} pattern the CI hygiene grep flags.
"""

import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import cos_config  # noqa: E402
import cos_health  # noqa: E402
import cos_manifest  # noqa: E402
import harvest_ledger  # noqa: E402
import harvest_state  # noqa: E402
import utils  # noqa: E402
import win_store  # noqa: E402

# Fake ids: valid chat-id shape but not -100xxxxxxxx, so the hygiene grep is clean.
PRODUCTIVITY = "-4242424242"
WORK_GROUP = "-5252525252"
DONE_TOPIC = "5"

# A known Friday and a known Monday in the user's local zone, for the digest-day gate.
FRIDAY = datetime(2026, 6, 19, 9, 0, tzinfo=cos_config.local_tz())
MONDAY = datetime(2026, 6, 15, 9, 0, tzinfo=cos_config.local_tz())

WORK_BOARD = """# Work

## 🔴 Q1
- [ ] **Add social updates to World Cup skill** task_id::tsk_abc123 area:: Delivery
"""

PR_PAYLOAD = [{
    "title": "Add social updates to World Cup skill",
    "number": 7,
    "repository": {"nameWithOwner": "kesslerio/world-cup-soccer-openclaw-skill"},
    "url": "https://example.test/pr/7",
}]


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolate every state file + the board + the ledger + the wins log under tmp_path."""
    state_dir = tmp_path / "state"
    work = tmp_path / "Work Tasks.md"
    work.write_text(WORK_BOARD)
    ledger = tmp_path / "events.jsonl"

    monkeypatch.setenv("TASK_MGMT_STATE_DIR", str(state_dir))
    monkeypatch.setenv("TASK_TRACKER_WORK_FILE", str(work))
    monkeypatch.setenv("TASK_TRACKER_LEDGER_FILE", str(ledger))
    monkeypatch.setenv("TASK_TRACKER_ERROR_LOG", str(state_dir / "errors.jsonl"))
    monkeypatch.setattr(utils, "OBSIDIAN_WORK", work)
    return {"work": work, "ledger": ledger, "state_dir": state_dir}


def _set_productivity_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID_PRODUCTIVITY", PRODUCTIVITY)
    monkeypatch.setenv("TELEGRAM_CHAT_ID_WORK", WORK_GROUP)
    monkeypatch.setenv("OPENCLAW_TOPIC_PRODUCTIVITY_DONE", DONE_TOPIC)


class _FakeCompleted:
    def __init__(self, returncode, stdout):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


def _stub_sources(monkeypatch, *, gh_payload=None, gog_payload=None):
    """Stub the harvest subprocesses (gh/gog) so a test never spawns a real one."""

    def fake_run(cmd, **kwargs):
        if cmd[0] == "gh":
            return _FakeCompleted(0, json.dumps(gh_payload if gh_payload is not None else []))
        if cmd[0] == "gog":
            return _FakeCompleted(0, json.dumps(gog_payload if gog_payload is not None else {"threads": []}))
        raise AssertionError(f"unexpected command {cmd!r}")

    monkeypatch.setattr(harvest_ledger.subprocess, "run", fake_run)


def _run(monkeypatch, *, auto, now, since="2026-01-01", dry_run=False):
    return harvest_ledger.run_harvest(
        "week", since_override=since, dry_run=dry_run,
        trigger="cron:ledger_harvest" if auto else "user_command:/ledger",
        auto=auto, now=now,
    )


def _ledger_event_types(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [json.loads(line)["event_type"] for line in path.read_text().splitlines() if line.strip()]


# === DONE 1: weekly + silent-when-empty auto gate ============================


def test_non_friday_auto_run_sends_nothing(env, monkeypatch):
    """(a) A non-Friday AUTO run pushes NOTHING even with content, and is silent."""
    _set_productivity_env(monkeypatch)
    _stub_sources(monkeypatch, gh_payload=PR_PAYLOAD)
    result = _run(monkeypatch, auto=True, now=MONDAY)
    assert result["draft_pushed"] is False
    assert result["reason"] == "not_digest_day"
    assert result["message"] is None  # no blank "nothing happened" message leaks
    assert "ledger_draft_pushed" not in _ledger_event_types(env["ledger"])


def test_friday_auto_run_with_no_content_sends_nothing(env, monkeypatch):
    """(b) A Friday AUTO run with an EMPTY digest pushes NOTHING and is silent."""
    _set_productivity_env(monkeypatch)
    _stub_sources(monkeypatch, gh_payload=[], gog_payload={"threads": []})
    result = _run(monkeypatch, auto=True, now=FRIDAY, since="2030-01-01")
    assert result["draft_pushed"] is False
    assert result["reason"] == "no_new_evidence"
    assert result["message"] is None
    assert "ledger_draft_pushed" not in _ledger_event_types(env["ledger"])


def test_friday_auto_run_with_content_sends_digest(env, monkeypatch):
    """(c) A Friday AUTO run WITH content proves the target and pushes the digest."""
    _set_productivity_env(monkeypatch)
    _stub_sources(monkeypatch, gh_payload=PR_PAYLOAD)
    result = _run(monkeypatch, auto=True, now=FRIDAY)
    assert result["draft_pushed"] is True
    assert result["delivery_target"]["chat_id"] == PRODUCTIVITY
    assert result["delivery_target"]["topic_id"] == DONE_TOPIC
    assert "ledger_draft_pushed" in _ledger_event_types(env["ledger"])


def test_on_demand_ledger_works_any_day(env, monkeypatch):
    """(d) An on-demand /ledger (auto=False) pushes a content digest on a MONDAY."""
    _set_productivity_env(monkeypatch)
    _stub_sources(monkeypatch, gh_payload=PR_PAYLOAD)
    result = _run(monkeypatch, auto=False, now=MONDAY)
    assert result["draft_pushed"] is True
    assert result["delivery_target"] is not None


def test_suppressed_auto_run_consumes_no_evidence(env, monkeypatch):
    """A suppressed (non-Friday) auto run must NOT mark evidence seen -- the same PR
    is delivered when Friday's fire is allowed (accomplishments never silently lost)."""
    _set_productivity_env(monkeypatch)
    _stub_sources(monkeypatch, gh_payload=PR_PAYLOAD)
    blocked = _run(monkeypatch, auto=True, now=MONDAY)
    assert blocked["draft_pushed"] is False
    assert harvest_state.load_state() is None  # nothing persisted

    _stub_sources(monkeypatch, gh_payload=PR_PAYLOAD)
    delivered = _run(monkeypatch, auto=True, now=FRIDAY)
    assert delivered["draft_pushed"] is True
    assert delivered["evidence_count"] == 1


# === DONE 2: /win frictionless capture + round-trip into the digest ==========


def test_win_capture_appends_and_surfaces_in_digest(env, monkeypatch):
    """/win shipped the pricing deck appends a win that a LATER digest includes."""
    _set_productivity_env(monkeypatch)
    result = harvest_ledger.capture_win("shipped the pricing deck")
    assert result["ok"] is True
    # Durable: a fresh read (simulating a process restart) still sees it.
    wins = win_store.read_wins()
    assert [w["text"] for w in wins] == ["shipped the pricing deck"]

    _stub_sources(monkeypatch, gh_payload=[], gog_payload={"threads": []})
    digest = _run(monkeypatch, auto=True, now=FRIDAY)
    assert digest["draft_pushed"] is True
    assert "shipped the pricing deck" in digest["draft"]


def test_win_capture_is_frictionless_never_blocks(env, monkeypatch):
    """Capture has NO cap/validation gate -- many wins in a row all persist."""
    for i in range(25):
        assert harvest_ledger.capture_win(f"win number {i}")["ok"] is True
    assert len(win_store.read_wins()) == 25


def test_win_empty_text_is_handled_not_crash(env, monkeypatch):
    """An empty /win is the ONLY refusal (no win to record) -- a handled message,
    not a crash, and nothing is persisted."""
    result = harvest_ledger.capture_win("   ")
    assert result["ok"] is False
    assert result["reason"] == "empty"
    assert win_store.read_wins() == []


def test_win_persists_across_a_crash(env, monkeypatch):
    """The win log is append-only on disk: a record written before a simulated crash
    is fully present (one complete JSON line) for the next process to read."""
    harvest_ledger.capture_win("decided to pivot the roadmap")
    raw = win_store.wins_path().read_text().splitlines()
    assert len(raw) == 1
    record = json.loads(raw[0])  # a complete, parseable line (not torn)
    assert record["text"] == "decided to pivot the roadmap"


# === DONE 3: four-bucket digest =============================================


def test_digest_renders_four_buckets(env, monkeypatch):
    """A digest renders shipped / advanced / decisions / maintenance, with harvested
    evidence and manual wins routed into the right bucket."""
    _set_productivity_env(monkeypatch)
    # A PR that evidence-links a task -> shipped; an email -> maintenance.
    _stub_sources(
        monkeypatch,
        gh_payload=PR_PAYLOAD,
        gog_payload={"threads": [{"id": "t1", "subject": "Re: vendor invoice follow-up"}]},
    )
    harvest_ledger.capture_win("decided to hire a CFO")     # -> decisions
    harvest_ledger.capture_win("pushed the partnership forward")  # -> advanced (default)

    result = _run(monkeypatch, auto=True, now=FRIDAY)
    draft = result["draft"]
    assert "Shipped:" in draft
    assert "Advanced:" in draft
    assert "Decisions:" in draft
    assert "Maintenance:" in draft
    # Each item lands in its bucket.
    assert "Add social updates to World Cup skill" in draft  # PR -> shipped
    assert "decided to hire a CFO" in draft                  # win -> decisions
    assert "pushed the partnership forward" in draft         # win -> advanced
    assert "vendor invoice follow-up" in draft               # email -> maintenance


def test_bucketise_classifies_evidence_and_wins():
    """Unit: the classifier routes a PR evidence-link to shipped, an email to
    maintenance, a needs-review to advanced, and respects a win's bucket."""
    matches = [
        {"title": "shipped feature", "decision": "evidence-link", "source_type": "pr",
         "matched_task_id": "tsk_1", "score": 0.95},
        {"title": "Re: thread", "decision": "no-match", "source_type": "email",
         "matched_task_id": None, "score": 0.0},
        {"title": "fuzzy work", "decision": "needs-review", "source_type": "pr",
         "matched_task_id": "tsk_2", "score": 0.8},
    ]
    wins = [{"text": "made a call", "bucket": "decisions"}]
    buckets = harvest_ledger.bucketise(matches, wins)
    assert [i["line"] for i in buckets["shipped"]] == ["shipped feature"]
    assert [i["line"] for i in buckets["maintenance"]] == ["Re: thread"]
    assert [i["line"] for i in buckets["advanced"]] == ["fuzzy work"]
    assert [i["line"] for i in buckets["decisions"]] == ["made a call"]


# === DONE 4: ledger_harvest health wiring (cron path only) ==================


def test_cron_harvest_records_success_and_manifest_not_missing(env, monkeypatch):
    """After a CRON (--auto) harvest, ledger_harvest health shows recently-succeeded
    and the manifest no longer flags it MISSING."""
    _set_productivity_env(monkeypatch)
    _stub_sources(monkeypatch, gh_payload=PR_PAYLOAD)
    harvest_ledger._record_ledger_health(_run(monkeypatch, auto=True, now=FRIDAY))

    entry = cos_health.read_health().get("ledger_harvest")
    assert entry is not None and entry.get("last_success_ts")
    # The manifest health view classifies it OK (fresh success), not MISSING.
    lines = cos_manifest.health_lines()
    assert any(line.startswith("OK ledger_harvest") for line in lines)
    assert not any("MISSING ledger_harvest" in line for line in lines)


def test_cron_harvest_records_failure_on_ok_false(env, monkeypatch):
    """An ok:false harvest records a ledger_harvest FAILURE with the right class +
    cron trigger, and the manifest surfaces that last bad run."""
    harvest_ledger._record_ledger_health({"ok": False, "reason": "harvest_failed"})
    entry = cos_health.read_health()["ledger_harvest"]
    assert entry["last_failure"]["error_class"] == "harvest_failed"
    assert entry["last_failure"]["trigger"] == "cron:ledger_harvest"
    # The manifest shows the failure (a failure with no recorded success is STALE-by-
    # absent-success per the documented precedence, never silently OK/green).
    line = next(l for l in cos_manifest.health_lines() if "ledger_harvest" in l)
    assert line.startswith("STALE ledger_harvest")
    assert "last_failure: harvest_failed" in line


def test_cron_harvest_failure_after_success_is_degraded(env, monkeypatch):
    """A FRESH failure after a recorded success reads DEGRADED -- the most-recent
    outcome is a failure, so it must not false-green just because a success exists."""
    harvest_ledger._record_ledger_health({"ok": True})
    harvest_ledger._record_ledger_health({"ok": False, "reason": "harvest_failed"})
    assert any("DEGRADED ledger_harvest" in line for line in cos_manifest.health_lines())


def test_reactive_ledger_records_no_health(env, monkeypatch):
    """A reactive /ledger (auto=False) records NO ledger_harvest health -- the cron and
    reactive paths are never conflated (only the scheduled fire owns the health signal)."""
    _set_productivity_env(monkeypatch)
    _stub_sources(monkeypatch, gh_payload=PR_PAYLOAD)
    # Drive the CLI path the reactive /ledger uses (no --auto) to prove it skips health.
    import argparse as _argparse
    args = _argparse.Namespace(window="week", since="2026-01-01", dry_run=False,
                               json=True, auto=False)
    harvest_ledger._run_harvest_cli(args)
    assert "ledger_harvest" not in cos_health.read_health()
