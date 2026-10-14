/**
 * knowhow-panel.tsx
 *
 * 「Knowhow 表」总览：表列表 → 表格网格 → 行详情抽屉 三层，外加导入向导 /
 * 建表向导 / 表管理三个 modal 的挂载点。page.tsx 只负责接线（导航按钮 +
 * `<KnowhowPanel notebookId apiBase canEdit onClose />` 一处挂载，外加 Task 12
 * 引用跳转用的可选 `initialTableId`/`initialRowId`），面板的顶层状态机（选中
 * 哪张表、哪一行、哪个弹窗打开）与各挂载点在这里；自带状态机的弹窗与样式在
 * 各自的文件里（见下方「编辑入口的分工」）。三层视图与三块领域状态也各有
 * 文件：表列表 knowhow-table-list.tsx + use-knowhow-table-list.ts，表格网格
 * knowhow-table-grid.tsx（徽章在 knowhow-badges.tsx），行详情抽屉
 * knowhow-row-drawer.tsx + use-knowhow-row-code.ts，概念矩阵的状态与删除命令
 * use-knowhow-concept-matrix.ts（矩阵抽屉本身在 knowhow-matrix-drawer.tsx）。
 * 表详情、引用跳转、表级写命令与 handleCellSave 留在本文件。
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
 * 建空行后自动打开首格编辑态。行详情抽屉上的三个批量弹窗各自持有状态机：
 * 「智能补全空列」在 knowhow-row-completion-modal.tsx，「优化整行」在
 * knowhow-row-optimize-modal.tsx，「一键规整整行/整表」在
 * knowhow-reformat-batch-modal.tsx；整套样式在 knowhow-panel-styles.tsx。
 * 本文件对这些 modal 只做「打开/关闭 + 完成后刷新」的接线（保存统一经
 * handleCellSave），不重复实现它们内部的任何逻辑。
 */
"use client";

import {
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import {
  X,
} from "lucide-react";
import { requestBlob } from "./api-client.ts";
import {
  cellSummary,
  fetchKnowhowTable,
  deleteKnowhowTable,
  reprojectKnowhowTable,
  addKnowhowRow,
  patchKnowhowCell,
  batchPatchKnowhowCells,
  knowhowTemplateUrl,
  patchKnowhowColumn,
  type KnowhowHealthFilter,
  type KnowhowTableDetail,
} from "./knowhow-model.ts";
import {
  orderColumnsForGrid,
  resolveRowTitleText,
  appendRowOptimistically,
} from "./knowhow-panel-logic.ts";
import {
  groupRowsByAnchor,
  groupCellWriteTargets,
  isSharedColumn,
} from "./knowhow-grouping-logic.ts";
import { extractErrorMessage } from "./knowhow-import-logic.ts";
import { rowFallbackTitle } from "./knowhow-cell-editor-logic.ts";
import { KnowhowImportWizard, KnowhowAppendWizard } from "./knowhow-import.tsx";
import { KnowhowCreateWizard, KnowhowManageModal } from "./knowhow-manage.tsx";
import { KnowhowCellPreview, KnowhowCellEditor } from "./knowhow-cell-editor.tsx";
// knowhow 表版本管理 Task 16：格子浮窗第三态（历史）——与 KnowhowCellPreview/
// KnowhowCellEditor 同一套 cellModal 顶层状态机驱动，见下方 cellModal 类型与
// 渲染处的三选一分支。
import { KnowhowCellHistory } from "./knowhow-cell-history.tsx";
import { KnowhowCodeModal } from "./knowhow-code.tsx";
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
import { KnowhowRowCompletionModal } from "./knowhow-row-completion-modal.tsx";
import { KnowhowRowOptimizeModal } from "./knowhow-row-optimize-modal.tsx";
import { KnowhowReformatBatchModal } from "./knowhow-reformat-batch-modal.tsx";
import { KnowhowTableList } from "./knowhow-table-list.tsx";
import { useKnowhowTableList } from "./use-knowhow-table-list.ts";
import { KnowhowTableGrid } from "./knowhow-table-grid.tsx";
import { KnowhowRowDrawer } from "./knowhow-row-drawer.tsx";
import { useKnowhowRowCode } from "./use-knowhow-row-code.ts";
import { useKnowhowConceptMatrix } from "./use-knowhow-concept-matrix.ts";
import { transferKnowhowTable, KnowhowSourceCleanupError } from "./knowhow-transfer.ts";
import { resolveCellCodeView } from "./knowhow-code-logic.ts";
import {
  templateDownloadFilename,
  openSavedReformatCellAfterReload,
  REFORMAT_OPEN_CELL_ERROR,
  type ReformatBatchScope,
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
  // 三个 owner hook（表列表 / 行代码 / 概念矩阵）各调用一次，用单一标识符持有，
  // 在使用点直接读 `.view.*` 或调命令：不解构、不逐字段摊平，也不把返回对象放进
  // 依赖数组或整体下传（每次渲染都是新对象）。调用位置决定 effect 顺序，是承重
  // 的：表列表在最前，行代码在拉表详情的 effect 之后、notebook 复位 effect 之前，
  // 概念矩阵在 return 之前最后。owner-view-no-reflatten 与 knowhow-panel-boundary
  // 两个守卫钉住这些约定。
  const knowhowTableList = useKnowhowTableList({ notebookId, initialHealthFilter });

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
  // 删表失败、而用户此时已经不在那张表上（删除进行中返回或换了表，S6）：失败不能写进
  // 当前这张表的 actionError 横幅（会被当成这张表的失败），改在面板顶部的提示条里点名
  // 被删的表。只在打开表、手动关闭、换笔记本时清掉——提示让用户回列表重新打开那张表，
  // 所以回列表时要留着（用户裁决保留面板顶部这一位置）。
  const [deleteNotice, setDeleteNotice] = useState<string | null>(null);
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
  // 选中表的「最后一次写入值」：与每处 setSelectedTableId 同步写（openTable / backToList /
  // notebook 复位 effect，三处之外不得再写选中表）。同一批 effect 里先复位再跳转时，
  // 闭包里的 selectedTableId 还是渲染时的旧值，只有它能告诉 openTable「选中已被清过」；
  // 异步命令落地时也靠它判断用户此刻还在不在发起命令的那张表上（S6）。
  const selectedTableIdRef = useRef<string | null>(null);
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
  // 编辑态」），见 knowhow-table-grid.tsx 里 KnowhowTableGrid 的
  // `opensRowDrawer = index === 0 && filled` 判定。三态共用同一份 useFloatingWindow storageKey / useFullscreen
  // Toggle 键（见 knowhow-cell-editor.tsx FULLSCREEN_STORAGE_KEY 头注释），切
  // 页签时浮窗位置/尺寸不跳动。
  const [cellModal, setCellModal] = useState<
    { rowId: string; columnId: string; mode: "preview" | "edit" | "history" } | null
  >(null);

  // Task 11（代码附件）：当前打开的代码查看/编辑浮层——与 cellModal/
  // optimizeRowId 平级的另一个顶层 modal 状态，堆叠在行详情抽屉之上。
  const [codeModal, setCodeModal] = useState<{ rowId: string; columnId: string } | null>(null);

  // stale-response guard（收尾修复）：loadDetail 与 loadRowCode（在
  // use-knowhow-row-code.ts，同一套写法）都可能被快速
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

  // 只跟 selectedTableId 走，不跟 loadDetail 的身份走：loadDetail 只随 notebookId
  // 变，而 notebookId 一变，下方 notebook 复位 effect 必然把 selectedTableId 清空——
  // 若这里也依赖 loadDetail，它在复位之前执行、看到的还是旧表 id，会对新笔记本多发
  // 一次注定被丢弃的旧表详情请求。切笔记本时作废 in-flight 旧详情的那一次 bump
  // 由复位 effect 自己做（同一次提交，时机与改动前相同）。
  useEffect(() => {
    if (selectedTableId) loadDetail(selectedTableId);
    return () => {
      detailRequestRef.current += 1; // 切表/回列表/卸载：作废 in-flight 的旧表详情
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedTableId]);

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

  // 行代码 owner：调用位置就是原「按 openRowId 拉行代码」effect 的位置（拉表详情
  // 的 effect 之后、下方 notebook 复位 effect 之前），见文件内三个 hook 的总说明。
  const knowhowRowCode = useKnowhowRowCode(openRowId);

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
  // 同表重开时发现引用的行不在手头的 detail 里、已发起重拉（S6）：记下这份将被替换的
  // detail。路由 effect 与陈旧引用 effect 和跳转 effect 在同一次 flush 里执行，闭包里
  // 还是它——按它路由会走错分支、按它判「行未找到」会误报，所以两者都跳过这份对象，
  // 等重拉回来的新 detail 再判。
  const supersededDetailRef = useRef<KnowhowTableDetail | null>(null);

  // notebook 切换时(理论上面板会被 page.tsx 一并卸载，这里仅作兜底)整体复位，
  // 避免残留上一个 notebook 的表选中态。
  useEffect(() => {
    detailRequestRef.current += 1; // 作废上一个笔记本 in-flight 的表详情（拉详情 effect 不再因 notebookId 重跑）
    // 同一次 flush 里路由 / 陈旧引用 effect 的闭包还是上一个笔记本的 detail：同 id 的表被
    // 重新选回时（见 openTable）别按它判，等新笔记本的 detail 到了再判。
    supersededDetailRef.current = detail;
    selectedTableIdRef.current = null;
    setSelectedTableId(null);
    setDetail(null);
    setDetailError(null);
    setActionError(null);
    setQuery("");
    setOpenRowId(null);
    setCellModal(null);
    knowhowConceptMatrix.setOpenConceptValue(null); // Task 8：与 openTable/backToList 同理一并清空
    knowhowConceptMatrix.setHighlightRowId(null); // Task 11：与 openConceptValue 并列一并清空
    setConfirmDelete(false);
    setManageOpen(false);
    setTransferOpen(false); // C3：与 manageOpen 同级的顶层 modal 状态，一并清空
    setImportOpen(false); // 导入向导按 notebookId 上传：留着会把上一个笔记本里开始的导入交给新笔记本（S6）
    setCreateOpen(false);
    setAppendOpen(false);
    setOptimizeRowId(null);
    setCompleteRowId(null);
    setReformatRowId(null);
    setReformatTableOpen(false);
    setCodeModal(null);
    setHistoryOpen(false); // Task 15：与 manageOpen/transferOpen 同级的顶层 modal 状态，一并清空
    // 两条提示都说的是上一个笔记本里的表（复制结果 / 陈旧引用），换笔记本后一并清掉（S6）。
    setTransferNotice(null);
    setDeleteNotice(null);
    setJumpNotice(null);
    // deleting 刻意不清（openTable/backToList 同样不清）：删表请求落地才解锁，见 confirmDeleteTable。
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
    openTable(initialTableId, initialRowId);
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
  //
  // detail 必须是这次跳转目标表的（S6）：面板已开着一张表时目标换了，本 effect 与上面
  // 的跳转 effect 在同一次提交里执行，读到的还是旧表的 detail——按它路由会把记录型旧表
  // 的判定套到新目标上（例如本该开概念矩阵却开了行抽屉），还会把目标标成已路由。不是
  // 目标表就先不动，等目标表的 detail 到了再路由一次（用户中途手动换到别的表时同理）。
  useEffect(() => {
    if (!initialRowId || !detail) return;
    if (detail.id !== initialTableId) return;
    if (detail === supersededDetailRef.current) return; // 正在重拉，见声明处注释
    if (jumpRoutedRef.current === initialRowId) return;
    jumpRoutedRef.current = initialRowId;
    if (detail.anchorColumnId) {
      const group = groupRowsByAnchor(detail.rows, detail.anchorColumnId)
        .find((g) => g.rows.some((r) => r.id === initialRowId));
      if (group && group.anchorValue) {
        knowhowConceptMatrix.setOpenConceptValue(group.anchorValue);
        knowhowConceptMatrix.setHighlightRowId(initialRowId);
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
    if (detail === supersededDetailRef.current) return; // 正在重拉，见声明处注释（失败仍走上面的回退）
    if (detail && initialRowId && !detail.rows.some((row) => row.id === initialRowId)) {
      setJumpNotice("引用指向的行未找到，可能已被删除或调整，已为你展开整表内容。");
    }
  }, [detail, detailError, initialRowId, initialTableId, selectedTableId]);

  function openTable(tableId: string, citedRowId?: string | null) {
    // 重开的就是当前这张表（引用跳转换到本表的另一行，S6）：拉详情 effect 只跟
    // selectedTableId 走，id 不变就不会重拉，所以保留现有 detail / detailError，让路由
    // effect 直接按它定位新目标。此前这里一律清空 detail，网格一直空白到用户点返回。
    // 「当前」以 selectedTableIdRef 为准：同一批里 notebook 复位刚清过选中、跳转又选回
    // 同一个 id 时，那是另一个笔记本里的表，必须当成换表。
    const reopening = tableId === selectedTableIdRef.current;
    if (!reopening) {
      setDetail(null);
      setDetailError(null);
      // 渲染时的选中值恰好就是这个 id（同一批先清空再选回）：两次提交之间 selectedTableId
      // 没变，拉详情 effect 不会重跑，只能在这里拉。正常换表时两者不等，照旧交给 effect。
      if (tableId === selectedTableId) loadDetail(tableId);
    } else if (citedRowId && detail && !detail.rows.some((row) => row.id === citedRowId)) {
      // 同表重开、但引用的行不在手头的 detail 里（可能是加载之后才新增的行）：重拉一次再
      // 判定。行在手头时零开销；detail 还在加载或已失败时也不重拉（在途请求 / 陈旧引用
      // effect 各自接住）。
      supersededDetailRef.current = detail;
      loadDetail(tableId);
    }
    selectedTableIdRef.current = tableId;
    setSelectedTableId(tableId);
    setActionError(null);
    setTransferNotice(null);
    setDeleteNotice(null);
    setQuery("");
    setOpenRowId(null);
    setCellModal(null);
    // Task 8：openConceptValue 是纯按字符串匹配 detail 里的 anchor 分组
    // （见 use-knowhow-concept-matrix.ts 里 openConceptGroup 的 useMemo），
    // 不像 cellModal/codeModal 那样带
    // rowId 归属校验——不清掉的话，切到另一张表后一旦该表也凑巧有同名概念，
    // 矩阵抽屉会在用户没点任何东西的情况下自己弹出来（openConceptGroup 只是
    // 普通 useMemo，detail 一变就重算，不经用户交互）。与其余顶层 modal 状态
    // 一起在切表/返回列表时清空。
    knowhowConceptMatrix.setOpenConceptValue(null);
    knowhowConceptMatrix.setHighlightRowId(null); // Task 11：与 openConceptValue 并列一并清空
    setConfirmDelete(false);
    setManageOpen(false);
    setTransferOpen(false); // C3：与 manageOpen 同级的顶层 modal 状态，一并清空
    setAppendOpen(false);
    setOptimizeRowId(null);
    setCompleteRowId(null);
    // 规整批量弹窗作用于上一张表的快照（行作用域还按行 id 反查，换表后同 id 的行会
    // 被错认）：与其余顶层 modal 一起清空（S6，此前切表/重新跳转会把它带到新表上）。
    setReformatRowId(null);
    setReformatTableOpen(false);
    setCodeModal(null);
    setHistoryOpen(false); // Task 15：与 manageOpen/transferOpen 同级的顶层 modal 状态，一并清空
  }

  // 与 openTable 清同一组表级状态（S6 起两者对齐）。刻意不清 jumpNotice：引用的表
  // 加载失败时，陈旧引用 effect 先写提示再调本函数回到列表，提示必须留下。
  function backToList() {
    selectedTableIdRef.current = null;
    setSelectedTableId(null);
    setDetail(null);
    setDetailError(null);
    setActionError(null);
    setTransferNotice(null);
    // 刻意不清 deleteNotice：它让用户回列表重新打开那张表，回列表时还得看得见。
    setQuery("");
    setOpenRowId(null);
    setCellModal(null);
    knowhowConceptMatrix.setOpenConceptValue(null);
    knowhowConceptMatrix.setHighlightRowId(null); // Task 11：与 openConceptValue 并列一并清空
    setConfirmDelete(false);
    setManageOpen(false);
    setTransferOpen(false); // C3：与 manageOpen 同级的顶层 modal 状态，一并清空
    setAppendOpen(false);
    setOptimizeRowId(null);
    setCompleteRowId(null);
    setReformatRowId(null);
    setReformatTableOpen(false);
    setCodeModal(null);
    setHistoryOpen(false); // Task 15：与 manageOpen/transferOpen 同级的顶层 modal 状态，一并清空
  }

  async function confirmDeleteTable() {
    if (!selectedTableId) return;
    const deletedTableId = selectedTableId;
    // 表详情加载失败或还在加载时也能删表：标题先取 detail，取不到再按 id 去清单里找。
    const deletedTitle = detail?.title
      || knowhowTableList.view.tables?.find((table) => table.id === deletedTableId)?.title
      || "";
    setDeleting(true);
    setActionError(null);
    try {
      await deleteKnowhowTable(notebookId, deletedTableId);
      // 删除落地才解禁：与 backToList() 写在同一个同步块里，React 自动批处理成一次
      // 提交，网格离场与解禁同时发生，中间不会出现可点的「确认删除」。不放进
      // backToList()/openTable：删除进行中「返回」再开表时，按钮必须仍禁用到请求
      // 落地，否则可以对同一张表再发一次删除。
      setDeleting(false);
      // 只有用户此刻还在被删的这张表上才回列表（S6）：删除进行中返回、又打开了别的表时，
      // 留在那张表上，只刷新清单。「此刻」读 selectedTableIdRef，闭包里是发起时的值。
      if (selectedTableIdRef.current === deletedTableId) backToList();
      knowhowTableList.loadTables();
    } catch {
      setDeleting(false);
      if (selectedTableIdRef.current === deletedTableId) {
        setActionError("删除失败，请重试");
        setConfirmDelete(false);
      } else {
        setDeleteNotice(deletedTitle
          ? `「${deletedTitle}」没有删除成功，请重新打开这张表再删一次。`
          : "有一张表没有删除成功，请重新打开它再删一次。");
      }
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
      knowhowTableList.loadTables();
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
    knowhowConceptMatrix.setConceptDrawerError(null);
    try {
      const newRow = await addKnowhowRow(notebookId, selectedTableId, {
        cells: { [detail.anchorColumnId]: anchorValue },
      });
      setDetail((prev) => (prev ? { ...prev, rows: appendRowOptimistically(prev.rows, newRow) } : prev));
      loadDetail(selectedTableId);
      knowhowTableList.loadTables();
    } catch (err) {
      // 复审修复：同一条错误文案双写——actionError 保留既有渠道（关闭抽屉后
      // 主网格横幅可见），conceptDrawerError 供抽屉在打开期间就地显示（见
      // KnowhowMatrixDrawer 的 error prop）。
      const message = extractErrorMessage(err, "添加分支失败，请重试");
      setActionError(message);
      knowhowConceptMatrix.setConceptDrawerError(message);
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
      knowhowTableList.loadTables();
      openCellEdit(newRow.id, detail.anchorColumnId);
    } catch (err) {
      setActionError(extractErrorMessage(err, "添加概念失败，请重试"));
    } finally {
      setAddingRow(false);
    }
  }

  // 管理 modal 里任一写操作成功：重拉表详情（modal 拿到新 detail prop 原地
  // 刷新）+ 表列表（标题/行数在列表卡片上要跟着变）。
  function handleManageChanged() {
    if (selectedTableId) loadDetail(selectedTableId);
    knowhowTableList.loadTables();
  }

  // knowhow 表版本管理 Task 15：历史抽屉「回到这里」回退成功后的回调——重拉
  // 当前表详情（行/格内容、列结构、行标题列都可能被回退改动）+ 表清单（行数/
  // 最近活动时间同样可能变化）。镜像 handleManageChanged 的既有写法；抽屉自己
  // 只重拉历史时间线，不知道也不需要知道 detail 的形状。
  function handleReverted() {
    if (selectedTableId) loadDetail(selectedTableId);
    knowhowTableList.loadTables();
  }

  // 网格表头列名 inline 改名：双击列名进入编辑框（见 knowhow-table-grid.tsx），
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
      knowhowTableList.loadTables();
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
  // 且格子为空时什么也不做——只读成员看不到可点的空格（见 knowhow-table-grid.tsx
  // 里 KnowhowTableGrid 的 clickable 判定），这里的 !canEdit 分支只是防御性兜底。
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
  // 这同一个函数（见 knowhow-row-optimize-modal.tsx 的 Props 注释），必须对任意
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
    // onAcceptCell 也走本函数，同样受益）。loadRowCode（use-knowhow-row-code.ts）
    // 自带请求号 guard，与
    // 快速切行/连续保存并存时旧响应不会倒灌。
    if (openRowId && targets.includes(openRowId)) knowhowRowCode.loadRowCode(openRowId);
  }

  // Task 11：打开代码浮层（抽屉 chip 的 onOpen，及"添加代码"安静入口共用同
  // 一个打开函数——两者的区别只在于 KnowhowCodeModal 内部按 code.status 是
  // 否为 none 决定初始 mode 是 view 还是 edit，本函数不需要关心这一点）。
  function openCodeModal(rowId: string, columnId: string) {
    setCodeModal({ rowId, columnId });
  }

  // 行抽屉的关闭回调身份稳定（S6）：抽屉的 window Esc 监听依赖 [onClose, cellModalOpen]，
  // 此前这里传内联箭头，面板每渲染一次监听就拆了重挂一次。现在只在 cellModalOpen 翻转时
  // 重挂——正是叠放弹窗开/关的那次提交，监听仍按「抽屉先、其上的弹窗后」排队，Esc 只关
  // 最上层的语义不变（抽屉在有弹窗时短路，捕获的仍是那次渲染的 cellModalOpen）。
  const closeRowDrawer = useCallback(() => setOpenRowId(null), []);

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

  // 概念矩阵 owner：必须是 return 之前最后一个 hook 调用，让「切概念/关抽屉时复位
  // 删除确认态」的 effect 仍在面板所有 effect 之后执行（搬家前它就在这个位置）。
  // 上方 openTable / backToList / notebook 复位 / 引用跳转 / addBranch 对
  // `knowhowConceptMatrix` 的引用都在回调或 effect 体里，渲染之后才执行，不会撞上
  // 暂时性死区。
  const knowhowConceptMatrix = useKnowhowConceptMatrix({
    notebookId,
    selectedTableId,
    detail,
    effects: { loadDetail, loadTables: knowhowTableList.loadTables, reportActionError: setActionError },
  });

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

      {deleteNotice && (
        <div className="knowhow-jump-notice knowhow-jump-notice--error" role="alert">
          <span>{deleteNotice}</span>
          <button type="button" onClick={() => setDeleteNotice(null)} title="关闭">
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
            tables={knowhowTableList.view.visibleTables}
            hasUnfilteredTables={(knowhowTableList.view.tables?.length ?? 0) > 0}
            loading={knowhowTableList.view.tablesLoading}
            error={knowhowTableList.view.tablesError}
            canEdit={canEdit}
            healthFilter={knowhowTableList.view.healthFilter}
            onHealthFilterChange={knowhowTableList.setHealthFilter}
            onRetry={knowhowTableList.loadTables}
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
            onOpenConcept={knowhowConceptMatrix.setOpenConceptValue}
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
          onClose={closeRowDrawer}
          cellModalOpen={
            cellModal !== null || optimizeRowId !== null || completeRowId !== null || reformatRowId !== null || codeModal !== null
          }
          onOptimizeRow={() => setOptimizeRowId(openRow.id)}
          anchorColumnId={detail.anchorColumnId}
          onCompleteRow={(trigger) => openRowCompletion(openRow.id, trigger)}
          onReformatRow={() => setReformatRowId(openRow.id)}
          codeByColumn={knowhowRowCode.view.rowCodeByColumn}
          codeLoaded={knowhowRowCode.view.rowCodeLoaded}
          codeError={knowhowRowCode.view.rowCodeError}
          onRetryCode={() => knowhowRowCode.loadRowCode(openRow.id)}
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
      {knowhowConceptMatrix.view.openConceptGroup && detail?.anchorColumnId && (
        <KnowhowMatrixDrawer
          group={knowhowConceptMatrix.view.openConceptGroup}
          columns={orderColumnsForGrid(detail.columns)}
          anchorColumnId={detail.anchorColumnId}
          notebookId={notebookId}
          apiBase={apiBase}
          canEdit={canEdit}
          highlightRowId={knowhowConceptMatrix.view.highlightRowId}
          onEditCell={(rowId, columnId) => openCellAuto(rowId, columnId)}
          onCompleteRow={openRowCompletion}
          onClose={() => { knowhowConceptMatrix.setOpenConceptValue(null); knowhowConceptMatrix.setHighlightRowId(null); }}
          error={knowhowConceptMatrix.view.conceptDrawerError}
          // 挂载条件已保证 openConceptGroup 非空；`!` 只是告诉 TS（属性访问的收窄
          // 进不了闭包），运行时读的仍是本次渲染的同一个分组对象。
          onAddBranch={() => addBranch(knowhowConceptMatrix.view.openConceptGroup!.anchorValue)}
          addingBranch={addingRow}
          confirmDeleteConcept={knowhowConceptMatrix.view.confirmDeleteConcept}
          onRequestDeleteConcept={() => knowhowConceptMatrix.setConfirmDeleteConcept(true)}
          onCancelDeleteConcept={() => knowhowConceptMatrix.setConfirmDeleteConcept(false)}
          onConfirmDeleteConcept={knowhowConceptMatrix.deleteConcept}
          deletingConcept={knowhowConceptMatrix.view.deletingConcept}
          onDeleteBranch={knowhowConceptMatrix.deleteBranch}
          deletingBranchRowId={knowhowConceptMatrix.view.deletingBranchRowId}
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
            knowhowTableList.loadTables();
          }}
        />
      )}

      {createOpen && canEdit && (
        <KnowhowCreateWizard
          notebookId={notebookId}
          onClose={() => setCreateOpen(false)}
          onCreated={(created) => {
            setCreateOpen(false);
            knowhowTableList.loadTables();
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
                knowhowTableList.loadTables();
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
              knowhowTableList.loadTables();
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
            knowhowTableList.loadTables();
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
          code={resolveCellCodeView(knowhowRowCode.view.rowCodeByColumn, codeModal.columnId)}
          canEdit={canEdit}
          onSaved={knowhowRowCode.handleCodeSaved}
          onDeleted={knowhowRowCode.handleCodeDeleted}
          onClose={() => setCodeModal(null)}
        />
      )}

      <KnowhowPanelStyles />
    </section>
  );
}
