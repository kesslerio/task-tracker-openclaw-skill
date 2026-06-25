"""v0.4-C holdout: a deterministic, stable ~25% control split keyed on the slot id."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import initiation_holdout as holdout  # noqa: E402


def _slots(n):
    return [f"work:tsk_{i:016x}:2026-06-24" for i in range(n)]


def test_assignment_is_deterministic_and_stable():
    slot = "work:tsk_aaaaaaaaaaaaaaaa:2026-06-24"
    assert holdout.arm_for(slot) == holdout.arm_for(slot)
    assert holdout.arm_for(slot) in (holdout.ARM_TREATMENT, holdout.ARM_CONTROL)


def test_split_is_roughly_25_percent_control():
    arms = [holdout.arm_for(s) for s in _slots(2000)]
    control = arms.count(holdout.ARM_CONTROL)
    assert 0.20 < control / len(arms) < 0.30  # ~25% with sampling slack


def test_distinct_slots_get_distinct_arms():
    arms = {holdout.arm_for(s) for s in _slots(50)}
    assert arms == {holdout.ARM_TREATMENT, holdout.ARM_CONTROL}


def test_pct_zero_disables_holdout(monkeypatch):
    monkeypatch.setenv("INITIATION_HOLDOUT_PCT", "0")
    assert all(holdout.arm_for(s) == holdout.ARM_TREATMENT for s in _slots(200))


def test_pct_hundred_is_all_control(monkeypatch):
    monkeypatch.setenv("INITIATION_HOLDOUT_PCT", "100")
    assert all(holdout.arm_for(s) == holdout.ARM_CONTROL for s in _slots(200))
