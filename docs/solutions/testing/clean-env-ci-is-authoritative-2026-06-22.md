---
module: task-tracker
date: "2026-06-22"
problem_type: testing_issue
category: testing
component: ci_testing
severity: high
applies_when:
  - "A test reads task-tracker config from process env (TASK_TRACKER_*, TELEGRAM_CHAT_ID_*, OPENCLAW_TOPIC_*) rather than isolating it in its fixture"
  - "Tests pass locally but a CI job (GitHub Actions, clean env) fails the same commit"
  - "A reviewer/agent reports 'review-clean' but CI is red"
symptoms:
  - "Local `pytest -q` is green while CI fails the identical commit with no flake/randomizer"
  - "main itself is red and inherits into every feature PR"
  - "A non-isolated fixture passes only where an ambient env var happens to be set (the author's machine)"
  - "autoreview/thermo/codex all approve, but CI is red"
---

# Clean-env CI is the authority, not local pytest (env pollution masks failures)

## What happened (v0.3 build, 2026-06-22)

The first unit's PR (U1) showed red CI on `test_weekly_brag.py::test_dual_push_preserves_reactive_approvable_task`. The test **passed locally** and the three static reviewers (autoreview/thermo/codex) all **approved** the code — but CI was red on the exact merge commit. Investigation showed the failure also reproduced on **clean `origin/main`**: `main` itself had been red since the test was added (#138). So the failure was **pre-existing and inherited**, not caused by U1.

## Root cause

This NixOS dev host's shell exports dozens of real config vars (`TASK_TRACKER_WORK_FILE`, `TASK_TRACKER_DAILY_NOTES_DIR`, `TELEGRAM_CHAT_ID_*`, `OPENCLAW_TOPIC_*`, …). The failing test's shared `env` fixture set most state paths but **omitted `TASK_TRACKER_DAILY_NOTES_DIR`/`DONE_LOG_DIR`**, so `approve()` → `complete_by_id()` → done-logging read the **ambient** value (the author's real Obsidian dir). The test therefore passed wherever that var was set (the author's box, a CI env leak in an earlier suite run) and **failed in a truly clean env** — returning `approve(...) -> ok:False`. CI runs `pip install pytest && pytest -q` with no ambient task-tracker env, so it exposed the gap.

Static reviewers (autoreview/thermo/codex) read the diff; they **cannot see an env-pollution behavioral failure**. Only running the suite in a clean env catches it.

## Fix

Make the fixture self-isolating — pin the done-log dir under `tmp_path` like the other state files (PR #140):

```python
daily = tmp_path / "daily"
monkeypatch.setenv("TASK_TRACKER_DAILY_NOTES_DIR", str(daily))
monkeypatch.setenv("TASK_TRACKER_DONE_LOG_DIR", str(daily))
```

Full suite then passed in a clean env (was 1 failed → 822 passed), greening `main`.

## The durable lessons

1. **CI's clean env is the authority** — a green local `pytest` with a red CI means a **fixture isolation gap**, not a flake. Trust CI.
2. **Reproduce CI locally by unsetting the ambient vars** (do NOT trust a polluted local run):
   ```bash
   ( unset ${!TASK_TRACKER_*} ${!TASK_MGMT_*} ${!TELEGRAM_CHAT_ID*} ${!OPENCLAW_TOPIC_*} ${!DIALPAD_*} 2>/dev/null
     python3 -m pytest -q )
   ```
   **Never strip `PATH`** on this host (an isolated/empty `PATH` can deadlock system binary wrappers and freeze the workstation — use targeted `unset`, never `env -i`).
3. **`main` must be green before starting a unit** — a red `main` inherits into every PR and masks the unit's own status. Confirm `main` green first; if a unit's PR is red, check `main` before blaming the unit.
4. **A fixture that mutates board/ledger/done-log state must pin ALL of them under `tmp_path`**, including the done-log dir — a fixture docstring promising "isolate every state file" must actually do so.
5. **Static review ≠ CI.** autoreview/thermo/codex approving is necessary but not sufficient; a behavioral env-isolation failure is invisible to them.

## Related: the dual-registration CI contract

A new ledger event type must be added to **both** `task_ledger.KNOWN_EVENT_TYPES` **and** `tests/test_event_registry.py`'s `REQUIRED_NEW_TYPES` — the registry test asserts both sides match, so missing either is a hard CI failure (hit while adding the `eod_disposition_*` types in U5).
