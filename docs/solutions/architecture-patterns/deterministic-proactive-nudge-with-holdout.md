---
module: task-tracker
date: "2026-06-24"
problem_type: architecture_pattern
category: architecture-patterns
component: development_workflow
severity: high
applies_when:
  - "Adding a PROACTIVE, system-initiated action (a nudge/reminder) that fires before the user has done anything, where a wrongly-timed or duplicate send is the costly failure"
  - "A deterministic decision is parked now and delivered later, and must be re-validated against current state at send time without a double-send"
  - "The same external source (calendar) is already read one way (windowed evidence) and now needs the opposite read (point-in-time gate)"
  - "Shipping a behaviour-change feature whose value is unproven and must be measured before it is trusted or escalated to an LLM"
related_components:
  - "initiation_dispatch"
  - "initiation_contract"
  - "initiation_eval"
  - "availability"
  - "initiation_holdout"
  - "outbox"
---

# Deterministic, measured, proactive nudge (brain decides / hands deliver)

## Context

The v0.4-C build (`task-tracker` PRs #164–#168) added a proactive "initiation nudge" — *"you said X
was today's #1, it's 2pm, you haven't started — Start it?"* — as a **deterministic** slice (a rules-only
state machine, no LLM), shipped **behind a 25% holdout** so its value could be measured before being
trusted. The hard constraint was the v0.2 hardening rationale: an LLM-relay cron is fragile (stale
targets, fire-after-done, double-sends, prompt injection), so **the decision may draft and time the
nudge, but it must never *be* the send** — a receipt-backed, idempotent, proven-target seam delivers.
Across five units a small set of patterns each turned out to be the load-bearing reason a class of bug
did not ship. They generalize to any proactive, deterministic, measured action. (Sibling note:
[deterministic-confirm-gated-harvest-pipeline](deterministic-confirm-gated-harvest-pipeline.md) — the pull-shaped harvest layer this push layer sits on.)

## Guidance

**1. Generalize the existing idempotent dispatcher; do the send-time recheck INSIDE its lock.**
Do not build a new delivery path for a proactive action. Generalize the shipped idempotent dispatcher
(reload state → bail if no longer eligible → **re-prove the delivery target NOW** → render **inert
templated text** → `deliver_once` keyed for at-most-once). Re-validate that the parked decision is still
safe to act on by adding an **optional in-lock `precheck`** to the delivery primitive: it runs inside the
existing flock, *after* the dedup short-circuit and *before* the sender, and a `False` aborts the send
**without recording a receipt** (the slot stays open for a corrected re-fire). Reuse the existing flock;
do **not** invent a new lock/receipt scheme. **Be precise about the guarantee**, though: the *dedup* (no
double-send) IS atomic — it is the receipt write under the flock. The *staleness* recheck is
**optimistic revalidation, not a true compare-and-swap**: the guarded task state lives under a
*different* lock (pattern 3), so the precheck only makes the recheck as-late-as-possible and narrows the
stale-send window — it does not close it. A genuinely atomic claim would compare-and-claim under the
guarded state's *own* lock (or a shared transaction); don't over-claim the precheck as atomic across two
independent locks. The decision and the delivery are decoupled across cron ticks via an **expiring
proposal** in a small sidecar store, so a crash between deciding and sending loses nothing.

**2. A proactive nudge fires before any work-session exists — key on a pre-session "slot" id.**
An idempotency key (and an A/B assignment) normally keys on the work-session id. But a *cold-start*
nudge fires precisely because **no session exists yet**, so there is nothing to key on. Define a
deterministic **slot id** from `(user_scope, entity_id, local_date)` that exists the moment the
commitment is made; it is identical across the experiment arms, and the real session (when the user
finally acts) binds to it. Guard the segments against the key delimiter so two distinct slots can never
collide. This pre-session slot is the keystone that lets a "before you've started" action have a
well-defined at-most-once identity at all.

**3. Compare-and-swap on a monotonic counter, not a timestamp.**
To detect "did the underlying state move between the decision and the send," add a **monotonic `rev`
integer** to the state you guard and compare-and-swap on it. Bump it under the state's own flock with a
**read-on-disk floor** (`rev = max(in_memory_rev, on_disk_rev) + 1`) so it never regresses even when a
caller writes a *fresh* state document (e.g. a daily re-propose that carries no `rev`). A coarse 1-second
ISO `updated_at` is clock-sensitive and can coincidentally match after churn — the integer cannot. The
CAS scopes to exactly the versions that matter (the committed entity + the episode lifecycle), not a
whole-snapshot version which is too broad.

**4. A GATE read of a source is the inverse shape of an EVIDENCE read of the same source — fail closed.**
The same calendar already read for *evidence* (a day-window of accepted events, lenient parsing, empty is
fine) needs the **opposite** shape for a *gate*: a **point-in-time containment** read ("is the user in a
meeting *right now*?") that **fails CLOSED** — any uncertainty (no config, breaker open, CLI error,
unparseable output, an untimeable event) suppresses the proactive action, because acting at a bad moment
is the worst outcome. Single-source the *classification predicate* (what counts as a real, accepted
event) so it cannot drift, but the *policy can legitimately differ*: a focus-block / out-of-office period
is **busy** for an interruption gate yet **not evidence** for a harvest — reusing the harvest's
event-type denylist verbatim would nudge straight through a "do not disturb" block. Parse strictly for a
gate (raise on an odd payload) where the evidence read coerces to empty.

**5. Rules-only evaluator that reads only, failing SAFE toward silence.**
The "adaptivity" everyone reaches for an LLM/heartbeat to provide is a **deterministic state machine**
over stored facts: committed-#1 · not-started · elapsed-threshold · not-busy-now · not-snoozed · budget.
Order the gates **cheap-to-expensive** so the one network/subprocess read runs last (a normal "too soon"
tick does zero I/O). For a *proactive* action, wrap the whole evaluation to **fail toward silence**: any
read error returns "no nudge," because a missed nudge is harmless and an errant one is not. **Note this is
the *same* direction as the calendar gate's fail-closed (pattern 4), not its opposite** — both resolve
uncertainty to "don't send." They differ only in *why*: the gate maps an *Unknown* calendar to suppress
(a deliberate "don't interrupt if unsure"), the evaluator catches *internal errors* and stays silent
(fail-safe). The unifying rule for a proactive action is that **every uncertain path collapses to
no-send**. (It is "rules-only" and side-effect-free in the sense that matters — no mutation, no send —
but it does READ state and shell out to the calendar, so it is not a pure function; for testability,
separate fact-acquisition, pure policy, and persistence.) The decision's only side effect is parking the
expiring proposal; that write itself also fails toward silence (a store error must not crash the cron).

**6. Ship a behaviour-change feature behind a deterministic holdout — and make the control arm symmetric.**
Assign each entity a stable A/B arm by **hashing the slot id** (no RNG, no wall-clock), so the same
episode is always the same arm. The subtle, experiment-poisoning bug: the **control arm must traverse the
identical path and record a counterfactual "held" receipt**. If control simply suppresses without
recording, the evaluator re-decides the held entity *every tick* and the held-observation count inflates
by roughly the number of ticks in the active window, tripping the escalation gate on a fraction of the
intended data. Route control through the same delivery primitive with a **no-op sender**: it records the
at-most-once receipt — so the stage cadence and dedup stay symmetric with treatment — but delivers
nothing. Be honest that this **reuses the delivery receipt as an exposure/stage-advance marker**; it is
not a real delivery. A cleaner design would record an explicit "assignment/exposure" event rather than a
sentinel receipt — reusing the receipt keeps both arms on one code path at the cost of that semantic
fudge. **Aggregate efficacy per-EPISODE** (fold a re-decided slot or a cold+re-nudge pair to one
observation; exclude windows still open as *pending*, never as misses), and fix the denominator at
*assignment* (intention-to-treat), not at delivery. The metric reader only *collects* (no mutation, no
send); the escalation decision stays a **human** read. **Mind the scope of what the holdout proves**,
though: it measures **does nudging help** (nudge vs no-nudge), NOT **does the deterministic version need
an LLM** (deterministic vs LLM) — those are *different* experiments. The holdout count + treatment-vs-
control lift + manually-labelled *semantic* misses are necessary inputs to an LLM-upgrade decision but
not sufficient on their own. The agent never promotes itself either way. Keep the **live trigger
operator-deferred** until the holdout exists, so the feature can never run un-measured.

## Why this matters

Each pattern is the reason a specific bug did not ship, all surfaced by adversarial review:
- Without the in-lock `precheck` (1) or the monotonic `rev` (3), a parked decision could fire after the
  user re-prioritised or already started — a stale, annoying send.
- Without the pre-session slot (2), the cold-start nudge has no idempotency identity and re-sends.
- Reusing the evidence read's event-type policy (4) would nudge during focus-time / out-of-office — the
  exact moments to stay silent.
- Without fail-toward-silence (5), a transient read error becomes an errant proactive send.
- Without the symmetric held receipt (6), the holdout's whole purpose — an honest efficacy read — is
  defeated by a denominator inflated once per tick.

## Shared system invariants (with the pull/harvest layer)

This push layer and the pull/harvest layer ([deterministic-confirm-gated-harvest-pipeline](deterministic-confirm-gated-harvest-pipeline.md))
are one system and rely on the same invariants — state them once, in both notes:
- **One authority per fact.** Semantic task state (DONE / STARTED / priority) is recorded *only* through
  user confirmation; the push layer records *proposals, exposures, receipts, metrics* and sends *inert*
  prompts — it never records a DONE, and it consumes only **confirmed commitments**, never harvested
  candidates or LLM drafts. A nudge's button action returns through the normal user-mutation path.
- **One timezone + identity authority** (the Pacific local-day window; the slot/episode id), shared by
  both layers so a date or an id means the same thing everywhere.
- **Expiring, replay-safe proposals bound to a state revision** are the durable primitive on *both*
  sides: the pull layer's confirm-gated draft and the push layer's deferred send both need stale-draft
  protection + an idempotent token, so a late confirmation or a late send can't act on moved state.
- **Lock ordering is explicit** wherever a recheck spans two locks (see pattern 1's honesty about the
  precheck) — a true claim compares-and-claims under the guarded state's lock.

## When to apply

Any system-initiated action (reminder, nudge, alert, auto-escalation) where the action is *proactive*
(fires before the user acts), *deferred* (decided now, delivered later), and *unproven* (its value should
be measured before trust). The full contract for this instance lives in
`docs/contracts/v0.4-initiation-contract.md`. The companion pull-shaped harvest patterns are in
[deterministic-confirm-gated-harvest-pipeline](deterministic-confirm-gated-harvest-pipeline.md).
