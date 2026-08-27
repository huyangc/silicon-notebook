/**
 * knowhow-cell-editor.tsx
 *
 * 格子浮窗（规格②路A + ⑤「阅读、录入、修改三条路径汇于同一个格子浮窗组件」）：
 * 已填格子先进渲染预览态（KnowhowCellPreview，长内容快速阅读入口，右上「编辑」
 * 切入编辑）；空格子/点「编辑」直接进编辑态（KnowhowCellEditor：markdown 编辑器
 * + 实时预览分栏、轻工具栏(列表/代码/图片)、图片粘贴/拖拽直传、格子级
 * localStorage 自动草稿、保存/保存并下一格(行主序)/取消、Cmd+Enter/
 * Cmd+Shift+Enter/Esc 快捷键(未保存提醒)、行上下文条(同行其他格摘要可展开)、
 * procedure 列的步骤提示）。
 *
 * knowhow-panel.tsx 持有「当前打开的是哪一格 + 预览还是编辑」这一顶层状态机
 * (cellModal)，本文件两个组件都是纯受控组件——不知道 notebookId/tableId 之外
 * 的路由细节，靠 props 里的回调 (onSave/onNavigate/onEdit/onClose) 把决策权交
 * 还给 panel；「保存并下一格」跳到哪一格也是纯函数算出来再经 onNavigate 报给
 * panel，本组件自己不做路由判断。
 *
 * KnowhowMarkdown（供预览/编辑两态共用的只读渲染，含图片鉴权 fetch）也从
 * knowhow-panel.tsx 移到本文件——它是「阅读」路径的核心，与「录入/修改」同源
 * 同款渲染管线，放在一起更贴近规格⑤的设计意图。样式（含 KnowhowMarkdown 用到
 * 的 .knowhow-image* 与本文件新增的 .kh-* 一整套）仍统一登记在
 * knowhow-panel.tsx 的顶层 `<style jsx global>`——那是本特性唯一能保证「无论
 * 预览态还是编辑态谁先渲染都已经挂载」的容器；styled-jsx 的 global 样式注入
 * 绑定「声明该 <style jsx global> 的组件是否渲染过」，若把样式分别放在
 * KnowhowCellPreview/KnowhowCellEditor 任一方，只用过另一方的会话就会看到
 * 没有样式的裸 DOM。
 *
 * 纯逻辑（草稿键/恢复决策、保存并下一格的行主序推进、textarea 光标插入原语等）
 * 都在 knowhow-cell-editor-logic.ts 里，供 knowhow-cell-editor.test.mjs 直接
 * import（本文件含 JSX，Node 原生类型剥离不支持 .tsx）。
 */
"use client";

import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ChangeEvent,
  type ClipboardEvent,
  type DragEvent,
  type KeyboardEvent as ReactKeyboardEvent,
  type MouseEvent as ReactMouseEvent,
} from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import "katex/dist/katex.min.css";
import { normalizeMathMarkdown } from "./math-markdown";
import {
  ArrowRight,
  Check,
  ChevronDown,
  Code,
  Columns2,
  Edit3,
  Eye,
  History as HistoryIcon,
  ImagePlus,
  List,
  ListChecks,
  Loader2,
  Maximize2,
  Minimize2,
  Pencil,
  Sparkles,
  X,
} from "lucide-react";
import { requestBlob } from "./api-client.ts";
import { useFloatingWindow } from "./use-floating-window.ts";
import {
  ROLE_LABELS,
  cellSummary,
  optimizeKnowhowCell,
  reformatKnowhowCell,
  rewriteAssetUrls,
  uploadNotebookAsset,
  type KnowhowColumn,
  type KnowhowTableDetail,
} from "./knowhow-model.ts";
import { orderColumnsForGrid, sortColumnsByPosition, isInternalAssetUrl } from "./knowhow-panel-logic.ts";
import { extractErrorMessage } from "./knowhow-import-logic.ts";
import {
  CANCEL_LABEL,
  CELL_REFORMAT_IDLE,
  CELL_REFORMAT_STALE,
  CLOSE_GUARD_CONTINUE_LABEL,
  CLOSE_GUARD_DISCARD_LABEL,
  CLOSE_GUARD_MESSAGE,
  DISCARD_DRAFT_LABEL,
  DRAFT_BANNER_TEXT,
  EDIT_LABEL,
  HISTORY_LABEL,
  PROCEDURE_HINT_TEXT,
  REFORMAT_SERVER_STALE_MESSAGE,
  REFORMAT_STALE_MESSAGE,
  REFORMAT_SUGGESTION_LABEL,
  REFORMAT_UNCHANGED_DISMISS_LABEL,
  REFORMAT_UNCHANGED_MESSAGE,
  RESTORE_DRAFT_LABEL,
  ROW_CONTEXT_TOGGLE_LABEL,
  SAVE_AND_NEXT_LABEL,
  SAVE_LABEL,
  TOOLBAR_CODE_LABEL,
  TOOLBAR_IMAGE_LABEL,
  TOOLBAR_LIST_LABEL,
  TOOLBAR_REFORMAT_LABEL,
  beginCellReformat,
  deriveAltFromFilename,
  dismissCellReformat,
  draftStorageKey,
  failCellReformat,
  hasUnsavedChanges,
  insertAtCursor,
  insertCodeFence,
  insertImageMarkdown,
  insertListMarker,
  insertViaExecCommandOrFallback,
  isCellReformatLoading,
  isImageFile,
  nextCellCoordinates,
  reformatArrivalNeedsParentRefresh,
  reformatCellDisabledReason,
  reformatSourceLabel,
  resolveReformatArrival,
  ruleNormalize,
  shouldNormalizePaste,
  shouldOfferDraftRestore,
  readCellDraft,
  sortRowsByPosition,
  type CellReformatState,
  type TextareaSelection,
  CELL_VIEW_MODE_STORAGE_KEY,
  VIEW_MODE_EDIT_LABEL,
  VIEW_MODE_SPLIT_LABEL,
  VIEW_MODE_PREVIEW_LABEL,
  normalizeCellViewMode,
  type CellViewMode,
  SWITCH_GUARD_MESSAGE,
  SWITCH_GUARD_DISCARD_LABEL,
  DRAFT_FLUSH_FAILED_MESSAGE,
  resolveCloseRequest,
  resolveSwitchRequest,
  draftFlushAction,
  applyDraftFlush,
  isEditorBusy,
  isSaveBlocked,
  SAVE_BLOCKED_UPLOADING_HINT,
  RESTORE_PENDING_UPLOAD_HINT,
  resolveUploadBlock,
  ACCEPT_BLOCKED_UPLOADING_HINT,
  LEAVE_WAITING_UPLOAD_HINT,
  DISCARD_UPLOAD_AND_LEAVE_LABEL,
  resolveLeaveDuringUpload,
  isAbortError,
  BUSY_HINT,
  imageMarkdown,
  resolveUploadInsertion,
  OPTIMIZE_SUGGESTION_STALE_MESSAGE,
  resolveSaveCompletion,
  type LeaveIntent,
} from "./knowhow-cell-editor-logic.ts";
import {
  ACCEPT_SUGGESTION_LABEL,
  CELL_OPTIMIZE_IDLE,
  DISCARD_SUGGESTION_LABEL,
  OPTIMIZE_CELL_BUTTON_LABEL,
  OPTIMIZE_ORIGINAL_LABEL,
  OPTIMIZE_SUGGESTION_LABEL,
  beginCellOptimize,
  dismissCellOptimize,
  failCellOptimize,
  isCellOptimizeLoading,
  optimizeCellDisabledReason,
  resolveCellOptimizeSuggestion,
  type CellOptimizeState,
} from "./knowhow-optimize-logic.ts";

// ---------------------------------------------------------------------------
// KnowhowMarkdown — 复用 answer-markdown.tsx 的 GFM+KaTeX 渲染管线（原属
// knowhow-panel.tsx，Task 7 随格子浮窗一起挪到本文件——见文件头注释）。
// ---------------------------------------------------------------------------
//
// 未直接复用 AnswerMarkdown 组件本身：它耦合了引用徽章体系（必填
// onReferenceClick、anchors/citations→referenceByCitationKey 映射、
// remarkCitations 插件），knowhow 格子内容没有引用概念，硬套空数组
// /空回调既别扭又会在格子文本恰好出现「[k1]」字样时误当引用扫描。
// 因此这里只复刻其 remark/rehype 管线本身（remarkGfm+remarkMath+
// rehypeKatex）、pre/table 的包装 class、以及 <a> 强制新标签打开
// （target="_blank" rel="noreferrer"，同 answer-markdown.tsx 的普通外链
// 分支——没有 cite: 引用徽章分支，因为本组件压根不产生 cite: 链接），
// 行为对齐、无引用负担。

export function KnowhowMarkdown({
  md,
  notebookId,
  apiBase,
  inert = false,
}: {
  md: string;
  notebookId: string;
  apiBase: string;
  /** Cross-library evidence is untrusted display text: keep Markdown layout,
   * but never create links, image requests, or active-notebook asset rewrites
   * for content whose source notebook may differ. */
  inert?: boolean;
}) {
  const content = inert ? (md ?? "") : rewriteAssetUrls(md ?? "", notebookId, apiBase);

  const components = useMemo<Components>(
    () => ({
      // 格子内容可能包含普通链接（如工具文档 URL）；不强制新标签打开的话
      // 点击会在同一个 SPA 标签页里跳走，丢失当前 notebook/knowhow 视图上下文。
      a({ href, children }) {
        if (inert) return <span>{children}</span>;
        return (
          <a href={href} target="_blank" rel="noreferrer">
            {children}
          </a>
        );
      },
      pre({ children }) {
        return <pre className="answer-code">{children}</pre>;
      },
      table({ children }) {
        return (
          <div className="answer-table-wrap">
            <table className="answer-table">{children}</table>
          </div>
        );
      },
      img({ src, alt }) {
        if (inert) return <span>{alt ? `[图片：${alt}]` : "[图片]"}</span>;
        return <KnowhowImage src={typeof src === "string" ? src : undefined} alt={alt} apiBase={apiBase} />;
      },
    }),
    [apiBase, inert],
  );

  if (!content.trim()) {
    return <p className="knowhow-empty-cell">（空）</p>;
  }

  return (
    <div className="answer-markdown knowhow-markdown">
      <ReactMarkdown remarkPlugins={[remarkGfm, remarkMath]} rehypePlugins={[rehypeKatex]} components={components}>
        {normalizeMathMarkdown(content)}
      </ReactMarkdown>
    </div>
  );
}

// 鉴权 token 只存在 localStorage，从不随 <img src> 请求自动带上（见
// knowhow-panel-logic.ts 的 isInternalAssetUrl 注释）。本站资产 URL 走
// fetch+blob 带 Authorization 头；其余来源(外链图片等)走普通 <img src>。
function KnowhowImage({ src, alt, apiBase }: { src?: string; alt?: string; apiBase: string }) {
  const needsAuth = Boolean(src) && isInternalAssetUrl(src ?? "", apiBase);
  const [blobUrl, setBlobUrl] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    if (!needsAuth || !src) return;
    let cancelled = false;
    let created: string | null = null;
    const controller = new AbortController();
    setFailed(false);
    setBlobUrl(null);
    requestBlob(src, { signal: controller.signal, tag: "knowhow" })
      .then((blob) => {
        if (cancelled) return;
        created = URL.createObjectURL(blob);
        setBlobUrl(created);
      })
      .catch(() => {
        if (!cancelled) setFailed(true);
      });
    return () => {
      cancelled = true;
      controller.abort();
      if (created) URL.revokeObjectURL(created);
    };
  }, [src, needsAuth]);

  if (!src) return null;
  if (!needsAuth) return <img src={src} alt={alt ?? ""} className="knowhow-image" />;
  if (failed) return <span className="knowhow-image-fallback">（图片加载失败{alt ? `：${alt}` : ""}）</span>;
  if (!blobUrl) return <span className="knowhow-image-loading">（图片加载中…）</span>;
  return <img src={blobUrl} alt={alt ?? ""} className="knowhow-image" />;
}

// ---------------------------------------------------------------------------
// 浮窗全屏切换（查看/编辑共用）：sessionStorage 记住本会话选择，避免用户每
// 开一格都要再点一次。跨会话不持久（不进 localStorage）——用户可能只是当次
// 阅读长内容想全屏，不代表长期偏好。
// ---------------------------------------------------------------------------

// knowhow 表版本管理 Task 16：导出给 knowhow-cell-history.tsx（格子浮窗第三态
// ——历史）复用——三态必须共用同一个 sessionStorage 键，切页签时浮窗位置/
// 全屏选择才不会跳动（预览态/编辑态两个既有消费方也是各自 mount 一份
// useFullscreenToggle 实例但读写同一个键，效果等同「共享」，见下方
// useFullscreenToggle 注释）。
export const FULLSCREEN_STORAGE_KEY = "knowhow.cellModal.fullscreen";
// 导出给 knowhow-matrix-drawer.tsx 的全屏切换按钮复用——图标/aria/title 文案
// 与格子浮窗保持逐字一致（任务要求「与格子浮窗一致」），不在那个文件另开一份
// 可能措辞漂移的拷贝。
export const FULLSCREEN_LABEL = "全屏";
export const RESTORE_SIZE_LABEL = "还原大小";
const PREVIEW_MODE_TAG = "查看";
const EDITING_MODE_TAG = "编辑中";
// knowhow 表版本管理 Task 16：格子浮窗第三态（历史）的模式徽标文案——导出给
// knowhow-cell-history.tsx 使用（PREVIEW_MODE_TAG/EDITING_MODE_TAG 两个都只
// 在本文件内部消费，故保持私有；这一个额外被另一个文件的组件用到，才需要
// export，两者的可见性差异只是"消费方是否跨文件"，不代表待遇不一致）。
export const HISTORY_MODE_TAG = "历史";

// storageKey 参数化（原为格子浮窗写死 FULLSCREEN_STORAGE_KEY）+ 导出：概念
// 矩阵抽屉（knowhow-matrix-drawer.tsx）补全屏时复用同一份实现风格（sessionStorage
// 记忆 + 图标/aria 一致），只是换一个独立的 storageKey（knowhow.conceptDrawer.
// fullscreen）——两个弹窗的全屏选择互不影响，不共用同一个会话记忆。
export function useFullscreenToggle(storageKey: string): [boolean, () => void] {
  const [fullscreen, setFullscreen] = useState<boolean>(() => {
    if (typeof window === "undefined") return false;
    try {
      return window.sessionStorage.getItem(storageKey) === "1";
    } catch {
      return false;
    }
  });
  const toggle = useCallback(() => {
    setFullscreen((current) => {
      const next = !current;
      try {
        if (typeof window !== "undefined") {
          window.sessionStorage.setItem(storageKey, next ? "1" : "0");
        }
      } catch {
        // sessionStorage 不可用（隐私模式等）时静默——只是记不住本会话选择，
        // 不影响功能本身。
      }
      return next;
    });
  }, [storageKey]);
  return [fullscreen, toggle];
}

// 编辑器视图三态（编辑/并列/预览）——sessionStorage per-session 记忆（跨会话回落
// edit），与 useFullscreenToggle 同款持久化方式但各用各的键、互不影响。仅编辑态
// 用，不导出。
function useCellViewMode(): [CellViewMode, (mode: CellViewMode) => void] {
  const [viewMode, setViewModeState] = useState<CellViewMode>(() => {
    if (typeof window === "undefined") return "edit";
    try {
      return normalizeCellViewMode(window.sessionStorage.getItem(CELL_VIEW_MODE_STORAGE_KEY));
    } catch {
      return "edit";
    }
  });
  const setViewMode = useCallback((mode: CellViewMode) => {
    setViewModeState(mode);
    try {
      if (typeof window !== "undefined") {
        window.sessionStorage.setItem(CELL_VIEW_MODE_STORAGE_KEY, mode);
      }
    } catch {
      // sessionStorage 不可用（隐私模式等）静默——只是记不住本会话选择。
    }
  }, []);
  return [viewMode, setViewMode];
}

// ---------------------------------------------------------------------------
// KnowhowRowContext — 本行其他格子分节（查看/编辑共用）：默认展开，包含当前
// 列并高亮，方便用户一眼看到当前格子在整行中的位置。
// ---------------------------------------------------------------------------

function KnowhowRowContext({
  table,
  rowId,
  currentColumnId,
  defaultOpen = true,
  onSwitchCell,
}: {
  table: KnowhowTableDetail;
  rowId: string;
  currentColumnId: string;
  defaultOpen?: boolean;
  /** 点某个非当前格条目 → 浮窗切换到那一格（保持浮窗打开）。未传入时全部
   * 条目保持纯展示（不可点）——是否接这条路径、接给哪种权限的用户，由调用方
   * （KnowhowCellPreview/KnowhowCellEditor 的调用方 knowhow-panel.tsx）决定，
   * 本组件不关心背后的权限判定。 */
  onSwitchCell?: (columnId: string) => void;
}) {
  const [open, setOpen] = useState<boolean>(defaultOpen);
  const row = table.rows.find((item) => item.id === rowId);
  const orderedColumns = useMemo(() => sortColumnsByPosition(table.columns), [table.columns]);
  if (!row) return null;
  return (
    <>
      <button
        type="button"
        className="kh-row-context-toggle"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
      >
        {ROW_CONTEXT_TOGGLE_LABEL}
        <ChevronDown size={14} className={open ? "kh-chevron-open" : ""} />
      </button>
      {open && (
        <div className="kh-row-context-body">
          {orderedColumns.length === 0 ? (
            <p className="kh-row-context-empty">这一行只有这一格。</p>
          ) : (
            orderedColumns.map((sibling) => {
              const isCurrent = sibling.id === currentColumnId;
              // 当前格已高亮、点了没意义，保持纯展示；其余格子仅在调用方接了
              // onSwitchCell 时才可点——镜像 knowhow-matrix-drawer.tsx
              // clickableCellProps 的既有 a11y 写法（role="button" + tabIndex
              // + Enter/Space 与 onClick 同一个 activate 闭包），不可点时不挂
              // 任何交互属性，保持"不可点=纯展示、不出现在 tab 顺序里"。
              const clickable = !isCurrent && Boolean(onSwitchCell);
              const activate = () => onSwitchCell?.(sibling.id);
              const interactiveProps = clickable
                ? {
                    role: "button" as const,
                    tabIndex: 0,
                    onClick: activate,
                    onKeyDown: (event: ReactKeyboardEvent<HTMLDivElement>) => {
                      if (event.key === "Enter" || event.key === " " || event.key === "Spacebar") {
                        event.preventDefault();
                        activate();
                      }
                    },
                  }
                : {};
              return (
                <div
                  key={sibling.id}
                  className={`kh-row-context-item${isCurrent ? " kh-row-context-item--current" : ""}${clickable ? " kh-row-context-item--clickable" : ""}`}
                  aria-current={isCurrent ? "true" : undefined}
                  {...interactiveProps}
                >
                  {isCurrent && <span className="kh-row-context-current-tag">当前格</span>}
                  {sibling.role !== "attribute" && (
                    <span className={`knowhow-role-badge knowhow-role-badge--${sibling.role}`}>
                      {ROLE_LABELS[sibling.role]}
                    </span>
                  )}
                  <strong>{sibling.name}</strong>
                  <span className="kh-row-context-text">
                    {cellSummary(row.cells[sibling.id] ?? "", 120) || "（空）"}
                  </span>
                </div>
              );
            })
          )}
        </div>
      )}
    </>
  );
}

// ---------------------------------------------------------------------------
// KnowhowCellPreview — 格子浮窗预览态（规格②路A「已填格子先进渲染预览态」+
// ⑤「单格长内容的快速阅读入口」）：只读渲染 + 右上「编辑」(canEdit) + 关闭。
// ---------------------------------------------------------------------------

export interface KnowhowCellPreviewProps {
  /** 行标题面包屑文本（resolveRowTitleText/合成兜底，由 panel 统一算好传入，
   * 保证与抽屉/网格显示的行标题完全一致）。 */
  rowTitle: string;
  column: KnowhowColumn;
  contentMd: string;
  notebookId: string;
  apiBase: string;
  canEdit: boolean;
  onEdit: () => void;
  onClose: () => void;
  /** knowhow 表版本管理 Task 16：切到格子浮窗第三态（历史）——与 onEdit 同级
   * 的顶层入口，但不受 canEdit 门控（规格⑦「只读成员看得到历史」，只有历史
   * 页签内部的「恢复此版本」按钮才按 canEdit 收紧，见 knowhow-cell-
   * history.tsx）。 */
  onHistory: () => void;
  /** 整表详情——用来渲染「本行其他格子」定位区（含当前列高亮）。仅读，本
   * 组件不改。传入后浮层就能显示行内其他列的摘要，用户一眼看到当前格子在
   * 整行的哪个位置。可选（个别调用方 table 尚未加载完全时不传，此时不显示
   * 定位区）。 */
  table?: KnowhowTableDetail;
  rowId?: string;
  /** 「本行其他格子」条目可点击切换——透传给 KnowhowRowContext，未传入时该
   * 区保持纯展示。由调用方（knowhow-panel.tsx）决定是否接（如仅 canEdit
   * 用户可接，见该文件调用处注释），本组件自己不做权限判断。 */
  onSwitchCell?: (columnId: string) => void;
}

export function KnowhowCellPreview({
  rowTitle,
  column,
  contentMd,
  notebookId,
  apiBase,
  canEdit,
  onEdit,
  onClose,
  onHistory,
  table,
  rowId,
  onSwitchCell,
}: KnowhowCellPreviewProps) {
  const [fullscreen, toggleFullscreen] = useFullscreenToggle(FULLSCREEN_STORAGE_KEY);
  // 查看态/编辑态共用同一个浮窗身份（storageKey 相同）——切换模式时（点
  // 「编辑」/关闭编辑回到查看，见 knowhow-panel.tsx 的 cellModal 状态机）
  // 拖动/resize 记住的位置和尺寸不该跳位置，两态各自 mount 一份 hook 实例但
  // 读写同一个 sessionStorage 键，效果等同「共享」。全屏时禁用拖动/resize，
  // 交给 .kh-modal-card--fullscreen 的 CSS 完全接管尺寸/位置。
  const floating = useFloatingWindow({ storageKey: "knowhow.cellModal.window", disabled: fullscreen });
  useEffect(() => {
    function handleKeyDown(event: globalThis.KeyboardEvent) {
      if (event.key === "Escape") onClose();
    }
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [onClose]);

  // 背景点击关闭（镜像 knowhow-import.tsx 的 handleBackdropClick /
  // knowhow-manage.tsx 的 ManageModalShell 既有习语）：只读预览态没有未保存
  // 内容的概念，点背景直接关闭，不需要经过任何确认层。
  function handleBackdropClick(event: ReactMouseEvent<HTMLDivElement>) {
    if (event.currentTarget === event.target) onClose();
  }

  return (
    <div className="kh-modal-overlay" onClick={handleBackdropClick}>
      <div
        ref={floating.cardRef}
        className={`kh-modal-card kh-modal-card--preview${fullscreen ? " kh-modal-card--fullscreen" : ""}`}
        style={floating.style}
        role="dialog"
        aria-modal="true"
        aria-label={`${rowTitle} › ${column.name}`}
        onClick={(event) => event.stopPropagation()}
      >
        <header className="kh-modal-header" {...floating.dragHandleProps}>
          <div className="kh-modal-header-top">
            <div className="kh-modal-breadcrumb">
              <span className="kh-modal-row-title" title={rowTitle}>
                {rowTitle}
              </span>
              <span className="kh-modal-sep">›</span>
              <span className="kh-modal-col-name">{column.name}</span>
              {column.role !== "attribute" && (
                <span className={`knowhow-role-badge knowhow-role-badge--${column.role}`}>
                  {ROLE_LABELS[column.role]}
                </span>
              )}
              <span className="kh-mode-tag">
                <Eye size={11} /> {PREVIEW_MODE_TAG}
              </span>
            </div>
            <div className="kh-modal-header-actions">
              {canEdit && (
                <button type="button" className="kh-preview-edit-button" onClick={onEdit}>
                  <Edit3 size={14} /> {EDIT_LABEL}
                </button>
              )}
              {/* knowhow 表版本管理 Task 16：不受 canEdit 门控——只读成员也该看得到
                  这一格的历史（规格⑦），只是历史页签内部的「恢复此版本」按钮才收紧。 */}
              <button type="button" className="kh-preview-edit-button" onClick={onHistory}>
                <HistoryIcon size={14} /> {HISTORY_LABEL}
              </button>
              <button
                type="button"
                className="icon-button"
                title={fullscreen ? RESTORE_SIZE_LABEL : FULLSCREEN_LABEL}
                aria-label={fullscreen ? RESTORE_SIZE_LABEL : FULLSCREEN_LABEL}
                onClick={toggleFullscreen}
              >
                {fullscreen ? <Minimize2 size={16} /> : <Maximize2 size={16} />}
              </button>
              <button type="button" className="icon-button" title="关闭" onClick={onClose}>
                <X size={18} />
              </button>
            </div>
          </div>
          {table && rowId && (
            <KnowhowRowContext
              table={table}
              rowId={rowId}
              currentColumnId={column.id}
              onSwitchCell={onSwitchCell}
            />
          )}
        </header>
        <div className="kh-modal-body kh-preview-body">
          <KnowhowMarkdown md={contentMd} notebookId={notebookId} apiBase={apiBase} />
        </div>
        {!fullscreen && <span className="kh-modal-resize-handle" aria-hidden="true" {...floating.resizeHandleProps} />}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// KnowhowCellEditor — 格子浮窗编辑态（规格②路A 全部要点）。
// ---------------------------------------------------------------------------

export interface KnowhowCellEditorProps {
  notebookId: string;
  apiBase: string;
  /** 整表详情——用来算「本行其他格子」摘要与「保存并下一格」的行主序目标。
   * 只读，本组件不直接改它；改动经 onSave 回报给 panel 后由 panel 合并回
   * 它自己的 detail 状态。 */
  table: KnowhowTableDetail;
  rowId: string;
  columnId: string;
  rowTitle: string;
  /** 该格所属概念组的分支数——仅当这一格是「合并共享格」（anchor 分组内该列
   * 全分支同值、组内多于一行，见 knowhow-grouping-logic.ts 的
   * isSharedColumn）时由 panel 传入且 >1；用于 header 提示改动会同步到整组。
   * undefined 或 <=1（记录型表 / 非共享格 / 单行组）时不显示提示。 */
  affectedBranchCount?: number;
  /** 落库；panel 负责真正调用 patchKnowhowCell + 把结果合并回 detail 状态。
   * 若该格是合并共享格，panel 会批量写整组、不只是这一行（同一份
   * affectedBranchCount 判定，保证提示与实际写入范围一致）。reject 时本组件
   * 在原地展示错误，不关闭浮窗（用户可以重试）。 */
  onSave: (rowId: string, columnId: string, contentMd: string) => Promise<void>;
  /** 「保存并下一格」算出下一格坐标后，把「该切到哪一格、停留在编辑态」的
   * 决定报给 panel——panel 更新 cellModal 状态，本组件靠 key 变化重新挂载。 */
  onNavigate: (rowId: string, columnId: string) => void;
  onClose: () => void;
  /** knowhow 表版本管理 Task 16：切到格子浮窗第三态（历史）——编辑态能挂载
   * 到画面上本就证明 canEdit 为真（见本文件其余「编辑态无需再判 canEdit」的
   * 既有取向），故不需要像预览态那样额外说明"不受 canEdit 门控"。
   *
   * codex 第 6 轮 P2：编辑态的 History 按钮**经 performLeave({kind:"history"})**
   * 走与 Esc/关闭同一套 commitLeave 草稿落盘守卫，不再直调本回调。早前直调的
   * 理由是「卸载兜底会把未落盘文字写进草稿、不会丢」——但那条 mountedRef 清理
   * effect 调 flushDraft **忽略了返回值**，localStorage 满/不可用时草稿没写成、
   * 编辑器照样卸载、文字静默丢失，与「refuses to leave if that write fails」这条
   * 不变量相悖。经 commitLeave 后：落盘成功即打开历史（无二次确认，纯粹切页签），
   * 失败则首次拦下+DRAFT_FLUSH_FAILED 警告、二次强制放行（存储坏了也不把用户
   * 永久关在弹窗里）。预览态的 History 按钮没有未保存文字，仍直调本回调。 */
  onHistory: () => void;
  /** 「本行其他格子」条目可点击切换——透传给 KnowhowRowContext，未传入时该
   * 区保持纯展示。编辑态本身已隐含 canEdit（能进到这一态就说明有写权限），
   * panel 侧无需再额外按 canEdit 判断是否传入，见该文件调用处注释。 */
  onSwitchCell?: (columnId: string) => void;
  /** F（review）：规整到达解析判 server-stale（他人在本编辑器打开后又保存过这一格）时
   * 回调——请父级重取整表 detail，把陈旧的 savedContent 快照换新，否则关掉重开撞同一
   * server-stale 态、永远循环（判定见 knowhow-cell-editor-logic.ts 的
   * reformatArrivalNeedsParentRefresh）。可选：未接线的调用方（测试/预览）省略即无副作用。 */
  onServerStale?: () => void;
}

export function KnowhowCellEditor({
  notebookId,
  apiBase,
  table,
  rowId,
  columnId,
  rowTitle,
  affectedBranchCount,
  onSave,
  onNavigate,
  onClose,
  onHistory,
  onSwitchCell,
  onServerStale,
}: KnowhowCellEditorProps) {
  const [fullscreen, toggleFullscreen] = useFullscreenToggle(FULLSCREEN_STORAGE_KEY);
  const [viewMode, setViewMode] = useCellViewMode();
  // 与 KnowhowCellPreview 共用同一个 storageKey——查看态切编辑态（反之亦然）
  // 是同一个浮窗身份的两种渲染，不该在切换模式时跳位置/跳尺寸，见该组件
  // 对应注释。
  const floating = useFloatingWindow({ storageKey: "knowhow.cellModal.window", disabled: fullscreen });
  // panel 只在 row/column 都存在时才会挂载本组件（见 knowhow-panel.tsx 的
  // cellModalRow/cellModalColumn 守卫），加上 key={rowId+columnId} 保证切格
  // 时整个组件重新挂载——这里的非空断言在该前提下是安全的。
  const row = table.rows.find((item) => item.id === rowId)!;
  const column = table.columns.find((item) => item.id === columnId)!;
  const savedContent = row.cells[columnId] ?? "";

  const [content, setContent] = useState<string>(savedContent);
  // 草稿扫描在**首次渲染期间同步完成**，不是 useEffect。被动 effect 通常要等首帧
  // 绘制之后才跑，那之间界面已经可交互：任何读 showRestoreBanner/restoreBannerRef
  // 的门禁在那个窗口里都会读到假的 false（fail-open）。粘贴/拖放正是在这个窗口里
  // 能启动上传，随后 banner 才出现，用户一点「恢复」又把刚落笔的图片整段覆盖掉。
  // 放进 useState 初始化器后，banner 在**第一帧**就是终值，那个窗口不存在。
  // 只读 storage（幂等、无副作用）；清陈旧草稿是副作用，仍留在下面的 effect 里。
  // 三点说明，免得看着像违规：
  // ① **不是订阅外部可变源**（那才需要 useSyncExternalStore）——这是 mount 时读一次、
  //    立刻落进 state，此后一切以 state 为准，不存在并发渲染撕裂；初始化器在
  //    StrictMode 下被双调用也无妨（纯读、幂等）。
  // ② **不担心水合不一致**：本组件只在用户点开某一格后才渲染，服务端渲染阶段根本
  //    不存在这棵子树；`typeof window` 判断只是防御性兜底。
  // ③ **storage 属性访问也在 helper 的异常边界内**：`window.localStorage` 的属性
  //    getter 在受限环境会抛 SecurityError，故以 thunk 传入、由 readCellDraft 在自己
  //    的 try 内求值；若把 window.localStorage 直接当实参传，getter 抛错会发生在进入
  //    helper 之前、render 期崩掉整个编辑器（第 8 轮复审）。
  const [draftScan] = useState(() => {
    const stored = readCellDraft(
      () => (typeof window === "undefined" ? null : window.localStorage),
      draftStorageKey(rowId, columnId),
    );
    return { stored, offer: shouldOfferDraftRestore(stored, savedContent) };
  });
  const [draftText, setDraftText] = useState<string | null>(draftScan.offer ? draftScan.stored : null);
  const [showRestoreBanner, setShowRestoreBanner] = useState(draftScan.offer);
  const [savingMode, setSavingMode] = useState<"save" | "next" | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [pendingLeave, setPendingLeave] = useState<LeaveIntent | null>(null);
  // 「等这次上传落完就离开」——上传在飞时点关闭/取消不再强退，而是记下意图，由上传
  // 收尾自动执行（见 performLeave / uploadAndInsertImages 的 finally）。
  const [leaveAfterUpload, setLeaveAfterUpload] = useState<LeaveIntent | null>(null);
  const [dragActive, setDragActive] = useState(false);
  const [optimizeState, setOptimizeState] = useState<CellOptimizeState>(CELL_OPTIMIZE_IDLE);
  const [reformatState, setReformatState] = useState<CellReformatState>(CELL_REFORMAT_IDLE);
  // F（review）：server-stale 一旦确认就请父级刷新整表 detail（见 logic 文件
  // reformatArrivalNeedsParentRefresh 注释）。回调收进 ref，让下面的 notify effect 只依赖
  // reformatState——onServerStale 若是父级 inline 箭头会每次重渲染换 identity，直接依赖它
  // 会在仍是 server-stale 期间被无关重渲染重复触发（破坏「只触发一次」）。
  const onServerStaleRef = useRef(onServerStale);
  useEffect(() => {
    onServerStaleRef.current = onServerStale;
  }, [onServerStale]);
  // reformatState 转入 server-stale 的那一刻触发一次（CELL_REFORMAT_SERVER_STALE 是引用
  // 稳定的 singleton，reformatState 未变的重渲染 deps 不变、不重触发）；本地 stale 不触发
  // （谓词按 reason 区分——见 reformatArrivalNeedsParentRefresh）。
  useEffect(() => {
    if (reformatArrivalNeedsParentRefresh(reformatState)) onServerStaleRef.current?.();
  }, [reformatState]);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const pendingCursorRef = useRef<number | null>(null);
  // 本格编辑器是否仍挂载——异步收尾（保存返回）据此判断自己是不是「陈旧回调」：
  // 用户在保存途中关掉本格、又打开了别的格子时，本格的 key 已变、实例已卸载，
  // 此时若继续调 onNavigate/onClose 会把**后来打开的那一格**关掉或跳走（它刚输入
  // 且防抖未落盘的内容随之丢失）。组件按 key={rowId:columnId} 每格独立挂载，
  // 「已卸载」正是「这次回调已过期」的准确信号。
  const mountedRef = useRef(true);
  // 在飞上传的中断句柄。上传**绝不允许比本组件实例活得久**：卸载/放弃时 abort 掉，
  // 免得 continuation 落到已卸载的树上（图片进不了正文、资产却已在服务端落盘）。
  const uploadAbortRef = useRef<AbortController | null>(null);
  // 优化/规整共用一个请求句柄：新请求换代、确认离开或组件卸载都会断开
  // request-local stream，让后端设置同一协作取消事件。
  const modelTaskAbortRef = useRef<AbortController | null>(null);
  // 卸载清理要读到最新的落盘函数，但它自己挂在 [] 依赖的 effect 上（拿到的是首帧
  // 闭包），故经 ref 取。
  const flushDraftRef = useRef<() => boolean>(() => true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      // 父级直接卸载本组件（返回列表 / 切笔记本 / 切到别的格）时，没有任何离开路径
      // 跑过：① 掐掉在飞上传，② 同步把正文落进草稿键——300ms 自动草稿定时器会随
      // 卸载一起被 clearTimeout 掉，此刻不写就等于把这些字（含刚落笔的图片引用）
      // 从服务端和本地一起抹掉。
      uploadAbortRef.current?.abort();
      // **只写、绝不删**：清陈旧草稿是 mount 扫描与 300ms 自动草稿的职责。若这里
      // 也照 flushDraft 的完整决策去 remove，React StrictMode 开发期的「挂载 →
      // 立刻卸载 → 再挂载」会在第一次卸载时，把刚被扫描发现、用户还没决定恢复/
      // 丢弃的那份草稿直接删掉（此刻正文还等于已保存内容 → 决策正是 remove）。
      if (hasUnsavedChanges(contentRef.current, savedContentRef.current)) flushDraftRef.current();
    };
  }, []);
  // 独立于上面的上传/草稿清理状态机：它的语义和结构都有专门 AST 守卫，模型
  // request-local stream 只需在真实卸载时终止，不参与草稿决策。
  useEffect(() => () => {
    modelTaskAbortRef.current?.abort();
    modelTaskAbortRef.current = null;
  }, []);
  // 「最新值」ref：异步收尾（保存返回）读到的 content 是发起那一刻被闭包冻住的
  // payload，看不见用户在请求在飞期间又敲进去的字（textarea 保存中并不禁用）。
  // 收尾要拿实时内容去决定草稿怎么处置，只能经 ref 取。
  // 用 useLayoutEffect 而非 useEffect：被动 effect 是提交后在调度器任务里刷的，
  // 保存的 promise 收尾有可能抢在它前面读到落后一拍的 ref，把「刚敲的最后一笔」
  // 误判成没改动而清掉草稿。layout effect 在提交阶段同步跑，不留这个窗口。
  const contentRef = useRef(content);
  useLayoutEffect(() => {
    contentRef.current = content;
  }, [content]);
  const restoreBannerRef = useRef(showRestoreBanner);
  useLayoutEffect(() => {
    restoreBannerRef.current = showRestoreBanner;
  }, [showRestoreBanner]);
  // 草稿比较的基线。用 ref 而非 props 闭包：保存成功后 props.savedContent 要等父级
  // 刷新才更新，而卸载清理与上传收尾可能抢在那之前跑，拿旧基线会误判成「有未保存
  // 改动」多写一份垃圾草稿。runSave 成功后就地把基线推进到刚落库的内容。
  const savedContentRef = useRef(savedContent);
  useLayoutEffect(() => {
    savedContentRef.current = savedContent;
  }, [savedContent]);
  // 「屏幕上是否还挂着落盘失败警告」是强制离开的判据（见 commitLeave），异步收尾
  // 里也要读，故同样经 ref。
  const saveErrorRef = useRef(saveError);
  useLayoutEffect(() => {
    saveErrorRef.current = saveError;
  }, [saveError]);
  // uploading / leaveAfterUpload 的**同步**镜像：这两个值决定「现在能不能离开」，
  // 而 setState 要等下一次提交才可见。用户粘贴完立刻按 Esc（或上传恰好在点击同一
  // 刻收尾）时，读 state 会看到落后一拍的值——前者会当成「没在上传」直接放行、
  // 后者会让延后的离开没人执行。故这两处一律读写 ref。
  const uploadingRef = useRef(uploading);
  useLayoutEffect(() => {
    uploadingRef.current = uploading;
  }, [uploading]);
  const leaveAfterUploadRef = useRef(leaveAfterUpload);
  useLayoutEffect(() => {
    leaveAfterUploadRef.current = leaveAfterUpload;
  }, [leaveAfterUpload]);

  // 有异步在飞（保存 / 图片上传 / 优化表达 / 规整格式）：门控**发起类**入口（保存、
  // 切兄弟格），也是「优化表达」⇄「规整格式」互斥的唯一真源（见 isEditorBusy 注释——
  // 规整在飞必须置灰优化，反之亦然）；但刻意不门控**离开类**入口（关闭/Esc/背景/取消）
  // ——离开的安全性由 mountedRef 陈旧回调守卫保证；若连离开都禁掉，请求卡住时用户会被
  // 关在弹窗里出不来。
  const busy = isEditorBusy(
    savingMode !== null,
    uploading,
    isCellOptimizeLoading(optimizeState),
    isCellReformatLoading(reformatState),
  );
  // 保存的阻塞面比 busy 窄：优化在飞时仍允许保存（见 isSaveBlocked 注释——LLM 请求
  // 无超时，锁死存盘会让用户这段时间敲的内容存不下去）。
  const saveBlocked = isSaveBlocked(savingMode !== null, uploading);

  // 扫描本身已在首次渲染时同步做完（见上方 draftScan）；这里只做它的**副作用**部分：
  // 清掉与已保存内容相等的陈旧草稿。副作用不能放在 render 里，故留在 effect。
  // 组件靠 panel 给的 key={rowId+columnId} 保证每次切格都是全新挂载，故空依赖数组
  // 等价于「每次打开这一格都做一次」。
  useEffect(() => {
    if (draftScan.offer || draftScan.stored === null) return;
    try {
      window.localStorage.removeItem(draftStorageKey(rowId, columnId));
    } catch {
      /* ignore */
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 自动草稿：内容偏离已保存值时写入 localStorage，改回一致时清理——300ms
  // 防抖，避免逐字敲击都写一次磁盘。恢复提示还没决出胜负前暂停：否则会在
  // 用户点「恢复」之前，就把 content(仍是初始的 savedContent)===savedContent
  // 这一「暂时」的相等状态误判成「没有改动」，抢先清掉刚检测到的草稿。
  useEffect(() => {
    if (showRestoreBanner) return;
    const key = draftStorageKey(rowId, columnId);
    const timer = window.setTimeout(() => {
      try {
        if (hasUnsavedChanges(content, savedContent)) {
          window.localStorage.setItem(key, content);
        } else {
          window.localStorage.removeItem(key);
        }
      } catch {
        /* ignore（隐私模式/存储配额） */
      }
    }, 300);
    return () => window.clearTimeout(timer);
  }, [content, rowId, columnId, savedContent, showRestoreBanner]);

  // 工具栏/图片插入后，把光标移动到插入结果指定的位置——setContent 触发的
  // 重渲染完成后才能操作真实 DOM 节点，故放在 content 变化的 effect 里，靠
  // pendingCursorRef 区分「这次 content 变化是不是一次程序化插入」（普通打字
  // 不设这个 ref，effect 空转）。
  useEffect(() => {
    if (pendingCursorRef.current === null) return;
    const el = textareaRef.current;
    if (el) {
      const pos = pendingCursorRef.current;
      el.focus();
      el.setSelectionRange(pos, pos);
    }
    // 无论有没有 textarea 都要清掉：预览态下上传落笔时没有 textarea 可定位，若把
    // 偏移留在 ref 里，切回编辑态后的下一次内容变化会拿这个陈旧偏移把光标拽走。
    pendingCursorRef.current = null;
  }, [content]);

  function applyInsertion(result: { value: string; cursor: number }) {
    pendingCursorRef.current = result.cursor;
    // contentRef 同步领先一步（平时由 layout effect 在提交后同步）：批量上传是
    // 「传一张、落一张」，下一张落笔要读上一张之后的实时正文。若只等 setContent
    // 的提交，两张图接连很快传完时第二张可能读到还没提交的旧值，被判成「正文变过」
    // 而甩到末尾——顺序就乱了。
    contentRef.current = result.value;
    setContent(result.value);
  }

  function currentSelection(): TextareaSelection {
    const el = textareaRef.current;
    if (!el) return { value: content, start: content.length, end: content.length };
    return { value: content, start: el.selectionStart ?? content.length, end: el.selectionEnd ?? content.length };
  }

  // 放弃/切走/卸载前把当前内容同步落进草稿键——兜住「组件卸载抢在 300ms 自动草稿
  // 防抖之前、草稿没写成」的漏洞。动作由纯函数 draftFlushAction 定死（见其注释）：
  // write=写当前内容；keep=有待恢复旧草稿时原样保住、绝不删（否则会抢在用户点
  // 「恢复」前清掉它——与自动草稿 effect 的 showRestoreBanner 守卫同一口径）；
  // remove=清陈旧草稿。返回是否已安全落盘：write 抛错（隐私模式/配额）返 false，
  // 让执行器据此原地报错、不静默离开；keep/remove 恒真（本就没有要保存的内容，
  // 离开是安全的）。
  // 一律读 ref（正文/基线/恢复提示）而不是 props+state 闭包：本函数会被上传收尾、
  // 卸载清理这类**异步/延后**路径调用，闭包值在那一刻可能已落后好几拍，拿旧正文
  // 落盘等于把用户最后敲的字（以及刚落笔的图片引用）丢掉。也因此它对本实例是稳定
  // 的——rowId/columnId 由 key 保证一生不变。
  const flushDraft = useCallback((): boolean => {
    const live = contentRef.current;
    const action = draftFlushAction(hasUnsavedChanges(live, savedContentRef.current), restoreBannerRef.current);
    try {
      return applyDraftFlush(window.localStorage, draftStorageKey(rowId, columnId), live, action);
    } catch {
      // 连取 window.localStorage 本身都抛（极端隐私设置）：只有 write 才算失败，
      // keep/remove 本就没有要保存的内容，离开是安全的。
      return action !== "write";
    }
  }, [rowId, columnId]);
  flushDraftRef.current = flushDraft;

  const runSave = useCallback(
    async (mode: "save" | "next") => {
      // 上传未完成就保存会漏掉「还没插进正文的图片」，故上传中不许发起保存；⌘↩ 走
      // 到这里会静默无响应，所以顺手把原因显示出来。优化在飞**不**挡保存（见
      // isSaveBlocked 注释：LLM 请求可能卡很久，不能连带锁死存盘）。
      if (isSaveBlocked(savingMode !== null, uploading)) {
        if (uploading) setSaveError(SAVE_BLOCKED_UPLOADING_HINT);
        return;
      }
      setSavingMode(mode);
      setSaveError(null);
      try {
        await onSave(rowId, columnId, content);
        // 保存成功后的草稿处置——与所有离开路径共用同一个 flushDraft：
        //   · 期间没再敲字 → remove（草稿已冗余）；
        //   · 期间用户继续敲 → write 实时内容（关键！收尾马上要 onClose/onNavigate
        //     卸载组件，300ms 自动草稿定时器会被 clearTimeout 掐掉，此刻不写就等于
        //     把这些字从服务端和本地一起抹掉）；
        //   · 恢复提示还开着 → keep（那份旧草稿还没轮到我们处置）。
        // 先把基线推进到「刚落库的这份内容」，flushDraft 才判得准：props.savedContent
        // 要等父级刷新才更新，拿它当基线会把已保存内容误判成未保存改动、多写一份
        // 垃圾草稿。组件已卸载时跳过落盘：卸载清理已经同步落过，这里再动同一个键
        // 只会覆盖掉更晚的状态。
        savedContentRef.current = content;
        if (mountedRef.current) {
          // 与 commitLeave 同一条契约：草稿没落成就**不离开**。此处若照常
          // onClose/onNavigate，卸载会掐掉 300ms 自动草稿定时器，保存期间敲的字
          // 就从服务端和本地一起没了——正是本轮复审抓到的残留口子。
          if (!flushDraft()) {
            setSaveError(DRAFT_FLUSH_FAILED_MESSAGE);
            return;
          }
        }
        // 收尾动作经 resolveSaveCompletion 决策：本格已卸载（保存途中被关掉、用户
        // 又开了别的格子）时返回 none——绝不能拿本格捕获的 onNavigate/onClose 去
        // 操作后来打开的那一格。
        const orderedColumns = orderColumnsForGrid(table.columns);
        const orderedRows = sortRowsByPosition(table.rows);
        const next =
          mode === "next" ? nextCellCoordinates(orderedColumns, orderedRows, { rowId, columnId }) : null;
        const completion = resolveSaveCompletion(mode, next, mountedRef.current);
        if (completion.kind === "navigate") onNavigate(completion.rowId, completion.columnId);
        else if (completion.kind === "close") onClose();
      } catch (err) {
        setSaveError(extractErrorMessage(err, "保存失败，请重试"));
      } finally {
        setSavingMode(null);
      }
    },
    [savingMode, uploading, onSave, rowId, columnId, content, table, onNavigate, onClose, flushDraft],
  );

  // 唯一的「确认离开」执行器：所有 close/switch 路径（Esc/背景/关闭按钮、取消、
  // 切兄弟格、守卫「放弃」按钮）都经此，保存收尾那条出口也遵循同一契约。
  // 契约：先同步落草稿；落不进则**这一次不离开**——清掉守卫、原地报
  // DRAFT_FLUSH_FAILED_MESSAGE、内容留在编辑框，绝不在 UI 承诺「可恢复」的同时
  // 把内容悄悄丢掉。唯一例外是用户正看着那条警告仍再点一次离开：视为已知情的
  // 明确放弃，强制放行（否则存储不可用时无路可出，见下方 if 内注释）。
  // 「每条离开路径都先落草稿」由「只有这一个执行器」这一结构保证，而非各分支各写。
  // 真正执行离开：先同步落草稿，成功才 close/switch。上传门在它之外（见
  // performLeave）——本函数只负责「已经可以走了」这一步，故上传收尾可以直接调它
  // 提交那次被延后的离开，不必等 setUploading(false) 的重渲染。
  const commitLeave = useCallback(
    (intent: LeaveIntent) => {
      if (!flushDraft()) {
        // 第一次：拦下并说明；第二次（用户此刻正看着那条警告仍要走）：强制放行。
        // 若没有这条逃生口，存储长期不可用时所有出口都被挡死——「取消」并入统一
        // 路径后最后一个无守卫出口也没了，用户会被永久关在弹窗里。
        //
        // 判据刻意用「警告是否还挂在屏幕上」而不是一个独立 latch：latch 一旦置位
        // 就只有成功离开才会清，而 runSave / handleOptimize 都会 setSaveError(null)
        // 把警告擦掉。那样「存储坏了 → 重试保存又失败 → 点取消」会在用户看不到任何
        // 警告的情况下第一次点击就静默丢字。
        if (saveErrorRef.current !== DRAFT_FLUSH_FAILED_MESSAGE) {
          setPendingLeave(null);
          setLeaveAfterUpload(null);
          leaveAfterUploadRef.current = null;
          setSaveError(DRAFT_FLUSH_FAILED_MESSAGE);
          return;
        }
      }
      setSaveError(null);
      setPendingLeave(null);
      setLeaveAfterUpload(null);
      leaveAfterUploadRef.current = null;
      modelTaskAbortRef.current?.abort();
      modelTaskAbortRef.current = null;
      if (intent.kind === "switch") onSwitchCell?.(intent.columnId);
      else if (intent.kind === "history") onHistory();
      else onClose();
    },
    [flushDraft, onSwitchCell, onClose, onHistory],
  );
  // 上传收尾（可能晚于好几次重渲染）要调到**当前**这版 commitLeave，而不是发起
  // 上传那一帧闭包里的旧版（它捕获的 onClose/onSwitchCell 可能已经换过）。
  const commitLeaveRef = useRef(commitLeave);
  commitLeaveRef.current = commitLeave;

  // 所有「确认离开」入口的统一门：上传在飞就**延后**（记下意图，等上传落进正文后
  // 由收尾自动提交），否则立刻提交。判据读 uploadingRef 而非 uploading state——
  // 用户粘贴完立刻按 Esc 时，state 还是上一帧的 false，读它会直接放行、把这次
  // 上传甩成没人引用的孤儿。
  const performLeave = useCallback(
    (intent: LeaveIntent) => {
      const decision = resolveLeaveDuringUpload(uploadingRef.current, intent);
      if (decision.kind === "defer") {
        setPendingLeave(null);
        setLeaveAfterUpload(intent);
        // 同步置位：上传可能在本次点击的同一刻收尾，收尾读的是 ref，晚一帧就没人
        // 执行这次离开了。
        leaveAfterUploadRef.current = intent;
        return;
      }
      commitLeave(intent);
    },
    [commitLeave],
  );

  // 延后离开期间的两个出口：继续编辑（取消这次离开）、放弃上传并离开（abort 掉在飞
  // 上传，收尾里照常提交离开——已经传完并落笔的那几张仍留在正文里）。
  const cancelDeferredLeave = useCallback(() => {
    setLeaveAfterUpload(null);
    leaveAfterUploadRef.current = null;
  }, []);

  const discardUploadAndLeave = useCallback(() => {
    uploadAbortRef.current?.abort();
    // 意图已经记在 leaveAfterUploadRef 里，由上传收尾提交；若上传恰好已经收尾
    // （abort 落空），这里补一次提交，免得离开请求悬在半空。
    if (!uploadingRef.current) {
      const intent = leaveAfterUploadRef.current;
      if (intent) commitLeave(intent);
    }
  }, [commitLeave]);

  // 点「本行其他格子」的兄弟格：有异步在飞（保存/上传/优化）一律不切——continuation 可能
  // 回写已卸载组件、或让旧保存的收尾（onClose/onNavigate）落到新格（复审 #3）；
  // 此时兄弟格也已被置为不可点（见 footer 的 KnowhowRowContext onSwitchCell 门），
  // 这里再挡一道。其余按 resolveSwitchRequest 决策：有未保存改动弹守卫、无改动经
  // 执行器立刻切（顺带清旧稿）。
  const handleSwitchCell = useCallback(
    (targetColumnId: string) => {
      if (busy) return;
      const { next, leave } = resolveSwitchRequest(targetColumnId, hasUnsavedChanges(content, savedContent));
      setPendingLeave(next);
      if (leave) performLeave(leave);
    },
    [busy, content, savedContent, performLeave],
  );

  // Esc/背景/关闭按钮：按 resolveCloseRequest 决策（含「二次 Esc 强制关闭」与「切格
  // 守卫弹着时按 Esc 取消切换」两条既有习惯），需要离开时统一经执行器——故二次 Esc
  // 也会先落草稿。显式点「取消」按钮**不经过本函数**（它是用户已明确做出的放弃
  // 决定，不再二次确认），但同样经 performLeave 执行——照样同步落草稿、内容可恢复，
  // 不留一条绕过离开状态机的旁路。
  const requestClose = useCallback(() => {
    // 已经在「等上传落完就离开」状态：这次离开请求已经收下了，再按 Esc/点关闭不该
    // 叠一层守卫（那会在用户点「继续编辑」后诈尸）。此刻的两个出口就是等待条上的
    // 两个按钮。
    if (leaveAfterUploadRef.current) return;
    const { next, leave } = resolveCloseRequest(pendingLeave, hasUnsavedChanges(content, savedContent));
    setPendingLeave(next);
    if (leave) performLeave(leave);
  }, [pendingLeave, content, savedContent, performLeave]);

  useEffect(() => {
    function handleKeyDown(event: globalThis.KeyboardEvent) {
      const meta = event.metaKey || event.ctrlKey;
      if (meta && event.key === "Enter") {
        event.preventDefault();
        void runSave(event.shiftKey ? "next" : "save");
        return;
      }
      if (event.key === "Escape") {
        event.preventDefault();
        // 正在看优化对照时，Esc 先退出对照回到编辑视图，而不是直接关闭整个
        // 浮窗——用户此刻的注意力在"要不要接受这条建议"上，不在"要不要放弃
        // 整格编辑"上，两次不同意图不该共用同一次按键的最终效果。
        if (optimizeState.status === "ready") {
          setOptimizeState(dismissCellOptimize());
          return;
        }
        // 「规整格式」对照/无需改动/已失效提示同理——Esc 先退出这一层，不直接
        // 关闭整个浮窗。
        if (
          reformatState.status === "ready" ||
          reformatState.status === "unchanged" ||
          reformatState.status === "stale"
        ) {
          setReformatState(dismissCellReformat());
          return;
        }
        requestClose();
      }
    }
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [runSave, requestClose, optimizeState, reformatState]);

  function handleRestoreDraft() {
    if (draftText !== null) setContent(draftText);
    setShowRestoreBanner(false);
  }

  function handleDiscardDraft() {
    try {
      window.localStorage.removeItem(draftStorageKey(rowId, columnId));
    } catch {
      /* ignore */
    }
    setShowRestoreBanner(false);
  }

  function handleListClick() {
    applyInsertion(insertListMarker(currentSelection(), column.role === "procedure"));
  }

  function handleCodeClick() {
    applyInsertion(insertCodeFence(currentSelection()));
  }

  function handleImageButtonClick() {
    fileInputRef.current?.click();
  }

  async function uploadAndInsertImages(files: File[]) {
    const images = files.filter(isImageFile);
    if (images.length === 0) return;
    // 上传入口的统一门禁（决策见 resolveUploadBlock）。工具栏「图片」按钮虽已置灰，
    // 但 textarea 的 paste 与编辑区的 drop 不经按钮、绕得过置灰，必须在函数入口
    // **同步**再挡一次。三类拦截各自的理由：
    // ① 保存在飞：收尾会卸载本格，晚返回的上传插进已卸载的树（服务端留孤儿资产、
    //    这次粘贴无声消失）；
    // ② 恢复提示未决：「恢复」是整段 setContent(draftText)，此刻自动草稿又是暂停的，
    //    落笔的图片没有第二份记录，一点「恢复」就随整段覆盖消失；
    // ③ 优化在飞 / 另一批上传在飞：接受建议会整段改写正文、两批上传会互相错位。
    // 判据一律读**同步**的 ref，不读落后一拍的 state：粘贴与「刚点完恢复/丢弃」、
    // 「上一次粘贴」可能发生在同一帧内，读 state 会漏判。
    const uploadBlocked = resolveUploadBlock(
      savingMode !== null,
      uploading || uploadingRef.current,
      isCellOptimizeLoading(optimizeState),
      restoreBannerRef.current,
    );
    if (uploadBlocked) {
      setUploadError(uploadBlocked);
      return;
    }
    const controller = new AbortController();
    uploadAbortRef.current = controller;
    uploadingRef.current = true; // 同步置位：紧接着的离开请求必须能看见「正在上传」
    setUploading(true);
    setUploadError(null);
    // 落笔锚点：正文没变过就插回用户当初粘贴的光标处（保住「插在光标处」语义）；
    // 变过（恢复草稿等）则追加到末尾，绝不拿旧偏移往新正文里劈。每落一张就把锚点
    // 推进到这张之后，于是同一批的多张图按选择顺序连续排开。
    let anchorSnapshot = contentRef.current;
    let anchorCaret = textareaRef.current?.selectionStart ?? anchorSnapshot.length;
    try {
      for (const file of images) {
        // 一张传完就落一张（不是整批传完再一起落）：中途 abort/失败时，已经传完的
        // 那几张已经在正文里、随草稿落盘，不会变成「服务端有资产、正文没引用」的
        // 孤儿。
        const asset = await uploadNotebookAsset(notebookId, file, controller.signal);
        const landed = resolveUploadInsertion(
          anchorSnapshot,
          anchorCaret,
          contentRef.current,
          imageMarkdown(asset.id, deriveAltFromFilename(file.name)),
        );
        applyInsertion(landed);
        anchorSnapshot = landed.value;
        anchorCaret = landed.cursor;
      }
    } catch (err) {
      // 中断是用户自己点「放弃上传并离开」或组件卸载时我们主动掐的，不是错误。
      // 除了认 AbortError 这个 name，还看**我们自己的 signal 是否已中断**：并非所有
      // 浏览器在所有中断时机都抛标准的 AbortError（如响应体读取途中被掐可能报成
      // 网络错误），只按 name 判会在放弃上传后弹一条假的「图片上传失败」。
      if (!isAbortError(err) && !controller.signal.aborted) {
        setUploadError(extractErrorMessage(err, "图片上传失败，请重试"));
      }
    } finally {
      uploadAbortRef.current = null;
      uploadingRef.current = false;
      setUploading(false);
      // 「上传中不能保存」的提示过期了。
      setSaveError((current) => (current === SAVE_BLOCKED_UPLOADING_HINT ? null : current));
      // 上传期间被延后的离开：此刻图片已经落进正文，commitLeave 会把含图片引用的
      // 正文同步写进草稿再关闭/切走。直接调 commitLeave 而不是 performLeave——后者
      // 的上传门读 uploadingRef，虽已置 false，但绕开门更不容易在将来被改坏。
      const deferred = leaveAfterUploadRef.current;
      if (deferred && mountedRef.current) commitLeaveRef.current(deferred);
    }
  }

  function handleFileInputChange(event: ChangeEvent<HTMLInputElement>) {
    const files = Array.from(event.target.files ?? []);
    event.target.value = ""; // 允许连续两次选同一个文件都能触发 onChange
    void uploadAndInsertImages(files);
  }

  function handlePaste(event: ClipboardEvent<HTMLTextAreaElement>) {
    const items = event.clipboardData?.items;
    if (!items) return;
    const files: File[] = [];
    for (let i = 0; i < items.length; i += 1) {
      const item = items[i];
      if (item.kind === "file") {
        const file = item.getAsFile();
        if (file && isImageFile(file)) files.push(file);
      }
    }
    if (files.length > 0) {
      event.preventDefault();
      void uploadAndInsertImages(files);
      return;
    }
    // 剪贴板里没有图片：纯文本粘贴默认交给浏览器原生行为；只有出现**明确的
    // 列表证据**（项目符号字形，或「缩进 + 行首列表标记」的 Excel 子项形状，
    // 见 shouldNormalizePaste）才拦截并用 ruleNormalize 规整后插入。刻意不把
    // 「含 TAB」单独当作触发条件：TAB 缩进的代码（Tcl/Makefile 等）会被
    // ruleNormalize 判成散文而丢掉缩进——漏判只是让用户多点一次「规整格式」
    // 按钮（有前后对比可确认），误判却会毁掉内容，两者代价不对称，故从严。
    //
    // P2-f 插入方式（说实话版）：先试 document.execCommand("insertText")——它于
    // 光标处插入并替换选区、**加入 textarea 原生撤销栈**、触发 input 事件让受控
    // 组件的 onChange 照常更新 content（与正常打字同一条路），所以这次自动重排
    // Cmd/Ctrl+Z **撤得掉**。execCommand 已废弃、个别环境返回 false 或抛错时，
    // 回退到 applyInsertion 老路径（程序化 setContent，**不进撤销栈**、撤不掉，
    // 但粘贴仍生效）——优雅降级。决策与回退抽到 insertViaExecCommandOrFallback
    // 便于单测（此前这里承诺"撤销语义与其它程序化插入一致"，那条老路根本不进
    // 撤销栈，是空头承诺，故一并改正）。
    const text = event.clipboardData?.getData("text/plain") ?? "";
    if (!shouldNormalizePaste(text)) return;
    event.preventDefault();
    const normalized = ruleNormalize(text);
    insertViaExecCommandOrFallback(
      () => {
        const el = textareaRef.current;
        if (!el) return false;
        el.focus();
        return document.execCommand("insertText", false, normalized);
      },
      () => applyInsertion(insertAtCursor(currentSelection(), normalized)),
    );
  }

  function handleDrop(event: DragEvent<HTMLDivElement>) {
    // 无论拖进来的是不是图片都先 preventDefault：避免浏览器默认行为(部分
    // 浏览器会尝试改为导航到被拖拽的文件)。
    event.preventDefault();
    setDragActive(false);
    const files = Array.from(event.dataTransfer?.files ?? []).filter(isImageFile);
    if (files.length === 0) return;
    void uploadAndInsertImages(files);
  }

  function handleDragOver(event: DragEvent<HTMLDivElement>) {
    event.preventDefault(); // HTML5 DnD 规范：dragover 不 preventDefault 则 drop 不会触发
    setDragActive(true);
  }

  function handleDragLeave() {
    setDragActive(false);
  }

  // 「优化表达」（规格③，显式触发）：optimize_cell 读的是已保存的格子内容
  // （store 里的值），不是这里可能还没保存的 content 草稿——若两者不一致仍
  // 允许触发，用户会看到"原文"对不上自己刚打的字，分不清是不是 bug，所以
  // optimizeCellDisabledReason 在有未保存修改时就把按钮挡住，不悄悄用旧内容
  // 生成一份令人费解的建议。
  const optimizeDisabledReason = optimizeCellDisabledReason(content, savedContent, optimizeState);

  async function handleOptimize() {
    // 与按钮 disabled 的条件保持一致（按钮同时受 busy 置灰），避免键盘/编程路径
    // 绕过其中一半——与 runSave / handleSwitchCell 的「入口再查一遍」写法对齐。
    if (optimizeDisabledReason || busy) return;
    modelTaskAbortRef.current?.abort();
    const controller = new AbortController();
    modelTaskAbortRef.current = controller;
    setSaveError(null); // 清掉上一轮可能留下的「建议已失效」等提示，避免常驻红字
    setOptimizeState(beginCellOptimize);
    try {
      const result = await optimizeKnowhowCell(
        notebookId, table.id, rowId, columnId, controller.signal,
      );
      if (controller.signal.aborted || !mountedRef.current) return;
      setOptimizeState((state) => resolveCellOptimizeSuggestion(state, result.suggestionMd));
    } catch (err) {
      if (controller.signal.aborted || !mountedRef.current) return;
      setOptimizeState((state) => failCellOptimize(state, extractErrorMessage(err, "优化失败，请重试")));
    } finally {
      if (modelTaskAbortRef.current === controller) modelTaskAbortRef.current = null;
    }
  }

  // 「接受」：规格③「接受(填入编辑框)」——只把建议填进编辑框的 content 草稿，
  // 不直接调用 patchKnowhowCell；用户仍需走既有的「保存」/「保存并下一格」
  // 才会真正落库+触发重投影，与格子浮窗其余编辑路径完全一致。
  function handleAcceptSuggestion() {
    if (optimizeState.status !== "ready") return;
    // 纯防御：对照态下正文区被替换成两栏对照，没有 textarea/拖放区/文件选择框，
    // 因此建议待确认期间起不了新上传；反向也被 busy 挡住。真跑到这里说明上面某条
    // 前提被改坏了，此时宁可不接受——接受会整段改写正文，而随后落地的上传要把图片
    // 插进正文，两者互相覆盖。（注意：真发生时上传插入会让 content≠savedContent，
    // 下一次「接受」会落到下面的基线失效分支被丢弃，不是「等等再接受就行」。）
    if (uploading) {
      setSaveError(ACCEPT_BLOCKED_UPLOADING_HINT);
      return;
    }
    // 发起优化的前提是「无未保存改动」，但请求在飞期间 textarea 并没禁用。若用户
    // 此时又敲了字，这条建议的原文基线已经不是眼前的内容——直接 setContent 会把
    // 那些字覆盖掉（随后自动草稿把草稿也改写成建议内容，本地同样没了）。故不接受，
    // 丢弃这条已失效的建议并说明原因，用户的字原样留在编辑框。
    if (hasUnsavedChanges(content, savedContent)) {
      setOptimizeState(dismissCellOptimize());
      setSaveError(OPTIMIZE_SUGGESTION_STALE_MESSAGE);
      return;
    }
    setContent(optimizeState.suggestionMd);
    setOptimizeState(dismissCellOptimize());
  }

  function handleDiscardSuggestion() {
    setOptimizeState(dismissCellOptimize());
  }

  // 「规整格式」（knowhow-md-normalize Task 8，显式触发）：reformat_cell 与
  // optimize_cell 同样读的是已保存的格子内容，未保存修改时同样挡住按钮，
  // 理由与上面 optimizeDisabledReason 完全一致，见 reformatCellDisabledReason
  // 注释。此外互斥彼此的 loading 态——同一时刻只允许一条候选内容在途，避免
  // 用户连点「优化表达」和「规整格式」后，两个异步请求谁先回来就顶掉工具栏
  // 这种竞态。这条互斥**不**在这里各写一份 disabled 表达式（曾有一版 graft 残留出
  // 两个从未接线的 *ButtonDisabled 常量、却与 title 用的 busy 判定漂移），而是
  // 统一由上面的 busy 承载：busy 已同时纳入
  // isCellOptimizeLoading 与 isCellReformatLoading，两个按钮的 disabled 与 title
  // 都读它，天然一致。一旦其中一个进入 ready/unchanged，工具栏本身连同另一个按钮
  // 都不再渲染（见下方 return JSX 的分支），故只需在两者都还"看得见"的 loading
  // 阶段互斥。
  const reformatDisabledReason = reformatCellDisabledReason(content, savedContent, reformatState);

  async function handleReformat() {
    // 与按钮 disabled 一致：busy 含兄弟「优化表达」的 loading，键盘/编程路径也互斥。
    if (reformatDisabledReason || busy) return;
    modelTaskAbortRef.current?.abort();
    const controller = new AbortController();
    modelTaskAbortRef.current = controller;
    // P1-d：快照发起请求那一刻的编辑器内容。reformat_cell 读的是已保存内容，但
    // 请求在飞期间 textarea 未禁用——用户此刻敲的字会让内容偏离这份快照。到达时
    // 用 contentRef.current（实时值，非本闭包冻住的旧 content）比对，变了就丢弃这
    // 次候选、转 stale，绝不拿陈旧候选覆盖用户刚敲的字。
    const contentAtRequest = content;
    setReformatState(beginCellReformat);
    try {
      const result = await reformatKnowhowCell(
        notebookId, table.id, rowId, columnId, controller.signal,
      );
      if (controller.signal.aborted || !mountedRef.current) return;
      setReformatState((state) => resolveReformatArrival(state, contentAtRequest, contentRef.current, result));
    } catch (err) {
      if (controller.signal.aborted || !mountedRef.current) return;
      setReformatState((state) => failCellReformat(state, extractErrorMessage(err, "规整格式失败，请重试")));
    } finally {
      if (modelTaskAbortRef.current === controller) modelTaskAbortRef.current = null;
    }
  }

  // 「接受」：与「优化表达」的 handleAcceptSuggestion 同规矩——只把候选内容
  // 填进编辑框的 content 草稿（仍需手动「保存」/「保存并下一格」才真正落库），
  // 不直接调用 patchKnowhowCell。
  function handleAcceptReformat() {
    if (reformatState.status !== "ready") return;
    // F3（与 handleAcceptSuggestion 的 uploading 守卫字面对称）：图片上传在飞时
    // 不接受——接受会整段 setContent(candidateMd)，而随后落地的上传续写要把图片
    // 插进正文，两者会落在不同的整篇快照上互相错位（图片被追加到末尾/覆盖已接受
    // 内容）。规整在飞期间 textarea 未禁用，用户可粘贴图片起一个上传、再在其落地
    // 前点「接受」，故必须在此挡住（accept 按钮也 disabled={uploading}，这是键盘/
    // 编程路径的入口兜底）。真发生时上传插入会让 content≠savedContent，下一次
    // 「接受」落到下面的基线失效分支被丢弃，不是「等等再接受就行」。
    if (uploading) {
      setSaveError(ACCEPT_BLOCKED_UPLOADING_HINT);
      return;
    }
    // P1-d 第二道守卫（与 handleReformat 的到达守卫对称）：候选到达后、接受前，
    // 若当前内容已偏离已保存内容，setContent(candidateMd) 会整段覆盖掉这些字——
    // 拒绝，转同一句 stale 提示，用户的字原样留在编辑框。对照态下正文区已被两栏
    // 对照替换、正常路径打不了字，此守卫是"UI 前提被改坏"时的兜底，同
    // handleAcceptSuggestion 的防御姿态。
    if (hasUnsavedChanges(content, savedContent)) {
      setReformatState(CELL_REFORMAT_STALE);
      return;
    }
    setContent(reformatState.candidateMd);
    setReformatState(dismissCellReformat());
  }

  // 「放弃」候选 / changed=false 提示的「知道了」共用同一个收尾——两者都只是
  // 退出这一层展示、回到正常编辑视图，没有额外副作用需要区分。
  function handleDismissReformat() {
    setReformatState(dismissCellReformat());
  }

  // 背景点击关闭（镜像 knowhow-import.tsx / knowhow-manage.tsx 既有习语）：
  // 编辑态可能有未保存内容，走与 Esc/关闭按钮相同的 requestClose 守卫
  // （未保存时弹「确认放弃」而不是直接关闭），不能像预览态那样直接 onClose。
  function handleBackdropClick(event: ReactMouseEvent<HTMLDivElement>) {
    if (event.currentTarget === event.target) requestClose();
  }

  return (
    <div className="kh-modal-overlay" onClick={handleBackdropClick}>
      <div
        ref={floating.cardRef}
        className={`kh-modal-card kh-modal-card--editor${fullscreen ? " kh-modal-card--fullscreen" : ""}`}
        style={floating.style}
        role="dialog"
        aria-modal="true"
        aria-label={`${rowTitle} › ${column.name}`}
        onClick={(event) => event.stopPropagation()}
      >
        <header className="kh-modal-header" {...floating.dragHandleProps}>
          <div className="kh-modal-header-top">
            <div className="kh-modal-breadcrumb">
              <span className="kh-modal-row-title" title={rowTitle}>
                {rowTitle}
              </span>
              <span className="kh-modal-sep">›</span>
              <span className="kh-modal-col-name">{column.name}</span>
              {column.role !== "attribute" && (
                <span className={`knowhow-role-badge knowhow-role-badge--${column.role}`}>
                  {ROLE_LABELS[column.role]}
                </span>
              )}
              <span className="kh-mode-tag kh-mode-tag--editor">
                <Pencil size={11} /> {EDITING_MODE_TAG}
              </span>
              {affectedBranchCount && affectedBranchCount > 1 && (
                <span className="kh-affect-hint">改动将同步到该概念下全部 {affectedBranchCount} 个分支</span>
              )}
            </div>
            <div className="kh-modal-header-actions">
              {/* knowhow 表版本管理 Task 16（codex 第 6 轮 P2 修）：经 performLeave 走
                  与 Esc/关闭同一套草稿落盘守卫——落盘成功即打开历史，失败首次拦下+
                  警告、二次强制放行，绝不静默丢未保存文字。理由见
                  KnowhowCellEditorProps.onHistory 注释。 */}
              <button
                type="button"
                className="kh-preview-edit-button"
                onClick={() => performLeave({ kind: "history" })}
              >
                <HistoryIcon size={14} /> {HISTORY_LABEL}
              </button>
              <button
                type="button"
                className="icon-button"
                title={fullscreen ? RESTORE_SIZE_LABEL : FULLSCREEN_LABEL}
                aria-label={fullscreen ? RESTORE_SIZE_LABEL : FULLSCREEN_LABEL}
                onClick={toggleFullscreen}
              >
                {fullscreen ? <Minimize2 size={16} /> : <Maximize2 size={16} />}
              </button>
              <button type="button" className="icon-button" title="关闭" onClick={requestClose}>
                <X size={18} />
              </button>
            </div>
          </div>
          <KnowhowRowContext
            table={table}
            rowId={rowId}
            currentColumnId={columnId}
            // 有异步在飞（保存/上传/优化）时兄弟格置为不可点（onSwitchCell 传
            // undefined → KnowhowRowContext 的 clickable 判定为 false、退出 tab 顺序）：
            // 此时切走会让旧 continuation 落到新格 / 回写已卸载组件。
            onSwitchCell={busy ? undefined : handleSwitchCell}
          />
        </header>

        <div className="kh-modal-body">
          {showRestoreBanner && (
            <div className="kh-draft-banner">
              <span>{DRAFT_BANNER_TEXT}</span>
              <div className="kh-draft-banner-actions">
                <button type="button" onClick={handleRestoreDraft}>
                  {RESTORE_DRAFT_LABEL}
                </button>
                <button type="button" onClick={handleDiscardDraft}>
                  {DISCARD_DRAFT_LABEL}
                </button>
              </div>
            </div>
          )}

          {optimizeState.status === "ready" ? (
            <div className="kh-optimize-compare">
              <div className="kh-optimize-pane">
                <h5>{OPTIMIZE_ORIGINAL_LABEL}</h5>
                <div className="kh-optimize-pane-body">
                  <KnowhowMarkdown md={savedContent} notebookId={notebookId} apiBase={apiBase} />
                </div>
              </div>
              <div className="kh-optimize-pane kh-optimize-pane--suggestion">
                <h5>{OPTIMIZE_SUGGESTION_LABEL}</h5>
                <div className="kh-optimize-pane-body">
                  <KnowhowMarkdown md={optimizeState.suggestionMd} notebookId={notebookId} apiBase={apiBase} />
                </div>
              </div>
            </div>
          ) : reformatState.status === "ready" ? (
            // 「规整格式」候选就绪：与「优化表达」共用同一套 .kh-optimize-compare
            // 左右对照面板结构（任务要求"相同的 before/after compare 面板"），
            // 只是右侧标题换成 REFORMAT_SUGGESTION_LABEL（区分"规整建议"与
            // "优化建议"）并额外挂一个 source 友好文案小标签——source 是后端
            // 枚举，绝不直接渲染原始字符串，一律经 reformatSourceLabel 映射。
            <div className="kh-optimize-compare">
              <div className="kh-optimize-pane">
                <h5>{OPTIMIZE_ORIGINAL_LABEL}</h5>
                <div className="kh-optimize-pane-body">
                  <KnowhowMarkdown md={savedContent} notebookId={notebookId} apiBase={apiBase} />
                </div>
              </div>
              <div className="kh-optimize-pane kh-optimize-pane--reformat-suggestion">
                <h5>
                  {REFORMAT_SUGGESTION_LABEL}{" "}
                  <span className="kh-reformat-source-tag">{reformatSourceLabel(reformatState.source)}</span>
                </h5>
                <div className="kh-optimize-pane-body">
                  <KnowhowMarkdown md={reformatState.candidateMd} notebookId={notebookId} apiBase={apiBase} />
                </div>
              </div>
            </div>
          ) : reformatState.status === "unchanged" ? (
            // changed=false：不展示左右一模一样的空对照，改用友好提示（任务
            // 要求）。
            <div className="kh-reformat-unchanged">
              <p>{REFORMAT_UNCHANGED_MESSAGE}</p>
              <span className="kh-reformat-source-tag">{reformatSourceLabel(reformatState.source)}</span>
            </div>
          ) : reformatState.status === "stale" ? (
            // 陈旧 → 不展示会误导的候选对照，改用友好信息态（复用 unchanged 的柔和
            // 样式，非红色 error）。按 reason 选文案：local=本编辑器在飞期间被改动
            // （P1-d，用户的字仍在 content 里，「知道了」退回即可见、可续编）；
            // server=他人已在服务器改过这一格（F3，基线已旧，需关闭后重开）。
            <div className="kh-reformat-unchanged">
              <p>
                {reformatState.reason === "server"
                  ? REFORMAT_SERVER_STALE_MESSAGE
                  : REFORMAT_STALE_MESSAGE}
              </p>
            </div>
          ) : (
            <>
              {column.role === "procedure" && viewMode !== "preview" && (
                <p className="kh-procedure-hint">{PROCEDURE_HINT_TEXT}</p>
              )}

              <div className="kh-toolbar">
                {viewMode !== "preview" && (
                  <>
                    <button
                      type="button"
                      className="kh-toolbar-button"
                      title={TOOLBAR_LIST_LABEL}
                      onClick={handleListClick}
                      disabled={uploading}
                    >
                      <List size={15} /> {TOOLBAR_LIST_LABEL}
                    </button>
                    <button
                      type="button"
                      className="kh-toolbar-button"
                      title={TOOLBAR_CODE_LABEL}
                      onClick={handleCodeClick}
                      disabled={uploading}
                    >
                      <Code size={15} /> {TOOLBAR_CODE_LABEL}
                    </button>
                    <button
                      type="button"
                      className="kh-toolbar-button"
                      // 恢复提示未决时一并置灰（理由见 RESTORE_PENDING_UPLOAD_HINT）。
                      // 置灰必须配对得上的提示语，不能灰着却写「插入图片」——那是
                      // knowhow-optimize-logic.ts 声明的不变量。
                      title={showRestoreBanner ? RESTORE_PENDING_UPLOAD_HINT : TOOLBAR_IMAGE_LABEL}
                      onClick={handleImageButtonClick}
                      disabled={uploading || showRestoreBanner}
                    >
                      <ImagePlus size={15} /> {TOOLBAR_IMAGE_LABEL}
                    </button>
                    <button
                      type="button"
                      className="kh-toolbar-button kh-toolbar-button--optimize"
                      // busy 也会置灰本按钮，此时 optimizeDisabledReason 可能为
                      // null——不补这一句就会「灰着但提示语说得像可点」，破坏
                      // knowhow-optimize-logic.ts 声明的「置灰必有对得上的原因」不变量。
                      title={optimizeDisabledReason ?? (busy ? BUSY_HINT : OPTIMIZE_CELL_BUTTON_LABEL)}
                      onClick={handleOptimize}
                      disabled={optimizeDisabledReason !== null || busy}
                    >
                      {isCellOptimizeLoading(optimizeState) ? (
                        <Loader2 size={15} className="knowhow-spin" />
                      ) : (
                        <Sparkles size={15} />
                      )}
                      {isCellOptimizeLoading(optimizeState) ? "优化中…" : OPTIMIZE_CELL_BUTTON_LABEL}
                    </button>
                    <button
                      type="button"
                      className="kh-toolbar-button kh-toolbar-button--reformat"
                      // 同 optimize：busy 也会置灰本按钮，此时 reformatDisabledReason
                      // 可能为 null——不补这一句就会「灰着但提示语说得像可点」，破坏
                      // knowhow-optimize-logic.ts 声明的「置灰必有对得上的原因」不变量。
                      title={reformatDisabledReason ?? (busy ? BUSY_HINT : TOOLBAR_REFORMAT_LABEL)}
                      onClick={handleReformat}
                      disabled={reformatDisabledReason !== null || busy}
                    >
                      {isCellReformatLoading(reformatState) ? (
                        <Loader2 size={15} className="knowhow-spin" />
                      ) : (
                        <ListChecks size={15} />
                      )}
                      {isCellReformatLoading(reformatState) ? "规整中…" : TOOLBAR_REFORMAT_LABEL}
                    </button>
                    {uploading && (
                      <span className="kh-toolbar-status">
                        <Loader2 size={14} className="knowhow-spin" /> 图片上传中…
                      </span>
                    )}
                  </>
                )}
                <input
                  ref={fileInputRef}
                  type="file"
                  accept="image/*"
                  className="kh-hidden-file-input"
                  onChange={handleFileInputChange}
                />
                <div className="kh-view-switch" role="group" aria-label="视图模式">
                  <button
                    type="button"
                    className={`kh-view-switch-button${viewMode === "edit" ? " kh-view-switch-button--active" : ""}`}
                    aria-pressed={viewMode === "edit"}
                    title={VIEW_MODE_EDIT_LABEL}
                    onClick={() => setViewMode("edit")}
                  >
                    <Pencil size={14} /> {VIEW_MODE_EDIT_LABEL}
                  </button>
                  <button
                    type="button"
                    className={`kh-view-switch-button${viewMode === "split" ? " kh-view-switch-button--active" : ""}`}
                    aria-pressed={viewMode === "split"}
                    title={VIEW_MODE_SPLIT_LABEL}
                    onClick={() => setViewMode("split")}
                  >
                    <Columns2 size={14} /> {VIEW_MODE_SPLIT_LABEL}
                  </button>
                  <button
                    type="button"
                    className={`kh-view-switch-button${viewMode === "preview" ? " kh-view-switch-button--active" : ""}`}
                    aria-pressed={viewMode === "preview"}
                    title={VIEW_MODE_PREVIEW_LABEL}
                    onClick={() => setViewMode("preview")}
                  >
                    <Eye size={14} /> {VIEW_MODE_PREVIEW_LABEL}
                  </button>
                </div>
              </div>

              {uploadError && <p className="kh-inline-error">{uploadError}</p>}
              {optimizeState.status === "error" && <p className="kh-inline-error">{optimizeState.message}</p>}
              {reformatState.status === "error" && <p className="kh-inline-error">{reformatState.message}</p>}

              <div className={`kh-split kh-split--${viewMode}`}>
                {viewMode !== "preview" && (
                  <div
                    className={`kh-editor-pane${dragActive ? " kh-editor-pane--drag" : ""}`}
                    onDragOver={handleDragOver}
                    onDragLeave={handleDragLeave}
                    onDrop={handleDrop}
                  >
                    <textarea
                      ref={textareaRef}
                      className="kh-textarea"
                      value={content}
                      disabled={uploading}
                      onChange={(event) => setContent(event.target.value)}
                      onPaste={handlePaste}
                      placeholder="输入 markdown 内容…"
                    />
                  </div>
                )}
                {viewMode !== "edit" && (
                  <div className="kh-preview-pane">
                    <KnowhowMarkdown md={content} notebookId={notebookId} apiBase={apiBase} />
                  </div>
                )}
              </div>
            </>
          )}
        </div>

        <footer className="kh-modal-footer">
          {/* saveError 提到三个分支之外：它也承载「草稿落盘失败，没能离开」与「建议
              已失效」这类在守卫/优化对照分支下产生的信息，若只在最后一个分支渲染，
              用户会遇到「点了放弃却没关掉、且毫无解释」的死胡同。 */}
          {saveError && <span className="kh-inline-error">{saveError}</span>}
          {/* 上传在飞时点了离开：不强退、也不静默丢弃，而是等这次上传落进正文后
              自动离开。给两个出口——继续编辑（撤销这次离开），或明确放弃：后者
              abort 掉在飞请求（已传完并落笔的那几张仍留在正文里），不再需要向用户
              承诺任何「会尽量留着」的事。放在 pendingLeave 之前：延后期间守卫已被
              清掉，这一支就是此刻唯一该出现的东西。 */}
          {leaveAfterUpload ? (
            <div className="kh-close-guard">
              <span>{LEAVE_WAITING_UPLOAD_HINT}</span>
              <div className="kh-close-guard-actions">
                <button type="button" onClick={cancelDeferredLeave}>
                  {CLOSE_GUARD_CONTINUE_LABEL}
                </button>
                <button type="button" className="kh-danger-button" onClick={discardUploadAndLeave}>
                  {DISCARD_UPLOAD_AND_LEAVE_LABEL}
                </button>
              </div>
            </div>
          ) : pendingLeave ? (
            <div className="kh-close-guard">
              {/* 落盘失败警告挂着时不再叠一句「可恢复」——此刻再点放弃就是强制丢弃，
                  那句承诺是假的；上方已渲染的 DRAFT_FLUSH_FAILED_MESSAGE 才是实情。 */}
              {saveError !== DRAFT_FLUSH_FAILED_MESSAGE && (
                <span>{pendingLeave.kind === "switch" ? SWITCH_GUARD_MESSAGE : CLOSE_GUARD_MESSAGE}</span>
              )}
              <div className="kh-close-guard-actions">
                <button type="button" onClick={() => setPendingLeave(null)}>
                  {CLOSE_GUARD_CONTINUE_LABEL}
                </button>
                {/* 切格分支同样受 busy 门控——守卫弹着期间用户仍可能粘贴图片/触发
                    保存，此时放行切格会重演「异步收尾落到新格」。关闭分支不设门
                    （离开类入口一律留出口，见 busy 注释）。 */}
                <button
                  type="button"
                  className="kh-danger-button"
                  onClick={() => performLeave(pendingLeave)}
                  disabled={pendingLeave.kind === "switch" && busy}
                  title={pendingLeave.kind === "switch" && busy ? BUSY_HINT : undefined}
                >
                  {pendingLeave.kind === "switch" ? SWITCH_GUARD_DISCARD_LABEL : CLOSE_GUARD_DISCARD_LABEL}
                </button>
              </div>
            </div>
          ) : optimizeState.status === "ready" ? (
            <div className="kh-footer-actions">
              <button type="button" onClick={handleDiscardSuggestion}>
                {DISCARD_SUGGESTION_LABEL}
              </button>
              <button
                type="button"
                className="kh-primary-button"
                onClick={handleAcceptSuggestion}
                disabled={uploading}
                title={uploading ? ACCEPT_BLOCKED_UPLOADING_HINT : undefined}
              >
                <Check size={14} /> {ACCEPT_SUGGESTION_LABEL}
              </button>
            </div>
          ) : reformatState.status === "ready" ? (
            <div className="kh-footer-actions">
              <button type="button" onClick={handleDismissReformat}>
                {DISCARD_SUGGESTION_LABEL}
              </button>
              {/* F3：与优化「接受」按钮字面同款——上传在飞时置灰 + 对得上的 title
                  （「置灰必有对得上的原因」），接受与上传互斥（见 handleAcceptReformat）。 */}
              <button
                type="button"
                className="kh-primary-button"
                onClick={handleAcceptReformat}
                disabled={uploading}
                title={uploading ? ACCEPT_BLOCKED_UPLOADING_HINT : undefined}
              >
                <Check size={14} /> {ACCEPT_SUGGESTION_LABEL}
              </button>
            </div>
          ) : reformatState.status === "unchanged" ? (
            <div className="kh-footer-actions">
              <button type="button" className="kh-primary-button" onClick={handleDismissReformat}>
                {REFORMAT_UNCHANGED_DISMISS_LABEL}
              </button>
            </div>
          ) : reformatState.status === "stale" ? (
            // P1-d：陈旧提示的唯一出口——「知道了」退回编辑视图（内容原样保留）。
            <div className="kh-footer-actions">
              <button type="button" className="kh-primary-button" onClick={handleDismissReformat}>
                {REFORMAT_UNCHANGED_DISMISS_LABEL}
              </button>
            </div>
          ) : (
            <>
              <div className="kh-footer-actions">
                {/* 「取消」是用户已明确做出的放弃决定，不再二次确认；但仍走统一执行器
                    performLeave——同步落草稿后再关闭，未保存内容照样可恢复，不留一条
                    绕过离开状态机、不落草稿的旁路。离开类入口不受 busy 门控（见 busy
                    注释）。 */}
                <button type="button" onClick={() => performLeave({ kind: "close" })}>
                  {CANCEL_LABEL}
                </button>
                <button
                  type="button"
                  onClick={() => runSave("save")}
                  disabled={saveBlocked}
                  title={uploading ? SAVE_BLOCKED_UPLOADING_HINT : undefined}
                >
                  <Check size={14} /> {savingMode === "save" ? "保存中…" : SAVE_LABEL}
                </button>
                <button
                  type="button"
                  className="kh-primary-button"
                  onClick={() => runSave("next")}
                  disabled={saveBlocked}
                  title={uploading ? SAVE_BLOCKED_UPLOADING_HINT : undefined}
                >
                  {savingMode === "next" ? "保存中…" : SAVE_AND_NEXT_LABEL} <ArrowRight size={14} />
                </button>
              </div>
            </>
          )}
        </footer>
        {!fullscreen && <span className="kh-modal-resize-handle" aria-hidden="true" {...floating.resizeHandleProps} />}
      </div>
    </div>
  );
}
