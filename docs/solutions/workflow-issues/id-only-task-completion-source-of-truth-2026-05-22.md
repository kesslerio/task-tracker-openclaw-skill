---
module: task-tracker
date: "2026-05-22"
problem_type: workflow_issue
category: workflow-issues
component: development_workflow
severity: high
applies_when:
  - "Task completion flows mutate durable task state"
  - "Workflows consume candidate completions from prompts, logs, or inboxes"
  - "Task identity can be confused by fuzzy titles, duplicate DONE markers, or stale parser behavior"
symptoms:
  - "Weak task identity makes completion state unsafe to mutate"
  - "Fuzzy or title-based DONE mutation can mark the wrong task complete"
  - "Multiple sources of truth cause duplicated or missed DONE state"
  - "Workflow consumers lack a safe completion-candidate contract"
root_cause: missing_workflow_step
resolution_type: workflow_improvement
related_components:
  - "lobster-workflows"
  - "openclaw-workflows"
  - "completion-evidence-inbox"
  - "productivity-prompts"
tags:
  - task-tracker
  - task-identity
  - id-only-mutation
  - completion-evidence
  - workflow-safety
  - lobster
  - openclaw
  - source-of-truth
---

# ID-only task completion and evidence-backed workflow consumption

## Context

The task-tracker redesign exposed a durable workflow problem: task state was
editable in Obsidian, but several agents, scripts, and cron workflows also tried
to infer completion from titles, daily notes, archive text, calendar data, or
prompt output. That made duplicate titles, fuzzy matches, stale checked lines,
and parser drift capable of mutating the wrong task or hiding an incomplete one.

The safe foundation is intentionally small:

- Active task boards own current task state.
- Daily and weekly notes are logs or evidence, not canonical task state.
- The JSONL sidecar ledger stores audit and candidate history, not the active
  task database.
- `task_id::` is the only identity that can mutate an active task.
- Completion evidence creates candidates first; it does not complete tasks.

## Guidance

Use one current-state owner. The active board markdown remains editable and
human-readable, but it is the only current-state surface. Logs, summaries,
calendar entries, emails, session notes, and chat messages may explain or
suggest work, but they do not own task state.

Use one mutation identity. Every active task must have a durable `task_id::`.
Mutation commands must resolve exactly one active canonical task ID. Titles,
list positions, quick IDs, legacy fallback IDs, and fuzzy matches are diagnostic
or suggestion fields only.

Separate evidence from mutation. Completion evidence from daily notes, EOD
summaries, Telegram, Lobster, calendar, email, or sessions should be scanned into
a completion inbox. Scanning writes candidate history only. Confirmation must
route through the ID-only completion kernel.

Make candidate JSON hard to misuse. Exact `task_id::` or canonical links may
expose `confirmable_task_id`. Title, fuzzy, fallback, or normalized matches
should expose only `suggested_match`, requiring an explicit canonical task ID
before confirmation.

Wire workflow consumers before adding noisy sources. Standup, EOD, weekly,
Telegram, and Lobster should first list, show, reject, snooze, or confirm
existing candidates. Gmail, calendar, session-log, and broader Telegram DONE
ingestion should wait until the review-and-confirm loop is proven.

Keep wrappers thin. Workflow scripts should call task-tracker commands instead
of re-parsing task state or implementing their own completion semantics.

## Examples

Do not complete by title:

```bash
python3 scripts/tasks.py done "Draft proposal"
```

Complete by canonical ID:

```bash
python3 scripts/tasks.py done tsk_abc123
```

Use the evidence inbox path:

```bash
python3 scripts/tasks.py completion-candidates scan --file done-log.md
python3 scripts/tasks.py completion-candidates list
python3 scripts/tasks.py completion-candidates confirm cand_abc123 --task-id tsk_abc123
```

Workflow wrappers should behave as control surfaces over the inbox:

```bash
bash scripts/task-mgmt/completion-inbox-control.sh list
bash scripts/task-mgmt/completion-inbox-control.sh confirm \
  cand_abc123 --task-id tsk_abc123
```

Prompt contract for productivity topics:

```text
Complete tasks only by canonical task_id::. Treat calendar, Gmail, session,
chat, and daily-note DONEs as evidence until the user confirms the candidate.
```

## Why This Matters

This pattern prevents old automation from becoming a second task system. It lets
Obsidian stay editable while keeping agent and cron writes deterministic. It also
gives users a visible review point for ambiguous work evidence instead of letting
fuzzy matching silently mutate task state.

## Verification

The implementation sequence that produced this pattern used these gates:

- Identity kernel: `task_id::` generation, identity audit, repair, ID-only done,
  and append-only ledger tests.
- Parser consolidation: one `TaskRecord` read model and line-number-verified
  mutation helpers.
- Hardening: strict malformed-ledger handling, completion rollback across board,
  daily log, and ledger writes, and shared weekly cleanup helpers.
- Evidence inbox: scan, list, show, confirm, reject, snooze, duplicate handling,
  candidate projection, and no board mutation from scans.
- Workflow consumption: standup, EOD, weekly, Telegram, and Lobster surfaces
  consume the inbox without auto-completion or external ingestion.

## Related

- `README.md`
- `SKILL.md`
- `references/commands.md`
- `docs/ARCHITECTURE.md`
- `docs/plans/2026-05-20-001-refactor-pr108-identity-kernel-split-plan.md`
- `docs/plans/2026-05-21-003-feat-completion-evidence-inbox-plan.md`
- `docs/plans/2026-05-21-004-feat-inbox-workflow-consumption-plan.md`
- `lobster-workflows/docs/cron-reliability.md`
- `lobster-workflows/scripts/task-mgmt/STATE_MODEL.md`

Session-history scan: no relevant prior sessions were found for `task-tracker`
in the seven-day scan used for this refresh.
