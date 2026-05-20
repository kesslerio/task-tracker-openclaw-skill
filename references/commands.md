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
python3 scripts/tasks.py identity-audit
python3 scripts/tasks.py identity-repair
python3 scripts/tasks.py identity-repair --apply
python3 scripts/tasks.py done "tsk_example"
python3 scripts/tasks.py --personal done "tsk_personal"
```

`done` and other active mutations require canonical `task_id::` values. Title
queries are blocked because duplicate titles and board reorder can mutate the
wrong task.

### State transitions

```bash
python3 scripts/tasks.py state pause "tsk_example" --until 2026-03-01
python3 scripts/tasks.py state delegate "tsk_example" --to Alex --followup 2026-03-01
python3 scripts/tasks.py state backlog "tsk_example"
python3 scripts/tasks.py state drop "tsk_example"
```

### Backlog workflows

```bash
python3 scripts/tasks.py promote-from-backlog --cap 3
python3 scripts/tasks.py review-backlog --stale-days 45 --json
```

### Standup/review extras

```bash
python3 scripts/standup.py --compact-json
# Schema: references/standup-compact-schema-v1.md
python3 scripts/tasks.py list --completed-since 24h
python3 scripts/tasks.py list --completed-since 7d
python3 scripts/tasks.py done-scan --window 24h --json
python3 scripts/tasks.py daily-links --window today --json
python3 scripts/tasks.py calendar sync --json
python3 scripts/tasks.py calendar resolve --window today --json
```

### Task primitives (issue #88)

```bash
python3 scripts/tasks.py standup-summary
python3 scripts/tasks.py weekly-review-summary --week 2026-W08
python3 scripts/tasks.py weekly-review-summary --start 2026-02-16 --end 2026-02-22
python3 scripts/tasks.py ingest-daily-log --file /tmp/done-log.md
cat /tmp/done-log.md | python3 scripts/tasks.py ingest-daily-log
python3 scripts/tasks.py calendar-sync
python3 scripts/tasks.py completion-candidates list
python3 scripts/tasks.py completion-candidates add \
  --source-type daily-note \
  --source-pointer 2026-05-20.md:12 \
  --summary "Shipped milestone" \
  --task-id tsk_example
python3 scripts/tasks.py completion-candidates decide <dedupe-key> confirmed
```

All primitives return JSON with a stable envelope:

```json
{
  "schema_version": "v1",
  "command": "..."
}
```

Detailed shape: `references/task-primitives-schema-v1.md`.

## Wrapper shortcuts

```bash
bash scripts/task-shortcuts.sh daily
bash scripts/task-shortcuts.sh standup   # alias of daily
bash scripts/task-shortcuts.sh weekly
bash scripts/task-shortcuts.sh done24h
bash scripts/task-shortcuts.sh done7d
bash scripts/task-shortcuts.sh tasks     # quick priorities view
```
