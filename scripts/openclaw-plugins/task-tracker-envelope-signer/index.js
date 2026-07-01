// KTD8 task-tracker gateway chat envelope signer.
//
// SDK mechanism chosen: (A) OpenClaw plugins can register the broad
// `message_received` typed hook via `api.on("message_received", ...)`. The SDK
// event carries the inbound text plus senderId, messageId, channel metadata,
// threadId, and timestamp. This is not an interactive-callback-only plugin.
// `message_received` is fire-and-forget in the gateway, so this signer cannot
// block normal agent dispatch; it sends a best-effort ack through the channel
// outbound adapter when that surface is available. We intentionally do not use
// free-text completion inference. Only strict `done <task_id>` signs.
//
// Security invariant: a signed envelope is equivalent to an authorized board
// write request. The plugin signs only when all of these are true:
//   1. inbound text is exactly `done <task_id>` or `done task_id::<task_id>`;
//   2. sender identity exactly matches a configured commands.ownerAllowFrom
//      owner key (bare sender id, channel-prefixed sender id, or tg-prefixed
//      Telegram sender id; no wildcards and no DM allowlist);
//   3. inbound chatId/threadId is explicitly configured in
//      commands.envelopeSignerAllowChats;
//   4. the task id resolves to exactly one active, non-objective, non-recurring
//      board task by record.task_id;
//   5. capture_envelope.py sign returns a signed envelope.
// HMAC stays in scripts/capture_envelope.py. This JavaScript never computes or
// logs the secret.
import { spawn } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const LOG_PREFIX = "[task-tracker-envelope-signer]";
const DONE_RE = /^\s*done\s+(?:task_id::)?([A-Za-z0-9._:-]+)\s*$/i;
const MAX_STDOUT_CHARS = 1024 * 1024;
const MAX_STDERR_CHARS = 32 * 1024;

const HERE = dirname(fileURLToPath(import.meta.url));
const DEFAULT_REPO = join(HERE, "..", "..", "..");

// Backlog:
// - adv-6: add an in-plugin concurrency throttle keyed by senderId.
// - adv-8: evaluate nonce-consume-only-on-success for verified envelope fallbacks.
// - sec-2: include conversation id in replay keys if the capture surface expands
//   beyond the explicit chat/topic allowlist.

const RESOLVE_TASK_SCRIPT = String.raw`
import json
import re
import sys
from task_records import active_records, load_records

TASK_ID = sys.argv[1].strip()
RECURRING_RE = re.compile(r"\brecur\s*::", re.I)

_tasks_file, _content, records = load_records(False)
active = []
done_count = 0
for record in records:
    if record.task_id != TASK_ID or record.is_objective:
        continue
    if record.done:
        done_count += 1
        continue
    if record in active_records([record]):
        active.append({
            "task_id": record.task_id,
            "title": record.title,
            "recurring": bool(record.recur) or bool(RECURRING_RE.search(record.raw_line or "")),
        })

print(json.dumps({"ok": True, "active": active, "done_count": done_count}, separators=(",", ":")))
`;

function repoRoot() {
  return process.env.TASK_TRACKER_REPO || DEFAULT_REPO;
}

function pythonBin() {
  return process.env.TASK_TRACKER_PYTHON || "python3";
}

function captureEnvelopePath() {
  return join(repoRoot(), "scripts", "capture_envelope.py");
}

function tasksPath() {
  return join(repoRoot(), "scripts", "tasks.py");
}

function scriptsDir() {
  return join(repoRoot(), "scripts");
}

function safeMessage(error) {
  const raw = error && typeof error === "object" && "message" in error ? error.message : String(error || "");
  return raw.split("\n")[0].trim() || "unknown error";
}

function trimAppend(current, chunk, maxChars) {
  if (current.length >= maxChars) return current;
  const next = current + String(chunk);
  return next.length > maxChars ? next.slice(0, maxChars) : next;
}

export function runProcess(command, args, options = {}) {
  const spawnImpl = options.spawnImpl || spawn;
  return new Promise((resolve) => {
    let stdout = "";
    let stderr = "";
    let child;
    try {
      child = spawnImpl(command, args, {
        cwd: options.cwd,
        env: options.env || process.env,
        stdio: ["ignore", "pipe", "pipe"],
      });
    } catch (error) {
      resolve({ code: -1, stdout: "", stderr: "", error });
      return;
    }

    child.stdout?.on?.("data", (chunk) => {
      stdout = trimAppend(stdout, chunk, MAX_STDOUT_CHARS);
    });
    child.stderr?.on?.("data", (chunk) => {
      stderr = trimAppend(stderr, chunk, MAX_STDERR_CHARS);
    });
    child.on?.("error", (error) => {
      resolve({ code: -1, stdout, stderr, error });
    });
    child.on?.("close", (code) => {
      resolve({ code: code ?? -1, stdout, stderr });
    });
  });
}

export function parseLastJson(stdout) {
  const raw = String(stdout || "").trim();
  if (!raw) return null;
  try {
    return JSON.parse(raw);
  } catch {
    // Fall through.
  }
  const lines = raw.split("\n");
  for (let i = lines.length - 1; i >= 0; i--) {
    const line = lines[i].trim();
    if (!line.startsWith("{")) continue;
    try {
      return JSON.parse(line);
    } catch {
      // Try an enclosing block below.
    }
  }
  const start = raw.indexOf("{");
  const end = raw.lastIndexOf("}");
  if (start !== -1 && end > start) {
    try {
      return JSON.parse(raw.slice(start, end + 1));
    } catch {
      return null;
    }
  }
  return null;
}

export function decodeDoneIntent(text) {
  const match = DONE_RE.exec(String(text || ""));
  return match ? { taskId: match[1] } : null;
}

function normalizeString(value) {
  return typeof value === "string" || typeof value === "number" ? String(value).trim() : "";
}

function ownerList(config) {
  const raw = config?.commands?.ownerAllowFrom;
  if (!Array.isArray(raw)) return [];
  return raw.map(normalizeString).filter(Boolean);
}

function chatAllowList(config) {
  const raw = config?.commands?.envelopeSignerAllowChats;
  if (!Array.isArray(raw)) return [];
  return raw;
}

function ownerCandidateKeys(senderId, channel) {
  const sender = normalizeString(senderId);
  const normalizedChannel = normalizeString(channel).toLowerCase();
  if (!sender) return [];
  const keys = [sender];
  if (normalizedChannel) {
    keys.push(`${normalizedChannel}:${sender}`);
    if (normalizedChannel === "telegram") keys.push(`tg:${sender}`);
  }
  return keys;
}

export function verifyOwner({ config, event = {}, ctx = {} }) {
  const owners = ownerList(config);
  if (owners.length === 0) {
    return { ok: false, reason: "owner-list-unavailable" };
  }
  const senderId = normalizeString(event.senderId) || normalizeString(ctx.senderId);
  const channel = normalizeString(event.channel) || normalizeString(ctx.channelId);
  const ownerSet = new Set(owners);
  for (const key of ownerCandidateKeys(senderId, channel)) {
    if (key !== "*" && ownerSet.has(key)) {
      return { ok: true, senderId, matchedOwner: key };
    }
  }
  return { ok: false, reason: "sender-not-owner", senderId };
}

function chatIdFor(event = {}, ctx = {}) {
  return (
    normalizeString(event?.chatId) ||
    normalizeString(event?.chat_id) ||
    normalizeString(event?.metadata?.chatId) ||
    normalizeString(event?.metadata?.chat_id) ||
    normalizeString(event?.metadata?.originatingTo) ||
    normalizeString(event?.metadata?.to) ||
    normalizeString(ctx?.chatId) ||
    normalizeString(ctx?.chat_id) ||
    normalizeString(ctx?.conversationId) ||
    normalizeString(event?.from)
  );
}

function threadIdFor(event = {}, ctx = {}) {
  return (
    normalizeString(event?.threadId) ||
    normalizeString(event?.topicId) ||
    normalizeString(event?.messageThreadId) ||
    normalizeString(event?.metadata?.threadId) ||
    normalizeString(event?.metadata?.topicId) ||
    normalizeString(event?.metadata?.messageThreadId) ||
    normalizeString(ctx?.threadId) ||
    normalizeString(ctx?.topicId) ||
    normalizeString(ctx?.messageThreadId)
  );
}

function chatMatchesAllowedEntry(entry, chatId, threadId) {
  if (typeof entry === "string" || typeof entry === "number") {
    return normalizeString(entry) === chatId;
  }
  if (!entry || typeof entry !== "object" || Array.isArray(entry)) return false;
  const allowedChatId = normalizeString(entry.chatId) || normalizeString(entry.chat_id);
  if (!allowedChatId || allowedChatId !== chatId) return false;
  const allowedThreadId =
    normalizeString(entry.threadId) ||
    normalizeString(entry.thread_id) ||
    normalizeString(entry.topicId) ||
    normalizeString(entry.topic_id);
  return !allowedThreadId || allowedThreadId === threadId;
}

export function verifyAllowedChat({ config, event = {}, ctx = {} }) {
  const allowlist = chatAllowList(config);
  if (allowlist.length === 0) {
    return { ok: false, reason: "chat-allowlist-unavailable" };
  }
  const chatId = chatIdFor(event, ctx);
  const threadId = threadIdFor(event, ctx);
  if (!chatId) {
    return { ok: false, reason: "chat-not-allowlisted", chatId, threadId };
  }
  if (allowlist.some((entry) => chatMatchesAllowedEntry(entry, chatId, threadId))) {
    return { ok: true, chatId, threadId };
  }
  return { ok: false, reason: "chat-not-allowlisted", chatId, threadId };
}

function isForwardedMessage(event = {}, ctx = {}) {
  const metadata = event?.metadata || {};
  return Boolean(
    event?.isForwarded ||
      event?.forwardOrigin ||
      event?.forward_from ||
      event?.forward_date ||
      event?.forwardFrom ||
      event?.forwardDate ||
      metadata?.isForwarded ||
      metadata?.forwardOrigin ||
      metadata?.forward_from ||
      metadata?.forward_date ||
      metadata?.forwardFrom ||
      metadata?.forwardDate ||
      ctx?.isForwarded ||
      ctx?.forwardOrigin ||
      ctx?.forward_from ||
      ctx?.forward_date,
  );
}

function isoTimestamp(value, now = new Date()) {
  if (typeof value === "number" && Number.isFinite(value)) {
    const millis = Math.abs(value) < 10_000_000_000 ? value * 1000 : value;
    return new Date(millis).toISOString();
  }
  const raw = normalizeString(value);
  if (raw) {
    const parsed = new Date(raw);
    if (!Number.isNaN(parsed.getTime())) return parsed.toISOString();
  }
  return new Date(now).toISOString();
}

function channelName(event, ctx) {
  return normalizeString(event?.channel) || normalizeString(ctx?.channelId);
}

function messageIdFor(event, ctx) {
  return (
    normalizeString(event?.messageId) ||
    normalizeString(ctx?.messageId) ||
    normalizeString(event?.metadata?.messageId)
  );
}

function ackTarget(event, ctx) {
  return (
    normalizeString(event?.metadata?.originatingTo) ||
    normalizeString(event?.metadata?.to) ||
    normalizeString(ctx?.conversationId) ||
    normalizeString(event?.from)
  );
}

export async function resolveTaskId(taskId, options = {}) {
  const result = await runProcess(
    pythonBin(),
    ["-c", RESOLVE_TASK_SCRIPT, taskId],
    {
      cwd: scriptsDir(),
      env: { ...process.env, HOME: process.env.HOME || "/data" },
      spawnImpl: options.spawnImpl,
    },
  );
  const parsed = parseLastJson(result.stdout);
  if (result.code !== 0 || !parsed || parsed.ok !== true || !Array.isArray(parsed.active)) {
    return { ok: false, reason: "resolve-failed" };
  }
  if (parsed.active.length === 0) {
    return { ok: false, reason: parsed.done_count > 0 ? "already-done" : "not-found" };
  }
  if (parsed.active.length !== 1) {
    return { ok: false, reason: "ambiguous" };
  }
  const match = parsed.active[0];
  if (match?.recurring === true) {
    return { ok: false, reason: "recurring-task" };
  }
  const resolvedId = normalizeString(match?.task_id);
  if (!resolvedId) {
    return { ok: false, reason: "resolve-failed" };
  }
  return { ok: true, taskId: resolvedId, title: normalizeString(match?.title) };
}

export async function signEnvelope(envelope, options = {}) {
  const result = await runProcess(
    pythonBin(),
    [captureEnvelopePath(), "sign", "--json", JSON.stringify(envelope)],
    {
      cwd: repoRoot(),
      env: { ...process.env, HOME: process.env.HOME || "/data" },
      spawnImpl: options.spawnImpl,
    },
  );
  const signed = parseLastJson(result.stdout);
  if (result.code !== 0 || !signed || typeof signed.sig !== "string" || !signed.sig) {
    throw new Error("envelope signing failed");
  }
  return signed;
}

export async function invokeCapture(signedEnvelope, options = {}) {
  const signedJson = JSON.stringify(signedEnvelope);
  const result = await runProcess(
    pythonBin(),
    [tasksPath(), "capture", "--envelope", signedJson],
    {
      cwd: repoRoot(),
      env: { ...process.env, HOME: process.env.HOME || "/data" },
      spawnImpl: options.spawnImpl,
    },
  );
  const parsed = parseLastJson(result.stdout);
  return {
    ok: result.code === 0 && parsed && parsed.ok !== false,
    code: result.code,
    result: parsed,
  };
}

export function ackForReason(reason) {
  switch (reason) {
    case "auto":
      return "Auto-completed the task.";
    case "already-done":
      return "That task is already done.";
    case "not-found":
    case "auto-task-not-found":
      return "Couldn't find that active task. Send a fresh `done <id>` message if you retry.";
    case "ambiguous":
      return "Couldn't identify exactly one active task for that id.";
    case "recurring-task":
      return "Recurring tasks are not auto-completed from chat. Send a fresh `done <id>` message if you retry.";
    case "autowrite-disabled":
      return "Auto-complete is disabled. Send a fresh `done <id>` message after it is enabled.";
    case "missing-message-id":
      return "Couldn't verify the message id, so nothing was auto-completed.";
    case "capture-failed":
    case "auto-complete-failed":
      return "Couldn't auto-complete that task; it may have changed. Send a fresh `done <id>` message if you retry.";
    default:
      return "Couldn't auto-complete that task.";
  }
}

export function ackForCapture(capture) {
  const result = capture?.result || {};
  if (capture?.ok && result.action === "auto") {
    return ackForReason("auto");
  }
  return ackForReason(result.decision_reason || result.envelope_fallback_reason || "capture-failed");
}

async function sendAck(api, event, ctx, text) {
  if (!api || !text) return;
  try {
    const target = ackTarget(event, ctx);
    if (!target) return;
    const channel = channelName(event, ctx);
    const adapter = await api.runtime?.channel?.outbound?.loadAdapter?.(channel);
    if (!adapter?.sendText) return;
    await adapter.sendText({
      cfg: api.config || {},
      to: target,
      text,
      accountId: ctx?.accountId || event?.accountId || null,
      threadId: event?.threadId ?? null,
    });
  } catch (error) {
    console.error(`${LOG_PREFIX} ack send failed: ${safeMessage(error)}`);
  }
}

function baseResult(overrides = {}) {
  return {
    handled: false,
    matched: false,
    signed: false,
    captureInvoked: false,
    ack: null,
    reason: "ignored",
    ...overrides,
  };
}

export async function handleInboundMessage(event = {}, ctx = {}, options = {}) {
  const api = options.api;
  try {
    const intent = decodeDoneIntent(event?.content ?? event?.body ?? "");
    if (!intent) return baseResult();

    const channel = channelName(event, ctx).toLowerCase();
    if (channel !== "telegram") {
      return baseResult({ matched: true, reason: "unsupported-channel", taskId: intent.taskId });
    }

    const config = options.config || api?.config || {};
    const owner = verifyOwner({ config, event, ctx });
    if (!owner.ok) {
      console.info(`${LOG_PREFIX} strict done ignored: ${owner.reason}`);
      return baseResult({ handled: true, matched: true, reason: owner.reason });
    }

    const allowedChat = verifyAllowedChat({ config, event, ctx });
    if (!allowedChat.ok) {
      console.info(`${LOG_PREFIX} strict done ignored: ${allowedChat.reason}`);
      return baseResult({ handled: true, matched: true, reason: allowedChat.reason, taskId: intent.taskId });
    }

    if (isForwardedMessage(event, ctx)) {
      console.info(`${LOG_PREFIX} strict done ignored: forwarded-message`);
      return baseResult({ handled: true, matched: true, reason: "forwarded-message", taskId: intent.taskId });
    }

    const messageId = messageIdFor(event, ctx);
    if (!messageId) {
      const ack = ackForReason("missing-message-id");
      await (options.sendAck || sendAck)(api, event, ctx, ack);
      return baseResult({ handled: true, matched: true, reason: "missing-message-id", ack });
    }

    const resolved = await resolveTaskId(intent.taskId, options);
    if (!resolved.ok) {
      const ack = ackForReason(resolved.reason);
      await (options.sendAck || sendAck)(api, event, ctx, ack);
      return baseResult({ handled: true, matched: true, reason: resolved.reason, taskId: intent.taskId, ack });
    }

    const unsignedEnvelope = {
      v: 1,
      sender: owner.senderId,
      channel,
      message_id: messageId,
      timestamp: isoTimestamp(event?.timestamp, options.now || new Date()),
      task_id: resolved.taskId,
      intent: "complete",
    };

    const signedEnvelope = await signEnvelope(unsignedEnvelope, options);
    const capture = await invokeCapture(signedEnvelope, options);
    const ack = ackForCapture(capture);
    await (options.sendAck || sendAck)(api, event, ctx, ack);

    return baseResult({
      handled: true,
      matched: true,
      signed: true,
      captureInvoked: true,
      reason: capture.ok ? "capture-invoked" : "capture-failed",
      taskId: resolved.taskId,
      envelope: signedEnvelope,
      capture: capture.result,
      ack,
    });
  } catch (error) {
    console.error(`${LOG_PREFIX} signing flow failed: ${safeMessage(error)}`);
    return baseResult({ handled: true, matched: true, reason: "error", ack: ackForReason("error") });
  }
}

export default {
  id: "task-tracker-envelope-signer",
  name: "Task Tracker Envelope Signer",
  description:
    "Owner-verifies strict done <task_id> inbound messages, signs capture envelopes via capture_envelope.py, and invokes task-tracker capture --envelope. Non-owner and free prose never sign.",
  configSchema: {
    type: "object",
    additionalProperties: true,
    properties: {
      commands: {
        type: "object",
        additionalProperties: true,
        properties: {
          ownerAllowFrom: { type: "array", items: { type: ["string", "number"] } },
          envelopeSignerAllowChats: {
            type: "array",
            items: {
              anyOf: [
                { type: ["string", "number"] },
                {
                  type: "object",
                  additionalProperties: true,
                  required: ["chatId"],
                  properties: {
                    chatId: { type: ["string", "number"] },
                    threadId: { type: ["string", "number"] },
                    topicId: { type: ["string", "number"] },
                  },
                },
              ],
            },
          },
        },
      },
    },
  },
  register(api) {
    api.on("message_received", async (event, ctx) => {
      await handleInboundMessage(event, ctx, { api });
    }, { timeoutMs: 30_000 });
  },
};
