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
  TOOLBAR_REFORMAT_LABEL,
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
  BULLET_GLYPHS,
  shouldNormalizePaste,
  ruleNormalize,
  isRichMarkdown,
  CELL_REFORMAT_IDLE,
  CELL_REFORMAT_STALE,
  CELL_REFORMAT_SERVER_STALE,
  REFORMAT_SUGGESTION_LABEL,
  REFORMAT_UNCHANGED_MESSAGE,
  REFORMAT_UNCHANGED_DISMISS_LABEL,
  REFORMAT_STALE_MESSAGE,
  REFORMAT_SERVER_STALE_MESSAGE,
  REFORMAT_LOADING_HINT,
  REFORMAT_UNSAVED_HINT,
  REFORMAT_EMPTY_HINT,
  REFORMAT_SOURCE_LLM_LABEL,
  REFORMAT_SOURCE_RULE_LABEL,
  REFORMAT_SOURCE_FALLBACK_LABEL,
  beginCellReformat,
  resolveCellReformat,
  resolveReformatArrival,
  reformatArrivalNeedsParentRefresh,
  failCellReformat,
  dismissCellReformat,
  isCellReformatLoading,
  reformatCellDisabledReason,
  reformatSourceLabel,
  insertViaExecCommandOrFallback,
} from "./knowhow-cell-editor-logic.ts";
import {
  callSitesIn,
  callsIn,
  declarations,
  findFunction,
  ifBranchesIn,
  ifConditionsIn,
  jsxElements,
  parseModule,
  propertyAccesses,
  stringLiterals,
  variableInitializersIn,
} from "./test/semantic-source.mjs";

const editorModule = await parseModule("knowhow-cell-editor.tsx");

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

test("isEditorBusy: 保存 / 上传 / 优化 / 规整 任一在飞即为忙（挡切兄弟格 + 优化⇄规整互斥）", () => {
  assert.strictEqual(isEditorBusy(false, false, false, false), false);
  assert.strictEqual(isEditorBusy(true, false, false, false), true);
  assert.strictEqual(isEditorBusy(false, true, false, false), true);
  assert.strictEqual(isEditorBusy(false, false, true, false), true);
  // 规整在飞（第 4 个入参）同样算忙——这是 P2 互斥修复的核心：规整期间优化按钮
  // 必须置灰，否则两个建议面板会竞态互相顶掉。
  assert.strictEqual(isEditorBusy(false, false, false, true), true);
});

test("isSaveBlocked: 保存中/上传中挡保存；签名里根本没有 optimizing（LLM 可能卡死，不能锁住存盘）", () => {
  assert.strictEqual(isSaveBlocked(false, false), false);
  assert.strictEqual(isSaveBlocked(true, false), true);
  assert.strictEqual(isSaveBlocked(false, true), true);
  // 「优化中仍可保存」不是靠传 false 传出来的，而是这个判定压根不接受 optimizing
  // 这个入参——用元数不变式把它钉住，避免日后有人把 optimizing 塞进来。
  assert.strictEqual(isSaveBlocked.length, 2);
  assert.strictEqual(isEditorBusy.length, 4);
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


test("editor semantically wires upload through the tested state-machine boundaries", async () => {
  const upload = findFunction(editorModule, "uploadAndInsertImages");
  const uploadCalls = callsIn(upload);
  const accesses = propertyAccesses(editorModule);

  for (const target of [
    "resolveUploadBlock",
    "uploadNotebookAsset",
    "resolveUploadInsertion",
    "applyInsertion",
  ]) {
    assert.ok(uploadCalls.includes(target), target);
  }
  assert.ok(accesses.includes("uploadAbortRef.current?.abort"));
  assert.equal(stringLiterals(editorModule).includes("kh-cell-pending-upload"), false);
});

// --- 优化表达 ⇄ 规整格式 互斥接线（P2 修复）------------------------------------
//
// 两个工具栏按钮（「优化表达」「规整格式」）各调一次可能卡很久的后端请求，同一
// 时刻只允许一条候选在途；互斥**只**经共享的 busy 实现——reformatCellDisabledReason
// / optimizeCellDisabledReason 各自只看自己的 loading，从不看兄弟。一次 rebase graft
// 里 busy 的 isEditorBusy 调用漏掉了 reformat loading 项，导致规整在飞时优化按钮
// 仍可点，两个建议面板互相顶掉。下列接线断言把「busy 单一真源同时纳入两种 loading」
// 钉死。这里查询 TypeScript AST 的调用、守卫和 JSX 绑定，不依赖源码行号、位置或排版。

test("接线：busy 计算必须同时纳入优化与规整 loading（规整在飞→优化置灰，反之亦然）", () => {
  const editor = findFunction(editorModule, "KnowhowCellEditor");
  assert.deepEqual(
    variableInitializersIn(editor).find(({ name }) => name === "busy"),
    {
      name: "busy",
      initializer: "isEditorBusy(savingMode !== null, uploading, isCellOptimizeLoading(optimizeState), isCellReformatLoading(reformatState))",
    },
  );
});

test("接线：两个按钮的 disabled 都经 busy 门控（互斥经共享真源，非各写一份）", () => {
  const buttons = jsxElements(editorModule, "button");
  const optimize = buttons.find(
    ({ bindings }) => bindings?.onClick === "handleOptimize",
  );
  const reformat = buttons.find(
    ({ bindings }) => bindings?.onClick === "handleReformat",
  );
  assert.equal(optimize?.bindings.disabled, "optimizeDisabledReason !== null || busy");
  assert.equal(reformat?.bindings.disabled, "reformatDisabledReason !== null || busy");
});

test("接线：handleOptimize / handleReformat 的 handler 守卫都早退于 busy（键盘/编程触发也互斥）", () => {
  // 置灰只挡鼠标点击；⌘↩ / 程序化触发绕得过 disabled，必须在 handler 入口再挡一次。
  assert.ok(
    ifConditionsIn(findFunction(editorModule, "handleOptimize"))
      .includes("optimizeDisabledReason || busy"),
  );
  assert.ok(
    ifConditionsIn(findFunction(editorModule, "handleReformat"))
      .includes("reformatDisabledReason || busy"),
  );
});

test("接线：置灰必有对得上的原因——两个按钮的 title 在 busy 时落到 BUSY_HINT", () => {
  // knowhow-optimize-logic.ts 声明的不变量：按钮灰着就不能显示「可点」的文案。
  const buttons = jsxElements(editorModule, "button");
  const optimize = buttons.find(
    ({ bindings }) => bindings?.onClick === "handleOptimize",
  );
  const reformat = buttons.find(
    ({ bindings }) => bindings?.onClick === "handleReformat",
  );
  assert.equal(
    optimize?.bindings.title,
    "optimizeDisabledReason ?? (busy ? BUSY_HINT : OPTIMIZE_CELL_BUTTON_LABEL)",
  );
  assert.equal(
    reformat?.bindings.title,
    "reformatDisabledReason ?? (busy ? BUSY_HINT : TOOLBAR_REFORMAT_LABEL)",
  );
});

test("接线：不得留下绕开 busy 单一真源的死常量（graft 残留，回归锁）", () => {
  // optimizeButtonDisabled / reformatButtonDisabled 是一次 graft 里加进来、却从未被
  // 任何按钮引用的「另一套」互斥计算。留着它们=埋一条与 busy 漂移的第二真源，日后
  // 有人接上去就会与 title 用的 busy 判定对不上，破坏「置灰必有对得上的原因」不变量。
  const names = declarations(editorModule).map(({ name }) => name);
  assert.equal(names.includes("optimizeButtonDisabled"), false);
  assert.equal(names.includes("reformatButtonDisabled"), false);
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
  assert.strictEqual(readCellDraft(() => ({ getItem: () => "草稿" }), "k"), "草稿");
  assert.strictEqual(readCellDraft(() => ({ getItem: () => null }), "k"), null);
});

test("readCellDraft: storage 为 null（SSR，无 window）→ null，不抛", () => {
  assert.strictEqual(readCellDraft(() => null, "k"), null);
});

test("readCellDraft: getItem 抛错（隐私模式）→ null，不抛", () => {
  assert.strictEqual(
    readCellDraft(
      () => ({
        getItem() {
          throw new Error("SecurityError");
        },
      }),
      "k",
    ),
    null,
  );
});

// ===========================================================================
// knowhow-md-normalize Task 8：编辑器「规整格式」按钮 + 粘贴即时规整
// ===========================================================================

// --- TOOLBAR_REFORMAT_LABEL --------------------------------------------------

test("TOOLBAR_REFORMAT_LABEL: 与任务简报原文逐字一致", () => {
  assert.strictEqual(TOOLBAR_REFORMAT_LABEL, "规整格式");
});

// --- shouldNormalizePaste：粘贴触发判定 ---------------------------------------
//
// 代价不对称，判定必须保守：漏判(miss)代价低——用户仍可手动点「规整格式」
// 按钮，那条路径带 before/after 对照预览，可以先看结果再决定接受/放弃；
// 误判(false positive)代价高且可能不可恢复——粘贴命中后走的插入路径
// (applyInsertion -> setContent) 是对受控 textarea 编程式设置 .value，不进
// 浏览器原生撤销栈，Cmd+Z 大概率救不回被强行拆散的原文。故要求"正的列表
// 证据"（规则 a/b 之一），不能仅凭一个裸 TAB 就触发——那正是下面这组用例要
// 锁住不再回归的历史缺陷：整段 TAB 缩进的代码（Tcl/Python/Makefile 等）曾被
// 当成 Excel 粘贴内容拆散、逐行插空行，且不可靠撤销。

test("shouldNormalizePaste: 含项目符号 • -> true（规则 a：字形是强证据，代码基本不会用它）", () => {
  assert.strictEqual(shouldNormalizePaste("• 第一项\n• 第二项"), true);
});

test("shouldNormalizePaste: BULLET_GLYPHS 里每个字形单独出现都触发（规则 a）", () => {
  for (const glyph of BULLET_GLYPHS) {
    assert.strictEqual(shouldNormalizePaste(`${glyph} 项`), true, `glyph=${JSON.stringify(glyph)}`);
  }
});

test("shouldNormalizePaste: 缩进行 + 字母子项 marker（Excel「缩进子项」形状）-> true（规则 b）", () => {
  assert.strictEqual(shouldNormalizePaste("1. 步骤一\n\ta. 子项"), true);
});

test("shouldNormalizePaste: 字母顶层标题 + 缩进项目符号子项 -> true（字形已构成规则 a 的证据）", () => {
  assert.strictEqual(shouldNormalizePaste("A. 考量\n\t• 增大 R"), true);
});

test("shouldNormalizePaste: TAB 缩进代码（proc/if 块）-> false——这正是本次修复要堵住的误伤（原判定见 TAB 即触发，会把这段代码拆成散行）", () => {
  const code = 'proc foo {} {\n\tputs "hello"\n}';
  assert.strictEqual(shouldNormalizePaste(code), false);
});

test("shouldNormalizePaste: 任意缩进深度的纯 TAB 缩进代码行 -> false（不因缩进层数变多而误判）", () => {
  assert.strictEqual(shouldNormalizePaste("\t\t\tdeeply indented code"), false);
});

test("shouldNormalizePaste: 普通散文（无 TAB/项目符号）-> false，不干预正常粘贴", () => {
  assert.strictEqual(shouldNormalizePaste("这是一段普通的说明文字，没有任何特殊格式。"), false);
});

test("shouldNormalizePaste: 已是干净 markdown 列表（无缩进、无字形）-> false，规整不会有收益", () => {
  assert.strictEqual(shouldNormalizePaste("- 一\n- 二"), false);
});

test("shouldNormalizePaste: 代码粘贴（空格缩进，不含 TAB/字形/列表 marker）-> false，不误伤代码", () => {
  assert.strictEqual(shouldNormalizePaste("function foo() {\n  return 1;\n}"), false);
});

test("shouldNormalizePaste: 空字符串 -> false", () => {
  assert.strictEqual(shouldNormalizePaste(""), false);
});

test("shouldNormalizePaste: 纯 TAB 分隔单行（Excel 一行原样贴入，如「列1 TAB 列2 TAB 列3」）不再触发——ruleNormalize 对单行输入是 no-op，拦截没有收益", () => {
  const text = "列1\t列2\t列3";
  assert.strictEqual(shouldNormalizePaste(text), false);
  // 用实际调用验证上面注释的论断为真，而不是仅凭断言：单行文本没有列表
  // marker，整行归类成一个 prose 段，ruleNormalize 前后应逐字相同。
  assert.strictEqual(ruleNormalize(text), text);
});

test("shouldNormalizePaste: TAB 出现在行中间（非行首）不构成缩进证据 -> false（规则 b 要求 TAB/空格必须在行首）", () => {
  assert.strictEqual(shouldNormalizePaste("说明文字\t补充说明"), false);
});

// --- I3 修复：规则 a 要求字形是「行首 marker」，而非仅仅「出现在文本中任意
// 位置」-----------------------------------------------------------------------
//
// BULLET_GLYPHS 含 · (U+00B7)，中文里常作人名内的分隔符（如"弗拉基米尔·普京"）。
// 旧判定「字形出现在文本任意位置即触发」会把这种含人名的普通段落误判为
// Excel 列表粘贴，进而拆散成每行一段——且这条插入路径按文件本身的注释不进
// 浏览器原生撤销栈，用户 Cmd+Z 救不回来。修复：字形必须在「行首（允许前面有
// 缩进）+ 后跟空白」时才算 marker，与 ruleNormalize 自己识别 bullet 的标准
// （BULLET_RE 应用在 lstripTabSpace 之后的行首）完全同源。

test("shouldNormalizePaste: 人名中的 · 分隔符（非行首）-> false（I3 回归用例：不再仅凭字形出现即触发）", () => {
  assert.strictEqual(shouldNormalizePaste("会议记录：弗拉基米尔·普京 出席\n第二行"), false);
});

test("shouldNormalizePaste: 行首 · + 空格的真实项目符号列表 -> true（未被 I3 修复误伤）", () => {
  assert.strictEqual(shouldNormalizePaste("· 项目一\n· 项目二"), true);
});

// --- 保守门接入：粘贴内容若已是富 markdown 结构，一律不拦截 --------------------
//
// 与 ruleNormalize 共用同一道 isRichMarkdown 保守闸门（knowhow-md-normalize
// 后续 PR，镜像后端 rule_normalize 的架构性修复）：粘贴进来的内容如果已经是
// 真实 markdown 结构（围栏代码块、GFM 表格……），不该被当成 Excel 脏排版
// 拦截重排——这条插入路径 (applyInsertion -> setContent) 本就不进浏览器原生
// 撤销栈，误判代价高。以下前两个用例是"翻转"回归用例：在接入这道门之前，
// 规则 (a)/(b) 会误判为 true（围栏/表格内部恰好长得像 Excel 缩进子项），
// 接入门之后必须变回 false。

test("shouldNormalizePaste: 围栏代码块内容恰好长得像缩进子项 -> false（门优先于规则 b，翻转回归用例）", () => {
  // 门接入前：line[0]==='\t' 且 lstripTabSpace 后 "a. code inside" 命中
  // ALPHA_RE，规则 b 会误判 true；isRichMarkdown 先命中围栏信号，整体判为
  // 富 markdown，门必须先拦下，规则 a/b 根本不会被跑到。
  const raw = "```\n\ta. code inside\n```";
  assert.strictEqual(isRichMarkdown(raw), true);
  assert.strictEqual(shouldNormalizePaste(raw), false);
});

test("shouldNormalizePaste: 无前导竖线 GFM 表格里一行恰好长得像缩进子项 -> false（门优先于规则 b，翻转回归用例）", () => {
  // 门接入前：第三行 "\ta. weirdo | x" 经 lstripTabSpace 后命中 ALPHA_RE，
  // 规则 b 会误判 true；is_rich_markdown 的"无前导竖线表格行"信号（与分隔行
  // 相邻）先命中第一行，门必须先拦下。
  const raw = "A | B\n--- | ---\n\ta. weirdo | x";
  assert.strictEqual(isRichMarkdown(raw), true);
  assert.strictEqual(shouldNormalizePaste(raw), false);
});

test("shouldNormalizePaste: 粘贴一个干净的围栏代码块 -> false", () => {
  assert.strictEqual(shouldNormalizePaste("```\nconst x = 1;\n```"), false);
});

test("shouldNormalizePaste: 粘贴一个干净的 GFM 表格（行首竖线）-> false", () => {
  assert.strictEqual(shouldNormalizePaste("| A | B |\n| - | - |\n| 1 | 2 |"), false);
});

// --- P2-f：insertViaExecCommandOrFallback —— 优先走原生撤销栈，失败优雅回退 ----
//
// 命中 shouldNormalizePaste 的粘贴 preventDefault 后要插入规整文本。老路径
// applyInsertion->setContent 是程序化赋值 .value，不进 textarea 原生撤销栈、
// Cmd/Ctrl+Z 撤不掉。改进：先试 execCommand("insertText")（进撤销栈 + 触发 input
// 事件让 React onChange 更新 state），返回 false 或抛错才回退老路径。

test("insertViaExecCommandOrFallback: execCommand 返回 true → 不调回退，返回 true（不叠加第二次插入）", () => {
  let fallbackCalls = 0;
  const ok = insertViaExecCommandOrFallback(
    () => true,
    () => {
      fallbackCalls += 1;
    },
  );
  assert.strictEqual(ok, true);
  assert.strictEqual(fallbackCalls, 0);
});

test("insertViaExecCommandOrFallback: execCommand 返回 false（环境已移除该 API）→ 调回退，返回 false", () => {
  let fallbackCalls = 0;
  const ok = insertViaExecCommandOrFallback(
    () => false,
    () => {
      fallbackCalls += 1;
    },
  );
  assert.strictEqual(ok, false);
  assert.strictEqual(fallbackCalls, 1);
});

test("insertViaExecCommandOrFallback: execCommand 抛错 → 吞掉异常、调回退，返回 false（不让异常冒泡毁掉粘贴）", () => {
  let fallbackCalls = 0;
  const ok = insertViaExecCommandOrFallback(
    () => {
      throw new Error("execCommand is not supported");
    },
    () => {
      fallbackCalls += 1;
    },
  );
  assert.strictEqual(ok, false);
  assert.strictEqual(fallbackCalls, 1);
});

test("接线（P2-f）：handlePaste 命中规整时先试 execCommand(insertText)，失败才回退 applyInsertion", () => {
  const calls = callSitesIn(findFunction(editorModule, "handlePaste"));
  const fallbackBoundary = calls.find(
    ({ target }) => target === "insertViaExecCommandOrFallback",
  );
  assert.deepEqual(fallbackBoundary?.arguments, [
    "() => { const el = textareaRef.current; if (!el) return false; el.focus(); return document.execCommand(\"insertText\", false, normalized); }",
    "() => applyInsertion(insertAtCursor(currentSelection(), normalized))",
  ]);
});

// --- CellReformatState：对照状态机（镜像 knowhow-optimize-logic.ts 的
// CellOptimizeState，多一个 changed=false 对应的 "unchanged" 态）---------------

test("CELL_REFORMAT_IDLE: 初始态", () => {
  assert.deepStrictEqual(CELL_REFORMAT_IDLE, { status: "idle" });
});

test("beginCellReformat: idle -> loading", () => {
  assert.deepStrictEqual(beginCellReformat({ status: "idle" }), { status: "loading" });
});

test("beginCellReformat: error -> loading（允许重新点「规整格式」）", () => {
  assert.deepStrictEqual(beginCellReformat({ status: "error", message: "x" }), { status: "loading" });
});

test("beginCellReformat: 已在 loading 时原样返回（防重复触发）", () => {
  const state = { status: "loading" };
  assert.strictEqual(beginCellReformat(state), state);
});

test("resolveCellReformat: loading + changed=true -> ready(candidateMd, source)", () => {
  const next = resolveCellReformat({ status: "loading" }, { candidateMd: "- a\n- b", source: "llm", changed: true });
  assert.deepStrictEqual(next, { status: "ready", candidateMd: "- a\n- b", source: "llm" });
});

test("resolveCellReformat: loading + changed=false -> unchanged(source)（不展示空对照）", () => {
  const next = resolveCellReformat(
    { status: "loading" },
    { candidateMd: "已经规整过的内容", source: "rule/no-llm", changed: false },
  );
  assert.deepStrictEqual(next, { status: "unchanged", source: "rule/no-llm" });
});

test("resolveCellReformat: 非 loading 态收到结果时原样返回（迟到结果不覆盖新状态）", () => {
  const idle = { status: "idle" };
  assert.strictEqual(resolveCellReformat(idle, { candidateMd: "x", source: "llm", changed: true }), idle);
});

// --- P1-d：resolveReformatArrival —— 到达时的陈旧丢弃守卫 ----------------------
//
// reformat_cell 读的是已保存内容；请求在飞期间 textarea 未禁用，用户敲的字会让
// 编辑器内容偏离「发起请求那一刻的快照」。到达时先比对当前内容与快照：变了就
// 丢弃候选转 stale（不拿陈旧候选覆盖用户刚敲的字），没变才照常。

test("CELL_REFORMAT_STALE: 本地陈旧单例带 reason:'local'", () => {
  assert.deepStrictEqual(CELL_REFORMAT_STALE, { status: "stale", reason: "local" });
});

test("CELL_REFORMAT_SERVER_STALE: 服务器陈旧单例带 reason:'server'", () => {
  assert.deepStrictEqual(CELL_REFORMAT_SERVER_STALE, { status: "stale", reason: "server" });
});

test("REFORMAT_STALE_MESSAGE: 与任务简报文案逐字一致", () => {
  assert.strictEqual(REFORMAT_STALE_MESSAGE, "编辑器内容已变化，本次规整结果已失效，请重试");
});

test("REFORMAT_SERVER_STALE_MESSAGE: 服务器陈旧文案（与本地陈旧区分）", () => {
  assert.strictEqual(REFORMAT_SERVER_STALE_MESSAGE, "服务器上的内容已被其他人修改，请关闭后重新打开");
  assert.notStrictEqual(REFORMAT_SERVER_STALE_MESSAGE, REFORMAT_STALE_MESSAGE);
});

test("resolveReformatArrival: 内容未变 + sourceMd 一致 + changed=true → ready（照常委托 resolveCellReformat）", () => {
  const next = resolveReformatArrival({ status: "loading" }, "原文", "原文", {
    candidateMd: "- a\n- b",
    source: "llm",
    changed: true,
    sourceMd: "原文",
  });
  assert.deepStrictEqual(next, { status: "ready", candidateMd: "- a\n- b", source: "llm" });
});

test("resolveReformatArrival: 内容未变 + sourceMd 一致 + changed=false → unchanged（照常委托）", () => {
  const next = resolveReformatArrival({ status: "loading" }, "原文", "原文", {
    candidateMd: "原文",
    source: "rule/no-llm",
    changed: false,
    sourceMd: "原文",
  });
  assert.deepStrictEqual(next, { status: "unchanged", source: "rule/no-llm" });
});

test("resolveReformatArrival: 内容已变（用户在飞期间敲了字）→ 本地 stale，绝不展示陈旧候选", () => {
  const next = resolveReformatArrival({ status: "loading" }, "原文", "原文+用户新敲的字", {
    candidateMd: "- 基于旧原文的候选",
    source: "llm",
    changed: true,
    sourceMd: "原文",
  });
  assert.deepStrictEqual(next, { status: "stale", reason: "local" });
  // 关键：候选内容没有被带出去（没有 candidateMd 字段可供 setContent 覆盖）。
  assert.strictEqual(next.candidateMd, undefined);
});

test("resolveReformatArrival: 内容已变但 changed=false 也一样丢弃转本地 stale（陈旧就是陈旧，与 changed 无关）", () => {
  const next = resolveReformatArrival({ status: "loading" }, "原文", "改过的内容", {
    candidateMd: "原文",
    source: "rule/no-llm",
    changed: false,
    sourceMd: "原文",
  });
  assert.deepStrictEqual(next, { status: "stale", reason: "local" });
});

// F3：本地内容没变、但后端回带的 sourceMd ≠ 发起快照——他人在本编辑器打开后又保存
// 过这一格，候选是拿本地从没见过的服务器内容算出来的，对照面板左侧「原文」已是旧值。
test("resolveReformatArrival: sourceMd 与发起快照不一致（他人已改）→ 服务器 stale，绝不展示误导候选", () => {
  const next = resolveReformatArrival({ status: "loading" }, "原文", "原文", {
    candidateMd: "基于更新后服务器内容的候选",
    source: "llm",
    changed: true,
    sourceMd: "他人保存后的新原文",
  });
  assert.deepStrictEqual(next, { status: "stale", reason: "server" });
  assert.strictEqual(next.candidateMd, undefined);
});

test("resolveReformatArrival: sourceMd 不一致即便 changed=false 也判服务器 stale（左侧原文已对不上）", () => {
  const next = resolveReformatArrival({ status: "loading" }, "原文", "原文", {
    candidateMd: "原文",
    source: "rule/no-llm",
    changed: false,
    sourceMd: "他人保存后的新原文",
  });
  assert.deepStrictEqual(next, { status: "stale", reason: "server" });
});

// 本地改动与服务器改动同时发生：本地优先（用户的字仍在、"重试"是对他们的正确指引；
// 且本地分支保持 P1-d 既有契约不变），故 reason 仍是 local。
test("resolveReformatArrival: 本地已变 + sourceMd 也不一致 → 本地 stale 优先", () => {
  const next = resolveReformatArrival({ status: "loading" }, "原文", "原文+新字", {
    candidateMd: "候选",
    source: "llm",
    changed: true,
    sourceMd: "他人保存后的新原文",
  });
  assert.deepStrictEqual(next, { status: "stale", reason: "local" });
});

test("resolveReformatArrival: 非 loading 态 → 原样返回（迟到结果不覆盖新状态，同 resolveCellReformat）", () => {
  const idle = { status: "idle" };
  assert.strictEqual(
    resolveReformatArrival(idle, "原文", "改过的内容", { candidateMd: "x", source: "llm", changed: true, sourceMd: "原文" }),
    idle,
  );
  const err = { status: "error", message: "boom" };
  assert.strictEqual(
    resolveReformatArrival(err, "原文", "原文", { candidateMd: "x", source: "llm", changed: true, sourceMd: "原文" }),
    err,
  );
});

test("端到端（P1-d）：loading → 用户在飞期间改动 → 到达转本地 stale → 「知道了」回 idle（内容未被覆盖）", () => {
  let state = beginCellReformat(CELL_REFORMAT_IDLE);
  assert.strictEqual(state.status, "loading");
  // 发起时快照="草稿A"，到达时实时内容已变成"草稿A+B"。
  state = resolveReformatArrival(state, "草稿A", "草稿A+B", { candidateMd: "规整候选", source: "llm", changed: true, sourceMd: "草稿A" });
  assert.deepStrictEqual(state, { status: "stale", reason: "local" });
  state = dismissCellReformat();
  assert.deepStrictEqual(state, { status: "idle" });
});

// --- F（review）：server-stale 必须触发父级刷新表 detail ---------------------------
//
// 缺陷：到达解析判 server-stale（他人在本编辑器打开后又保存过这一格）时提示「请关闭
// 后重新打开」，但关闭只清 modal——父级 detail 快照（savedContent 之源）没被重拉，重开
// 撞同一 server-stale 态、永远循环。修复：组件在到达后按 reformatArrivalNeedsParentRefresh
// 判定回调 onServerStale，父级重取整表，下次打开就有新鲜 savedContent。纯谓词单测 +
// 接线源码 pin（.tsx 在本仓库 node:test 模型里只能源码断言，见下方「接线守卫」一节）。

test("reformatArrivalNeedsParentRefresh: server-stale → true（须请父级刷新）", () => {
  assert.strictEqual(reformatArrivalNeedsParentRefresh(CELL_REFORMAT_SERVER_STALE), true);
  assert.strictEqual(reformatArrivalNeedsParentRefresh({ status: "stale", reason: "server" }), true);
});

test("reformatArrivalNeedsParentRefresh: 本地 stale → false（重试即可，服务器基线没变，不刷新）", () => {
  assert.strictEqual(reformatArrivalNeedsParentRefresh(CELL_REFORMAT_STALE), false);
  assert.strictEqual(reformatArrivalNeedsParentRefresh({ status: "stale", reason: "local" }), false);
});

test("reformatArrivalNeedsParentRefresh: 其余态（ready/unchanged/idle/loading/error）→ false（不刷新）", () => {
  assert.strictEqual(reformatArrivalNeedsParentRefresh({ status: "ready", candidateMd: "x", source: "llm" }), false);
  assert.strictEqual(reformatArrivalNeedsParentRefresh({ status: "unchanged", source: "rule/no-llm" }), false);
  assert.strictEqual(reformatArrivalNeedsParentRefresh(CELL_REFORMAT_IDLE), false);
  assert.strictEqual(reformatArrivalNeedsParentRefresh({ status: "loading" }), false);
  assert.strictEqual(reformatArrivalNeedsParentRefresh({ status: "error", message: "boom" }), false);
});

// --- P1-d 接线守卫：纯函数锁不住「组件确实这么接线」，用 AST 语义契约补上。---

test("接线（P1-d）：handleReformat 发起前快照 content，到达用 resolveReformatArrival + contentRef.current 比对", () => {
  const handler = findFunction(editorModule, "handleReformat");
  assert.deepEqual(
    variableInitializersIn(handler).find(({ name }) => name === "contentAtRequest"),
    { name: "contentAtRequest", initializer: "content" },
  );
  const calls = callSitesIn(handler);
  assert.deepEqual(
    calls.find(({ target }) => target === "resolveReformatArrival"),
    {
      target: "resolveReformatArrival",
      arguments: ["state", "contentAtRequest", "contentRef.current", "result"],
    },
  );
  assert.equal(calls.some(({ target }) => target === "resolveCellReformat"), false);
});

test("接线（P1-d）：handleAcceptReformat 第二道守卫——内容偏离已保存则拒绝、转 CELL_REFORMAT_STALE，不 setContent", () => {
  const guard = ifBranchesIn(
    findFunction(editorModule, "handleAcceptReformat"),
  ).find(
    ({ condition }) => condition === "hasUnsavedChanges(content, savedContent)",
  );
  assert.equal(guard?.thenReturns, true);
  assert.ok(guard?.thenCalls.includes("setReformatState"));
  assert.equal(guard?.thenCalls.includes("setContent"), false);
});

// 接线（F3）：纯函数把服务器陈旧判成 reason:"server"，组件必须读取该原因选择文案。
test("接线（F3）：stale 渲染按 reformatState.reason 选服务器/本地文案", () => {
  assert.ok(propertyAccesses(editorModule).includes("reformatState.reason"));
});

// 接线（F review）：server-stale 转入的那一刻必须回调 onServerStale 请父级刷新表 detail
// （否则关掉重开撞同一陈旧态、永远循环）。用 effect 观察 reformatState，谓词
// reformatArrivalNeedsParentRefresh 判定；回调收进 ref、effect 只依赖 [reformatState]，
// 避免父级 inline 回调换 identity 在仍是 server-stale 期间被无关重渲染重复触发。
test("接线（F）：onServerStale 收进 ref 并随 prop 同步（不让 effect 直接依赖易变回调）", () => {
  const editor = findFunction(editorModule, "KnowhowCellEditor");
  assert.deepEqual(
    variableInitializersIn(editor).find(({ name }) => name === "onServerStaleRef"),
    { name: "onServerStaleRef", initializer: "useRef(onServerStale)" },
  );
  assert.ok(
    callSitesIn(editor).some(
      ({ target, arguments: args }) => (
        target === "useEffect"
        && args[0] === "() => { onServerStaleRef.current = onServerStale; }"
        && args[1] === "[onServerStale]"
      ),
    ),
  );
});

test("接线（F）：notify effect 按谓词回调、且 deps 恰为 [reformatState]（只触发一次、本地 stale 不触发）", () => {
  // 谓词 reformatArrivalNeedsParentRefresh 只对 reason:"server" 为真（见 logic 单测），
  // 故本地 stale 不触发；deps 恰 [reformatState] + singleton 引用稳定 => 每次「转入」只
  // 触发一次。把 effect 体 + dep 数组一起锚死：改用裸 onServerStale 或把它塞进 deps（会
  // 在 server-stale 期间被无关重渲染重复触发）都会红。
  const effects = callSitesIn(
    findFunction(editorModule, "KnowhowCellEditor"),
  );
  assert.ok(
    effects.some(
      ({ target, arguments: args }) => (
        target === "useEffect"
        && args[0] === "() => { if (reformatArrivalNeedsParentRefresh(reformatState)) onServerStaleRef.current?.(); }"
        && args[1] === "[reformatState]"
      ),
    ),
  );
});

// --- F3 接线守卫：规整「接受」在图片上传在飞时必须互斥——与「优化表达」的
//     handleAcceptSuggestion 完全对称（accept 按钮 disabled + handler 内先 return）。
//     缺陷：规整在飞时用户可粘贴图片起一个上传，随后在上传落地前点「接受」，
//     setContent(candidateMd) 与晚到的上传续写落在不同的整篇快照上，图片被追加到
//     末尾/与已接受内容竞态。优化侧已用同一套 uploading 门守住，这里镜像它。
//     纯函数锁不住 JSX/handler 接线，因此用 AST 分支和 JSX 属性绑定补上。-----------

test("接线（F3）：handleAcceptReformat 在 uploading 时先 setSaveError(ACCEPT_BLOCKED_UPLOADING_HINT) 并 return，镜像 handleAcceptSuggestion", () => {
  for (const handlerName of ["handleAcceptSuggestion", "handleAcceptReformat"]) {
    const guard = ifBranchesIn(
      findFunction(editorModule, handlerName),
    ).find(({ condition }) => condition === "uploading");
    assert.equal(guard?.thenReturns, true, handlerName);
    assert.ok(guard?.thenCalls.includes("setSaveError"), handlerName);
    assert.equal(guard?.thenCalls.includes("setContent"), false, handlerName);
  }
});

test("接线（F3）：规整「接受」按钮 uploading 时置灰 + 对得上的 title（字面同优化「接受」按钮）", () => {
  const buttons = jsxElements(editorModule, "button");
  for (const handlerName of ["handleAcceptSuggestion", "handleAcceptReformat"]) {
    const button = buttons.find(
      ({ bindings }) => bindings?.onClick === handlerName,
    );
    assert.equal(button?.bindings.disabled, "uploading", handlerName);
    assert.equal(
      button?.bindings.title,
      "uploading ? ACCEPT_BLOCKED_UPLOADING_HINT : undefined",
      handlerName,
    );
  }
});

test("failCellReformat: loading -> error(message)", () => {
  assert.deepStrictEqual(failCellReformat({ status: "loading" }, "规整格式失败，请重试"), {
    status: "error",
    message: "规整格式失败，请重试",
  });
});

test("failCellReformat: 非 loading 态原样返回", () => {
  const ready = { status: "ready", candidateMd: "x", source: "llm" };
  assert.strictEqual(failCellReformat(ready, "err"), ready);
});

test("dismissCellReformat: 任何时候都回到 idle", () => {
  assert.deepStrictEqual(dismissCellReformat(), { status: "idle" });
});

test("isCellReformatLoading: 仅 loading 态为 true", () => {
  assert.strictEqual(isCellReformatLoading({ status: "idle" }), false);
  assert.strictEqual(isCellReformatLoading({ status: "loading" }), true);
  assert.strictEqual(isCellReformatLoading({ status: "ready", candidateMd: "x", source: "llm" }), false);
  assert.strictEqual(isCellReformatLoading({ status: "unchanged", source: "llm" }), false);
  assert.strictEqual(isCellReformatLoading({ status: "stale" }), false);
  assert.strictEqual(isCellReformatLoading({ status: "error", message: "x" }), false);
});

// --- reformatCellDisabledReason（同 optimizeCellDisabledReason 的优先级：
// 正在请求中 > 有未保存修改 > 内容为空 > 可点击）------------------------------

test("reformatCellDisabledReason: 正在规整中 -> 优先于其它原因", () => {
  assert.strictEqual(
    reformatCellDisabledReason("有未保存修改", "已保存", { status: "loading" }),
    REFORMAT_LOADING_HINT,
  );
});

test("reformatCellDisabledReason: 有未保存修改 -> 提示先保存", () => {
  assert.strictEqual(reformatCellDisabledReason("新内容", "旧内容", { status: "idle" }), REFORMAT_UNSAVED_HINT);
});

test("reformatCellDisabledReason: 内容为空(且无未保存修改) -> 提示格子为空", () => {
  assert.strictEqual(reformatCellDisabledReason("", "", { status: "idle" }), REFORMAT_EMPTY_HINT);
  assert.strictEqual(reformatCellDisabledReason("   ", "   ", { status: "idle" }), REFORMAT_EMPTY_HINT);
});

test("reformatCellDisabledReason: 已保存且非空 -> null（可点击）", () => {
  assert.strictEqual(reformatCellDisabledReason("已保存的内容", "已保存的内容", { status: "idle" }), null);
});

test("reformatCellDisabledReason: ready/unchanged 态且内容一致非空 -> null", () => {
  assert.strictEqual(
    reformatCellDisabledReason("已保存的内容", "已保存的内容", { status: "ready", candidateMd: "x", source: "llm" }),
    null,
  );
  assert.strictEqual(
    reformatCellDisabledReason("已保存的内容", "已保存的内容", { status: "unchanged", source: "llm" }),
    null,
  );
});

test("readCellDraft: localStorage 属性 getter 本身抛 SecurityError → null，不崩（回归锁）", () => {
  // 第 8 轮复审探针：受限存储环境下 window.localStorage 属性访问自身抛错。thunk 在
  // try 内求值，异常不逃逸；若把 getStorage() 提到 try 外，本条立刻红。
  assert.strictEqual(
    readCellDraft(() => {
      throw new Error("SecurityError");
    }, "k"),
    null,
  );
});
// --- reformatSourceLabel：后端 source 枚举 -> 友好文案（CRITICAL：原始枚举
// 值绝不能泄漏到界面上）-------------------------------------------------------

test("reformatSourceLabel: 'llm' -> 「已用 AI 规整格式」", () => {
  assert.strictEqual(reformatSourceLabel("llm"), REFORMAT_SOURCE_LLM_LABEL);
  assert.strictEqual(reformatSourceLabel("llm"), "已用 AI 规整格式");
});

test("reformatSourceLabel: 'rule/llm-failed' -> 「已用规则规整格式」（不暴露 AI 失败这一实现细节）", () => {
  assert.strictEqual(reformatSourceLabel("rule/llm-failed"), REFORMAT_SOURCE_RULE_LABEL);
  assert.strictEqual(reformatSourceLabel("rule/llm-failed"), "已用规则规整格式");
});

test("reformatSourceLabel: 'rule/no-llm' -> 「已用规则规整格式」", () => {
  assert.strictEqual(reformatSourceLabel("rule/no-llm"), REFORMAT_SOURCE_RULE_LABEL);
});

test("reformatSourceLabel: 未知/意外枚举值 -> 兜底友好文案，绝不泄漏原始字符串", () => {
  const label = reformatSourceLabel("some-unexpected-enum-value");
  assert.strictEqual(label, REFORMAT_SOURCE_FALLBACK_LABEL);
  assert.ok(!label.includes("some-unexpected-enum-value"));
  assert.ok(!label.includes("rule/"));
  assert.ok(!label.includes("llm"));
});

test("reformatSourceLabel: 空字符串 -> 兜底友好文案", () => {
  assert.strictEqual(reformatSourceLabel(""), REFORMAT_SOURCE_FALLBACK_LABEL);
});

test("reformatSourceLabel: 三个已知枚举值的映射结果都不是裸枚举字符串本身", () => {
  for (const raw of ["llm", "rule/llm-failed", "rule/no-llm"]) {
    assert.notStrictEqual(reformatSourceLabel(raw), raw);
  }
});

// --- 其余文案常量非空 ---------------------------------------------------------

test("REFORMAT_SUGGESTION_LABEL / REFORMAT_UNCHANGED_MESSAGE / REFORMAT_UNCHANGED_DISMISS_LABEL: 非空文案", () => {
  for (const value of [REFORMAT_SUGGESTION_LABEL, REFORMAT_UNCHANGED_MESSAGE, REFORMAT_UNCHANGED_DISMISS_LABEL]) {
    assert.ok(typeof value === "string" && value.length > 0);
  }
  assert.strictEqual(REFORMAT_UNCHANGED_MESSAGE, "已经是规整格式，无需改动");
});

// --- 端到端：规整格式对照状态机走一遍完整流程 ---------------------------------

test("端到端：规整格式——loading -> ready -> 放弃回到 idle", () => {
  let state = CELL_REFORMAT_IDLE;
  state = beginCellReformat(state);
  assert.strictEqual(state.status, "loading");
  state = resolveCellReformat(state, { candidateMd: "- a\n- b", source: "llm", changed: true });
  assert.deepStrictEqual(state, { status: "ready", candidateMd: "- a\n- b", source: "llm" });
  state = dismissCellReformat();
  assert.deepStrictEqual(state, { status: "idle" });
});

test("端到端：规整格式——changed=false 时到 unchanged，「知道了」回到 idle", () => {
  let state = beginCellReformat(CELL_REFORMAT_IDLE);
  state = resolveCellReformat(state, { candidateMd: "原内容", source: "rule/no-llm", changed: false });
  assert.deepStrictEqual(state, { status: "unchanged", source: "rule/no-llm" });
  state = dismissCellReformat();
  assert.deepStrictEqual(state, { status: "idle" });
});

test("端到端：规整格式——失败后可重试", () => {
  let state = beginCellReformat(CELL_REFORMAT_IDLE);
  state = failCellReformat(state, "规整格式失败，请重试");
  assert.strictEqual(state.status, "error");
  state = beginCellReformat(state);
  assert.strictEqual(state.status, "loading");
});
