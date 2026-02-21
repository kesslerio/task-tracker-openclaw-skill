# Setup and Configuration

This skill is environment-first: configure file paths with env vars, then run scripts.

## Required

```bash
export TASK_TRACKER_WORK_FILE="$HOME/path/to/Work Tasks.md"
```

For personal commands (`--personal`), also set:

```bash
export TASK_TRACKER_PERSONAL_FILE="$HOME/path/to/Personal Tasks.md"
```

## Optional

```bash
export TASK_TRACKER_ARCHIVE_DIR="$HOME/path/to/archive"
export TASK_TRACKER_LEGACY_FILE="$HOME/path/to/TASKS.md"
export TASK_TRACKER_DAILY_NOTES_DIR="$HOME/path/to/Daily"
export TASK_TRACKER_WEEKLY_TODOS="$HOME/path/to/Weekly TODOs.md"
```

## Defaults

If env vars are not set, task-tracker falls back to:

- Work: `$HOME/path/to/Work Tasks.md`
- Personal: `$HOME/path/to/Personal Tasks.md`
- Legacy: `~/clawd/memory/work/TASKS.md`

Defaults are convenient but user-specific; explicit env vars are recommended.

## Run location

```bash
cd <workspace>/<skill>
# Example: cd ~/projects/skills/shared/task-tracker
```

Then run scripts with `python3 scripts/...`.

## Troubleshooting

### "Tasks file not found"
- Verify `TASK_TRACKER_WORK_FILE` and/or `TASK_TRACKER_PERSONAL_FILE`
- Confirm files exist and are readable

### Tasks not appearing
- Check markdown checkbox format: `- [ ]`
- Verify expected section headers in your task file

### Date filtering issues
- Use supported due date formats for your chosen task syntax (see `task-format.md` and `obsidian-and-dataview.md`)
