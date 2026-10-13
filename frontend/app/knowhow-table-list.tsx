/**
 * knowhow-table-list.tsx
 *
 * Knowhow 面板第一层「表列表」的呈现：健康筛选下拉、新建/导入入口、卡片清单、
 * 空态与分页（每页 24 张）。从 knowhow-panel.tsx 原样搬来（S4-d）：组件体与
 * 段落注释一字未改，只把声明改为 export 供面板 import。
 *
 * 数据与 loadTables 在 use-knowhow-table-list.ts，面板只做接线。样式仍由
 * knowhow-panel-styles.tsx 的 `<style jsx global>` 提供，本文件不含 styled-jsx。
 */
"use client";

import { Plus, Upload } from "lucide-react";
import { Pagination } from "./Pagination";
import { useClientPagination } from "./use-client-pagination.ts";
import type { KnowhowHealthFilter, KnowhowTableSummary } from "./knowhow-model.ts";

// ---------------------------------------------------------------------------
// Layer 1 — 表列表
// ---------------------------------------------------------------------------

/** Knowhow 表卡片清单每页条数。接口一次性返回整份清单(GET /notebooks/{id}/knowhow
 * 不分页),分页只发生在界面。 */
const KNOWHOW_TABLE_PAGE_SIZE = 24;

export function KnowhowTableList({
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
