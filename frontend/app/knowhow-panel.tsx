/**
 * knowhow-panel.tsx
 *
 * 「Knowhow 表」总览：表列表 → 表格网格 → 行详情抽屉 三层，外加导入向导 /
 * 建表向导 / 表管理三个 modal 的挂载点。page.tsx 只负责接线（导航按钮 +
 * `<KnowhowPanel notebookId apiBase canEdit onClose />` 一处挂载，外加 Task 12
 * 引用跳转用的可选 `initialTableId`/`initialRowId`），面板自身的状态机与渲染
 * 都集中在这里。
 *
 * 引用跳转（Task 12，跳转目标 Task 11 改）：ask 回答里命中 knowhow 格子的
 * 引用点「在表格中查看」时，page.tsx 打开本面板并传入
 * `initialTableId`/`initialRowId`——挂载（或这两个 prop 变化）时自动选中该
 * 表；detail 加载后按该表有没有 anchor 列分流展开目标：有 anchor 定位命中行
 * 所在的概念组，打开矩阵抽屉并高亮该分支列（规格 §4.5）；记录型表（无
 * anchor）仍展开原有的行详情抽屉。目标表/行已被删除等陈旧 id 会给出内联
 * 提示并回退到列表/整表视图，而不是卡在一个死胡同式的错误。
 *
 * 纯逻辑（行过滤 / 列序 / 状态徽标映射 / 抽屉标题 / 图片鉴权判定）都在
 * knowhow-panel-logic.ts 里（无 JSX，供 knowhow-panel.test.mjs 直接 import——
 * Node 原生 TS 类型剥离不支持 .tsx，只能拆到 .ts）。
 *
 * 权限（PR-2+3 Task 5）：`canEdit`（= notebook 内容写权限，page.tsx 传
 * `!readOnlyWorkspace`）统一门控**全部写入口**——新建表 / 导入表格 / 添加行 /
 * 管理 / 重建投影 / 删除表 / 失败行「重试」。⚠ P2-T2 起是 `!readOnlyWorkspace`
 * 而**不是** `!isReader`:knowhow 写是内容写（knowhow:write=admin），组管理员
 * 有权（access 仍是 reader 却 canEdit=true）。无写权者（canEdit=false）看到纯
 * 浏览视图，一个写按钮都不出现（规格⑦「只读成员可看」）。唯一例外（C3）：
 * 「复制/移动到…」不受 canEdit 整体门控——它不写入本笔记本，copy 只需对源
 * 笔记本有读权限即可，只读成员也能看到并使用（把表复制到自己另有写权限的
 * 笔记本）；`allowMove={canEdit}` 单独收紧「移动」这一半（会从源删除，需要
 * 源的写权限），交给 DestinationPicker（C2）据此隐藏移动选项。
 *
 * 编辑入口的分工：建表向导 + 表/列/行管理在 knowhow-manage.tsx；导入向导在
 * knowhow-import.tsx；格子内容编辑浮窗（预览态 KnowhowCellPreview / 编辑态
 * KnowhowCellEditor）在 knowhow-cell-editor.tsx（Task 7），本文件只持有
 * 「当前打开的是哪一格 + 预览还是编辑」这一顶层状态机（cellModal）与三个click
 * 路由入口：网格非行标题列格子点击、行详情抽屉分节「编辑」按钮、「添加行」
 * 建空行后自动打开首格编辑态。本文件对这些 modal 只做「打开/关闭 + 完成后
 * 刷新」的接线，不重复实现格子编辑器内部的任何逻辑。
 */
"use client";

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type MouseEvent as ReactMouseEvent,
} from "react";
import {
  Check,
  ChevronLeft,
  Copy,
  Download,
  Edit3,
  History,
  ListChecks,
  ListPlus,
  Loader2,
  Plus,
  RefreshCw,
  Search,
  Settings2,
  Sparkles,
  Trash2,
  Upload,
  X,
} from "lucide-react";
import { requestBlob } from "./api-client.ts";
import { fetchModelServiceStatus } from "./model-services.ts";
import { modeLabel } from "./ask-modes.ts";
import { ReasoningTracePanel } from "./answer-panel.tsx";
import { Pagination } from "./Pagination";
import { useClientPagination } from "./use-client-pagination.ts";
import { useFloatingWindow } from "./use-floating-window.ts";
import {
  ROLE_LABELS,
  cellSummary,
  fetchKnowhowTables,
  fetchKnowhowTable,
  deleteKnowhowTable,
  reprojectKnowhowTable,
  addKnowhowRow,
  deleteKnowhowRow,
  patchKnowhowCell,
  batchPatchKnowhowCells,
  knowhowTemplateUrl,
  optimizeKnowhowCell,
  completeKnowhowRow,
  reformatKnowhowCell,
  fetchKnowhowRowCodeByColumn,
  filterKnowhowTables,
  patchKnowhowColumn,
  type KnowhowTableSummary,
  type KnowhowHealthFilter,
  type KnowhowTableDetail,
  type KnowhowRow,
  type KnowhowColumn,
  type KnowhowCellCode,
  type KnowhowRowCompletion,
  type KnowhowRowCompletionSuggestion,
  type Role,
  type ProjectionStatus,
} from "./knowhow-model.ts";
import {
  COMPLETION_CONFIDENCE_LABELS,
  canAcceptCompletionSuggestion,
  completableKnowhowColumns,
  completionEvidenceForSuggestion,
  completionEvidenceTierLabel,
  completionRetrievalStatusLabel,
  completionSavePlan,
  completionReferenceLabel,
} from "./knowhow-complete-logic.ts";
import {
  filterRows,
  sortColumnsByPosition,
  orderColumnsForGrid,
  PROJECTION_STATUS_LABELS,
  PROJECTION_STATUS_TONE,
  isRetryableProjectionStatus,
  resolveRowTitleText,
  appendRowOptimistically,
} from "./knowhow-panel-logic.ts";
import {
  groupRowsByAnchor,
  computeGridSpans,
  groupCellWriteTargets,
  isSharedColumn,
} from "./knowhow-grouping-logic.ts";
import { extractErrorMessage } from "./knowhow-import-logic.ts";
import { httpErrorStatus } from "./errors.ts";
import { rowFallbackTitle, reformatSourceLabel, REFORMAT_SUGGESTION_LABEL } from "./knowhow-cell-editor-logic.ts";
import { KnowhowImportWizard, KnowhowAppendWizard } from "./knowhow-import.tsx";
import { KnowhowCreateWizard, KnowhowManageModal } from "./knowhow-manage.tsx";
import { KnowhowMarkdown, KnowhowCellPreview, KnowhowCellEditor } from "./knowhow-cell-editor.tsx";
// knowhow 表版本管理 Task 16：格子浮窗第三态（历史）——与 KnowhowCellPreview/
// KnowhowCellEditor 同一套 cellModal 顶层状态机驱动，见下方 cellModal 类型与
// 渲染处的三选一分支。
import { KnowhowCellHistory } from "./knowhow-cell-history.tsx";
import { KnowhowCodeChip, KnowhowCodeModal } from "./knowhow-code.tsx";
import { KnowhowMatrixDrawer } from "./knowhow-matrix-drawer.tsx";
// knowhow 表版本管理 Task 15：历史抽屉（时间线 + 单次改动 diff + 两版对比 +
// 回退 + 里程碑）。与 KnowhowMatrixDrawer 同一挂载模式——面板自持
// historyOpen 显隐态，本文件只负责打开/关闭 + 回退成功后重拉表详情。
import { KnowhowHistoryDrawer } from "./knowhow-history-drawer.tsx";
// C3：表级「复制/移动到…」——目标笔记本选择器是 C2 的共享组件（同一层 UI 也
// 服务 Memory 的 C4），网络客户端与其 409 source_cleanup_failed 专用错误类型
// 在 knowhow-transfer.ts（共享 transport 的领域客户端，无 JSX，见该文件头注释）。
import { DestinationPicker } from "./transfer-picker.tsx";
import { KnowhowPanelStyles } from "./knowhow-panel-styles.tsx";
import { transferKnowhowTable, KnowhowSourceCleanupError } from "./knowhow-transfer.ts";
import { resolveCellCodeView, CODE_STATUS_LOAD_ERROR } from "./knowhow-code-logic.ts";
import { diffKnowhowMarkdown, type KnowhowDiffLine } from "./knowhow-markdown-diff.ts";
import {
  computeKnowhowColumnWidths,
  sampleVisibleKnowhowRows,
} from "./knowhow-column-widths.ts";
import { isFloatingDisabledWidth } from "./floating-window-logic.ts";
import {
  ACCEPT_SUGGESTION_LABEL,
  OPTIMIZE_ORIGINAL_LABEL,
  OPTIMIZE_SUGGESTION_LABEL,
  ROW_OPTIMIZE_ABORT_LABEL,
  ROW_OPTIMIZE_BUTTON_LABEL,
  ROW_OPTIMIZE_CLOSE_LABEL,
  ROW_OPTIMIZE_DONE_TEXT,
  ROW_OPTIMIZE_EMPTY_TEXT,
  ROW_OPTIMIZE_RETRY_LABEL,
  ROW_OPTIMIZE_SKIP_LABEL,
  ROW_OPTIMIZE_STATUS_LABELS,
  abortQueue,
  applyError,
  applySuggestion,
  beginAcceptCurrent,
  completeAcceptCurrent,
  currentRowOptimizeItem,
  failAcceptCurrent,
  initRowOptimizeQueue,
  isRowOptimizeQueueFinished,
  markCurrentInProgress,
  retryCurrent,
  rowOptimizeProgress,
  skipCurrent,
  templateDownloadFilename,
  type RowOptimizeItem,
  type RowOptimizeQueueState,
  BATCH_REFORMAT_ROW_BUTTON_LABEL,
  BATCH_REFORMAT_TABLE_BUTTON_LABEL,
  BATCH_REFORMAT_START_LABEL,
  BATCH_REFORMAT_ABORT_LABEL,
  BATCH_REFORMAT_CONFIRM_LABEL,
  BATCH_REFORMAT_DISCARD_LABEL,
  BATCH_REFORMAT_CLOSE_LABEL,
  BATCH_REFORMAT_EMPTY_TEXT_ROW,
  BATCH_REFORMAT_EMPTY_TEXT_TABLE,
  BATCH_REFORMAT_DONE_TEXT,
  BATCH_REFORMAT_STATUS_LABELS,
  batchReformatScaleText,
  reformatReviewingSummaryText,
  initReformatBatch,
  reformatBatchSummary,
  beginReformatBatchRun,
  beginReformatBatchRetry,
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
  applyReformatCachedResult,
  markReformatItemRunStale,
  planReformatSaves,
  reformatSaveCellKey,
  isReformatUnitStale,
  markReformatItemStaleSkipped,
  resolveReformatGenerationConcurrency,
  runReformatGenerationPool,
  openSavedReformatCellAfterReload,
  REFORMAT_OPEN_CELL_ERROR,
  BATCH_REFORMAT_RETRY_LABEL,
  BATCH_REFORMAT_STALE_SKIP_TEXT,
  type ReformatBatchState,
  type ReformatBatchItem,
  type ReformatBatchScope,
  type ReformatGenerationResult,
} from "./knowhow-optimize-logic.ts";

// ---------------------------------------------------------------------------
// KnowhowPanel — 顶层：全屏 dialog 外壳 + 三层状态机
// ---------------------------------------------------------------------------

export interface KnowhowPanelProps {
  notebookId: string;
  apiBase: string;
  /** notebook 内容写权限（page.tsx 传 `!readOnlyWorkspace`，P2-T2 起含组管理员）：
   * false=无写权，隐藏全部写入口（新建/导入/添加行/管理/重建投影/删除表/失败重试）。 */
  canEdit: boolean;
  /** 关闭整个面板（page.tsx 用于收起挂载它的 knowhowOpen 态）。 */
  onClose: () => void;
  /** Task 12（引用跳转）：非空时挂载（或这两个 prop 变化时）自动打开该表，
   * 若 initialRowId 也给了、且该表有 anchor 列，进一步定位命中行所在的概念
   * 组并打开矩阵抽屉高亮该分支列（Task 11，规格 §4.5）；无 anchor 的记录型
   * 表仍展开该行的行详情抽屉。目标已不存在时面板自己给出内联提示并回退，
   * 不需要调用方处理。 */
  initialTableId?: string | null;
  initialRowId?: string | null;
  initialHealthFilter?: KnowhowHealthFilter;
}

export function KnowhowPanel({
  notebookId, apiBase, canEdit, onClose, initialTableId, initialRowId, initialHealthFilter,
}: KnowhowPanelProps) {
  const [tables, setTables] = useState<KnowhowTableSummary[] | null>(null);
  const [tablesLoading, setTablesLoading] = useState(false);
  const [tablesError, setTablesError] = useState<string | null>(null);
  const [healthFilter, setHealthFilter] = useState<KnowhowHealthFilter>(initialHealthFilter ?? "all");
  const visibleTables = useMemo(
    () => filterKnowhowTables(tables ?? [], healthFilter),
    [tables, healthFilter],
  );

  useEffect(() => {
    setHealthFilter(initialHealthFilter ?? "all");
  }, [initialHealthFilter]);

  // modal 的显隐：面板自持状态，不经 page.tsx 转发——page.tsx 挂载
  // <KnowhowPanel> 时只传 notebookId/apiBase/canEdit/onClose 四个 prop，
  // 导入/新建/管理/追加入口完全由本文件内部驱动。
  const [importOpen, setImportOpen] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [manageOpen, setManageOpen] = useState(false);
  // C3：「复制/移动到…」目标笔记本选择器（DestinationPicker）显隐——与
  // manageOpen 同级的顶层 modal 状态，挂载点见 render 尾部。
  const [transferOpen, setTransferOpen] = useState(false);
  // 复审 Minor：复制成功的轻量提示——move 靠 backToList()+ 表列表刷新已自证
  // 成功，copy 源视图不变、目标又不在眼前，不给提示用户没法确认"复制到底有
  // 没有效果"。复用 jumpNotice 同一套 .knowhow-jump-notice 视觉(信息蓝，不新
  // 造 chrome)，但用独立 state 而不是接了 jumpNotice 本身——理由同
  // conceptDrawerError 与 actionError 分开维护的理由一样：两件事互不相关时不
  // 该互相覆盖(比如打开一张有陈旧引用提示的表、随手又复制了一次，两条提示不
  // 该互吃)。
  const [transferNotice, setTransferNotice] = useState<string | null>(null);
  // Task 9：追加导入向导显隐 + 模板下载进行中标记（工具栏「下载模板」按钮
  // 自身的 loading 态，不走 actionError/detailError 那一套整表级错误）。
  const [appendOpen, setAppendOpen] = useState(false);
  const [templateDownloading, setTemplateDownloading] = useState(false);
  // Task 9：行详情抽屉「优化整行」批量弹窗——null=未打开，否则是正在批量
  // 优化的行 id（与 cellModal 平级的另一个顶层 modal 状态，堆叠在抽屉之上，
  // 镜像 cellModal 自己的挂载方式）。
  const [optimizeRowId, setOptimizeRowId] = useState<string | null>(null);
  // 行详情「智能补全空列」审阅弹窗；只保存目标行 id，建议由弹窗按当前快照
  // 显式请求，关闭/切行即卸载并用请求归属守卫丢弃晚到响应。
  const [completeRowId, setCompleteRowId] = useState<string | null>(null);
  const completionTriggerRef = useRef<HTMLButtonElement | null>(null);
  const completionFallbackFocusRef = useRef<HTMLElement | null>(null);
  // knowhow-md-normalize Task 9：「一键规整」批量弹窗——行作用域用 rowId 记
  // 正在批量规整哪一行（与 optimizeRowId 同一种"堆叠在抽屉之上"的顶层 modal
  // 状态）；表作用域只是个布尔开关（作用对象是 detail.rows 整体，不需要再
  // 记哪一行）。两者共用同一个 KnowhowReformatBatchModal 组件，只是
  // scope/rows 参数不同（见该组件定义处注释）。
  const [reformatRowId, setReformatRowId] = useState<string | null>(null);
  const [reformatTableOpen, setReformatTableOpen] = useState(false);
  // knowhow 表版本管理 Task 15：历史抽屉显隐——与 manageOpen/transferOpen 同级
  // 的顶层 modal 状态，工具栏「历史」按钮打开，抽屉自己管理时间线/diff/回退/
  // 里程碑的全部内部状态（本文件只负责显隐 + 回退成功后重拉表详情）。
  const [historyOpen, setHistoryOpen] = useState(false);

  const [selectedTableId, setSelectedTableId] = useState<string | null>(null);
  const [detail, setDetail] = useState<KnowhowTableDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const panelMountedRef = useRef(true);
  useEffect(() => {
    panelMountedRef.current = true;
    return () => {
      panelMountedRef.current = false;
    };
  }, []);
  // Task 12（引用跳转）：跳转目标（表/行）已不存在时的友好提示——与
  // actionError 分开是因为这不是「操作失败」，是「引用陈旧」，文案与触发
  // 时机都不同（见下方两个 initialTableId/initialRowId 相关 effect）。
  const [jumpNotice, setJumpNotice] = useState<string | null>(null);

  const [query, setQuery] = useState("");
  const [openRowId, setOpenRowId] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [retryingReproject, setRetryingReproject] = useState(false);
  const [addingRow, setAddingRow] = useState(false);

  // 格子浮窗顶层状态机（Task 7；knowhow 表版本管理 Task 16 扩三态）：null=未
  // 打开；mode="preview"=已填格子的只读预览态（右上「编辑」切到 edit，「历史」
  // 切到 history）；mode="edit"=编辑态（同样有「历史」入口）；mode="history"=
  // 历史页签（这一格的历次值 + 恢复此版本，见 knowhow-cell-history.tsx）。行
  // 标题列格子（网格首列）仅在「已填」时不走这里——那时仍走既有的 onOpenRow
  // 打开整行抽屉，与规格⑤「点行首/概念列打开」行详情抽屉的既有约定一致；该格
  // 为空时和其余空格子一样直接进本状态机的编辑态（规格②路A「空格子直接进
  // 编辑态」），见下方 KnowhowTableGrid 的 `opensRowDrawer = index === 0 &&
  // filled` 判定。三态共用同一份 useFloatingWindow storageKey / useFullscreen
  // Toggle 键（见 knowhow-cell-editor.tsx FULLSCREEN_STORAGE_KEY 头注释），切
  // 页签时浮窗位置/尺寸不跳动。
  const [cellModal, setCellModal] = useState<
    { rowId: string; columnId: string; mode: "preview" | "edit" | "history" } | null
  >(null);

  // Task 8（点概念打开矩阵抽屉，规格 §4.3）：当前打开的概念值——null=未打开；
  // 非空时对应 anchor 列某个分组的 anchorValue，驱动下方渲染
  // KnowhowMatrixDrawer（与 cellModal/codeModal 平级的另一个顶层 modal 状态，
  // 堆叠在网格之上）。
  const [openConceptValue, setOpenConceptValue] = useState<string | null>(null);

  // Task 11（引用命中→跳概念矩阵抽屉+高亮，规格 §4.5）：ask 引用跳转命中的
  // 分支（行 id）——null=无高亮；非空时传给 KnowhowMatrixDrawer 的
  // highlightRowId prop，抽屉据此高亮该分支列（表头 + 该行全部格子）。只在
  // 「有 anchor 的表被引用跳转命中」时非空，其余打开方式（点概念格/加分支/
  // 加概念）不设置，恒为 null——与 openConceptValue 同生命周期但不完全同步：
  // openConceptValue 变化时不逐一清空这个值（那样会清掉刚设好的高亮，见下方
  // 引用跳转 effect 的注释），只在关闭抽屉/切表/返回列表等「离开当前概念」
  // 的位置与 openConceptValue 并列清零。
  const [highlightRowId, setHighlightRowId] = useState<string | null>(null);

  // Task 10（删概念）：矩阵抽屉「删除整个概念」二次确认态——镜像表级
  // confirmDelete/deleting 那一对状态（KnowhowTableGrid 消费的同名 prop），
  // 只是作用对象从"这张表"换成"当前打开的概念组"。openConceptValue 变化
  // （切概念/关抽屉）时下面紧跟的 effect 统一重置，不必在每个改
  // openConceptValue 的地方各自补一行重置代码。
  const [confirmDeleteConcept, setConfirmDeleteConcept] = useState(false);
  const [deletingConcept, setDeletingConcept] = useState(false);
  // 复审 Important 修复：矩阵抽屉内操作（加分支/删概念）失败的错误——与
  // actionError 分开维护，因为 actionError 只在 KnowhowTableGrid 渲染成横幅，
  // 被抽屉的 .kh-modal-overlay（z-index 65）盖住，抽屉开着时用户看不到。
  // addBranch/deleteConcept 失败时仍然并行写 actionError（不改变既有行为：
  // 关闭抽屉后主网格横幅照常出现），同时把同一条文案写进这里传给
  // KnowhowMatrixDrawer 就地渲染。用独立 state 而不是直接透传 actionError，
  // 是为了避免一个无关操作（改名/重新投影/下载模板等）留下的陈旧 actionError
  // 在用户打开一个全新的矩阵抽屉时被误显示——openConceptValue 变化（切概念/
  // 关抽屉）时随 confirmDeleteConcept/deletingConcept 一并重置，保证每次
  // 打开都是干净状态。
  const [conceptDrawerError, setConceptDrawerError] = useState<string | null>(null);

  // Follow-up B（删分支，复审 Important 修复）：单个分支删除请求进行中的
  // 目标 rowId——null=当前没有删除在途。镜像 addingRow 的既有重入防护用法
  // （deleteBranch 顶部据此短路重入调用），但用 rowId 而非布尔值：删除按
  // 具体某一行发起，用 rowId 既能在 deleteBranch 内部短路重入（避免并发的
  // 第二次调用读到同一份 stale openConceptGroup.rows.length、两次都误判"非
  // 最后一支"、最终留下一个悬挂指向已删概念的 openConceptValue——详见
  // deleteBranch 函数体注释），也原样传给 KnowhowMatrixDrawer 驱动"这一列"
  // 表头确认按钮的 disabled + "删除中…"文案，不影响其余分支列的入口。
  const [deletingBranchRowId, setDeletingBranchRowId] = useState<string | null>(null);

  // Task 11（代码附件）：当前打开的代码查看/编辑浮层——与 cellModal/
  // optimizeRowId 平级的另一个顶层 modal 状态，堆叠在行详情抽屉之上。
  const [codeModal, setCodeModal] = useState<{ rowId: string; columnId: string } | null>(null);
  // 当前展开行的「按列索引代码状态」map——抽屉打开时一次性拉取（见
  // knowhow-model.ts 的 fetchKnowhowRowCodeByColumn 头注释：GET 一次行详情
  // 换全行代码状态，不随网格/抽屉渲染逐格 GET）。rowCodeLoaded 只在成功加载
  // 后才置 true；加载失败时保持 false 且不合成任何 map——宁可"暂时不显示
  // chip"也不能在数据不确定时显示可能误导的"添加代码"（明明有代码却因为这
  // 次请求失败而提示"添加"）。
  const [rowCodeByColumn, setRowCodeByColumn] = useState<Record<string, KnowhowCellCode>>({});
  const [rowCodeLoaded, setRowCodeLoaded] = useState(false);
  const [rowCodeError, setRowCodeError] = useState<string | null>(null);

  const loadTables = useCallback(() => {
    setTablesLoading(true);
    setTablesError(null);
    fetchKnowhowTables(notebookId)
      .then((list) => setTables(list))
      .catch(() => setTablesError("加载 knowhow 表失败，请重试"))
      .finally(() => setTablesLoading(false));
  }, [notebookId]);

  useEffect(() => {
    loadTables();
  }, [loadTables]);

  // stale-response guard（收尾修复）：loadDetail / loadRowCode 都可能被快速
  // 连续触发（连点两张表卡片、连开两行抽屉、操作后的手动刷新），旧请求的
  // 响应后到会覆盖新目标的状态。修法=单调递增请求号：发起时领号，落地前
  // 校验「自己仍是最新一次请求」，否则整批丢弃；目标变化/组件卸载时（effect
  // cleanup）再 bump 一次，把已无归属的 in-flight 响应一并作废。guard 放在
  // load 回调内部而非仅 effect 内，让 effect 触发与操作后的手动刷新（addRow/
  // retryReproject/handleManageChanged/handleCellSave 等）共用同一套防倒灌。
  const detailRequestRef = useRef(0);
  const loadDetail = useCallback(
    async (tableId: string): Promise<KnowhowTableDetail | null> => {
      const requestId = ++detailRequestRef.current;
      setDetailLoading(true);
      setDetailError(null);
      try {
        const data = await fetchKnowhowTable(notebookId, tableId);
        if (detailRequestRef.current !== requestId) return null;
        setDetail(data);
        return data;
      } catch {
        if (detailRequestRef.current === requestId) setDetailError("加载表格详情失败，请重试");
        return null;
      } finally {
        if (detailRequestRef.current === requestId) setDetailLoading(false);
      }
    },
    [notebookId],
  );

  useEffect(() => {
    if (selectedTableId) loadDetail(selectedTableId);
    return () => {
      detailRequestRef.current += 1; // 切表/回列表/卸载：作废 in-flight 的旧表详情
    };
  }, [selectedTableId, loadDetail]);

  // F（review，单格 F2 + 批量 F3 共用）：规整发现「服务器原文已被他人改动」后，请求刷新
  // 当前表 detail——复用带 stale-guard 的 loadDetail（见 detailRequestRef 注释），把陈旧的
  // detail 快照（单格 savedContent / 批量 originalMd 基线之源）换成最新，避免关掉重开撞
  // 同一陈旧态、永远循环。detail 是喂给编辑器/批量弹窗的**单一真源**，且期间一切编辑都
  // 经 handleCellSave -> setDetail 落库合并（没有未落盘的本地态会被覆盖），故「重取整表
  // 后整体 set」是安全的。useCallback 稳定 identity：既省无谓重渲染，也让编辑器那侧即便
  // 直接依赖它也不会误触发（编辑器另用 ref 兜底，双保险）。
  const reloadTableDetail = useCallback(async (): Promise<KnowhowTableDetail | null> => {
    if (!selectedTableId) return null;
    return loadDetail(selectedTableId);
  }, [selectedTableId, loadDetail]);

  // Task 11：行详情抽屉展开的这一行的代码状态——独立于 loadDetail（整表结构/
  // 内容），只在抽屉真正打开时才发起，避免网格视图本身多背一次网络请求。
  const rowCodeRequestRef = useRef(0);
  const loadRowCode = useCallback((rowId: string) => {
    const requestId = ++rowCodeRequestRef.current;
    setRowCodeError(null);
    fetchKnowhowRowCodeByColumn(rowId)
      .then((map) => {
        if (rowCodeRequestRef.current !== requestId) return; // 已切行/关抽屉：丢弃旧行响应
        setRowCodeByColumn(map);
        setRowCodeLoaded(true);
      })
      .catch(() => {
        if (rowCodeRequestRef.current === requestId) setRowCodeError(CODE_STATUS_LOAD_ERROR);
      });
  }, []);

  useEffect(() => {
    if (!openRowId) {
      setRowCodeByColumn({});
      setRowCodeLoaded(false);
      setRowCodeError(null);
      return;
    }
    setRowCodeLoaded(false);
    loadRowCode(openRowId);
    return () => {
      rowCodeRequestRef.current += 1; // 切行/关抽屉：作废 in-flight 的旧行代码状态
    };
  }, [openRowId, loadRowCode]);

  // Task 11（引用命中→跳概念矩阵抽屉+高亮，规格 §4.5）：记住「当前这个跳转
  // 目标（initialRowId）是否已经落地过一次路由」——detail 是整表状态，后面
  // 几乎任何写操作（编辑格子/加行/改名/重新投影……）都会替换它的对象引用；
  // 下面依赖 detail 的引用跳转 effect 若不加这层记忆，用户关掉跳转打开的
  // 抽屉后随手做一次这类操作，detail 一变就会把同一个 initialRowId 重新
  // 路由一遍，把刚关掉的抽屉又弹回来。只在 initialTableId/initialRowId 真正
  // 变化（真的换了一次跳转目标）时清空，允许新目标再落地一次；notebook 切换
  // 这个兜底重置也顺带清空（下方 effect），避免残留上一个 notebook 里一个
  // 几乎不可能撞上、但理论上可能存在的陈旧 rowId。
  const jumpRoutedRef = useRef<string | null>(null);

  // notebook 切换时(理论上面板会被 page.tsx 一并卸载，这里仅作兜底)整体复位，
  // 避免残留上一个 notebook 的表选中态。
  useEffect(() => {
    setSelectedTableId(null);
    setDetail(null);
    setDetailError(null);
    setActionError(null);
    setQuery("");
    setOpenRowId(null);
    setCellModal(null);
    setOpenConceptValue(null); // Task 8：与 openTable/backToList 同理一并清空
    setHighlightRowId(null); // Task 11：与 openConceptValue 并列一并清空
    setConfirmDelete(false);
    setManageOpen(false);
    setTransferOpen(false); // C3：与 manageOpen 同级的顶层 modal 状态，一并清空
    setCreateOpen(false);
    setAppendOpen(false);
    setOptimizeRowId(null);
    setCompleteRowId(null);
    setReformatRowId(null);
    setReformatTableOpen(false);
    setCodeModal(null);
    setHistoryOpen(false); // Task 15：与 manageOpen/transferOpen 同级的顶层 modal 状态，一并清空
    jumpRoutedRef.current = null; // Task 11：兜底一并重置，理由见上方声明处注释
  }, [notebookId]);

  // Task 12（引用跳转）：挂载时（或 initialTableId/initialRowId 变化时——面板
  // 已开着又点了另一条引用）选中目标表。openTable 会把 openRowId/
  // openConceptValue/highlightRowId 等全部顶层 modal 状态清零；具体展开成
  // 「行抽屉」还是「概念矩阵抽屉 + 高亮命中分支」要等 detail 加载完、拿到
  // anchorColumnId 才能判定（Task 11，规格 §4.5），交给下面依赖 detail 的
  // 另一个 effect——这里不再像改动前那样直接 setOpenRowId（那一行已经搬过
  // 去，且要等 detail 到位才能决定该开哪个抽屉）。
  useEffect(() => {
    if (!initialTableId) return;
    setJumpNotice(null);
    openTable(initialTableId);
    jumpRoutedRef.current = null; // 新的跳转目标：允许下面的 effect 再次落地一次
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialTableId, initialRowId]);

  // Task 11（引用命中→跳概念矩阵抽屉 + 高亮，规格 §4.5）：detail 加载后按
  // anchorColumnId 分流——有 anchor 的表定位 initialRowId 所在的概念组，打开
  // 矩阵抽屉并高亮该分支列；记录型表（无 anchor）保持原有的行抽屉。找不到组
  // （行已被删除、或恰好加载到的这一批 detail 还不包含它），或找到的组恰好是
  // 「空 anchor」组（group.anchorValue === ""）时，回退到行抽屉——空 anchor
  // 行是 groupRowsByAnchor 里"每个空行各自成组"的产物，同一批空行会有多个
  // anchorValue 恒为 "" 的组，靠 anchorValue 反查（openConceptValue ===
  // g.anchorValue）必然命中第一个而非这次命中的那行，会开错抽屉，故不能像
  // 非空概念那样走概念矩阵分支（final-review fix, Important 2）。「行已被
  // 删除」的情形下 setOpenRowId 指向一个 detail.rows 里已不存在的 id 同样安
  // 全——openRow 反查 `?? null` 后仅 `{openRow && ...}` 才渲染，等效于什么
  // 也不做；下面「目标表/行是否陈旧」的 effect 仍按 detail.rows 是否含
  // initialRowId 单独出内联提示，两者不冲突。
  // jumpRoutedRef 短路见上方声明处注释——没有它，用户关掉跳转打开的抽屉后
  // 随手编辑一格（几乎任何写操作都会替换 detail 的对象引用），这个 effect
  // 会对同一个 initialRowId 重新路由一遍，把刚关掉的抽屉又弹回来。
  useEffect(() => {
    if (!initialRowId || !detail) return;
    if (jumpRoutedRef.current === initialRowId) return;
    jumpRoutedRef.current = initialRowId;
    if (detail.anchorColumnId) {
      const group = groupRowsByAnchor(detail.rows, detail.anchorColumnId)
        .find((g) => g.rows.some((r) => r.id === initialRowId));
      if (group && group.anchorValue) {
        setOpenConceptValue(group.anchorValue);
        setHighlightRowId(initialRowId);
      } else {
        setOpenRowId(initialRowId); // 空 anchor 行(或没找到组)：回退行抽屉，同记录型
      }
    } else {
      setOpenRowId(initialRowId); // 记录型表：原行抽屉
    }
  }, [initialRowId, detail]);

  // Task 12：目标表/行是否陈旧——只在「当前选中的表就是这次跳转的目标表」时
  // 判定，避免用户跳转后又手动切到别的表时被这两个陈旧的 prop 值误伤。
  useEffect(() => {
    if (!initialTableId || selectedTableId !== initialTableId) return;
    if (detailError) {
      // 表本身加载失败，大概率已被删除——回退列表而不是留在一个「重试」也
      // 无法恢复的死胡同页面。
      setJumpNotice("未能定位到引用指向的表格，它可能已被删除。");
      backToList();
      return;
    }
    if (detail && initialRowId && !detail.rows.some((row) => row.id === initialRowId)) {
      setJumpNotice("引用指向的行未找到，可能已被删除或调整，已为你展开整表内容。");
    }
  }, [detail, detailError, initialRowId, initialTableId, selectedTableId]);

  function openTable(tableId: string) {
    setSelectedTableId(tableId);
    setDetail(null);
    setDetailError(null);
    setActionError(null);
    setTransferNotice(null);
    setQuery("");
    setOpenRowId(null);
    setCellModal(null);
    // Task 8：openConceptValue 是纯按字符串匹配 detail 里的 anchor 分组
    // （见 openConceptGroup 的 useMemo），不像 cellModal/codeModal 那样带
    // rowId 归属校验——不清掉的话，切到另一张表后一旦该表也凑巧有同名概念，
    // 矩阵抽屉会在用户没点任何东西的情况下自己弹出来（openConceptGroup 只是
    // 普通 useMemo，detail 一变就重算，不经用户交互）。与其余顶层 modal 状态
    // 一起在切表/返回列表时清空。
    setOpenConceptValue(null);
    setHighlightRowId(null); // Task 11：与 openConceptValue 并列一并清空
    setConfirmDelete(false);
    setManageOpen(false);
    setTransferOpen(false); // C3：与 manageOpen 同级的顶层 modal 状态，一并清空
    setAppendOpen(false);
    setOptimizeRowId(null);
    setCompleteRowId(null);
    setCodeModal(null);
    setHistoryOpen(false); // Task 15：与 manageOpen/transferOpen 同级的顶层 modal 状态，一并清空
  }

  function backToList() {
    setSelectedTableId(null);
    setDetail(null);
    setDetailError(null);
    setActionError(null);
    setTransferNotice(null);
    setQuery("");
    setOpenRowId(null);
    setCellModal(null);
    setOpenConceptValue(null);
    setHighlightRowId(null); // Task 11：与 openConceptValue 并列一并清空
    setConfirmDelete(false);
    setManageOpen(false);
    setTransferOpen(false); // C3：与 manageOpen 同级的顶层 modal 状态，一并清空
    setAppendOpen(false);
    setOptimizeRowId(null);
    setCodeModal(null);
    setHistoryOpen(false); // Task 15：与 manageOpen/transferOpen 同级的顶层 modal 状态，一并清空
  }

  async function confirmDeleteTable() {
    if (!selectedTableId) return;
    setDeleting(true);
    setActionError(null);
    try {
      await deleteKnowhowTable(notebookId, selectedTableId);
      backToList();
      loadTables();
    } catch {
      setActionError("删除失败，请重试");
      setConfirmDelete(false);
      setDeleting(false);
    }
  }

  async function retryReproject() {
    if (!selectedTableId) return;
    setRetryingReproject(true);
    setActionError(null);
    try {
      await reprojectKnowhowTable(notebookId, selectedTableId);
      loadDetail(selectedTableId);
    } catch {
      setActionError("重新同步失败，请重试");
    } finally {
      setRetryingReproject(false);
    }
  }

  // 「添加行」：建空行 + 自动打开首格（网格列顺序的第一列）编辑态——规格②
  // 路A「新增行 = 建空行 + 自动打开首格浮窗，一路「保存并下一格」即天然填写
  // 向导」。用 addKnowhowRow 直接返回的新行 id 打开编辑器，不必等 loadDetail
  // 的整表重拉——列表不受影响，用旧的 detail.columns 算首列 id 即可。
  async function addRow() {
    if (!selectedTableId || addingRow) return;
    setAddingRow(true);
    setActionError(null);
    try {
      const newRow = await addKnowhowRow(notebookId, selectedTableId, { cells: {} });
      // 乐观地把新行拼进本地 detail.rows 再打开编辑器（T7 复审 Important
      // 修复）：openCellEdit 之后 KnowhowCellEditor 靠
      // `table.rows.find(r => r.id === rowId)!` 定位这一行——若只等下面的
      // loadDetail 那次后台整表重拉来补上它，重拉一旦比这次渲染慢（正常网络
      // 延迟）或失败，编辑器就永远不会出现：行已经在服务端建好，用户却看不
      // 到任何编辑器，也没有错误提示（addKnowhowRow 本身成功了）。本地拼接
      // 让这一步纯同步、不依赖网络往返；loadDetail 仍然保留，作为核对服务端
      // 真实状态的后台校准，不再是编辑器出现的前提条件。
      setDetail((prev) => (prev ? { ...prev, rows: appendRowOptimistically(prev.rows, newRow) } : prev));
      loadDetail(selectedTableId);
      loadTables();
      const firstColumnId = detail ? orderColumnsForGrid(detail.columns)[0]?.id : undefined;
      if (firstColumnId) openCellEdit(newRow.id, firstColumnId);
    } catch (err) {
      setActionError(extractErrorMessage(err, "添加行失败，请重试"));
    } finally {
      setAddingRow(false);
    }
  }

  // Task 10（加分支）：矩阵抽屉「+ 分支」——在当前打开的概念组下新建一个物理
  // 行，anchor 列预填该概念值（其余列留空待填，用户在矩阵里逐格补）。与
  // addRow 共用同一个 addingRow guard/loading 态——加分支/加概念/添加行三者
  // 本质都是"新增一行"，同一时刻只会有一路在途，不需要三份独立状态。新行
  // 落地后不主动打开任何格子编辑器（加一个分支通常是先看这多出的一列在矩阵
  // 里对不对，而不是立刻扎进某一格编辑），与"加概念"那种"加了就要马上填
  // 名字"的引导语义不同。
  async function addBranch(anchorValue: string) {
    if (!selectedTableId || !detail?.anchorColumnId || addingRow) return;
    setAddingRow(true);
    setActionError(null);
    setConceptDrawerError(null);
    try {
      const newRow = await addKnowhowRow(notebookId, selectedTableId, {
        cells: { [detail.anchorColumnId]: anchorValue },
      });
      setDetail((prev) => (prev ? { ...prev, rows: appendRowOptimistically(prev.rows, newRow) } : prev));
      loadDetail(selectedTableId);
      loadTables();
    } catch (err) {
      // 复审修复：同一条错误文案双写——actionError 保留既有渠道（关闭抽屉后
      // 主网格横幅可见），conceptDrawerError 供抽屉在打开期间就地显示（见
      // KnowhowMatrixDrawer 的 error prop）。
      const message = extractErrorMessage(err, "添加分支失败，请重试");
      setActionError(message);
      setConceptDrawerError(message);
    } finally {
      setAddingRow(false);
    }
  }

  // Task 10（加概念）：主网格底部「+ 概念」——addRow 的变体，唯一区别是把
  // 自动打开的编辑目标列从 orderColumnsForGrid(...)[0] 换成明确的
  // detail.anchorColumnId（有 anchor 列的表里两者今天恰好是同一列——
  // orderColumnsForGrid 会把 anchor 列排到首位——但这里显式表达"这是在新增
  // 一个概念"的意图，不依赖网格列序这一巧合；后者哪天调整排序规则也不会
  // 悄悄改变这个按钮的行为）。
  async function addConcept() {
    if (!selectedTableId || !detail?.anchorColumnId || addingRow) return;
    setAddingRow(true);
    setActionError(null);
    try {
      const newRow = await addKnowhowRow(notebookId, selectedTableId, { cells: {} });
      setDetail((prev) => (prev ? { ...prev, rows: appendRowOptimistically(prev.rows, newRow) } : prev));
      loadDetail(selectedTableId);
      loadTables();
      openCellEdit(newRow.id, detail.anchorColumnId);
    } catch (err) {
      setActionError(extractErrorMessage(err, "添加概念失败，请重试"));
    } finally {
      setAddingRow(false);
    }
  }

  // Task 10（删概念）：矩阵抽屉「删除整个概念」——概念组内的物理行全部归属
  // 这一个概念，删概念即删组内每一行，Promise.all 并发触发（前端并发触发、
  // 非后端单事务的取舍——不同于 handleCellSave 的合并格批量写：那一路
  // followup A 已经换成后端单事务的 batchPatchKnowhowCells，这里的批量删除
  // 目前仍是各行独立 DELETE，不在本次改动范围内）：任一路失败就不当整批
  // 成功处理，不关抽屉、不刷新——失败的那几行在服务端可能已经被删除，下一次
  // loadDetail 会照实反映服务端的（可能部分删除后的）真实状态，这次不用
  // 本地乐观更新去掩盖它。
  async function deleteConcept() {
    if (!selectedTableId || !openConceptGroup) return;
    setDeletingConcept(true);
    setActionError(null);
    setConceptDrawerError(null);
    try {
      await Promise.all(openConceptGroup.rows.map((row) => deleteKnowhowRow(notebookId, selectedTableId, row.id)));
      setDeletingConcept(false);
      setOpenConceptValue(null);
      setHighlightRowId(null); // Task 11：概念连同其全部分支已删除，没有可高亮的行了
      loadDetail(selectedTableId);
      loadTables();
    } catch {
      // 复审修复：抽屉删除失败时抽屉仍开着（不像成功路径会关闭），必须让
      // 错误在抽屉内可见——conceptDrawerError 与 actionError 同文案双写，
      // 理由同 addBranch 的 catch 分支注释。
      setActionError("删除失败，请重试");
      setConceptDrawerError("删除失败，请重试");
      setConfirmDeleteConcept(false);
      setDeletingConcept(false);
    }
  }

  // Follow-up B（删分支，spec §4.4）：矩阵抽屉表头「删除该分支」——删除概念组
  // 里单独一个物理行（一个分支），与上面 deleteConcept「删整个概念（组内全部
  // 行）」不同粒度。二次确认态本身在 KnowhowMatrixDrawer 组件内部持有
  // （confirmDeleteBranchRowId 本地 state，见该组件），这里只负责确认后的真正
  // 删除请求——错误处理与 deleteConcept 同一套双写渠道（actionError 供关闭
  // 抽屉后主网格横幅可见 + conceptDrawerError 供抽屉开着时就地显示）。
  //
  // 删最后一个分支的边界：openConceptGroup 是调用这一刻（删除请求发出前）捕获
  // 的旧引用，await 期间不会因为其它渲染变化——在发起删除前先判定
  // `rows.length <= 1`（= 正在删的就是这个概念组仅剩的最后一个分支），删除
  // 成功后据此关闭抽屉（setOpenConceptValue(null)），否则该组变空后
  // KnowhowMatrixDrawer 会尝试渲染一个 branchRowIds 为空的矩阵。非最后一个
  // 分支时抽屉照常开着，loadDetail 拿到少一行的新 detail 后 openConceptGroup
  // 会自动重算成少一个分支的矩阵，不需要额外处理。
  //
  // 复审 Important 修复：上面这段"isLastBranch 判定"的前提是 openConceptGroup
  // 这份旧引用在函数调用的一瞬间是权威的——但若同一个概念组里还有另一个分支
  // 在这次请求 await 落地（+ loadDetail 刷新）之前也被确认删除，第二次调用会
  // 读到同一份 stale rows.length，两次都判定 isLastBranch=false；两个分支都
  // 真删掉后概念组剩 0 行，却没有一次调用触发 setOpenConceptValue(null)——
  // openConceptValue 悬挂指向一个已不存在的概念（openConceptGroup 的 useMemo
  // 找不到匹配组会变回 null，抽屉不再渲染，不会崩，但状态本身是脏的：万一之
  // 后有同锚点文本的行被加回/导入，抽屉会诡异地自动弹出）。同一个分支被快速
  // 二次点击确认同理会打两次 DELETE，第二次因行已不存在而 400，误报"删除
  // 失败"。deletingBranchRowId（镜像 addingRow 的既有重入防护）在函数最顶部
  // 短路重入调用，保证同一时刻至多一路删除请求在途，堵住这两条窗口；配合
  // KnowhowMatrixDrawer 侧不再提前收起 confirmDeleteBranchRowId（见该组件
  // handleConfirmDeleteBranch 注释），在制造出第二次点击之前先让按钮转
  // disabled + "删除中…"。
  async function deleteBranch(rowId: string) {
    if (deletingBranchRowId) return;
    if (!selectedTableId || !openConceptGroup) return;
    const isLastBranch = openConceptGroup.rows.length <= 1;
    setDeletingBranchRowId(rowId);
    setActionError(null);
    setConceptDrawerError(null);
    try {
      await deleteKnowhowRow(notebookId, selectedTableId, rowId);
      if (isLastBranch) {
        setOpenConceptValue(null);
        setHighlightRowId(null); // 同 deleteConcept：没有可高亮的行了
      }
      loadDetail(selectedTableId);
      loadTables();
    } catch {
      setActionError("删除失败，请重试");
      setConceptDrawerError("删除失败，请重试");
    } finally {
      setDeletingBranchRowId(null);
    }
  }

  // 管理 modal 里任一写操作成功：重拉表详情（modal 拿到新 detail prop 原地
  // 刷新）+ 表列表（标题/行数在列表卡片上要跟着变）。
  function handleManageChanged() {
    if (selectedTableId) loadDetail(selectedTableId);
    loadTables();
  }

  // knowhow 表版本管理 Task 15：历史抽屉「回到这里」回退成功后的回调——重拉
  // 当前表详情（行/格内容、列结构、行标题列都可能被回退改动）+ 表清单（行数/
  // 最近活动时间同样可能变化）。镜像 handleManageChanged 的既有写法；抽屉自己
  // 只重拉历史时间线，不知道也不需要知道 detail 的形状。
  function handleReverted() {
    if (selectedTableId) loadDetail(selectedTableId);
    loadTables();
  }

  // 网格表头列名 inline 改名：双击列名进入编辑框（见 KnowhowTableGrid），
  // 回车/失焦时把新名字丢给这里。走既有 patchKnowhowColumn 端点——同一个
  // 管理 modal 里那个改名走的也是它，语义完全等价，只是入口 UX 更近。改名
  // 成功后本地就地合并到 detail（避免闪一下的重拉），后台再拉一次 detail
  // 校准服务端权威（后端可能对同名/空名做拒绝，若失败会经 actionError 报
  // 出来，本地 optimistic 会被后台重拉推回真实值）。
  async function handleRenameColumn(columnId: string, name: string) {
    if (!selectedTableId) return;
    setActionError(null);
    try {
      const updated = await patchKnowhowColumn(notebookId, selectedTableId, columnId, { name });
      setDetail((prev) =>
        prev
          ? { ...prev, columns: prev.columns.map((c) => (c.id === columnId ? { ...c, name: updated.name } : c)) }
          : prev,
      );
      loadTables();
    } catch (err) {
      setActionError(extractErrorMessage(err, "改名失败，请重试"));
    }
  }

  // 「下载模板」（Task 9，规格②路B）：模板端点走既有 session 鉴权（Bearer
  // header），不能像普通静态资源那样直接 `<a href>` 导航——浏览器原生导航
  // 不会带上 localStorage 里的 token。镜像 KnowhowImage（knowhow-cell-
  // editor.tsx）的认证 fetch 习语：带鉴权头 fetch → blob → createObjectURL →
  // 点一个隐藏的 <a download> → 用完撤销 URL。blob: URL 不携带原始响应的
  // Content-Disposition 头，下载文件名必须显式给 download 属性
  // （templateDownloadFilename，与后端 f"{table['title']}-template.xlsx"
  // 同规则），否则浏览器会给出形如 "template" 的裸文件名。
  async function downloadTemplate() {
    if (!selectedTableId || !detail || templateDownloading) return;
    setTemplateDownloading(true);
    setActionError(null);
    try {
      const blob = await requestBlob(knowhowTemplateUrl(notebookId, selectedTableId), { tag: "knowhow" });
      const objectUrl = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = objectUrl;
      link.download = templateDownloadFilename(detail.title);
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(objectUrl);
    } catch (err) {
      setActionError(extractErrorMessage(err, "下载模板失败，请重试"));
    } finally {
      setTemplateDownloading(false);
    }
  }

  // 直接以编辑态打开某一格——供「添加行」引导、行详情抽屉「编辑」按钮、
  // 「保存并下一格」的下一格跳转共用（这三处都已经知道自己要的是编辑态，
  // 不需要走 openCellAuto 的「按是否已填内容」判定）。
  function openCellEdit(rowId: string, columnId: string) {
    setCellModal({ rowId, columnId, mode: "edit" });
  }

  // 网格里点某一格（非行标题列）：按该格是否已有内容自动决定预览还是编辑态
  // （规格②路A「空格子直接进编辑态；已填格子先进渲染预览态」）。canEdit=false
  // 且格子为空时什么也不做——只读成员看不到可点的空格（见 KnowhowTableGrid
  // 的 clickable 判定），这里的 !canEdit 分支只是防御性兜底。
  function openCellAuto(rowId: string, columnId: string) {
    const row = detail?.rows.find((item) => item.id === rowId);
    const content = row?.cells[columnId] ?? "";
    if (!content.trim() && !canEdit) return;
    setCellModal({ rowId, columnId, mode: content.trim() ? "preview" : "edit" });
  }

  function openReformatSavedCell(
    scope: ReformatBatchScope,
    rowId: string,
    columnId: string,
    needsReload: boolean,
  ) {
    const currentDetail = detail;
    if (!currentDetail) {
      setActionError(REFORMAT_OPEN_CELL_ERROR);
      return;
    }
    setActionError(null);
    void openSavedReformatCellAfterReload({
      rowId,
      columnId,
      currentRows: currentDetail.rows,
      currentColumns: currentDetail.columns,
      currentAnchorColumnId: currentDetail.anchorColumnId,
      closeBatch: () => {
        if (scope === "row") setReformatRowId(null);
        else setReformatTableOpen(false);
      },
      // loadDetail 只在 requestId 仍为最新且面板/表目标未变化时返回 detail；
      // 失败、切表或卸载均返回 null，helper 因此不会迟到打开旧物理格。
      reload: needsReload ? reloadTableDetail : undefined,
      openCell: (targetRowId, targetColumnId, stableRows) => {
        const content = stableRows.find((row) => row.id === targetRowId)?.cells[targetColumnId] ?? "";
        if (!content.trim() && !canEdit) return;
        setCellModal({
          rowId: targetRowId,
          columnId: targetColumnId,
          mode: content.trim() ? "preview" : "edit",
        });
      },
    }).then((result) => {
      // reload 的请求 epoch 已失效、请求失败，或刷新后行/列已被删除时，批次已经
      // 安全关闭；沿表格现有 actionError 横幅给出可恢复提示，绝不回开旧目标。
      if (panelMountedRef.current && !result.ok) setActionError(result.errorText);
    });
  }

  // 「本行其他格子」点击切换（KnowhowCellEditor/KnowhowCellPreview 的
  // onSwitchCell 共用这一个 handler）：兄弟格永远是同一行的不同列，切换只需
  // 换 columnId，rowId/mode 原样保留——查看态点兄弟格切完还是查看那一格，
  // 编辑态点切完还是编辑那一格。不再经 openCellEdit：那个函数会把 mode 硬
  // 切成 "edit"，用在这里会把预览态的只读用户也拽进编辑 UI，是越权口子
  // （也正是本次要修的复审 finding）。
  //
  // 两处调用点都不需要另判 canEdit：本函数从不把 mode 改成 "edit"、只保留
  // 原样，而所有能把 mode 置为 "edit" 的入口（openCellEdit 的三处调用——
  // 添加行/添加概念/行详情抽屉「编辑」按钮，以及 openCellAuto 的空格子
  // 分支）本身都受 canEdit 门控；换句话说 KnowhowCellEditor 能挂载到画面
  // 上就已经证明当时 canEdit 为真，本函数不会破坏这条链路。KnowhowCellPreview
  // 这边同理成立：mode 保持 "preview" 不变，只读用户点兄弟格永远是
  // preview→preview，绕不进 KnowhowCellEditor——与本文件头部 :24-25 记录的
  // 「只读成员看不到任何写入口」不变量一致。
  function switchCell(columnId: string) {
    setCellModal((current) => (current ? { ...current, columnId } : current));
  }

  // 格子浮窗「保存」：真正调用 patchKnowhowCell + 把结果合并回 detail 状态
  // （只更新命中的那一格/那一组与其行的 projectionStatus，不必整表重拉——
  // patch 端点本身就返回了更新后的值，见 knowhow-model.ts patchKnowhowCell
  // 的注释）。失败时把异常原样往上抛，编辑器组件在原地展示错误、不关闭浮窗。
  //
  // 合并格批量写（anchor 分组 spec §4.4/§6）：这一格若落在一个「合并共享格」
  // （anchor 分组内该列全分支同值、组内多于一行，见 isSharedColumn）——浮窗
  // 提示用户「改动将同步到全部 N 个分支」（cellModalAffectedBranchCount），
  // 保存就必须真的写回组内每一行的这一列，否则提示和实际落库范围对不上：
  // 其余分支的值仍是旧值，直到之后又有人凑巧编辑到那一行才被悄悄覆盖成这次
  // 的新值，造成一段时间内隐蔽的数据不一致。记录型表没有 anchorColumnId，
  // group 恒为 null，天然落到单格写（行为与改动前一致）。本函数不依赖
  // cellModal 状态自己独立判定分组——「优化整行」浮层的 onAcceptCell 也复用
  // 这同一个函数（见下方 KnowhowRowOptimizeModalProps 注释），必须对任意
  // (rowId, columnId) 的调用都成立，不能假设正巧是当前 cellModal 打开的
  // 那一格。
  //
  // followup A（后端单事务批量写）：合并格分支不再是 N 个独立
  // patchKnowhowCell 请求并发（Promise.all）+ 前端"任一失败则不合并本地
  // detail"的权宜取舍——一次 batchPatchKnowhowCells 调用，后端在 ONE DB
  // 事务里把整组 rowIds 全写或全不写（见 knowhow-model.ts 该函数注释 +
  // 后端 KnowhowStore.update_knowhow_cells）。批内任一 rowId/columnId 不合法
  // 时请求整体 400，服务端连这一组里合法的行都不会写，前端也就不会误把半改
  // 状态合并进 detail。非共享格（单 rowId）仍走 patchKnowhowCell 单格
  // 端点，没必要为一格套批量端点的开销。targets.length > 1 当且仅当上面
  // 命中了合并共享格分支——isSharedColumn 自己保证组内 <=1 行恒 false（见其
  // 注释），用这个长度判断选路径，避免再把 group 塞进第二个条件表达式（TS
  // 无法把 isBatch 变量的窄化跨表达式传回 group 本身）。
  //
  // 并发防护（P1-b）：`expectedByRowId` 仅由**批量规整保存**传入（rowId ->
  // 建批次那一刻该格的 originalMd 快照），触发**服务端事务内**比对——当前落库内容
  // 与基线不一致就 409 拒写（nothing written），fan-out 整组 all-or-nothing。手动
  // 格子编辑器（onSave）与「优化整行」接受（onAcceptCell）**不传**（undefined），
  // 保持既有 last-write-wins：那是真人在主动编辑，不该被自己更早的快照挡下（本
  // 修复刻意不改这条流的语义）。基线按 targets 顺序平行下发；某写目标缺基线时退化
  // 成 ""（几乎必然判陈旧、保守跳过，不盲写）。
  //
  // 并发防护（P1 round-4）：`anchorGuard` 也仅由**批量规整保存**传入（快照 anchor 列
  // id + rowId -> 建批次那刻该行 anchor 快照值）——后端在同一事务内额外重读每行 anchor 列，
  // 任一行 anchor 自快照以来被移出组就整组 409（离组行不再被冻结扇出误写）；round-5 再加
  // 「指定未变 + 组成员集与冻结写目标精确相等」结构守卫（增员/减员亦 409）。缺值退化成 ""。
  // 并发防护（P1 round-6）：`anchorGuard` 一旦传入就**恒**走 guarded 批量端点——**含只有一个
  // 写目标的单例组**（下方 split：`useBatchEndpoint = targets.length > 1 || anchorGuard`）。此前
  // 单例组按 targets.length===1 落单格端点、绕过成员校验：建批次那刻组内仅一行，之后有兄弟行
  // join 该 anchor 组，就只写原行、悄悄把已共享的组留成不一致（round-5 为多目标关掉的同类结构
  // 漂移，单例组漏在门外）。runSave 侧 `coversAnchorGroup` 门保证 anchorGuard 只对**整组写**
  // （合并共享列扇写 / 单例组）下发，故「带 anchorGuard ⟺ 该走 guarded 端点」恒成立；多行组的
  // **非共享列**是有意子集写、不带守卫，仍走单格端点。**手动格子编辑 / 优化整行**不带 anchorGuard、
  // 单目标恒落单格端点（无组可离、last-write-wins），逐字不变。
  // origin（knowhow 表版本管理 Task 16 评审修复）：格子历史「恢复此版本」、
  // 手动编辑、批量规整保存共用这同一个函数——各流唯一的区别是变更流水上要留
  // 下什么 origin，不是"要不要走 anchor 分组批量写判定"（那套判定一视同仁）。
  // 省略时不进请求体、退回后端默认 "user"：手动编辑器与「优化整行」接受
  // （onAcceptCell）既有调用点字节不变（真人在编辑，last-write-wins）。显式传
  // origin 的两条：① 恢复（本文件 KnowhowCellHistory 挂载处）传 "revert"；
  // ② **批量规整**（KnowhowReformatBatchModal 的 onSaveCell）传 "llm_reformat"
  // ——它是自动保存 LLM 规整结果的循环，不是人手一格复核，必须留下真实来源，
  // 否则「格式规整」徽章对这条主流程永不出现（codex 第 6 轮 P2）。单格规整
  // 是用户复核后走编辑器手动保存，按 "user" 处理、不在此列。第 7 个位置参数，
  // 与 patchKnowhowCell/batchPatchKnowhowCells 的同名参数一致（见 knowhow-model.ts）。
  async function handleCellSave(
    rowId: string,
    columnId: string,
    contentMd: string,
    expectedByRowId?: Map<string, string>,
    targetRowIds?: string[],
    anchorGuard?: { anchorColumnId: string; expectedAnchorByRowId: Map<string, string> },
    origin?: string,
  ) {
    if (!selectedTableId || !detail) return;
    // 扇出写目标（合并共享格写整组）的两条来源，刻意分流（P1）：
    //  · 批量规整保存（runSave）显式传入 `targetRowIds` = 建批次那一刻**冻结快照**里
    //    规划好的写目标（planReformatSaves 的 unit.writeTargets 行 id）——此处**照单
    //    全写、绝不从实时 detail 重算分组**。否则：detail 在弹窗打开期间刷新、某 anchor
    //    组分裂/合并时，规划校验的是旧成员、回调却按新分组只扇写代表行，保存循环仍把
    //    旧成员整组标 saved = 漏写却报成功。规划与扇写必须同源于同一份冻结快照。
    //  · 手动格子编辑（onSave）/「优化整行」接受（onAcceptCell）**不传**——退回从
    //    **实时** detail 重算分组的既有行为：那是真人在当前表上编辑，本就该按眼前的
    //    分组扇写（与改动前逐字一致）。`group` 仅这条缺省路径会消费（批量路径照用显式
    //    写目标、不看它；仍无条件求值，成本等同改动前、无回归）。
    // 权威裁决在服务端：带守卫的批量写在**同一事务内逐行**校验成员归属 + expected_before，
    // 任一不符整体 409（nothing written）。故即便快照规划的写目标之一已被他人真正改动，
    // 也会安全落到既有的 409 -> stale-skip 路径，绝不盲写覆盖——客户端出「意图」，
    // 服务端裁「真相」。
    const group = detail.anchorColumnId
      ? groupRowsByAnchor(detail.rows, detail.anchorColumnId).find((g) => g.rows.some((r) => r.id === rowId))
      : null;
    const targets =
      targetRowIds ?? (group && isSharedColumn(group, columnId) ? groupCellWriteTargets(group, columnId) : [rowId]);
    // 端点选路（split，P1 round-6）：走 guarded 批量端点当且仅当——
    //  · targets.length > 1：合并共享格扇写（手动编辑或规整保存都可能命中），必须整组一事务写；
    //  · 或带 anchorGuard：**规整保存**对「整组写」单元下发的锚定守卫（runSave 的 coversAnchorGroup
    //    门保证只有合并共享列扇写 / **单例组** 才带它）。带上它就必须让服务端跑「指定未变 + 组成员
    //    精确相等 + expected_before」三重校验——否则**单例组**（建批次那刻仅一行、只有一个写目标）
    //    会走单格端点、无成员校验：若之后有兄弟行 join 该 anchor 组，只写原行、悄悄把已共享的组留成
    //    不一致（正是 round-5 为多目标单元关掉的同一类结构漂移，单例组此前漏在门外）。成员漂移即整批
    //    409 → 落既有 stale-skip。**手动格子编辑 / 优化整行**不带 anchorGuard（也不带 targetRowIds），
    //    单目标恒落下面的单格端点，逐字不变（last-write-wins，无组可离）。
    const useBatchEndpoint = targets.length > 1 || Boolean(anchorGuard);
    const results = useBatchEndpoint
      ? await batchPatchKnowhowCells(notebookId, selectedTableId, {
          columnId,
          rowIds: targets,
          contentMd,
          ...(expectedByRowId
            ? { expectedBefore: targets.map((rid) => expectedByRowId.get(rid) ?? "") }
            : {}),
          // 整组写才带 anchor 守卫（与 expectedBefore 一样按 targets 平行下发；单例组 targets 长度 1
          // 也照常带——后端 length-validated 平行数组接受 1 行批次，成员校验即在此单行上跑）。
          ...(anchorGuard
            ? {
                anchorColumnId: anchorGuard.anchorColumnId,
                expectedAnchor: targets.map((rid) => anchorGuard.expectedAnchorByRowId.get(rid) ?? ""),
              }
            : {}),
          // Task 16 评审修复：格子历史恢复走这条批量路径时携带 origin="revert"；
          // 手动编辑/批量规整保存不传（省略），退回后端默认 "user"/各自既有语义。
          ...(origin !== undefined ? { origin } : {}),
        })
      : [
          // 单格路径：无 anchorGuard 的手动编辑落到这里，不带 anchor 守卫（见上方 split 说明及
          // handleCellSave 头注释）。expectedBefore 仍随手动编辑的 last-write-wins 语义按需下发。
          await patchKnowhowCell(
            notebookId,
            selectedTableId,
            rowId,
            columnId,
            contentMd,
            expectedByRowId ? expectedByRowId.get(rowId) ?? "" : undefined,
            origin,
          ),
        ];
    setDetail((prev) => {
      if (!prev) return prev;
      const resultByRowId = new Map(results.map((result) => [result.rowId, result]));
      return {
        ...prev,
        rows: prev.rows.map((row) => {
          const result = resultByRowId.get(row.id);
          if (!result) return row;
          return {
            ...row,
            cells: { ...row.cells, [result.columnId]: result.contentMd },
            projectionStatus: result.projectionStatus,
          };
        }),
      };
    });
    // 收尾修复（浏览器 QA 实测）：格子内容一变，该格挂着的代码派生在后端立即
    // 转 stale——而保存成功路径此前只合并了格子内容，抽屉 chip / 代码浮层会
    // 一直停留在「已实现」直到重开抽屉。保存命中的行里若包含当前展开行
    // （单格写时就是它自己；批量写整组时组内任一分支都可能是它），立即重取
    // 行级代码状态，让 stale chip 无需重开抽屉即浮现（优化整行的
    // onAcceptCell 也走本函数，同样受益）。loadRowCode 自带请求号 guard，与
    // 快速切行/连续保存并存时旧响应不会倒灌。
    if (openRowId && targets.includes(openRowId)) loadRowCode(openRowId);
  }

  // Task 11：打开代码浮层（抽屉 chip 的 onOpen，及"添加代码"安静入口共用同
  // 一个打开函数——两者的区别只在于 KnowhowCodeModal 内部按 code.status 是
  // 否为 none 决定初始 mode 是 view 还是 edit，本函数不需要关心这一点）。
  function openCodeModal(rowId: string, columnId: string) {
    setCodeModal({ rowId, columnId });
  }

  // 代码保存成功：把最新代码视图合并进行级 map——不必整行重拉（putCellCode
  // 本身已返回更新后的值，见 knowhow-model.ts）。
  function handleCodeSaved(columnId: string, entry: KnowhowCellCode) {
    setRowCodeByColumn((prev) => ({ ...prev, [columnId]: entry }));
  }

  // 代码删除成功：从行级 map 里摘除这一列——摘除后 resolveCellCodeView 会
  // 合成 none 占位，chip 自然回落到「添加代码」（或 canEdit=false 时不显示）。
  function handleCodeDeleted(columnId: string) {
    setRowCodeByColumn((prev) => {
      const next = { ...prev };
      delete next[columnId];
      return next;
    });
  }

  const openRow = detail?.rows.find((row) => row.id === openRowId) ?? null;
  const cellModalRow = cellModal ? detail?.rows.find((row) => row.id === cellModal.rowId) ?? null : null;
  const cellModalColumn = cellModal ? detail?.columns.find((column) => column.id === cellModal.columnId) ?? null : null;
  // 行标题面包屑文本：与行详情抽屉标题同规则(resolveRowTitleText + 截断)，
  // 保证同一行在抽屉/格子浮窗两处显示完全一致的行标签；空/无行标题列时退回
  // 「行 N」（规格①「全空则「行 N」」）。
  const cellModalRowTitle = cellModalRow && detail
    ? cellSummary(resolveRowTitleText(cellModalRow, detail.columns), 60) || rowFallbackTitle(cellModalRow.position)
    : "";

  // 合并格编辑影响范围（anchor 分组 spec §4.4）：cellModal 当前格若落在一个
  // 合并共享格（概念组内该列全分支同值、组内多于一行），算出组的分支数传给
  // KnowhowCellEditor 触发 header 提示；与 handleCellSave 的批量写判定同一套
  // groupRowsByAnchor + isSharedColumn 标准，保证「提示的范围」与「保存时
  // 实际写入的范围」永远一致，不会出现提示说影响 N 个分支、保存却只改了 1
  // 个的错位。记录型表没有 anchorColumnId，恒为 undefined（不显示提示）。
  // 计算只依赖 cellModal.rowId/columnId，与 mode 无关——Task 16 评审修复起，
  // KnowhowCellHistory（mode==="history"）复用同一份值驱动确认框里的「恢复
  // 将同步到全部 N 个分支」提示，不需要另算一遍（见其挂载处）。
  const cellModalGroup = cellModal && detail && detail.anchorColumnId
    ? groupRowsByAnchor(detail.rows, detail.anchorColumnId).find((g) => g.rows.some((r) => r.id === cellModal.rowId))
    : null;
  const cellModalAffectedBranchCount =
    cellModalGroup && cellModal && isSharedColumn(cellModalGroup, cellModal.columnId)
      ? cellModalGroup.rows.length
      : undefined;

  // Task 9「优化整行」批量弹窗当前作用的行——与 cellModalRow 同一套查找方式。
  const optimizeRow = detail?.rows.find((row) => row.id === optimizeRowId) ?? null;
  const optimizeRowTitle = optimizeRow && detail
    ? cellSummary(resolveRowTitleText(optimizeRow, detail.columns), 60) || rowFallbackTitle(optimizeRow.position)
    : "";

  const completeRow = detail?.rows.find((row) => row.id === completeRowId) ?? null;
  const completeRowTitle = completeRow && detail
    ? cellSummary(resolveRowTitleText(completeRow, detail.columns), 60) || rowFallbackTitle(completeRow.position)
    : "";

  function openRowCompletion(rowId: string, trigger: HTMLButtonElement) {
    completionTriggerRef.current = trigger;
    // 接受最后一个空列后，入口会按门控从底层抽屉卸载。打开时同时冻结所在
    // 行详情/概念矩阵里稳定存在的关闭按钮，作为焦点恢复的第二落点。
    const ownerDialog = trigger.closest<HTMLElement>('[role="dialog"]');
    completionFallbackFocusRef.current = ownerDialog?.querySelector<HTMLElement>('button[title="关闭"]') ?? ownerDialog;
    setCompleteRowId(rowId);
  }

  // knowhow-md-normalize Task 9「一键规整整行」批量弹窗当前作用的行——同一套
  // 查找/标题合成方式。
  const reformatRow = detail?.rows.find((row) => row.id === reformatRowId) ?? null;
  const reformatRowTitle = reformatRow && detail
    ? cellSummary(resolveRowTitleText(reformatRow, detail.columns), 60) || rowFallbackTitle(reformatRow.position)
    : "";

  // Task 11「代码附件」浮层当前作用的行/列——与 cellModalRow/cellModalColumn
  // 同一套查找方式。
  const codeModalRow = codeModal ? detail?.rows.find((row) => row.id === codeModal.rowId) ?? null : null;
  const codeModalColumn = codeModal ? detail?.columns.find((column) => column.id === codeModal.columnId) ?? null : null;
  const codeModalRowTitle = codeModalRow && detail
    ? cellSummary(resolveRowTitleText(codeModalRow, detail.columns), 60) || rowFallbackTitle(codeModalRow.position)
    : "";

  // Task 8：openConceptValue 对应的 anchor 分组——按 detail.rows 全量分组
  // （不受网格搜索 query 过滤影响，抽屉展示该概念完整的分支集合，不因为
  // 搜索命中了其中一支就漏显示其余分支）；表已刷新/该概念值不再存在时
  // 找不到、为 null，抽屉不渲染（见下方 KnowhowMatrixDrawer 挂载条件）。
  const openConceptGroup = useMemo(() => {
    if (!detail?.anchorColumnId || openConceptValue === null) return null;
    const groups = groupRowsByAnchor(detail.rows, detail.anchorColumnId);
    return groups.find((g) => g.anchorValue === openConceptValue) ?? null;
  }, [detail, openConceptValue]);

  // Task 10（删概念）：openConceptValue 一变（切到另一个概念、或关闭抽屉）就
  // 收起二次确认态——不然上一个概念点了「删除」没确认就关抽屉，下次（同一个
  // 或另一个）概念一开就已经是确认态，等于半自动帮用户点了一半删除流程。
  useEffect(() => {
    setConfirmDeleteConcept(false);
    setDeletingConcept(false);
    setConceptDrawerError(null); // 复审修复：同上，切概念/关抽屉不留旧错误
  }, [openConceptValue]);

  return (
    <section className="knowhow-view" role="dialog" aria-modal="true">
      <div className="knowhow-view-header">
        <div>
          <h2>Knowhow 表</h2>
          <p>
            {canEdit
              ? "结构化经验表：可导入、新建与在线维护，逐行沉淀问题排查知识。"
              : "结构化经验表：按行浏览与核对（只读共享，不可修改）。"}
          </p>
        </div>
        <button className="icon-button" onClick={onClose} title="关闭">
          <X size={20} />
        </button>
      </div>

      {jumpNotice && (
        <div className="knowhow-jump-notice">
          <span>{jumpNotice}</span>
          <button type="button" onClick={() => setJumpNotice(null)} title="关闭">
            <X size={14} />
          </button>
        </div>
      )}

      {transferNotice && (
        <div className="knowhow-jump-notice">
          <span>{transferNotice}</span>
          <button type="button" onClick={() => setTransferNotice(null)} title="关闭">
            <X size={14} />
          </button>
        </div>
      )}

      <div className="knowhow-view-body">
        {selectedTableId === null ? (
          <KnowhowTableList
            tables={visibleTables}
            hasUnfilteredTables={(tables?.length ?? 0) > 0}
            loading={tablesLoading}
            error={tablesError}
            canEdit={canEdit}
            healthFilter={healthFilter}
            onHealthFilterChange={setHealthFilter}
            onRetry={loadTables}
            onOpen={openTable}
            onImportClick={() => setImportOpen(true)}
            onCreateClick={() => setCreateOpen(true)}
          />
        ) : (
          <KnowhowTableGrid
            detail={detail}
            loading={detailLoading}
            error={detailError}
            actionError={actionError}
            canEdit={canEdit}
            onDismissActionError={() => setActionError(null)}
            onRetryLoad={() => loadDetail(selectedTableId)}
            query={query}
            onQueryChange={setQuery}
            onOpenRow={setOpenRowId}
            onOpenCell={openCellAuto}
            onOpenConcept={setOpenConceptValue}
            onBack={backToList}
            confirmDelete={confirmDelete}
            onRequestDelete={() => setConfirmDelete(true)}
            onCancelDelete={() => setConfirmDelete(false)}
            onConfirmDelete={confirmDeleteTable}
            deleting={deleting}
            onRetryReproject={retryReproject}
            retryingReproject={retryingReproject}
            onOpenManage={() => setManageOpen(true)}
            onTransfer={() => {
              // 复审 Minor：重开弹窗前清掉上一次的成功提示，避免旧提示在新一
              // 轮操作进行时继续挂在屏幕上造成误导。
              setTransferNotice(null);
              setTransferOpen(true);
            }}
            onAddRow={addRow}
            addingRow={addingRow}
            onAddConcept={addConcept}
            onDownloadTemplate={downloadTemplate}
            templateDownloading={templateDownloading}
            onAppendClick={() => setAppendOpen(true)}
            onReformatTableClick={() => setReformatTableOpen(true)}
            onRenameColumn={handleRenameColumn}
            onOpenHistory={() => setHistoryOpen(true)}
          />
        )}
      </div>

      {openRow && detail && (
        <KnowhowRowDrawer
          row={openRow}
          columns={detail.columns}
          notebookId={notebookId}
          apiBase={apiBase}
          canEdit={canEdit}
          onEditCell={openCellEdit}
          onClose={() => setOpenRowId(null)}
          cellModalOpen={
            cellModal !== null || optimizeRowId !== null || completeRowId !== null || reformatRowId !== null || codeModal !== null
          }
          onOptimizeRow={() => setOptimizeRowId(openRow.id)}
          anchorColumnId={detail.anchorColumnId}
          onCompleteRow={(trigger) => openRowCompletion(openRow.id, trigger)}
          onReformatRow={() => setReformatRowId(openRow.id)}
          codeByColumn={rowCodeByColumn}
          codeLoaded={rowCodeLoaded}
          codeError={rowCodeError}
          onRetryCode={() => loadRowCode(openRow.id)}
          onOpenCode={openCodeModal}
        />
      )}

      {/* Task 8（规格 §4.3）：点主网格某个概念（anchor 列的合并格，见
          KnowhowTableGrid 的 onOpenConcept）→ 打开该概念的「属性×分支」矩阵
          抽屉。必须排在 cellModal 渲染之前（而不是之后）：KnowhowMatrixDrawer
          外壳复用与 cellModal 完全相同的 .kh-modal-overlay/.kh-modal-card
          （z-index: 65，同一层级，不像 KnowhowRowDrawer 那样有专属更低的
          z-index 天然垫底）——抽屉内点格复用 openCellAuto 会在本抽屉之上再开
          一个 cellModal，同 z-index 下靠 DOM 顺序决出叠放，后渲染的盖在先渲染
          的上面；本块若排在 cellModal 后面，nested 的 cellModal 反而会被本
          抽屉的全屏 overlay 盖住、连点击都点不到。KnowhowMatrixDrawer 本身
          刻意不带 Esc 监听器（见该组件头注释：裸 Esc 会与嵌套的格子浮窗监听器
          双关，一次 Esc 关掉两层），故这里也不加。 */}
      {openConceptGroup && detail?.anchorColumnId && (
        <KnowhowMatrixDrawer
          group={openConceptGroup}
          columns={orderColumnsForGrid(detail.columns)}
          anchorColumnId={detail.anchorColumnId}
          notebookId={notebookId}
          apiBase={apiBase}
          canEdit={canEdit}
          highlightRowId={highlightRowId}
          onEditCell={(rowId, columnId) => openCellAuto(rowId, columnId)}
          onCompleteRow={openRowCompletion}
          onClose={() => { setOpenConceptValue(null); setHighlightRowId(null); }}
          error={conceptDrawerError}
          onAddBranch={() => addBranch(openConceptGroup.anchorValue)}
          addingBranch={addingRow}
          confirmDeleteConcept={confirmDeleteConcept}
          onRequestDeleteConcept={() => setConfirmDeleteConcept(true)}
          onCancelDeleteConcept={() => setConfirmDeleteConcept(false)}
          onConfirmDeleteConcept={deleteConcept}
          deletingConcept={deletingConcept}
          onDeleteBranch={deleteBranch}
          deletingBranchRowId={deletingBranchRowId}
        />
      )}

      {/* 必须排在矩阵抽屉之后：两者同为 z-index 65，DOM 后项才会盖在矩阵之上。 */}
      {completeRowId && completeRow && detail && selectedTableId && canEdit && (
        <KnowhowRowCompletionModal
          key={`${selectedTableId}:${completeRow.id}`}
          notebookId={notebookId}
          apiBase={apiBase}
          tableId={selectedTableId}
          row={completeRow}
          rows={detail.rows}
          columns={detail.columns}
          anchorColumnId={detail.anchorColumnId}
          rowTitle={completeRowTitle}
          returnFocusTo={completionTriggerRef.current}
          fallbackFocusTo={completionFallbackFocusRef.current}
          onAcceptCell={handleCellSave}
          onClose={() => setCompleteRowId(null)}
        />
      )}

      {/* knowhow 表版本管理 Task 15：工具栏「历史」→ 历史抽屉（时间线+diff+
          两版对比+回退+里程碑）。必须排在 cellModal 渲染之前——同上面
          KnowhowMatrixDrawer 那段注释的同一条规则：两者共用同一个
          .kh-modal-overlay（z-index 65），DOM 顺序决定层叠。本抽屉当前不提供
          "编辑格子"之类会在其上再开一层 cellModal 的入口（Task 15 范围内没有
          嵌套浮窗），但仍照抄这条顺序约定，保持与本文件其余 kh-modal-* 消费
          方一致、也给未来若真的长出这类入口留好位置。 */}
      {historyOpen && detail && selectedTableId && (
        <KnowhowHistoryDrawer
          notebookId={notebookId}
          apiBase={apiBase}
          tableId={selectedTableId}
          tableTitle={detail.title}
          columns={detail.columns}
          canEdit={canEdit}
          onClose={() => setHistoryOpen(false)}
          onReverted={handleReverted}
        />
      )}

      {cellModal && cellModalRow && cellModalColumn && detail && (
        cellModal.mode === "edit" ? (
          <KnowhowCellEditor
            key={`${cellModal.rowId}:${cellModal.columnId}`}
            notebookId={notebookId}
            apiBase={apiBase}
            table={detail}
            rowId={cellModal.rowId}
            columnId={cellModal.columnId}
            rowTitle={cellModalRowTitle}
            affectedBranchCount={cellModalAffectedBranchCount}
            onSave={handleCellSave}
            onNavigate={(rowId, columnId) => setCellModal({ rowId, columnId, mode: "edit" })}
            onClose={() => setCellModal(null)}
            // 「本行其他格子」点击切换——见 switchCell 定义处注释（保持 mode
            // 不变即天然安全，这里不需要再判 canEdit）。
            onSwitchCell={switchCell}
            // F（review）：规整判 server-stale 时刷新整表 detail，下次打开就有新鲜
            // savedContent（否则关掉重开撞同一陈旧态、永远循环）。
            onServerStale={reloadTableDetail}
            // knowhow 表版本管理 Task 16：切到历史页签——mode 变化即卸载本组件，
            // 未落盘内容由既有的卸载草稿兜底接住（见 onHistory prop 注释）。
            onHistory={() => setCellModal((current) => (current ? { ...current, mode: "history" } : current))}
          />
        ) : cellModal.mode === "history" ? (
          <KnowhowCellHistory
            rowTitle={cellModalRowTitle}
            column={cellModalColumn}
            contentMd={cellModalRow.cells[cellModal.columnId] ?? ""}
            notebookId={notebookId}
            apiBase={apiBase}
            table={detail}
            rowId={cellModal.rowId}
            canEdit={canEdit}
            // 评审修复（Important）：恢复不再在 KnowhowCellHistory 内部直调
            // patchKnowhowCell——委托给 handleCellSave 本身（与手动编辑/批量
            // 规整保存同一条 anchor 分组 + 批量写判定），只多传 origin="revert"。
            // affectedBranchCount 复用同一份 cellModalAffectedBranchCount，驱动
            // 确认框里的合并格提示（同 KnowhowCellEditor 的 header 提示同源）。
            affectedBranchCount={cellModalAffectedBranchCount}
            onRestore={(restoreRowId, restoreColumnId, restoreContentMd) =>
              handleCellSave(restoreRowId, restoreColumnId, restoreContentMd, undefined, undefined, undefined, "revert")
            }
            onBack={() => setCellModal((current) => (current ? { ...current, mode: "preview" } : current))}
            onClose={() => setCellModal(null)}
          />
        ) : (
          <KnowhowCellPreview
            rowTitle={cellModalRowTitle}
            column={cellModalColumn}
            contentMd={cellModalRow.cells[cellModal.columnId] ?? ""}
            notebookId={notebookId}
            apiBase={apiBase}
            canEdit={canEdit}
            onEdit={() => setCellModal((current) => (current ? { ...current, mode: "edit" } : current))}
            onClose={() => setCellModal(null)}
            table={detail ?? undefined}
            rowId={cellModal.rowId}
            // 「本行其他格子」点击切换——见 switchCell 定义处注释。mode 保持
            // "preview" 不变，只读用户点兄弟格还是落在预览态，绕不进
            // KnowhowCellEditor，这里也不需要再判 canEdit（无条件传，查看态
            // 下只读/可写用户行为一致，都是查看→查看）。
            onSwitchCell={switchCell}
            // knowhow 表版本管理 Task 16：不受 canEdit 门控（规格⑦），只读成员
            // 也能切到历史页签浏览。
            onHistory={() => setCellModal((current) => (current ? { ...current, mode: "history" } : current))}
          />
        )
      )}

      {importOpen && canEdit && (
        <KnowhowImportWizard
          notebookId={notebookId}
          apiBase={apiBase}
          onClose={() => setImportOpen(false)}
          onDone={() => {
            setImportOpen(false);
            loadTables();
          }}
        />
      )}

      {createOpen && canEdit && (
        <KnowhowCreateWizard
          notebookId={notebookId}
          onClose={() => setCreateOpen(false)}
          onCreated={(created) => {
            setCreateOpen(false);
            loadTables();
            // 建表向导承诺的下一步（规格②）：创建后直接打开网格，用「添加行」
            // 开始填值。
            openTable(created.id);
          }}
        />
      )}

      {manageOpen && canEdit && detail && (
        <KnowhowManageModal
          notebookId={notebookId}
          detail={detail}
          onClose={() => setManageOpen(false)}
          onChanged={handleManageChanged}
        />
      )}

      {/* C3：「复制/移动到…」——不像 manageOpen 那样再加 canEdit 门：copy 只需
          对源笔记本有读权限，只读成员也该能把表复制到自己有写权限的另一个
          笔记本（工具栏按钮同理不整体挂在 canEdit 下，见 KnowhowTableGrid）。
          真正的权限边界收在 allowMove——只有可写源才允许挑「移动」，交给
          DestinationPicker 内部据此隐藏移动单选项，与后端 source_check 的
          copy/move 分支（routes.py transfer_knowhow_table）对齐。 */}
      {transferOpen && detail && (
        <DestinationPicker
          sourceNotebookId={notebookId}
          allowMove={canEdit}
          // knowhow 表 transfer 的目标走 knowhow:write（admin）——组管理员可管理的共享库
          // 能接收(后端实测 200),故把它们列进候选(P2-T2 评审 P2-4)。
          allowManagedTargets
          title={`复制/移动表：${detail.title}`}
          onCancel={() => setTransferOpen(false)}
          onSubmit={async (targetNotebookId, mode) => {
            try {
              await transferKnowhowTable(notebookId, detail.id, targetNotebookId, mode);
            } catch (err) {
              if (err instanceof KnowhowSourceCleanupError) {
                // 复制已提交、源清理（拆投影/删源表）失败：副本已在目标落地，
                // 源表还在——绝不能让这个异常落回 DestinationPicker 自己的
                // catch（那会把「确认」按钮重新点亮，诱导用户用同一个 move
                // 流程重试，只会在目标又堆一份重复副本，见 knowhow-transfer.ts
                // 头部注释）。这里就地吞掉、正常 resolve（不 throw）——
                // DestinationPicker.submit 只在 onSubmit 抛出时才回到它自己的
                // 错误态并解开 busy 锁；resolve 意味着它保持 busy、把「何时
                // 关」交还给调用方，本分支紧接着自己 setTransferOpen(false)
                // 促成卸载（同 onSubmit 头注释的既定协议）。把这条消息改浮到
                // 表级 actionError 横幅——源表仍是当前打开的这张，用户能直接看到。
                //
                // round 10 P1-A：下一步该怎么做，不再由这里替用户下判断——
                // err.message 是后端按 SourceCleanupFailed.reason 分两种成因
                // 各自写好的人话文案（见 transfer.py 该类的文档字符串）：
                // "source_cleanup_failed" 时源确实原封未动，走既有的「删除表」
                // 入口手动清理是安全的；"source_changed_kept" 时源是被有意保留
                // 下来保护一份并发编辑的，走「删除表」会连带永久丢失它——这条
                // 消息本身已经把正确的下一步讲清楚了（核对目标里的旧副本/重新
                // 发起搬迁，别删源），这里只管原样显示，不能再补一句通用的
                // "可以安全删除源表"，那对第二种成因是错的。
                setTransferOpen(false);
                setActionError(err.message);
                loadDetail(detail.id);
                loadTables();
                return;
              }
              throw err; // 其它错误：什么都没发生，交回 picker 自己的错误态+可重试
            }
            // 复审 Carried minor C3-1：成功路径顶部清空 actionError——这个文件
            // 里其余每个动作 handler 都在起手处清空这条横幅，传输成功路径此前
            // 漏了。move 靠紧接着的 backToList() 顺带清掉，但 copy 没有等价的
            // 副作用：若表上原本挂着一条陈旧的 actionError（比如上次删除行失败
            // 留下的），一次成功的复制之后它会继续显示，用户会误以为这次操作
            // 也失败了。
            setActionError(null);
            setTransferOpen(false);
            if (mode === "move") {
              // 移动成功：源表已被后端删除，留在原地会看到一张不存在的表——
              // 回到表列表并刷新。backToList 是既有的选中态清空函数，一并清掉
              // openRowId/cellModal 等一整套顶层弹层态（不止 selectedTableId
              // 一项），避免遗留指向已删表的行抽屉/格子浮窗。
              backToList();
              loadTables();
            } else {
              // 复审 Minor：copy 成功没有任何自然可见的反馈——源表原样不动，
              // 目标笔记本又不在当前视野里，用户点了「确认」之后除了 modal
              // 关闭，看不出到底发生没发生。补一条轻量提示，复用既有的
              // jumpNotice 视觉(informational，非错误色调)。
              setTransferNotice("已复制到目标笔记本");
            }
          }}
        />
      )}

      {appendOpen && canEdit && detail && selectedTableId && (
        <KnowhowAppendWizard
          notebookId={notebookId}
          tableId={selectedTableId}
          columns={detail.columns}
          onClose={() => setAppendOpen(false)}
          onDone={() => {
            setAppendOpen(false);
            loadDetail(selectedTableId);
            loadTables();
          }}
        />
      )}

      {optimizeRowId && optimizeRow && detail && selectedTableId && (
        <KnowhowRowOptimizeModal
          notebookId={notebookId}
          apiBase={apiBase}
          tableId={selectedTableId}
          row={optimizeRow}
          columns={detail.columns}
          rowTitle={optimizeRowTitle}
          onAcceptCell={handleCellSave}
          onClose={() => setOptimizeRowId(null)}
        />
      )}

      {/* knowhow-md-normalize Task 9「一键规整整行」——KnowhowReformatBatchModal
          按 scope="row" 只处理这一行，rows 传单元素数组（"整行/整表用同一份
          init 逻辑"，见 knowhow-optimize-logic.ts 第 5 节头注释）。 */}
      {reformatRowId && reformatRow && detail && selectedTableId && (
        <KnowhowReformatBatchModal
          notebookId={notebookId}
          apiBase={apiBase}
          tableId={selectedTableId}
          scope="row"
          rows={[reformatRow]}
          allRows={detail.rows}
          columns={detail.columns}
          anchorColumnId={detail.anchorColumnId}
          title={reformatRowTitle}
          onSaveCell={handleCellSave}
          // F（review）：保存阶段跳过过 stale 格 -> 刷新整表，下次批量用新鲜基线复跑。
          onStaleReload={reloadTableDetail}
          onOpenSavedCell={(rowId, columnId, needsReload) =>
            openReformatSavedCell("row", rowId, columnId, needsReload)}
          onClose={() => setReformatRowId(null)}
        />
      )}

      {/* 「一键规整整表」——scope="table"，rows 传整表全部行；这是导入后把
          整表 LLM 精整的入口（设计文档 §5.3）。 */}
      {reformatTableOpen && detail && selectedTableId && (
        <KnowhowReformatBatchModal
          notebookId={notebookId}
          apiBase={apiBase}
          tableId={selectedTableId}
          scope="table"
          rows={detail.rows}
          allRows={detail.rows}
          columns={detail.columns}
          anchorColumnId={detail.anchorColumnId}
          title={detail.title}
          onSaveCell={handleCellSave}
          // F（review）：保存阶段跳过过 stale 格 -> 刷新整表，下次批量用新鲜基线复跑。
          onStaleReload={reloadTableDetail}
          onOpenSavedCell={(rowId, columnId, needsReload) =>
            openReformatSavedCell("table", rowId, columnId, needsReload)}
          onClose={() => setReformatTableOpen(false)}
        />
      )}

      {codeModal && codeModalRow && codeModalColumn && (
        <KnowhowCodeModal
          key={`${codeModal.rowId}:${codeModal.columnId}`}
          rowId={codeModal.rowId}
          columnId={codeModal.columnId}
          rowTitle={codeModalRowTitle}
          columnName={codeModalColumn.name}
          code={resolveCellCodeView(rowCodeByColumn, codeModal.columnId)}
          canEdit={canEdit}
          onSaved={handleCodeSaved}
          onDeleted={handleCodeDeleted}
          onClose={() => setCodeModal(null)}
        />
      )}

      <KnowhowPanelStyles />
    </section>
  );
}

// ---------------------------------------------------------------------------
// Layer 1 — 表列表
// ---------------------------------------------------------------------------

/** Knowhow 表卡片清单每页条数。接口一次性返回整份清单(GET /notebooks/{id}/knowhow
 * 不分页),分页只发生在界面。 */
const KNOWHOW_TABLE_PAGE_SIZE = 24;

function KnowhowTableList({
  tables,
  hasUnfilteredTables,
  loading,
  error,
  canEdit,
  healthFilter,
  onHealthFilterChange,
  onRetry,
  onOpen,
  onImportClick,
  onCreateClick,
}: {
  tables: KnowhowTableSummary[];
  hasUnfilteredTables: boolean;
  loading: boolean;
  error: string | null;
  canEdit: boolean;
  healthFilter: KnowhowHealthFilter;
  onHealthFilterChange: (filter: KnowhowHealthFilter) => void;
  onRetry: () => void;
  onOpen: (tableId: string) => void;
  onImportClick: () => void;
  onCreateClick: () => void;
}) {
  const tablePage = useClientPagination(tables, KNOWHOW_TABLE_PAGE_SIZE, healthFilter);
  return (
    <>
      <div className="knowhow-toolbar">
        <span className="panel-count">{tables.length > 0 ? `${tables.length} 张表` : ""}</span>
        <label className="knowhow-health-filter">
          <span>状态</span>
          <select
            value={healthFilter}
            onChange={(event) => onHealthFilterChange(event.target.value as KnowhowHealthFilter)}
          >
            <option value="all">全部表</option>
            <option value="projection_pending">待同步</option>
            <option value="projection_failed">同步失败</option>
            <option value="stale_code">代码已过期</option>
          </select>
        </label>
        {canEdit && (
          <div className="knowhow-toolbar-actions">
            <button type="button" className="sort-button knowhow-import-button" onClick={onCreateClick}>
              <Plus size={16} /> 新建表
            </button>
            <button type="button" className="sort-button knowhow-import-button" onClick={onImportClick}>
              <Upload size={16} /> 导入表格
            </button>
          </div>
        )}
      </div>

      {loading ? (
        <p className="knowhow-loading">加载中…</p>
      ) : error ? (
        <p className="knowhow-inline-error">
          {error}
          <button type="button" className="sort-button" onClick={onRetry}>
            重试
          </button>
        </p>
      ) : tables.length === 0 ? (
        <div className="knowhow-empty">
          <div className="knowhow-empty-icon">▦</div>
          <strong>{hasUnfilteredTables ? "当前筛选下没有表" : "还没有 knowhow 表"}</strong>
          {!hasUnfilteredTables && (
            <p>{canEdit ? "可从 Excel/CSV/Markdown 导入，或新建空表现场定表头" : "当前为只读访问"}</p>
          )}
        </div>
      ) : (
        <>
          <div className="knowhow-table-cards">
            {tablePage.pageItems.map((table) => (
              <button
                type="button"
                key={table.id}
                className="knowhow-table-card"
                aria-label={`打开表格：${table.title}`}
                onClick={() => onOpen(table.id)}
              >
                <strong title={table.title}>{table.title}</strong>
                {table.description && <p>{table.description}</p>}
                <span className="knowhow-table-card-meta">{table.rowCount} 行</span>
              </button>
            ))}
          </div>
          <Pagination
            page={tablePage.page}
            pageSize={KNOWHOW_TABLE_PAGE_SIZE}
            total={tablePage.total}
            onPage={tablePage.setPage}
            label="Knowhow 表分页"
          />
        </>
      )}
    </>
  );
}

// ---------------------------------------------------------------------------
// Layer 2 — 表格网格
// ---------------------------------------------------------------------------

function KnowhowTableGrid({
  detail,
  loading,
  error,
  actionError,
  canEdit,
  onDismissActionError,
  onRetryLoad,
  query,
  onQueryChange,
  onOpenRow,
  onOpenCell,
  onOpenConcept,
  onBack,
  confirmDelete,
  onRequestDelete,
  onCancelDelete,
  onConfirmDelete,
  deleting,
  onRetryReproject,
  retryingReproject,
  onOpenManage,
  onTransfer,
  onAddRow,
  addingRow,
  onAddConcept,
  onDownloadTemplate,
  templateDownloading,
  onAppendClick,
  onReformatTableClick,
  onRenameColumn,
  onOpenHistory,
}: {
  detail: KnowhowTableDetail | null;
  loading: boolean;
  error: string | null;
  actionError: string | null;
  canEdit: boolean;
  onDismissActionError: () => void;
  onRetryLoad: () => void;
  query: string;
  onQueryChange: (value: string) => void;
  onOpenRow: (rowId: string) => void;
  /** 非行标题列格子点击（规格②路A「点格子弹浮窗」）：panel 按该格是否已有
   * 内容自动决定预览还是编辑态。 */
  onOpenCell: (rowId: string, columnId: string) => void;
  /** anchor 列格子点击（Task 8，规格 §4.3「点概念打开矩阵抽屉」）：传回该格
   * 的概念值，panel 据此打开「属性×分支」矩阵抽屉。只有已填值的 anchor 格
   * 才走这里（见 tbody 内 opensConceptDrawer 判据）——forward-fill 后仍空的
   * 「无概念」行（规格 §4.2.2）保持走 onOpenCell 原地补概念，不经这里。 */
  onOpenConcept: (anchorValue: string) => void;
  onBack: () => void;
  confirmDelete: boolean;
  onRequestDelete: () => void;
  onCancelDelete: () => void;
  onConfirmDelete: () => void;
  deleting: boolean;
  onRetryReproject: () => void;
  retryingReproject: boolean;
  onOpenManage: () => void;
  /** C3「复制/移动到…」：打开目标笔记本选择器（DestinationPicker）。与
   * onOpenManage 同一模式——面板自持 modal 显隐态，这里只负责打开。不像
   * 其余写入口那样整体挂在 canEdit 门内：copy 只需对源笔记本有读权限，只读
   * 成员也该看到这个按钮（能复制、不能移动），故渲染处单独处理，见下方
   * JSX。 */
  onTransfer: () => void;
  onAddRow: () => void;
  addingRow: boolean;
  /** Task 10（加概念）：主网格底部「+ 概念」——只在有 anchor 列（分组视图）
   * 且 canEdit 时渲染；记录型表没有这个入口，继续只有顶部「添加行」一个
   * 写入口不变。 */
  onAddConcept: () => void;
  /** 「下载模板」（Task 9，规格②路B）：按当前表头下载 xlsx 模板。 */
  onDownloadTemplate: () => void;
  templateDownloading: boolean;
  /** 「追加导入」（Task 9，规格②路B）：打开追加导入向导。 */
  onAppendClick: () => void;
  /** 「一键规整整表」（knowhow-md-normalize Task 9）：打开批量规整弹窗，对
   * 整表非空格子逐格调用 reformat_cell，汇总后人工整体确认才落库——是导入
   * 后把整表 LLM 精整的入口（设计文档 §5.3）。 */
  onReformatTableClick: () => void;
  /** 表头列名 inline 改名（canEdit 时启用）：双击列名进入编辑框，回车/失焦
   * 时把新名字丢回来落库。用户不必再翻「管理」抽屉找列改名——就地改。 */
  onRenameColumn: (columnId: string, name: string) => void;
  /** knowhow 表版本管理 Task 15：打开历史抽屉——同 onTransfer 一样不整体挂在
   * canEdit 门内（只读成员也能看时间线和 diff，见按钮渲染处注释），面板自持
   * historyOpen 显隐态，这里只负责打开。 */
  onOpenHistory: () => void;
}) {
  const orderedColumns = useMemo(() => (detail ? orderColumnsForGrid(detail.columns) : []), [detail]);
  const filteredRows = useMemo(() => (detail ? filterRows(detail.rows, query) : []), [detail, query]);
  const [compactColumnWidths, setCompactColumnWidths] = useState(false);

  // Reuse the established 720px narrow-screen boundary. Width computation stays
  // data-only; viewport changes merely select the approved compact clamp table.
  useEffect(() => {
    const sync = () => setCompactColumnWidths(isFloatingDisabledWidth(window.innerWidth));
    sync();
    window.addEventListener("resize", sync);
    return () => window.removeEventListener("resize", sync);
  }, []);

  const sampledRows = useMemo(() => sampleVisibleKnowhowRows(filteredRows), [filteredRows]);

  // 有 anchor 列 → 分组合并矩阵渲染（spec §4.2 G2）；无 anchor（记录型表）
  // → gridDisplayRows 为 null，<tbody> 落回原平铺渲染，零改动（spec §4.2.3）。
  const anchorColumnId = detail?.anchorColumnId ?? null;
  const columnWidths = useMemo(() => computeKnowhowColumnWidths({
    columns: orderedColumns,
    visibleRowsSample: sampledRows,
    anchorColumnId,
    compact: compactColumnWidths,
  }), [anchorColumnId, compactColumnWidths, orderedColumns, sampledRows]);
  const gridDisplayRows = useMemo(() => {
    if (!anchorColumnId) return null;
    const groups = groupRowsByAnchor(filteredRows, anchorColumnId);
    return computeGridSpans(groups, orderedColumns);
  }, [anchorColumnId, filteredRows, orderedColumns]);

  return (
    <div className="knowhow-grid-view">
      <div className="knowhow-grid-toolbar">
        <button type="button" className="sort-button" onClick={onBack}>
          <ChevronLeft size={16} /> 返回
        </button>
        <div className="knowhow-grid-title">
          <h3 title={detail?.title}>{detail?.title ?? ""}</h3>
          {detail?.description && <p>{detail.description}</p>}
        </div>
        {/* 添加行/下载模板/追加导入/管理/重建投影/删除表——这些写入口只对
            canEdit 出现；只读成员的工具栏在返回与标题之外，只剩下面
            「复制/移动到…」一个入口（规格⑦的例外，C3）：它不写入
            本笔记本——copy 只需对源笔记本有读权限，只读成员也该能把表复制到
            自己有写权限的另一个笔记本；真正的写权限边界（能否「移动」=会从
            源删除）收在 allowMove={canEdit}，由 DestinationPicker 内部据此
            隐藏移动单选项，与后端 source_check 的 copy/move 分支对齐。 */}
        <div className="knowhow-grid-toolbar-actions">
          {canEdit && (
            <>
              <button
                type="button"
                className="sort-button knowhow-reproject-button"
                onClick={onAddRow}
                disabled={addingRow || deleting || !detail}
                title="在表末尾添加一行"
              >
                <ListPlus size={14} />
                {addingRow ? "添加中…" : "添加行"}
              </button>
              {/* 「下载模板」+「追加导入」（Task 9，规格②路B）：前者按当前表头
                  下载 xlsx 模板供线下批量填写，后者把填好的文件追加导入回这
                  张表——两个入口紧邻，模板下载天然是追加导入的前置步骤。 */}
              <button
                type="button"
                className="sort-button knowhow-reproject-button"
                onClick={onDownloadTemplate}
                disabled={!detail || templateDownloading}
                title="按当前表头下载 Excel 模板，线下批量填写"
              >
                {templateDownloading ? <Loader2 size={14} className="knowhow-spin" /> : <Download size={14} />}
                {templateDownloading ? "下载中…" : "下载模板"}
              </button>
              <button
                type="button"
                className="sort-button knowhow-reproject-button"
                onClick={onAppendClick}
                disabled={!detail || deleting}
                title="从填好的 Excel/CSV/Markdown 追加导入行"
              >
                <Upload size={14} />
                追加导入
              </button>
              {/* 「一键规整整表」（knowhow-md-normalize Task 9）：紧邻「追加导入」
                  ——最典型的使用时机就是导入后把整表统一交给 AI 精整排版一遍
                  （设计文档 §5.3）。批量弹窗自己会先展示将处理的格子数、跑完后
                  要求人工整体确认才落库，这里的按钮本身不需要二次确认。 */}
              <button
                type="button"
                className="sort-button knowhow-reproject-button"
                onClick={onReformatTableClick}
                disabled={!detail || deleting}
                title="对整张表的非空格子批量规整格式（跑完后需人工确认才保存）"
              >
                <ListChecks size={14} />
                {BATCH_REFORMAT_TABLE_BUTTON_LABEL}
              </button>
              <button
                type="button"
                className="sort-button knowhow-reproject-button"
                onClick={onOpenManage}
                disabled={!detail || deleting}
                title="表信息、行标题列、列与行管理"
              >
                <Settings2 size={14} />
                管理
              </button>
            </>
          )}
          {/* C3：「复制/移动到…」——不整体挂在 canEdit 门内的表级动作之一
              （另一个是紧随其后的「历史」，见该按钮注释），紧邻「管理」，
              理由见上方容器注释。 */}
          <button
            type="button"
            className="sort-button knowhow-reproject-button"
            onClick={onTransfer}
            disabled={!detail || deleting}
            title="把这张表复制或移动到另一个笔记本"
          >
            <Copy size={14} />
            复制/移动到…
          </button>
          {/* knowhow 表版本管理 Task 15：「历史」——同「复制/移动到…」一样不
              整体挂在 canEdit 门内（只读成员也能看时间线和单次改动/两版
              diff，design doc §8.2 末尾"权限"一节明文；只有回退/里程碑这两个
              写入口收在抽屉内部按 canEdit 二级门控，见 knowhow-history-
              drawer.tsx）。 */}
          <button
            type="button"
            className="sort-button knowhow-reproject-button"
            onClick={onOpenHistory}
            disabled={!detail || deleting}
            title="查看这张表的变更历史"
          >
            <History size={14} />
            历史
          </button>
          {canEdit && (
            <>
              {/* 表级「重建投影」逃生口(spec 要求)：整表重投影，后台执行。区别于
                  失败行徽标上的行内「重试」——那只在某行 failed 时出现，这个入口
                  任何时候都在，供用户在整表走样时一键重建。进行中禁用防重复触发。 */}
              <button
                type="button"
                className="sort-button knowhow-reproject-button"
                onClick={onRetryReproject}
                disabled={retryingReproject || deleting || !detail}
                title="重新同步整张表（后台执行）"
              >
                <RefreshCw size={14} className={retryingReproject ? "knowhow-spin" : undefined} />
                {retryingReproject ? "重建中…" : "重新同步"}
              </button>
              {confirmDelete ? (
                <span className="knowhow-confirm">
                  <span>删除这张表？行、格与同步生成的内容将一并删除</span>
                  <button type="button" className="knowhow-confirm-yes" disabled={deleting} onClick={onConfirmDelete}>
                    {deleting ? "删除中…" : "确认删除"}
                  </button>
                  <button type="button" className="knowhow-confirm-no" onClick={onCancelDelete}>
                    取消
                  </button>
                </span>
              ) : (
                <button type="button" className="icon-button" title="删除表" onClick={onRequestDelete}>
                  <Trash2 size={18} />
                </button>
              )}
            </>
          )}
        </div>
      </div>

      {actionError && (
        <div className="knowhow-action-error">
          <span>{actionError}</span>
          <button type="button" onClick={onDismissActionError} title="关闭">
            <X size={14} />
          </button>
        </div>
      )}

      {loading ? (
        <p className="knowhow-loading">加载中…</p>
      ) : error ? (
        <p className="knowhow-inline-error">
          {error}
          <button type="button" className="sort-button" onClick={onRetryLoad}>
            重试
          </button>
        </p>
      ) : detail ? (
        <>
          <div className="knowhow-filter-row">
            <Search size={15} className="knowhow-filter-icon" />
            <input
              className="knowhow-filter-input"
              type="search"
              placeholder="按行标题/全文过滤行…"
              value={query}
              onChange={(event) => onQueryChange(event.target.value)}
            />
          </div>
          <div className="knowhow-grid-scroll">
            <table
              className="knowhow-grid-table"
              style={{ width: columnWidths.tableWidthPx, minWidth: "100%" }}
            >
              <colgroup>
                {columnWidths.columns.map((column) => (
                  <col key={column.columnId} style={{ width: column.widthPx }} />
                ))}
                <col key="projection-status" style={{ width: columnWidths.statusWidthPx }} />
              </colgroup>
              <thead>
                <tr>
                  {orderedColumns.map((column) => (
                    <th key={column.id}>
                      <div className="knowhow-col-header">
                        <ColumnNameCell
                          column={column}
                          canEdit={canEdit}
                          onRename={(name) => onRenameColumn(column.id, name)}
                        />
                        <RoleBadge role={column.role} />
                      </div>
                    </th>
                  ))}
                  <th>同步状态</th>
                </tr>
              </thead>
              <tbody>
                {gridDisplayRows === null ? (
                  // 记录型表（无 anchor 列）：原平铺渲染，逐物理行展开，
                  // 一行代码都不动——逻辑与改动前完全一致（规格 §4.2.3）。
                  filteredRows.map((row) => (
                    <tr key={row.id}>
                      {orderedColumns.map((column, index) => {
                        const text = cellSummary(row.cells[column.id] ?? "");
                        const filled = Boolean(text);
                        // 行标题列（网格首列，规格②路A）填了内容后仍走既有的
                        // 整行抽屉入口（规格⑤「点行首/概念列打开」）；空的话
                        // 直接进格子编辑态更有引导性（规格②路A「空格子直接进
                        // 编辑态」），而不是打开一个几乎全空的抽屉。其余列
                        // （或空的行标题列）一律走格子浮窗，由 panel 按内容
                        // 有无决定预览/编辑。空格 + 只读成员没有可点的入口
                        // （规格⑦「只读成员可看」——没什么可填，也没什么可读）。
                        const opensRowDrawer = index === 0 && filled;
                        const clickable = opensRowDrawer || filled || canEdit;
                        return (
                          <td key={column.id}>
                            {clickable ? (
                              <button
                                type="button"
                                className={`knowhow-cell-open${filled ? "" : " knowhow-cell-open--empty"}`}
                                onClick={() => (opensRowDrawer ? onOpenRow(row.id) : onOpenCell(row.id, column.id))}
                                title={filled ? text : "点击填写这一格"}
                              >
                                {filled ? text : "+"}
                              </button>
                            ) : (
                              <span className="knowhow-cell-text">—</span>
                            )}
                          </td>
                        );
                      })}
                      <td>
                        <ProjectionStatusBadge
                          status={row.projectionStatus}
                          canRetry={canEdit}
                          onRetry={onRetryReproject}
                          retrying={retryingReproject}
                        />
                      </td>
                    </tr>
                  ))
                ) : (
                  // 有 anchor 列：G2 合并矩阵——同概念内相邻同值列合并成一个
                  // rowspan 起始格（规格 §4.2.1），rowSpan===0 的格被上方合并
                  // 格覆盖、渲染跳过。非 anchor 格点击仍落到该格所属的具体物理
                  // 行、复用现有 onOpenCell 单格写入路径（合并格批量写回整组是
                  // 后续 task 的编辑增强，此处先保证显示正确、行为不倒退）；
                  // anchor 列格子改走 onOpenConcept 打开该概念的矩阵抽屉
                  // （Task 8，规格 §4.3），见下方 opensConceptDrawer 判据。
                  gridDisplayRows.map(({ row, cells }) => (
                    <tr key={row.id}>
                      {cells.map((cell) => {
                        if (cell.rowSpan === 0) return null;
                        const text = cellSummary(cell.text);
                        const filled = Boolean(text);
                        const clickable = filled || canEdit;
                        const merged = cell.rowSpan > 1;
                        // 只有已填值的 anchor 格才打开概念矩阵抽屉——forward-fill
                        // 后仍空的「无概念」行（规格 §4.2.2，leading-blank）不
                        // 聚合、每行各自单独成一个 anchorValue==="" 的组
                        // （groupRowsByAnchor），若也路由到这里，多个空概念行会
                        // 撞同一个 "" 值、打开彼此不相干的那一个；而矩阵抽屉本身
                        // 又不含 anchor 列（buildConceptMatrix 排除），压根没地方
                        // 补概念名，会把用户卡死。保持走 onOpenCell 原地补概念
                        // （空格子直接进编辑态，规格②路A），与下方「非 anchor
                        // 格」同一套行为，只是列身份不同。
                        const opensConceptDrawer = cell.columnId === anchorColumnId && filled;
                        return (
                          <td
                            key={cell.columnId}
                            rowSpan={merged ? cell.rowSpan : undefined}
                            className={merged ? "knowhow-cell-merged" : undefined}
                          >
                            {clickable ? (
                              <button
                                type="button"
                                className={`knowhow-cell-open${filled ? "" : " knowhow-cell-open--empty"}`}
                                onClick={() =>
                                  opensConceptDrawer
                                    ? onOpenConcept(cell.text.trim())
                                    : onOpenCell(row.id, cell.columnId)
                                }
                                title={filled ? text : "点击填写这一格"}
                              >
                                {filled ? text : "+"}
                              </button>
                            ) : (
                              <span className="knowhow-cell-text">—</span>
                            )}
                          </td>
                        );
                      })}
                      <td>
                        <ProjectionStatusBadge
                          status={row.projectionStatus}
                          canRetry={canEdit}
                          onRetry={onRetryReproject}
                          retrying={retryingReproject}
                        />
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
            {filteredRows.length === 0 && (
              <p className="knowhow-no-match">
                {detail.rows.length === 0
                  ? canEdit
                    ? anchorColumnId
                      ? "这张表还没有概念，点下方「概念」开始填写。"
                      : "这张表还没有行，点上方「添加行」开始填写。"
                    : "这张表还没有行。"
                  : `没有匹配「${query}」的行。`}
              </p>
            )}
          </div>
          {/* Task 10（加概念）：只在有 anchor 列的分组视图出现——记录型表
              （anchorColumnId 为 null）保持现状，仍只有顶部工具栏「添加行」
              一个入口，不在这里重复一个几乎等价的按钮。故意放在
              .knowhow-grid-scroll 之外（而不是紧贴表格最后一行内部）：矩阵
              可能很长，把入口钉在滚动区域外层能让它不必滚到底才可见，也不与
              G2 合并单元格的 rowSpan 结构绞在一起。 */}
          {canEdit && anchorColumnId && (
            <div>
              <button
                type="button"
                className="sort-button knowhow-reproject-button"
                onClick={onAddConcept}
                disabled={addingRow || deleting}
                title="新增一个概念"
              >
                <Plus size={14} />
                {addingRow ? "添加中…" : "概念"}
              </button>
            </div>
          )}
        </>
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Layer 3 — 行详情抽屉
// ---------------------------------------------------------------------------

function KnowhowRowDrawer({
  row,
  columns,
  notebookId,
  apiBase,
  canEdit,
  onEditCell,
  onClose,
  onOptimizeRow,
  anchorColumnId,
  onCompleteRow,
  onReformatRow,
  cellModalOpen,
  codeByColumn,
  codeLoaded,
  codeError,
  onRetryCode,
  onOpenCode,
}: {
  row: KnowhowRow;
  columns: KnowhowColumn[];
  notebookId: string;
  apiBase: string;
  /** 只读成员不出现分节「编辑」按钮（规格⑦「只读成员可看」）。 */
  canEdit: boolean;
  /** 分节「编辑」按钮（规格⑤「每节「编辑」按钮打开同一个格子浮窗」）：打开
   * 该列对应格子的编辑态，不经预览态中转——用户已经明确点了「编辑」。 */
  onEditCell: (rowId: string, columnId: string) => void;
  onClose: () => void;
  /** 「优化整行」入口（Task 9，规格③）：打开批量优化弹窗，堆叠在本抽屉之上。 */
  onOptimizeRow: () => void;
  /** 智能补全只面向空的非行标题列；只读成员无入口。 */
  anchorColumnId: string | null;
  onCompleteRow: (trigger: HTMLButtonElement) => void;
  /** 「一键规整整行」入口（knowhow-md-normalize Task 9）：打开批量规整弹窗，
   * 同样堆叠在本抽屉之上——与「优化整行」并列但语义不同（规整=只改格式，
   * 优化=改措辞，见 knowhow-cell-editor-logic.ts TOOLBAR_REFORMAT_LABEL
   * 头注释同一区分）。 */
  onReformatRow: () => void;
  /** 任何堆叠在本抽屉之上的顶层弹窗（格子浮窗编辑态/预览态，Task 9 的
   * KnowhowRowOptimizeModal，或 Task 11 的 KnowhowCodeModal）当前是否打开
   * （T7 复审 Important 修复）：为 true 时本抽屉的 Esc 监听器短路、不关闭
   * 抽屉——各自独立的 window keydown 监听器按注册顺序（抽屉先挂载，先注册）
   * 依次响应同一次按键，若不在这里短路，一次 Esc 会先无条件关闭本抽屉（它
   * 自己没有"未保存内容"的概念），顶层弹窗随后可能因为有未保存内容/进行中
   * 的操作改为弹出确认层而不真正关闭——结果抽屉已经消失、弹窗却还留着，
   * 用户之后关掉弹窗时会发现连"返回这一行"的抽屉上下文都丢了。真正想关的
   * 是最顶层的弹窗，Esc 应该只作用于它，抽屉留到弹窗自己关闭之后再响应下
   * 一次 Esc。 */
  cellModalOpen: boolean;
  /** Task 11：本行按列索引的代码状态 map（父组件在抽屉打开时一次性拉取，见
   * knowhow-panel.tsx 顶层的 loadRowCode）。 */
  codeByColumn: Record<string, KnowhowCellCode>;
  /** 是否已成功加载过 codeByColumn——false 时（含加载失败）任何分节都不渲染
   * chip，宁可暂时不显示也不能显示可能误导的"添加代码"。 */
  codeLoaded: boolean;
  /** 非 null 时在抽屉正文顶部渲染一条轻量内联提示 + 「重试」。 */
  codeError: string | null;
  onRetryCode: () => void;
  /** 打开某一格的代码查看/编辑浮层（chip 本身，及 canEdit 时的"添加代码"
   * 安静入口共用同一个回调）。 */
  onOpenCode: (rowId: string, columnId: string) => void;
}) {
  useEffect(() => {
    function handleKeyDown(event: globalThis.KeyboardEvent) {
      if (event.key === "Escape" && !cellModalOpen) onClose();
    }
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [onClose, cellModalOpen]);

  const orderedColumns = useMemo(() => sortColumnsByPosition(columns), [columns]);
  const titleText = cellSummary(resolveRowTitleText(row, columns), 200) || "行详情";
  const hasCompletableColumns = completableKnowhowColumns(row, columns, anchorColumnId).length > 0;

  return (
    <>
      <button type="button" className="knowhow-drawer-backdrop" aria-label="关闭行详情" onClick={onClose} />
      <section className="knowhow-drawer" role="dialog" aria-modal="true" aria-label={titleText}>
        <div className="knowhow-drawer-header">
          <h2>{titleText}</h2>
          <div className="knowhow-drawer-header-actions">
            {/* 「优化整行」批量入口（Task 9，规格③）——只读成员看不到（规格⑦）。 */}
            {canEdit && (
              <button type="button" className="knowhow-drawer-edit-button" onClick={onOptimizeRow}>
                <Sparkles size={13} /> {ROW_OPTIMIZE_BUTTON_LABEL}
              </button>
            )}
            {canEdit && hasCompletableColumns && (
              <button
                type="button"
                className="knowhow-drawer-edit-button"
                onClick={(event) => onCompleteRow(event.currentTarget)}
              >
                <Sparkles size={13} /> 智能补全空列
              </button>
            )}
            {/* 「一键规整整行」批量入口（knowhow-md-normalize Task 9）——挨着
                「优化整行」，同一只读门控。 */}
            {canEdit && (
              <button type="button" className="knowhow-drawer-edit-button" onClick={onReformatRow}>
                <ListChecks size={13} /> {BATCH_REFORMAT_ROW_BUTTON_LABEL}
              </button>
            )}
            <button className="icon-button" onClick={onClose} title="关闭">
              <X size={20} />
            </button>
          </div>
        </div>
        <div className="knowhow-drawer-body">
          {/* Task 11：本行代码状态加载失败的轻量提示——不阻塞其余分节的正常
              渲染，只影响代码 chip 这一小块辅助信息。 */}
          {codeError && (
            <p className="kh-code-status-error">
              <span>{codeError}</span>
              <button type="button" onClick={onRetryCode}>
                重试
              </button>
            </p>
          )}
          {orderedColumns.map((column) => (
            <section key={column.id}>
              <div className="knowhow-drawer-section-head">
                <h4>{column.name}</h4>
                <RoleBadge role={column.role} />
                {codeLoaded && (
                  <KnowhowCodeChip
                    code={resolveCellCodeView(codeByColumn, column.id)}
                    canEdit={canEdit}
                    onOpen={() => onOpenCode(row.id, column.id)}
                  />
                )}
                {canEdit && (
                  <button
                    type="button"
                    className="knowhow-drawer-edit-button"
                    onClick={() => onEditCell(row.id, column.id)}
                  >
                    <Edit3 size={13} /> 编辑
                  </button>
                )}
              </div>
              <KnowhowMarkdown md={row.cells[column.id] ?? ""} notebookId={notebookId} apiBase={apiBase} />
            </section>
          ))}
        </div>
      </section>
    </>
  );
}

// ---------------------------------------------------------------------------
// KnowhowRowCompletionModal — 对当前行所有空的非行标题列生成建议，逐项审阅。
// 生成本身不写库；每次接受都复用 handleCellSave，并以空串为并发基线。
// ---------------------------------------------------------------------------

type CompletionSave = (
  rowId: string,
  columnId: string,
  contentMd: string,
  expectedByRowId?: Map<string, string>,
  targetRowIds?: string[],
  anchorGuard?: { anchorColumnId: string; expectedAnchorByRowId: Map<string, string> },
  origin?: string,
) => Promise<void>;

export function KnowhowRowCompletionModal({
  notebookId,
  apiBase,
  tableId,
  row,
  rows,
  columns,
  anchorColumnId,
  rowTitle,
  returnFocusTo,
  fallbackFocusTo,
  onAcceptCell,
  onClose,
}: {
  notebookId: string;
  apiBase: string;
  tableId: string;
  row: KnowhowRow;
  rows: KnowhowRow[];
  columns: KnowhowColumn[];
  anchorColumnId: string | null;
  rowTitle: string;
  returnFocusTo: HTMLButtonElement | null;
  fallbackFocusTo: HTMLElement | null;
  onAcceptCell: CompletionSave;
  onClose: () => void;
}) {
  // 弹窗打开时冻结目标列。接受一项后父 detail 会更新，但不能因此重新请求、
  // 也不能让尚待审阅的其它建议突然从列表消失。
  const [targetColumns] = useState(() => completableKnowhowColumns(row, columns, anchorColumnId));
  const [savePlans] = useState(() => new Map(
    completableKnowhowColumns(row, columns, anchorColumnId).map((column) => [
      column.id,
      completionSavePlan(row.id, column.id, rows, anchorColumnId),
    ]),
  ));
  const [completion, setCompletion] = useState<KnowhowRowCompletion | null>(null);
  const suggestions = completion?.suggestions ?? null;
  const [loadError, setLoadError] = useState<string | null>(null);
  const [savingColumnId, setSavingColumnId] = useState<string | null>(null);
  const [acceptedColumnIds, setAcceptedColumnIds] = useState<Set<string>>(() => new Set());
  const [saveErrors, setSaveErrors] = useState<Record<string, string>>({});
  const mountedRef = useRef(false);
  const startedRef = useRef(false);
  const requestRef = useRef(0);
  const requestControllerRef = useRef<AbortController | null>(null);
  const pendingRequestAbortRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const floating = useFloatingWindow({ storageKey: "knowhow.rowCompletion.window" });

  function requestSuggestions() {
    requestControllerRef.current?.abort();
    const controller = new AbortController();
    requestControllerRef.current = controller;
    const requestId = ++requestRef.current;
    setCompletion(null);
    setLoadError(null);
    completeKnowhowRow(
      notebookId,
      tableId,
      row.id,
      targetColumns.map((column) => column.id),
      controller.signal,
    )
      .then((result) => {
        if (controller.signal.aborted || !mountedRef.current || requestRef.current !== requestId) return;
        const byColumnId = new Map(result.suggestions.map((suggestion) => [suggestion.columnId, suggestion]));
        setCompletion({
          ...result,
          suggestions: targetColumns.flatMap((column) => {
            const suggestion = byColumnId.get(column.id);
            return suggestion ? [suggestion] : [];
          }),
        });
      })
      .catch((error) => {
        if (controller.signal.aborted || !mountedRef.current || requestRef.current !== requestId) return;
        setLoadError(extractErrorMessage(error, "生成建议失败，请稍后重试"));
      })
      .finally(() => {
        if (requestControllerRef.current === controller) {
          requestControllerRef.current = null;
        }
      });
  }

  useEffect(() => {
    if (pendingRequestAbortRef.current !== null) {
      clearTimeout(pendingRequestAbortRef.current);
      pendingRequestAbortRef.current = null;
    }
    mountedRef.current = true;
    // React StrictMode 会做一次 effect setup→cleanup→setup 探测。cleanup 把取消
    // 延后一拍；紧随其后的 setup 撤销它并继续持有同一请求，真实卸载才会取消。
    if (!startedRef.current) {
      startedRef.current = true;
      requestSuggestions();
    }
    closeButtonRef.current?.focus();
    return () => {
      mountedRef.current = false;
      const controller = requestControllerRef.current;
      pendingRequestAbortRef.current = setTimeout(() => {
        if (requestControllerRef.current === controller) {
          controller?.abort();
          requestControllerRef.current = null;
        }
        pendingRequestAbortRef.current = null;
      }, 0);
      const focusTarget = returnFocusTo?.isConnected ? returnFocusTo : fallbackFocusTo?.isConnected ? fallbackFocusTo : null;
      focusTarget?.focus();
    };
    // 每次挂载只请求一次；retry 由按钮显式触发，目标列已冻结。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function requestClose() {
    // 保存中不允许卸载；请求建议时可以关闭并协作取消后端工作。
    if (savingColumnId) return;
    requestControllerRef.current?.abort();
    onClose();
  }

  useEffect(() => {
    function handleKeyDown(event: globalThis.KeyboardEvent) {
      if (event.key !== "Escape") return;
      event.preventDefault();
      requestClose();
    }
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [savingColumnId]);

  async function acceptSuggestion(suggestion: KnowhowRowCompletionSuggestion) {
    if (savingColumnId || acceptedColumnIds.has(suggestion.columnId) || !canAcceptCompletionSuggestion(suggestion)) {
      return;
    }
    setSavingColumnId(suggestion.columnId);
    setSaveErrors((current) => {
      const next = { ...current };
      delete next[suggestion.columnId];
      return next;
    });
    try {
      const savePlan = savePlans.get(suggestion.columnId);
      if (!savePlan) throw new Error("completion save plan missing");
      // 弹窗打开时已冻结完整写目标：共享格包含每个成员的空串基线与 anchor
      // 原值；非共享列只有当前行。服务端据此以 409 阻止覆盖或成员漂移。
      await onAcceptCell(
        row.id,
        suggestion.columnId,
        suggestion.suggestionMd ?? "",
        savePlan.expectedByRowId,
        savePlan.targetRowIds,
        savePlan.anchorGuard,
        "llm_complete",
      );
      if (!mountedRef.current) return;
      setAcceptedColumnIds((current) => new Set(current).add(suggestion.columnId));
    } catch (error) {
      if (!mountedRef.current) return;
      setSaveErrors((current) => ({
        ...current,
        [suggestion.columnId]: extractErrorMessage(error, "保存失败，请稍后重试"),
      }));
    } finally {
      if (mountedRef.current) setSavingColumnId(null);
    }
  }

  function handleBackdropClick(event: ReactMouseEvent<HTMLDivElement>) {
    if (event.currentTarget === event.target) requestClose();
  }

  function trapFocus(event: ReactKeyboardEvent<HTMLDivElement>) {
    if (event.key !== "Tab") return;
    const focusable = Array.from(
      event.currentTarget.querySelectorAll<HTMLElement>(
        'button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
      ),
    );
    if (focusable.length === 0) {
      event.preventDefault();
      return;
    }
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && (document.activeElement === first || !event.currentTarget.contains(document.activeElement))) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  return (
    <div className="kh-modal-overlay" onClick={handleBackdropClick}>
      <div
        ref={floating.cardRef}
        className="kh-modal-card kh-completion-card"
        style={floating.style}
        role="dialog"
        aria-modal="true"
        aria-label={`智能补全空列 · ${rowTitle}`}
        onClick={(event) => event.stopPropagation()}
        onKeyDown={trapFocus}
      >
        <header className="kh-modal-header" {...floating.dragHandleProps}>
          <div className="kh-modal-header-top">
            <div className="kh-modal-breadcrumb">
              <span className="kh-modal-row-title" title={rowTitle}>{rowTitle}</span>
              <span className="kh-modal-sep">›</span>
              <span className="kh-modal-col-name">智能补全空列</span>
            </div>
            <button ref={closeButtonRef} type="button" className="icon-button" title="关闭" onClick={requestClose} disabled={Boolean(savingColumnId)}>
              <X size={18} />
            </button>
          </div>
        </header>

        <div className="kh-modal-body kh-completion-body">
          {suggestions === null && !loadError && (
            <p className="kh-row-context-empty" role="status" aria-live="polite"><Loader2 size={15} className="spin" /> 正在执行表内参考 + 全库推理检索并生成建议…</p>
          )}
          {loadError && (
            <div className="kh-completion-load-error">
              <p className="kh-inline-error" role="alert">{loadError}</p>
              <button type="button" onClick={requestSuggestions}>重试</button>
            </div>
          )}
          {completion && (
            <section className="kh-completion-retrieval" aria-label="补全检索信息">
              <div className="kh-completion-retrieval-meta">
                <span>检索模式：{modeLabel("reasoning")}</span>
                <span>范围：当前笔记本与已挂载参考库</span>
                <span>{completionRetrievalStatusLabel(completion.retrievalStatus)}</span>
              </div>
              {completion.reasoningTrace.length > 0 && (
                <ReasoningTracePanel steps={completion.reasoningTrace} />
              )}
            </section>
          )}
          {suggestions && suggestions.length === 0 && (
            <p className="kh-row-context-empty">暂时没有可用建议，你可以保留空白并稍后手动填写。</p>
          )}
          {suggestions?.map((suggestion) => {
            const column = targetColumns.find((candidate) => candidate.id === suggestion.columnId);
            if (!column) return null;
            const accepted = acceptedColumnIds.has(suggestion.columnId);
            const acceptable = canAcceptCompletionSuggestion(suggestion);
            const saving = savingColumnId === suggestion.columnId;
            const linkedEvidence = completion
              ? completionEvidenceForSuggestion(suggestion, completion.evidence)
              : [];
            return (
              <section key={suggestion.columnId} className="kh-completion-item">
                <div className="kh-completion-item-head">
                  <h4>{column.name}</h4>
                  <span className={`kh-completion-confidence kh-completion-confidence--${suggestion.confidence}`}>
                    {COMPLETION_CONFIDENCE_LABELS[suggestion.confidence]}
                  </span>
                </div>
                {acceptable ? (
                  <div className="kh-completion-suggestion">
                    <KnowhowMarkdown md={suggestion.suggestionMd ?? ""} notebookId={notebookId} apiBase={apiBase} />
                  </div>
                ) : (
                  <p className="kh-completion-abstain">暂不建议填写：{suggestion.abstainReason.trim() || "现有记录不足以可靠判断"}</p>
                )}
                {suggestion.basis.trim() && <p className="kh-completion-basis">依据：{suggestion.basis}</p>}
                <div className="kh-completion-references" aria-label={`${column.name} 的表内参考`}>
                  <span>表内参考</span>
                  {suggestion.basedOnRowIds.length === 0 ? (
                    <span className="kh-completion-no-evidence">未引用表内参考记录。</span>
                  ) : suggestion.basedOnRowIds.map((referenceRowId, index) => (
                      <span key={`${referenceRowId}:${index}`} className="kh-completion-reference">
                        {completionReferenceLabel(referenceRowId, rows, columns)}
                      </span>
                    ))}
                </div>
                <div className="kh-completion-evidence" aria-label={`${column.name} 的知识库证据`}>
                  <h5>知识库证据</h5>
                  {linkedEvidence.length === 0 ? (
                    <p className="kh-completion-no-evidence">此项未引用知识库证据。</p>
                  ) : linkedEvidence.map((item) => (
                    <article key={item.key} className="kh-completion-evidence-card">
                      <div className="kh-completion-evidence-head">
                        <strong>{item.label.trim() || "知识库证据"}</strong>
                        <span>{completionEvidenceTierLabel(item.tier)}</span>
                      </div>
                      {(item.objectType.trim() || item.sourceTitle.trim() || item.locationLabel.trim()) && (
                        <div className="kh-completion-evidence-meta">
                          {item.objectType.trim() && <span>{item.objectType}</span>}
                          {item.sourceTitle.trim() && <span>{item.sourceTitle}</span>}
                          {item.locationLabel.trim() && <span>{item.locationLabel}</span>}
                        </div>
                      )}
                      {item.excerptMd.trim() ? (
                        <div className="kh-completion-evidence-excerpt">
                          <KnowhowMarkdown md={item.excerptMd} notebookId={notebookId} apiBase={apiBase} inert />
                        </div>
                      ) : (
                        <p className="kh-completion-no-evidence">未提供可展示的证据摘录。</p>
                      )}
                    </article>
                  ))}
                </div>
                {saveErrors[suggestion.columnId] && <p className="kh-inline-error" role="alert">{saveErrors[suggestion.columnId]}</p>}
                <div className="kh-completion-item-actions">
                  <button
                    type="button"
                    className="kh-primary-button"
                    onClick={() => void acceptSuggestion(suggestion)}
                    disabled={!acceptable || accepted || Boolean(savingColumnId)}
                    aria-label={`接受 ${column.name} 建议`}
                  >
                    {accepted ? <><Check size={14} /> 已填入</> : saving ? <span role="status" aria-live="polite"><Loader2 size={14} className="spin" /> 保存中…</span> : "接受此项"}
                  </button>
                </div>
              </section>
            );
          })}
        </div>
        <footer className="kh-modal-footer">
          <div className="kh-footer-actions">
            <button type="button" onClick={requestClose} disabled={Boolean(savingColumnId)}>关闭</button>
          </div>
        </footer>
        <span className="kh-modal-resize-handle" aria-hidden="true" {...floating.resizeHandleProps} />
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// KnowhowRowOptimizeModal — 行详情抽屉「优化整行」批量弹窗（Task 9，规格③ +
// 任务简报：非空格子逐格顺序调用、每格接受/跳过、进度与错误逐格显示）。
//
// 堆叠在 KnowhowRowDrawer 之上（复用同一套 kh-modal-* 视觉语言——与格子浮窗
// 同款外壳），抽屉本身留在背后不关闭，方便用户随时看行全貌；本弹窗只在这
// 一行的格子间推进，不涉及切行，交互形态与面向单格的 KnowhowCellEditor 不
// 同，故不复用那个组件，而是自己按 knowhow-optimize-logic.ts 的队列状态机
// 驱动渲染。
//
// 队列的推进方式：每次「当前格」从 waiting 变为其它状态后，若还有下一格且
// 未中止，本地算出下一格状态后立即触发它的 optimizeKnowhowCell 调用——不
// 依赖 useEffect 监听状态变化去自动推进（避免 React 18 StrictMode 开发期
// 双调用导致同一格重复发起请求那一整套额外复杂度），纯靠事件处理函数内部
// 显式串联「计算下一状态 -> setQueue -> 若有下一格则手动触发」。唯一的
// useEffect 只负责"挂载时启动队列首格"这一次性动作，用 startedRef 闩防
// StrictMode 双调用重复触发首格请求。
// ---------------------------------------------------------------------------

interface KnowhowRowOptimizeModalProps {
  notebookId: string;
  apiBase: string;
  tableId: string;
  row: KnowhowRow;
  columns: KnowhowColumn[];
  rowTitle: string;
  /** 接受：真正落库(patchKnowhowCell 或合并格批量走 batchPatchKnowhowCells)
   * +把结果合并回 detail 状态——复用面板自己的 handleCellSave，与格子浮窗
   * 保存走同一条路径，不重复实现。 */
  onAcceptCell: (rowId: string, columnId: string, contentMd: string) => Promise<void>;
  onClose: () => void;
}

function KnowhowRowOptimizeModal({
  notebookId,
  apiBase,
  tableId,
  row,
  columns,
  rowTitle,
  onAcceptCell,
  onClose,
}: KnowhowRowOptimizeModalProps) {
  const [queue, setQueue] = useState<RowOptimizeQueueState>(() => initRowOptimizeQueue(row, columns));
  const [acceptBusy, setAcceptBusy] = useState(false);
  const startedRef = useRef(false);
  const mountedRef = useRef(true);
  const optimizeControllerRef = useRef<AbortController | null>(null);
  const pendingOptimizeAbortRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // 本弹窗没有全屏概念（任务表未列出）——不传 disabled，拖动/resize 恒生效。
  const floating = useFloatingWindow({ storageKey: "knowhow.rowOptimize.window" });

  useEffect(() => {
    if (pendingOptimizeAbortRef.current !== null) {
      clearTimeout(pendingOptimizeAbortRef.current);
      pendingOptimizeAbortRef.current = null;
    }
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      const controller = optimizeControllerRef.current;
      pendingOptimizeAbortRef.current = setTimeout(() => {
        if (optimizeControllerRef.current === controller) {
          controller?.abort();
          optimizeControllerRef.current = null;
        }
        pendingOptimizeAbortRef.current = null;
      }, 0);
    };
  }, []);

  const current = currentRowOptimizeItem(queue);

  async function fireOptimizeFor(item: RowOptimizeItem) {
    optimizeControllerRef.current?.abort();
    const controller = new AbortController();
    optimizeControllerRef.current = controller;
    setQueue((state) => markCurrentInProgress(state));
    try {
      const result = await optimizeKnowhowCell(
        notebookId,
        tableId,
        row.id,
        item.columnId,
        controller.signal,
      );
      if (controller.signal.aborted || !mountedRef.current) return;
      setQueue((state) => applySuggestion(state, result.suggestionMd));
    } catch (err) {
      if (controller.signal.aborted || !mountedRef.current) return;
      setQueue((state) => applyError(state, extractErrorMessage(err, "优化失败，请重试")));
    } finally {
      if (optimizeControllerRef.current === controller) {
        optimizeControllerRef.current = null;
      }
    }
  }

  // 挂载时启动队列首格（若队列非空）。StrictMode 探测的 setup 复用同一请求，
  // 不形成第二次物理模型调用；真实卸载的延后 cleanup 才会取消它。
  useEffect(() => {
    if (startedRef.current) return;
    startedRef.current = true;
    if (current) void fireOptimizeFor(current);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function requestClose() {
    if (acceptBusy) return;
    optimizeControllerRef.current?.abort();
    onClose();
  }

  useEffect(() => {
    function handleKeyDown(event: globalThis.KeyboardEvent) {
      if (event.key === "Escape") requestClose();
    }
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [acceptBusy]);

  function handleBackdropClick(event: ReactMouseEvent<HTMLDivElement>) {
    if (event.currentTarget === event.target) requestClose();
  }

  // 「接受」：begin(标记进行中，复用同一状态展示 PATCH 落库这个短暂过程)
  // -> await onAcceptCell -> 成功则 complete(落地+推进游标)并立即触发下一格，
  // 失败则 fail(出错，游标不动，允许跳过或重试)。begin/complete 都基于同一个
  // 本地 `queue` 快照顺序计算(游标在这期间不会被其它操作改动——acceptBusy
  // 已经把接受按钮和跳过按钮都禁用)，不依赖异步 setQueue 是否已经落地。
  async function handleAccept() {
    if (!current || current.status !== "ready" || acceptBusy) return;
    setAcceptBusy(true);
    const busyState = beginAcceptCurrent(queue);
    setQueue(busyState);
    try {
      await onAcceptCell(row.id, current.columnId, current.suggestionMd ?? "");
      if (!mountedRef.current) return;
      const advanced = completeAcceptCurrent(busyState);
      setQueue(advanced);
      const next = currentRowOptimizeItem(advanced);
      if (next && !advanced.aborted) void fireOptimizeFor(next);
    } catch (err) {
      if (!mountedRef.current) return;
      setQueue((state) => failAcceptCurrent(state, extractErrorMessage(err, "保存失败，请重试")));
    } finally {
      if (mountedRef.current) setAcceptBusy(false);
    }
  }

  function handleSkip() {
    if (!current || acceptBusy) return;
    const advanced = skipCurrent(queue);
    if (advanced === queue) return; // 无操作（当前格不在 ready/error）
    setQueue(advanced);
    const next = currentRowOptimizeItem(advanced);
    if (next && !advanced.aborted) void fireOptimizeFor(next);
  }

  function handleRetry() {
    if (!current || acceptBusy) return;
    const retried = retryCurrent(queue);
    if (retried === queue) return; // 无操作（当前格不在 error）
    setQueue(retried);
    const retryItem = currentRowOptimizeItem(retried);
    if (retryItem) void fireOptimizeFor(retryItem);
  }

  function handleAbort() {
    optimizeControllerRef.current?.abort();
    setQueue((state) => abortQueue(state));
  }

  const finished = isRowOptimizeQueueFinished(queue);
  const progress = rowOptimizeProgress(queue);

  return (
    <div className="kh-modal-overlay" onClick={handleBackdropClick}>
      <div
        ref={floating.cardRef}
        className="kh-modal-card"
        style={floating.style}
        role="dialog"
        aria-modal="true"
        aria-label={`${ROW_OPTIMIZE_BUTTON_LABEL} · ${rowTitle}`}
        onClick={(event) => event.stopPropagation()}
      >
        <header className="kh-modal-header" {...floating.dragHandleProps}>
          <div className="kh-modal-header-top">
            <div className="kh-modal-breadcrumb">
              <span className="kh-modal-row-title" title={rowTitle}>
                {rowTitle}
              </span>
              <span className="kh-modal-sep">›</span>
              <span className="kh-modal-col-name">{ROW_OPTIMIZE_BUTTON_LABEL}</span>
              {queue.items.length > 0 && (
                <span className="knowhow-status-badge">{`${progress.done}/${progress.total}`}</span>
              )}
            </div>
            <div className="kh-modal-header-actions">
              {!finished && !queue.aborted && (
                <button type="button" className="kh-preview-edit-button" onClick={handleAbort}>
                  {ROW_OPTIMIZE_ABORT_LABEL}
                </button>
              )}
              <button
                type="button"
                className="icon-button"
                title="关闭"
                onClick={requestClose}
                disabled={acceptBusy}
              >
                <X size={18} />
              </button>
            </div>
          </div>
        </header>

        <div className="kh-modal-body">
          {queue.items.length === 0 ? (
            <p className="kh-row-context-empty">{ROW_OPTIMIZE_EMPTY_TEXT}</p>
          ) : (
            <>
              <ul className="kh-optimize-queue-list">
                {queue.items.map((item, index) => (
                  <li
                    key={item.columnId}
                    className={`kh-optimize-queue-item${index === queue.cursor ? " kh-optimize-queue-item--current" : ""}`}
                  >
                    <span className="kh-optimize-queue-col">{item.columnName}</span>
                    <span className={`kh-optimize-queue-status kh-optimize-queue-status--${item.status}`}>
                      {ROW_OPTIMIZE_STATUS_LABELS[item.status]}
                    </span>
                  </li>
                ))}
              </ul>

              {current && current.status === "ready" && (
                <div className="kh-optimize-compare">
                  <div className="kh-optimize-pane">
                    <h5>{OPTIMIZE_ORIGINAL_LABEL}</h5>
                    <div className="kh-optimize-pane-body">
                      <KnowhowMarkdown md={current.originalMd} notebookId={notebookId} apiBase={apiBase} />
                    </div>
                  </div>
                  <div className="kh-optimize-pane kh-optimize-pane--suggestion">
                    <h5>{OPTIMIZE_SUGGESTION_LABEL}</h5>
                    <div className="kh-optimize-pane-body">
                      <KnowhowMarkdown md={current.suggestionMd ?? ""} notebookId={notebookId} apiBase={apiBase} />
                    </div>
                  </div>
                </div>
              )}

              {current && current.status === "error" && <p className="kh-inline-error">{current.errorMessage}</p>}

              {finished && <p className="kh-row-context-empty">{ROW_OPTIMIZE_DONE_TEXT}</p>}
            </>
          )}
        </div>

        <footer className="kh-modal-footer">
          {current && current.status === "ready" ? (
            <div className="kh-footer-actions">
              <button type="button" onClick={handleSkip} disabled={acceptBusy}>
                {ROW_OPTIMIZE_SKIP_LABEL}
              </button>
              <button type="button" className="kh-primary-button" onClick={handleAccept} disabled={acceptBusy}>
                <Check size={14} /> {acceptBusy ? "保存中…" : ACCEPT_SUGGESTION_LABEL}
              </button>
            </div>
          ) : current && current.status === "error" ? (
            <div className="kh-footer-actions">
              <button type="button" onClick={handleSkip}>
                {ROW_OPTIMIZE_SKIP_LABEL}
              </button>
              <button type="button" onClick={handleRetry}>
                {ROW_OPTIMIZE_RETRY_LABEL}
              </button>
            </div>
          ) : (
            <div className="kh-footer-actions">
              <button type="button" onClick={requestClose} disabled={acceptBusy}>
                {ROW_OPTIMIZE_CLOSE_LABEL}
              </button>
            </div>
          )}
        </footer>
        <span className="kh-modal-resize-handle" aria-hidden="true" {...floating.resizeHandleProps} />
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// KnowhowReformatBatchModal — 「一键规整整行 / 整表」批量弹窗
// （knowhow-md-normalize Task 9，复用 knowhow-optimize-logic.ts 第 5 节的
// ReformatBatchState；行/表两个入口共用同一个组件，只是 scope/rows 不同）。
//
// 与上面 KnowhowRowOptimizeModal 的关键差异（对应状态机差异，见该 logic
// 文件第 5 节头注释）：这里没有逐格「接受/跳过」——reformat_cell 对全部非空
// 格子自动跑完（idle -> running），跑完后停在 reviewing 展示汇总 + 逐格
// before/after，用户看过之后**一次性**点「确认保存」才会进入 saving 逐格
// 落库（任务硬要求①"Never auto-persist"：状态机里根本没有 running 直通
// saving 的路径，唯一入口是这里 runSave 里调用的 beginReformatBatchSave，
// 而它只会被「确认保存」按钮的 onClick 触发）。开始规整前先展示将处理的
// 格子数（任务硬要求③），单格失败（无论是规整调用还是保存调用）不阻断
// 其余格子、只标记该格并计入 summary（任务硬要求④）。
// ---------------------------------------------------------------------------

interface KnowhowReformatBatchModalProps {
  notebookId: string;
  apiBase: string;
  tableId: string;
  scope: ReformatBatchScope;
  /** 行作用域传 `[row]`；表作用域传 `detail.rows` 全量——**只**用于建批次的展示
   * 条目（initReformatBatch）：行作用域下弹窗只展示被选中行那一行的格子。 */
  rows: KnowhowRow[];
  /** **完整** detail.rows（**两种作用域都传全量**）——保存阶段规划扇出/查陈旧走
   * 这一份（planReformatSaves），必须与 handleCellSave 重算 anchor 组用的行集
   * 严格同源，否则合并共享列的非展示兄弟行会漏查陈旧、被静默覆盖（F1）。 */
  allRows: KnowhowRow[];
  columns: KnowhowColumn[];
  /** 行标题列 id（记录型表为 null）——与 handleCellSave 同源的那份，用于保存前
   * 把「会扇写到同一合并共享组」的候选合并成一次代表保存（F1，见
   * planReformatSaves）。必须与 `allRows` 取自同一份 detail，两者一起决定分组。 */
  anchorColumnId: string | null;
  /** 面包屑首段：行作用域传行标题，表作用域传表标题。 */
  title: string;
  /** 保存：复用面板自己的 handleCellSave——与格子浮窗、「优化整行」保存走
   * 同一条路径（含合并共享格批量写判定），不重复实现。第 4 参 `expectedByRowId`
   * = 本 unit 每个写目标的 originalMd 基线映射（P1-b）：批量保存**总是**传它，触发
   * 服务端事务内比对（陈旧 -> 409）。第 5 参 `targetRowIds` = 本 unit 的写目标行 id
   * 全集（取自建批次冻结快照的 unit.writeTargets，P1「扇出同源」）：批量保存**总是**
   * 传它，令 handleCellSave 照用这份快照写目标、不从实时 detail 重算合并共享组——规划
   * 与扇写因此同源于同一份快照，杜绝 detail 刷新致组分裂时「只写代表行却整组报成功」。
   * handleCellSave 的这两个参都是可选的（手动编辑器省略、走实时分组 + last-write-wins），
   * 故此处签名要求它们、实现放宽它们，两侧兼容。第 6 参 `anchorGuard`（P1 round-4）=
   * 快照 anchor 列 id + rowId -> 建批次那刻该行 anchor 快照值：只在**有 anchor 列**时下发，
   * 令 handleCellSave 的合并共享组扇写额外带上 anchor 基线守卫，后端拦「离组行被冻结扇出
   * 误写」。记录型表（无 anchor 列）传 undefined，故此参可选。 */
  onSaveCell: (
    rowId: string,
    columnId: string,
    contentMd: string,
    expectedByRowId: Map<string, string>,
    targetRowIds: string[],
    anchorGuard?: { anchorColumnId: string; expectedAnchorByRowId: Map<string, string> },
    origin?: string,
  ) => Promise<void>;
  /** F（review）：保存阶段发生过 stale 跳过（preflight 比对 或 409）时回调——请父级重取
   * 整表，把陈旧的 detail 快照（喂给下一次批量的 rows/originalMd 基线之源）换新，否则关掉
   * 重开会用同一陈旧基线复跑、那些格子永远跳过（判定见 reformatBatchSaveNeedsRefresh）。 */
  onStaleReload?: () => Promise<KnowhowTableDetail | null>;
  /** 已保存项交给仍挂载的父级编排：先关批次；若 needsReload 则等待刷新成功，
   * 再按新 detail 重算 representative 并打开既有格子详情。 */
  onOpenSavedCell: (rowId: string, columnId: string, needsReload: boolean) => void;
  onClose: () => void;
}

function KnowhowReformatBatchModal({
  notebookId,
  apiBase,
  tableId,
  scope,
  rows,
  allRows,
  columns,
  anchorColumnId,
  title,
  onSaveCell,
  onStaleReload,
  onOpenSavedCell,
  onClose,
}: KnowhowReformatBatchModalProps) {
  // F2：批量规整排除 anchor 列——传入 anchorColumnId，buildReformatBatchItems 据此跳过
  // 分组键列（详见 knowhow-optimize-logic.ts）。记录型表 anchorColumnId=null 时不排除任何列。
  const [batch, setBatch] = useState<ReformatBatchState>(() => initReformatBatch(scope, rows, columns, anchorColumnId));
  // running/saving 两个自动循环阶段统称"忙"：忙时关闭按钮禁用、「开始规整」/
  // 「确认保存」按钮不可重复点击（镜像 KnowhowRowOptimizeModal 的
  // acceptBusy，只是这里覆盖的是整个批量循环而不是单次「接受」）。
  const [busy, setBusy] = useState(false);
  // 保存阶段的目标格数 = 点「确认保存」那一刻 status==="changed" 的格子数，在
  // runSave 开始时定格一次。saving 进度条的分母/进度都据它算（见下方
  // saveTargetTotal/saveProgressDone），而**不**从 summary.stale_skipped 反推——因为
  // stale_skipped 现在有两个来源（保存阶段的 F2，以及 P1-a 运行阶段的 sourceMd 陈旧），
  // 后者根本没进过保存集，混进保存分母会把它撑大。用「确认时的 changed 数」当分母，
  // 再用 total - 仍在 changed/saving 的数当已完成数，两个来源天然都不掺进来。
  const [saveTargetCount, setSaveTargetCount] = useState(0);
  const [batchView, setBatchView] = useState<
    { kind: "queue" } | { kind: "item"; rowId: string; columnId: string; tab: "diff" | "preview" }
  >({ kind: "queue" });
  // 中止只需要让"驱动循环的 for 语句"停止发起下一次请求——不经由 React
  // state（那样要等一次 re-render 才能读到，循环体里判断的仍是发起本次
  // 请求那一刻的旧值）。真正的 UI 状态翻转（phase -> reviewing、
  // aborted=true）仍然走 setBatch，两者各司其职。
  const mountedRef = useRef(true);
  const runEpochRef = useRef(0);
  const generationControllerRef = useRef<AbortController | null>(null);
  const generationCacheRef = useRef(new Map<string, ReformatGenerationResult>());
  const modalBodyRef = useRef<HTMLDivElement | null>(null);
  const queueScrollTopRef = useRef(0);
  const returnFocusKeyRef = useRef<string | null>(null);
  // F1（P1 回归：延迟重载）：run 阶段陈旧跳过与 save 阶段陈旧跳过是两个独立触发源（前者
  // 在 runBatch 收尾、后者在 runSave 收尾），都经 requestStaleReload **只置此标记**——批量
  // 进行中**绝不**立即重载父表 detail。原因：进行中重载会让 allRows/handleCellSave 读到刷新
  // 后的行，planReformatSaves 可能拿另一客户端的**新值**当 expected_before、让带守卫的写覆盖
  // 掉那次更新（正是守卫要挡的一类）。真正的 reloadTableDetail 延到弹窗**卸载（关闭）**才触发
  // （见下方 mount effect 的 cleanup）；关窗重开自然从刷新后的 detail 重建，而一个进行中的
  // 批次全程用建批次那一刻的不可变快照、内部始终自洽。置真幂等，卸载时恰消费一次。
  const pendingReloadRef = useRef(false);
  // F1（P1 回归：不可变快照）：建批次那一刻定格整表规划所需的 allRows/anchorColumnId。批量
  // 生命周期内**一切**写目标/基线（planReformatSaves）都取自它——父级 detail 因任何原因（含
  // 本批自己延迟触发的刷新、或 handleCellSave 成功后的 setDetail 合并）变化都不泄漏进来，
  // 保证一个进行中的批次始终对着同一份底稿规划。useRef 首帧初值只求值一次、之后 props 变化
  // 不改它（关窗重开是一次全新挂载 -> 从刷新后的 detail 拿到新快照）。
  const snapshotRef = useRef({ allRows, anchorColumnId });
  // 本弹窗没有全屏概念（任务表未列出，同 KnowhowRowOptimizeModal）——不传
  // disabled，拖动/resize 恒生效。
  const floating = useFloatingWindow({ storageKey: "knowhow.reformatBatch.window" });

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      runEpochRef.current += 1;
      generationControllerRef.current?.abort();
      // F1（延迟重载）：弹窗卸载=关闭。批量进行中攒下的「需要重载」标记在此唯一消费——run/save
      // 阶段的 stale 跳过意味着父级 detail 已落后于服务器，关窗时刷新一次整表，下次打开用新鲜
      // 基线复跑、被跳过的格子不再永远跳过。所有关闭路径（X/背景/Esc/父级清状态）都汇于卸载 ->
      // 天然「恰一次」；没跳过时标记为假 -> 不调（无谓 refetch 也省了）。onStaleReload 是稳定
      // useCallback（reloadTableDetail），弹窗生命周期内不变，[] 依赖捕获首帧即对应被编辑的这张表。
      if (pendingReloadRef.current) void onStaleReload?.();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 行标题 map：只有表作用域需要在条目列表里区分"这是哪一行"——行作用域下
  // rows 只有一个元素，行标题已经在 header 面包屑展示过一次，条目列表只需
  // 显示列名即可，不必逐条重复同一个行标题。
  const rowLabelById = useMemo(() => {
    const map = new Map<string, string>();
    for (const row of rows) {
      map.set(row.id, cellSummary(resolveRowTitleText(row, columns), 40) || rowFallbackTitle(row.position));
    }
    return map;
  }, [rows, columns]);

  function itemLabel(item: ReformatBatchItem): string {
    return scope === "table" ? `${rowLabelById.get(item.rowId) ?? ""} · ${item.columnName}` : item.columnName;
  }

  const selectedItem = batchView.kind === "item"
    ? batch.items.find((item) => item.rowId === batchView.rowId && item.columnId === batchView.columnId) ?? null
    : null;
  const selectedDiff = useMemo(
    () => selectedItem?.candidateMd === undefined
      ? null
      : diffKnowhowMarkdown(selectedItem.originalMd, selectedItem.candidateMd),
    [selectedItem?.rowId, selectedItem?.columnId, selectedItem?.originalMd, selectedItem?.candidateMd],
  );

  // F1（P1 回归：延迟重载）：run 阶段收尾（run-stale）与 save 阶段收尾（save-stale）都经这里
  // ——**只**置「需要重载」标记，**不**立即调用 onStaleReload。批量进行中重载父表 = 用他人新值
  // 覆盖的口子（见 pendingReloadRef 注释）。真正的 reloadTableDetail 延到弹窗卸载（关闭）才触发
  // （见 mount effect 的 cleanup）；置真幂等（两阶段都跳过也只置一次），卸载时恰消费一次。
  function requestStaleReload() {
    pendingReloadRef.current = true;
  }

  // 生成阶段只激活不超过模型状态快照许可（且产品上限为 3）的不同 dedupe key。
  // 同 key 由 pool single-flight；只有 fresh success 进入 modal 生命周期缓存并扇出。
  // error/stale leader 只结算自身，下一物理成员仍可成为 leader。
  async function runBatch(retry = false) {
    if (busy) return;
    setBusy(true);
    const epoch = runEpochRef.current + 1;
    runEpochRef.current = epoch;
    generationControllerRef.current?.abort();
    const controller = new AbortController();
    generationControllerRef.current = controller;
    setBatch((state) => retry ? beginReformatBatchRetry(state) : beginReformatBatchRun(state));
    const items = retry
      ? batch.items.filter((item) => item.status === "pending" || item.status === "aborted" || item.status === "reformat_error")
      : batch.items.filter((item) => item.status === "pending");
    let concurrency = resolveReformatGenerationConcurrency(null);
    try {
      concurrency = resolveReformatGenerationConcurrency(await fetchModelServiceStatus());
    } catch {
      // 状态快照只是浏览器发压提示；读取失败固定回退 2，后端调度器仍是容量权威。
    }
    const currentRun = () => mountedRef.current && runEpochRef.current === epoch && !controller.signal.aborted;
    const poolResult = await runReformatGenerationPool({
      items,
      concurrency,
      signal: controller.signal,
      cache: generationCacheRef.current,
      run: (item, signal) => reformatKnowhowCell(notebookId, tableId, item.rowId, item.columnId, signal),
      onRunning: (item) => {
        if (currentRun()) setBatch((state) => markReformatItemRunning(state, item.rowId, item.columnId));
      },
      onFresh: (item, result, cached) => {
        if (!currentRun()) return;
        setBatch((state) => cached
          ? applyReformatCachedResult(state, item.rowId, item.columnId, result)
          : applyReformatResult(state, item.rowId, item.columnId, result));
      },
      onStale: (item) => {
        if (currentRun()) {
          // 一旦观察到 stale 就立刻记账；即使随后用户 abort 其它慢请求、epoch
          // 失效而跳过 pool 收尾，关闭弹窗仍会消费此标记并刷新父表。
          requestStaleReload();
          setBatch((state) =>
            markReformatItemRunStale(state, item.rowId, item.columnId, BATCH_REFORMAT_STALE_SKIP_TEXT),
          );
        }
      },
      onError: (item, error) => {
        if (currentRun()) {
          setBatch((state) => applyReformatError(
            state,
            item.rowId,
            item.columnId,
            extractErrorMessage(error, "规整格式失败，请重试"),
          ));
        }
      },
    });
    if (currentRun()) {
      setBatch((state) => finishReformatBatchRun(state));
      setBusy(false);
      // F1：run 阶段跳过过 stale 格 -> 父级 detail 快照已落后于服务器（这些格子是别人
      // 刚写的新内容），刷新一次整表，让下次批量用新鲜基线复跑、不再永远跳过。经
      // requestStaleReload 与保存阶段共用同一个「恰一次」闸（两阶段都跳过也只刷一次）。
      if (reformatBatchRunNeedsRefresh(poolResult.staleCount)) requestStaleReload();
    }
  }

  // 中止递增 epoch 并 abort 全部在途 fetch；pending/running 同步结算 aborted。
  // 后端端点不是 durable job，UI 只承诺不再等待/接收，不能承诺模型服务端已终止。
  function handleAbort() {
    runEpochRef.current += 1;
    generationControllerRef.current?.abort();
    setBatch((state) => abortReformatBatchRun(state));
    setBusy(false);
  }

  // save 循环：只处理"确认那一刻" status==="changed" 的项（changed=false 的
  // 格子从未落在这个集合里，任务硬要求①）；单格保存失败同样不阻断其余格子
  // （任务硬要求④）。
  //
  // F1（合并共享格去重）：planReformatSaves 把「会经 handleCellSave 扇写到同一
  // 合并共享组」的 changed 条目合并成一个代表保存——N 个兄弟行原本各调一次
  // onSaveCell、每次又扇写整组（N² 次行 upsert、N 次投影 bump），合并后整组只
  // 发一次代表保存，其余成员的终态跟随这次结果。判定复用 handleCellSave 同源的
  // groupRowsByAnchor/isSharedColumn/groupCellWriteTargets（见该 logic 函数），
  // 不另发明等价。units/currentByKey 都在循环前算/取一次并被闭包固定，循环中
  // setDetail 引发的 re-render 不会改动它们。
  //
  // F2（保存前比对，防覆盖他人编辑）：建批次到点「确认保存」之间可能隔了几分钟，
  // 另一标签页/用户可能已改了某格。保存前 refetch 整表一次、把当前落库内容与
  // originalMd 快照逐格比对（isReformatUnitStale），不一致就整组跳过（终态
  // stale_skipped + 友好文案，计入 summary），不拿旧内容算出的候选盲写覆盖那次
  // 更新。后端不改 API，纯客户端「比对-跳过」。
  async function runSave() {
    if (busy) return;
    const changed = batch.items.filter((item) => item.status === "changed");
    // F1：用**完整** detail.rows 规划——与 handleCellSave 重算 anchor 组的行集同源，让合并
    // 共享列 unit 的写目标覆盖 fan-out 会写到的全部兄弟行（含行作用域下不展示的那些），保存前
    // 一并查陈旧、不静默覆盖他人对兄弟行的并发编辑。**取自建批次那一刻的不可变快照**
    // （snapshotRef，非实时 allRows/anchorColumnId props）：P1 回归的根因正是进行中重载后
    // 读到刷新过的行、把他人新值当 expected_before 基线（见 snapshotRef/pendingReloadRef 注释）。
    const units = planReformatSaves(changed, snapshotRef.current.allRows, snapshotRef.current.anchorColumnId);
    setBusy(true);
    setSaveTargetCount(changed.length); // 定格保存分母（见 saveTargetCount 注释）
    setBatch((state) => beginReformatBatchSave(state));

    // 保存前整表 refetch → (行,列)->当前落库内容 映射（一趟往返换掉逐格往返）。
    // refetch 失败则降级为「不做防覆盖检查」（currentByKey=null）：保持既有保存
    // 行为，不因一次网络抖动就整批拒绝保存（防覆盖是尽力而为的安全网，不是
    // 硬不变量——后端本就没有 expected-before 强一致）。
    let currentByKey: Map<string, string> | null = null;
    try {
      const fresh = await fetchKnowhowTable(notebookId, tableId);
      if (!mountedRef.current) return;
      const map = new Map<string, string>();
      for (const row of fresh.rows) {
        for (const [colId, content] of Object.entries(row.cells)) {
          map.set(reformatSaveCellKey(row.id, colId), content);
        }
      }
      currentByKey = map;
    } catch {
      if (!mountedRef.current) return;
      currentByKey = null;
    }

    // F（review）：保存阶段实际发生的 stale 跳过计数（preflight 比对命中 + 409 两个来源）。
    // 循环内局部计数、**不**从 summary.stale_skipped 反推——后者混入了 run 阶段 P1-a 的
    // stale（那批从没进保存集）。收尾据它决定是否请父级刷新整表（见下方与
    // reformatBatchSaveNeedsRefresh）。
    let saveStaleSkipCount = 0;
    for (const unit of units) {
      const rep = unit.representative;
      if (currentByKey && isReformatUnitStale(unit, currentByKey)) {
        // 内容已被他人改动：整组跳过，不发保存请求，标 stale_skipped + 友好文案。
        setBatch((state) =>
          unit.members.reduce(
            (s, m) => markReformatItemStaleSkipped(s, m.rowId, m.columnId, BATCH_REFORMAT_STALE_SKIP_TEXT),
            state,
          ),
        );
        saveStaleSkipCount += 1;
        continue;
      }
      // 把本扇出等价类的全部成员一起标 saving——representative 的一次保存经
      // handleCellSave 扇写到整组，成员物理上都会被写到。
      setBatch((state) => unit.members.reduce((s, m) => markReformatItemSaving(s, m.rowId, m.columnId), state));
      // P1-b：把本 unit 每个写目标的 originalMd 基线（含行作用域下不展示的兄弟行）
      // 下发给 handleCellSave，触发服务端事务内比对——这才是防覆盖他人编辑的**权威**
      // 判定（上面的 preflight refetch 只是省一次注定 409 的往返的廉价前置过滤）。
      const expectedByRowId = new Map(unit.writeTargets.map((t) => [t.rowId, t.originalMd]));
      // P1（扇出同源）：把本 unit 的写目标行 id（取自建批次冻结快照的 unit.writeTargets，与
      // 上面 planReformatSaves 用的 snapshotRef 同源）显式下发给 handleCellSave，令其**照用**、
      // 不从实时 detail 重算合并共享组。若弹窗打开期间 detail 刷新使该组分裂，实时重算只会写
      // 代表行、循环却整组标 saved（漏写报成功）；显式写目标堵住这条缝。基线（expectedBefore）
      // 仍逐行随行，服务端事务内逐行比对，真陈旧则 409 -> 落到既有 stale-skip。
      const targetRowIds = unit.writeTargets.map((t) => t.rowId);
      // P1（round-4）：把本 unit 每个写目标的**快照** anchor 值（unit.writeTargets[i].anchorMd，
      // 与 originalMd 同源于 snapshotRef 冻结快照）连同**快照** anchor 列 id 一起下发——服务端
      // 事务内额外重读每行 anchor 列，任一行 anchor 自建批次以来被移出组就整组 409（落到既有
      // stale-skip 路径）。anchorColumnId 取自 snapshotRef 而非实时 detail，与写目标同一份底稿。
      // P1（round-6）：再叠一道 unit.coversAnchorGroup 门——只有「整组写」（合并共享列扇写 /
      // 单例组）才下发 anchorGuard。多行组里的**非共享列**是有意的子集写（writeTargets ⊊ 组），
      // 若也带守卫，后端「组成员精确相等」会把「组里本就有别的兄弟行」误判成 joiner 漂移、把每次
      // 合法的逐行属性规整都假 409。记录型表 anchorColumnId=null（无兄弟组可离）本就 coversAnchorGroup
      // =false，两条件合一：唯有有 anchor 列且写目标恰为完整组时才带守卫。带上它的（=整组写）会由
      // handleCellSave 路由到 guarded 批量端点，即便只有一个写目标（单例组），令成员校验必跑。
      const anchorColumnId = snapshotRef.current.anchorColumnId;
      const anchorGuard = anchorColumnId && unit.coversAnchorGroup
        ? {
            anchorColumnId,
            expectedAnchorByRowId: new Map(unit.writeTargets.map((t) => [t.rowId, t.anchorMd])),
          }
        : undefined;
      try {
        // origin（codex 第 6 轮 P2）：批量规整是**自动保存** LLM 规整结果的循环，
        // 每格都要在变更流水里归因为 "llm_reformat"，否则落后端默认 "user"、
        // 「格式规整」徽章对这条主流程永不出现（后端 patch_knowhow_cells_batch
        // 正是为此保留 origin 白名单校验）。单格规整是用户复核后手动保存、按
        // "user" 处理，与此无关。
        await onSaveCell(
          rep.rowId,
          rep.columnId,
          rep.candidateMd ?? "",
          expectedByRowId,
          targetRowIds,
          anchorGuard,
          "llm_reformat",
        );
        if (!mountedRef.current) return;
        setBatch((state) => unit.members.reduce((s, m) => applyReformatSaveSuccess(s, m.rowId, m.columnId), state));
      } catch (err) {
        if (!mountedRef.current) return;
        if (httpErrorStatus(err) === 409) {
          // 服务端判定他人已改（内容已被并发编辑）：整组走 stale_skipped 而非保存
          // 失败——这不是故障，是主动放弃这次盲写以保护他人的新编辑（与 F2 同终态、
          // 同友好文案）。单 unit 跳过不阻断其余 unit（循环继续）。
          setBatch((state) =>
            unit.members.reduce(
              (s, m) => markReformatItemStaleSkipped(s, m.rowId, m.columnId, BATCH_REFORMAT_STALE_SKIP_TEXT),
              state,
            ),
          );
          saveStaleSkipCount += 1;
        } else {
          const message = extractErrorMessage(err, "保存失败，请重试");
          setBatch((state) => unit.members.reduce((s, m) => applyReformatSaveError(s, m.rowId, m.columnId, message), state));
        }
      }
    }
    if (mountedRef.current) {
      setBatch((state) => finishReformatBatchSave(state));
      setBusy(false);
      // F（review）：保存阶段跳过过 stale 格 -> 父级 detail 快照已落后于服务器（被跳过的
      // 格子是别人刚写的新内容），刷新一次整表，让下次批量用新鲜基线复跑、不再永远跳过。
      // F1：经 requestStaleReload 汇流（与 run 阶段共用「恰一次」闸，两阶段都跳过只刷一次）。
      if (reformatBatchSaveNeedsRefresh(saveStaleSkipCount)) requestStaleReload();
    }
  }

  function requestClose() {
    if (busy) return;
    onClose();
  }

  function openItemView(item: ReformatBatchItem) {
    queueScrollTopRef.current = modalBodyRef.current?.scrollTop ?? 0;
    returnFocusKeyRef.current = `${item.rowId}:${item.columnId}`;
    setBatchView({ kind: "item", rowId: item.rowId, columnId: item.columnId, tab: "diff" });
  }

  function returnToQueue() {
    setBatchView({ kind: "queue" });
    requestAnimationFrame(() => {
      if (modalBodyRef.current) modalBodyRef.current.scrollTop = queueScrollTopRef.current;
      const key = returnFocusKeyRef.current;
      const button = key
        ? modalBodyRef.current?.querySelector<HTMLButtonElement>(`[data-reformat-view="${CSS.escape(key)}"]`)
        : null;
      button?.focus();
    });
  }

  function openSavedCell(item: ReformatBatchItem) {
    if (item.status !== "saved" || busy) return;
    const needsReload = pendingReloadRef.current;
    // 交给父级显式消费，避免卸载 cleanup 再发第二次 reload；父级在批量子组件
    // 卸载后仍存活，可安全 await 刷新并基于新 detail 重算 representative。
    pendingReloadRef.current = false;
    onOpenSavedCell(item.rowId, item.columnId, needsReload);
  }

  useEffect(() => {
    function handleKeyDown(event: globalThis.KeyboardEvent) {
      if (event.key === "Escape") requestClose();
    }
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [busy]);

  function handleBackdropClick(event: ReactMouseEvent<HTMLDivElement>) {
    if (event.currentTarget === event.target) requestClose();
  }

  const summary = reformatBatchSummary(batch);
  const buttonLabel = scope === "row" ? BATCH_REFORMAT_ROW_BUTTON_LABEL : BATCH_REFORMAT_TABLE_BUTTON_LABEL;
  const emptyText = scope === "row" ? BATCH_REFORMAT_EMPTY_TEXT_ROW : BATCH_REFORMAT_EMPTY_TEXT_TABLE;
  const isEmpty = batch.items.length === 0;
  // run/save 两个阶段各自的"跑到第几个了"——两个分母不同（run 是全部
  // items；save 只数 status 曾经是/正是 changed 那一批），分别在对应阶段
  // 展示，不用同一个笼统的 done/total（见下方 header 徽标）。
  // run 阶段可达的终态含 stale_skipped（P1-a：sourceMd 陈旧的格子在运行阶段就落
  // 终态）——不计进去 run 徽标会卡在 <100% 的假未完成。此徽标只在 phase==="running"
  // 显示，那时 stale_skipped 全部来自运行阶段（保存还没开始），故直接并入即准确。
  const runProgressDone =
    summary.changed + summary.unchanged + summary.reformat_error + summary.stale_skipped + summary.aborted;
  // save 阶段分母 = 点「确认保存」那一刻的 changed 数（saveTargetCount，定格一次）；
  // 已完成 = 分母 - 仍在 changed/saving 的数。这样天然把「运行阶段就 stale_skipped
  // 的格子」排除在保存进度之外（它们从没进过保存集），不必去区分两类 stale_skipped。
  const saveTargetTotal = saveTargetCount;
  const saveProgressDone = saveTargetCount - summary.changed - summary.saving;

  // 条目列表：run/reviewing/saving/done 四个阶段共用同一套渲染（每个阶段
  // 里各项的 status 本身已经足以说明当前处境，不需要为每个阶段各写一份
  // 列表 JSX）。
  const queueList = (
    <ul className="kh-optimize-queue-list">
      {batch.items.map((item) => {
        const canView = item.candidateMd !== undefined
          && ["changed", "saving", "saved", "save_error", "stale_skipped"].includes(item.status);
        return (
        <li key={`${item.rowId}:${item.columnId}`} className="kh-optimize-queue-item">
          <span className="kh-optimize-queue-col">{itemLabel(item)}</span>
          <span className="kh-reformat-queue-actions">
            <span className={`kh-optimize-queue-status kh-optimize-queue-status--${item.status}`}>
              {BATCH_REFORMAT_STATUS_LABELS[item.status]}
            </span>
            {canView && (
              <button
                type="button"
                className="kh-reformat-link-button"
                data-reformat-view={`${item.rowId}:${item.columnId}`}
                onClick={() => openItemView(item)}
              >
                查看改动
              </button>
            )}
            {item.status === "saved" && (
              <button type="button" className="kh-reformat-link-button" onClick={() => openSavedCell(item)}>
                打开格子
              </button>
            )}
          </span>
        </li>
      )})}
    </ul>
  );

  function renderDiffLineText(line: KnowhowDiffLine) {
    if (!line.tokens) return line.text || " ";
    return line.tokens.map((token, index) => (
      <span
        key={`${index}:${token.text}`}
        className={`${token.changed ? "kh-md-diff-token--changed" : ""}${token.whitespace ? " kh-md-diff-token--whitespace" : ""}${token.text.includes("\t") ? " kh-md-diff-token--tab" : ""}`}
      >
        {token.text}
      </span>
    ));
  }

  return (
    <div className="kh-modal-overlay" onClick={handleBackdropClick}>
      <div
        ref={floating.cardRef}
        className="kh-modal-card"
        style={floating.style}
        role="dialog"
        aria-modal="true"
        aria-label={`${buttonLabel} · ${title}`}
        onClick={(event) => event.stopPropagation()}
      >
        <header className="kh-modal-header" {...floating.dragHandleProps}>
          <div className="kh-modal-header-top">
            <div className="kh-modal-breadcrumb">
              <span className="kh-modal-row-title" title={title}>
                {title}
              </span>
              <span className="kh-modal-sep">›</span>
              <span className="kh-modal-col-name">{buttonLabel}</span>
              {selectedItem && (
                <>
                  <span className="kh-modal-sep">›</span>
                  <span className="kh-modal-col-name">{itemLabel(selectedItem)}</span>
                </>
              )}
              {!isEmpty && batch.phase === "running" && (
                <span className="knowhow-status-badge">{`${runProgressDone}/${summary.total}`}</span>
              )}
              {!isEmpty && batch.phase === "saving" && (
                <span className="knowhow-status-badge">{`${saveProgressDone}/${saveTargetTotal}`}</span>
              )}
            </div>
            <div className="kh-modal-header-actions">
              {batchView.kind === "item" && (
                <button type="button" className="kh-preview-edit-button" onClick={returnToQueue}>
                  <ChevronLeft size={14} /> 返回批量结果
                </button>
              )}
              {batch.phase === "running" && (
                <button type="button" className="kh-preview-edit-button" onClick={handleAbort}>
                  {BATCH_REFORMAT_ABORT_LABEL}
                </button>
              )}
              <button type="button" className="icon-button" title="关闭" onClick={requestClose} disabled={busy}>
                <X size={18} />
              </button>
            </div>
          </div>
        </header>

        <div ref={modalBodyRef} className="kh-modal-body">
          {batchView.kind === "item" && selectedItem && selectedDiff ? (
            <div className="kh-reformat-item-detail">
              <div className="kh-reformat-item-toolbar">
                <strong>{itemLabel(selectedItem)}</strong>
                <span className="kh-reformat-source-tag">{reformatSourceLabel(selectedItem.source ?? "")}</span>
                <div className="kh-view-switch" role="tablist" aria-label="改动视图">
                  <button
                    type="button"
                    role="tab"
                    aria-selected={batchView.tab === "diff"}
                    className={`kh-view-switch-button${batchView.tab === "diff" ? " kh-view-switch-button--active" : ""}`}
                    onClick={() => setBatchView({ ...batchView, tab: "diff" })}
                  >
                    Markdown 改动
                  </button>
                  <button
                    type="button"
                    role="tab"
                    aria-selected={batchView.tab === "preview"}
                    className={`kh-view-switch-button${batchView.tab === "preview" ? " kh-view-switch-button--active" : ""}`}
                    onClick={() => setBatchView({ ...batchView, tab: "preview" })}
                  >
                    渲染预览
                  </button>
                </div>
              </div>
              {batchView.tab === "diff" ? (
                <div className="kh-md-diff" aria-label="Markdown 原文改动">
                  {selectedDiff.degraded && (
                    <p className="kh-md-diff-notice">内容较长，已切换为有界的粗粒度增删摘要；可用“渲染预览”查看完整前后内容。</p>
                  )}
                  <div className="kh-md-diff-lines">
                    {selectedDiff.lines.map((line, index) => (
                      <div key={`${index}:${line.kind}`} className={`kh-md-diff-line kh-md-diff-line--${line.kind}`}>
                        <span className="kh-md-diff-marker" aria-hidden="true">
                          {line.kind === "delete" ? "−" : line.kind === "add" ? "+" : " "}
                        </span>
                        <code>{renderDiffLineText(line)}{line.truncated ? " …" : ""}</code>
                      </div>
                    ))}
                  </div>
                </div>
              ) : (
                <div className="kh-optimize-compare">
                  <div className="kh-optimize-pane">
                    <h5>{OPTIMIZE_ORIGINAL_LABEL}</h5>
                    <div className="kh-optimize-pane-body">
                      <KnowhowMarkdown md={selectedItem.originalMd} notebookId={notebookId} apiBase={apiBase} />
                    </div>
                  </div>
                  <div className="kh-optimize-pane kh-optimize-pane--reformat-suggestion">
                    <h5>{REFORMAT_SUGGESTION_LABEL}</h5>
                    <div className="kh-optimize-pane-body">
                      <KnowhowMarkdown md={selectedItem.candidateMd ?? ""} notebookId={notebookId} apiBase={apiBase} />
                    </div>
                  </div>
                </div>
              )}
            </div>
          ) : isEmpty ? (
            <p className="kh-row-context-empty">{emptyText}</p>
          ) : (
            <>
              {batch.phase === "idle" && (
                <p className="kh-row-context-empty">{batchReformatScaleText(batch.items.length)}</p>
              )}
              {batch.phase === "reviewing" && (
                <p className="kh-row-context-empty">
                  {/* F4（review）：明细 vs「无需改动」的判定收进 reformatReviewingSummaryText
                      单一真源——旧内联三元漏了 reformat_error，整批全失败时会粉饰成「无需改动」。
                      P2-g：中止在飞项、P1-a：运行阶段 stale_skipped 都并入明细（不当失败、不被
                      NO_CHANGES 吞掉），逐格明细里另有友好文案说明。 */}
                  {reformatReviewingSummaryText(summary)}
                </p>
              )}
              {batch.phase === "done" && (
                <p className="kh-row-context-empty">
                  {BATCH_REFORMAT_DONE_TEXT}
                  {summary.saved > 0 ? ` ${summary.saved} 个${BATCH_REFORMAT_STATUS_LABELS.saved}` : ""}
                  {summary.save_error > 0 ? `，${summary.save_error} 个${BATCH_REFORMAT_STATUS_LABELS.save_error}` : ""}
                  {/* F2：被判「内容已变」跳过的格子也如实计入收尾摘要（不当失败、
                      不被吞），逐格明细里另有友好文案说明。 */}
                  {summary.stale_skipped > 0
                    ? `，${summary.stale_skipped} 个${BATCH_REFORMAT_STATUS_LABELS.stale_skipped}`
                    : ""}
                </p>
              )}

              {queueList}

              {/* 规整/保存失败的具体原因——中文错误文案，逐格列出（任务硬
                  要求④"surface the count"不只是数字，出错的格子也要能看到
                  为什么）。 */}
              {batch.items
                .filter((item) => item.errorMessage)
                .map((item) => (
                  <p key={`err:${item.rowId}:${item.columnId}`} className="kh-inline-error">
                    {itemLabel(item)}：{item.errorMessage}
                  </p>
                ))}

            </>
          )}
        </div>

        <footer className="kh-modal-footer">
          {batchView.kind === "item" ? (
            <div className="kh-footer-actions">
              <button type="button" onClick={returnToQueue}>返回批量结果</button>
              {selectedItem?.status === "saved" && (
                <button type="button" className="kh-primary-button" onClick={() => openSavedCell(selectedItem)}>
                  打开格子
                </button>
              )}
            </div>
          ) : isEmpty ? (
            <div className="kh-footer-actions">
              <button type="button" onClick={requestClose}>
                {BATCH_REFORMAT_CLOSE_LABEL}
              </button>
            </div>
          ) : batch.phase === "idle" ? (
            <div className="kh-footer-actions">
              <button type="button" onClick={requestClose} disabled={busy}>
                {BATCH_REFORMAT_CLOSE_LABEL}
              </button>
              <button type="button" className="kh-primary-button" onClick={() => runBatch()} disabled={busy}>
                {BATCH_REFORMAT_START_LABEL}
              </button>
            </div>
          ) : batch.phase === "reviewing" ? (
            <div className="kh-footer-actions">
              {/* disabled={busy}：中止后短暂仍 busy（等 runBatch 收尾，见
                  handleAbort 注释）——按钮在那个窗口里如实显示"暂不可点"，
                  而不是看着能点、点了却被 requestClose 内部悄悄吞掉。文案
                  按有没有 changed 项区分：没有任何改动时"放弃改动"用词不对
                  （根本没有改动可放弃），改用平实的"关闭"。 */}
              <button type="button" onClick={requestClose} disabled={busy}>
                {summary.changed > 0 ? BATCH_REFORMAT_DISCARD_LABEL : BATCH_REFORMAT_CLOSE_LABEL}
              </button>
              {(summary.reformat_error > 0 || summary.aborted > 0 || summary.pending > 0) && (
                <button type="button" onClick={() => runBatch(true)} disabled={busy}>
                  {BATCH_REFORMAT_RETRY_LABEL}
                </button>
              )}
              {summary.changed > 0 && (
                <button type="button" className="kh-primary-button" onClick={runSave} disabled={busy}>
                  <Check size={14} /> {BATCH_REFORMAT_CONFIRM_LABEL}
                </button>
              )}
            </div>
          ) : batch.phase === "done" ? (
            <div className="kh-footer-actions">
              <button type="button" onClick={requestClose}>
                {BATCH_REFORMAT_CLOSE_LABEL}
              </button>
            </div>
          ) : null}
        </footer>
        <span className="kh-modal-resize-handle" aria-hidden="true" {...floating.resizeHandleProps} />
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 小组件：角色徽章 / 投影状态徽章
// ---------------------------------------------------------------------------

function RoleBadge({ role }: { role: Role }) {
  // `attribute` 是列的默认类型，标签「普通」对用户零信息量；渲染成灰底灰字的
  // 小胶囊反而是视觉噪音（用户报告为「空胶囊 bug」）。只对具备独立语义的
  // anchor(行标题)/procedure(方法步骤)/entity(工具/事物) 三值显示徽章。
  if (role === "attribute") return null;
  return <span className={`knowhow-role-badge knowhow-role-badge--${role}`}>{ROLE_LABELS[role]}</span>;
}

// 表头列名 inline 改名（canEdit=true 时启用）：双击列名切编辑框，Enter/失焦
// 保存（改动才落库，同名不落），Esc 取消。落库经父组件 onRename 走既有
// patchKnowhowColumn；本组件只负责 UX 状态机与键盘/焦点管理。
function ColumnNameCell({
  column,
  canEdit,
  onRename,
}: {
  column: KnowhowColumn;
  canEdit: boolean;
  onRename: (name: string) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(column.name);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (editing) {
      inputRef.current?.focus();
      inputRef.current?.select();
    }
  }, [editing]);

  // 外部（其他会话/管理面板/reproject 后）改动了列名：只要不在本地编辑中就
  // 跟着刷新草稿——避免旧草稿覆盖新值。
  useEffect(() => {
    if (!editing) setDraft(column.name);
  }, [column.name, editing]);

  function commit() {
    const trimmed = draft.trim();
    setEditing(false);
    if (!trimmed || trimmed === column.name) {
      setDraft(column.name);
      return;
    }
    onRename(trimmed);
  }

  if (!canEdit) {
    return (
      <span className="knowhow-col-name" title={column.name}>
        {column.name}
      </span>
    );
  }

  if (editing) {
    return (
      <input
        ref={inputRef}
        className="knowhow-col-name-input"
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            commit();
          } else if (e.key === "Escape") {
            e.preventDefault();
            setDraft(column.name);
            setEditing(false);
          }
        }}
      />
    );
  }

  return (
    <span
      className="knowhow-col-name knowhow-col-name--editable"
      title={`${column.name}（双击改名）`}
      onDoubleClick={() => setEditing(true)}
    >
      {column.name}
    </span>
  );
}

function ProjectionStatusBadge({
  status,
  canRetry,
  onRetry,
  retrying,
}: {
  status: ProjectionStatus;
  /** 「重试」按 canEdit 门控：重试=触发整表重投影，是写操作，只读成员只看
   * 状态文案不见按钮。 */
  canRetry: boolean;
  onRetry: () => void;
  retrying: boolean;
}) {
  const tone = PROJECTION_STATUS_TONE[status];
  const retryable = isRetryableProjectionStatus(status) && canRetry;
  return (
    <span className={`knowhow-status-badge knowhow-status-badge--${tone}`}>
      {retryable && retrying ? "重新同步中…" : PROJECTION_STATUS_LABELS[status]}
      {retryable && (
        <button
          type="button"
          className="knowhow-status-retry"
          onClick={onRetry}
          disabled={retrying}
          title="重新同步整张表（后台执行）"
        >
          <RefreshCw size={12} className={retrying ? "knowhow-spin" : undefined} />
          {!retrying && "重试"}
        </button>
      )}
    </span>
  );
}

// KnowhowMarkdown（复用 answer-markdown.tsx 的 GFM+KaTeX 渲染管线，供本文件
// 的行详情抽屉 与 knowhow-cell-editor.tsx 的格子浮窗预览/编辑分栏共用）随格子
// 浮窗一起挪到了 knowhow-cell-editor.tsx——见该文件头注释；本文件已在顶部
// import 使用，不再在此重复定义。
