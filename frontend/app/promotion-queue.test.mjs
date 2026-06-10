import test from "node:test";
import assert from "node:assert/strict";

import {
  fetchPromotionQueue,
  proposePromotion,
  approvePromotion,
  rejectPromotion,
} from "./promotion-queue.ts";

// Capture the URL + init each client function builds, without real HTTP.
function withFetchStub(run) {
  const calls = [];
  const original = globalThis.fetch;
  globalThis.fetch = async (url, init) => {
    calls.push({ url, init });
    return {
      ok: true,
      status: 200,
      json: async () => ({}),
      text: async () => "",
    };
  };
  return Promise.resolve(run(calls)).finally(() => {
    globalThis.fetch = original;
  });
}

test("exports the four client functions", () => {
  assert.strictEqual(typeof fetchPromotionQueue, "function");
  assert.strictEqual(typeof proposePromotion, "function");
  assert.strictEqual(typeof approvePromotion, "function");
  assert.strictEqual(typeof rejectPromotion, "function");
});

test("fetchPromotionQueue GETs /promotion-queue with no status", () =>
  withFetchStub(async (calls) => {
    await fetchPromotionQueue();
    assert.match(calls[0].url, /\/promotion-queue$/);
  }));

test("fetchPromotionQueue appends the status query param", () =>
  withFetchStub(async (calls) => {
    await fetchPromotionQueue("proposed");
    assert.match(calls[0].url, /\/promotion-queue\?status=proposed$/);
  }));

test("proposePromotion POSTs the promote endpoint", () =>
  withFetchStub(async (calls) => {
    await proposePromotion("nb-1", "ko-9");
    assert.match(calls[0].url, /\/notebooks\/nb-1\/knowledge\/ko-9\/promote$/);
    assert.strictEqual(calls[0].init.method, "POST");
  }));

test("approvePromotion POSTs the approve endpoint and encodes the id", () =>
  withFetchStub(async (calls) => {
    await approvePromotion("promo-abc");
    assert.match(calls[0].url, /\/promotion-queue\/promo-abc\/approve$/);
    assert.strictEqual(calls[0].init.method, "POST");
  }));

test("rejectPromotion POSTs the reject endpoint with a reason body", () =>
  withFetchStub(async (calls) => {
    await rejectPromotion("promo-abc", "not canonical");
    assert.match(calls[0].url, /\/promotion-queue\/promo-abc\/reject$/);
    assert.strictEqual(calls[0].init.method, "POST");
    assert.deepStrictEqual(JSON.parse(calls[0].init.body), { reason: "not canonical" });
  }));

test("rejectPromotion defaults reason to empty string", () =>
  withFetchStub(async (calls) => {
    await rejectPromotion("promo-abc");
    assert.deepStrictEqual(JSON.parse(calls[0].init.body), { reason: "" });
  }));
