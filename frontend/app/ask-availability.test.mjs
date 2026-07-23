import test from "node:test";
import assert from "node:assert/strict";

import { isAskBlocked, mountedBaseCount } from "./ask-availability.ts";

const nb = (over = {}) => ({
  id: "nb",
  name: "n",
  purpose: "",
  primary_domain: "",
  status: "ready",
  counts: {},
  created_label: "",
  ...over,
});

test("mountedBaseCount: 无字段/空数组按 0，逐项计数", () => {
  assert.equal(mountedBaseCount(null), 0);
  assert.equal(mountedBaseCount(nb()), 0);
  assert.equal(mountedBaseCount(nb({ base_notebooks: [] })), 0);
  assert.equal(mountedBaseCount(nb({ base_notebooks: [{ id: "b", name: "B" }] })), 1);
});

test("无来源且无参考库 → 禁止对话", () => {
  assert.equal(isAskBlocked(nb(), 0), true);
  assert.equal(isAskBlocked(nb({ base_notebooks: [] }), 0), true);
});

test("有自有来源 → 放行(不论是否挂参考库)", () => {
  assert.equal(isAskBlocked(nb(), 1), false);
  assert.equal(isAskBlocked(nb(), 42), false);
});

test("无来源但挂了参考库 → 放行", () => {
  assert.equal(isAskBlocked(nb({ base_notebooks: [{ id: "b", name: "B" }] }), 0), false);
});

test("来源数为负(异常)按无来源处理，仅参考库能放行", () => {
  assert.equal(isAskBlocked(nb(), -1), true);
  assert.equal(isAskBlocked(nb({ base_notebooks: [{ id: "b", name: "B" }] }), -1), false);
});

test("currentNotebook 为 null 且无来源 → 禁止(安全默认)", () => {
  assert.equal(isAskBlocked(null, 0), true);
  // null 但已知有来源(计数来自独立状态)→ 放行
  assert.equal(isAskBlocked(null, 3), false);
});
