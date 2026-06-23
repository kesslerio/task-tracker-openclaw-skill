"""Contract 1: event_type registry.

Invariant: every new Chief-of-Staff event_type is registered (no unregistered
warning), round-trips through the ledger, and a full-ledger scan / the
completion_candidates projection does not misread or choke on them.
"""

import sys
import warnings
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import task_ledger
from task_ledger import KNOWN_EVENT_TYPES, append_event, new_event, read_events
import completion_candidates

# Every event_type the spec requires Phase 0a to register (Contract 1).
REQUIRED_NEW_TYPES = [
    "system_error",
    "agent_action", "state_transition_reverted", "pre_action_snapshot",
    "pre_action_snapshot_cancelled",
    "focus_proposed", "focus_approved", "focus_vetoed", "wip_cap_enforced",
    "disposition", "disposition_skipped", "capacity_overcommit",
    "nag_opened", "nag_sent", "nag_acked", "nag_snoozed", "nag_delivery_blocked",
    "body_double_started", "body_double_checkin", "body_double_ended",
    "ledger_harvest_started", "ledger_draft_pushed", "evidence_link",
    "ledger_approved", "ledger_rejected", "harvest_error",
    "calendar_block_created", "calendar_block_moved", "calendar_block_deleted",
    "calendar_block_refused", "brief_sent", "debrief_captured",
    "commitment_task_created", "freebusy_check_passed", "freebusy_check_failed",
    "delivery_target_resolved", "delivery_target_proof_failed",
    # U5 -- EOD forced disposition. Dual-registration contract: these MUST also be
    # in task_ledger.KNOWN_EVENT_TYPES (this test asserts both sides match).
    "eod_disposition_done", "eod_disposition_carry",
    "eod_disposition_reschedule", "eod_disposition_drop",
    # U6 -- set tomorrow's #1 (the loop's write side). Dual-registration contract:
    # MUST also be in task_ledger.KNOWN_EVENT_TYPES (this test asserts both match).
    "eod_tomorrow_top_set",
    # U7 -- the EOD delivered + ## EOD Summary upserted (end-to-end proof). Dual-
    # registration contract: MUST also be in task_ledger.KNOWN_EVENT_TYPES.
    "eod_summary_written",
]


def test_all_required_types_are_registered():
    missing = [t for t in REQUIRED_NEW_TYPES if t not in KNOWN_EVENT_TYPES]
    assert missing == [], f"unregistered event types: {missing}"


def test_agent_autonomous_source_recognised():
    assert "agent_autonomous" in task_ledger.KNOWN_EVENT_SOURCES


def test_registered_type_emits_no_warning():
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # any warning becomes an error
        for event_type in REQUIRED_NEW_TYPES:
            new_event(event_type, source="agent_autonomous")


def test_unregistered_type_warns():
    with pytest.warns(RuntimeWarning, match="Unregistered ledger event_type"):
        new_event("totally_made_up_type")


def test_new_types_round_trip_through_ledger(tmp_path):
    ledger = tmp_path / "events.jsonl"
    for i, event_type in enumerate(REQUIRED_NEW_TYPES):
        append_event(
            new_event(event_type, task_id=f"tsk_{i}", source="agent_autonomous"),
            path=ledger,
        )
    events = read_events(ledger)
    assert [e["event_type"] for e in events] == REQUIRED_NEW_TYPES


def test_completion_candidates_ignores_new_types(tmp_path):
    """A ledger full of new CoS types must not break the candidate projection --
    it filters on its own DECISION_EVENTS allowlist, so new types are skipped, not
    misread."""
    ledger = tmp_path / "events.jsonl"
    # Interleave a real candidate event with the new CoS types.
    append_event(
        new_event("candidate_seen", task_id="cand_1", source="completion_candidate_cli",
                  metadata={"candidate": {"candidate_id": "cand_1", "status": "new",
                                          "title": "t"}}),
        path=ledger,
    )
    for event_type in REQUIRED_NEW_TYPES:
        append_event(new_event(event_type, task_id="tsk_x", source="agent_autonomous"),
                     path=ledger)

    candidates = completion_candidates.project_candidates(ledger, include_terminal=True)
    ids = {c.get("candidate_id") for c in candidates}
    assert "cand_1" in ids  # the real candidate survives
    # No CoS task_id leaked into the candidate set.
    assert "tsk_x" not in ids
