---
name: task-tracker
description: "Personal task management with daily standups and weekly reviews. Use when: (1) User says 'daily standup' or asks what's on their plate, (2) User says 'weekly review' or asks about last week's progress, (3) User wants to add/update/complete tasks, (4) User asks about blockers or deadlines, (5) User shares meeting notes and wants tasks extracted, (6) User asks 'what's due this week' or similar."
homepage: https://github.com/kesslerio/task-tracker-clawdbot-skill
metadata: {"clawdbot":{"emoji":"📋","requires":{"files":["~/clawd/memory/work/TASKS.md"]},"install":[{"id":"init","kind":"script","script":"python3 scripts/init.py","label":"Initialize TASKS.md from template"}]}}
---

<div align="center">

![Task Tracker](https://img.shields.io/badge/Task_Tracker-Clawdbot_skill-blue?style=for-the-badge&logo=checklist)
![Python](https://img.shields.io/badge/Python-3.10+-yellow?style=flat-square&logo=python)
![Status](https://img.shields.io/badge/Status-Production-green?style=flat-square)
![Issues](https://img.shields.io/badge/Issues-0-black?style=flat-square)
![Last Updated](https://img.shields.io/badge/Last_Updated-Jan_2026-orange?style=flat-square)

**Personal task management with daily standups and weekly reviews**

[Homepage](https://github.com/kesslerio/task-tracker-clawdbot-skill) • [Trigger Patterns](#what-this-skill-does) • [Commands](#commands-reference)

</div>

---

# Task Tracker

A personal task management skill for daily standups and weekly reviews. Tracks work tasks, surfaces priorities, and manages blockers.

---

## What This Skill Does

1. **Lists tasks** - Shows what's on your plate, filtered by priority, status, or deadline
2. **Daily standup** - Shows today's #1 priority, blockers, and what was completed
3. **Weekly review** - Summarizes last week, archives done items, plans this week
4. **Add tasks** - Create new tasks with priority and due date
5. **Complete tasks** - Mark tasks as done
6. **Extract from notes** - Pull action items from meeting notes

---

## File Structure

```
~/clawd/memory/work/
├── TASKS.md              # Active tasks (source of truth)
├── ARCHIVE-2026-Q1.md    # Completed tasks by quarter
└── WORKFLOW.md           # Workflow documentation
```

**TASKS.md format:**
```markdown
# Work Tasks

## 🔴 High Priority (This Week)
- [ ] **Set up Apollo.io** — Access for Lilla
  - Due: ASAP
  - Blocks: Lilla (podcast outreach)

## 🟡 Medium Priority (This Week)
- [ ] **Review newsletter concept** — Figma design
  - Due: Before Feb 1

## ✅ Done
- [x] **Set up Calendly** — Configured with Zoom
```

---

## Quick Start

### View Your Tasks
```bash
python3 ~/clawd/skills/task-tracker/scripts/tasks.py list
```

### Daily Standup
```bash
python3 ~/clawd/skills/task-tracker/scripts/standup.py
```

### Weekly Review
```bash
python3 ~/clawd/skills/task-tracker/scripts/weekly_review.py
```

---

## Commands Reference

### List Tasks
```bash
# All tasks
tasks.py list

# Only high priority
tasks.py list --priority high

# Only blocked
tasks.py list --status blocked

# Due today or this week
tasks.py list --due today
tasks.py list --due this-week
```

### Add Task
```bash
# Simple
tasks.py add "Create IMCAS form"

# With details
tasks.py add "Create IMCAS form" \
  --priority high \
  --due "Before Jan 29" \
  --blocks "Lilla (IMCAS conference)"
```

### Complete Task
```bash
tasks.py done "IMCAS"  # Fuzzy match - finds "Create IMCAS form"
```

### Show Blockers
```bash
tasks.py blockers              # All blocking tasks
tasks.py blockers --person lilla  # Only blocking Lilla
```

### Extract from Meeting Notes
```bash
extract_tasks.py --from-text "Meeting: discuss Apollo setup, Lilla to own"
# Outputs: tasks.py add "Discuss Apollo setup" --priority medium
#          tasks.py add "Lilla to own" --owner lilla
```

---

## Priority Levels

| Icon | Meaning | When to Use |
|------|---------|-------------|
| 🔴 **High** | Critical, blocking, deadline-driven | Revenue impact, blocking others |
| 🟡 **Medium** | Important but not urgent | Reviews, feedback, planning |
| 🟢 **Low** | Monitoring, delegated | Waiting on others, backlog |

---

## Status Workflow

```
Todo → In Progress → Done
      ↳ Blocked (waiting on external)
      ↳ Waiting (delegated, monitoring)
```

---

## Automation (Cron)

| Job | When | What |
|-----|------|------|
| Daily Standup | Weekdays 8:30 AM | Posts to Telegram Journaling group |
| Weekly Review | Mondays 9:00 AM | Posts summary, archives done items |

---

## Natural Language Triggers

| You Say | Skill Does |
|---------|-----------|
| "daily standup" | Runs standup.py, posts to Journaling |
| "weekly review" | Runs weekly_review.py, posts summary |
| "what's on my plate?" | Lists all tasks |
| "what's blocking Lilla?" | Shows tasks blocking Lilla |
| "mark IMCAS done" | Completes matching task |
| "what's due this week?" | Lists tasks due this week |
| "add task: X" | Adds task X to TASKS.md |
| "extract tasks from: [notes]" | Parses notes, outputs add commands |

---

## Examples

**Morning check-in:**
```
$ python3 scripts/standup.py

📋 Daily Standup — Tuesday, January 21

🎯 #1 Priority: Set up Apollo.io access for Lilla
   ↳ Blocking: Lilla (podcast outreach)

⏰ Due Today:
  • Set up Apollo.io access for Lilla
  • Set up Lilla on Calendly

🔴 High Priority:
  • Create IMCAS lead capture form (due: Before Jan 29)
  • Post sales position job ad (due: ASAP)

✅ Recently Completed:
  • Add Lilla to Attio CRM
```

**Adding a task:**
```
$ python3 scripts/tasks.py add "Post sales job ad" --priority high --due ASAP

✅ Added task: Post sales job ad
```

**Extracting from meeting notes:**
```
$ python3 scripts/extract_tasks.py --from-text "Meeting: Lilla needs Apollo access, create IMCAS form before Jan 29"

# Extracted 2 task(s) from meeting notes
# Run these commands to add them:

tasks.py add "Apollo access for Lilla" --priority high
tasks.py add "Create IMCAS lead capture form" --priority high --due "Before Jan 29"
```

---

## Integration Points

- **Telegram Journaling group:** Standup/review summaries posted automatically
- **Obsidian:** Daily standups logged to `01-Daily/YYYY-MM-DD.md`
- **MEMORY.md:** Patterns and recurring blockers promoted during weekly reviews
- **Cron:** Automated standups and reviews

---

## Troubleshooting

**"Tasks file not found"**
```bash
# Create from template
python3 scripts/init.py
```

**Tasks not showing up**
- Check TASKS.md exists at `~/clawd/memory/work/TASKS.md`
- Verify task format (checkboxes `- [ ]`, headers `## 🔴`)
- Run `tasks.py list` to debug

**Date parsing issues**
- Due dates support: `ASAP`, `YYYY-MM-DD`, `Before Jan 29`, `Before IMCAS`
- `check_due_date()` handles common formats

---

## Files

| File | Purpose |
|------|---------|
| `scripts/tasks.py` | Main CLI - list, add, done, blockers, archive |
| `scripts/standup.py` | Daily standup generator |
| `scripts/weekly_review.py` | Weekly review generator |
| `scripts/extract_tasks.py` | Extract tasks from meeting notes |
| `scripts/utils.py` | Shared utilities (DRY) |
| `scripts/init.py` | Initialize new TASKS.md from template |
| `references/task-format.md` | Task format specification |
| `assets/templates/TASKS.md` | Template for new task files |
