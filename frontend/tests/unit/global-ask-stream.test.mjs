// `streamGlobalJob` 本体的行为——组件用例把整个导出换成了 mock，所以帧 → 结果的映射、
// 终态帧缺席、`error` 帧、`final` 的答案身份归一这些只在这里被真的跑到。
import test from "node:test";
import assert from "node:assert/strict";

const originalFetch = globalThis.fetch;
const originalWindow = globalThis.window;

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
  globalThis.window = {
    localStorage: {
      getItem: (key) => storage.get(key) ?? null,
      setItem: (key, value) => storage.set(key, String(value)),
      removeItem: (key) => storage.delete(key),
    },
    location: { reload() {} },
  };
}

test.afterEach(() => {
  globalThis.fetch = originalFetch;
  globalThis.window = originalWindow;
});

const progressFrame = (offset, summaries) => JSON.stringify({
  event: "progress", trace_offset: offset,
  steps: summaries.map((summary) => ({ step_type: "search", summary, detail: {} })),
  searched_notebook_ids: ["nb-0"], skipped_notebooks: [], degraded_notebook_ids: [],
});
const jobFrame = { job_id: "gask-1", conversation_id: "gconv-1", status: "done", question: "q",
  answer: { answer_id: "", answer: "结论" } };

test("frames map to progress callbacks and a final outcome with its answer identity", async () => {
  installWindow();
  const { streamGlobalJob } = await import("../../app/global-ask-api.ts");
  let captured;
  globalThis.fetch = async (url, init) => {
    captured = { url: String(url), method: init?.method ?? "GET" };
    return streamResponse(
      JSON.stringify({ event: "started", job_id: "gask-1", conversation_id: "gconv-1" }),
      progressFrame(0, ["A"]),
      "",
      progressFrame(1, ["B"]),
      JSON.stringify({ event: "final", job: jobFrame }),
    );
  };
  const seen = [];
  const outcome = await streamGlobalJob("gask-1", { onProgress: (frame) => seen.push([frame.trace_offset, frame.steps.map((step) => step.summary)]) });
  assert.match(captured.url, /\/global-ask\/jobs\/gask-1\/stream$/);
  assert.equal(captured.method, "GET");
  assert.deepEqual(seen, [[0, ["A"]], [1, ["B"]]]);
  assert.equal(outcome.kind, "final");
  // 全局回答的 answer_id 为空时以作业 id 兜底（分享按钮靠它出现）。
  assert.equal(outcome.job.answer.answer_id, "gask-1");
});

test("a gone frame resolves as gone", async () => {
  installWindow();
  const { streamGlobalJob } = await import("../../app/global-ask-api.ts");
  globalThis.fetch = async () => streamResponse(
    JSON.stringify({ event: "started", job_id: "gask-1", conversation_id: "gconv-1" }),
    JSON.stringify({ event: "gone" }),
  );
  assert.deepEqual(await streamGlobalJob("gask-1", { onProgress() {} }), { kind: "gone" });
});

test("a body that ends without a terminal frame, an error frame, and an HTTP error all reject", async () => {
  installWindow();
  const { streamGlobalJob } = await import("../../app/global-ask-api.ts");
  globalThis.fetch = async () => streamResponse(
    JSON.stringify({ event: "started", job_id: "gask-1", conversation_id: "gconv-1" }),
    progressFrame(0, ["A"]),
  );
  await assert.rejects(streamGlobalJob("gask-1", { onProgress() {} }));
  globalThis.fetch = async () => streamResponse(
    JSON.stringify({ event: "started", job_id: "gask-1", conversation_id: "gconv-1" }),
    JSON.stringify({ event: "error", error: "接回这次回答时出错" }),
  );
  await assert.rejects(streamGlobalJob("gask-1", { onProgress() {} }));
  globalThis.fetch = async () => new Response(JSON.stringify({ detail: "问答任务不存在，请刷新对话。" }), { status: 404 });
  await assert.rejects(streamGlobalJob("gask-1", { onProgress() {} }));
});
