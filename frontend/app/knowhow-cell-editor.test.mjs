import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

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
  readCellDraft,
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
  normalizeCellViewMode,
  CELL_VIEW_MODE_STORAGE_KEY,
  VIEW_MODE_EDIT_LABEL,
  VIEW_MODE_SPLIT_LABEL,
  VIEW_MODE_PREVIEW_LABEL,
  SWITCH_GUARD_MESSAGE,
  SWITCH_GUARD_DISCARD_LABEL,
  DRAFT_FLUSH_FAILED_MESSAGE,
  CLOSE_GUARD_MESSAGE,
  resolveCloseRequest,
  resolveSwitchRequest,
  draftFlushAction,
  applyDraftFlush,
  isEditorBusy,
  isSaveBlocked,
  imageMarkdown,
  resolveUploadInsertion,
  resolveLeaveDuringUpload,
  LEAVE_WAITING_UPLOAD_HINT,
  DISCARD_UPLOAD_AND_LEAVE_LABEL,
  isAbortError,
  SAVE_BLOCKED_UPLOADING_HINT,
  SAVE_IN_FLIGHT_UPLOAD_HINT,
  BUSY_UPLOAD_HINT,
  ACCEPT_BLOCKED_UPLOADING_HINT,
  RESTORE_PENDING_UPLOAD_HINT,
  resolveUploadBlock,
  resolveSaveCompletion,
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

// --- 编辑器视图三态 + 切格子守卫文案 ---------------------------------------------

test("normalizeCellViewMode: null / 空 / 非法值 → edit（唯一默认口径）", () => {
  assert.strictEqual(normalizeCellViewMode(null), "edit");
  assert.strictEqual(normalizeCellViewMode(""), "edit");
  assert.strictEqual(normalizeCellViewMode("garbage"), "edit");
  assert.strictEqual(normalizeCellViewMode("EDIT"), "edit"); // 大小写敏感
});

test("normalizeCellViewMode: 三个合法值原样返回", () => {
  assert.strictEqual(normalizeCellViewMode("edit"), "edit");
  assert.strictEqual(normalizeCellViewMode("split"), "split");
  assert.strictEqual(normalizeCellViewMode("preview"), "preview");
});

test("CELL_VIEW_MODE_STORAGE_KEY: 与全屏键区分、editor 专用", () => {
  assert.strictEqual(CELL_VIEW_MODE_STORAGE_KEY, "knowhow.cellEditor.viewMode");
});

test("视图三态标签：编辑 / 并列 / 预览", () => {
  assert.strictEqual(VIEW_MODE_EDIT_LABEL, "编辑");
  assert.strictEqual(VIEW_MODE_SPLIT_LABEL, "并列");
  assert.strictEqual(VIEW_MODE_PREVIEW_LABEL, "预览");
});

test("切格子守卫：放弃按钮文案 + 提醒含未保存/可恢复", () => {
  assert.strictEqual(SWITCH_GUARD_DISCARD_LABEL, "放弃并切换");
  assert.match(SWITCH_GUARD_MESSAGE, /未保存/);
  assert.match(SWITCH_GUARD_MESSAGE, /可恢复/);
});

// --- 离开决策纯函数（关闭 / 切格；每条路径的下一步都在此定死，执行器只负责
//     先 flushDraft 再执行 leave）------------------------------------------------

test("DRAFT_FLUSH_FAILED_MESSAGE: 存储不可用时不做虚假「可恢复」承诺", () => {
  assert.match(DRAFT_FLUSH_FAILED_MESSAGE, /存储不可用/);
  assert.match(DRAFT_FLUSH_FAILED_MESSAGE, /请先复制/);
  assert.doesNotMatch(DRAFT_FLUSH_FAILED_MESSAGE, /可恢复/);
});

test("resolveCloseRequest: 无守卫 + 有改动 → 弹关闭守卫、暂不离开", () => {
  assert.deepStrictEqual(resolveCloseRequest(null, true), { next: { kind: "close" }, leave: null });
});

test("resolveCloseRequest: 无守卫 + 无改动 → 立刻关闭（仍经执行器）", () => {
  assert.deepStrictEqual(resolveCloseRequest(null, false), { next: null, leave: { kind: "close" } });
});

test("resolveCloseRequest: 关闭守卫已弹再触发（二次 Esc）→ 强制关闭 leave（执行器会先落草稿）", () => {
  // 这是首版漏洞的回归锁：二次 Esc 必须产出一个 leave（→ 经执行器落草稿），
  // 不能是「直接 onClose 不落草稿」。
  assert.deepStrictEqual(resolveCloseRequest({ kind: "close" }, true), { next: null, leave: { kind: "close" } });
});

test("resolveCloseRequest: 切格守卫弹着时按 Esc → 取消切换、不离开、不误关整窗", () => {
  assert.deepStrictEqual(
    resolveCloseRequest({ kind: "switch", columnId: "c9" }, true),
    { next: null, leave: null },
  );
});

test("resolveSwitchRequest: 有改动 → 弹切格守卫、暂不切", () => {
  assert.deepStrictEqual(resolveSwitchRequest("c2", true), { next: { kind: "switch", columnId: "c2" }, leave: null });
});

test("resolveSwitchRequest: 无改动 → 立刻切（仍经执行器落草稿=清旧稿）", () => {
  assert.deepStrictEqual(resolveSwitchRequest("c2", false), { next: null, leave: { kind: "switch", columnId: "c2" } });
});

test("draftFlushAction: 有未保存改动 → write（无视恢复提示）", () => {
  assert.strictEqual(draftFlushAction(true, false), "write");
  assert.strictEqual(draftFlushAction(true, true), "write");
});

test("draftFlushAction: 无改动 + 恢复提示开着 → keep（不能删掉待恢复的旧草稿——回归锁）", () => {
  // 复审发现的回归：离开时对「无改动」也 flushDraft，若无条件清旧稿会抢在用户
  // 点「恢复」前删掉刚检测到的上次草稿。此处定死：恢复提示开着就 keep。
  assert.strictEqual(draftFlushAction(false, true), "keep");
});

test("draftFlushAction: 无改动 + 无恢复提示 → remove（清陈旧草稿）", () => {
  assert.strictEqual(draftFlushAction(false, false), "remove");
});


// --- applyDraftFlush：真实副作用路径（用会抛错的假 storage 覆盖 window.localStorage
//     在单测里没法触发的配额/隐私模式失败）--------------------------------------

test("applyDraftFlush: write 成功 → true，且确实写入了内容", () => {
  const calls = [];
  const storage = { setItem: (k, v) => calls.push(["set", k, v]), removeItem: (k) => calls.push(["rm", k]) };
  assert.strictEqual(applyDraftFlush(storage, "k", "正文", "write"), true);
  assert.deepStrictEqual(calls, [["set", "k", "正文"]]);
});

test("applyDraftFlush: write 抛错（配额/隐私模式）→ false，调用方据此不许离开", () => {
  const storage = {
    setItem: () => {
      throw new Error("QuotaExceededError");
    },
    removeItem: () => {},
  };
  assert.strictEqual(applyDraftFlush(storage, "k", "正文", "write"), false);
});

test("applyDraftFlush: keep 完全不碰 storage（保住待恢复的旧草稿）", () => {
  const calls = [];
  const storage = { setItem: () => calls.push("set"), removeItem: () => calls.push("rm") };
  assert.strictEqual(applyDraftFlush(storage, "k", "正文", "keep"), true);
  assert.deepStrictEqual(calls, []);
});

test("applyDraftFlush: remove 清旧稿；removeItem 抛错也算安全（本就无内容可丢）", () => {
  const calls = [];
  const ok = { setItem: () => {}, removeItem: (k) => calls.push(k) };
  assert.strictEqual(applyDraftFlush(ok, "k", "", "remove"), true);
  assert.deepStrictEqual(calls, ["k"]);
  const throwing = {
    setItem: () => {},
    removeItem: () => {
      throw new Error("nope");
    },
  };
  assert.strictEqual(applyDraftFlush(throwing, "k", "", "remove"), true);
});

// --- busy 互斥 + 陈旧回调守卫 -------------------------------------------------

test("isEditorBusy: 保存 / 上传 / 优化 任一在飞即为忙（用于挡切兄弟格）", () => {
  assert.strictEqual(isEditorBusy(false, false, false), false);
  assert.strictEqual(isEditorBusy(true, false, false), true);
  assert.strictEqual(isEditorBusy(false, true, false), true);
  assert.strictEqual(isEditorBusy(false, false, true), true);
});

test("isSaveBlocked: 保存中/上传中挡保存；签名里根本没有 optimizing（LLM 可能卡死，不能锁住存盘）", () => {
  assert.strictEqual(isSaveBlocked(false, false), false);
  assert.strictEqual(isSaveBlocked(true, false), true);
  assert.strictEqual(isSaveBlocked(false, true), true);
  // 「优化中仍可保存」不是靠传 false 传出来的，而是这个判定压根不接受 optimizing
  // 这个入参——用元数不变式把它钉住，避免日后有人把 optimizing 塞进来。
  assert.strictEqual(isSaveBlocked.length, 2);
  assert.strictEqual(isEditorBusy.length, 3);
});

// --- 保存成功后的草稿处置：与离开路径共用同一套 draftFlushAction/applyDraftFlush，
//     基线是「这次真正落库的内容」，比较对象是**解析时刻的实时内容**（不是发起
//     保存时捕获的那份）------------------------------------------------------------

test("保存收尾：期间没再敲字（实时内容===落库内容）→ remove，清掉冗余草稿", () => {
  assert.strictEqual(draftFlushAction(hasUnsavedChanges("X+AAA", "X+AAA"), false), "remove");
});

test("保存收尾：期间用户继续敲（实时内容≠落库内容）→ write 实时内容（回归锁）", () => {
  // 发起保存时捕获 "X+AAA"，其后用户敲成 "X+AAA+BBB"。保存返回后组件即将卸载、
  // 300ms 自动草稿定时器会被 clearTimeout 掐掉，此刻若不写实时内容，"+BBB" 就
  // 从服务端和本地同时消失。
  assert.strictEqual(draftFlushAction(hasUnsavedChanges("X+AAA+BBB", "X+AAA"), false), "write");
});

test("保存收尾：恢复提示还开着 → keep，绝不动那份未决定的旧草稿", () => {
  assert.strictEqual(draftFlushAction(hasUnsavedChanges("X", "X"), true), "keep");
});

// --- 上传落笔基于「实时内容」而非上传开始时的快照（复审：接受的建议被旧快照覆盖）---

test("imageMarkdown: 产出 asset:// 图片片段", () => {
  assert.strictEqual(imageMarkdown("a1", "图"), "![图](asset://a1)");
  assert.strictEqual(imageMarkdown("a1", ""), "![](asset://a1)");
});

// 以下直接调用**生产落笔函数** resolveUploadInsertion。此前这里是测试自己手工调
// insertAtCursor 模拟一遍，生产即便改回「从上传起始快照整篇写回」也照样绿——那样
// 等于没锁住回归。

test("resolveUploadInsertion: 正文期间没变 → 插回粘贴时的光标处", () => {
  const r = resolveUploadInsertion("abcdef", 3, "abcdef", "<IMG>");
  assert.strictEqual(r.value, "abc<IMG>def");
  assert.strictEqual(r.cursor, 8);
});

test("resolveUploadInsertion: 正文期间被改写 → 追加末尾，绝不回退到旧快照（回归锁）", () => {
  // 时序：在 "原文" 上粘贴图片 → 上传在飞 → 优化建议返回、用户点「接受」，正文
  // 变成 "优化稿" → 上传完成。旧写法会把 "优化稿" 整段覆盖回 "原文+图"。
  const snippet = imageMarkdown("a1", "图");
  const r = resolveUploadInsertion("原文", 2, "优化稿", snippet);
  assert.strictEqual(r.value, `优化稿${snippet}`);
  assert.doesNotMatch(r.value, /原文/);
  assert.notStrictEqual(r.value, `原文${snippet}`);
});

test("resolveUploadInsertion: 起始光标偏移越界 → 夹到合法范围，不抛不越界", () => {
  const r = resolveUploadInsertion("很长的一段原文", 99, "很长的一段原文", "<IMG>");
  assert.strictEqual(r.value, "很长的一段原文<IMG>");
});


test("并发提示文案彼此可区分（各自对应一种在飞操作，不能混用）", () => {
  const hints = [
    SAVE_BLOCKED_UPLOADING_HINT,
    SAVE_IN_FLIGHT_UPLOAD_HINT,
    BUSY_UPLOAD_HINT,
    ACCEPT_BLOCKED_UPLOADING_HINT,
    LEAVE_WAITING_UPLOAD_HINT,
  ];
  assert.strictEqual(new Set(hints).size, hints.length);
});

test("resolveSaveCompletion: 已卸载 → none（陈旧回调绝不操作后来打开的格子）", () => {
  // 复审指出的跨格污染：A 格保存途中被关掉、用户打开 B 格，A 的旧回调若仍执行
  // onNavigate/onClose 就会把 B 关掉或跳走，B 里刚输入且防抖未落盘的内容随之丢失。
  // 注意本测试锁的是**决策表**，不是组件接线——「runSave 确实把 mountedRef.current
  // 传进来」这一步 .tsx 侧无法在本仓库的 node:test 模型里断言（见文件头说明）。
  assert.deepStrictEqual(resolveSaveCompletion("next", { rowId: "r2", columnId: "c1" }, false), { kind: "none" });
  assert.deepStrictEqual(resolveSaveCompletion("save", null, false), { kind: "none" });
});

test("resolveSaveCompletion: 仍挂载 + 保存并下一格 + 有下一格 → navigate", () => {
  assert.deepStrictEqual(resolveSaveCompletion("next", { rowId: "r2", columnId: "c1" }, true), {
    kind: "navigate",
    rowId: "r2",
    columnId: "c1",
  });
});

test("resolveSaveCompletion: 仍挂载 + 已是整表末格 → close", () => {
  assert.deepStrictEqual(resolveSaveCompletion("next", null, true), { kind: "close" });
});

test("resolveSaveCompletion: 仍挂载 + 普通保存 → close（不跳格）", () => {
  assert.deepStrictEqual(resolveSaveCompletion("save", { rowId: "r2", columnId: "c1" }, true), { kind: "close" });
});

test("守卫文案：不再用「已自动保存」过去式承诺（同步落盘发生在点「放弃」之后）", () => {
  for (const msg of [CLOSE_GUARD_MESSAGE, SWITCH_GUARD_MESSAGE]) {
    assert.doesNotMatch(msg, /已自动保存/);
    assert.match(msg, /可恢复/);
  }
});


// --- 上传中离开：延后而不是强退 -------------------------------------------------
//
// 这里锁的是本 PR 的核心契约变更：上传在飞时的离开请求**一律延后**，由上传收尾
// 提交。此前是「警告一次、再点一次强制离开」，continuation 会落到已卸载的组件上，
// 于是需要在 localStorage 上自造一套「待完成上传日志 + 认领」协议来救那些图片——
// 那套协议连续四轮被查出竞态（首次 mount 的 effect 顺序、跨标签页 claim 的非原子
// 读改写删、部分写入失败导致的丢失+重复、键名字符串排序打乱多图顺序）。改成延后
// 之后，「上传比编辑器活得久」这个前提本身不成立，那一整类竞态没有了落脚点。

test("resolveLeaveDuringUpload: 上传在飞 → defer（绝不放行，也不丢弃）", () => {
  assert.deepStrictEqual(resolveLeaveDuringUpload(true, { kind: "close" }), {
    kind: "defer",
    intent: { kind: "close" },
  });
  assert.deepStrictEqual(resolveLeaveDuringUpload(true, { kind: "switch", columnId: "c2" }), {
    kind: "defer",
    intent: { kind: "switch", columnId: "c2" },
  });
});

test("resolveLeaveDuringUpload: 没有上传在飞 → commit（照常走落草稿+离开）", () => {
  assert.deepStrictEqual(resolveLeaveDuringUpload(false, { kind: "close" }), {
    kind: "commit",
    intent: { kind: "close" },
  });
});

test("上传中离开的文案：只承诺「完成后自动离开」，不再承诺「会尽量留着」（回归锁）", () => {
  // 旧文案要向用户承诺强退后图片「会尽量留着、下次打开补进来」——那份承诺依赖
  // localStorage 可用，存储不可用时兑现不了。现在离开要么等上传落完（图片真在
  // 正文里），要么明确放弃（abort 掉），没有需要打折的承诺。
  assert.doesNotMatch(LEAVE_WAITING_UPLOAD_HINT, /尽量/);
  assert.doesNotMatch(LEAVE_WAITING_UPLOAD_HINT, /下次打开/);
  assert.doesNotMatch(LEAVE_WAITING_UPLOAD_HINT, /再点一次/);
  assert.match(LEAVE_WAITING_UPLOAD_HINT, /自动离开/);
  // 放弃出口必须让用户看得出「这次上传不要了」，而不是含糊的「关闭」。
  assert.match(DISCARD_UPLOAD_AND_LEAVE_LABEL, /放弃/);
});

test("isAbortError: 只认 AbortError，普通失败照常报错", () => {
  const aborted = new Error("aborted");
  aborted.name = "AbortError";
  assert.strictEqual(isAbortError(aborted), true);
  assert.strictEqual(isAbortError(new Error("500 boom")), false);
  assert.strictEqual(isAbortError(null), false);
  assert.strictEqual(isAbortError("AbortError"), false);
});

// --- 批量上传：传一张落一张，顺序与位置都由 resolveUploadInsertion 决定 ---------

// 下面这条按生产循环的真实写法推进锚点（landed.value / landed.cursor），而不是
// 测试自己另写一套拼接：生产若改回「整批传完再一起落」或忘了推进锚点，这里就红。
function landBatch(startValue, startCaret, snippets) {
  let anchorSnapshot = startValue;
  let anchorCaret = startCaret;
  let live = startValue;
  for (const snippet of snippets) {
    const landed = resolveUploadInsertion(anchorSnapshot, anchorCaret, live, snippet);
    live = landed.value;
    anchorSnapshot = landed.value;
    anchorCaret = landed.cursor;
  }
  return live;
}

test("批量上传：多张图按选择顺序连续落在粘贴处（>10 张也不乱序）", () => {
  // 旧实现把待认领的图片按 localStorage 键名字符串排序还原顺序，而键名是
  // `<随机UUID>-<序号>`：跨批次顺序随机、同批超过 10 张时序号排成 0,1,10,11,2…。
  // 现在顺序由「传完即落笔」的循环本身保证，与任何键名编码无关。
  const snippets = Array.from({ length: 12 }, (_, i) => imageMarkdown(`a${i}`, `图${i}`));
  const value = landBatch("前后", 1, snippets);
  assert.strictEqual(value, `前${snippets.join("")}后`);
  const positions = snippets.map((s) => value.indexOf(s));
  assert.deepStrictEqual(positions, [...positions].sort((a, b) => a - b));
});

test("批量上传：中途中断（放弃/卸载）→ 已传完的那几张仍留在正文里", () => {
  // 「传一张落一张」的意义：abort 时前面成功的图片已经在正文里、随草稿同步落盘，
  // 不会变成「服务端有资产、正文无引用」的孤儿。整批传完再一起落则会全丢。
  const done = [imageMarkdown("a0", "图0"), imageMarkdown("a1", "图1")];
  assert.strictEqual(landBatch("原文", 2, done), `原文${done.join("")}`);
});

test("批量上传：期间正文被改写 → 后续图片追加末尾，绝不拿旧偏移劈开新正文", () => {
  const first = imageMarkdown("a0", "图0");
  const second = imageMarkdown("a1", "图1");
  const afterFirst = resolveUploadInsertion("abcdef", 3, "abcdef", first);
  // 用户在两张之间恢复了草稿：实时正文与锚点快照不再相等。
  const afterSecond = resolveUploadInsertion(afterFirst.value, afterFirst.cursor, "另一份正文", second);
  assert.strictEqual(afterSecond.value, `另一份正文${second}`);
});

// --- 接线守卫：纯函数锁不住「组件确实这么接线」，用源码断言补上 -----------------
//
// 本仓库前端测试是 node --test 跑 .mjs、只 import 得了纯 .ts（.tsx 需要 JSX
// 变换），组件的 effect 顺序/异步收尾无法真渲染断言。仓库既有做法是对源码本身下
// 断言（见 architecture-boundaries.test.mjs），这里沿用：锁住的都是**改坏了就会
// 复现已知数据丢失**的接线点，不是形态偏好。

const editorSrc = await readFile(new URL("./knowhow-cell-editor.tsx", import.meta.url), "utf8");
const modelSrc = await readFile(new URL("./knowhow-model.ts", import.meta.url), "utf8");
const uploadFnSrc = editorSrc.slice(
  editorSrc.indexOf("async function uploadAndInsertImages"),
  editorSrc.indexOf("function handleFileInputChange"),
);
// 守卫必须切到**具体的块**再断言。此前这两条写成「函数体里同时出现 A 和 B」，
// `[\s\S]*?` 会越过块的收尾大括号——把落笔挪到循环外、把提交延后离开挪出 finally，
// 守卫照样绿（都是真 bug：前者中途中断全丢图，后者上传出错时窗口永远关不掉）。
// 切到**循环自己的收尾大括号**（6 空格缩进），不是切到 `} catch`：后者会把「挪到
// 循环之后、catch 之前」的落笔也圈进来，那正是要防的整批落笔形态。
const uploadLoopStart = uploadFnSrc.indexOf("for (const file of images) {");
const uploadLoopSrc = uploadFnSrc.slice(
  uploadLoopStart,
  uploadFnSrc.indexOf("\n      }", uploadLoopStart),
);
const uploadFinallySrc = uploadFnSrc.slice(uploadFnSrc.indexOf("} finally {"));

test("接线：上传请求必须带 AbortSignal，且 fetcher 真的把它转发出去", () => {
  // 少了它，「上传比编辑器活得久」就又成立了——continuation 落到已卸载的树上，
  // 资产在服务端却没有任何东西引用它。
  assert.ok(uploadFnSrc.length > 0, "uploadAndInsertImages 函数体没截到，守卫失效");
  assert.match(uploadFnSrc, /uploadNotebookAsset\(\s*notebookId,\s*file,\s*controller\.signal\s*\)/);
  assert.match(modelSrc, /uploadNotebookAsset = \([\s\S]*?signal\?: AbortSignal,[\s\S]*?\{[\s\S]*?body: form, signal \}/);
});

test("接线：上传是「传一张落一张」——落笔必须在 for 循环体内", () => {
  // 整批落笔时，中途 abort/失败会让已经传完的图片全部既进不了正文、也无从回收。
  assert.ok(uploadLoopSrc.length > 0, "for 循环体没截到，守卫失效");
  assert.match(uploadLoopSrc, /applyInsertion\(landed\);/);
});

test("接线：延后的离开必须在 finally 里提交（try 里提交挡不住出错/中断路径）", () => {
  // 放在 try 末尾的话，上传报错或被 abort 时根本走不到：用户点了关闭却永远关不掉
  // （延后期间 Esc/背景/关闭都被 requestClose 早退挡住，只剩「继续编辑」）。
  assert.ok(uploadFinallySrc.length > 0, "finally 块没截到，守卫失效");
  assert.match(uploadFinallySrc, /leaveAfterUploadRef\.current;[\s\S]*?commitLeaveRef\.current\(deferred\)/);
});

test("接线：离开门读的是同步 ref，不是落后一拍的 uploading state", () => {
  // 读 state 会让「粘贴完立刻按 Esc」这条路直接放行（那一刻 state 还是 false）。
  assert.match(editorSrc, /resolveLeaveDuringUpload\(uploadingRef\.current, intent\)/);
});

test("接线：卸载清理必须中断上传并同步落草稿", () => {
  // 父级直接卸载（返回列表/切笔记本）时没有任何离开路径跑过，300ms 自动草稿定时器
  // 也会被清掉——不在这里落盘就等于把刚敲的字和刚落笔的图片引用一起丢掉。
  // 切到 cleanup 块本身，不靠数字符窗口（多写两行注释就会假红）。
  const afterMountedFalse = editorSrc.slice(editorSrc.indexOf("mountedRef.current = false;"));
  const unmountCleanup = afterMountedFalse.slice(0, afterMountedFalse.indexOf("}, []);"));
  assert.ok(unmountCleanup.length > 0, "卸载清理块没截到，守卫失效");
  assert.match(unmountCleanup, /uploadAbortRef\.current\?\.abort\(\);/);
  // 只写不删：StrictMode 的「挂载→立刻卸载→再挂载」里，若照完整决策 remove，
  // 会把刚被扫描发现、用户还没决定的草稿删掉。
  assert.match(unmountCleanup, /if \(hasUnsavedChanges\(contentRef\.current, savedContentRef\.current\)\) flushDraftRef\.current\(\);/);
});

test("接线：不得再把待完成上传寄存到 localStorage（回归锁）", () => {
  // 那套「日志 + 认领」协议连续四轮被查出竞态（首次 mount 的 effect 顺序、跨标签页
  // claim 的非原子读改写删、部分写入失败导致丢失+重复、键名排序打乱多图顺序）。
  // 现在靠「上传不比编辑器活得久」在结构上消除，别再加回来。
  // 盯日志本身的符号，而不是裸子串 "PENDING_UPLOAD"：后者会被
  // RESTORE_PENDING_UPLOAD_HINT 这类无关常量误伤（本轮就误报过一次）。
  assert.equal(editorSrc.includes("kh-cell-pending-upload"), false);
  assert.equal(editorSrc.includes("PENDING_UPLOAD_EVENT"), false);
  assert.equal(editorSrc.includes("PENDING_UPLOAD_STORAGE_PREFIX"), false);
  assert.equal(editorSrc.includes("pendingUploadStorageKey"), false);
  assert.equal(editorSrc.includes("selectPendingUploadKeys"), false);
});

// --- 恢复提示未决时禁止上传 -----------------------------------------------------
//
// 复审第 6 轮 P1：banner 未决时上传入口仍开着，而「恢复」是**整段**
// setContent(draftText)、此刻自动草稿又处于暂停（否则会抢在用户决定前改写那份待恢复
// 草稿）——落笔的图片没有第二份记录，一点「恢复」就随整段覆盖消失，服务端那条资产
// 再没有任何东西引用它。删掉待完成上传日志后更没有兜底，故从入口挡住。

test("resolveUploadBlock: 恢复提示未决 → 拒绝上传（回归锁）", () => {
  assert.strictEqual(resolveUploadBlock(false, false, false, true), RESTORE_PENDING_UPLOAD_HINT);
});

test("resolveUploadBlock: 全空闲且无待决草稿 → 放行", () => {
  assert.strictEqual(resolveUploadBlock(false, false, false, false), null);
});

test("resolveUploadBlock: 在飞的异步先说（会自己结束），恢复提示后说（要用户动手）", () => {
  // 保存在飞同时草稿未决：先提示保存，它自己会结束。
  assert.strictEqual(resolveUploadBlock(true, false, false, true), SAVE_IN_FLIGHT_UPLOAD_HINT);
  // 优化/另一批上传在飞同时草稿未决：草稿那条更可操作，先说它。
  assert.strictEqual(resolveUploadBlock(false, true, false, true), RESTORE_PENDING_UPLOAD_HINT);
  assert.strictEqual(resolveUploadBlock(false, false, true, true), RESTORE_PENDING_UPLOAD_HINT);
});

test("resolveUploadBlock: 上传/优化在飞（无待决草稿）→ 笼统忙态", () => {
  assert.strictEqual(resolveUploadBlock(false, true, false, false), BUSY_UPLOAD_HINT);
  assert.strictEqual(resolveUploadBlock(false, false, true, false), BUSY_UPLOAD_HINT);
});

test("RESTORE_PENDING_UPLOAD_HINT: 指向用户能动手的那两个按钮", () => {
  assert.match(RESTORE_PENDING_UPLOAD_HINT, /恢复/);
  assert.match(RESTORE_PENDING_UPLOAD_HINT, /丢弃/);
});

test("接线：上传门禁必须在任何 await 之前同步判定，且把恢复提示算进去", () => {
  // paste/drop 不经工具栏按钮，绕得过置灰；判据还必须读同步 ref——「刚点完恢复/
  // 丢弃」与粘贴可能落在同一帧，读 state 会漏判。
  const beforeFirstAwait = uploadFnSrc.slice(0, uploadFnSrc.indexOf("await "));
  assert.match(beforeFirstAwait, /resolveUploadBlock\(\s*savingMode !== null,\s*uploading \|\| uploadingRef\.current,\s*isCellOptimizeLoading\(optimizeState\),\s*restoreBannerRef\.current,\s*\)/);
  assert.match(beforeFirstAwait, /if \(uploadBlocked\) \{[\s\S]*?return;/);
});

test("接线：恢复提示未决时工具栏「图片」按钮置灰，且提示语对得上", () => {
  const imageButton = editorSrc.slice(
    editorSrc.indexOf("onClick={handleImageButtonClick}") - 400,
    editorSrc.indexOf("onClick={handleImageButtonClick}") + 120,
  );
  assert.match(imageButton, /disabled=\{uploading \|\| showRestoreBanner\}/);
  assert.match(imageButton, /title=\{showRestoreBanner \? RESTORE_PENDING_UPLOAD_HINT : TOOLBAR_IMAGE_LABEL\}/);
});

// --- 草稿扫描必须在首帧之前完成（否则门禁 fail-open）-----------------------------
//
// 复审第 7 轮 P1：把上传门禁接上 restoreBanner 之后仍有一个**扫描前窗口**——
// showRestoreBanner/restoreBannerRef 初值都是 false，而扫描原先在普通 useEffect 里，
// 被动 effect 通常要等首帧绘制之后才跑。界面在那之间已经可交互，粘贴/拖放读到假的
// false 就启动上传；随后 banner 才出现，用户一点「恢复」又把刚落笔的图片整段覆盖掉，
// 与上一条 P1 同样的丢失+孤儿资产。
//
// 修法不是再加一个 draftScanDone 门（那只是把窗口改成"拒绝"），而是把扫描挪进
// useState 初始化器：banner 在**第一帧**就是终值，窗口不存在。下面锁的正是这一点。

test("readCellDraft: 正常读到值", () => {
  assert.strictEqual(readCellDraft({ getItem: () => "草稿" }, "k"), "草稿");
  assert.strictEqual(readCellDraft({ getItem: () => null }, "k"), null);
});

test("readCellDraft: storage 为 null（SSR，无 window）→ null，不抛", () => {
  assert.strictEqual(readCellDraft(null, "k"), null);
});

test("readCellDraft: 读取抛错（隐私模式）→ null，不抛", () => {
  assert.strictEqual(
    readCellDraft(
      {
        getItem() {
          throw new Error("SecurityError");
        },
      },
      "k",
    ),
    null,
  );
});

test("接线：草稿扫描在 useState 初始化器里同步完成，不在 effect 里", () => {
  // 只要它回到 effect，首帧到 effect 提交之间的门禁就又 fail-open 了。
  assert.match(editorSrc, /const \[draftScan\] = useState\(\(\) => \{[\s\S]*?readCellDraft\([\s\S]*?\}\);/);
  // 全文只此一处读草稿，杜绝"初始化器里读一份、effect 里又读一份"的分叉。
  assert.strictEqual((editorSrc.match(/readCellDraft\(/g) || []).length, 1);
});

test("接线：banner 与其同步镜像 ref 都从首帧扫描结果取初值", () => {
  assert.match(editorSrc, /const \[showRestoreBanner, setShowRestoreBanner\] = useState\(draftScan\.offer\);/);
  assert.match(editorSrc, /const restoreBannerRef = useRef\(showRestoreBanner\);/);
});
