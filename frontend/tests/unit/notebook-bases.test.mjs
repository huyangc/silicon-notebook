import { test } from "node:test";
import assert from "node:assert/strict";
import {
  groupMountable,
  mergeMountCandidates,
  mountCostHint,
  MOUNT_HINT_THRESHOLD,
  resolvePromotionTarget,
  shouldShowBorrowedBaseHint,
  toMountedBases,
} from "../../app/notebook-bases.ts";

// 群组知识共享 P1:「读权 ⇒ 可挂载」放开之后,别人 owner 的库也会进候选。只按 tier
// 分组会把它们标成「我的笔记本」——一句事实错误的标签。后端因此随每个候选带回
// origin(base/mine/shared),前端按它分三组;缺失时才退回 tier 两组分法。

test("groupMountable 按 origin 分成公共/我的/共享给我的三组", () => {
  const got = groupMountable([
    { id: "1", name: "模拟", tier: "base", origin: "base" },
    { id: "2", name: "我的笔记", tier: "personal", origin: "mine" },
    { id: "3", name: "同事的库", tier: "personal", origin: "shared" },
    { id: "4", name: "组里的库", tier: "personal", origin: "shared" },
  ]);
  assert.deepEqual(got.public.map((n) => n.id), ["1"]);
  assert.deepEqual(got.mine.map((n) => n.id), ["2"]);
  assert.deepEqual(got.shared.map((n) => n.id), ["3", "4"]);
});

test("origin 缺失(旧后端 / 只存在于挂载边的死边)退回 tier 两组分法,不猜「我的」", () => {
  const got = groupMountable([
    { id: "1", name: "模拟", tier: "base" },
    { id: "2", name: "我的笔记", tier: "personal" },
  ]);
  assert.deepEqual(got.public.map((n) => n.id), ["1"]);
  assert.deepEqual(got.mine.map((n) => n.id), ["2"]);
  assert.deepEqual(got.shared, []);
});

test("mergeMountCandidates 把候选的 origin 捎到已挂载的边上(最常见的那种库靠它分组)", () => {
  // 已挂上的群组共享库:边(/bases)不带 origin,候选(/mountable)带。直接用边就等于
  // 让每一条**已经挂上**的借来库退回按 tier 猜——而那正是最需要看清来路的一批。
  const merged = mergeMountCandidates(
    [{ id: "2", name: "组里的库", tier: "personal", origin: "shared" }],
    [{ id: "2", name: "组里的库", tier: "personal", active: true, inactive_reason: "" }],
  );
  assert.equal(merged.length, 1);
  assert.equal(merged[0].origin, "shared");
  assert.equal(merged[0].active, true, "边自身的 active/inactive_reason 仍是权威");
  assert.deepEqual(groupMountable(merged).shared.map((n) => n.id), ["2"]);
});

test("只存在于挂载边的失效边没有 origin:base 的仍归公共知识库", () => {
  const merged = mergeMountCandidates(
    [],
    [{ id: "9", name: "已不可用的知识库", tier: "base", active: false, inactive_reason: "原因" }],
  );
  assert.equal(merged[0].origin, undefined);
  assert.deepEqual(groupMountable(merged).public.map((n) => n.id), ["9"]);
});

// origin 缺席只剩一种形态:候选里没有、只存在于挂载边的**失效边**。这类边里
// tier='personal' 的恰恰是别人家的库(共享被撤销 / 被挂库易主 / 本笔记本被共享而
// 关掉借入边),落进「我的笔记本」正是这次要修的那句事实错误的标签。
test("失效的 personal 边不落「我的笔记本」——那是别人家的库,归「共享给我的」", () => {
  const merged = mergeMountCandidates(
    [],
    [{ id: "7", name: "同事的库", tier: "personal", active: false, inactive_reason: "共享已撤销" }],
  );
  const groups = groupMountable(merged);
  assert.deepEqual(groups.mine, []);
  assert.deepEqual(groups.shared.map((n) => n.id), ["7"]);
  assert.equal(groups.shared[0].inactive_reason, "共享已撤销", "失效原因必须保留");
});

test("旧后端(全部候选都没有 origin)逐字回到 tier 两组分法:active 恒真,不落第三组", () => {
  const groups = groupMountable([
    { id: "1", name: "模拟", tier: "base", active: true, inactive_reason: "" },
    { id: "2", name: "我的笔记", tier: "personal", active: true, inactive_reason: "" },
  ]);
  assert.deepEqual(groups.public.map((n) => n.id), ["1"]);
  assert.deepEqual(groups.mine.map((n) => n.id), ["2"]);
  assert.deepEqual(groups.shared, []);
});

test("groupMountable 按 tier 分成公共/我的两组", () => {
  const got = groupMountable([
    { id: "1", name: "模拟", tier: "base" },
    { id: "2", name: "我的笔记", tier: "personal" },
    { id: "3", name: "物理设计", tier: "base" },
  ]);
  assert.deepEqual(got.public.map((n) => n.id), ["1", "3"]);
  assert.deepEqual(got.mine.map((n) => n.id), ["2"]);
});

test("挂载数不超过阈值时无成本提示", () => {
  assert.equal(mountCostHint(MOUNT_HINT_THRESHOLD), "");
});

test("超过阈值给出成本提示且提到检索", () => {
  const hint = mountCostHint(MOUNT_HINT_THRESHOLD + 1);
  assert.ok(hint.length > 0);
  assert.match(hint, /检索/);
});

test("resolvePromotionTarget: 0 个挂载的公共知识库 -> none(禁用提交)", () => {
  const got = resolvePromotionTarget([
    { id: "1", name: "我的笔记", tier: "personal", active: true, inactive_reason: "" },
  ]);
  assert.deepEqual(got, { kind: "none" });
});

test("resolvePromotionTarget: 没挂任何库(空数组) -> none", () => {
  assert.deepEqual(resolvePromotionTarget([]), { kind: "none" });
});

test("resolvePromotionTarget: 1 个公共知识库 -> auto 直接使用,不打扰用户", () => {
  const got = resolvePromotionTarget([
    { id: "1", name: "我的笔记", tier: "personal", active: true, inactive_reason: "" },
    { id: "2", name: "模拟电路", tier: "base", active: true, inactive_reason: "" },
  ]);
  assert.deepEqual(got, { kind: "auto", baseId: "2" });
});

test("resolvePromotionTarget: >1 个公共知识库 -> choose,携带全部候选供选择器渲染", () => {
  const got = resolvePromotionTarget([
    { id: "2", name: "模拟电路", tier: "base", active: true, inactive_reason: "" },
    { id: "3", name: "数字电路", tier: "base", active: true, inactive_reason: "" },
  ]);
  assert.equal(got.kind, "choose");
  assert.deepEqual(got.options.map((o) => o.id), ["2", "3"]);
});

test("resolvePromotionTarget 忽略失效挂载边(降级/易主后的死边不是晋升候选)", () => {
  const got = resolvePromotionTarget([
    { id: "2", name: "已不可用的知识库", tier: "base", active: false, inactive_reason: "该库已不是公共知识库，且不属于你" },
  ]);
  assert.deepEqual(got, { kind: "none" });
});

test("resolvePromotionTarget 忽略同 owner 的个人库(晋升只能进公共知识库)", () => {
  const got = resolvePromotionTarget([
    { id: "2", name: "我自己的私有库", tier: "personal", active: true, inactive_reason: "" },
  ]);
  assert.deepEqual(got, { kind: "none" });
});

// Task 14 审查修复 #2:「本笔记本无图，将使用参考库…推理」这条 .chat-hint 曾经
// 丢了 base_kg_available 门控(以及"仅深入分析 tab"的限定)。base_notebooks
// 非空只代表挂了参考库，不代表参考库已建图——挂了一个还没建图的参考库时，
// 旧代码会让这条提示和上面的「该 notebook 尚无知识图谱，需先构建」提示同框
// 打架。钉住:两条提示门控必须一致地要求 groupOf(askMode)==="strict" 且
// base 确有 KG(kgAvailable && base_kg_available)。
test("借用参考库提示要求 strict、可用 base KG、当前未建图", () => {
  const baseline = {
    strict: true,
    kgAvailable: true,
    baseKgAvailable: true,
    kgReady: false,
    baseCount: 1,
  };
  assert.equal(shouldShowBorrowedBaseHint(baseline), true);
  for (const override of [
    { strict: false },
    { kgAvailable: false },
    { baseKgAvailable: false },
    { kgReady: true },
    { baseCount: 0 },
  ]) {
    assert.equal(
      shouldShowBorrowedBaseHint({ ...baseline, ...override }),
      false,
    );
  }
});

// 最终整支审查 BLOCKER 1:参考库选择器过去只渲染 groupMountable(mountable),而
// mountable 与失效边的判定谓词是同一个表达式——一条失效边永远不会出现在
// mountable 里,那一行永远渲染不出来,用户也就没法取消勾选。mergeMountCandidates
// 是抽出来的纯合并逻辑(mountable ∪ mountEdges),下面锁住三种输入组合。

test("mergeMountCandidates: 尚未挂载的候选补 active=true/inactive_reason=''", () => {
  const got = mergeMountCandidates(
    [{ id: "1", name: "模拟电路", tier: "base" }],
    []
  );
  assert.deepEqual(got, [
    { id: "1", name: "模拟电路", tier: "base", active: true, inactive_reason: "" },
  ]);
});

test("mergeMountCandidates: 已挂载且仍生效的边只出现一次,数据取自 mountEdges", () => {
  const got = mergeMountCandidates(
    [{ id: "1", name: "模拟电路", tier: "base" }],
    [{ id: "1", name: "模拟电路", tier: "base", active: true, inactive_reason: "" }]
  );
  assert.equal(got.length, 1);
  assert.equal(got[0].active, true);
});

test("mergeMountCandidates: 失效边不在 mountable 候选里,仍必须渲染出来(核心回归)", () => {
  const got = mergeMountCandidates(
    [], // mountable_notebooks 与 MOUNT_VALID_EXPR 同谓词——失效边不会出现在这里
    [{ id: "2", name: "已不可用的知识库", tier: "base", active: false, inactive_reason: "该库已不是公共知识库，且不属于你" }]
  );
  assert.deepEqual(got, [
    { id: "2", name: "已不可用的知识库", tier: "base", active: false, inactive_reason: "该库已不是公共知识库，且不属于你" },
  ]);
});

test("mergeMountCandidates: 候选+失效边混合,顺序为 mountable 优先、失效边追加在后", () => {
  const got = mergeMountCandidates(
    [{ id: "1", name: "模拟电路", tier: "base" }],
    [{ id: "2", name: "已不可用的知识库", tier: "base", active: false, inactive_reason: "原因" }]
  );
  assert.deepEqual(got.map((n) => n.id), ["1", "2"]);
  assert.equal(got[1].active, false);
});

// codex 对 PR#304 的审查(2026-07-19)P2 #2:全局 Memory 页晋升死胡同——
// scope==="global" 时 promotionTarget 恒为 null,挂了 >1 个公共库的笔记本
// 从全局页永远晋升不了。修法:按 memory.notebook_id 查
// NotebookSummary.base_notebooks(NotebookRef[],无 active/inactive_reason),
// toMountedBases 补上这两个字段(该查询本就只回填当前生效的挂载边,天然等价于
// active=true)后即可直接喂给 resolvePromotionTarget——page.tsx 的
// notebookPromotionBases/notebookBasesById 都过这一份适配,不重复写字面量。

test("toMountedBases: 补 active=true/inactive_reason='',其余字段透传", () => {
  const got = toMountedBases([
    { id: "1", name: "模拟电路", tier: "base" },
    { id: "2", name: "我的私有库", tier: "personal" },
  ]);
  assert.deepEqual(got, [
    { id: "1", name: "模拟电路", tier: "base", active: true, inactive_reason: "" },
    { id: "2", name: "我的私有库", tier: "personal", active: true, inactive_reason: "" },
  ]);
});

test("toMountedBases: 空数组 -> 空数组", () => {
  assert.deepEqual(toMountedBases([]), []);
});

test("toMountedBases 喂给 resolvePromotionTarget 复现三态判定(0/1/>1 个公共知识库)", () => {
  assert.deepEqual(resolvePromotionTarget(toMountedBases([])), { kind: "none" });
  assert.deepEqual(
    resolvePromotionTarget(toMountedBases([{ id: "b1", name: "模拟", tier: "base" }])),
    { kind: "auto", baseId: "b1" },
  );
  const chooseResult = resolvePromotionTarget(toMountedBases([
    { id: "b1", name: "模拟", tier: "base" },
    { id: "b2", name: "物理设计", tier: "base" },
  ]));
  assert.equal(chooseResult.kind, "choose");
  assert.deepEqual(chooseResult.options.map((o) => o.id), ["b1", "b2"]);
});

test("mergeMountCandidates 的结果喂给 groupMountable 后失效边落在正确分组且保留 active/inactive_reason", () => {
  const merged = mergeMountCandidates(
    [{ id: "1", name: "我的笔记", tier: "personal" }],
    [{ id: "2", name: "已不可用的知识库", tier: "base", active: false, inactive_reason: "原因" }]
  );
  const groups = groupMountable(merged);
  assert.deepEqual(groups.public.map((n) => n.id), ["2"]);
  assert.deepEqual(groups.mine.map((n) => n.id), ["1"]);
  assert.equal(groups.public[0].active, false, "groupMountable 必须是泛型,不能把 active 字段擦掉");
  assert.equal(groups.public[0].inactive_reason, "原因");
});
