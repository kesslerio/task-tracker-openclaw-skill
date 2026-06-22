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


def _clear_productivity_env(monkeypatch):
    """Unset the push-target env so a push is BLOCKED (unprovable target)."""
    for name in ("TELEGRAM_CHAT_ID_PRODUCTIVITY", "TELEGRAM_CHAT_ID_WORK",
                 "OPENCLAW_TOPIC_PRODUCTIVITY_DONE"):
        monkeypatch.delenv(name, raising=False)


class _FakeCompleted:
    def __init__(self, returncode, stdout):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


def _stub_sources(monkeypatch, *, gh_payload=None, gog_payload=None, gh_rc=0, gog_rc=0):
    """Stub the harvest subprocesses (gh/gog) so a test never spawns a real one.

    ``gh_rc`` / ``gog_rc`` set a nonzero exit to simulate a SOURCE ERROR (the cron
    path then records a health FAILURE, distinct from a clean-empty week).
    """

    def fake_run(cmd, **kwargs):
        if cmd[0] == "gh":
            return _FakeCompleted(gh_rc, json.dumps(gh_payload if gh_payload is not None else []))
        if cmd[0] == "gog":
            return _FakeCompleted(gog_rc, json.dumps(gog_payload if gog_payload is not None else {"threads": []}))
        raise AssertionError(f"unexpected command {cmd!r}")

    monkeypatch.setattr(harvest_ledger.subprocess, "run", fake_run)


def fake_sender(record=None, *, message_id="msg-1"):
    """A deliver_once-shaped fake sender: records (target, draft) and returns a canned
    ``{"message_id": ...}`` receipt. NEVER calls real openclaw. The auto digest now
    OWNS its send through the receipt-backed outbox, so a receipt-returning sender is
    required for an auto run to consume state (mirrors test_nag_check.fake_sender)."""
    calls = record if record is not None else []

    def _send(target, draft):
        calls.append((target, draft))
        return {"message_id": message_id}

    return _send


def _run(monkeypatch, *, auto, now, since="2026-01-01", dry_run=False, sender=None):
    # On the AUTO path the digest delivers itself (receipt-backed outbox), so inject a
    # fake receipt-returning sender by default; tests that exercise a transport FAILURE
    # pass their own raising sender. The reactive path ignores ``sender`` (the relay
    # delivers there).
    if auto and sender is None and not dry_run:
        sender = fake_sender()
    return harvest_ledger.run_harvest(
        "week", since_override=since, dry_run=dry_run,
        trigger="cron:ledger_harvest" if auto else "user_command:/ledger",
        auto=auto, now=now, sender=sender,
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


def test_reactive_ledger_does_not_preempt_friday_auto_digest(env, monkeypatch):
    """P3: a mid-week reactive /ledger pushes, but the headline Friday AUTO digest
    must STILL fire for the SAME ISO week (kind-aware dedup) -- a reactive pull never
    silently cancels the weekly Friday brag digest. Each kind dedups independently."""
    _set_productivity_env(monkeypatch)
    # Mid-week: a reactive /ledger pushes the week's evidence so far.
    _stub_sources(monkeypatch, gh_payload=PR_PAYLOAD)
    reactive = _run(monkeypatch, auto=False, now=MONDAY)
    assert reactive["draft_pushed"] is True

    # Friday (same ISO week): NEW evidence has landed; the AUTO digest must fire,
    # NOT be blocked by the same-window reactive push.
    new_pr = [{"title": "Close the Acme migration", "number": 9,
               "repository": {"nameWithOwner": "kesslerio/acme"},
               "url": "https://example.test/pr/9"}]
    _stub_sources(monkeypatch, gh_payload=new_pr)
    friday = _run(monkeypatch, auto=True, now=FRIDAY)
    assert friday["draft_pushed"] is True
    assert "Close the Acme migration" in friday["draft"]

    # A SECOND reactive run with fresh content is still deduped (reactive already
    # pushed this window) -- kind-aware dedup only frees the OTHER kind.
    _stub_sources(monkeypatch, gh_payload=[{"title": "Another", "number": 11,
        "repository": {"nameWithOwner": "kesslerio/x"}, "url": "https://example.test/pr/11"}])
    second_reactive = _run(monkeypatch, auto=False, now=MONDAY)
    assert second_reactive["draft_pushed"] is False
    assert second_reactive["reason"] == "already_pushed"

    # And a SECOND Friday auto run is itself deduped (auto already pushed this window).
    _stub_sources(monkeypatch, gh_payload=[{"title": "Yet another", "number": 13,
        "repository": {"nameWithOwner": "kesslerio/y"}, "url": "https://example.test/pr/13"}])
    second_auto = _run(monkeypatch, auto=True, now=FRIDAY)
    assert second_auto["draft_pushed"] is False
    assert second_auto["reason"] == "already_pushed"


def test_legacy_draft_pushed_flag_blocks_a_same_window_reactive(env, monkeypatch):
    """Back-compat: a pre-kind state file with the old single ``draft_pushed`` flag
    is honored as a REACTIVE push for its window (so a reactive re-run is deduped),
    while the Friday auto digest is still free to fire."""
    _set_productivity_env(monkeypatch)
    window_id = harvest_state.window_id("week")
    legacy = harvest_state.new_window_state(window_id)
    legacy["draft_pushed"] = True  # the retired single-flag shape
    legacy.pop("reactive_pushed_window", None)
    harvest_state.save_state(legacy, "week")

    _stub_sources(monkeypatch, gh_payload=PR_PAYLOAD)
    reactive = _run(monkeypatch, auto=False, now=MONDAY)
    assert reactive["draft_pushed"] is False
    assert reactive["reason"] == "already_pushed"

    # The Friday auto digest is NOT blocked by the legacy reactive flag.
    _stub_sources(monkeypatch, gh_payload=PR_PAYLOAD)
    friday = _run(monkeypatch, auto=True, now=FRIDAY)
    assert friday["draft_pushed"] is True


def test_dual_push_preserves_reactive_approvable_task(env, monkeypatch):
    """Kind-aware dual push must MERGE pending-approval state, not overwrite it: a
    task the reactive digest advertised as approvable ('/approve tsk_abc123') stays
    approvable after the Friday auto digest fires for the same window with different
    evidence. (Round-3 regression guard: the Friday push used to overwrite pending.)"""
    _set_productivity_env(monkeypatch)
    # Reactive push: PR #7 matches board task tsk_abc123, so it is approvable.
    _stub_sources(monkeypatch, gh_payload=PR_PAYLOAD)
    reactive = _run(monkeypatch, auto=False, now=MONDAY)
    assert reactive["draft_pushed"] is True
    assert "tsk_abc123" in harvest_state.load_state("week")["pending_task_ids"]

    # Friday auto push, SAME window, DIFFERENT (unmatched) evidence -> this run's
    # pending is empty; pre-fix that overwrote and dropped tsk_abc123.
    _stub_sources(monkeypatch, gh_payload=[{"title": "Close the Acme migration", "number": 9,
        "repository": {"nameWithOwner": "kesslerio/acme"}, "url": "https://example.test/pr/9"}])
    friday = _run(monkeypatch, auto=True, now=FRIDAY)
    assert friday["draft_pushed"] is True
    # tsk_abc123 SURVIVES the Friday push and is still one-tap approvable.
    assert "tsk_abc123" in harvest_state.load_state("week")["pending_task_ids"]
    approved = harvest_ledger.approve("tsk_abc123", inbound_topic_id=DONE_TOPIC)
    assert approved["ok"] is True
    assert approved.get("reason") != "stale-approval"


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


# === V2 (O3 HIGH 1): the AUTO digest is RECEIPT-BACKED, not consume-on-proof ==
# The scheduled Friday digest now OWNS its delivery through the receipt-backed outbox
# and consumes NOTHING until the transport returns a message-id receipt. A relay/
# transport failure can no longer lose the digest AND false-green the ritual.


def _raising_sender(exc=None):
    """A sender that simulates a transport FAILURE: it RAISES (the openclaw_sender
    contract on a non-zero exit / unparseable output), so deliver_once records no
    receipt and the auto path consumes nothing."""

    def _send(target, draft):
        raise (exc or RuntimeError("simulated transport failure"))

    return _send


def test_auto_digest_consumes_on_receipt(env, monkeypatch):
    """A receipt-returning sender => the digest is DELIVERED: evidence + wins marked
    seen, ledger_draft_pushed logged (carrying the message-id receipt), auto_pushed_
    window set, and the receipt recorded under the idem-key."""
    _set_productivity_env(monkeypatch)
    _stub_sources(monkeypatch, gh_payload=PR_PAYLOAD)
    harvest_ledger.capture_win("shipped the pricing deck")

    sent: list = []
    result = _run(monkeypatch, auto=True, now=FRIDAY, sender=fake_sender(sent, message_id="m-77"))
    assert result["draft_pushed"] is True
    assert len(sent) == 1  # the script OWNED the send (one outbox delivery)
    # Evidence consumed.
    state = harvest_state.load_state()
    assert state["seen_hashes"]
    assert state["auto_pushed_window"] == harvest_state.window_id("week")
    # Wins consumed.
    assert win_store.read_unseen_wins() == []
    # ledger_draft_pushed logged WITH the receipt message-id + idem-key.
    pushed = [json.loads(l) for l in env["ledger"].read_text().splitlines()
              if l.strip() and json.loads(l)["event_type"] == "ledger_draft_pushed"]
    assert len(pushed) == 1
    assert pushed[0]["metadata"]["message_id"] == "m-77"
    # The receipt is recorded under the ledger idem-key.
    import outbox  # noqa: PLC0415
    idem_key = outbox.make_idem_key("ledger", harvest_state.window_id("week"), "auto")
    assert outbox.get_receipt(idem_key)["message_id"] == "m-77"


def test_auto_digest_no_receipt_consumes_nothing_and_reattempts(env, monkeypatch):
    """A sender that RAISES (transport failure) => the digest delivered NOTHING:
    evidence NOT consumed, wins NOT consumed, NO ledger_draft_pushed, auto_pushed_
    window NOT set, push_blocked_reason carried. A SECOND fire re-attempts the send."""
    _set_productivity_env(monkeypatch)
    _stub_sources(monkeypatch, gh_payload=PR_PAYLOAD)
    harvest_ledger.capture_win("closed the Acme deal")

    first = _run(monkeypatch, auto=True, now=FRIDAY, sender=_raising_sender())
    assert first["draft_pushed"] is False
    assert first["push_blocked_reason"].startswith("delivery_failed:")
    # NOTHING consumed: no state persisted at all (no pushed-window, no seen evidence).
    assert harvest_state.load_state() is None
    assert [w["text"] for w in win_store.read_unseen_wins()] == ["closed the Acme deal"]
    assert "ledger_draft_pushed" not in _ledger_event_types(env["ledger"])

    # A SECOND fire re-attempts the send (deliver_once is called again -- the prior
    # failure recorded no receipt, so it is not short-circuited).
    _stub_sources(monkeypatch, gh_payload=PR_PAYLOAD)
    sent: list = []
    second = _run(monkeypatch, auto=True, now=FRIDAY, sender=fake_sender(sent, message_id="m-9"))
    assert len(sent) == 1  # the re-attempt actually called the sender
    assert second["draft_pushed"] is True
    assert second["evidence_count"] == 1  # the same PR is re-harvested + now delivered
    assert win_store.read_unseen_wins() == []  # the win is finally delivered


def test_auto_digest_refire_after_success_does_not_double_send(env, monkeypatch):
    """After a SUCCESSFUL send, a re-fire of the SAME window+kind does NOT double-send:
    deliver_once short-circuits on the recorded receipt and the evidence is already
    seen (so the harvest finds no new evidence -> no_new_evidence / already_pushed)."""
    _set_productivity_env(monkeypatch)
    _stub_sources(monkeypatch, gh_payload=PR_PAYLOAD)

    sent: list = []
    first = _run(monkeypatch, auto=True, now=FRIDAY, sender=fake_sender(sent, message_id="m-1"))
    assert first["draft_pushed"] is True
    assert len(sent) == 1

    # Re-fire the SAME window+kind. The auto_pushed_window dedup short-circuits before
    # the send, AND even if it did not, deliver_once would short-circuit on the receipt.
    _stub_sources(monkeypatch, gh_payload=PR_PAYLOAD)
    second = _run(monkeypatch, auto=True, now=FRIDAY, sender=fake_sender(sent, message_id="m-2"))
    assert second["draft_pushed"] is False
    assert second["reason"] in ("no_new_evidence", "already_pushed")
    assert len(sent) == 1  # NO second send -- the digest was not double-delivered


def test_auto_digest_proof_without_receipt_records_health_failure(env, monkeypatch):
    """A digest that PROVED a target but FAILED to deliver records a ledger_harvest
    health FAILURE -- no false-green (the exact O3 HIGH 1 invariant) -- on the real
    cron CLI flow."""
    _set_productivity_env(monkeypatch)
    _stub_sources(monkeypatch, gh_payload=PR_PAYLOAD)
    _run_cli_auto(monkeypatch, sender=_raising_sender())
    entry = cos_health.read_health()["ledger_harvest"]
    assert entry.get("last_success_ts") is None
    assert entry["last_failure"]["error_class"] == "push_blocked"
    assert entry["last_failure"]["trigger"] == "cron:ledger_harvest"


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


# === FIX 1: wins are seen-on-push (never silently lost after a digest) ========


def test_win_after_push_surfaces_in_next_digest_and_first_not_repeated(env, monkeypatch):
    """Capture win A, push a digest (A appears + becomes seen); capture win B AFTER
    that push; the NEXT pushed digest includes B and NOT A. A delivered win never
    repeats, and a win captured after the push is never silently lost."""
    _set_productivity_env(monkeypatch)
    _stub_sources(monkeypatch, gh_payload=[], gog_payload={"threads": []})

    harvest_ledger.capture_win("shipped the pricing deck")  # win A
    first = _run(monkeypatch, auto=True, now=FRIDAY)
    assert first["draft_pushed"] is True
    assert "shipped the pricing deck" in first["draft"]
    # A is now seen (delivered).
    assert "win:" in next(iter(win_store.read_seen_win_ids()))
    assert win_store.read_unseen_wins() == []

    # A win captured AFTER the push (the Fri-evening -> Sun gap) stays unseen.
    harvest_ledger.capture_win("closed the Acme deal")  # win B
    unseen = [w["text"] for w in win_store.read_unseen_wins()]
    assert unseen == ["closed the Acme deal"]

    # The next pushed digest (simulate next week's window) includes B and NOT A.
    _stub_sources(monkeypatch, gh_payload=[], gog_payload={"threads": []})
    monkeypatch.setattr(harvest_state, "iso_week_id", lambda reference=None: "2026-W99")
    second = _run(monkeypatch, auto=True, now=FRIDAY)
    assert second["draft_pushed"] is True
    assert "closed the Acme deal" in second["draft"]
    assert "shipped the pricing deck" not in second["draft"]


def test_unseen_old_win_still_surfaces_regardless_of_capture_date(env, monkeypatch):
    """A win captured 'last week' (captured_on before this week's Monday) that was
    NEVER delivered still appears in this week's digest -- seen-on-push, not the
    weekly window, is what consumes a win (the 'never silently lost' invariant)."""
    _set_productivity_env(monkeypatch)
    _stub_sources(monkeypatch, gh_payload=[], gog_payload={"threads": []})

    # Capture a win dated well before this week's Monday; it was never pushed.
    monkeypatch.setattr(cos_config, "local_today", lambda: __import__("datetime").date(2026, 6, 1))
    harvest_ledger.capture_win("decided to pivot the roadmap")
    monkeypatch.setattr(cos_config, "local_today", lambda: __import__("datetime").date(2026, 6, 19))

    # This week's digest still surfaces the old, undelivered win.
    result = _run(monkeypatch, auto=True, now=FRIDAY)
    assert result["draft_pushed"] is True
    assert "decided to pivot the roadmap" in result["draft"]


def test_blocked_push_does_not_consume_wins(env, monkeypatch):
    """A win is marked seen ONLY when the digest actually PUSHED. A blocked push
    (no proven target) leaves the win unseen so the next allowed fire delivers it."""
    # No productivity env => the push is BLOCKED (unprovable target).
    _clear_productivity_env(monkeypatch)
    _stub_sources(monkeypatch, gh_payload=[], gog_payload={"threads": []})
    harvest_ledger.capture_win("hired the new VP of Sales")

    blocked = _run(monkeypatch, auto=True, now=FRIDAY)
    assert blocked["draft_pushed"] is False
    assert blocked["push_blocked_reason"] is not None
    # The win was NOT consumed -- it is still unseen for the next (provable) fire.
    assert [w["text"] for w in win_store.read_unseen_wins()] == ["hired the new VP of Sales"]


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


def _run_cli_auto(monkeypatch, since="2026-01-01", *, sender=None):
    """Drive the REAL cron CLI path (--auto) so health is recorded by the real flow.

    The CLI does not take ``now``, so force the Friday digest-day gate open here --
    the tests target the harvest/push/health behavior, not the day gate (that is
    covered by the DONE-1 tests). The auto digest delivers itself through the
    receipt-backed outbox, so the default ``openclaw_sender`` is replaced with a fake
    receipt-returning sender (or a test-supplied raising one) -- the CLI never reaches
    real Telegram. A test exercising a BLOCKED push (env unset) never reaches the send,
    so the sender is moot there."""
    monkeypatch.setattr(harvest_ledger, "is_digest_day", lambda now=None: True)
    monkeypatch.setattr(harvest_ledger.ledger_delivery.outbox, "openclaw_sender",
                        sender or fake_sender())
    import argparse as _argparse
    args = _argparse.Namespace(window="week", since=since, dry_run=False, json=True, auto=True)
    return harvest_ledger._run_harvest_cli(args)


def test_auto_cli_stdout_carries_status_not_draft(env, monkeypatch, capsys):
    """Anti-double-send (O3 HIGH 1): the --auto CLI prints ONLY a compact status line on
    stdout, NEVER the draft. The auto digest already delivered itself via the
    receipt-backed outbox, so the cron's blind announce of stdout must carry nothing
    user-facing -- else it would re-send the digest the script already sent."""
    _set_productivity_env(monkeypatch)
    _stub_sources(monkeypatch, gh_payload=PR_PAYLOAD)
    rc = _run_cli_auto(monkeypatch)  # delivers via the fake receipt-returning sender
    assert rc == 0  # the CLI returns an exit code; clean success
    out = capsys.readouterr().out
    # The draft text is ABSENT from stdout (no re-announce / double-send)...
    assert "Accomplishment Ledger" not in out
    assert PR_PAYLOAD[0]["title"] not in out
    # ...only the operational status keys are present.
    assert '"draft_pushed": true' in out
    assert '"harvest_window_id"' in out
    assert '"delivery_target"' not in out  # the proven target is NOT leaked to stdout


def test_cron_source_error_records_failure_and_manifest_degraded(env, monkeypatch):
    """A SOURCE-SUBPROCESS failure on the REAL cron flow (gh exits nonzero) records a
    ledger_harvest FAILURE -- a quiet-empty digest is NOT mistaken for healthy. This
    drives the real flow (not a hand-made ok:false), the gap the old test missed."""
    _set_productivity_env(monkeypatch)
    _stub_sources(monkeypatch, gh_rc=1, gog_payload={"threads": []})
    _run_cli_auto(monkeypatch)

    entry = cos_health.read_health()["ledger_harvest"]
    assert entry["last_failure"]["error_class"] == "harvest_source_error"
    assert entry["last_failure"]["trigger"] == "cron:ledger_harvest"
    # No recorded success + a fresh failure => STALE-by-absent-success (loud, not green).
    line = next(l for l in cos_manifest.health_lines() if "ledger_harvest" in l)
    assert line.startswith("STALE ledger_harvest")
    assert "last_failure: harvest_source_error" in line


def test_cron_source_error_after_success_is_degraded(env, monkeypatch):
    """A source error AFTER a recorded success reads DEGRADED -- the most-recent real
    outcome is a failure (the manifest must not false-green on the stale success)."""
    _set_productivity_env(monkeypatch)
    _stub_sources(monkeypatch, gh_payload=PR_PAYLOAD)
    _run_cli_auto(monkeypatch)  # clean success first
    _stub_sources(monkeypatch, gh_rc=1, gog_payload={"threads": []})
    _run_cli_auto(monkeypatch)  # then a source error
    assert any("DEGRADED ledger_harvest" in line for line in cos_manifest.health_lines())


def test_cron_blocked_push_records_failure(env, monkeypatch):
    """A BLOCKED push (digest had content but the target was unprovable -- env unset)
    records a FAILURE: nothing was delivered, so it is not a healthy run."""
    # No productivity env => the push is blocked though there IS content.
    _clear_productivity_env(monkeypatch)
    _stub_sources(monkeypatch, gh_payload=PR_PAYLOAD)
    _run_cli_auto(monkeypatch)
    entry = cos_health.read_health()["ledger_harvest"]
    assert entry["last_failure"]["error_class"] == "push_blocked"
    assert entry["last_failure"]["trigger"] == "cron:ledger_harvest"


def test_cron_empty_week_records_success(env, monkeypatch):
    """A legitimately-empty week (sources ran CLEAN, nothing to report) records
    SUCCESS -- a quiet week is healthy, not a failure."""
    _set_productivity_env(monkeypatch)
    _stub_sources(monkeypatch, gh_payload=[], gog_payload={"threads": []})
    _run_cli_auto(monkeypatch, since="2030-01-01")
    entry = cos_health.read_health()["ledger_harvest"]
    assert entry.get("last_success_ts")
    assert entry.get("last_failure") is None
    assert any(line.startswith("OK ledger_harvest") for line in cos_manifest.health_lines())


def test_cron_crash_mid_harvest_records_failure(env, monkeypatch):
    """A HARD crash mid-harvest on the cron path records a FAILURE before the no-raw-
    leak boundary swallows it to exit 0 -- never false-green-until-STALE."""
    _set_productivity_env(monkeypatch)

    # Patch a non-source-handled crash point: match_evidence is called on every run.
    def explode(*a, **k):
        raise RuntimeError("matcher exploded")

    _stub_sources(monkeypatch, gh_payload=PR_PAYLOAD)
    monkeypatch.setattr(harvest_ledger, "match_evidence", explode)
    with pytest.raises(RuntimeError):
        _run_cli_auto(monkeypatch)
    entry = cos_health.read_health()["ledger_harvest"]
    assert entry["last_failure"]["error_class"] == "RuntimeError"
    assert entry["last_failure"]["trigger"] == "cron:ledger_harvest"


def test_cron_harvest_failure_after_success_is_degraded(env, monkeypatch):
    """A FRESH failure after a recorded success reads DEGRADED -- the most-recent
    outcome is a failure, so it must not false-green just because a success exists."""
    harvest_ledger._record_ledger_health({"ok": True})
    harvest_ledger._record_ledger_health({"ok": False, "reason": "harvest_failed"})
    assert any("DEGRADED ledger_harvest" in line for line in cos_manifest.health_lines())


def _run_cli_reactive(monkeypatch):
    import argparse as _argparse
    args = _argparse.Namespace(window="week", since="2026-01-01", dry_run=False,
                               json=True, auto=False)
    return harvest_ledger._run_harvest_cli(args)


def test_reactive_ledger_records_no_health(env, monkeypatch):
    """A reactive /ledger (auto=False) records NO ledger_harvest health -- the cron and
    reactive paths are never conflated (only the scheduled fire owns the health signal)."""
    _set_productivity_env(monkeypatch)
    _stub_sources(monkeypatch, gh_payload=PR_PAYLOAD)
    _run_cli_reactive(monkeypatch)
    assert "ledger_harvest" not in cos_health.read_health()


def test_reactive_source_error_records_no_health(env, monkeypatch):
    """A reactive /ledger with a source error still records NO health (reactive never
    owns the health signal, even on failure)."""
    _set_productivity_env(monkeypatch)
    _stub_sources(monkeypatch, gh_rc=1, gog_payload={"threads": []})
    _run_cli_reactive(monkeypatch)
    assert "ledger_harvest" not in cos_health.read_health()


def test_reactive_crash_records_no_health(env, monkeypatch):
    """A reactive /ledger that crashes records NO health -- only the cron path records
    a crash failure (no cron-vs-reactive conflation)."""
    _set_productivity_env(monkeypatch)

    def explode(*a, **k):
        raise RuntimeError("matcher exploded")

    _stub_sources(monkeypatch, gh_payload=PR_PAYLOAD)
    monkeypatch.setattr(harvest_ledger, "match_evidence", explode)
    with pytest.raises(RuntimeError):
        _run_cli_reactive(monkeypatch)
    assert "ledger_harvest" not in cos_health.read_health()
