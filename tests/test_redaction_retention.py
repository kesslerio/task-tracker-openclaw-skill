"""H10 privacy unit: redaction-by-default + undo-window-safe ledger retention.

Invariants pinned here (the Oracle finding: proving the destination doesn't prove
the CONTENT is appropriate):

* REDACT-BY-DEFAULT (Part A). An event carrying a raw ``body`` / ``snippet`` /
  ``content`` is persisted to the append-only ledger with its REFERENCES intact
  (subject/title, id, url, source_type) but the raw body STRIPPED -- the headline
  DONE test. An unknown oversized free-text field is truncated by default while
  short reference fields pass through. The redactor never raises on a malformed
  payload (fail-open). Email bodies are not (and defensively cannot be) stored.
* RETENTION (Part B). An OLD ledger entry beyond the window is pruned on append,
  but an entry INSIDE the undo window -- or referenced by a pending approval -- is
  NEVER pruned: a real ``/undo`` and ``/audit`` still resolve after the prune ran.

Fake chat ids only (``-4242424242`` / ``-5252525252``), never the public-hygiene
``-100xxxxxxxx`` pattern.
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import redaction
import task_ledger
from task_ledger import append_event, new_event, read_events


# --- Part A: redactor is pure + total (unit) --------------------------------


def test_content_fields_stripped_references_kept():
    """A body/snippet/content is dropped; subject/title/id/url survive."""
    event = {
        "event_type": "evidence_link",
        "task_id": "tsk_1",
        "timestamp": "2026-06-22T00:00:00+00:00",
        "evidence": {
            "source_type": "email",
            "source_url": "https://mail/thread/1",
            "match_title": "Q3 planning",
            "title": "Q3 planning subject",
            "body": "CONFIDENTIAL health detail about a customer",
            "snippet": "patient X said ...",
            "content": "the full email body text",
        },
        "metadata": {
            "description": "raw free-form customer notes",
            "score": 0.91,
            "decision": "evidence-link",
        },
    }
    out = redaction.redact_event(event)
    ev = out["evidence"]
    # References KEPT.
    assert ev["source_type"] == "email"
    assert ev["source_url"] == "https://mail/thread/1"
    assert ev["match_title"] == "Q3 planning"
    assert ev["title"] == "Q3 planning subject"
    # Raw content STRIPPED.
    assert ev["body"] == redaction.REDACTED_MARKER
    assert ev["snippet"] == redaction.REDACTED_MARKER
    assert ev["content"] == redaction.REDACTED_MARKER
    assert out["metadata"]["description"] == redaction.REDACTED_MARKER
    # Non-content references in metadata survive.
    assert out["metadata"]["score"] == 0.91
    assert out["metadata"]["decision"] == "evidence-link"
    # No raw substring leaks anywhere in the serialised event.
    blob = json.dumps(out)
    assert "CONFIDENTIAL" not in blob
    assert "patient X" not in blob
    assert "customer notes" not in blob


def test_unknown_oversized_field_redacted_short_references_pass():
    """Redact-by-default: an unknown large free-text field is truncated; short
    reference fields (id, url, type, short title) pass through untouched."""
    big = "Z" * 4000
    out = redaction.redact_event({
        "event_type": "x",
        "metadata": {
            "mystery_blob": big,
            "id": "abc-123",
            "url": "https://ref",
            "source_type": "pr",
            "title": "short title",
        },
    })
    m = out["metadata"]
    assert len(m["mystery_blob"]) < len(big)
    assert m["mystery_blob"].endswith("[redacted]")
    assert m["id"] == "abc-123"
    assert m["url"] == "https://ref"
    assert m["source_type"] == "pr"
    assert m["title"] == "short title"


def test_long_reference_field_is_not_over_redacted():
    """A long TITLE/subject is the product's signal and must NOT be capped --
    over-redacting the reference would blank the digest."""
    long_title = "T" * 2000
    out = redaction.redact_event({"event_type": "x", "evidence": {"title": long_title}})
    assert out["evidence"]["title"] == long_title
    daily_note_context_line = "  " + json.dumps({"task_id": "tsk_1", "title": long_title})
    out = redaction.redact_event({
        "event_type": "state_transition",
        "metadata": {
            "daily_note_path": "/tmp/" + "d" * 600,
            "daily_note_line": "- 12:34 ✅ " + long_title,
            "daily_note_context_line": daily_note_context_line,
        },
    })
    assert out["metadata"]["daily_note_path"] == "/tmp/" + "d" * 600
    assert out["metadata"]["daily_note_line"] == "- 12:34 ✅ " + long_title
    assert out["metadata"]["daily_note_context_line"] == daily_note_context_line


def test_short_text_reference_survives_long_text_blob_truncated():
    """A short ``text`` (a manual win line) survives; a long ``text`` blob is capped."""
    short = redaction.redact_event({"event_type": "x", "metadata": {"text": "shipped the deck"}})
    assert short["metadata"]["text"] == "shipped the deck"
    blob = redaction.redact_event({"event_type": "x", "metadata": {"text": "B" * 3000}})
    assert blob["metadata"]["text"].endswith("[redacted]")
    assert len(blob["metadata"]["text"]) < 3000


def test_nested_content_field_is_not_a_bypass():
    """A body nested inside a kept reference dict is still stripped."""
    out = redaction.redact_event({
        "event_type": "x",
        "metadata": {"candidate": {"title": "t", "body": "secret nested body"}},
    })
    assert out["metadata"]["candidate"]["title"] == "t"
    assert out["metadata"]["candidate"]["body"] == redaction.REDACTED_MARKER


def test_redactor_never_raises_on_malformed_or_empty_payload():
    """Fail-open: a malformed/empty/None payload returns a safe value, never raises."""
    assert redaction.redact_event({}) == {}
    assert redaction.redact_event(None) is None  # total: non-dict returned safe
    assert redaction.redact_payload(None) is None
    assert redaction.redact_payload([{"body": "x"}]) == [{"body": redaction.REDACTED_MARKER}]
    assert redaction.redact_message(None) == ""
    assert redaction.redact_message(12345) == "12345"
    # A deeply nested / weird payload still returns without raising.
    weird = {"metadata": {"a": {"b": {"c": {"body": "deep"}}}}}
    assert redaction.redact_event(weird)["metadata"]["a"]["b"]["c"]["body"] == redaction.REDACTED_MARKER


# --- Part A: centralized at the append seam (no bypass) ----------------------


def test_append_event_persists_references_not_raw_body(tmp_path, monkeypatch):
    """HEADLINE: a harvested email/event carrying a raw body is persisted with the
    subject/title + id + url but NOT the raw body. Centralized inside append_event,
    so NO caller can bypass it."""
    ledger = tmp_path / "events.jsonl"
    append_event(
        new_event(
            "evidence_link",
            task_id="tsk_42",
            source="ledger_agent",
            evidence={
                "source_type": "email",
                "source_url": "https://mail/thread/9",
                "title": "Quarterly review subject",
                "match_title": "Quarterly review",
                "body": "SENSITIVE: salary numbers and health notes",
                "snippet": "do not leak this",
            },
        ),
        path=ledger,
    )
    raw_file = ledger.read_text(encoding="utf-8")
    # The raw body never reaches disk.
    assert "SENSITIVE" not in raw_file
    assert "salary numbers" not in raw_file
    assert "do not leak this" not in raw_file
    # The references DO.
    [event] = read_events(ledger)
    ev = event["evidence"]
    assert ev["title"] == "Quarterly review subject"
    assert ev["source_url"] == "https://mail/thread/9"
    assert ev["source_type"] == "email"
    assert ev["body"] == redaction.REDACTED_MARKER
    assert ev["snippet"] == redaction.REDACTED_MARKER


def test_proactive_message_text_uses_references_not_bodies():
    """The assembled brag digest renders subjects/titles + the /approve hint, never
    a raw body -- and redact_message defensively caps an over-large body."""
    import harvest_ledger

    matches = [{
        "title": "Ship pricing page [acme/web#12]",
        "match_title": "Ship pricing page",
        "source_type": "pr",
        "url": "https://gh/acme/web/pull/12",
        "decision": "evidence-link",
        "matched_task_id": "tsk_pricing",
        "score": 0.95,
    }]
    draft = harvest_ledger.build_draft(matches, "2026-W25", wins=[])
    assert "Ship pricing page" in draft
    assert "/approve tsk_pricing" in draft
    # redact_message never clips a real (short) digest.
    assert redaction.redact_message(draft) == draft
    # But a body-sized blob spliced into a message is truncated.
    huge = "X" * 20000
    capped = redaction.redact_message(huge)
    assert len(capped) < len(huge)
    assert capped.endswith("[redacted]")
    # The SEND SEAM is actually wired: build_draft routes its assembled text through
    # redact_message, so an over-large value spliced into a digest line is capped by
    # build_draft itself -- not only by redact_message called in isolation.
    giant = [{
        "title": "X" * 20000, "match_title": "x", "source_type": "pr",
        "url": "u", "decision": "needs-review", "matched_task_id": "tsk_big", "score": 0.5,
    }]
    big_draft = harvest_ledger.build_draft(giant, "2026-W25", wins=[])
    assert len(big_draft) < 20000
    assert big_draft.endswith("[redacted]")


# --- Part B: retention is undo-window-safe -----------------------------------


def _stamped_event(event_type: str, *, when: datetime, **kw) -> dict:
    event = new_event(event_type, source="agent_autonomous", **kw)
    event["timestamp"] = when.isoformat()
    return event


def test_old_entry_pruned_in_window_entry_kept(tmp_path, monkeypatch):
    """An OLD entry beyond max(retention, undo-window) is pruned on the next append;
    a recent entry inside the undo window is NEVER pruned."""
    ledger = tmp_path / "events.jsonl"
    monkeypatch.setenv("TASK_TRACKER_LEDGER_FILE", str(ledger))
    # Tight retention so the test's "old" event is well past max(retention, undo).
    monkeypatch.setenv("LEDGER_RETENTION_DAYS", "1")
    monkeypatch.setenv("UNDO_WINDOW_BOARD_HOURS", "168")  # 7d floor

    now = datetime.now(timezone.utc)
    old = _stamped_event("agent_action", when=now - timedelta(days=30), task_id="tsk_old")
    recent = _stamped_event("agent_action", when=now - timedelta(hours=1), task_id="tsk_recent")
    # Write old + recent WITHOUT triggering a prune that would drop them prematurely:
    # both are written via append_event, but the prune keeps anything inside the
    # 7d floor. The OLD one (30d) is older than the floor, so it ages out on the
    # next append.
    append_event(old, path=ledger)
    append_event(recent, path=ledger)
    # A fresh append now triggers the prune; the 30d-old entry is dropped.
    append_event(new_event("agent_action", task_id="tsk_trigger", source="agent_autonomous"),
                 path=ledger)

    task_ids = [e.get("task_id") for e in read_events(ledger)]
    assert "tsk_old" not in task_ids       # pruned (30d > max(1d, 7d floor))
    assert "tsk_recent" in task_ids        # kept (1h, inside undo window)
    assert "tsk_trigger" in task_ids       # the just-appended event always survives


def test_retention_never_prunes_inside_undo_window(tmp_path, monkeypatch):
    """Even with a 0/short retention misconfig, an event inside the board undo
    window is kept -- retention can never drop below the undo-window floor."""
    ledger = tmp_path / "events.jsonl"
    monkeypatch.setenv("TASK_TRACKER_LEDGER_FILE", str(ledger))
    monkeypatch.setenv("LEDGER_RETENTION_DAYS", "1")
    monkeypatch.setenv("UNDO_WINDOW_BOARD_HOURS", "168")  # 7d

    now = datetime.now(timezone.utc)
    # 3 days old: PAST the 1d retention but INSIDE the 7d undo window -> must be kept.
    in_window = _stamped_event("pre_action_snapshot", when=now - timedelta(days=3),
                               task_id="tsk_inwindow")
    append_event(in_window, path=ledger)
    append_event(new_event("agent_action", task_id="tsk_new", source="agent_autonomous"),
                 path=ledger)

    task_ids = [e.get("task_id") for e in read_events(ledger)]
    assert "tsk_inwindow" in task_ids, "an in-undo-window event must never be pruned"


def test_prune_keeps_undateable_lines(tmp_path, monkeypatch):
    """A torn/undateable line is never dropped by the prune (we only drop lines we
    can confidently date as stale)."""
    ledger = tmp_path / "events.jsonl"
    monkeypatch.setenv("TASK_TRACKER_LEDGER_FILE", str(ledger))
    monkeypatch.setenv("LEDGER_RETENTION_DAYS", "1")
    monkeypatch.setenv("UNDO_WINDOW_BOARD_HOURS", "1")

    now = datetime.now(timezone.utc)
    old = _stamped_event("agent_action", when=now - timedelta(days=30), task_id="tsk_old")
    append_event(old, path=ledger)
    # Inject a line with no parseable timestamp directly.
    with ledger.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"event_type": "no_ts", "task_id": "tsk_nots"}) + "\n")
    # Trigger a prune.
    append_event(new_event("agent_action", task_id="tsk_new", source="agent_autonomous"),
                 path=ledger)

    task_ids = [e.get("task_id") for e in read_events(ledger)]
    assert "tsk_old" not in task_ids       # confidently stale -> pruned
    assert "tsk_nots" in task_ids          # undateable -> kept (never dropped blind)
    assert "tsk_new" in task_ids


def test_prune_cutoff_is_max_of_retention_and_undo_window(monkeypatch):
    """The cutoff floor is the undo window, even when retention is shorter."""
    monkeypatch.setenv("LEDGER_RETENTION_DAYS", "1")
    monkeypatch.setenv("UNDO_WINDOW_BOARD_HOURS", "168")
    now = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)
    cutoff = task_ledger._prune_cutoff(now)
    # max(1d, 7d) == 7d window.
    assert cutoff == now - timedelta(hours=168)

    monkeypatch.setenv("LEDGER_RETENTION_DAYS", "30")
    monkeypatch.setenv("UNDO_WINDOW_BOARD_HOURS", "168")
    cutoff = task_ledger._prune_cutoff(now)
    # max(30d, 7d) == 30d retention.
    assert cutoff == now - timedelta(hours=30 * 24)


# --- Part B: real /audit + /undo survive a prune -----------------------------


def test_audit_and_undo_still_resolve_after_prune(tmp_path, monkeypatch):
    """Drive the REAL prune path, then prove an in-window /audit + /undo still
    resolve: retention never deletes an event a live undo needs."""
    monkeypatch.setenv("TASK_MGMT_STATE_DIR", str(tmp_path / "state"))
    ledger = tmp_path / "ledger.events.jsonl"
    monkeypatch.setenv("TASK_TRACKER_LEDGER_FILE", str(ledger))
    monkeypatch.setenv("LEDGER_RETENTION_DAYS", "1")
    monkeypatch.setenv("UNDO_WINDOW_BOARD_HOURS", "168")

    import autonomy
    import autonomy_gate

    # A real board act with a snapshot, gated + logged ``executed`` through the
    # autonomy log (the proven pattern from test_autonomy_undo).
    raw_line = "- [ ] write the spec estimate::2h"
    board = tmp_path / "Weekly TODOs.md"
    board.write_text(f"# Board\n{raw_line}\n", encoding="utf-8")
    snapshot = {"file": str(board), "raw_line": raw_line, "line_number": 2}
    config = autonomy_gate.ensure_autonomy_config()
    config.setdefault("act_type_rungs", {})["wip_cap_enforced"] = autonomy_gate.RUNG_APPROVE
    autonomy_gate._atomic_write(
        autonomy_gate.autonomy_config_path(),
        json.dumps(config, indent=2, sort_keys=True) + "\n",
    )
    gated = autonomy_gate.gate(
        "wip_cap_enforced", task_id="tsk_spec", unit="U3",
        snapshot_provider=lambda: snapshot,
    )
    assert gated["ok"], gated
    act_id = gated["act_id"]

    # Pollute the ledger with a genuinely-OLD event, then force a prune via append.
    old = _stamped_event("agent_action",
                         when=datetime.now(timezone.utc) - timedelta(days=30),
                         task_id="tsk_ancient")
    append_event(old, path=ledger)
    append_event(new_event("agent_action", task_id="tsk_trigger", source="agent_autonomous"),
                 path=ledger)
    # The old event aged out, proving the prune actually ran.
    assert "tsk_ancient" not in [e.get("task_id") for e in read_events(ledger)]

    # The act is still discoverable via /audit and reversible via /undo -- the
    # in-window act log + its snapshot were NOT collateral-damaged by the prune.
    rows = autonomy.list_acts()
    assert any(r.get("act_id") == act_id for r in rows), "act vanished from /audit after prune"
    result = autonomy.undo_act(act_id)
    assert result["ok"], result
    # The line was never removed from the board, so undo is an idempotent no-op
    # restore -- the point is that the act + its snapshot survived the prune and the
    # undo path RESOLVED, not that the board changed.
    assert raw_line in board.read_text(encoding="utf-8")
