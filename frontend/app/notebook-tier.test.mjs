import test from "node:test";
import assert from "node:assert/strict";

import { setNotebookTier, nextTier, tierLabel } from "./notebook-tier.ts";

function withFetchStub(run) {
  const calls = [];
  const original = globalThis.fetch;
  globalThis.fetch = async (url, init) => {
    calls.push({ url, init });
    return { ok: true, status: 200, json: async () => ({}), text: async () => "" };
  };
  return Promise.resolve(run(calls)).finally(() => {
    globalThis.fetch = original;
  });
}

test("nextTier flips between base and personal", () => {
  assert.strictEqual(nextTier("personal"), "base");
  assert.strictEqual(nextTier("base"), "personal");
  assert.strictEqual(nextTier(undefined), "base");
});

test("tierLabel describes the toggle action for the current tier", () => {
  assert.strictEqual(tierLabel("personal"), "设为基准库");
  assert.strictEqual(tierLabel("base"), "取消基准库");
});

test("setNotebookTier POSTs /notebooks/{id}/tier with the tier body", () =>
  withFetchStub(async (calls) => {
    await setNotebookTier("nb-1", "base");
    assert.match(calls[0].url, /\/notebooks\/nb-1\/tier$/);
    assert.strictEqual(calls[0].init.method, "POST");
    assert.deepStrictEqual(JSON.parse(calls[0].init.body), { tier: "base" });
  }));
