# Task Primitives Schema v1

All `scripts/tasks.py` task primitives use a stable top-level envelope:

```json
{
  "schema_version": "v1",
  "command": "<primitive-name>"
}
```

Task rows should expose canonical `task_id` values when available. Fallback
identifiers may appear in read-only diagnostics, but mutation commands must use
canonical IDs from `task_id::` or migrated legacy `id::` metadata.

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

- `score >= evidence_link threshold` -> `evidence-link`
- `review_threshold <= score < evidence_link threshold` -> `needs-review`
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
    "evidence_link": 0.9,
    "needs_review": 0.7
  },
  "totals": {
    "input_lines": 3,
    "parsed_done_lines": 2,
    "evidence_linked": 1,
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
        "goal": null,
        "fallback_id": "fallback-abc123",
        "missing_task_id": false,
        "fallback_only": false
      },
      "match_metadata": {
        "matched_task_id": "A-1",
        "score": 1.0,
        "decision": "evidence-link",
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

## `completion-candidates`

The completion evidence inbox stores candidate lifecycle events in the JSONL
ledger. Candidate projection uses strict ledger reads; malformed JSONL returns a
structured `malformed-ledger` error instead of producing a partial inbox.

Scanning commands accept stdin, `--file PATH`, or `--date YYYY-MM-DD` with
`TASK_TRACKER_DAILY_NOTES_DIR` / `--notes-dir`. Scan only appends
`candidate_seen` events for new evidence. It does not write the board, daily
completion log, or task state. `no-match` lines remain report-only and are not
persisted as candidates.

```json
{
  "schema_version": "v1",
  "command": "completion-candidates scan",
  "created": [
    {
      "candidate_id": "cand_abc123",
      "status": "new",
      "source": {
        "type": "file|stdin|daily_note",
        "path": "/tmp/done-log.md",
        "date": "2026-05-21",
        "line_number": 4
      },
      "raw_summary": "- [x] Ship alpha milestone task_id::tsk_ship",
      "summary": "Ship alpha milestone task_id::tsk_ship",
      "normalized_summary": "ship alpha milestone task id tsk_ship",
      "matched_task_id": "tsk_ship",
      "confirmable_task_id": "tsk_ship",
      "review_required": false,
      "suggested_match": {
        "task_id": "tsk_ship",
        "title": "Ship alpha milestone",
        "fallback_only": false
      },
      "match_metadata": {
        "matched_task_id": "tsk_ship",
        "score": 1.0,
        "decision": "evidence-link",
        "match_type": "exact-id-or-link"
      }
    }
  ],
  "existing": [],
  "totals": {
    "parsed_evidence": 1,
    "created": 1,
    "existing": 0
  }
}
```

Decision commands use the same envelope with command names such as
`completion-candidates confirm`, `completion-candidates reject`, and
`completion-candidates duplicate`. Candidate statuses are `new`, `shown`,
`confirmed`, `rejected`, `duplicate`, `snoozed`, `expired`, and `apply_failed`.

Confirmation rules:

- exact `task_id::` or exact link evidence may confirm without `--task-id`
- `confirmable_task_id` is present only for exact canonical ID/link evidence
- title, fuzzy, issue-number fallback, and fallback-only matches require
  explicit `--task-id`
- non-exact evidence may include `suggested_match`, but workflows must treat it
  as review-required
- confirmation calls the canonical ID-only completion path and writes a
  `candidate_confirmed` event only after task completion succeeds
- failed application writes `candidate_apply_failed` and leaves the candidate
  retryable

Workflow wrappers should use `scripts/completion_inbox_control.py` or the
`completion-candidates` command group. These wrappers review and decide existing
candidates only; Gmail, calendar, and session-log ingestion remain deferred.
