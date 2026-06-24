---
module: task-tracker
date: "2026-06-24"
problem_type: architecture_pattern
category: architecture-patterns
component: development_workflow
severity: high
applies_when:
  - "Building a periodic ritual (standup/digest) that harvests external signals (commits, email, calendar, SMS) into a user-facing surface"
  - "A deterministic pipeline needs exactly one LLM step and must stay safe, repeatable, and confirm-gated"
  - "Auto-collected evidence could be mistaken for a user's confirmed accomplishment"
  - "A read-only nudge must surface a decision without acting on it"
related_components:
  - "standup_harvest"
  - "harvest_state"
  - "evidence_record"
  - "reconcile"
  - "standup_summarizer"
---

# Deterministic, confirm-gated harvest pipeline

## Context

The v0.3.1 build (`task-tracker` PRs #154–#162) turned the morning standup's "what I got done"
into an in-repo, deterministic harvest of external signals (GitHub PRs/commits, Gmail, calendar,
Dialpad SMS), replacing an external agent that freelanced a prompt. Across nine units a small set
of patterns recurred — each one the load-bearing reason a class of bug did not ship. They generalize
to any periodic "harvest external activity → show the user → let the user confirm" ritual.

## Guidance

**1. Stable local-day window over a rolling cutoff (idempotency core).**
Derive a stable Pacific local-day/week window (one timezone authority + ISO-week id), NOT a rolling
`done24h` cutoff. The window resolves explicitly per run (a manual rerun can re-derive a past day),
carries **stable provider ids + per-source watermarks**, and persists under an exclusive sidecar
**flock with merge-on-save** (re-load, union seen-hashes/watermarks, write) so a concurrent cron +
manual run never lose each other's updates. Each independent ritual gets its **own state file** —
never share one state file across rituals (a standup write must not clobber the weekly digest's
`seen`/`pending`). Dedup is **update-by-identity**: a record whose `provider_state` changed
(e.g. a meeting accepted→cancelled) is re-processed, not skipped. Advance a source's watermark
**only on full success** — a partial failure must not move it past records the failed query missed.

**2. Evidence vs. accomplishment — candidates are never silent DONEs.**
Model three kinds: *activity evidence* (a PR/email/elapsed event), *commitment* (an upcoming block),
and *accomplishment* (set ONLY at the confirm-gate). Adapters emit activity/commitment; the
constructor must forbid minting `accomplishment`. The user's stated DONES are **authoritative**;
harvested evidence **supplements** a confirmed item as provenance and **never overwrites the user's
words**. Unconfirmed evidence is **never auto-promoted** into the completed list — *even with an
exact task-ref* — it stays a candidate for the confirm-gate. Calendar/SMS are never auto-promoted
(`auto_done_eligible=False`, enforced in the record constructor regardless of caller input).

**3. The bounded single-LLM-step contract.**
When one deterministic pipeline needs one model call, make it a **confirm-gated draft**, not an agent:
no tools, no session/conversation, no model fallback; a hard timeout + token cap; a **pinned exact
model id** (not a label). Minimize input (send only the metadata needed — e.g. commit titles + evidence
ids — never email/SMS/calendar bodies or raw text). **Validate output against an allowlist**: every
emitted item must reference a *validated input id*, a restricted taxonomy, sanitized/length-capped
text; treat all output as untrusted display text. On any failure (HTTP error, timeout, malformed JSON,
validation failure) fall back to **deterministic grouping** — never freelance. Cache by
`sha256(canonical-input + prompt-version + exact-model-id)`; the cache **write is best-effort** (a
write failure must not abort the pipeline). The draft is shown for confirmation and **never recorded
as a DONE** by the LLM step.

**4. Redact secrets at the error-log boundary; deliver credentials by env.**
A subprocess adapter that passes a token as a CLI arg (`--access-token X`) leaks it via
`subprocess.TimeoutExpired`/`CalledProcessError` repr and captured `stderr` straight into the on-disk
error log. Add a generic **secret-redaction pass at the logging boundary** (scrub `--token`/`Bearer`/
`Authorization` patterns, before truncation) so every current and future adapter is covered. Prefer
env-delivered credentials over argv. And keep operator paths **env-only** — never hardcode a private
`/home/...` default in a public repo (it trips the home-path hygiene check and tempts an allowlist
entry; keep the allowlist empty by removing the literal instead).

**5. One source of truth for derived display.**
Resolve the day/week/date once from a single window object and route ALL rendering (the `%A` day
label, the ISO week id, the daily-note links, the state-file key) through it. Divergent standalone
date resolutions — one for the label, another buried in best-effort harvest metadata — drift on
degrade and produce "Tuesday labelled Monday" / wrong week-number bugs. A *valid* explicit date
selects an explicit window; a *missing or malformed* one falls through to the implicit default
(don't let a typo silently retarget the window).

**6. Typed-command == cron parity, locked by a test.**
A typed `/command` and its scheduled cron must run the *identical* deterministic path. A cron
descriptor that uses a **login shell** (`sh -lc`) reloads host profile env and can read different
state than the interactive run — use `sh -c`. Lock the no-drift guarantee with a characterization
test that runs both entry points as subprocesses and asserts **byte-identical** structured output.
Keep the live cron registration a **deferred operator step** (the descriptor is a code-only template;
no real ids, no `openclaw.json` edit).

**7. Bounded rules-only push-back.**
A "nudge" that must not act stays **pure read + render**: it returns a string, never mutates state,
never sends, and never *chooses* for the user — it lists candidates ranked by an **explicit stored
fact** (here: due date) and asks. It is **fail-open** (any parse/compute error → no nudge, never a
raised exception that breaks the surface). When the live entry point passes no pre-loaded records,
the engine loads them itself rather than silently no-op'ing.

## Why This Matters

Each pattern is the difference between a personal-productivity surface the user trusts and one that
quietly corrupts their record: a rolling cutoff double-counts or drops DONES; auto-promoted evidence
marks the wrong thing done; an unleashed LLM step can be steered by a hostile commit message or record
a hallucinated DONE; a token in argv lands in a log; a divergent date label erodes confidence; a
login-shell cron silently diverges from what the user tested by hand; a push-back that mutates the
board takes a decision away from the user. The confirm-gate + "evidence is provenance, not truth" is
the spine: **the brain (deterministic harvest + a leashed draft) proposes; the human confirms; only
then is anything recorded.**

## When to Apply

- Any recurring harvest/digest/standup that ingests external activity into a user-facing surface.
- Any deterministic pipeline that wants exactly one model call without becoming an agent.
- Any place auto-collected signal could be confused with a user's confirmed state.
- Any read-only nudge/advice surface that must inform without acting.

## Examples

- **Evidence record invariant (pattern 2):** the adapter-facing constructor raises on
  `kind="accomplishment"`; a separate gate-only path mints it. `auto_done_eligible` is forced `False`
  for `calendar`/`dialpad_sms` even when a caller passes `True`.
- **Reconcile (pattern 2):** `merge(user_stated, evidence_candidates) -> (completed, remaining)` —
  `completed` is built only from user claims; matching evidence attaches as a `provenance` entry;
  unmatched evidence (even with a task-ref) returns in `remaining`, never promoted.
- **Summarizer fallback (pattern 3):** model timeout / bullet referencing a non-existent evidence id
  → deterministic keyword grouping + "translation unavailable", `translated=False`; the cache stores
  only the translated path so a transient outage never pins a permanent fallback.
- **Parity test (pattern 6):** both `telegram-commands.sh daily` and the cron descriptor's `argv`
  run as subprocesses; the test asserts identical compact JSON and that the descriptor carries env-var
  *names* (no real `-4242424242`-style id).

## Related

- Process learning — clean-env CI is authoritative: `../testing/clean-env-ci-is-authoritative-2026-06-22.md`
  (ambient `TASK_TRACKER_*` / `TELEGRAM_CHAT_ID_*` / `OPENCLAW_TOPIC_*` vars mask non-isolated test
  failures). Corollary from this build: run `scripts/ci/check-public-hygiene.sh` **after** `git add`,
  because `git grep` skips untracked files — a hygiene check on a not-yet-added new file falsely
  passes. And never allowlist a real id or weaken a check to force a merge; fix the source instead
  (here: removed a hardcoded `/home/...` default so the allowlist stays empty).
- Task identity / completion source-of-truth: `../workflow-issues/id-only-task-completion-source-of-truth-2026-05-22.md`
- Deferred operator steps + delivery seam: `../workflow-issues/inline-button-seam-and-deferred-operator-steps-2026-06-22.md`
