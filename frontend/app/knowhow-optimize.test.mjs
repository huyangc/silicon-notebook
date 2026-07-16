// knowhow-optimize-logic.ts 的纯逻辑单测（Task 9：模板+优化 UI）。The three
// consumer .tsx files (knowhow-cell-editor.tsx / knowhow-panel.tsx /
// knowhow-import.tsx) all contain JSX, so — mirroring every other
// knowhow-*-logic.ts split in this codebase — the testable state machines and
// display-mapping helpers live in knowhow-optimize-logic.ts and are imported
// directly here.
import test from "node:test";
import assert from "node:assert/strict";

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
} from "./knowhow-optimize-logic.ts";

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
