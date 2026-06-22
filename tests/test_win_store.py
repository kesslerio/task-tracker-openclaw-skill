"""H8 manual-win store (win_store.py) -- unit invariants.

Pins the store's own contracts:
- append_win persists a flocked, line-atomic JSON record (one win per line);
- read_wins is fail-soft (missing file -> [], a torn/corrupt line is skipped, never raised);
- the `since` filter drops wins captured before the window start;
- classify_bucket routes a decision/hire phrase to `decisions`, an explicit tag to its
  bucket, and everything else to `advanced` (the harvest's two blind-spot buckets);
- concurrent appends survive the flock (no lost/torn lines).

Fake ids only; no real openclaw.
"""

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import cos_config  # noqa: E402
import win_store  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_MGMT_STATE_DIR", str(tmp_path / "state"))
    yield


# --- classify_bucket --------------------------------------------------------


@pytest.mark.parametrize("text,expected", [
    ("decided to hire a CFO", "decisions"),
    ("chose vendor B over A", "decisions"),
    ("approved the Q3 budget", "decisions"),
    ("pushed the partnership forward", "advanced"),
    ("had a great call with the team", "advanced"),
])
def test_classify_bare_text(text, expected):
    bucket, cleaned = win_store.classify_bucket(text)
    assert bucket == expected
    assert cleaned == text


@pytest.mark.parametrize("text,bucket,cleaned", [
    ("shipped: cut the v2 release", "shipped", "cut the v2 release"),
    ("maintenance: cleared the inbox backlog", "maintenance", "cleared the inbox backlog"),
    ("decision: pivot the roadmap", "decisions", "pivot the roadmap"),
    ("advanced - moved the deal to contract", "advanced", "moved the deal to contract"),
])
def test_classify_explicit_tag_is_stripped(text, bucket, cleaned):
    assert win_store.classify_bucket(text) == (bucket, cleaned)


def test_decision_substring_does_not_false_trigger():
    """'undecided' / 'advanced' must not trip the decision classifier (whole-word)."""
    bucket, _ = win_store.classify_bucket("still undecided on the office lease")
    assert bucket == "advanced"


# --- append_win / read_wins round-trip --------------------------------------


def test_append_then_read_roundtrip():
    win_store.append_win("decided on the pricing model")
    win_store.append_win("had a productive 1:1")
    wins = win_store.read_wins()
    assert [w["text"] for w in wins] == ["decided on the pricing model", "had a productive 1:1"]
    assert wins[0]["bucket"] == "decisions"
    assert wins[1]["bucket"] == "advanced"
    # Each record carries a UTC ts and a local captured_on date.
    assert wins[0]["ts"] and wins[0]["captured_on"]


def test_read_missing_file_is_empty():
    assert win_store.read_wins() == []


def test_read_skips_corrupt_line_without_raising():
    """One torn/corrupt line must not hide every other win from the digest."""
    win_store.append_win("good win one")
    # Inject a corrupt line between two good records.
    with win_store.wins_path().open("a", encoding="utf-8") as handle:
        handle.write("{ this is not valid json\n")
    win_store.append_win("good win two")
    texts = [w["text"] for w in win_store.read_wins()]
    assert texts == ["good win one", "good win two"]


def test_since_filter_drops_older_wins(monkeypatch):
    """A win captured before the window start is excluded by the `since` filter."""
    monkeypatch.setattr(cos_config, "local_today", lambda: __import__("datetime").date(2026, 6, 1))
    win_store.append_win("old win from June 1")
    monkeypatch.setattr(cos_config, "local_today", lambda: __import__("datetime").date(2026, 6, 15))
    win_store.append_win("new win from June 15")
    recent = win_store.read_wins(since="2026-06-10")
    assert [w["text"] for w in recent] == ["new win from June 15"]
    # No filter -> both.
    assert len(win_store.read_wins()) == 2


# --- seen-on-push dedup (mark_wins_seen / read_unseen_wins) -----------------


def test_unseen_excludes_marked_wins_regardless_of_date():
    """read_unseen_wins returns wins NOT yet delivered, unfiltered by capture date --
    so a delivered win never repeats and an undelivered old win still surfaces."""
    a = win_store.append_win("shipped the deck")
    b = win_store.append_win("closed the deal")
    assert {w["id"] for w in win_store.read_unseen_wins()} == {a["id"], b["id"]}

    win_store.mark_wins_seen([a["id"]])
    unseen = win_store.read_unseen_wins()
    assert [w["text"] for w in unseen] == ["closed the deal"]
    assert win_store.read_seen_win_ids() == {a["id"]}


def test_win_id_is_stable_across_reads():
    """The win id is derived from immutable capture facts, so a re-read resolves the
    same id (a delivered win's seen-mark keeps matching it)."""
    rec = win_store.append_win("decided to pivot")
    reread = win_store.read_wins()[0]
    assert reread["id"] == rec["id"] == win_store.win_id(rec)


def test_mark_wins_seen_is_idempotent():
    """Re-marking an already-seen id does not grow the seen file (no unbounded growth
    on a heartbeat re-fire)."""
    rec = win_store.append_win("hired a CFO")
    win_store.mark_wins_seen([rec["id"]])
    win_store.mark_wins_seen([rec["id"], rec["id"]])
    lines = win_store.seen_wins_path().read_text().splitlines()
    assert lines == [rec["id"]]


def test_read_seen_missing_file_is_empty_set():
    assert win_store.read_seen_win_ids() == set()


def test_concurrent_appends_do_not_lose_or_tear_lines():
    """The flocked append serialises concurrent writers -- every line is complete."""
    n = 30

    def write_one(i):
        win_store.append_win(f"win {i:03d}")

    with ThreadPoolExecutor(max_workers=n) as pool:
        list(pool.map(write_one, range(n)))

    # Every line parses (none torn) and all n wins are present.
    lines = win_store.wins_path().read_text().splitlines()
    assert len(lines) == n
    parsed = [json.loads(line)["text"] for line in lines]
    assert sorted(parsed) == sorted(f"win {i:03d}" for i in range(n))
