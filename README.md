# Task Tracker Skill for OpenClaw

Personal task management with daily standups and weekly reviews.

## Features

- 📋 **Task Management:** Create, list, complete, and archive tasks
- 📊 **Daily Standup:** See today's priorities, blockers, and recent completions
- 📅 **Weekly Review:** Summarize progress and plan the week ahead
- 🏷️ **Priority Levels:** High (🔴), Medium (🟡), Low/Delegated (🟢)
- ⏰ **Due Dates:** Track deadlines and filter by due date
- 🚧 **Blockers:** Identify tasks blocking team members
- 📱 **Telegram Integration:** Slash commands for quick access
- 🤖 **Automated Briefings:** Optional cron jobs for daily/weekly summaries

## Quick Start

```bash
# Daily standup
python3 ~/clawd/skills/task-tracker/scripts/standup.py

# List all tasks
python3 ~/clawd/skills/task-tracker/scripts/tasks.py list

# Add a task
python3 ~/clawd/skills/task-tracker/scripts/tasks.py add "Task description" --priority high --due 2026-01-25

# Mark task done (fuzzy match)
python3 ~/clawd/skills/task-tracker/scripts/tasks.py done "task keyword"

# Weekly review
python3 ~/clawd/skills/task-tracker/scripts/weekly_review.py
```

## Installation

1. Clone to your OpenClaw skills directory:
   ```bash
   git clone https://github.com/kesslerio/task-tracker-openclaw-skill.git \
       ~/clawd/skills/task-tracker
   ```

2. Create tasks file from template:
   ```bash
   cp ~/clawd/skills/task-tracker/assets/templates/TASKS.md \
       ~/clawd/memory/work/TASKS.md
   ```

3. (Optional) Set up Telegram slash commands - see [TELEGRAM.md](TELEGRAM.md)

## Task File Format

Tasks are stored in `~/clawd/memory/work/TASKS.md` using this format:

```markdown
## 🔴 High Priority (This Week)

- [ ] **Task title** — Brief description
  - Owner: sarah
  - Due: 2026-01-29
  - Status: Todo
  - Blocks: teammate (reason)
```

### Priority Levels
- **🔴 High:** Blocking others, critical deadline, revenue impact
- **🟡 Medium:** Important but not urgent
- **🟢 Low/Delegated:** Monitoring, no deadline pressure

### Statuses
- `Todo` → `In Progress` → `Done`
- `Blocked` (waiting on external)
- `Waiting` (delegated, monitoring)

## Commands

### Daily Standup
```bash
python3 scripts/standup.py
python3 scripts/standup.py --compact-json   # DONEs/Calendar DOs/DOs API shape
```

### State transitions
```bash
python3 scripts/tasks.py state pause "task title" --until 2026-03-01
python3 scripts/tasks.py state delegate "task title" --to Alex --followup 2026-03-01
python3 scripts/tasks.py state backlog "task title"
python3 scripts/tasks.py state drop "task title"
python3 scripts/tasks.py promote-from-backlog --cap 3
python3 scripts/tasks.py review-backlog --stale-days 45 --json
```

Output:
- 🎯 #1 Priority
- ⏰ Due today
- 🔴 High priority tasks
- ✅ Recently completed

### Weekly Review
```bash
python3 scripts/weekly_review.py
```

Output:
- Last week's completions
- Tasks pushed to this week
- This week's priorities
- Automatically archives done tasks

### Task Operations

**List tasks:**
```bash
python3 scripts/tasks.py list                          # All tasks
python3 scripts/tasks.py list --priority high          # High priority only
python3 scripts/tasks.py list --status done            # Completed tasks
python3 scripts/tasks.py list --due today              # Due today
python3 scripts/tasks.py list --due this-week          # Due this week
python3 scripts/tasks.py list --owner your-name           # My tasks
python3 scripts/tasks.py list --completed-since 24h    # Done last 24h
python3 scripts/tasks.py list --completed-since 7d     # Done last week
```

**Add task:**
```bash
python3 scripts/tasks.py add "Task description" \
  --priority high \
  --due 2026-01-29 \
  --owner your-name \
  --blocks "teammate"
```

**Complete task:**
```bash
python3 scripts/tasks.py done "task keyword"
```

Uses fuzzy matching - just type a few words from the task title.

**Show blockers:**
```bash
python3 scripts/tasks.py blockers                # All blockers
python3 scripts/tasks.py blockers --person lilla # Blocking specific person
```

**Archive completed tasks:**
```bash
python3 scripts/tasks.py archive
```

Aggregates completions from daily notes into quarterly `ARCHIVE-YYYY-QN.md`.

## Telegram Slash Commands

Optional integration for Telegram users. See [TELEGRAM.md](TELEGRAM.md) for setup.

**Commands:**
- `/daily` - Daily standup
- `/weekly` - Weekly priorities
- `/done24h` - Completed last 24 hours
- `/done7d` - Completed last 7 days

## Cron Jobs (Optional)

Set up automated standups and reviews:

```bash
# Daily standup (8:30 AM PT, weekdays)
openclaw cron add \
  --name "Daily Standup" \
  --schedule "30 8 * * 1-5" \
  --timezone "America/Los_Angeles" \
  --command "python3 ~/clawd/skills/task-tracker/scripts/standup.py"

# Weekly review (9:00 AM PT, Mondays)
openclaw cron add \
  --name "Weekly Review" \
  --schedule "0 9 * * 1" \
  --timezone "America/Los_Angeles" \
  --command "python3 ~/clawd/skills/task-tracker/scripts/weekly_review.py"
```

## Workflow

1. **Morning:** Daily standup surfaces #1 priority
2. **Throughout day:** Update status as work progresses
3. **After meetings:** Extract tasks with `extract_tasks.py`
4. **End of week:** Weekly review archives done, plans next week

## Files

```
task-tracker/
├── README.md              # This file
├── SKILL.md               # OpenClaw skill documentation
├── TELEGRAM.md            # Telegram integration guide
├── scripts/
│   ├── tasks.py           # Task CRUD operations
│   ├── standup.py         # Daily standup generator
│   ├── weekly_review.py   # Weekly review + archiving
│   ├── extract_tasks.py   # Extract tasks from notes
│   ├── telegram-commands.sh # Telegram slash command wrapper
│   └── init.py            # Initialize tasks file
├── assets/
│   └── templates/
│       └── TASKS.md       # Task file template
└── references/
    └── task-format.md     # Task format specification
```

## Integration with OpenClaw

The agent automatically:
- Recognizes task-related questions ("What's my #1 priority?")
- Runs standup when asked ("daily standup")
- Updates tasks when you say "mark X done"
- Filters tasks by criteria ("show high priority tasks due this week")

## Dependencies

- Python 3.10+
- OpenClaw (for cron/messaging integration)

## License

Apache 2.0 - See [LICENSE](LICENSE) file for details.

## Related Skills

- **[finance-news](https://github.com/kesslerio/finance-news-openclaw-skill):** AI-powered market briefings with multi-source aggregation (WSJ, Barron's, CNBC), portfolio tracking, and automated WhatsApp delivery in German/English
- **[oura-analytics](https://github.com/kesslerio/openclaw-oura-skill):** Sleep and health tracking
- **[session-logs](https://github.com/kesslerio/openclaw-session-logs-skill):** Search conversation history
- **[task-tracker](https://github.com/kesslerio/task-tracker-openclaw-skill):** This skill
