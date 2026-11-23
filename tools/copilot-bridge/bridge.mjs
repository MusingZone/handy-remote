// Copilot bridge: serves your GitHub Copilot models as a small OpenAI-compatible API on this
// computer (http://127.0.0.1:47870/v1), so Handy Remote's "refine before paste" - or any other
// OpenAI-compatible client - can use them. Built on the official GitHub Copilot SDK.
//
//   GET  /v1/models             models your Copilot plan allows
//   POST /v1/chat/completions   one answer per request, no tools, nothing remembered
//   GET  /health
//
// Only loopback connections are served, browser requests (with an Origin header) are refused,
// and dictated text is never written to the log.

import http from "node:http";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { randomUUID } from "node:crypto";
import { fileURLToPath } from "node:url";

export const DEFAULT_PORT = 47870;
const EFFORT_ORDER = [
  "none",
  "minimal",
  "low",
  "medium",
  "high",
  "xhigh",
  "max",
];
const MODEL_CACHE_MS = 10 * 60 * 1000;
const MAX_BODY = 1 << 20;

/** The reasoning effort to ask for: the requested one if the model has it, otherwise the
 *  model's lowest; nothing for models without reasoning control or when none was requested. */
export function pickEffort(requested, supported) {
  if (!requested || !Array.isArray(supported) || supported.length === 0)
    return undefined;
  if (supported.includes(requested)) return requested;
  const known = supported.filter((e) => EFFORT_ORDER.includes(e));
  known.sort((a, b) => EFFORT_ORDER.indexOf(a) - EFFORT_ORDER.indexOf(b));
  return known[0] ?? supported[0];
}

function text(content) {
  if (typeof content === "string") return content;
  if (Array.isArray(content))
    return content
      .map((p) =>
        typeof p === "string" ? p : p?.type === "text" ? (p.text ?? "") : "",
      )
      .join("");
  return "";
}

function httpError(status, message, type = "invalid_request_error") {
  return Object.assign(new Error(message), { status, type });
}

/** OpenAI `messages` → { system, prompt } for a single Copilot turn. */
export function toPrompt(messages) {
  if (!Array.isArray(messages) || messages.length === 0)
    throw httpError(400, "messages is empty");
  const isSystem = (m) => m.role === "system" || m.role === "developer";
  const system = messages.filter(isSystem).map((m) => text(m.content));
  const turns = messages.filter((m) => !isSystem(m));
  if (turns.length === 0) throw httpError(400, "messages has no user message");
  const prompt =
    turns.length === 1
      ? text(turns[0].content)
      : turns
          .map(
            (m) =>
              `${m.role === "assistant" ? "Assistant" : "User"}: ${text(m.content)}`,
          )
          .join("\n\n");
  return { system: system.join("\n\n").trim(), prompt };
}

function requestedEffort(body) {
  if (typeof body.reasoning_effort === "string") return body.reasoning_effort;
  if (typeof body.reasoning?.effort === "string") return body.reasoning.effort;
  if (body.thinking?.type === "disabled") return "none";
  return undefined;
}

export class Bridge {
  /** @param {{ createClient: () => any, log?: (line: string) => void }} deps */
  constructor({ createClient, log = () => {} }) {
    this.createClient = createClient;
    this.log = log;
    this.client = null;
    this.starting = null;
    this.models = null;
    this.modelsAt = 0;
  }

  async getClient() {
    if (this.client) return this.client;
    if (!this.starting) {
      this.starting = (async () => {
        const c = this.createClient();
        await c.start();
        this.client = c;
        return c;
      })().finally(() => {
        this.starting = null;
      });
    }
    return this.starting;
  }

  async listModels() {
    if (this.models && Date.now() - this.modelsAt < MODEL_CACHE_MS)
      return this.models;
    const client = await this.getClient();
    const all = await client.listModels();
    this.models = all.filter((m) => !m.policy || m.policy.state === "enabled");
    this.modelsAt = Date.now();
    return this.models;
  }

  async complete(body) {
    if (body.n && body.n !== 1) throw httpError(400, "only n = 1 is supported");
    const model = body.model;
    if (!model || typeof model !== "string")
      throw httpError(400, "model is required");
    const models = await this.listModels();
    const info = models.find((m) => m.id === model);
    if (!info && model !== "auto")
      throw httpError(
        404,
        `model '${model}' is not available to this Copilot account`,
        "model_not_found",
      );
    const { system, prompt } = toPrompt(body.messages);
    const effort = pickEffort(
      requestedEffort(body),
      info?.capabilities?.supports?.reasoning_effort,
    );

    const client = await this.getClient();
    const started = Date.now();
    let session;
    try {
      session = await client.createSession({
        model,
        ...(effort ? { reasoningEffort: effort } : {}),
        availableTools: [],
        systemMessage: {
          mode: "replace",
          content: system || "You are a helpful assistant.",
        },
        infiniteSessions: { enabled: false },
        enableSessionStore: false,
        enableSessionTelemetry: false,
        onPermissionRequest: () => ({ kind: "reject" }),
      });
    } catch (err) {
      // A dead runtime fails here; start a fresh one on the next request.
      await this.stop();
      throw err;
    }
    let usage = null;
    const off = session.on?.("session.usage_record", (e) => {
      usage = e.data?.usage ?? null;
    });
    try {
      const reply = await session.sendAndWait({ prompt }, 120_000);
      const content = reply?.data?.content ?? "";
      this.log(
        `${model}${effort ? ` effort=${effort}` : ""} in=${prompt.length} out=${content.length} ${Date.now() - started} ms`,
      );
      const inTok = usage?.inputTokens ?? 0;
      const outTok = usage?.outputTokens ?? 0;
      return {
        id: `chatcmpl-${randomUUID()}`,
        object: "chat.completion",
        created: Math.floor(Date.now() / 1000),
        model,
        choices: [
          {
            index: 0,
            message: { role: "assistant", content },
            finish_reason: "stop",
          },
        ],
        usage: {
          prompt_tokens: inTok,
          completion_tokens: outTok,
          total_tokens: inTok + outTok,
        },
      };
    } finally {
      off?.();
      await session.disconnect().catch(() => {});
      await Promise.resolve(client.deleteSession?.(session.sessionId)).catch(
        () => {},
      );
    }
  }

  async stop() {
    const c = this.client;
    this.client = null;
    if (c) await c.stop().catch(() => {});
  }
}

function send(res, status, obj) {
  const data = JSON.stringify(obj);
  res.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "content-length": Buffer.byteLength(data),
  });
  res.end(data);
}

function sendError(res, err) {
  send(res, err.status ?? 502, {
    error: {
      message: err.message,
      type: err.type ?? "upstream_error",
      code: null,
    },
  });
}

function readJson(req) {
  return new Promise((resolve, reject) => {
    let size = 0;
    const chunks = [];
    req.on("data", (c) => {
      size += c.length;
      if (size > MAX_BODY) {
        reject(httpError(413, "request too large"));
        req.destroy();
      } else chunks.push(c);
    });
    req.on("end", () => {
      try {
        resolve(JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}"));
      } catch {
        reject(httpError(400, "body is not JSON"));
      }
    });
    req.on("error", reject);
  });
}

/** Loopback peers only, a loopback Host (no DNS rebinding), no browser origins, optional token. */
export function admit(req, token) {
  const peer = req.socket.remoteAddress ?? "";
  if (!["127.0.0.1", "::1", "::ffff:127.0.0.1"].includes(peer))
    return httpError(403, "loopback only", "forbidden");
  const host = (req.headers.host ?? "")
    .replace(/:\d+$/, "")
    .replace(/^\[|\]$/g, "")
    .toLowerCase();
  if (!["127.0.0.1", "localhost", "::1"].includes(host))
    return httpError(403, "bad host", "forbidden");
  if (req.headers.origin)
    return httpError(403, "browser requests are not allowed", "forbidden");
  if (token && req.headers.authorization !== `Bearer ${token}`)
    return httpError(401, "wrong or missing token", "unauthorized");
  return null;
}

export function createServer(bridge, { token } = {}) {
  return http.createServer(async (req, res) => {
    const denied = admit(req, token);
    if (denied) return sendError(res, denied);
    const url = (req.url ?? "/").split("?")[0].replace(/\/+$/, "");
    try {
      if (req.method === "GET" && url === "/health") {
        const models = await bridge.listModels();
        return send(res, 200, { ok: true, models: models.length });
      }
      if (req.method === "GET" && url === "/v1/models") {
        const models = await bridge.listModels();
        return send(res, 200, {
          object: "list",
          data: models.map((m) => ({
            id: m.id,
            object: "model",
            created: 0,
            owned_by: "github-copilot",
            name: m.name,
          })),
        });
      }
      if (req.method === "POST" && url === "/v1/chat/completions") {
        const body = await readJson(req);
        const out = await bridge.complete(body);
        if (!body.stream) return send(res, 200, out);
        // Clients that insist on streaming get the whole answer as one chunk.
        res.writeHead(200, {
          "content-type": "text/event-stream",
          "cache-control": "no-cache",
        });
        const chunk = {
          ...out,
          object: "chat.completion.chunk",
          choices: [
            { index: 0, delta: out.choices[0].message, finish_reason: "stop" },
          ],
        };
        res.write(`data: ${JSON.stringify(chunk)}\n\ndata: [DONE]\n\n`);
        return res.end();
      }
      sendError(res, httpError(404, "not found", "not_found"));
    } catch (err) {
      if (!err.status || err.status >= 500) bridge.log(`error: ${err.message}`);
      sendError(
        res,
        err.status
          ? err
          : httpError(502, `Copilot: ${err.message}`, "upstream_error"),
      );
    }
  });
}

function fileLogger(file) {
  return (line) => {
    try {
      if (fs.existsSync(file) && fs.statSync(file).size > 1 << 20)
        fs.renameSync(file, `${file}.1`);
      fs.appendFileSync(file, `${new Date().toISOString()} ${line}\n`);
    } catch {
      // Logging must never break a request.
    }
  };
}

async function main() {
  const { CopilotClient } = await import("@github/copilot-sdk");
  const home =
    process.env.COPILOT_BRIDGE_HOME ??
    path.join(process.env.LOCALAPPDATA ?? os.homedir(), "copilot-bridge");
  fs.mkdirSync(home, { recursive: true });
  const log = fileLogger(path.join(home, "bridge.log"));
  const port = Number(process.env.COPILOT_BRIDGE_PORT ?? DEFAULT_PORT);
  const token = process.env.COPILOT_BRIDGE_TOKEN || undefined;
  const bridge = new Bridge({
    log,
    createClient: () =>
      new CopilotClient({
        mode: "empty",
        baseDirectory: path.join(home, "runtime"),
        logLevel: "error",
        env: { ...process.env, COPILOT_TELEMETRY_DISABLED: "true" },
      }),
  });
  const server = createServer(bridge, { token });
  server.on("error", (e) => {
    log(`cannot listen on 127.0.0.1:${port}: ${e.message}`);
    process.exit(1);
  });
  server.listen(port, "127.0.0.1", () =>
    log(`listening on http://127.0.0.1:${port}/v1 (pid ${process.pid})`),
  );
  // Start the runtime now so the first dictation does not wait for it.
  bridge.listModels().then(
    (m) => log(`signed in, ${m.length} models`),
    (e) => log(`Copilot not ready yet: ${e.message}`),
  );
  const quit = async () => {
    server.close();
    await bridge.stop();
    process.exit(0);
  };
  process.on("SIGINT", quit);
  process.on("SIGTERM", quit);
}

if (
  process.argv[1] &&
  path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)
)
  main();
