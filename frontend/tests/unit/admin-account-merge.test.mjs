import test from "node:test";
import assert from "node:assert/strict";

import { parseAccountMergeBlocked } from "../../app/admin/usage/api.ts";

const blocked = {
  detail: {
    code: "merge_source_has_data",
    message: "该账号名下已有数据，不能合并删除：笔记本 2 个",
    items: [{ label: "笔记本", unit: "个", count: 2 }, { label: "提问记录", unit: "条", count: 3 }],
  },
};

test("parseAccountMergeBlocked 认出 409 merge_source_has_data 并原样给出类别与条数", () => {
  assert.deepEqual(parseAccountMergeBlocked(409, blocked), [
    { label: "笔记本", unit: "个", count: 2 },
    { label: "提问记录", unit: "条", count: 3 },
  ]);
});

test("parseAccountMergeBlocked 状态码、code 或条目形状不对一律不认", () => {
  assert.equal(parseAccountMergeBlocked(400, blocked), null);
  assert.equal(parseAccountMergeBlocked(409, { detail: "该账号名下已有数据" }), null);
  assert.equal(parseAccountMergeBlocked(409, { detail: { ...blocked.detail, code: "other" } }), null);
  assert.equal(parseAccountMergeBlocked(409, { detail: { ...blocked.detail, items: [] } }), null);
  for (const item of [
    { label: "", unit: "个", count: 1 },
    { label: "笔记本", unit: "个", count: 0 },
    { label: "笔记本", unit: "个", count: 1.5 },
    { label: "笔记本", count: 1 },
    null,
  ]) {
    assert.equal(parseAccountMergeBlocked(409, { detail: { ...blocked.detail, items: [item] } }), null);
  }
  assert.equal(parseAccountMergeBlocked(409, null), null);
});
