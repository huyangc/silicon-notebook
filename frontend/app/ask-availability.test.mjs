import test from "node:test";
import assert from "node:assert/strict";

import {
  hasLocalEvidence,
  isAskBlocked,
  strictModeKgAvailable,
} from "./ask-availability.ts";

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


// --- 严格推理这一组模式的图可用性 -------------------------------------------
// codex #431 R8 P2-B 的前端半边:`base_kg_available` 是整库聚合字段,回答的是
// 「挂着的库里有没有图」,不是「这次勾了的库里有没有图」。

test("本库自己有图 → 与参考库勾选无关,恒可用", () => {
  // R1:库维度不得关掉本库自己的能力。
  assert.equal(
    strictModeKgAvailable(nb({ kg_ready: true, base_kg_available: true }), 0),
    true,
  );
  assert.equal(strictModeKgAvailable(nb({ kg_ready: true }), 0), true);
});

test("本库无图 + 参考库一个都没勾 → 判不可用", () => {
  assert.equal(
    strictModeKgAvailable(nb({ kg_ready: false, base_kg_available: true }), 0),
    false,
  );
});

test("本库无图 + 参考库还勾着 → 仍可用(不是一刀切关掉)", () => {
  assert.equal(
    strictModeKgAvailable(nb({ kg_ready: false, base_kg_available: true }), 1),
    true,
  );
});

test("挂载库里本来就没有图 → 勾多少个都不可用", () => {
  assert.equal(
    strictModeKgAvailable(nb({ kg_ready: false, base_kg_available: false }), 3),
    false,
  );
});

test("字段缺失 / notebook 为 null → 不可用,与改前逐字一致", () => {
  assert.equal(strictModeKgAvailable(nb(), 2), false);
  assert.equal(strictModeKgAvailable(null, 2), false);
});
