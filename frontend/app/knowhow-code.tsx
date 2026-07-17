/**
 * knowhow-code.tsx
 *
 * 格子级代码附件 UI（Task 11，规格⑥-4 UI clause）："格子/行「代码」徽章
 * （三态）→ 点击浮层查看（等宽、复制、语言标记），用户可编辑保存"。
 *
 * 两个导出组件：
 *   - KnowhowCodeChip：行详情抽屉分节头的三态入口——implemented/stale 总是
 *     可见（只读成员也能看到"有没有代码"），none 时只有 canEdit 才露出一个
 *     安静的「添加代码」虚线入口（纯只读成员看不到，无写权限点了也无处可
 *     去）。
 *   - KnowhowCodeModal：点 chip 打开的查看/编辑浮层——等宽代码块 + 复制按钮
 *     + 语言标记（编辑态自由文本）+ 新鲜度 chip/说明句 + 编辑/保存/删除确认。
 *     Agent 写入的代码对 canEdit 用户完全可见可改可删（规格⑥-4"notebook 不
 *     执行代码、不做 LLM 语义审代码"——本组件只负责存储/展示/编辑，不做任何
 *     代码执行或语义校验）。
 *
 * 代码不是 markdown（它是原样文本，不经 KnowhowMarkdown 渲染），故本组件不
 * 依赖 notebookId/apiBase 或资产改写——比格子内容浮窗（knowhow-cell-
 * editor.tsx）简单得多：没有图片粘贴/草稿自动保存/实时预览分栏，只有一块纯
 * 文本 textarea。
 *
 * 复用 knowhow-panel.tsx 顶层 `<style jsx global>` 里登记的既有 kh-modal-,
 * kh-footer-actions, kh-close-guard- 一整套视觉语言（同 knowhow-cell-
 * editor.tsx 的既有约定——那是本特性唯一保证任何时候都已挂载的样式容器）；
 * 本文件新增的 chip/代码块专属样式（.kh-code-chip* / .kh-code-block 等）与
 * 新增的 .knowhow-status-badge--warning 色调同样登记在那里，不在本文件内
 * 另开 <style jsx>。
 *
 * 纯逻辑（三态展示映射/chip 门控/行级 map 缺席补位/保存前校验/复制分支决策）
 * 都在 knowhow-code-logic.ts 里，供 knowhow-code.test.mjs 直接 import。
 */
"use client";

import { useEffect, useState, type MouseEvent as ReactMouseEvent } from "react";
import { Check, Code, Copy, Edit3, Trash2, X } from "lucide-react";
import { deleteCellCode, putCellCode, type KnowhowCellCode } from "./knowhow-model.ts";
import { extractErrorMessage } from "./knowhow-import-logic.ts";
import { useFloatingWindow } from "./use-floating-window.ts";
import {
  ADD_CODE_LABEL,
  CANCEL_CODE_LABEL,
  CODE_CLOSE_GUARD_CONTINUE_LABEL,
  CODE_CLOSE_GUARD_DISCARD_LABEL,
  CODE_CLOSE_GUARD_MESSAGE,
  CODE_DELETE_ERROR_FALLBACK,
  CODE_SAVE_ERROR_FALLBACK,
  CODE_STATUS_EXPLANATIONS,
  CODE_STATUS_LABELS,
  CODE_STATUS_TONE,
  CODE_TEXTAREA_PLACEHOLDER,
  COPIED_CODE_LABEL,
  COPY_CODE_LABEL,
  DELETE_CODE_CONFIRM_TEXT,
  DELETE_CODE_TITLE,
  EDIT_CODE_LABEL,
  LANGUAGE_INPUT_PLACEHOLDER,
  NO_LANGUAGE_TAG_TEXT,
  SAVE_CODE_LABEL,
  codeEditorIsDirty,
  codeProvenanceSuffix,
  codeSaveDisabledReason,
  normalizeLanguageInput,
  resolveCopyStrategy,
} from "./knowhow-code-logic.ts";

// ---------------------------------------------------------------------------
// KnowhowCodeChip — 抽屉分节头的三态入口
// ---------------------------------------------------------------------------

export function KnowhowCodeChip({
  code,
  canEdit,
  onOpen,
}: {
  code: KnowhowCellCode;
  canEdit: boolean;
  onOpen: () => void;
}) {
  if (code.status === "none") {
    if (!canEdit) return null;
    return (
      <button type="button" className="kh-code-chip kh-code-chip--add" onClick={onOpen} title="添加代码附件">
        <Code size={12} /> {ADD_CODE_LABEL}
      </button>
    );
  }
  const tone = CODE_STATUS_TONE[code.status];
  return (
    <button
      type="button"
      className={`knowhow-status-badge knowhow-status-badge--${tone} kh-code-chip`}
      onClick={onOpen}
      title="查看代码附件"
    >
      <Code size={12} /> {CODE_STATUS_LABELS[code.status]}
    </button>
  );
}

// ---------------------------------------------------------------------------
// KnowhowCodeModal — 查看/编辑浮层
// ---------------------------------------------------------------------------

export interface KnowhowCodeModalProps {
  rowId: string;
  columnId: string;
  rowTitle: string;
  columnName: string;
  /** 该格当前的代码视图——缺席时调用方已用 resolveCellCodeView 合成 none
   * 占位（codeText/language 空串），本组件因此永远拿到一个完整形状，无需
   * 再判断"有没有代码"这件事本身。 */
  code: KnowhowCellCode;
  canEdit: boolean;
  /** 保存成功：把最新代码视图报给上层——上层（knowhow-panel.tsx）合并进它
   * 自己按列索引的行级 map，不必整行重拉。 */
  onSaved: (columnId: string, entry: KnowhowCellCode) => void;
  /** 删除成功：通知上层把这一列从行级 map 里摘除。 */
  onDeleted: (columnId: string) => void;
  onClose: () => void;
}

export function KnowhowCodeModal({
  rowId,
  columnId,
  rowTitle,
  columnName,
  code,
  canEdit,
  onSaved,
  onDeleted,
  onClose,
}: KnowhowCodeModalProps) {
  // 空（none）时直接进编辑态——镜像格子浮窗"空格子直接进编辑态"的既有习语
  // （knowhow-cell-editor.tsx 的 openCellAuto）；有内容时先进只读查看态。
  const [mode, setMode] = useState<"view" | "edit">(code.status === "none" ? "edit" : "view");
  const [codeText, setCodeText] = useState(code.codeText);
  const [language, setLanguage] = useState(code.language);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [deleteConfirm, setDeleteConfirm] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [closeGuard, setCloseGuard] = useState(false);
  // 本浮层没有全屏概念（任务表未列出）——不传 disabled，拖动/resize 恒生效。
  const floating = useFloatingWindow({ storageKey: "knowhow.codeModal.window" });

  function requestClose() {
    if (closeGuard) {
      onClose();
      return;
    }
    if (codeEditorIsDirty(codeText, language, code)) {
      setCloseGuard(true);
    } else {
      onClose();
    }
  }

  useEffect(() => {
    function handleKeyDown(event: globalThis.KeyboardEvent) {
      if (event.key === "Escape") {
        event.preventDefault();
        requestClose();
      }
    }
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [closeGuard, codeText, language, code]);

  function handleBackdropClick(event: ReactMouseEvent<HTMLDivElement>) {
    if (event.currentTarget === event.target) requestClose();
  }

  function handleStartEdit() {
    setSaveError(null);
    setMode("edit");
  }

  function handleCancelEdit() {
    // 从"添加代码"（none）直接进来的编辑态没有可回退的查看态，取消即关闭
    // 整个浮层——镜像 KnowhowCellEditor 对空格子取消行为的既有心智。
    if (code.status === "none") {
      onClose();
      return;
    }
    setCodeText(code.codeText);
    setLanguage(code.language);
    setSaveError(null);
    setMode("view");
  }

  async function handleSave() {
    const disabledReason = codeSaveDisabledReason(codeText);
    if (disabledReason) {
      setSaveError(disabledReason);
      return;
    }
    setSaving(true);
    setSaveError(null);
    try {
      const result = await putCellCode(rowId, columnId, codeText, normalizeLanguageInput(language));
      onSaved(columnId, result);
      setCodeText(result.codeText);
      setLanguage(result.language);
      setMode("view");
    } catch (err) {
      setSaveError(extractErrorMessage(err, CODE_SAVE_ERROR_FALLBACK));
    } finally {
      setSaving(false);
    }
  }

  async function handleDelete() {
    setDeleting(true);
    setDeleteError(null);
    try {
      await deleteCellCode(rowId, columnId);
      onDeleted(columnId);
      onClose();
    } catch (err) {
      setDeleteError(extractErrorMessage(err, CODE_DELETE_ERROR_FALLBACK));
      setDeleteConfirm(false);
      setDeleting(false);
    }
  }

  async function handleCopy() {
    const strategy = resolveCopyStrategy(Boolean(navigator.clipboard?.writeText));
    try {
      if (strategy === "clipboard-api") {
        await navigator.clipboard.writeText(code.codeText);
      } else {
        // execCommand 兜底（不支持 Clipboard API 的旧浏览器/非安全上下文）：
        // 镜像 answer-panel.tsx 的 copyTextToClipboard 既有习语——隐藏
        // textarea 承载待复制文本，select()+execCommand('copy') 后即时移除。
        const textarea = document.createElement("textarea");
        textarea.value = code.codeText;
        textarea.setAttribute("readonly", "true");
        textarea.style.position = "fixed";
        textarea.style.opacity = "0";
        document.body.appendChild(textarea);
        textarea.select();
        document.execCommand("copy");
        document.body.removeChild(textarea);
      }
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1200);
    } catch {
      /* clipboard 被浏览器拒绝（权限/非安全上下文）——静默忽略，同既有
         CopyButton 习语，不为"复制"这个辅助功能弹错误打断用户。 */
    }
  }

  const tone = CODE_STATUS_TONE[code.status];

  return (
    <div className="kh-modal-overlay" onClick={handleBackdropClick}>
      <div
        ref={floating.cardRef}
        className="kh-modal-card"
        style={floating.style}
        role="dialog"
        aria-modal="true"
        aria-label={`${rowTitle} › ${columnName} › 代码`}
        onClick={(event) => event.stopPropagation()}
      >
        <header className="kh-modal-header" {...floating.dragHandleProps}>
          <div className="kh-modal-header-top">
            <div className="kh-modal-breadcrumb">
              <span className="kh-modal-row-title" title={rowTitle}>
                {rowTitle}
              </span>
              <span className="kh-modal-sep">›</span>
              <span className="kh-modal-col-name">{columnName}</span>
              <span className="kh-modal-sep">›</span>
              <span className="kh-modal-col-name">代码</span>
              {mode === "view" && (
                <span className={`knowhow-status-badge knowhow-status-badge--${tone}`}>
                  {CODE_STATUS_LABELS[code.status]}
                </span>
              )}
            </div>
            <div className="kh-modal-header-actions">
              {mode === "view" && (
                <button type="button" className="kh-toolbar-button" onClick={handleCopy} title="复制代码">
                  {copied ? <Check size={14} /> : <Copy size={14} />} {copied ? COPIED_CODE_LABEL : COPY_CODE_LABEL}
                </button>
              )}
              {mode === "view" && canEdit && (
                <button type="button" className="kh-preview-edit-button" onClick={handleStartEdit}>
                  <Edit3 size={14} /> {EDIT_CODE_LABEL}
                </button>
              )}
              {mode === "view" &&
                canEdit &&
                code.status !== "none" &&
                (deleteConfirm ? (
                  <span className="knowhow-confirm">
                    <span>{DELETE_CODE_CONFIRM_TEXT}</span>
                    <button type="button" className="knowhow-confirm-yes" disabled={deleting} onClick={handleDelete}>
                      {deleting ? "删除中…" : "确认删除"}
                    </button>
                    <button type="button" className="knowhow-confirm-no" onClick={() => setDeleteConfirm(false)}>
                      取消
                    </button>
                  </span>
                ) : (
                  <button
                    type="button"
                    className="icon-button"
                    title={DELETE_CODE_TITLE}
                    onClick={() => setDeleteConfirm(true)}
                  >
                    <Trash2 size={16} />
                  </button>
                ))}
              <button type="button" className="icon-button" title="关闭" onClick={requestClose}>
                <X size={18} />
              </button>
            </div>
          </div>
        </header>

        <div className="kh-modal-body">
          {mode === "view" ? (
            <>
              <p className="kh-code-explain">{CODE_STATUS_EXPLANATIONS[code.status]}</p>
              {deleteError && <p className="kh-inline-error">{deleteError}</p>}
              <div className="kh-code-lang-row">
                <span className="kh-code-lang-tag">{code.language || NO_LANGUAGE_TAG_TEXT}</span>
                {code.updatedAt && (
                  <span className="kh-code-updated">
                    最近更新：{new Date(code.updatedAt).toLocaleString("zh-CN")}
                    {codeProvenanceSuffix(code.updatedBy)}
                  </span>
                )}
              </div>
              <pre className="kh-code-block">
                <code>{code.codeText}</code>
              </pre>
            </>
          ) : (
            <>
              {saveError && <p className="kh-inline-error">{saveError}</p>}
              <div className="kh-code-lang-row">
                <input
                  type="text"
                  className="kh-code-lang-input"
                  value={language}
                  disabled={saving}
                  onChange={(event) => setLanguage(event.target.value)}
                  placeholder={LANGUAGE_INPUT_PLACEHOLDER}
                />
              </div>
              <textarea
                className="kh-textarea kh-code-textarea"
                value={codeText}
                disabled={saving}
                onChange={(event) => setCodeText(event.target.value)}
                placeholder={CODE_TEXTAREA_PLACEHOLDER}
                autoFocus
              />
            </>
          )}
        </div>

        <footer className="kh-modal-footer">
          {closeGuard ? (
            <div className="kh-close-guard">
              <span>{CODE_CLOSE_GUARD_MESSAGE}</span>
              <div className="kh-close-guard-actions">
                <button type="button" onClick={() => setCloseGuard(false)}>
                  {CODE_CLOSE_GUARD_CONTINUE_LABEL}
                </button>
                <button type="button" className="kh-danger-button" onClick={onClose}>
                  {CODE_CLOSE_GUARD_DISCARD_LABEL}
                </button>
              </div>
            </div>
          ) : mode === "edit" ? (
            <div className="kh-footer-actions">
              <button type="button" onClick={handleCancelEdit} disabled={saving}>
                {CANCEL_CODE_LABEL}
              </button>
              <button type="button" className="kh-primary-button" onClick={handleSave} disabled={saving}>
                <Check size={14} /> {saving ? "保存中…" : SAVE_CODE_LABEL}
              </button>
            </div>
          ) : null}
        </footer>
        <span className="kh-modal-resize-handle" aria-hidden="true" {...floating.resizeHandleProps} />
      </div>
    </div>
  );
}
