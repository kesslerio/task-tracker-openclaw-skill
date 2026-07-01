import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from task_records import repair_hint  # noqa: E402


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


def test_identity_repair_apply_removes_matching_repair_hint(tmp_path):
    work = tmp_path / "Work Tasks.md"
    work.write_text(f"""# Work

## 🔴 Q1
- [ ] **Ship milestone** area:: Delivery
{repair_hint("Ship milestone")}
""")

    proc = subprocess.run(
        ["python3", "scripts/tasks.py", "identity-repair", "--apply"],
        capture_output=True,
        text=True,
        check=False,
        env=_env(tmp_path, work),
    )

    assert proc.returncode == 0
    updated = work.read_text()
    assert "- [ ] **Ship milestone** area:: Delivery task_id::tsk_" in updated
    assert repair_hint("Ship milestone") not in updated


def test_identity_repair_preserves_hint_for_unrepaired_bare_line(tmp_path):
    work = tmp_path / "Work Tasks.md"
    work.write_text(f"""# Work

## 🔴 Q1
- [ ] **Ship milestone** area:: Delivery
{repair_hint("Ship milestone")}

## ⚪ Backlog
- [ ] **Someday task** area:: Ideas
{repair_hint("Someday task")}
""")

    proc = subprocess.run(
        ["python3", "scripts/tasks.py", "identity-repair", "--apply"],
        capture_output=True,
        text=True,
        check=False,
        env=_env(tmp_path, work),
    )

    assert proc.returncode == 0
    updated = work.read_text()
    assert "- [ ] **Ship milestone** area:: Delivery task_id::tsk_" in updated
    assert repair_hint("Ship milestone") not in updated
    assert "- [ ] **Someday task** area:: Ideas\n" in updated
    assert repair_hint("Someday task") in updated


def test_identity_repair_removes_multiple_following_repair_hints_in_one_apply(tmp_path):
    work = tmp_path / "Work Tasks.md"
    work.write_text(f"""# Work

## 🔴 Q1
- [ ] **First repair** area:: Ops
{repair_hint("First repair")}
- [ ] **Second repair** area:: Ops
{repair_hint("Second repair")}
Plain note between repairs
- [ ] **Third repair** area:: Ops
{repair_hint("Third repair")}
- [ ] **Already stable** task_id::tsk_stable area:: Ops
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
    assert payload["changed"] == 3
    updated = work.read_text()
    assert "<!-- repair:" not in updated
    assert "- [ ] **First repair** area:: Ops task_id::tsk_" in updated
    assert "- [ ] **Second repair** area:: Ops task_id::tsk_" in updated
    assert "- [ ] **Third repair** area:: Ops task_id::tsk_" in updated
    assert "Plain note between repairs\n- [ ] **Third repair**" in updated
    assert "- [ ] **Already stable** task_id::tsk_stable area:: Ops" in updated


def test_identity_repair_handles_end_of_file_task_without_adjacent_hint(tmp_path):
    work = tmp_path / "Work Tasks.md"
    original = "# Work\n\n## 🔴 Q1\n- [ ] **Lonely EOF task** area:: Ops"
    work.write_text(original)

    proc = subprocess.run(
        ["python3", "scripts/tasks.py", "identity-repair", "--apply"],
        capture_output=True,
        text=True,
        check=False,
        env=_env(tmp_path, work),
    )

    assert proc.returncode == 0
    updated = work.read_text()
    assert updated.startswith("# Work\n\n## 🔴 Q1\n")
    assert "- [ ] **Lonely EOF task** area:: Ops task_id::tsk_" in updated


def test_rollover_repair_roundtrip_removes_stale_repair_hints(tmp_path):
    from rollover import rollover_board

    work = tmp_path / "Work Tasks.md"
    initial = """# Weekly TODOs — 2026-W25

## 🔴 Q1
- [ ] **Needs id** area:: Ops
- [ ] **Already has id** task_id::tsk_existing area:: Ops
"""
    rendered = rollover_board(initial, [], target_date="2026-06-29").content
    assert repair_hint("Needs id") in rendered
    work.write_text(rendered)

    proc = subprocess.run(
        ["python3", "scripts/tasks.py", "identity-repair", "--apply"],
        capture_output=True,
        text=True,
        check=False,
        env=_env(tmp_path, work),
    )

    assert proc.returncode == 0
    repaired = work.read_text()
    generated_id = re.search(r"Needs id\*\* area:: Ops task_id::(tsk_[a-f0-9]{16})", repaired)
    assert generated_id
    assert "task_id::tsk_existing" in repaired
    assert "<!-- repair:" not in repaired
    rerendered = rollover_board(repaired, [], target_date="2026-06-29").content
    assert "<!-- repair:" not in rerendered
    assert f"task_id::{generated_id.group(1)}" in rerendered
    assert "task_id::tsk_existing" in rerendered


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
    import task_identity
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
    monkeypatch.setattr(
        task_repair,
        "load_records",
        lambda personal=False: (
            work,
            work.read_text(encoding="utf-8"),
            task_identity.task_records(work.read_text(encoding="utf-8"), personal=personal),
        ),
    )

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


def test_identity_repair_removes_preflight_created_ledger_on_append_failure(monkeypatch, tmp_path):
    import task_identity
    import task_repair

    work = tmp_path / "Work Tasks.md"
    original = """# Work

## 🔴 Q1
- [ ] **Ship milestone** area:: Delivery
"""
    work.write_text(original)
    ledger = tmp_path / "events.jsonl"
    monkeypatch.setenv("TASK_TRACKER_WORK_FILE", str(work))
    monkeypatch.setenv("TASK_TRACKER_LEDGER_FILE", str(ledger))
    monkeypatch.setattr(
        task_repair,
        "load_records",
        lambda personal=False: (
            work,
            work.read_text(encoding="utf-8"),
            task_identity.task_records(work.read_text(encoding="utf-8"), personal=personal),
        ),
    )
    monkeypatch.setattr(task_repair, "append_event", lambda event, path=None: (_ for _ in ()).throw(OSError("simulated full disk")))

    payload = task_repair.repair_missing_ids(apply=True)

    assert payload["blocked"] is True
    assert "ledger-append-failed" in payload["blocking_invariants"]
    assert work.read_text() == original
    assert not ledger.exists()


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
