import assert from "node:assert/strict";
import test from "node:test";

import {
  crossLibrarySourceNotebookId,
  defaultSourceScopeSelection,
  selectedSourceCount,
  sourceIsSelected,
  sourceScopePayload,
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
