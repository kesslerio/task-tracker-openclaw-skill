# EOD Sync and Weekly Embeds

The EOD workflow syncs completed items from daily notes into Weekly TODOs.

## Environment

```bash
export TASK_TRACKER_WEEKLY_TODOS="$HOME/path/to/Weekly TODOs.md"
export TASK_TRACKER_DAILY_NOTES_DIR="$HOME/path/to/Daily"
```

## EOD sync

```bash
python3 scripts/eod_sync.py --dry-run
python3 scripts/eod_sync.py
python3 scripts/eod_sync.py --date 2026-02-18
python3 scripts/eod_sync.py --verbose
```

Behavior:

- Reads `## ✅ Done` from the selected daily note
- Fuzzy-matches against open tasks in weekly TODOs
- Marks matched items complete as `- [x] ... ✅ YYYY-MM-DD`

Match thresholds:

- `>= 80%`: auto-sync
- `60-79%`: uncertain, manual review
- `< 60%`: skipped

## Weekly transclusion refresh

```bash
python3 scripts/update_weekly_embeds.py --dry-run
python3 scripts/update_weekly_embeds.py
python3 scripts/update_weekly_embeds.py --week 2026-02-17
```

This refreshes the `## 📊 Daily Progress` section in Weekly TODOs with Obsidian transclusion links.
