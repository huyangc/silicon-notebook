import test from "node:test";
import assert from "node:assert/strict";

import { setNotebookTier, tierActionState } from "./notebook-tier.ts";

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

test("tierActionState: 当前 notebook 已是 base → unset(可取消)", () => {
  const cur = { id: "a", name: "A", tier: "base" };
  const s = tierActionState(cur);
  assert.strictEqual(s.action, "unset");
});

test("公共知识库不再唯一：别处已有 base 时仍是 set 而非 replace", () => {
  const got = tierActionState({ id: "a", name: "A", tier: "personal" });
  assert.equal(got.action, "set");
  assert.equal(got.label, "设为公共知识库");
});

test("当前已是公共知识库 → unset", () => {
  const got = tierActionState({ id: "a", name: "A", tier: "base" });
  assert.equal(got.action, "unset");
  assert.equal(got.label, "取消公共知识库");
});

test("tierActionState: 全局无 base → set", () => {
  const cur = { id: "a", name: "A", tier: "personal" };
  const s = tierActionState(cur);
  assert.strictEqual(s.action, "set");
});

test("tierActionState: current 为空也不报错(默认 set)", () => {
  const s = tierActionState(undefined);
  assert.strictEqual(s.action, "set");
});

test("setNotebookTier POSTs /notebooks/{id}/tier with the tier body", () =>
  withFetchStub(async (calls) => {
    await setNotebookTier("nb-1", "base");
    assert.match(calls[0].url, /\/notebooks\/nb-1\/tier$/);
    assert.strictEqual(calls[0].init.method, "POST");
    assert.deepStrictEqual(JSON.parse(calls[0].init.body), { tier: "base" });
  }));
