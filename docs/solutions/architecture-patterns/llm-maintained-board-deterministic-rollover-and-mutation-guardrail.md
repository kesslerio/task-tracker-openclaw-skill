---
title: "LLM-maintained markdown board: deterministic rollover + multi-layer mutation guardrail"
date: "2026-06-30"
problem_type: architecture_pattern
category: architecture-patterns
module: task-tracker
component: development_workflow
severity: high
applies_when:
  - "An LLM agent is the writer of a structured artifact (board, ledger, config) that carries canonical identity or derived structure"
  - "The artifact's priority or grouping is derived from its shape (section headers) rather than inline fields"
  - "A periodic maintenance step (weekly rollover, compaction) currently runs as a free-form LLM file write"
  - "The agent permission model is tool-name-based and cannot path-scope writes to deny edits on one file"
related_components:
  - "rollover_script"
  - "reconcile_script"
  - "task_cli"
  - "event_ledger"
  - "skill_prompt"
tags:
  - llm-data-integrity
  - deterministic-tooling
  - markdown-board
  - mutation-guardrail
  - tool-permissions
  - task-id-identity
---

# LLM-maintained markdown board: deterministic rollover + multi-layer mutation guardrail

## Context

A weekly task board (`Weekly TODOs.md`) is maintained by an LLM agent. Tasks and
completions carry a canonical `task_id::` identity, and the board's priority is
**derived from section headers** (a task is "P1" because it lives under the P1
section, not because of an inline priority field). The agent was instructed, via
prompt and templating, to "roll the board over" each week — and it did so as a raw
`write` of the whole file.

A single hand-write of a structured board corrupts it three ways, silently:

1. It **strips the canonical `task_id::`** from carried-forward tasks, breaking
   identity for every downstream consumer.
2. It **authors a dual representation** — a section-grouped priority view *plus* a
   bare flat "All Tasks" copy of the same items — so each task now appears twice.
3. It **re-lists ledger-closed (completed) tasks as open**, because the free-form
   write does not consult the event ledger that records completion.

Every downstream ritual then degrades: the standup and nag count the duplicated
rows against the capacity cap, completed work resurrects as open, and priority is
lost the moment the board collapses to a flat list. The root failure is structural,
not a one-off mistake: **an LLM maintaining structured data by free-form writing
will corrupt identity and derived structure**, and you cannot rely on the model to
not do it again.

## Guidance

The durable fix has three parts. Move the mutation behind deterministic tools, make
the rule undeniably discoverable at every instruction layer, and ship a one-time
repair for boards already corrupted.

**1. Replace the agent hand-roll with a deterministic rollover script.**
`scripts/rollover.py` carries open tasks into the new week with `task_id::` intact;
it **never resurrects ledger-closed tasks** (it reads closed state from the event
ledger, not the board text); it **preserves the priority-section structure** by
emitting a section-grouped board and never a flat list; and it **preserves special
sections verbatim** (e.g. a Parking Lot copied through unchanged). Run it on a
deterministic schedule — a silent-command cron — not via the LLM. The board is now
mutated by code whose behavior is testable and repeatable, and the weekly step
stops depending on the model getting a prompt right.

**2. Enforce one board-mutation rule at EVERY instruction layer.**
Key insight: the agent's tool-permission system is **tool-name-based, not
path-scoped**. You cannot `deny` `write`/`edit` on a single file path without also
blocking the agent's legitimate edits everywhere else. Capability removal is simply
not available here, so enforcement is **contract + prompt**, and one layer is not
enough — the agent originally improvised the hand-roll precisely because
`rollover.py` existed but was referenced *nowhere it would actually read*. The rule:

> Mutate the board ONLY via the scripts. Add / complete / reschedule a task via the
> task CLI; run the weekly rollover via `scripts/rollover.py`; do one-time cleanup
> via `scripts/reconcile_board.py`. NEVER hand-edit the board with
> `write`/`edit`/`apply_patch`/`sed`.

Repeat that rule in all three places the agent reads instructions: the skill's own
`SKILL.md`, the channel / group system prompt, and the agent workspace instruction
file (`AGENTS.md` / `TOOLS.md`). Discoverability is the control; a tool that exists
but is unreferenced is equivalent to a tool that does not exist.

**3. Ship a one-time reconcile tool to repair an already-corrupted board.**
`scripts/reconcile_board.py` (dry-run by default) repairs the existing damage
**conservatively**: merge bare duplicate lines into their id-bearing rows, strike
rows that the ledger shows are closed-but-resurrected, and flag/repair rows missing
an id. The guiding constraint is *never drop a genuinely-open task* — when in doubt,
keep and flag rather than delete.

## Why This Matters

The board is the source of truth for a person's day. A rolling free-form rewrite
double-counts capacity, marks the wrong things done, and discards priority — and it
does all of this *silently*, so the human only notices once the surface has already
lied to them. The fix is structural: **deterministic tools own the mutation, and the
"go through the tools" rule is replicated at every layer the agent reads**, because
the only available enforcement is discoverability — path-scoped capability denial
does not exist in a tool-name-based permission model. This generalizes far beyond a
task board: any structured artifact an LLM "maintains" by writing free-form text is
one rollover away from silent corruption.

## When to Apply

- An LLM agent writes a structured artifact (board, ledger, config) carrying
  canonical identity (`task_id::`-style) or structure-derived semantics (priority by
  section).
- A periodic maintenance step (weekly rollover, compaction, archival) is currently a
  free-form model write.
- Your permission model cannot path-scope writes, so you cannot deny edits to one
  file without breaking the agent's legitimate edits elsewhere.
- A bug-class fix lands in one mutation tool and other tools share the same class —
  it must be mirrored to all of them.

## Examples

**Before (agent free-form rollover, corrupts on write):**

```markdown
## Priority 1
- Ship the export feature
- Ship the export feature   <!-- duplicated into a flat list below -->

## All Tasks
- Ship the export feature
- Fix the login redirect     <!-- closed last week, resurrected as open -->
```
Identity stripped (no `task_id::`), dual representation, a ledger-closed task back as
open.

**After (deterministic rollover, structure + identity preserved):**

```markdown
## Priority 1
- Ship the export feature <!-- task_id:: t_4f9a -->

## Parking Lot
- Revisit the pricing experiment <!-- task_id:: t_0c12 -->   (preserved verbatim)
```
`rollover.py` carried forward only open ids from the ledger, kept the section
structure, and copied the Parking Lot through unchanged.

**Identity / dedup invariant.** Dedup id-bearing rows by `task_id` ONLY. A matching
title may suppress a *bare / no-id* duplicate line, but a title alone must never
merge two id-bearing rows. An ambiguous or partial match becomes a one-tap
**CANDIDATE** for confirmation, never an auto-write — this is what prevents
title-collision data loss (two different tasks sharing a title getting silently
merged).

**Shared bug-class must be routed to every surface.** The title-collision dedup fix
and the parking-lot-preservation fix were first applied in `rollover.py`. Because
`reconcile_board.py` shares the same identity/structure logic, the *same* fixes had
to be mirrored there. A focused re-review caught that the parking-lot fix had landed
in one tool but not the other — exactly the gap a single-file review misses.

**Independent review is load-bearing for data-mutating code.** This logic was
generated by a different model whose own tests passed while data-integrity bugs
remained (title-collision drops, resurrected-done rows, archive data-loss). A
multi-agent review-clean — an independent correctness reviewer on a strong model
plus a focused re-review — is what surfaced them. Passing tests authored by the same
model that wrote the bug are not sufficient evidence for code that mutates a source
of truth.

**Tooling gaps to close (they force forbidden edits).** Two gaps surfaced that push
the agent back toward raw edits:
- There was no sanctioned "remove / cancel task" command, so removing a stray line
  required a forbidden raw edit. Add an explicit cancel path to the task CLI.
- The identity-repair step left its own `<!-- repair: missing task_id -->` hint
  comment behind after filling the id. The repair must clean up its own scaffolding.

## Related

- Task identity / completion source-of-truth (dedup by id only, candidates not
  auto-writes): `../workflow-issues/id-only-task-completion-source-of-truth-2026-05-22.md`
- Clean-env CI is authoritative (same-model passing tests can mask data-integrity
  bugs; an independent clean run is the gate):
  `../testing/clean-env-ci-is-authoritative-2026-06-22.md`
- Deterministic, confirm-gated harvest pipeline (companion pattern — the brain
  proposes via deterministic tools, the human confirms, only then is state recorded):
  `./deterministic-confirm-gated-harvest-pipeline.md`
- Inline-button seam and deferred operator steps (the one-time reconcile is a
  deferred operator step; the name-based-not-path-scoped permission caution
  complements that doc's single-authority seam):
  `../workflow-issues/inline-button-seam-and-deferred-operator-steps-2026-06-22.md`
