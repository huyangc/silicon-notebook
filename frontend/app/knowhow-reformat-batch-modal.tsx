/**
 * knowhow-reformat-batch-modal.tsx
 *
 * 「一键规整整行 / 整表」批量弹窗 KnowhowReformatBatchModal 及其 props 类型，
 * 从 knowhow-panel.tsx 原样搬来（S4-c）：组件体、类型与段落注释一字未改，只把
 * 声明改为 export 供面板 import；import 只带走本组件实际用到的名字。
 *
 * 只由 KnowhowPanel 渲染（行、表两个挂载点都在面板 JSX 的原位置），所以弹窗
 * 的 DOM 叠放顺序、effect 顺序和传入回调的身份都不变。样式仍由
 * knowhow-panel-styles.tsx 的 `<style jsx global>` 提供，本文件不含 styled-jsx。
 *
 * tests/unit/knowhow-optimize.test.mjs 按 AST 钉住本组件的内部结构（runSave /
 * runBatch / requestStaleReload、snapshotRef、卸载 cleanup 里恰好一次
 * onStaleReload），本轮刻意不拆成 hook + 视图；要拆先重写那组守卫。
 */
"use client";

import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type MouseEvent as ReactMouseEvent,
} from "react";
import {
  Check,
  ChevronLeft,
  X,
} from "lucide-react";
import { fetchModelServiceStatus } from "./model-services.ts";
import { useFloatingWindow } from "./use-floating-window.ts";
import {
  cellSummary,
  fetchKnowhowTable,
  reformatKnowhowCell,
  type KnowhowTableDetail,
  type KnowhowRow,
  type KnowhowColumn,
} from "./knowhow-model.ts";
import {
  resolveRowTitleText,
} from "./knowhow-panel-logic.ts";
import { extractErrorMessage } from "./knowhow-import-logic.ts";
import { httpErrorStatus } from "./errors.ts";
import { rowFallbackTitle, reformatSourceLabel, REFORMAT_SUGGESTION_LABEL } from "./knowhow-cell-editor-logic.ts";
import { KnowhowMarkdown } from "./knowhow-cell-editor.tsx";
import { diffKnowhowMarkdown, type KnowhowDiffLine } from "./knowhow-markdown-diff.ts";
import {
  OPTIMIZE_ORIGINAL_LABEL,
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
  BATCH_REFORMAT_RETRY_LABEL,
  BATCH_REFORMAT_STALE_SKIP_TEXT,
  type ReformatBatchState,
  type ReformatBatchItem,
  type ReformatBatchScope,
  type ReformatGenerationResult,
} from "./knowhow-optimize-logic.ts";

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

export function KnowhowReformatBatchModal({
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
