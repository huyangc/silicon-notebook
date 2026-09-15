import { test } from "node:test";
import assert from "node:assert";
import { pageMeta, slicePage } from "../../app/pagination-logic.mjs";

test("pageMeta computes last page + range", () => {
  assert.deepEqual(pageMeta({ page: 0, pageSize: 50, total: 120 }),
    { lastPage: 2, canPrev: false, canNext: true, from: 1, to: 50 });
  assert.deepEqual(pageMeta({ page: 2, pageSize: 50, total: 120 }),
    { lastPage: 2, canPrev: true, canNext: false, from: 101, to: 120 });
  assert.deepEqual(pageMeta({ page: 0, pageSize: 50, total: 0 }),
    { lastPage: 0, canPrev: false, canNext: false, from: 0, to: 0 });
});

test("slicePage returns the requested page of an already-fetched list", () => {
  const rows = Array.from({ length: 45 }, (_, i) => i);
  assert.deepEqual(slicePage(rows, 0, 20), { page: 0, items: rows.slice(0, 20) });
  assert.deepEqual(slicePage(rows, 2, 20), { page: 2, items: [40, 41, 42, 43, 44] });
});

test("slicePage clamps a page that no longer exists after the list shrank", () => {
  // 第 3 页只剩一行,移除后清单变 40 行:视图退回第 2 页,而不是一张空页。
  const rows = Array.from({ length: 40 }, (_, i) => i);
  assert.deepEqual(slicePage(rows, 2, 20), { page: 1, items: rows.slice(20, 40) });
  assert.deepEqual(slicePage([], 3, 20), { page: 0, items: [] });
  assert.deepEqual(slicePage(rows, -1, 20), { page: 0, items: rows.slice(0, 20) });
});
