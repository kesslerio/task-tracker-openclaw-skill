// Unit + integration test for the task-tracker interactive handler.
// Run: node scripts/openclaw-plugins/task-tracker-interactive/test-handler.mjs
//
// Proves, with NO live gateway:
//  - routeTap maps each action correctly (one-shot actions run; bare `rsch` edits to date
//    options; `rsch:<date>` runs; unknown/empty ignored), NEVER throws, NEVER rejects;
//  - senderId AND the inbound topic id are forwarded VERBATIM (the handler authorizes nothing —
//    the skill command + topic guard do, KTD-4);
//  - the registered handler shells the runner with the right args-json for a one-shot tap;
//  - parseResult / ackText / disposition / isStale map a dispatch result correctly, and a
//    stale tap reads as a clean "already actioned" ack, not a failure;
//  - the handler AWAITS the dispatch and EDITS the originating message (clearing the buttons),
//    and a bare `rsch` opens the date keyboard WITHOUT running a dispatch;
//  - an edit that throws after a completed action still resolves {handled:true}.
import { mkdtempSync, writeFileSync, readFileSync, existsSync, chmodSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

const HERE = new URL(".", import.meta.url).pathname;
const mod = await import(pathToFileURL(join(HERE, "index.js")).href);
const { routeTap, parseResult, ackText, disposition, isStale, dateOptionRows } = mod;
const plugin = mod.default;

let failures = 0;
function check(cond, msg) {
  if (cond) { console.log(`  ok: ${msg}`); }
  else { console.error(`  FAIL: ${msg}`); failures++; }
}
function eq(a, b, msg) { check(JSON.stringify(a) === JSON.stringify(b), `${msg} (got ${JSON.stringify(a)})`); }

const FAKE_SENDER = "8352721935";
const FAKE_TOPIC = "5";

// --- 1) routeTap pure routing contract (the failing-test-first contract) --------------------
console.log("routeTap routing contract:");
{
  // done -> run, payload + senderId + topicId forwarded verbatim, taskId surfaced for logging.
  const r = routeTap({ payload: "done:tsk_abc", senderId: FAKE_SENDER, topicId: FAKE_TOPIC });
  eq(r, { kind: "run", taskId: "tsk_abc",
          args: { callback_data: "done:tsk_abc", sender_id: FAKE_SENDER, topic_id: FAKE_TOPIC } },
    "done -> run with callback_data/sender_id/topic_id verbatim + taskId");
}
{
  // snooze with a span arg -> run, full payload (incl. the :1d arg) carried verbatim.
  const r = routeTap({ payload: "snz:tsk_abc:1d", senderId: FAKE_SENDER, topicId: FAKE_TOPIC });
  eq(r.kind, "run", "snz:tsk:1d -> run");
  eq(r.args.callback_data, "snz:tsk_abc:1d", "snz run carries the full payload incl. the span arg");
}
{
  // bare rsch (no date) -> edit-date, NOT a run.
  const r = routeTap({ payload: "rsch:tsk_abc", senderId: FAKE_SENDER, topicId: FAKE_TOPIC });
  eq(r, { kind: "edit-date", action: "rsch", taskId: "tsk_abc" }, "bare rsch -> edit-date (open date options), not a run");
}
{
  // rsch with a date -> run (a concrete reschedule).
  const r = routeTap({ payload: "rsch:tsk_abc:2026-06-24", senderId: FAKE_SENDER, topicId: FAKE_TOPIC });
  eq(r.kind, "run", "rsch:tsk:DATE -> run");
  eq(r.args.callback_data, "rsch:tsk_abc:2026-06-24", "rsch-date run carries the full payload incl. the date");
}
{
  // each remaining one-shot action routes to run.
  for (const a of ["carry", "drop", "appr", "top"]) {
    const r = routeTap({ payload: `${a}:tsk_x`, senderId: FAKE_SENDER, topicId: FAKE_TOPIC });
    eq(r.kind, "run", `${a} -> run`);
  }
}
// unknown / typo'd action and empty payload -> ignored (never run, never throw).
eq(routeTap({ payload: "frobnicate:tsk_x", senderId: FAKE_SENDER }), { kind: "ignored", action: "frobnicate" },
  "unknown action -> ignored");
check(routeTap({ payload: "", senderId: FAKE_SENDER }).kind === "ignored", "empty payload -> ignored");
{
  // topicId is forwarded verbatim even when it is a string id; missing topicId -> "".
  const r1 = routeTap({ payload: "appr:tsk_y", senderId: FAKE_SENDER, topicId: "5" });
  eq(r1.args.topic_id, "5", "appr forwards the topic id verbatim (the topic guard is the authority)");
  const r2 = routeTap({ payload: "appr:tsk_y", senderId: FAKE_SENDER });
  eq(r2.args.topic_id, "", "missing topic id -> empty string (never throws)");
}
{
  // A KNOWN action with an EMPTY task id is ignored (never run/edit) — an empty id can never
  // resolve to a real task. Covers `done` (no colon), bare `rsch` (no id), and `rsch::DATE`.
  check(routeTap({ payload: "done", senderId: FAKE_SENDER }).kind === "ignored", "done with no task id -> ignored");
  check(routeTap({ payload: "rsch", senderId: FAKE_SENDER }).kind === "ignored", "bare rsch with no task id -> ignored (not an empty date keyboard)");
  check(routeTap({ payload: "rsch::2026-07-01", senderId: FAKE_SENDER }).kind === "ignored", "rsch with empty task id -> ignored");
}
{
  // NEVER throws / NEVER rejects on hostile input.
  let threw = false;
  try { routeTap({}); routeTap({ payload: null }); routeTap({ payload: ":::" }); routeTap({ payload: 42 }); routeTap({ payload: [] }); }
  catch { threw = true; }
  check(!threw, "routeTap never throws on garbage input");
}

// dateOptionRows builds the gateway button shape with round-trippable tt:rsch:<id>:<date> values.
{
  const rows = dateOptionRows("tsk_abc", new Date(Date.UTC(2026, 5, 22)));  // 2026-06-22
  check(Array.isArray(rows) && Array.isArray(rows[0]), "dateOptionRows returns rows-of-buttons (Array<Array<...>>)");
  check(rows[0].every((b) => typeof b.text === "string" && typeof b.callback_data === "string"),
    "each date button has {text, callback_data}");
  check(rows[0].every((b) => b.callback_data.startsWith("tt:rsch:tsk_abc:")),
    "each date callback_data is a concrete tt:rsch:<id>:<date> (routes back as a run)");
  eq(rows[0].map((b) => b.callback_data),
     ["tt:rsch:tsk_abc:2026-06-22", "tt:rsch:tsk_abc:2026-06-23", "tt:rsch:tsk_abc:2026-06-29"],
     "date options are today / tomorrow / +1 week");
}

// --- 2) parseResult / ackText / disposition / isStale --------------------------------------
console.log("result mapping:");
eq(parseResult('{"ok":true,"action":"done","task_id":"tsk_abc"}', 0), { ok: true, action: "done", task_id: "tsk_abc" },
  "parseResult reads a compact JSON result line");
eq(parseResult("[lobster] starting\n{\"ok\":true,\"action\":\"snooze\"}", 0).action, "snooze",
  "parseResult skips a leading log line and reads the JSON");
eq(parseResult("", 0), { ok: false, action: "none", error: "no result returned" }, "empty stdout -> synthesized failure");
eq(parseResult("garbage", 1).ok, false, "non-JSON stdout + nonzero -> failure result");

check(ackText({ ok: true, action: "done" }).includes("Done"), "done success ack");
check(ackText({ ok: true, action: "snooze" }).includes("Snoozed"), "snooze success ack");
check(ackText({ ok: true, action: "reschedule" }).includes("Rescheduled"), "reschedule success ack");
check(ackText({ ok: true, action: "carry" }).includes("Carried"), "carry success ack");
check(ackText({ ok: true, action: "drop" }).includes("Dropped"), "drop success ack");
check(ackText({ ok: true, action: "approve" }).includes("Confirmed"), "approve success ack");
check(ackText({ ok: true, action: "top" }).includes("#1"), "top success ack");
check(/Already actioned/i.test(ackText({ ok: false, error: { code: "canonical-id-resolution-failed" } })),
  "stale (already done / not on board) -> clean 'already actioned' ack, not a failure");
check(/Already actioned/i.test(ackText({ ok: false, reason: "stale-approval" })), "stale-approval -> clean ack");
check(/Already actioned/i.test(ackText({ ok: false, reason: "no-open-nag" })), "no-open-nag -> clean ack");
check(/Couldn't action|logged/i.test(ackText({ ok: false, reason: "wrong-topic" })),
  "wrong-topic (a real reject) -> failure ack, NOT a clean 'already actioned'");
check(/Couldn't action|logged/i.test(ackText({})), "unrecognized result never reads as success");

check(isStale({ ok: false, error: { code: "canonical-id-resolution-failed" } }), "isStale true for canonical-id-resolution-failed");
check(isStale({ ok: false, reason: "stale-approval" }), "isStale true for stale-approval");
check(!isStale({ ok: false, reason: "wrong-topic" }), "isStale false for wrong-topic (a real reject)");
check(!isStale({ ok: true, action: "done" }), "isStale false for a success");

eq(disposition({ ok: true, action: "done" }), "done", "disposition reads the action on success");
eq(disposition({ ok: false, reason: "stale-approval" }), "stale", "disposition 'stale' for a stale tap");
eq(disposition({ ok: false, reason: "wrong-topic" }), "failed", "disposition 'failed' for a real reject");

// --- 3) handler integration (a one-shot tap shells the runner + edits the message) ----------
console.log("handler integration (one-shot):");

// Capture the registered interactive handler.
let reg = null;
plugin.register({ registerInteractiveHandler(r) { reg = r; } });
check(reg && reg.channel === "telegram" && reg.namespace === "tt", "plugin registers the telegram 'tt' interactive handler");

// A stub runner: records its single argv (the args-json) to a file AND prints RW_TEST_RESULT to
// stdout (one compact JSON line, the real callback_dispatch.py shape). Empty result prints
// nothing (a wrapper that died before dispatch emitted). exit via RW_TEST_EXIT.
const tmp = mkdtempSync(join(tmpdir(), "tt-interactive-"));
const recPath = join(tmp, "argv.json");
const runner = join(tmp, "callback_run.sh");
writeFileSync(
  runner,
  `#!/usr/bin/env bash
printf '%s' "$1" > "${recPath}"
if [ -n "\${RW_TEST_RESULT:-}" ]; then printf '%s\\n' "\${RW_TEST_RESULT}"; fi
exit "\${RW_TEST_EXIT:-0}"
`,
);
chmodSync(runner, 0o755);
process.env.TASK_TRACKER_CALLBACK_RUNNER = runner;

// Build a ctx exposing the REAL OpenClaw telegram interactive responder surface
// (respond.editMessage / respond.clearButtons -- there is NO respond.edit), invoke the handler,
// and capture what it called. `ctx.threadId` is the canonical inbound topic field (a number in a
// forum topic). `respondMissing` drops the responder entirely (a gateway-downgrade resilience case).
async function callHandler(registration, ctx, { result = "", exit = 0, editThrows = false, respondMissing = false } = {}) {
  const prevResult = process.env.RW_TEST_RESULT;
  const prevExit = process.env.RW_TEST_EXIT;
  process.env.RW_TEST_RESULT = result;
  process.env.RW_TEST_EXIT = String(exit);
  let edited = null;
  let cleared = false;
  const logs = [];
  const errs = [];
  const origLog = console.log;
  const origErr = console.error;
  console.log = (...a) => logs.push(a.join(" "));
  console.error = (...a) => errs.push(a.join(" "));
  const respond = respondMissing ? undefined : {
    editMessage: async (params) => { if (editThrows) throw new Error("edit boom"); edited = params; },
    clearButtons: async () => { cleared = true; },
  };
  const fullCtx = { ...ctx, respond };
  let r;
  try { r = await registration.handler(fullCtx); }
  finally {
    console.log = origLog; console.error = origErr;
    if (prevResult === undefined) delete process.env.RW_TEST_RESULT; else process.env.RW_TEST_RESULT = prevResult;
    if (prevExit === undefined) delete process.env.RW_TEST_EXIT; else process.env.RW_TEST_EXIT = prevExit;
  }
  return { r, edited, cleared, logs, errs };
}

// done tap -> runner invoked with the right args-json; message edited to "Done" + buttons CLEARED.
{
  rmSync(recPath, { force: true });
  const { r, edited, cleared, logs } = await callHandler(reg,
    { callback: { payload: "done:tsk_abc" }, senderId: FAKE_SENDER, threadId: 5 },
    { result: '{"ok":true,"action":"done","task_id":"tsk_abc"}' });
  eq(r, { handled: true }, "one-shot tap returns {handled:true}");
  check(existsSync(recPath), "done tap invoked the runner");
  // The runner recorded its single argv element verbatim: the args-json the handler built.
  const args = JSON.parse(readFileSync(recPath, "utf8"));
  eq(args.callback_data, "done:tsk_abc", "runner got callback_data=done:tsk_abc");
  eq(args.sender_id, FAKE_SENDER, "runner got sender_id forwarded verbatim");
  eq(args.topic_id, "5", "runner got the inbound topic id (ctx.threadId) forwarded verbatim");
  check(edited && /Done/i.test(edited.text || ""), "message edited to 'Done' (via respond.editMessage)");
  check(cleared, "inline keyboard CLEARED via respond.clearButtons (a resolved task keeps no live buttons)");
  check(logs.some((l) => /disposition=done/.test(l)), "tap outcome logged disposition=done");
}

// stale done tap -> clean "already actioned" edit, NOT a failure, no second board write claim.
{
  const { r, edited, cleared } = await callHandler(reg,
    { callback: { payload: "done:tsk_gone" }, senderId: FAKE_SENDER },
    { result: '{"ok":false,"error":{"code":"canonical-id-resolution-failed"}}' });
  eq(r, { handled: true }, "stale tap returns {handled:true}");
  check(edited && /Already actioned/i.test(edited.text || ""), "stale tap edited to 'already actioned' (clean, not an error)");
  check(cleared, "stale tap also clears the now-dead buttons");
}

// approve tap in the WRONG topic -> rejected downstream (topic guard); failure ack, no bypass.
{
  const { edited } = await callHandler(reg,
    { callback: { payload: "appr:tsk_abc" }, senderId: FAKE_SENDER, threadId: 99 },
    { result: '{"ok":false,"reason":"wrong-topic"}' });
  check(edited && /Couldn't action|logged/i.test(edited.text || ""), "wrong-topic approve -> failure ack (the plugin added no bypass)");
}

// dispatch failure (nonzero, no JSON) -> failure ack; still {handled:true}; no raw leak to edit.
{
  const { r, edited } = await callHandler(reg,
    { callback: { payload: "done:tsk_abc" }, senderId: FAKE_SENDER },
    { result: "Traceback (most recent call last): ...", exit: 1 });
  eq(r, { handled: true }, "dispatch failure still returns {handled:true}");
  check(edited && !/Traceback/i.test(edited.text || ""), "a raw traceback on stdout never reaches the edited message");
  check(edited && /Couldn't action|logged/i.test(edited.text || ""), "dispatch failure -> friendly failure ack");
}

// editMessage THROWS after a completed action -> still resolves {handled:true}, records a fallback.
{
  const { r, errs } = await callHandler(reg,
    { callback: { payload: "done:tsk_abc" }, senderId: FAKE_SENDER },
    { result: '{"ok":true,"action":"done"}', editThrows: true });
  eq(r, { handled: true }, "edit-throw after success still returns {handled:true}");
  check(errs.some((l) => /editMessage failed/.test(l)), "edit-throw recorded as a fallback error (action not masked)");
}

// respond MISSING entirely (a gateway downgrade) -> handler still resolves, never throws.
{
  const { r } = await callHandler(reg,
    { callback: { payload: "done:tsk_abc" }, senderId: FAKE_SENDER },
    { result: '{"ok":true,"action":"done"}', respondMissing: true });
  eq(r, { handled: true }, "missing responder still returns {handled:true} (no crash)");
}

// ignored tap (unknown action) reaching the handler -> NO run, NO edit, just {handled:true}.
{
  rmSync(recPath, { force: true });
  const { r, edited, cleared } = await callHandler(reg,
    { callback: { payload: "frobnicate:tsk_abc" }, senderId: FAKE_SENDER });
  eq(r, { handled: true }, "ignored tap returns {handled:true}");
  check(!existsSync(recPath), "ignored tap did NOT invoke the runner");
  check(edited === null && cleared === false, "ignored tap makes no edit and no clear (silent no-op)");
}

// --- 4) bare rsch tap opens the date keyboard WITHOUT running a dispatch ---------------------
console.log("handler integration (edit-date):");
{
  rmSync(recPath, { force: true });
  const { r, edited } = await callHandler(reg,
    { callback: { payload: "rsch:tsk_abc" }, senderId: FAKE_SENDER });
  eq(r, { handled: true }, "bare rsch tap returns {handled:true}");
  check(!existsSync(recPath), "bare rsch did NOT invoke the runner (no reschedule with no date)");
  check(edited && /date|pick/i.test(edited.text || ""), "bare rsch edited the message to a date prompt");
  // The buttons are the REAL gateway shape: rows of {text, callback_data}, each a concrete
  // tt:rsch:<id>:<date> that routes back as a run.
  check(edited && Array.isArray(edited.buttons) && Array.isArray(edited.buttons[0]),
    "bare rsch swapped in a date keyboard of the gateway button shape (Array<Array<...>>)");
  check(edited && edited.buttons[0].every((b) => b.callback_data && b.callback_data.startsWith("tt:rsch:tsk_abc:")),
    "each date option carries a concrete tt:rsch:tsk_abc:<date> callback_data");
}

rmSync(tmp, { recursive: true, force: true });
if (failures) { console.error(`\nFAIL: ${failures} check(s) failed`); process.exit(1); }
console.log("\nPASS: task-tracker-interactive handler (routing, real responder API, clear-buttons, stale-clean, topic-verbatim, edit-date rows, ignored no-op, missing-responder resilient)");
