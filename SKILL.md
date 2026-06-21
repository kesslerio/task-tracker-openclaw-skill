---
name: task-tracker
description: "Personal task management with daily standups and weekly reviews. Supports both Work and Personal tasks from Obsidian. Use when: (1) User says 'daily standup' or asks what's on my plate, (2) User says 'weekly review' or asks about last week's progress, (3) User wants to add/update/complete tasks, (4) User asks about blockers or deadlines, (5) User shares meeting notes and wants tasks extracted, (6) User asks 'what's due this week' or similar."
homepage: https://github.com/kesslerio/task-tracker-openclaw-skill
metadata: {"openclaw":{"emoji":"📋","requires":{"env":["TASK_TRACKER_WORK_FILE","TASK_TRACKER_PERSONAL_FILE"]},"install":[{"id":"verify-paths","kind":"check","label":"Verify task file paths are configured"}]}}
---

# Task Tracker

Personal task management for work + personal workflows, with daily standups and
weekly reviews.

Source-of-truth model: active task boards are the current task state. Daily and
weekly notes are logs/evidence, not canonical task state. The JSONL sidecar
ledger is audit/candidate history for identity repairs, ID-based completions,
and completion evidence decisions.

## When to Use

Use this skill when the user asks to:

- Run a daily or personal standup
- Run a weekly review
- Add, list, update, or complete tasks
- Check blockers or due dates
- Extract actions from meeting notes
- Report completed daily items against weekly todos without changing canonical
  task state
- Review completion evidence candidates before confirming task completion
- Schedule freebusy-gated focus blocks for the day's priorities on an agent-owned
  "Task Focus" calendar, send a morning brief / pre-brief / debrief, and propose
  next week's priorities on Friday (U6 proactive layer; opt-in — degrades silently
  when the focus calendar / `STANDUP_CALENDARS` are absent and never overbooks)

## Proactive layer (U6)

`scripts/proactive_brief.py --mode {brief,prebrief,slip,friday,create}` is the cron
entry point for the proactive layer, plus `--mode debrief-capture --event-key <id>
--notes "<notes>"` for the reactive `/debrief` path. Every push proves its delivery
target FIRST (`prove_delivery_target` -> gated `act_id` -> `assert_send_target`) and
lands only on the proven Productivity topic; an unset/wrong target blocks the push
and sends nothing.

- `create` places freebusy-gated focus blocks for the day's Defended Three (read
  from U3's `focus-state.json`, never written by U6); `slip` slides a slipped block
  to the next free window and notifies the user.
- Calendar writes go through `scripts/calendar_blocks.py`, which freebusy-gates
  every create/move against EXTERNAL calendars (an overlap OR an unknown freebusy
  refuses the write — NEVER-OVERBOOK-EXTERNAL), slides a slipped block via
  `gog calendar update` (never delete+create), and refuses to delete/move any
  non-`agent_created` event (`ExternalEventError`).
- State lives in `focus-calendar.json` and `proactive-state.json` (atomic,
  torn-read safe — no duplicate briefs on a `*/5` scan; reactive `/debrief` capture
  is idempotent on a closed loop).

## Quick Start

Prefer environment-based configuration first, then run scripts from `<workspace>/<skill>`.

### 1) Configure paths (env-first)

```bash
# Required for work task workflows
export TASK_TRACKER_WORK_FILE="$HOME/path/to/Work Tasks.md"

# Required only for --personal commands
export TASK_TRACKER_PERSONAL_FILE="$HOME/path/to/Personal Tasks.md"

# Optional
export TASK_TRACKER_ARCHIVE_DIR="$HOME/path/to/archive"
export TASK_TRACKER_LEGACY_FILE="$HOME/path/to/TASKS.md"
export TASK_TRACKER_DAILY_NOTES_DIR="$HOME/path/to/Daily"
export TASK_TRACKER_WEEKLY_TODOS="$HOME/path/to/Weekly TODOs.md"
```

Defaults exist, but explicit env vars are recommended for portability.

### 2) Run from the skill directory

```bash
cd <workspace>/<skill>
# Example: cd ~/projects/skills/shared/task-tracker
```

### 3) Core commands

```bash
# Work
python3 scripts/tasks.py list
python3 scripts/standup.py
python3 scripts/weekly_review.py

# Personal
python3 scripts/tasks.py --personal list
python3 scripts/personal_standup.py
```

## Core Commands

### Task listing and filtering

```bash
python3 scripts/tasks.py list
python3 scripts/tasks.py list --priority high
python3 scripts/tasks.py list --due today
python3 scripts/tasks.py list --due this-week
python3 scripts/tasks.py blockers
```

### Add and complete tasks

```bash
python3 scripts/tasks.py add "Draft proposal" --priority high --due 2026-01-23
python3 scripts/tasks.py --personal add "Call mom" --priority high --due 2026-01-22
python3 scripts/tasks.py identity-audit
python3 scripts/tasks.py task-audit
python3 scripts/tasks.py identity-repair --apply
python3 scripts/tasks.py done "tsk_example"
python3 scripts/tasks.py --personal done "tsk_personal"
python3 scripts/tasks.py completion-candidates scan --file /tmp/done-log.md
python3 scripts/tasks.py completion-candidates list
python3 scripts/tasks.py completion-candidates confirm cand_example --task-id tsk_example
python3 scripts/completion_inbox_control.py list
python3 scripts/completion_inbox_control.py confirm cand_example --task-id tsk_example
```

Completion candidates are evidence suggestions. Scanning does not mutate active
tasks; confirmation must resolve to a canonical `task_id::`. Workflow wrappers
such as Telegram/Lobster should call `completion_inbox_control.py` or the
`completion-candidates` command group by candidate ID. They must not call
`done` by title, fuzzy match, fallback ID, quick ID, or list position.

Task audits are read-only health checks. They can flag duplicate titles, stale
active tasks, unresolved candidates, missing IDs, and backlog pressure, but they
must not be treated as authority to freeze, delete, merge, or complete tasks.

### Daily Priorities + capacity cap (Focus Core)

Two layers (Decision #7):

- **Layer 1 — Daily Top Priorities.** Each morning the agent proposes 2-3
  must-do-today priorities (veto/approve). This is a *selection* over active
  tasks; it surfaces and chases, it does not limit how many tasks exist.
- **Layer 2 — Active-inventory cap.** `tasks add` is gated at write time: when the
  active board's estimate-sum exceeds `WEEKLY_CAPACITY_HOURS` (default 25h;
  unestimated tasks counted at `UNESTIMATED_TASK_HOURS`=2h) OR the active count
  exceeds `ACTIVE_TASK_HARD_CAP` (default 20), a new add is blocked and nudged to
  the parking lot. It NEVER force-evicts existing tasks. `--force-parking` routes
  an over-cap add to the parking lot.

```bash
# /focus*  → propose / approve / veto / override / status
python3 scripts/focus_commands.py focus
python3 scripts/focus_commands.py approve
python3 scripts/focus_commands.py veto 2
python3 scripts/focus_commands.py override
python3 scripts/focus_commands.py status

# Capacity cap on add (blocks before the board write when over capacity)
python3 scripts/tasks.py add "New task"                 # blocked when over cap
python3 scripts/tasks.py add "New task" --force-parking  # route to parking lot
```

Map `/focus`, `/focus-approve`, `/focus-veto <N>`, `/focus-override`, and
`/focus-status` to these subcommands. The cap is date-independent (it governs
total active load, not today's plan), so it applies even if the morning ritual is
skipped; `focus-state.json` (Layer-1) is re-proposed each day.

### Backlog ops

```bash
python3 scripts/tasks.py promote-from-backlog --cap 3
python3 scripts/tasks.py review-backlog --stale-days 45 --json
```

### Standup and review

```bash
python3 scripts/standup.py
python3 scripts/standup.py --compact-json
python3 scripts/personal_standup.py
python3 scripts/weekly_review.py
```

### Extraction and automation helpers

```bash
python3 scripts/extract_tasks.py --from-text "Meeting notes..."
bash scripts/task-shortcuts.sh daily
bash scripts/task-shortcuts.sh standup   # alias of daily
bash scripts/task-shortcuts.sh weekly
bash scripts/task-shortcuts.sh done24h
bash scripts/task-shortcuts.sh done7d
bash scripts/task-shortcuts.sh tasks     # quick priorities view
```

### EOD sync + weekly embed refresh

```bash
python3 scripts/eod_sync.py --dry-run
python3 scripts/eod_sync.py
python3 scripts/eod_sync.py --apply   # legacy Weekly TODO checkbox write only
python3 scripts/update_weekly_embeds.py --dry-run
python3 scripts/update_weekly_embeds.py
```

### Autonomy audit + undo (U2 — 🧭 Identity topic 1909)

Reactive, owner-only commands that inspect or reverse a prior autonomous act.
Both reply in-topic (origin-proven) and never push to Telegram themselves.

```bash
bash scripts/telegram-commands.sh audit            # list recent autonomous acts
bash scripts/telegram-commands.sh audit act_<id>   # full detail for one act
bash scripts/telegram-commands.sh undo act_<id>    # reverse a reversible act
```

- `/undo act_<id>` — undo a recent autonomous act (tiered window: 4h for a nag
  ack, 7d for a board mutation). A board mutation is restored by re-inserting the
  snapshot's exact `raw_line` via content search (not a line-number guess), so it
  survives other edits to the board; a `nag_sent` act is undone by acking the nag
  loop (`ack_type=user_undo`) so it will not re-fire.
- `/audit` — list recent autonomous acts (act_id, type, task, target, status),
  newest first; an already-undone act is flagged.

As of v0.2 (U4), rung-3 proactive Telegram pushes are ENABLED at the gate
(`autonomy_gate.RUNG3_PUSH_ENABLED=True`): a nag push executes only with a PROVEN,
gated, asserted delivery target. The proof is unchanged — an unset env / wrong
group / mismatched send is still blocked. Boot preflight (U1) must keep
`autonomy-log.jsonl` writable for these.

### Accountability / nag engine (U4 — Productivity Group topic 2)

The nag engine chases overdue tasks until they are acknowledged. A nag is an open
loop persisted in `nag-state.json`; it closes ONLY on an explicit ack
(`/done`/`/reschedule`), a verified disappearance from the board, or a reschedule
out of the overdue window — never silently, never on a crash, and a `/snooze`
pauses but does NOT close it.

```bash
bash scripts/telegram-commands.sh nag-check            # cron pass (every ~3h, work hrs)
bash scripts/telegram-commands.sh nag-check --dry-run  # preview, no state write / push
bash scripts/telegram-commands.sh done <task_id>             # complete + close loop (same turn)
bash scripts/telegram-commands.sh reschedule <task_id> <date># move due:: + close loop
bash scripts/telegram-commands.sh snooze <task_id> <dur>     # pause loop (cap: 3 snoozes)
bash scripts/telegram-commands.sh body-double <task_id> <dur># focus session + check-ins
bash scripts/telegram-commands.sh cancel-session <task_id>   # end a body-double session
```

- Q1-aware: thresholds are `NAG_Q1_THRESHOLD_DAYS=1`, `NAG_Q2_THRESHOLD_DAYS=3`,
  `NAG_Q3_THRESHOLD_DAYS=7`, read off the scalar `overdue_days` because
  `effective_priority()` short-circuits non-q2/q3 tasks to `escalated=False`.
- Every nag push goes through `prove_delivery_target()` →
  `autonomy_gate.gate()` (act_id) → `assert_send_target()`. An unset
  `TELEGRAM_CHAT_ID_PRODUCTIVITY` / `OPENCLAW_TOPIC_PRODUCTIVITY_STANDUP` ⇒
  `nag_delivery_blocked:env_missing` and the loop STAYS OPEN.
- Delivery: each proven+gated+asserted nag text is emitted on `nag_check.py`'s
  stdout, which the cron job's explicit `delivery.to`
  (`${TELEGRAM_CHAT_ID_PRODUCTIVITY}:topic:${OPENCLAW_TOPIC_PRODUCTIVITY_STANDUP}`,
  topic 2) announces. A nag is counted as sent only once its text is collected
  for that announce — the script never logs `nag_sent` while delivering nothing.
- `nag_check.py` is READ-ONLY on the board; all board mutations happen through the
  reactive command path (`/done`, `/reschedule`).
- Body-double check-ins are ephemeral one-shot crons with `deleteAfterRun:true`
  and an explicit proven `delivery.to` + `agentId` set at session-start time.

## Agent Invocation Guidance

Use explicit, workspace-relative paths when running commands from agents:

```bash
python3 <workspace>/<skill>/scripts/standup.py
python3 <workspace>/<skill>/scripts/tasks.py list
```

## References Index

Detailed docs moved to `references/`:

- `references/setup-and-config.md` — environment variables, defaults, setup flow
- `references/commands.md` — command catalog and examples
- `references/obsidian-and-dataview.md` — task structures, plugins, Dataview snippets
- `references/eod-sync.md` — EOD sync + weekly transclusion behavior
- `references/migration.md` — legacy migration and compatibility notes
- `references/task-format.md` — legacy task format spec

## Compatibility Notes

- Active task mutations require canonical `task_id::` values; fallback IDs are
  diagnostics only.
- `scripts/eod_sync.py` is report-only by default. `--apply` is a legacy Weekly
  TODO checkbox helper and does not complete canonical tasks.
- Legacy file fallback (`TASK_TRACKER_LEGACY_FILE`) is still supported.
- Migration guidance remains available in `references/migration.md`.
