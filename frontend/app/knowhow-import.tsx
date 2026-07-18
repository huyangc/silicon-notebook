/**
 * knowhow-import.tsx
 *
 * Knowhow 表导入向导：三步 modal，挂载在 knowhow-panel.tsx 的
 * `onImportClick` 挂载点上（面板持有 `importOpen` 布尔态，点击"导入表格"时
 * 渲染本组件；本组件不知道、也不关心自己被谁挂载）。
 *
 *   ① 选文件（.xlsx/.csv/.md，拖拽或点击）
 *   ② 预览 + 列设置（前 5 行预览；每列一个内容类型下拉——三项：方法步骤/
 *      工具/事物/普通，默认取后端猜测；表级「行标题列：[某列 ▾ / 不设置]」
 *      选择器带规格①提示语，预选取后端建议（无建议则不设置，无首列兜底）；
 *      表标题输入框默认=文件名去后缀；行标题 0..1 由单选选择器天然保证，
 *      不再有「恰好一列」校验）
 *   ③ 提交（进度态；后端错误中文原样展示；成功后调用 onDone 回调——由
 *      面板负责刷新表列表 + 关闭向导）
 *
 * 步骤②③在实现上共用同一屏（预览表格 + 提交按钮），"提交中"只是这屏的一个
 * 子状态（submitting=true 时输入禁用+按钮显示进度文案），而非跳转到另一个
 * 页面——避免用户在等待期间看到界面跳变、丢失已核对的列设置上下文。
 * 顶部步骤指示器仍按①②③三态展示，视觉上保留"三步"心智模型。
 *
 * 纯逻辑分两处（都无 JSX）：文件类型校验/默认标题/错误文案抽取仍在
 * knowhow-import-logic.ts；内容类型选项与提示语/猜测预选映射（legacy
 * guessed_role → 三值 kind + 行标题建议）/选择器版 payload 组装在
 * knowhow-manage-logic.ts（Task 5 起与建表向导共用，单测见
 * knowhow-manage.test.mjs）。knowhow-import-logic.ts 里被取代的
 * ROLE_OPTIONS/conceptValidationError/assembleImportColumns/canSubmitImport
 * 已注明 deprecated，等 Task 3 wire 落地后统一清理。
 *
 * 样式：namespaced `knowhow-import-*` class + `<style jsx global>`，消费
 * knowhow-panel.tsx 已经在用的同一套 CSS 变量（--panel/--ink/--line/
 * --blue/--muted/--soft/--red/--shadow），视觉上与面板本体保持一致，但不
 * 依赖面板的 style 标签是否同时挂载在 DOM 里（自成一体，谁引用谁都能独立
 * 工作）。
 */
"use client";

import { useEffect, useMemo, useRef, useState, type ChangeEvent, type MouseEvent as ReactMouseEvent } from "react";
import { ChevronLeft, Loader2, Upload, X } from "lucide-react";
import { useFloatingWindow } from "./use-floating-window.ts";
import {
  appendKnowhowCommit,
  appendKnowhowPreview,
  cellSummary,
  importKnowhow,
  importKnowhowPreview,
  type ColumnKind,
  type KnowhowAppendPreview,
  type KnowhowColumn,
  type KnowhowImportPreview,
} from "./knowhow-model.ts";
import {
  IMPORT_ACCEPT,
  IMPORT_ACCEPT_EXTENSIONS,
  deriveDefaultTitle,
  extractErrorMessage,
  computePreviewSpans,
  isBlankTitle,
  isSupportedImportFile,
} from "./knowhow-import-logic.ts";
import {
  KIND_HINTS,
  KIND_OPTIONS,
  ANCHOR_SELECTOR_LABEL,
  ANCHOR_NONE_LABEL,
  ANCHOR_SET_HINT,
  anchorHint,
  deriveImportSelection,
  assembleImportColumnKinds,
} from "./knowhow-manage-logic.ts";
import { KindLegend } from "./knowhow-manage.tsx";
import { sortColumnsByPosition } from "./knowhow-panel-logic.ts";
import {
  formatUnmatchedColumnsMessage,
  isAppendPreviewRowDuplicate,
  mapAppendDuplicatesForDisplay,
} from "./knowhow-optimize-logic.ts";

export interface KnowhowImportWizardProps {
  notebookId: string;
  /** 接受但当前不直接消费：预览格子是纯文本摘要（cellSummary 已把图片语法
   * 转成"（图示：…）"占位文案），不涉及鉴权图片渲染；保留此 prop 只是为了
   * 与 KnowhowPanel 等同族组件的 {notebookId, apiBase, ...} 签名保持一致。 */
  apiBase: string;
  /** 用户主动取消/关闭向导（未完成导入）：面板据此仅收起向导，不重新拉表。 */
  onClose: () => void;
  /** 导入成功后触发：面板据此刷新表列表并收起向导。 */
  onDone: () => void;
}

type Step = "select" | "map";

export function KnowhowImportWizard({ notebookId, onClose, onDone }: KnowhowImportWizardProps) {
  const [step, setStep] = useState<Step>("select");

  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);

  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<KnowhowImportPreview | null>(null);
  const [title, setTitle] = useState("");
  // 每列内容类型（三值，不含 anchor）+ 表级行标题列下标（null=不设置）。
  const [kinds, setKinds] = useState<ColumnKind[]>([]);
  const [anchorIndex, setAnchorIndex] = useState<number | null>(null);

  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  // 本向导没有全屏概念（任务表未列出）——不传 disabled，拖动/resize 恒生效；
  // submitting 时也不禁用拖动本身（只禁用关闭，见下方 requestClose），拖动
  // 一个提交中的向导没有数据风险。
  const floating = useFloatingWindow({ storageKey: "knowhow.import.window" });

  // 向导可能在请求进行中被关闭（用户点 X/Esc）；卸载后忽略迟到的
  // then/catch，避免对已卸载组件 setState（镜像 knowhow-panel.tsx 里
  // KnowhowImage 组件的同类 cancelled 守卫写法）。
  // 必须在 effect 体里把 mountedRef 置 true（而非只靠 useRef(true) 初值）：
  // React 18 StrictMode 的 dev 双调用会 mount→unmount→mount，cleanup 已把
  // ref 置 false，若 remount 不重置，ref 会永久停在 false——之后预览请求
  // resolve 时被 `!mountedRef.current` 当作「已卸载」丢弃，界面卡在「解析中…」。
  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  // 提交中（真正建表的请求，不可撤销/不可取消）时不允许关闭向导：Esc/背景点击
  // /右上角 X 一律经这道守卫。否则用户提前关闭后，导入仍可能在后台悄悄成功，
  // 但面板既不会收到 onDone 也就不会刷新列表——用户会看不到新表，一头雾水。
  // 预览阶段（previewLoading）不受此限：预览是纯读请求，提前关闭没有副作用。
  function requestClose() {
    if (submitting) return;
    onClose();
  }

  useEffect(() => {
    function handleKeyDown(event: globalThis.KeyboardEvent) {
      if (event.key === "Escape") requestClose();
    }
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- requestClose 是
    // 组件内的普通函数（未 useCallback），依赖数组直接列它没有意义（每次渲染
    // 都是新引用）；改列它实际捕获的值，effect 才会在 submitting/onClose 真
    // 变化时才重新订阅监听器，而不是每次渲染都重新订阅。
  }, [onClose, submitting]);

  // 行标题列 0..1 由单选选择器天然保证（规格①「至多一」）；唯一的提交前置
  // 校验只剩「标题非空」。
  const submitDisabled = submitting || isBlankTitle(title);

  async function handleFileSelected(selected: File) {
    if (!isSupportedImportFile(selected.name)) {
      setPreviewError(`不支持的文件类型，请选择 ${IMPORT_ACCEPT_EXTENSIONS.join(" / ")} 文件`);
      return;
    }
    setPreviewError(null);
    setPreviewLoading(true);
    try {
      const result = await importKnowhowPreview(notebookId, selected);
      if (!mountedRef.current) return;
      setFile(selected);
      setPreview(result);
      // 猜测预选：legacy guessed_role → 三值 kind + 行标题建议（Task 3 起
      // 后端直接给三值 kind + anchor_suggestion，映射函数自动透传）。
      const selection = deriveImportSelection(result);
      setKinds(selection.kinds);
      setAnchorIndex(selection.anchorIndex);
      setTitle(deriveDefaultTitle(selected.name));
      setStep("map");
    } catch (err) {
      if (!mountedRef.current) return;
      setPreviewError(extractErrorMessage(err, "解析文件失败，请重试"));
    } finally {
      if (mountedRef.current) setPreviewLoading(false);
    }
  }

  function handleFileInputChange(event: ChangeEvent<HTMLInputElement>) {
    const picked = event.target.files?.[0];
    event.target.value = "";
    if (picked) handleFileSelected(picked);
  }

  function backToSelect() {
    setStep("select");
    setFile(null);
    setPreview(null);
    setKinds([]);
    setAnchorIndex(null);
    setTitle("");
    setPreviewError(null);
    setSubmitError(null);
  }

  function setKindAt(index: number, kind: ColumnKind) {
    setKinds((prev) => prev.map((current, i) => (i === index ? kind : current)));
  }

  async function handleSubmit() {
    if (!file || !preview || submitDisabled) return;
    setSubmitting(true);
    setSubmitError(null);
    try {
      const columns = assembleImportColumnKinds(
        preview.columns.map((column) => column.name),
        kinds,
      );
      // 行标题列走独立的 anchor_index——后端不读列定义里的 role
      // （见 knowhow-model.importKnowhow / manage-logic.assembleImportColumnKinds）。
      await importKnowhow(notebookId, file, title.trim(), columns, anchorIndex);
      if (!mountedRef.current) return;
      onDone();
    } catch (err) {
      if (!mountedRef.current) return;
      setSubmitError(extractErrorMessage(err, "导入失败，请重试"));
    } finally {
      if (mountedRef.current) setSubmitting(false);
    }
  }

  function handleBackdropClick(event: ReactMouseEvent<HTMLDivElement>) {
    if (event.currentTarget === event.target) requestClose();
  }

  const stepNumber = step === "select" ? 1 : submitting ? 3 : 2;

  return (
    <div className="knowhow-import-backdrop" onClick={handleBackdropClick}>
      <section
        ref={floating.cardRef}
        className="knowhow-import-card"
        style={floating.style}
        role="dialog"
        aria-modal="true"
        aria-label="导入 Knowhow 表"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="knowhow-import-header" {...floating.dragHandleProps}>
          <div>
            <h2>导入 Knowhow 表</h2>
            <ImportStepIndicator current={stepNumber} />
          </div>
          <button
            type="button"
            className="icon-button"
            onClick={requestClose}
            disabled={submitting}
            title={submitting ? "导入进行中，请稍候" : "关闭"}
          >
            <X size={20} />
          </button>
        </div>

        <div className="knowhow-import-body">
          {step === "select" ? (
            <SelectFileStep
              loading={previewLoading}
              error={previewError}
              onFileInputChange={handleFileInputChange}
            />
          ) : preview ? (
            <MapStep
              fileName={file?.name ?? ""}
              preview={preview}
              title={title}
              onTitleChange={setTitle}
              kinds={kinds}
              onKindChange={setKindAt}
              anchorIndex={anchorIndex}
              onAnchorChange={setAnchorIndex}
              submitting={submitting}
              submitError={submitError}
              onDismissSubmitError={() => setSubmitError(null)}
              onBack={backToSelect}
              onSubmit={handleSubmit}
              submitDisabled={submitDisabled}
            />
          ) : null}
        </div>
        <span className="kh-modal-resize-handle" aria-hidden="true" {...floating.resizeHandleProps} />
      </section>
      <ImportWizardStyles />
    </div>
  );
}

// ---------------------------------------------------------------------------
// 步骤指示器
// ---------------------------------------------------------------------------

const STEP_LABELS: [1 | 2 | 3, string][] = [
  [1, "选文件"],
  [2, "预览与列设置"],
  [3, "提交"],
];

// Task 9「追加导入」复用同一套指示器壳，但没有"列设置"这一步（目标表结构
// 已固定，只按表头名匹配，见 KnowhowAppendWizard 头部注释）。
const APPEND_STEP_LABELS: [1 | 2 | 3, string][] = [
  [1, "选文件"],
  [2, "预览"],
  [3, "提交"],
];

function ImportStepIndicator({
  current,
  labels = STEP_LABELS,
}: {
  current: 1 | 2 | 3;
  /** 默认取建表导入的三步文案；Task 9 的追加导入传入 APPEND_STEP_LABELS。 */
  labels?: [1 | 2 | 3, string][];
}) {
  return (
    <ol className="knowhow-import-steps">
      {labels.map(([n, label]) => (
        <li key={n} className={n === current ? "is-active" : n < current ? "is-done" : undefined}>
          <span className="knowhow-import-step-num">{n}</span>
          <span>{label}</span>
        </li>
      ))}
    </ol>
  );
}

// ---------------------------------------------------------------------------
// 步骤① — 选文件
// ---------------------------------------------------------------------------

function SelectFileStep({
  loading,
  error,
  onFileInputChange,
}: {
  loading: boolean;
  error: string | null;
  onFileInputChange: (event: ChangeEvent<HTMLInputElement>) => void;
}) {
  return (
    <div className="knowhow-import-select-step">
      <label className={`knowhow-import-dropzone${loading ? " is-loading" : ""}`}>
        <input type="file" accept={IMPORT_ACCEPT} onChange={onFileInputChange} disabled={loading} />
        <span className="knowhow-import-drop-icon">
          {loading ? <Loader2 size={28} className="knowhow-import-spin" /> : <Upload size={28} />}
        </span>
        <strong>{loading ? "解析中…" : "点击或拖拽文件到此处"}</strong>
        <small>支持 {IMPORT_ACCEPT_EXTENSIONS.join(" / ")}</small>
      </label>
      {error && <p className="knowhow-import-error">{error}</p>}
    </div>
  );
}

// ---------------------------------------------------------------------------
// 步骤②③ — 预览 + 列设置（内容类型 + 行标题列）+ 提交
// ---------------------------------------------------------------------------

function MapStep({
  fileName,
  preview,
  title,
  onTitleChange,
  kinds,
  onKindChange,
  anchorIndex,
  onAnchorChange,
  submitting,
  submitError,
  onDismissSubmitError,
  onBack,
  onSubmit,
  submitDisabled,
}: {
  fileName: string;
  preview: KnowhowImportPreview;
  title: string;
  onTitleChange: (value: string) => void;
  kinds: ColumnKind[];
  onKindChange: (index: number, kind: ColumnKind) => void;
  anchorIndex: number | null;
  onAnchorChange: (anchorIndex: number | null) => void;
  submitting: boolean;
  submitError: string | null;
  onDismissSubmitError: () => void;
  onBack: () => void;
  onSubmit: () => void;
  submitDisabled: boolean;
}) {
  // 预览即所得：与导入后的主网格 G2 看到的形状一致——先把行标题列
  // forward-fill（镜像后端落库前的 forward_fill_column），再把每列相邻同值
  // 合并成 rowspan 格。「分组只写一次」的概念列在文件里是「首行有值、兄弟
  // 行留空」，不这么做预览里就是一串「—」，看着像数据丢了。
  const previewSpans = useMemo(
    () => computePreviewSpans(preview.rowsPreview, anchorIndex),
    [preview.rowsPreview, anchorIndex],
  );
  return (
    <div className="knowhow-import-map-step">
      <div className="knowhow-import-file-row">
        <span className="knowhow-import-file-name" title={fileName}>
          {fileName}
        </span>
        <button type="button" className="sort-button" onClick={onBack} disabled={submitting}>
          <ChevronLeft size={14} /> 重新选择文件
        </button>
      </div>

      <label className="knowhow-import-field">
        <span>表标题</span>
        <input
          type="text"
          value={title}
          onChange={(event) => onTitleChange(event.target.value)}
          disabled={submitting}
          placeholder="表标题"
        />
      </label>

      <div className="knowhow-import-anchor-row">
        <label className="knowhow-import-anchor-label">
          <span>{ANCHOR_SELECTOR_LABEL}</span>
          <select
            value={anchorIndex === null ? "" : String(anchorIndex)}
            onChange={(event) => onAnchorChange(event.target.value === "" ? null : Number(event.target.value))}
            disabled={submitting}
          >
            <option value="">{ANCHOR_NONE_LABEL}</option>
            {preview.columns.map((column, index) => (
              <option key={index} value={String(index)}>
                {column.name || `列 ${index + 1}`}
              </option>
            ))}
          </select>
        </label>
        <p className={`knowhow-import-anchor-hint${anchorIndex === null ? " is-none" : ""}`}>{anchorHint(anchorIndex)}</p>
      </div>

      <div className="knowhow-import-preview-scroll">
        <table className="knowhow-import-preview-table">
          <thead>
            <tr>
              {preview.columns.map((column, index) => (
                <th key={index}>
                  <div className="knowhow-import-col-head">
                    <span className="knowhow-import-col-name" title={column.name}>
                      {column.name}
                    </span>
                    {index === anchorIndex ? (
                      <span className="knowhow-import-anchor-badge" title={ANCHOR_SET_HINT}>
                        行标题
                      </span>
                    ) : (
                      <select
                        value={kinds[index]}
                        title={KIND_HINTS[kinds[index] ?? "attribute"]}
                        onChange={(event) => onKindChange(index, event.target.value as ColumnKind)}
                        disabled={submitting}
                      >
                        {KIND_OPTIONS.map((option) => (
                          <option key={option.value} value={option.value} title={option.hint}>
                            {option.label}
                          </option>
                        ))}
                      </select>
                    )}
                  </div>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {previewSpans.map((cells, rowIndex) => (
              <tr key={rowIndex}>
                {cells.map((cell, cellIndex) => {
                  // rowSpan===0 的格被上方同列的合并格覆盖，渲染跳过。
                  if (cell.rowSpan === 0) return null;
                  const text = cellSummary(cell.text);
                  const merged = cell.rowSpan > 1;
                  return (
                    <td
                      key={cellIndex}
                      rowSpan={merged ? cell.rowSpan : undefined}
                      className={merged ? "is-merged" : undefined}
                      title={text || undefined}
                    >
                      <span className="knowhow-import-cell-text">{text || "—"}</span>
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <p className="knowhow-import-meta">{`共 ${preview.totalRows} 行，预览前 ${preview.rowsPreview.length} 行`}</p>

      <KindLegend />

      {submitError && (
        <div className="knowhow-import-submit-error">
          <span>{submitError}</span>
          <button type="button" onClick={onDismissSubmitError} title="关闭">
            <X size={14} />
          </button>
        </div>
      )}

      <div className="knowhow-import-actions">
        <button type="button" className="new-pill knowhow-import-submit-button" disabled={submitDisabled} onClick={onSubmit}>
          {submitting && <Loader2 size={14} className="knowhow-import-spin" />}
          {submitting ? "导入中…" : "确认导入"}
        </button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// KnowhowAppendWizard — 追加导入（Task 9，规格②路B「用户线下批量填写后
// 上传...给出导入预览...确认后追加导入」）：复用本文件的向导壳（backdrop/
// card/步骤指示器/选文件步骤/整套 knowhow-import-* 样式），只把「预览+列
// 设置」换成「预览+重名/未匹配列提示」——追加导入的目标表结构已经固定，不
// 需要再选内容类型或行标题列，按表头名匹配是后端 preview_append/
// commit_append 的既有职责，本组件只负责把匹配结果讲清楚给用户看。
// ---------------------------------------------------------------------------

export interface KnowhowAppendWizardProps {
  notebookId: string;
  tableId: string;
  /** 当前表的列——渲染预览表头用 position 顺序（须与后端
   * `_align_rows_to_table_columns` 产出的 `aligned_rows[i][j]` 对齐的
   * table_columns 顺序完全一致；那是 get_knowhow_table 已排好的纯 position
   * 序，不是网格展示用的「行标题列钉首」序——两者只在行标题列不在
   * position 0 时才会不同，用错顺序会让预览表头与实际列错位）。 */
  columns: KnowhowColumn[];
  /** 用户主动取消/关闭向导（未完成追加）：面板据此仅收起向导，不重新拉表。 */
  onClose: () => void;
  /** 追加成功后触发：面板据此重拉表详情+表列表并收起向导。 */
  onDone: () => void;
}

type AppendStep = "select" | "preview";

export function KnowhowAppendWizard({ notebookId, tableId, columns, onClose, onDone }: KnowhowAppendWizardProps) {
  const [step, setStep] = useState<AppendStep>("select");

  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);

  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<KnowhowAppendPreview | null>(null);

  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  // 独立于 KnowhowImportWizard 的 knowhow.import.window——追加导入是单独一个
  // 弹窗身份，各自记住各自的拖动/resize 位置。本向导同样没有全屏概念。
  const floating = useFloatingWindow({ storageKey: "knowhow.append.window" });

  // 卸载后忽略迟到的 then/catch（镜像 KnowhowImportWizard 的 mountedRef 守卫，
  // 含 StrictMode remount 重置——见该组件同一处注释）。
  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  // 追加提交中（真正落库、不可撤销）时不允许关闭：镜像 KnowhowImportWizard
  // 的同名守卫，理由完全一致（提前关闭后追加仍可能在后台悄悄成功，面板却
  // 收不到 onDone 也就不会刷新，用户看不到新增的行）。预览阶段可随时关闭。
  function requestClose() {
    if (submitting) return;
    onClose();
  }

  useEffect(() => {
    function handleKeyDown(event: globalThis.KeyboardEvent) {
      if (event.key === "Escape") requestClose();
    }
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [onClose, submitting]);

  async function handleFileSelected(selected: File) {
    if (!isSupportedImportFile(selected.name)) {
      setPreviewError(`不支持的文件类型，请选择 ${IMPORT_ACCEPT_EXTENSIONS.join(" / ")} 文件`);
      return;
    }
    setPreviewError(null);
    setPreviewLoading(true);
    try {
      const result = await appendKnowhowPreview(notebookId, tableId, selected);
      if (!mountedRef.current) return;
      setFile(selected);
      setPreview(result);
      setStep("preview");
    } catch (err) {
      if (!mountedRef.current) return;
      setPreviewError(extractErrorMessage(err, "解析文件失败，请重试"));
    } finally {
      if (mountedRef.current) setPreviewLoading(false);
    }
  }

  function handleFileInputChange(event: ChangeEvent<HTMLInputElement>) {
    const picked = event.target.files?.[0];
    event.target.value = "";
    if (picked) handleFileSelected(picked);
  }

  function backToSelect() {
    setStep("select");
    setFile(null);
    setPreview(null);
    setPreviewError(null);
    setSubmitError(null);
  }

  async function handleSubmit() {
    if (!file || submitting) return;
    setSubmitting(true);
    setSubmitError(null);
    try {
      await appendKnowhowCommit(notebookId, tableId, file);
      if (!mountedRef.current) return;
      onDone();
    } catch (err) {
      if (!mountedRef.current) return;
      setSubmitError(extractErrorMessage(err, "追加导入失败，请重试"));
    } finally {
      if (mountedRef.current) setSubmitting(false);
    }
  }

  function handleBackdropClick(event: ReactMouseEvent<HTMLDivElement>) {
    if (event.currentTarget === event.target) requestClose();
  }

  const stepNumber = step === "select" ? 1 : submitting ? 3 : 2;
  // 预览表头顺序：见上方 columns prop 的注释——必须是纯 position 序。
  const orderedColumns = useMemo(() => sortColumnsByPosition(columns), [columns]);

  return (
    <div className="knowhow-import-backdrop" onClick={handleBackdropClick}>
      <section
        ref={floating.cardRef}
        className="knowhow-import-card"
        style={floating.style}
        role="dialog"
        aria-modal="true"
        aria-label="追加导入 Knowhow 表"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="knowhow-import-header" {...floating.dragHandleProps}>
          <div>
            <h2>追加导入</h2>
            <ImportStepIndicator current={stepNumber} labels={APPEND_STEP_LABELS} />
          </div>
          <button
            type="button"
            className="icon-button"
            onClick={requestClose}
            disabled={submitting}
            title={submitting ? "追加进行中，请稍候" : "关闭"}
          >
            <X size={20} />
          </button>
        </div>

        <div className="knowhow-import-body">
          {step === "select" ? (
            <SelectFileStep loading={previewLoading} error={previewError} onFileInputChange={handleFileInputChange} />
          ) : preview ? (
            <AppendPreviewStep
              fileName={file?.name ?? ""}
              preview={preview}
              columns={orderedColumns}
              submitting={submitting}
              submitError={submitError}
              onDismissSubmitError={() => setSubmitError(null)}
              onBack={backToSelect}
              onSubmit={handleSubmit}
            />
          ) : null}
        </div>
        <span className="kh-modal-resize-handle" aria-hidden="true" {...floating.resizeHandleProps} />
      </section>
      <ImportWizardStyles />
    </div>
  );
}

// ---------------------------------------------------------------------------
// 追加导入 — 预览步骤（重名标黄 + 未匹配列提示；无内容类型/行标题列选择——
// 目标表结构已固定，见 KnowhowAppendWizard 头部注释）。
// ---------------------------------------------------------------------------

function AppendPreviewStep({
  fileName,
  preview,
  columns,
  submitting,
  submitError,
  onDismissSubmitError,
  onBack,
  onSubmit,
}: {
  fileName: string;
  preview: KnowhowAppendPreview;
  columns: KnowhowColumn[];
  submitting: boolean;
  submitError: string | null;
  onDismissSubmitError: () => void;
  onBack: () => void;
  onSubmit: () => void;
}) {
  // 未匹配列提示：文件里存在、当前表却没有的列（信息性提示，不阻止提交——
  // 后端 preview_append/commit_append 对这些列的处理完全一致，都是"报告后
  // 丢弃"，不是校验失败）。
  const unmatchedMessage = formatUnmatchedColumnsMessage(preview.unmatchedColumns);
  // 重名展示映射：把后端 0-based row_index 转成用户看的 1-based 行号，并
  // 标记该重名是否落在下方预览表格的前 5 行窗口内——duplicate_titles 是对
  // 整份上传文件算的，可能命中预览窗口之外的行，那部分只能靠下面的文字
  // 列表提醒，无法在预览表格里高亮对应行。
  const duplicateDisplay = mapAppendDuplicatesForDisplay(preview.duplicateTitles, preview.rowsPreview.length);

  return (
    <div className="knowhow-import-map-step">
      <div className="knowhow-import-file-row">
        <span className="knowhow-import-file-name" title={fileName}>
          {fileName}
        </span>
        <button type="button" className="sort-button" onClick={onBack} disabled={submitting}>
          <ChevronLeft size={14} /> 重新选择文件
        </button>
      </div>

      {unmatchedMessage && <p className="knowhow-import-warning">{unmatchedMessage}</p>}

      <div className="knowhow-import-preview-scroll">
        <table className="knowhow-import-preview-table">
          <thead>
            <tr>
              {columns.map((column) => (
                <th key={column.id}>
                  <span className="knowhow-import-col-name" title={column.name}>
                    {column.name}
                  </span>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {preview.rowsPreview.map((row, rowIndex) => (
              <tr
                key={rowIndex}
                className={
                  isAppendPreviewRowDuplicate(preview.duplicateTitles, rowIndex)
                    ? "knowhow-import-row-duplicate"
                    : undefined
                }
              >
                {row.map((cell, cellIndex) => {
                  const text = cellSummary(cell ?? "");
                  return (
                    <td key={cellIndex} title={text || undefined}>
                      <span className="knowhow-import-cell-text">{text || "—"}</span>
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <p className="knowhow-import-meta">{`共 ${preview.totalRows} 行，预览前 ${preview.rowsPreview.length} 行`}</p>

      {duplicateDisplay.length > 0 && (
        <div className="knowhow-import-warning">
          <p className="knowhow-import-duplicate-lead">
            以下 {duplicateDisplay.length} 行标题与现有行重复（追加后仍会写入，仅作提醒）：
          </p>
          <ul className="knowhow-import-duplicate-list">
            {duplicateDisplay.map((item, index) => (
              <li key={index}>
                第 {item.displayIndex} 行「{item.title}」{!item.inPreviewWindow && "（不在上方预览范围内）"}
              </li>
            ))}
          </ul>
        </div>
      )}

      {submitError && (
        <div className="knowhow-import-submit-error">
          <span>{submitError}</span>
          <button type="button" onClick={onDismissSubmitError} title="关闭">
            <X size={14} />
          </button>
        </div>
      )}

      <div className="knowhow-import-actions">
        <button type="button" className="new-pill knowhow-import-submit-button" disabled={submitting} onClick={onSubmit}>
          {submitting && <Loader2 size={14} className="knowhow-import-spin" />}
          {submitting ? "追加中…" : `确认追加 ${preview.totalRows} 行`}
        </button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 样式：namespaced `knowhow-import-*` + `<style jsx global>`。必须挂在**始终
// 渲染**的顶层 KnowhowImportWizard 里，而非某个按步骤条件渲染的子组件——否则
// 「选文件」这一步（MapStep 尚未挂载）拿不到任何 .knowhow-import-* 规则，
// modal 会以无样式的文档流原样铺开（背景/卡片/步骤 chip/隐藏原生 input 全失效）。
// ---------------------------------------------------------------------------

function ImportWizardStyles() {
  return (
    <style jsx global>{`
        .knowhow-import-backdrop {
          position: fixed;
          inset: 0;
          z-index: 60;
          display: grid;
          place-items: center;
          padding: 24px;
          background: rgba(17, 24, 32, 0.36);
        }

        .knowhow-import-card {
          width: min(760px, 100%);
          max-height: calc(100vh - 48px);
          display: flex;
          flex-direction: column;
          background: var(--panel);
          color: var(--ink);
          border-radius: 16px;
          box-shadow: var(--shadow);
          overflow: hidden;
          /* 浮窗 resize 手柄（.kh-modal-resize-handle，见下方）绝对定位需要
             一个定位上下文——backdrop 是 position: fixed，不加这条手柄会贴到
             整个视口右下角而不是卡片右下角。 */
          position: relative;
        }

        /* 浮窗 resize 手柄——与 knowhow-panel.tsx 顶层 <style jsx global> 里
           .kh-modal-resize-handle 那份定义逐字同款（同一个 class 名，沿用
           kh-modal-* 命名），但必须在本文件自己的样式块里重复登记一份：本
           文件的既有约定是「自成一体，不依赖面板的 style 标签是否同时挂载」
           （见文件头注释），不能指望 KnowhowPanel 的全局样式恰好也已经渲染
           过。两份定义规则完全相同，互不冲突，哪个先被浏览器解析都一样。 */
        .kh-modal-resize-handle {
          position: absolute;
          right: 0;
          bottom: 0;
          width: 22px;
          height: 22px;
          cursor: nwse-resize;
          touch-action: none;
          /* 右下角斜纹拖拽角——与 knowhow-panel.tsx 的 .kh-modal-resize-handle
             逐字同款（放大 + 斜纹，见那份注释）。 */
          clip-path: polygon(100% 0%, 100% 100%, 0% 100%);
          background-image: repeating-linear-gradient(
            -45deg,
            var(--muted) 0 1.5px,
            transparent 1.5px 4.5px
          );
        }

        .kh-modal-resize-handle:hover {
          background-color: var(--soft);
          background-image: repeating-linear-gradient(
            -45deg,
            var(--ink) 0 1.5px,
            transparent 1.5px 4.5px
          );
        }

        .knowhow-import-header {
          flex: 0 0 auto;
          display: flex;
          align-items: flex-start;
          justify-content: space-between;
          gap: 18px;
          padding: 20px 24px;
          border-bottom: 1px solid var(--line);
        }

        .knowhow-import-header h2 {
          margin: 0 0 10px;
          font-size: 20px;
        }

        .knowhow-import-header .icon-button:disabled {
          opacity: 0.5;
          cursor: not-allowed;
        }

        .knowhow-import-steps {
          display: flex;
          align-items: center;
          gap: 14px;
          margin: 0;
          padding: 0;
          list-style: none;
        }

        .knowhow-import-steps li {
          display: flex;
          align-items: center;
          gap: 6px;
          font-size: 12px;
          color: var(--muted);
          white-space: nowrap;
        }

        .knowhow-import-step-num {
          display: inline-flex;
          align-items: center;
          justify-content: center;
          width: 18px;
          height: 18px;
          border-radius: 50%;
          border: 1px solid var(--line);
          background: var(--soft);
          color: var(--muted);
          font-size: 11px;
          font-weight: 700;
        }

        .knowhow-import-steps li.is-active {
          color: var(--ink);
          font-weight: 600;
        }

        .knowhow-import-steps li.is-active .knowhow-import-step-num {
          border-color: var(--blue);
          background: var(--blue);
          color: #fff;
        }

        .knowhow-import-steps li.is-done .knowhow-import-step-num {
          border-color: var(--blue);
          background: #eef2ff;
          color: var(--blue);
        }

        .knowhow-import-body {
          flex: 1 1 auto;
          min-height: 0;
          overflow-y: auto;
          padding: 24px;
          display: flex;
          flex-direction: column;
          gap: 16px;
        }

        .knowhow-import-select-step {
          display: flex;
          flex-direction: column;
          gap: 12px;
        }

        .knowhow-import-dropzone {
          position: relative;
          min-height: 220px;
          display: grid;
          place-items: center;
          align-content: center;
          gap: 10px;
          text-align: center;
          border: 1.5px dashed var(--line);
          border-radius: 14px;
          background: var(--soft);
          cursor: pointer;
        }

        .knowhow-import-dropzone:hover {
          border-color: var(--blue);
        }

        .knowhow-import-dropzone.is-loading {
          cursor: not-allowed;
          opacity: 0.75;
        }

        .knowhow-import-dropzone input {
          position: absolute;
          inset: 0;
          opacity: 0;
          cursor: pointer;
        }

        .knowhow-import-dropzone.is-loading input {
          cursor: not-allowed;
        }

        .knowhow-import-dropzone strong {
          font-size: 16px;
        }

        .knowhow-import-dropzone small {
          color: var(--muted);
        }

        .knowhow-import-drop-icon {
          width: 56px;
          height: 56px;
          display: grid;
          place-items: center;
          border-radius: 50%;
          background: #eef0ff;
          color: var(--blue);
        }

        .knowhow-import-spin {
          animation: knowhow-import-spin 0.8s linear infinite;
        }

        @keyframes knowhow-import-spin {
          to {
            transform: rotate(360deg);
          }
        }

        .knowhow-import-error {
          margin: 0;
          padding: 10px 14px;
          border: 1px solid #f0c0c0;
          border-radius: 8px;
          background: #fef2f2;
          color: #b91c1c;
          font-size: 13px;
        }

        .knowhow-import-warning {
          margin: 0;
          padding: 10px 14px;
          border: 1px solid #f0dab3;
          border-radius: 8px;
          background: #fdf4e6;
          color: #9a5b00;
          font-size: 13px;
        }

        /* Task 9：追加导入重名提示列表——.knowhow-import-warning 这个外壳同时
           被单行(未匹配列)与"提示语+列表"(重名标题)两种内容复用。 */
        .knowhow-import-duplicate-lead {
          margin: 0 0 6px;
        }

        .knowhow-import-duplicate-list {
          margin: 0;
          padding: 0;
          list-style: none;
          display: flex;
          flex-direction: column;
          gap: 2px;
        }

        .knowhow-import-map-step {
          display: flex;
          flex-direction: column;
          gap: 14px;
          min-height: 0;
        }

        .knowhow-import-file-row {
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 12px;
        }

        .knowhow-import-file-name {
          font-size: 13px;
          color: var(--muted);
          overflow: hidden;
          text-overflow: ellipsis;
          white-space: nowrap;
          min-width: 0;
        }

        .knowhow-import-field {
          display: grid;
          gap: 6px;
          font-size: 13px;
          font-weight: 600;
          color: var(--ink);
        }

        .knowhow-import-field input {
          box-sizing: border-box;
          width: 100%;
          padding: 9px 12px;
          border: 1px solid var(--line);
          border-radius: 8px;
          background: #fff;
          color: var(--ink);
          font-size: 13px;
          font-weight: 400;
        }

        .knowhow-import-field input:disabled {
          background: var(--soft);
          color: var(--muted);
        }

        .knowhow-import-anchor-row {
          display: flex;
          flex-direction: column;
          gap: 6px;
        }

        .knowhow-import-anchor-label {
          display: flex;
          align-items: center;
          gap: 10px;
          font-size: 13px;
          font-weight: 600;
          color: var(--ink);
        }

        .knowhow-import-anchor-label span {
          flex: 0 0 auto;
        }

        .knowhow-import-anchor-label select {
          box-sizing: border-box;
          max-width: 280px;
          padding: 8px 8px;
          border: 1px solid var(--line);
          border-radius: 8px;
          background: #fff;
          color: var(--ink);
          font-size: 13px;
          font-weight: 400;
        }

        .knowhow-import-anchor-label select:disabled {
          background: var(--soft);
          color: var(--muted);
        }

        .knowhow-import-anchor-hint {
          margin: 0;
          font-size: 12px;
          color: var(--muted);
          line-height: 1.5;
        }

        .knowhow-import-anchor-hint.is-none {
          color: #9a5b00;
        }

        .knowhow-import-anchor-badge {
          display: inline-flex;
          align-items: center;
          justify-content: center;
          font-size: 12px;
          font-weight: 600;
          padding: 4px 8px;
          border-radius: 999px;
          border: 1px solid #c7d6ff;
          background: #eef2ff;
          color: #1f5eff;
          white-space: nowrap;
        }

        .knowhow-import-preview-scroll {
          overflow: auto;
          max-height: 280px;
          border: 1px solid var(--line);
          border-radius: 10px;
        }

        .knowhow-import-preview-table {
          border-collapse: separate;
          border-spacing: 0;
          width: 100%;
          table-layout: fixed;
          font-size: 13px;
        }

        .knowhow-import-preview-table th,
        .knowhow-import-preview-table td {
          border-bottom: 1px solid var(--line);
          border-right: 1px solid var(--line);
          padding: 8px 10px;
          text-align: left;
          vertical-align: top;
          width: 180px;
        }

        /* 跨行合并格——与导入后主网格的 .knowhow-cell-merged 同款视觉
           （soft 底 + 垂直居中），让预览里「这几行同属一个概念/共享同一个
           属性值」和 Excel 原表、导入后的样子都对得上。 */
        .knowhow-import-preview-table td.is-merged {
          background: var(--soft);
          vertical-align: middle;
        }

        .knowhow-import-preview-table th:last-child,
        .knowhow-import-preview-table td:last-child {
          border-right: 0;
        }

        .knowhow-import-preview-table th {
          background: var(--soft);
          position: sticky;
          top: 0;
          z-index: 1;
        }

        /* Task 9：追加导入预览里与现有行标题重名的行标黄提醒（规格②路B
           「概念列与已有行重名标黄提醒」）。 */
        .knowhow-import-row-duplicate td {
          background: #fdf4e6;
        }

        .knowhow-import-col-head {
          display: flex;
          flex-direction: column;
          gap: 6px;
          min-width: 0;
        }

        .knowhow-import-col-name {
          overflow: hidden;
          text-overflow: ellipsis;
          white-space: nowrap;
          font-weight: 600;
          color: var(--ink);
        }

        .knowhow-import-col-head select {
          box-sizing: border-box;
          width: 100%;
          padding: 5px 6px;
          border: 1px solid var(--line);
          border-radius: 6px;
          background: #fff;
          color: var(--ink);
          font-size: 12px;
          font-weight: 400;
        }

        .knowhow-import-col-head select:disabled {
          background: var(--soft);
          color: var(--muted);
        }

        .knowhow-import-cell-text {
          display: block;
          overflow: hidden;
          text-overflow: ellipsis;
          white-space: nowrap;
          color: #333a45;
        }

        .knowhow-import-meta {
          margin: 0;
          font-size: 12px;
          color: var(--muted);
        }

        .knowhow-import-submit-error {
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

        .knowhow-import-submit-error button {
          border: 0;
          background: transparent;
          color: inherit;
          font-size: 16px;
          line-height: 1;
          cursor: pointer;
        }

        .knowhow-import-actions {
          display: flex;
          justify-content: flex-end;
          gap: 10px;
          padding-top: 4px;
        }

        .knowhow-import-submit-button {
          display: inline-flex;
          align-items: center;
          gap: 6px;
        }

        .knowhow-import-submit-button:disabled {
          opacity: 0.6;
          cursor: not-allowed;
        }
      `}</style>
  );
}
