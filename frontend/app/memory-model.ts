import type { MemoryOrigin, MemoryPromotionState, MemoryStatus } from "./workspace-model";
import { label, EVIDENCE_LEVEL } from "./vocabulary.ts";
import { ASK_MODES } from "./ask-modes.ts";

const ASK_MODE_LABELS: Record<string, string> = Object.fromEntries(
  ASK_MODES.map((m) => [m.id, m.label]),
);

export type MemoryScope = "global" | "notebook";

export const MEMORY_INPUT_LIMITS = {
  titleMaxChars: 80,
  contentMaxChars: 40_000,
  tagMaxCount: 20,
  tagMaxChars: 80,
} as const;

export function validateMemoryDraft(draft: {
  title: string;
  content_md: string;
  tags: string[];
}): string {
  const title = draft.title.trim();
  const content = draft.content_md.trim();
  if (!title) return "标题不能为空";
  if (title.length > MEMORY_INPUT_LIMITS.titleMaxChars) {
    return `标题不能超过 ${MEMORY_INPUT_LIMITS.titleMaxChars} 个字符`;
  }
  if (!content) return "内容不能为空";
  if (content.length > MEMORY_INPUT_LIMITS.contentMaxChars) {
    return `内容不能超过 ${MEMORY_INPUT_LIMITS.contentMaxChars} 个字符`;
  }
  if (draft.tags.length > MEMORY_INPUT_LIMITS.tagMaxCount) {
    return `标签不能超过 ${MEMORY_INPUT_LIMITS.tagMaxCount} 个`;
  }
  const normalizedTags = draft.tags.map((tag) => tag.trim());
  if (normalizedTags.some((tag) => !tag)) return "标签不能为空";
  const tags = Array.from(new Set(normalizedTags));
  if (tags.some((tag) => tag.length > MEMORY_INPUT_LIMITS.tagMaxChars)) {
    return `单个标签不能超过 ${MEMORY_INPUT_LIMITS.tagMaxChars} 个字符`;
  }
  return "";
}

/**
 * Confirm-request body. `extract_kg` is included only when the notebook's KG is
 * eligible; otherwise the key is omitted entirely because the backend model uses
 * `extra="forbid"` and an unknown field would be rejected with 422.
 */
export function confirmMemoryBody(input: {
  title: string;
  content_md: string;
  tags: string[];
  eligible: boolean;
  extractKg: boolean;
}): { title: string; content_md: string; tags: string[]; extract_kg?: boolean } {
  const body: { title: string; content_md: string; tags: string[]; extract_kg?: boolean } = {
    title: input.title,
    content_md: input.content_md,
    tags: input.tags,
  };
  if (input.eligible) body.extract_kg = input.extractKg;
  return body;
}

/**
 * From-answer capture body. Same eligibility gate on `extract_kg` as
 * {@link confirmMemoryBody}; `answerId` is mapped to the wire field `answer_id`.
 */
export function fromAnswerMemoryBody(input: {
  answerId: string;
  title: string;
  content_md: string;
  tags: string[];
  eligible: boolean;
  extractKg: boolean;
}): { answer_id: string; title: string; content_md: string; tags: string[]; extract_kg?: boolean } {
  const body: { answer_id: string; title: string; content_md: string; tags: string[]; extract_kg?: boolean } = {
    answer_id: input.answerId,
    title: input.title,
    content_md: input.content_md,
    tags: input.tags,
  };
  if (input.eligible) body.extract_kg = input.extractKg;
  return body;
}

type MemoryMeta = { label: string; tone: string };

const STATUS_META: Record<MemoryStatus, MemoryMeta> = {
  candidate: { label: "待确认", tone: "warning" },
  confirmed: { label: "已确认", tone: "success" },
  rejected: { label: "已拒绝", tone: "muted" },
  deprecated: { label: "已停用", tone: "muted" },
};

const ORIGIN_META: Record<MemoryOrigin, MemoryMeta> = {
  ask_answer: { label: "Ask 回答", tone: "ask" },
  external_agent: { label: "Agent 提议", tone: "agent" },
};

export function memoryStatusMeta(status: MemoryStatus): MemoryMeta {
  return STATUS_META[status];
}

export function memoryOriginMeta(origin: MemoryOrigin): MemoryMeta {
  return ORIGIN_META[origin];
}

export function canEditMemory(status: MemoryStatus): boolean {
  return status === "candidate" || status === "confirmed";
}

export function canPromoteMemory(memory: {
  status: MemoryStatus;
  promotion_state: MemoryPromotionState;
}): boolean {
  return memory.status === "confirmed" && memory.promotion_state === "none";
}

export function memoryPromotionPath(memoryId: string): string {
  return `/memories/${encodeURIComponent(memoryId)}/promote`;
}

export function memoryPromotionLabel(state: MemoryPromotionState): string {
  return {
    none: "贡献到公共知识库",
    proposed: "审核中",
    approved: "已收录",
    rejected: "未采纳",
  }[state];
}

export type MemoryNavigationTarget = {
  notebookId?: string | null;
  status?: MemoryStatus | null;
  itemId?: string | null;
};

export type ParsedMemoryHash = {
  scope: MemoryScope;
  notebookId: string | null;
  filterNotebookId: string | null;
  status: MemoryStatus | null;
  itemId: string | null;
};

export function memoryHash(
  notebookId: string | null,
  target: MemoryNavigationTarget = {},
): string {
  if (notebookId) {
    return `#notebook=${encodeURIComponent(notebookId)}&tab=memory`;
  }
  const parts = ["memory"];
  if (target.notebookId) parts.push(`notebook=${encodeURIComponent(target.notebookId)}`);
  if (target.status) parts.push(`status=${encodeURIComponent(target.status)}`);
  if (target.itemId) parts.push(`item=${encodeURIComponent(target.itemId)}`);
  return `#${parts.join("&")}`;
}

export function parseMemoryHash(hash: string): ParsedMemoryHash | null {
  const raw = hash.replace(/^#/, "");
  if (raw === "memory" || raw.startsWith("memory&")) {
    const params = new URLSearchParams(raw === "memory" ? "" : raw.slice(7));
    const status = params.get("status");
    const validStatus = status === "candidate"
      || status === "confirmed"
      || status === "rejected"
      || status === "deprecated";
    return {
      scope: "global",
      notebookId: null,
      filterNotebookId: params.get("notebook"),
      status: validStatus ? status : null,
      itemId: params.get("item"),
    };
  }
  const params = new URLSearchParams(raw);
  const notebookId = params.get("notebook");
  if (notebookId && params.get("tab") === "memory") {
    return {
      scope: "notebook",
      notebookId,
      filterNotebookId: null,
      status: null,
      itemId: null,
    };
  }
  return null;
}

// 与 memoryHash/parseMemoryHash 同住一个文件是有意的:两条 hash 共用同一套语法
// (`#notebook=<id>` 与 `#notebook=<id>&tab=memory` 只差一个 tab 参数),拆开必然漂移。
export function notebookHash(notebookId: string, sourceId = ""): string {
  const source = sourceId ? `&source=${encodeURIComponent(sourceId)}` : "";
  return `#notebook=${encodeURIComponent(notebookId)}${source}`;
}

// 只认「有 notebook 且没有 tab=memory」的裸工作区 hash。带 tab=memory 的归
// parseMemoryHash 管——两个解析器必须互斥,否则挂载时会抢同一条 hash。
export function parseWorkspaceHash(hash: string): { notebookId: string; sourceId: string } | null {
  const raw = hash.replace(/^#/, "");
  if (raw === "memory" || raw.startsWith("memory&")) return null;
  const params = new URLSearchParams(raw);
  const notebookId = params.get("notebook");
  if (!notebookId || params.get("tab") === "memory") return null;
  return { notebookId, sourceId: params.get("source") || "" };
}

export function answerIdBatches(answerIds: string[], batchSize = 200): string[][] {
  const uniqueIds = Array.from(new Set(answerIds.map((answerId) => answerId.trim()).filter(Boolean)));
  const size = Math.max(1, Math.min(200, Math.floor(batchSize) || 200));
  const batches: string[][] = [];
  for (let offset = 0; offset < uniqueIds.length; offset += size) {
    batches.push(uniqueIds.slice(offset, offset + size));
  }
  return batches;
}

export function subscribeMemorySessionAbort(
  signal: AbortSignal,
  abortRequests: () => void,
): () => void {
  if (signal.aborted) {
    abortRequests();
    return () => undefined;
  }
  signal.addEventListener("abort", abortRequests, { once: true });
  return () => signal.removeEventListener("abort", abortRequests);
}

export async function collectSavedAnswerFlags(
  batches: string[][],
  loadBatch: (answerIds: string[]) => Promise<{ links: Record<string, string> }>,
  signal: AbortSignal,
): Promise<Record<string, boolean> | null> {
  if (signal.aborted) return null;
  const results = await Promise.all(batches.map((batch) => loadBatch(batch)));
  if (signal.aborted) return null;
  return Object.fromEntries(
    results.flatMap((result) => Object.keys(result.links).map((answerId) => [answerId, true])),
  );
}

export function memoryListPath({
  scope,
  notebookId,
  status,
  origin,
  query,
  offset,
  limit,
}: {
  scope: MemoryScope;
  notebookId: string | null;
  status: MemoryStatus | "all";
  origin: MemoryOrigin | "all";
  query: string;
  offset: number;
  limit: number;
}): string {
  const base = scope === "notebook" && notebookId
    ? `/notebooks/${encodeURIComponent(notebookId)}/memories`
    : "/memories";
  const params = new URLSearchParams();
  if (status !== "all") params.set("status", status);
  if (origin !== "all") params.set("origin", origin);
  if (scope === "global" && notebookId) params.set("notebook_id", notebookId);
  if (query.trim()) params.set("query", query.trim());
  params.set("offset", String(offset));
  params.set("limit", String(limit));
  return `${base}?${params.toString()}`;
}

function contextSummary(value: unknown): string {
  if (!value || typeof value !== "object" || Array.isArray(value)) return "";
  return Object.entries(value as Record<string, unknown>)
    .filter(([, item]) => ["string", "number", "boolean"].includes(typeof item))
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([key, item]) => `${key}: ${String(item)}`)
    .join(" · ");
}

// P2-A（round 6 评审）：跨 notebook 复制/移动而来的 memory，provenance 顶层只
// 有 imported_from 一个键（memory_service.py transfer() 的既有约定——源的
// 完整 provenance 原样嵌在 imported_from.source_provenance 之下，绝不铺到顶
// 层：里面的 anchors/citations 指向的是源 notebook 的行，在当前 notebook 里
// 根本解析不了）。下面两个 helper 把这层嵌套投影成「仅存档」的纯文本行——
// 不是活引用，不可点击，只是留档"这条记忆曾经有过什么依据"。
const TRANSFER_ACTION_LABEL: Record<string, string> = {
  copy: "复制",
  move: "移动",
};

type ImportedFrom = {
  notebook_id?: unknown;
  action?: unknown;
  source_provenance?: unknown;
};

function importedFromOf(provenance: Record<string, unknown>): ImportedFrom | null {
  const imported = provenance.imported_from;
  return imported && typeof imported === "object" ? (imported as ImportedFrom) : null;
}

// round 8 P2-B：一条 memory 被传输不止一次（A → B → C……）时，
// memory_service.py transfer() 的既有约定（"源的完整 provenance 原样嵌在
// imported_from.source_provenance 之下"）会逐跳叠罗汉——第二次传输把第一次
// 传输产出的整个 {imported_from: {...}} 对象原样塞进它自己的
// source_provenance，而不是被"拆开揉平"。结果是一条 imported_from.
// source_provenance.imported_from.source_provenance…… 的链，长度等于这条
// memory 被传输过的次数。单跳（只传输过一次）时链长为 1，source_provenance
// 直接就是最初 ask-answer/agent 的原始载荷——这正是本文件已有测试覆盖的
// 形状。importedFromChain 把整条链摘出来，最近的一跳在前（数组下标 0 =
// 最后一次传输，即"这条 memory 是从哪个 notebook 直接搬过来的"）；archival
// ProvenanceRows 再从链的最后一环（最早、最深的那一跳）取真正的原始 ask-
// answer/agent 载荷——那才是 question/citations/evidence_refs 实际存在的
// 地方，中间任何一跳的 source_provenance 都只是"下一层包装"，没有这些字段。
// 上限 10 跳纯粹是防御性的（畸形/循环数据的兜底），不是产品期望的深度——
// 真实使用中几次传输就会到两三跳，10 只是绝不该触发的安全阀。
const MAX_PROVENANCE_HOPS = 10;

function importedFromChain(provenance: Record<string, unknown>): ImportedFrom[] {
  const hops: ImportedFrom[] = [];
  let current = importedFromOf(provenance);
  while (current && hops.length < MAX_PROVENANCE_HOPS) {
    hops.push(current);
    const nested = current.source_provenance;
    const nestedObj = nested && typeof nested === "object" ? (nested as Record<string, unknown>) : null;
    current = nestedObj ? importedFromOf(nestedObj) : null;
  }
  return hops;
}

// 仅存档投影本身：不复用「活」provenance 那两条分支的完整字段集合（创建
// Agent/客户端请求这些字段对一条已经离开源 notebook 的记忆没有意义，只挑
// 「回答了什么问题/背后有多少证据」这类留档价值最高的信号），只读最深层
// source_provenance 里的 question/citations（ask_answer）或 evidence_refs
// （external_agent）——与下面「活」分支各自读的字段一一对应，故意不合并成
// 一份共享逻辑：两处未来各自演化时不该互相牵连。
function archivalProvenanceRows(
  origin: MemoryOrigin,
  provenance: Record<string, unknown>,
): Array<[string, string]> {
  const hops = importedFromChain(provenance);
  if (hops.length === 0) return [];
  // 每一跳各自一行："来源"给最近的一跳（与既有单跳测试的字面文案保持逐字
  // 一致，不引入无谓的 diff）；再往上（更早）的每一跳单独编号，避免 React
  // 渲染用 label 当 key 时因同名"来源"行相撞（见 memory-panel.tsx 的
  // <div key={label}>）。
  const rows: Array<[string, string]> = hops.map((hop, index) => {
    const notebookId = String(hop.notebook_id ?? "");
    const actionLabel = label(TRANSFER_ACTION_LABEL, String(hop.action ?? ""), "传输");
    const text = notebookId ? `${actionLabel}自笔记本 ${notebookId}` : `${actionLabel}而来`;
    return index === 0 ? ["来源", text] : [`上级来源 ${index}`, text];
  });
  const deepest = hops[hops.length - 1];
  const rawDeepest = deepest.source_provenance;
  const sourceProvenance =
    rawDeepest && typeof rawDeepest === "object" ? (rawDeepest as Record<string, unknown>) : {};
  if (origin === "ask_answer") {
    const question = String(sourceProvenance.question ?? "");
    if (question) rows.push(["原笔记本问题（仅存档）", question]);
    const citations = Array.isArray(sourceProvenance.citations)
      ? sourceProvenance.citations.length
      : 0;
    if (citations > 0) rows.push(["原笔记本引用（仅存档）", `${citations} 条`]);
  } else {
    const evidenceRefs = Array.isArray(sourceProvenance.evidence_refs)
      ? sourceProvenance.evidence_refs.length
      : 0;
    if (evidenceRefs > 0) rows.push(["原笔记本证据引用（仅存档）", `${evidenceRefs} 条`]);
  }
  return rows;
}

export function memoryProvenanceRows(memory: {
  origin: MemoryOrigin;
  provenance: Record<string, unknown>;
  agent_profile_id?: string | null;
}): Array<[string, string]> {
  const provenance = memory.provenance ?? {};
  const imported = importedFromOf(provenance);
  if (imported) return archivalProvenanceRows(memory.origin, provenance);
  if (memory.origin === "ask_answer") {
    const citations = Array.isArray(provenance.citations) ? provenance.citations.length : 0;
    const rows: Array<[string, string]> = [
      ["原问题", String(provenance.question ?? "")],
      ["提问方式", label(ASK_MODE_LABELS, String(provenance.mode ?? ""), "—")],
      ["依据", label(EVIDENCE_LEVEL, String(provenance.evidence_level ?? ""), "—")],
      ["引用", `${citations} 条`],
    ];
    return rows.filter(([, value]) => Boolean(value));
  }
  const profile = provenance.agent_profile && typeof provenance.agent_profile === "object"
    ? provenance.agent_profile as Record<string, unknown>
    : null;
  const profileName = String(profile?.name ?? "");
  const profileId = String(profile?.id ?? memory.agent_profile_id ?? "");
  const rows: Array<[string, string]> = [
    ["创建 Agent", profileName ? `${profileName}${profileId ? ` (${profileId})` : ""}` : profileId],
    ["客户端请求", String(provenance.client_request_id ?? "")],
    ["提议原因", String(provenance.reason ?? "")],
    ["任务上下文", contextSummary(provenance.task_context)],
  ];
  return rows.filter(([, value]) => Boolean(value));
}

export type MemoryCitationReference = {
  sourceId: string;
  elementId: string;
  sourceTitle: string;
  sourceFileName: string;
  locationLabel: string;
  quotedSpan: string;
  archival: boolean;
};

/**
 * Project persisted Ask citations into a stable, display-safe shape.
 *
 * Transferred memories retain their original provenance under nested
 * ``imported_from`` wrappers.  Those references remain useful as an archive,
 * but are not live navigation targets in the destination notebook.
 */
export function memoryCitationReferences(memory: {
  origin: MemoryOrigin;
  provenance: Record<string, unknown>;
}): MemoryCitationReference[] {
  if (memory.origin !== "ask_answer") return [];
  const provenance = memory.provenance ?? {};
  const hops = importedFromChain(provenance);
  const archival = hops.length > 0;
  const raw = archival ? hops[hops.length - 1]?.source_provenance : provenance;
  const source = raw && typeof raw === "object" ? raw as Record<string, unknown> : {};
  const citations = Array.isArray(source.citations) ? source.citations : [];
  const seen = new Set<string>();
  const references: MemoryCitationReference[] = [];
  for (const value of citations) {
    if (!value || typeof value !== "object") continue;
    const citation = value as Record<string, unknown>;
    const sourceId = String(citation.source_id ?? "");
    const elementId = String(citation.element_id ?? "");
    const citationLabel = String(citation.label ?? "");
    const sourceFileName = String(citation.source_file_name ?? "");
    const locationLabel = String(citation.location_label ?? "");
    const locationSuffix = locationLabel ? ` · ${locationLabel}` : "";
    const sourceTitle = locationSuffix && citationLabel.endsWith(locationSuffix)
      ? citationLabel.slice(0, -locationSuffix.length).trim()
      : citationLabel;
    const quotedSpan = String(citation.quoted_span ?? "");
    if (!sourceTitle && !sourceFileName && !locationLabel && !quotedSpan) continue;
    const identity = `${sourceId}\u0000${elementId}\u0000${quotedSpan}`;
    if (seen.has(identity)) continue;
    seen.add(identity);
    references.push({
      sourceId,
      elementId,
      sourceTitle,
      sourceFileName,
      locationLabel,
      quotedSpan,
      archival,
    });
  }
  return references;
}

export type MemoryEvidenceRow = {
  type: string;
  identity: string;
  status: string;
  reason: string;
  trusted: boolean;
};

// Agent 证据卡片自己的枚举——只在 memory 功能内使用，不进 vocabulary.ts（该文件只装
// 跨模块枚举）。放在这个 model 文件而不是 memory-panel.tsx，是因为 Node 的
// `--test` 原生 TS loader 不认 `.tsx`（无法 import JSX 文件），表要能被单测
// 直接 import 就只能落在 `.ts` 侧；渲染层 memory-panel.tsx 只 import 不重复定义。
// 取值真源：backend/app/repositories/sqlite/memory_store.py 的
// `_validate_evidence_ref_on` / `_validation`。
export const EVIDENCE_TYPE: Record<string, string> = {
  source_element: "原文片段",
  source: "原文出处",
  knowledge: "知识条目",
  memory: "记忆",
  unsupported: "无法识别",
};
export const EVIDENCE_STATUS: Record<string, string> = {
  validated: "已核对",
  invalid: "未能核对",
};

export function memoryEvidenceRows(memory: {
  origin: MemoryOrigin;
  provenance: Record<string, unknown>;
}): MemoryEvidenceRow[] {
  if (memory.origin !== "external_agent") return [];
  const refs = Array.isArray(memory.provenance?.evidence_refs)
    ? memory.provenance.evidence_refs
    : [];
  return refs.map((raw) => {
    const ref = raw && typeof raw === "object" ? raw as Record<string, unknown> : {};
    const validation = ref.validation && typeof ref.validation === "object"
      ? ref.validation as Record<string, unknown>
      : {};
    const identity = [
      ref.source_id,
      ref.element_id,
      ref.knowledge_id,
      ref.memory_id,
    ].filter(Boolean).map(String).join(" / ") || `#${Number(ref.index ?? 0) + 1}`;
    return {
      type: String(ref.type ?? "unsupported"),
      identity,
      status: String(validation.status ?? "invalid"),
      reason: String(validation.reason ?? "unverified"),
      trusted: ref.trusted === true,
    };
  });
}
