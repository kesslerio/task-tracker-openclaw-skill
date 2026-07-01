---
module: task-tracker
date: "2026-07-01"
problem_type: architecture_pattern
category: architecture-patterns
component: capture
severity: high
applies_when:
  - "Turning ambient/external/partly-attacker-influenceable input (chat text, merged PRs, calendar) into a mutation of a source-of-truth store"
  - "Tempted to parse free-form prose or a loose proxy (a mention, an accepted invite) to authorize an automatic write"
  - "Adversarial review keeps finding a new bypass each round on a parse-untrusted-then-mutate path"
  - "Deciding between a silent auto-write and a one-tap human-confirm lane"
related_components:
  - "chat_capture"
  - "capture_envelope"
  - "harvest_auto"
  - "completion_candidates"
  - "task_transitions"
  - "evidence_matching"
tags: [auto-write, trust-boundary, source-of-truth, adversarial-review, candidates, idempotency]
---

# Auto-writing a source-of-truth store from ambient input requires an *authoritative* signal, not a parse

## Context

The task board is a source-of-truth markdown file. Phase 2 added "auto-capture": completions should land on the board on their own, inferred from ambient signals — a chat message the owner types ("finished the auth refactor"), a merged GitHub PR, an attended calendar event — instead of requiring an explicit command. The tempting design is: parse the signal, match it to a task, and complete the task automatically.

That design is a trap. This note records why, and the architecture that replaced it.

## What didn't work: parsing free-form / proxy input to *authorize* a write

The first two auto-write units each started from a "close enough" proxy for intent and were taken apart by adversarial review, one bypass at a time:

- **Chat prose.** "Detect a completion statement, then complete the matched task." Adversarial review (live-executing attacks against a real board) found a new class of bypass every round: unlisted negations ("not working on X"), quoted/forwarded pastes ("Bob: I finished X"), multi-part statements where a hedge on one clause didn't block a sibling clause, a completion verb sitting inside the task's own title, meta-usage ("done *thinking about* X"), and a retraction split across clauses ("nope, finished X"). Each fix (deny-list → allow-list of completion verbs → a positive "clean shape" heuristic) closed the named cases and opened new, more esoteric ones. The bypass count fell (3 → 3 → 1) but never reached zero.
- **Merged PRs.** "If a merged PR *mentions* the task's id or a shared URL, complete it." A merged PR titled "Revert `task_id::X`" or "tests showing `task_id::X` still fails" would auto-*complete* X. A "Revert deploy `<url>`" PR would complete an unrelated "Track deploy `<url>`" task that cited the same URL. A bare `#12` reference collided with a *different repo's* `#12`.
- **Calendar.** "If an accepted past event's title matches a task, complete it." Anyone who learned a task's title could send the owner a meeting invite with that title; on accept (or org auto-accept), the task auto-completed.

The through-line: **natural language and loose proxies are open sets.** A mention is not a closure; an accepted invite is not attendance; a plausible sentence is not an authorization. No deny-list is complete, and even a positive heuristic keeps leaking, because the input space is unbounded and partly attacker-influenceable.

## The fix: two lanes, split on an authoritative trust signal

Treat ambient input as untrusted **evidence**, never as **authorization**. Split every capture surface into two lanes:

1. **Auto-write lane** — fires *only* on a machine-unambiguous, authoritative signal that the owner performed the action, resolved to an *exact* identity:
   - chat: a gateway-signed envelope (HMAC over `{sender, channel, message_id, timestamp, task_id, intent}`) carrying an **exact** `task_id` — the CLI verifies the signature and never parses prose for the write;
   - PRs: GitHub's own **closure linkage** (`closingIssuesReferences`) — the merged PR, authored by the owner, *closed* an issue the task tracks (repo-qualified) — not a mention;
   - calendar: an event the owner **organized** (un-injectable by an outsider), not merely accepted.
2. **Candidate lane** — *everything else* becomes a one-tap "confirm" candidate. Obvious-looking prose, fuzzy/title matches, SMS, scheduled events — all stage a candidate the human taps, never a silent write.

The extractor/matcher still exists, but it is **demoted below the mutation boundary**: it ranks and pre-fills candidates. It is not a safety gate. The auto sink is reachable only through the authoritative-signal branch — a structural mutex, not a heuristic.

## Why this works

It eliminates the bypass *class* by construction. Untrusted input cannot reach the write sink at all, so there is no list of phrasings/pastes/collisions to keep patching. The remaining attack surface is a small, deterministic, testable one (verify an HMAC; check an exact id; check an organizer flag) instead of an open-ended parser. After the redesign, adversarial review converged: "no live path where untrusted input auto-writes."

## Prevention / when to apply

For any feature that turns ambient, external, or partly-attacker-influenceable input into a mutation of a source-of-truth store:

- Require an **authoritative** signal for the mutation (a signed command, a provider-attested state change, an owner-controlled action). If the only signal is "the text looks like intent," route it to a confirm lane instead.
- Make the trust boundary **structural** (a branch/mutex that untrusted input cannot cross), not a heuristic that must enumerate bad cases.
- Keep the fuzzy matcher — for *ranking candidates*, explicitly labeled as not-an-authorization-boundary in code, so a future maintainer can't wire it back into the write path.

## Supporting learnings from the same work

- **Adversarial review is load-bearing for data-mutating code.** Repeatedly, model-generated tests passed while an independent adversarial reviewer — constructing and *executing* attacks against a real board — found P0/P1 wrong-write bugs the tests never exercised. Green tests are necessary, not sufficient, for a write path.
- **Reversibility is damage control, not permission.** Snapshots, an undo command, and a morning veto surface are worth building — but they do not justify optimistically auto-writing from ambiguous input. The bad failure mode isn't "wrong box checked"; it's "a real task silently vanishes, the owner misses the veto window, and the source of truth is quietly corrupted."
- **Don't put an LLM classifier in the authorization path.** "The model says yes twice" is a weaker guarantee than "a verified, signed, exact-id command arrived." Use the model to *propose* candidates, not to *authorize* writes.
- **A never-expiring UI action button needs a server-side time-bound.** An inline "undo" button in a chat message lives forever; tapping a days-old one must not replay a write against stale state. The write handler — not the UI — enforces the window.
- **A candidate's dedup identity must include its source's stable id**, not just a human-facing (possibly masked) summary — or two distinct events collapse into one and get lost.
- **Verify green in a separate step from commit/merge.** Bundling "run tests && commit && merge" in one shell invocation once merged a red branch because the test failure didn't gate the merge. Keep the gate and the action as separate, observable steps.
