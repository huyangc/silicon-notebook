// Knowhow 表 — 前端模型层：类型 + 资产 URL 改写/格子摘要/行标题合成（纯逻辑，
// 单测见 knowhow-model.test.mjs）+ fetch 封装（镜像 notebook-share.ts 的封装
// 风格）。覆盖 PR-1（导入+只读总览）与 PR-2+3（编辑维护/模板往返/LLM 表达
// 优化/代码附件/引用跳转）两代 wire 契约。
// 后端 JSON 为 snake_case（projection_status/row_count/guessed_kind/
// anchor_column_id/suggestion_md/...），本文件对外一律暴露 camelCase，字段
// 改名集中在下方 mapXxx() 里。
//
// PR-2+3 落地时后端按波次分批交付（本文件先于 Task 3/6/8/10/12 的具体端点
// 落地而编写）：mapDetail()/mapPreview()/mapColumn() 对「字段尚未改名」与
// 「字段已按新契约改名」两种线上形状都做兼容读取（优先新字段、回退旧字段/
// 派生），使本文件在后端逐任务合入的过程中始终可用，不需要等全部任务落地
// 才能联调。

import { authHeaders } from "./auth.ts";

// --- 内容类型（kind）与文案 -----------------------------------------------------

// 格子级节点模型（规格 2026-07-15 修订）：列的「角色」改名为域中立的内容类型
// kind，四值：anchor(行标题，表级单选，不进列 kind 下拉) / procedure(方法
// 步骤) / entity(工具/事物) / attribute(普通)。
export type CellKind = "anchor" | "procedure" | "entity" | "attribute";

// 列 kind 下拉只提供三种「内容类型」——anchor(行标题) 不是列 kind 下拉的可选
// 项，走表级 anchorColumnId（见 KnowhowTableDetail.anchorColumnId）单独设置；
// 只有被表级选中为「行标题列」的那一列，其 kind 才会是 anchor（仅经后端
// set_knowhow_anchor_column 写入，规格①）。
export type ColumnKind = Exclude<CellKind, "anchor">;

// 三种内容类型下拉文案，顺序与规格①一致：方法步骤/工具/事物/普通，各自在
// UI 上还带一行提示语(由消费方渲染，本文件只提供徽章/选项文案本身)。
export const KIND_LABELS: Record<ColumnKind, string> = {
  procedure: "方法步骤",
  entity: "工具/事物",
  attribute: "普通",
};

// deprecated: PR-1 时期的六值 Role 词表（concept/identify/root_cause/fix/
// tool/plain）已被上面的四值 CellKind 词表替换（规格 2026-07-15 修订——五
// 角色只是时序修复域的实例，被用户纠正为域中立行为类型）。此处保留
// Role/ROLE_LABELS 别名，只是为了不让仍按「每列一个角色徽章/下拉」心智工作
// 的旧消费方（knowhow-panel.tsx 的 RoleBadge、knowhow-import.tsx/
// knowhow-import-logic.ts 的角色下拉）编译/运行失败；Task 5 迁移这些消费方
// 到 CellKind/KIND_LABELS + 表级 anchorColumnId 之后，本别名可删除。
export type Role = CellKind;

// 含 anchor 的四值文案（旧徽章渲染路径用；anchor 文案="行标题"，使旧代码在
// 列 kind 恰为 anchor 时也能渲染出合理文案，而不是 undefined）。
export const ROLE_LABELS: Record<CellKind, string> = {
  anchor: "行标题",
  ...KIND_LABELS,
};

// 行投影状态：pending=待投影，syncing=同步中(投影器正在(重)投影)，
// synced=已同步，failed=失败可重试。
export type ProjectionStatus = "pending" | "syncing" | "synced" | "failed";

// 格子代码附件新鲜度三态：implemented=附件 hash 与格子当前 hash 一致，
// stale=格子内容已变而附件未跟进重审，none=该格尚无代码附件。
export type CellCodeStatus = "implemented" | "stale" | "none";

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
  // 行标题列 id；null=未设置(记录型表，只做检索投影，不建图谱节点)。
  anchorColumnId: string | null;
};

// 建表/导入时提交的列定义（列名 + 角色，与文件列序对齐）。旧导入向导(Task 9
// 之前)per-column 下拉仍可选「行标题」，故此处保持四值 Role 而非收窄到
// ColumnKind——Task 5 引入独立行标题列选择器后，可将该下拉收窄为三项。
export type KnowhowColumnInput = { name: string; role: Role };

// 导入预览：每列的猜测内容类型 + 前若干行预览 + 总行数 + 行标题列建议
// index(anchorSuggestion；后端 Task 1 guess_kinds 的第二个返回值，仅在列名
// 命中"名称/概念/类型/..."等提示词才给，无首列兜底；null=不建议)。
export type KnowhowPreviewColumn = { name: string; guessedRole: ColumnKind };

export type KnowhowImportPreview = {
  columns: KnowhowPreviewColumn[];
  rowsPreview: string[][];
  totalRows: number;
  anchorSuggestion: number | null;
};

// --- 编辑 API 输入类型（Task 3）--------------------------------------------------

export type KnowhowTablePatch = { title?: string; description?: string; anchorColumnId?: string | null };
export type KnowhowNewColumnInput = { name: string; kind: ColumnKind; position?: number };
export type KnowhowColumnEdit = { name?: string; kind?: ColumnKind };
export type KnowhowNewRowInput = { cells: Record<string, string>; position?: number };

export type KnowhowCellPatchResult = {
  rowId: string;
  columnId: string;
  contentMd: string;
  projectionStatus: ProjectionStatus;
};

// --- 模板往返 / 追加导入类型（Task 6）--------------------------------------------

export type KnowhowAppendDuplicateTitle = { rowIndex: number; title: string };

export type KnowhowAppendPreview = {
  rowsPreview: string[][];
  totalRows: number;
  unmatchedColumns: string[];
  duplicateTitles: KnowhowAppendDuplicateTitle[];
};

// --- 代码附件类型（Task 10）------------------------------------------------------

export type KnowhowCellCode = {
  codeText: string;
  language: string;
  status: CellCodeStatus;
  updatedAt: string | null;
};

// --- 引用跳转（Task 12）----------------------------------------------------------

// ask 引用命中 knowhow 格子来源 chunk 时后端附带的富化字段；非 knowhow 引用
// 该字段不存在(undefined)。
export type CitationKnowhowRef = { tableId: string; rowId: string };

// --- 后端线上形状（snake_case，仅本文件内部使用）--------------------------------

// 列的线上形状：Task 3 起字段改名为 kind，但迁移期间(该任务尚未落地前)仍可能
// 收到旧字段名 role——两者都接受，优先 kind。
type WireKnowhowColumn = {
  id: string;
  name: string;
  position: number;
  kind?: CellKind;
  role?: CellKind;
};

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
  columns: WireKnowhowColumn[];
  rows: WireKnowhowRow[];
  // Task 3 起附带；未落地前该字段不存在(undefined)——mapDetail 据此回退到从
  // columns[].role/kind==='anchor' 派生。
  anchor_column_id?: string | null;
};

// 预览列的线上形状：Task 3 起字段改名为 guessed_kind，迁移期间仍可能是
// guessed_role——两者都接受，优先 guessed_kind。
type WirePreviewColumn = { name: string; guessed_kind?: ColumnKind; guessed_role?: ColumnKind };

type WireImportPreview = {
  columns: WirePreviewColumn[];
  rows_preview: string[][];
  total_rows: number;
  // Task 3 起附带；未落地前不存在(undefined)。
  anchor_suggestion?: number | null;
};

type WireKnowhowCellPatchResult = {
  row_id: string;
  column_id: string;
  content_md: string;
  projection_status: ProjectionStatus;
};

type WireKnowhowAppendPreview = {
  rows_preview: string[][];
  total_rows: number;
  unmatched_columns: string[];
  duplicate_titles: { row_index: number; title: string }[];
};

type WireKnowhowCellCode = {
  code_text: string | null;
  language: string | null;
  status: CellCodeStatus;
  updated_at: string | null;
};

function mapColumn(column: WireKnowhowColumn): KnowhowColumn {
  return {
    id: column.id,
    name: column.name,
    position: column.position,
    role: (column.kind ?? column.role) as Role,
  };
}

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

// 行标题列 id：优先取显式 anchor_column_id 字段(含显式 null——Task 3 落地后
// 的真实形状，"确实没有行标题列"与"字段不存在"是两回事)；该字段不存在
// (undefined，Task 3 落地前的线上形状)时，从 columns[].role==='anchor' 派生。
function deriveAnchorColumnId(columns: KnowhowColumn[]): string | null {
  const anchor = columns.find((column) => column.role === "anchor");
  return anchor ? anchor.id : null;
}

function mapDetail(table: WireKnowhowTableDetail): KnowhowTableDetail {
  const columns = (table.columns ?? []).map(mapColumn);
  return {
    id: table.id,
    title: table.title,
    description: table.description ?? "",
    columns,
    rows: (table.rows ?? []).map(mapRow),
    anchorColumnId: table.anchor_column_id !== undefined ? table.anchor_column_id : deriveAnchorColumnId(columns),
  };
}

function mapPreview(preview: WireImportPreview): KnowhowImportPreview {
  return {
    columns: (preview.columns ?? []).map((column) => ({
      name: column.name,
      guessedRole: (column.guessed_kind ?? column.guessed_role) as ColumnKind,
    })),
    rowsPreview: preview.rows_preview ?? [],
    totalRows: preview.total_rows,
    anchorSuggestion: preview.anchor_suggestion ?? null,
  };
}

function mapAppendPreview(preview: WireKnowhowAppendPreview): KnowhowAppendPreview {
  return {
    rowsPreview: preview.rows_preview ?? [],
    totalRows: preview.total_rows,
    unmatchedColumns: preview.unmatched_columns ?? [],
    duplicateTitles: (preview.duplicate_titles ?? []).map((item) => ({
      rowIndex: item.row_index,
      title: item.title,
    })),
  };
}

function mapCellCode(code: WireKnowhowCellCode): KnowhowCellCode {
  return {
    codeText: code.code_text ?? "",
    language: code.language ?? "",
    status: code.status,
    updatedAt: code.updated_at ?? null,
  };
}

// ask 引用富化字段的 mapper：非 knowhow 引用该字段在线上形状中不存在
// (null/undefined)，两者都归一为 null，供 answer-formatting.ts(Task 12)
// 直接复用而不必重复这段 snake->camel 转换。
export function mapCitationKnowhowRef(
  wire: { table_id: string; row_id: string } | null | undefined,
): CitationKnowhowRef | null {
  if (!wire) return null;
  return { tableId: wire.table_id, rowId: wire.row_id };
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

// 单表详情（列+行+格+行投影状态+行标题列)。
export const fetchKnowhowTable = (notebookId: string, tableId: string): Promise<KnowhowTableDetail> =>
  apiFetch<WireKnowhowTableDetail>(`/notebooks/${notebookId}/knowhow/${tableId}`).then(mapDetail);

// 导入预览：上传文件、拿列名+猜测内容类型+行标题列建议+前 5 行预览+总行数，
// 不建表。
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

// 建空表（建表向导「定表头」提交）：标题 + 列定义(名+内容类型三值) + 行标题列
// 下标(null=不设置)。`POST /notebooks/{nb}/knowhow`，JSON body
// {title, columns:[{name,kind}], anchor_index}——镜像导入 commit 的 wire
// （columns_json + anchor_index）去掉文件段。
export type KnowhowCreateTableInput = {
  title: string;
  columns: { name: string; kind: ColumnKind }[];
  anchorIndex: number | null;
};

export const createKnowhowTable = (
  notebookId: string,
  input: KnowhowCreateTableInput,
): Promise<KnowhowTableDetail> =>
  apiFetch<WireKnowhowTableDetail>(`/notebooks/${notebookId}/knowhow`, {
    method: "POST",
    body: JSON.stringify({
      title: input.title,
      columns: input.columns,
      anchor_index: input.anchorIndex,
    }),
  }).then(mapDetail);

// 删表(级联行/格/投影产物/隐藏源)。
export const deleteKnowhowTable = (notebookId: string, tableId: string): Promise<void> =>
  apiFetch<void>(`/notebooks/${notebookId}/knowhow/${tableId}`, { method: "DELETE" });

// 全量重投影逃生口(后台执行，不等待完成)。
export const reprojectKnowhowTable = (notebookId: string, tableId: string): Promise<void> =>
  apiFetch<void>(`/notebooks/${notebookId}/knowhow/${tableId}/reproject`, { method: "POST" });

// --- 编辑 API fetchers（Task 3：表/列/行/格 CRUD + 调度器统一在后端触发）---------

// 表元信息 patch：title/description/anchorColumnId 均可选，未提供的键
// JSON.stringify 时自动丢弃(不触碰对应字段)；anchorColumnId 显式传 null 会
// 保留在请求体里，对应后端"显式 null 清除行标题列"语义——区分"不改"与
// "改成空"。返回更新后的表详情（同 fetchKnowhowTable 的形状）。
export const patchKnowhowTable = (
  notebookId: string,
  tableId: string,
  patch: KnowhowTablePatch,
): Promise<KnowhowTableDetail> =>
  apiFetch<WireKnowhowTableDetail>(`/notebooks/${notebookId}/knowhow/${tableId}`, {
    method: "PATCH",
    body: JSON.stringify({
      title: patch.title,
      description: patch.description,
      anchor_column_id: patch.anchorColumnId,
    }),
  }).then(mapDetail);

export const addKnowhowColumn = (
  notebookId: string,
  tableId: string,
  input: KnowhowNewColumnInput,
): Promise<KnowhowColumn> =>
  apiFetch<WireKnowhowColumn>(`/notebooks/${notebookId}/knowhow/${tableId}/columns`, {
    method: "POST",
    body: JSON.stringify({ name: input.name, kind: input.kind, position: input.position }),
  }).then(mapColumn);

export const patchKnowhowColumn = (
  notebookId: string,
  tableId: string,
  columnId: string,
  patch: KnowhowColumnEdit,
): Promise<KnowhowColumn> =>
  apiFetch<WireKnowhowColumn>(`/notebooks/${notebookId}/knowhow/${tableId}/columns/${columnId}`, {
    method: "PATCH",
    body: JSON.stringify({ name: patch.name, kind: patch.kind }),
  }).then(mapColumn);

export const deleteKnowhowColumn = (notebookId: string, tableId: string, columnId: string): Promise<void> =>
  apiFetch<void>(`/notebooks/${notebookId}/knowhow/${tableId}/columns/${columnId}`, { method: "DELETE" });

export const addKnowhowRow = (
  notebookId: string,
  tableId: string,
  input: KnowhowNewRowInput,
): Promise<KnowhowRow> =>
  apiFetch<WireKnowhowRow>(`/notebooks/${notebookId}/knowhow/${tableId}/rows`, {
    method: "POST",
    body: JSON.stringify({ cells: input.cells, position: input.position }),
  }).then(mapRow);

export const deleteKnowhowRow = (notebookId: string, tableId: string, rowId: string): Promise<void> =>
  apiFetch<void>(`/notebooks/${notebookId}/knowhow/${tableId}/rows/${rowId}`, { method: "DELETE" });

// 格子内容 patch：返回 {rowId,columnId,contentMd,projectionStatus}——调用方
// 用 projectionStatus 立即刷新该行的同步状态徽标，不必等待下一次整表拉取。
export const patchKnowhowCell = (
  notebookId: string,
  tableId: string,
  rowId: string,
  columnId: string,
  contentMd: string,
): Promise<KnowhowCellPatchResult> =>
  apiFetch<WireKnowhowCellPatchResult>(
    `/notebooks/${notebookId}/knowhow/${tableId}/rows/${rowId}/cells/${columnId}`,
    { method: "PATCH", body: JSON.stringify({ content_md: contentMd }) },
  ).then((wire) => ({
    rowId: wire.row_id,
    columnId: wire.column_id,
    contentMd: wire.content_md,
    projectionStatus: wire.projection_status,
  }));

// --- Excel 模板往返（Task 6）------------------------------------------------------

// 模板下载 URL(不是 fetcher)：调用方(Task 9)自行用带鉴权头的 fetch+blob 触发
// 下载(镜像 knowhow-panel.tsx 里 KnowhowImage 的认证 fetch 习语)，本函数只
// 负责拼出正确的 URL，不在 URL 里带 token。
export const knowhowTemplateUrl = (notebookId: string, tableId: string): string =>
  `${API_BASE}/notebooks/${notebookId}/knowhow/${tableId}/template`;

function appendKnowhowRequest(
  notebookId: string,
  tableId: string,
  file: File | Blob,
  mode: "preview" | "commit",
): Promise<unknown> {
  const form = new FormData();
  form.append("file", file);
  form.append("mode", mode);
  return apiFetch<unknown>(`/notebooks/${notebookId}/knowhow/${tableId}/append`, {
    method: "POST",
    body: form,
  });
}

// 追加导入预览：按表头列名匹配上传文件，不落库。
export const appendKnowhowPreview = (
  notebookId: string,
  tableId: string,
  file: File | Blob,
): Promise<KnowhowAppendPreview> =>
  appendKnowhowRequest(notebookId, tableId, file, "preview").then((wire) =>
    mapAppendPreview(wire as WireKnowhowAppendPreview),
  );

// 追加导入确认：真正落库+调度整表重投影。
export const appendKnowhowCommit = (
  notebookId: string,
  tableId: string,
  file: File | Blob,
): Promise<{ added: number }> =>
  appendKnowhowRequest(notebookId, tableId, file, "commit").then((wire) => wire as { added: number });

// --- LLM 表达优化（Task 8，显式触发，不写库——回填走 patchKnowhowCell）----------

export const optimizeKnowhowCell = (
  notebookId: string,
  tableId: string,
  rowId: string,
  columnId: string,
): Promise<{ suggestionMd: string }> =>
  apiFetch<{ suggestion_md: string }>(
    `/notebooks/${notebookId}/knowhow/${tableId}/rows/${rowId}/cells/${columnId}/optimize`,
    { method: "POST" },
  ).then((wire) => ({ suggestionMd: wire.suggestion_md }));

// --- 格子级代码附件（Task 10；HTTP 端点 session/agent token 皆可访问，本文件
// 的调用方(Task 11 用户界面)走既有 session 鉴权，与其余 fetcher 共用
// apiFetch/authHeaders，不需要为 agent token 走另一套客户端逻辑）-------------

// 路径不含 notebookId/tableId 段——行/格子 id 本身已定位到具体表，与本文件
// 其余"表级"fetcher 的参数形状不同，是端点真实形状使然，不是遗漏。
export const getCellCode = (rowId: string, columnId: string): Promise<KnowhowCellCode> =>
  apiFetch<WireKnowhowCellCode>(`/agent/knowhow/rows/${rowId}/cells/${columnId}/code`).then(mapCellCode);

export const putCellCode = (
  rowId: string,
  columnId: string,
  codeText: string,
  language: string,
): Promise<KnowhowCellCode> =>
  apiFetch<WireKnowhowCellCode>(`/agent/knowhow/rows/${rowId}/cells/${columnId}/code`, {
    method: "PUT",
    body: JSON.stringify({ code_text: codeText, language }),
  }).then(mapCellCode);

export const deleteCellCode = (rowId: string, columnId: string): Promise<void> =>
  apiFetch<void>(`/agent/knowhow/rows/${rowId}/cells/${columnId}/code`, { method: "DELETE" });

// --- 纯 helper(单测) ------------------------------------------------------------

// 行标题自动合成(展示用，记录型/无行标题列的表用来在网格/抽屉里显示一个可读
// 的行标签；后端 textops.compose_row_title 同规则孪生实现——见设计文档④)：
// 按 cells 顺序(调用方保证按列 position 传入)取前 <= maxSegments 个「非空
// 首行」，每段截断到 <= 16 字(截断加省略号，省略号本身计入 16)，用 " · "
// 连接。首行为空白的格子视为空、跳过继续找下一个非空格子(不提前中止扫描)。
// 全部为空(或 cells 为空数组)时返回空串——由调用方兜底展示为「行 N」。
export function composeRowTitle(cells: string[], maxSegments: number = 3): string {
  const segments: string[] = [];
  for (const cell of cells ?? []) {
    if (segments.length >= maxSegments) break;
    const firstLine = (cell ?? "").split("\n")[0].trim();
    if (!firstLine) continue;
    segments.push(truncateSegment(firstLine, 16));
  }
  return segments.join(" · ");
}

function truncateSegment(text: string, maxLen: number): string {
  if (text.length <= maxLen) return text;
  return text.slice(0, Math.max(0, maxLen - 1)).trimEnd() + "…";
}

// 仅匹配「图片链接」且目标协议为 asset:// 的情形；非图片文本、非 asset 协议的
// 图片/链接一律不动(仅替换图片链接目标，不做全文 asset:// 字符串替换)。
// id 段收紧为 `[A-Za-z0-9_-]+`(资产 id 的真实字符集)：既避免把 `asset://../..`
// 之类含 `.`/`/` 的路径穿越写成受信 API URL(纵深防御)，也让不合法 id 原样保留。
const IMAGE_ASSET_URL_RE = /!\[([^\]]*)\]\(asset:\/\/([A-Za-z0-9_-]+)\)/g;

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
// 星号斜体：内容首尾不得是空白，避免把「2 * 3 * 4」这类乘号表达式误吃成斜体
// (星号紧邻空格时不算强调标记)。真正的 `*emphasis*` 内容首尾无空格，仍会剥离。
const ITALIC_STAR_RE = /\*(\S(?:[^*]*\S)?)\*/g;
// 注意：故意不做下划线斜体剥离(CommonMark 对词中下划线不视为强调)。EDA/硅仿真
// 场景中下划线标识符(place_opt_design、clk_out_en 等 innovus 命令/信号名)是
// 常态，比中文「_斜体_」写法常见得多，必须原样保留、不被当成 `_x_` 斜体吃掉。
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
