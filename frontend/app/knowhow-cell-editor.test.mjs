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
  pendingUploadStorageKey,
  pendingUploadKeyPrefix,
  selectPendingUploadKeys,
  SAVE_BLOCKED_UPLOADING_HINT,
  SAVE_IN_FLIGHT_UPLOAD_HINT,
  BUSY_UPLOAD_HINT,
  ACCEPT_BLOCKED_UPLOADING_HINT,
  LEAVE_BLOCKED_UPLOADING_HINT,
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
    LEAVE_BLOCKED_UPLOADING_HINT,
  ];
  assert.strictEqual(new Set(hints).size, hints.length);
  // 「上传中离开」必须讲明再点一次会发生什么；且**不得**把「图片残留在服务器」
  // 固化成预期——强退后图片进待完成上传日志，下次打开这一格会被认领补回，不是孤儿。
  assert.match(LEAVE_BLOCKED_UPLOADING_HINT, /再点一次/);
  assert.match(LEAVE_BLOCKED_UPLOADING_HINT, /下次打开/);
  // 「尽量」是刻意的：storage 完全不可用时这条承诺兑现不了（已知边界，见 PR）。
  assert.match(LEAVE_BLOCKED_UPLOADING_HINT, /尽量/);
  assert.doesNotMatch(LEAVE_BLOCKED_UPLOADING_HINT, /残留/);
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


// --- 待完成上传：一 upload 一键，避免整键 read-modify-write 的多标签页竞态 -------

test("pendingUploadStorageKey: 与草稿键彻底不同（绝不共用）", () => {
  const p = pendingUploadStorageKey("r1", "c1", "b1-0");
  assert.notStrictEqual(p, draftStorageKey("r1", "c1"));
  assert.match(p, /^kh-cell-pending-upload:r1:c1:b1-0$/);
});

test("selectPendingUploadKeys: 只挑本格的键，且按插入顺序还原", () => {
  const all = [
    "kh-cell-draft:r1:c1",
    pendingUploadStorageKey("r1", "c1", "b1-1"),
    pendingUploadStorageKey("r9", "c9", "b1-0"),
    pendingUploadStorageKey("r1", "c1", "b1-0"),
    "unrelated",
  ];
  assert.deepStrictEqual(selectPendingUploadKeys(all, "r1", "c1"), [
    pendingUploadStorageKey("r1", "c1", "b1-0"),
    pendingUploadStorageKey("r1", "c1", "b1-1"),
  ]);
});

test("selectPendingUploadKeys: 认领只覆盖此刻读到的键——并发新增的键不在其中（回归锁）", () => {
  // 多标签页竞态：A 读到 [E1]，B 随后写入 E2。旧的整键 read-modify-write/remove
  // 会让 A 的 removeItem 把 E2 一起删掉，E2 的图就永久没有引用了。
  const beforeConcurrentWrite = [pendingUploadStorageKey("r1", "c1", "b1-0")];
  const claimed = selectPendingUploadKeys(beforeConcurrentWrite, "r1", "c1");
  const afterConcurrentWrite = [...beforeConcurrentWrite, pendingUploadStorageKey("r1", "c1", "b2-0")];
  const remaining = selectPendingUploadKeys(afterConcurrentWrite, "r1", "c1").filter((k) => !claimed.includes(k));
  assert.deepStrictEqual(remaining, [pendingUploadStorageKey("r1", "c1", "b2-0")]);
});

test("pendingUploadKeyPrefix: 前缀含行列、不会误伤别格", () => {
  assert.strictEqual(pendingUploadKeyPrefix("r1", "c1"), "kh-cell-pending-upload:r1:c1:");
  assert.ok(!pendingUploadKeyPrefix("r1", "c1").startsWith(pendingUploadKeyPrefix("r1", "c12")));
});
