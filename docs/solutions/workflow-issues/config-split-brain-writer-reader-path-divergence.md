---
title: "Config split-brain: writer-path env var diverging from the reader path silently drops data"
date: "2026-06-30"
category: workflow-issues
module: config-driven-pipeline
problem_type: integration_issue
component: background_job
symptoms:
  - "Marked-done actions report success but never appear in daily or weekly summaries"
  - "Completion counts are near-zero in reports despite work being recorded"
  - "Two views disagree wildly on the same metric (e.g. a 7-day done count of 32 vs a weekly review count of 6)"
  - "No error is logged anywhere because the write itself is valid"
root_cause: config_error
resolution_type: config_change
severity: high
related_components:
  - "completion-writer"
  - "daily-summary-reader"
  - "weekly-review-reader"
tags:
  - config-split-brain
  - writer-reader-divergence
  - env-var-config
  - silent-data-loss
  - single-source-of-truth
  - config-change
---

# Config split-brain: writer-path env var diverging from the reader path silently drops data

## Problem

In a config-driven pipeline, the component that **writes** completion records was
pointed (via one env var, `TASK_TRACKER_DONE_LOG_DIR`) at a different directory
than the one every **reader** consumed (a separate env var,
`TASK_TRACKER_DAILY_NOTES_DIR`). Writes succeeded, so nothing errored — but every
report, summary, and count read the other location and saw nothing. Completions
silently vanished from all downstream views even though the work was recorded
on disk.

## Symptoms

- A "mark done" action returns success and the record lands on disk, yet it
  never shows up in any daily or weekly summary.
- Completion counts in reports are near-zero, far below what was actually done.
- Two views that should agree report wildly different numbers — for example a
  rolling 7-day "done" count of 32 against a weekly-review count of 6.
- No exception, log line, or failed write anywhere: the write is valid, it just
  lands where no reader looks.

## What Didn't Work

- **Inspecting the writer in isolation.** The write path opened the directory,
  wrote the record, and returned success. It looked correct, because it was —
  for its own (wrong) destination.
- **Inspecting a reader in isolation.** The summary code globbed its configured
  directory and correctly found nothing there. It also looked correct.
- Each side passes its own unit and smoke tests. The defect is invisible until
  you compare the two **resolved** paths against each other; nothing about the
  code on either side reveals the divergence.
- There was no path from "work happened" to "persisted where readers look" — the
  writer was emitting to a location no consumer ever read.

## Solution

Repoint the writer's env var at the canonical reader directory so that
`writer dir == reader dir`, then verify at the place it actually runs (inside the
container/runtime, not just in the shell that edited config):

Before — divergent destinations:

```bash
# writer
TASK_TRACKER_DONE_LOG_DIR=<state>/done-log
# every reader
TASK_TRACKER_DAILY_NOTES_DIR=<obsidian>/Daily
```

After — writer points at the canonical reader location:

```bash
TASK_TRACKER_DONE_LOG_DIR=<obsidian>/Daily   # same dir readers consume
TASK_TRACKER_DAILY_NOTES_DIR=<obsidian>/Daily
```

Then assert it in-process, where the runtime resolves the variables:

```bash
# run inside the container/runtime, not the editing shell
test "$TASK_TRACKER_DONE_LOG_DIR" = "$TASK_TRACKER_DAILY_NOTES_DIR" \
  || { echo "split-brain: writer dir != reader dir"; exit 1; }
```

After repointing, **restart the runtime** so the env change takes effect, then
confirm a fresh "mark done" appears in the daily/weekly views.

## Why This Works

A single canonical location for the completion record removes the divergence:
downstream consumers already read that directory, so writing there makes the
record immediately visible to every view. The bug was never in the write logic —
it was that there were **two sources of truth** for "where completions live", and
the writer owned the one nobody read. Collapsing the two destinations to one
fixes every downstream view at once.

## Prevention

- When a writer and its readers are each configured by **separate** path/dir env
  vars, add a startup assertion (or health check) that the writer destination is
  equal to — or contained within — the readers' source. Divergence should
  **fail loud at boot**, not silently drop data.
- Treat "action succeeded but never appears downstream" as a likely writer/reader
  path mismatch, and **diff the resolved paths first** — the concrete strings the
  runtime actually computed, not the code that builds them.
- Prefer a **single env var** (one source of truth) for a shared location over
  parallel writer/reader vars that can drift. If two vars must exist, derive one
  from the other rather than setting them independently.
- Verify config in the environment that runs the code (container/runtime), since
  the shell that edits config can resolve variables differently from the process
  that consumes them.

## Related Issues

- `../testing/clean-env-ci-is-authoritative-2026-06-22.md` — the same writer/reader
  path-divergence mechanism seen from the test-isolation side; this doc is the
  runtime/config angle plus the boot-time assertion that generalizes the fix.
- `id-only-task-completion-source-of-truth-2026-05-22.md` — one current-state owner
  per fact; a writer != reader split-brain is the failure of having a single path per fact.
