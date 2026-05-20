import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


def _env(tmp_path, work):
    env = os.environ.copy()
    env["TASK_TRACKER_WORK_FILE"] = str(work)
    env["TASK_TRACKER_DAILY_NOTES_DIR"] = str(tmp_path)
    env["TASK_TRACKER_LEDGER_FILE"] = str(tmp_path / "events.jsonl")
    env["STANDUP_CALENDARS"] = "{}"
    return env


def test_identity_repair_dry_run_writes_nothing(tmp_path):
    work = tmp_path / "Work Tasks.md"
    original = """# Work

## 🔴 Q1
- [ ] **Ship milestone** area:: Delivery
"""
    work.write_text(original)

    proc = subprocess.run(
        ["python3", "scripts/tasks.py", "identity-repair"],
        capture_output=True,
        text=True,
        check=False,
        env=_env(tmp_path, work),
    )

    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert payload["applied"] is False
    assert payload["proposed_repairs"][0]["title"] == "Ship milestone"
    assert work.read_text() == original
    assert not (tmp_path / "events.jsonl").exists()


def test_identity_repair_apply_adds_task_id_and_ledger_event(tmp_path):
    work = tmp_path / "Work Tasks.md"
    work.write_text("""# Work

## 🔴 Q1
- [ ] **Ship milestone** area:: Delivery
""")

    proc = subprocess.run(
        ["python3", "scripts/tasks.py", "identity-repair", "--apply"],
        capture_output=True,
        text=True,
        check=False,
        env=_env(tmp_path, work),
    )

    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert payload["applied"] is True
    assert payload["changed"] == 1
    assert " task_id::tsk_" in work.read_text()
    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert events[0]["event_type"] == "metadata_repair"


def test_identity_repair_blocks_duplicate_titles_without_writes(tmp_path):
    work = tmp_path / "Work Tasks.md"
    original = """# Work

## 🔴 Q1
- [ ] **Same title** area:: Delivery
- [ ] **Same title** area:: Ops
"""
    work.write_text(original)

    proc = subprocess.run(
        ["python3", "scripts/tasks.py", "identity-repair", "--apply"],
        capture_output=True,
        text=True,
        check=False,
        env=_env(tmp_path, work),
    )

    assert proc.returncode == 2
    payload = json.loads(proc.stdout)
    assert "ambiguous-title" in payload["blocking_invariants"]
    assert work.read_text() == original
    assert not (tmp_path / "events.jsonl").exists()


def test_identity_repair_noops_when_ambiguous_titles_have_ids(tmp_path):
    work = tmp_path / "Work Tasks.md"
    original = """# Work

## 🔴 Q1
- [ ] **Same title** task_id::tsk_one area:: Delivery
- [ ] **Same title** task_id::tsk_two area:: Ops
"""
    work.write_text(original)

    proc = subprocess.run(
        ["python3", "scripts/tasks.py", "identity-repair", "--apply"],
        capture_output=True,
        text=True,
        check=False,
        env=_env(tmp_path, work),
    )

    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert payload["applied"] is True
    assert payload["changed"] == 0
    assert work.read_text() == original


def test_identity_repair_aborts_when_ledger_unwritable(tmp_path):
    work = tmp_path / "Work Tasks.md"
    original = """# Work

## 🔴 Q1
- [ ] **Ship milestone** area:: Delivery
"""
    work.write_text(original)
    ledger_dir = tmp_path / "ledger-is-dir"
    ledger_dir.mkdir()
    env = _env(tmp_path, work)
    env["TASK_TRACKER_LEDGER_FILE"] = str(ledger_dir)

    proc = subprocess.run(
        ["python3", "scripts/tasks.py", "identity-repair", "--apply"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert proc.returncode == 2
    payload = json.loads(proc.stdout)
    assert "ledger-unwritable" in payload["blocking_invariants"]
    assert work.read_text() == original


def test_identity_repair_rolls_back_when_ledger_append_fails(tmp_path):
    if not os.path.exists("/dev/full"):
        return
    work = tmp_path / "Work Tasks.md"
    original = """# Work

## 🔴 Q1
- [ ] **Ship milestone** area:: Delivery
"""
    work.write_text(original)
    env = _env(tmp_path, work)
    env["TASK_TRACKER_LEDGER_FILE"] = "/dev/full"

    proc = subprocess.run(
        ["python3", "scripts/tasks.py", "identity-repair", "--apply"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert proc.returncode == 2
    payload = json.loads(proc.stdout)
    assert "ledger-append-failed" in payload["blocking_invariants"]
    assert work.read_text() == original


def test_identity_repair_restores_partial_ledger_append(monkeypatch, tmp_path):
    import task_repair

    work = tmp_path / "Work Tasks.md"
    original = """# Work

## 🔴 Q1
- [ ] **Ship milestone** area:: Delivery
- [ ] **Write docs** area:: Delivery
"""
    work.write_text(original)
    ledger = tmp_path / "events.jsonl"
    ledger.write_text('{"event_type":"existing"}\n')
    monkeypatch.setenv("TASK_TRACKER_WORK_FILE", str(work))
    monkeypatch.setenv("TASK_TRACKER_LEDGER_FILE", str(ledger))

    calls = {"count": 0}

    def append_then_fail(event, path=None):
        calls["count"] += 1
        if calls["count"] == 1:
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(event) + "\n")
            return event
        raise OSError("simulated full disk")

    monkeypatch.setattr(task_repair, "append_event", append_then_fail)

    payload = task_repair.repair_missing_ids(apply=True)

    assert payload["blocked"] is True
    assert "ledger-append-failed" in payload["blocking_invariants"]
    assert work.read_text() == original
    assert ledger.read_text() == '{"event_type":"existing"}\n'


def test_personal_identity_repair_uses_personal_sidecar_by_default(tmp_path):
    work = tmp_path / "Work Tasks.md"
    personal = tmp_path / "Personal Tasks.md"
    work.write_text("# Work\n\n## 🔴 Q1\n- [ ] **Work task** area:: Ops\n")
    personal.write_text("# Personal\n\n## 🔴 Q1\n- [ ] **Personal task** area:: Home\n")
    env = os.environ.copy()
    env["TASK_TRACKER_WORK_FILE"] = str(work)
    env["TASK_TRACKER_PERSONAL_FILE"] = str(personal)

    proc = subprocess.run(
        ["python3", "scripts/tasks.py", "--personal", "identity-repair", "--apply"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert proc.returncode == 0
    assert "task_id::" in personal.read_text()
    assert personal.with_suffix(".md.events.jsonl").exists()
    assert not work.with_suffix(".md.events.jsonl").exists()
