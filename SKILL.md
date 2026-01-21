---
name: task-tracker
description: "Personal task management with daily standups and weekly reviews. Use when: (1) User says 'daily standup' or asks what's on their plate, (2) User says 'weekly review' or asks about last week's progress, (3) User wants to add/update/complete tasks, (4) User asks about blockers or deadlines, (5) User shares meeting notes and wants tasks extracted, (6) User asks 'what's due this week' or similar."
---

# Task Tracker

Personal task management for daily standups and weekly reviews.

## File Locations

| File | Path |
|------|------|
| Active tasks | `~/clawd/memory/work/TASKS.md` |
| Archive | `~/clawd/memory/work/ARCHIVE-YYYY-QN.md` |
| Workflow | `~/clawd/memory/work/WORKFLOW.md` |

## Quick Commands

### Daily Standup
```bash
python3 ~/clawd/skills/task-tracker/scripts/standup.py
```
Output: Today's priorities, blockers, yesterday's completions.

### Weekly Review
```bash
python3 ~/clawd/skills/task-tracker/scripts/weekly_review.py
```
Output: Last week's done, pushed items, this week's priorities. Archives completed tasks.

### Task Operations
```bash
# List all tasks
python3 ~/clawd/skills/task-tracker/scripts/tasks.py list

# List by priority/status
python3 ~/clawd/skills/task-tracker/scripts/tasks.py list --priority high
python3 ~/clawd/skills/task-tracker/scripts/tasks.py list --status blocked

# List by deadline
python3 ~/clawd/skills/task-tracker/scripts/tasks.py list --due today
python3 ~/clawd/skills/task-tracker/scripts/tasks.py list --due this-week

# Add task
python3 ~/clawd/skills/task-tracker/scripts/tasks.py add "Task description" --priority high --due 2026-01-29

# Complete task (fuzzy match)
python3 ~/clawd/skills/task-tracker/scripts/tasks.py done "apollo"

# Show blockers
python3 ~/clawd/skills/task-tracker/scripts/tasks.py blockers
python3 ~/clawd/skills/task-tracker/scripts/tasks.py blockers --person lilla
```

### Extract Tasks from Meeting Notes
```bash
python3 ~/clawd/skills/task-tracker/scripts/extract_tasks.py --from-text "Meeting notes..."
```

## Natural Language Mapping

| User Says | Action |
|-----------|--------|
| "daily standup" | Run standup.py, post to Journaling group |
| "weekly review" | Run weekly_review.py, post summary, archive done |
| "add task: X" | tasks.py add "X" |
| "what's blocking [person]?" | tasks.py blockers --person [person] |
| "mark [task] done" | tasks.py done "[task]" (fuzzy match) |
| "what's due this week?" | tasks.py list --due this-week |
| "show my tasks" | tasks.py list |
| "extract tasks from: [notes]" | extract_tasks.py --from-text "[notes]" |

## Task Format

See [references/task-format.md](references/task-format.md) for full specification.

```markdown
- [ ] **Task title** — Brief description
  - Owner: martin
  - Due: 2026-01-29
  - Status: Todo
  - Blocks: lilla (reason)
```

### Priority Levels
- 🔴 **High**: Blocking others, critical deadline, revenue impact
- 🟡 **Medium**: Important but not urgent
- 🟢 **Low/Delegated**: Monitoring, no deadline pressure

### Statuses
- `Todo` → `In Progress` → `Done`
- `Blocked` (waiting on external)
- `Waiting` (delegated, monitoring)

## Cron Jobs

| Job | Schedule | Action |
|-----|----------|--------|
| Daily Standup | Weekdays 8:30 AM | standup.py → Journaling group |
| Weekly Review | Mondays 9:00 AM | weekly_review.py → Journaling group |

## Workflow Integration

1. **After meetings:** Extract tasks with `extract_tasks.py`
2. **Morning:** Daily standup surfaces #1 priority
3. **Throughout day:** Update status as work progresses
4. **End of week:** Weekly review archives done, plans next week

## Output Destinations

- **Telegram Journaling group:** Standup/review summaries
- **Obsidian daily notes:** Logged via `journal-entry.sh`
- **MEMORY.md:** Patterns promoted from weekly reviews

## Telegram Integration

This skill supports custom slash commands for Telegram. See [TELEGRAM.md](TELEGRAM.md) for setup instructions.

**Quick setup:**
1. Add `customCommands` to `channels.telegram` in clawdbot.json
2. Update AGENTS.md with command recognition patterns
3. Restart gateway

**Available commands:** `/daily`, `/weekly`, `/done24h`, `/done7d`
