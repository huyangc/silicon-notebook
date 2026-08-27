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
