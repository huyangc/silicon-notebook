// Knowhow 表 — Task 9「模板+优化 UI」的纯逻辑（无 JSX，可被
// knowhow-optimize.test.mjs 直接 import）。knowhow-cell-editor.tsx /
// knowhow-panel.tsx / knowhow-import.tsx 含 JSX，Node 原生 TS 类型剥离不支持
// .tsx（仅 .ts/.mts/.cts 可被 node --test 直接 import），故把三块可测纯逻辑
// 集中在本文件（镜像既有 knowhow-panel.tsx <-> knowhow-panel-logic.ts 的拆分
// 方式）：
//
//   1. 单格「优化表达」对照状态机（规格③：格子浮窗内按钮 → 原文/建议对照 →
//      接受(填入编辑框)/放弃）——knowhow-cell-editor.tsx 消费。
//   2. 行详情抽屉「优化整行」批量入口的逐格顺序队列（规格③ + 任务简报：
//      非空格子排队、每格进度可见、接受经 patchKnowhowCell 落库后推进、
//      中止随时可用）——knowhow-panel.tsx 消费。
//   3. 追加导入（Excel 模板往返，规格②路B）预览的展示映射：重名标黄 +
//      未匹配列提示 + 模板下载文件名——knowhow-import.tsx / knowhow-panel.tsx
//      消费。
//
// 规格③ UI 文案在此逐字登记为导出常量（同 knowhow-cell-editor-logic.ts 的
// PROCEDURE_HINT_TEXT 等既有写法），组件侧只引用、不内联硬编码字符串。

import type { KnowhowAppendDuplicateTitle } from "./knowhow-model.ts";
import { hasUnsavedChanges } from "./knowhow-cell-editor-logic.ts";

// ===========================================================================
// 1. 单格「优化表达」对照状态机（规格③）
// ===========================================================================

// idle=未触发；loading=正在调用 optimizeKnowhowCell；ready=建议已到达，展示
// 原文/建议对照；error=调用失败，展示后端中文原文，允许重新点「优化表达」。
export type CellOptimizeState =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "ready"; suggestionMd: string }
  | { status: "error"; message: string };

export const CELL_OPTIMIZE_IDLE: CellOptimizeState = { status: "idle" };

// 规格③ + 任务简报原文逐字对照。
export const OPTIMIZE_CELL_BUTTON_LABEL = "优化表达";
export const ROW_OPTIMIZE_BUTTON_LABEL = "优化整行";
export const OPTIMIZE_ORIGINAL_LABEL = "原文";
export const OPTIMIZE_SUGGESTION_LABEL = "优化建议";
export const ACCEPT_SUGGESTION_LABEL = "接受";
export const DISCARD_SUGGESTION_LABEL = "放弃";

// 禁用原因提示文案——组件侧同一函数算出的原因同时驱动 disabled 与 title，
// 不会出现"按钮置灰但提示语与真实原因对不上"的错位。
export const OPTIMIZE_LOADING_HINT = "优化中，请稍候";
export const OPTIMIZE_UNSAVED_HINT = "有未保存的修改，请先保存后再优化";
export const OPTIMIZE_EMPTY_HINT = "格子为空，无需优化";

// 点击「优化表达」：仅当当前不在请求中才真正转入 loading（按钮本身也会用
// isCellOptimizeLoading disable，这里再兜底一次，防止竞态下重复触发）。
export function beginCellOptimize(state: CellOptimizeState): CellOptimizeState {
  return state.status === "loading" ? state : { status: "loading" };
}

// 建议到达：只有「正在请求中」才会被这条结果打断——已经被用户之后的其它
// 操作带离 loading 态（理论上不会发生，因为按钮在 loading 期间被禁用）时，
// 迟到的结果不应覆盖新状态。
export function resolveCellOptimizeSuggestion(state: CellOptimizeState, suggestionMd: string): CellOptimizeState {
  return state.status === "loading" ? { status: "ready", suggestionMd } : state;
}

export function failCellOptimize(state: CellOptimizeState, message: string): CellOptimizeState {
  return state.status === "loading" ? { status: "error", message } : state;
}

// 接受/放弃都回到 idle——组件侧另有副作用（accept 把 suggestionMd 填入编辑框
// 的 content 状态，discard 什么也不做），本函数只管这个小状态机本身怎么变。
export function dismissCellOptimize(): CellOptimizeState {
  return CELL_OPTIMIZE_IDLE;
}

export function isCellOptimizeLoading(state: CellOptimizeState): boolean {
  return state.status === "loading";
}

// 「优化表达」按钮禁用原因（null=可点击）——后端 optimize_cell 读的是已保存
// 的格子内容（store 里的值），不是编辑框里可能还没保存的草稿；若两者不一致
// 而仍允许触发，用户会看到"原文"对不上自己刚打的字、却分不清这是不是 bug，
// 所以有未保存修改时先挡住，而不是悄悄用旧内容生成一份令人费解的建议。
// 优先级：正在优化中 > 有未保存修改 > 内容为空——素材来源单一（hasUnsavedChanges
// 复用 knowhow-cell-editor-logic.ts，不重复实现"内容是否偏离已保存值"判断）。
export function optimizeCellDisabledReason(
  content: string,
  savedContent: string,
  state: CellOptimizeState,
): string | null {
  if (isCellOptimizeLoading(state)) return OPTIMIZE_LOADING_HINT;
  if (hasUnsavedChanges(content, savedContent)) return OPTIMIZE_UNSAVED_HINT;
  if (!content.trim()) return OPTIMIZE_EMPTY_HINT;
  return null;
}

// ===========================================================================
// 2. 「优化整行」——非空格子逐格顺序队列（规格③ + 任务简报）
// ===========================================================================

// 六态穷举（任务简报原文）：等待/进行中/建议就绪/已接受/已跳过/出错。
export type RowOptimizeCellStatus = "waiting" | "in_progress" | "ready" | "accepted" | "skipped" | "error";

export const ROW_OPTIMIZE_STATUS_LABELS: Record<RowOptimizeCellStatus, string> = {
  waiting: "等待",
  in_progress: "进行中",
  ready: "建议就绪",
  accepted: "已接受",
  skipped: "已跳过",
  error: "出错",
};

export const ROW_OPTIMIZE_SKIP_LABEL = "跳过";
export const ROW_OPTIMIZE_RETRY_LABEL = "重试";
export const ROW_OPTIMIZE_ABORT_LABEL = "中止";
export const ROW_OPTIMIZE_CLOSE_LABEL = "关闭";
export const ROW_OPTIMIZE_EMPTY_TEXT = "这一行没有可优化的内容格子。";
export const ROW_OPTIMIZE_DONE_TEXT = "已处理完这一行的全部格子。";

export type RowOptimizeItem = {
  columnId: string;
  columnName: string;
  /** 建队列那一刻该格的已保存内容快照——即 optimizeKnowhowCell 实际会读到
   * 的原文，用于"原文/建议"对照展示，不随后续编辑改变。 */
  originalMd: string;
  status: RowOptimizeCellStatus;
  suggestionMd?: string;
  errorMessage?: string;
};

export type RowOptimizeQueueState = {
  items: RowOptimizeItem[];
  /** 指向"当前正在处理/等待用户决定"的格子；等于 items.length 表示队列已走完。 */
  cursor: number;
  /** 中止后队列不再自动推进到下一格；不影响已经产生的各格状态。 */
  aborted: boolean;
};

// 建队列：按列 position 顺序扫描本行，跳过空格子（任务简报"skip empty"）。
// 顺序用纯 position（sortColumnsByPosition 同规则，非 orderColumnsForGrid
// 的"行标题钉首"展示序）——"逐格顺序"指的是表的自然列序，行标题列本身若非空
// 也一视同仁排入队列。全部为空则返回空队列，调用方据此展示
// ROW_OPTIMIZE_EMPTY_TEXT、不发起任何请求。
export function initRowOptimizeQueue(
  row: { cells: Record<string, string> },
  columns: { id: string; name: string; position: number }[],
): RowOptimizeQueueState {
  const ordered = [...columns].sort((a, b) => a.position - b.position);
  const items: RowOptimizeItem[] = ordered
    .filter((column) => (row.cells[column.id] ?? "").trim() !== "")
    .map((column) => ({
      columnId: column.id,
      columnName: column.name,
      originalMd: row.cells[column.id] ?? "",
      status: "waiting",
    }));
  return { items, cursor: 0, aborted: false };
}

export function currentRowOptimizeItem(state: RowOptimizeQueueState): RowOptimizeItem | null {
  return state.items[state.cursor] ?? null;
}

export function isRowOptimizeQueueFinished(state: RowOptimizeQueueState): boolean {
  return state.cursor >= state.items.length;
}

// 进度文案（"3/7"）：cursor 本身就是"已经离开等待态的格数"。
export function rowOptimizeProgress(state: RowOptimizeQueueState): { done: number; total: number } {
  return { done: Math.min(state.cursor, state.items.length), total: state.items.length };
}

function replaceCurrent(state: RowOptimizeQueueState, patch: Partial<RowOptimizeItem>): RowOptimizeQueueState {
  return {
    ...state,
    items: state.items.map((item, index) => (index === state.cursor ? { ...item, ...patch } : item)),
  };
}

// 当前格「等待」->「进行中」（即将发起 optimizeKnowhowCell 请求）。中止后
// 或当前格不在等待态时原样返回（无操作）——自动推进的调用方应自己先查
// aborted，这里是纯函数层再兜底一次，避免中止后仍产生新请求。
export function markCurrentInProgress(state: RowOptimizeQueueState): RowOptimizeQueueState {
  const current = currentRowOptimizeItem(state);
  if (state.aborted || !current || current.status !== "waiting") return state;
  return replaceCurrent(state, { status: "in_progress" });
}

// LLM 建议到达：「进行中」->「建议就绪」。中止后到达的迟到结果被丢弃——不
// "复活"一个用户已经决定停下的队列。
export function applySuggestion(state: RowOptimizeQueueState, suggestionMd: string): RowOptimizeQueueState {
  const current = currentRowOptimizeItem(state);
  if (state.aborted || !current || current.status !== "in_progress") return state;
  return replaceCurrent(state, { status: "ready", suggestionMd });
}

// LLM 调用失败：「进行中」->「出错」，原样保留后端中文错误文案（调用方已用
// extractErrorMessage 抽取）。中止后到达同样丢弃。
export function applyError(state: RowOptimizeQueueState, message: string): RowOptimizeQueueState {
  const current = currentRowOptimizeItem(state);
  if (state.aborted || !current || current.status !== "in_progress") return state;
  return replaceCurrent(state, { status: "error", errorMessage: message });
}

// 「接受」两段式（任务简报："accept applies via patchKnowhowCell then
// advances"）：begin 把当前格标记「进行中」（借用同一状态展示 PATCH 落库这个
// 短暂过程，六态词表里没有专门再加一个"保存中"），调用方随后 await
// patchKnowhowCell；成功 -> completeAcceptCurrent（落地为"已接受"+推进游标），
// 失败 -> failAcceptCurrent（出错，游标不动，允许用户跳过或重试）。只允许
// 从「建议就绪」发起，其它状态点「接受」是无操作。
export function beginAcceptCurrent(state: RowOptimizeQueueState): RowOptimizeQueueState {
  const current = currentRowOptimizeItem(state);
  if (!current || current.status !== "ready") return state;
  return replaceCurrent(state, { status: "in_progress" });
}

export function completeAcceptCurrent(state: RowOptimizeQueueState): RowOptimizeQueueState {
  const current = currentRowOptimizeItem(state);
  if (!current || current.status !== "in_progress") return state;
  return { ...replaceCurrent(state, { status: "accepted" }), cursor: state.cursor + 1 };
}

export function failAcceptCurrent(state: RowOptimizeQueueState, message: string): RowOptimizeQueueState {
  const current = currentRowOptimizeItem(state);
  if (!current || current.status !== "in_progress") return state;
  return replaceCurrent(state, { status: "error", errorMessage: message });
}

// 「跳过」：允许从「建议就绪」或「出错」发起（出错时跳过=放弃这一格、继续
// 队列——一次失败不该永久卡住整行）；等待/进行中/已经决定过的格子点「跳过」
// 是无操作。
export function skipCurrent(state: RowOptimizeQueueState): RowOptimizeQueueState {
  const current = currentRowOptimizeItem(state);
  if (!current || (current.status !== "ready" && current.status !== "error")) return state;
  return { ...replaceCurrent(state, { status: "skipped" }), cursor: state.cursor + 1 };
}

// 「重试」：出错 -> 等待（游标不动），供组件对同一格重新发起
// optimizeKnowhowCell。不是六态之外的新状态——只是把已有的「出错」态转回
// 「等待」，让"等待中的格子会被自动触发"这条既有机制自然接管重试。
export function retryCurrent(state: RowOptimizeQueueState): RowOptimizeQueueState {
  const current = currentRowOptimizeItem(state);
  if (!current || current.status !== "error") return state;
  return replaceCurrent(state, { status: "waiting", errorMessage: undefined });
}

// 「中止」：任何时候都可调用，只是把"继续自动推进"的闸门关掉（组件侧的
// 自动触发逻辑需先查 aborted 再发起下一格请求）；不改变已经产生的各格状态。
export function abortQueue(state: RowOptimizeQueueState): RowOptimizeQueueState {
  return { ...state, aborted: true };
}

// ===========================================================================
// 3. 追加导入预览展示映射（Task 6 wire 消费方，规格②路B）
// ===========================================================================

export type AppendDuplicateDisplayItem = {
  /** 1-based，供用户阅读（后端 duplicate_titles[].row_index 是 0-based）。 */
  displayIndex: number;
  title: string;
  /** false=重名命中的是预览窗口之外的行（rows_preview 只截前 5 行，但
   * duplicate_titles 是对整份上传文件算的）——这类重名无法在预览表格里高亮
   * 对应行，只能靠文字列表提醒，调用方据此追加"（不在预览范围内）"标注。 */
  inPreviewWindow: boolean;
};

export function mapAppendDuplicatesForDisplay(
  duplicateTitles: KnowhowAppendDuplicateTitle[],
  previewRowCount: number,
): AppendDuplicateDisplayItem[] {
  return duplicateTitles.map((item) => ({
    displayIndex: item.rowIndex + 1,
    title: item.title,
    inPreviewWindow: item.rowIndex < previewRowCount,
  }));
}

// 预览表格里某一行（0-based，与 rowsPreview 下标一致）是否要标黄——只在
// 「行标题重名」意义下判定，未匹配列不影响这个判断。
export function isAppendPreviewRowDuplicate(
  duplicateTitles: KnowhowAppendDuplicateTitle[],
  previewRowIndex: number,
): boolean {
  return duplicateTitles.some((item) => item.rowIndex === previewRowIndex);
}

// 未匹配列提示文案（null=没有未匹配列，调用方据此不渲染横幅）。
export function formatUnmatchedColumnsMessage(unmatchedColumns: string[]): string | null {
  if (unmatchedColumns.length === 0) return null;
  return `以下列在当前表中未匹配，追加后将被忽略：${unmatchedColumns.join("、")}`;
}

// ===========================================================================
// 4. 模板下载文件名（Task 6 后端 download_knowhow_template 的
//    f"{table['title']}-template.xlsx" 逐字同规则）
// ===========================================================================
//
// 模板下载走鉴权 blob fetch（见 knowhow-panel.tsx），而非浏览器原生 <a href>
// 直接导航——blob: URL 不携带原始响应的 Content-Disposition 头，下载文件名
// 必须由前端自己在 <a download> 上显式给出，否则浏览器会给出一个形如
// "template" 的裸 blob 文件名，而不是这张表自己的标题。

export function templateDownloadFilename(tableTitle: string): string {
  return `${tableTitle}-template.xlsx`;
}
