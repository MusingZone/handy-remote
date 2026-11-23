import { test } from "node:test";
import assert from "node:assert/strict";
import { Bridge, createServer, pickEffort, toPrompt } from "./bridge.mjs";

function fakeClient(calls) {
  return {
    async start() {},
    async stop() {},
    async listModels() {
      return [
        {
          id: "small",
          name: "Small",
          capabilities: { supports: { reasoning_effort: ["low", "medium"] } },
        },
        { id: "plain", name: "Plain", capabilities: { supports: {} } },
        { id: "blocked", name: "Blocked", policy: { state: "disabled" } },
      ];
    },
    async createSession(cfg) {
      calls.push(cfg);
      const handlers = {};
      return {
        sessionId: "s1",
        on(type, fn) {
          handlers[type] = fn;
          return () => {};
        },
        async sendAndWait({ prompt }) {
          handlers["session.usage_record"]?.({
            data: { usage: { inputTokens: 7, outputTokens: 3 } },
          });
          return { data: { content: `echo:${prompt}` } };
        },
        async disconnect() {},
      };
    },
    async deleteSession() {},
  };
}

async function withServer(fn, opts) {
  const calls = [];
  const server = createServer(
    new Bridge({ createClient: () => fakeClient(calls) }),
    opts,
  );
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  const base = `http://127.0.0.1:${server.address().port}`;
  try {
    await fn(base, calls);
  } finally {
    server.close();
  }
}

test("lowest supported effort when the requested one is missing", () => {
  assert.equal(pickEffort("none", ["low", "medium", "high"]), "low");
  assert.equal(pickEffort("none", ["none", "low"]), "none");
  assert.equal(pickEffort("none", undefined), undefined);
  assert.equal(pickEffort(undefined, ["low"]), undefined);
});

test("system messages become the system prompt, a single user turn the prompt", () => {
  const p = toPrompt([
    { role: "system", content: "rules" },
    { role: "user", content: [{ type: "text", text: "hi" }] },
  ]);
  assert.deepEqual(p, { system: "rules", prompt: "hi" });
  assert.throws(
    () => toPrompt([{ role: "system", content: "x" }]),
    /no user message/,
  );
});

test("models lists only enabled models", async () => {
  await withServer(async (base) => {
    const r = await fetch(`${base}/v1/models`);
    const j = await r.json();
    assert.deepEqual(
      j.data.map((m) => m.id),
      ["small", "plain"],
    );
  });
});

test("chat completion: no tools, replaced system prompt, reasoning mapped", async () => {
  await withServer(async (base, calls) => {
    const r = await fetch(`${base}/v1/chat/completions`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        model: "small",
        reasoning_effort: "none",
        messages: [
          { role: "system", content: "S" },
          { role: "user", content: "第一点" },
        ],
      }),
    });
    const j = await r.json();
    assert.equal(r.status, 200);
    assert.equal(j.choices[0].message.content, "echo:第一点");
    assert.equal(j.usage.total_tokens, 10);
    assert.deepEqual(calls[0].availableTools, []);
    assert.deepEqual(calls[0].systemMessage, { mode: "replace", content: "S" });
    assert.equal(calls[0].reasoningEffort, "low");
  });
});

test("unknown model is a 404, browser origins and wrong tokens are refused", async () => {
  await withServer(async (base) => {
    const body = JSON.stringify({
      model: "nope",
      messages: [{ role: "user", content: "x" }],
    });
    let r = await fetch(`${base}/v1/chat/completions`, {
      method: "POST",
      body,
    });
    assert.equal(r.status, 404);
    r = await fetch(`${base}/v1/models`, {
      headers: { origin: "https://evil.example" },
    });
    assert.equal(r.status, 403);
  });
  await withServer(
    async (base) => {
      assert.equal((await fetch(`${base}/v1/models`)).status, 401);
      const ok = await fetch(`${base}/v1/models`, {
        headers: { authorization: "Bearer t0k" },
      });
      assert.equal(ok.status, 200);
    },
    { token: "t0k" },
  );
});
