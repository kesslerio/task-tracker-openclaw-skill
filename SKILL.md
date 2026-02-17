---
name: task-tracker
description: "Personal task management with daily standups and weekly reviews. Supports both Work and Personal tasks from Obsidian. Use when: (1) User says 'daily standup' or asks what's on my plate, (2) User says 'weekly review' or asks about last week's progress, (3) User wants to add/update/complete tasks, (4) User asks about blockers or deadlines, (5) User shares meeting notes and wants tasks extracted, (6) User asks 'what's due this week' or similar."
homepage: https://github.com/kesslerio/task-tracker-openclaw-skill
metadata: {"openclaw":{"emoji":"📋","requires":{"env":["TASK_TRACKER_WORK_FILE","TASK_TRACKER_PERSONAL_FILE"]},"install":[{"id":"verify-paths","kind":"check","label":"Verify task file paths are configured"}]}}
---

<div align="center">

![Task Tracker](https://img.shields.io/badge/Task_Tracker-OpenClaw_skill-blue?style=for-the-badge&logo=checklist)
![Python](https://img.shields.io/badge/Python-3.10+-yellow?style=flat-square&logo=python)
![Status](https://img.shields.io/badge/Status-Production-green?style=flat-square)
![Issues](https://img.shields.io/badge/Issues-0-black?style=flat-square)
![Last Updated](https://img.shields.io/badge/Last_Updated-Jan_2026-orange?style=flat-square)

**Personal task management with daily standups and weekly reviews**

[Homepage](https://github.com/kesslerio/task-tracker-openclaw-skill) • [Trigger Patterns](#what-this-skill-does) • [Commands](#commands-reference)

</div>

---

# Task Tracker

A personal task management skill for daily standups and weekly reviews. Tracks work and personal tasks from your Obsidian vault (or standalone markdown files), surfaces priorities, and manages blockers.

---

## What This Skill Does

1. **Lists tasks** - Shows what's on your plate, filtered by priority, status, or deadline
2. **Daily standup** - Shows today's #1 priority, blockers, and what was completed (Work & Personal)
3. **Weekly review** - Summarizes last week, archives done items, plans this week
4. **Add tasks** - Create new tasks with priority and due date
5. **Complete tasks** - Mark tasks as done (logged to daily notes, removed from board)
6. **Extract from notes** - Pull action items from meeting notes
7. **Dual support** - Separate Work and Personal task workflows

---

## Configuration

Configure paths via environment variables in your shell profile or `.openclaw/.env`:

```bash
# Recommended: Override the default work task file path
export TASK_TRACKER_WORK_FILE="$HOME/Obsidian/03-Areas/Work/Work Tasks.md"

# Optional: Personal task file (required only for --personal commands)
export TASK_TRACKER_PERSONAL_FILE="$HOME/Obsidian/03-Areas/Personal/Personal Tasks.md"

# Optional: Custom archive location
export TASK_TRACKER_ARCHIVE_DIR="$HOME/clawd/memory/work"

# Optional: Legacy fallback (if Obsidian files don't exist)
export TASK_TRACKER_LEGACY_FILE="$HOME/clawd/memory/work/TASKS.md"

# Optional: Daily notes directory for completion logging (YYYY-MM-DD.md files)
export TASK_TRACKER_DAILY_NOTES_DIR="$HOME/Obsidian/06-Daily"
```

**Default paths (used when env vars are not set):**
- Work: `~/Obsidian/03-Areas/Work/Work Tasks.md`
- Personal: `~/Obsidian/03-Areas/Personal/Personal Tasks.md`
- Legacy: `~/clawd/memory/work/TASKS.md`

Setting `TASK_TRACKER_WORK_FILE` explicitly is recommended — the defaults assume an Obsidian vault layout.

---

## Obsidian Setup

This skill reads tasks directly from markdown files. Works best with Obsidian but any markdown editor works.

### Required Obsidian Plugins

| Plugin | Purpose | Required? |
|--------|---------|-----------|
| **Dataview** | TASK queries in daily notes | ✅ Yes |
| **Templater** | Auto-populate daily note templates | Optional |
| **Periodic Notes** | Daily/weekly note templates | Optional |
| **[Tasks](https://github.com/obsidian-tasks-group/obsidian-tasks)** | Advanced task management | Optional (recommended) |

> **Note:** The Tasks plugin provides a richer task format with `📅` due dates, `🔺` priorities, and `✅` completion dates. This skill supports both the emoji format (below) and the Tasks plugin format.

### Task Format

This skill supports **two task formats**:

#### 1. Tasks Plugin Format (Recommended)

If you use the [Obsidian Tasks](https://github.com/obsidian-tasks-group/obsidian-tasks) plugin:

```markdown
- [ ] **Task name** 📅 2026-01-22 🔺 #tag
- [x] Completed task ✅ 2026-01-20 🔼
```

**Features:**
- `📅 YYYY-MM-DD` — Due date
- `🔺` — Urgent (maps to Q1)
- `⏫` — High priority (maps to Q1)
- `🔼` — Medium priority (maps to Q2)
- `🔽`/`⏬` — Low priority (maps to backlog)
- `✅ YYYY-MM-DD` — Completion timestamp
- `#tag` — Department/category tags

**Sub-sections as departments:**
```markdown
### 👥 Hiring #hiring
- [ ] Post to Indeed 📅 2026-02-17 🔺
```

#### 2. Emoji Date Format (Legacy/Dataview)

Tasks use the **emoji date format** for Dataview compatibility:

```markdown
- [ ] **Task name** 🗓️2026-01-22 area:: Sales
  - Additional notes here
```

**Inline Fields (Legacy format):**

| Field | Purpose | Example |
|-------|---------|---------|
| `🗓️YYYY-MM-DD` | Due date | `🗓️2026-01-22` |
| `area::` | Category/area | `area:: Sales` |
| `goal::` | Weekly goal link | `goal:: [[2026-W04]]` |
| `owner::` | Task owner | `owner:: Sarah` |

### File Structure (Eisenhower Matrix)

```markdown
# Work Tasks

## 🔴 Q1: Do Now (Urgent & Important)

> Max 5 tasks. If overloaded, triage to Q2 or delegate.

- [ ] **Critical task** 🗓️2026-01-22 area:: Operations

## 🟡 Q2: Schedule (Important, Not Urgent)

> Deep work, strategic tasks. Schedule on calendar.

- [ ] **Strategic task** 🗓️2026-01-26 area:: Planning

## 🟠 Q3: Waiting (Blocked on External)

> Tasks waiting on others or external factors.

- [ ] **Blocked task** owner:: Sarah

## 👥 Team Tasks (Monitor/Check-in)

> Delegated tasks you're monitoring.

- [ ] **Team member's task** owner:: Alex

## ⚪ Q4: Backlog (Someday/Maybe)

> Low priority, not scheduled.

- [ ] **Future idea**
```

### Personal Tasks Structure

```markdown
# Personal Tasks

## 🔴 Must Do Today
- [ ] **Urgent personal task** 🗓️2026-01-22

## 🟡 Should Do This Week
- [ ] **Important task** 🗓️2026-01-26

## 🟠 Waiting On
- [ ] **Waiting for response**

## ⚪ Backlog
- [ ] **Someday task**
```

---

## Quick Start

### List Work Tasks
```bash
python3 scripts/tasks.py list

# Due today
python3 scripts/tasks.py list --due today

# By priority
python3 scripts/tasks.py list --priority high
```

### List Personal Tasks
```bash
python3 scripts/tasks.py --personal list

# Due today
python3 scripts/tasks.py --personal list --due today
```

### Daily Standup
```bash
# Work standup
python3 scripts/standup.py

# Personal standup
python3 scripts/personal_standup.py
```

### Weekly Review
```bash
python3 scripts/weekly_review.py
```

---

## Agent Integration

Use explicit paths with a `{baseDir}` variable when invoking scripts from agents.

Example:
```bash
python3 {baseDir}/scripts/standup.py
```

Available direct script commands:
```bash
python3 {baseDir}/scripts/standup.py
python3 {baseDir}/scripts/personal_standup.py
python3 {baseDir}/scripts/weekly_review.py
python3 {baseDir}/scripts/tasks.py list
python3 {baseDir}/scripts/tasks.py --personal list
python3 {baseDir}/scripts/tasks.py add "Task title" --priority high --due 2026-01-23
python3 {baseDir}/scripts/tasks.py done "task query"
python3 {baseDir}/scripts/tasks.py blockers
python3 {baseDir}/scripts/extract_tasks.py --from-text "Meeting notes"
```

Available heartbeat wrapper commands:
```bash
bash {baseDir}/scripts/task-shortcuts.sh daily
bash {baseDir}/scripts/task-shortcuts.sh weekly
bash {baseDir}/scripts/task-shortcuts.sh done24h
bash {baseDir}/scripts/task-shortcuts.sh done7d
```

---

## Commands Reference

### List Tasks
```bash
# All tasks
tasks.py list
tasks.py --personal list

# Only high priority
tasks.py list --priority high
tasks.py --personal list --priority high

# Due today or this week
tasks.py list --due today
tasks.py list --due this-week

# Only blocked
tasks.py blockers
```

### Add Task
```bash
# Work task
tasks.py add "Draft project proposal" --priority high --due 2026-01-23

# Personal task
tasks.py --personal add "Call mom" --priority high --due 2026-01-22

# With area
tasks.py add "Review budget" --priority medium --due 2026-01-25 --area Finance
```

### Complete Task
```bash
tasks.py done "proposal"  # Fuzzy match - finds "Draft project proposal"
tasks.py --personal done "call mom"
```

### Show Blockers
```bash
tasks.py blockers              # All blocking tasks
tasks.py blockers --person sarah  # Only blocking Sarah
```

### Extract from Meeting Notes
```bash
extract_tasks.py --from-text "Meeting: discuss Q1 planning, Sarah to own budget review"
# Outputs: tasks.py add "Discuss Q1 planning" --priority medium
#          tasks.py add "Sarah to own budget review" --owner sarah
```

---

## Priority Levels (Work)

| Icon | Meaning | When to Use |
|------|---------|-------------|
| 🔴 **Q1** | Critical, blocking, deadline-driven | Revenue impact, blocking others |
| 🟡 **Q2** | Important but not urgent | Reviews, feedback, planning |
| 🟠 **Q3** | Waiting on external | Blocked by others |
| 👥 **Team** | Monitor team tasks | Delegated, check-in only |
| ⚪ **Backlog** | Someday/maybe | Low priority |

## Priority Levels (Personal)

| Icon | Meaning |
|------|---------|
| 🔴 **Must Do Today** | Non-negotiable today |
| 🟡 **Should Do This Week** | Important, flexible timing |
| 🟠 **Waiting On** | Blocked by others/external |
| ⚪ **Backlog** | Someday/maybe |

---

## Automation (Cron)

Set up cron jobs for automated standups:

| Job | Schedule | Command |
|-----|----------|---------|
| Daily Work Standup | Weekdays 8:30 AM | `python3 scripts/standup.py` |
| Daily Personal Standup | Daily 8:00 AM | `python3 scripts/personal_standup.py` |
| Weekly Review | Mondays 9:00 AM | `python3 scripts/weekly_review.py` |

Example OpenClaw cron:
```bash
openclaw cron add \
  --name "Daily Work Standup" \
  --cron "30 8 * * 1-5" \
  --tz "America/Los_Angeles" \
  --session "isolated" \
  --message "Run work standup" \
  --channel "telegram:YOUR_GROUP_ID" \
  --deliver
```

---

## Natural Language Triggers

| You Say | Skill Does |
|---------|-----------|
| "daily standup" | Runs work standup, posts to channel |
| "personal standup" | Runs personal standup, posts to channel |
| "weekly review" | Runs weekly review, posts summary |
| "what's on my plate?" | Lists all work tasks |
| "personal tasks" | Lists all personal tasks |
| "what's blocking Sarah?" | Shows tasks blocking Sarah |
| "mark proposal done" | Completes matching work task |
| "what's due this week?" | Lists work tasks due this week |
| "add task: X" | Adds work task X |
| "add personal task: X" | Adds personal task X |

---

## Dataview Integration

Add these queries to your Obsidian daily note template:

### Today's Tasks

```dataview
TASK
FROM "03-Areas/Work/Work Tasks.md"
WHERE due = date("today")
SORT due ASC
LIMIT 10
```

### This Week

```dataview
TASK
FROM "03-Areas/Work/Work Tasks.md"
WHERE due > date("today") AND due <= date("today") + dur(7 days)
SORT due ASC
LIMIT 10
```

### Completed Today

```dataview
TASK
FROM "03-Areas/Work/Work Tasks.md"
WHERE completed AND due = date("today")
SORT file.mtime DESC
```

**Note:** Adjust the `FROM` path to match your Obsidian vault structure.

---

## Troubleshooting

**"Tasks file not found"**
```bash
# Configure your paths
export TASK_TRACKER_WORK_FILE="$HOME/path/to/Work Tasks.md"
export TASK_TRACKER_PERSONAL_FILE="$HOME/path/to/Personal Tasks.md"
```

**Tasks not showing up**
- Check task format uses `- [ ] **Task name**`
- Verify section headers start with emoji: `## 🔴`, `## 🟡`, etc.
- Run `tasks.py list` to debug parsing

**Date filtering issues**
- Due dates must use emoji format: `🗓️YYYY-MM-DD`
- Examples: `🗓️2026-01-22`, `🗓️2026-12-31`

**Dataview queries empty**
- Install Dataview community plugin
- Adjust `FROM` path to match your vault structure
- Reload Obsidian after installing plugin

---

## Files

| File | Purpose |
|------|---------|
| `scripts/tasks.py` | Main CLI - list, add, done, blockers (supports --personal) |
| `scripts/standup.py` | Work daily standup generator |
| `scripts/personal_standup.py` | Personal daily standup generator |
| `scripts/weekly_review.py` | Weekly review generator |
| `scripts/extract_tasks.py` | Extract tasks from meeting notes |
| `scripts/utils.py` | Shared utilities |
| `assets/templates/` | Template task files |

---

## Migration from Legacy Format

If you were using `~/clawd/memory/work/TASKS.md`:

1. **Set environment variables** pointing to your new files
2. **Convert dates** from `Due: ASAP` to `🗓️2026-01-22`
3. **Convert sections** from `## High Priority` to `## 🔴 Q1: Do Now`
4. **Remove inline metadata** - convert `  - Due: 2026-01-22` to `🗓️2026-01-22`

The skill auto-detects format and falls back to legacy if Obsidian files don't exist.
