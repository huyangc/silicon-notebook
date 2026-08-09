import test from "node:test";
import assert from "node:assert/strict";

import * as ask from "../../app/ask-api.ts";
import * as kg from "../../features/kg-maintenance/kg-api.ts";
import * as notebook from "../../app/notebook-api.ts";
import * as report from "../../app/report-api.ts";
import * as source from "../../app/source-api.ts";
import { probeReady } from "../../app/system-api.ts";

const originalFetch = globalThis.fetch;
const originalWindow = globalThis.window;
const originalDocument = globalThis.document;
const originalCreateObjectURL = URL.createObjectURL;
const originalRevokeObjectURL = URL.revokeObjectURL;

function streamResponse(...lines) {
  const encoder = new TextEncoder();
  return new Response(new ReadableStream({
    start(controller) {
      for (const line of lines) controller.enqueue(encoder.encode(`${line}\n`));
      controller.close();
    },
  }), { status: 200 });
}

function installWindow() {
  const storage = new Map();
  let reloads = 0;
  let paints = 0;
  globalThis.window = {
    localStorage: {
      getItem: (key) => storage.get(key) ?? null,
      setItem: (key, value) => storage.set(key, String(value)),
      removeItem: (key) => storage.delete(key),
    },
    location: { reload: () => { reloads += 1; } },
    requestAnimationFrame: (callback) => {
      paints += 1;
      callback();
      return 1;
    },
  };
  return { storage, reloads: () => reloads, paints: () => paints };
}

test.afterEach(() => {
  globalThis.fetch = originalFetch;
  globalThis.window = originalWindow;
  globalThis.document = originalDocument;
  URL.createObjectURL = originalCreateObjectURL;
  URL.revokeObjectURL = originalRevokeObjectURL;
});

test("domain clients are directly importable and Ask owns notebook search", async () => {
  const [system, notebook, source, ask, knowledge, report, kg] = await Promise.all([
    import("../../app/system-api.ts"),
    import("../../app/notebook-api.ts"),
    import("../../app/source-api.ts"),
    import("../../app/ask-api.ts"),
    import("../../app/knowledge-api.ts"),
    import("../../app/report-api.ts"),
    import("../../features/kg-maintenance/kg-api.ts"),
  ]);

  for (const module of [system, notebook, source, ask, knowledge, report, kg]) {
    assert.ok(Object.keys(module).length > 0);
  }
  assert.equal(typeof ask.searchNotebook, "function");
  assert.equal("searchNotebook" in notebook, false);
});

test("Ask stream consumes started progress final events and yields for paint", async () => {
  const { paints } = installWindow();
  const progress = [];
  const starts = [];
  let captured;
  globalThis.fetch = async (url, init) => {
    captured = { url, init };
    return streamResponse(
      JSON.stringify({ event: "started", job_id: "job-1", conversation_id: "conv-1" }),
      JSON.stringify({ event: "progress", step: { step_type: "retrieve", summary: "检索", detail: {} } }),
      JSON.stringify({ event: "final", response: { answer_id: "answer-1" } }),
    );
  };

  const result = await ask.runAskStream(
    "nb-1",
    { question: "what" },
    (step) => progress.push(step.summary),
    undefined,
    (jobId, conversationId) => starts.push([jobId, conversationId]),
  );

  assert.deepEqual(starts, [["job-1", "conv-1"]]);
  assert.deepEqual(progress, ["检索"]);
  assert.deepEqual(result, { answer_id: "answer-1" });
  assert.match(String(captured.url), /notebooks\/nb-1\/ask\/stream$/);
  assert.equal(captured.init.method, "POST");
  assert.equal(captured.init.body, JSON.stringify({ question: "what" }));
  assert.equal(paints(), 1);
});

test("Ask stream awaits started handling before consuming progress", async () => {
  installWindow();
  const order = [];
  globalThis.fetch = async () => streamResponse(
    JSON.stringify({ event: "started", job_id: "job-1", conversation_id: "conv-1" }),
    JSON.stringify({ event: "progress", step: { step_type: "retrieve", summary: "检索", detail: {} } }),
    JSON.stringify({ event: "final", response: { answer_id: "answer-1" } }),
  );

  await ask.runAskStream(
    "nb-1",
    { question: "what" },
    () => { order.push("progress"); },
    undefined,
    async () => {
      order.push("started");
      await new Promise((resolve) => setTimeout(resolve, 0));
      order.push("started-handled");
    },
  );

  assert.deepEqual(order, ["started", "started-handled", "progress"]);
});

test("Ask intent preview runs before the streaming endpoint and carries conversation context", async () => {
  installWindow();
  let captured;
  globalThis.fetch = async (url, init) => {
    captured = { url, init };
    return new Response(JSON.stringify({
      objective: "分析一下这个问题",
      resolved_question: "分析电荷泵 PLL",
      mandatory_topics: [],
      ambiguities: [],
      needs_clarification: false,
      confirmed: false,
    }), { status: 200, headers: { "Content-Type": "application/json" } });
  };

  const result = await ask.previewAskIntent(
    "nb-1", "分析一下这个问题", "conv-1",
  );

  assert.equal(result.resolved_question, "分析电荷泵 PLL");
  assert.match(String(captured.url), /notebooks\/nb-1\/ask\/intent$/);
  assert.equal(captured.init.method, "POST");
  assert.equal(captured.init.body, JSON.stringify({
    question: "分析一下这个问题",
    conversation_id: "conv-1",
  }));
});

test("Ask stream maps stream errors and cancellation to safe outcomes", async () => {
  globalThis.fetch = async () => streamResponse(
    JSON.stringify({ event: "error", error: "raw backend detail" }),
  );
  await assert.rejects(
    ask.runAskStream("nb-1", {}, () => {}),
    { message: "回答没能完成，请重试" },
  );

  globalThis.fetch = async () => streamResponse(JSON.stringify({ event: "cancelled" }));
  await assert.rejects(
    ask.runAskStream("nb-1", {}, () => {}),
    { name: "AbortError" },
  );
});

test("ready probe is unauthenticated no-store and fails open", async () => {
  const { storage } = installWindow();
  storage.set("silicon_notebook_token", "secret");
  let captured;
  globalThis.fetch = async (_url, init) => {
    captured = init;
    return new Response(JSON.stringify({ phase: "warming", detail: "loading" }), {
      status: 503,
      headers: { "Content-Type": "application/json" },
    });
  };
  assert.deepEqual(await probeReady(), {
    ready: false,
    phase: "warming",
    detail: "loading",
    warmed_notebooks: undefined,
    total_notebooks: undefined,
    preloaded_indexes: undefined,
    total_indexes: undefined,
    error: null,
  });
  assert.equal(captured.cache, "no-store");
  assert.equal(captured.headers.has("Authorization"), false);

  globalThis.fetch = async () => { throw new TypeError("offline"); };
  assert.equal(await probeReady(), null);
});

test("source APIs preserve multipart inference and return asset blobs without object URLs", async () => {
  installWindow();
  const objectUrls = [];
  URL.createObjectURL = (blob) => { objectUrls.push(blob); return "blob:unexpected"; };
  const calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push({ url, init });
    return String(url).includes("/assets/")
      ? new Response("asset-bytes", { status: 200 })
      : new Response(JSON.stringify([]), { status: 200 });
  };
  const body = new FormData();
  body.append("files", new Blob(["source"]), "notes.md");
  await source.uploadSources("nb-1", body);
  const blob = await source.fetchInternalAssetBlob("/notebooks/nb-1/assets/asset-1");

  assert.equal(calls[0].init.headers.has("Content-Type"), false);
  assert.equal(await blob.text(), "asset-bytes");
  assert.deepEqual(objectUrls, []);
});

test("report ZIP downloads with the fixed filename and revokes its URL", async () => {
  installWindow();
  globalThis.fetch = async () => new Response("zip", { status: 200 });
  const created = [];
  const revoked = [];
  const clicks = [];
  URL.createObjectURL = (blob) => { created.push(blob); return "blob:reports"; };
  URL.revokeObjectURL = (url) => revoked.push(url);
  globalThis.document = {
    createElement: () => ({ click() { clicks.push(this); } }),
  };

  await report.downloadReportsZip("nb-1", ["r-1"]);
  assert.equal(clicks[0].href, "blob:reports");
  assert.equal(clicks[0].download, "reports.zip");
  assert.deepEqual(revoked, ["blob:reports"]);
});

test("domain clients preserve representative queries, JSON bodies, and clear-session 401 policy", async () => {
  const { storage, reloads } = installWindow();
  storage.set("silicon_notebook_token", "secret");
  const calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push({ url, init });
    if (String(url).endsWith("/index-status")) return new Response(null, { status: 401 });
    return new Response(JSON.stringify({ hits: [] }), { status: 200 });
  };

  await ask.searchNotebook("nb 1", "a & b");
  await assert.rejects(kg.fetchIndexStatus("nb-1"));
  assert.match(String(calls[0].url), /notebooks\/nb%201\/search\?q=a%20%26%20b$/);
  assert.equal(storage.has("silicon_notebook_token"), false);
  assert.equal(reloads(), 1);
});
