import type { MemoryOrigin, MemoryPromotionState, MemoryStatus } from "./workspace-model";

export type MemoryScope = "global" | "notebook";

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
}): Array<[string, string]> {
  const provenance = memory.provenance ?? {};
  if (memory.origin === "ask_answer") {
    const citations = Array.isArray(provenance.citations) ? provenance.citations.length : 0;
    const rows: Array<[string, string]> = [
      ["原问题", String(provenance.question ?? "")],
      ["问答模式", String(provenance.mode ?? "")],
      ["证据等级", String(provenance.evidence_level ?? "")],
      ["引用", `${citations} 条`],
    ];
    return rows.filter(([, value]) => Boolean(value));
  }
  const evidenceRefs = Array.isArray(provenance.evidence_refs) ? provenance.evidence_refs.length : 0;
  const rows: Array<[string, string]> = [
    ["提议原因", String(provenance.reason ?? "")],
    ["任务上下文", contextSummary(provenance.task_context)],
    ["证据引用", `${evidenceRefs} 条`],
  ];
  return rows.filter(([, value]) => Boolean(value));
}
