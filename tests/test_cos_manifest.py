"""H4 manifest + health surface (cos_manifest.py).

Invariants pinned here:
- `manifest` writes a cos-manifest.json carrying enabled_units + the rituals health
  map (so a watchdog can poll one file);
- `health` flags a ritual with an old last_success_ts as STALE and a fresh one as OK;
- skill_version is "unknown" when no stamp file exists, and the stamp value when one
  is present (best-effort, never a hard dependency).

Fake ids only; no real openclaw.
"""

import json
import sys
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import cos_config  # noqa: E402
import cos_health  # noqa: E402
import cos_manifest  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_MGMT_STATE_DIR", str(tmp_path / "state"))
    yield


# --- manifest subcommand ----------------------------------------------------


def test_manifest_writes_file_with_units_and_ritual_health(capsys):
    cos_health.record_success("standup")
    rc = cos_manifest.main(["manifest"])
    assert rc == 0

    on_disk = json.loads(cos_manifest.manifest_path().read_text())
    # enabled_units encodes the deploy decision (U1..U5 live, U6 dormant).
    units = {u["unit"]: u["status"] for u in on_disk["enabled_units"]}
    assert units["U1"] == "live" and units["U6"] == "dormant"
    # the rituals health map is embedded verbatim.
    assert "standup" in on_disk["rituals"]
    assert on_disk["rituals"]["standup"]["last_success_ts"]
    assert "generated_ts" in on_disk and "skill_version" in on_disk
    # the printed manifest matches what was written.
    printed = json.loads(capsys.readouterr().out)
    assert printed == on_disk


# --- health subcommand: STALE vs OK ----------------------------------------


def _write_health(state):
    cos_health.health_path().parent.mkdir(parents=True, exist_ok=True)
    cos_health.health_path().write_text(json.dumps(state))


def test_health_flags_old_success_as_stale_and_fresh_as_ok(capsys):
    fresh = cos_config.local_now().isoformat()
    old = (cos_config.local_now() - timedelta(hours=72)).isoformat()
    _write_health({
        "fresh_ritual": {"last_success_ts": fresh},
        "stale_ritual": {"last_success_ts": old},
    })

    rc = cos_manifest.main(["health"])  # default 36h threshold
    assert rc == 0
    out = capsys.readouterr().out
    assert "OK fresh_ritual" in out
    assert "STALE stale_ritual" in out


def test_health_flags_never_run_ritual_as_stale(capsys):
    _write_health({"never_ran": {"last_failure": {"error_class": "auth", "ts": "x"}}})
    cos_manifest.main(["health"])
    out = capsys.readouterr().out
    assert "STALE never_ran" in out
    assert "last_failure: auth" in out  # the last bad run is surfaced too


# --- R1 Fix 4: DEGRADED on a fresh failure + the expected-ritual registry --------


def test_r1_newer_failure_is_degraded_not_ok(capsys):
    """R1 Fix 4: a ritual whose last_failure_ts is NEWER than its (still-fresh)
    last_success_ts is DEGRADED, not OK. A ritual that ran and BROKE must not read OK
    just because the older success is inside the stale window -- the most-recent outcome
    is a failure, the loudest signal."""
    now = cos_config.local_now()
    success = (now - timedelta(hours=2)).isoformat()  # fresh success...
    failure = (now - timedelta(hours=1)).isoformat()  # ...but a NEWER failure
    _write_health({"standup": {
        "last_success_ts": success,
        "last_failure": {"error_class": "transient", "ts": failure},
        "last_failure_ts": failure,
    }})
    cos_manifest.main(["health"])
    out = capsys.readouterr().out
    assert "DEGRADED standup" in out
    assert "OK standup" not in out  # NOT a false-green
    assert "last_failure: transient" in out  # the bad run is still surfaced


def test_r1_failure_equal_to_success_is_degraded(capsys):
    """The boundary: last_failure_ts EQUAL to last_success_ts is DEGRADED (>= is the
    documented rule -- a failure at least as recent as the last good run)."""
    ts = cos_config.local_now().isoformat()
    _write_health({"nag_check": {
        "last_success_ts": ts,
        "last_failure": {"error_class": "nonzero_exit", "ts": ts},
        "last_failure_ts": ts,
    }})
    cos_manifest.main(["health"])
    assert "DEGRADED nag_check" in capsys.readouterr().out


def test_r1_older_failure_then_newer_success_is_ok(capsys):
    """A failure OLDER than a newer success is a RECOVERED ritual -- OK, not DEGRADED.
    The fresh-failure rule must not flag a ritual that already recovered."""
    now = cos_config.local_now()
    failure = (now - timedelta(hours=5)).isoformat()  # older failure...
    success = (now - timedelta(hours=1)).isoformat()  # ...then a newer success
    _write_health({"standup": {
        "last_success_ts": success,
        "last_failure": {"error_class": "auth", "ts": failure},
        "last_failure_ts": failure,
    }})
    cos_manifest.main(["health"])
    out = capsys.readouterr().out
    assert "OK standup" in out
    assert "DEGRADED standup" not in out


def test_r1_registered_ritual_never_run_is_missing(capsys):
    """A REGISTERED ritual with NO health entry at all (never ran) is flagged MISSING --
    the registry is iterated even when health.json lacks the entry, so a never-started
    ritual is visible, not silently absent. Here only standup is recorded; nag_check and
    the rest are MISSING."""
    _write_health({"standup": {"last_success_ts": cos_config.local_now().isoformat()}})
    cos_manifest.main(["health"])
    out = capsys.readouterr().out
    assert "OK standup" in out  # the one recorded ritual is OK...
    # ...and every OTHER registered ritual that never ran is MISSING, not absent.
    for missing in ("nag_check", "weekly_review", "ledger_harvest", "eod_review"):
        assert f"MISSING {missing}: last_success never" in out


def test_r1_registry_iterated_when_health_json_lacks_entry(capsys):
    """Even with a health.json that records ONLY an unregistered ad-hoc ritual, every
    REGISTERED ritual still appears (MISSING) -- the view is registry UNION recorded, so
    a registered ritual is never dropped just because the file has no entry for it."""
    _write_health({"some_adhoc_ritual": {"last_success_ts": cos_config.local_now().isoformat()}})
    cos_manifest.main(["health"])
    out = capsys.readouterr().out
    for ritual in cos_manifest._EXPECTED_RITUALS:
        assert f"MISSING {ritual}" in out
    # The unregistered recorded ritual is still shown (with its own OK/STALE status).
    assert "some_adhoc_ritual" in out


def test_r1_fresh_success_no_newer_failure_is_ok(capsys):
    """The OK baseline under the new rule: a recent success with no failure at all (or
    only an older one) reads OK -- Fix 4 does not over-flag a healthy ritual."""
    _write_health({"standup": {"last_success_ts": cos_config.local_now().isoformat()}})
    cos_manifest.main(["health"])
    assert "OK standup" in capsys.readouterr().out


def test_r1_failure_without_success_stays_stale(capsys):
    """A ritual with a failure but NO recorded success is STALE-by-absent-success, NOT
    DEGRADED: there is no good run to be 'newer than', so the staleness story holds.
    (Pins the documented precedence boundary so DEGRADED requires a comparable success.)"""
    _write_health({"standup": {"last_failure": {"error_class": "auth", "ts": "x"},
                               "last_failure_ts": "x"}})
    cos_manifest.main(["health"])
    out = capsys.readouterr().out
    assert "STALE standup" in out
    assert "DEGRADED standup" not in out


def test_health_threshold_is_configurable(capsys):
    at_30h = (cos_config.local_now() - timedelta(hours=30)).isoformat()
    _write_health({"r": {"last_success_ts": at_30h}})
    # 30h-old success is OK under the default 36h, STALE under a tight 24h.
    cos_manifest.main(["health", "--stale-hours", "24"])
    assert "STALE r" in capsys.readouterr().out
    cos_manifest.main(["health", "--stale-hours", "36"])
    assert "OK r" in capsys.readouterr().out


def test_health_empty_shows_registry_rituals_as_missing(capsys):
    """R1 Fix 4: with NOTHING recorded, the expected-ritual registry is still iterated --
    every registered ritual that has never run is flagged MISSING (a kind of STALE), so
    a never-started ritual is LOUD, not silently absent. This replaces the pre-R1
    'No ritual health recorded yet.' empty line (the registry is never empty)."""
    cos_manifest.main(["health"])
    out = capsys.readouterr().out
    for ritual in ("standup", "nag_check", "weekly_review", "ledger_harvest", "eod_review"):
        assert f"MISSING {ritual}: last_success never" in out


# --- skill_version best-effort ----------------------------------------------


def test_skill_version_reads_stamp_when_present(monkeypatch, tmp_path):
    root = tmp_path / "skill"
    root.mkdir()
    (root / "VERSION").write_text("9.9.9\n")
    monkeypatch.setattr(cos_manifest, "_skill_root", lambda: root)
    assert cos_manifest.skill_version() == "9.9.9"


def test_skill_version_unknown_when_no_stamp(monkeypatch, tmp_path):
    root = tmp_path / "skill"
    root.mkdir()  # no VERSION / DEPLOY_STAMP file
    monkeypatch.setattr(cos_manifest, "_skill_root", lambda: root)
    assert cos_manifest.skill_version() == "unknown"


def test_skill_version_prefers_version_then_deploy_stamp(monkeypatch, tmp_path):
    root = tmp_path / "skill"
    root.mkdir()
    (root / "DEPLOY_STAMP").write_text("sha-abc123\n")
    monkeypatch.setattr(cos_manifest, "_skill_root", lambda: root)
    # No VERSION -> falls through to DEPLOY_STAMP.
    assert cos_manifest.skill_version() == "sha-abc123"
