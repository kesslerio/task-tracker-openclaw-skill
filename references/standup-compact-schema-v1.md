# Standup compact-json schema v1

`python3 scripts/standup.py --compact-json` returns a stable schema for orchestration clients.

## Shape

- `schema_version` (string): always `"1"` for this schema.
- `dones` (array<string>): recently completed task titles.
- `calendar_dos` (array<object>): pending calendar action items.
  - `quick_id` (string): stable response ID (`c1`, `c2`, ...)
  - `title` (string)
  - `status` (string): `scheduled`
- `calendar_dones` (array<object>): completed calendar-linked items.
  - `quick_id` (string): (`cd1`, `cd2`, ...)
  - `title` (string)
  - `status` (string): `done`
- `dos` (array<object>): active to-do actions.
  - `quick_id` (string): (`d1`, `d2`, ...)
  - `title` (string)
  - `section` (string): `q1`, `q2`, or `q3`
- `links` (object): Obsidian links when daily-notes env is configured.
  - `today_daily_note` and `yesterday_daily_note`
    - `universal`: `https://obsidian.md/open?...`
    - `deep`: `obsidian://open?...`

## Backward compatibility

v1 keeps existing top-level blocks (`dones`, `calendar_dos`, `dos`) and adds explicit:

- `schema_version`
- `calendar_dones`
- `links`
- typed fields on list entries (`status`, `section`)

Older consumers reading only `dones` / `calendar_dos` / `dos` continue to work.
