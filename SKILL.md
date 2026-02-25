---
name: task-tracker
description: "Personal task management with daily standups and weekly reviews. Supports both Work and Personal tasks from Obsidian. Use when: (1) User says 'daily standup' or asks what's on my plate, (2) User says 'weekly review' or asks about last week's progress, (3) User wants to add/update/complete tasks, (4) User asks about blockers or deadlines, (5) User shares meeting notes and wants tasks extracted, (6) User asks 'what's due this week' or similar."
homepage: https://github.com/kesslerio/task-tracker-openclaw-skill
metadata: {"openclaw":{"emoji":"📋","requires":{"env":["TASK_TRACKER_WORK_FILE","TASK_TRACKER_PERSONAL_FILE"]},"install":[{"id":"verify-paths","kind":"check","label":"Verify task file paths are configured"}]}}
---

# Task Tracker

Personal task management for work + personal workflows, with daily standups and weekly reviews.

## When to Use

Use this skill when the user asks to:

- Run a daily or personal standup
- Run a weekly review
- Add, list, update, or complete tasks
- Check blockers or due dates
- Extract actions from meeting notes
- Sync completed daily items to weekly todos

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
python3 scripts/tasks.py done "proposal"
python3 scripts/tasks.py --personal done "call mom"
```

### State transitions and backlog ops

```bash
python3 scripts/tasks.py state pause "task title" --until 2026-03-01
python3 scripts/tasks.py state delegate "task title" --to Alex --followup 2026-03-01
python3 scripts/tasks.py state backlog "task title"
python3 scripts/tasks.py state drop "task title"
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
```

### EOD sync + weekly embed refresh

```bash
python3 scripts/eod_sync.py --dry-run
python3 scripts/eod_sync.py
python3 scripts/update_weekly_embeds.py --dry-run
python3 scripts/update_weekly_embeds.py
```

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

- Existing scripts/commands are preserved; this is a docs structure refactor.
- Legacy file fallback (`TASK_TRACKER_LEGACY_FILE`) is still supported.
- Migration guidance remains available in `references/migration.md`.
