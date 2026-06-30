"""U8 consolidated morning standup -- behavioral invariant tests.

These assert the U8 invariants, not the implementation path:

* OPENS WITH TOMORROW'S #1 (the load-bearing piece): the standup's FIRST content line
  resolves the U6 tomorrow-pointer against the LIVE board and shows it as today's #1.
* DEGRADES CLEANLY, NEVER CRASHES: no pointer / an explicit "none" pointer -> "no #1 set
  -- pick one"; a since-completed (off-board) pointer -> "pick a fresh one". A standup
  must never blow up because the pointer is missing or stale.
* DETERMINISTIC COMMAND CRON: the 8am cron descriptor is ``payload.kind == "command"``
  running ``telegram-commands.sh daily`` and announcing to the Productivity STANDUP
  thread -- asserted on the descriptor JSON (CODE-ONLY; no live ``openclaw cron add``).
* PARITY (KTD-5): the ``daily`` standup output still covers the board / priorities /
  blockers the legacy Lobster standup surfaced -- the fields the user relies on (the
  ledger supersedes the legacy Obsidian audit blocks, which are NOT reproduced).

Public-repo hygiene: the only chat id here is the FAKE ``-4242424242`` (does NOT match
the ``-100[0-9]{8,}`` pattern the CI hygiene grep flags). Task ids are fake ``tsk_*``.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import standup
import tomorrow_pointer
import utils

# Fake id: valid chat-id shape but not -100xxxxxxxx, so the hygiene grep is clean.
PRODUCTIVITY = "-4242424242"

WORK_BOARD = """# Work

## 🔴 Q1
- [ ] **Re-evaluate ActiveCampaign** task_id::tsk_top0001 area:: Ops
- [ ] **Ship the EOD ritual** task_id::tsk_q1other area:: Dev priority:: high

## 🟡 Q2
- [ ] **Draft the roadmap** task_id::tsk_q2task area:: Product

## 🟠 Waiting
- [ ] **Vendor reply** task_id::tsk_block01 blocks:: Launch area:: Ops
"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolate the board + the pointer state dir under tmp_path."""
    state_dir = tmp_path / "state"
    work = tmp_path / "Work Tasks.md"
    work.write_text(WORK_BOARD)

    monkeypatch.setenv("TASK_MGMT_STATE_DIR", str(state_dir))
    monkeypatch.setenv("TASK_TRACKER_WORK_FILE", str(work))
    # OBSIDIAN_WORK is resolved at import time; point load_records at the tmp board.
    monkeypatch.setattr(utils, "OBSIDIAN_WORK", work)
    # No calendars: get_calendar_events returns {} (not configured), never an error.
    monkeypatch.delenv("STANDUP_CALENDARS", raising=False)
    return {"work": work, "state_dir": state_dir}


def _records(env):
    from task_records import load_records

    _, _, records = load_records(personal=False)
    return records


# --- the opening line: tomorrow's #1, resolved against the live board ----------


def test_opening_line_shows_live_pointer_as_todays_number_one(env):
    """With a pointer to a still-active task, the standup opens with it as today's #1
    (using the LIVE board title), the read side of the daily loop."""
    tomorrow_pointer.set_top("tsk_top0001", "Re-evaluate ActiveCampaign")

    line = standup.tomorrow_pointer_line(records=_records(env))

    assert "Today's #1" in line
    assert "Re-evaluate ActiveCampaign" in line
    # Not the degrade messages.
    assert "pick one" not in line
    assert "pick a fresh" not in line


def test_opening_line_uses_the_live_board_title_not_the_stored_one(env):
    """A pointer stamped with a stale title resolves to the CURRENT board title, so a
    since-renamed task shows correctly."""
    tomorrow_pointer.set_top("tsk_top0001", "OLD STALE TITLE")

    line = standup.tomorrow_pointer_line(records=_records(env))

    assert "Re-evaluate ActiveCampaign" in line
    assert "OLD STALE TITLE" not in line


def test_no_pointer_degrades_to_pick_one(env):
    """No pointer file (the EOD never ran) -> 'no #1 set -- pick one', never a crash."""
    line = standup.tomorrow_pointer_line(records=_records(env))
    assert "No #1 set" in line
    assert "pick one" in line


def test_explicit_none_pointer_degrades_to_pick_one(env):
    """An explicit 'none' pointer (EOD ran on an empty board) -> the clean 'pick one'
    prompt, NOT a stale resurfaced #1."""
    tomorrow_pointer.set_none()
    line = standup.tomorrow_pointer_line(records=_records(env))
    assert "No #1 set" in line


def test_since_completed_pointer_degrades_to_pick_a_fresh_one(env):
    """A pointer to a task no longer on the active board (done/dropped/rescheduled off)
    degrades to 'pick a fresh one' -- NEVER resurfaces a dead #1."""
    tomorrow_pointer.set_top("tsk_gone999", "A task that left the board")

    line = standup.tomorrow_pointer_line(records=_records(env))

    assert "pick a fresh one" in line
    # The dead task title is never shown as today's #1.
    assert "A task that left the board" not in line


def test_pointer_line_never_raises_on_a_broken_board(env, monkeypatch):
    """A board read/parse failure degrades the opening line, never crashes the standup."""
    def boom(*_a, **_k):
        raise RuntimeError("board unreadable")

    # Force load_records (the records=None path inside the line builder) to blow up.
    import task_records

    monkeypatch.setattr(task_records, "load_records", boom)
    line = standup.tomorrow_pointer_line(records=None)
    assert "No #1 set" in line  # degraded, not raised


# --- the standup's FIRST content line is the pointer line ----------------------


def test_generate_standup_first_content_line_is_the_pointer(env):
    """The single-message ``daily`` standup OPENS with the pointer line right after the
    header -- it is the first content line, the read side of the daily loop."""
    tomorrow_pointer.set_top("tsk_top0001", "Re-evaluate ActiveCampaign")

    md = standup.generate_standup(json_output=False)
    lines = [ln for ln in md.splitlines() if ln.strip()]

    # First non-blank line is the standup header, the second is the #1 pointer line.
    assert lines[0].startswith("📋 **Daily Standup")
    assert "Today's #1" in lines[1]
    assert "Re-evaluate ActiveCampaign" in lines[1]


def test_generate_standup_renders_only_one_number_one_when_pointer_is_set(env):
    """A live tomorrow-pointer is the single coherent #1; Q1 escalation must not add
    a second '#1 Priority' row or a contradictory pick-one prompt."""
    tomorrow_pointer.set_top("tsk_top0001", "Re-evaluate ActiveCampaign")

    md = standup.generate_standup(json_output=False)

    assert md.count("Today's #1") == 1
    assert "Re-evaluate ActiveCampaign" in md
    assert "No #1 set" not in md
    assert "#1 Priority" not in md


def test_generate_standup_without_pointer_prompts_once_and_does_not_invent_number_one(env):
    md = standup.generate_standup(json_output=False)

    assert md.count("No #1 set") == 1
    assert "#1 Priority" not in md
    assert "Today's #1" not in md


def test_generate_standup_json_carries_the_pointer_line(env):
    """The JSON payload carries ``tomorrow_pointer_line`` so automation clients see the
    same #1 the markdown opens with."""
    tomorrow_pointer.set_top("tsk_top0001", "Re-evaluate ActiveCampaign")

    payload = standup.generate_standup(json_output=True)
    assert "Today's #1" in payload["tomorrow_pointer_line"]
    assert "Re-evaluate ActiveCampaign" in payload["tomorrow_pointer_line"]


def test_standup_proposes_at_most_three_daily_top_priorities_not_full_q1(env, monkeypatch):
    env["work"].write_text("""# Work

## 🔴 Q1
- [ ] **First Q1** task_id::tsk_first area:: Ops
- [ ] **Second Q1** task_id::tsk_second area:: Ops
- [ ] **Third Q1** task_id::tsk_third area:: Ops
- [ ] **Fourth Q1** task_id::tsk_fourth area:: Ops
- [ ] **Fifth Q1** task_id::tsk_fifth area:: Ops
""")
    monkeypatch.setattr(
        standup,
        "_standup_harvest_result",
        lambda date_str, *, trigger, resolved_window=None: {
            "evidence_candidates": [],
            "health": {},
            "window": resolved_window.as_dict() if resolved_window else None,
        },
    )

    md = standup.generate_standup(json_output=False)

    assert "Daily Top Priorities" in md
    priority_rows = [line for line in md.splitlines() if line.strip().startswith(("1. ", "2. ", "3. "))]
    assert len(priority_rows) <= 3
    assert "First Q1" in md and "Second Q1" in md and "Third Q1" in md
    assert "Fourth Q1" not in md
    assert "Fifth Q1" not in md
    assert "Urgent & Important (Q1)" not in md


# --- the deterministic command cron descriptor (CODE-ONLY shape) ---------------


def test_standup_cron_descriptor_is_a_command_cron_to_the_standup_thread():
    """The 8am standup cron descriptor is a DETERMINISTIC command cron
    (payload.kind == 'command') that runs ``telegram-commands.sh daily`` and announces to
    the Productivity STANDUP thread -- asserted on the descriptor JSON (CODE-ONLY; no
    live openclaw cron add)."""
    desc = standup.standup_cron_descriptor()

    # Deterministic command cron -- NOT an LLM agentTurn.
    assert desc["payload"]["kind"] == "command"
    argv = desc["payload"]["argv"]
    assert argv[0] == "sh" and argv[1] == "-c"
    assert "telegram-commands.sh daily" in argv[2]
    # The 8am hour, announce delivery to the Productivity STANDUP thread (env-var NAMES).
    assert desc["schedule"]["hour"] == 8
    assert desc["delivery"]["mode"] == "announce"
    assert desc["delivery"]["chat_id_env"] == "TELEGRAM_CHAT_ID_PRODUCTIVITY"
    assert desc["delivery"]["topic_env"] == "OPENCLAW_TOPIC_PRODUCTIVITY_STANDUP"
    # No real chat id is baked into the descriptor -- only env-var names.
    assert "-100" not in json.dumps(desc)


def test_standup_cron_descriptor_carries_no_committed_chat_id():
    """The descriptor must embed env-var NAMES, never a literal -100xxxxxxxx chat id
    (public-repo hygiene): a serialised descriptor carries no production id."""
    serialised = json.dumps(standup.standup_cron_descriptor())
    assert "TELEGRAM_CHAT_ID_PRODUCTIVITY" in serialised
    assert PRODUCTIVITY not in serialised  # not even the fake test id is hardcoded
    assert "-100" not in serialised


# --- parity (KTD-5): daily covers board / priorities / blockers ----------------


def test_daily_output_covers_board_priorities_and_blockers(env):
    """Characterization (KTD-5): the ``daily`` standup the deterministic cron runs covers
    the fields the legacy Lobster standup surfaced -- the #1 priority, the Q1/Q2 board
    sections, and the blocked/waiting items -- so retiring the Lobster lane loses nothing
    the user relies on. The ledger supersedes the legacy Obsidian audit blocks, which are
    intentionally NOT reproduced here."""
    tomorrow_pointer.set_top("tsk_top0001", "Re-evaluate ActiveCampaign")

    payload = standup.generate_standup(json_output=True)

    # The #1 priority is surfaced (the standup picks the day's lead item).
    assert payload["priority"] is not None
    assert payload["priority"]["title"]
    # The board's quadrant sections are surfaced (priorities the user works from).
    q1_titles = {t["title"] for t in payload["q1"]}
    q2_titles = {t["title"] for t in payload["q2"]}
    assert "Ship the EOD ritual" in q1_titles or "Re-evaluate ActiveCampaign" in q1_titles
    assert "Draft the roadmap" in q2_titles
    # Blocked / waiting items are surfaced with their blocker (the Q3 lane).
    q3 = payload["q3"]
    assert any(t["title"] == "Vendor reply" for t in q3)
    assert any(t.get("blocks") for t in q3)
    # And the standup opens with tomorrow's #1 (the read side of the loop).
    assert "Re-evaluate ActiveCampaign" in payload["tomorrow_pointer_line"]
