// Knowhow 表面板 — 纯逻辑（无 JSX，可被 knowhow-panel.test.mjs 直接 import）。
// knowhow-panel.tsx 含 JSX，Node 原生 TS 类型剥离不支持 .tsx（仅 .ts/.mts/.cts
// 可被 node --test 直接 import），故本文件把行过滤 / 列序 / 投影状态徽标文案
// 与可重试判定 / 抽屉标题解析 / 图片鉴权判定 这些可测纯逻辑单独抽出。
// knowhow-panel.tsx 只调用本文件导出的函数，不重复实现判断逻辑。

import type { KnowhowColumn, KnowhowRow, ProjectionStatus } from "./knowhow-model.ts";

// --- 行过滤（顶部过滤框：按概念/全文包含过滤）---------------------------------

// 大小写不敏感、查询串去首尾空白；空查询返回全部行（保持原序）。匹配范围=
// 行内全部单元格文本 —— 概念列本身就是其中一个单元格，"按概念"过滤天然被
// "全文"匹配覆盖，无需为概念列单独写第二套匹配路径。
export function filterRows(rows: KnowhowRow[], query: string): KnowhowRow[] {
  const needle = query.trim().toLowerCase();
  if (!needle) return rows;
  return rows.filter((row) =>
    Object.values(row.cells).some((cell) => (cell ?? "").toLowerCase().includes(needle)),
  );
}

// --- 列序 --------------------------------------------------------------------

// 抽屉分节顺序：纯按 position 升序，不做概念置顶（抽屉标题本身已展示概念值，
// 分节里概念列按其真实 position 出现即可）。
export function sortColumnsByPosition(columns: KnowhowColumn[]): KnowhowColumn[] {
  return [...columns].sort((a, b) => a.position - b.position);
}

// 网格列头顺序：概念列钉首列（组件侧配合 sticky 定位，横向滚动时概念列保持
// 可见），其余列按 position 升序跟随。无概念角色列时退化为纯 position 排序。
export function orderColumnsForGrid(columns: KnowhowColumn[]): KnowhowColumn[] {
  const sorted = sortColumnsByPosition(columns);
  const conceptIndex = sorted.findIndex((column) => column.role === "concept");
  if (conceptIndex <= 0) return sorted;
  const concept = sorted[conceptIndex];
  return [concept, ...sorted.slice(0, conceptIndex), ...sorted.slice(conceptIndex + 1)];
}

// --- 行投影状态徽标 -------------------------------------------------------------

export const PROJECTION_STATUS_LABELS: Record<ProjectionStatus, string> = {
  pending: "待同步",
  syncing: "同步中",
  synced: "已同步",
  failed: "同步失败·可重试",
};

export type ProjectionStatusTone = "neutral" | "info" | "success" | "danger";

export const PROJECTION_STATUS_TONE: Record<ProjectionStatus, ProjectionStatusTone> = {
  pending: "neutral",
  syncing: "info",
  synced: "success",
  failed: "danger",
};

// 唯一可重试的状态是 failed；组件侧据此决定是否在徽标里露出重试按钮。
export function isRetryableProjectionStatus(status: ProjectionStatus): boolean {
  return status === "failed";
}

// --- 行详情抽屉标题 -------------------------------------------------------------

// 抽屉标题取「概念」列的原始格子文本（未截断，组件侧再套 cellSummary 截断显
// 示）；行内没有概念角色列时退化为 position 最小的列（通常即首列），一列都
// 没有时返回空串交给组件侧兜底文案。
export function resolveRowTitleText(row: KnowhowRow, columns: KnowhowColumn[]): string {
  const ordered = sortColumnsByPosition(columns);
  const conceptColumn = ordered.find((column) => column.role === "concept") ?? ordered[0];
  if (!conceptColumn) return "";
  return row.cells[conceptColumn.id] ?? "";
}

// --- 图片鉴权判定 ---------------------------------------------------------------

// 鉴权 token 只存在 localStorage（见 auth.ts），从不随 <img src> 请求自动带上。
// 因此渲染 rewriteAssetUrls() 产出的本站资产图片时必须走带 Authorization 头
// 的 fetch+blob，而非普通 <img src>；但只对"以 apiBase 开头的本站资产 URL"
// 这样做——任何其它来源(外链图片等)一律用普通 <img src>，避免把鉴权头发给
// 第三方主机造成令牌外泄。
export function isInternalAssetUrl(url: string, apiBase: string): boolean {
  if (!url || !apiBase) return false;
  return url.startsWith(`${apiBase}/notebooks/`) && url.includes("/assets/");
}
