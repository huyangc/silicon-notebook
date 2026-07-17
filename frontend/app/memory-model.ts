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
    none: "提升到 KG",
    proposed: "KG 审核中",
    approved: "已进入 Base KG",
    rejected: "KG 晋升已拒绝",
  }[state];
}

export function memoryHash(notebookId: string | null): string {
  return notebookId
    ? `#notebook=${encodeURIComponent(notebookId)}&tab=memory`
    : "#memory";
}

export function parseMemoryHash(hash: string): { scope: MemoryScope; notebookId: string | null } | null {
  if (hash === "#memory") return { scope: "global", notebookId: null };
  const params = new URLSearchParams(hash.replace(/^#/, ""));
  const notebookId = params.get("notebook");
  if (notebookId && params.get("tab") === "memory") {
    return { scope: "notebook", notebookId };
  }
  return null;
}

// 与 memoryHash/parseMemoryHash 同住一个文件是有意的:两条 hash 共用同一套语法
// (`#notebook=<id>` 与 `#notebook=<id>&tab=memory` 只差一个 tab 参数),拆开必然漂移。
export function notebookHash(notebookId: string): string {
  return `#notebook=${encodeURIComponent(notebookId)}`;
}

// 只认「有 notebook 且没有 tab=memory」的裸工作区 hash。带 tab=memory 的归
// parseMemoryHash 管——两个解析器必须互斥,否则挂载时会抢同一条 hash。
export function parseWorkspaceHash(hash: string): { notebookId: string } | null {
  const params = new URLSearchParams(hash.replace(/^#/, ""));
  const notebookId = params.get("notebook");
  if (!notebookId || params.get("tab") === "memory") return null;
  return { notebookId };
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

export function memoryProvenanceRows(memory: {
  origin: MemoryOrigin;
  provenance: Record<string, unknown>;
  agent_profile_id?: string | null;
}): Array<[string, string]> {
  const provenance = memory.provenance ?? {};
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
