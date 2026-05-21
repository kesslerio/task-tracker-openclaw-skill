---
title: fix: harden 108B task identity foundation before evidence inbox
type: fix
status: active
date: 2026-05-21
origin: oracle-progress-review-after-108a-108b
---

# fix: harden 108B task identity foundation before evidence inbox

## Summary

Harden the landed 108A/108B foundation before starting 108C. Oracle found no P0 blocker and agreed 108C is still the right next product slice, but flagged three P1 risks that should be closed first: tolerant ledger reads can hide malformed audit history, `complete_by_id()` does not restore all side effects when board writes fail after completion logging, and weekly archive cleanup still has a raw string removal path instead of the shared line-number-verified helper.

This plan is intentionally smaller than 108C. It does not add completion candidates or new evidence sources. It makes the existing mutation/audit boundary safer so 108C can build on it.

## Requirements

- R1. Ledger reads used by task-tracker code must not silently discard malformed JSONL. They must surface malformed lines as structured warnings or explicit errors.
- R2. ID-only completion must restore board, daily-log, and ledger snapshots if any write after daily-log append fails.
- R3. Weekly archive stale-line cleanup must use the shared `task_lines.remove_task_line()` helper with raw-line and line-number verification.
- R4. The hardening must preserve the current contract: board markdown remains current state, ledger remains audit trail, daily notes remain user-facing completion log.
- R5. Public docs/plans should reflect that these P1s are closed before 108C and that 108C remains evidence-inbox-only.

## Scope Boundaries

- Do not implement 108C completion candidates.
- Do not add Gmail, calendar, session-log, Telegram, or Lobster cron evidence ingestion.
- Do not make ledger replay the source of truth.
- Do not add broad state transitions, weekly UX redesign, or standup decision UX.
- Do not remove the legacy `eod_sync.py --apply` path in this PR; it remains explicitly legacy and non-canonical.

## Technical Decisions

- Keep `read_events()` backward-compatible for callers by returning only valid events by default, but add a strict/reporting API so audit-sensitive callers and future 108C code can detect malformed lines.
- Treat board write failure the same as ledger append failure: restore all snapshots captured before mutation and return a structured error.
- Sort weekly stale cleanup removals bottom-up so line numbers remain stable across multiple deletions in one pass.

## Implementation Units

### U1. Ledger Malformation Reporting

**Files:**

- Modify: `scripts/task_ledger.py`
- Modify: `tests/test_task_ledger.py`

**Approach:**

- Introduce structured malformed-line reporting for JSONL reads.
- Preserve existing tolerant reads where compatibility requires it, but expose a strict option or companion function that raises/returns warnings.
- Ensure malformed lines include enough context for repair: path, line number, message, and raw line.

**Test scenarios:**

- Tolerant read returns valid events and exposes/report malformed line metadata.
- Strict read fails or returns an error when malformed JSONL is present.
- Empty and missing ledger files still behave as empty event streams.

### U2. Completion Rollback Hardening

**Files:**

- Modify: `scripts/task_transitions.py`
- Modify: `tests/test_task_transitions.py`

**Approach:**

- Wrap daily-log append, board write, and ledger append in one guarded write section after snapshots are captured.
- If board write fails after daily-log append, restore the daily log and ledger snapshots as well as the board snapshot.
- If restore itself fails, report the original error plus restore failure details rather than pretending the mutation succeeded.

**Test scenarios:**

- Simulated board write failure after daily-log append leaves board unchanged and removes/restores the daily log.
- Ledger append failure still restores board and daily log.
- Successful completion still writes daily log, board mutation, and ledger event once.

### U3. Weekly Archive Cleanup Uses Shared Line Helper

**Files:**

- Modify: `scripts/weekly_review.py`
- Modify: `tests/test_weekly_review.py`

**Approach:**

- Replace raw `content.replace(raw_line + "\n", "", 1)` cleanup with `remove_task_line(content, raw_line, line_number)`.
- Process stale done tasks bottom-up to avoid line-number drift.
- Return the same removed count semantics.

**Test scenarios:**

- Cleanup removes a checked parent and its indented children.
- Cleanup handles tab-indented children through the shared helper.
- Cleanup does not remove a duplicated sibling line when the stored line number/raw line no longer matches.

### U4. Plan and Documentation Alignment

**Files:**

- Modify: `docs/plans/2026-05-20-001-refactor-pr108-identity-kernel-split-plan.md`
- Modify: `docs/plans/2026-05-21-001-refactor-single-parser-mutation-boundary-plan.md`
- Add: `docs/plans/2026-05-21-002-hardening-108b-before-evidence-inbox-plan.md`

**Approach:**

- Add concise follow-up notes that 108B hardening closes the Oracle P1s before 108C.
- Keep 108C scoped to completion evidence inbox only.

**Test scenarios:**

- Markdown lint passes for changed plan docs.
- Docs do not imply 108C should include Gmail/calendar/session/Lobster ingestion or auto-mutation.

## Verification

- `python3 -m pytest -q tests/test_task_ledger.py tests/test_task_transitions.py tests/test_weekly_review.py`
- `python3 -m pytest -q`
- `bash scripts/ci/check-public-hygiene.sh`
- `markdownlint` on changed plan docs when available
- `git diff --check`
