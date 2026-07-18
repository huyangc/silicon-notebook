/**
 * knowhow-manage.tsx
 *
 * Knowhow 表编辑维护的两个 modal（PR-2+3 Task 5），由 knowhow-panel.tsx 在
 * canEdit（notebook 写权限）下挂载：
 *
 *   · KnowhowCreateWizard —— 建表向导「定表头」：列名输入 + 内容类型下拉
 *     （三项：方法步骤/工具/事物/普通，规格①）+ 增删/上下移 + 表级
 *     「行标题列：[某列 ▾ / 不设置]」选择器（带规格①提示语）→ 创建空表，
 *     成功后由面板打开网格、以「添加行」引导填值（逐格填写属 Task 7）。
 *   · KnowhowManageModal —— 管理菜单收拢：表信息（标题/描述）、行标题列
 *     设置（选择器 + 无行标题提示语）、列管理（加列/重命名/改类型/删列）、
 *     行管理（删行）。破坏性操作（删列/删行）走行内确认层，确认文案明示
 *     「连格子与代码附件一并删除」。
 *
 * 纯逻辑（向导表头状态机 / 选项与提示语 / payload 组装 / 表信息 patch）都在
 * knowhow-manage-logic.ts（无 JSX，供 knowhow-manage.test.mjs 直接 import——
 * Node 原生 TS 类型剥离不支持 .tsx）。本文件只做状态接线与渲染。
 *
 * 样式：namespaced `knowhow-manage-*` class + `<style jsx global>`（镜像
 * knowhow-import.tsx 的自成一体写法），消费与面板同一套 CSS 变量。
 */
"use client";

import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { ArrowDown, ArrowUp, Loader2, Plus, Trash2, X } from "lucide-react";
import { useFloatingWindow, type UseFloatingWindowResult } from "./use-floating-window.ts";
import {
  cellSummary,
  createKnowhowTable,
  patchKnowhowTable,
  addKnowhowColumn,
  patchKnowhowColumn,
  deleteKnowhowColumn,
  deleteKnowhowRow,
  type ColumnKind,
  type KnowhowTableDetail,
} from "./knowhow-model.ts";
import {
  KIND_HINTS,
  KIND_OPTIONS,
  ANCHOR_SELECTOR_LABEL,
  ANCHOR_NONE_LABEL,
  ANCHOR_SET_HINT,
  anchorHint,
  initialWizardState,
  addWizardColumn,
  updateWizardColumn,
  removeWizardColumn,
  moveWizardColumn,
  setWizardAnchor,
  createValidationError,
  assembleCreatePayload,
  COLUMN_DELETE_CONFIRM,
  ROW_DELETE_CONFIRM,
  tableMetaPatch,
  hasMetaChanges,
} from "./knowhow-manage-logic.ts";
import { isBlankTitle, extractErrorMessage } from "./knowhow-import-logic.ts";
import { sortColumnsByPosition, resolveRowTitleText } from "./knowhow-panel-logic.ts";

// ---------------------------------------------------------------------------
// 共用小件：内容类型图例（规格①「各带一行提示语」——下拉里放不下整句提示，
// 统一用图例逐行展示，下拉项上另有 title 悬浮提示）
// ---------------------------------------------------------------------------

export function KindLegend() {
  return (
    <>
      <ul className="knowhow-manage-kind-legend">
        {KIND_OPTIONS.map((option) => (
          <li key={option.value}>
            <strong>{option.label}</strong>
            <span>{option.hint}</span>
          </li>
        ))}
      </ul>
      {/* 图例样式随组件自带（而非挂在 ManageStyles 里）：导入向导（knowhow-
          import.tsx）也复用本图例，且可能在管理/建表 modal 未挂载时单独出现，
          样式必须跟人走。styled-jsx 对相同内容的 global 样式自动去重。 */}
      <style jsx global>{`
        .knowhow-manage-kind-legend {
          margin: 0;
          padding: 10px 14px;
          list-style: none;
          display: grid;
          gap: 4px;
          border: 1px solid var(--line);
          border-radius: 10px;
          background: var(--soft);
        }

        .knowhow-manage-kind-legend li {
          display: flex;
          gap: 8px;
          font-size: 12px;
          color: var(--muted);
          line-height: 1.5;
        }

        .knowhow-manage-kind-legend strong {
          flex: 0 0 auto;
          color: var(--ink);
          font-weight: 600;
        }
      `}</style>
    </>
  );
}

// ---------------------------------------------------------------------------
// KnowhowCreateWizard — 建表向导（定表头 → 创建空表）
// ---------------------------------------------------------------------------

export interface KnowhowCreateWizardProps {
  notebookId: string;
  /** 用户主动取消/关闭（未建表）。 */
  onClose: () => void;
  /** 建表成功：面板据此刷新表列表并直接打开新表网格（「添加行」引导）。 */
  onCreated: (detail: KnowhowTableDetail) => void;
}

export function KnowhowCreateWizard({ notebookId, onClose, onCreated }: KnowhowCreateWizardProps) {
  const [title, setTitle] = useState("");
  const [header, setHeader] = useState(initialWizardState);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  // 卸载后忽略迟到的 then/catch（镜像 knowhow-import.tsx 的 mountedRef 守卫，
  // 含 StrictMode remount 重置）。
  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  // 建表请求进行中不允许关闭（成功后面板要打开新表；提前关掉会让表在后台
  // 悄悄建成而列表不刷新）。
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
    // eslint-disable-next-line react-hooks/exhaustive-deps -- 同
    // knowhow-import.tsx：列 requestClose 实际捕获的值，而非每渲染新引用的函数。
  }, [onClose, submitting]);

  const validationError = createValidationError(title, header.columns);
  const submitDisabled = submitting || validationError !== null;
  // 校验提示的展示时机：用户已开始输入（标题或任一列名非空，或动过列结构）
  // 才显示——刚打开向导就满屏「不能为空」是在训人，不是在帮人。
  const touched =
    title.trim() !== "" || header.columns.some((column) => column.name.trim() !== "") || header.columns.length !== 1;

  async function handleCreate() {
    if (submitDisabled) return;
    setSubmitting(true);
    setSubmitError(null);
    try {
      const detail = await createKnowhowTable(notebookId, assembleCreatePayload(title, header));
      if (!mountedRef.current) return;
      onCreated(detail);
    } catch (err) {
      if (!mountedRef.current) return;
      setSubmitError(extractErrorMessage(err, "创建失败，请重试"));
    } finally {
      if (mountedRef.current) setSubmitting(false);
    }
  }

  return (
    <ManageModalShell
      label="新建 Knowhow 表"
      storageKey="knowhow.createWizard.window"
      onRequestClose={requestClose}
      closeDisabled={submitting}
    >
      {(floating) => (
        <>
          <div className="knowhow-manage-header-row" {...floating.dragHandleProps}>
            <div>
              <h2>新建 Knowhow 表</h2>
              <p className="knowhow-manage-subtitle">先定表头（列名 + 内容类型 + 行标题列），创建后再逐行填值。</p>
            </div>
            <button
              type="button"
              className="icon-button"
              onClick={requestClose}
              disabled={submitting}
              title={submitting ? "创建进行中，请稍候" : "关闭"}
            >
              <X size={20} />
            </button>
          </div>

          <div className="knowhow-manage-body">
            <label className="knowhow-manage-field">
              <span>表标题</span>
              <input
                type="text"
                value={title}
                onChange={(event) => setTitle(event.target.value)}
                disabled={submitting}
                placeholder="例如 时序违例修复"
              />
            </label>

            <div className="knowhow-manage-columns-head">
              <span>列定义</span>
              <button
                type="button"
                className="sort-button knowhow-manage-small-button"
                onClick={() => setHeader(addWizardColumn)}
                disabled={submitting}
              >
                <Plus size={14} /> 加一列
              </button>
            </div>

            <div className="knowhow-manage-column-list">
              {header.columns.map((column, index) => (
                <div className="knowhow-manage-column-row knowhow-manage-column-row--wizard" key={index}>
                  <input
                    type="text"
                    className="knowhow-manage-col-input"
                    value={column.name}
                    placeholder={`列 ${index + 1} 名称`}
                    onChange={(event) => setHeader((state) => updateWizardColumn(state, index, { name: event.target.value }))}
                    disabled={submitting}
                  />
                  {header.anchorIndex === index ? (
                    <span className="knowhow-manage-anchor-badge" title={ANCHOR_SET_HINT}>
                      行标题
                    </span>
                  ) : (
                    <select
                      className="knowhow-manage-kind-select"
                      value={column.kind}
                      title={KIND_HINTS[column.kind]}
                      onChange={(event) =>
                        setHeader((state) => updateWizardColumn(state, index, { kind: event.target.value as ColumnKind }))
                      }
                      disabled={submitting}
                    >
                      {KIND_OPTIONS.map((option) => (
                        <option key={option.value} value={option.value} title={option.hint}>
                          {option.label}
                        </option>
                      ))}
                    </select>
                  )}
                  <div className="knowhow-manage-column-ops">
                    <button
                      type="button"
                      className="knowhow-manage-op-button"
                      title="上移"
                      disabled={submitting || index === 0}
                      onClick={() => setHeader((state) => moveWizardColumn(state, index, -1))}
                    >
                      <ArrowUp size={14} />
                    </button>
                    <button
                      type="button"
                      className="knowhow-manage-op-button"
                      title="下移"
                      disabled={submitting || index === header.columns.length - 1}
                      onClick={() => setHeader((state) => moveWizardColumn(state, index, 1))}
                    >
                      <ArrowDown size={14} />
                    </button>
                    <button
                      type="button"
                      className="knowhow-manage-op-button knowhow-manage-op-danger"
                      title="删除该列"
                      disabled={submitting}
                      onClick={() => setHeader((state) => removeWizardColumn(state, index))}
                    >
                      <Trash2 size={14} />
                    </button>
                  </div>
                </div>
              ))}
            </div>

            <div className="knowhow-manage-anchor-row">
              <label className="knowhow-manage-anchor-label">
                <span>{ANCHOR_SELECTOR_LABEL}</span>
                <select
                  value={header.anchorIndex === null ? "" : String(header.anchorIndex)}
                  onChange={(event) =>
                    setHeader((state) =>
                      setWizardAnchor(state, event.target.value === "" ? null : Number(event.target.value)),
                    )
                  }
                  disabled={submitting}
                >
                  <option value="">{ANCHOR_NONE_LABEL}</option>
                  {header.columns.map((column, index) => (
                    <option key={index} value={String(index)}>
                      {column.name.trim() || `列 ${index + 1}`}
                    </option>
                  ))}
                </select>
              </label>
              <p className={`knowhow-manage-hint${header.anchorIndex === null ? " is-none" : ""}`}>
                {anchorHint(header.anchorIndex)}
              </p>
            </div>

            <KindLegend />

            {touched && validationError && <p className="knowhow-manage-warning">{validationError}</p>}

            {submitError && (
              <div className="knowhow-manage-error">
                <span>{submitError}</span>
                <button type="button" onClick={() => setSubmitError(null)} title="关闭">
                  <X size={14} />
                </button>
              </div>
            )}

            <div className="knowhow-manage-actions">
              <button
                type="button"
                className="new-pill knowhow-manage-submit-button"
                disabled={submitDisabled}
                onClick={handleCreate}
              >
                {submitting && <Loader2 size={14} className="knowhow-manage-spin" />}
                {submitting ? "创建中…" : "创建"}
              </button>
            </div>
          </div>
        </>
      )}
    </ManageModalShell>
  );
}

// ---------------------------------------------------------------------------
// KnowhowManageModal — 表管理（表信息 / 行标题列 / 列 / 行）
// ---------------------------------------------------------------------------

export interface KnowhowManageModalProps {
  notebookId: string;
  detail: KnowhowTableDetail;
  onClose: () => void;
  /** 任一写操作成功后触发：面板据此重拉表详情与表列表，modal 拿到新 detail
   * prop 原地刷新（不关闭，用户常连续做多个管理动作）。 */
  onChanged: () => void;
}

export function KnowhowManageModal({ notebookId, detail, onClose, onChanged }: KnowhowManageModalProps) {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  function requestClose() {
    if (pending) return;
    onClose();
  }

  useEffect(() => {
    function handleKeyDown(event: globalThis.KeyboardEvent) {
      if (event.key === "Escape") requestClose();
    }
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- 同上：列实际捕获值。
  }, [onClose, pending]);

  // 所有写操作的统一执行器：串行（pending 期间控件禁用）、失败给中文横幅、
  // 成功回调 onChanged 让面板重拉数据。
  async function run(action: () => Promise<unknown>) {
    setPending(true);
    setError(null);
    try {
      await action();
      if (mountedRef.current) onChanged();
    } catch (err) {
      if (mountedRef.current) setError(extractErrorMessage(err));
    } finally {
      if (mountedRef.current) setPending(false);
    }
  }

  const orderedColumns = useMemo(() => sortColumnsByPosition(detail.columns), [detail.columns]);
  const orderedRows = useMemo(() => [...detail.rows].sort((a, b) => a.position - b.position), [detail.rows]);

  // --- 表信息草稿（保存按钮仅在真有变化且标题非空时可用）---
  const [titleDraft, setTitleDraft] = useState(detail.title);
  const [descDraft, setDescDraft] = useState(detail.description);
  useEffect(() => {
    // 只在切换到另一张表时重置草稿；同表的 detail 刷新（行/列操作后重拉）
    // 不能冲掉用户未保存的表信息编辑。
    setTitleDraft(detail.title);
    setDescDraft(detail.description);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [detail.id]);
  const metaPatch = tableMetaPatch(detail, titleDraft, descDraft);
  const metaSaveDisabled = pending || isBlankTitle(titleDraft) || !hasMetaChanges(metaPatch);

  // --- 加列表单 ---
  const [newColName, setNewColName] = useState("");
  const [newColKind, setNewColKind] = useState<ColumnKind>("attribute");

  return (
    <ManageModalShell
      label={`表管理：${detail.title}`}
      storageKey="knowhow.manage.window"
      onRequestClose={requestClose}
      closeDisabled={pending}
    >
      {(floating) => (
        <>
          <div className="knowhow-manage-header-row" {...floating.dragHandleProps}>
            <div>
              <h2>表管理</h2>
              <p className="knowhow-manage-subtitle" title={detail.title}>
                {detail.title}
              </p>
            </div>
            <button
              type="button"
              className="icon-button"
              onClick={requestClose}
              disabled={pending}
              title={pending ? "操作进行中，请稍候" : "关闭"}
            >
              <X size={20} />
            </button>
          </div>

          <div className="knowhow-manage-body">
            {error && (
              <div className="knowhow-manage-error">
                <span>{error}</span>
                <button type="button" onClick={() => setError(null)} title="关闭">
                  <X size={14} />
                </button>
              </div>
            )}

            {/* --- 表信息 --- */}
            <section className="knowhow-manage-section">
              <h3>表信息</h3>
              <label className="knowhow-manage-field">
                <span>标题</span>
                <input
                  type="text"
                  value={titleDraft}
                  onChange={(event) => setTitleDraft(event.target.value)}
                  disabled={pending}
                />
              </label>
              <label className="knowhow-manage-field">
                <span>描述</span>
                <textarea
                  rows={2}
                  value={descDraft}
                  onChange={(event) => setDescDraft(event.target.value)}
                  disabled={pending}
                  placeholder="这张表沉淀什么知识（可选）"
                />
              </label>
              <div className="knowhow-manage-inline-actions">
                <button
                  type="button"
                  className="sort-button knowhow-manage-small-button"
                  disabled={metaSaveDisabled}
                  onClick={() => run(() => patchKnowhowTable(notebookId, detail.id, metaPatch))}
                >
                  保存
                </button>
              </div>
            </section>

            {/* --- 行标题列 --- */}
            <section className="knowhow-manage-section">
              <h3>{ANCHOR_SELECTOR_LABEL}</h3>
              <div className="knowhow-manage-anchor-row">
                <label className="knowhow-manage-anchor-label">
                  <span>{ANCHOR_SELECTOR_LABEL}</span>
                  <select
                    value={detail.anchorColumnId ?? ""}
                    disabled={pending}
                    onChange={(event) => {
                      const value = event.target.value || null;
                      if (value === detail.anchorColumnId) return;
                      run(() => patchKnowhowTable(notebookId, detail.id, { anchorColumnId: value }));
                    }}
                  >
                    <option value="">{ANCHOR_NONE_LABEL}</option>
                    {orderedColumns.map((column) => (
                      <option key={column.id} value={column.id}>
                        {column.name}
                      </option>
                    ))}
                  </select>
                </label>
                <p className={`knowhow-manage-hint${detail.anchorColumnId === null ? " is-none" : ""}`}>
                  {anchorHint(detail.anchorColumnId)}
                </p>
              </div>
            </section>

            {/* --- 列管理 --- */}
            <section className="knowhow-manage-section">
              <h3>列（{orderedColumns.length}）</h3>
              <div className="knowhow-manage-column-list">
                {orderedColumns.map((column) => (
                  <ManageColumnRow
                    key={column.id}
                    name={column.name}
                    kind={column.role === "anchor" ? "attribute" : (column.role as ColumnKind)}
                    isAnchor={column.id === detail.anchorColumnId}
                    pending={pending}
                    onRename={(name) => run(() => patchKnowhowColumn(notebookId, detail.id, column.id, { name }))}
                    onKindChange={(kind) => run(() => patchKnowhowColumn(notebookId, detail.id, column.id, { kind }))}
                    onDelete={() => run(() => deleteKnowhowColumn(notebookId, detail.id, column.id))}
                  />
                ))}
              </div>
              <div className="knowhow-manage-addcol-row">
                <input
                  type="text"
                  className="knowhow-manage-col-input"
                  value={newColName}
                  placeholder="新列名称"
                  onChange={(event) => setNewColName(event.target.value)}
                  disabled={pending}
                />
                <select
                  className="knowhow-manage-kind-select"
                  value={newColKind}
                  title={KIND_HINTS[newColKind]}
                  onChange={(event) => setNewColKind(event.target.value as ColumnKind)}
                  disabled={pending}
                >
                  {KIND_OPTIONS.map((option) => (
                    <option key={option.value} value={option.value} title={option.hint}>
                      {option.label}
                    </option>
                  ))}
                </select>
                <button
                  type="button"
                  className="sort-button knowhow-manage-small-button"
                  disabled={pending || !newColName.trim()}
                  onClick={() =>
                    run(async () => {
                      await addKnowhowColumn(notebookId, detail.id, { name: newColName.trim(), kind: newColKind });
                      setNewColName("");
                    })
                  }
                >
                  <Plus size={14} /> 加列
                </button>
              </div>
              <KindLegend />
            </section>

            {/* --- 行管理 --- */}
            <section className="knowhow-manage-section">
              <h3>行（{orderedRows.length}）</h3>
              {orderedRows.length === 0 ? (
                <p className="knowhow-manage-empty">这张表还没有行。</p>
              ) : (
                <div className="knowhow-manage-row-list">
                  {orderedRows.map((row, index) => (
                    <ManageRowItem
                      key={row.id}
                      label={cellSummary(resolveRowTitleText(row, detail.columns), 60) || `行 ${index + 1}`}
                      pending={pending}
                      onDelete={() => run(() => deleteKnowhowRow(notebookId, detail.id, row.id))}
                    />
                  ))}
                </div>
              )}
            </section>
          </div>
        </>
      )}
    </ManageModalShell>
  );
}

// 列管理单行：名称改动出现「保存」、内容类型即改即存、删除走行内确认层。
// 名称草稿放在本组件内（keyed by column.id 的挂载实例），面板重拉 detail 后
// props.name 变化时同步草稿（已保存态），未保存的行/列外部变化不常见、不特判。
function ManageColumnRow({
  name,
  kind,
  isAnchor,
  pending,
  onRename,
  onKindChange,
  onDelete,
}: {
  name: string;
  kind: ColumnKind;
  isAnchor: boolean;
  pending: boolean;
  onRename: (name: string) => void;
  onKindChange: (kind: ColumnKind) => void;
  onDelete: () => void;
}) {
  const [nameDraft, setNameDraft] = useState(name);
  const [confirming, setConfirming] = useState(false);
  useEffect(() => {
    setNameDraft(name);
  }, [name]);
  const trimmed = nameDraft.trim();
  const dirty = trimmed !== "" && trimmed !== name;

  if (confirming) {
    return (
      <div className="knowhow-manage-confirm" role="alert">
        <span>{COLUMN_DELETE_CONFIRM}</span>
        <button
          type="button"
          className="knowhow-manage-confirm-yes"
          disabled={pending}
          onClick={() => {
            setConfirming(false);
            onDelete();
          }}
        >
          确认删除
        </button>
        <button type="button" className="knowhow-manage-confirm-no" onClick={() => setConfirming(false)}>
          取消
        </button>
      </div>
    );
  }

  return (
    <div className="knowhow-manage-column-row">
      <input
        type="text"
        className="knowhow-manage-col-input"
        value={nameDraft}
        onChange={(event) => setNameDraft(event.target.value)}
        disabled={pending}
        aria-label={`列名：${name}`}
      />
      <span className="knowhow-manage-save-slot">
        {dirty && (
          <button
            type="button"
            className="sort-button knowhow-manage-small-button"
            disabled={pending}
            onClick={() => onRename(trimmed)}
          >
            保存
          </button>
        )}
      </span>
      {isAnchor ? (
        <span className="knowhow-manage-anchor-badge" title="该列当前是行标题列；在上方「行标题列」选择器中更改">
          行标题
        </span>
      ) : (
        <select
          className="knowhow-manage-kind-select"
          value={kind}
          title={KIND_HINTS[kind]}
          onChange={(event) => onKindChange(event.target.value as ColumnKind)}
          disabled={pending}
        >
          {KIND_OPTIONS.map((option) => (
            <option key={option.value} value={option.value} title={option.hint}>
              {option.label}
            </option>
          ))}
        </select>
      )}
      <div className="knowhow-manage-column-ops">
        <button
          type="button"
          className="knowhow-manage-op-button knowhow-manage-op-danger"
          title="删除列"
          disabled={pending}
          onClick={() => setConfirming(true)}
        >
          <Trash2 size={14} />
        </button>
      </div>
    </div>
  );
}

function ManageRowItem({ label, pending, onDelete }: { label: string; pending: boolean; onDelete: () => void }) {
  const [confirming, setConfirming] = useState(false);

  if (confirming) {
    return (
      <div className="knowhow-manage-confirm" role="alert">
        <span>{ROW_DELETE_CONFIRM}</span>
        <button
          type="button"
          className="knowhow-manage-confirm-yes"
          disabled={pending}
          onClick={() => {
            setConfirming(false);
            onDelete();
          }}
        >
          确认删除
        </button>
        <button type="button" className="knowhow-manage-confirm-no" onClick={() => setConfirming(false)}>
          取消
        </button>
      </div>
    );
  }

  return (
    <div className="knowhow-manage-row-item">
      <span className="knowhow-manage-row-label" title={label}>
        {label}
      </span>
      <button
        type="button"
        className="knowhow-manage-op-button knowhow-manage-op-danger"
        title="删除行"
        disabled={pending}
        onClick={() => setConfirming(true)}
      >
        <Trash2 size={14} />
      </button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// modal 外壳 + 样式（backdrop 点击关闭走与 Esc 同一道 requestClose 守卫）
// ---------------------------------------------------------------------------

function ManageModalShell({
  label,
  storageKey,
  onRequestClose,
  closeDisabled,
  children,
}: {
  label: string;
  /** 两个调用点各传各的键（knowhow.createWizard.window /
   * knowhow.manage.window）——建表向导与表管理是两个独立弹窗，位置/尺寸各记
   * 各的，不共享（不像格子浮窗查看/编辑态那样是同一身份的两种渲染）。本壳
   * 只管把调用方给的键转交给 useFloatingWindow，不在这里硬编码任何字面量键。 */
  storageKey: string;
  onRequestClose: () => void;
  closeDisabled: boolean;
  /** 渲染函数而非普通 ReactNode：header（拖动手柄）由两个调用点自己渲染，
   * 但拖动状态由本壳持有的 useFloatingWindow 实例产出（单一 hook 调用点，
   * 镜像其余 7 个已接浮窗「一个组件一个 useFloatingWindow 实例」的约定，
   * 只是这里那个组件是壳、header 是子组件传入的 children）——把 floating
   * 结果通过函数参数交还给调用方，好让它们把 dragHandleProps 挂到自己的
   * header 行上、同时仍可读 floating 的其余字段。 */
  children: (floating: UseFloatingWindowResult) => ReactNode;
}) {
  // 这两个弹窗没有全屏概念（不像格子浮窗/概念矩阵抽屉），resize 手柄无条件
  // 渲染，不需要按 fullscreen 状态门控 disabled。
  const floating = useFloatingWindow({ storageKey });

  return (
    <div
      className="knowhow-manage-backdrop"
      onClick={(event) => {
        if (event.currentTarget === event.target && !closeDisabled) onRequestClose();
      }}
    >
      <section
        ref={floating.cardRef}
        className="knowhow-manage-card"
        style={floating.style}
        role="dialog"
        aria-modal="true"
        aria-label={label}
        onClick={(event) => event.stopPropagation()}
      >
        {children(floating)}
        <span className="kh-modal-resize-handle" aria-hidden="true" {...floating.resizeHandleProps} />
      </section>
      <ManageStyles />
    </div>
  );
}

function ManageStyles() {
  return (
    <style jsx global>{`
      .knowhow-manage-backdrop {
        position: fixed;
        inset: 0;
        z-index: 60;
        display: grid;
        place-items: center;
        padding: 24px;
        background: rgba(17, 24, 32, 0.36);
      }

      .knowhow-manage-card {
        width: min(720px, 100%);
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
         （见文件头注释「镜像 knowhow-import.tsx 的自成一体写法」），不能
         指望 KnowhowPanel 的全局样式恰好也已经渲染过。两份定义规则完全
         相同，互不冲突，哪个先被浏览器解析都一样。 */
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

      .knowhow-manage-header-row {
        flex: 0 0 auto;
        display: flex;
        align-items: flex-start;
        justify-content: space-between;
        gap: 18px;
        padding: 20px 24px;
        border-bottom: 1px solid var(--line);
      }

      .knowhow-manage-header-row h2 {
        margin: 0;
        font-size: 20px;
      }

      .knowhow-manage-subtitle {
        margin: 4px 0 0;
        font-size: 13px;
        color: var(--muted);
        max-width: 52ch;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }

      .knowhow-manage-header-row .icon-button:disabled {
        opacity: 0.5;
        cursor: not-allowed;
      }

      .knowhow-manage-body {
        flex: 1 1 auto;
        min-height: 0;
        overflow-y: auto;
        padding: 20px 24px 24px;
        display: flex;
        flex-direction: column;
        gap: 18px;
      }

      .knowhow-manage-section {
        display: flex;
        flex-direction: column;
        gap: 10px;
        padding-top: 14px;
        border-top: 1px solid var(--line);
      }

      .knowhow-manage-section:first-of-type {
        padding-top: 0;
        border-top: 0;
      }

      .knowhow-manage-section h3 {
        margin: 0;
        font-size: 13px;
        font-weight: 700;
        color: var(--muted);
      }

      .knowhow-manage-field {
        display: grid;
        gap: 6px;
        font-size: 13px;
        font-weight: 600;
        color: var(--ink);
      }

      .knowhow-manage-field input,
      .knowhow-manage-field textarea {
        box-sizing: border-box;
        width: 100%;
        padding: 9px 12px;
        border: 1px solid var(--line);
        border-radius: 8px;
        background: #fff;
        color: var(--ink);
        font-size: 13px;
        font-weight: 400;
        font-family: inherit;
        resize: vertical;
      }

      .knowhow-manage-field input:disabled,
      .knowhow-manage-field textarea:disabled {
        background: var(--soft);
        color: var(--muted);
      }

      .knowhow-manage-inline-actions {
        display: flex;
        justify-content: flex-end;
      }

      .knowhow-manage-small-button {
        display: inline-flex;
        align-items: center;
        gap: 5px;
        min-height: 32px;
        padding: 0 12px;
        font-size: 13px;
        white-space: nowrap;
      }

      .knowhow-manage-small-button:disabled {
        opacity: 0.55;
        cursor: not-allowed;
      }

      .knowhow-manage-columns-head {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 12px;
        font-size: 13px;
        font-weight: 600;
        color: var(--ink);
      }

      .knowhow-manage-column-list {
        display: flex;
        flex-direction: column;
        gap: 8px;
      }

      /* 列行网格：名称输入 / 保存槽（固定宽，避免按钮出现时行内跳动）/
         类型下拉或行标题徽章 / 操作按钮，同列纵向对齐。建表向导的列行没有
         保存槽（名称直接受控、无单独保存动作），加列行同理——各自三列模板。 */
      .knowhow-manage-column-row,
      .knowhow-manage-addcol-row {
        display: grid;
        grid-template-columns: minmax(0, 1fr) 64px 132px auto;
        gap: 8px;
        align-items: center;
      }

      .knowhow-manage-column-row--wizard {
        grid-template-columns: minmax(0, 1fr) 132px auto;
      }

      .knowhow-manage-addcol-row {
        grid-template-columns: minmax(0, 1fr) 132px auto;
        padding-top: 2px;
      }

      .knowhow-manage-save-slot {
        display: inline-flex;
        justify-content: center;
        min-width: 64px;
      }

      .knowhow-manage-col-input {
        box-sizing: border-box;
        width: 100%;
        min-width: 0;
        padding: 8px 10px;
        border: 1px solid var(--line);
        border-radius: 8px;
        background: #fff;
        color: var(--ink);
        font-size: 13px;
      }

      .knowhow-manage-col-input:disabled {
        background: var(--soft);
        color: var(--muted);
      }

      .knowhow-manage-kind-select,
      .knowhow-manage-anchor-label select {
        box-sizing: border-box;
        width: 100%;
        padding: 8px 8px;
        border: 1px solid var(--line);
        border-radius: 8px;
        background: #fff;
        color: var(--ink);
        font-size: 13px;
      }

      .knowhow-manage-kind-select:disabled,
      .knowhow-manage-anchor-label select:disabled {
        background: var(--soft);
        color: var(--muted);
      }

      .knowhow-manage-column-ops {
        display: inline-flex;
        align-items: center;
        gap: 4px;
      }

      .knowhow-manage-op-button {
        display: grid;
        place-items: center;
        width: 30px;
        height: 30px;
        border: 1px solid var(--line);
        border-radius: 8px;
        background: #fff;
        color: var(--muted);
        cursor: pointer;
      }

      .knowhow-manage-op-button:hover:not(:disabled) {
        border-color: var(--blue);
        color: var(--blue);
      }

      .knowhow-manage-op-button:disabled {
        opacity: 0.45;
        cursor: not-allowed;
      }

      .knowhow-manage-op-danger:hover:not(:disabled) {
        border-color: #f0c0c0;
        color: #ba2d2d;
        background: #fef2f2;
      }

      .knowhow-manage-anchor-row {
        display: flex;
        flex-direction: column;
        gap: 6px;
      }

      .knowhow-manage-anchor-label {
        display: flex;
        align-items: center;
        gap: 10px;
        font-size: 13px;
        font-weight: 600;
        color: var(--ink);
      }

      .knowhow-manage-anchor-label span {
        flex: 0 0 auto;
      }

      .knowhow-manage-anchor-label select {
        max-width: 280px;
      }

      .knowhow-manage-anchor-badge {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        font-size: 12px;
        font-weight: 600;
        padding: 6px 10px;
        border-radius: 999px;
        border: 1px solid #c7d6ff;
        background: #eef2ff;
        color: #1f5eff;
        white-space: nowrap;
      }

      .knowhow-manage-hint {
        margin: 0;
        font-size: 12px;
        color: var(--muted);
        line-height: 1.5;
      }

      .knowhow-manage-hint.is-none {
        color: #9a5b00;
      }

      .knowhow-manage-warning {
        margin: 0;
        padding: 10px 14px;
        border: 1px solid #f0dab3;
        border-radius: 8px;
        background: #fdf4e6;
        color: #9a5b00;
        font-size: 13px;
      }

      .knowhow-manage-error {
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

      .knowhow-manage-error button {
        border: 0;
        background: transparent;
        color: inherit;
        font-size: 16px;
        line-height: 1;
        cursor: pointer;
      }

      .knowhow-manage-actions {
        display: flex;
        justify-content: flex-end;
        gap: 10px;
        padding-top: 4px;
      }

      .knowhow-manage-submit-button {
        display: inline-flex;
        align-items: center;
        gap: 6px;
      }

      .knowhow-manage-submit-button:disabled {
        opacity: 0.6;
        cursor: not-allowed;
      }

      .knowhow-manage-confirm {
        display: flex;
        align-items: center;
        gap: 8px;
        padding: 8px 8px 8px 12px;
        border: 1px solid #f0c0c0;
        border-radius: 10px;
        background: #fef2f2;
        font-size: 13px;
        color: #b91c1c;
      }

      .knowhow-manage-confirm > span {
        flex: 1 1 auto;
        min-width: 0;
      }

      .knowhow-manage-confirm-yes,
      .knowhow-manage-confirm-no {
        flex: 0 0 auto;
        border: none;
        border-radius: 999px;
        padding: 5px 12px;
        font-size: 12px;
        font-weight: 600;
        cursor: pointer;
      }

      .knowhow-manage-confirm-yes {
        background: var(--red);
        color: #fff;
      }

      .knowhow-manage-confirm-yes:disabled {
        opacity: 0.6;
        cursor: not-allowed;
      }

      .knowhow-manage-confirm-no {
        background: #fff;
        color: var(--muted);
        border: 1px solid var(--line);
      }

      .knowhow-manage-row-list {
        display: flex;
        flex-direction: column;
        gap: 6px;
        max-height: 260px;
        overflow-y: auto;
        padding-right: 2px;
      }

      .knowhow-manage-row-item {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 10px;
        padding: 7px 10px;
        border: 1px solid var(--line);
        border-radius: 10px;
        background: #fff;
      }

      .knowhow-manage-row-label {
        flex: 1 1 auto;
        min-width: 0;
        font-size: 13px;
        color: var(--ink);
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }

      .knowhow-manage-empty {
        margin: 0;
        font-size: 13px;
        color: var(--muted);
        font-style: italic;
      }

      .knowhow-manage-spin {
        animation: knowhow-manage-spin 0.8s linear infinite;
      }

      @keyframes knowhow-manage-spin {
        to {
          transform: rotate(360deg);
        }
      }
    `}</style>
  );
}
