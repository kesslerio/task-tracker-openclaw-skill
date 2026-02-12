from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from tasks import _remove_task_line
from utils import detect_format, parse_tasks


OBJECTIVES_CONTENT = """## Objectives
- [ ] Launch job posting #HR #high
  - [x] Draft job description ✅ 2026-02-10
  - [x] Research platforms ✅ 2026-02-10
  - [ ] Post on LinkedIn

- [ ] Close Fizzi deal #Sales #urgent
  - [ ] Call re: pricing
  - [ ] Send follow-up proposal

## Today: Wednesday Feb 12
- [ ] Post on LinkedIn
- [ ] 1:1 meeting (standalone, no parent objective)

## 🅿️ Parking Lot
- [ ] Set up webhook integration #Dev #low
- [ ] Notion integration research #Dev #medium
"""


def test_detect_format_prefers_objectives_header():
    assert detect_format(OBJECTIVES_CONTENT) == "objectives"


def test_detect_format_handles_existing_obsidian_header():
    content = "## 🔴 Q1: Urgent & Important\n- [ ] **Ship release**"
    assert detect_format(content) == "obsidian"
    # Legacy hint is respected even when 🔴 is present (both formats use it)
    assert detect_format(content, fallback="legacy") == "legacy"


def test_parse_objectives_format_nested_tasks_and_tags():
    tasks = parse_tasks(OBJECTIVES_CONTENT)

    assert len(tasks["all"]) == 11
    assert len(tasks["done"]) == 2

    launch = next(t for t in tasks["all"] if t["title"] == "Launch job posting")
    assert launch["section"] == "objectives"
    assert launch["is_objective"] is True
    assert launch["parent_objective"] is None
    assert launch["department"] == "HR"
    assert launch["priority"] == "high"

    draft = next(t for t in tasks["all"] if t["title"] == "Draft job description")
    assert draft["is_objective"] is False
    assert draft["parent_objective"] == "Launch job posting"
    assert draft["completed_date"] == "2026-02-10"

    today_post = next(
        t for t in tasks["all"]
        if t["title"] == "Post on LinkedIn" and t["section"] == "today"
    )
    assert today_post["parent_objective"] is None
    assert today_post["is_objective"] is False

    parking = next(
        t for t in tasks["all"]
        if t["title"] == "Set up webhook integration"
    )
    assert parking["section"] == "parking_lot"
    assert parking["department"] == "Dev"
    assert parking["priority"] == "low"


def test_objectives_priority_mapping_populates_legacy_buckets():
    tasks = parse_tasks(OBJECTIVES_CONTENT)

    q1_titles = {t["title"] for t in tasks["q1"]}
    q2_titles = {t["title"] for t in tasks["q2"]}
    backlog_titles = {t["title"] for t in tasks["backlog"]}

    assert "Launch job posting" in q1_titles
    assert "Close Fizzi deal" in q1_titles
    assert "Notion integration research" in q2_titles
    assert "Set up webhook integration" in backlog_titles


def test_parse_objectives_autodetects_even_with_legacy_hint():
    tasks = parse_tasks(OBJECTIVES_CONTENT, format="legacy")
    assert any(t["section"] == "today" for t in tasks["all"])
    assert any(t["title"] == "Launch job posting" for t in tasks["q1"])


def test_backward_compat_q_sections_and_bold_remain_supported():
    content = """## 🔴 Q1: Urgent & Important
- [ ] **Ship proposal** 🗓️2026-02-20 area:: Sales owner:: me
- [x] **Call completed** ✅ 2026-02-10

## 🟡 Q2: Important, Not Urgent
- [ ] **Plan roadmap** area:: Product
"""

    tasks = parse_tasks(content, format="obsidian")

    ship = next(t for t in tasks["all"] if t["title"] == "Ship proposal")
    assert ship["section"] == "q1"
    assert ship["due"] == "2026-02-20"
    assert ship["area"] == "Sales"

    done_task = next(t for t in tasks["done"] if t["title"] == "Call completed")
    assert done_task["completed_date"] == "2026-02-10"

    for task in tasks["all"]:
        assert task["parent_objective"] is None
        assert task["is_objective"] is False
        assert task["department"] is None
        assert task["priority"] is None


def test_plain_and_bold_task_patterns_are_both_parsed():
    content = """## 🔴 Q1: Urgent & Important
- [ ] **Bold task** area:: Sales
- [ ] Plain task area:: Marketing
"""

    tasks = parse_tasks(content, format="obsidian")

    bold = next(t for t in tasks["all"] if t["title"] == "Bold task")
    plain = next(t for t in tasks["all"] if t["title"] == "Plain task")

    assert bold["area"] == "Sales"
    assert plain["area"] == "Marketing"


def test_remove_task_line_removes_parent_and_subtasks():
    content = """## Objectives
- [ ] Parent objective
  - [ ] Child one

  - [ ] Child two
- [ ] Sibling objective
"""

    updated = _remove_task_line(content, "- [ ] Parent objective")

    assert updated == """## Objectives
- [ ] Sibling objective
"""


def test_remove_task_line_preserves_sibling_tasks():
    content = """- [ ] Parent A
  - [ ] Child A1
- [ ] Parent B
  - [ ] Child B1
"""

    updated = _remove_task_line(content, "- [ ] Parent A")

    assert updated == """- [ ] Parent B
  - [ ] Child B1
"""


def test_remove_task_line_handles_flat_task():
    content = """- [ ] Task one
- [ ] Task two
"""

    updated = _remove_task_line(content, "- [ ] Task one")

    assert updated == """- [ ] Task two
"""
