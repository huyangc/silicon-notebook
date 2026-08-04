import test from "node:test";
import assert from "node:assert/strict";

import { hasLocalEvidence, isAskBlocked } from "./ask-availability.ts";

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


// --- 本地那一半 -----------------------------------------------------------
// ask_available 是合并后的单个布尔,分不出「有得可搜」是本地撑起来的还是参考库
// 撑起来的。取消勾选全部参考库之后还剩不剩东西,只能问 local_evidence_available。

test("后端说本地有证据 → 即使零可见来源也算有得可搜", () => {
  // Knowhow-only / confirmed-memory-only:两者都没有可见来源。
  assert.equal(
    hasLocalEvidence(nb({ ask_available: true, local_evidence_available: true })),
    true,
  );
});

test("挂载参考库撑起来的 ask_available 不算本地证据", () => {
  // 反向护栏:算进来的话,把参考库全取消勾选后仍会放行一次零证据检索。
  assert.equal(
    hasLocalEvidence(nb({ ask_available: true, local_evidence_available: false })),
    false,
  );
});

test("字段缺失(旧后端/版本 skew)→ false,消费侧与来源数取或,逐字回落旧判据", () => {
  assert.equal(hasLocalEvidence(nb()), false);
  assert.equal(hasLocalEvidence(nb({ local_evidence_available: undefined })), false);
  assert.equal(hasLocalEvidence(null), false);
});
