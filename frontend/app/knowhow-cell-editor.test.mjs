import test from "node:test";
import assert from "node:assert/strict";

import {
  PROCEDURE_HINT_TEXT,
  SAVE_LABEL,
  SAVE_AND_NEXT_LABEL,
  CANCEL_LABEL,
  EDIT_LABEL,
  ROW_CONTEXT_TOGGLE_LABEL,
  RESTORE_DRAFT_LABEL,
  DISCARD_DRAFT_LABEL,
  draftStorageKey,
  shouldOfferDraftRestore,
  hasUnsavedChanges,
  rowFallbackTitle,
  sortRowsByPosition,
  nextCellCoordinates,
  insertAtCursor,
  insertListMarker,
  insertCodeFence,
  insertImageMarkdown,
  deriveAltFromFilename,
  isImageFile,
} from "./knowhow-cell-editor-logic.ts";

// --- UI 文案常量：byte-exact vs 规格②路A / 任务简报原文 -------------------------

test("PROCEDURE_HINT_TEXT: 与规格②路A 原文逐字一致", () => {
  assert.strictEqual(PROCEDURE_HINT_TEXT, "用有序列表写步骤，系统会识别为可执行步骤");
});

test("底部三按钮文案：与规格②路A 原文逐字一致", () => {
  assert.strictEqual(SAVE_AND_NEXT_LABEL, "保存并下一格");
  assert.strictEqual(SAVE_LABEL, "保存");
  assert.strictEqual(CANCEL_LABEL, "取消");
});

test("EDIT_LABEL: 与规格⑤「编辑」按钮同词", () => {
  assert.strictEqual(EDIT_LABEL, "编辑");
});

test("ROW_CONTEXT_TOGGLE_LABEL: 与任务简报原话「本行其他格子」一致", () => {
  assert.strictEqual(ROW_CONTEXT_TOGGLE_LABEL, "本行其他格子");
});

test("草稿恢复提示按钮：与任务简报原话「恢复/丢弃」一致", () => {
  assert.strictEqual(RESTORE_DRAFT_LABEL, "恢复");
  assert.strictEqual(DISCARD_DRAFT_LABEL, "丢弃");
});

// --- draftStorageKey / shouldOfferDraftRestore / hasUnsavedChanges -------------

test("draftStorageKey: 固定前缀 + rowId + columnId", () => {
  assert.strictEqual(draftStorageKey("r1", "c1"), "kh-cell-draft:r1:c1");
});

test("draftStorageKey: 不同 (rowId,columnId) 产生不同键", () => {
  assert.notStrictEqual(draftStorageKey("r1", "c1"), draftStorageKey("r1", "c2"));
  assert.notStrictEqual(draftStorageKey("r1", "c1"), draftStorageKey("r2", "c1"));
});

test("shouldOfferDraftRestore: 草稿为 null 时不提示", () => {
  assert.strictEqual(shouldOfferDraftRestore(null, "已保存内容"), false);
});

test("shouldOfferDraftRestore: 草稿与已保存内容相同时不提示（陈旧草稿）", () => {
  assert.strictEqual(shouldOfferDraftRestore("同样的内容", "同样的内容"), false);
});

test("shouldOfferDraftRestore: 草稿与已保存内容不同时提示恢复", () => {
  assert.strictEqual(shouldOfferDraftRestore("草稿内容", "已保存内容"), true);
});

test("shouldOfferDraftRestore: 空字符串草稿与非空已保存内容不同——仍提示（空字符串是合法草稿值,非 null）", () => {
  assert.strictEqual(shouldOfferDraftRestore("", "已保存内容"), true);
});

test("hasUnsavedChanges: 内容与已保存一致 → false", () => {
  assert.strictEqual(hasUnsavedChanges("同样", "同样"), false);
});

test("hasUnsavedChanges: 内容与已保存不同 → true", () => {
  assert.strictEqual(hasUnsavedChanges("改过的内容", "原内容"), true);
});

// --- rowFallbackTitle -----------------------------------------------------------

test("rowFallbackTitle: position 0 → 「行 1」（1-based 展示）", () => {
  assert.strictEqual(rowFallbackTitle(0), "行 1");
});

test("rowFallbackTitle: position 5 → 「行 6」", () => {
  assert.strictEqual(rowFallbackTitle(5), "行 6");
});

// --- sortRowsByPosition -----------------------------------------------------------

test("sortRowsByPosition: 按 position 升序排列", () => {
  const rows = [{ id: "r3", position: 2 }, { id: "r1", position: 0 }, { id: "r2", position: 1 }];
  assert.deepStrictEqual(
    sortRowsByPosition(rows).map((r) => r.id),
    ["r1", "r2", "r3"],
  );
});

test("sortRowsByPosition: 不修改原数组（返回新数组）", () => {
  const rows = [{ id: "r2", position: 1 }, { id: "r1", position: 0 }];
  const original = [...rows];
  sortRowsByPosition(rows);
  assert.deepStrictEqual(rows, original);
});

// --- nextCellCoordinates（保存并下一格：行主序 + 跨行 + 末格语义）-----------------

const COLS = [{ id: "c1" }, { id: "c2" }, { id: "c3" }];
const ROWS = [{ id: "r1" }, { id: "r2" }, { id: "r3" }];

test("nextCellCoordinates: 同行内未到最后一列 → 同行下一列", () => {
  assert.deepStrictEqual(nextCellCoordinates(COLS, ROWS, { rowId: "r1", columnId: "c1" }), {
    rowId: "r1",
    columnId: "c2",
  });
});

test("nextCellCoordinates: 本行最后一列、还有下一行 → 跳到下一行第一列", () => {
  assert.deepStrictEqual(nextCellCoordinates(COLS, ROWS, { rowId: "r1", columnId: "c3" }), {
    rowId: "r2",
    columnId: "c1",
  });
});

test("nextCellCoordinates: 中间行最后一列 → 下一行首列（不是表尾也适用）", () => {
  assert.deepStrictEqual(nextCellCoordinates(COLS, ROWS, { rowId: "r2", columnId: "c3" }), {
    rowId: "r3",
    columnId: "c1",
  });
});

test("nextCellCoordinates: 整表最后一格（最后一行的最后一列）→ null（保存后收尾/关闭）", () => {
  assert.strictEqual(nextCellCoordinates(COLS, ROWS, { rowId: "r3", columnId: "c3" }), null);
});

test("nextCellCoordinates: 单行单列表——唯一一格既是首格也是末格 → null", () => {
  assert.strictEqual(
    nextCellCoordinates([{ id: "only-col" }], [{ id: "only-row" }], { rowId: "only-row", columnId: "only-col" }),
    null,
  );
});

test("nextCellCoordinates: 「添加行」引导流程——新行追加表尾，填完新行最后一格自然收尾", () => {
  // 新增行 = 建空行 + 自动打开首格；一路「保存并下一格」应在新行（表的最后
  // 一行）填完最后一列时返回 null，而不是循环回表首。
  const newRow = { id: "new-row" };
  const rowsWithNew = [...ROWS, newRow];
  assert.deepStrictEqual(nextCellCoordinates(COLS, rowsWithNew, { rowId: "new-row", columnId: "c1" }), {
    rowId: "new-row",
    columnId: "c2",
  });
  assert.strictEqual(nextCellCoordinates(COLS, rowsWithNew, { rowId: "new-row", columnId: "c3" }), null);
});

test("nextCellCoordinates: 当前列 id 不在列表内（防御性）→ null", () => {
  assert.strictEqual(nextCellCoordinates(COLS, ROWS, { rowId: "r1", columnId: "unknown" }), null);
});

test("nextCellCoordinates: 当前行 id 不在列表内（防御性）→ null", () => {
  assert.strictEqual(nextCellCoordinates(COLS, ROWS, { rowId: "unknown", columnId: "c1" }), null);
});

test("nextCellCoordinates: 空列数组（防御性，不应崩溃）→ null", () => {
  assert.strictEqual(nextCellCoordinates([], ROWS, { rowId: "r1", columnId: "c1" }), null);
});

// --- insertAtCursor ---------------------------------------------------------------

test("insertAtCursor: 无选区时在光标处插入，光标停在插入文本末尾", () => {
  const result = insertAtCursor({ value: "abcdef", start: 3, end: 3 }, "XYZ");
  assert.strictEqual(result.value, "abcXYZdef");
  assert.strictEqual(result.cursor, 6);
});

test("insertAtCursor: 有选区时替换选中内容", () => {
  const result = insertAtCursor({ value: "hello world", start: 6, end: 11 }, "there");
  assert.strictEqual(result.value, "hello there");
  assert.strictEqual(result.cursor, 11);
});

test("insertAtCursor: 在全文开头插入", () => {
  const result = insertAtCursor({ value: "world", start: 0, end: 0 }, "hello ");
  assert.strictEqual(result.value, "hello world");
  assert.strictEqual(result.cursor, 6);
});

test("insertAtCursor: 在全文末尾插入（空文本场景）", () => {
  const result = insertAtCursor({ value: "", start: 0, end: 0 }, "新内容");
  assert.strictEqual(result.value, "新内容");
  assert.strictEqual(result.cursor, 3);
});

// --- insertListMarker --------------------------------------------------------------

test("insertListMarker: 空文本处插入无序列表标记，不补多余换行", () => {
  const result = insertListMarker({ value: "", start: 0, end: 0 }, false);
  assert.strictEqual(result.value, "- ");
  assert.strictEqual(result.cursor, 2);
});

test("insertListMarker: 光标前是非换行文字时，先换行再插标记", () => {
  const result = insertListMarker({ value: "已有文字", start: 4, end: 4 }, false);
  assert.strictEqual(result.value, "已有文字\n- ");
  assert.strictEqual(result.cursor, 7);
});

test("insertListMarker: 光标已在换行之后时，不补多余空行", () => {
  const result = insertListMarker({ value: "第一行\n", start: 4, end: 4 }, false);
  assert.strictEqual(result.value, "第一行\n- ");
  assert.strictEqual(result.cursor, 6);
});

test("insertListMarker: ordered=true 时插入有序列表标记「1. 」", () => {
  const result = insertListMarker({ value: "", start: 0, end: 0 }, true);
  assert.strictEqual(result.value, "1. ");
  assert.strictEqual(result.cursor, 3);
});

// --- insertCodeFence ----------------------------------------------------------------

test("insertCodeFence: 无选区时插入空代码块，光标停在中间空行", () => {
  const result = insertCodeFence({ value: "", start: 0, end: 0 });
  assert.strictEqual(result.value, "```\n\n```");
  assert.strictEqual(result.cursor, 4);
});

test("insertCodeFence: 有选区时把选中内容包进围栏，光标落在代码之后", () => {
  const result = insertCodeFence({ value: "print(1)", start: 0, end: 8 });
  assert.strictEqual(result.value, "```\nprint(1)\n```");
  assert.strictEqual(result.cursor, 12);
});

test("insertCodeFence: 光标前有非换行文字时先补换行再起围栏", () => {
  const result = insertCodeFence({ value: "说明：", start: 3, end: 3 });
  assert.strictEqual(result.value, "说明：\n```\n\n```");
  assert.strictEqual(result.cursor, 8);
});

// --- insertImageMarkdown -------------------------------------------------------------

test("insertImageMarkdown: 生成 ![alt](asset://<id>) 并插入光标处", () => {
  const result = insertImageMarkdown({ value: "", start: 0, end: 0 }, "asset-1", "示意图");
  assert.strictEqual(result.value, "![示意图](asset://asset-1)");
  assert.strictEqual(result.cursor, result.value.length);
});

test("insertImageMarkdown: alt 为空串时仍生成合法 markdown", () => {
  const result = insertImageMarkdown({ value: "", start: 0, end: 0 }, "asset-2", "");
  assert.strictEqual(result.value, "![](asset://asset-2)");
});

// --- deriveAltFromFilename -----------------------------------------------------------

test("deriveAltFromFilename: 去掉最后一个扩展名", () => {
  assert.strictEqual(deriveAltFromFilename("波形截图.png"), "波形截图");
});

test("deriveAltFromFilename: 多个点时只切最后一段", () => {
  assert.strictEqual(deriveAltFromFilename("v1.2.diagram.png"), "v1.2.diagram");
});

test("deriveAltFromFilename: 无扩展名时原样返回", () => {
  assert.strictEqual(deriveAltFromFilename("截图"), "截图");
});

test("deriveAltFromFilename: 点在开头（隐藏文件式命名）时原样返回整串", () => {
  assert.strictEqual(deriveAltFromFilename(".gitignore"), ".gitignore");
});

test("deriveAltFromFilename: 去除首尾空白", () => {
  assert.strictEqual(deriveAltFromFilename("  截图  .png"), "截图");
});

test("deriveAltFromFilename: 空文件名返回空串", () => {
  assert.strictEqual(deriveAltFromFilename(""), "");
});

// --- isImageFile ---------------------------------------------------------------------

test("isImageFile: image/* 类型返回 true", () => {
  assert.strictEqual(isImageFile({ type: "image/png" }), true);
  assert.strictEqual(isImageFile({ type: "image/jpeg" }), true);
});

test("isImageFile: 非图片类型返回 false", () => {
  assert.strictEqual(isImageFile({ type: "text/plain" }), false);
  assert.strictEqual(isImageFile({ type: "application/pdf" }), false);
});

test("isImageFile: 空类型返回 false", () => {
  assert.strictEqual(isImageFile({ type: "" }), false);
});
