import { test } from "node:test";
import assert from "node:assert/strict";

process.env.NEXT_PUBLIC_API_BASE_URL = "http://api.example/api";
const { withDeletedSources, referenceSourceDeleted, DELETED_SOURCE_LABEL } = await import(
  "../../app/global-deleted-sources.ts"
);
const { normalizeGlobalJob } = await import("../../app/global-ask-api.ts");

const image = { element_id: "img-1", asset_id: "asset-1", caption: "图" };
const citation = (notebook) => ({
  label: notebook, source_id: `s-${notebook}`, element_id: `e-${notebook}`,
  location_label: "p. 1", quoted_span: "摘录", notebook_id: notebook, images: [image],
});
const anchor = (key, notebook) => ({
  key, object_id: "", object_type: "element", label: key, notebook_id: notebook, images: [image],
});

test("only the deleted library's references are marked, and lose their images", () => {
  const job = {
    job_id: "gask-1",
    deleted_notebook_ids: ["nb-gone"],
    answer: {
      answer_id: "gask-1",
      answer: "正文 [k1] [k2]",
      anchors: [anchor("k1", "nb-live"), anchor("k2", "nb-gone")],
      citations: [citation("nb-live"), citation("nb-gone")],
    },
    response: null,
  };
  const marked = withDeletedSources(job);
  const [liveAnchor, goneAnchor] = marked.answer.anchors;
  assert.equal(liveAnchor, job.answer.anchors[0], "a live reference is the same object");
  assert.equal(goneAnchor.source_deleted, true);
  assert.equal("images" in goneAnchor, false, "no image of a deleted library is ever requested");
  assert.equal(marked.answer.citations[1].source_deleted, true);
  assert.equal("images" in marked.answer.citations[1], false);
  assert.equal(marked.answer.answer, job.answer.answer, "the answer body is untouched");
  assert.equal(job.answer.anchors[1].source_deleted, undefined, "the input job is not mutated");
  assert.equal(referenceSourceDeleted({ anchor: goneAnchor }), true);
  assert.equal(referenceSourceDeleted({ anchor: liveAnchor, citation: citation("nb-live") }), false);
  assert.equal(DELETED_SOURCE_LABEL, "来源已删除");
});

test("legacy answers and collection images of a deleted library are degraded too", () => {
  const job = {
    job_id: "gask-2",
    deleted_notebook_ids: ["nb-gone"],
    answer: {
      answer_id: "gask-2", answer: "清单", anchors: [], citations: [],
      result_sets: [{
        kind: "collection", collection: "elements", element_kind: "image",
        items: [
          { item_id: "i-1", notebook_id: "nb-gone", element_type: "image", asset_id: "asset-x" },
          { item_id: "i-2", notebook_id: "nb-live", element_type: "image", asset_id: "asset-y" },
        ],
        coverage: { returned_total: 2, total: 2, complete: true }, synthesis_rows: 0,
      }],
    },
    response: { anchors: [anchor("k1", "nb-gone")], citations: [citation("nb-gone")] },
  };
  const marked = withDeletedSources(job);
  const [gone, live] = marked.answer.result_sets[0].items;
  assert.equal(gone.asset_id, "");
  assert.equal(live.asset_id, "asset-y");
  assert.equal(marked.response.anchors[0].source_deleted, true);
  assert.equal(marked.response.citations[0].source_deleted, true);
});

test("a turn with no deleted library passes through as the same object", () => {
  const plain = { job_id: "gask-3", answer: { answer_id: "gask-3", anchors: [], citations: [] } };
  assert.equal(withDeletedSources(plain), plain);
  const empty = { ...plain, deleted_notebook_ids: [] };
  assert.equal(withDeletedSources(empty), empty);
});

test("the read entry applies both the answer identity and the degradation", () => {
  const job = {
    job_id: "gask-4",
    deleted_notebook_ids: ["nb-gone"],
    answer: { answer_id: "", answer: "正文", anchors: [], citations: [citation("nb-gone")] },
    response: null,
  };
  const normalized = normalizeGlobalJob(job);
  assert.equal(normalized.answer.answer_id, "gask-4");
  assert.equal(normalized.answer.citations[0].source_deleted, true);
});

// 每一个作业读入口都经 `normalizeGlobalJob`：会话详情、单作业轮询、推送流终态帧、
// 停止与反馈的返回。漏掉任何一处，那条路径交给界面的引用就仍可点、仍会去取已删库的图。
const api = await import("../../app/global-ask-api.ts");

const retainedJob = () => ({
  job_id: "gask-9", conversation_id: "gconv-9", status: "done", question: "q",
  deleted_notebook_ids: ["nb-gone"], retained_until: "2027-04-17T00:00:00+00:00",
  answer: { answer_id: "gask-9", answer: "正文 [k1]", anchors: [anchor("k1", "nb-gone")], citations: [] },
  response: null,
});

async function withFetch(body, run) {
  const originalFetch = globalThis.fetch;
  const originalWindow = globalThis.window;
  globalThis.window = { localStorage: { getItem: () => "session-token" } };
  const ndjson = typeof body === "string";
  globalThis.fetch = async () => new Response(ndjson ? body : JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": ndjson ? "application/x-ndjson" : "application/json" },
  });
  try {
    await run();
  } finally {
    globalThis.fetch = originalFetch;
    globalThis.window = originalWindow;
  }
}

const degraded = (job) => job.answer.anchors[0].source_deleted === true;

test("every job read entry hands back degraded references", async () => {
  await withFetch({ id: "gconv-9", title: "t", turns: [retainedJob()], has_more: false, next_offset: null }, async () => {
    const detail = await api.getGlobalConversation("gconv-9");
    assert.ok(degraded(detail.turns[0]), "conversation detail");
  });
  await withFetch(retainedJob(), async () => {
    assert.ok(degraded(await api.getGlobalJob("gask-9")), "job poll");
    assert.ok(degraded(await api.cancelGlobalJob("gask-9")), "cancel");
    assert.ok(degraded(await api.submitGlobalFeedback("gask-9", "useful")), "feedback");
    assert.ok(degraded(await api.askGlobal({
      question: "q", notebook_scope: { mode: "all" }, client_request_id: "r",
    })), "submission");
  });
  await withFetch(`${JSON.stringify({ event: "final", job: retainedJob() })}\n`, async () => {
    const outcome = await api.streamGlobalJob("gask-9", { onProgress: () => undefined });
    assert.equal(outcome.kind, "final");
    assert.ok(degraded(outcome.job), "stream final frame");
  });
});
