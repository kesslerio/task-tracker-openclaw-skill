# Task Primitives Schema v1

All `scripts/tasks.py` task primitives use a stable top-level envelope:

```json
{
  "schema_version": "v1",
  "command": "<primitive-name>"
}
```

## `standup-summary`

```json
{
  "schema_version": "v1",
  "command": "standup-summary",
  "generated_at": "2026-02-22T10:00:00",
  "date": "2026-02-22",
  "dones": [],
  "dos": [],
  "overdue": [],
  "carryover_suggestions": [],
  "groups": {
    "dones_by_area": {},
    "dos_by_area": {},
    "overdue_by_area": {},
    "dos_by_category": {}
  }
}
```

## `weekly-review-summary`

```json
{
  "schema_version": "v1",
  "command": "weekly-review-summary",
  "range": {
    "mode": "current-week|iso-week|custom-range",
    "week": "2026-W08",
    "start_date": "2026-02-16",
    "end_date": "2026-02-22"
  },
  "DONE": {
    "items": [],
    "by_area": {},
    "by_category": {}
  },
  "DO": {
    "items": [],
    "by_area": {},
    "by_category": {}
  }
}
```

## `ingest-daily-log`

Pipeline order is deterministic:
1. exact id/link match
2. normalized title exact match
3. fuzzy match with threshold bands

Threshold decision bands:
- `score >= auto_threshold` -> `auto-link`
- `review_threshold <= score < auto_threshold` -> `needs-review`
- `score < review_threshold` -> `no-match`

```json
{
  "schema_version": "v1",
  "command": "ingest-daily-log",
  "source": {
    "type": "stdin|file",
    "path": "/tmp/done-log.md"
  },
  "thresholds": {
    "auto_link": 0.9,
    "needs_review": 0.7
  },
  "totals": {
    "input_lines": 3,
    "parsed_done_lines": 2,
    "auto_linked": 1,
    "needs_review": 1,
    "no_match": 0
  },
  "items": [
    {
      "raw_line": "- [x] Ship alpha milestone",
      "parsed_title": "Ship alpha milestone",
      "normalized_title": "ship alpha milestone",
      "canonical_task": {
        "task_id": "A-1",
        "title": "Ship alpha milestone",
        "done": false,
        "section": "q1",
        "area": "Delivery",
        "priority": null,
        "due": null,
        "owner": null,
        "goal": null
      },
      "match_metadata": {
        "matched_task_id": "A-1",
        "score": 1.0,
        "decision": "auto-link",
        "match_type": "exact-id-or-link|normalized-title|fuzzy"
      }
    }
  ]
}
```

## `calendar-sync`

Optional helper command. It must not hard-fail if calendar/task sources are unavailable.

```json
{
  "schema_version": "v1",
  "command": "calendar-sync",
  "idempotent": true,
  "optional_helper": true,
  "warnings": [],
  "events_seen": 0,
  "meetings_seen": 0,
  "lifecycle_map": {
    "scheduled": [],
    "done": [],
    "blocked": [],
    "canceled": []
  }
}
```
