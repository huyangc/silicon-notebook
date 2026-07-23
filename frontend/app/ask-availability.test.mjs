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

test("有可见来源(乐观快路) → 放行,不看 ask_available", () => {
  // notebookSourceTotal>0 表示本地已知有来源(上传即刷新)——必有 chunk 证据。
  assert.equal(isAskBlocked(nb({ ask_available: false }), 1), false);
  assert.equal(isAskBlocked(nb({ ask_available: false }), 42), false);
});

test("无可见来源 + 后端 ask_available=false → 禁止对话", () => {
  assert.equal(isAskBlocked(nb({ ask_available: false }), 0), true);
});

test("无可见来源 + 后端 ask_available=true → 放行", () => {
  // 覆盖 knowhow-only / confirmed-memory-only / 挂载有KG参考库等前端看不到的证据。
  assert.equal(isAskBlocked(nb({ ask_available: true }), 0), false);
});

test("ask_available 缺失(旧后端/版本 skew) → fail-open 放行", () => {
  assert.equal(isAskBlocked(nb(), 0), false);
  assert.equal(isAskBlocked(nb({ ask_available: undefined }), 0), false);
});

test("来源数为负(异常)按无来源处理,仍听 ask_available", () => {
  assert.equal(isAskBlocked(nb({ ask_available: false }), -1), true);
  assert.equal(isAskBlocked(nb({ ask_available: true }), -1), false);
});

test("currentNotebook 为 null:无来源→fail-open 放行;已知有来源→放行", () => {
  assert.equal(isAskBlocked(null, 0), false);
  assert.equal(isAskBlocked(null, 3), false);
});
