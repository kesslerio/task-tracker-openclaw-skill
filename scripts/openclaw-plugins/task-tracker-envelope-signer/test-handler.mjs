// Unit test for task-tracker-envelope-signer.
// Run: node scripts/openclaw-plugins/task-tracker-envelope-signer/test-handler.mjs
import { EventEmitter } from "node:events";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { pathToFileURL } from "node:url";
import { join } from "node:path";

const HERE = new URL(".", import.meta.url).pathname;
const mod = await import(pathToFileURL(join(HERE, "index.js")).href);
const {
  decodeDoneIntent,
  handleInboundMessage,
  resolveTaskId,
  verifyAllowedChat,
  verifyOwner,
} = mod;
const plugin = mod.default;

let failures = 0;
function check(cond, msg) {
  if (cond) console.log(`  ok: ${msg}`);
  else {
    console.error(`  FAIL: ${msg}`);
    failures++;
  }
}
function eq(a, b, msg) {
  check(JSON.stringify(a) === JSON.stringify(b), `${msg} (got ${JSON.stringify(a)})`);
}

const OWNER = "123456";
const OTHER = "654321";
const CONFIG = {
  commands: {
    ownerAllowFrom: [OWNER, "telegram:777777"],
    envelopeSignerAllowChats: ["telegram:chat:42"],
  },
};
const BASE_EVENT = {
  content: "done tsk_live",
  channel: "telegram",
  senderId: OWNER,
  messageId: "msg-1",
  timestamp: "2026-07-01T12:00:00Z",
  metadata: { originatingTo: "telegram:chat:42" },
};
const BASE_CTX = {
  channelId: "telegram",
  accountId: "default",
  conversationId: "chat:42",
};

function makeSpawn(handler) {
  const calls = [];
  const fakeSpawn = (cmd, args, opts) => {
    const call = { cmd, args, opts };
    calls.push(call);
    const child = new EventEmitter();
    child.stdout = new EventEmitter();
    child.stderr = new EventEmitter();
    queueMicrotask(() => {
      let response;
      try {
        response = handler(call, calls);
      } catch (error) {
        child.emit("error", error);
        return;
      }
      if (response?.stdout) child.stdout.emit("data", Buffer.from(response.stdout));
      if (response?.stderr) child.stderr.emit("data", Buffer.from(response.stderr));
      child.emit("close", response?.code ?? 0);
    });
    return child;
  };
  fakeSpawn.calls = calls;
  return fakeSpawn;
}

function happySpawn() {
  return makeSpawn((call) => {
    if (call.args[0] === "-c") {
      return {
        stdout: JSON.stringify({
          ok: true,
          active: [{ task_id: call.args[2], title: "Live task", recurring: false }],
          done_count: 0,
        }),
      };
    }
    if (String(call.args[0]).endsWith("capture_envelope.py")) {
      const envelope = JSON.parse(call.args[3]);
      return { stdout: JSON.stringify({ ...envelope, sig: "signed-sig" }) };
    }
    if (String(call.args[0]).endsWith("tasks.py")) {
      return {
        stdout: JSON.stringify({
          ok: true,
          action: "auto",
          task_id: "tsk_live",
          completion_id: "evt_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        }),
      };
    }
    return { code: 99, stderr: "unexpected spawn" };
  });
}

function resolverSpawn(payload) {
  return makeSpawn((call) => {
    if (call.args[0] === "-c") return { stdout: JSON.stringify(payload) };
    return { stdout: JSON.stringify({ ok: false }) };
  });
}

console.log("SDK registration:");
{
  let registration = null;
  plugin.register({
    on(hookName, handler, opts) {
      registration = { hookName, handler, opts };
    },
  });
  check(registration?.hookName === "message_received", "plugin registers message_received typed hook");
  check(typeof registration?.handler === "function", "message_received handler is registered");
  check(registration?.opts?.timeoutMs === 30000, "handler has a bounded timeout");
}

console.log("intent + owner checks:");
eq(decodeDoneIntent("done tsk_abc"), { taskId: "tsk_abc" }, "strict done parses");
eq(decodeDoneIntent("  DONE task_id::tsk_abc-1  "), { taskId: "tsk_abc-1" }, "task_id:: prefix parses case-insensitively");
eq(decodeDoneIntent("finished the thing"), null, "free prose does not parse");
eq(decodeDoneIntent("closed tsk_abc"), null, "non-done verb does not parse");
eq(decodeDoneIntent("done"), null, "done with no id does not parse");
eq(decodeDoneIntent("done bad$id"), null, "invalid id does not parse");
check(verifyOwner({ config: CONFIG, event: BASE_EVENT, ctx: BASE_CTX }).ok, "bare owner id verifies");
check(
  verifyOwner({ config: CONFIG, event: { ...BASE_EVENT, senderId: "777777" }, ctx: BASE_CTX }).ok,
  "channel-prefixed owner entry verifies by exact key",
);
check(
  !verifyOwner({ config: CONFIG, event: { ...BASE_EVENT, senderId: OTHER }, ctx: BASE_CTX }).ok,
  "non-owner does not verify",
);
check(
  !verifyOwner({ config: { commands: { ownerAllowFrom: ["*"] } }, event: BASE_EVENT, ctx: BASE_CTX }).ok,
  "wildcard owner entry is not accepted",
);
check(verifyAllowedChat({ config: CONFIG, event: BASE_EVENT, ctx: BASE_CTX }).ok, "configured chat verifies");
check(
  verifyAllowedChat({
    config: { commands: { envelopeSignerAllowChats: [{ chatId: "telegram:chat:42", threadId: 7 }] } },
    event: { ...BASE_EVENT, threadId: 7 },
    ctx: BASE_CTX,
  }).ok,
  "configured chat+thread verifies",
);
check(
  !verifyAllowedChat({ config: { commands: { envelopeSignerAllowChats: [] } }, event: BASE_EVENT, ctx: BASE_CTX }).ok,
  "empty chat allowlist fails closed",
);

console.log("non-owner and ignored text:");
{
  const fakeSpawn = happySpawn();
  const acks = [];
  const result = await handleInboundMessage(
    { ...BASE_EVENT, senderId: OTHER },
    BASE_CTX,
    { config: CONFIG, spawnImpl: fakeSpawn, sendAck: async (_api, _event, _ctx, text) => acks.push(text) },
  );
  check(result.matched && !result.signed && !result.captureInvoked, "non-owner valid done is not signed or captured");
  eq(fakeSpawn.calls.length, 0, "non-owner does not spawn resolver/sign/capture");
  eq(acks.length, 0, "non-owner strict done emits zero acks");
}
for (const text of ["finished the thing", "closed tsk_live", "done", "done bad$id", "", null]) {
  const fakeSpawn = happySpawn();
  const result = await handleInboundMessage(
    { ...BASE_EVENT, content: text },
    BASE_CTX,
    { config: CONFIG, spawnImpl: fakeSpawn, sendAck: async () => {} },
  );
  check(!result.signed && !result.captureInvoked, `ignored text does not sign: ${JSON.stringify(text)}`);
  eq(fakeSpawn.calls.length, 0, `ignored text does not spawn: ${JSON.stringify(text)}`);
}
{
  const fakeSpawn = happySpawn();
  const result = await handleInboundMessage(
    { ...BASE_EVENT, channel: "discord" },
    { ...BASE_CTX, channelId: "discord" },
    { config: CONFIG, spawnImpl: fakeSpawn, sendAck: async () => {} },
  );
  check(result.reason === "unsupported-channel" && !result.signed, "non-Telegram strict done does not sign");
  eq(fakeSpawn.calls.length, 0, "non-Telegram strict done does not spawn");
}

console.log("owner happy path:");
{
  const fakeSpawn = happySpawn();
  const acks = [];
  const result = await handleInboundMessage(
    BASE_EVENT,
    BASE_CTX,
    { config: CONFIG, spawnImpl: fakeSpawn, sendAck: async (_api, _event, _ctx, text) => acks.push(text) },
  );
  check(result.signed && result.captureInvoked, "owner strict done signs and invokes capture");
  eq(fakeSpawn.calls.length, 3, "owner path spawns resolver, signer, capture");

  const signCall = fakeSpawn.calls[1];
  check(String(signCall.args[0]).endsWith("capture_envelope.py"), "sign call uses capture_envelope.py");
  eq(signCall.args.slice(1, 3), ["sign", "--json"], "sign call uses sign --json argv");
  const unsigned = JSON.parse(signCall.args[3]);
  eq(unsigned, {
    v: 1,
    sender: OWNER,
    channel: "telegram",
    message_id: "msg-1",
    timestamp: "2026-07-01T12:00:00.000Z",
    task_id: "tsk_live",
    intent: "complete",
  }, "unsigned envelope shape is exact");
  check(!("sig" in unsigned), "unsigned envelope passed to signer has no sig");

  const captureCall = fakeSpawn.calls[2];
  check(String(captureCall.args[0]).endsWith("tasks.py"), "capture call uses tasks.py");
  eq(captureCall.args.slice(1, 3), ["capture", "--envelope"], "capture call uses capture --envelope argv");
  const signed = JSON.parse(captureCall.args[3]);
  check(signed.sig === "signed-sig", "capture receives signed envelope with sig");
  eq(signed.task_id, "tsk_live", "capture envelope carries resolved task id");
  check(/Auto-completed/i.test(acks[0] || ""), "owner path sends auto-completed ack");
}
{
  const fakeSpawn = happySpawn();
  const acks = [];
  const result = await handleInboundMessage(
    { ...BASE_EVENT, channel: undefined },
    { ...BASE_CTX, channelId: "Telegram" },
    { config: CONFIG, spawnImpl: fakeSpawn, sendAck: async (_api, _event, _ctx, text) => acks.push(text) },
  );
  check(result.signed && result.captureInvoked, "Telegram channel casing still signs");
  const unsigned = JSON.parse(fakeSpawn.calls[1].args[3]);
  eq(unsigned.channel, "telegram", "signed envelope normalizes channel casing");
}

console.log("chat scope + forwarded skips:");
{
  const fakeSpawn = happySpawn();
  const acks = [];
  const result = await handleInboundMessage(
    { ...BASE_EVENT, metadata: { originatingTo: "telegram:chat:999" } },
    BASE_CTX,
    { config: CONFIG, spawnImpl: fakeSpawn, sendAck: async (_api, _event, _ctx, text) => acks.push(text) },
  );
  check(result.reason === "chat-not-allowlisted" && !result.signed, "non-allowlisted chat is not signed");
  eq(fakeSpawn.calls.length, 0, "non-allowlisted chat does not spawn");
  eq(acks.length, 0, "non-allowlisted chat emits zero acks");
}
{
  const fakeSpawn = happySpawn();
  const result = await handleInboundMessage(
    BASE_EVENT,
    BASE_CTX,
    {
      config: { commands: { ownerAllowFrom: [OWNER], envelopeSignerAllowChats: [] } },
      spawnImpl: fakeSpawn,
      sendAck: async () => {},
    },
  );
  check(result.reason === "chat-allowlist-unavailable" && !result.signed, "empty allowlist never signs");
  eq(fakeSpawn.calls.length, 0, "empty allowlist does not spawn");
}
{
  const fakeSpawn = happySpawn();
  const acks = [];
  const result = await handleInboundMessage(
    { ...BASE_EVENT, metadata: { ...BASE_EVENT.metadata, forwardOrigin: { type: "user" } } },
    BASE_CTX,
    { config: CONFIG, spawnImpl: fakeSpawn, sendAck: async (_api, _event, _ctx, text) => acks.push(text) },
  );
  check(result.reason === "forwarded-message" && !result.signed, "forwarded strict done is not signed");
  eq(fakeSpawn.calls.length, 0, "forwarded strict done does not spawn");
  eq(acks.length, 0, "forwarded strict done emits zero acks");
}

console.log("resolution failures:");
{
  const fakeSpawn = resolverSpawn({ ok: true, active: [], done_count: 0 });
  const result = await handleInboundMessage(
    { ...BASE_EVENT, content: "done tsk_missing" },
    BASE_CTX,
    { config: CONFIG, spawnImpl: fakeSpawn, sendAck: async () => {} },
  );
  check(result.reason === "not-found" && !result.signed && !result.captureInvoked, "nonexistent task is not signed");
  eq(fakeSpawn.calls.length, 1, "nonexistent task only runs resolver");
}
{
  const fakeSpawn = makeSpawn((call) => {
    if (call.args[0] === "-c") {
      return {
        stdout: JSON.stringify({
          ok: true,
          active: [{ task_id: call.args[2], title: "Live task", recurring: false }],
          done_count: 0,
        }),
      };
    }
    if (String(call.args[0]).endsWith("capture_envelope.py")) {
      const envelope = JSON.parse(call.args[3]);
      return { stdout: JSON.stringify({ ...envelope, sig: "signed-sig" }) };
    }
    if (String(call.args[0]).endsWith("tasks.py")) {
      return {
        stdout: JSON.stringify({
          ok: true,
          action: "candidate",
          task_id: "tsk_missing",
          decision_reason: "auto-task-not-found",
        }),
      };
    }
    return { code: 99, stderr: "unexpected spawn" };
  });
  const acks = [];
  const result = await handleInboundMessage(
    { ...BASE_EVENT, content: "done tsk_missing" },
    BASE_CTX,
    { config: CONFIG, spawnImpl: fakeSpawn, sendAck: async (_api, _event, _ctx, text) => acks.push(text) },
  );
  check(result.signed && result.captureInvoked, "verified capture fallback still invokes capture");
  check(/Couldn't find that active task/i.test(acks[0] || ""), "not-found fallback ack names the real reason");
  check(/fresh `done <id>` message/i.test(acks[0] || ""), "not-found fallback ack tells owner to send a fresh message");
}
{
  const fakeSpawn = resolverSpawn({ ok: true, active: [], done_count: 1 });
  const result = await handleInboundMessage(
    BASE_EVENT,
    BASE_CTX,
    { config: CONFIG, spawnImpl: fakeSpawn, sendAck: async () => {} },
  );
  check(result.reason === "already-done" && !result.signed, "already-done task is not signed");
}
{
  const fakeSpawn = resolverSpawn({ ok: true, active: [{ task_id: "tsk_live", recurring: true }], done_count: 0 });
  const result = await handleInboundMessage(
    BASE_EVENT,
    BASE_CTX,
    { config: CONFIG, spawnImpl: fakeSpawn, sendAck: async () => {} },
  );
  check(result.reason === "recurring-task" && !result.signed, "recurring task is not signed");
}
{
  const fakeSpawn = resolverSpawn({
    ok: true,
    active: [{ task_id: "tsk_live", recurring: false }, { task_id: "tsk_live", recurring: false }],
    done_count: 0,
  });
  const result = await handleInboundMessage(
    BASE_EVENT,
    BASE_CTX,
    { config: CONFIG, spawnImpl: fakeSpawn, sendAck: async () => {} },
  );
  check(result.reason === "ambiguous" && !result.signed, "duplicate active ids are not signed");
}

console.log("real resolver identity boundary:");
{
  const oldRepo = process.env.TASK_TRACKER_REPO;
  const oldWorkFile = process.env.TASK_TRACKER_WORK_FILE;
  const temp = mkdtempSync(join(tmpdir(), "tt-envelope-signer-"));
  const work = join(temp, "Work Tasks.md");
  writeFileSync(
    work,
    "# Work\n\n## 🔴 Q1\n- [ ] **Legacy only** id::legacy_x\n- [ ] **Real task** task_id::tsk_real\n",
  );
  process.env.TASK_TRACKER_REPO = join(HERE, "..", "..", "..");
  process.env.TASK_TRACKER_WORK_FILE = work;
  try {
    const legacy = await resolveTaskId("legacy_x");
    const real = await resolveTaskId("tsk_real");
    check(!legacy.ok && legacy.reason === "not-found", "legacy-id-only record does not resolve for signing");
    check(real.ok && real.taskId === "tsk_real", "real task_id record resolves for signing");
  } finally {
    if (oldRepo == null) delete process.env.TASK_TRACKER_REPO;
    else process.env.TASK_TRACKER_REPO = oldRepo;
    if (oldWorkFile == null) delete process.env.TASK_TRACKER_WORK_FILE;
    else process.env.TASK_TRACKER_WORK_FILE = oldWorkFile;
    rmSync(temp, { recursive: true, force: true });
  }
}

console.log("malformed input resilience:");
for (const event of [
  {},
  { content: "done tsk_live" },
  { content: "done tsk_live", senderId: OWNER },
  { content: ["done", "tsk_live"], senderId: OWNER, messageId: "msg-array" },
]) {
  const fakeSpawn = happySpawn();
  let threw = false;
  try {
    await handleInboundMessage(event, {}, { config: CONFIG, spawnImpl: fakeSpawn, sendAck: async () => {} });
  } catch {
    threw = true;
  }
  check(!threw, `malformed event never throws: ${JSON.stringify(event)}`);
}

if (failures) {
  console.error(`\nFAIL: ${failures} assertion(s) failed`);
  process.exit(1);
}
console.log("\nPASS: task-tracker-envelope-signer handler (message_received, owner verify, strict done, no free prose, no non-owner signing, mocked sign/capture)");
