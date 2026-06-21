"""U6 proactive-state: torn-read-safe idempotency for briefs + debriefs.

Asserts the spec §3.2 / mustFix #7 contract:

* a torn / corrupt / unreadable state file re-inits from empty (quarantined
  aside, never erased) so the NEXT */5 fire always runs and never double-briefs;
* a stale-date state resets (yesterday's "already sent" never suppresses today);
* a pre-brief is idempotent per event per day;
* a debrief loop closes ONLY on capture or skip -- never by time.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import cos_config  # noqa: E402
import proactive_state  # noqa: E402


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_MGMT_STATE_DIR", str(tmp_path / "state"))
    return cos_config.state_dir()


def test_daily_brief_idempotent(state_dir):
    state = proactive_state.load_proactive_state()
    assert proactive_state.daily_brief_due(state) is True
    proactive_state.mark_daily_brief_sent(state)
    proactive_state.save_proactive_state(state)
    reloaded = proactive_state.load_proactive_state()
    assert proactive_state.daily_brief_due(reloaded) is False


def test_pre_brief_idempotent_per_event(state_dir):
    state = proactive_state.load_proactive_state()
    assert proactive_state.pre_brief_due(state, "evt_1") is True
    proactive_state.mark_pre_brief_sent(state, "evt_1", "Q3 Review", "2026-06-20T10:00:00+00:00")
    proactive_state.save_proactive_state(state)
    reloaded = proactive_state.load_proactive_state()
    assert proactive_state.pre_brief_due(reloaded, "evt_1") is False
    # a DIFFERENT event is still due
    assert proactive_state.pre_brief_due(reloaded, "evt_2") is True


def test_torn_read_reinits_and_quarantines(state_dir):
    """A corrupt state file is quarantined aside and treated as fresh empty state."""
    path = proactive_state.proactive_state_path()
    path.write_text("{ this is not valid json", encoding="utf-8")
    state = proactive_state.load_proactive_state()  # must NOT raise
    assert proactive_state.daily_brief_due(state) is True  # fresh state
    # the bad bytes are preserved aside, never erased
    quarantined = list(path.parent.glob(f"{path.name}.corrupt-*"))
    assert len(quarantined) == 1


def test_unreadable_state_not_clobbered(state_dir, monkeypatch):
    """A present-but-unreadable file returns empty state WITHOUT being clobbered."""
    path = proactive_state.proactive_state_path()
    path.write_text(json.dumps({"date": proactive_state.today_str(), "daily_brief_sent": True}),
                    encoding="utf-8")
    original = path.read_text()

    def boom(*_a, **_k):
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "read_text", boom)
    state = proactive_state.load_proactive_state()
    assert proactive_state.daily_brief_due(state) is True  # fresh in memory
    monkeypatch.undo()
    assert path.read_text() == original  # file untouched


def test_open_debrief_loop_carries_across_date_rollover(state_dir):
    """autoreview P3: a debrief closes ONLY on capture/skip, never by time -- so an
    open loop from yesterday must migrate into today's state, not be dropped at
    midnight. A CLOSED loop is not carried."""
    path = proactive_state.proactive_state_path()
    path.write_text(json.dumps({
        "schema_version": 1, "date": "2020-01-01",
        "daily_brief_sent": True, "friday_proposal_sent": True,
        "pre_briefs": [
            {"event_id": "open_evt", "event_summary": "Open", "debrief_requested": True,
             "debrief_captured_at": None, "debrief_skipped_at": None},
            {"event_id": "closed_evt", "event_summary": "Closed", "debrief_requested": True,
             "debrief_captured_at": "2020-01-01T12:00:00+00:00", "debrief_skipped_at": None},
        ],
    }), encoding="utf-8")
    state = proactive_state.load_proactive_state()  # today
    # idempotency flags reset for the new day
    assert proactive_state.daily_brief_due(state) is True
    # the OPEN loop survives; the CLOSED one does not
    keys = [e["event_id"] for e in state["pre_briefs"]]
    assert keys == ["open_evt"]
    assert proactive_state.is_debrief_open(state["pre_briefs"][0]) is True


def test_stale_date_resets(state_dir):
    """A state from a prior date never suppresses today's briefs."""
    path = proactive_state.proactive_state_path()
    path.write_text(json.dumps({
        "schema_version": 1, "date": "2020-01-01",
        "daily_brief_sent": True, "friday_proposal_sent": True, "pre_briefs": [],
    }), encoding="utf-8")
    state = proactive_state.load_proactive_state()  # today
    assert proactive_state.daily_brief_due(state) is True
    assert proactive_state.friday_proposal_due(state) is True


def test_debrief_closes_only_on_capture_or_skip(state_dir):
    """A debrief loop stays OPEN until capture/skip -- never closed by time."""
    state = proactive_state.load_proactive_state()
    proactive_state.mark_pre_brief_sent(state, "evt_1", "Q3", "2026-06-20T10:00:00+00:00")
    entry = proactive_state.open_debrief(state, "evt_1")
    assert proactive_state.is_debrief_open(entry) is True

    # capturing closes it
    proactive_state.capture_debrief(state, "evt_1", ["tsk_new1"])
    assert proactive_state.is_debrief_open(state["pre_briefs"][0]) is False
    assert state["pre_briefs"][0]["commitments_task_ids"] == ["tsk_new1"]


def test_debrief_skip_closes(state_dir):
    state = proactive_state.load_proactive_state()
    proactive_state.mark_pre_brief_sent(state, "evt_1", "Q3", "2026-06-20T10:00:00+00:00")
    proactive_state.open_debrief(state, "evt_1")
    proactive_state.skip_debrief(state, "evt_1")
    assert proactive_state.is_debrief_open(state["pre_briefs"][0]) is False


def test_open_debrief_missing_event_returns_none(state_dir):
    state = proactive_state.load_proactive_state()
    assert proactive_state.open_debrief(state, "no_such_event") is None


def test_debrief_reprompt_due_tolerates_naive_timestamp(state_dir):
    """autoreview P3: a hand-edited NAIVE last-reprompt timestamp must not raise
    TypeError against the tz-aware now -- it degrades gracefully."""
    from datetime import datetime, timezone
    now = datetime(2026, 6, 20, 12, 0, tzinfo=timezone.utc)
    # naive timestamp 30 min ago -> within the 120-min interval -> NOT due, no crash
    entry = {"debrief_last_reprompt_at": "2026-06-20T11:30:00"}  # no tzinfo
    assert proactive_state.debrief_reprompt_due(entry, now=now, interval_minutes=120) is False
    # naive timestamp 3h ago -> past interval -> due
    entry2 = {"debrief_last_reprompt_at": "2026-06-20T09:00:00"}
    assert proactive_state.debrief_reprompt_due(entry2, now=now, interval_minutes=120) is True


def test_transition_serializes_no_lost_update(state_dir):
    """The locked transition does read-modify-write under flock, so two sequential
    transitions accumulate -- the second never clobbers the first's update (the
    lost-update the */5 cron flock prevents)."""
    proactive_state.transition(lambda s: proactive_state.mark_daily_brief_sent(s))
    # a second transition mutating a DIFFERENT flag must preserve the first's
    proactive_state.transition(lambda s: proactive_state.mark_friday_proposal_sent(s))
    reloaded = proactive_state.load_proactive_state()
    assert proactive_state.daily_brief_due(reloaded) is False  # first survived
    assert proactive_state.friday_proposal_due(reloaded) is False  # second applied
