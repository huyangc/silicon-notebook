import test from "node:test";
import assert from "node:assert/strict";

import {
  fetchEdgeReviewQueue,
  reviewRelation,
  formatEdgeReviewQueueTitle,
} from "../../app/edge-review-queue.ts";

function withFetchStub(run, body = {}) {
  const calls = [];
  const original = globalThis.fetch;
  globalThis.fetch = async (url, init) => {
    calls.push({ url, init });
    return new Response(JSON.stringify(body), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  };
  return Promise.resolve(run(calls)).finally(() => {
    globalThis.fetch = original;
  });
}

test("exports the two client functions", () => {
  assert.strictEqual(typeof fetchEdgeReviewQueue, "function");
  assert.strictEqual(typeof reviewRelation, "function");
});

test("fetchEdgeReviewQueue GETs the edge-review-queue with no limit", () =>
  withFetchStub(async (calls) => {
    await fetchEdgeReviewQueue("nb-1");
    assert.match(calls[0].url, /\/notebooks\/nb-1\/edge-review-queue$/);
  }));

test("fetchEdgeReviewQueue appends the limit query param", () =>
  withFetchStub(async (calls) => {
    await fetchEdgeReviewQueue("nb-1", 25);
    assert.match(calls[0].url, /\/notebooks\/nb-1\/edge-review-queue\?limit=25$/);
  }));

test("fetchEdgeReviewQueue passes through the {items, total} response shape (R3 T-A3)", () =>
  withFetchStub(
    async () => {
      const response = await fetchEdgeReviewQueue("nb-1");
      assert.deepStrictEqual(response, { items: [{ rel_id: "rel-1" }], total: 42 });
    },
    { items: [{ rel_id: "rel-1" }], total: 42 },
  ));

test("reviewRelation POSTs the review endpoint with the status body", () =>
  withFetchStub(async (calls) => {
    await reviewRelation("nb-1", "rel-9", "verified");
    assert.match(calls[0].url, /\/notebooks\/nb-1\/relations\/rel-9\/review$/);
    assert.strictEqual(calls[0].init.method, "POST");
    assert.deepStrictEqual(JSON.parse(calls[0].init.body), { status: "verified" });
  }));

// R3 T-A3 review (P1-1): the modal title must disclose truncation instead of
// only claiming a total when the page is a strict subset of the real queue.
test("formatEdgeReviewQueueTitle shows only the total when the page is complete", () => {
  assert.strictEqual(formatEdgeReviewQueueTitle(3, 3), "（共 3 条）");
  assert.strictEqual(formatEdgeReviewQueueTitle(0, 0), "（共 0 条）");
});

test("formatEdgeReviewQueueTitle discloses truncation when total exceeds the page", () => {
  assert.strictEqual(
    formatEdgeReviewQueueTitle(120, 100),
    "（共 120 条 · 显示前 100 条）"
  );
});

test("formatEdgeReviewQueueTitle renders nothing while total is not yet known", () => {
  assert.strictEqual(formatEdgeReviewQueueTitle(null, 0), "");
});
