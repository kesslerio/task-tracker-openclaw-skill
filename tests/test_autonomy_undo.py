"""U2 invariants: REVERSIBILITY (/undo) + the delivery-target / push gate.

The DENIED paths are the load-bearing assertions here:

* REVERSIBILITY denied: a rung-2 board act with no snapshot is blocked at the
  gate (T2); an undo past its window is refused (T8); a double-undo is refused.
* DELIVERY-TARGET denied: the Work group is rejected at the gate (T5); a rung-3
  act that names a proven productivity target is STILL blocked because v0.1 ships
  board-only (no rung-3 push passes CI).
* NO-RAW-ERROR-LEAK denied path: an IO fault inside undo returns a structured
  error, never a traceback (T9).

Real chat ids are env-sourced at runtime; tests use fake ids that do NOT match
the public-hygiene pattern -100[0-9]{8,}.
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import autonomy
import autonomy_gate


# Fake ids: valid chat-id shape (^-?\d+$) but NOT -100xxxxxxxx, so the
# public-hygiene grep never flags them. Real ids are env-sourced in production.
PRODUCTIVITY_CHAT = "-4242424242"
WORK_GROUP_CHAT_ID = "-5252525252"

_PRODUCTIVITY_ENV = {
    "TELEGRAM_CHAT_ID_PRODUCTIVITY": PRODUCTIVITY_CHAT,
    "TELEGRAM_CHAT_ID_WORK": WORK_GROUP_CHAT_ID,
    "OPENCLAW_TOPIC_PRODUCTIVITY_STANDUP": "2",
    "OPENCLAW_TOPIC_PRODUCTIVITY_DONE": "5",
    "OPENCLAW_TOPIC_PRODUCTIVITY_IDENTITY": "1909",
}

TOPIC_2 = {"chat_id": PRODUCTIVITY_CHAT, "topic_id": "2",
           "agent_id": "niemand-work", "channel": "telegram"}
WORK_TARGET = {"chat_id": WORK_GROUP_CHAT_ID, "topic_id": "2",
               "agent_id": "niemand-work", "channel": "telegram"}


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_MGMT_STATE_DIR", str(tmp_path / "state"))
    # Point the task ledger at an isolated file so undo's ledger append is hermetic.
    monkeypatch.setenv("TASK_TRACKER_LEDGER_FILE", str(tmp_path / "ledger.events.jsonl"))
    for name, value in _PRODUCTIVITY_ENV.items():
        monkeypatch.setenv(name, value)
    yield


def _board_snapshot(tmp_path, raw_line, line_number):
    board = tmp_path / "Weekly TODOs.md"
    return board, {"file": str(board), "raw_line": raw_line, "line_number": line_number}


def _override_rung(act_type, rung):
    config = autonomy_gate.ensure_autonomy_config()
    config.setdefault("act_type_rungs", {})[act_type] = rung
    autonomy_gate._atomic_write(
        autonomy_gate.autonomy_config_path(),
        json.dumps(config, indent=2, sort_keys=True) + "\n",
    )


# --- REVERSIBILITY (gate denied path) ---------------------------------------

def test_rung2_board_act_without_snapshot_is_blocked():
    """T2: a rung-2 board mutation with no pre_action_snapshot is blocked."""
    _override_rung("wip_cap_enforced", autonomy_gate.RUNG_APPROVE)
    result = autonomy_gate.gate("wip_cap_enforced", task_id="tsk_abc", unit="U3")
    assert result["ok"] is False
    assert result["reason"] == "missing-snapshot"
    # No executed record exists for this act -> a later send is refused too.
    log = autonomy_gate.read_autonomy_log()
    statuses = [r["status"] for r in log if r["act_id"] == result["act_id"]]
    assert statuses == ["blocked:missing-snapshot"]


def test_rung2_board_act_with_snapshot_proceeds():
    """T1 (allowed path, for contrast): snapshot present -> executed + logged."""
    _override_rung("wip_cap_enforced", autonomy_gate.RUNG_APPROVE)
    snap = {"file": "/x/board.md", "raw_line": "- [ ] Task X estimate::2h", "line_number": 14}
    result = autonomy_gate.gate(
        "wip_cap_enforced", task_id="tsk_abc", unit="U3",
        snapshot_provider=lambda: snap,
    )
    assert result["ok"] is True
    assert result["record"]["status"] == "executed"
    assert result["record"]["pre_action_snapshot"]["raw_line"] == "- [ ] Task X estimate::2h"


# --- DELIVERY-TARGET PROOF (gate denied path) -------------------------------

def test_work_group_target_is_blocked_at_gate():
    """T5: the Work group is rejected -- a productivity nag must not ride it."""
    result = autonomy_gate.gate("nag_sent", delivery_target=WORK_TARGET, unit="U4")
    assert result["ok"] is False
    assert result["reason"] == "unproven-target"
    assert result["proof_reason"] == "work_group"
    assert result["record"]["delivery_target"] is None


def test_rung3_push_act_is_enabled_in_v0_2():
    """v0.2 (U4) ships the delivery seam: a rung-3 act naming a PROVEN target
    executes + binds, and a send to that SAME target is then authorised. The proof
    is unchanged -- an unproven target is still blocked (see the work-group test)."""
    assert autonomy_gate.RUNG3_PUSH_ENABLED is True
    result = autonomy_gate.gate("nag_sent", delivery_target=TOPIC_2, unit="U4")
    assert result["ok"] is True
    assert result["delivery_target"] == TOPIC_2
    # The send to the gated target is authorised; any other target is not.
    assert autonomy_gate.assert_send_target(result["act_id"], TOPIC_2)["ok"] is True


def test_rung3_board_only_act_without_target_still_executes():
    """A rung-3 act with NO delivery_target is a board act, not a push -> allowed."""
    _override_rung("monitored_board_act", autonomy_gate.RUNG_MONITORED_AUTO)
    snap = {"file": "/x/board.md", "raw_line": "- [ ] y", "line_number": 3}
    result = autonomy_gate.gate(
        "monitored_board_act", unit="U2", snapshot_provider=lambda: snap,
    )
    assert result["ok"] is True
    assert result["record"]["status"] == "executed"


# --- /undo of a nag act (REVERSIBILITY + NAG-CLOSE-ON-ACK) -------------------

def _gate_nag_executed(task_id):
    """Gate a nag act to 'executed' for undo tests.

    The undo path is keyed on act_type prefix + snapshot shape, not the rung, so
    we register nag_sent as a board-only rung-2 act (no delivery_target) and gate
    it with a snapshot. This isolates the undo logic from the push/delivery seam.
    """
    _override_rung("nag_sent", autonomy_gate.RUNG_APPROVE)
    snap = {"file": "/tmp/nope.md", "raw_line": "", "line_number": 0}  # nag has no board line
    return autonomy_gate.gate("nag_sent", task_id=task_id, unit="U4",
                              snapshot_provider=lambda: snap)


def test_undo_nag_acks_loop_and_records_reversal():
    """T6: /undo of a nag acks the loop (ack_type=user_undo) + logs reverted."""
    gated = _gate_nag_executed("tsk_3d89")
    result = autonomy.undo_act(gated["act_id"])
    assert result["ok"] is True
    assert result["kind"] == "nag"
    # nag-state.json shows the ack.
    nag_state = json.loads(autonomy_gate.nag_state_path().read_text())
    assert nag_state["tsk_3d89"]["ack"] is True
    assert nag_state["tsk_3d89"]["ack_type"] == "user_undo"
    # autonomy-log has a reverted record; task ledger has the reverted event.
    assert autonomy._already_reverted(gated["act_id"]) is True
    from task_ledger import read_events
    events = read_events()
    reverts = [e for e in events if e["event_type"] == "state_transition_reverted"]
    assert reverts and reverts[0]["metadata"]["reverted_act_id"] == gated["act_id"]
    assert reverts[0]["source"] == "agent_autonomous"


# --- /undo of a board mutation: CONTENT-SEARCH restore (T7) ------------------

def test_undo_board_restores_raw_line_by_content_search(tmp_path):
    """T7: undo restores the exact raw_line by CONTENT, not a line-number guess.

    The board has been edited since the act (the line is gone AND every other line
    has shifted), so a stored line_number is stale. The restore must key on the
    raw_line text and bring it back regardless.
    """
    raw_line = "- [ ] Migrate payments to Stripe estimate::4h"
    board, snapshot = _board_snapshot(tmp_path, raw_line, line_number=14)
    # Board AFTER the move: line absent, and content shifted so 14 points elsewhere.
    board.write_text("# Weekly TODOs\n- [ ] Other task A\n- [ ] Other task B\n",
                     encoding="utf-8")
    _override_rung("wip_cap_enforced", autonomy_gate.RUNG_APPROVE)
    gated = autonomy_gate.gate("wip_cap_enforced", task_id="tsk_pay", unit="U3",
                               snapshot_provider=lambda: snapshot)
    result = autonomy.undo_act(gated["act_id"])
    assert result["ok"] is True
    assert result["board_restored"] is True
    assert raw_line in board.read_text(encoding="utf-8")  # restored by content


def test_undo_board_is_idempotent_when_line_already_present(tmp_path):
    """If the raw_line is already on the board, restore is a no-op (no duplicate)."""
    raw_line = "- [ ] Already here estimate::1h"
    board, snapshot = _board_snapshot(tmp_path, raw_line, line_number=2)
    board.write_text(f"# Board\n{raw_line}\n", encoding="utf-8")
    _override_rung("wip_cap_enforced", autonomy_gate.RUNG_APPROVE)
    gated = autonomy_gate.gate("wip_cap_enforced", task_id="tsk_dup", unit="U3",
                               snapshot_provider=lambda: snapshot)
    result = autonomy.undo_act(gated["act_id"])
    assert result["ok"] is True
    assert result["board_restored"] is False
    assert board.read_text(encoding="utf-8").count(raw_line) == 1  # no duplicate


def test_restore_line_by_content_does_not_trust_line_number():
    """Unit: the restore matches text, not number; a wrong hint still restores."""
    content = "a\nb\nc"
    raw_line = "- [ ] restored me"
    new_content, restored = autonomy.restore_line_by_content(
        content, raw_line, line_number_hint=999)
    assert restored is True
    assert raw_line in new_content.split("\n")


def test_restore_replaces_post_action_line_in_place():
    """REPLACE undo: a [x] toggle is reversed by swapping the line, not duplicating.

    The act wrote `- [x] Task` over `- [ ] Task`. Undo must put `- [ ] Task` back
    WITHOUT leaving the `- [x]` copy -> exactly one task line, the original one.
    """
    before = "- [ ] Pay invoice estimate::1h"
    after = "- [x] Pay invoice estimate::1h"
    content = f"# Board\n{after}\n- [ ] Other\n"
    new_content, restored = autonomy.restore_line_by_content(
        content, before, post_raw_line=after, line_number_hint=2)
    assert restored is True
    lines = new_content.split("\n")
    assert before in lines
    assert after not in lines  # no duplicate / stale toggled copy
    assert lines.count(before) == 1


def test_restore_preserves_trailing_newline_no_blank_line():
    """P3a: append-style restore preserves the trailing newline; no blank line."""
    content = "# Board\n"
    raw_line = "- [ ] Re-added"
    new_content, restored = autonomy.restore_line_by_content(
        content, raw_line, line_number_hint=999)
    assert restored is True
    assert new_content.endswith("\n")  # trailing newline preserved
    assert "\n\n" not in new_content  # no spurious blank line injected
    assert raw_line in new_content.split("\n")


def test_undo_board_replace_act_does_not_duplicate(tmp_path):
    """End-to-end REPLACE undo: a checkbox-toggle act restores without a duplicate."""
    before = "- [ ] Ship the thing estimate::2h"
    after = "- [x] Ship the thing estimate::2h"
    board = tmp_path / "Weekly TODOs.md"
    board.write_text(f"# Board\n{after}\n", encoding="utf-8")
    snapshot = {"file": str(board), "raw_line": before, "post_raw_line": after,
                "line_number": 2}
    _override_rung("task_marked_done", autonomy_gate.RUNG_APPROVE)
    gated = autonomy_gate.gate("task_marked_done", task_id="tsk_rep", unit="U5",
                               snapshot_provider=lambda: snapshot)
    result = autonomy.undo_act(gated["act_id"])
    assert result["ok"] is True
    text = board.read_text(encoding="utf-8")
    assert before in text.split("\n")
    assert after not in text.split("\n")  # toggled copy gone, no duplicate


# --- H9: undo by STABLE id + board revision (not content guessing) ----------

def _enriched_snapshot(tmp_path, board_content, raw_line, line_number, *,
                       post_raw_line=None):
    """A board snapshot stamped with the H9 anchors (revision + task_id), built the
    way a real board writer would: from the board content AT act time."""
    board = tmp_path / "Weekly TODOs.md"
    board.write_text(board_content, encoding="utf-8")
    snapshot = autonomy.board_snapshot(board, raw_line, line_number,
                                       content=board_content, post_raw_line=post_raw_line)
    return board, snapshot


def test_undo_restores_correct_task_by_id_after_board_drift(tmp_path):
    """Headline correctness: after the board DRIFTS (an unrelated edit changed other
    lines + shifted line numbers), undo restores the RIGHT line by stable id -- the
    others are untouched. Content/line-number guessing could not do this safely."""
    raw_line = "- [ ] Migrate payments to Stripe task_id::tsk_pay estimate::4h"
    at_act_time = (f"# Board\n- [ ] Other A task_id::tsk_a\n{raw_line}\n"
                   "- [ ] Other B task_id::tsk_b\n")
    board, snapshot = _enriched_snapshot(tmp_path, at_act_time, raw_line, line_number=3)
    # The act toggled the target done; THEN the board drifted: an unrelated line was
    # edited and a new one inserted, so line numbers no longer match the snapshot.
    drifted = ("# Board\n- [ ] Other A EDITED task_id::tsk_a\n"
               "- [ ] Brand new task_id::tsk_new\n"
               "- [x] Migrate payments to Stripe task_id::tsk_pay estimate::4h\n"
               "- [ ] Other B task_id::tsk_b\n")
    board.write_text(drifted, encoding="utf-8")
    snapshot["post_raw_line"] = "- [x] Migrate payments to Stripe task_id::tsk_pay estimate::4h"
    _override_rung("task_marked_done", autonomy_gate.RUNG_APPROVE)
    gated = autonomy_gate.gate("task_marked_done", task_id="tsk_pay", unit="U5",
                               snapshot_provider=lambda: snapshot)
    result = autonomy.undo_act(gated["act_id"])
    assert result["ok"] is True
    assert result["board_restored"] is True
    assert result["resolved_by"] == "task_id"
    text = board.read_text(encoding="utf-8")
    assert raw_line in text.split("\n")  # the original (unchecked) line is back
    assert "- [x] Migrate payments to Stripe task_id::tsk_pay" not in text  # toggle gone
    # The drift to the OTHER lines is untouched -- undo restored only the target.
    assert "- [ ] Other A EDITED task_id::tsk_a" in text
    assert "- [ ] Brand new task_id::tsk_new" in text


def test_undo_duplicate_task_id_is_conflict_writes_nothing(tmp_path):
    """THE headline test: two board lines carry the SAME task_id as the snapshot's
    target -> undo REFUSES with a CONFLICT, writes NOTHING, and names the candidates.
    Never a wrong-line restore."""
    raw_line = "- [ ] Pay invoice task_id::tsk_dup estimate::1h"
    at_act_time = f"# Board\n{raw_line}\n"
    board, snapshot = _enriched_snapshot(tmp_path, at_act_time, raw_line, line_number=2)
    # Board drifts: the act's line was toggled AND a duplicate id snuck onto the board.
    drifted = ("# Board\n- [x] Pay invoice task_id::tsk_dup estimate::1h\n"
               "- [ ] Pay invoice AGAIN task_id::tsk_dup estimate::1h\n")
    board.write_text(drifted, encoding="utf-8")
    snapshot["post_raw_line"] = "- [x] Pay invoice task_id::tsk_dup estimate::1h"
    _override_rung("task_marked_done", autonomy_gate.RUNG_APPROVE)
    gated = autonomy_gate.gate("task_marked_done", task_id="tsk_dup", unit="U5",
                               snapshot_provider=lambda: snapshot)
    before = board.read_text(encoding="utf-8")
    result = autonomy.undo_act(gated["act_id"])
    assert result["ok"] is False
    assert result["reason"] == "conflict-duplicate"
    assert result["candidates"] == 2
    assert result["board_restored"] is False
    assert "tsk_dup" in result["message"] and "2" in result["message"]
    # NOTHING was written: the board is byte-identical to before the undo.
    assert board.read_text(encoding="utf-8") == before
    # And the act is NOT marked reverted -> the user can fix it and retry.
    assert autonomy._already_reverted(gated["act_id"]) is False


def test_undo_duplicate_raw_line_no_id_is_conflict(tmp_path):
    """The no-id path also refuses ambiguity: two IDENTICAL raw_lines (no stable id)
    that match the snapshot -> CONFLICT, no wrong-line restore."""
    after = "- [x] Buy milk"
    before = "- [ ] Buy milk"
    at_act_time = f"# Board\n{before}\n"
    board, snapshot = _enriched_snapshot(tmp_path, at_act_time, before, line_number=2,
                                         post_raw_line=after)
    # Two identical toggled copies appear; with no stable id, neither is resolvable.
    board.write_text(f"# Board\n{after}\n- [ ] something\n{after}\n", encoding="utf-8")
    _override_rung("task_marked_done", autonomy_gate.RUNG_APPROVE)
    gated = autonomy_gate.gate("task_marked_done", task_id="tsk_milk", unit="U5",
                               snapshot_provider=lambda: snapshot)
    before_text = board.read_text(encoding="utf-8")
    result = autonomy.undo_act(gated["act_id"])
    assert result["ok"] is False
    assert result["reason"] == "conflict-duplicate"
    assert result["board_restored"] is False
    assert board.read_text(encoding="utf-8") == before_text  # nothing written


def test_undo_unchanged_board_takes_revision_fast_path(tmp_path):
    """Board UNCHANGED since the act (current revision == snapshot revision) -> the
    direct content restore still works and is flagged resolved_by=revision."""
    raw_line = "- [ ] Ship it task_id::tsk_ship"
    after = "- [x] Ship it task_id::tsk_ship"
    at_act_time = f"# Board\n{after}\n"
    board, snapshot = _enriched_snapshot(tmp_path, at_act_time, raw_line, line_number=2,
                                         post_raw_line=after)
    # Board is byte-identical to act time (the toggled line is what the act wrote).
    _override_rung("task_marked_done", autonomy_gate.RUNG_APPROVE)
    gated = autonomy_gate.gate("task_marked_done", task_id="tsk_ship", unit="U5",
                               snapshot_provider=lambda: snapshot)
    result = autonomy.undo_act(gated["act_id"])
    assert result["ok"] is True
    assert result["resolved_by"] == "revision"
    assert result["board_restored"] is True
    text = board.read_text(encoding="utf-8").split("\n")
    assert raw_line in text and after not in text


def test_undo_edited_target_line_resolved_by_id_not_missed(tmp_path):
    """A later edit to the TARGET line means post_raw_line is no longer present
    verbatim, but the stable id still resolves it -> restored by id, not missed."""
    raw_line = "- [ ] Draft Q3 plan task_id::tsk_q3 estimate::2h"
    at_act_time = f"# Board\n{raw_line}\n"
    board, snapshot = _enriched_snapshot(tmp_path, at_act_time, raw_line, line_number=2,
                                         post_raw_line="- [x] Draft Q3 plan task_id::tsk_q3 estimate::2h")
    # The target line was edited AFTER the act: the due date changed + it was toggled,
    # so the snapshot's post_raw_line text no longer appears verbatim. Id still holds.
    board.write_text("# Board\n- [x] Draft Q3 plan task_id::tsk_q3 estimate::2h 📅 2026-07-01\n",
                     encoding="utf-8")
    _override_rung("task_marked_done", autonomy_gate.RUNG_APPROVE)
    gated = autonomy_gate.gate("task_marked_done", task_id="tsk_q3", unit="U5",
                               snapshot_provider=lambda: snapshot)
    result = autonomy.undo_act(gated["act_id"])
    assert result["ok"] is True
    assert result["resolved_by"] == "task_id"
    assert result["board_restored"] is True
    text = board.read_text(encoding="utf-8")
    assert raw_line in text.split("\n")  # original line restored via id
    # The edited/toggled variant is replaced in place -> no duplicate id on the board.
    assert text.count("task_id::tsk_q3") == 1
    # Transparency: the user's since-edit (the added date) was reverted, so the undo
    # must SURFACE that -- never silently overwrite a since-edited line.
    assert result["overwrote_edit"] is True
    assert "edited since the act" in result["message"]


def test_undo_id_match_not_found_is_conflict(tmp_path):
    """Drifted board where the target id is GONE and the original text is not present
    -> conflict-not-found, nothing written (we will not blindly re-insert)."""
    raw_line = "- [ ] Vanished task task_id::tsk_gone"
    at_act_time = f"# Board\n{raw_line}\n"
    board, snapshot = _enriched_snapshot(tmp_path, at_act_time, raw_line, line_number=2)
    # Board drifted and the line (id and all) is simply gone.
    board.write_text("# Board\n- [ ] Unrelated task_id::tsk_other\n", encoding="utf-8")
    snapshot["post_raw_line"] = "- [x] Vanished task task_id::tsk_gone"
    _override_rung("task_marked_done", autonomy_gate.RUNG_APPROVE)
    gated = autonomy_gate.gate("task_marked_done", task_id="tsk_gone", unit="U5",
                               snapshot_provider=lambda: snapshot)
    before = board.read_text(encoding="utf-8")
    result = autonomy.undo_act(gated["act_id"])
    assert result["ok"] is False
    assert result["reason"] == "conflict-not-found"
    assert board.read_text(encoding="utf-8") == before  # nothing written


def test_undo_old_snapshot_without_anchors_still_undoes(tmp_path):
    """Back-compat: an OLD snapshot lacking board_revision/task_id still undoes via
    the legacy content path -- it never crashes."""
    raw_line = "- [ ] Legacy line no id"
    board, snapshot = _board_snapshot(tmp_path, raw_line, line_number=14)  # no anchors
    assert "board_revision" not in snapshot and "task_id" not in snapshot
    board.write_text("# Board\n- [ ] Other A\n- [ ] Other B\n", encoding="utf-8")
    _override_rung("wip_cap_enforced", autonomy_gate.RUNG_APPROVE)
    gated = autonomy_gate.gate("wip_cap_enforced", task_id="tsk_legacy", unit="U3",
                               snapshot_provider=lambda: snapshot)
    result = autonomy.undo_act(gated["act_id"])
    assert result["ok"] is True
    assert result["board_restored"] is True
    assert result["resolved_by"] == "legacy-content"
    assert raw_line in board.read_text(encoding="utf-8").split("\n")


def test_legacy_id_bearing_removal_undo_reinserts_via_content_path(tmp_path):
    """Back-compat regression guard: a LEGACY snapshot (no board_revision) for a
    line-REMOVAL act whose raw_line carries a task_id:: marker must RE-INSERT the line
    via the content path (origin/main behavior), NOT refuse conflict-not-found because
    the id vanished with the removed line. The id+revision drift logic applies only to
    NEW snapshots that carry a revision."""
    raw_line = "- [ ] Ship the thing task_id::tsk_rm"
    board, snapshot = _board_snapshot(tmp_path, raw_line, line_number=3)  # no anchors
    assert "board_revision" not in snapshot
    # The act REMOVED the line; at undo time it is absent from the board.
    board.write_text("# Board\n- [ ] Other A\n- [ ] Other B\n", encoding="utf-8")
    _override_rung("wip_cap_enforced", autonomy_gate.RUNG_APPROVE)
    gated = autonomy_gate.gate("wip_cap_enforced", task_id="tsk_rm", unit="U3",
                               snapshot_provider=lambda: snapshot)
    result = autonomy.undo_act(gated["act_id"])
    assert result["ok"] is True
    assert result["board_restored"] is True
    assert result["resolved_by"] == "legacy-content"
    assert raw_line in board.read_text(encoding="utf-8").split("\n")


def test_anchored_removal_act_undo_reinserts_after_drift(tmp_path):
    """A NEW (anchored) snapshot for a line-REMOVAL act must RE-INSERT on undo, not
    refuse. A removal's board_revision is taken pre-mutation, so the board ALWAYS
    drifts (the act then removes the line), forcing the id path -- where the id is
    gone WITH the removed line. The removal (post_raw_line absent) re-inserts via the
    content path instead of conflict-not-found, so anchored removals stay undoable."""
    board = tmp_path / "Weekly TODOs.md"
    raw_line = "- [ ] Migrate billing task_id::tsk_mig"
    pre_content = f"# Board\n- [ ] Other A\n{raw_line}\n- [ ] Other B\n"
    board.write_text(pre_content, encoding="utf-8")
    # Anchored snapshot taken PRE-removal (no post_raw_line -> a removal act).
    snapshot = autonomy.board_snapshot(board, raw_line, 3, content=pre_content)
    assert snapshot["board_revision"].startswith("sha256:")
    assert snapshot["task_id"] == "tsk_mig"
    # The act removed the line AND the board drifted (other lines changed).
    board.write_text("# Board\n- [ ] Other A renamed\n- [ ] Other B\n- [ ] New C\n",
                     encoding="utf-8")
    _override_rung("wip_cap_enforced", autonomy_gate.RUNG_APPROVE)
    gated = autonomy_gate.gate("wip_cap_enforced", task_id="tsk_mig", unit="U3",
                               snapshot_provider=lambda: snapshot)
    result = autonomy.undo_act(gated["act_id"])
    assert result["ok"] is True
    assert result["board_restored"] is True
    assert result["resolved_by"] == "task_id"
    assert raw_line in board.read_text(encoding="utf-8").split("\n")


def test_board_snapshot_helper_stamps_revision_and_id(tmp_path):
    """Unit: board_snapshot() captures sha256 revision + the line's stable id."""
    board = tmp_path / "b.md"
    content = "# Board\n- [ ] Do thing task_id::tsk_x\n"
    board.write_text(content, encoding="utf-8")
    snap = autonomy.board_snapshot(board, "- [ ] Do thing task_id::tsk_x", 2,
                                   content=content)
    assert snap["task_id"] == "tsk_x"
    assert snap["board_revision"] == autonomy.board_revision(content)
    assert snap["board_revision"].startswith("sha256:")


def test_undo_survives_non_oserror_fault(tmp_path, monkeypatch):
    """P3b: a non-IO fault (e.g. ValueError) is caught -> structured error, no raise."""
    raw_line = "- [ ] Will hit a ValueError"
    board, snapshot = _board_snapshot(tmp_path, raw_line, line_number=2)
    board.write_text("# Board\n", encoding="utf-8")
    _override_rung("wip_cap_enforced", autonomy_gate.RUNG_APPROVE)
    gated = autonomy_gate.gate("wip_cap_enforced", task_id="tsk_ve", unit="U3",
                               snapshot_provider=lambda: snapshot)

    def boom(*args, **kwargs):
        raise ValueError("malformed state")

    monkeypatch.setattr(autonomy, "_append_ledger_revert", boom)
    result = autonomy.undo_act(gated["act_id"])
    assert result["ok"] is False
    assert result["reason"] == "error:internal"


def test_retry_after_marker_write_failure_does_not_duplicate_ledger_event(tmp_path, monkeypatch):
    """Partial-failure retry path: if the reverted-marker write fails AFTER the
    ledger event committed, a retry must not append a SECOND revert event.

    First undo: board restore + ledger event succeed, but the marker write
    (`_log_undo_outcome`) raises -> structured error, act NOT marked reverted.
    Second undo (retry): board restore is idempotent and the ledger append is now
    skipped (a revert event already exists) -> exactly one revert event total."""
    from task_ledger import read_events

    raw_line = "- [ ] Retry safe"
    board, snapshot = _board_snapshot(tmp_path, raw_line, line_number=2)
    board.write_text("# Board\n", encoding="utf-8")
    _override_rung("wip_cap_enforced", autonomy_gate.RUNG_APPROVE)
    gated = autonomy_gate.gate("wip_cap_enforced", task_id="tsk_retry", unit="U3",
                               snapshot_provider=lambda: snapshot)

    # First attempt: let the ledger append happen, then fail the marker write.
    real_log = autonomy._log_undo_outcome

    def fail_marker(act_id, status, **kwargs):
        raise OSError("marker write failed")

    monkeypatch.setattr(autonomy, "_log_undo_outcome", fail_marker)
    first = autonomy.undo_act(gated["act_id"])
    assert first["ok"] is False and first["reason"] == "error:internal"
    assert autonomy._already_reverted(gated["act_id"]) is False  # no marker yet

    # Retry with the marker write restored.
    monkeypatch.setattr(autonomy, "_log_undo_outcome", real_log)
    second = autonomy.undo_act(gated["act_id"])
    assert second["ok"] is True

    reverts = [e for e in read_events()
               if e["event_type"] == "state_transition_reverted"
               and e["metadata"]["reverted_act_id"] == gated["act_id"]]
    assert len(reverts) == 1  # the retry did NOT double-log the revert event


def test_undo_returns_refusal_even_if_breadcrumb_write_also_fails(tmp_path, monkeypatch):
    """P3b: if the error-breadcrumb write ALSO raises (e.g. log unwritable), undo
    still returns the structured refusal -- it never re-raises out of the except."""
    raw_line = "- [ ] Double fault"
    board, snapshot = _board_snapshot(tmp_path, raw_line, line_number=2)
    board.write_text("# Board\n", encoding="utf-8")
    _override_rung("wip_cap_enforced", autonomy_gate.RUNG_APPROVE)
    gated = autonomy_gate.gate("wip_cap_enforced", task_id="tsk_2x", unit="U3",
                               snapshot_provider=lambda: snapshot)

    def boom(*args, **kwargs):
        raise OSError("primary fault")

    def breadcrumb_boom(*args, **kwargs):
        raise OSError("log also unwritable")

    monkeypatch.setattr(autonomy, "_atomic_write", boom)
    monkeypatch.setattr(autonomy, "_log_undo_outcome", breadcrumb_boom)
    result = autonomy.undo_act(gated["act_id"])  # must not raise
    assert result["ok"] is False
    assert result["reason"] == "error:internal"


# --- Undo window + double-undo refusals (REVERSIBILITY denied paths) ---------

def test_undo_past_window_is_refused(tmp_path, monkeypatch):
    """T8: an act older than its undo window is refused; no mutation occurs."""
    raw_line = "- [ ] Old task"
    board, snapshot = _board_snapshot(tmp_path, raw_line, line_number=2)
    board.write_text("# Board\n", encoding="utf-8")
    _override_rung("wip_cap_enforced", autonomy_gate.RUNG_APPROVE)
    # Board undo window default is 168h; make this act 200h old.
    monkeypatch.setenv("UNDO_WINDOW_BOARD_HOURS", "168")
    gated = autonomy_gate.gate("wip_cap_enforced", task_id="tsk_old", unit="U3",
                               snapshot_provider=lambda: snapshot)
    _age_act(gated["act_id"], hours=200)
    result = autonomy.undo_act(gated["act_id"])
    assert result["ok"] is False
    assert result["reason"] == "undo-window-expired"
    assert raw_line not in board.read_text(encoding="utf-8")  # board untouched


def test_double_undo_is_refused(tmp_path):
    """A second /undo of the same act is refused (idempotent-by-refusal)."""
    raw_line = "- [ ] Once only"
    board, snapshot = _board_snapshot(tmp_path, raw_line, line_number=2)
    board.write_text("# Board\n", encoding="utf-8")
    _override_rung("wip_cap_enforced", autonomy_gate.RUNG_APPROVE)
    gated = autonomy_gate.gate("wip_cap_enforced", task_id="tsk_twice", unit="U3",
                               snapshot_provider=lambda: snapshot)
    assert autonomy.undo_act(gated["act_id"])["ok"] is True
    second = autonomy.undo_act(gated["act_id"])
    assert second["ok"] is False
    assert second["reason"] == "already-reverted"


def test_undo_unknown_act_is_refused():
    result = autonomy.undo_act("act_doesnotexist")
    assert result["ok"] is False
    assert result["reason"] == "unknown-act"


def test_concurrent_undo_appends_at_most_one_revert_event(tmp_path):
    """The undo lock serializes the cycle: N concurrent /undo of the same act
    yield exactly one success + one state_transition_reverted event, not N."""
    from concurrent.futures import ThreadPoolExecutor

    from task_ledger import read_events

    raw_line = "- [ ] Race me"
    board, snapshot = _board_snapshot(tmp_path, raw_line, line_number=2)
    board.write_text("# Board\n", encoding="utf-8")
    _override_rung("wip_cap_enforced", autonomy_gate.RUNG_APPROVE)
    gated = autonomy_gate.gate("wip_cap_enforced", task_id="tsk_race", unit="U3",
                               snapshot_provider=lambda: snapshot)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: autonomy.undo_act(gated["act_id"]), range(8)))

    successes = [r for r in results if r["ok"]]
    already = [r for r in results if not r["ok"] and r["reason"] == "already-reverted"]
    assert len(successes) == 1  # exactly one winner
    assert len(already) == 7    # the rest are refused, not duplicated
    reverts = [e for e in read_events()
               if e["event_type"] == "state_transition_reverted"
               and e["metadata"]["reverted_act_id"] == gated["act_id"]]
    assert len(reverts) == 1  # at most one revert event per act


def test_undo_blocked_act_is_refused():
    """An act the gate BLOCKED made no change, so it cannot be undone."""
    result = autonomy_gate.gate("nag_sent", delivery_target=WORK_TARGET, unit="U4")
    assert result["ok"] is False  # blocked at gate
    undo = autonomy.undo_act(result["act_id"])
    assert undo["ok"] is False
    assert undo["reason"] == "act-not-executed"


# --- NO-RAW-ERROR-LEAK (T9): IO fault returns structured error --------------

def test_undo_io_fault_returns_structured_error(tmp_path, monkeypatch):
    """T9: an IO fault during a board restore is caught -> structured error, no raise."""
    raw_line = "- [ ] Will fail to restore"
    board, snapshot = _board_snapshot(tmp_path, raw_line, line_number=2)
    board.write_text("# Board\n", encoding="utf-8")
    _override_rung("wip_cap_enforced", autonomy_gate.RUNG_APPROVE)
    gated = autonomy_gate.gate("wip_cap_enforced", task_id="tsk_io", unit="U3",
                               snapshot_provider=lambda: snapshot)

    def boom(*args, **kwargs):
        raise OSError("disk gone")

    monkeypatch.setattr(autonomy, "_atomic_write", boom)
    result = autonomy.undo_act(gated["act_id"])
    assert result["ok"] is False
    assert result["reason"] == "error:internal"
    assert "internal error" in result["message"]
    # The act is NOT marked reverted, so a retry once the disk recovers can work.
    assert autonomy._already_reverted(gated["act_id"]) is False


# --- /audit read model ------------------------------------------------------

def test_list_acts_newest_first_and_folds_reverted(tmp_path):
    """list_acts returns canonical acts newest-first; a reverted act is flagged."""
    raw_line = "- [ ] Audit me"
    board, snapshot = _board_snapshot(tmp_path, raw_line, line_number=2)
    board.write_text("# Board\n", encoding="utf-8")
    _override_rung("wip_cap_enforced", autonomy_gate.RUNG_APPROVE)
    g1 = autonomy_gate.gate("wip_cap_enforced", task_id="tsk_a", unit="U3",
                            snapshot_provider=lambda: snapshot)
    g2 = autonomy_gate.gate("wip_cap_enforced", task_id="tsk_b", unit="U3",
                            snapshot_provider=lambda: dict(snapshot, raw_line="- [ ] two"))
    autonomy.undo_act(g1["act_id"])
    acts = autonomy.list_acts(since_hours=48, limit=10)
    ids = [a["act_id"] for a in acts]
    assert g1["act_id"] in ids and g2["act_id"] in ids
    by_id = {a["act_id"]: a for a in acts}
    assert by_id[g1["act_id"]]["reverted"] is True
    assert by_id[g2["act_id"]]["reverted"] is False


def test_audit_window_excludes_old_acts(tmp_path):
    snapshot = {"file": str(tmp_path / "b.md"), "raw_line": "- [ ] old", "line_number": 1}
    _override_rung("wip_cap_enforced", autonomy_gate.RUNG_APPROVE)
    gated = autonomy_gate.gate("wip_cap_enforced", task_id="tsk_o", unit="U3",
                               snapshot_provider=lambda: snapshot)
    _age_act(gated["act_id"], hours=100)
    assert autonomy.list_acts(since_hours=48) == []


def test_default_audit_window_lists_still_undoable_board_act(tmp_path):
    """Default /audit window == the 7d board undo window: a 100h-old board act
    (past 48h but still undoable) is discoverable by default, so the documented
    audit->undo flow works for the whole undo window."""
    board = tmp_path / "b.md"
    board.write_text("# Board\n", encoding="utf-8")
    snapshot = {"file": str(board), "raw_line": "- [ ] still undoable", "line_number": 1}
    _override_rung("wip_cap_enforced", autonomy_gate.RUNG_APPROVE)
    gated = autonomy_gate.gate("wip_cap_enforced", task_id="tsk_disc", unit="U3",
                               snapshot_provider=lambda: snapshot)
    _age_act(gated["act_id"], hours=100)  # > 48h, < 168h board window
    listed = autonomy.list_acts()  # default window
    assert any(a["act_id"] == gated["act_id"] for a in listed)
    # And it is genuinely still undoable -> the listing did not lie.
    assert autonomy.undo_act(gated["act_id"])["ok"] is True


# --- helpers ----------------------------------------------------------------

def _age_act(act_id, *, hours):
    """Rewrite the act's logged timestamp to `hours` in the past (in place)."""
    log_path = autonomy_gate.autonomy_log_path()
    old = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    lines = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if record.get("act_id") == act_id:
            record["timestamp"] = old
        lines.append(json.dumps(record, ensure_ascii=False, sort_keys=True))
    autonomy_gate._atomic_write(log_path, "\n".join(lines) + "\n")
