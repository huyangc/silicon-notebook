import test from "node:test";
import assert from "node:assert/strict";

import { crossLibrarySourceNotebookId } from "./source-scope.ts";

test("同库来源不算跨库(空串 = 照常给写入按钮)", () => {
  assert.equal(crossLibrarySourceNotebookId("nb-1", "nb-1"), "");
});

test("参考库来源返回它自己的库 id,供弹窗标注「来自参考库《名》」并收起写入按钮", () => {
  assert.equal(crossLibrarySourceNotebookId("base-9", "nb-1"), "base-9");
});

test("当前库未知(null)时保守地当同库,不凭空标成参考库", () => {
  assert.equal(crossLibrarySourceNotebookId("base-9", null), "");
  assert.equal(crossLibrarySourceNotebookId("base-9", ""), "");
});

test("来源自己没带 notebook_id(旧数据/兜底)时同样不下跨库结论", () => {
  assert.equal(crossLibrarySourceNotebookId("", "nb-1"), "");
});
