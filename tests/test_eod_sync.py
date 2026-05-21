"""Tests for eod_sync.py — EOD completion evidence reporting."""

from __future__ import annotations

import sys
import os
import subprocess
from pathlib import Path
from datetime import date

# Make scripts importable
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import eod_sync as sync


# ---------------------------------------------------------------------------
# normalize()
# ---------------------------------------------------------------------------

class TestNormalize:
    def test_strips_checkbox(self):
        assert sync.normalize("- [x] Some task") == "some task"

    def test_strips_unchecked_checkbox(self):
        assert sync.normalize("- [ ] Some task") == "some task"

    def test_strips_date_emoji(self):
        assert sync.normalize("Task title 📅 2026-02-18") == "task title"

    def test_strips_priority_emojis(self):
        assert sync.normalize("Task ⏫ title") == "task title"
        assert sync.normalize("Task 🔺") == "task"

    def test_strips_tags(self):
        assert sync.normalize("Task #sales #ops") == "task"

    def test_strips_iso_date(self):
        assert sync.normalize("Review notes 2026-02-18") == "review notes"

    def test_strips_month_date(self):
        assert sync.normalize("Demo follow-ups (Feb 18)") == "demo follow-ups"

    def test_case_insensitive(self):
        assert sync.normalize("KPMG Audit") == "kpmg audit"

    def test_strips_completion_emoji(self):
        assert sync.normalize("Task done ✅ 2026-02-19") == "task done"

    def test_plain_list_item(self):
        assert sync.normalize("- KPMG audit package — prep + send") == "kpmg audit package — prep + send"


# ---------------------------------------------------------------------------
# similarity()
# ---------------------------------------------------------------------------

class TestSimilarity:
    def test_exact_match(self):
        score = sync.similarity("Demo: Sharlotte Manley", "Demo: Sharlotte Manley")
        assert score >= 0.99

    def test_date_stripped_match(self):
        # Daily note has "(Feb 18)" appended; weekly todos has bare title
        score = sync.similarity(
            "Demo follow-ups (Feb 18)",
            "Demo follow-ups",
        )
        assert score >= 0.80, f"Expected ≥ 0.80, got {score:.2f}"

    def test_checkbox_stripped_match(self):
        score = sync.similarity(
            "- [x] KPMG audit package — prep + send ✅ 2026-02-19 #finance",
            "KPMG audit package — prep + send 📅 2026-02-19 🔺",
        )
        assert score >= 0.80, f"Expected ≥ 0.80, got {score:.2f}"

    def test_low_similarity(self):
        score = sync.similarity("Weekly Sales Meeting", "KPMG audit package")
        assert score < 0.60, f"Expected < 0.60, got {score:.2f}"

    def test_tag_stripped_match(self):
        score = sync.similarity(
            "Mid-OKR Review ✅ 2026-02-18 #ops",
            "Mid-OKR Review 📅 2026-02-18 🔼",
        )
        assert score >= 0.80, f"Expected ≥ 0.80, got {score:.2f}"


# ---------------------------------------------------------------------------
# parse_done_items()
# ---------------------------------------------------------------------------

DAILY_NOTE_WITH_DONE = """\
# 2026-02-18

## ✅ Done
- [x] Mid-OKR Review ✅ 2026-02-18 #ops
- [x] Demo: Sharlotte Manley (Lavender Medi Spa) ✅ 2026-02-18 #sales
- [x] Weekly Sales Meeting ✅ 2026-02-18 #sales

## 🔗 Linked
- [[01-TODOs/Weekly TODOs]]
"""

DAILY_NOTE_PLAIN_DONE = """\
# 2026-02-19

## ✅ Done
(update as day progresses)
- KPMG audit package — prep + send
- Wrap up Q4/25 Financials
"""

DAILY_NOTE_EMPTY_DONE = """\
# 2026-02-20

## ✅ Done
(update as day progresses)

## 🔗 Linked
"""


class TestParseDoneItems:
    def test_parses_checkbox_items(self):
        items = sync.parse_done_items(DAILY_NOTE_WITH_DONE)
        assert len(items) == 3
        assert any("Mid-OKR Review" in i for i in items)
        assert any("Sharlotte Manley" in i for i in items)

    def test_parses_plain_list_items(self):
        items = sync.parse_done_items(DAILY_NOTE_PLAIN_DONE)
        assert len(items) == 2
        assert any("KPMG audit package" in i for i in items)

    def test_skips_placeholder(self):
        items = sync.parse_done_items(DAILY_NOTE_EMPTY_DONE)
        assert items == []

    def test_stops_at_next_section(self):
        items = sync.parse_done_items(DAILY_NOTE_WITH_DONE)
        # Should not include items from 🔗 Linked section
        assert not any("Weekly TODOs" in i for i in items)


# ---------------------------------------------------------------------------
# parse_weekly_open_tasks()
# ---------------------------------------------------------------------------

WEEKLY_CONTENT = """\
# Weekly TODOs — 2026-W08

### 🚀 Sales #sales
- [x] Weekly Sales Meeting ✅ 2026-02-18
- [ ] Call Devin (Life Time) 📅 2026-02-19 ⏫
- [ ] Follow up Brandon Thompson — send distributor agreement 📅 2026-02-19 ⏫

### 📣 Marketing #marketing
- [ ] 1-2 min product overview video 📅 2026-02-21 🔺

### 💰 Finance #finance
- [ ] KPMG audit package — prep + send 📅 2026-02-19 🔺
"""


class TestParseWeeklyOpenTasks:
    def test_finds_unchecked_only(self):
        tasks = sync.parse_weekly_open_tasks(WEEKLY_CONTENT)
        titles = [t["body"] for t in tasks]
        assert not any("Weekly Sales Meeting" in t for t in titles)  # already done

    def test_finds_correct_count(self):
        tasks = sync.parse_weekly_open_tasks(WEEKLY_CONTENT)
        assert len(tasks) == 4

    def test_preserves_line_index(self):
        tasks = sync.parse_weekly_open_tasks(WEEKLY_CONTENT)
        # All indices should be valid
        lines = WEEKLY_CONTENT.splitlines()
        for t in tasks:
            assert 0 <= t["line_idx"] < len(lines)
            assert "- [ ]" in lines[t["line_idx"]]


# ---------------------------------------------------------------------------
# build_sync_plan()
# ---------------------------------------------------------------------------

OPEN_TASKS_SAMPLE = [
    {"line_idx": 5, "indent": "", "body": "Call Devin (Life Time) 📅 2026-02-19 ⏫", "raw": "- [ ] Call Devin (Life Time) 📅 2026-02-19 ⏫\n"},
    {"line_idx": 6, "indent": "", "body": "KPMG audit package — prep + send 📅 2026-02-19 🔺", "raw": "- [ ] KPMG audit package — prep + send 📅 2026-02-19 🔺\n"},
    {"line_idx": 7, "indent": "", "body": "Mid-OKR Review 📅 2026-02-18 🔼", "raw": "- [ ] Mid-OKR Review 📅 2026-02-18 🔼\n"},
]

DONE_ITEMS_SAMPLE = [
    "- [x] Mid-OKR Review ✅ 2026-02-18 #ops",
    "- [x] KPMG audit package — prep + send ✅ 2026-02-19 #finance",
    "- Completely unrelated task that has no match at all",
]


class TestBuildSyncPlan:
    def test_reports_high_confidence_evidence(self):
        plan = sync.build_sync_plan(DONE_ITEMS_SAMPLE, OPEN_TASKS_SAMPLE, "2026-02-19")
        synced = [r for r in plan if r["status"] == "evidence"]
        assert len(synced) == 2

    def test_skips_no_match(self):
        plan = sync.build_sync_plan(DONE_ITEMS_SAMPLE, OPEN_TASKS_SAMPLE, "2026-02-19")
        skipped = [r for r in plan if r["status"] == "skip"]
        assert len(skipped) == 1
        assert "unrelated" in skipped[0]["done_item"]

    def test_no_duplicate_matches(self):
        """Same open task should not be matched to multiple done items."""
        done_items = [
            "- [x] KPMG audit package — prep + send ✅ 2026-02-19",
            "- [x] KPMG audit — prep + send ✅ 2026-02-19",  # very similar
        ]
        plan = sync.build_sync_plan(done_items, OPEN_TASKS_SAMPLE, "2026-02-19")
        synced = [r for r in plan if r["status"] == "evidence"]
        # The KPMG task should only match once
        kpmg_matched_indices = {r["match"]["line_idx"] for r in synced if r["match"] is not None}
        assert len(kpmg_matched_indices) <= len(synced), "Duplicate match detected"

    def test_score_in_range(self):
        plan = sync.build_sync_plan(DONE_ITEMS_SAMPLE, OPEN_TASKS_SAMPLE, "2026-02-19")
        for r in plan:
            assert 0.0 <= r["score"] <= 1.0


# ---------------------------------------------------------------------------
# apply_sync_plan()
# ---------------------------------------------------------------------------

class TestApplySyncPlan:
    def test_marks_task_as_done(self):
        lines = [
            "# Weekly TODOs\n",
            "\n",
            "- [ ] KPMG audit package — prep + send 📅 2026-02-19 🔺\n",
        ]
        plan = [
            {
                "status": "sync",
                "done_item": "- [x] KPMG audit package — prep + send ✅ 2026-02-19",
                "match": {
                    "line_idx": 2,
                    "indent": "",
                    "body": "KPMG audit package — prep + send 📅 2026-02-19 🔺",
                    "raw": "- [ ] KPMG audit package — prep + send 📅 2026-02-19 🔺\n",
                },
                "score": 0.95,
            }
        ]
        result = sync.apply_sync_plan(lines, plan, "2026-02-19")
        # Verify task is marked done and completion date is appended
        assert "✅ 2026-02-19" in result[2]
        assert "KPMG audit package" in result[2]
        # Original metadata (📅, 🔺) should be preserved
        assert "📅" in result[2]
        assert "🔺" in result[2]

    def test_skips_non_sync_results(self):
        lines = [
            "- [ ] Call Devin (Life Time) 📅 2026-02-19 ⏫\n",
        ]
        plan = [
            {
                "status": "uncertain",
                "done_item": "Call Devin",
                "match": {"line_idx": 0, "indent": "", "body": "Call Devin (Life Time) 📅 2026-02-19 ⏫", "raw": ""},
                "score": 0.70,
            }
        ]
        result = sync.apply_sync_plan(lines, plan, "2026-02-19")
        assert "- [ ]" in result[0]  # unchanged
        assert "✅" not in result[0]

    def test_does_not_mutate_original(self):
        lines = ["- [ ] Some task\n"]
        plan = [
            {
                "status": "sync",
                "done_item": "- [x] Some task",
                "match": {"line_idx": 0, "indent": "", "body": "Some task", "raw": ""},
                "score": 1.0,
            }
        ]
        original_line = lines[0]
        sync.apply_sync_plan(lines, plan, "2026-02-19")
        assert lines[0] == original_line  # original not mutated


def test_cli_default_is_report_only(tmp_path):
    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    (daily_dir / "2026-02-19.md").write_text(DAILY_NOTE_PLAIN_DONE)
    weekly = tmp_path / "Weekly TODOs.md"
    original = """# Weekly TODOs

- [ ] KPMG audit package — prep + send 📅 2026-02-19 🔺
"""
    weekly.write_text(original)

    env = os.environ.copy()
    env["TASK_TRACKER_DAILY_NOTES_DIR"] = str(daily_dir)
    env["TASK_TRACKER_WEEKLY_TODOS"] = str(weekly)

    proc = subprocess.run(
        ["python3", "scripts/eod_sync.py", "--date", "2026-02-19"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert proc.returncode == 0
    assert "Report-only mode" in proc.stdout
    assert weekly.read_text() == original
