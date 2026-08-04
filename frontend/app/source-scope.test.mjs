import assert from "node:assert/strict";
import test from "node:test";

import {
  baseIsSelected,
  baseScopePayload,
  crossLibrarySourceNotebookId,
  defaultBaseScopeSelection,
  defaultSourceScopeSelection,
  localScopeIsEmpty,
  retrievalScopeSummary,
  selectedBaseCount,
  selectedBaseIds,
  selectedSourceCount,
  sourceIsSelected,
  sourceScopePayload,
  toggleBaseSelection,
  toggleSourceSelection,
} from "./source-scope.ts";

test("同库来源不算跨库(空串 = 照常给写入按钮)", () => {
  assert.equal(crossLibrarySourceNotebookId("nb-1", "nb-1"), "");
});

test("参考库来源返回它自己的库 id,供弹窗标注并收起写入按钮", () => {
  assert.equal(crossLibrarySourceNotebookId("base-9", "nb-1"), "base-9");
});

test("当前库未知或来源归属缺失时不凭空标成参考库", () => {
  assert.equal(crossLibrarySourceNotebookId("base-9", null), "");
  assert.equal(crossLibrarySourceNotebookId("base-9", ""), "");
  assert.equal(crossLibrarySourceNotebookId("", "nb-1"), "");
});

test("source scope defaults to all and represents exclusions compactly", () => {
  let selection = defaultSourceScopeSelection();
  assert.equal(sourceIsSelected(selection, "s1"), true);
  selection = toggleSourceSelection(selection, "s1");
  assert.equal(sourceIsSelected(selection, "s1"), false);
  assert.equal(selectedSourceCount(selection, 3), 2);
  assert.deepEqual(sourceScopePayload(selection, 3), {
    mode: "exclude",
    source_ids: ["s1"],
  });
});

test("all unchecked normalizes to an explicit empty include scope", () => {
  let selection = defaultSourceScopeSelection();
  selection = toggleSourceSelection(selection, "s1");
  selection = toggleSourceSelection(selection, "s2");
  assert.deepEqual(sourceScopePayload(selection, 2), {
    mode: "include",
    source_ids: [],
  });
});

test("a complete visible universe uses compact include when only one source remains", () => {
  const selection = {
    allSelected: true,
    ids: new Set(["s1", "s2", "s3"]),
  };
  assert.deepEqual(sourceScopePayload(selection, 4, ["s1", "s2", "s3", "s4"]), {
    mode: "include",
    source_ids: ["s4"],
  });
});

test("an explicit include remains a hard ceiling even with a complete client universe", () => {
  const selection = {
    allSelected: false,
    ids: new Set(["s1", "s2", "s3"]),
  };
  assert.deepEqual(sourceScopePayload(selection, 4, ["s1", "s2", "s3", "s4"]), {
    mode: "include",
    source_ids: ["s1", "s2", "s3"],
  });
});

test("an explicit include cannot widen when the server universe gains a source", () => {
  const selection = {
    allSelected: false,
    ids: new Set(["s1", "s2", "s3"]),
  };
  const payload = sourceScopePayload(selection, 4, ["s1", "s2", "s3", "s4"]);
  assert.deepEqual(payload, { mode: "include", source_ids: ["s1", "s2", "s3"] });
  assert.equal(payload.source_ids.includes("s5"), false);
});

test("a paged universe preserves the known compact representation", () => {
  const selection = {
    allSelected: false,
    ids: new Set(["s7"]),
  };
  assert.deepEqual(sourceScopePayload(selection, 10_000, ["s7"]), {
    mode: "include",
    source_ids: ["s7"],
  });
});


// --- 参考库检索范围 -------------------------------------------------------
// 真机事故:挂了 84 篇论文参考库的笔记本里勾定单篇文章提问,16 条引用全部来自参考库
// —— 参考库当时无条件全量参与。以下用例钉住这一维现在真的可被收窄。

test("参考库默认全选,取消勾选后压成 exclude 名单", () => {
  let selection = defaultBaseScopeSelection();
  assert.equal(baseIsSelected(selection, "base-a"), true);
  selection = toggleBaseSelection(selection, "base-a");
  assert.equal(baseIsSelected(selection, "base-a"), false);
  assert.deepEqual(selectedBaseIds(selection, ["base-a", "base-b"]), ["base-b"]);
  assert.equal(selectedBaseCount(selection, ["base-a", "base-b"]), 1);
  assert.deepEqual(baseScopePayload(selection, ["base-a", "base-b"]), {
    mode: "exclude",
    notebook_ids: ["base-a"],
  });
});

test("全部取消勾选归一成显式空 include(后端据此判定范围为空)", () => {
  let selection = defaultBaseScopeSelection();
  selection = toggleBaseSelection(selection, "base-a");
  selection = toggleBaseSelection(selection, "base-b");
  assert.equal(selectedBaseCount(selection, ["base-a", "base-b"]), 0);
  assert.deepEqual(baseScopePayload(selection, ["base-a", "base-b"]), {
    mode: "include",
    notebook_ids: [],
  });
});

test("「清空」态(allSelected=false + 空集)同样是显式空 include", () => {
  const cleared = { allSelected: false, ids: new Set() };
  assert.equal(baseIsSelected(cleared, "base-a"), false);
  assert.deepEqual(baseScopePayload(cleared, ["base-a"]), {
    mode: "include",
    notebook_ids: [],
  });
});

test("单独勾回一个库时提交 include 名单", () => {
  let selection = { allSelected: false, ids: new Set() };
  selection = toggleBaseSelection(selection, "base-b");
  assert.deepEqual(baseScopePayload(selection, ["base-a", "base-b"]), {
    mode: "include",
    notebook_ids: ["base-b"],
  });
});

// 后端 `_validate_base_scope` 对不在当前挂载集里的 id 一律 422。取消挂载不经过来源
// 页签、不会触发选择状态重置,所以过滤必须发生在这里,否则一次卸载就让所有后续提问
// 422 —— 而且 include/exclude 两条分支都要过滤,不能只保 include 那条。
test("已卸载的旧库 id 不会被提交(exclude 与 include 两条分支都过滤)", () => {
  let excluding = defaultBaseScopeSelection();
  excluding = toggleBaseSelection(excluding, "base-gone");
  excluding = toggleBaseSelection(excluding, "base-a");
  assert.deepEqual(baseScopePayload(excluding, ["base-a", "base-b"]), {
    mode: "exclude",
    notebook_ids: ["base-a"],
  });

  const including = { allSelected: false, ids: new Set(["base-gone", "base-b"]) };
  assert.deepEqual(baseScopePayload(including, ["base-a", "base-b"]), {
    mode: "include",
    notebook_ids: ["base-b"],
  });
});

test("挂载集为空时不会凭空提交任何库", () => {
  assert.deepEqual(baseScopePayload(defaultBaseScopeSelection(), []), {
    mode: "include",
    notebook_ids: [],
  });
});

// 库 id 与来源 id 分两份存,不共用一个集合:后端对两者分开校验,一个来源 id 出现在
// notebook_ids 里就是 422(反之亦然)。
test("两维互不串扰:改一维不影响另一维的载荷", () => {
  const sourceSelection = toggleSourceSelection(
    defaultSourceScopeSelection(), "src-1",
  );
  const baseSelection = toggleBaseSelection(defaultBaseScopeSelection(), "base-1");
  assert.deepEqual(sourceScopePayload(sourceSelection, 3), {
    mode: "exclude",
    source_ids: ["src-1"],
  });
  assert.deepEqual(baseScopePayload(baseSelection, ["base-1", "base-2"]), {
    mode: "exclude",
    notebook_ids: ["base-1"],
  });
});


// --- 双段计数文案 ---------------------------------------------------------
// 这句话在来源页签工具条和问答输入框上方各显示一次。收敛成一个纯函数,是为了让
// 「只改了一处」这个坑在结构上不可能发生。

test("没挂参考库时只报本库一段", () => {
  assert.equal(retrievalScopeSummary({ selected: 1, total: 5 }, null), "本库 1/5");
  assert.equal(
    retrievalScopeSummary({ selected: 1, total: 5 }, { selected: 0, total: 0 }),
    "本库 1/5",
  );
});

test("挂了参考库就必须报出第二段(含 0/L 这种全被排除的情形)", () => {
  assert.equal(
    retrievalScopeSummary({ selected: 1, total: 5 }, { selected: 0, total: 1 }),
    "本库 1/5 · 参考库 0/1",
  );
  assert.equal(
    retrievalScopeSummary({ selected: 5, total: 5 }, { selected: 3, total: 3 }),
    "本库 5/5 · 参考库 3/3",
  );
});


// ---------------------------------------------------------------------------
// 本地那一维的空判(后端 _require_non_empty_scope 里 has_local 的镜像)
//
// 起因:只有 Knowhow 表(或只有已确认 Memory)的笔记本可见来源恒为 0,勾选计数因此
// 也恒为 0 —— 拿它当「本地证据宇宙为空」,问答输入框与新建报告会被整个锁死,而那些
// 格子照常可搜。
// ---------------------------------------------------------------------------

test("Knowhow-only 库(零可见来源但后端说有本地证据)不算范围为空", () => {
  assert.equal(localScopeIsEmpty(0, 0, true), false);
});

test("真的什么都没有(零来源、零本地证据)才算范围为空", () => {
  assert.equal(localScopeIsEmpty(0, 0, false), true);
});

test("用户主动清空全部来源时以选择为准,本地证据信号不得把它翻回来", () => {
  assert.equal(localScopeIsEmpty(0, 5, true), true);
});

test("勾了一部分来源就不是空范围", () => {
  assert.equal(localScopeIsEmpty(2, 5, false), false);
  assert.equal(localScopeIsEmpty(5, 5, false), false);
});

test("有来源但尚未解析出证据时仍按来源数放行(信号只增不减)", () => {
  // local_evidence_available 为假(还没有任何 chunk),但 5 篇来源全勾着——
  // 这与该字段存在之前的行为逐字一致,不能因为新信号而多拦一次。
  assert.equal(localScopeIsEmpty(5, 5, false), false);
});
