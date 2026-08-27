// Knowhow 表 — 前端模型层：类型 + 资产 URL 改写/格子摘要/行标题合成（纯逻辑，
// 单测见 knowhow-model.test.mjs）+ 共享 transport 请求。覆盖 PR-1（导入+只读总览）与 PR-2+3（编辑维护/模板往返/LLM 表达
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

import { API_BASE } from "./api-config.ts";
import { performApiRequest, requestJson, requestVoid } from "./api-client.ts";
import type { ReasoningTraceStep } from "./ask-stream.ts";
import { throwHumanizedHttpError } from "./errors.ts";
import { requestTaskStream } from "./request-task-stream.ts";

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
  projectionPending: number;
  projectionFailed: number;
  staleCodeCount: number;
  lastActivityAt: string;
};

export type KnowhowHealthFilter =
  | "all"
  | "projection_pending"
  | "projection_failed"
  | "stale_code";

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

export type KnowhowImportOrientation = "columns" | "rows";

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

// 行级智能补全只生成待审阅建议，不直接写库。confidence 是后端稳定协议；
// suggestionMd=null 表示证据不足，调用方必须按 abstainReason 展示且禁止接受。
export type KnowhowCompletionConfidence = "high" | "medium" | "low";
export type KnowhowCompletionRetrievalScope = "active_and_mounted";
export type KnowhowCompletionRetrievalStatus = "succeeded" | "no_evidence";

export type KnowhowCompletionEvidence = {
  key: string;
  kind: "knowledge" | "chunk" | "element";
  objectType: string;
  label: string;
  excerptMd: string;
  sourceTitle: string;
  locationLabel: string;
  tier: "base" | "personal";
};

export type KnowhowRowCompletionSuggestion = {
  columnId: string;
  suggestionMd: string | null;
  confidence: KnowhowCompletionConfidence;
  basedOnRowIds: string[];
  evidenceKeys: string[];
  basis: string;
  abstainReason: string;
};

export type KnowhowRowCompletion = {
  retrievalMode: "reasoning";
  retrievalScope: KnowhowCompletionRetrievalScope;
  retrievalStatus: KnowhowCompletionRetrievalStatus;
  reasoningTrace: ReasoningTraceStep[];
  evidence: KnowhowCompletionEvidence[];
  suggestions: KnowhowRowCompletionSuggestion[];
};

// followup A（anchor 分组 spec §6「整组批量写单事务，不半改」）：合并共享格批量
// 写的输入——一列 + 一组 rowId + 同一份新内容，对应后端 KnowhowCellsBatchPatch。
export type KnowhowCellsBatchPatchInput = {
  columnId: string;
  rowIds: string[];
  contentMd: string;
  // 并发防护（P1-b）：可选的每行基线，**按 rowIds 顺序平行**（expectedBefore[i]
  // 对应 rowIds[i]）。提供时后端把整组当作一次 all-or-nothing 事务内比对，任一
  // 行的基线陈旧就整组 409 拒写；省略时退回 last-write-wins（手动编辑器合并格保存）。
  expectedBefore?: string[];
  // 并发防护（P1 round-4）：可选的 anchor 基线守卫。anchorColumnId = 分组键列 id，
  // expectedAnchor **按 rowIds 顺序平行** = 每行 anchor 列的快照值。与 expectedBefore
  // 一起由批量扇出下发：后端在同一事务内额外重读每行 anchor 列，任一行 anchor 自快照
  // 以来被移出组就整组 409（离组行不再被冻结扇出误写；被编辑格没变、expectedBefore
  // 单独拦不住这类漂移）。两字段成对，缺一即不带该守卫（手动编辑器合并格省略）。
  anchorColumnId?: string;
  expectedAnchor?: string[];
  // knowhow 表版本管理 Task 14：来源标记（如批量规整走这条路径时传
  // "llm_reformat"）——省略时不进请求体，退回后端默认 "user"（既有手动编辑器
  // 合并格调用点字节不变）。与后端 KnowhowCellsBatchPatch.origin 同一契约，见
  // patchKnowhowCell 的 origin 参数注释。
  origin?: string;
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
  // 溯源（收尾修复）：这段代码最近一次是谁写入的（外部 Agent 名或用户名）。
  // 行级和单格 GET/PUT 都携带 updated_by；它是可读审计 label，不是 ownership id。
  // 查看态只在非空时展示来源，绝不从当前登录人伪造溯源。
  updatedBy: string | null;
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

type WireKnowhowRowCompletionSuggestion = {
  column_id: string;
  suggestion_md: string | null;
  confidence: KnowhowCompletionConfidence;
  based_on_row_ids: string[];
  evidence_keys: string[];
  basis: string;
  abstain_reason: string;
};

type WireKnowhowCompletionEvidence = {
  key: string;
  kind: "knowledge" | "chunk" | "element";
  object_type: string;
  label: string;
  excerpt_md: string;
  source_title: string;
  location_label: string;
  tier: "base" | "personal";
};

type WireKnowhowRowCompletion = {
  retrieval_mode: "reasoning";
  retrieval_scope: KnowhowCompletionRetrievalScope;
  retrieval_status: KnowhowCompletionRetrievalStatus;
  reasoning_trace: ReasoningTraceStep[];
  evidence: WireKnowhowCompletionEvidence[];
  suggestions: WireKnowhowRowCompletionSuggestion[];
};

type WireKnowhowTableSummary = {
  id: string;
  title: string;
  description: string | null;
  row_count?: number | null;
  projection_pending?: number | null;
  projection_failed?: number | null;
  stale_code_count?: number | null;
  last_activity_at?: string | null;
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
  updated_by?: string | null;
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
    rowCount: table.row_count ?? 0,
    projectionPending: table.projection_pending ?? 0,
    projectionFailed: table.projection_failed ?? 0,
    staleCodeCount: table.stale_code_count ?? 0,
    lastActivityAt: table.last_activity_at ?? "",
  };
}

export function filterKnowhowTables(
  tables: KnowhowTableSummary[],
  filter: KnowhowHealthFilter,
): KnowhowTableSummary[] {
  if (filter === "projection_pending") {
    return tables.filter((table) => table.projectionPending > 0);
  }
  if (filter === "projection_failed") {
    return tables.filter((table) => table.projectionFailed > 0);
  }
  if (filter === "stale_code") {
    return tables.filter((table) => table.staleCodeCount > 0);
  }
  return tables;
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
    updatedBy: code.updated_by?.trim() || null,
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

// 表清单（总览网格用）。
export const fetchKnowhowTables = (notebookId: string): Promise<KnowhowTableSummary[]> =>
  requestJson<WireKnowhowTableSummary[]>(`/notebooks/${notebookId}/knowhow`, { tag: "knowhow" }).then((tables) => tables.map(mapSummary));

// 单表详情（列+行+格+行投影状态+行标题列)。
export const fetchKnowhowTable = (notebookId: string, tableId: string): Promise<KnowhowTableDetail> =>
  requestJson<WireKnowhowTableDetail>(`/notebooks/${notebookId}/knowhow/${tableId}`, { tag: "knowhow" }).then(mapDetail);

// 导入预览：上传文件、拿列名+猜测内容类型+行标题列建议+前 5 行预览+总行数，
// 不建表。`orientation` 决定原始属性按列还是按行；`anchorIndex` 可选——向导在
// 步骤②改/清行标题列后重取预览时带上它，后端
// 据此按**所选**锚定列跳过规整（而非猜测列），保住「预览即所得」（见后端
// preview_import 的 anchor_index 参数）。
// 三态编码（评审残留修复）：省略参数只能表达三态之一，故——
//   undefined（初始加载，尚不知用户会选哪列，猜测列由响应带回）→ 不发，后端按猜测列跳过；
//   number（用户选了某列）→ 发该列下标（typeof === "number" 判断，anchorIndex=0 不被吞）；
//   null（明确清空「不设行标题」）→ 发 -1，后端解读为「无锚定列」→ 全列规整，
//   与 commit 不带 anchor_index 的落库行为对齐。null 若也不发，预览会按猜测列跳过
//   而提交却全列规整——预览即所得在猜测列上撒谎。
export const importKnowhowPreview = (
  notebookId: string,
  file: File | Blob,
  orientation: KnowhowImportOrientation = "columns",
  anchorIndex?: number | null,
): Promise<KnowhowImportPreview> => {
  const form = new FormData();
  form.append("file", file);
  form.append("orientation", orientation);
  if (typeof anchorIndex === "number") form.append("anchor_index", String(anchorIndex));
  else if (anchorIndex === null) form.append("anchor_index", "-1");
  return requestJson<WireImportPreview>(`/notebooks/${notebookId}/knowhow/import/preview`, {
    method: "POST",
    body: form,
    tag: "knowhow",
  }).then(mapPreview);
};

// 确认导入：文件 + 标题 + 用户确认后的列角色映射，建表+全量入库+后台投影。
// 导入 commit：文件 + 标题 + 列定义(名+内容类型三值) + 行标题列下标。
// wire 与建表 createKnowhowTable 同构：`columns_json=[{name,kind}]` + 独立的
// `anchor_index`。行标题列**只**能经 anchor_index 传达——后端
// `_columns_with_anchor` 只读 `column["kind"]`（缺失时静默默认 'attribute'
// 而不报错），且 VALID_KINDS 明确排除 'anchor'。把 anchor 编码进列定义、或
// 把字段名写成 role，都会被后端无声丢弃：曾导致用户在向导选的行标题列与每
// 列内容类型全部失效、整张表落库成无 anchor 的记录型表（forward-fill 随之
// 跳过，anchor 分组显示完全不生效）。契约由 knowhow-model.test.mjs 锁住。
export const importKnowhow = (
  notebookId: string,
  file: File | Blob,
  title: string,
  columns: { name: string; kind: ColumnKind }[],
  anchorIndex: number | null,
  orientation: KnowhowImportOrientation = "columns",
): Promise<KnowhowTableDetail> => {
  const form = new FormData();
  form.append("file", file);
  form.append("title", title);
  form.append("columns_json", JSON.stringify(columns));
  form.append("orientation", orientation);
  // null=不设行标题列（记录型表）：不发该字段，走后端 Form(None) 默认。
  if (anchorIndex !== null) form.append("anchor_index", String(anchorIndex));
  return requestJson<WireKnowhowTableDetail>(`/notebooks/${notebookId}/knowhow/import`, {
    method: "POST",
    body: form,
    tag: "knowhow",
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
  requestJson<WireKnowhowTableDetail>(`/notebooks/${notebookId}/knowhow`, {
    method: "POST",
    body: JSON.stringify({
      title: input.title,
      columns: input.columns,
      anchor_index: input.anchorIndex,
    }),
    tag: "knowhow",
  }).then(mapDetail);

// 删表(级联行/格/投影产物/隐藏源)。
export const deleteKnowhowTable = (notebookId: string, tableId: string): Promise<void> =>
  requestVoid(`/notebooks/${notebookId}/knowhow/${tableId}`, { method: "DELETE", tag: "knowhow" });

// 全量重投影逃生口(后台执行，不等待完成)。
export const reprojectKnowhowTable = (notebookId: string, tableId: string): Promise<void> =>
  requestVoid(`/notebooks/${notebookId}/knowhow/${tableId}/reproject`, { method: "POST", tag: "knowhow" });

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
  requestJson<WireKnowhowTableDetail>(`/notebooks/${notebookId}/knowhow/${tableId}`, {
    method: "PATCH",
    body: JSON.stringify({
      title: patch.title,
      description: patch.description,
      anchor_column_id: patch.anchorColumnId,
    }),
    tag: "knowhow",
  }).then(mapDetail);

export const addKnowhowColumn = (
  notebookId: string,
  tableId: string,
  input: KnowhowNewColumnInput,
): Promise<KnowhowColumn> =>
  requestJson<WireKnowhowColumn>(`/notebooks/${notebookId}/knowhow/${tableId}/columns`, {
    method: "POST",
    body: JSON.stringify({ name: input.name, kind: input.kind, position: input.position }),
    tag: "knowhow",
  }).then(mapColumn);

export const patchKnowhowColumn = (
  notebookId: string,
  tableId: string,
  columnId: string,
  patch: KnowhowColumnEdit,
): Promise<KnowhowColumn> =>
  requestJson<WireKnowhowColumn>(`/notebooks/${notebookId}/knowhow/${tableId}/columns/${columnId}`, {
    method: "PATCH",
    body: JSON.stringify({ name: patch.name, kind: patch.kind }),
    tag: "knowhow",
  }).then(mapColumn);

export const deleteKnowhowColumn = (notebookId: string, tableId: string, columnId: string): Promise<void> =>
  requestVoid(`/notebooks/${notebookId}/knowhow/${tableId}/columns/${columnId}`, { method: "DELETE", tag: "knowhow" });

export const addKnowhowRow = (
  notebookId: string,
  tableId: string,
  input: KnowhowNewRowInput,
): Promise<KnowhowRow> =>
  requestJson<WireKnowhowRow>(`/notebooks/${notebookId}/knowhow/${tableId}/rows`, {
    method: "POST",
    body: JSON.stringify({ cells: input.cells, position: input.position }),
    tag: "knowhow",
  }).then(mapRow);

export const deleteKnowhowRow = (notebookId: string, tableId: string, rowId: string): Promise<void> =>
  requestVoid(`/notebooks/${notebookId}/knowhow/${tableId}/rows/${rowId}`, { method: "DELETE", tag: "knowhow" });

function mapCellPatchResult(wire: WireKnowhowCellPatchResult): KnowhowCellPatchResult {
  return {
    rowId: wire.row_id,
    columnId: wire.column_id,
    contentMd: wire.content_md,
    projectionStatus: wire.projection_status,
  };
}

// 格子内容 patch：返回 {rowId,columnId,contentMd,projectionStatus}——调用方
// 用 projectionStatus 立即刷新该行的同步状态徽标，不必等待下一次整表拉取。
//
// expectedBefore（并发防护 P1-b）：提供时随请求体带 expected_before，后端在**同一
// 写事务内**比对当前落库内容，不一致就 409 拒写（nothing written）——批量规整保存
// 走这条路。省略时不带该键，退回既有 last-write-wins（手动编辑器的语义，刻意不变：
// 那是真人在编辑，不该被自己更早的快照挡下）。
// origin（knowhow 表版本管理 Task 14，第 7 个位置参数——与 expectedBefore 同一
// 层级的"追加可选位置参数"手法，不引入 options 对象、不动既有调用点）：谁/
// 什么产生了这次写入，原样落进后端这条 knowhow_changes 流水（见后端
// KnowhowCellPatch.origin 与设计文档 §4.1/§8.1）。省略时不进请求体，退回后端
// 默认 "user"——手动编辑器的既有调用点字节不变；"从历史恢复某格"等新调用点
// 显式传 "revert"，批量规整回填走 batchPatchKnowhowCells 的同名字段传
// "llm_reformat"。后端对该字段做白名单校验（VALID_ORIGINS），非法值 400，
// 前端不需要重复校验。
export const patchKnowhowCell = (
  notebookId: string,
  tableId: string,
  rowId: string,
  columnId: string,
  contentMd: string,
  expectedBefore?: string,
  origin?: string,
): Promise<KnowhowCellPatchResult> =>
  requestJson<WireKnowhowCellPatchResult>(
    `/notebooks/${notebookId}/knowhow/${tableId}/rows/${rowId}/cells/${columnId}`,
    {
      method: "PATCH",
      body: JSON.stringify({
        content_md: contentMd,
        ...(expectedBefore === undefined ? {} : { expected_before: expectedBefore }),
        ...(origin === undefined ? {} : { origin }),
      }),
      tag: "knowhow",
    },
  ).then(mapCellPatchResult);

// followup A（anchor 分组 spec §6「整组批量写单事务，不半改」）：合并共享格
// 批量写——一次 PATCH 把同一列在整组 rowIds 上的新内容交给后端在 ONE DB
// 事务里全写或全不写（repo.update_knowhow_cells），取代过去 knowhow-panel
// handleCellSave 对每个 rowId 各打一个 patchKnowhowCell、Promise.all 并发、
// 中途失败即半改的取舍。响应形状与 patchKnowhowCell 相同，只是按 rowIds
// 顺序回一个列表(逐项走同一个 mapCellPatchResult)。
export const batchPatchKnowhowCells = (
  notebookId: string,
  tableId: string,
  input: KnowhowCellsBatchPatchInput,
): Promise<KnowhowCellPatchResult[]> => {
  // 逐键条件装配请求体：省略的守卫字段**不进** body（区别于传 null）——既有测试锁死
  // 「省略 expectedBefore 时 body 不含该键」，anchor 守卫同理。
  const body: Record<string, unknown> = {
    column_id: input.columnId,
    row_ids: input.rowIds,
    content_md: input.contentMd,
  };
  if (input.expectedBefore !== undefined) body.expected_before = input.expectedBefore;
  // anchor 守卫两字段成对：只有都提供才带上（后端也要求 anchor_column_id 与
  // expected_anchor 同时在场才启用守卫）。
  if (input.anchorColumnId !== undefined && input.expectedAnchor !== undefined) {
    body.anchor_column_id = input.anchorColumnId;
    body.expected_anchor = input.expectedAnchor;
  }
  // knowhow 表版本管理 Task 14：同 patchKnowhowCell 的 origin 参数——省略时不
  // 进请求体，退回后端默认 "user"。
  if (input.origin !== undefined) body.origin = input.origin;
  return requestJson<WireKnowhowCellPatchResult[]>(`/notebooks/${notebookId}/knowhow/${tableId}/cells`, {
    method: "PATCH",
    body: JSON.stringify(body),
    tag: "knowhow",
  }).then((wireResults) => wireResults.map(mapCellPatchResult));
};

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
  return requestJson<unknown>(`/notebooks/${notebookId}/knowhow/${tableId}/append`, {
    method: "POST",
    body: form,
    tag: "knowhow",
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
  signal?: AbortSignal,
): Promise<{ suggestionMd: string }> =>
  requestTaskStream<{ suggestion_md: string }>(
    `/notebooks/${notebookId}/knowhow/${tableId}/rows/${rowId}/cells/${columnId}/optimize/stream`,
    { method: "POST", signal, tag: "knowhow" },
    { fallbackMessage: "优化失败，请重试" },
  ).then((wire) => ({ suggestionMd: wire.suggestion_md }));

// --- 单行空列智能补全（显式触发、只返回建议）----------------------------------

function mapKnowhowRowCompletionSuggestion(
  wire: WireKnowhowRowCompletionSuggestion,
): KnowhowRowCompletionSuggestion {
  return {
    columnId: wire.column_id,
    suggestionMd: wire.suggestion_md,
    confidence: wire.confidence,
    basedOnRowIds: wire.based_on_row_ids ?? [],
    evidenceKeys: wire.evidence_keys ?? [],
    basis: wire.basis ?? "",
    abstainReason: wire.abstain_reason ?? "",
  };
}

export const completeKnowhowRow = (
  notebookId: string,
  tableId: string,
  rowId: string,
  targetColumnIds?: string[],
  signal?: AbortSignal,
): Promise<KnowhowRowCompletion> =>
  requestTaskStream<WireKnowhowRowCompletion>(
    `/notebooks/${notebookId}/knowhow/${tableId}/rows/${rowId}/complete/stream`,
    {
      method: "POST",
      body: JSON.stringify(
        targetColumnIds === undefined ? {} : { target_column_ids: targetColumnIds },
      ),
      signal,
      tag: "knowhow",
    },
    { fallbackMessage: "生成建议失败，请稍后重试" },
  ).then((wire) => ({
    retrievalMode: wire.retrieval_mode,
    retrievalScope: wire.retrieval_scope,
    retrievalStatus: wire.retrieval_status,
    reasoningTrace: wire.reasoning_trace ?? [],
    evidence: (wire.evidence ?? []).map((item) => ({
      key: item.key,
      kind: item.kind,
      objectType: item.object_type,
      label: item.label ?? "",
      excerptMd: item.excerpt_md ?? "",
      sourceTitle: item.source_title,
      locationLabel: item.location_label,
      tier: item.tier,
    })),
    suggestions: (wire.suggestions ?? []).map(mapKnowhowRowCompletionSuggestion),
  }));

// --- 排版重排（knowhow-md-normalize Task 4，同样显式触发、不写库）-----------
// 与 optimizeKnowhowCell 的关键差异：后端 reformat_cell 对「未配置模型」等
// 情况已内部优雅兜底（source="rule/no-llm"，绝不抛错），所以本端点恒 200，
// 调用方据 source 决定候选文案的呈现方式（供 Task 5 编辑器按钮/Task 后续
// 批量动作消费）。
// source_md（并发防护 P1-a）：后端回带它实际读到并喂给 reformat_cell 的原文，
// 映射为 sourceMd。批量规整据此比对建批次那一刻的 originalMd 快照——不一致说明
// 候选是拿「客户端没见过的内容」算出来的（别的标签页/用户已改了这格），该格直接
// 跳过、且不进去重缓存（见 knowhow-optimize-logic.ts reformatResultIsStale）。
export const reformatKnowhowCell = (
  notebookId: string,
  tableId: string,
  rowId: string,
  columnId: string,
  signal?: AbortSignal,
) =>
  requestTaskStream<{ candidate_md: string; source: string; changed: boolean; source_md: string }>(
    `/notebooks/${notebookId}/knowhow/${tableId}/rows/${rowId}/cells/${columnId}/reformat/stream`,
    { method: "POST", signal, tag: "knowhow" },
    { fallbackMessage: "格式化失败，请重试" },
  ).then((w) => ({ candidateMd: w.candidate_md, source: w.source, changed: w.changed, sourceMd: w.source_md }));

// --- 格子级代码附件（Task 10；HTTP 端点 session/agent token 皆可访问，本文件
// 的调用方(Task 11 用户界面)走既有 session 鉴权，与其余请求共用共享 transport，
// 不需要为 agent token 走另一套客户端逻辑）----------------------------------

// 路径不含 notebookId/tableId 段——行/格子 id 本身已定位到具体表，与本文件
// 其余"表级"fetcher 的参数形状不同，是端点真实形状使然，不是遗漏。
export const getCellCode = (rowId: string, columnId: string): Promise<KnowhowCellCode> =>
  requestJson<WireKnowhowCellCode>(`/agent/knowhow/rows/${rowId}/cells/${columnId}/code`, { tag: "knowhow" }).then(mapCellCode);

export const putCellCode = (
  rowId: string,
  columnId: string,
  codeText: string,
  language: string,
): Promise<KnowhowCellCode> =>
  requestJson<WireKnowhowCellCode>(`/agent/knowhow/rows/${rowId}/cells/${columnId}/code`, {
    method: "PUT",
    body: JSON.stringify({ code_text: codeText, language }),
    tag: "knowhow",
  }).then(mapCellCode);

export const deleteCellCode = (rowId: string, columnId: string): Promise<void> =>
  requestVoid(`/agent/knowhow/rows/${rowId}/cells/${columnId}/code`, { method: "DELETE", tag: "knowhow" });

// 行级代码列表（Task 11 抽屉用）：抽屉展开一行时，若为该行每一列逐格 GET
// .../cells/{col}/code 会是 N 次网络往返；GET /agent/knowhow/rows/{row_id}
// （Task 10 已实现、同一套 user_or_agent_scope 会话分支即可访问，见
// knowhow_agent_routes.py）一次性返回这一行全部已存在的代码附件（`code[]`，
// 没有附件的列不会出现在里面——后端"缺席即 none"的既有约定，build_row_detail
// 的文档串原话），一次请求换全行状态，是"最省成本的正确数据路径"（任务简报
// 原话）。本文件只消费该端点的 code 字段、按 column_id 建索引成 map，不映射
// title/cells——那些字段服务于 Agent 判别场景，抽屉自己已有 KnowhowRow.cells
// 可用，没有理由为同一份数据再建一套重复的展示模型。
type WireKnowhowRowCodeEntry = {
  column_id: string;
  language: string;
  code_text: string;
  status: CellCodeStatus;
  updated_at: string;
  updated_by: string;
};

type WireKnowhowRowDetailCode = {
  code?: WireKnowhowRowCodeEntry[];
};

function mapRowCodeByColumn(entries: WireKnowhowRowCodeEntry[] | undefined): Record<string, KnowhowCellCode> {
  const result: Record<string, KnowhowCellCode> = {};
  for (const entry of entries ?? []) {
    result[entry.column_id] = {
      codeText: entry.code_text,
      language: entry.language,
      status: entry.status,
      updatedAt: entry.updated_at,
      updatedBy: entry.updated_by ?? null,
    };
  }
  return result;
}

export const fetchKnowhowRowCodeByColumn = (rowId: string): Promise<Record<string, KnowhowCellCode>> =>
  requestJson<WireKnowhowRowDetailCode>(`/agent/knowhow/rows/${rowId}`, { tag: "knowhow" }).then((wire) => mapRowCodeByColumn(wire.code));

// --- Notebook 资产上传（Task 7；端点本身是 PR-1 既有的
// `POST /notebooks/{nb}/assets`——起草本文件时 Task 4 遗漏了这个 fetcher，
// Task 7 格子编辑器的图片粘贴/拖拽/工具栏插入都需要它，在此补上）----------------

// 响应 {id,url} 本身就是合法 camelCase（两个字段名都不含下划线），没有
// snake_case 字段需要转换，故不另设 WireXxx+mapXxx——直接把响应体类型当
// UploadedAsset 用（本文件其余 fetcher 遇到 snake_case 字段时才需要那一层）。
export type UploadedAsset = { id: string; url: string };

// signal 可选：格子编辑器用它保证「上传绝不比编辑器实例活得久」——组件卸载或用户
// 明确放弃时中断这次请求，避免服务端已落盘资产、前端却没有任何东西引用它。
export const uploadNotebookAsset = (
  notebookId: string,
  file: File | Blob,
  signal?: AbortSignal,
): Promise<UploadedAsset> => {
  const form = new FormData();
  form.append("file", file);
  return requestJson<UploadedAsset>(`/notebooks/${notebookId}/assets`, { method: "POST", body: form, signal, tag: "knowhow" });
};

// --- 版本管理（历史时间线/单条详情/两版对比/单格历史/回退/里程碑/清理，
// knowhow 表版本管理 Task 14；HTTP 端点由 Task 12 落地，见
// backend/app/api/knowhow_routes.py 与设计文档
// docs/superpowers/specs/2026-07-22-knowhow-table-version-control-design.md
// §8.1/§4.4）---------------------------------------------------------------
//
// 四个读端点（时间线/单条/diff/单格历史）在后端没有独立的 response_model——
// 路由函数直接把 store 的 dict 形状原样返回（同 reproject_knowhow_table 等既
// 有薄端点的写法，见 knowhow_routes.py 该节头部注释）。这里的 WireXxx 类型是
// 照 store 方法（app/repositories/sqlite/knowhow_history_store.py）与服务层
// aggregate_diff（app/services/knowhow/history.py）的真实返回形状逐字段核对
// 写的，不是端点自己的类型声明——核对方式：直接读 store 代码，而不是照搬任务
// 简报定稿前的猜测（简报字段名与下面这版有出入的地方，见本任务报告）。

// 14 种变更类型（design doc §4.1，与后端 content_strings_in_payload 的
// REGISTERED_KINDS 逐字对拍一致）。
export type KnowhowChangeKind =
  | "table_create" | "table_meta" | "anchor_set"
  | "column_add" | "column_rename" | "column_kind" | "column_delete"
  | "row_add" | "row_delete" | "cell_update"
  | "cell_code_put" | "cell_code_delete" | "import_append" | "revert";

export type KnowhowChange = {
  id: string;
  tableId: string;
  seq: number;
  kind: KnowhowChangeKind | string; // 未知值原样保留，不在前端收窄成字面量联合
  actor: string;
  origin: string;
  fingerprint: string;
  note: string;
  createdAt: string;
  // 原样透传后端 payload，不做逐 kind 递归 camelCase 转换——14 种 kind 形状差
  // 异很大（design doc §4.4），转换成本和出错面都不小，而 payload 本就没有
  // 统一形状可言。knowhow-history-logic.ts 的纯函数（summarizeChange/
  // foldLocalChanges）直接按后端真实字段名(row_id/column_id/target_seq/rows 等
  // snake_case)读取这个字段。
  payload: Record<string, any>;
};

export type KnowhowMilestone = {
  id: string;
  tableId: string;
  seq: number;
  name: string;
  note: string;
  createdBy: string;
  createdAt: string;
  // 指向的 seq 是否已被「清理历史」删掉——里程碑本身仍保留，只是灰显失效
  // （knowhow_milestones.seq 刻意不设 FK，design doc §4.2）。create 端点的
  // 响应不带这个字段（刚创建的里程碑，目标 seq 此刻必然存在），兜底 false。
  stale: boolean;
};

export type KnowhowHistoryPage = {
  // 服务端当前最新 seq——revert 请求体的 expected_head_seq 就是它的回声；
  // isStaleHead()（knowhow-history-logic.ts）用它判断前端手上的时间线是否
  // 已经落后。
  headSeq: number;
  changes: KnowhowChange[]; // seq 倒序，见 before_seq 向更旧翻页
  milestones: KnowhowMilestone[]; // 本表全部里程碑（含 stale 标记）
};

// 两版净变化的格子条目（服务端 aggregate_diff 的 cells 键）。与 knowhow-
// history-logic.ts 本地折叠函数 foldLocalChanges 的 LocalDiffCell 同形状但
// 类型不同——两者是各自独立的类型（一个来自服务端权威计算，一个来自客户端
// 本地折叠），只是恰好同形。
export type KnowhowHistoryDiffCell = {
  rowId: string;
  columnId: string;
  before: string | null;
  after: string | null;
};

export type KnowhowHistoryDiffCodeEntry = {
  columnId: string;
  codeText: string;
  language: string;
  cellContentHash: string;
  updatedBy: string;
};

// rows_added/rows_removed 里的整行快照——与 row_add/row_delete payload 的
// rows[] 条目同形状（design doc §4.4）：{row_id,position,created_at,cells,code}。
export type KnowhowHistoryDiffRow = {
  rowId: string;
  position: number;
  createdAt: string;
  cells: Record<string, string>; // column_id -> content_md，键本身不转换
  code: KnowhowHistoryDiffCodeEntry[];
};

// 列结构变更的字段子集：只包含区间内真正变化过的键（服务端 aggregate_diff
// 只在 changed 时才收进该字段，见 history.py）。字段名 name/role/position 是
// knowhow_columns 表的真实列名——历史子系统的 payload 从不使用"kind"这个
// 只在 to_wire_column() 才生效的展示层改名（KnowhowColumn.role 同理，本文件
// 顶部已有这个别名，见其注释），故这里字段名与 KnowhowColumn.role 一致，
// 不需要额外转换。
export type KnowhowHistoryDiffColumnFields = Partial<{ name: string; role: string; position: number }>;

export type KnowhowHistoryDiffColumn = {
  columnId: string;
  before: KnowhowHistoryDiffColumnFields;
  after: KnowhowHistoryDiffColumnFields;
};

export type KnowhowHistoryDiffTableMetaFields = { title: string; description: string };

// 服务端 aggregate_diff() 的完整 5 键输出（app/services/knowhow/history.py）。
// 与本文件其余 mapXxx 一致的做法：顶层 snake_case 键改名为 camelCase，内容
// 本身字段名不含下划线的（row_id/column_id 除外，均已展开）原样保留。
export type KnowhowHistoryDiff = {
  cells: KnowhowHistoryDiffCell[];
  rowsAdded: KnowhowHistoryDiffRow[];
  rowsRemoved: KnowhowHistoryDiffRow[];
  columns: KnowhowHistoryDiffColumn[];
  // 区间内表元(title/description)有真实变化才非 null（design doc §6.5）。
  tableMeta: { before: KnowhowHistoryDiffTableMetaFields; after: KnowhowHistoryDiffTableMetaFields } | null;
};

export type KnowhowCellHistoryEntry = {
  seq: number;
  actor: string;
  origin: string;
  createdAt: string;
  before: string | null;
  after: string | null;
};

// revert_to() 的返回形状（knowhow_history_store.py）：正常路径是新 revert
// 流水的 seq；"回退到当前 head"这个 no-op 分支里则是当前 head 本身的 seq
// （不产生新流水）——两种情况下 targetSeq 都原样回声请求里的 target_seq。
export type KnowhowRevertResult = { seq: number; targetSeq: number };

// --- 后端线上形状（snake_case）----------------------------------------------

type WireKnowhowChange = {
  id: string;
  table_id: string;
  seq: number;
  kind: string;
  actor: string;
  origin: string;
  fingerprint: string;
  note: string;
  created_at: string;
  payload: Record<string, any>;
};

type WireKnowhowMilestone = {
  id: string;
  table_id: string;
  seq: number;
  name: string;
  note: string;
  created_by: string;
  created_at: string;
  // create 端点的响应不带这个键（见上面 KnowhowMilestone.stale 的注释）；
  // list（随 history 一起回）恒带。
  stale?: boolean;
};

type WireKnowhowHistoryPage = {
  head_seq: number;
  changes: WireKnowhowChange[];
  milestones: WireKnowhowMilestone[];
};

type WireKnowhowHistoryDiffCodeEntry = {
  column_id: string;
  code_text: string;
  language: string;
  cell_content_hash: string;
  updated_by: string;
};

type WireKnowhowHistoryDiffRow = {
  row_id: string;
  position: number;
  created_at: string;
  cells: Record<string, string>;
  code: WireKnowhowHistoryDiffCodeEntry[];
};

type WireKnowhowHistoryDiffColumn = {
  column_id: string;
  before: KnowhowHistoryDiffColumnFields;
  after: KnowhowHistoryDiffColumnFields;
};

type WireKnowhowHistoryDiff = {
  cells: { row_id: string; column_id: string; before: string | null; after: string | null }[];
  rows_added: WireKnowhowHistoryDiffRow[];
  rows_removed: WireKnowhowHistoryDiffRow[];
  columns: WireKnowhowHistoryDiffColumn[];
  table_meta: {
    before: KnowhowHistoryDiffTableMetaFields;
    after: KnowhowHistoryDiffTableMetaFields;
  } | null;
};

type WireKnowhowCellHistoryEntry = {
  seq: number;
  actor: string;
  origin: string;
  created_at: string;
  before: string | null;
  after: string | null;
};

type WireKnowhowRevertResult = { seq: number; target_seq: number };

function mapChange(wire: WireKnowhowChange): KnowhowChange {
  return {
    id: wire.id,
    tableId: wire.table_id,
    seq: wire.seq,
    // 无需 as 断言：KnowhowChange.kind 的类型本就是 KnowhowChangeKind | string，
    // 一个 string 天然满足这个联合——断言只会误导读者以为这里发生了真实收窄。
    kind: wire.kind,
    actor: wire.actor,
    origin: wire.origin,
    fingerprint: wire.fingerprint,
    note: wire.note,
    createdAt: wire.created_at,
    payload: wire.payload ?? {},
  };
}

function mapMilestone(wire: WireKnowhowMilestone): KnowhowMilestone {
  return {
    id: wire.id,
    tableId: wire.table_id,
    seq: wire.seq,
    name: wire.name,
    note: wire.note ?? "",
    createdBy: wire.created_by ?? "",
    createdAt: wire.created_at,
    stale: wire.stale ?? false,
  };
}

function mapHistoryPage(wire: WireKnowhowHistoryPage): KnowhowHistoryPage {
  return {
    headSeq: wire.head_seq,
    changes: (wire.changes ?? []).map(mapChange),
    milestones: (wire.milestones ?? []).map(mapMilestone),
  };
}

function mapHistoryDiffRow(row: WireKnowhowHistoryDiffRow): KnowhowHistoryDiffRow {
  return {
    rowId: row.row_id,
    position: row.position,
    createdAt: row.created_at,
    cells: row.cells ?? {},
    code: (row.code ?? []).map((entry) => ({
      columnId: entry.column_id,
      codeText: entry.code_text,
      language: entry.language,
      cellContentHash: entry.cell_content_hash,
      updatedBy: entry.updated_by,
    })),
  };
}

function mapHistoryDiffColumn(column: WireKnowhowHistoryDiffColumn): KnowhowHistoryDiffColumn {
  return { columnId: column.column_id, before: column.before ?? {}, after: column.after ?? {} };
}

function mapHistoryDiff(wire: WireKnowhowHistoryDiff): KnowhowHistoryDiff {
  return {
    cells: (wire.cells ?? []).map((cell) => ({
      rowId: cell.row_id,
      columnId: cell.column_id,
      before: cell.before ?? null,
      after: cell.after ?? null,
    })),
    rowsAdded: (wire.rows_added ?? []).map(mapHistoryDiffRow),
    rowsRemoved: (wire.rows_removed ?? []).map(mapHistoryDiffRow),
    columns: (wire.columns ?? []).map(mapHistoryDiffColumn),
    tableMeta: wire.table_meta ?? null,
  };
}

function mapCellHistoryEntry(entry: WireKnowhowCellHistoryEntry): KnowhowCellHistoryEntry {
  return {
    seq: entry.seq,
    actor: entry.actor,
    origin: entry.origin,
    createdAt: entry.created_at,
    before: entry.before ?? null,
    after: entry.after ?? null,
  };
}

function mapRevertResult(wire: WireKnowhowRevertResult): KnowhowRevertResult {
  return { seq: wire.seq, targetSeq: wire.target_seq };
}

// 时间线分页：limit/milestonesOnly 是后端既支持、任务简报草稿未列出的查询
// 参数（GET .../history?limit=&before_seq=&milestones_only=，见
// get_knowhow_history），补上以支撑后续「向更旧翻页」与「只看里程碑」筛选，
// 省得下一任务再回头改这个文件。三者都是尾部可选位置参数，省略时不带对应
// query（不是空串/0），退回后端自己的默认值（limit=50/milestones_only=false）。
export const fetchKnowhowHistory = (
  notebookId: string,
  tableId: string,
  beforeSeq?: number,
  limit?: number,
  milestonesOnly?: boolean,
): Promise<KnowhowHistoryPage> => {
  const params = new URLSearchParams();
  if (beforeSeq !== undefined) params.set("before_seq", String(beforeSeq));
  if (limit !== undefined) params.set("limit", String(limit));
  if (milestonesOnly !== undefined) params.set("milestones_only", String(milestonesOnly));
  const query = params.toString();
  return requestJson<WireKnowhowHistoryPage>(
    `/notebooks/${notebookId}/knowhow/${tableId}/history${query ? `?${query}` : ""}`,
    { tag: "knowhow" },
  ).then(mapHistoryPage);
};

// 单条变更详情：payload 原样返回，供调用方渲染这一次改动的红绿 diff。
export const fetchKnowhowChange = (
  notebookId: string,
  tableId: string,
  seq: number,
): Promise<KnowhowChange> =>
  requestJson<WireKnowhowChange>(
    `/notebooks/${notebookId}/knowhow/${tableId}/history/${seq}`,
    { tag: "knowhow" },
  ).then(mapChange);

// 两版净变化（服务端权威计算，纯只读——不等价于 knowhow-history-logic.ts 的
// 本地 foldLocalChanges，见该文件对应注释）。⚠两版对比视图请用这个（服务端
// 全量 diff，含列结构/表元变化），不要用本地折叠——本地折叠只覆盖 cells + 行
// 增删，接错不会有类型错误，只会静默漏掉列/表元变化。
export const fetchKnowhowHistoryDiff = (
  notebookId: string,
  tableId: string,
  fromSeq: number,
  toSeq: number,
): Promise<KnowhowHistoryDiff> =>
  requestJson<WireKnowhowHistoryDiff>(
    `/notebooks/${notebookId}/knowhow/${tableId}/history/diff?from=${fromSeq}&to=${toSeq}`,
    { tag: "knowhow" },
  ).then(mapHistoryDiff);

// 单格历史时间线，最新在前。limit 同 fetchKnowhowHistory，省略时退回后端
// 默认值（50）。
export const fetchKnowhowCellHistory = (
  notebookId: string,
  tableId: string,
  rowId: string,
  columnId: string,
  limit?: number,
): Promise<KnowhowCellHistoryEntry[]> => {
  const query = limit === undefined ? "" : `?limit=${limit}`;
  return requestJson<WireKnowhowCellHistoryEntry[]>(
    `/notebooks/${notebookId}/knowhow/${tableId}/rows/${rowId}/cells/${columnId}/history${query}`,
    { tag: "knowhow" },
  ).then((entries) => entries.map(mapCellHistoryEntry));
};

// --- revert 三种结构化失败（评审问题 1 修复）------------------------------------
//
// 后端 revert 端点的三种失败（knowhow_routes.py revert_knowhow_table）返回
// 结构化 detail={"code","message"}，不是 user_error() 打 X-User-Message 头的
// 纯字符串——共享错误层 errors.ts 的可展示通道（pickUserDetail）只认字符串/
// 字符串数组形状的 detail，对象形状会被判定为"抠不出来"而返回空串；这条路径
// 也没有 X-User-Message 头，闸1（信任）本来就不会放行。两条都不满足，若什么
// 都不做就会被 throwHumanizedHttpError 兜底成按状态码泛化的通用中文，后端为
// 这三种场景精心写的具体中文（"这张表刚被其他人改过，请刷新后重试"等）用户
// 一句都看不到。
//
// 解法照抄本仓库解决同类问题的先例——transfer 端点的 409
// {code,new_table_id,message}：frontend/app/transfer-model.ts 的
// parseCleanupFailure（纯函数，探测 status+已知 code）+
// frontend/app/knowhow-transfer.ts 的 KnowhowSourceCleanupError（类型化异常）
// + transferKnowhowTable（克隆 body 探测，命中转类型化异常，未命中落回
// throwHumanizedHttpError）。下面的 parseKnowhowRevertFailure/
// KnowhowRevertError/revertKnowhowTable 是同一模式在 revert 上的镜像——文案仍
// 100% 来自后端，这里只负责把它从结构化 body 里取出来，不在前端编造/硬编码
// 这三段话（后端改文案会自动同步，409/400/500 在别的端点还复用于完全不同的
// 语义，前端不能按状态码猜文案）。
export type KnowhowRevertFailureCode =
  | "knowhow_history_stale"
  | "knowhow_history_inconsistent"
  | "knowhow_revert_verify_failed";

// 三个 code 与后端三个 except 分支一一对应的 HTTP 状态：knowhow_history_stale
// =409（head 已变，刷新重试）/ knowhow_history_inconsistent=400（前置指纹不
// 一致，回退已中止）/ knowhow_revert_verify_failed=500（后置校验失败，已整表
// 回滚，表未被改动）。解析时要求 status 与 code 成对匹配（不是只看 code 是否
// 已知），同 parseCleanupFailure 校验 status===409 一样，是防止未来某个无关
// 端点凑巧返回同形状 body 时被误判命中。
const KNOWHOW_REVERT_FAILURE_STATUS: Record<KnowhowRevertFailureCode, number> = {
  knowhow_history_stale: 409,
  knowhow_history_inconsistent: 400,
  knowhow_revert_verify_failed: 500,
};

export type KnowhowRevertFailure = { code: KnowhowRevertFailureCode; message: string };

const FALLBACK_REVERT_FAILURE_MESSAGE = "回退未成功，请刷新后重试";

/**
 * 判定一个 HTTP 响应是否是 revert 端点结构化的 `{code,message}`（三种 code 之
 * 一，见 KnowhowRevertFailureCode）。命中 → {code,message}；其余任何情况
 * （status 与 code 对不上、code 不是这三者之一、detail 是普通字符串、body 根
 * 本不是对象……）一律 null，调用方落回通用错误路径（含目标 seq 不存在的 404
 * ——那条走的是普通字符串 detail，本就不该在这里命中）。纯函数，不摸网络——
 * status/body 由调用方（res.status/已解析的 JSON）转手过来。
 */
export const parseKnowhowRevertFailure = (
  status: number,
  body: unknown,
): KnowhowRevertFailure | null => {
  if (typeof body !== "object" || body === null) return null;
  const detail = (body as Record<string, unknown>).detail;
  if (typeof detail !== "object" || detail === null) return null;
  const d = detail as Record<string, unknown>;
  if (typeof d.code !== "string") return null;
  if (!(d.code in KNOWHOW_REVERT_FAILURE_STATUS)) return null;
  const code = d.code as KnowhowRevertFailureCode;
  if (KNOWHOW_REVERT_FAILURE_STATUS[code] !== status) return null;
  const message = typeof d.message === "string" && d.message ? d.message : FALLBACK_REVERT_FAILURE_MESSAGE;
  return { code, message };
};

// 类型化异常：调用方用 instanceof 分流（如 knowhow_history_stale → 提示刷新
// 页面重来），.message 是后端原文（不再是 throwHumanizedHttpError 兜底的通用
// 文案），.code 供需要按三种场景分别处理的调用方精确分流，不必做脆弱的文案
// 匹配。同 KnowhowSourceCleanupError（knowhow-transfer.ts）的既有取向。
export class KnowhowRevertError extends Error {
  readonly code: KnowhowRevertFailureCode;

  constructor(code: KnowhowRevertFailureCode, message: string) {
    super(message);
    this.name = "KnowhowRevertError";
    this.code = code;
  }
}

// 整表回退到 targetSeq。expectedHeadSeq 是前端上次拉时间线时看到的 head——
// 服务端据此判断陈旧（与库内实际 head 不符 -> 409），并发编辑安全网的关键
// 一环，永远必须传（不是可选参数）。
//
// 走 performApiRequest 而非 requestJson：需要在共享人话层兜底之前先探测上面
// 的三种结构化失败。res.clone() 手法与 knowhow-transfer.ts
// transferKnowhowTable 一致——body 只能消费一次（见 errors.ts readHttpError
// 头注释），探测用克隆读，真正报错仍交给原始 res 走 throwHumanizedHttpError，
// 不能在这里抢先吃掉。
export const revertKnowhowTable = async (
  notebookId: string,
  tableId: string,
  targetSeq: number,
  expectedHeadSeq: number,
): Promise<KnowhowRevertResult> => {
  const res = await performApiRequest(`/notebooks/${notebookId}/knowhow/${tableId}/revert`, {
    method: "POST",
    body: JSON.stringify({ target_seq: targetSeq, expected_head_seq: expectedHeadSeq }),
    tag: "knowhow",
  });
  if (!res.ok) {
    let parsed: unknown;
    try {
      parsed = JSON.parse(await res.clone().text());
    } catch {
      parsed = undefined;
    }
    const failure = parseKnowhowRevertFailure(res.status, parsed);
    if (failure) throw new KnowhowRevertError(failure.code, failure.message);
    await throwHumanizedHttpError(res, "knowhow");
  }
  return mapRevertResult((await res.json()) as WireKnowhowRevertResult);
};

// 给某个 seq 命名。seq 必须是这张表真实存在的一条流水，否则 404；重名
// （UNIQUE(table_id,name)）是 user_error() 打过标记的纯字符串 400，会正常
// 经共享错误层展示给用户（与上面 revertKnowhowTable 的结构化错误体不同）。
export const createKnowhowMilestone = (
  notebookId: string,
  tableId: string,
  seq: number,
  name: string,
  note = "",
): Promise<KnowhowMilestone> =>
  requestJson<WireKnowhowMilestone>(
    `/notebooks/${notebookId}/knowhow/${tableId}/milestones`,
    { method: "POST", body: JSON.stringify({ seq, name, note }), tag: "knowhow" },
  ).then(mapMilestone);

// 删除里程碑（只删指针，不牵动任何流水）；已经不存在时后端静默 no-op。
export const deleteKnowhowMilestone = (
  notebookId: string,
  tableId: string,
  milestoneId: string,
): Promise<void> =>
  requestVoid(
    `/notebooks/${notebookId}/knowhow/${tableId}/milestones/${milestoneId}`,
    { method: "DELETE", tag: "knowhow" },
  );

// 清理 N 天前的历史（只删最老连续前缀，head 永远保留，见 design doc §7.7）。
// 响应 {removed:number} 本身已是合法 camelCase，无需 WireXxx+mapXxx（同本文件
// UploadedAsset 的既有取向）。执行后端会顺带触发一次资产清扫（后台执行，
// 不影响这次请求的响应时间）。
export const pruneKnowhowHistory = (
  notebookId: string,
  tableId: string,
  beforeDays: number,
): Promise<{ removed: number }> =>
  requestJson<{ removed: number }>(
    `/notebooks/${notebookId}/knowhow/${tableId}/history/prune`,
    { method: "POST", body: JSON.stringify({ before_days: beforeDays }), tag: "knowhow" },
  );

// --- 纯 helper(单测) ------------------------------------------------------------

// 行标题自动合成(展示用，记录型/无行标题列的表用来在网格/抽屉里显示一个可读
// 的行标签；后端 textops.compose_row_title 同规则孪生实现——见设计文档④)：
// 按 cells 顺序(调用方保证按列 position 传入)取前 <= maxSegments 个「非空
// 首行」，每段截断到 <= 16 字——**纯 slice，不加省略号**（与 node_name 不同：
// 本函数拼接的是多个已截断的短片段，每段都叠一个省略号只会更花哨、不会更
// 清楚，后端孪生实现同理不加），用 " · " 连接。首行为空白的格子视为空、跳过
// 继续找下一个非空格子(不提前中止扫描)。全部为空(或 cells 为空数组)时返回
// 空串——由调用方兜底展示为「行 N」。
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

// 与后端 textops.compose_row_title 的 `first_line[:_ROW_TITLE_SEGMENT_LIMIT]`
// 严格同规则：纯 slice，本身就是恒等幂等(<=maxLen 时原样返回)，不额外 trim/
// 加省略号（省略号是 node_name 的记号，composeRowTitle 的合成标题不用）。
function truncateSegment(text: string, maxLen: number): string {
  return text.slice(0, maxLen);
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
