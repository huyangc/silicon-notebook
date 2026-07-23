import test from "node:test";
import assert from "node:assert/strict";

import { resolveDocumentCapacity } from "./document-limit.ts";

test("未满额:显示指示但不门控", () => {
  const cap = resolveDocumentCapacity({ isAdmin: false, documentLimit: 20, documentCount: 5 });
  assert.deepEqual(cap, { show: true, atCapacity: false, limit: 20, count: 5 });
});

test("恰好满额(count === limit)即门控", () => {
  const cap = resolveDocumentCapacity({ isAdmin: false, documentLimit: 20, documentCount: 20 });
  assert.equal(cap.show, true);
  assert.equal(cap.atCapacity, true);
});

test("超额(count > limit)也门控", () => {
  const cap = resolveDocumentCapacity({ isAdmin: false, documentLimit: 20, documentCount: 21 });
  assert.equal(cap.atCapacity, true);
});

test("管理员豁免:既不显示指示也不门控", () => {
  const cap = resolveDocumentCapacity({ isAdmin: true, documentLimit: 20, documentCount: 999 });
  assert.equal(cap.show, false);
  assert.equal(cap.atCapacity, false);
});

test("上限为 0 哨兵 / 缺失 / null:当未知,不显示不门控", () => {
  for (const documentLimit of [0, undefined, null]) {
    const cap = resolveDocumentCapacity({ isAdmin: false, documentLimit, documentCount: 100 });
    assert.equal(cap.show, false, `documentLimit=${String(documentLimit)}`);
    assert.equal(cap.atCapacity, false, `documentLimit=${String(documentLimit)}`);
  }
});

test("上限为 1 是合法小值,单篇即满额", () => {
  const cap = resolveDocumentCapacity({ isAdmin: false, documentLimit: 1, documentCount: 1 });
  assert.equal(cap.show, true);
  assert.equal(cap.atCapacity, true);
});

// 回归:满额笔记本(20/20)。documentCount 必须是**不受来源搜索过滤**的真实总数;
// 若误把来源搜索命中的子集计数(如 3)喂进来,门控会误判未满而放行上传。page.tsx
// 用独立的 visibleDocTotal(而非会被搜索覆盖的 sourcesTotal)正是为了钉住这条。
test("满额笔记本:用真实总数仍判满额,用过滤计数会误放行", () => {
  const trueTotal = resolveDocumentCapacity({ isAdmin: false, documentLimit: 20, documentCount: 20 });
  assert.equal(trueTotal.atCapacity, true, "真实总数 20/20 必须判满额");

  const filteredCount = resolveDocumentCapacity({ isAdmin: false, documentLimit: 20, documentCount: 3 });
  assert.equal(filteredCount.atCapacity, false, "过滤子集计数 3 会误放行——所以计数入参绝不能用搜索态的 sourcesTotal");
});
