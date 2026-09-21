import test from "node:test";
import assert from "node:assert/strict";

import { requestTaskStream } from "../../app/request-task-stream.ts";

const originalFetch = globalThis.fetch;
const originalWindow = globalThis.window;

function installWindow(withPaint = false) {
  globalThis.window = {
    localStorage: {
      getItem: () => null,
      setItem: () => {},
      removeItem: () => {},
    },
    location: { origin: "http://localhost" },
    ...(withPaint
      ? { requestAnimationFrame(callback) { callback(); return 1; } }
      : {}),
  };
}

function response(...events) {
  const encoder = new TextEncoder();
  return new Response(new ReadableStream({
    start(controller) {
      for (const event of events) {
        controller.enqueue(encoder.encode(`${JSON.stringify(event)}\n`));
      }
      controller.close();
    },
  }), { status: 200, headers: { "Content-Type": "application/x-ndjson" } });
}

test.afterEach(() => {
  globalThis.fetch = originalFetch;
  globalThis.window = originalWindow;
});

test("request task stream reports heartbeat progress and returns final result", async () => {
  installWindow(true);
  globalThis.fetch = async () => response(
    { event: "started", stage: "preview", elapsed_ms: 0 },
    { event: "heartbeat", stage: "preview", elapsed_ms: 5000 },
    { event: "final", stage: "preview", result: { ok: true } },
  );
  const beats = [];

  const result = await requestTaskStream("/preview", { tag: "test" }, {
    onHeartbeat: (elapsed, stage) => beats.push([elapsed, stage]),
  });

  assert.deepEqual(beats, [[0, "preview"], [5000, "preview"]]);
  assert.deepEqual(result, { ok: true });
});

test("request task stream never exposes a backend stream error as user text", async () => {
  installWindow();
  globalThis.fetch = async () => response(
    { event: "error", stage: "preview", error: "private upstream detail" },
  );

  await assert.rejects(
    requestTaskStream("/preview", { tag: "test" }, { fallbackMessage: "预览失败，请重试" }),
    { message: "预览失败，请重试" },
  );
});

test("request task stream keeps draining when requestAnimationFrame is suspended", async () => {
  installWindow();
  globalThis.window.requestAnimationFrame = () => 1;
  globalThis.fetch = async () => response(
    { event: "started", stage: "preview", elapsed_ms: 0 },
    { event: "final", stage: "preview", result: { ok: true } },
  );

  const result = await Promise.race([
    requestTaskStream("/preview", { tag: "test" }),
    new Promise((_, reject) => setTimeout(() => reject(new Error("stream stalled behind rAF")), 500)),
  ]);

  assert.deepEqual(result, { ok: true });
});


test("an error frame carrying a user refusal surfaces that sentence and its status", async () => {
  // 把校验放进了心跳覆盖的那段工作里的流，拒绝只能经错误帧带出来：有整句就原样给用户，
  // 不退成调用方的兜底文案（那句话与「模型服务挂了」无从区分）。
  installWindow(true);
  const { httpErrorStatus } = await import("../../app/errors.ts");
  globalThis.fetch = async () => response(
    { event: "started", stage: "global_ask_intent", elapsed_ms: 0 },
    { event: "error", stage: "global_ask_intent", error: "global_ask_intent_failed", status: 404, message: "对话不存在，请刷新列表。" },
  );
  await assert.rejects(
    requestTaskStream("/global-ask/intent/stream", { tag: "test" }, { fallbackMessage: "问题理解没能完成，请重试" }),
    (error) => error.message === "对话不存在，请刷新列表。" && httpErrorStatus(error) === 404,
  );
});

test("a content-free error frame and an early end both fall back, and are told apart in the console", async () => {
  installWindow(true);
  const logged = [];
  const originalError = console.error;
  console.error = (...args) => { logged.push(args.map(String).join(" ")); };
  try {
    globalThis.fetch = async () => response(
      { event: "started", stage: "global_ask_intent", elapsed_ms: 0 },
      { event: "error", stage: "global_ask_intent", error: "global_ask_intent_failed" },
    );
    await assert.rejects(
      requestTaskStream("/global-ask/intent/stream", { tag: "test" }, { fallbackMessage: "问题理解没能完成，请重试" }),
      /问题理解没能完成/,
    );
    globalThis.fetch = async () => response(
      { event: "started", stage: "global_ask_intent", elapsed_ms: 0 },
      { event: "heartbeat", stage: "global_ask_intent", elapsed_ms: 5000 },
    );
    await assert.rejects(
      requestTaskStream("/global-ask/intent/stream", { tag: "test" }, { fallbackMessage: "问题理解没能完成，请重试" }),
      /问题理解没能完成/,
    );
  } finally {
    console.error = originalError;
  }
  assert.ok(logged.some((line) => line.includes("global_ask_intent_failed")), logged.join("\n"));
  assert.ok(logged.some((line) => line.includes("ended-without-terminal-frame")), logged.join("\n"));
});
