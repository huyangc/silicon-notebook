// Knowhow 表 — 前端模型层：类型 + 资产 URL 改写/格子摘要（纯逻辑，单测见
// knowhow-model.test.mjs）+ fetch 封装（镜像 notebook-share.ts 的封装风格）。
// 后端 JSON 为 snake_case（projection_status/row_count/guessed_role/...），
// 本文件对外一律暴露 camelCase，字段改名集中在下方 mapXxx() 里。

import { authHeaders } from "./auth.ts";

// --- 角色与文案 ---------------------------------------------------------------

export type Role = "concept" | "identify" | "root_cause" | "fix" | "tool" | "plain";

// 角色徽章/下拉的中文文案，顺序与规格一致：概念/现象识别/根因分析/修复方法/依赖工具/普通。
export const ROLE_LABELS: Record<Role, string> = {
  concept: "概念",
  identify: "现象识别",
  root_cause: "根因分析",
  fix: "修复方法",
  tool: "依赖工具",
  plain: "普通",
};

// 行投影状态：pending=同步中，synced=已同步，failed=失败可重试。
export type ProjectionStatus = "pending" | "synced" | "failed";

// --- 领域类型（camelCase，供组件消费）------------------------------------------

export type KnowhowColumn = {
  id: string;
  name: string;
  role: Role;
  position: number;
};

export type KnowhowRow = {
  id: string;
  position: number;
  projectionStatus: ProjectionStatus;
  cells: Record<string, string>; // column_id -> content_md(原始 markdown，含 asset:// 引用)
};

export type KnowhowTableSummary = {
  id: string;
  title: string;
  description: string;
  rowCount: number;
};

export type KnowhowTableDetail = {
  id: string;
  title: string;
  description: string;
  columns: KnowhowColumn[];
  rows: KnowhowRow[];
};

// 建表/导入时提交的列定义（列名 + 角色，与文件列序对齐）。
export type KnowhowColumnInput = { name: string; role: Role };

// 导入预览：每列的猜测角色 + 前若干行预览 + 总行数。
export type KnowhowPreviewColumn = { name: string; guessedRole: Role };

export type KnowhowImportPreview = {
  columns: KnowhowPreviewColumn[];
  rowsPreview: string[][];
  totalRows: number;
};

// --- 后端线上形状（snake_case，仅本文件内部使用）--------------------------------

type WireKnowhowRow = {
  id: string;
  position: number;
  projection_status: ProjectionStatus;
  cells: Record<string, string>;
};

type WireKnowhowTableSummary = {
  id: string;
  title: string;
  description: string | null;
  row_count: number;
};

type WireKnowhowTableDetail = {
  id: string;
  title: string;
  description: string | null;
  columns: KnowhowColumn[];
  rows: WireKnowhowRow[];
};

type WirePreviewColumn = { name: string; guessed_role: Role };

type WireImportPreview = {
  columns: WirePreviewColumn[];
  rows_preview: string[][];
  total_rows: number;
};

function mapRow(row: WireKnowhowRow): KnowhowRow {
  return {
    id: row.id,
    position: row.position,
    projectionStatus: row.projection_status,
    cells: row.cells ?? {},
  };
}

function mapSummary(table: WireKnowhowTableSummary): KnowhowTableSummary {
  return {
    id: table.id,
    title: table.title,
    description: table.description ?? "",
    rowCount: table.row_count,
  };
}

function mapDetail(table: WireKnowhowTableDetail): KnowhowTableDetail {
  return {
    id: table.id,
    title: table.title,
    description: table.description ?? "",
    columns: table.columns ?? [],
    rows: (table.rows ?? []).map(mapRow),
  };
}

function mapPreview(preview: WireImportPreview): KnowhowImportPreview {
  return {
    columns: (preview.columns ?? []).map((column) => ({ name: column.name, guessedRole: column.guessed_role })),
    rowsPreview: preview.rows_preview ?? [],
    totalRows: preview.total_rows,
  };
}

// --- fetch 封装 ----------------------------------------------------------------

const API_BASE =
  (typeof process !== "undefined"
    ? process.env?.NEXT_PUBLIC_API_BASE_URL
    : undefined) ?? "http://127.0.0.1:8000/api";

async function apiFetch<T>(url: string, init?: RequestInit): Promise<T> {
  // multipart(FormData) 请求不设 Content-Type，交给浏览器自带 boundary。
  const isForm = init?.body instanceof FormData;
  const res = await fetch(API_BASE + url, {
    headers: isForm ? { ...authHeaders() } : { "Content-Type": "application/json", ...authHeaders() },
    ...init,
  });
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  // 204 No Content(删除/重投影触发)没有 body。
  if (res.status === 204) return null as T;
  return res.json() as Promise<T>;
}

// 表清单（总览网格用）。
export const fetchKnowhowTables = (notebookId: string): Promise<KnowhowTableSummary[]> =>
  apiFetch<WireKnowhowTableSummary[]>(`/notebooks/${notebookId}/knowhow`).then((tables) => tables.map(mapSummary));

// 单表详情（列+行+格+行投影状态)。
export const fetchKnowhowTable = (notebookId: string, tableId: string): Promise<KnowhowTableDetail> =>
  apiFetch<WireKnowhowTableDetail>(`/notebooks/${notebookId}/knowhow/${tableId}`).then(mapDetail);

// 导入预览：上传文件、拿列名+猜测角色+前 5 行预览+总行数，不建表。
export const importKnowhowPreview = (notebookId: string, file: File | Blob): Promise<KnowhowImportPreview> => {
  const form = new FormData();
  form.append("file", file);
  return apiFetch<WireImportPreview>(`/notebooks/${notebookId}/knowhow/import/preview`, {
    method: "POST",
    body: form,
  }).then(mapPreview);
};

// 确认导入：文件 + 标题 + 用户确认后的列角色映射，建表+全量入库+后台投影。
export const importKnowhow = (
  notebookId: string,
  file: File | Blob,
  title: string,
  columns: KnowhowColumnInput[],
): Promise<KnowhowTableDetail> => {
  const form = new FormData();
  form.append("file", file);
  form.append("title", title);
  form.append("columns_json", JSON.stringify(columns));
  return apiFetch<WireKnowhowTableDetail>(`/notebooks/${notebookId}/knowhow/import`, {
    method: "POST",
    body: form,
  }).then(mapDetail);
};

// 删表(级联行/格/投影产物/隐藏源)。
export const deleteKnowhowTable = (notebookId: string, tableId: string): Promise<void> =>
  apiFetch<void>(`/notebooks/${notebookId}/knowhow/${tableId}`, { method: "DELETE" });

// 全量重投影逃生口(后台执行，不等待完成)。
export const reprojectKnowhowTable = (notebookId: string, tableId: string): Promise<void> =>
  apiFetch<void>(`/notebooks/${notebookId}/knowhow/${tableId}/reproject`, { method: "POST" });

// --- 纯 helper(单测) ------------------------------------------------------------

// 仅匹配「图片链接」且目标协议为 asset:// 的情形；非图片文本、非 asset 协议的
// 图片/链接一律不动(仅替换图片链接目标，不做全文 asset:// 字符串替换)。
const IMAGE_ASSET_URL_RE = /!\[([^\]]*)\]\(asset:\/\/([^)\s]+)\)/g;

// 格子 markdown 中 `![alt](asset://<id>)` → 带鉴权的资产 API URL，供渲染态直接
// 当 <img src> 使用。写死协议为 asset:// 才改写，其余 URL(http/相对路径等)原样保留。
export function rewriteAssetUrls(md: string, notebookId: string, apiBase: string): string {
  const src = md ?? "";
  return src.replace(IMAGE_ASSET_URL_RE, (_match, alt: string, assetId: string) => {
    return `![${alt}](${apiBase}/notebooks/${notebookId}/assets/${assetId})`;
  });
}

// 任意协议的图片语法 → 图示占位文案(与后端 textops.strip_images 的
// `(图示：alt)` 约定一致，alt 常含线索，为空则不带冒号)。
const IMAGE_ANY_RE = /!\[([^\]]*)\]\([^)]*\)/g;
const LINK_RE = /\[([^\]]*)\]\([^)]*\)/g;
const CODE_FENCE_RE = /```[a-zA-Z0-9_-]*\n?/g;
const INLINE_CODE_RE = /`([^`]+)`/g;
const BOLD_STAR_RE = /\*\*([^*]+)\*\*/g;
const BOLD_UNDERSCORE_RE = /__([^_]+)__/g;
const ITALIC_STAR_RE = /\*([^*]+)\*/g;
const ITALIC_UNDERSCORE_RE = /_([^_]+)_/g;
const STRIKE_RE = /~~([^~]+)~~/g;
const HEADER_RE = /^#{1,6}\s+/gm;
const BLOCKQUOTE_RE = /^>\s?/gm;
const HR_RE = /^\s*(-{3,}|\*{3,}|_{3,})\s*$/gm;
const UL_MARKER_RE = /^\s*[-*+]\s+/gm;
const OL_MARKER_RE = /^\s*\d+\.\s+/gm;

function imagePlaceholder(_match: string, alt: string): string {
  const label = (alt ?? "").trim();
  return label ? `（图示：${label}）` : "（图示）";
}

// 依次剥图占位 → 去除常见 md 记号(代码块/行内代码/链接/加粗/斜体/删除线/标题/
// 引用/分隔线/列表符号)。注意顺序：图片先于普通链接(避免残留前缀 `!`)；
// 加粗先于斜体(避免 `**x**` 被单星号规则误吃掉一半)。
function stripMarkdownMarks(text: string): string {
  return text
    .replace(IMAGE_ANY_RE, imagePlaceholder)
    .replace(CODE_FENCE_RE, "")
    .replace(INLINE_CODE_RE, "$1")
    .replace(LINK_RE, "$1")
    .replace(BOLD_STAR_RE, "$1")
    .replace(BOLD_UNDERSCORE_RE, "$1")
    .replace(ITALIC_STAR_RE, "$1")
    .replace(ITALIC_UNDERSCORE_RE, "$1")
    .replace(STRIKE_RE, "$1")
    .replace(HEADER_RE, "")
    .replace(BLOCKQUOTE_RE, "")
    .replace(HR_RE, "")
    .replace(UL_MARKER_RE, "")
    .replace(OL_MARKER_RE, "");
}

// 总览网格单元格摘要：剥图占位、去 md 记号、空白折叠为单行，超过 maxLen 截断
// 并加省略号("…" 本身计入 maxLen，故结果长度恒 <= maxLen)。空/纯空白格子
// 返回空串。
export function cellSummary(md: string, maxLen: number = 80): string {
  const src = md ?? "";
  const stripped = stripMarkdownMarks(src);
  const flat = stripped.replace(/\s+/g, " ").trim();
  if (flat.length <= maxLen) return flat;
  return flat.slice(0, Math.max(0, maxLen - 1)).trimEnd() + "…";
}
