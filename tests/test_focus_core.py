"""U3 Layer-1 + Layer-2 unit tests: capacity model + daily-priorities proposal.

Owns the unit invariants:
- I-CAP: active load <= ~1 week of capacity (estimate-sum vs WEEKLY_CAPACITY_HOURS,
  count vs ACTIVE_TASK_HARD_CAP); holding_tank/parking_lot members excluded from
  the cap count.
- I-DAILY3: 2-3 daily priorities are surfaced; veto/approve persist correctly;
  stale focus-state.date != today is treated as "not set".
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import cos_config  # noqa: E402
import defended_three  # noqa: E402
import focus_state  # noqa: E402
from focus_core import (  # noqa: E402
    count_active_tasks,
    evaluate_add,
    summarize_capacity,
)
from task_records import task_records  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_MGMT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("TASK_TRACKER_LEDGER_FILE", str(tmp_path / "events.jsonl"))
    # Pin the cap knobs so a host env override cannot skew the assertions.
    monkeypatch.setenv("WEEKLY_CAPACITY_HOURS", "25")
    monkeypatch.setenv("UNESTIMATED_TASK_HOURS", "2")
    monkeypatch.setenv("ACTIVE_TASK_HARD_CAP", "20")
    monkeypatch.setenv("DAILY_PRIORITY_COUNT", "3")
    yield


def _records(content: str):
    return task_records(content)


# --- I-CAP: capacity model -------------------------------------------------

def test_active_count_includes_section_none_excludes_parking_and_backlog():
    content = """# Work

## 🔴 Q1
- [ ] **Q1 task** task_id::tsk_aaaaaaaaaaaaaaaa

## 📋 All Tasks
### Dev
- [ ] **All Tasks one** task_id::tsk_bbbbbbbbbbbbbbbb
- [ ] **All Tasks two** task_id::tsk_cccccccccccccccc

## ⚪ Backlog
- [ ] **Backlog task** task_id::tsk_dddddddddddddddd

## 🅿️ Parking Lot
- [ ] **Parked** created::2026-06-01 task_id::tsk_eeeeeeeeeeeeeeee
"""
    # 1 Q1 + 2 section=None ("All Tasks") = 3 active; backlog + parking excluded.
    assert count_active_tasks(_records(content)) == 3


def test_estimate_sum_counts_unestimated_at_default_hours():
    content = """# Work

## 🔴 Q1
- [ ] **Estimated** estimate:: 3h task_id::tsk_aaaaaaaaaaaaaaaa
- [ ] **No estimate** task_id::tsk_bbbbbbbbbbbbbbbb

## 🅿️ Parking Lot
"""
    summary = summarize_capacity(_records(content))
    # 3h estimated + 1 unestimated @ 2h (UNESTIMATED_TASK_HOURS) = 5h = 300m.
    assert summary.estimated_minutes == 300
    assert summary.unestimated_count == 1
    assert summary.over_cap is False


def test_capacity_over_when_estimate_sum_exceeds_week():
    # 26h of estimated active work > 25h WEEKLY_CAPACITY_HOURS.
    content = """# Work

## 🔴 Q1
- [ ] **Big one** estimate:: 13h task_id::tsk_aaaaaaaaaaaaaaaa
- [ ] **Big two** estimate:: 13h task_id::tsk_bbbbbbbbbbbbbbbb

## 🅿️ Parking Lot
"""
    summary = summarize_capacity(_records(content))
    assert summary.estimate_exceeded is True
    assert summary.over_cap is True


def test_parking_lot_members_excluded_from_estimate_sum():
    # A parking-lot item with a huge estimate must not inflate the active load.
    content = """# Work

## 🔴 Q1
- [ ] **Active** estimate:: 1h task_id::tsk_aaaaaaaaaaaaaaaa

## 🅿️ Parking Lot
- [ ] **Parked huge** estimate:: 100h created::2026-06-01 task_id::tsk_bbbbbbbbbbbbbbbb
"""
    summary = summarize_capacity(_records(content))
    assert summary.active_count == 1
    assert summary.estimated_minutes == 60  # only the 1h active task


def test_objective_header_lines_excluded_from_cap():
    # On an objectives-format board, the parent objective line (is_objective) is a
    # grouping header, not work -- it must not inflate the count or estimate, so
    # Layer-2 agrees with Layer-1 (rank_active_records) and the standup DO-list.
    content = """# Board

## Objectives

- [ ] Hiring #hiring
  - [ ] **Write JD** estimate:: 1h task_id::tsk_aaaaaaaaaaaaaaaa
  - [ ] **Post listing** estimate:: 30m task_id::tsk_bbbbbbbbbbbbbbbb

## Today

## 🅿️ Parking Lot
"""
    recs = _records(content)
    assert any(r.is_objective for r in recs)  # the header is parsed as one
    assert count_active_tasks(recs) == 2  # only the two real subtasks
    summary = summarize_capacity(recs)
    assert summary.active_count == 2
    assert summary.estimated_minutes == 90  # 1h + 30m, no phantom 2h header


def test_count_cap_breaches_independently_of_estimate():
    # Tiny estimates but the count safety-valve trips.
    content_lines = ["# Work", "", "## 🔴 Q1"]
    for i in range(4):
        content_lines.append(f"- [ ] **T{i}** estimate:: 1m task_id::tsk_{i:016d}")
    content_lines += ["", "## 🅿️ Parking Lot", ""]
    content = "\n".join(content_lines)

    import os
    os.environ["ACTIVE_TASK_HARD_CAP"] = "3"
    try:
        summary = summarize_capacity(_records(content))
        assert summary.active_count == 4
        assert summary.count_exceeded is True
        assert summary.estimate_exceeded is False
        assert summary.over_cap is True
    finally:
        os.environ["ACTIVE_TASK_HARD_CAP"] = "20"


def test_capacity_counts_distinct_active_records_not_raw_lines():
    content = """# Work

## 🔴 Q1
- [ ] **Same id first** estimate:: 1h task_id::tsk_same
- [ ] **Same id duplicate** estimate:: 9h task_id::tsk_same
- [ ] **Bare duplicate** estimate:: 1h
- [ ] **Bare duplicate** estimate:: 9h

## 🅿️ Parking Lot
"""
    summary = summarize_capacity(_records(content))

    assert summary.active_count == 2
    assert summary.estimated_minutes == 120
    assert count_active_tasks(_records(content)) == 2


def test_capacity_dedupes_same_task_id_and_same_bare_title():
    content = """# Work

## 🔴 Q1
- [ ] **Shared id A** estimate:: 30m task_id::tsk_shared
- [ ] **Shared id B** estimate:: 30m task_id::tsk_shared
- [ ] **Repeated bare title** estimate:: 30m
- [ ] **Repeated bare title** estimate:: 30m

## 🅿️ Parking Lot
"""
    summary = summarize_capacity(_records(content))

    assert summary.active_count == 2
    assert summary.estimated_minutes == 60


def test_add_gate_uses_distinct_capacity_before_parking():
    import os

    os.environ["ACTIVE_TASK_HARD_CAP"] = "3"
    os.environ["WEEKLY_CAPACITY_HOURS"] = "6"
    try:
        content = """# Work

## 🔴 Q1
- [ ] **Shared id first** estimate:: 2h task_id::tsk_shared
- [ ] **Shared id duplicate** estimate:: 2h task_id::tsk_shared
- [ ] **Bare repeated** estimate:: 2h
- [ ] **Bare repeated** estimate:: 2h

## 🅿️ Parking Lot
"""
        decision = evaluate_add(content, "obsidian", "Legitimate new task")

        assert decision.allowed is True
        assert decision.summary is not None
        assert decision.summary.active_count == 2
        assert decision.summary.estimated_minutes == 240
        assert decision.summary.projected_count == 3
        assert decision.summary.projected_minutes == 360
    finally:
        os.environ["ACTIVE_TASK_HARD_CAP"] = "20"
        os.environ["WEEKLY_CAPACITY_HOURS"] = "25"


# --- I-DAILY3: daily-priorities proposal -----------------------------------

def test_proposes_at_most_daily_priority_count():
    content = """# Work

## 🔴 Q1
- [ ] **A** estimate:: 1h task_id::tsk_aaaaaaaaaaaaaaaa
- [ ] **B** estimate:: 1h task_id::tsk_bbbbbbbbbbbbbbbb

## 🟡 Q2
- [ ] **C** estimate:: 1h task_id::tsk_cccccccccccccccc
- [ ] **D** estimate:: 1h task_id::tsk_dddddddddddddddd
- [ ] **E** estimate:: 1h task_id::tsk_eeeeeeeeeeeeeeee

## 🅿️ Parking Lot
"""
    proposal = defended_three.propose_defended_three(_records(content))
    assert 2 <= len(proposal.defended) <= 3
    assert len(proposal.defended) == 3
    # The two beyond the count are demoted to holding_tank (NOT force-evicted).
    assert len(proposal.holding_tank) == 2
    positions = [row["position"] for row in proposal.defended]
    assert positions == [1, 2, 3]


def test_proposal_orders_q1_before_q2():
    content = """# Work

## 🟡 Q2
- [ ] **Q2 task** estimate:: 1h task_id::tsk_bbbbbbbbbbbbbbbb

## 🔴 Q1
- [ ] **Q1 task** estimate:: 1h task_id::tsk_aaaaaaaaaaaaaaaa

## 🅿️ Parking Lot
"""
    proposal = defended_three.propose_defended_three(_records(content))
    assert proposal.defended[0]["title"] == "Q1 task"


def test_write_proposal_persists_and_logs(tmp_path):
    content = """# Work

## 🔴 Q1
- [ ] **A** estimate:: 1h task_id::tsk_aaaaaaaaaaaaaaaa

## 🅿️ Parking Lot
"""
    proposal = defended_three.propose_defended_three(_records(content))
    state = defended_three.write_proposal(proposal)
    assert state["status"] == focus_state.STATUS_PROPOSED
    assert len(state["daily_priorities"]) == 1

    saved = json.loads((tmp_path / "state" / "focus-state.json").read_text())
    assert saved["status"] == "proposed"

    events = [json.loads(l) for l in (tmp_path / "events.jsonl").read_text().splitlines() if l.strip()]
    assert any(e["event_type"] == "focus_proposed" for e in events)
    proposed = next(e for e in events if e["event_type"] == "focus_proposed")
    assert proposed["source"] == "agent_autonomous"
    assert proposed["actor"] == "niemand-work"


def test_approve_transitions_status_and_logs(tmp_path):
    content = "# Work\n\n## 🔴 Q1\n- [ ] **A** estimate:: 1h task_id::tsk_aaaaaaaaaaaaaaaa\n\n## 🅿️ Parking Lot\n"
    state = defended_three.write_proposal(defended_three.propose_defended_three(_records(content)))
    approved = defended_three.approve_focus_state(state)
    assert approved["status"] == focus_state.STATUS_APPROVED
    assert approved["approved_at"] is not None

    events = [json.loads(l) for l in (tmp_path / "events.jsonl").read_text().splitlines() if l.strip()]
    assert any(e["event_type"] == "focus_approved" for e in events)


def test_override_records_user_explicit_reason():
    content = "# Work\n\n## 🔴 Q1\n- [ ] **A** estimate:: 1h task_id::tsk_aaaaaaaaaaaaaaaa\n\n## 🅿️ Parking Lot\n"
    state = defended_three.write_proposal(defended_three.propose_defended_three(_records(content)))
    approved = defended_three.approve_focus_state(state, override_reason="user_explicit")
    assert approved["override_reason"] == "user_explicit"


def test_veto_swaps_in_next_candidate_and_logs(tmp_path):
    content = """# Work

## 🔴 Q1
- [ ] **A** estimate:: 1h task_id::tsk_aaaaaaaaaaaaaaaa
- [ ] **B** estimate:: 1h task_id::tsk_bbbbbbbbbbbbbbbb
- [ ] **C** estimate:: 1h task_id::tsk_cccccccccccccccc
- [ ] **D** estimate:: 1h task_id::tsk_dddddddddddddddd

## 🅿️ Parking Lot
"""
    records = _records(content)
    state = defended_three.write_proposal(defended_three.propose_defended_three(records))
    # Default 3 priorities: A, B, C. Veto position 2 (B) -> D promoted.
    updated = defended_three.veto_and_repropose(state, 2, records)
    titles = [row["title"] for row in updated["daily_priorities"]]
    assert "B" not in titles
    assert "D" in titles
    assert len(updated["daily_priorities"]) == 3
    assert [row["position"] for row in updated["daily_priorities"]] == [1, 2, 3]

    events = [json.loads(l) for l in (tmp_path / "events.jsonl").read_text().splitlines() if l.strip()]
    vetoed = next(e for e in events if e["event_type"] == "focus_vetoed")
    assert vetoed["metadata"]["removed_task_id"] == "tsk_bbbbbbbbbbbbbbbb"

    # The promoted candidate (D) is NOT double-counted: it left the holding tank
    # when it became a daily priority, so no priority id appears in holding_tank.
    priority_ids = {row["task_id"] for row in updated["daily_priorities"]}
    holding_ids = {row["task_id"] for row in updated["holding_tank"]}
    assert priority_ids.isdisjoint(holding_ids)
    assert "tsk_dddddddddddddddd" not in holding_ids


def test_veto_keeps_priority_order_after_promotion():
    # Proposal A(q1), B(q1), C(q2). Veto A -> promote D(q1, from holding). The
    # promoted q1 task must NOT land below the q2 survivor C; the list stays
    # most-important-first.
    content = """# Work

## 🔴 Q1
- [ ] **A** estimate:: 1h task_id::tsk_aaaaaaaaaaaaaaaa
- [ ] **B** estimate:: 1h task_id::tsk_bbbbbbbbbbbbbbbb
- [ ] **D** estimate:: 1h task_id::tsk_dddddddddddddddd

## 🟡 Q2
- [ ] **C** estimate:: 1h task_id::tsk_cccccccccccccccc

## 🅿️ Parking Lot
"""
    records = _records(content)
    # Default 3 -> A, B, C (D in holding). Veto position 1 (A) -> D (q1) promoted.
    state = defended_three.write_proposal(defended_three.propose_defended_three(records))
    updated = defended_three.veto_and_repropose(state, 1, records)
    rows = updated["daily_priorities"]
    # The q2 task (C) must be last; the two q1 tasks (B, D) come first.
    sections = [row["section"] for row in rows]
    assert sections == sorted(sections, key=lambda s: {"q1": 0, "q2": 1, "q3": 2}.get(s, 3))
    assert rows[-1]["title"] == "C"


def test_veto_is_sticky_across_a_chain():
    # A,B,C proposed (holding: D). Veto B -> A,C,D. Veto A -> the previously
    # vetoed B must NOT be re-promoted; only a non-vetoed candidate fills the slot.
    content = """# Work

## 🔴 Q1
- [ ] **A** estimate:: 1h task_id::tsk_aaaaaaaaaaaaaaaa
- [ ] **B** estimate:: 1h task_id::tsk_bbbbbbbbbbbbbbbb
- [ ] **C** estimate:: 1h task_id::tsk_cccccccccccccccc
- [ ] **D** estimate:: 1h task_id::tsk_dddddddddddddddd

## 🅿️ Parking Lot
"""
    records = _records(content)
    state = defended_three.write_proposal(defended_three.propose_defended_three(records))
    state = defended_three.veto_and_repropose(state, 2, records)  # drop B
    # Now priorities are A, C, D. Veto position 1 (A).
    state = defended_three.veto_and_repropose(state, 1, records)
    titles = [row["title"] for row in state["daily_priorities"]]
    assert "B" not in titles  # B stays vetoed
    assert "A" not in titles
    assert set(state["vetoed"]) == {"tsk_aaaaaaaaaaaaaaaa", "tsk_bbbbbbbbbbbbbbbb"}


# --- Stale-date handling (date-independent cap; date-dependent priorities) ---

def test_stale_focus_state_treated_as_not_set():
    state = {
        "schema_version": 1,
        "date": "2020-01-01",
        "status": "approved",
        "daily_priorities": [],
    }
    assert focus_state.is_current(state) is False
    assert focus_state.status_for_today(state) is None


def test_current_focus_state_reports_status():
    state = {
        "schema_version": 1,
        "date": focus_state.today_str(),
        "status": "approved",
        "daily_priorities": [],
    }
    assert focus_state.is_current(state) is True
    assert focus_state.status_for_today(state) == "approved"


def test_corrupt_focus_state_quarantined_not_destroyed(tmp_path):
    path = focus_state.focus_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}not valid json")
    assert focus_state.load_focus_state() is None
    # The bad bytes survive aside for forensics; the live file is gone.
    quarantined = list(path.parent.glob("focus-state.json.corrupt-*"))
    assert len(quarantined) == 1
    assert quarantined[0].read_text() == "{}not valid json"
