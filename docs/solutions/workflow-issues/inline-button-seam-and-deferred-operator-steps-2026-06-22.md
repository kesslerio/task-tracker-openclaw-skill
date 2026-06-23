---
module: task-tracker
date: "2026-06-22"
problem_type: workflow_issue
category: workflow-issues
component: telegram_interactive
severity: medium
applies_when:
  - "Adding tappable Telegram inline buttons to a deterministic, receipt-backed ritual"
  - "Wiring a new button action end-to-end across the send codec, gateway plugin, and dispatcher"
  - "A code change implies a live gateway/cron mutation that must not happen in the merge loop"
symptoms:
  - "A tapped button silently no-ops (action not wired end-to-end, or plugin not registered)"
  - "An action that is position-keyed (e.g. /focus-veto N) won't fit a task-id-keyed callback scheme"
  - "Uncertainty about what a build PR may change vs. what an operator must deploy"
---

# v0.3 inline-button seam + the deferred-operator-step pattern

Durable patterns from building the v0.3 inline-button UX (U1–U10) on the hardened task-tracker.

## The inline-button seam (send / receive / auth)

- **Send (U1):** `telegram_buttons.py` is a `tt:<action>:<task_id>[:<arg>]` `callback_data` codec (`encode`/`decode`, single source of truth) with a **64-byte UTF-8 guard** — over-budget → `encode` returns `None`, the row is dropped, and the typed command survives. Buttons ride the existing receipt-backed outbox via `openclaw message send --presentation` — **not** a new delivery path, so every v0.2 guarantee (delivery-target proof, idempotent receipt) is inherited. Buttons are NOT part of the idem-key (a button message and its text twin are one logical delivery).
- **Receive (U2):** a sideloaded ESM gateway plugin (`scripts/openclaw-plugins/task-tracker-interactive/`, namespace `tt`, modeled on `reply-watcher-interactive`) with a **pure `routeTap()`** + `callback_dispatch.py`, which decode the tap (reusing `telegram_buttons.decode`) and invoke the **existing deterministic command** (done/snooze/reschedule/carry/drop/approve/set-top/start). The dispatcher calls the command MODULE directly (not the shell front-end) because the shell masks a non-`ok` result behind a friendly notice, discarding the structured stale/wrong-topic signal the tap needs.
- **Auth (single authority, KTD-4):** the plugin authorizes NOTHING — it forwards `senderId` + inbound topic id verbatim; the downstream command + topic guard are the sole authority. A forged/stale tap can only no-op (`routeTap` never throws/rejects). Defense-in-depth example: the dispatcher refuses an empty-topic `appr` BEFORE shelling, so an empty inbound topic + empty env can't degrade the topic guard to `"" == ""`. This **narrows, never widens** authorization — no new bypass flag.

## New-action wiring is cross-cutting

Adding a button action (e.g. `start` in U10) touches **three** places: the codec's known-actions/arg-policy, the plugin's `ONE_SHOT` set, and the dispatcher's `_ACTION_TO_COMMAND`. Plan a new action as a small end-to-end change, not a one-file edit. A decodable action whose command doesn't exist yet returns a clean `not_yet_available` (carry/drop/top were stubs until their units landed) — never a crash.

**Task-id-keyed vs position-keyed:** the `tt:<action>:<task_id>` scheme is task-id-keyed. An action keyed by board POSITION (e.g. `focus_commands` `/focus-veto <N>`) does NOT fit it without a new action + arg policy — which is why the standup veto/approve buttons (U8) were deferred to a follow-up rather than forced into the scheme.

## The deferred-operator-step pattern

An autonomous build loop ships **code + tests merged to main**; it must NOT mutate the live gateway. So any unit whose feature implies a live gateway change ships the change as a **code-only template** and records the live step as a deferred operator action:

- **Cron descriptors are templates** — `eod_ritual.eod_cron_descriptor()` / `standup.standup_cron_descriptor()` return the `payload.kind:"command"` descriptor and a shape-asserting test; the code **never** calls `openclaw cron add`.
- **Plugin registration is operator** — the `tt` plugin must be added to `openclaw.json` `plugins.allow` + `entries` and **boot-synced as REAL files** (never symlinks — OpenClaw mishandles symlinked writable state) into the AlphaClaw state root, then the gateway restarted. Until then buttons render but a tap is a silent no-op.
- **Legacy-cron retirement is operator + gated** — delete the legacy Lobster crons (EOD `3f35796e`, standup `c42b6a07`) only after the hardened replacements are proven live for ≥1 weekday loop, and confirm no relied-on Obsidian write is lost (the JSONL ledger supersedes the legacy Standup-Audit/Provenance blocks).

Grep guard: a false-positive scan for `openclaw cron add` in a diff usually matches the **docstrings affirming code-only**; confirm with `rg "subprocess.*openclaw|run\(\[.*openclaw.*cron"` (no actual call) before flagging.

## Other reusable invariants

- **No board mutation without a tap** — the EOD detect step uses a read-only `dry_run` harvest; a tap drives the existing reversible/gated `approve`/transition. An un-tapped item is reported, never silently mutated.
- **Reweight, not add** (priority-first nag, U10) — reorder candidates (today's committed priorities first), keep `NAG_DISPLAY_LIMIT` / cadence / `/quiet` unchanged. Degrade to the overdue tail when no priorities are set; never go silent.
- **Slot-bucketed idem-key** (U3) — bucket a nag's dedup period into its scheduled cron SLOT (`date + slot-hour`), not the raw wall-clock hour, so a cron fire + a retry + a manual run between slots all deliver exactly once.
