/**
 * knowhow-panel.tsx
 *
 * 「Knowhow 表」总览：表列表 → 表格网格 → 行详情抽屉 三层，外加导入向导 /
 * 建表向导 / 表管理三个 modal 的挂载点。page.tsx 只负责接线（导航按钮 +
 * `<KnowhowPanel notebookId apiBase canEdit onClose />` 一处挂载），面板自身
 * 的状态机与渲染都集中在这里。
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
 * knowhow-import.tsx；格子内容编辑浮窗属 Task 7（本文件网格暂仍只读展示格子
 * 内容）。本文件对这些 modal 只做「打开/关闭 + 完成后刷新」的接线。
 */
"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import "katex/dist/katex.min.css";
import { ChevronLeft, ListPlus, Plus, RefreshCw, Search, Settings2, Trash2, Upload, X } from "lucide-react";
import {
  ROLE_LABELS,
  rewriteAssetUrls,
  cellSummary,
  fetchKnowhowTables,
  fetchKnowhowTable,
  deleteKnowhowTable,
  reprojectKnowhowTable,
  addKnowhowRow,
  type KnowhowTableSummary,
  type KnowhowTableDetail,
  type KnowhowRow,
  type KnowhowColumn,
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
  isInternalAssetUrl,
} from "./knowhow-panel-logic.ts";
import { extractErrorMessage } from "./knowhow-import-logic.ts";
import { authHeaders } from "./auth.ts";
import { KnowhowImportWizard } from "./knowhow-import.tsx";
import { KnowhowCreateWizard, KnowhowManageModal } from "./knowhow-manage.tsx";

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
}

export function KnowhowPanel({ notebookId, apiBase, canEdit, onClose }: KnowhowPanelProps) {
  const [tables, setTables] = useState<KnowhowTableSummary[] | null>(null);
  const [tablesLoading, setTablesLoading] = useState(false);
  const [tablesError, setTablesError] = useState<string | null>(null);

  // 三个 modal 的显隐：面板自持状态，不经 page.tsx 转发——page.tsx 挂载
  // <KnowhowPanel> 时只传 notebookId/apiBase/canEdit/onClose 四个 prop，
  // 导入/新建/管理入口完全由本文件内部驱动。
  const [importOpen, setImportOpen] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [manageOpen, setManageOpen] = useState(false);

  const [selectedTableId, setSelectedTableId] = useState<string | null>(null);
  const [detail, setDetail] = useState<KnowhowTableDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const [query, setQuery] = useState("");
  const [openRowId, setOpenRowId] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [retryingReproject, setRetryingReproject] = useState(false);
  const [addingRow, setAddingRow] = useState(false);

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

  const loadDetail = useCallback(
    (tableId: string) => {
      setDetailLoading(true);
      setDetailError(null);
      fetchKnowhowTable(notebookId, tableId)
        .then((data) => setDetail(data))
        .catch(() => setDetailError("加载表格详情失败，请重试"))
        .finally(() => setDetailLoading(false));
    },
    [notebookId],
  );

  useEffect(() => {
    if (selectedTableId) loadDetail(selectedTableId);
  }, [selectedTableId, loadDetail]);

  // notebook 切换时(理论上面板会被 page.tsx 一并卸载，这里仅作兜底)整体复位，
  // 避免残留上一个 notebook 的表选中态。
  useEffect(() => {
    setSelectedTableId(null);
    setDetail(null);
    setDetailError(null);
    setActionError(null);
    setQuery("");
    setOpenRowId(null);
    setConfirmDelete(false);
    setManageOpen(false);
    setCreateOpen(false);
  }, [notebookId]);

  function openTable(tableId: string) {
    setSelectedTableId(tableId);
    setDetail(null);
    setDetailError(null);
    setActionError(null);
    setQuery("");
    setOpenRowId(null);
    setConfirmDelete(false);
    setManageOpen(false);
  }

  function backToList() {
    setSelectedTableId(null);
    setDetail(null);
    setDetailError(null);
    setActionError(null);
    setQuery("");
    setOpenRowId(null);
    setConfirmDelete(false);
    setManageOpen(false);
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

  // 「添加行」：建空行后重拉详情。逐格填写浮窗（点空格直入编辑）属 Task 7，
  // 落地后新行的引导自然衔接到编辑态；当前先把「行出现在网格里」这一步走通。
  async function addRow() {
    if (!selectedTableId || addingRow) return;
    setAddingRow(true);
    setActionError(null);
    try {
      await addKnowhowRow(notebookId, selectedTableId, { cells: {} });
      loadDetail(selectedTableId);
      loadTables();
    } catch (err) {
      setActionError(extractErrorMessage(err, "添加行失败，请重试"));
    } finally {
      setAddingRow(false);
    }
  }

  // 管理 modal 里任一写操作成功：重拉表详情（modal 拿到新 detail prop 原地
  // 刷新）+ 表列表（标题/行数在列表卡片上要跟着变）。
  function handleManageChanged() {
    if (selectedTableId) loadDetail(selectedTableId);
    loadTables();
  }

  const openRow = detail?.rows.find((row) => row.id === openRowId) ?? null;

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
          />
        )}
      </div>

      {openRow && detail && (
        <KnowhowRowDrawer
          row={openRow}
          columns={detail.columns}
          notebookId={notebookId}
          apiBase={apiBase}
          onClose={() => setOpenRowId(null)}
        />
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
}) {
  const orderedColumns = useMemo(() => (detail ? orderColumnsForGrid(detail.columns) : []), [detail]);
  const filteredRows = useMemo(() => (detail ? filterRows(detail.rows, query) : []), [detail, query]);

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
        {/* 全部写入口（添加行/管理/重建投影/删除表）只对 canEdit 出现；只读
            成员的工具栏只剩返回与标题（规格⑦）。 */}
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
                        <span className="knowhow-col-name" title={column.name}>
                          {column.name}
                        </span>
                        <RoleBadge role={column.role} />
                      </div>
                    </th>
                  ))}
                  <th>同步状态</th>
                </tr>
              </thead>
              <tbody>
                {filteredRows.map((row) => (
                  <tr key={row.id}>
                    {orderedColumns.map((column, index) => {
                      const text = cellSummary(row.cells[column.id] ?? "");
                      return (
                        <td key={column.id}>
                          {index === 0 ? (
                            <button
                              type="button"
                              className="knowhow-cell-open"
                              onClick={() => onOpenRow(row.id)}
                              title={text || "打开行详情"}
                            >
                              {text || "—"}
                            </button>
                          ) : (
                            <span className="knowhow-cell-text" title={text || undefined}>
                              {text || "—"}
                            </span>
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
                ))}
              </tbody>
            </table>
            {filteredRows.length === 0 && (
              <p className="knowhow-no-match">
                {detail.rows.length === 0
                  ? canEdit
                    ? "这张表还没有行，点上方「添加行」开始填写。"
                    : "这张表还没有行。"
                  : `没有匹配「${query}」的行。`}
              </p>
            )}
          </div>
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
  onClose,
}: {
  row: KnowhowRow;
  columns: KnowhowColumn[];
  notebookId: string;
  apiBase: string;
  onClose: () => void;
}) {
  useEffect(() => {
    function handleKeyDown(event: globalThis.KeyboardEvent) {
      if (event.key === "Escape") onClose();
    }
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [onClose]);

  const orderedColumns = useMemo(() => sortColumnsByPosition(columns), [columns]);
  const titleText = cellSummary(resolveRowTitleText(row, columns), 200) || "行详情";

  return (
    <>
      <button type="button" className="knowhow-drawer-backdrop" aria-label="关闭行详情" onClick={onClose} />
      <section className="knowhow-drawer" role="dialog" aria-modal="true" aria-label={titleText}>
        <div className="knowhow-drawer-header">
          <h2>{titleText}</h2>
          <button className="icon-button" onClick={onClose} title="关闭">
            <X size={20} />
          </button>
        </div>
        <div className="knowhow-drawer-body">
          {orderedColumns.map((column) => (
            <section key={column.id}>
              <div className="knowhow-drawer-section-head">
                <h4>{column.name}</h4>
                <RoleBadge role={column.role} />
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
// 小组件：角色徽章 / 投影状态徽章
// ---------------------------------------------------------------------------

function RoleBadge({ role }: { role: Role }) {
  return <span className={`knowhow-role-badge knowhow-role-badge--${role}`}>{ROLE_LABELS[role]}</span>;
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

// ---------------------------------------------------------------------------
// KnowhowMarkdown — 复用 answer-markdown.tsx 的 GFM+KaTeX 渲染管线
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

function KnowhowMarkdown({ md, notebookId, apiBase }: { md: string; notebookId: string; apiBase: string }) {
  const content = rewriteAssetUrls(md ?? "", notebookId, apiBase);

  const components = useMemo<Components>(
    () => ({
      // 格子内容可能包含普通链接（如工具文档 URL）；不强制新标签打开的话
      // 点击会在同一个 SPA 标签页里跳走，丢失当前 notebook/knowhow 视图上下文。
      a({ href, children }) {
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
        return <KnowhowImage src={typeof src === "string" ? src : undefined} alt={alt} apiBase={apiBase} />;
      },
    }),
    [apiBase],
  );

  if (!content.trim()) {
    return <p className="knowhow-empty-cell">（空）</p>;
  }

  return (
    <div className="answer-markdown knowhow-markdown">
      <ReactMarkdown remarkPlugins={[remarkGfm, remarkMath]} rehypePlugins={[rehypeKatex]} components={components}>
        {content}
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
    setFailed(false);
    setBlobUrl(null);
    fetch(src, { headers: authHeaders() })
      .then((res) => {
        if (!res.ok) throw new Error(String(res.status));
        return res.blob();
      })
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
      if (created) URL.revokeObjectURL(created);
    };
  }, [src, needsAuth]);

  if (!src) return null;
  if (!needsAuth) return <img src={src} alt={alt ?? ""} className="knowhow-image" />;
  if (failed) return <span className="knowhow-image-fallback">（图片加载失败{alt ? `：${alt}` : ""}）</span>;
  if (!blobUrl) return <span className="knowhow-image-loading">（图片加载中…）</span>;
  return <img src={blobUrl} alt={alt ?? ""} className="knowhow-image" />;
}
