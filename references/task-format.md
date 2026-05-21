# Task Format Specification

## Basic Format

```markdown
- [ ] **Task title** — Brief description
  - Owner: your-name
  - Due: 2026-01-29
  - Status: Todo
  - Blocks: lilla (podcast outreach)
  - Location: https://example.com
```

## Fields

| Field | Required | Description |
|-------|----------|-------------|
| title | Yes | Brief, actionable (verb + noun) |
| task_id | Yes for active mutations | Opaque canonical ID stored inline as `task_id::tsk_example` |
| description | No | Additional context after `—` |
| Owner | No | Person responsible (default: configurable via TASK_TRACKER_DEFAULT_OWNER) |
| Due | No | ASAP, YYYY-MM-DD, or "Before [event]" |
| Status | No | Todo, In Progress, Blocked, Waiting, Done |
| Blocks | No | Who/what is blocked + reason |
| Location | No | URL or file path |

## Checkbox States

- `[ ]` — Open task
- `[x]` — Completed task

Active task mutations use `task_id::` as the durable identity. Legacy `id::`
values are readable during migration, but new repair output writes `task_id::`.

## Priority Sections

| Section | Emoji | Criteria |
|---------|-------|----------|
| High Priority | 🔴 | Blocking others, critical deadline, revenue impact |
| Medium Priority | 🟡 | Important but not urgent, reviews/feedback |
| Delegated/Waiting | 🟢 | Someone else owns, monitoring only |
| Upcoming | 📅 | Future deadlines, scheduled events |

## Status Definitions

| Status | Description |
|--------|-------------|
| Todo | Not started |
| In Progress | Actively working |
| Blocked | Waiting on external dependency |
| Waiting | Delegated, monitoring for completion |
| Done | Completed |

## Due Date Formats

- `ASAP` — Do immediately
- `2026-01-29` — Specific date
- `Before Jan 29` — Deadline tied to event
- `Before IMCAS` — Named deadline
- `This week` — Current week
- `Next Monday` — Relative date

## Examples

### High Priority (Blocking)
```markdown
- [ ] **Set up Apollo.io access for Lilla** — Restore account for email finding
  - Owner: Sarah
  - Due: ASAP
  - Status: Todo
  - Blocks: Lilla (podcast outreach sequence)
```

### High Priority (Deadline)
```markdown
- [ ] **Create IMCAS lead capture form** — Signup form + ActiveCampaign sequence
  - Owner: Sarah
  - Due: Before Jan 29
  - Status: Todo
  - Location: https://bysha.pe/imcas
```

### Medium Priority (Review)
```markdown
- [ ] **Review Q1 promo designs** — Identify which need carousel versions
  - Owner: Sarah
  - Status: Todo
  - Location: https://dropbox.com/...
```

### Delegated
```markdown
- [ ] **JGO release form signature** — Lilla following up
  - Owner: Lilla
  - Status: Waiting
```
