import test from "node:test";
import assert from "node:assert/strict";

import { isAskBlocked } from "./ask-availability.ts";

const nb = (over = {}) => ({
  id: "nb",
  name: "n",
  purpose: "",
  primary_domain: "",
  status: "ready",
  counts: { sources: 0, memories: 0 },
  created_label: "",
  ...over,
});

test("后端 ask_available=false → 禁止对话", () => {
  assert.equal(isAskBlocked(nb({ ask_available: false })), true);
});

test("后端 ask_available=true → 放行", () => {
  // 覆盖 knowhow-only / confirmed-memory-only / 挂载有KG参考库等前端看不到的证据。
  assert.equal(isAskBlocked(nb({ ask_available: true })), false);
});

test("不用本地来源计数做乐观快路:仅凭 ask_available 判定", () => {
  // codex 第3轮 P1:上传未解析/解析失败的来源没有 chunk,不能靠"有来源"解禁。
  // 该库有可见来源计数但后端仍判不可用时,依旧禁止。
  assert.equal(isAskBlocked(nb({ ask_available: false, counts: { sources: 5, memories: 0 } })), true);
});

test("ask_available 缺失(旧后端/版本 skew) → fail-open 放行", () => {
  assert.equal(isAskBlocked(nb()), false);
  assert.equal(isAskBlocked(nb({ ask_available: undefined })), false);
});

test("currentNotebook 为 null → fail-open 放行(安全默认)", () => {
  assert.equal(isAskBlocked(null), false);
});
