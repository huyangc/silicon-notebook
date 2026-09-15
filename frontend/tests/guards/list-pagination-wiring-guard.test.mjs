// page.tsx 里清单分页的接线守卫。
//
// 分页本身(切页、夹紧、resetKey)由各组件的组件测试钉住;但 `Home()` 太大,没有能单独
// 渲染的接缝,page.tsx 这一侧「传进去的是不是对的东西」只能按源码语义钉——例如 resetKey
// 被删掉、或出处清单被改回硬截断,组件测试照样全绿。
import test from "node:test";
import assert from "node:assert/strict";

import { jsxElements, parseModule, variableInitializersIn } from "../../test-support/semantic-source.mjs";

test("笔记本集合两个分区共用同一个 resetKey,且它随筛选/搜索/排序/视图变化", async () => {
  const page = await parseModule("page.tsx");
  const sections = jsxElements(page, "NotebookCollectionSection");
  assert.equal(sections.length, 2, "page.tsx 应当恰好挂两处 NotebookCollectionSection(自有 / 群组)");
  for (const section of sections) {
    assert.equal(
      section.bindings.resetKey, "notebookCollectionResetKey",
      "NotebookCollectionSection 没有接 notebookCollectionResetKey:换了筛选还停在上一份结果翻到的页",
    );
  }
  const [key] = variableInitializersIn(page).filter((item) => item.name === "notebookCollectionResetKey");
  assert.ok(key, "找不到 notebookCollectionResetKey 的定义");
  for (const field of ["filter", "searchQuery", "sortMode", "viewMode"]) {
    assert.match(
      key.initializer, new RegExp(`notebookCollection\\.${field}\\b`),
      `notebookCollectionResetKey 没有包含 ${field}:它变化时两个分区不会回到第一页`,
    );
  }
});

test("知识浏览器的出处走分页清单,不再硬截断", async () => {
  const page = await parseModule("page.tsx");
  const lists = jsxElements(page, "KgOccurrenceList");
  assert.equal(lists.length, 1, "知识浏览器条目展开里应当恰好用一处 KgOccurrenceList");
  assert.match(lists[0].bindings.occurrences ?? "", /\.occurrences\b/);
  assert.equal(
    jsxElements(page, "KgOccurrenceCard").length, 0,
    "page.tsx 又直接渲染 KgOccurrenceCard 了——出处清单应当经 KgOccurrenceList 分页",
  );
});

test("「已分享」弹窗外层清单渲染的是当前页", async () => {
  const page = await parseModule("page.tsx");
  const pagers = jsxElements(page, "Pagination").filter((element) => element.attributes.label === "已分享笔记本分页");
  assert.equal(pagers.length, 1, "找不到「已分享笔记本分页」的翻页控件");
  assert.equal(pagers[0].bindings.total, "sharedByMePage.total");
  const [shared] = variableInitializersIn(page).filter((item) => item.name === "sharedByMePage");
  assert.ok(shared, "找不到 sharedByMePage 的定义");
  assert.match(shared.initializer, /useClientPagination\(\s*sharedByMeList\b/);
  assert.ok(
    page.getFullText().includes("sharedByMePage.pageItems.map("),
    "「已分享」弹窗没有按 sharedByMePage.pageItems 渲染——翻页控件在,清单却还是整份",
  );
});
