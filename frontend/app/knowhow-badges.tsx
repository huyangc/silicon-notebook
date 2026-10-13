/**
 * knowhow-badges.tsx
 *
 * Knowhow 面板的两个小徽章：列角色徽章 RoleBadge（网格表头与行详情抽屉分节
 * 共用）和行同步状态徽章 ProjectionStatusBadge（网格每行末列，可写时带「重试」）。
 * 从 knowhow-panel.tsx 原样搬来（S4-d）：组件体与注释一字未改，只把声明改为
 * export。样式仍由 knowhow-panel-styles.tsx 的 `<style jsx global>` 提供。
 */
"use client";

import { RefreshCw } from "lucide-react";
import { ROLE_LABELS, type Role, type ProjectionStatus } from "./knowhow-model.ts";
import {
  PROJECTION_STATUS_LABELS,
  PROJECTION_STATUS_TONE,
  isRetryableProjectionStatus,
} from "./knowhow-panel-logic.ts";

// ---------------------------------------------------------------------------
// 小组件：角色徽章 / 投影状态徽章
// ---------------------------------------------------------------------------

export function RoleBadge({ role }: { role: Role }) {
  // `attribute` 是列的默认类型，标签「普通」对用户零信息量；渲染成灰底灰字的
  // 小胶囊反而是视觉噪音（用户报告为「空胶囊 bug」）。只对具备独立语义的
  // anchor(行标题)/procedure(方法步骤)/entity(工具/事物) 三值显示徽章。
  if (role === "attribute") return null;
  return <span className={`knowhow-role-badge knowhow-role-badge--${role}`}>{ROLE_LABELS[role]}</span>;
}

export function ProjectionStatusBadge({
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
          title="重新同步整张表（后台执行）"
        >
          <RefreshCw size={12} className={retrying ? "knowhow-spin" : undefined} />
          {!retrying && "重试"}
        </button>
      )}
    </span>
  );
}
