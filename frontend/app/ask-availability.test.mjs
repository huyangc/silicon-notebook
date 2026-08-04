import test from "node:test";
import assert from "node:assert/strict";

import {
  borrowedKgBaseNames,
  hasLocalEvidence,
  isAskBlocked,
  kgAvailableForScope,
  kgBlockedByBaseScope,
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


// --- 严格推理本轮取不取得到图谱 --------------------------------------------
// 参考库可以按库取消勾选。本库无图、又恰好取消勾选了唯一带图的那个参考库时,读**聚合**
// 的 base_kg_available 会放行一个这轮根本搜不到图的模式,并点名一个不参与的库
// (codex #438 R2)。判据必须落在「勾选集 ∩ 带图库」上。

const ref = (id, name) => ({ id, name, tier: "base" });

/** 两个挂载的参考库:A 有图、B 无图。 */
const twoBases = (over = {}) => nb({
  kg_ready: false,
  base_notebooks: [ref("A", "模拟集成电路"), ref("B", "内部笔记")],
  base_kg_available: true,
  base_kg_notebook_ids: ["A"],
  ...over,
});

test("本库有图 ⇒ 可用(一个参考库都没勾也一样)", () => {
  assert.equal(kgAvailableForScope(twoBases({ kg_ready: true }), []), true);
});

test("本库无图 + 勾选集含带图的参考库 ⇒ 可用", () => {
  assert.equal(kgAvailableForScope(twoBases(), ["A", "B"]), true);
  assert.equal(kgAvailableForScope(twoBases(), ["A"]), true);
});

test("本库无图 + 勾选集只含无图的参考库 ⇒ 不可用", () => {
  // 这一条就是缺陷本身:base_kg_available 仍为真(A 有图),但 A 这次没勾。
  assert.equal(twoBases().base_kg_available, true);   // 前提:聚合布尔为真
  assert.equal(kgAvailableForScope(twoBases(), ["B"]), false);
});

test("本库无图 + 一个参考库都没勾 ⇒ 不可用", () => {
  assert.equal(kgAvailableForScope(twoBases(), []), false);
});

test("借用提示只点名本轮参与、且已建图谱的参考库", () => {
  // 全勾:B 没建图,不该被说成「借用」。
  assert.deepEqual(borrowedKgBaseNames(twoBases(), ["A", "B"]), ["模拟集成电路"]);
  // 取消勾选 A(唯一带图的那个)：一个都不借,提示整条不该出现。
  assert.deepEqual(borrowedKgBaseNames(twoBases(), ["B"]), []);
  // 两个库都建了图、只勾一个:另一个不参与,不能被点名。
  assert.deepEqual(
    borrowedKgBaseNames(
      twoBases({ base_kg_notebook_ids: ["A", "B"] }),
      ["A"],
    ),
    ["模拟集成电路"],
  );
});

test("分解字段缺失(旧后端/版本 skew)→ 退回聚合布尔,且只会更保守", () => {
  const skewed = (over = {}) => twoBases({
    base_kg_notebook_ids: undefined,
    ...over,
  });
  // 聚合为真:按勾选集作答 —— 仍不会点名一个本轮不参与的库。
  assert.equal(kgAvailableForScope(skewed(), ["B"]), true);
  assert.deepEqual(borrowedKgBaseNames(skewed(), ["B"]), ["内部笔记"]);
  // 聚合为假:一个都不算(不能凭「挂了库」就放行)。
  assert.equal(
    kgAvailableForScope(skewed({ base_kg_available: false }), ["A", "B"]),
    false,
  );
  // 挂载列表也缺失时不炸,判为不可用。
  assert.equal(kgAvailableForScope(nb(), ["A"]), false);
  assert.deepEqual(borrowedKgBaseNames(null, ["A"]), []);
});

test("分得清「没勾」与「没建图」——两者出路差着一次整库整理", () => {
  // 挂了带图的库、这次没勾:出路是把勾点回来,不该劝用户跑一次整库整理。
  assert.equal(kgBlockedByBaseScope(twoBases(), ["B"]), true);
  assert.equal(kgBlockedByBaseScope(twoBases(), []), true);
  // 挂的库一个都没建图:这时才真需要为本笔记本整理知识图谱。
  assert.equal(
    kgBlockedByBaseScope(twoBases({ base_kg_available: false, base_kg_notebook_ids: [] }), []),
    false,
  );
  // 没被拦住的情形一律为假(它只用来解释「为什么拦」)。
  assert.equal(kgBlockedByBaseScope(twoBases(), ["A"]), false);
  assert.equal(kgBlockedByBaseScope(twoBases({ kg_ready: true }), []), false);
  // 一个参考库都没挂 / 没有笔记本:同样是「该建图」那一支。
  assert.equal(kgBlockedByBaseScope(nb({ kg_ready: false }), []), false);
  assert.equal(kgBlockedByBaseScope(null, []), false);
});
