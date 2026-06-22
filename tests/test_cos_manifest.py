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


def test_health_threshold_is_configurable(capsys):
    at_30h = (cos_config.local_now() - timedelta(hours=30)).isoformat()
    _write_health({"r": {"last_success_ts": at_30h}})
    # 30h-old success is OK under the default 36h, STALE under a tight 24h.
    cos_manifest.main(["health", "--stale-hours", "24"])
    assert "STALE r" in capsys.readouterr().out
    cos_manifest.main(["health", "--stale-hours", "36"])
    assert "OK r" in capsys.readouterr().out


def test_health_empty_when_nothing_recorded(capsys):
    cos_manifest.main(["health"])
    assert "No ritual health recorded yet." in capsys.readouterr().out


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
