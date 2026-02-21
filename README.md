# Task Tracker Skill for OpenClaw

Personal task management with daily standups and weekly reviews.

## Documentation Map

- **`SKILL.md`**: lean runtime entrypoint (when to use, quick start, core commands)
- **`references/setup-and-config.md`**: environment variables, defaults, troubleshooting
- **`references/commands.md`**: full command catalog
- **`references/obsidian-and-dataview.md`**: Obsidian patterns and Dataview snippets
- **`references/eod-sync.md`**: EOD sync and weekly embed refresh
- **`references/migration.md`**: migration + legacy compatibility notes
- **`references/task-format.md`**: legacy task format specification
- **`TELEGRAM.md`**: optional Telegram slash command setup

## Quick Start

```bash
git clone https://github.com/kesslerio/task-tracker-openclaw-skill.git <workspace>/<skill>
cd <workspace>/<skill>

# Required for work workflows
export TASK_TRACKER_WORK_FILE="$HOME/path/to/Work Tasks.md"

# Required only for --personal commands
export TASK_TRACKER_PERSONAL_FILE="$HOME/path/to/Personal Tasks.md"

python3 scripts/tasks.py list
python3 scripts/standup.py
python3 scripts/weekly_review.py
```

## What this skill provides

- Task list/add/done workflows for work and personal boards
- Daily standup summaries (`standup.py`, `personal_standup.py`)
- Weekly review + archive workflows (`weekly_review.py`, `archive.py`)
- State transitions and backlog hygiene (`tasks.py state ...`)
- Action extraction from notes (`extract_tasks.py`)
- End-of-day sync into weekly TODOs (`eod_sync.py`)
- Weekly transclusion refresh for Obsidian (`update_weekly_embeds.py`)

## Compatibility

This repository keeps existing scripts and command behavior intact. The recent docs refactor reorganizes guidance into `references/` and keeps legacy migration notes available.

## Development

```bash
# run tests
pytest -q

# enforce public-repo hygiene (paths, IDs, and default examples)
bash scripts/ci/check-public-hygiene.sh
```

If a hygiene match is intentional, add a targeted allowlist entry in
`scripts/ci/public-hygiene-allowlist.txt` using one of:

- `RULE|path/to/file`
- `RULE|path/to/file:line`

## License

Apache 2.0 - See [LICENSE](LICENSE).
