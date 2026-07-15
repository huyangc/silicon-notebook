/**
 * knowhow-import.tsx
 *
 * Knowhow 表导入向导：三步 modal，挂载在 knowhow-panel.tsx 的
 * `onImportClick` 挂载点上（面板持有 `importOpen` 布尔态，点击"导入表格"时
 * 渲染本组件；本组件不知道、也不关心自己被谁挂载）。
 *
 *   ① 选文件（.xlsx/.csv/.md，拖拽或点击）
 *   ② 预览 + 角色映射（前 5 行预览；每列一个角色下拉，默认 guessedRole；
 *      表标题输入框默认=文件名去后缀；concept 必须恰好一列，否则禁用提交
 *      并给出中文提示）
 *   ③ 提交（进度态；后端错误中文原样展示；成功后调用 onDone 回调——由
 *      面板负责刷新表列表 + 关闭向导）
 *
 * 步骤②③在实现上共用同一屏（预览表格 + 提交按钮），"提交中"只是这屏的一个
 * 子状态（submitting=true 时输入禁用+按钮显示进度文案），而非跳转到另一个
 * 页面——避免用户在等待期间看到界面跳变、丢失已核对的角色映射上下文。
 * 顶部步骤指示器仍按①②③三态展示，视觉上保留"三步"心智模型。
 *
 * 纯逻辑（payload 组装/concept 校验/角色选项/默认标题/文件类型校验/错误
 * 文案抽取）都在 knowhow-import-logic.ts 里（无 JSX，供
 * knowhow-import.test.mjs 直接 import——Node 原生 TS 类型剥离不支持
 * .tsx，只能拆到 .ts，镜像 knowhow-panel.tsx 的既有拆分方式）。
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
import { cellSummary, importKnowhow, importKnowhowPreview, type KnowhowImportPreview, type Role } from "./knowhow-model.ts";
import {
  IMPORT_ACCEPT,
  IMPORT_ACCEPT_EXTENSIONS,
  ROLE_OPTIONS,
  assembleImportColumns,
  canSubmitImport,
  conceptValidationError,
  deriveDefaultTitle,
  extractErrorMessage,
  isSupportedImportFile,
} from "./knowhow-import-logic.ts";

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
  const [roles, setRoles] = useState<Role[]>([]);

  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

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

  const conceptError = useMemo(() => (step === "map" ? conceptValidationError(roles) : null), [step, roles]);
  const submitDisabled = submitting || !canSubmitImport(title, roles);

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
      setRoles(result.columns.map((column) => column.guessedRole));
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
    setRoles([]);
    setTitle("");
    setPreviewError(null);
    setSubmitError(null);
  }

  function setRoleAt(index: number, role: Role) {
    setRoles((prev) => prev.map((current, i) => (i === index ? role : current)));
  }

  async function handleSubmit() {
    if (!file || !preview || submitDisabled) return;
    setSubmitting(true);
    setSubmitError(null);
    try {
      const columns = assembleImportColumns(preview.columns, roles);
      await importKnowhow(notebookId, file, title.trim(), columns);
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
        className="knowhow-import-card"
        role="dialog"
        aria-modal="true"
        aria-label="导入 Knowhow 表"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="knowhow-import-header">
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
              roles={roles}
              onRoleChange={setRoleAt}
              conceptError={conceptError}
              submitting={submitting}
              submitError={submitError}
              onDismissSubmitError={() => setSubmitError(null)}
              onBack={backToSelect}
              onSubmit={handleSubmit}
              submitDisabled={submitDisabled}
            />
          ) : null}
        </div>
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
  [2, "预览与角色映射"],
  [3, "提交"],
];

function ImportStepIndicator({ current }: { current: 1 | 2 | 3 }) {
  return (
    <ol className="knowhow-import-steps">
      {STEP_LABELS.map(([n, label]) => (
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
// 步骤②③ — 预览 + 角色映射 + 提交
// ---------------------------------------------------------------------------

function MapStep({
  fileName,
  preview,
  title,
  onTitleChange,
  roles,
  onRoleChange,
  conceptError,
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
  roles: Role[];
  onRoleChange: (index: number, role: Role) => void;
  conceptError: string | null;
  submitting: boolean;
  submitError: string | null;
  onDismissSubmitError: () => void;
  onBack: () => void;
  onSubmit: () => void;
  submitDisabled: boolean;
}) {
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

      {conceptError && <p className="knowhow-import-warning">{conceptError}</p>}

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
                    <select
                      value={roles[index]}
                      onChange={(event) => onRoleChange(index, event.target.value as Role)}
                      disabled={submitting}
                    >
                      {ROLE_OPTIONS.map((option) => (
                        <option key={option.value} value={option.value}>
                          {option.label}
                        </option>
                      ))}
                    </select>
                  </div>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {preview.rowsPreview.map((row, rowIndex) => (
              <tr key={rowIndex}>
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
