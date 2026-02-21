# Migration and Legacy Compatibility

This skill still supports legacy workflows while preferring env-configured file paths.

## Legacy support preserved

- `TASK_TRACKER_LEGACY_FILE` fallback is still supported.
- Existing command names and scripts are unchanged.

## Suggested migration flow

1. Set env vars for your current work/personal markdown files.
2. Keep legacy file available during transition.
3. Normalize task date/style gradually.
4. Validate with list + standup + weekly review commands.

## Migrating from `~/clawd/memory/work/TASKS.md`

- Set `TASK_TRACKER_WORK_FILE` and (if needed) `TASK_TRACKER_PERSONAL_FILE`
- Convert old date markers like `Due: ASAP` to your preferred supported format
- Move old section naming toward Q1/Q2/Q3/backlog structure if useful

## Verification checklist

```bash
python3 scripts/tasks.py list
python3 scripts/standup.py
python3 scripts/weekly_review.py
```

If output looks correct, migration is complete.
