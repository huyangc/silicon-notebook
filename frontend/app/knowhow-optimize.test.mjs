// knowhow-optimize-logic.ts 的纯逻辑单测（Task 9：模板+优化 UI）。The three
// consumer .tsx files (knowhow-cell-editor.tsx / knowhow-panel.tsx /
// knowhow-import.tsx) all contain JSX, so — mirroring every other
// knowhow-*-logic.ts split in this codebase — the testable state machines and
// display-mapping helpers live in knowhow-optimize-logic.ts and are imported
// directly here.
import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

import {
  CELL_OPTIMIZE_IDLE,
  OPTIMIZE_CELL_BUTTON_LABEL,
  ROW_OPTIMIZE_BUTTON_LABEL,
  OPTIMIZE_ORIGINAL_LABEL,
  OPTIMIZE_SUGGESTION_LABEL,
  ACCEPT_SUGGESTION_LABEL,
  DISCARD_SUGGESTION_LABEL,
  OPTIMIZE_LOADING_HINT,
  OPTIMIZE_UNSAVED_HINT,
  OPTIMIZE_EMPTY_HINT,
  beginCellOptimize,
  resolveCellOptimizeSuggestion,
  failCellOptimize,
  dismissCellOptimize,
  isCellOptimizeLoading,
  optimizeCellDisabledReason,
  ROW_OPTIMIZE_STATUS_LABELS,
  ROW_OPTIMIZE_SKIP_LABEL,
  ROW_OPTIMIZE_RETRY_LABEL,
  ROW_OPTIMIZE_ABORT_LABEL,
  ROW_OPTIMIZE_CLOSE_LABEL,
  ROW_OPTIMIZE_EMPTY_TEXT,
  ROW_OPTIMIZE_DONE_TEXT,
  initRowOptimizeQueue,
  currentRowOptimizeItem,
  isRowOptimizeQueueFinished,
  rowOptimizeProgress,
  markCurrentInProgress,
  applySuggestion,
  applyError,
  beginAcceptCurrent,
  completeAcceptCurrent,
  failAcceptCurrent,
  skipCurrent,
  retryCurrent,
  abortQueue,
  mapAppendDuplicatesForDisplay,
  isAppendPreviewRowDuplicate,
  formatUnmatchedColumnsMessage,
  templateDownloadFilename,
  BATCH_REFORMAT_ROW_BUTTON_LABEL,
  BATCH_REFORMAT_TABLE_BUTTON_LABEL,
  BATCH_REFORMAT_START_LABEL,
  BATCH_REFORMAT_ABORT_LABEL,
  BATCH_REFORMAT_CONFIRM_LABEL,
  BATCH_REFORMAT_DISCARD_LABEL,
  BATCH_REFORMAT_CLOSE_LABEL,
  BATCH_REFORMAT_EMPTY_TEXT_ROW,
  BATCH_REFORMAT_EMPTY_TEXT_TABLE,
  BATCH_REFORMAT_NO_CHANGES_TEXT,
  BATCH_REFORMAT_DONE_TEXT,
  BATCH_REFORMAT_STATUS_LABELS,
  BATCH_REFORMAT_ABORTED_PENDING_LABEL,
  batchReformatScaleText,
  initReformatBatch,
  reformatBatchSummary,
  reformatReviewingSummaryText,
  beginReformatBatchRun,
  markReformatItemRunning,
  applyReformatResult,
  applyReformatError,
  abortReformatBatchRun,
  finishReformatBatchRun,
  beginReformatBatchSave,
  markReformatItemSaving,
  applyReformatSaveSuccess,
  applyReformatSaveError,
  finishReformatBatchSave,
  reformatBatchSaveNeedsRefresh,
  reformatBatchRunNeedsRefresh,
  reformatDedupeKey,
  applyReformatCachedResult,
  planReformatSaves,
  reformatSaveCellKey,
  isReformatUnitStale,
  markReformatItemStaleSkipped,
  BATCH_REFORMAT_STALE_SKIP_TEXT,
  reformatResultIsStale,
  markReformatItemRunStale,
} from "./knowhow-optimize-logic.ts";
// P1（扇出同源）：合并共享组的实时重算用这套（与 handleCellSave 同源），下面的
// 「实时组分裂 -> 扇出漂移」逻辑测试据它坐实缺陷机制。
import {
  groupRowsByAnchor,
  isSharedColumn,
  groupCellWriteTargets,
} from "./knowhow-grouping-logic.ts";

// ===========================================================================
// 1. 单格「优化表达」对照状态机
// ===========================================================================

test("UI 文案常量：与规格③/任务简报原文逐字一致", () => {
  assert.strictEqual(OPTIMIZE_CELL_BUTTON_LABEL, "优化表达");
  assert.strictEqual(ROW_OPTIMIZE_BUTTON_LABEL, "优化整行");
  assert.strictEqual(OPTIMIZE_ORIGINAL_LABEL, "原文");
  assert.strictEqual(OPTIMIZE_SUGGESTION_LABEL, "优化建议");
  assert.strictEqual(ACCEPT_SUGGESTION_LABEL, "接受");
  assert.strictEqual(DISCARD_SUGGESTION_LABEL, "放弃");
});

test("CELL_OPTIMIZE_IDLE: 初始态", () => {
  assert.deepStrictEqual(CELL_OPTIMIZE_IDLE, { status: "idle" });
});

test("beginCellOptimize: idle -> loading", () => {
  assert.deepStrictEqual(beginCellOptimize({ status: "idle" }), { status: "loading" });
});

test("beginCellOptimize: error -> loading（允许重新点「优化表达」）", () => {
  assert.deepStrictEqual(beginCellOptimize({ status: "error", message: "x" }), { status: "loading" });
});

test("beginCellOptimize: 已在 loading 时保持不变（防重复触发）", () => {
  const state = { status: "loading" };
  assert.strictEqual(beginCellOptimize(state), state);
});

test("resolveCellOptimizeSuggestion: loading -> ready(suggestionMd)", () => {
  assert.deepStrictEqual(resolveCellOptimizeSuggestion({ status: "loading" }, "改写后的文本"), {
    status: "ready",
    suggestionMd: "改写后的文本",
  });
});

test("resolveCellOptimizeSuggestion: 非 loading 态收到结果时原样返回（迟到结果不覆盖新状态）", () => {
  const idle = { status: "idle" };
  assert.strictEqual(resolveCellOptimizeSuggestion(idle, "x"), idle);
});

test("failCellOptimize: loading -> error(message)", () => {
  assert.deepStrictEqual(failCellOptimize({ status: "loading" }, "优化服务暂时不可用，请稍后再试"), {
    status: "error",
    message: "优化服务暂时不可用，请稍后再试",
  });
});

test("failCellOptimize: 非 loading 态原样返回", () => {
  const ready = { status: "ready", suggestionMd: "x" };
  assert.strictEqual(failCellOptimize(ready, "err"), ready);
});

test("dismissCellOptimize: 任何时候都回到 idle", () => {
  assert.deepStrictEqual(dismissCellOptimize(), { status: "idle" });
});

test("isCellOptimizeLoading: 仅 loading 态为 true", () => {
  assert.strictEqual(isCellOptimizeLoading({ status: "idle" }), false);
  assert.strictEqual(isCellOptimizeLoading({ status: "loading" }), true);
  assert.strictEqual(isCellOptimizeLoading({ status: "ready", suggestionMd: "x" }), false);
  assert.strictEqual(isCellOptimizeLoading({ status: "error", message: "x" }), false);
});

// --- optimizeCellDisabledReason -------------------------------------------------

test("optimizeCellDisabledReason: 正在优化中 -> 优先于其它原因", () => {
  assert.strictEqual(optimizeCellDisabledReason("有未保存修改", "已保存", { status: "loading" }), OPTIMIZE_LOADING_HINT);
});

test("optimizeCellDisabledReason: 有未保存修改 -> 提示先保存", () => {
  assert.strictEqual(optimizeCellDisabledReason("新内容", "旧内容", { status: "idle" }), OPTIMIZE_UNSAVED_HINT);
});

test("optimizeCellDisabledReason: 内容为空(且无未保存修改) -> 提示格子为空", () => {
  assert.strictEqual(optimizeCellDisabledReason("", "", { status: "idle" }), OPTIMIZE_EMPTY_HINT);
  assert.strictEqual(optimizeCellDisabledReason("   ", "   ", { status: "idle" }), OPTIMIZE_EMPTY_HINT);
});

test("optimizeCellDisabledReason: 已保存且非空 -> null（可点击）", () => {
  assert.strictEqual(optimizeCellDisabledReason("已保存的内容", "已保存的内容", { status: "idle" }), null);
});

test("optimizeCellDisabledReason: ready 态且内容一致非空 -> null", () => {
  assert.strictEqual(
    optimizeCellDisabledReason("已保存的内容", "已保存的内容", { status: "ready", suggestionMd: "x" }),
    null,
  );
});

// ===========================================================================
// 2. 「优化整行」逐格队列
// ===========================================================================

test("ROW_OPTIMIZE_STATUS_LABELS: 六态文案与任务简报原文逐字一致", () => {
  assert.deepStrictEqual(ROW_OPTIMIZE_STATUS_LABELS, {
    waiting: "等待",
    in_progress: "进行中",
    ready: "建议就绪",
    accepted: "已接受",
    skipped: "已跳过",
    error: "出错",
  });
});

test("按钮/文案常量非空", () => {
  for (const value of [
    ROW_OPTIMIZE_SKIP_LABEL,
    ROW_OPTIMIZE_RETRY_LABEL,
    ROW_OPTIMIZE_ABORT_LABEL,
    ROW_OPTIMIZE_CLOSE_LABEL,
    ROW_OPTIMIZE_EMPTY_TEXT,
    ROW_OPTIMIZE_DONE_TEXT,
  ]) {
    assert.ok(typeof value === "string" && value.length > 0);
  }
});

const COLUMNS = [
  { id: "c-b", name: "B列", position: 1 },
  { id: "c-a", name: "A列", position: 0 },
  { id: "c-c", name: "C列", position: 2 },
];

test("initRowOptimizeQueue: 按 position 顺序建队列，跳过空格子", () => {
  const row = { cells: { "c-a": "内容A", "c-b": "", "c-c": "内容C" } };
  const state = initRowOptimizeQueue(row, COLUMNS);
  assert.deepStrictEqual(
    state.items.map((item) => item.columnId),
    ["c-a", "c-c"],
  );
  assert.strictEqual(state.items[0].originalMd, "内容A");
  assert.strictEqual(state.items[0].status, "waiting");
  assert.strictEqual(state.cursor, 0);
  assert.strictEqual(state.aborted, false);
});

test("initRowOptimizeQueue: 空白(仅空格)格子视为空，跳过", () => {
  const row = { cells: { "c-a": "   ", "c-b": "有内容", "c-c": "" } };
  const state = initRowOptimizeQueue(row, COLUMNS);
  assert.deepStrictEqual(state.items.map((item) => item.columnId), ["c-b"]);
});

test("initRowOptimizeQueue: 全部为空时返回空队列", () => {
  const row = { cells: {} };
  const state = initRowOptimizeQueue(row, COLUMNS);
  assert.deepStrictEqual(state.items, []);
  assert.strictEqual(isRowOptimizeQueueFinished(state), true);
});

test("currentRowOptimizeItem / isRowOptimizeQueueFinished / rowOptimizeProgress", () => {
  const row = { cells: { "c-a": "1", "c-b": "2", "c-c": "3" } };
  const state = initRowOptimizeQueue(row, COLUMNS);
  assert.strictEqual(currentRowOptimizeItem(state)?.columnId, "c-a");
  assert.strictEqual(isRowOptimizeQueueFinished(state), false);
  assert.deepStrictEqual(rowOptimizeProgress(state), { done: 0, total: 3 });

  const advanced = { ...state, cursor: 3 };
  assert.strictEqual(currentRowOptimizeItem(advanced), null);
  assert.strictEqual(isRowOptimizeQueueFinished(advanced), true);
  assert.deepStrictEqual(rowOptimizeProgress(advanced), { done: 3, total: 3 });
});

function queueOf(statuses) {
  return {
    items: statuses.map((status, index) => ({
      columnId: `c${index}`,
      columnName: `列${index}`,
      originalMd: `原文${index}`,
      status,
    })),
    cursor: 0,
    aborted: false,
  };
}

test("markCurrentInProgress: waiting -> in_progress", () => {
  const state = queueOf(["waiting", "waiting"]);
  const next = markCurrentInProgress(state);
  assert.strictEqual(next.items[0].status, "in_progress");
  assert.strictEqual(next.cursor, 0);
});

test("markCurrentInProgress: 非 waiting 态原样返回", () => {
  const state = queueOf(["ready"]);
  assert.strictEqual(markCurrentInProgress(state), state);
});

test("markCurrentInProgress: 已中止时原样返回(不再产生新请求)", () => {
  const state = { ...queueOf(["waiting"]), aborted: true };
  assert.strictEqual(markCurrentInProgress(state), state);
});

test("applySuggestion: in_progress -> ready(suggestionMd)", () => {
  const state = queueOf(["in_progress"]);
  const next = applySuggestion(state, "建议内容");
  assert.strictEqual(next.items[0].status, "ready");
  assert.strictEqual(next.items[0].suggestionMd, "建议内容");
});

test("applySuggestion: 非 in_progress 态原样返回", () => {
  const state = queueOf(["waiting"]);
  assert.strictEqual(applySuggestion(state, "x"), state);
});

test("applySuggestion: 已中止后到达的迟到结果被丢弃", () => {
  const state = { ...queueOf(["in_progress"]), aborted: true };
  assert.strictEqual(applySuggestion(state, "x"), state);
});

test("applyError: in_progress -> error(message)", () => {
  const state = queueOf(["in_progress"]);
  const next = applyError(state, "优化服务暂时不可用，请稍后再试");
  assert.strictEqual(next.items[0].status, "error");
  assert.strictEqual(next.items[0].errorMessage, "优化服务暂时不可用，请稍后再试");
});

test("applyError: 已中止后到达的迟到结果被丢弃", () => {
  const state = { ...queueOf(["in_progress"]), aborted: true };
  assert.strictEqual(applyError(state, "x"), state);
});

test("beginAcceptCurrent: ready -> in_progress(游标不动)", () => {
  const state = queueOf(["ready"]);
  const next = beginAcceptCurrent(state);
  assert.strictEqual(next.items[0].status, "in_progress");
  assert.strictEqual(next.cursor, 0);
});

test("beginAcceptCurrent: 非 ready 态(如 waiting/error)原样返回", () => {
  const waitingState = queueOf(["waiting"]);
  assert.strictEqual(beginAcceptCurrent(waitingState), waitingState);
  const errState = queueOf(["error"]);
  assert.strictEqual(beginAcceptCurrent(errState), errState);
});

test("completeAcceptCurrent: in_progress -> accepted 且游标推进", () => {
  const state = queueOf(["in_progress", "waiting"]);
  const next = completeAcceptCurrent(state);
  assert.strictEqual(next.items[0].status, "accepted");
  assert.strictEqual(next.cursor, 1);
});

test("completeAcceptCurrent: 非 in_progress 态原样返回", () => {
  const state = queueOf(["ready"]);
  assert.strictEqual(completeAcceptCurrent(state), state);
});

test("failAcceptCurrent: in_progress -> error(message)，游标不动", () => {
  const state = queueOf(["in_progress", "waiting"]);
  const next = failAcceptCurrent(state, "保存失败，请重试");
  assert.strictEqual(next.items[0].status, "error");
  assert.strictEqual(next.items[0].errorMessage, "保存失败，请重试");
  assert.strictEqual(next.cursor, 0);
});

test("failAcceptCurrent: 非 in_progress 态原样返回", () => {
  const state = queueOf(["ready"]);
  assert.strictEqual(failAcceptCurrent(state, "x"), state);
});

test("skipCurrent: 从 ready 跳过 -> skipped 且游标推进", () => {
  const state = queueOf(["ready", "waiting"]);
  const next = skipCurrent(state);
  assert.strictEqual(next.items[0].status, "skipped");
  assert.strictEqual(next.cursor, 1);
});

test("skipCurrent: 从 error 跳过 -> skipped 且游标推进（一次失败不永久卡住整行）", () => {
  const state = queueOf(["error", "waiting"]);
  const next = skipCurrent(state);
  assert.strictEqual(next.items[0].status, "skipped");
  assert.strictEqual(next.cursor, 1);
});

test("skipCurrent: 从 waiting/in_progress/accepted/skipped 跳过均无操作", () => {
  for (const status of ["waiting", "in_progress", "accepted", "skipped"]) {
    const state = queueOf([status]);
    assert.strictEqual(skipCurrent(state), state, `status=${status} 应无操作`);
  }
});

test("retryCurrent: error -> waiting，游标不动，清空错误信息", () => {
  const state = queueOf(["error", "waiting"]);
  state.items[0].errorMessage = "旧错误";
  const next = retryCurrent(state);
  assert.strictEqual(next.items[0].status, "waiting");
  assert.strictEqual(next.items[0].errorMessage, undefined);
  assert.strictEqual(next.cursor, 0);
});

test("retryCurrent: 非 error 态原样返回", () => {
  const state = queueOf(["ready"]);
  assert.strictEqual(retryCurrent(state), state);
});

test("abortQueue: 置 aborted=true，不改变已有格子状态", () => {
  const state = queueOf(["accepted", "ready"]);
  const next = abortQueue(state);
  assert.strictEqual(next.aborted, true);
  assert.deepStrictEqual(
    next.items.map((item) => item.status),
    ["accepted", "ready"],
  );
});

test("端到端：两格队列——第一格接受推进到第二格，第二格跳过后队列完成", () => {
  let state = initRowOptimizeQueue({ cells: { "c-a": "原文1", "c-b": "原文2" } }, [
    { id: "c-a", name: "A", position: 0 },
    { id: "c-b", name: "B", position: 1 },
  ]);
  state = markCurrentInProgress(state);
  state = applySuggestion(state, "建议1");
  assert.strictEqual(currentRowOptimizeItem(state).status, "ready");

  state = beginAcceptCurrent(state);
  state = completeAcceptCurrent(state);
  assert.strictEqual(state.items[0].status, "accepted");
  assert.strictEqual(currentRowOptimizeItem(state).columnId, "c-b");

  state = markCurrentInProgress(state);
  state = applyError(state, "优化服务暂时不可用，请稍后再试");
  assert.strictEqual(currentRowOptimizeItem(state).status, "error");

  state = skipCurrent(state);
  assert.strictEqual(state.items[1].status, "skipped");
  assert.strictEqual(isRowOptimizeQueueFinished(state), true);
});

// ===========================================================================
// 3. 追加导入预览展示映射
// ===========================================================================

test("mapAppendDuplicatesForDisplay: row_index 0-based 转 1-based displayIndex", () => {
  const out = mapAppendDuplicatesForDisplay([{ rowIndex: 0, title: "过冲问题" }], 5);
  assert.deepStrictEqual(out, [{ displayIndex: 1, title: "过冲问题", inPreviewWindow: true }]);
});

test("mapAppendDuplicatesForDisplay: rowIndex 落在预览窗口之外时 inPreviewWindow=false", () => {
  const out = mapAppendDuplicatesForDisplay([{ rowIndex: 6, title: "第七行" }], 5);
  assert.deepStrictEqual(out, [{ displayIndex: 7, title: "第七行", inPreviewWindow: false }]);
});

test("mapAppendDuplicatesForDisplay: 空数组返回空数组", () => {
  assert.deepStrictEqual(mapAppendDuplicatesForDisplay([], 5), []);
});

test("mapAppendDuplicatesForDisplay: 边界——rowIndex 恰等于 previewRowCount 视为窗口之外", () => {
  const out = mapAppendDuplicatesForDisplay([{ rowIndex: 5, title: "第六行" }], 5);
  assert.strictEqual(out[0].inPreviewWindow, false);
});

test("isAppendPreviewRowDuplicate: 命中 rowIndex 返回 true", () => {
  const dup = [{ rowIndex: 2, title: "x" }];
  assert.strictEqual(isAppendPreviewRowDuplicate(dup, 2), true);
  assert.strictEqual(isAppendPreviewRowDuplicate(dup, 0), false);
});

test("isAppendPreviewRowDuplicate: 空数组恒为 false", () => {
  assert.strictEqual(isAppendPreviewRowDuplicate([], 0), false);
});

test("formatUnmatchedColumnsMessage: 空数组返回 null", () => {
  assert.strictEqual(formatUnmatchedColumnsMessage([]), null);
});

test("formatUnmatchedColumnsMessage: 非空数组给出中文提示，含全部列名", () => {
  const message = formatUnmatchedColumnsMessage(["备注", "其他"]);
  assert.ok(typeof message === "string");
  assert.ok(message.includes("备注"));
  assert.ok(message.includes("其他"));
});

// ===========================================================================
// 4. 模板下载文件名
// ===========================================================================

test("templateDownloadFilename: 与后端 f\"{table['title']}-template.xlsx\" 同规则", () => {
  assert.strictEqual(templateDownloadFilename("时序违例修复"), "时序违例修复-template.xlsx");
});

test("templateDownloadFilename: 标题含中文/斜杠等字符原样拼接(转义留给下载 API 层，不在此处处理)", () => {
  assert.strictEqual(templateDownloadFilename("输入/输出对照表"), "输入/输出对照表-template.xlsx");
});

// ===========================================================================
// 5. 批量「一键规整」（整行/整表，knowhow-md-normalize Task 9）
// ===========================================================================
//
// 与上面第 2 节「优化整行」的关键差异：那边是逐格人工 接受/跳过（每格都是
// 一次独立的用户决定）；这里没有逐格决定——reformat_cell 对全部非空格子
// 自动跑完，人工唯一的决定点是跑完之后**一次性**的「确认保存」（任务硬
// 要求①）。因此状态机不需要 cursor/"当前格等待用户决定" 这类概念：每个
// item 只按自己的 (rowId, columnId) 定位与转移，同一套 init 函数天然支持
// "整行"（传一个 row）与"整表"（传多个 row）两种作用域。顶层 phase 字段
// 只用来区分五个 UI 阶段：
//   idle（展示将处理的格子数，等用户点「开始规整」——任务硬要求③）
//     -> running（逐格调用 reformat_cell；用户随时可「中止」）
//     -> reviewing（人工整体确认：全部候选已收集，changed=false 的格子
//        不会出现在待保存范围内——任务硬要求①）
//     -> saving（仅对 status==="changed" 的格子逐格调用保存；单格失败不
//        阻断其它格——任务硬要求④）
//     -> done。
// phase 是每个 run/save 阶段转移函数的"总闸"：一旦 phase 离开
// "running"（中止或自然跑完），markReformatItemRunning/
// applyReformatResult/applyReformatError 一律不再生效——迟到的结果被丢弃，
// 镜像上面第 2 节 applySuggestion/applyError 对 state.aborted 的丢弃语义
// （不复用同一个函数，是两个刻意分开、各自独立可测的小状态机）。saving
// 阶段同理由 phase==="saving" 总闸。

const BATCH_ROW_COLUMNS = [
  { id: "c-b", name: "B列", position: 1 },
  { id: "c-a", name: "A列", position: 0 },
  { id: "c-c", name: "C列", position: 2 },
];

// 构造任意 phase/items 组合的裸状态，供转移函数单测直接摆放前置状态——
// 镜像上面第 2 节 queueOf 的用法，只是 items 改成显式列表（每个 item 可能
// 属于不同 rowId，覆盖"整表"场景），不是"同一行 N 个状态各异的格子"。
function batchStateOf(phase, items, opts = {}) {
  return { scope: opts.scope ?? "row", phase, aborted: opts.aborted ?? false, items };
}

function makeItem(rowId, columnId, status, extra = {}) {
  return { rowId, columnId, columnName: `${columnId}-name`, originalMd: `${columnId}-original`, status, ...extra };
}

// --- 文案常量 ---------------------------------------------------------------

test("BATCH_REFORMAT_ROW/TABLE_BUTTON_LABEL: 与设计文档「一键规整整表 / 整行」措辞一致", () => {
  assert.strictEqual(BATCH_REFORMAT_ROW_BUTTON_LABEL, "一键规整整行");
  assert.strictEqual(BATCH_REFORMAT_TABLE_BUTTON_LABEL, "一键规整整表");
});

test("批量规整操作文案常量非空", () => {
  for (const value of [
    BATCH_REFORMAT_START_LABEL,
    BATCH_REFORMAT_ABORT_LABEL,
    BATCH_REFORMAT_CONFIRM_LABEL,
    BATCH_REFORMAT_DISCARD_LABEL,
    BATCH_REFORMAT_CLOSE_LABEL,
    BATCH_REFORMAT_EMPTY_TEXT_ROW,
    BATCH_REFORMAT_EMPTY_TEXT_TABLE,
    BATCH_REFORMAT_NO_CHANGES_TEXT,
    BATCH_REFORMAT_DONE_TEXT,
  ]) {
    assert.ok(typeof value === "string" && value.length > 0);
  }
});

test("BATCH_REFORMAT_STATUS_LABELS: 十态各有非空中文文案，键集合恰好是十态", () => {
  const expectedKeys = [
    "pending", "running", "changed", "unchanged", "reformat_error", "aborted", "saving", "saved", "save_error",
    // F2：保存前比对发现该格已被他人改动 → 独立终态「已跳过」，不当失败也不当成功。
    "stale_skipped",
  ];
  assert.deepStrictEqual(Object.keys(BATCH_REFORMAT_STATUS_LABELS).sort(), expectedKeys.sort());
  for (const key of expectedKeys) {
    assert.ok(
      typeof BATCH_REFORMAT_STATUS_LABELS[key] === "string" && BATCH_REFORMAT_STATUS_LABELS[key].length > 0,
      `status=${key} 缺少文案`,
    );
  }
  // P2-g：中止在飞项的终态标签必须是「已中止」，不是「规整失败」——中止不是失败。
  assert.strictEqual(BATCH_REFORMAT_STATUS_LABELS.aborted, "已中止");
});

test("batchReformatScaleText: 含处理格子数、AI 与「确认」字样（任务硬要求③：开始前先说明规模与代价）", () => {
  const text = batchReformatScaleText(7);
  assert.ok(text.includes("7"));
  assert.ok(text.includes("AI"));
  assert.ok(text.includes("确认"));
});

test("batchReformatScaleText: 格子数为 0 时仍生成合法文案（不崩溃）", () => {
  assert.ok(batchReformatScaleText(0).includes("0"));
});

// --- initReformatBatch -------------------------------------------------------

test("initReformatBatch: row 作用域——按列 position 顺序建条目，跳过空格子，初始 phase=idle", () => {
  const row = { id: "r1", position: 0, cells: { "c-a": "内容A", "c-b": "", "c-c": "内容C" } };
  const state = initReformatBatch("row", [row], BATCH_ROW_COLUMNS);
  assert.strictEqual(state.scope, "row");
  assert.strictEqual(state.phase, "idle");
  assert.strictEqual(state.aborted, false);
  assert.deepStrictEqual(state.items.map((item) => item.columnId), ["c-a", "c-c"]);
  assert.strictEqual(state.items[0].rowId, "r1");
  assert.strictEqual(state.items[0].originalMd, "内容A");
  assert.strictEqual(state.items[0].status, "pending");
  assert.strictEqual(state.items[0].candidateMd, undefined);
});

test("initReformatBatch: 空白(仅空格)格子视为空，跳过", () => {
  const row = { id: "r1", position: 0, cells: { "c-a": "   ", "c-b": "有内容", "c-c": "" } };
  const state = initReformatBatch("row", [row], BATCH_ROW_COLUMNS);
  assert.deepStrictEqual(state.items.map((item) => item.columnId), ["c-b"]);
});

test("initReformatBatch: 全部为空时返回空 items（不是报错）", () => {
  const state = initReformatBatch("row", [{ id: "r1", position: 0, cells: {} }], BATCH_ROW_COLUMNS);
  assert.deepStrictEqual(state.items, []);
});

test("initReformatBatch: table 作用域——按行 position 顺序展开多行，行内再按列 position 顺序", () => {
  const rows = [
    { id: "r2", position: 1, cells: { "c-a": "R2A", "c-c": "R2C" } },
    { id: "r1", position: 0, cells: { "c-a": "R1A", "c-b": "R1B" } },
  ];
  const state = initReformatBatch("table", rows, BATCH_ROW_COLUMNS);
  assert.strictEqual(state.scope, "table");
  assert.deepStrictEqual(
    state.items.map((item) => `${item.rowId}:${item.columnId}`),
    ["r1:c-a", "r1:c-b", "r2:c-a", "r2:c-c"],
  );
});

test("initReformatBatch: table 作用域——某一行全空则该行不产生任何条目（不是整批清零）", () => {
  const rows = [
    { id: "r1", position: 0, cells: { "c-a": "内容" } },
    { id: "r2", position: 1, cells: {} },
  ];
  const state = initReformatBatch("table", rows, BATCH_ROW_COLUMNS);
  assert.deepStrictEqual(state.items.map((item) => item.rowId), ["r1"]);
});

// --- F2：批量规整必须排除行标题（anchor）列 --------------------------------------
// 缺陷：buildReformatBatchItems 把每个非空列都纳入，包括 anchor 列——整表/整行批量
// 因此会重排分组键（`A. Foo` -> `**A. Foo**`），把概念组拆开或合并。契约：只有显式
// 的 PER-CELL 动作可以碰 anchor 格（它的扇出保持整组一致、且有人工复核那一格）。
// 修复：initReformatBatch 传入 anchorColumnId（= handleCellSave/planReformatSaves 分组
// 用的同一个键），buildReformatBatchItems 跳过 column.id === anchorColumnId 的列。用
// anchorColumnId 而非 role：扇出重排的正是 anchorColumnId 那一列，排除它才与扇出严格对齐。

const BATCH_ANCHORED_COLUMNS = [
  { id: "c-anchor", name: "概念", position: 0 },
  { id: "c-1", name: "内容1", position: 1 },
  { id: "c-2", name: "内容2", position: 2 },
];

test("initReformatBatch: row 作用域——排除 anchor 列，只纳入内容列（anchor 非空也不纳入）", () => {
  const row = { id: "r1", position: 0, cells: { "c-anchor": "A. Foo", "c-1": "内容一", "c-2": "内容二" } };
  const state = initReformatBatch("row", [row], BATCH_ANCHORED_COLUMNS, "c-anchor");
  assert.deepStrictEqual(state.items.map((item) => item.columnId), ["c-1", "c-2"]);
  // 规模文案的计数 = state.items.length，排除 anchor 后为 2，与实际处理条目数一致。
  assert.strictEqual(state.items.length, 2);
  assert.ok(batchReformatScaleText(state.items.length).includes("2"));
});

test("initReformatBatch: table 作用域——每一行都排除 anchor 列", () => {
  const rows = [
    { id: "r1", position: 0, cells: { "c-anchor": "A", "c-1": "R1一", "c-2": "R1二" } },
    { id: "r2", position: 1, cells: { "c-anchor": "B", "c-1": "R2一" } },
  ];
  const state = initReformatBatch("table", rows, BATCH_ANCHORED_COLUMNS, "c-anchor");
  assert.deepStrictEqual(
    state.items.map((item) => `${item.rowId}:${item.columnId}`),
    ["r1:c-1", "r1:c-2", "r2:c-1"],
  );
});

test("initReformatBatch: 记录型表（anchorColumnId=null）——不排除任何列（不受影响）", () => {
  const row = { id: "r1", position: 0, cells: { "c-anchor": "值", "c-1": "内容一", "c-2": "内容二" } };
  const state = initReformatBatch("row", [row], BATCH_ANCHORED_COLUMNS, null);
  assert.deepStrictEqual(state.items.map((item) => item.columnId), ["c-anchor", "c-1", "c-2"]);
});

test("initReformatBatch: 省略 anchorColumnId（默认 null）——向后兼容，不排除任何列", () => {
  const row = { id: "r1", position: 0, cells: { "c-anchor": "值", "c-1": "内容一" } };
  const state = initReformatBatch("row", [row], BATCH_ANCHORED_COLUMNS);
  assert.deepStrictEqual(state.items.map((item) => item.columnId), ["c-anchor", "c-1"]);
});

// --- reformatBatchSummary（规模/汇总计数——驱动"开始前展示规模"与"确认前
// 展示改动数"两处 UI，任务硬要求①③共用同一个计数源）--------------------------

test("reformatBatchSummary: 初始态全部 pending，total 即将处理的格子数", () => {
  const state = initReformatBatch(
    "row",
    [{ id: "r1", position: 0, cells: { "c-a": "1", "c-b": "2" } }],
    BATCH_ROW_COLUMNS,
  );
  assert.deepStrictEqual(reformatBatchSummary(state), {
    total: 2, pending: 2, running: 0, changed: 0, unchanged: 0, reformat_error: 0, aborted: 0, saving: 0, saved: 0, save_error: 0, stale_skipped: 0,
  });
});

test("reformatBatchSummary: 按各 item.status 精确计数", () => {
  const state = batchStateOf("reviewing", [
    makeItem("r1", "c-a", "changed"),
    makeItem("r1", "c-b", "unchanged"),
    makeItem("r1", "c-c", "reformat_error"),
  ]);
  assert.deepStrictEqual(reformatBatchSummary(state), {
    total: 3, pending: 0, running: 0, changed: 1, unchanged: 1, reformat_error: 1, aborted: 0, saving: 0, saved: 0, save_error: 0, stale_skipped: 0,
  });
});

// --- reformatReviewingSummaryText（F4 review：reviewing 阶段收尾摘要文案）---------
// 缺陷是「明细 vs 无需改动」的判定漏了 reformat_error——整批全失败时 changed/aborted/
// stale_skipped 皆 0，会把一整批失败粉饰成「无需改动」。修复：任何非平凡终态都走明细。

test("reformatReviewingSummaryText: 全部规整失败 -> 明细文案含失败计数（不粉饰为无需改动）", () => {
  // THE F4 bug: 3 个全 reformat_error，旧逻辑落到 NO_CHANGES；修复后走明细、点名失败数。
  const summary = reformatBatchSummary(
    batchStateOf("reviewing", [
      makeItem("r1", "c-a", "reformat_error"),
      makeItem("r1", "c-b", "reformat_error"),
      makeItem("r1", "c-c", "reformat_error"),
    ]),
  );
  const text = reformatReviewingSummaryText(summary);
  assert.notStrictEqual(text, BATCH_REFORMAT_NO_CHANGES_TEXT);
  assert.ok(text.includes(`3 个${BATCH_REFORMAT_STATUS_LABELS.reformat_error}`), text);
  assert.ok(text.includes("共处理 3 个格子"), text);
});

test("reformatReviewingSummaryText: 清一色 unchanged -> 无需改动", () => {
  const summary = reformatBatchSummary(
    batchStateOf("reviewing", [makeItem("r1", "c-a", "unchanged"), makeItem("r1", "c-b", "unchanged")]),
  );
  assert.strictEqual(reformatReviewingSummaryText(summary), BATCH_REFORMAT_NO_CHANGES_TEXT);
});

test("reformatReviewingSummaryText: 空批次（total=0）-> 无需改动", () => {
  assert.strictEqual(reformatReviewingSummaryText(reformatBatchSummary(batchStateOf("reviewing", []))), BATCH_REFORMAT_NO_CHANGES_TEXT);
});

test("reformatReviewingSummaryText: 混合（改动+无需改动+失败）-> 明细逐项计数", () => {
  const summary = reformatBatchSummary(
    batchStateOf("reviewing", [
      makeItem("r1", "c-a", "changed"),
      makeItem("r1", "c-b", "unchanged"),
      makeItem("r1", "c-c", "reformat_error"),
    ]),
  );
  const text = reformatReviewingSummaryText(summary);
  assert.ok(text.includes("共处理 3 个格子"), text);
  assert.ok(text.includes(`1 个${BATCH_REFORMAT_STATUS_LABELS.changed}`), text);
  assert.ok(text.includes(`1 个${BATCH_REFORMAT_STATUS_LABELS.unchanged}`), text);
  assert.ok(text.includes(`1 个${BATCH_REFORMAT_STATUS_LABELS.reformat_error}`), text);
  assert.ok(text.endsWith("。"), text);
});

test("reformatReviewingSummaryText: 只有改动（无失败/中止/跳过）-> 明细、不追加多余枚举", () => {
  const summary = reformatBatchSummary(
    batchStateOf("reviewing", [makeItem("r1", "c-a", "changed"), makeItem("r1", "c-b", "unchanged")]),
  );
  const text = reformatReviewingSummaryText(summary);
  assert.strictEqual(
    text,
    `共处理 2 个格子：1 个${BATCH_REFORMAT_STATUS_LABELS.changed}、1 个${BATCH_REFORMAT_STATUS_LABELS.unchanged}。`,
  );
});

// --- F2（abort）：早中止把未开始的格子留在 pending——「共处理 N」若用 total 会把
//     这些从没跑过的格子也算成已处理。修复：已处理 = total - pending，另如实点出
//     未开始数（未开始（已中止））。pending===0（正常跑完）时文案逐字不变。--------

test("BATCH_REFORMAT_ABORTED_PENDING_LABEL: 友好中文点出「未开始」与中止缘由，不暴露 pending 枚举", () => {
  assert.strictEqual(BATCH_REFORMAT_ABORTED_PENDING_LABEL, "未开始（已中止）");
});

test("reformatReviewingSummaryText: 早中止留 pending -> 只算已处理数(total-pending) + 如实点出未开始数", () => {
  // THE F2 bug：5 格中止在第 2 格后——1 changed + 1 aborted(在飞) + 3 pending(从没起跑)。
  // 旧逻辑「共处理 ${total}」会谎报「共处理 5」；修复后「共处理 2」并点出「3 个未开始」。
  const summary = reformatBatchSummary(
    batchStateOf(
      "reviewing",
      [
        makeItem("r1", "c-a", "changed"),
        makeItem("r1", "c-b", "aborted"),
        makeItem("r1", "c-c", "pending"),
        makeItem("r1", "c-d", "pending"),
        makeItem("r1", "c-e", "pending"),
      ],
      { aborted: true },
    ),
  );
  const text = reformatReviewingSummaryText(summary);
  assert.ok(text.includes("共处理 2 个格子"), text); // total(5) - pending(3) = 2
  assert.ok(!text.includes("共处理 5"), text); // 绝不把未开始的格子谎报成已处理
  assert.ok(text.includes(`3 个${BATCH_REFORMAT_ABORTED_PENDING_LABEL}`), text);
  assert.ok(text.includes(`1 个${BATCH_REFORMAT_STATUS_LABELS.changed}`), text);
  assert.ok(text.includes(`1 个${BATCH_REFORMAT_STATUS_LABELS.aborted}`), text);
  assert.ok(text.endsWith("。"), text);
});

test("reformatReviewingSummaryText: 中止只剩 pending（0 在飞）-> 仍如实报未开始、不谎报无需改动", () => {
  // 明细各态皆 0、只有 pending——旧 hasReport 会落到「无需改动」粉饰中止；修复后 pending>0
  // 也进明细，共处理 0、点出全部未开始。
  const summary = reformatBatchSummary(
    batchStateOf("reviewing", [makeItem("r1", "c-a", "pending"), makeItem("r1", "c-b", "pending")], { aborted: true }),
  );
  const text = reformatReviewingSummaryText(summary);
  assert.notStrictEqual(text, BATCH_REFORMAT_NO_CHANGES_TEXT);
  assert.ok(text.includes("共处理 0 个格子"), text);
  assert.ok(text.includes(`2 个${BATCH_REFORMAT_ABORTED_PENDING_LABEL}`), text);
});

test("reformatReviewingSummaryText: pending===0（正常跑完）文案逐字不变（含明细各态）", () => {
  // 完成态（无 pending）必须与旧行为字节一致——「共处理 ${total}」、无「未开始」子句。
  const summary = reformatBatchSummary(
    batchStateOf("reviewing", [
      makeItem("r1", "c-a", "changed"),
      makeItem("r1", "c-b", "unchanged"),
      makeItem("r1", "c-c", "reformat_error"),
    ]),
  );
  const text = reformatReviewingSummaryText(summary);
  assert.strictEqual(
    text,
    `共处理 3 个格子：1 个${BATCH_REFORMAT_STATUS_LABELS.changed}、1 个${BATCH_REFORMAT_STATUS_LABELS.unchanged}、1 个${BATCH_REFORMAT_STATUS_LABELS.reformat_error}。`,
  );
  assert.ok(!text.includes(BATCH_REFORMAT_ABORTED_PENDING_LABEL), text);
});

// --- run 阶段：beginReformatBatchRun / markReformatItemRunning /
// applyReformatResult / applyReformatError / abortReformatBatchRun /
// finishReformatBatchRun ------------------------------------------------------

test("beginReformatBatchRun: idle -> running", () => {
  const state = batchStateOf("idle", [makeItem("r1", "c-a", "pending")]);
  assert.strictEqual(beginReformatBatchRun(state).phase, "running");
});

test("beginReformatBatchRun: 非 idle 态原样返回", () => {
  const state = batchStateOf("running", [makeItem("r1", "c-a", "pending")]);
  assert.strictEqual(beginReformatBatchRun(state), state);
});

test("markReformatItemRunning: pending -> running（按 rowId+columnId 定位，不影响同批其它项）", () => {
  const state = batchStateOf("running", [makeItem("r1", "c-a", "pending"), makeItem("r1", "c-b", "pending")]);
  const next = markReformatItemRunning(state, "r1", "c-b");
  assert.strictEqual(next.items[0].status, "pending");
  assert.strictEqual(next.items[1].status, "running");
});

test("markReformatItemRunning: 非 pending 态原样返回", () => {
  const state = batchStateOf("running", [makeItem("r1", "c-a", "running")]);
  assert.strictEqual(markReformatItemRunning(state, "r1", "c-a"), state);
});

test("markReformatItemRunning: 未知 (rowId,columnId) 原样返回", () => {
  const state = batchStateOf("running", [makeItem("r1", "c-a", "pending")]);
  assert.strictEqual(markReformatItemRunning(state, "r9", "c-9"), state);
});

test("markReformatItemRunning: 非 running 阶段原样返回（即使该项是 pending）——phase 是总闸", () => {
  const state = batchStateOf("reviewing", [makeItem("r1", "c-a", "pending")]);
  assert.strictEqual(markReformatItemRunning(state, "r1", "c-a"), state);
});

test("applyReformatResult: running + changed=true -> changed(candidateMd, source)", () => {
  const state = batchStateOf("running", [makeItem("r1", "c-a", "running")]);
  const next = applyReformatResult(state, "r1", "c-a", { candidateMd: "- a\n- b", source: "llm", changed: true });
  assert.strictEqual(next.items[0].status, "changed");
  assert.strictEqual(next.items[0].candidateMd, "- a\n- b");
  assert.strictEqual(next.items[0].source, "llm");
});

test("applyReformatResult: running + changed=false -> unchanged(source)，不带 candidateMd（任务硬要求①：这类格子不会被保存）", () => {
  const state = batchStateOf("running", [makeItem("r1", "c-a", "running")]);
  const next = applyReformatResult(state, "r1", "c-a", { candidateMd: "原内容", source: "rule/no-llm", changed: false });
  assert.strictEqual(next.items[0].status, "unchanged");
  assert.strictEqual(next.items[0].source, "rule/no-llm");
  assert.strictEqual(next.items[0].candidateMd, undefined);
});

test("applyReformatResult: source 原样透传，不做映射/校验（渲染层再用 reformatSourceLabel 映射——任务硬要求②的分工）", () => {
  const state = batchStateOf("running", [makeItem("r1", "c-a", "running")]);
  const next = applyReformatResult(state, "r1", "c-a", {
    candidateMd: "x", source: "some-unexpected-enum-value", changed: true,
  });
  assert.strictEqual(next.items[0].source, "some-unexpected-enum-value");
});

test("applyReformatResult: 非 running 态的项（如已是 changed/pending）原样返回——重复/迟到结果不覆盖", () => {
  const state = batchStateOf("running", [makeItem("r1", "c-a", "changed", { candidateMd: "已定", source: "llm" })]);
  const next = applyReformatResult(state, "r1", "c-a", { candidateMd: "新的", source: "llm", changed: true });
  assert.strictEqual(next, state);
});

test("applyReformatResult: 未知 (rowId,columnId) 原样返回", () => {
  const state = batchStateOf("running", [makeItem("r1", "c-a", "running")]);
  assert.strictEqual(applyReformatResult(state, "r9", "c-9", { candidateMd: "x", source: "llm", changed: true }), state);
});

test("applyReformatResult: 已跑完/已中止（phase 不是 running）时到达的结果被丢弃——镜像 applySuggestion 对 aborted 的丢弃语义", () => {
  const reviewing = batchStateOf("reviewing", [makeItem("r1", "c-a", "running")]);
  const next = applyReformatResult(reviewing, "r1", "c-a", { candidateMd: "x", source: "llm", changed: true });
  assert.strictEqual(next, reviewing);
});

test("applyReformatError: running -> reformat_error(message)", () => {
  const state = batchStateOf("running", [makeItem("r1", "c-a", "running")]);
  const next = applyReformatError(state, "r1", "c-a", "规整失败，请重试");
  assert.strictEqual(next.items[0].status, "reformat_error");
  assert.strictEqual(next.items[0].errorMessage, "规整失败，请重试");
});

test("applyReformatError: 非 running 态的项原样返回", () => {
  const state = batchStateOf("running", [makeItem("r1", "c-a", "pending")]);
  assert.strictEqual(applyReformatError(state, "r1", "c-a", "x"), state);
});

test("applyReformatError: 已跑完/已中止（phase 不是 running）时到达的结果被丢弃", () => {
  const reviewing = batchStateOf("reviewing", [makeItem("r1", "c-a", "running")]);
  const next = applyReformatError(reviewing, "r1", "c-a", "x");
  assert.strictEqual(next, reviewing);
});

test("abortReformatBatchRun: running -> reviewing 且 aborted=true，不改变各项已有状态", () => {
  const state = batchStateOf("running", [makeItem("r1", "c-a", "changed"), makeItem("r1", "c-b", "pending")]);
  const next = abortReformatBatchRun(state);
  assert.strictEqual(next.phase, "reviewing");
  assert.strictEqual(next.aborted, true);
  assert.deepStrictEqual(next.items.map((i) => i.status), ["changed", "pending"]);
});

test("abortReformatBatchRun: 非 running 态原样返回", () => {
  const state = batchStateOf("idle", [makeItem("r1", "c-a", "pending")]);
  assert.strictEqual(abortReformatBatchRun(state), state);
});

// --- P2-g：中止时把仍在飞的 running 格子就地结算为终态 aborted ----------------
//
// 缺陷：中止只把 phase 翻到 reviewing，当前 running 的那一格保持 running；它的
// 迟到响应随后被 phase 总闸丢弃（这是对的），于是没有任何转移能把它带离
// running——review 面板里那一格永远显示「规整中」。修复：abort 的同一次转移里
// 就把 running -> aborted。

test("abortReformatBatchRun: 在飞的 running 格子就地结算为终态 aborted（不再永远停在「规整中」）", () => {
  const state = batchStateOf("running", [
    makeItem("r1", "c-a", "changed"),
    makeItem("r1", "c-b", "running"), // 中止那一刻正在途中
    makeItem("r1", "c-c", "pending"),
  ]);
  const next = abortReformatBatchRun(state);
  assert.strictEqual(next.phase, "reviewing");
  assert.strictEqual(next.aborted, true);
  // 已完成的照旧、在飞的结算为 aborted、未处理的仍停在 pending。
  assert.deepStrictEqual(next.items.map((i) => i.status), ["changed", "aborted", "pending"]);
});

test("abortReformatBatchRun: 结算 aborted 后 summary 各态计数之和仍等于 total（计数加总）", () => {
  const state = batchStateOf("running", [
    makeItem("r1", "c-a", "changed"),
    makeItem("r1", "c-b", "running"),
    makeItem("r1", "c-c", "pending"),
  ]);
  const summary = reformatBatchSummary(abortReformatBatchRun(state));
  assert.strictEqual(summary.aborted, 1);
  assert.strictEqual(summary.running, 0); // 不再有任何格子卡在 running
  const {
    total, pending, running, changed, unchanged, reformat_error, aborted, saving, saved, save_error,
  } = summary;
  assert.strictEqual(
    pending + running + changed + unchanged + reformat_error + aborted + saving + saved + save_error,
    total,
  );
  // aborted 不计入 reformat_error——中止不是失败（任务硬要求：别当失败展示）。
  assert.strictEqual(reformat_error, 0);
});

test("abortReformatBatchRun: 中止后到达的迟到 applyReformatResult / applyReformatError 对已 aborted 的格子是 no-op（迟到响应仍被丢弃）", () => {
  const running = batchStateOf("running", [makeItem("r1", "c-a", "running")]);
  const aborted = abortReformatBatchRun(running);
  assert.strictEqual(aborted.items[0].status, "aborted");
  // 那条在飞请求随后才落地——phase 已是 reviewing（总闸），且该项已不是 running
  // （第二道门），两重防线都拦下，格子稳定停在 aborted。
  const lateResult = applyReformatResult(aborted, "r1", "c-a", { candidateMd: "迟到候选", source: "llm", changed: true });
  assert.strictEqual(lateResult, aborted);
  const lateError = applyReformatError(aborted, "r1", "c-a", "迟到错误");
  assert.strictEqual(lateError, aborted);
});

test("端到端（P2-g）：中止在飞项 → 已中止终态；确认保存只写 changed，从不写 aborted", () => {
  let state = initReformatBatch(
    "row",
    [{ id: "r1", position: 0, cells: { "c-a": "1", "c-b": "2", "c-c": "3" } }],
    BATCH_ROW_COLUMNS,
  );
  state = beginReformatBatchRun(state);
  // 第一格拿到 changed 结果，第二格刚发起请求（running）时用户点「中止」。
  state = markReformatItemRunning(state, "r1", "c-a");
  state = applyReformatResult(state, "r1", "c-a", { candidateMd: "改写1", source: "llm", changed: true });
  state = markReformatItemRunning(state, "r1", "c-b");
  state = abortReformatBatchRun(state);

  assert.strictEqual(state.phase, "reviewing");
  const cA = state.items.find((i) => i.columnId === "c-a");
  const cB = state.items.find((i) => i.columnId === "c-b");
  const cC = state.items.find((i) => i.columnId === "c-c");
  assert.strictEqual(cA.status, "changed");
  assert.strictEqual(cB.status, "aborted"); // 就地结算，不再是 running
  assert.strictEqual(cC.status, "pending"); // 从未处理
  assert.strictEqual(BATCH_REFORMAT_STATUS_LABELS[cB.status], "已中止");

  // 确认保存：只有 changed 会进入 saving——aborted/pending 都不带候选、进不了 save 阶段。
  state = beginReformatBatchSave(state);
  assert.strictEqual(state.phase, "saving");
  // aborted 项不是 changed，markReformatItemSaving 对它是 no-op。
  const noop = markReformatItemSaving(state, "r1", "c-b");
  assert.strictEqual(noop.items.find((i) => i.columnId === "c-b").status, "aborted");
  state = markReformatItemSaving(state, "r1", "c-a");
  state = applyReformatSaveSuccess(state, "r1", "c-a");
  state = finishReformatBatchSave(state);
  assert.strictEqual(state.phase, "done");
  assert.strictEqual(state.items.find((i) => i.columnId === "c-a").status, "saved");
  assert.strictEqual(state.items.find((i) => i.columnId === "c-b").status, "aborted"); // 从未被保存
  assert.strictEqual(state.items.find((i) => i.columnId === "c-c").status, "pending");
});

test("finishReformatBatchRun: running -> reviewing（自然跑完，未中止）", () => {
  const state = batchStateOf("running", [makeItem("r1", "c-a", "changed")]);
  const next = finishReformatBatchRun(state);
  assert.strictEqual(next.phase, "reviewing");
  assert.strictEqual(next.aborted, false);
});

test("finishReformatBatchRun: 非 running 态原样返回", () => {
  const state = batchStateOf("idle", []);
  assert.strictEqual(finishReformatBatchRun(state), state);
});

// --- 确认闸门：beginReformatBatchSave（任务硬要求①"人工整体确认"的唯一入口）---

test("beginReformatBatchSave: reviewing 且存在 changed 项 -> saving", () => {
  const state = batchStateOf("reviewing", [makeItem("r1", "c-a", "changed"), makeItem("r1", "c-b", "unchanged")]);
  assert.strictEqual(beginReformatBatchSave(state).phase, "saving");
});

test("beginReformatBatchSave: reviewing 但没有任何 changed 项（全部 unchanged/reformat_error）-> done（无需保存，直接收尾）", () => {
  const state = batchStateOf("reviewing", [makeItem("r1", "c-a", "unchanged"), makeItem("r1", "c-b", "reformat_error")]);
  assert.strictEqual(beginReformatBatchSave(state).phase, "done");
});

test("beginReformatBatchSave: 非 reviewing 态原样返回", () => {
  const state = batchStateOf("running", [makeItem("r1", "c-a", "changed")]);
  assert.strictEqual(beginReformatBatchSave(state), state);
});

// --- save 阶段：markReformatItemSaving / applyReformatSaveSuccess /
// applyReformatSaveError / finishReformatBatchSave ---------------------------

test("markReformatItemSaving: changed -> saving", () => {
  const state = batchStateOf("saving", [makeItem("r1", "c-a", "changed"), makeItem("r1", "c-b", "changed")]);
  const next = markReformatItemSaving(state, "r1", "c-b");
  assert.strictEqual(next.items[0].status, "changed");
  assert.strictEqual(next.items[1].status, "saving");
});

test("markReformatItemSaving: 非 changed 态（如 unchanged）原样返回——changed=false 的格子永远不会被保存", () => {
  const state = batchStateOf("saving", [makeItem("r1", "c-a", "unchanged")]);
  assert.strictEqual(markReformatItemSaving(state, "r1", "c-a"), state);
});

test("markReformatItemSaving: 非 saving 阶段原样返回（即使该项是 changed）——phase 是总闸", () => {
  const state = batchStateOf("reviewing", [makeItem("r1", "c-a", "changed")]);
  assert.strictEqual(markReformatItemSaving(state, "r1", "c-a"), state);
});

test("applyReformatSaveSuccess: saving -> saved", () => {
  const state = batchStateOf("saving", [makeItem("r1", "c-a", "saving")]);
  assert.strictEqual(applyReformatSaveSuccess(state, "r1", "c-a").items[0].status, "saved");
});

test("applyReformatSaveSuccess: 非 saving 态的项原样返回", () => {
  const state = batchStateOf("saving", [makeItem("r1", "c-a", "changed")]);
  assert.strictEqual(applyReformatSaveSuccess(state, "r1", "c-a"), state);
});

test("applyReformatSaveError: saving -> save_error(message)", () => {
  const state = batchStateOf("saving", [makeItem("r1", "c-a", "saving")]);
  const next = applyReformatSaveError(state, "r1", "c-a", "保存失败，请重试");
  assert.strictEqual(next.items[0].status, "save_error");
  assert.strictEqual(next.items[0].errorMessage, "保存失败，请重试");
});

test("applyReformatSaveError: 非 saving 态的项原样返回", () => {
  const state = batchStateOf("saving", [makeItem("r1", "c-a", "changed")]);
  assert.strictEqual(applyReformatSaveError(state, "r1", "c-a", "x"), state);
});

test("finishReformatBatchSave: saving -> done", () => {
  const state = batchStateOf("saving", [makeItem("r1", "c-a", "saved")]);
  assert.strictEqual(finishReformatBatchSave(state).phase, "done");
});

test("finishReformatBatchSave: 非 saving 态原样返回", () => {
  const state = batchStateOf("reviewing", [makeItem("r1", "c-a", "changed")]);
  assert.strictEqual(finishReformatBatchSave(state), state);
});

// --- 端到端 -------------------------------------------------------------------

test("端到端（行作用域）：一格有改动一格无需改动——确认保存后只落地有改动的那一格", () => {
  let state = initReformatBatch(
    "row",
    [{ id: "r1", position: 0, cells: { "c-a": "原文1", "c-b": "原文2" } }],
    [{ id: "c-a", name: "A", position: 0 }, { id: "c-b", name: "B", position: 1 }],
  );
  state = beginReformatBatchRun(state);
  assert.strictEqual(state.phase, "running");

  state = markReformatItemRunning(state, "r1", "c-a");
  state = applyReformatResult(state, "r1", "c-a", { candidateMd: "- 项目1", source: "llm", changed: true });
  state = markReformatItemRunning(state, "r1", "c-b");
  state = applyReformatResult(state, "r1", "c-b", { candidateMd: "原文2", source: "rule/no-llm", changed: false });
  state = finishReformatBatchRun(state);
  assert.strictEqual(state.phase, "reviewing");
  assert.deepStrictEqual(reformatBatchSummary(state), {
    total: 2, pending: 0, running: 0, changed: 1, unchanged: 1, reformat_error: 0, aborted: 0, saving: 0, saved: 0, save_error: 0, stale_skipped: 0,
  });

  state = beginReformatBatchSave(state); // 人工整体确认——唯一的写入闸门
  assert.strictEqual(state.phase, "saving");
  state = markReformatItemSaving(state, "r1", "c-a");
  state = applyReformatSaveSuccess(state, "r1", "c-a");
  state = finishReformatBatchSave(state);
  assert.strictEqual(state.phase, "done");
  assert.strictEqual(state.items[0].status, "saved");
  assert.strictEqual(state.items[1].status, "unchanged"); // changed=false 的格子全程未进入 saving
});

test("端到端（表作用域）：多行独立处理，一格规整失败不阻断其余格子（任务硬要求④：韧性）", () => {
  const rows = [
    { id: "r1", position: 0, cells: { "c-a": "R1A" } },
    { id: "r2", position: 1, cells: { "c-a": "R2A" } },
  ];
  const columns = [{ id: "c-a", name: "A", position: 0 }];
  let state = initReformatBatch("table", rows, columns);
  state = beginReformatBatchRun(state);

  state = markReformatItemRunning(state, "r1", "c-a");
  state = applyReformatError(state, "r1", "c-a", "规整失败，请重试"); // 第一行失败
  state = markReformatItemRunning(state, "r2", "c-a");
  state = applyReformatResult(state, "r2", "c-a", { candidateMd: "- R2A", source: "llm", changed: true }); // 第二行仍正常处理
  state = finishReformatBatchRun(state);

  assert.strictEqual(state.items.find((i) => i.rowId === "r1").status, "reformat_error");
  assert.strictEqual(state.items.find((i) => i.rowId === "r2").status, "changed");
  assert.deepStrictEqual(reformatBatchSummary(state), {
    total: 2, pending: 0, running: 0, changed: 1, unchanged: 0, reformat_error: 1, aborted: 0, saving: 0, saved: 0, save_error: 0, stale_skipped: 0,
  });

  // 确认保存：reformat_error 的格子没有候选内容，从未进入 saving。
  state = beginReformatBatchSave(state);
  state = markReformatItemSaving(state, "r2", "c-a");
  state = applyReformatSaveSuccess(state, "r2", "c-a");
  state = finishReformatBatchSave(state);
  assert.strictEqual(state.items.find((i) => i.rowId === "r1").status, "reformat_error");
  assert.strictEqual(state.items.find((i) => i.rowId === "r2").status, "saved");
});

test("端到端：保存阶段一格失败不阻断其余格子保存（任务硬要求④），失败计数可从 summary 读出", () => {
  let state = batchStateOf("reviewing", [
    makeItem("r1", "c-a", "changed", { candidateMd: "候选A" }),
    makeItem("r1", "c-b", "changed", { candidateMd: "候选B" }),
  ]);
  state = beginReformatBatchSave(state);
  state = markReformatItemSaving(state, "r1", "c-a");
  state = applyReformatSaveError(state, "r1", "c-a", "保存失败，请重试");
  state = markReformatItemSaving(state, "r1", "c-b");
  state = applyReformatSaveSuccess(state, "r1", "c-b");
  state = finishReformatBatchSave(state);

  assert.strictEqual(state.phase, "done");
  assert.strictEqual(state.items[0].status, "save_error");
  assert.strictEqual(state.items[1].status, "saved");
  assert.deepStrictEqual(reformatBatchSummary(state), {
    total: 2, pending: 0, running: 0, changed: 0, unchanged: 0, reformat_error: 0, aborted: 0, saving: 0, saved: 1, save_error: 1, stale_skipped: 0,
  });
});

test("端到端：中止批量处理——已处理的格子保留结果，未处理的格子停在 pending 不再被处理", () => {
  let state = initReformatBatch(
    "row",
    [{ id: "r1", position: 0, cells: { "c-a": "1", "c-b": "2", "c-c": "3" } }],
    BATCH_ROW_COLUMNS,
  );
  state = beginReformatBatchRun(state);
  state = markReformatItemRunning(state, "r1", "c-a");
  state = applyReformatResult(state, "r1", "c-a", { candidateMd: "改写1", source: "llm", changed: true });

  state = abortReformatBatchRun(state); // 用户在第一格完成、第二格尚未发起请求时点「中止」
  assert.strictEqual(state.phase, "reviewing");
  assert.strictEqual(state.aborted, true);
  assert.strictEqual(state.items.find((i) => i.columnId === "c-a").status, "changed");
  assert.strictEqual(state.items.find((i) => i.columnId === "c-c").status, "pending"); // 从未处理

  // 中止后 phase 已不是 running：调用方按 abortedRef 短路不再发起新请求，
  // 这里额外验证纯函数自身的防御——不依赖调用方守规矩。
  const stray = markReformatItemRunning(state, "r1", "c-c");
  assert.strictEqual(stray, state);
});

test("端到端：空作用域——items 为空时也能顺利走完 idle -> running -> reviewing -> done，不崩溃", () => {
  let state = initReformatBatch("row", [{ id: "r1", position: 0, cells: {} }], BATCH_ROW_COLUMNS);
  assert.deepStrictEqual(state.items, []);
  state = beginReformatBatchRun(state);
  assert.strictEqual(state.phase, "running");
  state = finishReformatBatchRun(state);
  assert.strictEqual(state.phase, "reviewing");
  assert.strictEqual(reformatBatchSummary(state).total, 0);
  state = beginReformatBatchSave(state); // 没有 changed 项 -> 直接 done
  assert.strictEqual(state.phase, "done");
});

// ===========================================================================
// 6. 批量规整按内容去重（代码评审修复：anchor 分组的兄弟行 forward-fill 出
//    同一份格子内容时，「整表」批量此前会对每个重复格各调一次 reformat_cell
//    ——LLM temperature=1.0 且默认不缓存，N 份相同输入会产生 N 份互不相同但
//    各自合法的输出；随后 handleCellSave 的合并共享格批量写会用"最后处理的
//    那一格"的候选覆盖整组，而弹窗仍展示每一格各自的候选，用户的确认因此被
//    静默作废。修复：按 (columnId, 原文精确内容) 去重，重复项复用第一次拿到
//    的结果，不再重复调用后端）。
// ===========================================================================

// --- reformatDedupeKey -------------------------------------------------------

test("reformatDedupeKey: 同一列、字面完全相同的原文 → 相同 key", () => {
  const key1 = reformatDedupeKey("c-a", "共享的原文内容");
  const key2 = reformatDedupeKey("c-a", "共享的原文内容");
  assert.strictEqual(key1, key2);
});

test("reformatDedupeKey: 列不同（原文相同）→ 不同 key", () => {
  const key1 = reformatDedupeKey("c-a", "同样的内容");
  const key2 = reformatDedupeKey("c-b", "同样的内容");
  assert.notStrictEqual(key1, key2);
});

test("reformatDedupeKey: 原文不同（列相同）→ 不同 key", () => {
  const key1 = reformatDedupeKey("c-a", "内容甲");
  const key2 = reformatDedupeKey("c-a", "内容乙");
  assert.notStrictEqual(key1, key2);
});

test("reformatDedupeKey: 原文只差首尾空白 → 相同 key（对齐 isSharedColumn 的 .trim() 等价——扇出会把一份候选写满整组）", () => {
  // 修复前用逐字节精确 key，"内容" 与 "内容 " 判为不同 → 两次随机 LLM 调用拿到
  // 两份不同候选；但 knowhow-grouping-logic.ts 的 isSharedColumn 用 .trim() 判两个
  // 兄弟格为「合并共享格」，确认保存时对整组做**一次**批量写（一份内容写满全组）。
  // 两套等价不一致时，弹窗展示的是各格各自的候选、落库的却是最后处理那格的候选，
  // 用户对每格的确认被悄悄作废。去重 key 必须与扇出（保存）用同一套 .trim() 等价。
  const key1 = reformatDedupeKey("c-a", "内容");
  const key2 = reformatDedupeKey("c-a", "内容 ");
  assert.strictEqual(key1, key2);
  // 只归一首尾空白：内部差异（含内部空白）仍是不同 key——与 .trim() 语义一致。
  assert.notStrictEqual(reformatDedupeKey("c-a", "内 容"), reformatDedupeKey("c-a", "内容"));
});

test("reformatDedupeKey: 拼接歧义安全——(列, 内含分隔符的原文) 与 (内含同一分隔符的列, 原文) 不会撞成同一 key", () => {
  // 若实现是简单的 `${columnId}|${originalMd}` 字符串拼接，以下两组会撞成
  // 同一个 key（"a|b|c"）；正确实现必须能区分它们。
  const key1 = reformatDedupeKey("a", "b|c");
  const key2 = reformatDedupeKey("a|b", "c");
  assert.notStrictEqual(key1, key2);
});

test("reformatDedupeKey: 返回字符串类型", () => {
  assert.strictEqual(typeof reformatDedupeKey("c-a", "x"), "string");
});

// --- applyReformatCachedResult（"扇出"：把已经拿到的候选套用到另一个格子，
// 不再发起新请求）------------------------------------------------------------

test("applyReformatCachedResult: pending 格子套用缓存的 changed 结果 → changed(candidateMd, source)，与真实网络结果套用效果一致", () => {
  let state = initReformatBatch(
    "row",
    [{ id: "r1", position: 0, cells: { "c-a": "原文" } }],
    [{ id: "c-a", name: "A", position: 0 }],
  );
  state = beginReformatBatchRun(state);
  const cached = { candidateMd: "- 规整后的内容", source: "llm", changed: true };
  state = applyReformatCachedResult(state, "r1", "c-a", cached);
  assert.strictEqual(state.items[0].status, "changed");
  assert.strictEqual(state.items[0].candidateMd, "- 规整后的内容");
  assert.strictEqual(state.items[0].source, "llm");
});

test("applyReformatCachedResult: 套用 changed=false 的缓存结果 → unchanged(source)，不带 candidateMd", () => {
  let state = initReformatBatch(
    "row",
    [{ id: "r1", position: 0, cells: { "c-a": "原文" } }],
    [{ id: "c-a", name: "A", position: 0 }],
  );
  state = beginReformatBatchRun(state);
  state = applyReformatCachedResult(state, "r1", "c-a", { candidateMd: "原文", source: "rule/no-llm", changed: false });
  assert.strictEqual(state.items[0].status, "unchanged");
  assert.strictEqual(state.items[0].candidateMd, undefined);
});

test("applyReformatCachedResult: 扇出——同一份缓存结果套用到两个不同格子，两者 candidateMd 完全一致", () => {
  let state = initReformatBatch(
    "table",
    [
      { id: "r1", position: 0, cells: { "c-a": "重复内容" } },
      { id: "r2", position: 1, cells: { "c-a": "重复内容" } },
    ],
    [{ id: "c-a", name: "A", position: 0 }],
  );
  state = beginReformatBatchRun(state);
  const cached = { candidateMd: "- 唯一一次调用拿到的候选", source: "llm", changed: true };
  state = applyReformatCachedResult(state, "r1", "c-a", cached);
  state = applyReformatCachedResult(state, "r2", "c-a", cached);
  const r1 = state.items.find((item) => item.rowId === "r1");
  const r2 = state.items.find((item) => item.rowId === "r2");
  assert.strictEqual(r1.candidateMd, r2.candidateMd);
  assert.strictEqual(r1.candidateMd, "- 唯一一次调用拿到的候选");
  assert.strictEqual(r1.status, "changed");
  assert.strictEqual(r2.status, "changed");
});

test("applyReformatCachedResult: 非 running 阶段原样返回（phase 总闸同 applyReformatResult）", () => {
  const state = batchStateOf("reviewing", [makeItem("r1", "c-a", "pending")]);
  assert.strictEqual(applyReformatCachedResult(state, "r1", "c-a", { candidateMd: "x", source: "llm", changed: true }), state);
});

test("applyReformatCachedResult: 目标项不是 pending（如已经 changed）时原样返回——不会覆盖已经处理过的格子", () => {
  const state = batchStateOf("running", [makeItem("r1", "c-a", "changed", { candidateMd: "已有候选" })]);
  const next = applyReformatCachedResult(state, "r1", "c-a", { candidateMd: "另一个候选", source: "llm", changed: true });
  assert.strictEqual(next, state);
});

// --- 端到端：整表批量对「同列同内容」的重复格子只需一次真实结果 ---------------

test("端到端（表作用域，去重）：两行同列同内容——只对第一次出现调用后端，第二次命中缓存复用同一候选，两格最终内容完全一致", () => {
  const rows = [
    { id: "r1", position: 0, cells: { "c-a": "同一份原文" } },
    { id: "r2", position: 1, cells: { "c-a": "同一份原文" } },
  ];
  const columns = [{ id: "c-a", name: "A", position: 0 }];
  let state = initReformatBatch("table", rows, columns);
  state = beginReformatBatchRun(state);

  // 模拟 runBatch 的去重循环：一个本地 Map，逐项检查 dedupe key。
  const cache = new Map();
  let backendCallCount = 0;
  for (const item of state.items) {
    const key = reformatDedupeKey(item.columnId, item.originalMd);
    if (cache.has(key)) {
      state = applyReformatCachedResult(state, item.rowId, item.columnId, cache.get(key));
      continue;
    }
    backendCallCount += 1;
    // 真实场景里 temperature=1.0 每次都可能拿到不同但同样合法的候选——这里
    // 用「调用序号」模拟这种"每次不同"，用来证明第二格并没有真的再调一次
    // （否则它会拿到 "- 候选#2" 而不是复用 "- 候选#1"）。
    const result = { candidateMd: `- 候选#${backendCallCount}`, source: "llm", changed: true };
    cache.set(key, result);
    state = markReformatItemRunning(state, item.rowId, item.columnId);
    state = applyReformatResult(state, item.rowId, item.columnId, result);
  }
  state = finishReformatBatchRun(state);

  assert.strictEqual(backendCallCount, 1); // 两个重复格只发起了一次"后端调用"
  const r1 = state.items.find((item) => item.rowId === "r1");
  const r2 = state.items.find((item) => item.rowId === "r2");
  assert.strictEqual(r1.candidateMd, "- 候选#1");
  assert.strictEqual(r2.candidateMd, "- 候选#1"); // 复用同一候选，不是 "- 候选#2"
  assert.strictEqual(r1.candidateMd, r2.candidateMd);
});

test("端到端（表作用域，去重）：两行同列、内容只差首尾空白——仍去重到一次调用，两格拿到同一候选（对齐扇出 .trim() 等价，防显示≠落库）", () => {
  // 兄弟行把同一概念值 forward-fill 后，某列常出现「一格带尾随空格、另一格没有」
  // 这种手写异构（isSharedColumn 用 .trim() 仍判为合并共享格 → 保存写满整组一份
  // 内容）。去重 key 若按逐字节精确，会放两次 temperature=1.0 调用、拿到两份不同
  // 候选，弹窗逐格展示、保存却整组覆盖成最后一格的候选——正是本 memo 要防的
  // 「显示≠落库」。修复后 key 走 .trim()，两格命中同一次调用、拿到同一候选。
  const rows = [
    { id: "r1", position: 0, cells: { "c-a": "示波器 " } }, // 尾随空格
    { id: "r2", position: 1, cells: { "c-a": "示波器" } },
  ];
  const columns = [{ id: "c-a", name: "A", position: 0 }];
  let state = initReformatBatch("table", rows, columns);
  state = beginReformatBatchRun(state);

  const cache = new Map();
  let backendCallCount = 0;
  for (const item of state.items) {
    const key = reformatDedupeKey(item.columnId, item.originalMd);
    if (cache.has(key)) {
      state = applyReformatCachedResult(state, item.rowId, item.columnId, cache.get(key));
      continue;
    }
    backendCallCount += 1;
    const result = { candidateMd: `- 候选#${backendCallCount}`, source: "llm", changed: true };
    cache.set(key, result);
    state = markReformatItemRunning(state, item.rowId, item.columnId);
    state = applyReformatResult(state, item.rowId, item.columnId, result);
  }
  state = finishReformatBatchRun(state);

  assert.strictEqual(backendCallCount, 1); // 只差首尾空白——必须去重成一次调用
  const r1 = state.items.find((item) => item.rowId === "r1");
  const r2 = state.items.find((item) => item.rowId === "r2");
  assert.strictEqual(r1.candidateMd, "- 候选#1");
  assert.strictEqual(r2.candidateMd, "- 候选#1"); // 复用同一候选，与整组批量写落库的那一份一致
  assert.strictEqual(r1.candidateMd, r2.candidateMd);
});

test("端到端（表作用域，去重）：同列但内容不同的格子——各自独立调用，互不影响", () => {
  const rows = [
    { id: "r1", position: 0, cells: { "c-a": "内容甲" } },
    { id: "r2", position: 1, cells: { "c-a": "内容乙" } },
  ];
  const columns = [{ id: "c-a", name: "A", position: 0 }];
  let state = initReformatBatch("table", rows, columns);
  state = beginReformatBatchRun(state);

  const cache = new Map();
  let backendCallCount = 0;
  for (const item of state.items) {
    const key = reformatDedupeKey(item.columnId, item.originalMd);
    if (cache.has(key)) {
      state = applyReformatCachedResult(state, item.rowId, item.columnId, cache.get(key));
      continue;
    }
    backendCallCount += 1;
    const result = { candidateMd: `- 候选#${backendCallCount}`, source: "llm", changed: true };
    cache.set(key, result);
    state = markReformatItemRunning(state, item.rowId, item.columnId);
    state = applyReformatResult(state, item.rowId, item.columnId, result);
  }
  state = finishReformatBatchRun(state);

  assert.strictEqual(backendCallCount, 2); // 内容不同——不应被去重
  const r1 = state.items.find((item) => item.rowId === "r1");
  const r2 = state.items.find((item) => item.rowId === "r2");
  assert.notStrictEqual(r1.candidateMd, r2.candidateMd);
});

// --- 扇出时 `changed` 必须按**目标格自己的原文**重算（代码评审修复）------------
//
// 去重 key 归一到 .trim()（与保存侧合并共享格的 .trim() 等价扇出对齐），所以
// 「foo」与「foo 」落在同一等价类、复用同一份 candidateMd——这是对的（候选本就
// 该扇出一份）。但缓存里那份结果的 `changed` 是**第一次处理的那一格**算出来的：
// 若 "foo"（本就干净 → changed=false）先处理，其 changed=false 被复用到 "foo "，
// 后者会被误标 unchanged、排除出待保存集、尾随空白永不规整（静默跳过）。
// 修复：applyReformatCachedResult 落地缓存候选时，按 candidateMd !== 目标格
// 自己的 originalMd 重算 changed（候选共享，只是那个 FLAG 之前算错了）。

test("applyReformatCachedResult: changed 按**目标格自己的原文**重算——缓存 changed=false 但候选≠本格原文时，本格仍应 changed(带候选)（修复：尾随空白静默不规整）", () => {
  let state = initReformatBatch(
    "table",
    [{ id: "r2", position: 0, cells: { "c-a": "foo " } }], // 目标格原文带尾随空格
    [{ id: "c-a", name: "A", position: 0 }],
  );
  state = beginReformatBatchRun(state);
  // 缓存来自先处理的 "foo"（本就干净）：candidateMd="foo"、changed=false。
  state = applyReformatCachedResult(state, "r2", "c-a", { candidateMd: "foo", source: "rule/no-llm", changed: false });
  assert.strictEqual(state.items[0].status, "changed"); // 候选 "foo" ≠ 本格原文 "foo " → 有改动
  assert.strictEqual(state.items[0].candidateMd, "foo");
});

test("applyReformatCachedResult: 反向——缓存 changed=true 但候选==本格原文时，本格应 unchanged（不误标改动、不带候选）", () => {
  let state = initReformatBatch(
    "table",
    [{ id: "r2", position: 0, cells: { "c-a": "foo" } }], // 目标格原文已干净
    [{ id: "c-a", name: "A", position: 0 }],
  );
  state = beginReformatBatchRun(state);
  // 缓存来自先处理的 "foo "（带尾随空格）：candidateMd="foo"、changed=true。
  state = applyReformatCachedResult(state, "r2", "c-a", { candidateMd: "foo", source: "llm", changed: true });
  assert.strictEqual(state.items[0].status, "unchanged"); // 候选 "foo" == 本格原文 "foo" → 无改动
  assert.strictEqual(state.items[0].candidateMd, undefined);
});

test("端到端（表作用域，去重）：'foo'(本就干净) 先处理、'foo '(应规整) 后命中缓存——changed 按目标格重算，第二格进保存集、尾随空白被规整", () => {
  const rows = [
    { id: "r1", position: 0, cells: { "c-a": "foo" } }, // 本就干净 → changed=false
    { id: "r2", position: 1, cells: { "c-a": "foo " } }, // 尾随空格 → 应规整成 "foo"
  ];
  const columns = [{ id: "c-a", name: "A", position: 0 }];
  let state = initReformatBatch("table", rows, columns);
  state = beginReformatBatchRun(state);

  const cache = new Map();
  let backendCallCount = 0;
  for (const item of state.items) {
    const key = reformatDedupeKey(item.columnId, item.originalMd);
    if (cache.has(key)) {
      state = applyReformatCachedResult(state, item.rowId, item.columnId, cache.get(key));
      continue;
    }
    backendCallCount += 1;
    // 后端对 "foo" 规整：已经干净 → 候选就是 "foo"、changed=false。
    const result = { candidateMd: "foo", source: "rule/no-llm", changed: false };
    cache.set(key, result);
    state = markReformatItemRunning(state, item.rowId, item.columnId);
    state = applyReformatResult(state, item.rowId, item.columnId, result);
  }
  state = finishReformatBatchRun(state);

  assert.strictEqual(backendCallCount, 1); // 同一 .trim() 等价类只调一次后端
  const r1 = state.items.find((i) => i.rowId === "r1");
  const r2 = state.items.find((i) => i.rowId === "r2");
  assert.strictEqual(r1.status, "unchanged"); // 第一格本就干净：不进保存集
  assert.strictEqual(r2.status, "changed"); // 第二格：候选≠自己原文 → 有改动
  assert.strictEqual(r2.candidateMd, "foo");
  // 确认那一刻的保存集（status==="changed"）只含第二格。
  const saveSet = state.items.filter((i) => i.status === "changed");
  assert.deepStrictEqual(
    saveSet.map((i) => i.rowId),
    ["r2"],
  );
});

test("端到端（表作用域，去重）：镜像顺序 'foo ' 先处理、'foo' 后命中缓存——第一格 changed=true、第二格按自己原文重算为 unchanged", () => {
  const rows = [
    { id: "r1", position: 0, cells: { "c-a": "foo " } }, // 尾随空格 → changed=true
    { id: "r2", position: 1, cells: { "c-a": "foo" } }, // 本就干净 → changed=false
  ];
  const columns = [{ id: "c-a", name: "A", position: 0 }];
  let state = initReformatBatch("table", rows, columns);
  state = beginReformatBatchRun(state);

  const cache = new Map();
  let backendCallCount = 0;
  for (const item of state.items) {
    const key = reformatDedupeKey(item.columnId, item.originalMd);
    if (cache.has(key)) {
      state = applyReformatCachedResult(state, item.rowId, item.columnId, cache.get(key));
      continue;
    }
    backendCallCount += 1;
    // 后端对 "foo " 规整：去掉尾随空白 → 候选 "foo"、changed=true。
    const result = { candidateMd: "foo", source: "rule/no-llm", changed: true };
    cache.set(key, result);
    state = markReformatItemRunning(state, item.rowId, item.columnId);
    state = applyReformatResult(state, item.rowId, item.columnId, result);
  }
  state = finishReformatBatchRun(state);

  assert.strictEqual(backendCallCount, 1);
  const r1 = state.items.find((i) => i.rowId === "r1");
  const r2 = state.items.find((i) => i.rowId === "r2");
  assert.strictEqual(r1.status, "changed"); // 第一格带空格：进保存集
  assert.strictEqual(r1.candidateMd, "foo");
  assert.strictEqual(r2.status, "unchanged"); // 第二格候选 "foo" == 自己原文 "foo" → 无改动
  const saveSet = state.items.filter((i) => i.status === "changed");
  assert.deepStrictEqual(
    saveSet.map((i) => i.rowId),
    ["r1"],
  );
});

// ===========================================================================
// 6. 批量保存：合并共享格「扇出等价类」去重（代码评审 F1）
// ===========================================================================
//
// anchor 分组里某个合并共享列（组内多于一行、该列全分支同值，见
// knowhow-grouping-logic.ts isSharedColumn）一旦有改动，整表批量会为组内每一
// 兄弟行各建一个 changed 条目——去重后它们的 candidateMd 逐字相同。此前 runSave
// 对每个 changed 条目各调一次 onSaveCell(=handleCellSave)，而 handleCellSave 又
// 把每次调用扇写到组内全部 rowId：N 兄弟 → N 次请求、N² 次行 upsert、N 次投影
// 版本 bump，纯浪费（内容相同，正确性无碍）。planReformatSaves 在保存循环之前
// 把「会扇写到同一组」的条目合并成一个代表保存——判定复用 groupRowsByAnchor /
// isSharedColumn / groupCellWriteTargets 这套与 handleCellSave 完全相同的等价，
// 不另发明。

// 共享列场景：anchor=概念，c-shared 是组内全分支同值的共享列，c-branch 是每行
// 各异的分支列。
const F1_SHARED_ROWS = [
  { id: "r1", position: 0, cells: { "c-anchor": "示波器", "c-shared": "旧共享说明", "c-branch": "分支甲" } },
  { id: "r2", position: 1, cells: { "c-anchor": "示波器", "c-shared": "旧共享说明", "c-branch": "分支乙" } },
  { id: "r3", position: 2, cells: { "c-anchor": "示波器", "c-shared": "旧共享说明", "c-branch": "分支丙" } },
];
const F1_COLUMNS = [
  { id: "c-anchor", name: "概念", position: 0 },
  { id: "c-shared", name: "共享说明", position: 1 },
  { id: "c-branch", name: "分支值", position: 2 },
];

// 把一批 rows/columns 驱动到 saving 阶段（模拟 runBatch 收集候选 + 用户确认），
// candidate(item) 返回该格的 {candidateMd, source, changed}——正是真实 runSave
// 保存循环开始那一刻的状态。
function buildSavingState(scope, rows, columns, candidate) {
  let state = initReformatBatch(scope, rows, columns);
  state = beginReformatBatchRun(state);
  for (const item of state.items) {
    state = markReformatItemRunning(state, item.rowId, item.columnId);
    state = applyReformatResult(state, item.rowId, item.columnId, candidate(item));
  }
  state = finishReformatBatchRun(state);
  state = beginReformatBatchSave(state);
  return state;
}

test("planReformatSaves: 合并共享格——同组同列的 3 个 changed 兄弟合并成一个 save unit（representative + 3 成员）", () => {
  const changed = F1_SHARED_ROWS.map((r) => ({
    rowId: r.id,
    columnId: "c-shared",
    columnName: "共享说明",
    originalMd: "旧共享说明",
    status: "changed",
    candidateMd: "新共享说明",
    source: "llm",
  }));
  const units = planReformatSaves(changed, F1_SHARED_ROWS, "c-anchor");
  assert.strictEqual(units.length, 1); // 3 兄弟合并成 1 个代表保存
  assert.strictEqual(units[0].representative.rowId, "r1"); // 输入顺序里第一个作代表
  assert.deepStrictEqual(
    units[0].members.map((m) => m.rowId),
    ["r1", "r2", "r3"],
  );
});

test("planReformatSaves: 非共享列（组内各行值不同）——不合并，各自独立成 unit", () => {
  const changed = [
    { rowId: "r1", columnId: "c-branch", columnName: "分支值", originalMd: "分支甲", status: "changed", candidateMd: "分支甲改", source: "llm" },
    { rowId: "r2", columnId: "c-branch", columnName: "分支值", originalMd: "分支乙", status: "changed", candidateMd: "分支乙改", source: "llm" },
  ];
  const units = planReformatSaves(changed, F1_SHARED_ROWS, "c-anchor");
  assert.strictEqual(units.length, 2);
  assert.deepStrictEqual(units.map((u) => u.members.length), [1, 1]);
});

test("planReformatSaves: 记录型表（anchorColumnId=null）——不分组，全部各自独立", () => {
  const rows = [
    { id: "r1", position: 0, cells: { "c-a": "同内容" } },
    { id: "r2", position: 1, cells: { "c-a": "同内容" } },
  ];
  const changed = [
    { rowId: "r1", columnId: "c-a", columnName: "A", originalMd: "同内容", status: "changed", candidateMd: "新内容", source: "llm" },
    { rowId: "r2", columnId: "c-a", columnName: "A", originalMd: "同内容", status: "changed", candidateMd: "新内容", source: "llm" },
  ];
  const units = planReformatSaves(changed, rows, null);
  assert.strictEqual(units.length, 2); // 无 anchor → 无扇出 → 不合并
});

test("planReformatSaves: 不同概念组即使同列也不合并（写目标集互斥）", () => {
  const rows = [
    { id: "r1", position: 0, cells: { "c-anchor": "示波器", "c-shared": "同值" } },
    { id: "r2", position: 1, cells: { "c-anchor": "万用表", "c-shared": "同值" } },
  ];
  const changed = [
    { rowId: "r1", columnId: "c-shared", columnName: "共享说明", originalMd: "同值", status: "changed", candidateMd: "新值", source: "llm" },
    { rowId: "r2", columnId: "c-shared", columnName: "共享说明", originalMd: "同值", status: "changed", candidateMd: "新值", source: "llm" },
  ];
  // 每组只有一行 → isSharedColumn 恒 false（单行组无「其他分支」）→ 各自独立。
  const units = planReformatSaves(changed, rows, "c-anchor");
  assert.strictEqual(units.length, 2);
});

// --- F1（P1）：行作用域批量必须按 handleCellSave 真正扇写的**完整**写目标集
// 规划 + 查陈旧 ---------------------------------------------------------------
// 行作用域下 batch.items 只含被选中行（展示成员），但确认保存走 handleCellSave，
// 它用**完整** detail.rows 重算 anchor 组、把候选扇写到组内每一个兄弟行。若
// planReformatSaves 只拿到被选中行来规划，unit 的写目标就只有那一格：并发改动
// 某个**非展示**兄弟行既不查陈旧、也不展示，会被静默覆盖。修复：无论作用域，
// planReformatSaves/查陈旧都吃**完整** detail.rows，让共享列 unit 的 writeTargets
// 覆盖 fan-out 会写到的全部兄弟；展示成员（members）仍只限被选中行。
test("planReformatSaves 行作用域：共享列 unit 的 writeTargets 覆盖全部兄弟行（不只被选中行），members 仍只含被选中行", () => {
  // 行作用域：只有被选中行 r1 是展示条目（batch.items 只建了 r1 那一格）。
  const changed = [
    { rowId: "r1", columnId: "c-shared", columnName: "共享说明", originalMd: "旧共享说明", status: "changed", candidateMd: "新共享说明", source: "llm" },
  ];
  // 传**完整** detail.rows（r1/r2/r3 同组共享列）——与 handleCellSave 同源。
  const units = planReformatSaves(changed, F1_SHARED_ROWS, "c-anchor");
  assert.strictEqual(units.length, 1);
  // 展示成员仍只有被选中行（弹窗只展示 r1 那一格的 before/after）。
  assert.deepStrictEqual(units[0].members.map((m) => m.rowId), ["r1"]);
  // 写目标覆盖 fan-out 会写到的全部兄弟——这正是 handleCellSave 的 groupCellWriteTargets。
  assert.deepStrictEqual(
    units[0].writeTargets.map((t) => t.rowId).slice().sort(),
    ["r1", "r2", "r3"],
  );
  // 每个写目标带上建批次那一刻该格的 originalMd 快照（查陈旧的基线）。
  for (const t of units[0].writeTargets) {
    assert.strictEqual(t.columnId, "c-shared");
    assert.strictEqual(t.originalMd, "旧共享说明");
  }
});

// --- P1（round-4）：写目标还带上快照 anchorMd，让守卫拦住「离组行被冻结扇出误写」---
// 批量扇出用建批次那一刻冻结的行 id 写共享格。若他人把某兄弟行的 anchor 改掉（移出
// 本组）但没动被编辑的格，expected_before 仍匹配、后端旧守卫看不见——候选就落到已
// 离组的行。修复：每个写目标额外带上该行 anchor 列的**快照**值（anchorMd），后端在同
// 一事务内重读比对，任一离组即整组 409。anchorMd 与 originalMd 同源于传入的那份快照。
test("planReformatSaves: 共享列 unit 的每个 writeTarget 带上快照 anchorMd（= 建批次那刻该行 anchor 列值）", () => {
  const changed = [
    { rowId: "r1", columnId: "c-shared", columnName: "共享说明", originalMd: "旧共享说明", status: "changed", candidateMd: "新共享说明", source: "llm" },
  ];
  const units = planReformatSaves(changed, F1_SHARED_ROWS, "c-anchor");
  assert.strictEqual(units.length, 1);
  // 三个写目标各带自己那行的 anchor 列快照值（F1_SHARED_ROWS 全组 c-anchor="示波器"）。
  const anchorByRow = new Map(units[0].writeTargets.map((t) => [t.rowId, t.anchorMd]));
  assert.deepStrictEqual(
    [...anchorByRow.entries()].sort(),
    [["r1", "示波器"], ["r2", "示波器"], ["r3", "示波器"]],
  );
});

test("planReformatSaves: writeTarget.anchorMd 在建批次那刻定格——之后改实时行的 anchor 不泄漏进来（快照不可变）", () => {
  // 局部快照（不污染共享夹具）。planReformatSaves 是纯函数：只读传入这份 rows，
  // 故 runSave 传 snapshotRef.current.allRows 时 anchor 基线恒为快照值。
  const snapshot = [
    { id: "r1", position: 0, cells: { "c-anchor": "示波器", "c-shared": "旧共享说明" } },
    { id: "r2", position: 1, cells: { "c-anchor": "示波器", "c-shared": "旧共享说明" } },
  ];
  const changed = [
    { rowId: "r1", columnId: "c-shared", columnName: "共享说明", originalMd: "旧共享说明", status: "changed", candidateMd: "新共享说明", source: "llm" },
  ];
  const units = planReformatSaves(changed, snapshot, "c-anchor");
  // 弹窗打开期间实时行被改（模拟 detail 刷新/他人把该行移出组）——已规划的 unit 不受影响。
  snapshot[0].cells["c-anchor"] = "万用表";
  snapshot[1].cells["c-anchor"] = "万用表";
  for (const t of units[0].writeTargets) {
    assert.strictEqual(t.anchorMd, "示波器"); // 仍是建批次那刻的快照值
  }
});

test("planReformatSaves: 记录型表（anchorColumnId=null）——writeTarget.anchorMd 为空串（无 anchor 列可读）", () => {
  const rows = [{ id: "r1", position: 0, cells: { "c-a": "同内容" } }];
  const changed = [
    { rowId: "r1", columnId: "c-a", columnName: "A", originalMd: "同内容", status: "changed", candidateMd: "新内容", source: "llm" },
  ];
  const units = planReformatSaves(changed, rows, null);
  assert.strictEqual(units[0].writeTargets[0].anchorMd, "");
});

test("isReformatUnitStale 行作用域：非展示兄弟(r3)被并发改动 → 整组判 stale（RED：旧实现只查被选中行，看不见 r3 的改动）", () => {
  const changed = [
    { rowId: "r1", columnId: "c-shared", columnName: "共享说明", originalMd: "旧共享说明", status: "changed", candidateMd: "新共享说明", source: "llm" },
  ];
  const units = planReformatSaves(changed, F1_SHARED_ROWS, "c-anchor");
  // r1 自己没变、r2 没变，只有**非展示**的 r3 被别人改了——fan-out 仍会覆盖 r3，
  // 故整组必须判 stale、拒绝盲写。旧实现的 unit 只含 r1，看不见 r3 → 误判 fresh。
  const staleCurrent = new Map([
    [reformatSaveCellKey("r1", "c-shared"), "旧共享说明"],
    [reformatSaveCellKey("r2", "c-shared"), "旧共享说明"],
    [reformatSaveCellKey("r3", "c-shared"), "r3 被别人改了"],
  ]);
  assert.strictEqual(isReformatUnitStale(units[0], staleCurrent), true);
});

test("isReformatUnitStale 行作用域：全部兄弟都新鲜 → 不 stale（照常保存整组）", () => {
  const changed = [
    { rowId: "r1", columnId: "c-shared", columnName: "共享说明", originalMd: "旧共享说明", status: "changed", candidateMd: "新共享说明", source: "llm" },
  ];
  const units = planReformatSaves(changed, F1_SHARED_ROWS, "c-anchor");
  const freshCurrent = new Map([
    [reformatSaveCellKey("r1", "c-shared"), "旧共享说明"],
    [reformatSaveCellKey("r2", "c-shared"), "旧共享说明"],
    [reformatSaveCellKey("r3", "c-shared"), "旧共享说明"],
  ]);
  assert.strictEqual(isReformatUnitStale(units[0], freshCurrent), false);
});

test("planReformatSaves 行作用域：非共享列（c-branch 每行各异）不受影响——writeTargets 只含被选中格", () => {
  const changed = [
    { rowId: "r1", columnId: "c-branch", columnName: "分支值", originalMd: "分支甲", status: "changed", candidateMd: "分支甲改", source: "llm" },
  ];
  const units = planReformatSaves(changed, F1_SHARED_ROWS, "c-anchor");
  assert.strictEqual(units.length, 1);
  assert.deepStrictEqual(units[0].writeTargets.map((t) => t.rowId), ["r1"]); // 非共享列不扇出
  assert.deepStrictEqual(units[0].members.map((m) => m.rowId), ["r1"]);
});

// --- P1（round-6）：coversAnchorGroup 门——服务端「组成员精确相等」结构守卫只对**整组写**
//     成立。planReformatSaves 给每个 unit 标注：它的 writeTargets 是否**恰好**是建批次那一刻
//     该 anchor 组的完整成员集。true 的两类才由 runSave 下发 anchorGuard、handleCellSave 走
//     guarded 批量端点：①合并共享列扇写（writeTargets=整组）②**单例组**（组内仅一行，
//     writeTargets=那一行=整组）。false 的一类是回归防线：多行组里的**非共享列**——writeTargets
//     是**有意的子集**（只写这一行），若也带守卫，后端「current==frozen」会把「组里本就有别的
//     兄弟行」误判成 joiner 漂移、把每一次合法的逐行属性规整都**假 409**。记录型表（无 anchor 组）
//     恒 false。这是「单例组增员漏检」P1 与「非共享列假 409」回归之间的唯一判别位。----------------
const P1R6_SINGLETON_ROWS = [
  { id: "r1", position: 0, cells: { "c-anchor": "示波器", "c-x": "内容一" } },
  { id: "r2", position: 1, cells: { "c-anchor": "万用表", "c-x": "内容二" } },
];

test("planReformatSaves: 单例组（组内仅一行）——非共享分支但 coversAnchorGroup=true（writeTargets 即整组 → 走 guarded 端点，捕获增员漂移）", () => {
  const changed = [
    { rowId: "r1", columnId: "c-x", columnName: "X", originalMd: "内容一", status: "changed", candidateMd: "内容一改", source: "llm" },
  ];
  const units = planReformatSaves(changed, P1R6_SINGLETON_ROWS, "c-anchor");
  assert.strictEqual(units.length, 1);
  assert.deepStrictEqual(units[0].writeTargets.map((t) => t.rowId), ["r1"]); // 单行组只写自己
  assert.strictEqual(units[0].coversAnchorGroup, true); // = 整组 → runSave 下发 anchorGuard
});

test("planReformatSaves: 多行组的非共享列——coversAnchorGroup=false（writeTargets 是有意子集；带守卫会把兄弟行误判 joiner 假 409）", () => {
  const changed = [
    { rowId: "r1", columnId: "c-branch", columnName: "分支值", originalMd: "分支甲", status: "changed", candidateMd: "分支甲改", source: "llm" },
  ];
  const units = planReformatSaves(changed, F1_SHARED_ROWS, "c-anchor"); // {r1,r2,r3} 同 anchor，c-branch 每行各异
  assert.strictEqual(units.length, 1);
  assert.deepStrictEqual(units[0].writeTargets.map((t) => t.rowId), ["r1"]); // 只写这一行（子集）
  assert.strictEqual(units[0].coversAnchorGroup, false); // 子集 → runSave 不下发 anchorGuard → 单格端点 → 不假 409
});

test("planReformatSaves: 合并共享列（多行组全同值）——coversAnchorGroup=true（writeTargets=整组扇写）", () => {
  const changed = F1_SHARED_ROWS.map((r) => ({
    rowId: r.id, columnId: "c-shared", columnName: "共享说明", originalMd: "旧共享说明", status: "changed", candidateMd: "新共享说明", source: "llm",
  }));
  const units = planReformatSaves(changed, F1_SHARED_ROWS, "c-anchor");
  assert.strictEqual(units.length, 1);
  assert.deepStrictEqual(units[0].writeTargets.map((t) => t.rowId).slice().sort(), ["r1", "r2", "r3"]); // 整组
  assert.strictEqual(units[0].coversAnchorGroup, true);
});

test("planReformatSaves: 记录型表（anchorColumnId=null）——coversAnchorGroup=false（无 anchor 组可精确相等）", () => {
  const rows = [{ id: "r1", position: 0, cells: { "c-a": "同内容" } }];
  const changed = [
    { rowId: "r1", columnId: "c-a", columnName: "A", originalMd: "同内容", status: "changed", candidateMd: "新内容", source: "llm" },
  ];
  const units = planReformatSaves(changed, rows, null);
  assert.strictEqual(units[0].coversAnchorGroup, false);
});

test("端到端（保存循环，P1 round-6）：单例组遇 409 → 成员落既有 stale_skipped 停靠态（与多目标扇写同一终态）", () => {
  // 单例组 unit 带 coversAnchorGroup=true → runSave 下发 anchorGuard → handleCellSave 走 guarded
  // 批量端点。若建批次后有兄弟行 join 该 anchor 组，服务端「组成员精确相等」判 409（frozen {r1}
  // ≠ current {r1,新增行}）。这条坐实：单例经批量端点拿到的 409 落进**既有** stale-skip 机制，
  // 不是新终态——runSave 的 409 catch 分支（httpErrorStatus===409）整组 markReformatItemStaleSkipped。
  const changed = [
    { rowId: "r1", columnId: "c-x", columnName: "X", originalMd: "内容一", status: "changed", candidateMd: "内容一改", source: "llm" },
  ];
  const units = planReformatSaves(changed, P1R6_SINGLETON_ROWS, "c-anchor");
  assert.strictEqual(units[0].coversAnchorGroup, true); // 前置：确会下发 anchorGuard / 走 guarded 端点
  let state = batchStateOf("saving", units[0].members.map((m) => makeItem(m.rowId, m.columnId, "changed")));
  for (const m of units[0].members) state = markReformatItemSaving(state, m.rowId, m.columnId);
  for (const m of units[0].members) state = markReformatItemStaleSkipped(state, m.rowId, m.columnId, BATCH_REFORMAT_STALE_SKIP_TEXT);
  assert.strictEqual(reformatBatchSummary(state).stale_skipped, 1);
  assert.strictEqual(state.items[0].status, "stale_skipped");
  assert.strictEqual(state.items[0].errorMessage, BATCH_REFORMAT_STALE_SKIP_TEXT);
});

test("端到端（保存循环，F1）：合并共享格 3 兄弟——只调一次 onSaveCell（representative），三格全部 saved", () => {
  let state = buildSavingState("table", F1_SHARED_ROWS, F1_COLUMNS, (item) =>
    // c-shared 全组改动（旧共享说明 → 新共享说明），c-anchor/c-branch 无需改动。
    item.columnId === "c-shared"
      ? { candidateMd: "新共享说明", source: "llm", changed: true }
      : { candidateMd: item.originalMd, source: "rule/no-llm", changed: false },
  );
  const changed = state.items.filter((item) => item.status === "changed");
  assert.strictEqual(changed.length, 3); // 三兄弟各一个 changed 条目

  const units = planReformatSaves(changed, F1_SHARED_ROWS, "c-anchor");
  let saveCalls = 0;
  for (const unit of units) {
    saveCalls += 1; // 真实 runSave 里这一步是 await onSaveCell(representative...)
    state = unit.members.reduce((s, m) => markReformatItemSaving(s, m.rowId, m.columnId), state);
    state = unit.members.reduce((s, m) => applyReformatSaveSuccess(s, m.rowId, m.columnId), state);
  }
  assert.strictEqual(saveCalls, 1); // 关键：3 兄弟只发起一次保存（扇写覆盖整组）
  assert.strictEqual(reformatBatchSummary(state).saved, 3); // 三格终态都是 saved
});

test("端到端（保存循环，F1）：representative 保存失败——同组三成员一起 save_error（共享同一次失败）", () => {
  let state = buildSavingState("table", F1_SHARED_ROWS, F1_COLUMNS, (item) =>
    item.columnId === "c-shared"
      ? { candidateMd: "新共享说明", source: "llm", changed: true }
      : { candidateMd: item.originalMd, source: "rule/no-llm", changed: false },
  );
  const changed = state.items.filter((item) => item.status === "changed");
  const units = planReformatSaves(changed, F1_SHARED_ROWS, "c-anchor");
  let saveCalls = 0;
  for (const unit of units) {
    saveCalls += 1;
    state = unit.members.reduce((s, m) => markReformatItemSaving(s, m.rowId, m.columnId), state);
    // representative 保存抛错 → 整组成员共享这次失败。
    state = unit.members.reduce((s, m) => applyReformatSaveError(s, m.rowId, m.columnId, "保存失败，请重试"), state);
  }
  assert.strictEqual(saveCalls, 1);
  assert.strictEqual(reformatBatchSummary(state).save_error, 3);
});

test("端到端（保存循环，F1）：非共享列——每格各调一次 onSaveCell（不被去重误伤）", () => {
  let state = buildSavingState("table", F1_SHARED_ROWS, F1_COLUMNS, (item) =>
    // c-branch 每行各异、全部改动；c-shared/c-anchor 无需改动。
    item.columnId === "c-branch"
      ? { candidateMd: `${item.originalMd}-改`, source: "llm", changed: true }
      : { candidateMd: item.originalMd, source: "rule/no-llm", changed: false },
  );
  const changed = state.items.filter((item) => item.status === "changed");
  assert.strictEqual(changed.length, 3);
  const units = planReformatSaves(changed, F1_SHARED_ROWS, "c-anchor");
  let saveCalls = 0;
  for (const unit of units) {
    saveCalls += 1;
    state = unit.members.reduce((s, m) => markReformatItemSaving(s, m.rowId, m.columnId), state);
    state = unit.members.reduce((s, m) => applyReformatSaveSuccess(s, m.rowId, m.columnId), state);
  }
  assert.strictEqual(saveCalls, 3); // 非共享列不合并——三格三次保存
  assert.strictEqual(reformatBatchSummary(state).saved, 3);
});

// --- F1（P1 回归）：批量必须从建批次那一刻的**不可变快照**规划，实时(刷新后)行绝不
//     泄漏进来 -----------------------------------------------------------------------
// 缺陷（回归）：run 阶段的 stale 修复会在批量弹窗**仍开着**时重载父表 detail。批量条目
// 保留旧 originalMd，但 planReformatSaves 若读**刷新后**的 allRows，就会拿另一客户端的
// **新值**当非展示兄弟的 expected_before 基线——带守卫的写据此判 fresh，用旧文算出的候选
// 覆盖掉那次更新（正是守卫要挡的一类）。下面这条纯逻辑测试锚定「基线完全由传入 rows
// 决定」这一事实——由此推出 runSave **必须**传建批次快照而非实时 props（接线由
// knowhow-panel.tsx 的 snapshotRef 源码 pin 守卫，见下方「接线（F1，不可变快照）」）。
test("planReformatSaves（F1 快照动机）：合并共享格 writeTargets 的 expected_before 基线取自**传入的 rows**——传实时(刷新后)行会把他人新值当基线", () => {
  const changed = [
    { rowId: "r1", columnId: "c-shared", columnName: "共享说明", originalMd: "旧共享说明", status: "changed", candidateMd: "新共享说明", source: "llm" },
  ];
  // (a) 传建批次快照（r1/r2/r3 都还是「旧共享说明」）——writeTargets 基线都是它。
  const snapshotUnits = planReformatSaves(changed, F1_SHARED_ROWS, "c-anchor");
  for (const t of snapshotUnits[0].writeTargets) assert.strictEqual(t.originalMd, "旧共享说明");
  // (b) 刷新后另一客户端把这个合并共享格整组统一改成新值（仍全分支同值 -> 仍是共享列，
  //     writeTargets 仍覆盖 r1/r2/r3）。若用这份实时行规划，基线就变成**他人的新值**——
  //     这正是 P1：带守卫的写会拿它当 expected_before、判 fresh、用旧文候选覆盖他人编辑。
  const refreshedRows = F1_SHARED_ROWS.map((r) => ({
    ...r,
    cells: { ...r.cells, "c-shared": "他人刚统一改的新值" },
  }));
  const liveUnits = planReformatSaves(changed, refreshedRows, "c-anchor");
  assert.deepStrictEqual(liveUnits[0].writeTargets.map((t) => t.rowId).sort(), ["r1", "r2", "r3"]);
  for (const t of liveUnits[0].writeTargets) assert.strictEqual(t.originalMd, "他人刚统一改的新值");
  // 结论：writeTargets 基线完全由传入 rows 决定，故 runSave 必须传建批次快照、批量生命
  // 周期内绝不读实时 allRows（否则 mid-flight 重载后就复现 (b) 的覆盖）。
});

// --- F1（P1 续）：批量保存的**扇出**也必须同源于冻结快照。上一处修复冻结了「规划」
//     （写目标/基线取自 snapshotRef 快照），但保存循环仍调最新的 handleCellSave 闭包——而它
//     从**实时** detail 重算合并共享组的扇出。detail 在弹窗打开期间刷新、某 anchor 组分裂时，
//     规划校验的是旧成员、回调却按新分组只扇写代表行，循环仍把旧成员整组标 saved = 漏写却
//     报成功。修复：runSave 把 unit.writeTargets 的行 id 作显式 targetRowIds 下发，handleCellSave
//     照用、不重算。下面这条纯逻辑测试先坐实「实时重算会漂移成只写代表行」，再证明「下发
//     unit.writeTargets 的行 id 则整组不漂移」——handleCellSave 照用显式写目标由 knowhow-panel.tsx
//     源码 pin 守卫（见下方「接线（P1，扇出同源）」）。
test("端到端（保存循环，P1）：批量保存把**快照**写目标行 id 下发给 onSaveCell——实时组分裂也不漂移成只写代表行", async () => {
  let state = buildSavingState("table", F1_SHARED_ROWS, F1_COLUMNS, (item) =>
    // c-shared 全组同值 -> 合并共享 unit；c-anchor/c-branch 无需改动。
    item.columnId === "c-shared"
      ? { candidateMd: "新共享说明", source: "llm", changed: true }
      : { candidateMd: item.originalMd, source: "rule/no-llm", changed: false },
  );
  const changed = state.items.filter((item) => item.status === "changed");
  // units 取自**建批次那一刻的快照**（r1/r2/r3 同组共享列）——写目标覆盖整组。
  const units = planReformatSaves(changed, F1_SHARED_ROWS, "c-anchor");
  assert.strictEqual(units.length, 1);
  assert.deepStrictEqual(units[0].writeTargets.map((t) => t.rowId).slice().sort(), ["r1", "r2", "r3"]);

  // 建 unit 之后，父表 detail 刷新：他人把 r2/r3 的 anchor 改走，该组**分裂**（只剩 r1
  // 仍是「示波器」）。若 handleCellSave 从这份实时行重算扇出，合并共享组坍缩成单行 ->
  // 只写代表行 r1（正是 P1：规划的旧成员 r2/r3 漏写，却被循环整组标 saved）。
  const liveRowsAfterSplit = F1_SHARED_ROWS.map((r) =>
    r.id === "r1" ? r : { ...r, cells: { ...r.cells, "c-anchor": `另一概念-${r.id}` } },
  );
  const liveGroup = groupRowsByAnchor(liveRowsAfterSplit, "c-anchor").find((g) => g.rows.some((x) => x.id === "r1"));
  const buggyLiveFanout =
    liveGroup && isSharedColumn(liveGroup, "c-shared") ? groupCellWriteTargets(liveGroup, "c-shared") : ["r1"];
  assert.deepStrictEqual(buggyLiveFanout, ["r1"]); // 实时重算漂移：只写代表行

  // 修复后的保存循环接线（镜像 runSave）：把 unit.writeTargets 的行 id 作第 5 参
  // 显式下发。捕获传给 onSaveCell 的写目标，验证它=**快照**全组、不随实时分裂漂移。
  const calls = [];
  const onSaveCell = async (rowId, columnId, contentMd, expectedByRowId, targetRowIds) => {
    calls.push({ rowId, targetRowIds, expectedKeys: [...expectedByRowId.keys()] });
  };
  for (const unit of units) {
    const rep = unit.representative;
    const expectedByRowId = new Map(unit.writeTargets.map((t) => [t.rowId, t.originalMd]));
    await onSaveCell(rep.rowId, rep.columnId, rep.candidateMd ?? "", expectedByRowId, unit.writeTargets.map((t) => t.rowId));
  }
  assert.strictEqual(calls.length, 1);
  // 关键：下发的是**快照**全组（r1/r2/r3），不是实时分裂后的 [r1]——扇写整组，报 saved 名副其实。
  assert.deepStrictEqual(calls[0].targetRowIds.slice().sort(), ["r1", "r2", "r3"]);
  // 基线也逐行随行（服务端事务内逐行比对；真陈旧 -> 409 -> 既有 stale-skip）。
  assert.deepStrictEqual(calls[0].expectedKeys.slice().sort(), ["r1", "r2", "r3"]);
});

// ===========================================================================
// 7. 批量保存：保存前比对当前落库内容，拒绝覆盖他人编辑（代码评审 F2）
// ===========================================================================
//
// 建批次到点「确认保存」之间（可能几分钟），另一个标签页/用户可能改了某格。
// 直接把基于旧内容算出的候选 PATCH 下去会静默覆盖那次更新。修复：保存前
// refetch 整表一次，逐格把当前落库内容与 originalMd 快照比对，不一致就跳过
// 该 save unit（distinct 终态 stale_skipped + 友好文案），计入 summary，其余
// 照常保存。

test("BATCH_REFORMAT_STALE_SKIP_TEXT: 友好文案逐字一致（不暴露枚举/技术词）", () => {
  assert.strictEqual(BATCH_REFORMAT_STALE_SKIP_TEXT, "内容已被其他人修改，已跳过——请重新运行规整");
  // 不含原始枚举/技术标识。
  assert.ok(!BATCH_REFORMAT_STALE_SKIP_TEXT.includes("stale"));
  assert.ok(!BATCH_REFORMAT_STALE_SKIP_TEXT.includes("skip"));
});

test("reformatSaveCellKey: 二元组稳定键，(行,列) 唯一、拼接无歧义", () => {
  assert.strictEqual(reformatSaveCellKey("r1", "c-a"), reformatSaveCellKey("r1", "c-a"));
  assert.notStrictEqual(reformatSaveCellKey("r1", "c-a"), reformatSaveCellKey("r2", "c-a"));
  assert.notStrictEqual(reformatSaveCellKey("r1", "c-a"), reformatSaveCellKey("r1", "c-b"));
  // 拼接歧义安全（同 reformatDedupeKey 的理由）。
  assert.notStrictEqual(reformatSaveCellKey("a", "b:c"), reformatSaveCellKey("a:b", "c"));
});

test("isReformatUnitStale: 写目标当前落库内容 ≠ originalMd → stale", () => {
  const unit = {
    representative: { rowId: "r1", columnId: "c-a", originalMd: "旧内容" },
    members: [{ rowId: "r1", columnId: "c-a", originalMd: "旧内容" }],
    writeTargets: [{ rowId: "r1", columnId: "c-a", originalMd: "旧内容" }],
  };
  const current = new Map([[reformatSaveCellKey("r1", "c-a"), "别人改成的新内容"]]);
  assert.strictEqual(isReformatUnitStale(unit, current), true);
});

test("isReformatUnitStale: 写目标当前落库内容 == originalMd → fresh（照常保存）", () => {
  const unit = {
    representative: { rowId: "r1", columnId: "c-a", originalMd: "旧内容" },
    members: [{ rowId: "r1", columnId: "c-a", originalMd: "旧内容" }],
    writeTargets: [{ rowId: "r1", columnId: "c-a", originalMd: "旧内容" }],
  };
  const current = new Map([[reformatSaveCellKey("r1", "c-a"), "旧内容"]]);
  assert.strictEqual(isReformatUnitStale(unit, current), false);
});

test("isReformatUnitStale: 写目标键在 refetch 里缺失（格子被删/清空）→ 保守判 stale", () => {
  const unit = {
    representative: { rowId: "r1", columnId: "c-a", originalMd: "旧内容" },
    members: [{ rowId: "r1", columnId: "c-a", originalMd: "旧内容" }],
    writeTargets: [{ rowId: "r1", columnId: "c-a", originalMd: "旧内容" }],
  };
  assert.strictEqual(isReformatUnitStale(unit, new Map()), true);
});

test("isReformatUnitStale: 合并共享组任一写目标被改 → 整组 stale（扇写会覆盖整组，不能盲写）", () => {
  const unit = {
    representative: { rowId: "r1", columnId: "c-shared", originalMd: "旧共享说明" },
    members: [{ rowId: "r1", columnId: "c-shared", originalMd: "旧共享说明" }],
    writeTargets: [
      { rowId: "r1", columnId: "c-shared", originalMd: "旧共享说明" },
      { rowId: "r2", columnId: "c-shared", originalMd: "旧共享说明" },
      { rowId: "r3", columnId: "c-shared", originalMd: "旧共享说明" },
    ],
  };
  // 只有 r2 被别人改了，r1/r3 仍是旧值——整组仍应判 stale。
  const current = new Map([
    [reformatSaveCellKey("r1", "c-shared"), "旧共享说明"],
    [reformatSaveCellKey("r2", "c-shared"), "r2 被别人改了"],
    [reformatSaveCellKey("r3", "c-shared"), "旧共享说明"],
  ]);
  assert.strictEqual(isReformatUnitStale(unit, current), true);
});

test("markReformatItemStaleSkipped: saving 阶段 changed -> stale_skipped（带友好文案）", () => {
  const state = batchStateOf("saving", [makeItem("r1", "c-a", "changed", { candidateMd: "新" })]);
  const next = markReformatItemStaleSkipped(state, "r1", "c-a", BATCH_REFORMAT_STALE_SKIP_TEXT);
  assert.strictEqual(next.items[0].status, "stale_skipped");
  assert.strictEqual(next.items[0].errorMessage, BATCH_REFORMAT_STALE_SKIP_TEXT);
});

test("markReformatItemStaleSkipped: 非 saving 阶段原样返回（phase 总闸）", () => {
  const state = batchStateOf("reviewing", [makeItem("r1", "c-a", "changed")]);
  assert.strictEqual(markReformatItemStaleSkipped(state, "r1", "c-a", "x"), state);
});

test("markReformatItemStaleSkipped: F3 —— saving -> stale_skipped（preflight 通过后 409，成员已 changed->saving，须结算）", () => {
  // 服务端事务内比对判他人已改（409）时，unit 成员早已被 markReformatItemSaving
  // 翻成 saving。旧实现只认 changed -> 对 saving 是 no-op，格子永远卡在「保存中」、
  // 也不计入 stale_skipped。扩展前置状态到 saving 后能正确结算。
  const state = batchStateOf("saving", [makeItem("r1", "c-a", "saving")]);
  const next = markReformatItemStaleSkipped(state, "r1", "c-a", BATCH_REFORMAT_STALE_SKIP_TEXT);
  assert.strictEqual(next.items[0].status, "stale_skipped");
  assert.strictEqual(next.items[0].errorMessage, BATCH_REFORMAT_STALE_SKIP_TEXT);
});

test("markReformatItemStaleSkipped: 已达终态（saved）不被重开 -> 原样返回", () => {
  // 前置状态只放行 changed / saving 这两个「保存进行中」的态；已结算的终态
  // （saved/save_error/stale_skipped）不得被再次翻动。
  const state = batchStateOf("saving", [makeItem("r1", "c-a", "saved")]);
  assert.strictEqual(markReformatItemStaleSkipped(state, "r1", "c-a", "x"), state);
});

test("reformatBatchSummary: 计入 stale_skipped", () => {
  const state = batchStateOf("done", [
    makeItem("r1", "c-a", "saved"),
    makeItem("r2", "c-a", "stale_skipped", { errorMessage: BATCH_REFORMAT_STALE_SKIP_TEXT }),
  ]);
  const summary = reformatBatchSummary(state);
  assert.strictEqual(summary.saved, 1);
  assert.strictEqual(summary.stale_skipped, 1);
});

test("端到端（保存循环，F1+F2）：stale unit 被跳过（不调 onSaveCell）、fresh unit 照常保存，计数相加", () => {
  // 两个独立概念组各一行（互不合并）：r1 的 c-shared 被别人改了（stale），
  // r2 的 c-shared 没被动过（fresh）。
  const rows = [
    { id: "r1", position: 0, cells: { "c-anchor": "示波器", "c-shared": "旧甲" } },
    { id: "r2", position: 1, cells: { "c-anchor": "万用表", "c-shared": "旧乙" } },
  ];
  const columns = [
    { id: "c-anchor", name: "概念", position: 0 },
    { id: "c-shared", name: "共享说明", position: 1 },
  ];
  let state = buildSavingState("table", rows, columns, (item) =>
    item.columnId === "c-shared"
      ? { candidateMd: `${item.originalMd}-规整`, source: "llm", changed: true }
      : { candidateMd: item.originalMd, source: "rule/no-llm", changed: false },
  );
  const changed = state.items.filter((item) => item.status === "changed");
  assert.strictEqual(changed.length, 2);
  const units = planReformatSaves(changed, rows, "c-anchor");

  // 模拟保存前的整表 refetch：r1/c-shared 被外部改成了 "旧甲-别人的新版"。
  const current = new Map([
    [reformatSaveCellKey("r1", "c-shared"), "旧甲-别人的新版"], // ≠ 快照 "旧甲" → stale
    [reformatSaveCellKey("r2", "c-shared"), "旧乙"], // == 快照 → fresh
  ]);

  let saveCalls = 0;
  for (const unit of units) {
    if (isReformatUnitStale(unit, current)) {
      state = unit.members.reduce(
        (s, m) => markReformatItemStaleSkipped(s, m.rowId, m.columnId, BATCH_REFORMAT_STALE_SKIP_TEXT),
        state,
      );
      continue; // 不调 onSaveCell
    }
    saveCalls += 1;
    state = unit.members.reduce((s, m) => markReformatItemSaving(s, m.rowId, m.columnId), state);
    state = unit.members.reduce((s, m) => applyReformatSaveSuccess(s, m.rowId, m.columnId), state);
  }
  state = finishReformatBatchSave(state);

  assert.strictEqual(saveCalls, 1); // 只有 fresh 的 r2 真正保存
  const summary = reformatBatchSummary(state);
  assert.strictEqual(summary.saved, 1);
  assert.strictEqual(summary.stale_skipped, 1);
  // 计数相加：确认那一刻 2 个 changed，最终 = 1 saved + 1 stale_skipped。
  assert.strictEqual(summary.saved + summary.stale_skipped, 2);
  // stale 的 r1/c-shared 带上了友好文案（供逐格明细展示）。按 (行,列) 精确定位
  // ——r1 同时还有一个 c-anchor 条目（unchanged），只按 rowId 找会命中错的那个。
  const r1Shared = state.items.find((i) => i.rowId === "r1" && i.columnId === "c-shared");
  assert.strictEqual(r1Shared.status, "stale_skipped");
  assert.strictEqual(r1Shared.errorMessage, BATCH_REFORMAT_STALE_SKIP_TEXT);
});

test("端到端（保存循环，F3）：preflight 通过后 onSaveCell 抛 409——已 saving 的整组成员结算为 stale_skipped、循环继续保存其余 unit", () => {
  // 与上面的「保存前 preflight 跳过」不同：这里 preflight 全 fresh，unit 成员已被
  // markReformatItemSaving 翻成 saving，随后服务端事务内比对判他人已改（409）。旧
  // 实现的 markReformatItemStaleSkipped 只认 changed -> 对 saving 是 no-op，整组卡在
  // 「保存中」、且不计入 stale_skipped。修复后 saving 也能结算。含扇出（3 成员一组）。
  const rows = [
    { id: "r1", position: 0, cells: { "c-anchor": "示波器", "c-shared": "旧共享" } },
    { id: "r2", position: 1, cells: { "c-anchor": "示波器", "c-shared": "旧共享" } },
    { id: "r3", position: 2, cells: { "c-anchor": "示波器", "c-shared": "旧共享" } },
    { id: "r4", position: 3, cells: { "c-anchor": "万用表", "c-shared": "另一说明" } },
  ];
  const columns = [
    { id: "c-anchor", name: "概念", position: 0 },
    { id: "c-shared", name: "共享说明", position: 1 },
  ];
  let state = buildSavingState("table", rows, columns, (item) =>
    item.columnId === "c-shared"
      ? { candidateMd: `${item.originalMd}-规整`, source: "llm", changed: true }
      : { candidateMd: item.originalMd, source: "rule/no-llm", changed: false },
  );
  const changed = state.items.filter((item) => item.status === "changed");
  const units = planReformatSaves(changed, rows, "c-anchor");
  assert.strictEqual(units.length, 2); // 示波器组(3 兄弟合一) + 万用表组(1)

  let saveCalls = 0;
  for (const [idx, unit] of units.entries()) {
    // 真实 runSave：先把 unit 成员整组标 saving，再 await onSaveCell(representative)。
    state = unit.members.reduce((s, m) => markReformatItemSaving(s, m.rowId, m.columnId), state);
    saveCalls += 1;
    if (idx === 0) {
      // 409 分支：把已 saving 的成员整组结算为 stale_skipped（不是 save_error），
      // 单 unit 跳过不阻断其余 unit（循环继续）。
      state = unit.members.reduce(
        (s, m) => markReformatItemStaleSkipped(s, m.rowId, m.columnId, BATCH_REFORMAT_STALE_SKIP_TEXT),
        state,
      );
    } else {
      state = unit.members.reduce((s, m) => applyReformatSaveSuccess(s, m.rowId, m.columnId), state);
    }
  }
  state = finishReformatBatchSave(state);

  const summary = reformatBatchSummary(state);
  assert.strictEqual(saveCalls, 2);
  assert.strictEqual(summary.saving, 0); // 关键：没有格子卡在「保存中」
  assert.strictEqual(summary.stale_skipped, 3); // 409 那组的 3 个扇出成员全部结算
  assert.strictEqual(summary.saved, 1); // 第二组保存成功——循环没被 409 阻断
  for (const rid of ["r1", "r2", "r3"]) {
    const item = state.items.find((i) => i.rowId === rid && i.columnId === "c-shared");
    assert.strictEqual(item.status, "stale_skipped");
    assert.strictEqual(item.errorMessage, BATCH_REFORMAT_STALE_SKIP_TEXT);
  }
});

// ===========================================================================
// 8. 运行阶段：source_md 比对（并发编辑防护 · 代码评审 P1-a）
// ===========================================================================
//
// /reformat 读的是 LIVE 落库格子；建批次到某一格真正发请求之间，别的标签页/
// 用户可能已改了它。后端回带 source_md（它实际读到并喂给 reformat_cell 的原文），
// 前端把它与建批次那一刻的 originalMd 快照比对：不一致 → 这个候选是拿「客户端
// 从没见过的内容」算出来的，该格直接 stale_skipped，且**不进去重缓存**（否则它的
// 重复格会复用一份基于陌生内容的候选——正是本修复要堵的洞）。

test("reformatResultIsStale: sourceMd 等于 originalMd 快照 → 非陈旧（false）", () => {
  assert.strictEqual(reformatResultIsStale("原文内容", "原文内容"), false);
});

test("reformatResultIsStale: sourceMd 与 originalMd 快照不一致 → 陈旧（true，含仅空白差异也算）", () => {
  assert.strictEqual(reformatResultIsStale("原文内容", "原文内容（他人已改）"), true);
  assert.strictEqual(reformatResultIsStale("foo", "foo "), true); // 逐字比对，非 trim
});

test("markReformatItemRunStale: running + running 项 -> stale_skipped(带友好文案)", () => {
  const state = batchStateOf("running", [
    makeItem("r1", "c-a", "running", { originalMd: "foo" }),
    makeItem("r2", "c-a", "pending", { originalMd: "foo" }),
  ]);
  const next = markReformatItemRunStale(state, "r1", "c-a", BATCH_REFORMAT_STALE_SKIP_TEXT);
  const item = next.items.find((i) => i.rowId === "r1" && i.columnId === "c-a");
  assert.strictEqual(item.status, "stale_skipped");
  assert.strictEqual(item.errorMessage, BATCH_REFORMAT_STALE_SKIP_TEXT);
  // 不带候选内容——它永远不会进入保存集（同 unchanged/reformat_error）。
  assert.strictEqual(item.candidateMd, undefined);
  // 其余项不受影响（r2 仍 pending，会各自独立处理）。
  assert.strictEqual(next.items.find((i) => i.rowId === "r2").status, "pending");
});

test("markReformatItemRunStale: 非 running 阶段原样返回（迟到结果不复活队列）", () => {
  const reviewing = batchStateOf("reviewing", [makeItem("r1", "c-a", "running", { originalMd: "foo" })]);
  assert.strictEqual(markReformatItemRunStale(reviewing, "r1", "c-a", BATCH_REFORMAT_STALE_SKIP_TEXT), reviewing);
});

test("markReformatItemRunStale: 目标项不是 running（如已 changed）时原样返回", () => {
  const state = batchStateOf("running", [makeItem("r1", "c-a", "changed", { originalMd: "foo", candidateMd: "x" })]);
  assert.strictEqual(markReformatItemRunStale(state, "r1", "c-a", BATCH_REFORMAT_STALE_SKIP_TEXT), state);
});

test("markReformatItemRunStale: 未知 (rowId,columnId) 原样返回", () => {
  const state = batchStateOf("running", [makeItem("r1", "c-a", "running")]);
  assert.strictEqual(markReformatItemRunStale(state, "r9", "c-9", BATCH_REFORMAT_STALE_SKIP_TEXT), state);
});

// ===========================================================================
// 9. 批量保存收尾：stale 跳过后把新鲜表传回父级（代码评审 F）
// ===========================================================================
//
// 缺陷：runSave 的 preflight refetch 只喂了本地的 staleness 比对——当有格子被
// stale 跳过（preflight 命中 或 409）时，父级 detail 仍是旧快照，关掉重开会用
// **同一陈旧基线**（detail.rows -> originalMd）复跑、那些格子永远跳过，尽管文案
// 说「请重新运行规整」。修复：保存循环收尾时若发生过 stale 跳过，回调 onStaleReload
// 请父级刷新整表（reloadTableDetail），下次批量用新鲜基线。判定纯化成谓词 +
// runSave 接线源码 pin。

test("reformatBatchSaveNeedsRefresh: 保存阶段跳过过 stale（count>0）→ true（须刷新父表）", () => {
  assert.strictEqual(reformatBatchSaveNeedsRefresh(1), true);
  assert.strictEqual(reformatBatchSaveNeedsRefresh(3), true);
});

test("reformatBatchSaveNeedsRefresh: 无 stale 跳过（count===0）→ false（避免无谓 refetch）", () => {
  assert.strictEqual(reformatBatchSaveNeedsRefresh(0), false);
});

// F1：run 阶段 sourceMd 陈旧跳过的格子从没进保存集——之前只有保存阶段的 stale 才刷父表，
// 于是这些格子的父表快照永不刷新、关掉重开永远跳过。run 阶段同款谓词决定是否刷新。
test("reformatBatchRunNeedsRefresh: run 阶段跳过过 stale（count>0）→ true（须刷新父表）", () => {
  assert.strictEqual(reformatBatchRunNeedsRefresh(1), true);
  assert.strictEqual(reformatBatchRunNeedsRefresh(4), true);
});

test("reformatBatchRunNeedsRefresh: 无 run 阶段 stale 跳过（count===0）→ false", () => {
  assert.strictEqual(reformatBatchRunNeedsRefresh(0), false);
});

// --- 接线守卫（F）：runSave 必须在两个 stale 跳过分支各累加一次计数，并在收尾按
//     谓词回调 onStaleReload。.tsx 在本仓库 node:test 模型里只能源码断言（同
//     knowhow-cell-editor.test.mjs 的既有做法）。切到 runSave 函数块再断言，不让
//     [\s\S]*? 越过块边界。------------------------------------------------------------

const panelSrc = await readFile(new URL("./knowhow-panel.tsx", import.meta.url), "utf8");
const runSaveStart = panelSrc.indexOf("async function runSave()");
// 注意：`function requestClose()` 在本文件出现两次（另一个组件也有一个）——必须从
// runSave 之后开始找它自己后面那个，否则切到的是 runSave **之前**的那一个、区间空。
const runSaveSrc = panelSrc.slice(runSaveStart, panelSrc.indexOf("function requestClose()", runSaveStart));
// F1：runBatch 函数块（run 阶段收尾也要刷父表）。`function handleAbort()` 紧随 runBatch，
// 是稳定的块尾锚点——切到它之前，避免 [\s\S]*? 越过块边界读到别的收尾。
const runBatchStart = panelSrc.indexOf("async function runBatch()");
const runBatchSrc = panelSrc.slice(runBatchStart, panelSrc.indexOf("function handleAbort()", runBatchStart));

test("接线（F）：runSave 声明保存阶段 stale 计数（不从 summary 反推，避免掺入 run 阶段来源）", () => {
  assert.ok(runSaveSrc.length > 0, "runSave 函数体没截到，守卫失效");
  assert.match(runSaveSrc, /let saveStaleSkipCount = 0;/);
});

test("接线（F）：preflight 跳过分支累加计数（在 continue 之前）", () => {
  // 保存前比对命中 -> markReformatItemStaleSkipped 整组 + 计数 + continue。切到
  // `if (currentByKey && isReformatUnitStale...` 这个块，断言块内既 mark 又累加。
  const preflightStart = runSaveSrc.indexOf("if (currentByKey && isReformatUnitStale(unit, currentByKey)) {");
  const preflightSrc = runSaveSrc.slice(preflightStart, runSaveSrc.indexOf("continue;", preflightStart));
  assert.ok(preflightStart > 0 && preflightSrc.length > 0, "preflight 跳过块没截到");
  assert.match(preflightSrc, /markReformatItemStaleSkipped/);
  assert.match(preflightSrc, /saveStaleSkipCount \+= 1;/);
});

test("接线（F）：409 跳过分支累加计数（在 else 之前——不把普通保存失败算进 stale）", () => {
  // 409 分支：markReformatItemStaleSkipped 整组 + 计数；紧邻的 else 是 save_error
  // （**不**累加）。切到 `if (httpErrorStatus(err) === 409) {` 到它自己的 `} else {`。
  const s409Start = runSaveSrc.indexOf("if (httpErrorStatus(err) === 409) {");
  const s409Src = runSaveSrc.slice(s409Start, runSaveSrc.indexOf("} else {", s409Start));
  assert.ok(s409Start > 0 && s409Src.length > 0, "409 跳过块没截到");
  assert.match(s409Src, /markReformatItemStaleSkipped/);
  assert.match(s409Src, /saveStaleSkipCount \+= 1;/);
  // save_error 分支绝不能累加（否则普通保存失败也会误触发父表刷新）。
  const elseStart = runSaveSrc.indexOf("} else {", s409Start);
  const elseSrc = runSaveSrc.slice(elseStart, runSaveSrc.indexOf("\n      }", elseStart));
  assert.match(elseSrc, /applyReformatSaveError/);
  assert.doesNotMatch(elseSrc, /saveStaleSkipCount/);
});

test("接线（F）：save 阶段收尾在 mounted 块内按谓词经 requestStaleReload 刷父表（恰一次，无跳过则不调）", () => {
  // 收尾块：finishReformatBatchSave + setBusy(false) + 按 reformatBatchSaveNeedsRefresh
  // 决定是否 requestStaleReload()。F1 后回调收敛到 requestStaleReload（恰一次闸），
  // 不再各自直调 onStaleReload。用紧致锚定这条收尾（只夹白空/单行注释，不跨块）。
  assert.match(
    runSaveSrc,
    /if \(reformatBatchSaveNeedsRefresh\(saveStaleSkipCount\)\) requestStaleReload\(\);/,
  );
  // 谓词回调必须在 finishReformatBatchSave 之后（收尾块内），不是循环体里逐格触发。
  const finishIdx = runSaveSrc.indexOf("finishReformatBatchSave(state)");
  const notifyIdx = runSaveSrc.indexOf("reformatBatchSaveNeedsRefresh(saveStaleSkipCount)");
  assert.ok(finishIdx > 0 && notifyIdx > finishIdx, "requestStaleReload 回调必须在收尾块内、循环之后");
});

// --- 接线守卫（F1）：run 阶段陈旧也要刷父表——runBatch 在陈旧分支累加计数、收尾按
//     谓词经 requestStaleReload 刷一次；两阶段都跳过时经同一个「恰一次」闸只刷一次。----

test("接线（F1）：runBatch 声明 run 阶段 stale 计数并在陈旧分支累加（在 continue 之前）", () => {
  assert.ok(runBatchSrc.length > 0, "runBatch 函数体没截到，守卫失效");
  assert.match(runBatchSrc, /let runStaleSkipCount = 0;/);
  // 陈旧分支：markReformatItemRunStale 整格 + 计数 + continue。切到 markReformatItemRunStale
  // 到它自己后面那个 continue（去重命中的 continue 在此分支之前，不会误截）。
  const staleStart = runBatchSrc.indexOf("markReformatItemRunStale");
  const staleSrc = runBatchSrc.slice(staleStart, runBatchSrc.indexOf("continue;", staleStart));
  assert.ok(staleStart > 0 && staleSrc.length > 0, "run 阶段陈旧分支没截到");
  assert.match(staleSrc, /runStaleSkipCount \+= 1;/);
});

test("接线（F1）：runBatch 收尾在 mounted 块内按谓词经 requestStaleReload 刷父表", () => {
  assert.match(runBatchSrc, /if \(reformatBatchRunNeedsRefresh\(runStaleSkipCount\)\) requestStaleReload\(\);/);
  // 刷新必须在 finishReformatBatchRun 之后（收尾块内），不是循环体里逐格触发。
  const finishIdx = runBatchSrc.indexOf("finishReformatBatchRun(state)");
  const notifyIdx = runBatchSrc.indexOf("reformatBatchRunNeedsRefresh(runStaleSkipCount)");
  assert.ok(finishIdx > 0 && notifyIdx > finishIdx, "requestStaleReload 回调必须在收尾块内、循环之后");
});

// --- 接线守卫（F1，P1 回归）：批量进行中**绝不**重载父表——run/save 阶段的 stale 跳过只
//     置 pendingReloadRef 标记，真正的 reloadTableDetail 延到弹窗**卸载（关闭）**才触发；
//     且批量生命周期内一切规划取自建批次那一刻的**不可变快照**（snapshotRef），实时
//     allRows/anchorColumnId 绝不泄漏进来（否则 mid-flight 重载后拿他人新值当 expected_before
//     覆盖那次编辑）。这两条合力堵住 P1。--------------------------------------------------

test("接线（F1，延迟重载）：requestStaleReload 只置 pendingReloadRef 标记，进行中**不**直调 onStaleReload", () => {
  // 批量进行中立即重载父表 = 用他人新值覆盖的口子（P1）。两阶段收尾都经 requestStaleReload，
  // 但它现在只置标记；真正的重载延到卸载 cleanup（关窗才刷）。
  assert.match(
    panelSrc,
    /function requestStaleReload\(\) \{\s*pendingReloadRef\.current = true;\s*\}/,
  );
  // requestStaleReload 函数体内**不**出现 onStaleReload（那会 mid-flight 立即重载）。
  const reqStart = panelSrc.indexOf("function requestStaleReload()");
  const reqSrc = panelSrc.slice(reqStart, panelSrc.indexOf("}", reqStart) + 1);
  assert.ok(reqStart > 0 && reqSrc.length > 0, "requestStaleReload 函数体没截到");
  assert.doesNotMatch(reqSrc, /onStaleReload/);
  // 两个收尾仍都走 requestStaleReload（置标记），不各自直调 onStaleReload。
  assert.ok(runBatchSrc.includes("requestStaleReload()"), "runBatch 收尾必须经 requestStaleReload");
  assert.ok(runSaveSrc.includes("requestStaleReload()"), "runSave 收尾必须经 requestStaleReload");
});

test("接线（F1，延迟重载）：真正的 onStaleReload 只在**卸载 cleanup** 里按 pendingReloadRef 触发（关窗才重载、恰一次、无跳过则不调）", () => {
  // 卸载 cleanup 是唯一消费点：pendingReloadRef 为真才调 onStaleReload。所有关闭路径
  // （X/背景/Esc/父级清状态）都汇于卸载 -> 天然「恰一次」；没跳过时标记为假 -> 不调（no stale
  // -> no reload）；进行中永不卸载 -> 零重载（mid-flight -> zero reloads）。
  // `mountedRef.current = true;` 在本文件出现两次（KnowhowRowOptimizeModal 也有一个）——
  // 必须从 KnowhowReformatBatchModal 函数之后开始找它自己那个，否则截到的是别的 modal。
  const batchModalStart = panelSrc.indexOf("function KnowhowReformatBatchModal(");
  const mountStart = panelSrc.indexOf("mountedRef.current = true;", batchModalStart);
  const mountEffectSrc = panelSrc.slice(mountStart, panelSrc.indexOf("}, []);", mountStart));
  assert.ok(batchModalStart > 0 && mountStart > batchModalStart && mountEffectSrc.length > 0, "批量弹窗的挂载 effect 没截到");
  assert.match(mountEffectSrc, /if \(pendingReloadRef\.current\) onStaleReload\?\.\(\);/);
  // 消费点必须在 cleanup（return () => {…}）内、mountedRef.current = false 之后。
  const cleanupIdx = mountEffectSrc.indexOf("return () =>");
  const falseIdx = mountEffectSrc.indexOf("mountedRef.current = false;");
  const fireIdx = mountEffectSrc.indexOf("if (pendingReloadRef.current) onStaleReload");
  assert.ok(cleanupIdx > 0 && falseIdx > cleanupIdx && fireIdx > falseIdx, "onStaleReload 必须在卸载 cleanup 内、mountedRef=false 之后触发");
});

test("接线（F1，延迟重载）：onStaleReload?.() 在整个面板里**只有一个调用点**（卸载 cleanup）——进行中零重载", () => {
  // run/save 收尾都只 requestStaleReload（置标记），全组件里真正的 onStaleReload?.() 调用
  // 只出现一次（卸载 cleanup）。若哪天有人又在收尾直调 onStaleReload，这条即变红。
  const invocations = panelSrc.split("onStaleReload?.()").length - 1;
  assert.strictEqual(invocations, 1, "onStaleReload?.() 只应在卸载 cleanup 出现一次");
});

test("接线（F1，不可变快照）：建批次定格 snapshotRef，runSave 用它规划（不读实时 allRows/anchorColumnId）", () => {
  // P1 根因：进行中重载后 allRows 变新，planReformatSaves 拿他人新值当 expected_before。
  // 修复：建批次（组件挂载）时用 useRef 定格 {allRows, anchorColumnId} 一次，之后 props 变
  // 化不改它；runSave 的规划全取自 snapshotRef.current。
  assert.match(panelSrc, /const snapshotRef = useRef\(\{ allRows, anchorColumnId \}\);/);
  assert.match(
    runSaveSrc,
    /const units = planReformatSaves\(changed, snapshotRef\.current\.allRows, snapshotRef\.current\.anchorColumnId\);/,
  );
  // 绝不再从实时 props 规划（否则重载后的行泄漏进进行中的批次 -> 复现 P1）。
  assert.doesNotMatch(runSaveSrc, /planReformatSaves\(changed, allRows, anchorColumnId\)/);
});

test("接线（F）：两处 KnowhowReformatBatchModal 都传 onStaleReload={reloadTableDetail}", () => {
  // 行作用域 + 表作用域两个渲染点都要接线，否则一处漏了就复现「关掉重开永远跳过」。
  const occurrences = panelSrc.split("onStaleReload={reloadTableDetail}").length - 1;
  assert.strictEqual(occurrences, 2, "两个 KnowhowReformatBatchModal 渲染点都要传 onStaleReload");
  // reloadTableDetail 是带 stale-guard 的 loadDetail 复用（单一真源，见 detailRequestRef）。
  assert.match(panelSrc, /const reloadTableDetail = useCallback\(\(\) => \{\s*if \(selectedTableId\) loadDetail\(selectedTableId\);\s*\}, \[selectedTableId, loadDetail\]\);/);
});

test("接线（F2）：编辑器渲染点把 onServerStale 也接到同一个 reloadTableDetail", () => {
  // 单格 server-stale 与批量 stale 跳过共用父级刷新路径（reloadTableDetail），
  // 避免两套漂移。
  assert.match(panelSrc, /onServerStale=\{reloadTableDetail\}/);
});

// --- 接线守卫（P1，扇出同源）：批量保存的**扇出**必须取自冻结快照的写目标，不从实时
//     detail 重算合并共享组——否则规划（snapshotRef 冻结）与扇写（实时闭包）漂移：detail
//     刷新致某组分裂时，扇写只落代表行、循环却整组标 saved（漏写报成功，P1）。三处接线：
//     ① handleCellSave 接受可选 targetRowIds、提供时**照用**、缺省时退回实时分组重算（手动
//     路径不变）；② runSave 把 unit.writeTargets 的行 id 作第 5 参下发；③ 批量弹窗 props 的
//     onSaveCell 签名带上它。.tsx 在本仓库 node:test 模型里只能源码断言（同既有「接线」守卫）。--
const handleCellSaveStart = panelSrc.indexOf("async function handleCellSave(");
// openCodeModal 紧随 handleCellSave，是稳定的块尾锚点——切到它之前，避免 [\s\S]*? 越界。
const handleCellSaveSrc = panelSrc.slice(handleCellSaveStart, panelSrc.indexOf("function openCodeModal(", handleCellSaveStart));

test("接线（P1，扇出同源）：handleCellSave 签名带可选 targetRowIds（第 5 参）+ anchorGuard（第 6 参，round-4）", () => {
  assert.ok(handleCellSaveStart > 0 && handleCellSaveSrc.length > 0, "handleCellSave 函数体没截到，守卫失效");
  assert.match(
    handleCellSaveSrc,
    /async function handleCellSave\(\s*rowId: string,\s*columnId: string,\s*contentMd: string,\s*expectedByRowId\?: Map<string, string>,\s*targetRowIds\?: string\[\],\s*anchorGuard\?: \{ anchorColumnId: string; expectedAnchorByRowId: Map<string, string> \},\s*\)/,
  );
});

test("接线（P1，扇出同源）：提供 targetRowIds 时**照用**（不重算）；缺省时退回实时分组重算（手动路径不变）", () => {
  // targets = targetRowIds ??（实时 group && isSharedColumn ? groupCellWriteTargets : [rowId]）：
  // 显式写目标优先、缺省退回既有实时分组——两条来源刻意分流，手动编辑语义逐字不变。
  assert.match(
    handleCellSaveSrc,
    /const targets =\s*targetRowIds \?\?\s*\(group && isSharedColumn\(group, columnId\) \? groupCellWriteTargets\(group, columnId\) : \[rowId\]\);/,
  );
});

test("接线（P1，扇出同源）：runSave 把 unit.writeTargets 的行 id 作第 5 参、anchorGuard 作第 6 参下发给 onSaveCell", () => {
  // units 取自 snapshotRef 冻结快照（见「接线（F1，不可变快照）」），此处把它的写目标行 id
  // 一并显式下发——handleCellSave 照用、不从实时 detail 重算，规划与扇写严格同源。
  assert.match(runSaveSrc, /const targetRowIds = unit\.writeTargets\.map\(\(t\) => t\.rowId\);/);
  assert.match(
    runSaveSrc,
    /await onSaveCell\(\s*rep\.rowId,\s*rep\.columnId,\s*rep\.candidateMd \?\? "",\s*expectedByRowId,\s*targetRowIds,\s*anchorGuard,\s*\);/,
  );
});

test("接线（P1 round-4/round-6）：runSave 从 snapshotRef 冻结快照建 anchorGuard——有 anchor 列**且** unit.coversAnchorGroup（整组写）才带；anchorMd 取自 writeTargets", () => {
  // anchor 基线守卫与写目标/originalMd 同源于 snapshotRef 冻结快照，绝不读实时 detail——
  // 否则弹窗打开期间 detail 刷新会把他人新 anchor 当基线（正是守卫要挡的漂移）。记录型表
  // （anchorColumnId=null）不带守卫。round-6：再叠一道 unit.coversAnchorGroup 门——只有「整组
  // 写」（合并共享列扇写 / 单例组）才带 anchorGuard；多行组的**非共享列**是有意子集写、带守卫会
  // 被服务端「组成员精确相等」误判 joiner 假 409，故 coversAnchorGroup=false 时不下发。
  assert.match(runSaveSrc, /const anchorColumnId = snapshotRef\.current\.anchorColumnId;/);
  assert.match(
    runSaveSrc,
    /const anchorGuard = anchorColumnId && unit\.coversAnchorGroup\s*\?\s*\{\s*anchorColumnId,\s*expectedAnchorByRowId: new Map\(unit\.writeTargets\.map\(\(t\) => \[t\.rowId, t\.anchorMd\]\)\),\s*\}\s*: undefined;/,
  );
});

test("接线（P1 round-4）：handleCellSave 的批量分支把 anchorGuard 展开成 anchorColumnId + expectedAnchor（按 targets 平行），单格分支不带", () => {
  // 批量端点（targets>1 的扇写，或 round-6 起带 anchorGuard 的单目标单例组）才带 anchor 守卫；
  // expectedAnchor 与 expectedBefore 一样按 targets 顺序平行、缺值退化 ""。单格 patchKnowhowCell
  // 分支刻意不接（无 anchorGuard 的手动编辑走到那里，无组可离）。
  assert.match(
    handleCellSaveSrc,
    /\.\.\.\(anchorGuard\s*\?\s*\{\s*anchorColumnId: anchorGuard\.anchorColumnId,\s*expectedAnchor: targets\.map\(\(rid\) => anchorGuard\.expectedAnchorByRowId\.get\(rid\) \?\? ""\),\s*\}\s*: \{\}\),/,
  );
});

test("接线（P1 round-6）：带 anchorGuard 的保存**恒**走 guarded 批量端点——单例组一个写目标也走（否则漏成员校验）", () => {
  // 根因：单例组建批次后有兄弟行 join，若还按 targets.length===1 走单格端点，服务端不做「组成员
  // 精确相等」，只写原行、悄悄把已共享的组留成不一致。修复：路由改成 targets.length>1 **或**
  // 带 anchorGuard——后者仅由 runSave 对「整组写」单元下发（coversAnchorGroup 门），故带上它就
  // 该走 guarded 批量端点。手动编辑（无 anchorGuard、无 targetRowIds）仍只在 targets.length>1
  // （合并共享格扇写）时走批量端点，单目标走单格端点，逐字不变。
  assert.ok(handleCellSaveStart > 0 && handleCellSaveSrc.length > 0, "handleCellSave 函数体没截到，守卫失效");
  assert.match(handleCellSaveSrc, /const useBatchEndpoint = targets\.length > 1 \|\| Boolean\(anchorGuard\);/);
  assert.match(handleCellSaveSrc, /const results = useBatchEndpoint\s*\?\s*await batchPatchKnowhowCells\(/);
  // 单格端点仍是另一分支的备选（无 anchorGuard 的手动编辑落到这里）。
  assert.ok(handleCellSaveSrc.includes("await patchKnowhowCell("), "单格端点分支仍在（无 anchorGuard 的手动编辑）");
});

test("接线（P1，扇出同源）：KnowhowReformatBatchModalProps.onSaveCell 签名带 targetRowIds + anchorGuard（批量总是下发）", () => {
  // prop 类型要求它（批量恒传，同 expectedByRowId：prop 必填、handleCellSave 实现放宽为可选）。
  assert.match(
    panelSrc,
    /onSaveCell: \(\s*rowId: string,\s*columnId: string,\s*contentMd: string,\s*expectedByRowId: Map<string, string>,\s*targetRowIds: string\[\],\s*anchorGuard\?: \{ anchorColumnId: string; expectedAnchorByRowId: Map<string, string> \},\s*\) => Promise<void>;/,
  );
});
