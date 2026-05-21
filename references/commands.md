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

`done` requires canonical `task_id::` values. Title
queries are blocked because duplicate titles and board reorder can mutate the
wrong task.

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
```

All primitives return JSON with a stable envelope:

```json
{
  "schema_version": "v1",
  "command": "..."
}
```

Detailed shape: `references/task-primitives-schema-v1.md`.

### Completion evidence inbox

```bash
python3 scripts/tasks.py completion-candidates scan --file /tmp/done-log.md
cat /tmp/done-log.md | python3 scripts/tasks.py completion-candidates scan
python3 scripts/tasks.py completion-candidates scan --date 2026-05-21
python3 scripts/tasks.py completion-candidates list
python3 scripts/tasks.py completion-candidates show cand_example
python3 scripts/tasks.py completion-candidates confirm cand_example --task-id tsk_example
python3 scripts/tasks.py completion-candidates reject cand_example \
  --reason "not actually done"
python3 scripts/tasks.py completion-candidates duplicate cand_example --of cand_original
python3 scripts/tasks.py completion-candidates snooze cand_example --until 2026-05-28
```

Scanning creates durable candidate events in the ledger, but never mutates the
task board or completion log. Confirmation is the only task-changing candidate
action, and it completes through the same canonical-ID `done` path. Exact
`task_id::` evidence can confirm directly; title, fuzzy, and fallback-only
suggestions require `--task-id`.

## Wrapper shortcuts

```bash
bash scripts/task-shortcuts.sh daily
bash scripts/task-shortcuts.sh standup   # alias of daily
bash scripts/task-shortcuts.sh weekly
bash scripts/task-shortcuts.sh done24h
bash scripts/task-shortcuts.sh done7d
bash scripts/task-shortcuts.sh tasks     # quick priorities view
```
