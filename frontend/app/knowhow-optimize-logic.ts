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

import type { KnowhowAppendDuplicateTitle, KnowhowRow } from "./knowhow-model.ts";
import { hasUnsavedChanges } from "./knowhow-cell-editor-logic.ts";
// 批量保存去重（F1）复用 anchor 分组的「合并共享格」判定——不另发明第二套等价，
// 与 knowhow-panel.tsx handleCellSave 用的是同一套 groupRowsByAnchor /
// isSharedColumn / groupCellWriteTargets（grouping-logic 只 import 类型、无运行时
// 依赖，node --test 直接可载；也不构成循环——它不反向依赖本文件）。
import { groupRowsByAnchor, groupCellWriteTargets, isSharedColumn, type AnchorGroup } from "./knowhow-grouping-logic.ts";

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

// ===========================================================================
// 5. 批量「一键规整」（整行/整表，knowhow-md-normalize Task 9）
// ===========================================================================
//
// 复用上面第 2 节「优化整行」状态机的整体思路（逐格调用后端、进度可见、
// 单格失败不摆烂），但**不是同一个状态机**——两者面向的人机交互形态本质
// 不同，刻意分开实现（小体量并行实现，优于硬凑一个跨特性共享抽象）：
//
//   「优化整行」（第 2 节）：每一格都是一次独立的人工决定——LLM 建议
//   到达后停在 ready 态，等用户逐格点「接受」或「跳过」，接受即刻落库、
//   立即推进到下一格。是一个"人工把关的流水线"。
//
//   「批量一键规整」（本节，任务简报硬要求①）：人工只在**最后**把关一次
//   ——reformat_cell 对全部非空格子自动跑完并收集 {before, after, source,
//   changed}，汇总成一份摘要，用户看过这份摘要后一次性「确认保存」，才
//   会真正逐格调用保存；changed=false 的格子永远不进入待保存范围。是一个
//   "先跑完、后过目"的批处理。
//
// 这个差异决定了状态机形状的差异：
//   - 没有 RowOptimizeItem 的 "ready" 态（等待人工对某一格单独表态）；
//     取而代之的是 "changed"/"unchanged"（reformat_cell 已经给出结论，
//     无需人对**这一格**单独表态，只需要对**整批**表态一次）。
//   - 没有 cursor："优化整行"的 cursor 兼有"当前正在处理哪一格"与"处理到
//     第几格"双重含义，且天然要求严格串行推进（同一时刻只有一格在
//     等待人工决定）；本状态机的驱动方（knowhow-panel.tsx 里的
//     KnowhowReformatBatchModal）用一个前端 for 循环顺序遍历静态 items
//     数组来驱动请求发起顺序，状态机本身只需要按 (rowId, columnId) 这个
//     天然唯一键定位/改写某一项，不需要额外维护"当前在第几个"这份冗余
//     状态——这也是同一套 init 函数能不加改动地同时服务"整行"（items 只来自
//     一个 row）与"整表"（items 来自多个 row）两种作用域的原因：调用方
//     传几行 row 进来，跟状态机内部怎么定位/改写一项完全解耦。
//   - 顶层 phase 字段是本状态机独有的"总闸"：把"逐格调用 reformat_cell 收集
//     候选"（running）与"人工整体确认后逐格调用保存"（saving）分成两个
//     互斥的阶段，phase 本身就是这两阶段之间**人工确认**发生的地方
//     （beginReformatBatchSave 是唯一从 reviewing 走向 saving 的入口，
//     只有 UI 层的"确认保存"按钮点击才会调用它——这正是任务简报硬要求①
//     "Never auto-persist" 在状态机层面的落实：状态机里根本不存在任何一条
//     从 running 直接通向 saving 的路径）。running/saving 阶段各自的
//     mark*/apply* 转移函数都以 `state.phase` 是否命中对应阶段作为总闸，
//     离开该阶段后到达的迟到结果一律被丢弃（镜像第 2 节 applySuggestion/
//     applyError 对 state.aborted 的丢弃语义，但用的是独立的 phase 字段，
//     不是同一个 state.aborted——本状态机的 aborted 只是"是否曾经被用户
//     中止过"的展示位，真正驱动"还能不能继续处理"的是 phase）。

// 每个 item 从 pending 出发，run 阶段落到 changed/unchanged/reformat_error
// 三个终态之一；只有 changed 会在人工确认后进入 save 阶段，落到
// saved/save_error 之一——unchanged/reformat_error 没有可写的候选内容，
// 永远不会被 markReformatItemSaving 选中（任务简报硬要求①「changed=false
// 的格子跳过、不写入」在这里体现为"根本没有能让它进入 saving 态的转移函数
// 入口"，不是运行时判断绕过）。
export type ReformatBatchCellStatus =
  | "pending"
  | "running"
  | "changed"
  | "unchanged"
  | "reformat_error"
  // 「已中止」：用户点「中止」那一刻仍在飞的格子的终态。刻意独立于 reformat_error
  // ——中止不是失败，把它计入/展示成「规整失败」会误导用户（任务硬要求：aborted
  // 项要计数/展示得合理，别当失败）。见 abortReformatBatchRun 的就地结算。
  | "aborted"
  | "saving"
  | "saved"
  | "save_error"
  // 「已跳过（内容已变）」：保存前比对发现该格自建批次以来已被他人改动，拒绝
  // 用旧内容算出的候选覆盖它（F2）。刻意独立于 save_error——这不是保存失败，
  // 是主动放弃这次写入以保护他人的新编辑，计数/展示都不应混进「保存失败」。
  | "stale_skipped";

export type ReformatBatchScope = "row" | "table";

export type ReformatBatchItem = {
  rowId: string;
  columnId: string;
  columnName: string;
  /** 建批次那一刻该格的已保存内容快照——reformat_cell 实际会读到的原文，
   * 用于"原文/规整建议"对照展示，不随后续编辑改变（同 RowOptimizeItem。
   * originalMd 的约定）。 */
  originalMd: string;
  status: ReformatBatchCellStatus;
  /** 仅 changed/saving/saved/save_error 四态携带——reformat_cell 给出的
   * 候选内容（人工确认后即为将写入的新内容）。 */
  candidateMd?: string;
  /** 后端原始枚举（"llm" | "rule/llm-failed" | "rule/no-llm"）——原样透传，
   * 不在状态机里做任何映射/校验。任务简报硬要求②「绝不直出给用户」由
   * knowhow-panel.tsx 渲染这一字段时统一经 reformatSourceLabel（Task 8，
   * knowhow-cell-editor-logic.ts）转换，状态机本身只负责搬运数据。 */
  source?: string;
  errorMessage?: string;
};

// idle=展示将处理的格子数，等待用户点「开始规整」（任务简报硬要求③：开始
// 前先说明规模）；running=逐格调用 reformat_cell；reviewing=已收集完全部
// 候选，等待人工整体确认（任务简报硬要求①的唯一确认点）；saving=确认后
// 逐格调用保存；done=保存阶段结束（不论有没有 save_error，都会走到这里，
// 任务简报硬要求④「单格失败不阻断整批」在此体现为"保存阶段没有因失败而
// 提前终止 done 的路径"）。
export type ReformatBatchPhase = "idle" | "running" | "reviewing" | "saving" | "done";

export type ReformatBatchState = {
  scope: ReformatBatchScope;
  items: ReformatBatchItem[];
  phase: ReformatBatchPhase;
  /** 是否曾经被用户中止过 run 阶段——纯展示位（"已中止，以下是中止前收集到
   * 的结果"），不驱动任何转移函数（转移函数一律看 phase，见本节头注释）。 */
  aborted: boolean;
};

export type ReformatBatchSummary = {
  total: number;
  pending: number;
  running: number;
  changed: number;
  unchanged: number;
  reformat_error: number;
  aborted: number;
  saving: number;
  saved: number;
  save_error: number;
  stale_skipped: number;
};

// --- 文案常量（组件侧只引用，不内联硬编码字符串——同本文件其余三节的既有
// 约定）------------------------------------------------------------------------

export const BATCH_REFORMAT_ROW_BUTTON_LABEL = "一键规整整行";
export const BATCH_REFORMAT_TABLE_BUTTON_LABEL = "一键规整整表";
export const BATCH_REFORMAT_START_LABEL = "开始规整";
export const BATCH_REFORMAT_ABORT_LABEL = "中止";
export const BATCH_REFORMAT_CONFIRM_LABEL = "确认保存";
export const BATCH_REFORMAT_DISCARD_LABEL = "放弃改动";
export const BATCH_REFORMAT_CLOSE_LABEL = "关闭";
export const BATCH_REFORMAT_EMPTY_TEXT_ROW = "这一行没有可规整的内容格子。";
export const BATCH_REFORMAT_EMPTY_TEXT_TABLE = "这张表没有可规整的内容格子。";
// reviewing 阶段 changed 计数为 0 时的提示（不同于上面"没有可处理的格子"：
// 这里是"处理过了，只是没有需要改的"）。
export const BATCH_REFORMAT_NO_CHANGES_TEXT = "规整后没有需要改动的格子。";
export const BATCH_REFORMAT_DONE_TEXT = "已完成保存。";
// F2：保存前比对发现该格自建批次以来已被他人改动时的友好提示（不暴露枚举/
// 技术词，见 error-layer-trust-by-provenance——面向用户文案只走品牌化中文）。
export const BATCH_REFORMAT_STALE_SKIP_TEXT = "内容已被其他人修改，已跳过——请重新运行规整";
// F2（abort）：中止会把未开始的格子留在 pending。收尾摘要里把这些从没起跑过的
// 格子如实点成「未开始（已中止）」——不能算进「共处理 N」（否则 100 格中止在第
// 10 格却谎报「共处理 100」）。友好中文、不暴露 pending 枚举。
export const BATCH_REFORMAT_ABORTED_PENDING_LABEL = "未开始（已中止）";

export const BATCH_REFORMAT_STATUS_LABELS: Record<ReformatBatchCellStatus, string> = {
  pending: "待处理",
  running: "规整中",
  changed: "有改动",
  unchanged: "无需改动",
  reformat_error: "规整失败",
  aborted: "已中止",
  saving: "保存中",
  saved: "已保存",
  save_error: "保存失败",
  stale_skipped: "已跳过",
};

// 开始规整前的规模提示（任务简报硬要求③：批量每格至少一次后端调用、每格
// 都可能触发一次 LLM，用户必须在触发前就知道这件事的规模，而不是点了才
// 发现自己启动了一个跑很久、花费不小的操作）。
export function batchReformatScaleText(cellCount: number): string {
  return `共 ${cellCount} 个格子将被处理，每个格子最多调用一次 AI，处理完成后需要你确认才会保存。`;
}

// --- 建批次 --------------------------------------------------------------------

// 按行 position 顺序展开多行、行内再按列 position 顺序，跳过空格子——
// "整行"传一个 row，"整表"传该表全部 row，是同一份逻辑（不为"整行"只有
// 一行"这一特例另写代码路径）。
//
// F2：**排除 anchor（行标题）列**。缺陷是此前把每个非空列都纳入，包括 anchor 列，于是
// 整表/整行批量会重排分组键（`A. Foo` -> `**A. Foo**`）、把概念组拆开或合并。契约：只有
// 显式的 PER-CELL「优化表达」动作可以碰 anchor 格（它经 handleCellSave 的扇出保持整组
// 一致、且有人工复核那一格）。用 `anchorColumnId`（= handleCellSave/planReformatSaves 分组
// 用的**同一个键**）判定，而非列的 role：扇出重排的正是 anchorColumnId 那一列，排除它才
// 与扇出严格对齐；记录型表 anchorColumnId=null 则不排除任何列（本就无分组键可保护）。
function buildReformatBatchItems(
  rows: { id: string; position: number; cells: Record<string, string> }[],
  columns: { id: string; name: string; position: number }[],
  anchorColumnId: string | null,
): ReformatBatchItem[] {
  const orderedColumns = [...columns].sort((a, b) => a.position - b.position);
  const orderedRows = [...rows].sort((a, b) => a.position - b.position);
  const items: ReformatBatchItem[] = [];
  for (const row of orderedRows) {
    for (const column of orderedColumns) {
      if (anchorColumnId !== null && column.id === anchorColumnId) continue; // F2：anchor 列不批量规整
      const content = row.cells[column.id] ?? "";
      if (!content.trim()) continue;
      items.push({
        rowId: row.id,
        columnId: column.id,
        columnName: column.name,
        originalMd: content,
        status: "pending",
      });
    }
  }
  return items;
}

// `anchorColumnId`：行标题列 id（记录型表为 null）——buildReformatBatchItems 据此排除 anchor
// 列（F2）。省略时默认 null（不排除），保持既有 3 参调用向后兼容；真实调用方
// （KnowhowReformatBatchModal）必须显式传 detail.anchorColumnId。
export function initReformatBatch(
  scope: ReformatBatchScope,
  rows: { id: string; position: number; cells: Record<string, string> }[],
  columns: { id: string; name: string; position: number }[],
  anchorColumnId: string | null = null,
): ReformatBatchState {
  return { scope, items: buildReformatBatchItems(rows, columns, anchorColumnId), phase: "idle", aborted: false };
}

// --- 规模/汇总计数（驱动"开始前展示规模"③与"确认前展示改动数"①两处 UI，
// 同一个计数源，不重复实现两遍） -----------------------------------------------

export function reformatBatchSummary(state: ReformatBatchState): ReformatBatchSummary {
  const summary: ReformatBatchSummary = {
    total: state.items.length,
    pending: 0,
    running: 0,
    changed: 0,
    unchanged: 0,
    reformat_error: 0,
    aborted: 0,
    saving: 0,
    saved: 0,
    save_error: 0,
    stale_skipped: 0,
  };
  for (const item of state.items) summary[item.status] += 1;
  return summary;
}

// reviewing 阶段（run 跑完、等人工确认保存）的收尾摘要文案——单一真源，组件侧只调它。
//
// F4（review）：缺陷是选「明细 vs 无需改动」时只看了 changed/aborted/stale_skipped，
// **漏了 reformat_error**——整批规整全部失败时这三者皆 0，会错误落到「规整后没有需要
// 改动的格子」，把一整批失败粉饰成「无需改动」。修复：凡有任何**非平凡终态**（有改动 /
// 规整失败 / 已中止 / 已跳过 / 保存失败）就走明细分支如实计数；只有清一色 unchanged
// （真的没什么要改）才说「无需改动」。明细文案与旧内联实现逐字一致（changed/unchanged
// 恒列出，其余各态 >0 才追加），故 mixed/changed 场景字节不变、只补回被吞的全错场景。
//
// F2（abort）：中止把未开始的格子留在 pending。缺陷是「共处理 ${total}」把这些从没
// 起跑过的格子也算成已处理（100 格中止在第 10 格却说「共处理 100」）。修复：①pending>0
// 也算「非平凡终态」进明细（否则清一色 pending 的极早中止会被粉饰成「无需改动」）；
// ②「共处理」改用真正起跑过的数 = total - pending；③另起「N 个未开始（已中止）」如实
// 点出。pending===0（正常跑完）时 processed === total 且不追加未开始子句，与旧版逐字一致。
export function reformatReviewingSummaryText(summary: ReformatBatchSummary): string {
  const hasReport =
    summary.changed > 0 ||
    summary.reformat_error > 0 ||
    summary.aborted > 0 ||
    summary.stale_skipped > 0 ||
    summary.save_error > 0 ||
    summary.pending > 0;
  if (!hasReport) return BATCH_REFORMAT_NO_CHANGES_TEXT;
  // reviewing 阶段 running/saving/saved/save_error 恒为 0，故 processed 恰好等于明细
  // 各态（changed/unchanged/reformat_error/aborted/stale_skipped）之和——报数自洽。
  const processed = summary.total - summary.pending;
  let text =
    `共处理 ${processed} 个格子：${summary.changed} 个${BATCH_REFORMAT_STATUS_LABELS.changed}、` +
    `${summary.unchanged} 个${BATCH_REFORMAT_STATUS_LABELS.unchanged}`;
  if (summary.reformat_error > 0) text += `、${summary.reformat_error} 个${BATCH_REFORMAT_STATUS_LABELS.reformat_error}`;
  if (summary.aborted > 0) text += `、${summary.aborted} 个${BATCH_REFORMAT_STATUS_LABELS.aborted}`;
  if (summary.stale_skipped > 0) text += `、${summary.stale_skipped} 个${BATCH_REFORMAT_STATUS_LABELS.stale_skipped}`;
  if (summary.pending > 0) text += `，${summary.pending} 个${BATCH_REFORMAT_ABORTED_PENDING_LABEL}`;
  text += "。";
  return text;
}

// --- 按 (rowId, columnId) 定位/改写单项（本节的"当前项"概念是这个天然键，
// 不是第 2 节的 cursor，见本节头注释） ------------------------------------------

function findReformatItem(state: ReformatBatchState, rowId: string, columnId: string): ReformatBatchItem | null {
  return state.items.find((item) => item.rowId === rowId && item.columnId === columnId) ?? null;
}

function replaceReformatItem(
  state: ReformatBatchState,
  rowId: string,
  columnId: string,
  patch: Partial<ReformatBatchItem>,
): ReformatBatchState {
  return {
    ...state,
    items: state.items.map((item) => (item.rowId === rowId && item.columnId === columnId ? { ...item, ...patch } : item)),
  };
}

// --- run 阶段：idle -> running -> 逐格 pending -> running -> {changed |
// unchanged | reformat_error} -> reviewing --------------------------------------

export function beginReformatBatchRun(state: ReformatBatchState): ReformatBatchState {
  return state.phase === "idle" ? { ...state, phase: "running" } : state;
}

// 即将为某一格发起 reformat_cell 请求：pending -> running。phase 不是
// "running"（尚未开始、已跑完、已中止）时原样返回——调用方（前端 for 循环）
// 自己也会在中止后不再发起下一次请求，这里是纯函数层再兜底一次。
export function markReformatItemRunning(
  state: ReformatBatchState,
  rowId: string,
  columnId: string,
): ReformatBatchState {
  if (state.phase !== "running") return state;
  const target = findReformatItem(state, rowId, columnId);
  if (!target || target.status !== "pending") return state;
  return replaceReformatItem(state, rowId, columnId, { status: "running" });
}

// reformat_cell 结果到达：changed=true -> changed(candidateMd, source)；
// changed=false -> unchanged(source)，不带 candidateMd（这一格永远不会被
// markReformatItemSaving 选中，见本节头注释）。只有 phase==="running" 且
// 该项当前恰为 running 时才生效——已中止/已跑完后到达的迟到结果被丢弃
// （镜像 applySuggestion 对 state.aborted 的丢弃语义）。
export function applyReformatResult(
  state: ReformatBatchState,
  rowId: string,
  columnId: string,
  result: { candidateMd: string; source: string; changed: boolean },
): ReformatBatchState {
  if (state.phase !== "running") return state;
  const target = findReformatItem(state, rowId, columnId);
  if (!target || target.status !== "running") return state;
  return replaceReformatItem(
    state,
    rowId,
    columnId,
    result.changed
      ? { status: "changed", candidateMd: result.candidateMd, source: result.source }
      : { status: "unchanged", source: result.source },
  );
}

// reformat_cell 调用失败：running -> reformat_error(message)，原样保留后端
// /extractErrorMessage 抽取出的中文错误文案。这一格从此不再被处理（任务简报
// 硬要求④「单格失败不阻断整批」体现为：调用方在 catch 里记录这个错误后仍会
// 继续遍历下一项，不会因为这一格失败就停止整个 for 循环）。
export function applyReformatError(
  state: ReformatBatchState,
  rowId: string,
  columnId: string,
  message: string,
): ReformatBatchState {
  if (state.phase !== "running") return state;
  const target = findReformatItem(state, rowId, columnId);
  if (!target || target.status !== "running") return state;
  return replaceReformatItem(state, rowId, columnId, { status: "reformat_error", errorMessage: message });
}

// 运行阶段并发防护（代码评审 P1-a）：/reformat 读的是 LIVE 落库格子，而弹窗展示/
// 去重/保存都锚在建批次那一刻的 originalMd 快照。若某格在「建批次」到「真正发起
// 这一格 /reformat」之间被别的标签页/用户改了，后端会对**改后**的内容做规整，回带
// 的 sourceMd 就 ≠ 这一格的 originalMd 快照——候选是拿客户端从没见过的内容算出来
// 的。此时必须：①这一格直接判陈旧（走 stale_skipped，不拿它盲填）；②**绝不把这
// 份候选写进去重缓存**，否则它的重复格（同 trim 原文）会复用一份基于陌生内容的
// 候选，缓存一污染就再也纠不回来（正是本修复要堵的洞）。判定是纯函数（本函数），
// runBatch 据它决定「缓存 + applyReformatResult」还是「markReformatItemRunStale +
// 不缓存」，.tsx 只做这一次分流。
//
// 逐字比对（非 trim）：快照后的任何改动——哪怕只是空白——都意味着有人动过这格，
// 保守判陈旧（与保存侧 isReformatUnitStale 的逐字口径一致）。
export function reformatResultIsStale(originalMd: string, sourceMd: string): boolean {
  return originalMd !== sourceMd;
}

// 运行阶段：running -> stale_skipped（sourceMd 比对发现该格已被他人改动，P1-a）。
// 与 applyReformatResult / applyReformatError 同一套门槛（phase 必须 running、该项
// 必须正处于 running），只是走向「主动跳过」而非拿到候选：不带 candidateMd，故它
// 永远不会进入保存集（同 unchanged / reformat_error）。friendly 文案进 errorMessage，
// 与保存阶段的 markReformatItemStaleSkipped 复用同一处逐格明细展示与同一个终态。
// 刻意独立于 markReformatItemStaleSkipped（那是 saving 阶段 changed -> stale_skipped）：
// 两者阶段/前置状态不同，但都收敛到 stale_skipped 这一个用户可见终态。
export function markReformatItemRunStale(
  state: ReformatBatchState,
  rowId: string,
  columnId: string,
  message: string,
): ReformatBatchState {
  if (state.phase !== "running") return state;
  const target = findReformatItem(state, rowId, columnId);
  if (!target || target.status !== "running") return state;
  return replaceReformatItem(state, rowId, columnId, { status: "stale_skipped", errorMessage: message });
}

// 「中止」：running -> reviewing 且 aborted=true——立即把 UI 切到"整体确认"
// screen（不必等最后一次在途请求返回），已经收集到的候选照常可以被确认
// 保存；尚未处理的格子停在 pending，不会再被处理（调用方的 for 循环按自己
// 的 abortedRef 短路，不依赖这里；这里把 phase 一并翻到 reviewing 是为了让
// 纯函数层自己也能正确拒绝任何后续 markReformatItemRunning 调用，见其
// 头注释）。
//
// 关键：**同一次转移里**把仍处于 running 的在飞格子就地结算为终态 aborted。
// 不这么做的话，那一格会永远停在「规整中」——它的迟到响应到达时，phase 已经
// 是 reviewing，会被 applyReformatResult/applyReformatError 的 `phase !==
// "running"` 总闸丢弃（这是对的，中止后不该复活队列），于是没有任何转移能把它
// 带离 running，review 面板里它就一直显示「规整中」。就地结算后，那条迟到响应
// 仍被丢弃（phase 总闸不变；且此刻该项已不是 running，applyReformat* 的
// `status !== "running"` 第二道门也会拦下），格子却已经是稳定的终态。
export function abortReformatBatchRun(state: ReformatBatchState): ReformatBatchState {
  if (state.phase !== "running") return state;
  return {
    ...state,
    phase: "reviewing",
    aborted: true,
    items: state.items.map((item) => (item.status === "running" ? { ...item, status: "aborted" } : item)),
  };
}

// 自然跑完（未中止）：running -> reviewing。调用方在 for 循环正常结束后
// 调用；若循环因中止提前 break，abortReformatBatchRun 早已完成这次翻转，
// 这里是幂等的（phase 已经不是 running，直接原样返回）。
export function finishReformatBatchRun(state: ReformatBatchState): ReformatBatchState {
  return state.phase === "running" ? { ...state, phase: "reviewing" } : state;
}

// --- 确认闸门：reviewing -> saving | done（任务简报硬要求①"人工整体确认"
// 落在状态机里的唯一入口——只有 UI 的「确认保存」按钮会调用它，状态机里不
// 存在任何一条从 running 直通 saving 的路径） -----------------------------------

export function beginReformatBatchSave(state: ReformatBatchState): ReformatBatchState {
  if (state.phase !== "reviewing") return state;
  const hasChanged = state.items.some((item) => item.status === "changed");
  return { ...state, phase: hasChanged ? "saving" : "done" }; // 没有任何改动：无需保存，直接收尾
}

// --- save 阶段：仅 changed 逐格 -> saving -> {saved | save_error} --------------

// 即将为某一格调用保存：changed -> saving。非 changed（unchanged/
// reformat_error 没有候选内容；saved/save_error 已经处理过）原样返回。
export function markReformatItemSaving(
  state: ReformatBatchState,
  rowId: string,
  columnId: string,
): ReformatBatchState {
  if (state.phase !== "saving") return state;
  const target = findReformatItem(state, rowId, columnId);
  if (!target || target.status !== "changed") return state;
  return replaceReformatItem(state, rowId, columnId, { status: "saving" });
}

export function applyReformatSaveSuccess(
  state: ReformatBatchState,
  rowId: string,
  columnId: string,
): ReformatBatchState {
  if (state.phase !== "saving") return state;
  const target = findReformatItem(state, rowId, columnId);
  if (!target || target.status !== "saving") return state;
  return replaceReformatItem(state, rowId, columnId, { status: "saved" });
}

// 保存失败：saving -> save_error(message)——不影响其它格子的保存（任务简报
// 硬要求④：调用方的保存循环 catch 住这一格的异常后继续处理下一个 changed
// 项，不会因为这一格失败就放弃整批保存）。
export function applyReformatSaveError(
  state: ReformatBatchState,
  rowId: string,
  columnId: string,
  message: string,
): ReformatBatchState {
  if (state.phase !== "saving") return state;
  const target = findReformatItem(state, rowId, columnId);
  if (!target || target.status !== "saving") return state;
  return replaceReformatItem(state, rowId, columnId, { status: "save_error", errorMessage: message });
}

// 发现该格已被他人改动 -> stale_skipped(带友好文案)。前置状态放行「保存进行中」的
// 两个态，对应两条并发防护路径：
//   - changed：保存前 preflight refetch 逐格比对发现陈旧（F2），此刻成员还没被标 saving；
//   - saving：preflight 通过、成员已 markReformatItemSaving 翻成 saving，随后服务端事务内
//     比对判他人已改回 409（F3）——旧实现只认 changed，对已 saving 的成员是 no-op，格子
//     永远卡在「保存中」且不计入 stale_skipped；放行 saving 后能正确结算。
// 无论从哪个态进入，都走向「主动跳过」这一个用户可见终态（不是 save_error），这一格
// 不会被 onSaveCell 写入。已达终态（saved/save_error/stale_skipped）不得被重开。友好文案
// 进 errorMessage，与规整/保存失败共用同一处逐格明细展示（见 knowhow-panel.tsx
// errorMessage 列表）。message 由调用方传 BATCH_REFORMAT_STALE_SKIP_TEXT。
export function markReformatItemStaleSkipped(
  state: ReformatBatchState,
  rowId: string,
  columnId: string,
  message: string,
): ReformatBatchState {
  if (state.phase !== "saving") return state;
  const target = findReformatItem(state, rowId, columnId);
  if (!target || (target.status !== "changed" && target.status !== "saving")) return state;
  return replaceReformatItem(state, rowId, columnId, { status: "stale_skipped", errorMessage: message });
}

// 保存阶段结束：saving -> done。调用方在保存循环（遍历全部 changed 项）
// 结束后调用一次——不论其中有没有 save_error/stale_skipped，都会走到这里（硬
// 要求④「单格失败不阻断整批」在此体现为：done 不是"全部保存成功"才能到达的
// 状态，而是"保存阶段跑完了"就到达的状态，save_error/stale_skipped 计数留给
// summary 展示）。
export function finishReformatBatchSave(state: ReformatBatchState): ReformatBatchState {
  return state.phase === "saving" ? { ...state, phase: "done" } : state;
}

// F（review）：保存循环收尾——只要保存阶段发生过 stale 跳过（保存前 preflight 比对命中
// **或**服务端事务内比对判他人已改 409），父级 detail 快照就已落后于服务器（被跳过的
// 格子在服务端是别人刚写的新内容）。若不刷新一次父表，关掉批量弹窗重开会用**同一份
// 陈旧基线**（detail.rows -> originalMd 快照）复跑，那些格子每次都命中 stale、永远跳过，
// 尽管文案说「请重新运行规整」。判定纯化成谓词：runSave 收尾用它决定是否回调 onStaleReload
// （见 knowhow-panel.tsx）。参数是保存阶段实际发生的 stale 跳过计数（run 阶段 P1-a 的
// stale 不算——那批从没进保存集；用循环内局部计数、不从 summary.stale_skipped 反推，后者
// 混入了 run 阶段来源）。>0 即需刷新（无论跳过几格，父表都只需刷一次）。
export function reformatBatchSaveNeedsRefresh(saveStaleSkipCount: number): boolean {
  return saveStaleSkipCount > 0;
}

// F1（run 阶段陈旧也要刷父表）：运行阶段 sourceMd 陈旧的格子（P1-a）走 stale_skipped，
// 从没进过保存集——此前只有保存阶段的 stale 才回调 onStaleReload，于是这些格子对应的
// 父表快照永不刷新，关掉重开用同一陈旧基线复跑、永远跳过。判定与保存侧同款纯谓词
// （count>0），供 runBatch 收尾据它决定是否请父级刷新。与 reformatBatchSaveNeedsRefresh
// 刻意分成两个谓词（两个阶段各自的计数源），但组件侧经同一个「恰一次」闸
// （requestStaleReload）汇流——两阶段都跳过时父表也只刷一次，见 knowhow-panel.tsx。
export function reformatBatchRunNeedsRefresh(runStaleSkipCount: number): boolean {
  return runStaleSkipCount > 0;
}

// --- 按内容去重（代码评审修复）：anchor 分组的兄弟行会把同一份概念值
// forward-fill 到每一行，同一列因此在多行里出现字面完全相同的内容
// （真实数据：某表 20 个格子仅 12 份不同内容）。「整表」批量此前对每个
// (row, column) 都独立调一次 reformat_cell——LLM 端 temperature=1.0 且默认
// 不开缓存，N 份相同输入会各自拿到一份不同但都合法的候选；随后确认保存时，
// handleCellSave 对"合并共享格"的批量写会把整组都覆盖成"最后处理的那一格"
// 的候选，而弹窗仍展示每一格各自收到的候选——用户对每一格的确认因此被悄悄
// 作废，保存结果与展示内容对不上。
//
// 修复不需要引入 anchor 分组的概念（isSharedColumn/groupRowsByAnchor 是
// knowhow-grouping-logic.ts 的展示层概念，本文件不依赖它）——reformat_cell
// 本身是 (content_md, column_name, kind) 的纯函数、不看行身份（同一份任务
// 简报的 KEY INSIGHT），所以只需按 (columnId, 原文精确内容) 记忆化：字面
// 完全相同的输入只需成功拿到一次结果，其余重复项直接复用，不再重复请求。
// -----------------------------------------------------------------------------

// 记忆化 key——只用 (columnId, 规整前原文的 .trim()) 两者，不掺入 rowId/表作用域
// 等概念（与 reformat_cell 的纯函数签名对齐）。
//
// **originalMd 必须先 .trim() 再入 key**，且这个 .trim() 与 knowhow-grouping-logic.ts
// 的 isSharedColumn 判「合并共享格」用的**同一套** .trim() 等价（那边逐行 inline
// `.trim()`，无导出 helper 可 import，故此处镜像同一个裸 .trim()——不另发明第三种
// 等价）。原因：兄弟行 forward-fill 后，某列常有「一格带首尾空白、另一格没有」的
// 手写异构；isSharedColumn 用 .trim() 判它们为合并共享格，确认保存时对整组做**一次**
// 批量写（一份内容写满全组，见 spec §4.4 / groupCellWriteTargets）。去重 key 若按
// 逐字节精确，这两格会各放一次 temperature=1.0 调用、拿到两份不同候选；弹窗逐格
// 展示，保存却把整组覆盖成最后处理那格的候选——用户对每格的确认被悄悄作废，落库
// 与展示对不上（正是本 memo 要消除的「显示≠落库」类）。key 走同一套 .trim() 后，
// 同一 .trim() 等价类只成功调用一次、其余扇出复用同一候选，与整组批量写落库的那
// 一份天然一致。.trim() 只归一首尾空白，内部差异仍是不同 key（与 isSharedColumn
// 语义一致）。
//
// 用 JSON.stringify 包一个二元组而不是字符串拼接+分隔符——任意分隔符都可能恰好
// 出现在真实格子内容或列 id 里，拼接会让两个不同的 (columnId, trimmed) 组合意外
// 撞成同一个 key（例如 columnId="a"、trimmed="b|c" 与 columnId="a|b"、trimmed="c"
// 若用"|"拼接会得到同一个字符串）；JSON.stringify 对字符串数组的转义无歧义，两个
// 不同的二元组恒产生不同的 JSON 字符串。
export function reformatDedupeKey(columnId: string, originalMd: string): string {
  return JSON.stringify([columnId, originalMd.trim()]);
}

// 「扇出」：把已经拿到的候选结果（缓存命中，或本来就是这一格自己的真实网络
// 结果）套用到某个仍处于 pending 的格子——语义上是 markReformatItemRunning
// 与 applyReformatResult 这两个既有转移函数的固定组合（等价于"这一格立刻
// 经过 running 直接拿到结果"，只是这次没有真的发起请求）。调用方（runBatch
// 的去重循环）命中缓存时只需调这一个函数，不必自己在 .tsx 里拼两步转移；
// 同一份 result 对象被反复传给不同 (rowId, columnId) 调用，就是"一次结果
// 扇出给多个重复格"的完整实现——两次调用各自产出的 candidateMd 字面相同
// （来自同一个 result 引用），这正是修复要保证的效果。phase/状态双重门槛
// 与 applyReformatResult 完全一致（非 running 阶段、或目标项不是 pending，
// 都原样返回）：不会覆盖已经在处理中或已经有结论的格子。
//
// **`changed` 必须按目标格自己的 originalMd 重算，不能沿用缓存里那份 flag。**
// 去重 key 归一到 .trim()（对齐保存侧合并共享格的 .trim() 等价扇出），所以
// 「foo」与「foo 」落在同一等价类、共享同一份 candidateMd——候选本就该扇出一份，
// 这是对的。但缓存里的 result.changed 是**第一次处理的那一格**算出来的：若
// "foo"（本就干净 → changed=false）先处理，把 changed=false 复用到 "foo " 会把
// 后者误标 unchanged、排除出待保存集、尾随空白永不规整（静默跳过）；反向同理。
// 修复只重算这个 FLAG——candidateMd !== 目标格自己的 originalMd 即为有改动，
// 与后端若真对这一格调用 reformat_cell 会返回的 changed 逐字等价（originalMd 是
// 建批次那一刻该格的已保存内容快照，正是后端会读到的原文）。
export function applyReformatCachedResult(
  state: ReformatBatchState,
  rowId: string,
  columnId: string,
  result: { candidateMd: string; source: string; changed: boolean },
): ReformatBatchState {
  const running = markReformatItemRunning(state, rowId, columnId);
  const target = findReformatItem(running, rowId, columnId);
  // target 为 null（未知键）或未进入 running（phase/状态门槛未过）时，下面的
  // applyReformatResult 本就是 no-op，changed 取什么都不影响结果——回退到缓存
  // 里的 changed 只为不引入无意义的差异。
  const changed = target ? result.candidateMd !== target.originalMd : result.changed;
  return applyReformatResult(running, rowId, columnId, { ...result, changed });
}

// ===========================================================================
// 6. 批量保存：合并共享格「扇出等价类」去重（代码评审 F1）
// ===========================================================================
//
// 上面第 5 节的按内容去重（reformatDedupeKey）解决的是 **run 阶段** 的重复
// 「LLM 调用」；本节解决的是 **save 阶段** 的重复「保存请求」——两者独立。
//
// anchor 分组里，某个「合并共享格」列（组内多于一行、该列全分支 trim 后同值，
// 见 knowhow-grouping-logic.ts isSharedColumn）一旦有改动，整表批量会为组内
// **每一** 兄弟行各产生一个 changed 条目（去重后它们的 candidateMd 逐字相同）。
// 此前 runSave 对每个 changed 条目各调一次 onSaveCell（= knowhow-panel.tsx 的
// handleCellSave），而 handleCellSave 又把 **每一次** 调用都扇写到组内全部
// rowId（groupCellWriteTargets）：N 个兄弟 → N 次 batchPatch 请求、每次写 N 行
// = N² 次行 upsert、N 次投影版本 bump。内容相同，正确性无碍，但纯属浪费。
//
// 修复：保存循环开始前，把「会扇写到同一组」的 changed 条目合并成 **一个**
// 代表保存（representative），其余成员的终态跟随这次代表保存的结果（成功→全
// saved，失败→全 save_error，stale→全 stale_skipped，见第 7 节）——它们物理上
// 本就被这一次扇写覆盖到了。判定 **完全复用** handleCellSave 那套
// groupRowsByAnchor / isSharedColumn / groupCellWriteTargets，不另发明等价：
// 两条路对「哪些行会被一次写入一起覆盖」的答案必须逐字一致，否则代表保存写到
// 的行集与这里合并的成员集对不上，会漏标或误标某些格的终态。
//
// 边界：非共享列（记录型表、无 anchor、或该列在组内并非全分支同值）不合并，
// 每个 changed 条目各自成一个 unit——与改动前逐格保存的行为完全一致。行作用域
// （rows 只含单行）天然落到「单行组 isSharedColumn 恒 false」，也不合并（本就
// 只有一个条目，无重复可去）。

/** 写目标：representative 一次保存经 handleCellSave 扇写到的**一格**（含建批次
 *  那一刻该格的 originalMd 快照，作查陈旧的基线）。可能超出展示成员——合并共享
 *  列的兄弟行不一定都在 batch.items 里（行作用域下只有被选中行是展示条目）。 */
export type ReformatSaveWriteTarget = {
  rowId: string;
  columnId: string;
  /** 建批次那一刻该写目标格的已保存内容快照——保存前把它与当前落库内容比对判
   *  stale 的基线（fan-out 会覆盖它，故它自建批次以来若被他人改动就整组跳过）。
   *  取自 planReformatSaves 收到的**完整** detail.rows 的对应格，与展示成员的
   *  originalMd 同源（同一份 detail 快照）。 */
  originalMd: string;
  /** round-4 P1：该行 anchor 列在建批次那一刻的快照值（记录型表/无 anchor 列时为
   *  ""）。fan-out 的守卫写把它作为**每行**的 anchor 基线下发给后端——后端在同一
   *  事务内重读该行 anchor 列，任一行的 anchor 自快照以来被移出组就整组 409（离组
   *  行不再被冻结扇出误写；被编辑格没变、expected_before 单独拦不住这类漂移）。与
   *  originalMd 同源于传入的那份完整 detail.rows 快照。 */
  anchorMd: string;
};

export type ReformatSaveUnit = {
  /** onSaveCell 实际调用的那一格——handleCellSave 会把它的 candidateMd 扇写到
   *  整个合并共享组（groupCellWriteTargets），因此本单元其余写目标物理上都会被
   *  这一次调用写到，无需各自再调一次。取 changed 输入顺序里的第一个成员。 */
  representative: ReformatBatchItem;
  /** **展示/状态**成员：本扇出等价类里的 changed batch item（含 representative，
   *  保持 changed 输入顺序）——它们的终态一律跟随 representative 这一次保存的
   *  结果，且驱动弹窗逐格状态流转。受作用域限制（行作用域只含被选中行那一格），
   *  故可能**少于**下面的 writeTargets（弹窗不展示其它兄弟行）。 */
  members: ReformatBatchItem[];
  /** **写目标全集**：representative 这一次保存经 handleCellSave 扇写到的**全部**
   *  格（合并共享列 = 组内全部兄弟行，取自完整 detail.rows；非共享 = 仅这一格）。
   *  保存前的 stale 比对走这一份——扇写会覆盖它们，任一自建批次以来被他人改动就
   *  整组跳过，不拿旧内容算出的候选盲写。行作用域下可能**超出** members（兄弟行
   *  不在 batch.items 里、不展示，但物理上仍会被扇写到，必须一并查陈旧）。 */
  writeTargets: ReformatSaveWriteTarget[];
  /** round-6 P1：本单元的 writeTargets 是否**恰好**是建批次那一刻该 anchor 组的
   *  **完整**成员集——唯有此时后端「组成员精确相等」结构守卫才成立（joiner/leaver
   *  表现为 current≠frozen）。`true` 的两类：①合并共享列扇写（writeTargets=整组）；
   *  ②**单例组**（组内仅一行，writeTargets=那一行=整组）。`false` 的一类是回归防线：
   *  多行组里的**非共享列**——writeTargets 是**有意的子集**（只写这一行），若也带
   *  守卫，后端会把「组里本就有别的兄弟行」误判成 joiner 漂移、把每次合法的逐行
   *  属性规整都假 409。记录型表（无 anchor 组）恒 `false`。runSave 据此决定是否给
   *  该单元下发 anchorGuard；下发了的（=整组写）handleCellSave 一律走 guarded 批量
   *  端点，令服务端跑「指定未变 + 组成员精确相等 + expected_before」三重校验。 */
  coversAnchorGroup: boolean;
};

// 把待保存的 changed 条目按「会扇写到同一组」合并成代表保存单元。`rows` 必须传
// **与 handleCellSave 同源** 的那份 **完整** detail.rows（**两种作用域都传全量**，
// 不是被选中行的子集）——handleCellSave 用完整 detail.rows 重算 anchor 组并把候选
// 扇写到组内每一个兄弟行，故 unit 的写目标（含 stale 比对）必须在同一份完整行上
// 算，才与真正被写到的格严格一致。行作用域下 batch.items（展示成员）只含被选中
// 行，但写目标仍要覆盖全部兄弟——否则非展示兄弟被并发改动时既不查陈旧也不展示，
// 会被静默覆盖。`anchorColumnId` 为 null（记录型表）时不分组，每个条目各自独立。
export function planReformatSaves(
  changed: ReformatBatchItem[],
  rows: KnowhowRow[],
  anchorColumnId: string | null,
): ReformatSaveUnit[] {
  // rowId -> 它所属的 anchor 组（只在有 anchorColumnId 时构建；分组算一次、
  // 供每个 changed 条目 O(1) 查回，避免 per-item 重复 groupRowsByAnchor）。
  const groupByRowId = new Map<string, AnchorGroup>();
  if (anchorColumnId) {
    for (const group of groupRowsByAnchor(rows, anchorColumnId)) {
      for (const row of group.rows) groupByRowId.set(row.id, group);
    }
  }
  // rowId -> row：O(1) 查回写目标格的 originalMd 基线（含不在 batch.items 里的
  // 兄弟行——正是本次修复要纳入 stale 比对的那些）。
  const rowById = new Map<string, KnowhowRow>();
  for (const row of rows) rowById.set(row.id, row);
  // round-4 P1：某行 anchor 列在建批次那一刻的快照值（无 anchor 列 → ""）。fan-out
  // 的守卫写把它当每行的 anchor 基线下发给后端，据此拦「离组行被冻结扇出误写」。取自
  // 传入的这份 rows 快照，与 originalMd 同源（runSave 传 snapshotRef.current.allRows）。
  const anchorOf = (rowId: string): string =>
    anchorColumnId ? rowById.get(rowId)?.cells[anchorColumnId] ?? "" : "";

  const units: ReformatSaveUnit[] = [];
  const unitBySignature = new Map<string, ReformatSaveUnit>();
  for (const item of changed) {
    const group = groupByRowId.get(item.rowId);
    if (!group || !isSharedColumn(group, item.columnId)) {
      // 非共享格：只写这一格，写目标 = 展示成员本身（基线用它自己的 originalMd
      // 快照，与改动前逐格 onSaveCell + 逐格 stale 比对的行为完全一致）。
      // round-6：这一格覆盖整组 ⟺ 它就是个**单例组**（组内仅一行）——此时 writeTargets
      // ={这一行}=整组，后端「组成员精确相等」守卫成立（建批次后有兄弟行 join 即
      // current≠frozen → 409）。多行组里落到这个分支的一定是**非共享列**（子集写），
      // 带守卫会假 409，故 coversAnchorGroup=false。记录型表 group 为 undefined，恒 false。
      units.push({
        representative: item,
        members: [item],
        writeTargets: [
          { rowId: item.rowId, columnId: item.columnId, originalMd: item.originalMd, anchorMd: anchorOf(item.rowId) },
        ],
        coversAnchorGroup: !!group && group.rows.length === 1,
      });
      continue;
    }
    // 合并共享格：同一 (列, 写目标组) 的全部 changed 成员合并成一个代表保存。
    // signature = (列, 排序后的写目标 rowId 集)：groupRowsByAnchor 对行做划分、
    // 各组 rowId 互斥，故不同概念组即使同列也因写目标集不同而不撞成同一 unit；
    // 排序消除组内行序差异带来的 key 抖动。JSON.stringify 二元组避免分隔符歧义
    // （同 reformatDedupeKey 的理由）。
    const targetRowIds = groupCellWriteTargets(group, item.columnId);
    const signature = JSON.stringify([item.columnId, [...targetRowIds].sort()]);
    const existing = unitBySignature.get(signature);
    if (existing) {
      existing.members.push(item);
      continue;
    }
    // 写目标基线：组内**每一行**该列的 detail.rows 快照内容——含不在 batch.items
    // 里的兄弟行（行作用域下它们不展示，但 handleCellSave 的扇写仍会覆盖它们，
    // 故必须一并查陈旧）。缺失的行（理论上不该发生，groupCellWriteTargets 的
    // rowId 全部来自同一份 rows）退化成空串基线——比对时几乎必然判 stale，保守。
    const writeTargets: ReformatSaveWriteTarget[] = targetRowIds.map((rowId) => ({
      rowId,
      columnId: item.columnId,
      originalMd: rowById.get(rowId)?.cells[item.columnId] ?? "",
      anchorMd: anchorOf(rowId),
    }));
    // round-6：合并共享列扇写——writeTargets = groupCellWriteTargets(group) = 组内全部
    // 兄弟行 = 整组，故 coversAnchorGroup=true，带 anchorGuard 走 guarded 批量端点。
    const unit: ReformatSaveUnit = { representative: item, members: [item], writeTargets, coversAnchorGroup: true };
    unitBySignature.set(signature, unit);
    units.push(unit);
  }
  return units;
}

// ===========================================================================
// 7. 批量保存：保存前比对当前落库内容，拒绝覆盖他人编辑（代码评审 F2）
// ===========================================================================
//
// 建批次到点「确认保存」之间可能隔了几分钟——另一个标签页/用户可能已经改了
// 某格。直接把 **基于旧内容算出的候选** PATCH 下去会静默覆盖那次更新（正是
// CLI 侧现在用 expected-before 挡住的「移动目标」类）。后端不改 API，改由前端
// 客户端侧「比对-跳过」：保存循环开始前 refetch 整表一次（一趟往返换掉逐格
// 往返），把每格当前落库内容与建批次那一刻的 originalMd 快照比对，不一致就
// 跳过对应的 save unit（distinct 终态 stale_skipped + 友好文案，见
// markReformatItemStaleSkipped / BATCH_REFORMAT_STALE_SKIP_TEXT），计入 summary，
// 其余照常保存。

// (rowId, columnId) -> 稳定字符串键，用于把 refetch 到的当前落库内容按格索引。
// JSON.stringify 二元组，避免分隔符歧义（同 reformatDedupeKey 的理由）。
export function reformatSaveCellKey(rowId: string, columnId: string): string {
  return JSON.stringify([rowId, columnId]);
}

// 某个 save unit 是否因「建批次后被他人改动」而应整组跳过：只要本单元 **任一**
// **写目标**（representative 扇写会覆盖到的格，含行作用域下不展示的兄弟行）的当前
// 落库内容 ≠ 建批次那一刻的 originalMd 快照，即判 stale——representative 的扇写会
// 用同一份候选覆盖全部写目标，任一写目标的底稿已经变了就不能盲写。**走 writeTargets
// 而非 members**：members 只是展示子集（行作用域只含被选中行），若只查它就会漏掉被
// 并发改动的非展示兄弟行、把别人的编辑静默覆盖（正是 F1 要堵的洞）。`current` 为
// reformatSaveCellKey(rowId,columnId) -> 当前落库内容 的映射（调用方保存前 refetch
// 整表一次构建）。写目标键缺失（该格已被删除/清空）同样算 stale——宁可跳过让用户
// 重跑，也不冒盲写风险。比对用逐字相等（非 trim）：快照后的任何改动——哪怕只是
// 空白——都意味着有人动过这格，保守跳过。
export function isReformatUnitStale(unit: ReformatSaveUnit, current: Map<string, string>): boolean {
  return unit.writeTargets.some((target) => {
    const key = reformatSaveCellKey(target.rowId, target.columnId);
    if (!current.has(key)) return true;
    return current.get(key) !== target.originalMd;
  });
}
