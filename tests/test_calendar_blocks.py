"""U6 calendar blocks: NEVER-OVERBOOK-EXTERNAL (the unit invariant).

These tests assert the invariant on the DENIED paths, not just the happy path:

* a freebusy OVERLAP refuses the create (no gog create call) -- T2.
* a freebusy UNKNOWN (tool missing / timeout / bad JSON) is treated as BUSY and
  refuses the create (T7) -- an unknown freebusy is never assumed free.
* deleting / moving a NON-agent-created event raises ExternalEventError and makes
  no write -- T3.
* slip recovery is a gog calendar UPDATE, never delete+create -- T5.
* a delete without explicit approval makes no gog call.

The subprocess boundary is the injectable ``runner`` so "created vs refused" is
deterministic without a live gog. Fake calendar ids only -- no real ids.
"""

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import calendar_blocks  # noqa: E402

FOCUS_CAL = "task-focus-cal@group.calendar.google.com"
EXTERNAL_CAL = "primary"

BLOCK_START = "2026-06-20T09:00:00+00:00"
BLOCK_END = "2026-06-20T11:00:00+00:00"


def _completed(stdout: str, returncode: int = 0):
    return types.SimpleNamespace(stdout=stdout, stderr="", returncode=returncode)


class RecordingRunner:
    """A gog runner stub that records every command and replays canned JSON."""

    def __init__(self, responses):
        # responses: dict keyed by the gog subcommand (cmd[1]+cmd[2]) -> stdout str
        self.responses = responses
        self.calls: list[list[str]] = []

    def __call__(self, cmd):
        self.calls.append(cmd)
        key = f"{cmd[1]}.{cmd[2]}"  # e.g. "calendar.freebusy"
        if key not in self.responses:
            raise AssertionError(f"unexpected gog call: {cmd}")
        return _completed(self.responses[key])

    def made(self, key: str) -> bool:
        return any(f"{c[1]}.{c[2]}" == key for c in self.calls)


def _freebusy_json(busy):
    """A realistic gog freebusy response: an entry for EVERY calendar these tests
    request (gog returns one per requested calendar). The busy slots are on the
    external calendar; the agent's own focus calendar is free."""
    import json
    return json.dumps({"calendars": {EXTERNAL_CAL: {"busy": busy}, FOCUS_CAL: {"busy": []}}})


def _agent_event_json():
    import json
    return json.dumps({"id": "evt_1", "extendedProperties": {"private": {"agent_created": "task-tracker"}}})


def _external_event_json():
    import json
    return json.dumps({"id": "evt_x", "extendedProperties": {"private": {}}})


# --- T2: freebusy overlap refuses the create (NEVER-OVERBOOK-EXTERNAL) ------

def test_t2_freebusy_overlap_refuses_create():
    """A meeting overlapping the proposed slot => OverbookError, NO create call."""
    runner = RecordingRunner({
        "calendar.freebusy": _freebusy_json([
            {"start": "2026-06-20T09:30:00+00:00", "end": "2026-06-20T10:30:00+00:00"},
        ]),
    })
    with pytest.raises(calendar_blocks.OverbookError) as exc:
        calendar_blocks.create_focus_block(
            FOCUS_CAL, "tsk_abc", "Draft roadmap", BLOCK_START, BLOCK_END,
            freebusy_calendar_ids=[FOCUS_CAL, EXTERNAL_CAL], runner=runner)
    assert exc.value.reason == "freebusy_overlap"
    assert runner.made("calendar.freebusy")
    assert not runner.made("calendar.create")  # the write NEVER happened


# --- T7: freebusy UNKNOWN (timeout/missing/bad json) treated as busy --------

@pytest.mark.parametrize("response", ["", "not json", "{}"])
def test_t7_freebusy_unknown_treated_as_busy_refuses(response):
    """An unparseable / empty / no-calendars freebusy is treated as BUSY -> refuse."""
    runner = RecordingRunner({"calendar.freebusy": response})
    with pytest.raises(calendar_blocks.OverbookError) as exc:
        calendar_blocks.create_focus_block(
            FOCUS_CAL, "tsk_abc", "Draft roadmap", BLOCK_START, BLOCK_END,
            freebusy_calendar_ids=[FOCUS_CAL], runner=runner)
    assert exc.value.reason == "freebusy_unknown"
    assert not runner.made("calendar.create")


def test_t7_freebusy_timeout_treated_as_busy_refuses():
    """A subprocess timeout => unknown => busy => refuse, no traceback escapes."""
    import subprocess

    def timeout_runner(cmd):
        raise subprocess.TimeoutExpired(cmd, calendar_blocks.GOG_TIMEOUT_SECONDS)

    with pytest.raises(calendar_blocks.OverbookError) as exc:
        calendar_blocks.create_focus_block(
            FOCUS_CAL, "tsk_abc", "Draft roadmap", BLOCK_START, BLOCK_END,
            freebusy_calendar_ids=[FOCUS_CAL], runner=timeout_runner)
    assert exc.value.reason == "freebusy_unknown"


def test_no_freebusy_calendars_treated_as_busy():
    """No configured calendars => unknown => busy => refuse (degrade, never overbook)."""
    runner = RecordingRunner({})
    with pytest.raises(calendar_blocks.OverbookError):
        calendar_blocks.create_focus_block(
            FOCUS_CAL, "tsk_abc", "Draft roadmap", BLOCK_START, BLOCK_END,
            freebusy_calendar_ids=[], runner=runner)


# --- Happy path: free slot creates the block (REVERSIBILITY: id stored) -----

def test_create_with_no_event_id_refuses():
    """autoreview P3: a create response missing an id must NOT store an un-reversible
    block -- it is treated as a failure (OverbookError reason=no_event_id)."""
    runner = RecordingRunner({
        "calendar.freebusy": _freebusy_json([]),
        "calendar.create": '{"summary": "x"}',  # no id / event_id
    })
    with pytest.raises(calendar_blocks.OverbookError) as exc:
        calendar_blocks.create_focus_block(
            FOCUS_CAL, "tsk_abc", "Draft", BLOCK_START, BLOCK_END,
            freebusy_calendar_ids=[FOCUS_CAL], runner=runner)
    assert exc.value.reason == "no_event_id"


def test_create_on_free_slot_succeeds():
    runner = RecordingRunner({
        "calendar.freebusy": _freebusy_json([
            {"start": "2026-06-20T13:00:00+00:00", "end": "2026-06-20T14:00:00+00:00"},
        ]),
        "calendar.create": '{"id": "evt_new_1"}',
    })
    result = calendar_blocks.create_focus_block(
        FOCUS_CAL, "tsk_abc", "Draft roadmap", BLOCK_START, BLOCK_END,
        freebusy_calendar_ids=[FOCUS_CAL, EXTERNAL_CAL], runner=runner)
    assert result["event_id"] == "evt_new_1"
    assert runner.made("calendar.create")
    # the create carries the agent_created private property so it is recognisable
    create_cmd = next(c for c in runner.calls if c[1:3] == ["calendar", "create"])
    assert "agent_created=task-tracker" in create_cmd


# --- T3: act on an external (non-agent) event => ExternalEventError ----------

def test_t3_delete_external_event_refused():
    """Deleting a non-agent event => ExternalEventError, no delete call (approved)."""
    runner = RecordingRunner({"calendar.event": _external_event_json()})
    with pytest.raises(calendar_blocks.ExternalEventError) as exc:
        calendar_blocks.delete_focus_block(EXTERNAL_CAL, "evt_x", approved=True, runner=runner)
    assert exc.value.event_id == "evt_x"
    assert not runner.made("calendar.delete")


def test_t3_move_external_event_refused():
    """Sliding a non-agent event => ExternalEventError, no update call."""
    runner = RecordingRunner({"calendar.event": _external_event_json()})
    with pytest.raises(calendar_blocks.ExternalEventError):
        calendar_blocks.move_focus_block(
            EXTERNAL_CAL, "evt_x", "tsk_abc", BLOCK_START, BLOCK_END, runner=runner)
    assert not runner.made("calendar.update")


def test_delete_without_approval_makes_no_call():
    """A delete is irreversible: no approval => no gog call at all."""
    runner = RecordingRunner({})
    result = calendar_blocks.delete_focus_block(FOCUS_CAL, "evt_1", approved=False, runner=runner)
    assert result["ok"] is False
    assert result["reason"] == "needs_approval"
    assert runner.calls == []


def test_delete_agent_event_with_approval_succeeds():
    runner = RecordingRunner({
        "calendar.event": _agent_event_json(),
        "calendar.delete": '{"deleted": true}',
    })
    result = calendar_blocks.delete_focus_block(FOCUS_CAL, "evt_1", approved=True, runner=runner)
    assert result["ok"] is True
    assert runner.made("calendar.delete")


# --- T5: slip recovery is an UPDATE, never delete+create --------------------

def test_t5_slip_recovery_uses_update_not_delete_create():
    """An agent-created block slides via gog calendar UPDATE -- never delete+create."""
    runner = RecordingRunner({
        "calendar.event": _agent_event_json(),
        "calendar.freebusy": _freebusy_json([]),  # new window is free
        "calendar.update": '{"id": "evt_1"}',
    })
    result = calendar_blocks.move_focus_block(
        FOCUS_CAL, "evt_1", "tsk_abc", "2026-06-20T14:00:00+00:00", "2026-06-20T16:00:00+00:00",
        freebusy_calendar_ids=[FOCUS_CAL], runner=runner)
    assert result["event_id"] == "evt_1"  # SAME id -> reversible
    assert runner.made("calendar.update")
    assert not runner.made("calendar.delete")
    assert not runner.made("calendar.create")


def test_slip_into_busy_window_refused():
    """Even during recovery, a busy EXTERNAL window refuses the move (no update)."""
    runner = RecordingRunner({
        "calendar.event": _agent_event_json(),
        "calendar.freebusy": _freebusy_json([
            {"start": "2026-06-20T14:00:00+00:00", "end": "2026-06-20T15:00:00+00:00"},
        ]),
    })
    with pytest.raises(calendar_blocks.OverbookError) as exc:
        calendar_blocks.move_focus_block(
            FOCUS_CAL, "evt_1", "tsk_abc", "2026-06-20T14:00:00+00:00", "2026-06-20T16:00:00+00:00",
            freebusy_calendar_ids=[EXTERNAL_CAL], runner=runner)
    assert exc.value.reason == "freebusy_overlap"
    assert not runner.made("calendar.update")


def test_freebusy_per_calendar_error_treated_as_busy():
    """autoreview P2: a per-calendar error (inaccessible calendar) is UNKNOWN ->
    busy -> refuse, never treated as free."""
    import json
    runner = RecordingRunner({
        "calendar.freebusy": json.dumps({"calendars": {
            EXTERNAL_CAL: {"errors": [{"reason": "notFound"}]},  # inaccessible
        }}),
    })
    with pytest.raises(calendar_blocks.OverbookError) as exc:
        calendar_blocks.create_focus_block(
            FOCUS_CAL, "tsk_abc", "Draft", BLOCK_START, BLOCK_END,
            freebusy_calendar_ids=[EXTERNAL_CAL], runner=runner)
    assert exc.value.reason == "freebusy_unknown"


def test_freebusy_missing_requested_calendar_treated_as_busy():
    """A requested calendar absent from the response is UNKNOWN -> busy -> refuse."""
    import json
    runner = RecordingRunner({
        "calendar.freebusy": json.dumps({"calendars": {}}),  # requested cal not returned
    })
    with pytest.raises(calendar_blocks.OverbookError) as exc:
        calendar_blocks.create_focus_block(
            FOCUS_CAL, "tsk_abc", "Draft", BLOCK_START, BLOCK_END,
            freebusy_calendar_ids=[EXTERNAL_CAL], runner=runner)
    assert exc.value.reason == "freebusy_unknown"


# --- external_calendar_ids env assembly (focus calendar is EXCLUDED) --------

def test_external_calendar_ids_excludes_focus_calendar(monkeypatch):
    """The agent's own focus calendar is excluded from the freebusy gate so a MOVE
    does not self-overlap; only external (human) calendars are checked."""
    monkeypatch.setenv("TASK_TRACKER_FOCUS_CALENDAR_ID", "focus-cal")
    monkeypatch.setenv("STANDUP_CALENDARS",
                       '{"a": {"calendar_id": "primary"}, "b": {"calendar_id": "focus-cal"}}')
    ids = calendar_blocks.external_calendar_ids()
    assert ids == ["primary"]  # focus-cal dropped even though listed in STANDUP_CALENDARS


def test_external_calendar_ids_empty_when_unset(monkeypatch):
    monkeypatch.delenv("TASK_TRACKER_FOCUS_CALENDAR_ID", raising=False)
    monkeypatch.delenv("STANDUP_CALENDARS", raising=False)
    assert calendar_blocks.external_calendar_ids() == []
