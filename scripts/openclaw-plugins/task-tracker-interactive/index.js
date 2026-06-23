// task-tracker interactive handler — routes a Telegram inline-button tap (the U1
// `tt:<action>:<task_id>[:<arg>]` callback_data) back into the task-tracker skill's
// EXISTING deterministic commands, then edits the originating message to show the
// resolution. This is the RECEIVE half of the v0.3 inline-button seam (the send half
// is U1's telegram_buttons.py + outbox).
//
// Wiring: OpenClaw splits callback_data on the FIRST ":" into <namespace>:<payload> and
// dispatches to the handler registered for that namespace. telegram_buttons.encode emits
// "tt:<action>:<task_id>[:<arg>]", so this plugin owns the "tt" namespace and receives the
// <payload> ("<action>:<task_id>[:<arg>]") — which callback_dispatch.py decodes (re-prepending
// "tt:") with the SAME codec, so the scheme has one source of truth on both sides.
//
// AUTH (KTD-4): the handler forwards ctx.senderId AND the inbound topic id VERBATIM and performs
// NO authorization itself. The downstream command + topic guard are the single authority
// (harvest_ledger.approve hard-rejects a tap whose topic != the Productivity Done topic; the
// nag/board commands operate on the owner's board). One auth point, no caller-side escape hatch.
//
// A tap AWAITS callback_run.sh -> callback_dispatch.py (which prints ONE compact JSON result
// line through error_envelope.run_main, so a stale/failed tap is a clean result, never a
// traceback), then edits the originating message: clears the inline buttons and shows the
// disposition. A `rsch` tap with NO date is an EDIT (open the date-option keyboard), not a run.
//
// Sideloaded as a standalone ESM plugin (mirrors reply-watcher-interactive/); no openclaw-src
// build. Registration (plugins.allow + entries in openclaw.json) and boot-sync-as-real-files
// into the AlphaClaw state root are DEFERRED OPERATOR STEPS, not part of this repo change.
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const LOG_PREFIX = "[task-tracker-interactive]";

// This file lives at <repo>/scripts/openclaw-plugins/task-tracker-interactive/index.js, so the
// repo root is four directories up. Deriving it from the module's own location (rather than a
// hardcoded path) keeps the default correct wherever the skill is boot-synced and avoids baking a
// private host path into a PUBLIC repo. An operator may still override via TASK_TRACKER_REPO.
const HERE = dirname(fileURLToPath(import.meta.url));
const DEFAULT_REPO = join(HERE, "..", "..", "..");

// Resolve the repo root, the runner path, and the namespace at CALL time (not import time) so an
// operator override via env applies to a live gateway, and so a test can repoint the runner after
// importing the module.
function repoRoot() {
  return process.env.TASK_TRACKER_REPO || DEFAULT_REPO;
}
function runnerPath() {
  return process.env.TASK_TRACKER_CALLBACK_RUNNER || `${repoRoot()}/scripts/callback_run.sh`;
}
function namespace() {
  return process.env.TASK_TRACKER_CALLBACK_NAMESPACE || "tt";
}

// The actions that run a command once on tap (terminal one-shot). `rsch` is NOT here: a bare
// `rsch` (no date) opens the date-option keyboard instead of running, so it is handled as an
// edit. `rsch:<date>` IS a run (the payload carries a colon), routed by hasArg below.
const ONE_SHOT = new Set(["done", "start", "snz", "carry", "drop", "appr", "top"]);

// Pure routing decision — NO side effects, NO authorization — so it is unit-testable without a
// live gateway. `payload` is the "<action>:<task_id>[:<arg>]" part AFTER the namespace; senderId
// and topicId are forwarded VERBATIM (the handler never authorizes — the skill command does).
//
// Contract (KTD-2/KTD-4):
//   - split the action off the FIRST ":" only (the task_id/arg keep their own structure);
//   - a one-shot action WITH a task id -> {kind:"run", taskId} carrying the FULL payload +
//     senderId + topicId verbatim;
//   - `rsch` with a task id but NO date (no second ":") -> {kind:"edit-date", action, taskId} so
//     the handler opens the date-option keyboard rather than running a reschedule with no target;
//   - `rsch:<date>` -> {kind:"run"} (a concrete reschedule);
//   - anything else — unknown/typo'd action, empty payload, OR a known action with an EMPTY task
//     id (e.g. `done`, `rsch`, `rsch::DATE`) -> {kind:"ignored"} — NEVER reject, NEVER throw, so a
//     forged or stale callback can only ever no-op. (An empty task id can never resolve to a real
//     task; the dispatcher's decode would reject it anyway, so we drop it before it does work.)
export function routeTap({ payload, senderId, topicId }) {
  const raw = String(payload || "");
  const sep = raw.indexOf(":");
  const action = sep === -1 ? raw : raw.slice(0, sep);
  const rest = sep === -1 ? "" : raw.slice(sep + 1);
  const argSep = rest.indexOf(":");
  const taskId = argSep === -1 ? rest : rest.slice(0, argSep);
  const hasArg = argSep !== -1;

  if (!taskId) {
    // No task id (or an empty one between the colons) — nothing actionable.
    return { kind: "ignored", action };
  }
  if (action === "rsch" && !hasArg) {
    // Bare reschedule = "open date options" — an edit, not a run (the date is chosen next tap).
    return { kind: "edit-date", action: "rsch", taskId };
  }
  if (ONE_SHOT.has(action) || (action === "rsch" && hasArg)) {
    return {
      kind: "run",
      taskId,
      args: {
        callback_data: raw,
        sender_id: String(senderId || ""),
        topic_id: String(topicId == null ? "" : topicId),
      },
    };
  }
  return { kind: "ignored", action };
}

// Run callback_dispatch.py via the bash runner and RESOLVE with its captured result. The args
// object is passed as a SINGLE argv element (no shell parsing), so callback_data may contain any
// characters. We capture STDOUT only (callback_dispatch.py prints one compact JSON result line
// through error_envelope.run_main); stderr is INHERITED to the gateway's stderr (never captured
// or re-logged — avoids ever copying downstream stderr into a retained log line). NEVER rejects:
// a spawn error or non-zero exit resolves with a result the caller maps to a failure ack, so the
// handler always completes and returns {handled:true}.
function runDispatch(args) {
  const argsJson = JSON.stringify({
    callback_data: args.callback_data || "",
    sender_id: args.sender_id || "",
    topic_id: args.topic_id || "",
  });
  return new Promise((resolve) => {
    let out = "";
    const child = spawn("bash", [runnerPath(), argsJson], {
      cwd: `${repoRoot()}/scripts`,
      env: { ...process.env, HOME: process.env.HOME || "/data" },
      stdio: ["ignore", "pipe", "inherit"],
    });
    child.stdout.on("data", (d) => { out += d.toString(); });
    child.on("error", (e) => {
      console.error(`${LOG_PREFIX} runner spawn failed: ${e.message}`);
      resolve({ stdout: "", code: -1 });
    });
    child.on("close", (code) => {
      resolve({ stdout: out, code: code ?? -1 });
    });
  });
}

// Parse callback_dispatch.py's stdout into its result object. It prints ONE compact JSON line
// (via error_envelope.run_main, which prints a friendly line on a fatal error — itself not JSON,
// so it correctly maps to a failure ack). Scan for the LAST parseable JSON object line so a
// leading log line never defeats the parse. Pure + exported for unit testing.
export function parseResult(stdout, code) {
  const raw = String(stdout || "").trim();
  let found = null;
  if (raw) {
    const lines = raw.split("\n");
    for (let i = lines.length - 1; i >= 0 && !found; i--) {
      const line = lines[i].trim();
      if (!line || line[0] !== "{") continue;
      try {
        const obj = JSON.parse(line);
        if (obj && typeof obj === "object" && !Array.isArray(obj)) found = obj;
      } catch { /* try the previous line */ }
    }
  }
  if (found) return found;
  return { ok: false, action: "none", error: code === 0 ? "no result returned" : `dispatch exited ${code}` };
}

// Map a dispatch result to the message-edit acknowledgement text. A success ack requires POSITIVE
// evidence (ok === true); anything else (ok:false, no ok, an infra diagnostic) is a non-success
// ack — never let an unrecognized result read as a success. A stale/already-actioned tap is a
// CLEAN ack (not an error), because the board was already in the desired state. Pure + exported.
export function ackText(result) {
  const r = result || {};
  if (r.ok !== true) {
    // A stale tap (task already done / no open loop / no longer on the board) is benign: the
    // user's intent already holds. Distinguish it from a real failure so the edit is reassuring.
    if (isStale(r)) return "↩️ Already actioned — nothing to do.";
    return "⚠️ Couldn't action that — logged for review. Try the typed command.";
  }
  switch (r.action) {
    case "done": return "✅ Done.";
    case "start": return "▶️ Focus block started — nag muted while you work.";
    case "snooze": return "😴 Snoozed.";
    case "reschedule": return "🗓️ Rescheduled.";
    case "carry": return "➡️ Carried to tomorrow.";
    case "drop": return "🗑️ Dropped to the parking lot.";
    case "approve": return "✅ Confirmed.";
    case "top": return "⭐ Set as tomorrow's #1.";
    default: return "✅ Done.";
  }
}

// A stale/already-actioned tap: the downstream command reports the task is no longer actionable
// (already done / not on the board / no open nag loop / already harvested). These reasons/codes
// are emitted by the existing commands (task_transitions, nag_commands, harvest_ledger) — NOT a
// new contract. Pure + exported.
const STALE_REASONS = new Set([
  "already_actioned", "already_done", "no-open-nag", "stale-approval", "stale",
]);
const STALE_CODES = new Set(["canonical-id-resolution-failed"]);
export function isStale(result) {
  const r = result || {};
  if (STALE_REASONS.has(r.reason)) return true;
  const code = r.error && typeof r.error === "object" ? r.error.code : undefined;
  return STALE_CODES.has(code);
}

// A short disposition token for the observability log line. Pure + exported for testing.
export function disposition(result) {
  const r = result || {};
  if (r.ok === true) return r.action || "done";
  if (isStale(r)) return "stale";
  return "failed";
}

// Build the date-option keyboard for a bare reschedule tap, as the gateway's required button
// shape: Array<Array<{text, callback_data}>>. Each option's callback_data is the FULL
// `tt:rsch:<task_id>:<YYYY-MM-DD>` value the U1 codec produces, so the follow-up tap routes back
// here as a concrete reschedule (one tap-then-tap, no typed input — the R7 fix). Pure + exported.
// `now` is injectable for deterministic tests. The dates mirror the nag's date picker: today,
// tomorrow, +1 week. (If a future composite arg ever pushes a value past 64 bytes, the SEND side's
// codec drops it; here task ids are short, so all three always fit.)
export function dateOptionRows(taskId, now = new Date()) {
  const iso = (offsetDays) => {
    const d = new Date(now.getTime());
    d.setUTCDate(d.getUTCDate() + offsetDays);
    return d.toISOString().slice(0, 10);
  };
  const opt = (label, offset) => ({ text: label, callback_data: `tt:rsch:${taskId}:${iso(offset)}` });
  return [[opt("Today", 0), opt("Tomorrow", 1), opt("+1 week", 7)]];
}

export default {
  id: "task-tracker-interactive",
  name: "Task Tracker Interactive",
  description:
    "Routes task-tracker Telegram inline-button taps (tt:<action>:<task_id>) into the skill's existing deterministic commands, then edits the message to show the resolution. Auth is enforced downstream by the skill command + topic guard (KTD-4); the plugin forwards senderId and topic id verbatim and authorizes nothing.",
  configSchema: { type: "object", additionalProperties: false, properties: {} },
  register(api) {
    api.registerInteractiveHandler({
      channel: "telegram",
      namespace: namespace(),
      async handler(ctx) {
        const payload = ctx?.callback?.payload || "";
        const senderId = ctx?.senderId || "";
        // The inbound thread/topic id, read from the gateway's CANONICAL field (ctx.threadId; it
        // is a number in a forum topic, undefined otherwise) and forwarded VERBATIM to the
        // dispatcher (it is NOT trusted here — the topic guard inside harvest_ledger.approve is the
        // authority for the `appr` action; for board commands it is unused). Mirrors how senderId
        // is forwarded without being authorized.
        const topicId = ctx?.threadId == null ? "" : String(ctx.threadId);
        const decision = routeTap({ payload, senderId, topicId });

        // Best-effort responder helpers: never throw (a failed edit must not mask a completed
        // action — the dispatch result + log line are the durable record). These mirror the real
        // OpenClaw telegram interactive responder surface: respond.editMessage({text,buttons?}),
        // respond.clearButtons(). There is NO respond.edit.
        const editMessage = async (text, buttons) => {
          if (!text) return;
          try {
            const params = { text };
            if (buttons) params.buttons = buttons;
            await ctx?.respond?.editMessage?.(params);
          } catch (e) {
            console.error(`${LOG_PREFIX} editMessage failed (action completed; see result log): ${e?.message || e}`);
          }
        };
        const clearButtons = async () => {
          try {
            await ctx?.respond?.clearButtons?.();
          } catch (e) {
            console.error(`${LOG_PREFIX} clearButtons failed (action completed; see result log): ${e?.message || e}`);
          }
        };

        if (decision.kind === "run") {
          const action = String(payload).split(":")[0];
          const taskId = decision.taskId;
          // Observability: a tap is visible in `docker logs alphaclaw` — action + task id only,
          // no task title/body content. (task id is a tsk_<hex>, no control chars.)
          console.log(`${LOG_PREFIX} tap action=${action} task=${taskId}`);
          const { code, stdout } = await runDispatch(decision.args);
          const result = parseResult(stdout, code);
          console.log(
            `${LOG_PREFIX} result action=${action} task=${taskId} disposition=${disposition(result)} exit=${code}`,
          );
          // Show the disposition AND clear the inline keyboard (a resolved task must not keep live
          // buttons). editMessage with buttons:[] does NOT clear the keyboard in the gateway, so
          // clearButtons() is a separate, explicit call.
          await editMessage(ackText(result));
          await clearButtons();
        } else if (decision.kind === "edit-date") {
          // Bare reschedule: swap the keyboard for concrete date options. The follow-up
          // `tt:rsch:<id>:<date>` tap then routes as a run, closing the two-step reschedule into one
          // tap-then-tap (no typed input — fixes the R7 two-step `/reschedule` UX).
          console.log(`${LOG_PREFIX} tap action=rsch task=${decision.taskId} kind=edit-date`);
          await editMessage("🗓️ Pick a new date:", dateOptionRows(decision.taskId));
        }
        // {kind:"ignored"} — an unknown/forged/stale callback no-ops with no edit and no run.
        return { handled: true };
      },
    });
  },
};
