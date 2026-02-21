# Command Reference

Run from `<workspace>/<skill>`.

## Core scripts

```bash
python3 scripts/tasks.py list
python3 scripts/standup.py
python3 scripts/personal_standup.py
python3 scripts/weekly_review.py
python3 scripts/extract_tasks.py --from-text "Meeting notes"
```

## Tasks CLI

### List

```bash
python3 scripts/tasks.py list
python3 scripts/tasks.py --personal list
python3 scripts/tasks.py list --priority high
python3 scripts/tasks.py list --due today
python3 scripts/tasks.py list --due this-week
python3 scripts/tasks.py blockers
python3 scripts/tasks.py blockers --person sarah
```

### Add / Done

```bash
python3 scripts/tasks.py add "Draft project proposal" --priority high --due 2026-01-23
python3 scripts/tasks.py --personal add "Call mom" --priority high --due 2026-01-22
python3 scripts/tasks.py done "proposal"
python3 scripts/tasks.py --personal done "call mom"
```

### State transitions

```bash
python3 scripts/tasks.py state pause "task title" --until 2026-03-01
python3 scripts/tasks.py state delegate "task title" --to Alex --followup 2026-03-01
python3 scripts/tasks.py state backlog "task title"
python3 scripts/tasks.py state drop "task title"
```

### Backlog workflows

```bash
python3 scripts/tasks.py promote-from-backlog --cap 3
python3 scripts/tasks.py review-backlog --stale-days 45 --json
```

### Standup/review extras

```bash
python3 scripts/standup.py --compact-json
python3 scripts/tasks.py list --completed-since 24h
python3 scripts/tasks.py list --completed-since 7d
python3 scripts/tasks.py calendar sync --json
python3 scripts/tasks.py calendar resolve --window today --json
```

## Wrapper shortcuts

```bash
bash scripts/task-shortcuts.sh daily
bash scripts/task-shortcuts.sh weekly
bash scripts/task-shortcuts.sh done24h
bash scripts/task-shortcuts.sh done7d
```
