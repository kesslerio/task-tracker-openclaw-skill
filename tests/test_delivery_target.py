"""Contract 2: delivery-target descriptor + proof.

Invariants:
- accepts env-assembled productivity targets, including IDENTITY topic 1909;
- rejects the Work/heartbeat group;
- rejects unset env (blocked, never a guess).
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import delivery_target

# Fake chat ids: both are valid chat-id shape (^-?\d+$) but neither starts with
# -100, so the public-hygiene grep (-100[0-9]{8,}) will not flag them. The real
# group ids are env-sourced at runtime, never committed to source.
PRODUCTIVITY = "-4242424242"
WORK_GROUP = "-5252525252"

_TOPIC_ENV = {
    "OPENCLAW_TOPIC_PRODUCTIVITY_STANDUP": "2",
    "OPENCLAW_TOPIC_PRODUCTIVITY_WEEKLY_REVIEW_PLANNING": "4",
    "OPENCLAW_TOPIC_PRODUCTIVITY_DONE": "5",
    "OPENCLAW_TOPIC_PRODUCTIVITY_JOURNAL": "6",
    # IDENTITY intentionally NOT set -- it must default to 1909.
}


def _set_productivity_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID_PRODUCTIVITY", PRODUCTIVITY)
    monkeypatch.setenv("TELEGRAM_CHAT_ID_WORK", WORK_GROUP)
    for name, value in _TOPIC_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("OPENCLAW_TOPIC_PRODUCTIVITY_IDENTITY", raising=False)


def _clear_env(monkeypatch):
    for name in [
        "TELEGRAM_CHAT_ID_PRODUCTIVITY",
        "TELEGRAM_CHAT_ID_WORK",
        "OPENCLAW_TOPIC_PRODUCTIVITY_STANDUP",
        "OPENCLAW_TOPIC_PRODUCTIVITY_WEEKLY_REVIEW_PLANNING",
        "OPENCLAW_TOPIC_PRODUCTIVITY_DONE",
        "OPENCLAW_TOPIC_PRODUCTIVITY_JOURNAL",
        "OPENCLAW_TOPIC_PRODUCTIVITY_IDENTITY",
    ]:
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("topic", ["2", "4", "5", "6"])
def test_accepts_env_assembled_topics(monkeypatch, topic):
    _set_productivity_env(monkeypatch)
    result = delivery_target.prove_delivery_target(PRODUCTIVITY, topic)
    assert result["ok"] is True
    target = result["delivery_target"]
    assert target == {
        "chat_id": PRODUCTIVITY,
        "topic_id": topic,
        "agent_id": "niemand-work",
        "channel": "telegram",
    }


def test_accepts_identity_topic_1909_via_default(monkeypatch):
    _set_productivity_env(monkeypatch)
    result = delivery_target.prove_delivery_target(PRODUCTIVITY, "1909")
    assert result["ok"] is True
    assert result["delivery_target"]["topic_id"] == "1909"
    assert (PRODUCTIVITY, "1909") in delivery_target.known_safe_targets()


def test_rejects_work_group(monkeypatch):
    _set_productivity_env(monkeypatch)
    result = delivery_target.prove_delivery_target(WORK_GROUP, "2")
    assert result["ok"] is False
    assert result["reason"] == "work_group"


def test_rejects_unset_env_as_blocked_not_guess(monkeypatch):
    _clear_env(monkeypatch)
    assert delivery_target.known_safe_targets() == set()
    result = delivery_target.prove_delivery_target(PRODUCTIVITY, "2")
    assert result["ok"] is False
    assert result["reason"] == "env_missing"
    assert "delivery_target" not in result


def test_rejects_unknown_topic_in_productivity_group(monkeypatch):
    _set_productivity_env(monkeypatch)
    result = delivery_target.prove_delivery_target(PRODUCTIVITY, "9999")
    assert result["ok"] is False
    assert result["reason"] == "target_unknown"


# --- Finding #11: work-group whitespace / int / '+' variants -> work_group ----

@pytest.mark.parametrize(
    "variant",
    [
        WORK_GROUP,
        f"  {WORK_GROUP}  ",          # surrounding whitespace
        int(WORK_GROUP),              # int variant (no quotes)
        f"\t{WORK_GROUP}\n",          # tab/newline whitespace
    ],
)
def test_work_group_variants_all_rejected(monkeypatch, variant):
    _set_productivity_env(monkeypatch)
    result = delivery_target.prove_delivery_target(variant, "2")
    assert result["ok"] is False
    assert result["reason"] == "work_group"


def test_work_group_whitespace_variant_not_proven_as_safe(monkeypatch):
    """Defence in depth: even if the work group were the productivity chat, the
    whitespace variant must still normalise to the same rejected value."""
    monkeypatch.setenv("TELEGRAM_CHAT_ID_PRODUCTIVITY", WORK_GROUP)
    monkeypatch.setenv("TELEGRAM_CHAT_ID_WORK", WORK_GROUP)
    monkeypatch.setenv("OPENCLAW_TOPIC_PRODUCTIVITY_STANDUP", "2")
    result = delivery_target.prove_delivery_target(f" {WORK_GROUP} ", "2")
    assert result["ok"] is False
    assert result["reason"] == "work_group"


# --- Finding #12: shape validation + channel allowlist -----------------------

def test_garbage_topic_env_value_is_skipped(monkeypatch, recwarn):
    monkeypatch.setenv("TELEGRAM_CHAT_ID_PRODUCTIVITY", PRODUCTIVITY)
    monkeypatch.setenv("OPENCLAW_TOPIC_PRODUCTIVITY_STANDUP", "2")
    monkeypatch.setenv("OPENCLAW_TOPIC_PRODUCTIVITY_DONE", "not-a-topic")  # garbage
    monkeypatch.delenv("OPENCLAW_TOPIC_PRODUCTIVITY_WEEKLY_REVIEW_PLANNING", raising=False)
    monkeypatch.delenv("OPENCLAW_TOPIC_PRODUCTIVITY_JOURNAL", raising=False)
    monkeypatch.delenv("OPENCLAW_TOPIC_PRODUCTIVITY_IDENTITY", raising=False)
    safe = delivery_target.known_safe_targets()
    # The valid STANDUP=2 is present; the garbage DONE value is NOT in the allowlist.
    assert (PRODUCTIVITY, "2") in safe
    assert all(topic != "not-a-topic" for _, topic in safe)


def test_garbage_chat_id_yields_empty_allowlist(monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID_PRODUCTIVITY", "not-a-chat-id")
    monkeypatch.setenv("OPENCLAW_TOPIC_PRODUCTIVITY_STANDUP", "2")
    with pytest.warns(RuntimeWarning):
        assert delivery_target.known_safe_targets() == set()


def test_unknown_channel_is_rejected(monkeypatch):
    _set_productivity_env(monkeypatch)
    result = delivery_target.prove_delivery_target(PRODUCTIVITY, "2", channel="email")
    assert result["ok"] is False
    assert result["reason"] == "channel_unknown"


# --- Finding #14: no stale module-level KNOWN_SAFE_TARGETS snapshot -----------

def test_no_module_level_known_safe_targets_constant():
    assert not hasattr(delivery_target, "KNOWN_SAFE_TARGETS"), (
        "stale import-time KNOWN_SAFE_TARGETS snapshot must be removed; "
        "known_safe_targets() is the only (lazy) API"
    )


def test_known_safe_targets_picks_up_env_change_live(monkeypatch):
    _clear_env(monkeypatch)
    assert delivery_target.known_safe_targets() == set()
    monkeypatch.setenv("TELEGRAM_CHAT_ID_PRODUCTIVITY", PRODUCTIVITY)
    monkeypatch.setenv("OPENCLAW_TOPIC_PRODUCTIVITY_STANDUP", "2")
    assert (PRODUCTIVITY, "2") in delivery_target.known_safe_targets()
