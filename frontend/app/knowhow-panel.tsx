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
 * 权限（PR-2+3 Task 5）：`canEdit`（= notebook 写权限，page.tsx 传
 * `!isReader`）统一门控**全部写入口**——新建表 / 导入表格 / 添加行 / 管理 /
 * 重建投影 / 删除表 / 失败行「重试」。只读成员（canEdit=false）看到纯浏览
 * 视图，一个写按钮都不出现（规格⑦「只读成员可看」）。
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

import { useCallback, useEffect, useMemo, useRef, useState, type MouseEvent as ReactMouseEvent } from "react";
import {
  Check,
  ChevronLeft,
  Download,
  Edit3,
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
import { authHeaders } from "./auth.ts";
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
  fetchKnowhowRowCodeByColumn,
  patchKnowhowColumn,
  type KnowhowTableSummary,
  type KnowhowTableDetail,
  type KnowhowRow,
  type KnowhowColumn,
  type KnowhowCellCode,
  type Role,
  type ProjectionStatus,
} from "./knowhow-model.ts";
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
import { rowFallbackTitle } from "./knowhow-cell-editor-logic.ts";
import { KnowhowImportWizard, KnowhowAppendWizard } from "./knowhow-import.tsx";
import { KnowhowCreateWizard, KnowhowManageModal } from "./knowhow-manage.tsx";
import { KnowhowMarkdown, KnowhowCellPreview, KnowhowCellEditor } from "./knowhow-cell-editor.tsx";
import { KnowhowCodeChip, KnowhowCodeModal } from "./knowhow-code.tsx";
import { KnowhowMatrixDrawer } from "./knowhow-matrix-drawer.tsx";
import { resolveCellCodeView, CODE_STATUS_LOAD_ERROR } from "./knowhow-code-logic.ts";
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
} from "./knowhow-optimize-logic.ts";

// ---------------------------------------------------------------------------
// KnowhowPanel — 顶层：全屏 dialog 外壳 + 三层状态机
// ---------------------------------------------------------------------------

export interface KnowhowPanelProps {
  notebookId: string;
  apiBase: string;
  /** notebook 写权限（page.tsx 传 `!isReader`）：false=只读成员，隐藏全部
   * 写入口（新建/导入/添加行/管理/重建投影/删除表/失败重试）。 */
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
}

export function KnowhowPanel({
  notebookId, apiBase, canEdit, onClose, initialTableId, initialRowId,
}: KnowhowPanelProps) {
  const [tables, setTables] = useState<KnowhowTableSummary[] | null>(null);
  const [tablesLoading, setTablesLoading] = useState(false);
  const [tablesError, setTablesError] = useState<string | null>(null);

  // modal 的显隐：面板自持状态，不经 page.tsx 转发——page.tsx 挂载
  // <KnowhowPanel> 时只传 notebookId/apiBase/canEdit/onClose 四个 prop，
  // 导入/新建/管理/追加入口完全由本文件内部驱动。
  const [importOpen, setImportOpen] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [manageOpen, setManageOpen] = useState(false);
  // Task 9：追加导入向导显隐 + 模板下载进行中标记（工具栏「下载模板」按钮
  // 自身的 loading 态，不走 actionError/detailError 那一套整表级错误）。
  const [appendOpen, setAppendOpen] = useState(false);
  const [templateDownloading, setTemplateDownloading] = useState(false);
  // Task 9：行详情抽屉「优化整行」批量弹窗——null=未打开，否则是正在批量
  // 优化的行 id（与 cellModal 平级的另一个顶层 modal 状态，堆叠在抽屉之上，
  // 镜像 cellModal 自己的挂载方式）。
  const [optimizeRowId, setOptimizeRowId] = useState<string | null>(null);

  const [selectedTableId, setSelectedTableId] = useState<string | null>(null);
  const [detail, setDetail] = useState<KnowhowTableDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
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

  // 格子浮窗顶层状态机（Task 7）：null=未打开；mode="preview"=已填格子的只读
  // 预览态（右上「编辑」切到 edit）；mode="edit"=编辑态。行标题列格子（网格
  // 首列）仅在「已填」时不走这里——那时仍走既有的 onOpenRow 打开整行抽屉，
  // 与规格⑤「点行首/概念列打开」行详情抽屉的既有约定一致；该格为空时和其余
  // 空格子一样直接进本状态机的编辑态（规格②路A「空格子直接进编辑态」），见
  // 下方 KnowhowTableGrid 的 `opensRowDrawer = index === 0 && filled` 判定。
  const [cellModal, setCellModal] = useState<{ rowId: string; columnId: string; mode: "preview" | "edit" } | null>(
    null,
  );

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
    (tableId: string) => {
      const requestId = ++detailRequestRef.current;
      setDetailLoading(true);
      setDetailError(null);
      fetchKnowhowTable(notebookId, tableId)
        .then((data) => {
          if (detailRequestRef.current === requestId) setDetail(data);
        })
        .catch(() => {
          if (detailRequestRef.current === requestId) setDetailError("加载表格详情失败，请重试");
        })
        .finally(() => {
          if (detailRequestRef.current === requestId) setDetailLoading(false);
        });
    },
    [notebookId],
  );

  useEffect(() => {
    if (selectedTableId) loadDetail(selectedTableId);
    return () => {
      detailRequestRef.current += 1; // 切表/回列表/卸载：作废 in-flight 的旧表详情
    };
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
    setCreateOpen(false);
    setAppendOpen(false);
    setOptimizeRowId(null);
    setCodeModal(null);
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
    setAppendOpen(false);
    setOptimizeRowId(null);
    setCodeModal(null);
  }

  function backToList() {
    setSelectedTableId(null);
    setDetail(null);
    setDetailError(null);
    setActionError(null);
    setQuery("");
    setOpenRowId(null);
    setCellModal(null);
    setOpenConceptValue(null);
    setHighlightRowId(null); // Task 11：与 openConceptValue 并列一并清空
    setConfirmDelete(false);
    setManageOpen(false);
    setAppendOpen(false);
    setOptimizeRowId(null);
    setCodeModal(null);
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
      setActionError("重新投影失败，请重试");
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
      const res = await fetch(knowhowTemplateUrl(notebookId, selectedTableId), { headers: authHeaders() });
      if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
      const blob = await res.blob();
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
  async function handleCellSave(rowId: string, columnId: string, contentMd: string) {
    if (!selectedTableId || !detail) return;
    const group = detail.anchorColumnId
      ? groupRowsByAnchor(detail.rows, detail.anchorColumnId).find((g) => g.rows.some((r) => r.id === rowId))
      : null;
    const targets = group && isSharedColumn(group, columnId) ? groupCellWriteTargets(group, columnId) : [rowId];
    const results = targets.length > 1
      ? await batchPatchKnowhowCells(notebookId, selectedTableId, { columnId, rowIds: targets, contentMd })
      : [await patchKnowhowCell(notebookId, selectedTableId, rowId, columnId, contentMd)];
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

      <div className="knowhow-view-body">
        {selectedTableId === null ? (
          <KnowhowTableList
            tables={tables}
            loading={tablesLoading}
            error={tablesError}
            canEdit={canEdit}
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
            onAddRow={addRow}
            addingRow={addingRow}
            onAddConcept={addConcept}
            onDownloadTemplate={downloadTemplate}
            templateDownloading={templateDownloading}
            onAppendClick={() => setAppendOpen(true)}
            onRenameColumn={handleRenameColumn}
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
          cellModalOpen={cellModal !== null || optimizeRowId !== null || codeModal !== null}
          onOptimizeRow={() => setOptimizeRowId(openRow.id)}
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

      <style jsx global>{`
        .knowhow-view {
          position: fixed;
          inset: 0;
          z-index: 50;
          background: var(--panel);
          color: var(--ink);
          display: flex;
          flex-direction: column;
        }

        .knowhow-view-header {
          display: flex;
          justify-content: space-between;
          align-items: flex-start;
          gap: 18px;
          padding: 16px 20px;
          border-bottom: 1px solid var(--line);
        }

        .knowhow-view-header h2 {
          margin: 0;
        }

        .knowhow-view-header p {
          margin: 4px 0 0;
          color: var(--muted);
          font-size: 13px;
          max-width: 82ch;
        }

        .knowhow-view-body {
          flex: 1 1 auto;
          min-height: 0;
          overflow-y: auto;
          padding: 20px;
          display: flex;
          flex-direction: column;
          gap: 16px;
        }

        .knowhow-toolbar {
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 12px;
        }

        .knowhow-toolbar-actions {
          display: flex;
          align-items: center;
          gap: 8px;
        }

        .knowhow-import-button {
          display: inline-flex;
          align-items: center;
          gap: 6px;
        }

        .knowhow-loading,
        .knowhow-inline-error {
          padding: 40px 20px;
          text-align: center;
          color: var(--muted);
        }

        .knowhow-inline-error button {
          margin-left: 8px;
        }

        .knowhow-empty {
          min-height: 320px;
          display: grid;
          place-items: center;
          align-content: center;
          gap: 10px;
          text-align: center;
          color: var(--muted);
          padding: 18px;
        }

        .knowhow-empty-icon {
          font-size: 42px;
        }

        .knowhow-empty strong {
          color: var(--ink);
          font-size: 16px;
        }

        .knowhow-empty p {
          margin: 0;
          line-height: 1.5;
        }

        .knowhow-table-cards {
          display: grid;
          grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
          gap: 12px;
        }

        .knowhow-table-card {
          display: grid;
          gap: 6px;
          text-align: left;
          border: 1px solid var(--line);
          border-radius: 12px;
          padding: 16px;
          background: #fff;
          min-width: 0;
        }

        .knowhow-table-card:hover {
          border-color: var(--blue);
          box-shadow: var(--shadow);
        }

        .knowhow-table-card strong {
          font-size: 15px;
          color: var(--ink);
          overflow: hidden;
          text-overflow: ellipsis;
          white-space: nowrap;
        }

        .knowhow-table-card p {
          margin: 0;
          font-size: 13px;
          color: var(--muted);
          display: -webkit-box;
          -webkit-line-clamp: 2;
          -webkit-box-orient: vertical;
          overflow: hidden;
        }

        .knowhow-table-card-meta {
          font-size: 12px;
          color: var(--muted);
        }

        .knowhow-grid-view {
          display: flex;
          flex-direction: column;
          gap: 14px;
          min-height: 0;
          flex: 1 1 auto;
        }

        .knowhow-grid-toolbar {
          display: flex;
          align-items: flex-start;
          gap: 16px;
        }

        .knowhow-grid-title {
          flex: 1 1 auto;
          min-width: 0;
        }

        .knowhow-grid-title h3 {
          margin: 0;
          font-size: 18px;
          overflow: hidden;
          text-overflow: ellipsis;
          white-space: nowrap;
        }

        .knowhow-grid-title p {
          margin: 4px 0 0;
          font-size: 13px;
          color: var(--muted);
        }

        .knowhow-grid-toolbar-actions {
          flex: 0 0 auto;
          display: flex;
          align-items: center;
          gap: 8px;
        }

        .knowhow-reproject-button {
          display: inline-flex;
          align-items: center;
          gap: 6px;
          white-space: nowrap;
        }

        .knowhow-reproject-button:disabled {
          opacity: 0.6;
          cursor: not-allowed;
        }

        .knowhow-confirm {
          display: inline-flex;
          align-items: center;
          gap: 8px;
          padding: 6px 6px 6px 12px;
          border: 1px solid #f0c0c0;
          border-radius: 999px;
          background: #fef2f2;
          font-size: 13px;
          color: #b91c1c;
          white-space: nowrap;
        }

        .knowhow-confirm-yes,
        .knowhow-confirm-no {
          border: none;
          border-radius: 999px;
          padding: 5px 12px;
          font-size: 12px;
          font-weight: 600;
          cursor: pointer;
        }

        .knowhow-confirm-yes {
          background: var(--red);
          color: #fff;
        }

        .knowhow-confirm-yes:disabled {
          opacity: 0.6;
          cursor: not-allowed;
        }

        .knowhow-confirm-no {
          background: #fff;
          color: var(--muted);
          border: 1px solid var(--line);
        }

        .knowhow-action-error {
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 12px;
          padding: 8px 14px;
          border: 1px solid #f0c0c0;
          border-radius: 8px;
          background: #fef2f2;
          color: #b91c1c;
          font-size: 13px;
        }

        .knowhow-action-error button {
          border: 0;
          background: transparent;
          color: inherit;
          font-size: 16px;
          line-height: 1;
          cursor: pointer;
        }

        /* Task 12（引用跳转）：陈旧引用提示——信息性而非报错，用中性蓝而非
           .knowhow-action-error 的红，避免"引用的行已不在了"这种平常情况看
           起来像一次操作失败。 */
        .knowhow-jump-notice {
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 12px;
          margin: 12px 20px 0;
          padding: 8px 14px;
          border: 1px solid #bfdbfe;
          border-radius: 8px;
          background: #eff6ff;
          color: #1d4ed8;
          font-size: 13px;
        }

        .knowhow-jump-notice button {
          border: 0;
          background: transparent;
          color: inherit;
          font-size: 16px;
          line-height: 1;
          cursor: pointer;
        }

        .knowhow-filter-row {
          display: flex;
          align-items: center;
          gap: 8px;
          position: relative;
        }

        .knowhow-filter-input {
          width: 100%;
          max-width: 360px;
          box-sizing: border-box;
          padding: 9px 12px 9px 32px;
          background: var(--soft);
          border: 1px solid var(--line);
          border-radius: 8px;
          color: var(--ink);
          font-size: 13px;
        }

        .knowhow-filter-icon {
          position: absolute;
          left: 10px;
          color: var(--muted);
          pointer-events: none;
        }

        .knowhow-no-match {
          margin: 18px 0 0;
          color: var(--muted);
          font-size: 13px;
          text-align: center;
        }

        .knowhow-grid-scroll {
          overflow: auto;
          border: 1px solid var(--line);
          border-radius: 10px;
          flex: 1 1 auto;
          min-height: 0;
        }

        .knowhow-grid-table {
          border-collapse: separate;
          border-spacing: 0;
          width: 100%;
          table-layout: fixed;
          font-size: 13px;
        }

        .knowhow-grid-table th,
        .knowhow-grid-table td {
          border-bottom: 1px solid var(--line);
          border-right: 1px solid var(--line);
          padding: 10px 12px;
          text-align: left;
          vertical-align: top;
          width: 200px;
        }

        .knowhow-grid-table th:last-child,
        .knowhow-grid-table td:last-child {
          border-right: 0;
        }

        .knowhow-grid-table th {
          background: var(--soft);
          color: var(--muted);
          font-weight: 600;
          position: sticky;
          top: 0;
          z-index: 2;
        }

        .knowhow-grid-table td:first-child,
        .knowhow-grid-table th:first-child {
          position: sticky;
          left: 0;
          background: #fff;
          box-shadow: 2px 0 4px -2px rgba(24, 39, 75, 0.15);
        }

        .knowhow-grid-table th:first-child {
          z-index: 3;
          background: var(--soft);
        }

        .knowhow-grid-table tbody tr:hover td {
          background: #f4f7ff;
        }

        .knowhow-grid-table tbody tr:hover td:first-child {
          background: #eef2ff;
        }

        .knowhow-col-header {
          display: flex;
          align-items: center;
          gap: 6px;
          flex-wrap: wrap;
        }

        .knowhow-col-name {
          overflow: hidden;
          text-overflow: ellipsis;
          white-space: nowrap;
          min-width: 0;
        }

        /* canEdit=true 时的可改名列名：hover 时提示这里可以双击改名（虚线
           下划线 + cursor 变文本），不给太重的边框以免污染只读观感。 */
        .knowhow-col-name--editable {
          cursor: text;
          border-bottom: 1px dashed transparent;
        }
        .knowhow-col-name--editable:hover {
          border-bottom-color: var(--line);
        }

        /* inline 改名输入框：贴合列头字号，去掉浏览器默认外框，只在 focus
           时用蓝色边框强调正在编辑，不改变表格布局。 */
        .knowhow-col-name-input {
          font: inherit;
          color: inherit;
          background: #fff;
          border: 1px solid var(--blue);
          border-radius: 4px;
          padding: 2px 6px;
          min-width: 0;
          width: 100%;
          box-sizing: border-box;
        }
        .knowhow-col-name-input:focus {
          outline: none;
          box-shadow: 0 0 0 2px rgba(31, 94, 255, 0.15);
        }

        .knowhow-cell-open {
          display: -webkit-box;
          -webkit-line-clamp: 2;
          -webkit-box-orient: vertical;
          overflow: hidden;
          text-align: left;
          border: 0;
          background: transparent;
          color: var(--blue);
          font-weight: 600;
          padding: 0;
          cursor: pointer;
          line-height: 1.4;
        }

        .knowhow-cell-open:hover {
          text-decoration: underline;
        }

        .knowhow-cell-text {
          display: -webkit-box;
          -webkit-line-clamp: 2;
          -webkit-box-orient: vertical;
          overflow: hidden;
          line-height: 1.4;
          color: #333a45;
        }

        .knowhow-role-badge {
          display: inline-flex;
          align-items: center;
          font-size: 11px;
          font-weight: 600;
          padding: 2px 8px;
          border-radius: 999px;
          border: 1px solid var(--line);
          background: var(--soft);
          color: var(--muted);
          white-space: nowrap;
        }

        /* 徽章配色随词表收窄为四值 kind（迁移 17 起后端只返回这四值）：
           行标题=蓝、方法步骤=琥珀、工具/事物=绿、普通=中性。 */
        .knowhow-role-badge--anchor {
          color: #1f5eff;
          border-color: #c7d6ff;
          background: #eef2ff;
        }

        .knowhow-role-badge--procedure {
          color: #9a5b00;
          border-color: #f0dab3;
          background: #fdf4e6;
        }

        .knowhow-role-badge--entity {
          color: #177a55;
          border-color: #b7e4cf;
          background: #f0faf5;
        }

        .knowhow-role-badge--attribute {
          color: var(--muted);
          border-color: var(--line);
          background: var(--soft);
        }

        .knowhow-status-badge {
          display: inline-flex;
          align-items: center;
          gap: 6px;
          font-size: 12px;
          padding: 3px 9px;
          border-radius: 999px;
          border: 1px solid var(--line);
          background: var(--soft);
          color: var(--muted);
          white-space: nowrap;
        }

        .knowhow-status-badge--info {
          color: #1f5eff;
          border-color: #c7d6ff;
          background: #eef2ff;
        }

        .knowhow-status-badge--success {
          color: #177a55;
          border-color: #b7e4cf;
          background: #f0faf5;
        }

        .knowhow-status-badge--danger {
          color: #ba2d2d;
          border-color: #f0c0c0;
          background: #fef2f2;
        }

        /* Task 11（代码附件新鲜度"知识已更新"）新增色调——与
           .knowhow-role-badge--procedure / .kh-procedure-hint 同一套琥珀，
           视觉上"需要留意但不是错误"，区别于 --danger 的红。 */
        .knowhow-status-badge--warning {
          color: #9a5b00;
          border-color: #f0dab3;
          background: #fdf4e6;
        }

        .knowhow-status-retry {
          display: inline-flex;
          align-items: center;
          gap: 3px;
          border: 0;
          background: transparent;
          color: inherit;
          font-weight: 700;
          text-decoration: underline;
          padding: 0;
          cursor: pointer;
        }

        .knowhow-status-retry:disabled {
          opacity: 0.6;
          cursor: not-allowed;
        }

        .knowhow-spin {
          animation: knowhow-spin 0.8s linear infinite;
        }

        @keyframes knowhow-spin {
          to {
            transform: rotate(360deg);
          }
        }

        .knowhow-drawer-backdrop {
          position: fixed;
          inset: 0;
          z-index: 55;
          background: rgba(17, 24, 32, 0.36);
          border: 0;
          padding: 0;
        }

        .knowhow-drawer {
          position: fixed;
          top: 0;
          right: 0;
          bottom: 0;
          z-index: 56;
          width: min(640px, 100vw);
          background: #fff;
          box-shadow: var(--shadow);
          display: flex;
          flex-direction: column;
          animation: knowhow-drawer-in 0.18s ease-out;
        }

        @keyframes knowhow-drawer-in {
          from {
            transform: translateX(24px);
            opacity: 0;
          }
          to {
            transform: translateX(0);
            opacity: 1;
          }
        }

        .knowhow-drawer-header {
          display: flex;
          align-items: flex-start;
          justify-content: space-between;
          gap: 12px;
          padding: 18px 20px;
          border-bottom: 1px solid var(--line);
        }

        .knowhow-drawer-header h2 {
          margin: 0;
          font-size: 20px;
          overflow-wrap: anywhere;
        }

        .knowhow-drawer-header-actions {
          display: flex;
          align-items: center;
          gap: 8px;
          flex: 0 0 auto;
        }

        .knowhow-drawer-body {
          flex: 1 1 auto;
          min-height: 0;
          overflow-y: auto;
          padding: 20px;
          display: grid;
          gap: 22px;
          align-content: start;
        }

        .knowhow-drawer-section-head {
          display: flex;
          align-items: center;
          gap: 8px;
          margin-bottom: 8px;
          padding-bottom: 6px;
          border-bottom: 1px solid var(--line);
        }

        .knowhow-drawer-section-head h4 {
          margin: 0;
          font-size: 14px;
          color: var(--ink);
        }

        .knowhow-empty-cell {
          color: var(--muted);
          font-style: italic;
          margin: 0;
        }

        .knowhow-drawer .answer-markdown p,
        .knowhow-drawer .answer-markdown ul,
        .knowhow-drawer .answer-markdown ol {
          font-size: 14px;
        }

        .knowhow-image {
          max-width: 100%;
          border-radius: 8px;
          border: 1px solid var(--line);
        }

        .knowhow-image-loading,
        .knowhow-image-fallback {
          display: inline-block;
          color: var(--muted);
          font-size: 13px;
          padding: 8px 0;
        }

        @media (max-width: 720px) {
          .knowhow-drawer {
            width: 100vw;
          }
        }

        /* 网格空格「+」浅色占位（规格②路A「空格显浅色占位」）：复用
           .knowhow-cell-open 的布局/hover，只覆盖颜色与字重，与已填格子的
           蓝色粗体形成「有内容/待填写」的视觉区分。 */
        .knowhow-cell-open--empty {
          color: var(--muted);
          font-weight: 400;
        }

        /* G2 合并矩阵（规格 §4.2.1）：rowspan 起始格用 --soft 弱底色与四周
           独立格区分，纵向居中避免多行合并后内容贴顶——沿用本文件既有的
           CSS 变量令牌，不额外引入硬编码色值/暗色媒体查询（本应用当前无
           暗色主题，--soft 由全局 :root 统一定义，天然随主题走）。 */
        .knowhow-cell-merged {
          background: var(--soft);
          vertical-align: middle;
        }

        /* 行详情抽屉——分节「编辑」按钮（Task 7：每节打开同一个格子浮窗的
           编辑态）。 */
        .knowhow-drawer-edit-button {
          margin-left: auto;
          display: inline-flex;
          align-items: center;
          gap: 4px;
          border: 1px solid var(--line);
          border-radius: 999px;
          padding: 3px 10px;
          font-size: 12px;
          font-weight: 600;
          color: var(--blue);
          background: #fff;
          cursor: pointer;
          white-space: nowrap;
        }

        .knowhow-drawer-edit-button:hover {
          border-color: var(--blue);
          background: #eef2ff;
        }

        /* -------------------------------------------------------------------
           kh-* — 格子浮窗（knowhow-cell-editor.tsx 的 KnowhowCellPreview /
           KnowhowCellEditor 共用；命名空间独立于上面的 knowhow-* 前缀，避免
           与既有网格/抽屉样式混淆）。登记在这里而非那个文件自己的 style jsx
           标签里，是因为 styled-jsx 的 global 样式注入绑定「声明该标签的
           组件是否渲染过」——KnowhowPanel 是本特性唯一保证任何时候都已经
           挂载的容器，预览态/编辑态两者谁先渲染都不会缺样式。
           ------------------------------------------------------------------- */

        .kh-modal-overlay {
          position: fixed;
          inset: 0;
          z-index: 65;
          display: grid;
          place-items: center;
          padding: 24px;
          background: rgba(17, 24, 32, 0.36);
        }

        .kh-modal-card {
          width: min(880px, 100%);
          max-height: 80vh;
          background: #fff;
          border-radius: 16px;
          box-shadow: 0 24px 70px rgba(24, 39, 75, 0.24);
          display: flex;
          flex-direction: column;
          overflow: hidden;
          /* 左侧色条是查看/编辑态的第一眼视觉标记（见 --preview / --editor 修饰
             类），通过内边距让 header/body 内容与色条错开。 */
          border-left: 4px solid transparent;
        }

        /* 只读预览态：中性灰蓝左边条 + 白底 header 无强调。 */
        .kh-modal-card--preview {
          border-left-color: #c7d1de;
        }

        /* 编辑态：琥珀色左边条，与 procedure hint / role-badge--procedure 同一套
           琥珀语义（内容处在活跃编辑状态时的一致强调色）。 */
        .kh-modal-card--editor {
          border-left-color: #d99a3b;
        }

        /* 全屏切换：kh-modal-overlay 的 padding 已经把卡片缩进 24px，全屏时把
           overlay padding 归零由卡片自己撑满，同时把圆角 / 阴影去掉——真实全屏
           质感（不是「更大一点」）。 */
        .kh-modal-overlay:has(> .kh-modal-card--fullscreen) {
          padding: 0;
        }
        .kh-modal-card--fullscreen {
          width: 100vw;
          max-height: 100vh;
          height: 100vh;
          border-radius: 0;
          box-shadow: none;
        }

        /* header 里紧跟面包屑的小态标（「查看」/「编辑中」）——用同一支
           .kh-mode-tag，靠 --preview / --editor 修饰类换色调，让编辑/查看态在
           header 里也有明确文本标注（不只靠左侧色条）。 */
        .kh-mode-tag {
          display: inline-flex;
          align-items: center;
          gap: 4px;
          padding: 2px 8px;
          font-size: 11px;
          font-weight: 600;
          border-radius: 999px;
          border: 1px solid var(--line);
          background: var(--soft);
          color: var(--muted);
          white-space: nowrap;
        }
        .kh-mode-tag--editor {
          border-color: #f0dab3;
          background: #fdf4e6;
          color: #9a5b00;
        }

        /* 合并格编辑提示（anchor 分组 spec §4.4）：紧跟 kh-mode-tag--editor
           出现在面包屑里，告知这一格是「合并共享格」——保存会批量写整组，
           不只是眼前这一行（handleCellSave 的批量写判定与这里的
           affectedBranchCount 同源，见 knowhow-panel.tsx handleCellSave）。
           沿用 kh-mode-tag--editor / kh-procedure-hint 同一套琥珀语义，与
           「编辑中」标签同色系但用一句完整提示文本而非图标短标签的形态，
           避免和状态标签混淆。 */
        .kh-affect-hint {
          display: inline-flex;
          align-items: center;
          padding: 2px 8px;
          font-size: 11px;
          font-weight: 600;
          border-radius: 999px;
          border: 1px solid #f0dab3;
          background: #fdf4e6;
          color: #9a5b00;
          white-space: nowrap;
        }

        /* 全屏切换按钮：复用 icon-button 尺寸/交互，只是有独立 title/图标；不
           需要单独样式，此注释只标记它在 header-actions 里的位置。 */

        .kh-modal-header {
          flex: 0 0 auto;
          padding: 16px 20px;
          border-bottom: 1px solid var(--line);
          display: flex;
          flex-direction: column;
          gap: 8px;
        }

        .kh-modal-header-top {
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 12px;
        }

        .kh-modal-breadcrumb {
          display: flex;
          align-items: center;
          flex-wrap: wrap;
          gap: 8px;
          min-width: 0;
          font-size: 15px;
          font-weight: 600;
          color: var(--ink);
        }

        .kh-modal-row-title {
          overflow: hidden;
          text-overflow: ellipsis;
          white-space: nowrap;
          max-width: 320px;
        }

        .kh-modal-sep {
          color: var(--muted);
          font-weight: 400;
        }

        .kh-modal-col-name {
          overflow: hidden;
          text-overflow: ellipsis;
          white-space: nowrap;
        }

        .kh-modal-header-actions {
          display: flex;
          align-items: center;
          gap: 8px;
          flex: 0 0 auto;
        }

        .kh-row-context-toggle {
          align-self: flex-start;
          display: inline-flex;
          align-items: center;
          gap: 4px;
          border: 0;
          background: transparent;
          color: var(--muted);
          font-size: 12px;
          font-weight: 600;
          cursor: pointer;
          padding: 0;
        }

        .kh-row-context-toggle svg {
          transition: transform 0.15s ease;
        }

        .kh-row-context-toggle svg.kh-chevron-open {
          transform: rotate(180deg);
        }

        .kh-row-context-body {
          display: grid;
          gap: 6px;
          padding: 10px 12px;
          border: 1px solid var(--line);
          border-radius: 8px;
          background: var(--soft);
          font-size: 12px;
        }

        .kh-row-context-item {
          display: flex;
          align-items: baseline;
          gap: 8px;
          min-width: 0;
          padding: 4px 8px;
          border-radius: 6px;
          border: 1px solid transparent;
        }

        /* 高亮当前浮层对应的那一行——用户打开浮层后能一眼看到「我在整行的
           哪个位置」，无需在浮层和网格之间来回对照。琥珀色与 editor 左边条
           /kh-procedure-hint 同一色系，保持整套编辑体验的一致强调色。 */
        .kh-row-context-item--current {
          background: #fff7e6;
          border-color: #f0dab3;
        }

        .kh-row-context-current-tag {
          flex: 0 0 auto;
          padding: 1px 6px;
          font-size: 10.5px;
          font-weight: 700;
          letter-spacing: 0.4px;
          border-radius: 999px;
          background: #d99a3b;
          color: #fff;
        }

        .kh-row-context-item strong {
          flex: 0 0 auto;
          color: var(--ink);
        }

        .kh-row-context-text {
          flex: 1 1 auto;
          min-width: 0;
          overflow: hidden;
          text-overflow: ellipsis;
          white-space: nowrap;
          color: var(--muted);
        }

        .kh-row-context-empty {
          margin: 0;
          color: var(--muted);
        }

        .kh-modal-body {
          flex: 1 1 auto;
          min-height: 0;
          overflow-y: auto;
          padding: 16px 20px;
          display: flex;
          flex-direction: column;
          gap: 12px;
        }

        .kh-draft-banner {
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 12px;
          padding: 8px 14px;
          border: 1px solid #c7d6ff;
          border-radius: 8px;
          background: #eef2ff;
          color: #1f5eff;
          font-size: 13px;
        }

        .kh-draft-banner-actions {
          display: flex;
          gap: 8px;
          flex: 0 0 auto;
        }

        .kh-draft-banner-actions button {
          border: 1px solid #c7d6ff;
          background: #fff;
          border-radius: 999px;
          padding: 4px 12px;
          font-size: 12px;
          font-weight: 600;
          color: #1f5eff;
          cursor: pointer;
        }

        .kh-procedure-hint {
          margin: 0;
          padding: 8px 12px;
          border-radius: 8px;
          background: #fdf4e6;
          border: 1px solid #f0dab3;
          color: #9a5b00;
          font-size: 12.5px;
        }

        .kh-toolbar {
          display: flex;
          align-items: center;
          gap: 8px;
          flex-wrap: wrap;
        }

        .kh-toolbar-button {
          display: inline-flex;
          align-items: center;
          gap: 6px;
          border: 1px solid var(--line);
          border-radius: 8px;
          background: #fff;
          padding: 5px 10px;
          font-size: 12.5px;
          color: var(--ink);
          cursor: pointer;
        }

        .kh-toolbar-button:hover {
          border-color: var(--blue);
          color: var(--blue);
        }

        .kh-toolbar-button:disabled {
          opacity: 0.6;
          cursor: not-allowed;
        }

        .kh-toolbar-status {
          display: inline-flex;
          align-items: center;
          gap: 6px;
          font-size: 12.5px;
          color: var(--muted);
        }

        .kh-hidden-file-input {
          display: none;
        }

        .kh-inline-error {
          color: #ba2d2d;
          font-size: 12.5px;
          margin: 0 auto 0 0;
        }

        .kh-split {
          flex: 1 1 auto;
          min-height: 260px;
          display: grid;
          grid-template-columns: 1fr 1fr;
          gap: 14px;
        }

        .kh-editor-pane {
          display: flex;
          min-width: 0;
          border: 1px solid var(--line);
          border-radius: 8px;
          transition: border-color 0.15s ease, background 0.15s ease;
        }

        .kh-editor-pane--drag {
          border-color: var(--blue);
          background: #eef2ff;
        }

        .kh-textarea {
          flex: 1 1 auto;
          width: 100%;
          min-height: 220px;
          border: 0;
          border-radius: 8px;
          padding: 10px 12px;
          font: 13px/1.6 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
          color: var(--ink);
          background: transparent;
          resize: none;
          box-sizing: border-box;
        }

        .kh-textarea:focus {
          outline: none;
        }

        .kh-textarea:disabled {
          opacity: 0.6;
        }

        .kh-preview-pane {
          min-width: 0;
          overflow-y: auto;
          border: 1px solid var(--line);
          border-radius: 8px;
          padding: 10px 12px;
        }

        .kh-preview-body {
          min-height: 120px;
        }

        .kh-preview-edit-button {
          display: inline-flex;
          align-items: center;
          gap: 6px;
          border: 1px solid var(--line);
          border-radius: 999px;
          padding: 5px 12px;
          font-size: 12.5px;
          font-weight: 600;
          color: var(--blue);
          background: #fff;
          cursor: pointer;
        }

        .kh-preview-edit-button:hover {
          border-color: var(--blue);
          background: #eef2ff;
        }

        /* Task 9：「优化表达」工具栏按钮——与列表/代码/图片同款按钮外壳，配一个
           淡紫色调把它跟纯排版操作区分开(LLM 触发的动作，不是格式化)。 */
        .kh-toolbar-button--optimize {
          border-color: #d9c7ff;
          color: #6d3fd1;
        }

        .kh-toolbar-button--optimize:hover:not(:disabled) {
          border-color: #6d3fd1;
          background: #f5f0ff;
        }

        /* Task 9：单格「优化表达」原文/建议对照（规格③）与「优化整行」队列
           里当前格的对照复用同一套 .kh-optimize-* 样式。 */
        .kh-optimize-compare {
          flex: 1 1 auto;
          min-height: 220px;
          display: grid;
          grid-template-columns: 1fr 1fr;
          gap: 14px;
        }

        .kh-optimize-pane {
          display: flex;
          flex-direction: column;
          min-width: 0;
          border: 1px solid var(--line);
          border-radius: 8px;
          overflow: hidden;
        }

        .kh-optimize-pane h5 {
          margin: 0;
          padding: 8px 12px;
          font-size: 12px;
          font-weight: 700;
          color: var(--muted);
          background: var(--soft);
          border-bottom: 1px solid var(--line);
        }

        .kh-optimize-pane--suggestion h5 {
          color: #6d3fd1;
          background: #f5f0ff;
        }

        .kh-optimize-pane-body {
          flex: 1 1 auto;
          min-height: 0;
          overflow-y: auto;
          padding: 10px 12px;
        }

        .kh-optimize-queue-list {
          margin: 0;
          padding: 0;
          list-style: none;
          display: flex;
          flex-direction: column;
          gap: 4px;
        }

        .kh-optimize-queue-item {
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 10px;
          padding: 6px 10px;
          border-radius: 8px;
          font-size: 12.5px;
          color: var(--muted);
        }

        .kh-optimize-queue-item--current {
          background: var(--soft);
          color: var(--ink);
          font-weight: 600;
        }

        .kh-optimize-queue-col {
          overflow: hidden;
          text-overflow: ellipsis;
          white-space: nowrap;
          min-width: 0;
        }

        .kh-optimize-queue-status {
          flex: 0 0 auto;
          font-size: 11.5px;
          font-weight: 600;
        }

        .kh-optimize-queue-status--ready,
        .kh-optimize-queue-status--in_progress {
          color: #1f5eff;
        }

        .kh-optimize-queue-status--accepted {
          color: #177a55;
        }

        .kh-optimize-queue-status--error {
          color: #ba2d2d;
        }

        .kh-optimize-queue-status--skipped,
        .kh-optimize-queue-status--waiting {
          color: var(--muted);
        }

        .kh-modal-footer {
          flex: 0 0 auto;
          padding: 14px 20px;
          border-top: 1px solid var(--line);
          display: flex;
          align-items: center;
          justify-content: flex-end;
          gap: 12px;
        }

        .kh-footer-actions {
          display: flex;
          align-items: center;
          gap: 10px;
        }

        .kh-footer-actions button {
          display: inline-flex;
          align-items: center;
          gap: 6px;
          border: 1px solid var(--line);
          border-radius: 8px;
          padding: 8px 14px;
          font-size: 13px;
          font-weight: 600;
          background: #fff;
          color: var(--ink);
          cursor: pointer;
        }

        .kh-footer-actions button:disabled {
          opacity: 0.6;
          cursor: not-allowed;
        }

        /* 复合选择器而非 !important：.kh-footer-actions button 的 (0,1,1)
           特异度本会盖过裸的 .kh-primary-button (0,1,0)；两个类叠加成
           (0,2,0) 天然胜出，不需要 !important 这把大锤（也不会连累其它
           容器里若出现同名类时的可覆盖性）。本类目前只在 .kh-footer-actions
           内使用（KnowhowCellEditor 的「保存并下一格」/优化对照的「接受」、
           KnowhowRowOptimizeModal 的「接受」）。 */
        .kh-footer-actions .kh-primary-button {
          background: var(--blue);
          border-color: var(--blue);
          color: #fff;
        }

        .kh-close-guard {
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 12px;
          width: 100%;
          font-size: 13px;
          color: #ba2d2d;
        }

        .kh-close-guard-actions {
          display: flex;
          gap: 8px;
          flex: 0 0 auto;
        }

        .kh-close-guard-actions button {
          border: 1px solid var(--line);
          border-radius: 8px;
          padding: 6px 12px;
          font-size: 12.5px;
          font-weight: 600;
          background: #fff;
          cursor: pointer;
        }

        /* 同上，复合选择器而非 !important：本类目前只在
           .kh-close-guard-actions 内使用（「放弃并关闭」）。 */
        .kh-close-guard-actions .kh-danger-button {
          border-color: #f0c0c0;
          background: #fef2f2;
          color: #b91c1c;
        }

        /* -------------------------------------------------------------------
           Task 11（代码附件 UI，knowhow-code.tsx 的 KnowhowCodeChip /
           KnowhowCodeModal 共用）——登记在这里而非 knowhow-code.tsx 自己的
           <style jsx> 里，理由同上方 kh-modal-* 一段注释：这是本特性唯一
           保证任何时候都已挂载的样式容器。
           ------------------------------------------------------------------- */

        /* chip 本身是 <button>，这里只重置 UA 默认按钮观感（字体/指针），
           配色完全交给叠加的 .knowhow-status-badge--{tone} 类。 */
        .kh-code-chip {
          font: inherit;
          cursor: pointer;
        }

        /* "添加代码"安静入口（none 态 + canEdit）：虚线 + 弱化配色，与三态
           里 implemented/stale 的实心徽章形成"待完成 vs 已完成"的视觉区分，
           不与其它徽章抢注意力。 */
        .kh-code-chip--add {
          display: inline-flex;
          align-items: center;
          gap: 4px;
          border: 1px dashed var(--line);
          border-radius: 999px;
          padding: 2px 8px;
          font-size: 11px;
          color: var(--muted);
          background: transparent;
        }

        .kh-code-chip--add:hover {
          border-color: var(--blue);
          color: var(--blue);
        }

        .kh-code-explain {
          margin: 0;
          color: var(--muted);
          font-size: 13px;
        }

        .kh-code-lang-row {
          display: flex;
          align-items: center;
          gap: 10px;
          flex-wrap: wrap;
        }

        .kh-code-lang-tag {
          display: inline-flex;
          align-items: center;
          font-size: 11px;
          font-weight: 600;
          padding: 2px 8px;
          border-radius: 999px;
          border: 1px solid var(--line);
          background: var(--soft);
          color: var(--muted);
          white-space: nowrap;
        }

        .kh-code-updated {
          font-size: 12px;
          color: var(--muted);
        }

        .kh-code-lang-input {
          width: 100%;
          max-width: 320px;
          box-sizing: border-box;
          padding: 6px 10px;
          border: 1px solid var(--line);
          border-radius: 8px;
          font-size: 13px;
          color: var(--ink);
          background: #fff;
        }

        .kh-code-lang-input:disabled {
          opacity: 0.6;
        }

        /* 等宽代码块（规格⑥-4"点击浮层查看（等宽...）"）：只读展示，不经
           KnowhowMarkdown——代码是原样文本，不是 markdown。 */
        .kh-code-block {
          flex: 1 1 auto;
          min-height: 160px;
          max-height: 50vh;
          overflow: auto;
          margin: 0;
          padding: 12px 14px;
          border: 1px solid var(--line);
          border-radius: 8px;
          background: var(--soft);
          font: 13px/1.6 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
          color: var(--ink);
          white-space: pre-wrap;
          word-break: break-word;
        }

        .kh-code-textarea {
          flex: 1 1 auto;
          min-height: 240px;
        }

        /* 行详情抽屉：本行代码状态加载失败的轻量内联提示（不阻塞整行其余内容
           渲染——只影响代码 chip 这一小块辅助信息）。 */
        .kh-code-status-error {
          display: flex;
          align-items: center;
          gap: 8px;
          margin: 0 0 4px;
          font-size: 12.5px;
          color: var(--muted);
        }

        .kh-code-status-error button {
          border: 0;
          background: transparent;
          color: var(--blue);
          font-size: 12.5px;
          font-weight: 600;
          text-decoration: underline;
          cursor: pointer;
          padding: 0;
        }

        /* -------------------------------------------------------------------
           Task 7（C 概念矩阵抽屉，knowhow-matrix-drawer.tsx 的
           KnowhowMatrixDrawer 专用）——登记在这里而非该组件自己的
           <style jsx>，理由同上方 kh-modal-* / kh-code-* 两段注释：这是本
           特性唯一保证任何时候都已挂载的样式容器。
           矩阵语义（spec §4.3）：属性为行、分支为列，是主网格 G2（属性为列、
           概念为行，见上方 .knowhow-cell-merged 一段）的转置视图。
           ------------------------------------------------------------------- */

        /* 矩阵比普通格子浮窗（880px）宽——属性名列 + 多个分支列并排，880px
           在 3 个以上分支时会太挤。放在 .kh-modal-card 之后、mobile 断点
           媒体查询之前，窄屏时仍会被下方 @media (max-width: 720px) 里的
           .kh-modal-card 规则（源码序在后，同优先级取后者）压回 100vw。 */
        .kh-matrix-card {
          width: min(1040px, 96vw);
        }

        .kh-matrix {
          width: 100%;
          border-collapse: collapse;
          font-size: 13px;
        }

        .kh-matrix th,
        .kh-matrix td {
          border: 1px solid var(--line);
          padding: 8px 12px;
          text-align: left;
          vertical-align: top;
        }

        /* 可点格子（knowhow-matrix-drawer.tsx clickableCellProps 挂了
           role="button" 的共享格/分支格，isClickable 为真才有）：cursor
           提示两种格子共用；hover 底色只覆盖分支格，用 :not(.kh-matrix-
           shared) 与下方共享格的琥珀 hover 互斥，两条规则各管一半格子、
           不必比拼选择器优先级。不可点的格子没有 role="button"，不受影响。
           分支格 hover 呼应 G2 网格 .knowhow-grid-table tbody tr:hover td
           的浅蓝底语义，这里限定单格而非整行——矩阵同一行内每个分支格是
           各自独立的点击目标。 */
        .kh-matrix [role="button"] {
          cursor: pointer;
        }

        .kh-matrix td[role="button"]:not(.kh-matrix-shared):hover {
          background: #f4f7ff;
        }

        .kh-matrix thead th {
          background: var(--soft);
          color: var(--muted);
          font-weight: 600;
          white-space: nowrap;
          min-width: 140px;
        }

        /* Follow-up B（删分支，spec §4.4）：表头「分支 N」文本 + 删除图标
           一行两端对齐——图标常驻宽度不大，justify-content: space-between
           让它贴右边，不挤在文本正后面。二次确认态（.knowhow-confirm 整体
           替换本 span）不受这条规则影响，两者互斥渲染。 */
        .kh-matrix-branch-head {
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 8px;
        }

        /* 表头删除图标：比既有 .icon-button（42px 圆形，为 header 顶栏设计）
           小得多的定位样式——塞进表头格会显得极不协调，这里单开一个紧凑的
           无边框图标按钮，hover 时变红提示危险操作，同 .knowhow-confirm 的
           #fef2f2 底色/var(--red)，不新开色值。 */
        .kh-matrix-branch-delete {
          display: inline-flex;
          align-items: center;
          justify-content: center;
          flex: 0 0 auto;
          border: none;
          border-radius: 4px;
          padding: 2px;
          background: transparent;
          color: var(--muted);
          cursor: pointer;
        }

        .kh-matrix-branch-delete:hover {
          color: var(--red);
          background: #fef2f2;
        }

        /* 属性名列（行头）：灰底与表头呼应，纵向居中避免长文本内容把它挤到
           顶部——同一令牌 var(--soft)，与 G2 的 .knowhow-cell-merged / 表头
           视觉同源，强化"这一列是结构性标签，不是内容"的区分。 */
        .kh-matrix-rowhead {
          background: var(--soft);
          color: var(--ink);
          font-weight: 600;
          white-space: nowrap;
          vertical-align: middle;
        }

        /* 全分支同值的共享属性格（MatrixAttrRow.sharedSpan）：琥珀底，与本
           文件既有的 procedure 徽章 / kh-modal-card--editor 左边条 /
           status-badge--warning 同一套琥珀语义（"这一格代表多个分支共享的
           同一个值"，不是不同分支各自的内容）。 */
        .kh-matrix-shared {
          background: #fdf4e6;
          vertical-align: middle;
        }

        /* 共享格 hover：可点时保持琥珀色系而非上面分支格的浅蓝——复用既有
           琥珀边框色 #f0dab3（同 .knowhow-status-badge--warning 一套）当加
           深一档的 hover 底色，不新开色值，选择器与上方分支格 hover 互斥
           不会互相盖掉。 */
        .kh-matrix-shared[role="button"]:hover {
          background: #f0dab3;
        }

        /* 命中分支高亮（spec §4.5，ask 引用跳转 Task 11 接线）：表头文字变蓝
           + 内嵌蓝框标记整列/该格。用 inset box-shadow 而非 border——
           border-collapse: collapse 会让相邻格子的边框互相吃掉，box-shadow
           不参与折叠、能在合并边框表格里稳定画出完整的一圈高亮。 */
        .kh-matrix-branch--hi {
          color: var(--blue);
          box-shadow: inset 0 0 0 2px var(--blue);
        }

        .kh-matrix-cell--hi {
          box-shadow: inset 0 0 0 2px var(--blue);
        }

        @media (max-width: 720px) {
          .kh-modal-card {
            width: 100vw;
            max-height: 100vh;
            border-radius: 0;
          }

          .kh-split {
            grid-template-columns: 1fr;
          }

          .kh-optimize-compare {
            grid-template-columns: 1fr;
          }
        }
      `}</style>
    </section>
  );
}

// ---------------------------------------------------------------------------
// Layer 1 — 表列表
// ---------------------------------------------------------------------------

function KnowhowTableList({
  tables,
  loading,
  error,
  canEdit,
  onRetry,
  onOpen,
  onImportClick,
  onCreateClick,
}: {
  tables: KnowhowTableSummary[] | null;
  loading: boolean;
  error: string | null;
  canEdit: boolean;
  onRetry: () => void;
  onOpen: (tableId: string) => void;
  onImportClick: () => void;
  onCreateClick: () => void;
}) {
  return (
    <>
      <div className="knowhow-toolbar">
        <span className="panel-count">{tables && tables.length > 0 ? `${tables.length} 张表` : ""}</span>
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
      ) : !tables || tables.length === 0 ? (
        <div className="knowhow-empty">
          <div className="knowhow-empty-icon">▦</div>
          <strong>还没有 knowhow 表</strong>
          <p>{canEdit ? "可从 Excel/CSV/Markdown 导入，或新建空表现场定表头" : "当前为只读访问"}</p>
        </div>
      ) : (
        <div className="knowhow-table-cards">
          {tables.map((table) => (
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
  onAddRow,
  addingRow,
  onAddConcept,
  onDownloadTemplate,
  templateDownloading,
  onAppendClick,
  onRenameColumn,
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
  /** 表头列名 inline 改名（canEdit 时启用）：双击列名进入编辑框，回车/失焦
   * 时把新名字丢回来落库。用户不必再翻「管理」抽屉找列改名——就地改。 */
  onRenameColumn: (columnId: string, name: string) => void;
}) {
  const orderedColumns = useMemo(() => (detail ? orderColumnsForGrid(detail.columns) : []), [detail]);
  const filteredRows = useMemo(() => (detail ? filterRows(detail.rows, query) : []), [detail, query]);

  // 有 anchor 列 → 分组合并矩阵渲染（spec §4.2 G2）；无 anchor（记录型表）
  // → gridDisplayRows 为 null，<tbody> 落回原平铺渲染，零改动（spec §4.2.3）。
  const anchorColumnId = detail?.anchorColumnId ?? null;
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
        {/* 全部写入口（添加行/下载模板/追加导入/管理/重建投影/删除表）只对
            canEdit 出现；只读成员的工具栏只剩返回与标题（规格⑦）。 */}
        {canEdit && (
          <div className="knowhow-grid-toolbar-actions">
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
            {/* 表级「重建投影」逃生口(spec 要求)：整表重投影，后台执行。区别于
                失败行徽标上的行内「重试」——那只在某行 failed 时出现，这个入口
                任何时候都在，供用户在整表走样时一键重建。进行中禁用防重复触发。 */}
            <button
              type="button"
              className="sort-button knowhow-reproject-button"
              onClick={onRetryReproject}
              disabled={retryingReproject || deleting || !detail}
              title="重新投影整张表（后台执行）"
            >
              <RefreshCw size={14} className={retryingReproject ? "knowhow-spin" : undefined} />
              {retryingReproject ? "重建中…" : "重建投影"}
            </button>
            {confirmDelete ? (
              <span className="knowhow-confirm">
                <span>删除这张表？行、格与投影产物将一并删除</span>
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
          </div>
        )}
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
            <table className="knowhow-grid-table">
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

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const current = currentRowOptimizeItem(queue);

  async function fireOptimizeFor(item: RowOptimizeItem) {
    setQueue((state) => markCurrentInProgress(state));
    try {
      const result = await optimizeKnowhowCell(notebookId, tableId, row.id, item.columnId);
      if (!mountedRef.current) return;
      setQueue((state) => applySuggestion(state, result.suggestionMd));
    } catch (err) {
      if (!mountedRef.current) return;
      setQueue((state) => applyError(state, extractErrorMessage(err, "优化失败，请重试")));
    }
  }

  // 挂载时启动队列首格（若队列非空）；ref 闩防 StrictMode 开发期双调用
  // 重复发起同一格的请求（本组件不像 KnowhowImage 那样有天然的
  // cancelled 闭包变量可用——这里的请求会经过好几次 setState 才落地，用一次
  // 性 ref 闩更直接）。
  useEffect(() => {
    if (startedRef.current) return;
    startedRef.current = true;
    if (current) void fireOptimizeFor(current);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function requestClose() {
    if (acceptBusy) return;
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
    setQueue((state) => abortQueue(state));
  }

  const finished = isRowOptimizeQueueFinished(queue);
  const progress = rowOptimizeProgress(queue);

  return (
    <div className="kh-modal-overlay" onClick={handleBackdropClick}>
      <div
        className="kh-modal-card"
        role="dialog"
        aria-modal="true"
        aria-label={`${ROW_OPTIMIZE_BUTTON_LABEL} · ${rowTitle}`}
        onClick={(event) => event.stopPropagation()}
      >
        <header className="kh-modal-header">
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
          title="重新投影整张表（后台执行）"
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
