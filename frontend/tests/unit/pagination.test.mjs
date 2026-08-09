import { test } from "node:test";
import assert from "node:assert";
import { pageMeta } from "../../app/pagination-logic.mjs";

test("pageMeta computes last page + range", () => {
  assert.deepEqual(pageMeta({ page: 0, pageSize: 50, total: 120 }),
    { lastPage: 2, canPrev: false, canNext: true, from: 1, to: 50 });
  assert.deepEqual(pageMeta({ page: 2, pageSize: 50, total: 120 }),
    { lastPage: 2, canPrev: true, canNext: false, from: 101, to: 120 });
  assert.deepEqual(pageMeta({ page: 0, pageSize: 50, total: 0 }),
    { lastPage: 0, canPrev: false, canNext: false, from: 0, to: 0 });
});
