"use client";

import { ChangeEvent, FormEvent, KeyboardEvent, MouseEvent, useEffect, useMemo, useRef, useState } from "react";
import { BarChart3, Check, ChevronDown, ChevronRight, Copy, Database, Edit3, ExternalLink, FileText, LayoutDashboard, MessageSquareText, Network, PanelRightClose, Plus, Search, Settings, Share2, Sparkles, ThumbsDown, ThumbsUp, Trash2, X } from "lucide-react";
import katex from "katex";
import "katex/dist/katex.min.css";
import dynamic from "next/dynamic";
import {
  buildAnswerReferences,
  parseMarkdownBlocks,
  referenceByAnchorKey,
  renderTextWithReferenceNumbers,
  type AnswerReference,
  type MarkdownBlock,
} from "./answer-formatting";
import { takeNdjsonLines, type AskStreamEvent, type ReasoningTraceStep } from "./ask-stream";
import { getReasoningTraceSummary, getTraceStepDetail, TRACE_STEP_LABELS } from "./reasoning-trace";
import { lastTurnUsedReasoning } from "./session-reasoning";
import {
  approvePromotion,
  fetchPromotionQueue,
  proposePromotion,
  rejectPromotion,
  type PromotionCandidate,
} from "./promotion-queue";
// react-force-graph-2d uses canvas/window; load client-side only.
const ForceGraph2D = dynamic(() => import("react-force-graph-2d"), { ssr: false });

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://127.0.0.1:8000/api";

type NotebookSummary = {
  id: string;
  name: string;
  purpose: string;
  primary_domain: string;
  status: string;
  counts: Record<string, number>;
  created_label: string;
  target_users?: string;
  expected_questions?: string[];
  source_types?: string[];
  taxonomy?: string[];
  access_scope?: string;
  tier?: string;
};

type SourceSummary = {
  id: string;
  notebook_id: string;
  title: string;
  type: string;
  status: string;
  parse_status: string;
  summary: string;
  element_count: number;
  file_name: string;
  file_size: number;
  created_label: string;
  error_message?: string;
  extraction_warning?: string | null;
};

type SourceElement = {
  id: string;
  source_id: string;
  element_type: string;
  location_label: string;
  text: string;
  metadata: Record<string, unknown>;
};

type SearchHit = {
  scope: string;
  notebook_id: string;
  label: string;
  text: string;
  source_id: string;
  element_id: string;
};

type Health = {
  status: string;
  llm_configured: boolean;
};

type Evidence = {
  source_id: string;
  source_title: string;
  location_label: string;
  quoted_span: string;
  element_id: string;
};

type ArticleSummary = {
  id: string;
  notebook_id: string;
  source_id: string;
  title: string;
  status: string;
  summary: string;
};

type AnswerAnchor = {
  key: string;
  object_id: string;
  object_type: string;
  label: string;
  name: string;
  definition?: string | null;
  snippet?: string | null;
  source_title: string;
  location_label: string;
};

type AskResponse = {
  answer_id: string;
  conversation_id: string;
  conclusion: string;
  answer: string;
  grounded: boolean;
  anchors: AnswerAnchor[];
  related_knowledge: KnowledgeRecord[];
  citations: Citation[];
  llm_mode: string;
  evidence_level?: "grounded" | "overview" | "inferred";
  retrieval_query?: string;
  top_relevance?: number;
  reasoning_trace?: ReasoningTraceStep[];
};

type ChatTurn = { question: string; response: AskResponse };
type ConversationSummary = { id: string; title: string; updated_at: string; turn_count: number; used_reasoning?: boolean };
type ConversationDetail = {
  id: string;
  notebook_id: string;
  title: string;
  updated_at: string;
  turn_count: number;
  turns: { answer_id: string; question: string; response: AskResponse; created_at: string }[];
};

type ArticleResearchBrief = {
  article: ArticleSummary;
  core_contribution: string;
  claims: string[];
  limitations: string[];
  notebook_relationships: string[];
  derived_rule_candidates: string[];
  validation_plan: string[];
  citations: Citation[];
};

type Citation = {
  label: string;
  source_id: string;
  element_id: string;
  location_label: string;
  quoted_span: string;
};

type ChatMode = "ask" | "rules";

const CHAT_MODES: Array<[ChatMode, string]> = [
  ["ask", "问答"],
  ["rules", "知识库"]
];

// Any object_type string returned by /knowledge-types.
type KnowledgeKind = string;

type KnowledgeFieldValue = { key: string; value: string };
type KnowledgeTypeCount = { object_type: string; label: string; count: number };

type ObjectSchema = {
  object_type: string;
  plural: string;
  fields: string[];
  primary: string;
  description: string;
  label: string;
  list_fields: string[];
  source: string; // builtin | custom | induced
  status: string; // active | proposed | disabled
  rationale: string;
  notebook_id: string;
};
// Generic record returned by GET /notebooks/{id}/knowledge?type=
type KnowledgeRecord = {
  id: string;
  object_type: string;
  headline?: string;
  fields: KnowledgeFieldValue[];
  status: string;
  owner?: string;
  last_reviewed?: string;
  evidence: Evidence[];
};

const KNOWLEDGE_STATUS_OPTIONS = [
  "reviewed",
  "approved",
  "deprecated",
  "conflict",
  "project_specific"
];

// Loose shape covering every card type the knowledge endpoints return.
type KnowledgeItem = {
  id: string;
  status: string;
  owner?: string;
  last_reviewed?: string;
  evidence: Evidence[];
  title?: string;
  statement?: string;
  applies_to?: string[];
  recommendation?: string;
  risk_if_ignored?: string;
  severity?: string;
  name?: string;
  use_when?: string;
  benefit?: string;
  limitation?: string;
  description?: string;
  term?: string;
  definition?: string;
  // Generic-type fields (case/claim/finding/concept/principle/example).
  headline?: string;
  object_type?: string;
  fields?: KnowledgeFieldValue[];
};

const EMPTY_KNOWLEDGE: Record<string, KnowledgeItem[] | null> = {};

type KnowledgeRef = { id: string; object_type: string; headline: string; status: string };
type DuplicateGroup = { object_type: string; similarity: number; members: KnowledgeRef[] };

type DerivedRuleCandidate = {
  id: string;
  notebook_id: string;
  article_id: string;
  title: string;
  proposed_rule: string;
  rationale: string;
  status: string;
  evidence: Evidence[];
  created_label: string;
};

type NotebookAnalytics = {
  answers_total: number;
  feedback_useful: number;
  feedback_not_useful: number;
  usefulness_rate: number;
  low_rated_questions: string[];
  candidate_counts: Record<string, number>;
  knowledge_counts: Record<string, number>;
  source_status_counts: Record<string, number>;
};


type KnowledgeNode = { id: string; object_type: string; headline: string; status: string };
type KnowledgeEdge = { from_id: string; to_id: string; relation: string; label: string };
type KnowledgeGraph = { nodes: KnowledgeNode[]; edges: KnowledgeEdge[] };

type UnifiedConceptNode = { id: string; object_type: string; payload: { name?: string; [k: string]: unknown } };
type UnifiedEdge = { source_object_id: string; target_object_id: string; edge_type: string };
type UnifiedGraphResp = { nodes: UnifiedConceptNode[]; edges: UnifiedEdge[] };
type EvidenceItem = { source_id: string; source_title: string; element_id: string; element_type: string; location_label: string; quoted_span: string; confidence: number; element_text?: string };
type KgObject = { id: string; object_type: string; payload: { name?: string; section_path?: string; [k: string]: unknown }; evidence: EvidenceItem[]; edge_type?: string };
type ConceptDetailResp = { canonical_id: string; canonical_name: string; members: KgObject[]; attached: KgObject[]; evidence: EvidenceItem[] };
type KgOccurrence = { quoted_span?: string; source_title?: string; source_id?: string; element_text?: string; location_label?: string; element_type?: string; confidence?: number };
type KgProcedureStep = { name: string; element_text: string };
type NodeContext = { id: string; object_type: string; name: string; section_path: string; occurrences: KgOccurrence[]; definition: string | null; steps: KgProcedureStep[] | null };
type PendingMerge = { id: string; canonical_a: string; canonical_b: string; score: number; status: string };
type UnifiedKgStatus = { dirty: boolean; last_rebuild_at: string; objects: number; relations: number; clusters: number };
type MergeReviewSummary = { reviewed: number; confirmed: number; rejected: number; unsure: number };
type FgNode = { id: string; name: string; type: string; val: number; degree: number; x?: number; y?: number; vx?: number; vy?: number };
type FgLink = { source: string | FgNode; target: string | FgNode; label: string };

const RELATION_LABELS: Record<string, string> = {
  related_concepts: "关联概念",
  related_claims: "关联论断",
  related_formulas: "关联公式",
  related_procedures: "关联过程",
  about: "about",
  defines: "defines",
  supports: "supports",
  depends_on: "depends on",
  composed_of: "composed of",
  part_of: "part of",
  precedes: "precedes",
  contrasts_with: "contrasts"
};

const KG_TYPE_LABELS: Record<string, string> = {
  concept: "Concept",
  claim: "Claim",
  formula: "Formula",
  procedure: "Procedure"
};

const KG_TYPE_ORDER = ["concept", "claim", "formula", "procedure"];

const KG_TYPE_STYLE: Record<string, { color: string; border: string; text: string; glyph: string }> = {
  concept: { color: "#2f80ed", border: "#1555a8", text: "C", glyph: "circle" },
  claim: { color: "#16a085", border: "#0f6f5f", text: "CL", glyph: "triangle" },
  formula: { color: "#a855f7", border: "#6d28d9", text: "F", glyph: "diamond" },
  procedure: { color: "#f59e0b", border: "#b45309", text: "P", glyph: "square" }
};

type StudioOutput = {
  title: string;
  sections: Array<[string, string[]]>;
};

type InfoModal = {
  title: string;
  message: string;
  sections?: Array<[string, string[]]>;
  actions: Array<{
    label: string;
    primary?: boolean;
    danger?: boolean;
    action: () => void;
  }>;
};

type NotebookMenuPosition = {
  top: number;
  left: number;
};

// Domain-agnostic fallback prompts. Used when a notebook has no expected
// questions of its own; phrased around the KG knowledge types.
const GENERIC_PROMPTS: Array<[string, string]> = [
  ["基于来源回答问题", "请基于当前来源回答我的问题，并给出可追溯的引用。"],
  ["解释核心概念", "请解释来源中的核心概念，并说明它们之间的关系。"],
  ["列举关键论断", "请列举来源中的关键论断，并给出支撑证据。"],
  ["说明主要过程", "请说明来源中描述的主要过程或步骤。"]
];

function chipLabel(question: string): string {
  const text = question.trim();
  return text.length > 16 ? `${text.slice(0, 16)}…` : text;
}

type WelcomeCopy = {
  title: string;
  description: string;
  prompts: Array<[string, string]>;
};

function sourceTopicCandidates(notebook: NotebookSummary | null, sources: SourceSummary[]): string[] {
  const stop = new Set([
    "source", "untitled", "notebook", "markdown", "paper", "abstract", "section",
    "figure", "table", "results", "method", "methods", "model", "models",
    "current", "evidence", "engineering", "semiconductor", "optional",
    "debug", "viewer", "only", "verbatim", "slice", "original",
    "lines", "authoritative", "gold", "coordinates", "live", "yaml", "atom",
    "atoms", "span", "file", "mineru", "parsed", "text", "element", "elements",
    "under", "each", "may", "drift",
  ]);
  const counts = new Map<string, { label: string; count: number }>();
  const add = (text: string, weight: number) => {
    const normalized = text
      .replace(/\.(pdf|md|markdown|docx|pptx|csv|xlsx)\b/gi, " ")
      .replace(/[_/\\.:-]+/g, " ");
    for (const match of normalized.matchAll(/\b[A-Za-z][A-Za-z0-9+-]{2,}\b/g)) {
      const raw = match[0];
      const key = raw.toLowerCase();
      if (stop.has(key) || /^\d+$/.test(key)) continue;
      const label = raw === raw.toUpperCase() ? raw : raw.charAt(0).toUpperCase() + raw.slice(1);
      const previous = counts.get(key);
      counts.set(key, { label: previous?.label ?? label, count: (previous?.count ?? 0) + weight });
    }
  };
  for (const source of sources) {
    add(source.title || source.file_name || "", 4);
    add(source.file_name || "", 4);
    add(source.summary || "", 2);
  }
  if (counts.size === 0) {
    add(notebook?.primary_domain ?? "", 1);
    add(notebook?.name ?? "", 0.5);
  }
  return [...counts.values()]
    .sort((a, b) => b.count - a.count || b.label.length - a.label.length)
    .slice(0, 4)
    .map((item) => item.label);
}

function sourceTopicLabel(notebook: NotebookSummary | null, sources: SourceSummary[]): string {
  const [topic] = sourceTopicCandidates(notebook, sources);
  if (topic) return topic;
  const domain = notebook?.primary_domain?.trim();
  if (domain) return domain;
  const firstSource = sources.find((source) => compactSourceTitle(source).toLowerCase() !== "source");
  if (firstSource) return compactSourceTitle(firstSource);
  return "当前来源";
}

function sourceAwarePrompts(notebook: NotebookSummary | null, sources: SourceSummary[]): Array<[string, string]> {
  const topic = sourceTopicLabel(notebook, sources);
  const related = sourceTopicCandidates(notebook, sources).filter((item) => item !== topic).slice(0, 2);
  const compare = related.length > 0
    ? `请比较 ${topic} 与 ${related.join("、")} 的关系，并给出证据。`
    : `请解释 ${topic} 的关键概念，并说明它们之间的关系。`;
  return [
    [`解释 ${chipLabel(topic)}`, `请基于当前来源解释 ${topic} 是什么，并给出可追溯引用。`],
    ["核心论断", `请列出来源中关于 ${topic} 的核心论断，并说明每条论断的证据。`],
    ["表格总结", `请用 Markdown 表格总结 ${topic} 的结构、作用、限制和证据。`],
    ["关系与过程", compare],
  ];
}

// Quick-prompt chips for a notebook: prefer its own expected questions
// (set at creation from the template/user input), else derive from sources.
function promptChipsFor(notebook: NotebookSummary | null, sources: SourceSummary[] = []): Array<[string, string]> {
  const expected = (notebook?.expected_questions ?? []).map((q) => q.trim()).filter(Boolean);
  if (expected.length > 0) {
    return expected.slice(0, 4).map((q) => [chipLabel(q), q] as [string, string]);
  }
  if (sources.length > 0) return sourceAwarePrompts(notebook, sources);
  return GENERIC_PROMPTS;
}

function welcomeCopyFor(notebook: NotebookSummary | null, sources: SourceSummary[]): WelcomeCopy {
  if (sources.length === 0) {
    return {
      title: "导入来源后开始提问",
      description: "添加 PDF、Markdown、DOCX 或 PPTX 后，这里会根据来源内容生成可追溯的问题建议。",
      prompts: promptChipsFor(notebook, sources),
    };
  }
  const topic = sourceTopicLabel(notebook, sources);
  return {
    title: `围绕 ${topic} 提问`,
    description: `已导入 ${sources.length} 个来源。可以围绕 ${topic} 的概念、论断、公式和过程提问，回答会优先绑定出处。`,
    prompts: promptChipsFor(notebook, sources),
  };
}

// Placeholder for the Ask box: a real expected question if the notebook has
// one, else a domain-aware hint, else a neutral prompt.
function askPlaceholder(notebook: NotebookSummary | null): string {
  const expected = (notebook?.expected_questions ?? []).map((q) => q.trim()).find(Boolean);
  if (expected) return expected;
  const domain = notebook?.primary_domain?.trim();
  return domain ? `基于来源提问，例如：${domain} 场景下需要注意什么？` : "基于已导入的来源提问…";
}


async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const method = (options.method || "GET").toUpperCase();
  const started = performance.now();
  const response = await fetch(`${API_BASE}${path}`, {
    headers: options.body instanceof FormData ? options.headers : { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options
  });
  const elapsed = Math.round(performance.now() - started);
  const requestId = response.headers.get("X-Request-Id") || "";
  // Browser-side trace mirroring the backend request log (DevTools console).
  console.debug(`[api] ${method} ${path} -> ${response.status} ${elapsed}ms${requestId ? ` (${requestId})` : ""}`);
  if (!response.ok) {
    // Surface the backend's error detail instead of an opaque status line.
    let detail = "";
    try {
      const body = await response.clone().json();
      detail = (body && (body.detail || body.message)) || "";
    } catch {
      detail = (await response.text().catch(() => "")) || "";
    }
    const suffix = detail ? ` - ${typeof detail === "string" ? detail : JSON.stringify(detail)}` : "";
    throw new Error(`${response.status} ${response.statusText}${suffix}${requestId ? ` [${requestId}]` : ""}`);
  }
  if (response.status === 204) {
    return null as T;
  }
  return response.json();
}

async function readAskStream<TResponse>(
  path: string,
  payload: unknown,
  onProgress: (step: ReasoningTraceStep) => void | Promise<void>,
): Promise<TResponse> {
  const started = performance.now();
  const response = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const elapsed = Math.round(performance.now() - started);
  const requestId = response.headers.get("X-Request-Id") || "";
  console.debug(`[api] POST ${path} -> ${response.status} ${elapsed}ms${requestId ? ` (${requestId})` : ""}`);
  if (!response.ok) {
    let detail = "";
    try {
      const body = await response.clone().json();
      detail = (body && (body.detail || body.message)) || "";
    } catch {
      detail = (await response.text().catch(() => "")) || "";
    }
    const suffix = detail ? ` - ${typeof detail === "string" ? detail : JSON.stringify(detail)}` : "";
    throw new Error(`${response.status} ${response.statusText}${suffix}${requestId ? ` [${requestId}]` : ""}`);
  }
  if (!response.body) {
    throw new Error("Streaming response body is unavailable");
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let finalResponse: TResponse | null = null;

  const yieldToPaint = () => new Promise<void>((resolve) => {
    if (typeof window === "undefined" || typeof window.requestAnimationFrame !== "function") {
      resolve();
      return;
    }
    window.requestAnimationFrame(() => resolve());
  });

  const consumeLine = async (line: string) => {
    const event = JSON.parse(line) as AskStreamEvent<TResponse>;
    if (event.event === "progress") {
      await onProgress(event.step);
      await yieldToPaint();
    } else if (event.event === "final") {
      finalResponse = event.response;
    } else if (event.event === "error") {
      throw new Error(event.error);
    }
  };

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const parsed = takeNdjsonLines(buffer);
    buffer = parsed.remainder;
    for (const line of parsed.lines) {
      await consumeLine(line);
    }
  }
  buffer += decoder.decode();
  if (buffer.trim()) {
    await consumeLine(buffer.trim());
  }
  if (!finalResponse) {
    throw new Error("Streaming response ended without a final answer");
  }
  return finalResponse;
}

const rebuildUnifiedKg = (nb: string) => api<{ clusters: number }>(`/notebooks/${nb}/unified-kg/rebuild`, { method: "POST" });

function formatRelativeTime(iso: string): string {
  const then = new Date(iso).getTime();
  if (!Number.isFinite(then)) return "";
  const diffSec = Math.round((Date.now() - then) / 1000);
  if (diffSec < 60) return "刚刚";
  if (diffSec < 3600) return `${Math.floor(diffSec / 60)} 分钟前`;
  if (diffSec < 86400) return `${Math.floor(diffSec / 3600)} 小时前`;
  if (diffSec < 86400 * 30) return `${Math.floor(diffSec / 86400)} 天前`;
  return new Date(then).toLocaleDateString();
}
const fetchUnifiedGraph = (nb: string) => api<UnifiedGraphResp>(`/notebooks/${nb}/unified-kg?level=object`);
const fetchConceptDetail = (nb: string, cid: string) => api<ConceptDetailResp>(`/notebooks/${nb}/concepts/${encodeURIComponent(cid)}/detail`);
const fetchNodeContext = (nb: string, oid: string) => api<NodeContext>(`/notebooks/${nb}/objects/${encodeURIComponent(oid)}/context`);
const fetchPendingMerges = (nb: string) => api<PendingMerge[]>(`/notebooks/${nb}/unified-kg/pending-merges`);
const fetchUnifiedKgStatus = (nb: string) => api<UnifiedKgStatus>(`/notebooks/${nb}/unified-kg/status`);
const confirmMergeApi = (nb: string, cid: string) => api<{ ok: boolean }>(`/notebooks/${nb}/unified-kg/merges/${encodeURIComponent(cid)}/confirm`, { method: "POST" });
const rejectMergeApi = (nb: string, cid: string) => api<{ ok: boolean }>(`/notebooks/${nb}/unified-kg/merges/${encodeURIComponent(cid)}/reject`, { method: "POST" });
const reviewPendingMergesApi = (nb: string) =>
  api<MergeReviewSummary>(`/notebooks/${nb}/unified-kg/merges/review`, {
    method: "POST",
    body: JSON.stringify({ limit: 50, auto_confirm_threshold: 0.95 }),
  });

function formatFileSize(size: number): string {
  if (!size) return "metadata only";
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${Math.round(size / 1024)} KB`;
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
}

function compactSourceTitle(source: SourceSummary): string {
  const rawTitle = (source.title || source.file_name || "Untitled source").trim();
  const withoutExtension = rawTitle.replace(/\.(pdf|md|markdown|docx|pptx|csv|xlsx|xlsm)$/i, "");
  return withoutExtension || rawTitle;
}

function sourceTypeLabel(source: SourceSummary): string {
  return source.type || source.file_name.split(".").pop()?.toLowerCase() || "source";
}

function sourceElementDomId(elementId: string): string {
  return `source-element-${elementId.replace(/[^a-zA-Z0-9_-]/g, "_")}`;
}

function kgTypeLabel(type: string): string {
  return KG_TYPE_LABELS[type] ?? type.replace(/(^|_)([a-z])/g, (_match, separator, char) => `${separator ? " " : ""}${char.toUpperCase()}`);
}

function kgNodeName(node: UnifiedConceptNode): string {
  const name = typeof node.payload.name === "string" ? node.payload.name.trim() : "";
  return name || node.id.replace(/^K-/, "");
}

function truncateKgLabel(label: string, max = 34): string {
  return label.length > max ? `${label.slice(0, max - 1)}…` : label;
}

function kgPayloadValue(value: unknown): string {
  if (value == null || value === "") return "";
  if (Array.isArray(value)) return value.map(kgPayloadValue).filter(Boolean).join(", ");
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function KgTypeMark({ type }: { type: string }) {
  const style = KG_TYPE_STYLE[type] ?? { color: "#64748b", border: "#334155", text: type.slice(0, 2).toUpperCase(), glyph: "circle" };
  return (
    <span
      className={`kg-shape-mark ${style.glyph}`}
      style={{ background: style.color, borderColor: style.border }}
      aria-hidden="true"
    >
      {style.glyph === "circle" ? style.text : ""}
    </span>
  );
}

function kgTypeBandForce(width: number, height: number, activeTypes: string[]) {
  let nodes: FgNode[] = [];
  const targets: Record<string, [number, number]> = {
    concept: [width * 0.34, height * 0.38],
    claim: [width * 0.66, height * 0.36],
    formula: [width * 0.34, height * 0.72],
    procedure: [width * 0.66, height * 0.72]
  };
  const force = (alpha: number) => {
    const useTypeBands = activeTypes.length !== 1;
    nodes.forEach((node) => {
      const target = useTypeBands ? (targets[node.type] ?? [width / 2, height / 2]) : [width / 2, height / 2];
      node.vx = (node.vx ?? 0) + (target[0] - (node.x ?? 0)) * 0.035 * alpha;
      node.vy = (node.vy ?? 0) + (target[1] - (node.y ?? 0)) * 0.035 * alpha;
    });
  };
  force.initialize = (forceNodes: FgNode[]) => {
    nodes = forceNodes;
  };
  return force;
}

function drawKgNode(node: FgNode, ctx: CanvasRenderingContext2D, globalScale: number, selectedId: string | null, denseView: boolean) {
  const x = node.x ?? 0;
  const y = node.y ?? 0;
  const style = KG_TYPE_STYLE[node.type] ?? { color: "#64748b", border: "#334155", text: node.type.slice(0, 2).toUpperCase(), glyph: "circle" };
  const selected = node.id === selectedId;
  const radius = 10 + Math.min(14, Math.sqrt(Math.max(1, node.val)) * 3.2) + (selected ? 2 : 0);

  ctx.save();
  ctx.beginPath();
  if (style.glyph === "diamond") {
    ctx.moveTo(x, y - radius);
    ctx.lineTo(x + radius, y);
    ctx.lineTo(x, y + radius);
    ctx.lineTo(x - radius, y);
    ctx.closePath();
  } else if (style.glyph === "square") {
    ctx.rect(x - radius, y - radius, radius * 2, radius * 2);
  } else if (style.glyph === "triangle") {
    ctx.moveTo(x, y - radius);
    ctx.lineTo(x + radius * 1.08, y + radius * 0.9);
    ctx.lineTo(x - radius * 1.08, y + radius * 0.9);
    ctx.closePath();
  } else {
    ctx.arc(x, y, radius, 0, Math.PI * 2);
  }
  ctx.fillStyle = style.color;
  ctx.fill();
  ctx.lineWidth = (selected ? 3 : 1.5) / globalScale;
  ctx.strokeStyle = selected ? "#111827" : style.border;
  ctx.stroke();

  const innerFont = Math.max(7, 9 / globalScale);
  ctx.fillStyle = "#ffffff";
  ctx.font = `700 ${innerFont}px Inter, ui-sans-serif, system-ui, sans-serif`;
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(style.text, x, y + (style.glyph === "triangle" ? radius * 0.12 : 0));

  const shouldDrawLabel = selected || !denseView || node.degree >= 2;
  if (!shouldDrawLabel) {
    ctx.restore();
    return;
  }

  const label = truncateKgLabel(node.name, denseView ? 18 : (node.type === "claim" ? 30 : 24));
  const labelFont = Math.min(14, Math.max(9, 12 / globalScale));
  ctx.font = `650 ${labelFont}px Inter, ui-sans-serif, system-ui, sans-serif`;
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  const labelX = x;
  const labelY = y + radius + labelFont * 0.95;
  const metrics = ctx.measureText(label);
  ctx.fillStyle = selected ? "rgba(255,255,255,0.96)" : "rgba(255,255,255,0.82)";
  ctx.fillRect(labelX - metrics.width / 2 - 3 / globalScale, labelY - labelFont * 0.75, metrics.width + 6 / globalScale, labelFont * 1.5);
  ctx.fillStyle = selected ? "#111827" : "#27303f";
  ctx.fillText(label, labelX, labelY);
  ctx.restore();
}

function paintKgPointerArea(node: FgNode, color: string, ctx: CanvasRenderingContext2D) {
  const x = node.x ?? 0;
  const y = node.y ?? 0;
  const radius = 18 + Math.min(16, Math.sqrt(Math.max(1, node.val)) * 3.2);
  ctx.fillStyle = color;
  ctx.beginPath();
  ctx.arc(x, y, radius, 0, Math.PI * 2);
  ctx.fill();
}

function drawKgLinkLabel(link: FgLink, ctx: CanvasRenderingContext2D, globalScale: number, denseView: boolean) {
  if (denseView) return;
  const source = typeof link.source === "object" ? link.source : null;
  const target = typeof link.target === "object" ? link.target : null;
  if (!source || !target || source.x == null || source.y == null || target.x == null || target.y == null) return;
  const x = (source.x + target.x) / 2;
  const y = (source.y + target.y) / 2;
  const label = truncateKgLabel(RELATION_LABELS[link.label] ?? link.label, 18);
  const fontSize = Math.min(12, Math.max(8, 10 / globalScale));

  ctx.save();
  ctx.font = `600 ${fontSize}px Inter, ui-sans-serif, system-ui, sans-serif`;
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  const width = ctx.measureText(label).width + 8 / globalScale;
  ctx.fillStyle = "rgba(255,255,255,0.88)";
  ctx.fillRect(x - width / 2, y - fontSize * 0.72, width, fontSize * 1.45);
  ctx.fillStyle = "#475569";
  ctx.fillText(label, x, y);
  ctx.restore();
}

function cardTone(index: number): string {
  return ["tone-green", "tone-cream", "tone-lavender", "tone-rose", "tone-cream", "tone-blue"][index % 6];
}

function cardIcon(index: number, notebook: NotebookSummary): string {
  if (notebook.primary_domain.toLowerCase().includes("esd")) return "▣";
  return ["◇", "📒", "📈", "▤", "▧"][index % 5];
}

export default function Home() {
  const [health, setHealth] = useState<Health | null>(null);
  const [notebooks, setNotebooks] = useState<NotebookSummary[]>([]);
  const [searchHits, setSearchHits] = useState<Record<string, SearchHit[]>>({});
  const [currentNotebookId, setCurrentNotebookId] = useState<string | null>(null);
  const [currentNotebook, setCurrentNotebook] = useState<NotebookSummary | null>(null);
  const [sources, setSources] = useState<SourceSummary[]>([]);
  const [question, setQuestion] = useState("");
  const [turns, setTurns] = useState<ChatTurn[]>([]);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [sessions, setSessions] = useState<ConversationSummary[]>([]);
  const [asking, setAsking] = useState(false);
  const [pendingQuestion, setPendingQuestion] = useState("");
  const [pendingReasoning, setPendingReasoning] = useState(false);
  const [pendingTrace, setPendingTrace] = useState<ReasoningTraceStep[]>([]);
  const [filter, setFilter] = useState("mine");
  const [viewMode, setViewMode] = useState("grid");
  const [sortMode, setSortMode] = useState("recent");
  const [searchQuery, setSearchQuery] = useState("");
  const [sortOpen, setSortOpen] = useState(false);
  const [menuNotebookId, setMenuNotebookId] = useState<string | null>(null);
  const [menuPosition, setMenuPosition] = useState<NotebookMenuPosition | null>(null);
  const [editingNotebook, setEditingNotebook] = useState<NotebookSummary | null>(null);
  const [deleteNotebook, setDeleteNotebook] = useState<NotebookSummary | null>(null);
  const [sourceModalOpen, setSourceModalOpen] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [createName, setCreateName] = useState("");
  const [createDesc, setCreateDesc] = useState("");
  const [docTypeOptions, setDocTypeOptions] = useState<Array<{ id: string; label: string }>>([]);
  const [stagedFiles, setStagedFiles] = useState<File[]>([]);
  const [stagedDocTypes, setStagedDocTypes] = useState<string[]>([]);
  const [sourceDetail, setSourceDetail] = useState<SourceSummary | null>(null);
  const [sourceElements, setSourceElements] = useState<SourceElement[]>([]);
  const [infoModal, setInfoModal] = useState<InfoModal | null>(null);
  const [toast, setToast] = useState("");
  const [statusText, setStatusText] = useState("connecting");
  const [titleDraft, setTitleDraft] = useState("");
  const [titleSaveInFlight, setTitleSaveInFlight] = useState(false);
  const [feedbackSent, setFeedbackSent] = useState<Record<string, string>>({});
  const [sessionPanelOpen, setSessionPanelOpen] = useState(false);
  const [renamingSessionId, setRenamingSessionId] = useState<string | null>(null);
  const [sessionTitleDraft, setSessionTitleDraft] = useState("");
  const [articleModalOpen, setArticleModalOpen] = useState(false);
  const [articles, setArticles] = useState<ArticleSummary[]>([]);
  const [selectedArticleId, setSelectedArticleId] = useState("");
  const [chatMode, setChatMode] = useState<ChatMode>("ask");
  const [reasoningMode, setReasoningMode] = useState(false);
  const [knowledgeKind, setKnowledgeKind] = useState<KnowledgeKind>("concept");
  const [knowledge, setKnowledge] = useState<Record<string, KnowledgeItem[] | null>>(EMPTY_KNOWLEDGE);
  const [knowledgeTypes, setKnowledgeTypes] = useState<KnowledgeTypeCount[]>([]);
  const [knowledgeStatusFilter, setKnowledgeStatusFilter] = useState("all");
  const [duplicates, setDuplicates] = useState<DuplicateGroup[] | null>(null);
  const [derivedRules, setDerivedRules] = useState<DerivedRuleCandidate[] | null>(null);
  const [derivedOpen, setDerivedOpen] = useState(false);
  // Promotion queue modal (Track F governance)
  const [promoQueue, setPromoQueue] = useState<PromotionCandidate[] | null>(null);
  const [promoOpen, setPromoOpen] = useState(false);
  const [promoBusy, setPromoBusy] = useState(false);
  const [analytics, setAnalytics] = useState<NotebookAnalytics | null>(null);
  const [schemaModalOpen, setSchemaModalOpen] = useState(false);
  const [schemas, setSchemas] = useState<ObjectSchema[] | null>(null);
  const [schemaBusy, setSchemaBusy] = useState(false);
  const [graph, setGraph] = useState<KnowledgeGraph | null>(null);
  const [graphOpen, setGraphOpen] = useState(false);
  const [kgViewOpen, setKgViewOpen] = useState(false);
  const [uGraph, setUGraph] = useState<UnifiedGraphResp | null>(null);
  const [pendingMerges, setPendingMerges] = useState<PendingMerge[]>([]);
  const [unifiedKgStatus, setUnifiedKgStatus] = useState<UnifiedKgStatus | null>(null);
  const [kgRefreshBusy, setKgRefreshBusy] = useState(false);
  const [kgReviewBusy, setKgReviewBusy] = useState(false);
  const [selectedKgNodeId, setSelectedKgNodeId] = useState<string | null>(null);
  const [pendingKgFocusId, setPendingKgFocusId] = useState<string | null>(null);
  const [conceptDetail, setConceptDetail] = useState<ConceptDetailResp | null>(null);
  const [nodeCtx, setNodeCtx] = useState<NodeContext | null>(null);
  const [kgSearch, setKgSearch] = useState("");
  const [kgSelectedTypes, setKgSelectedTypes] = useState<string[]>([]);
  const [kgSize, setKgSize] = useState({ width: 720, height: 560 });
  const [highlightedElementId, setHighlightedElementId] = useState("");
  const pollCountRef = useRef(0);
  const chatBodyRef = useRef<HTMLDivElement | null>(null);
  const notebookMenuRef = useRef<HTMLDivElement | null>(null);
  const kgCanvasRef = useRef<HTMLDivElement | null>(null);
  const kgDetailRef = useRef<HTMLElement | null>(null);
  const kgGraphRef = useRef<any>(null);

  useEffect(() => {
    loadNotebookCollection().catch(reportError);
  }, []);

  useEffect(() => {
    const element = chatBodyRef.current;
    if (!element) return;
    element.scrollTop = element.scrollHeight;
  }, [turns.length, asking]);

  useEffect(() => {
    if (!kgViewOpen) return;
    const element = kgCanvasRef.current;
    if (!element) return;

    const updateSize = () => {
      const rect = element.getBoundingClientRect();
      setKgSize({
        width: Math.max(320, Math.floor(rect.width)),
        height: Math.max(360, Math.floor(rect.height))
      });
    };

    updateSize();
    const observer = typeof ResizeObserver !== "undefined" ? new ResizeObserver(updateSize) : null;
    observer?.observe(element);
    window.addEventListener("resize", updateSize);
    return () => {
      observer?.disconnect();
      window.removeEventListener("resize", updateSize);
    };
  }, [kgViewOpen]);

  useEffect(() => {
    if (!searchQuery.trim()) {
      setSearchHits({});
      return;
    }
    let cancelled = false;
    const timer = window.setTimeout(() => {
      Promise.all(
        notebooks.map(async (notebook) => {
          const response = await api<{ hits: SearchHit[] }>(`/notebooks/${notebook.id}/search?q=${encodeURIComponent(searchQuery)}`);
          return [notebook.id, response.hits] as const;
        })
      )
        .then((entries) => {
          if (!cancelled) {
            setSearchHits(Object.fromEntries(entries));
          }
        })
        .catch(reportError);
    }, 250);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [notebooks, searchQuery]);

  useEffect(() => {
    if (!toast) return;
    const timer = window.setTimeout(() => setToast(""), 2200);
    return () => window.clearTimeout(timer);
  }, [toast]);

  useEffect(() => {
    if (!sourceDetail || !highlightedElementId) return;
    const timer = window.setTimeout(() => {
      document
        .getElementById(sourceElementDomId(highlightedElementId))
        ?.scrollIntoView({ block: "center" });
    }, 80);
    return () => window.clearTimeout(timer);
  }, [highlightedElementId, sourceDetail, sourceElements]);

  useEffect(() => {
    if (!menuNotebookId) return;

    function closeMenu() {
      setMenuNotebookId(null);
      setMenuPosition(null);
    }

    function handlePointerDown(event: PointerEvent) {
      const target = event.target;
      if (
        target instanceof Node &&
        notebookMenuRef.current?.contains(target)
      ) {
        return;
      }
      closeMenu();
    }

    function handleKeyDown(event: globalThis.KeyboardEvent) {
      if (event.key === "Escape") closeMenu();
    }

    window.addEventListener("pointerdown", handlePointerDown);
    window.addEventListener("keydown", handleKeyDown);
    window.addEventListener("resize", closeMenu);
    window.addEventListener("scroll", closeMenu, true);
    return () => {
      window.removeEventListener("pointerdown", handlePointerDown);
      window.removeEventListener("keydown", handleKeyDown);
      window.removeEventListener("resize", closeMenu);
      window.removeEventListener("scroll", closeMenu, true);
    };
  }, [menuNotebookId]);

  // Poll non-terminal sources so the UI reflects queued→parsing→…→extracted live.
  useEffect(() => {
    if (!currentNotebookId) return;
    const pending = sources.filter(
      (source) => !["extracted", "failed"].includes(source.parse_status)
    );
    if (pending.length === 0) {
      pollCountRef.current = 0;
      return;
    }
    if (pollCountRef.current > 120) {
      setStatusText("处理超时：来源长时间未完成，请查看后端日志 .local/logs/events.jsonl");
      return; // ~3min safety cap
    }
    // Show which stage is pending and how long it has been running so a stuck
    // upload is visible instead of a silent spinner.
    const elapsedSec = Math.round((pollCountRef.current * 1500) / 1000);
    const pendingLabel = pending.map((s) => `${s.file_name || s.title}: ${s.parse_status}`).join("，");
    setStatusText(`处理中（已 ${elapsedSec}s）：${pendingLabel}`);
    let cancelled = false;
    const timer = window.setTimeout(async () => {
      pollCountRef.current += 1;
      try {
        const updated = await Promise.all(
          pending.map((source) => api<SourceSummary>(`/sources/${source.id}`))
        );
        if (cancelled) return;
        const reachedExtracted = updated.some((item) => {
          const previous = pending.find((source) => source.id === item.id);
          return previous && previous.parse_status !== "extracted" && item.parse_status === "extracted";
        });
        const justFailed = updated.find((item) => {
          const previous = pending.find((source) => source.id === item.id);
          return previous && previous.parse_status !== "failed" && item.parse_status === "failed";
        });
        setSources((previous) =>
          previous.map((source) => updated.find((item) => item.id === source.id) ?? source)
        );
        if (justFailed && !cancelled) {
          setStatusText(`来源处理失败：${justFailed.file_name || justFailed.title}${justFailed.error_message ? ` — ${justFailed.error_message}` : ""}`);
        }
        if (reachedExtracted && currentNotebookId) {
          await loadNotebookCollection();
          const refreshed = await api<NotebookSummary>(`/notebooks/${currentNotebookId}`);
          if (!cancelled) setCurrentNotebook(refreshed);
        }
      } catch (error) {
        reportError(error);
      }
    }, 1500);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [currentNotebookId, sources]);

  const visibleNotebooks = useMemo(() => {
    const query = searchQuery.trim();
    const enriched = notebooks
      .map((notebook, index) => ({ notebook, index, hits: searchHits[notebook.id] ?? [] }))
      .filter(({ notebook, hits }) => {
        if (filter === "featured") {
          const featured = Object.values(notebook.counts ?? {}).some((n) => (n ?? 0) > 0);
          if (!featured) return false;
        }
        return !query || hits.length > 0;
      });
    enriched.sort((left, right) => {
      if (sortMode === "name") return left.notebook.name.localeCompare(right.notebook.name, "zh-Hans-CN");
      if (sortMode === "sources") return (right.notebook.counts.sources ?? 0) - (left.notebook.counts.sources ?? 0);
      return left.index - right.index;
    });
    return enriched;
  }, [filter, notebooks, searchHits, searchQuery, sortMode]);

  // Example prompts / placeholders adapt to the open notebook's imported sources,
  // so a new notebook never shows demo examples.
  const welcomeCopy = useMemo(() => welcomeCopyFor(currentNotebook, sources), [currentNotebook, sources]);
  const askHint = useMemo(() => askPlaceholder(currentNotebook), [currentNotebook]);
  const currentSession = useMemo(
    () => sessions.find((session) => session.id === conversationId) ?? null,
    [conversationId, sessions],
  );

  const fgData = useMemo(() => {
    if (!uGraph) return { nodes: [] as FgNode[], links: [] as FgLink[] };
    const deg: Record<string, number> = {};
    uGraph.edges.forEach((e) => { deg[e.source_object_id] = (deg[e.source_object_id] ?? 0) + 1; deg[e.target_object_id] = (deg[e.target_object_id] ?? 0) + 1; });
    const q = kgSearch.trim().toLowerCase();
    const edgeMatchedNodes = new Set<string>();
    if (q) {
      uGraph.edges.forEach((edge) => {
        const label = `${edge.edge_type} ${RELATION_LABELS[edge.edge_type] ?? ""}`.toLowerCase();
        if (label.includes(q)) {
          edgeMatchedNodes.add(edge.source_object_id);
          edgeMatchedNodes.add(edge.target_object_id);
        }
      });
    }
    const nodes: FgNode[] = uGraph.nodes
      .filter((n) => {
        if (kgSelectedTypes.length > 0 && !kgSelectedTypes.includes(n.object_type)) return false;
        if (!q) return true;
        const searchable = `${kgNodeName(n)} ${n.object_type} ${kgPayloadValue(n.payload.section_path)}`.toLowerCase();
        return searchable.includes(q) || edgeMatchedNodes.has(n.id);
      })
      .map((n) => {
        const degree = deg[n.id] ?? 0;
        return { id: n.id, name: kgNodeName(n), type: n.object_type, val: 5 + Math.min(18, degree), degree };
      });
    const keep = new Set(nodes.map((n) => n.id));
    const links: FgLink[] = uGraph.edges
      .filter((e) => keep.has(e.source_object_id) && keep.has(e.target_object_id))
      .map((e) => ({ source: e.source_object_id, target: e.target_object_id, label: e.edge_type }));
    return { nodes, links };
  }, [uGraph, kgSearch, kgSelectedTypes]);

  const kgDenseView = kgSelectedTypes.length === 0 && !kgSearch.trim() && fgData.nodes.length > 36;

  const kgTypeCounts = useMemo(() => {
    if (!uGraph) return [] as Array<{ type: string; label: string; count: number }>;
    const counts = new Map<string, number>();
    uGraph.nodes.forEach((node) => counts.set(node.object_type, (counts.get(node.object_type) ?? 0) + 1));
    return Array.from(counts.entries())
      .map(([type, count]) => ({ type, label: kgTypeLabel(type), count }))
      .sort((left, right) => {
        const leftIndex = KG_TYPE_ORDER.indexOf(left.type);
        const rightIndex = KG_TYPE_ORDER.indexOf(right.type);
        if (leftIndex !== -1 || rightIndex !== -1) {
          return (leftIndex === -1 ? 999 : leftIndex) - (rightIndex === -1 ? 999 : rightIndex);
        }
        return left.label.localeCompare(right.label, "en");
      });
  }, [uGraph]);

  const selectedKgNode = useMemo(() => {
    if (!uGraph || !selectedKgNodeId) return null;
    return uGraph.nodes.find((node) => node.id === selectedKgNodeId) ?? null;
  }, [selectedKgNodeId, uGraph]);

  const selectedKgEdges = useMemo(() => {
    if (!uGraph || !selectedKgNodeId) return [];
    const nodeById = new Map(uGraph.nodes.map((node) => [node.id, node]));
    return uGraph.edges
      .filter((edge) => edge.source_object_id === selectedKgNodeId || edge.target_object_id === selectedKgNodeId)
      .map((edge) => ({
        ...edge,
        sourceName: kgNodeName(nodeById.get(edge.source_object_id) ?? { id: edge.source_object_id, object_type: "", payload: { name: edge.source_object_id } }),
        sourceType: nodeById.get(edge.source_object_id)?.object_type ?? "",
        targetName: kgNodeName(nodeById.get(edge.target_object_id) ?? { id: edge.target_object_id, object_type: "", payload: { name: edge.target_object_id } }),
        targetType: nodeById.get(edge.target_object_id)?.object_type ?? ""
      }));
  }, [selectedKgNodeId, uGraph]);

  const kgNodeGroups = useMemo(() => {
    const byType = new Map<string, FgNode[]>();
    fgData.nodes.forEach((node) => {
      byType.set(node.type, [...(byType.get(node.type) ?? []), node]);
    });
    return Array.from(byType.entries())
      .map(([type, nodes]) => ({
        type,
        label: kgTypeLabel(type),
        nodes: nodes.sort((left, right) => left.name.localeCompare(right.name, "zh-Hans-CN"))
      }))
      .sort((left, right) => {
        const leftIndex = KG_TYPE_ORDER.indexOf(left.type);
        const rightIndex = KG_TYPE_ORDER.indexOf(right.type);
        if (leftIndex !== -1 || rightIndex !== -1) {
          return (leftIndex === -1 ? 999 : leftIndex) - (rightIndex === -1 ? 999 : rightIndex);
        }
        return left.label.localeCompare(right.label, "en");
      });
  }, [fgData.nodes]);

  const relatedNodeGroups = useMemo(() => {
    if (!conceptDetail) return [] as Array<{ type: string; label: string; nodes: KgObject[] }>;
    const byType = new Map<string, KgObject[]>();
    conceptDetail.attached.forEach((node) => {
      byType.set(node.object_type, [...(byType.get(node.object_type) ?? []), node]);
    });
    return Array.from(byType.entries())
      .map(([type, nodes]) => ({
        type,
        label: kgTypeLabel(type),
        nodes: nodes.sort((left, right) => String(left.payload.name ?? "").localeCompare(String(right.payload.name ?? ""), "zh-Hans-CN"))
      }))
      .sort((left, right) => {
        const leftIndex = KG_TYPE_ORDER.indexOf(left.type);
        const rightIndex = KG_TYPE_ORDER.indexOf(right.type);
        if (leftIndex !== -1 || rightIndex !== -1) {
          return (leftIndex === -1 ? 999 : leftIndex) - (rightIndex === -1 ? 999 : rightIndex);
        }
        return left.label.localeCompare(right.label, "en");
      });
  }, [conceptDetail]);

  function toggleKgType(type: string) {
    const allTypes = kgTypeCounts.map((item) => item.type);
    if (allTypes.length === 0) return;
    setKgSelectedTypes((previous) => {
      const current = previous;
      const next = current.includes(type) ? current.filter((item) => item !== type) : [...current, type];
      if (next.length === allTypes.length) return [];
      return next;
    });
  }

  function fitKgGraphView(duration = 450) {
    const graph = kgGraphRef.current;
    if (!graph) return;
    const nodes = (graph.graphData?.().nodes ?? fgData.nodes) as FgNode[];
    if (nodes.length === 0) return;

    if (nodes.length <= 4) {
      const positioned = nodes.filter((node) => node.x != null && node.y != null);
      if (positioned.length > 0) {
        const centerX = positioned.reduce((sum, node) => sum + (node.x ?? 0), 0) / positioned.length;
        const centerY = positioned.reduce((sum, node) => sum + (node.y ?? 0), 0) / positioned.length;
        graph.centerAt?.(centerX, centerY, duration);
      }
      const sparseZoom = nodes.length === 1 ? 1.12 : nodes.length === 2 ? 1.25 : 1.38;
      graph.zoom?.(sparseZoom, duration);
      return;
    }

    graph.zoomToFit?.(duration, kgDenseView ? 96 : 72);
  }

  useEffect(() => {
    if (!kgViewOpen || fgData.nodes.length === 0) return;
    const graph = kgGraphRef.current;
    graph?.d3Force?.("link")?.distance?.(kgDenseView ? 128 : 96);
    graph?.d3Force?.("charge")?.strength?.(kgDenseView ? -310 : -190);
    graph?.d3Force?.("typeBand", kgTypeBandForce(kgSize.width, kgSize.height, kgSelectedTypes));
    graph?.d3ReheatSimulation?.();
    const timer = window.setTimeout(() => {
      fitKgGraphView(450);
    }, 700);
    return () => window.clearTimeout(timer);
  }, [fgData.nodes.length, fgData.links.length, kgDenseView, kgSelectedTypes, kgSize.height, kgSize.width, kgViewOpen]);

  useEffect(() => {
    if (!kgViewOpen || !pendingKgFocusId || !fgData.nodes.some((node) => node.id === pendingKgFocusId)) return;
    const nodeId = pendingKgFocusId;
    setPendingKgFocusId(null);
    selectKgNode(nodeId).catch(reportError);
    window.setTimeout(() => focusKgGraphNode(nodeId), 900);
  }, [fgData.nodes, kgViewOpen, pendingKgFocusId]);

  async function loadNotebookCollection() {
    const healthResponse = await api<Health>("/health");
    const notebookResponse = await api<NotebookSummary[]>("/notebooks");
    setHealth(healthResponse);
    setStatusText(`API ${healthResponse.status}; LLM configured: ${healthResponse.llm_configured}`);
    setNotebooks(notebookResponse);
    if (docTypeOptions.length === 0) {
      api<Array<{ id: string; label: string }>>("/doc-types")
        .then(setDocTypeOptions)
        .catch(() => undefined);
    }
  }

  async function openCreate() {
    const notebook = await api<NotebookSummary>("/notebooks", {
      method: "POST",
      body: JSON.stringify({ name: "Untitled notebook", purpose: "" })
    });
    await loadNotebookCollection();
    await openNotebook(notebook.id);
    setStagedFiles([]);
    setStagedDocTypes([]);
    setSourceModalOpen(true);
  }

  async function submitCreate() {
    const notebook = await api<NotebookSummary>("/notebooks", {
      method: "POST",
      body: JSON.stringify({ name: createName.trim() || "Untitled notebook", purpose: createDesc.trim() })
    });
    setCreateOpen(false);
    await loadNotebookCollection();
    await openNotebook(notebook.id);
    setStagedFiles([]);
    setStagedDocTypes([]);
    setSourceModalOpen(true);
  }

  async function openNotebook(notebookId: string) {
    const [notebook, notebookSources, notebookArticles] = await Promise.all([
      api<NotebookSummary>(`/notebooks/${notebookId}`),
      api<SourceSummary[]>(`/notebooks/${notebookId}/sources`),
      api<ArticleSummary[]>(`/notebooks/${notebookId}/articles`)
    ]);
    setCurrentNotebookId(notebookId);
    setCurrentNotebook(notebook);
    setTitleDraft(notebook.name);
    setSources(notebookSources);
    setArticles(notebookArticles);
    setSelectedArticleId(notebookArticles[0]?.id ?? "");
    setTurns([]);
    setConversationId(null);
    setAsking(false);
    setPendingQuestion("");
    setFeedbackSent({});
    setSessionPanelOpen(false);
    setRenamingSessionId(null);
    setSessionTitleDraft("");
    setChatMode("ask");
    setKnowledge(EMPTY_KNOWLEDGE);
    setKnowledgeKind("concept");
    setKnowledgeStatusFilter("all");
    setDuplicates(null);
    setSessions([]);
    pollCountRef.current = 0;
    await loadSessions(notebookId);
    window.history.replaceState(null, "", `#notebook=${encodeURIComponent(notebookId)}`);
    window.scrollTo(0, 0);
  }

  async function loadArticles(notebookId: string) {
    const list = await api<ArticleSummary[]>(`/notebooks/${notebookId}/articles`);
    setArticles(list);
    setSelectedArticleId((previous) =>
      list.some((article) => article.id === previous) ? previous : list[0]?.id ?? ""
    );
    return list;
  }

  async function createArticle(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!currentNotebookId) return;
    const formData = new FormData(event.currentTarget);
    const title = String(formData.get("title") || "").trim();
    if (!title) return;
    const created = await api<ArticleSummary>(`/notebooks/${currentNotebookId}/articles`, {
      method: "POST",
      body: JSON.stringify({
        title,
        abstract: String(formData.get("abstract") || ""),
        source_id: String(formData.get("source_id") || "")
      })
    });
    setArticles((previous) => [...previous, created]);
    setSelectedArticleId(created.id);
    setArticleModalOpen(false);
    await loadNotebookCollection();
    setToast("文章已添加，可在 Studio 生成研究简报");
  }

  async function submitFeedback(answerId: string, rating: "useful" | "not_useful", comment: string) {
    if (!answerId) return;
    await api(`/answers/${answerId}/feedback`, {
      method: "POST",
      body: JSON.stringify({ rating, comment })
    });
    setFeedbackSent((prev) => ({ ...prev, [answerId]: rating }));
    setToast("感谢反馈");
  }

  function showCollection() {
    setCurrentNotebookId(null);
    setCurrentNotebook(null);
    setSources([]);
    setArticles([]);
    setTitleDraft("");
    setTurns([]);
    setConversationId(null);
    setSessions([]);
    setAsking(false);
    setPendingQuestion("");
    setPendingReasoning(false);
    setPendingTrace([]);
    window.history.replaceState(null, "", window.location.pathname + window.location.search);
    window.scrollTo(0, 0);
  }

  async function saveInlineNotebookName() {
    if (!currentNotebook || titleSaveInFlight) return;
    const nextName = titleDraft.trim() || "Untitled notebook";
    setTitleDraft(nextName);
    if (nextName === currentNotebook.name) return;
    setTitleSaveInFlight(true);
    try {
      const updated = await api<NotebookSummary>(`/notebooks/${currentNotebook.id}`, {
        method: "PATCH",
        body: JSON.stringify({ name: nextName })
      });
      setCurrentNotebook(updated);
      setTitleDraft(updated.name);
      await loadNotebookCollection();
      setToast("Notebook 名称已更新");
    } catch (error) {
      setTitleDraft(currentNotebook.name);
      reportError(error);
    } finally {
      setTitleSaveInFlight(false);
    }
  }

  async function saveNotebookEdit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!editingNotebook) return;
    const formData = new FormData(event.currentTarget);
    const splitLines = (value: string) =>
      value.split(/[\n;,，；]/).map((s) => s.trim()).filter(Boolean);
    const updated = await api<NotebookSummary>(`/notebooks/${editingNotebook.id}`, {
      method: "PATCH",
      body: JSON.stringify({
        name: formData.get("name"),
        purpose: formData.get("purpose"),
        primary_domain: formData.get("primary_domain"),
        target_users: String(formData.get("target_users") || ""),
        access_scope: String(formData.get("access_scope") || ""),
        expected_questions: splitLines(String(formData.get("expected_questions") || "")),
        source_types: splitLines(String(formData.get("source_types") || "")),
        taxonomy: splitLines(String(formData.get("taxonomy") || ""))
      })
    });
    setEditingNotebook(null);
    if (currentNotebookId === updated.id) {
      setCurrentNotebook(updated);
      setTitleDraft(updated.name);
    }
    await loadNotebookCollection();
    setToast("Notebook 信息已更新");
  }

  async function confirmDeleteNotebook() {
    if (!deleteNotebook) return;
    await api<null>(`/notebooks/${deleteNotebook.id}`, { method: "DELETE" });
    if (currentNotebookId === deleteNotebook.id) {
      showCollection();
    }
    setDeleteNotebook(null);
    await loadNotebookCollection();
    setToast("Notebook 已删除");
  }

  // Stage selected files so the user can pick a document type per file before
  // uploading (auto-detect by default).
  function stageFiles(event: ChangeEvent<HTMLInputElement>) {
    const picked = Array.from(event.target.files || []).filter((file) =>
      /\.(pdf|md|markdown|docx|pptx|csv|xlsx|xlsm)$/i.test(file.name)
    );
    event.target.value = "";
    if (picked.length === 0) {
      setStatusText("Select PDF, Markdown, DOCX, PPTX, CSV or Excel files");
      return;
    }
    setStagedFiles(picked);
    setStagedDocTypes(picked.map(() => ""));
    setSourceModalOpen(true);
  }

  function setStagedDocType(index: number, value: string) {
    setStagedDocTypes((prev) => prev.map((dt, i) => (i === index ? value : dt)));
  }

  function setAllStagedDocTypes(value: string) {
    setStagedDocTypes((prev) => prev.map(() => value));
  }

  async function confirmUpload() {
    if (!currentNotebookId || stagedFiles.length === 0) return;
    const formData = new FormData();
    stagedFiles.forEach((file) => formData.append("files", file));
    stagedDocTypes.forEach((dt) => formData.append("doc_types", dt));
    const uploaded = await api<SourceSummary[]>(`/notebooks/${currentNotebookId}/sources`, {
      method: "POST",
      body: formData
    });
    setSources((previous) => [...previous.filter((source) => !uploaded.some((item) => item.id === source.id)), ...uploaded]);
    await loadNotebookCollection();
    setStagedFiles([]);
    setStagedDocTypes([]);
    setSourceModalOpen(false);
    setToast(`已上传 ${uploaded.length} 个来源`);
  }

  async function openSourceDetail(source: SourceSummary) {
    const [detail, elements] = await Promise.all([
      api<SourceSummary>(`/sources/${source.id}`),
      api<SourceElement[]>(`/sources/${source.id}/elements`)
    ]);
    setSourceDetail(detail);
    setSourceElements(elements);
  }

  async function reparseSource() {
    if (!sourceDetail) return;
    const updated = await api<SourceSummary>(`/sources/${sourceDetail.id}/parse`, { method: "POST" });
    setSources((previous) => previous.map((source) => source.id === updated.id ? updated : source));
    await openSourceDetail(updated);
    await loadNotebookCollection();
    setToast("Source 已重新解析");
  }

  function confirmDeleteSource(source: SourceSummary) {
    setInfoModal({
      title: "删除来源",
      message: `确定删除“${source.title}”吗？它的解析元素、候选知识和由该来源生成的已批准知识也会一起移除。`,
      actions: [
        { label: "取消", action: () => {} },
        { label: "删除来源", danger: true, action: () => deleteSource(source).catch(reportError) }
      ]
    });
  }

  async function deleteSource(source: SourceSummary) {
    const notebookId = currentNotebookId ?? source.notebook_id;
    await api<null>(`/sources/${source.id}`, { method: "DELETE" });
    setSources((previous) => previous.filter((item) => item.id !== source.id));
    if (sourceDetail?.id === source.id) {
      setSourceDetail(null);
      setSourceElements([]);
    }
    await loadNotebookCollection();
    const refreshed = await api<NotebookSummary>(`/notebooks/${notebookId}`);
    setCurrentNotebook(refreshed);
    setKnowledge(EMPTY_KNOWLEDGE);
    setDuplicates(null);
    setToast("来源已删除");
  }

  function confirmDeleteArticle(article: ArticleSummary) {
    setInfoModal({
      title: "删除文章",
      message: `确定删除“${article.title}”吗？它的研究简报、claims 和候选规则会一起移除。`,
      actions: [
        { label: "取消", action: () => {} },
        { label: "删除文章", danger: true, action: () => deleteArticle(article.id).catch(reportError) }
      ]
    });
  }

  async function deleteArticle(articleId: string) {
    if (!currentNotebookId) return;
    await api<null>(`/articles/${articleId}`, { method: "DELETE" });
    setArticles((previous) => previous.filter((article) => article.id !== articleId));
    await loadNotebookCollection();
    const refreshed = await api<NotebookSummary>(`/notebooks/${currentNotebookId}`);
    setCurrentNotebook(refreshed);
    setToast("文章已删除");
  }

  async function runAsk(nextQuestion = question) {
    if (!currentNotebookId) return;
    const q = nextQuestion.trim();
    if (!q) return;
    const useReasoning = reasoningMode;
    setChatMode("ask");
    setQuestion("");
    setPendingQuestion(q);
    setPendingReasoning(useReasoning);
    setPendingTrace([]);
    setAsking(true);
    try {
      const payload = { question: q, conversation_id: conversationId ?? undefined, mode: useReasoning ? "reasoning" : "fast" };
      const response = useReasoning
        ? await readAskStream<AskResponse>(
          `/notebooks/${currentNotebookId}/ask/stream`,
          payload,
          (step) => setPendingTrace((previous) => [...previous, step]),
        )
        : await api<AskResponse>(`/notebooks/${currentNotebookId}/ask`, {
          method: "POST",
          body: JSON.stringify(payload)
        });
      setTurns((prev) => [...prev, { question: q, response }]);
      setConversationId(response.conversation_id);
    } catch (error) {
      setQuestion(q);
      reportError(error);
    } finally {
      setPendingQuestion("");
      setPendingReasoning(false);
      setPendingTrace([]);
      setAsking(false);
    }
    await loadSessions(currentNotebookId);
  }

  async function loadSessions(notebookId: string | null = currentNotebookId) {
    if (!notebookId) return;
    const list = await api<ConversationSummary[]>(`/notebooks/${notebookId}/conversations`);
    setSessions(list);
  }

  async function openSession(id: string) {
    const detail = await api<ConversationDetail>(`/conversations/${id}`);
    setTurns(detail.turns.map((turn) => ({ question: turn.question, response: turn.response })));
    setReasoningMode(lastTurnUsedReasoning(detail.turns));
    setConversationId(id);
    setPendingQuestion("");
    setPendingReasoning(false);
    setPendingTrace([]);
    setChatMode("ask");
    setSessionPanelOpen(false);
    setRenamingSessionId(null);
  }

  function startNewSession() {
    setTurns([]);
    setConversationId(null);
    setReasoningMode(false);
    setPendingQuestion("");
    setPendingReasoning(false);
    setPendingTrace([]);
    setChatMode("ask");
    setSessionPanelOpen(false);
    setRenamingSessionId(null);
  }

  async function deleteSession(id: string) {
    await api(`/conversations/${id}`, { method: "DELETE" });
    if (id === conversationId) {
      setTurns([]);
      setConversationId(null);
      setPendingQuestion("");
      setPendingReasoning(false);
      setPendingTrace([]);
    }
    await loadSessions(currentNotebookId);
    setToast("会话已删除");
  }

  function requestDeleteSession(session: ConversationSummary) {
    setInfoModal({
      title: "删除会话",
      message: `确定删除“${session.title || "未命名会话"}”吗？对应的历史问答会一起移除。`,
      actions: [
        { label: "取消", action: () => undefined },
        { label: "删除", danger: true, action: () => { deleteSession(session.id).catch(reportError); } },
      ],
    });
  }

  function beginRenameSession(session: ConversationSummary) {
    setRenamingSessionId(session.id);
    setSessionTitleDraft(session.title || "未命名会话");
    setSessionPanelOpen(true);
  }

  async function commitRenameSession(sessionId: string) {
    const next = sessionTitleDraft.trim();
    const current = sessions.find((session) => session.id === sessionId);
    if (!next || next === current?.title) {
      setRenamingSessionId(null);
      return;
    }
    await api(`/conversations/${sessionId}`, { method: "PATCH", body: JSON.stringify({ title: next }) });
    await loadSessions(currentNotebookId);
    setRenamingSessionId(null);
    setToast("会话已重命名");
  }


  async function loadKnowledge(kind: KnowledgeKind) {
    if (!currentNotebookId) return;
    const records = await api<KnowledgeRecord[]>(
      `/notebooks/${currentNotebookId}/knowledge?type=${encodeURIComponent(kind)}`
    );
    const response: KnowledgeItem[] = records.map((record) => ({
      id: record.id,
      status: record.status,
      owner: record.owner,
      last_reviewed: record.last_reviewed,
      evidence: record.evidence,
      headline: record.headline,
      object_type: record.object_type,
      fields: record.fields
    }));
    setKnowledge((prev) => ({ ...prev, [kind]: response }));
  }

  async function loadKnowledgeTypes() {
    if (!currentNotebookId) return;
    const types = await api<KnowledgeTypeCount[]>(
      `/notebooks/${currentNotebookId}/knowledge-types`
    );
    setKnowledgeTypes(types);
    return types;
  }

  async function updateKnowledge(id: string, patch: { status?: string; owner?: string }) {
    if (!currentNotebookId) return;
    await api(`/notebooks/${currentNotebookId}/knowledge/${id}`, { method: "PATCH", body: JSON.stringify(patch) });
    await loadKnowledge(knowledgeKind);
    await loadKnowledgeTypes();
    await loadNotebookCollection();
    const refreshed = await api<NotebookSummary>(`/notebooks/${currentNotebookId}`);
    setCurrentNotebook(refreshed);
    setToast("知识已更新");
  }

  function switchKnowledgeKind(kind: KnowledgeKind) {
    setKnowledgeKind(kind);
    setKnowledgeStatusFilter("all");
    setDuplicates(null);
    if (knowledge[kind] == null) loadKnowledge(kind).catch(reportError);
  }

  async function findDuplicates(kind: KnowledgeKind) {
    if (!currentNotebookId) return;
    const response = await api<DuplicateGroup[]>(
      `/notebooks/${currentNotebookId}/duplicates?type=${encodeURIComponent(kind)}`
    );
    setDuplicates(response);
  }

  async function mergeKnowledge(sourceId: string, intoId: string) {
    if (!currentNotebookId) return;
    await api(`/notebooks/${currentNotebookId}/knowledge/${sourceId}/merge`, {
      method: "POST",
      body: JSON.stringify({ into_id: intoId })
    });
    await loadKnowledge(knowledgeKind);
    await loadKnowledgeTypes();
    await findDuplicates(knowledgeKind);
    setToast("已合并，源条目置为 deprecated");
  }


  async function openAnalytics() {
    if (!currentNotebookId) return;
    const response = await api<NotebookAnalytics>(`/notebooks/${currentNotebookId}/analytics`);
    setAnalytics(response);
  }

  async function loadSchemas() {
    const response = await api<ObjectSchema[]>(`/object-schemas`);
    setSchemas(response);
  }

  function openSchemas() {
    setSchemaModalOpen(true);
    loadSchemas().catch(reportError);
  }

  async function patchSchema(objectType: string, patch: Partial<ObjectSchema> & { status?: string }) {
    setSchemaBusy(true);
    try {
      await api(`/object-schemas/${encodeURIComponent(objectType)}`, {
        method: "PATCH",
        body: JSON.stringify(patch)
      });
      await loadSchemas();
      setToast("Schema 已更新");
    } finally {
      setSchemaBusy(false);
    }
  }

  async function createSchema(payload: { object_type: string; label: string; fields: string[]; description: string }) {
    setSchemaBusy(true);
    try {
      await api(`/object-schemas`, { method: "POST", body: JSON.stringify(payload) });
      await loadSchemas();
      setToast("已新增类型");
    } finally {
      setSchemaBusy(false);
    }
  }

  async function deleteSchema(objectType: string) {
    setSchemaBusy(true);
    try {
      await api(`/object-schemas/${encodeURIComponent(objectType)}`, { method: "DELETE" });
      await loadSchemas();
      setToast("类型已删除");
    } finally {
      setSchemaBusy(false);
    }
  }

  async function openGraph() {
    if (!currentNotebookId) return;
    setGraphOpen(true);
    const response = await api<KnowledgeGraph>(`/notebooks/${currentNotebookId}/graph`);
    setGraph(response);
  }

  async function openKgView(targetNodeId?: string) {
    if (!currentNotebookId) return;
    setKgViewOpen(true);
    setSelectedKgNodeId(null); setConceptDetail(null); setNodeCtx(null);
    setKgSearch("");
    setKgSelectedTypes([]);
    setPendingKgFocusId(targetNodeId ?? null);
    try {
      const [g, pend, status] = await Promise.all([
        fetchUnifiedGraph(currentNotebookId),
        fetchPendingMerges(currentNotebookId),
        fetchUnifiedKgStatus(currentNotebookId),
      ]);
      setUGraph(g); setPendingMerges(pend); setUnifiedKgStatus(status);
    } catch (err) { reportError(err); }
  }

  async function refreshUnifiedKg() {
    if (!currentNotebookId) return;
    setKgRefreshBusy(true);
    try {
      await rebuildUnifiedKg(currentNotebookId);
      const [g, pend, status] = await Promise.all([
        fetchUnifiedGraph(currentNotebookId),
        fetchPendingMerges(currentNotebookId),
        fetchUnifiedKgStatus(currentNotebookId),
      ]);
      setUGraph(g); setPendingMerges(pend); setUnifiedKgStatus(status);
    } catch (err) { reportError(err); }
    finally { setKgRefreshBusy(false); }
  }

  async function reviewPendingMerges() {
    if (!currentNotebookId) return;
    setKgReviewBusy(true);
    try {
      const summary = await reviewPendingMergesApi(currentNotebookId);
      setToast(`已预审 ${summary.reviewed} 项：合并 ${summary.confirmed}，分开 ${summary.rejected}，保留 ${summary.unsure}`);
      const [pend, status] = await Promise.all([
        fetchPendingMerges(currentNotebookId),
        fetchUnifiedKgStatus(currentNotebookId),
      ]);
      setPendingMerges(pend);
      setUnifiedKgStatus(status);
    } catch (err) { reportError(err); }
    finally { setKgReviewBusy(false); }
  }

  function focusKgGraphNode(nodeId: string) {
    window.setTimeout(() => {
      const graph = kgGraphRef.current;
      const nodes = (graph?.graphData?.().nodes ?? fgData.nodes) as FgNode[];
      const node = nodes.find((item) => item.id === nodeId);
      if (!graph || node?.x == null || node?.y == null) return;
      graph.centerAt?.(node.x, node.y, 450);
      const visibleCount = nodes.length;
      const focusZoom = visibleCount <= 1 ? 1.18 : visibleCount <= 4 ? 1.45 : visibleCount <= 12 ? 1.85 : 2.2;
      graph.zoom?.(focusZoom, 450);
    }, 120);
  }

  async function selectKgNode(nodeId: string) {
    if (!currentNotebookId) return;
    setSelectedKgNodeId(nodeId);
    setNodeCtx(null);
    focusKgGraphNode(nodeId);
    window.setTimeout(() => {
      kgDetailRef.current?.scrollTo({ top: 0, behavior: "smooth" });
    }, 0);
    const node = uGraph?.nodes.find((item) => item.id === nodeId);
    if (node?.object_type !== "concept") setConceptDetail(null);
    else { try { setConceptDetail(await fetchConceptDetail(currentNotebookId, nodeId)); } catch (err) { setConceptDetail(null); reportError(err); } }
    try { setNodeCtx(await fetchNodeContext(currentNotebookId, nodeId)); } catch { /* node context best-effort */ }
  }

  async function decideMerge(candidateId: string, confirm: boolean) {
    if (!currentNotebookId) return;
    try {
      if (confirm) await confirmMergeApi(currentNotebookId, candidateId);
      else await rejectMergeApi(currentNotebookId, candidateId);
      await rebuildUnifiedKg(currentNotebookId);
      const [g, pend] = await Promise.all([fetchUnifiedGraph(currentNotebookId), fetchPendingMerges(currentNotebookId)]);
      setUGraph(g); setPendingMerges(pend);
      const selected = selectedKgNodeId ? g.nodes.find((node) => node.id === selectedKgNodeId) : null;
      if (selected?.object_type === "concept") setConceptDetail(await fetchConceptDetail(currentNotebookId, selected.id).catch(() => null));
      else setConceptDetail(null);
      if (!selected) setNodeCtx(null);
    } catch (err) { reportError(err); }
  }

  async function induceSchemas() {
    if (!currentNotebookId) return;
    setSchemaBusy(true);
    try {
      const proposals = await api<ObjectSchema[]>(
        `/notebooks/${currentNotebookId}/schema-proposals`,
        { method: "POST" }
      );
      await loadSchemas();
      setToast(proposals.length ? `归纳出 ${proposals.length} 个候选类型` : "未发现可补充的新类型（或未配置 LLM）");
    } finally {
      setSchemaBusy(false);
    }
  }

  async function openDerivedRules() {
    if (!currentNotebookId) return;
    const response = await api<DerivedRuleCandidate[]>(`/notebooks/${currentNotebookId}/derived-rules`);
    setDerivedRules(response);
    setDerivedOpen(true);
  }

  async function decideDerivedRule(candidateId: string, decision: "approve" | "reject") {
    if (!currentNotebookId) return;
    await api(`/notebooks/${currentNotebookId}/derived-rules/${candidateId}/${decision}`, { method: "POST" });
    await openDerivedRules();
    if (decision === "approve") {
      if (knowledge.rule !== null) await loadKnowledge("rule");
      await loadNotebookCollection();
      setToast("派生规则已批准并加入规则库");
    } else {
      setToast("派生规则候选已拒绝");
    }
  }

  // --- Governance: promotion queue (Track F) ---------------------------
  async function openPromoQueue() {
    const queue = await fetchPromotionQueue();
    setPromoQueue(queue);
    setPromoOpen(true);
  }

  async function submitPromotion(objectId: string) {
    if (!currentNotebookId) return;
    await proposePromotion(currentNotebookId, objectId);
    setToast("已提交晋升请求");
  }

  async function decidePromotion(candidateId: string, decision: "approve" | "reject", reason = "") {
    setPromoBusy(true);
    try {
      if (decision === "approve") {
        const result = await approvePromotion(candidateId);
        const merged = result.merged_into ? `（与 ${result.merged_into.slice(0, 8)} 合并去重）` : "";
        setToast(`晋升已批准${merged}，节点已加入基准语料`);
      } else {
        await rejectPromotion(candidateId, reason);
        setToast("晋升已拒绝，个人 KG 节点保持不变");
      }
      // Refresh queue, then any loaded notebook collection / knowledge list.
      const queue = await fetchPromotionQueue();
      setPromoQueue(queue);
      await loadNotebookCollection();
    } finally {
      setPromoBusy(false);
    }
  }

  function switchChatMode(mode: ChatMode) {
    setChatMode(mode);
    if (mode === "rules") {
      loadKnowledgeTypes().then((types) => {
        if (!types || types.length === 0) return;
        const available = types.map((t) => t.object_type);
        if (!available.includes(knowledgeKind)) {
          switchKnowledgeKind(types[0].object_type);
        } else if (knowledge[knowledgeKind] == null) {
          loadKnowledge(knowledgeKind).catch(reportError);
        }
      }).catch(reportError);
    }
  }

  function showStudioOutput(output: StudioOutput) {
    setInfoModal({
      title: output.title,
      message: "生成结果基于当前 notebook 的来源与文章研究，暂以弹窗形式展示。",
      sections: output.sections,
      actions: [{ label: "关闭", primary: true, action: () => {} }]
    });
  }

  async function runStudio(kind: "mindmap" | "infographic") {
    if (!currentNotebookId) return;
    const notebookArticles = articles.length > 0 ? articles : await loadArticles(currentNotebookId);
    if (notebookArticles.length === 0) {
      showStudioOutput({
        title: kind === "mindmap" ? "思维导图" : "信息图",
        sections: [
          ["暂无文章", ["该 notebook 还没有文章。请在 Article Studio 添加文章后再生成研究简报。"]],
          ["下一步", ["上传来源", "新建文章", "运行文章研究"]]
        ]
      });
      return;
    }
    const brief = await api<ArticleResearchBrief>(`/articles/${notebookArticles[0].id}/research`, { method: "POST" });
    setArticles((previous) =>
      previous.map((article) => article.id === brief.article.id ? brief.article : article)
    );
    await loadNotebookCollection();
    const refreshed = await api<NotebookSummary>(`/notebooks/${currentNotebookId}`);
    setCurrentNotebook(refreshed);
    if (kind === "mindmap") {
      showStudioOutput({
        title: "思维导图",
        sections: [
          ["中心主题", [brief.article.title]],
          ["关键 claim", brief.claims],
          ["与 notebook 的关系", brief.notebook_relationships],
          ["候选规则", brief.derived_rule_candidates]
        ]
      });
      return;
    }
    showStudioOutput({
      title: "信息图",
      sections: [
        ["核心贡献", [brief.core_contribution]],
        ["局限性", brief.limitations],
        ["验证计划", brief.validation_plan]
      ]
    });
  }

  function reportError(error: unknown) {
    const message = error instanceof Error ? error.message : String(error);
    setStatusText(`API error: ${message}`);
  }

  function openNotebookMenu(notebookId: string, event: MouseEvent<HTMLButtonElement>) {
    event.stopPropagation();
    const rect = event.currentTarget.getBoundingClientRect();
    const menuWidth = 180;
    const menuHeight = 116;
    setMenuPosition({
      left: Math.min(
        window.innerWidth - menuWidth - 12,
        Math.max(12, rect.right - menuWidth),
      ),
      top: Math.min(
        window.innerHeight - menuHeight - 12,
        rect.bottom + 8,
      ),
    });
    setMenuNotebookId(notebookId);
  }

  const isWorkspace = Boolean(currentNotebookId && currentNotebook);
  const menuNotebook = menuNotebookId
    ? notebooks.find((item) => item.id === menuNotebookId) ?? null
    : null;

  return (
    <div className={`app ${isWorkspace ? "workspace-mode" : ""}`}>
      <header className="topbar">
        <div className="brand">
          <button className="brand-mark" onClick={showCollection} title="Notebook collection">SN</button>
          <div>
            <div className="brand-title">silicon-notebook</div>
            <div className="brand-subtitle">{isWorkspace ? "Notebook workspace" : "Notebook collection"}</div>
          </div>
        </div>
        <div className="status"><span className="status-dot" /><span>{statusText}</span></div>
      </header>

      {!isWorkspace && (
        <main className="page collection-view">
          <section className="library-toolbar">
            <div className="tabs">
              {[
                ["all", "全部"],
                ["mine", "我的笔记本"],
                ["featured", "精选笔记本"]
              ].map(([id, label]) => (
                <button key={id} className={`tab ${filter === id ? "active" : ""}`} onClick={() => setFilter(id)}>
                  {label}
                </button>
              ))}
            </div>
            <div className="library-actions">
              <div className={`collection-search ${searchQuery ? "search-open" : ""}`}>
                <button className="icon-button" title="Search">⌕</button>
                <input value={searchQuery} onChange={(event) => setSearchQuery(event.target.value)} type="search" placeholder="搜索 notebook、来源、元素" />
              </div>
              <div className="segmented" aria-label="View mode">
                {[
                  ["grid", "✓", "卡片视图"],
                  ["compact", "▦", "紧凑视图"],
                  ["list", "☰", "列表视图"]
                ].map(([id, label, title]) => (
                  <button key={id} className={viewMode === id ? "active" : ""} title={title} onClick={() => setViewMode(id)}>
                    {label}
                  </button>
                ))}
              </div>
              <div className="sort-menu-wrap">
                <button className="sort-button" onClick={() => setSortOpen((value) => !value)}>
                  {sortMode === "name" ? "名称 ▾" : sortMode === "sources" ? "来源 ▾" : "最近 ▾"}
                </button>
                <div className={`popover sort-menu ${sortOpen ? "" : "hidden"}`}>
                  {[
                    ["recent", "最近创建"],
                    ["name", "名称"],
                    ["sources", "来源数量"]
                  ].map(([id, label]) => (
                    <button key={id} onClick={() => { setSortMode(id); setSortOpen(false); }}>{label}</button>
                  ))}
                </div>
              </div>
              <button className="new-pill" onClick={() => openCreate().catch(reportError)}>＋ 新建</button>
            </div>
          </section>

          <section className="collection-title">
            <h1>我的笔记本</h1>
            {searchQuery && <p>{visibleNotebooks.length} 个 notebook，搜索 “{searchQuery}”</p>}
          </section>

          <section className={`notebook-grid view-${viewMode}`}>
            {viewMode === "list" ? (
              <NotebookList
                entries={visibleNotebooks}
                openNotebook={(id) => openNotebook(id).catch(reportError)}
                openMenu={openNotebookMenu}
              />
            ) : (
              <>
                {!searchQuery && filter !== "featured" && (
                  <button className="notebook-card create-card" onClick={() => openCreate().catch(reportError)}>
                    <div className="create-circle">＋</div>
                    <h2>新建笔记本</h2>
                  </button>
                )}
                {visibleNotebooks.map(({ notebook, hits }, index) => (
                  <article key={notebook.id} className={`notebook-card ${cardTone(index)}`}>
                    <button className="card-menu" onClick={(event) => openNotebookMenu(notebook.id, event)} title="Notebook actions">⋮</button>
                    <button className="notebook-card-main" onClick={() => openNotebook(notebook.id).catch(reportError)}>
                      <div className="card-icon">{cardIcon(index, notebook)}</div>
                      <div>
                        <h2>{notebook.name}</h2>
                        <p>{notebook.purpose || "No purpose set yet."}</p>
                      </div>
                      <div className="notebook-card-footer">
                        <p>{notebook.created_label} · {notebook.counts.sources ?? 0} 个来源</p>
                      </div>
                      <SearchHits hits={hits} compact={false} />
                    </button>
                  </article>
                ))}
              </>
            )}
            {visibleNotebooks.length === 0 && (
              <article className="empty-state">
                <strong>没有找到 notebook</strong>
                <p>换一个关键词，或回到“我的笔记本”创建新的 notebook。</p>
              </article>
            )}
          </section>
        </main>
      )}

      {isWorkspace && currentNotebook && (
        <main className="notebook-view">
          <section className="workspace-header">
            <div className="workspace-title">
              <button className="notebook-home" onClick={showCollection}>SN</button>
              <div>
                <input
                  className="notebook-title-input"
                  value={titleDraft}
                  disabled={titleSaveInFlight}
                  aria-label="Notebook name"
                  maxLength={80}
                  onChange={(event) => setTitleDraft(event.target.value)}
                  onBlur={() => saveInlineNotebookName().catch(reportError)}
                  onKeyDown={(event: KeyboardEvent<HTMLInputElement>) => {
                    if (event.key === "Enter") event.currentTarget.blur();
                    if (event.key === "Escape") {
                      setTitleDraft(currentNotebook.name);
                      event.currentTarget.blur();
                    }
                  }}
                />
                <p>{currentNotebook.purpose || "This notebook has not defined a purpose yet."}</p>
              </div>
            </div>
            <div className="workspace-toolbar" aria-label="Notebook actions">
              <button className="workspace-primary-action" onClick={() => openCreate().catch(reportError)}>
                <Plus size={18} strokeWidth={2.8} />
                <span>创建笔记本</span>
              </button>
              <div className="workspace-nav-group">
                <button className="workspace-nav-button" onClick={() => setInfoModal({
                  title: "分析",
                  message: "从当前 notebook 的来源、文章研究和问答上下文进入分析。Studio 侧栏已收起，输出会在弹窗中呈现。",
                  actions: [
                    { label: "运行当前提问", primary: true, action: () => runAsk().catch(reportError) },
                    { label: "运行思维导图", action: () => runStudio("mindmap").catch(reportError) },
                    { label: "运行信息图", action: () => runStudio("infographic").catch(reportError) },
                    { label: "新建文章", action: () => setArticleModalOpen(true) },
                    { label: "派生规则候选", action: () => openDerivedRules().catch(reportError) },
                    { label: "晋升队列", action: () => openPromoQueue().catch(reportError) }
                  ]
                })}>
                  <BarChart3 size={17} />
                  <span>分析</span>
                </button>
                <button className="workspace-nav-button" onClick={() => openAnalytics().catch(reportError)}>
                  <LayoutDashboard size={17} />
                  <span>看板</span>
                </button>
                <button className="workspace-nav-button" onClick={openSchemas}>
                  <Database size={17} />
                  <span>Schema</span>
                </button>
                <button className="workspace-nav-button" onClick={() => openKgView()}>
                  <Network size={17} />
                  <span>知识图谱</span>
                </button>
                <button className="workspace-nav-button" onClick={() => setInfoModal({
                  title: "分享",
                  message: "当前是本机单用户 beta，分享会生成本地 notebook 链接；多人权限后续再接入。",
                  actions: [{ label: "复制本机链接", primary: true, action: () => navigator.clipboard?.writeText(window.location.href).then(() => setToast("本机链接已复制")).catch(() => setStatusText(window.location.href)) }]
                })}>
                  <Share2 size={17} />
                  <span>分享</span>
                </button>
                <button className="workspace-nav-button" onClick={() => setInfoModal({
                  title: "设置",
                  message: `${health?.llm_configured ? "LLM 已配置" : "LLM 尚未配置"}。当前设置页先保留状态与 notebook 编辑入口。`,
                  actions: [{ label: "编辑当前 notebook", primary: true, action: () => setEditingNotebook(currentNotebook) }]
                })}>
                  <Settings size={17} />
                  <span>设置</span>
                </button>
              </div>
            </div>
          </section>

          <section className="workspace-grid">
            <aside className="workspace-panel sources-panel">
              <div className="workspace-panel-header">
                <h2>Source Stack</h2>
                <span className="panel-count">{sources.length} 个来源</span>
              </div>
              <div className="workspace-panel-body sources-body">
                <label className="add-source-button">
                  <Plus size={20} strokeWidth={2.7} /> 添加来源
                  <input type="file" multiple accept=".pdf,.md,.markdown,.docx,.pptx,.csv,.xlsx,.xlsm" onChange={stageFiles} />
                </label>
                <div className="future-search">
                  <strong>Network source scout</strong>
                  <p>后续开放从网络环境中检索并添加来源。</p>
                  <button className="future-search-button" disabled title="Coming soon">
                    <Search size={18} /> Web · Fast Research
                  </button>
                </div>
                <div className="source-list">
                  {sources.length === 0 ? (
                    <article className="source-empty">
                      <div>▧</div>
                      <strong>已保存的来源将显示在此处</strong>
                      <p>点击上方的“添加来源”导入 PDF、Markdown、DOCX 或 PPTX。</p>
                    </article>
                  ) : (
                    sources.map((source) => (
                      <div
                        key={source.id}
                        className="source-row compact-source-row"
                        title={source.title}
                      >
                        <button className="source-row-main" onClick={() => openSourceDetail(source).catch(reportError)}>
                          <FileText className="source-file-icon" size={20} />
                          <span className="source-title-short">{compactSourceTitle(source)}</span>
                          <span className={`source-status-dot status-${source.parse_status || source.status}`} />
                          {source.extraction_warning && <span title={source.extraction_warning} style={{cursor:"help",marginLeft:2}}>⚠</span>}
                        </button>
                        <button className="source-delete-button" title="删除来源" onClick={() => confirmDeleteSource(source)}>
                          <Trash2 size={15} />
                        </button>
                      </div>
                    ))
                  )}
                </div>
              </div>
            </aside>

            <section className="workspace-panel chat-panel">
              <div className="workspace-panel-header">
                <div className="chat-tabs">
                  {CHAT_MODES.map(([mode, label]) => (
                    <button
                      key={mode}
                      className={`chat-tab ${chatMode === mode ? "active" : ""}`}
                      onClick={() => switchChatMode(mode)}
                    >{label}</button>
                  ))}
                </div>
                <div className="chat-header-actions">
                  {chatMode === "ask" && (
                    <button
                      className={`chat-session-toggle ${sessionPanelOpen ? "active" : ""}`}
                      type="button"
                      onClick={() => setSessionPanelOpen((open) => !open)}
                    >
                      <MessageSquareText size={15} />
                      会话
                    </button>
                  )}
                  <button className="icon-button compact" title="Clear" onClick={() => setInfoModal({
                    title: "对话",
                    message: "开始新对话将清空当前多轮上下文，回到欢迎状态。",
                    actions: [{ label: "新对话", primary: true, action: startNewSession }]
                  })}>⋮</button>
                </div>
              </div>
              {chatMode === "ask" && (
                <div className="chat-session-context">
                  <button
                    className="chat-current-session"
                    type="button"
                    onClick={() => setSessionPanelOpen((open) => !open)}
                    title={currentSession?.title || "新会话"}
                  >
                    <MessageSquareText size={16} />
                    <span>{currentSession?.title || "新会话"}</span>
                    <small>{currentSession ? `${formatRelativeTime(currentSession.updated_at)} · ${currentSession.turn_count} 轮` : "下一次提问会创建会话"}</small>
                  </button>
                  <button className="chat-session-new" type="button" onClick={startNewSession}>
                    <Plus size={14} /> 新会话
                  </button>
                  <button
                    className={`chat-session-toggle slim ${sessionPanelOpen ? "active" : ""}`}
                    type="button"
                    onClick={() => setSessionPanelOpen((open) => !open)}
                  >
                    历史 {sessions.length}
                  </button>
                </div>
              )}
              {chatMode === "ask" && sessionPanelOpen && (
                <div className="chat-session-popover" role="dialog" aria-label="会话管理">
                  <div className="chat-session-popover-head">
                    <div>
                      <strong>会话</strong>
                      <small>切换历史问答，不压缩当前回答区域。</small>
                    </div>
                    <button className="icon-button compact" type="button" onClick={() => setSessionPanelOpen(false)} title="关闭">
                      <X size={15} />
                    </button>
                  </div>
                  <div className="chat-session-list">
                    <button className={`chat-session-card new ${conversationId == null ? "active" : ""}`} type="button" onClick={startNewSession}>
                      <Plus size={16} />
                      <span>新会话</span>
                      <small>从一个新的问题开始</small>
                    </button>
                    {sessions.length === 0 ? (
                      <div className="chat-session-empty">还没有历史会话。</div>
                    ) : sessions.map((session) => (
                      <article className={`chat-session-card ${session.id === conversationId ? "active" : ""}`} key={session.id}>
                        {renamingSessionId === session.id ? (
                          <div className="chat-session-rename">
                            <input
                              autoFocus
                              value={sessionTitleDraft}
                              onChange={(event) => setSessionTitleDraft(event.target.value)}
                              onKeyDown={(event) => {
                                if (event.key === "Enter") commitRenameSession(session.id).catch(reportError);
                                if (event.key === "Escape") setRenamingSessionId(null);
                              }}
                            />
                            <button type="button" title="保存" onClick={() => commitRenameSession(session.id).catch(reportError)}><Check size={15} /></button>
                            <button type="button" title="取消" onClick={() => setRenamingSessionId(null)}><X size={15} /></button>
                          </div>
                        ) : (
                          <>
                            <button className="chat-session-card-main" type="button" onClick={() => openSession(session.id).catch(reportError)}>
                              <span>{session.title || "未命名会话"}</span>
                              <small>
                                {formatRelativeTime(session.updated_at)} · {session.turn_count} 轮
                                {session.used_reasoning && <span className="chat-session-reasoning-badge">✦ 推理</span>}
                              </small>
                            </button>
                            <div className="chat-session-card-actions">
                              <button type="button" title="重命名" onClick={() => beginRenameSession(session)}><Edit3 size={14} /></button>
                              <button type="button" title="删除" onClick={() => requestDeleteSession(session)}><Trash2 size={14} /></button>
                            </div>
                          </>
                        )}
                      </article>
                    ))}
                  </div>
                </div>
              )}
              <div ref={chatBodyRef} className={`chat-body ${chatMode !== "ask" || turns.length > 0 || asking ? "answer-mode" : ""}`}>
                {chatMode === "ask" && (turns.length === 0 && !asking ? (
                  <div className="welcome">
                    <div className="wave">👋</div>
                    <h2>{welcomeCopy.title}</h2>
                    <p>{welcomeCopy.description}</p>
                    <div className="prompt-chips">
                      {welcomeCopy.prompts.map(([label, prompt]) => (
                        <button key={label} onClick={() => runAsk(prompt).catch(reportError)}>{label}</button>
                      ))}
                    </div>
                  </div>
                ) : (
                  <div className="chat-thread">
                    {turns.map((turn, index) => (
                      <div className="chat-turn" key={turn.response.answer_id || index}>
                        <div className="chat-user">{turn.question}</div>
                        <div className="chat-assistant">
                          <AnswerView
                            answer={turn.response}
                            feedbackSent={feedbackSent[turn.response.answer_id] ?? ""}
                            onFeedback={(rating) => submitFeedback(turn.response.answer_id, rating, "").catch(reportError)}
                            onOpenKnowledgeGraph={(objectId) => openKgView(objectId)}
                          />
                        </div>
                      </div>
                    ))}
                    {asking && (
                      <div className="chat-turn">
                        {pendingQuestion && <div className="chat-user">{pendingQuestion}</div>}
                        <div className="chat-assistant chat-thinking">
                          {pendingReasoning ? (
                            <ReasoningTracePanel steps={pendingTrace} live />
                          ) : "思考中…"}
                        </div>
                      </div>
                    )}
                  </div>
                ))}

                {chatMode === "rules" && (
                  <KnowledgeBrowser
                    kind={knowledgeKind}
                    items={knowledge[knowledgeKind] ?? null}
                    types={knowledgeTypes}
                    statusFilter={knowledgeStatusFilter}
                    duplicates={duplicates}
                    notebookId={currentNotebookId ?? ""}
                    onKind={switchKnowledgeKind}
                    setStatusFilter={setKnowledgeStatusFilter}
                    onStatus={(id, status) => updateKnowledge(id, { status }).catch(reportError)}
                    onOwner={(id, owner) => updateKnowledge(id, { owner }).catch(reportError)}
                    onFindDuplicates={() => findDuplicates(knowledgeKind).catch(reportError)}
                    onMerge={(sourceId, intoId) => mergeKnowledge(sourceId, intoId).catch(reportError)}
                    reload={() => loadKnowledge(knowledgeKind).catch(reportError)}
                    tier={currentNotebook?.tier}
                    onPropose={(id) => submitPromotion(id).catch(reportError)}
                  />
                )}
              </div>
              {chatMode === "ask" && (
                <div className="chat-input-bar">
                  <textarea className="chat-input" rows={1} placeholder={askHint} value={question} onChange={(event) => setQuestion(event.target.value)} />
                  <span>{sources.length} 个来源</span>
                  <button
                    className={`reasoning-toggle${reasoningMode ? " active" : ""}`}
                    onClick={() => setReasoningMode((v) => !v)}
                    title={reasoningMode ? "推理模式已开启（点击关闭）" : "开启推理模式"}
                  >
                    ✦ 推理
                  </button>
                  <button className="send-button" onClick={() => runAsk().catch(reportError)}>→</button>
                </div>
              )}
            </section>

          </section>
        </main>
      )}

      {menuNotebook && menuPosition && (
        <div
          ref={notebookMenuRef}
          className="popover notebook-menu"
          style={{ left: menuPosition.left, top: menuPosition.top }}
        >
          <button onClick={() => { setEditingNotebook(menuNotebook); setMenuNotebookId(null); setMenuPosition(null); }}>编辑信息</button>
          <button className="danger" onClick={() => { setDeleteNotebook(menuNotebook); setMenuNotebookId(null); setMenuPosition(null); }}>删除 notebook</button>
        </div>
      )}

      {createOpen && (
        <section className="utility-modal" role="dialog" aria-modal="true" onClick={(event) => { if (event.currentTarget === event.target) setCreateOpen(false); }}>
          <div className="utility-modal-card">
            <div className="source-modal-header">
              <div>
                <h2>新建笔记本</h2>
                <p>只需名称与描述。描述留空时会在你添加首批来源后自动生成。文档类型在上传每个文件时选择。</p>
              </div>
              <button className="icon-button" onClick={() => setCreateOpen(false)} title="Close">×</button>
            </div>
            <div className="source-detail-body">
              <label>名称
                <input value={createName} autoFocus placeholder="例如 模拟封装 Knowhow" onChange={(event) => setCreateName(event.target.value)} />
              </label>
              <label>描述（可选）
                <textarea rows={3} value={createDesc} placeholder="留空则根据首批来源自动生成" onChange={(event) => setCreateDesc(event.target.value)} />
              </label>
              <div className="tag-row">
                <button className="new-pill" onClick={() => submitCreate().catch(reportError)}>创建并添加来源</button>
                <button className="sort-button" onClick={() => setCreateOpen(false)}>取消</button>
              </div>
            </div>
          </div>
        </section>
      )}

      {sourceModalOpen && (
        <section className="source-modal" role="dialog" aria-modal="true" onClick={(event) => { if (event.currentTarget === event.target) setSourceModalOpen(false); }}>
          <div className="source-modal-card">
            <div className="source-modal-header">
              <div>
                <h2>添加来源</h2>
                <p>选择文件后，可为每个文件指定文档类型（默认自动检测）；类型决定该文件的抽取 schema。</p>
              </div>
              <button className="icon-button" onClick={() => { setStagedFiles([]); setStagedDocTypes([]); setSourceModalOpen(false); }} title="Close">×</button>
            </div>
            <label className="drop-zone">
              <input type="file" multiple accept=".pdf,.md,.markdown,.docx,.pptx,.csv,.xlsx,.xlsm" onChange={stageFiles} />
              <span className="drop-plus">＋</span>
              <strong>{stagedFiles.length > 0 ? "继续添加文件" : "选择来源文件"}</strong>
              <small>支持 PDF / Markdown / DOCX / PPTX / CSV / Excel；图片与 OCR 暂不处理。</small>
            </label>
            {stagedFiles.length > 0 && (
              <div className="source-detail-body">
                <div className="tool-input-row">
                  <span className="section-title">{stagedFiles.length} 个待上传文件</span>
                  <label>全部设为
                    <select value="" onChange={(event) => { if (event.target.value !== "__none__") setAllStagedDocTypes(event.target.value); }}>
                      <option value="__none__">— 批量 —</option>
                      {docTypeOptions.map((opt) => <option key={opt.id || "auto"} value={opt.id}>{opt.label}</option>)}
                    </select>
                  </label>
                </div>
                <div className="stack">
                  {stagedFiles.map((file, index) => (
                    <div className="checklist-row" key={`${file.name}-${index}`}>
                      <span style={{ flex: 1 }}>{file.name}</span>
                      <select
                        value={stagedDocTypes[index] ?? ""}
                        onChange={(event) => setStagedDocType(index, event.target.value)}
                      >
                        {docTypeOptions.length === 0 && <option value="">自动检测</option>}
                        {docTypeOptions.map((opt) => <option key={opt.id || "auto"} value={opt.id}>{opt.label}</option>)}
                      </select>
                    </div>
                  ))}
                </div>
                <div className="tag-row">
                  <button className="new-pill" onClick={() => confirmUpload().catch(reportError)}>上传 {stagedFiles.length} 个文件</button>
                  <button className="sort-button" onClick={() => { setStagedFiles([]); setStagedDocTypes([]); }}>清空</button>
                </div>
              </div>
            )}
          </div>
        </section>
      )}

      {editingNotebook && (
        <section className="utility-modal" role="dialog" aria-modal="true">
          <div className="utility-modal-card">
            <div className="source-modal-header">
              <div>
                <h2>编辑 notebook</h2>
                <p>第一版先手动修改标题、描述和领域；后续会由大模型从来源中补全。</p>
              </div>
              <button className="icon-button" onClick={() => setEditingNotebook(null)} title="Close">×</button>
            </div>
            <form className="edit-form" onSubmit={(event) => saveNotebookEdit(event).catch(reportError)}>
              <label>标题<input name="name" defaultValue={editingNotebook.name} maxLength={80} required /></label>
              <label>描述<textarea name="purpose" defaultValue={editingNotebook.purpose} rows={3} maxLength={260} /></label>
              <label>领域<input name="primary_domain" defaultValue={editingNotebook.primary_domain} maxLength={80} /></label>
              <label>目标用户<input name="target_users" defaultValue={editingNotebook.target_users ?? ""} maxLength={120} /></label>
              <label>预期问题（每行/逗号一条）<textarea name="expected_questions" defaultValue={(editingNotebook.expected_questions ?? []).join("\n")} rows={2} /></label>
              <label>来源类型（每行/逗号一条）<input name="source_types" defaultValue={(editingNotebook.source_types ?? []).join(", ")} /></label>
              <label>分类 taxonomy（每行/逗号一条）<input name="taxonomy" defaultValue={(editingNotebook.taxonomy ?? []).join(", ")} /></label>
              <label>访问范围<input name="access_scope" defaultValue={editingNotebook.access_scope ?? ""} maxLength={80} /></label>
              <div className="modal-actions">
                <button type="button" className="sort-button" onClick={() => setEditingNotebook(null)}>取消</button>
                <button type="submit" className="new-pill">保存</button>
              </div>
            </form>
          </div>
        </section>
      )}

      {deleteNotebook && (
        <section className="utility-modal" role="dialog" aria-modal="true">
          <div className="utility-modal-card narrow">
            <div className="source-modal-header">
              <div>
                <h2>删除 notebook</h2>
                <p>确定删除 “{deleteNotebook.name}” 吗？这个本机 beta 会同时移除它的来源和 Studio 输出。</p>
              </div>
              <button className="icon-button" onClick={() => setDeleteNotebook(null)} title="Close">×</button>
            </div>
            <div className="modal-actions padded">
              <button className="sort-button" onClick={() => setDeleteNotebook(null)}>取消</button>
              <button className="new-pill danger-pill" onClick={() => confirmDeleteNotebook().catch(reportError)}>确认</button>
            </div>
          </div>
        </section>
      )}

      {infoModal && (
        <section className="utility-modal" role="dialog" aria-modal="true">
          <div className="utility-modal-card narrow">
            <div className="source-modal-header">
              <div>
                <h2>{infoModal.title}</h2>
                <p>{infoModal.message}</p>
              </div>
              <button className="icon-button" onClick={() => setInfoModal(null)} title="Close">×</button>
            </div>
            <div className="info-body">
              {infoModal.sections && (
                <div className="info-section-stack">
                  {infoModal.sections.map(([title, values]) => (
                    <article className="info-section" key={title}>
                      <strong>{title}</strong>
                      <p>{values.length > 0 ? values.join(" / ") : "暂无数据"}</p>
                    </article>
                  ))}
                </div>
              )}
              {infoModal.actions.map((action) => (
                <button
                  key={action.label}
                  className={action.danger ? "new-pill danger-pill" : action.primary ? "new-pill" : "sort-button"}
                  onClick={() => { setInfoModal(null); action.action(); }}
                >
                  {action.label}
                </button>
              ))}
            </div>
          </div>
        </section>
      )}

      {sourceDetail && (
        <section className="utility-modal" role="dialog" aria-modal="true">
          <div className="utility-modal-card source-detail-card">
            <div className="source-detail-shell-header">
              <h2>来源</h2>
              <button className="icon-button subtle-icon" onClick={() => setSourceDetail(null)} title="Close">
                <PanelRightClose size={22} />
              </button>
            </div>
            <div className="source-detail-body">
              <div className="source-detail-title-row">
                <h1 title={sourceDetail.title}>{sourceDetail.title}</h1>
                <div className="source-detail-actions">
                  <button className="icon-button subtle-icon" onClick={() => reparseSource().catch(reportError)} title="重新解析">
                    <ExternalLink size={23} />
                  </button>
                  <button className="icon-button subtle-icon danger-icon" onClick={() => confirmDeleteSource(sourceDetail)} title="删除来源">
                    <Trash2 size={20} />
                  </button>
                </div>
              </div>
              <section className="source-guide-card">
                <div className="source-guide-heading">
                  <Sparkles size={26} fill="currentColor" />
                  <h3>来源指南</h3>
                </div>
                <p>{sourceDetail.summary || "解析完成后，这里会显示由模型生成的来源摘要。"}</p>
              </section>
              <div className="source-detail-meta">
                <span className="tag">{sourceTypeLabel(sourceDetail)}</span>
                <span className="tag">{sourceDetail.parse_status || sourceDetail.status}</span>
                <span className="tag">{formatFileSize(sourceDetail.file_size)}</span>
                <span className="tag">{sourceElements.length} 个元素</span>
              </div>
              {sourceDetail.extraction_warning && (
                <p className="tag" style={{color:"var(--color-warning,#b45309)",background:"var(--color-warning-bg,#fef3c7)",border:"1px solid var(--color-warning-border,#fcd34d)",borderRadius:4,padding:"4px 8px",marginTop:4}}>⚠ {sourceDetail.extraction_warning}</p>
              )}
              <div className="source-element-stack">
                {sourceElements.length > 0 ? sourceElements.map((element) => (
                  <article className="item source-element-card" key={element.id}>
                    <div className="element-head">
                      <h3>{element.location_label}</h3>
                      <span className="tag element-type-tag">{element.element_type}</span>
                    </div>
                    <ElementBody element={element} />
                  </article>
                )) : (
                  <article className="item">
                    <h3>等待解析</h3>
                    <p>{sourceDetail.error_message || "当前来源还没有解析出元素。"}</p>
                  </article>
                )}
              </div>
            </div>
          </div>
        </section>
      )}

      {articleModalOpen && (
        <section className="utility-modal" role="dialog" aria-modal="true" onClick={(event) => { if (event.currentTarget === event.target) setArticleModalOpen(false); }}>
          <div className="utility-modal-card">
            <div className="source-modal-header">
              <div>
                <h2>新建文章</h2>
                <p>输入标题与摘要。研究简报会基于文章内容生成 claim，并与已批准的规则建立关系。</p>
              </div>
              <button className="icon-button" onClick={() => setArticleModalOpen(false)} title="Close">×</button>
            </div>
            <form className="edit-form" onSubmit={(event) => createArticle(event).catch(reportError)}>
              <label>标题<input name="title" maxLength={160} required /></label>
              <label>摘要<textarea name="abstract" rows={6} maxLength={2000} /></label>
              <div className="modal-actions">
                <button type="button" className="sort-button" onClick={() => setArticleModalOpen(false)}>取消</button>
                <button type="submit" className="new-pill">保存</button>
              </div>
            </form>
          </div>
        </section>
      )}

      {analytics && (
        <section className="utility-modal" role="dialog" aria-modal="true" onClick={(event) => { if (event.currentTarget === event.target) setAnalytics(null); }}>
          <div className="utility-modal-card">
            <div className="source-modal-header">
              <div>
                <h2>知识分析看板</h2>
                <p>回答质量、审核进度、知识覆盖与来源状态的本机统计。</p>
              </div>
              <button className="icon-button" onClick={() => setAnalytics(null)} title="Close">×</button>
            </div>
            <div className="source-detail-body">
              <p className="section-title">回答质量</p>
              <div className="tag-row">
                <span className="tag">提问 {analytics.answers_total}</span>
                <span className="tag">👍 {analytics.feedback_useful}</span>
                <span className="tag">👎 {analytics.feedback_not_useful}</span>
                <span className="tag">有用率 {Math.round(analytics.usefulness_rate * 100)}%</span>
              </div>
              {analytics.low_rated_questions.length > 0 && (
                <>
                  <p className="section-title">低分提问（知识缺口）</p>
                  <div className="stack">{analytics.low_rated_questions.map((q) => <div className="checklist-row" key={q}>{q}</div>)}</div>
                </>
              )}
              <p className="section-title">知识覆盖（已批准）</p>
              <div className="tag-row">
                {Object.entries(analytics.knowledge_counts).map(([k, v]) => <span className="tag" key={k}>{k}: {v}</span>)}
                {Object.keys(analytics.knowledge_counts).length === 0 && <span className="tool-hint">暂无已批准知识</span>}
              </div>
              <p className="section-title">来源状态</p>
              <div className="tag-row">
                {Object.entries(analytics.source_status_counts).map(([k, v]) => <span className="tag" key={k}>{k}: {v}</span>)}
              </div>
            </div>
          </div>
        </section>
      )}

      {schemaModalOpen && (
        <section className="utility-modal" role="dialog" aria-modal="true" onClick={(event) => { if (event.currentTarget === event.target) setSchemaModalOpen(false); }}>
          <div className="utility-modal-card">
            <div className="source-modal-header">
              <div>
                <h2>Schema 管理</h2>
                <p>管理抽取的知识对象类型与字段。内置类型可改字段/标签/停用；可新增自定义类型；也可从当前笔记本内容归纳候选类型（建议态，需人工批准）。</p>
              </div>
              <button className="icon-button" onClick={() => setSchemaModalOpen(false)} title="Close">×</button>
            </div>
            <div className="source-detail-body">
              <SchemaManager
                schemas={schemas}
                busy={schemaBusy}
                canInduce={Boolean(currentNotebookId)}
                onPatch={(t, p) => patchSchema(t, p).catch(reportError)}
                onCreate={(p) => createSchema(p).catch(reportError)}
                onDelete={(t) => deleteSchema(t).catch(reportError)}
                onInduce={() => induceSchemas().catch(reportError)}
              />
            </div>
          </div>
        </section>
      )}

      {graphOpen && (
        <section className="utility-modal" role="dialog" aria-modal="true" onClick={(event) => { if (event.currentTarget === event.target) setGraphOpen(false); }}>
          <div className="utility-modal-card">
            <div className="source-modal-header">
              <div>
                <h2>知识关系图</h2>
                <p>由各知识对象的关系字段（related_concepts / claims / formulas / procedures）解析出的边。用于 Implication / 冲突检测的下游消费。</p>
              </div>
              <button className="icon-button" onClick={() => setGraphOpen(false)} title="Close">×</button>
            </div>
            <div className="source-detail-body">
              {graph === null ? (
                <p className="tool-hint">加载中…</p>
              ) : graph.edges.length === 0 ? (
                <p className="tool-hint">暂无关系边。当抽取/审核的对象在 related_* 字段引用了同库其它对象时，这里会出现连线。</p>
              ) : (
                <div className="stack">
                  <div className="tag-row"><span className="tag">节点 {graph.nodes.length}</span><span className="tag">边 {graph.edges.length}</span></div>
                  {graph.edges.map((edge, index) => {
                    const from = graph.nodes.find((n) => n.id === edge.from_id);
                    const to = graph.nodes.find((n) => n.id === edge.to_id);
                    return (
                      <div className="checklist-row" key={`edge-${index}`}>
                        <strong>{from?.headline ?? edge.from_id}</strong>
                        <span className="tag">{RELATION_LABELS[edge.relation] ?? edge.relation}</span>
                        → <strong>{to?.headline ?? edge.to_id}</strong>
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          </div>
        </section>
      )}

      {kgViewOpen && (
        <section className="kg-view" role="dialog" aria-modal="true">
          <div className="kg-view-header">
            <div><h2>知识图谱</h2><p>Object 级知识图谱：Concept / Claim / Formula / Procedure 同屏展示。节点名称、类型形状和边标签直接画在主视图中。</p></div>
            <button className="icon-button" onClick={() => setKgViewOpen(false)} title="Close">×</button>
          </div>
          <div className="kg-view-body">
            <aside className="kg-rail">
              <input className="kg-search" placeholder="搜索节点、类型或关系…" value={kgSearch} onChange={(e) => setKgSearch(e.target.value)} />
              <div className="kg-rail-section">
                <h3>当前视图</h3>
                <div className="tag-row">
                  <span className="tag">节点 {fgData.nodes.length}{uGraph ? ` / ${uGraph.nodes.length}` : ""}</span>
                  <span className="tag">边 {fgData.links.length}{uGraph ? ` / ${uGraph.edges.length}` : ""}</span>
                </div>
                {unifiedKgStatus && (
                  <div className="tag-row" style={{ marginTop: 4 }}>
                    <span className="tag" style={{ color: unifiedKgStatus.dirty ? "var(--color-warn, #b97a00)" : undefined }}>
                      {unifiedKgStatus.dirty ? "待重建" : "已同步"}
                    </span>
                    {unifiedKgStatus.last_rebuild_at && (
                      <span className="tag">{formatRelativeTime(unifiedKgStatus.last_rebuild_at)}</span>
                    )}
                  </div>
                )}
                <button
                  className="sort-button"
                  style={{ marginTop: 6, width: "100%" }}
                  onClick={refreshUnifiedKg}
                  disabled={kgRefreshBusy}
                >
                  {kgRefreshBusy ? "重建中…" : "刷新图谱"}
                </button>
              </div>
              <div className="kg-rail-section">
                <h3>类型过滤</h3>
                <div className="kg-type-filter">
                  <button
                    aria-pressed={kgSelectedTypes.length === 0}
                    className={kgSelectedTypes.length === 0 ? "active" : ""}
                    onClick={() => setKgSelectedTypes([])}
                  >
                    <span className="kg-shape-stack">
                      {kgTypeCounts.slice(0, 4).map((item) => <KgTypeMark key={item.type} type={item.type} />)}
                    </span>
                    <strong>全部</strong>
                    <em>{uGraph?.nodes.length ?? 0}</em>
                  </button>
                  {kgTypeCounts.map((item) => (
                    <button
                      aria-pressed={kgSelectedTypes.includes(item.type)}
                      className={kgSelectedTypes.includes(item.type) ? "active" : ""}
                      key={item.type}
                      onClick={() => toggleKgType(item.type)}
                    >
                      <KgTypeMark type={item.type} />
                      <strong>{item.label}</strong>
                      <em>{item.count}</em>
                    </button>
                  ))}
                </div>
              </div>
              <div className="kg-rail-section">
                <h3>待确认合并 ({pendingMerges.length})</h3>
                <button className="ghost-button" onClick={reviewPendingMerges} disabled={!pendingMerges.length || kgReviewBusy}>
                  LLM 预审
                </button>
                {pendingMerges.length === 0 ? <p className="tool-hint">无</p> : pendingMerges.map((m) => (
                  <div className="kg-merge-row" key={m.id}>
                    <span>{m.canonical_a.replace(/^K-/, "")} ↔ {m.canonical_b.replace(/^K-/, "")} <em>({m.score.toFixed(2)})</em></span>
                    <span className="kg-merge-actions">
                      <button onClick={() => decideMerge(m.id, true)}>合并</button>
                      <button onClick={() => decideMerge(m.id, false)}>拒绝</button>
                    </span>
                  </div>
                ))}
              </div>
            </aside>
            <div className="kg-canvas" ref={kgCanvasRef}>
              {uGraph === null ? <p className="tool-hint kg-canvas-empty">加载中…</p> : fgData.nodes.length === 0 ? (
                <p className="tool-hint kg-canvas-empty">没有匹配的节点。清空搜索后可查看完整图谱。</p>
              ) : (
                <ForceGraph2D
                  ref={kgGraphRef}
                  graphData={fgData}
                  nodeLabel={(n: any) => `${n.name} (${n.type})`}
                  nodeVal={(n: any) => n.val}
                  width={kgSize.width}
                  height={kgSize.height}
                  linkDirectionalArrowLength={7}
                  linkDirectionalArrowRelPos={1}
                  linkColor={() => "rgba(91, 105, 130, 0.42)"}
                  linkWidth={1.35}
                  linkLabel={(link: any) => RELATION_LABELS[link.label] ?? link.label}
                  linkCanvasObjectMode={() => "after"}
                  linkCanvasObject={(link: any, ctx: CanvasRenderingContext2D, globalScale: number) => drawKgLinkLabel(link, ctx, globalScale, kgDenseView)}
                  nodeCanvasObject={(node: any, ctx: CanvasRenderingContext2D, globalScale: number) => drawKgNode(node, ctx, globalScale, selectedKgNodeId, kgDenseView)}
                  nodePointerAreaPaint={(node: any, color: string, ctx: CanvasRenderingContext2D) => paintKgPointerArea(node, color, ctx)}
                  d3VelocityDecay={0.32}
                  onEngineStop={() => fitKgGraphView(350)}
                  onNodeClick={(n: any) => selectKgNode(n.id)}
                />
              )}
              <div className="kg-legend">
                {Object.entries(KG_TYPE_STYLE).map(([type]) => (
                  <span key={type}><KgTypeMark type={type} />{kgTypeLabel(type)}</span>
                ))}
              </div>
            </div>
            <aside className="kg-detail" ref={kgDetailRef}>
              <div className="kg-node-overview">
                <div className="kg-detail-heading">
                  <h3>节点总览</h3>
                  <span>{kgNodeGroups.reduce((sum, group) => sum + group.nodes.length, 0)} 个</span>
                </div>
                {kgNodeGroups.length === 0 ? <p className="tool-hint">暂无节点。</p> : kgNodeGroups.map((group) => (
                  <section className="kg-type-group" key={group.type}>
                    <div className="kg-type-header">
                      <span><KgTypeMark type={group.type} />{group.label}</span>
                      <strong>{group.nodes.length}</strong>
                    </div>
                    <div className="kg-node-list">
                      {group.nodes.map((node) => (
                        <button
                          className={`kg-node-button ${selectedKgNodeId === node.id ? "active" : ""}`}
                          key={node.id}
                          onClick={() => selectKgNode(node.id).catch(reportError)}
                        >
                          <span>{truncateKgLabel(node.name, 58)}</span>
                          <em>{node.degree}</em>
                        </button>
                      ))}
                    </div>
                  </section>
                ))}
              </div>

              <div className="kg-selected-detail">
                {!selectedKgNode ? <p className="tool-hint">点击图中节点或总览列表查看详情。</p> : (
                  <div className="stack">
                    <h3>{kgNodeName(selectedKgNode)}</h3>
                    <div className="tag-row">
                      <span className="tag kg-selected-type"><KgTypeMark type={selectedKgNode.object_type} />{kgTypeLabel(selectedKgNode.object_type)}</span>
                      <span className="tag">关系 {selectedKgEdges.length}</span>
                    </div>
                    {Object.entries(selectedKgNode.payload)
                      .filter(([key, value]) => !["name", "section_path"].includes(key) && Boolean(kgPayloadValue(value)))
                      .map(([key, value]) => (
                        <p key={key}><strong>{FIELD_LABELS[key] ?? key}：</strong>{kgPayloadValue(value)}</p>
                      ))}
                    {selectedKgEdges.length > 0 && (
                      <>
                        <h4>相邻关系</h4>
                        {selectedKgEdges.slice(0, 24).map((edge, index) => (
                          <div className="kg-relation-row" key={`${edge.source_object_id}-${edge.target_object_id}-${index}`}>
                            <span className="kg-relation-node"><KgTypeMark type={edge.sourceType} /><span>{truncateKgLabel(edge.sourceName, 28)}</span></span>
                            <strong>{RELATION_LABELS[edge.edge_type] ?? edge.edge_type}</strong>
                            <span className="kg-relation-node"><KgTypeMark type={edge.targetType} /><span>{truncateKgLabel(edge.targetName, 28)}</span></span>
                          </div>
                        ))}
                      </>
                    )}
                    {nodeCtx?.definition && (<><h4>定义</h4><p className="kg-text-card">{nodeCtx.definition}</p></>)}
                    {nodeCtx?.object_type === "procedure" && nodeCtx.steps && nodeCtx.steps.length > 0 && (
                      <><h4>流程步骤</h4>{nodeCtx.steps.map((s, i) => (
                        <KgProcedureStepCard step={s} index={i} key={`${s.name}-${i}`} />
                      ))}</>
                    )}
                    {conceptDetail && (
                      <>
                        <h4>出处</h4>
                        {conceptDetail.evidence.length === 0 ? <p className="tool-hint">无</p> : conceptDetail.evidence.slice(0, 20).map((ev, i) => (
                          <KgEvidenceCard evidence={ev} index={i} key={`${ev.source_id}-${ev.element_id}-${i}`} />
                        ))}
                        <h4>相关节点</h4>
                        {relatedNodeGroups.length === 0 ? <p className="tool-hint">无</p> : relatedNodeGroups.map((group) => (
                          <section className="kg-related-group" key={group.type}>
                            <div className="kg-type-header">
                              <span><KgTypeMark type={group.type} />{group.label}</span>
                              <strong>{group.nodes.length}</strong>
                            </div>
                            <div className="kg-related-list">
                              {group.nodes.map((node) => (
                                <div className="kg-related-node" key={node.id}>
                                  <span><KgTypeMark type={node.object_type} />{String(node.payload.name ?? "")}</span>
                                  {node.edge_type ? <em>{RELATION_LABELS[node.edge_type] ?? node.edge_type}</em> : null}
                                </div>
                              ))}
                            </div>
                          </section>
                        ))}
                      </>
                    )}
                    {!conceptDetail && nodeCtx && (nodeCtx.occurrences ?? []).length > 0 && (
                      <><h4>出处</h4>{(nodeCtx.occurrences ?? []).slice(0, 10).map((o, i) => (
                        <KgOccurrenceCard occurrence={o} index={i} key={`${o.source_title || o.source_id}-${i}`} />
                      ))}</>
                    )}
                  </div>
                )}
              </div>
            </aside>
          </div>
        </section>
      )}

      {derivedOpen && (
        <section className="utility-modal" role="dialog" aria-modal="true" onClick={(event) => { if (event.currentTarget === event.target) setDerivedOpen(false); }}>
          <div className="utility-modal-card">
            <div className="source-modal-header">
              <div>
                <h2>派生规则候选</h2>
                <p>来自文章研究的候选规则。批准后会加入正式规则库，可在知识库中浏览和检索。</p>
              </div>
              <button className="icon-button" onClick={() => setDerivedOpen(false)} title="Close">×</button>
            </div>
            <div className="source-detail-body">
              {(derivedRules ?? []).length === 0 ? (
                <p className="tool-hint">暂无派生规则候选。先在 Studio 对文章运行研究简报。</p>
              ) : (
                <div className="stack">
                  {(derivedRules ?? []).map((candidate) => (
                    <article className="item" key={candidate.id}>
                      <div className="tag-row"><span className="tag">{candidate.status}</span></div>
                      <h3>{candidate.title || candidate.proposed_rule.slice(0, 80)}</h3>
                      <p>{candidate.proposed_rule}</p>
                      {candidate.rationale && <p><strong>依据：</strong>{candidate.rationale}</p>}
                      <EvidenceLine evidence={candidate.evidence} />
                      {candidate.status === "draft" && (
                        <div className="modal-actions">
                          <button className="sort-button" onClick={() => decideDerivedRule(candidate.id, "reject").catch(reportError)}>拒绝</button>
                          <button className="new-pill" onClick={() => decideDerivedRule(candidate.id, "approve").catch(reportError)}>批准为规则</button>
                        </div>
                      )}
                    </article>
                  ))}
                </div>
              )}
            </div>
          </div>
        </section>
      )}

      {promoOpen && (
        <section
          className="utility-modal"
          role="dialog"
          aria-modal="true"
          onClick={(event) => { if (event.currentTarget === event.target) setPromoOpen(false); }}
        >
          <div className="utility-modal-card">
            <div className="source-modal-header">
              <div>
                <h2>晋升队列</h2>
                <p>个人 KG 节点申请晋升到基准语料。批准后会执行去重并加入基准库。</p>
              </div>
              <button className="icon-button" onClick={() => setPromoOpen(false)} title="Close">×</button>
            </div>
            <div className="source-detail-body">
              {(promoQueue ?? []).length === 0 ? (
                <p className="tool-hint">暂无待审晋升请求。</p>
              ) : (
                <div className="stack">
                  {(promoQueue ?? []).map((cand) => (
                    <article className="item" key={cand.id}>
                      <div className="tag-row">
                        <span className="tag">{cand.status}</span>
                        <span className="tag">{cand.object_type}</span>
                        {cand.base_match_id && (
                          <span className="tag conflict">去重匹配: {cand.base_match_id.slice(0, 10)}</span>
                        )}
                      </div>
                      <h3>{String((cand.payload as Record<string, unknown>).name ?? (cand.payload as Record<string, unknown>).title ?? cand.object_id)}</h3>
                      <p className="tool-hint">来源笔记本: {cand.notebook_id.slice(0, 10)}</p>
                      {cand.evidence.length > 0 && (
                        <p><strong>证据：</strong>{cand.evidence[0].quoted_span ?? ""}</p>
                      )}
                      {cand.base_match_id && (
                        <p className="conflict-note">基准库中已有相似节点 — 批准后将合并去重。</p>
                      )}
                      {(cand.status === "proposed" || cand.status === "under_review") && (
                        <div className="modal-actions">
                          <button
                            className="sort-button"
                            disabled={promoBusy}
                            onClick={() => decidePromotion(cand.id, "reject").catch(reportError)}
                          >
                            拒绝
                          </button>
                          <button
                            className="new-pill"
                            disabled={promoBusy}
                            onClick={() => decidePromotion(cand.id, "approve").catch(reportError)}
                          >
                            批准晋升
                          </button>
                        </div>
                      )}
                    </article>
                  ))}
                </div>
              )}
            </div>
          </div>
        </section>
      )}

      {toast && <div className="toast">{toast}</div>}
    </div>
  );
}

function NotebookList({
  entries,
  openNotebook,
  openMenu
}: {
  entries: Array<{ notebook: NotebookSummary; index: number; hits: SearchHit[] }>;
  openNotebook: (id: string) => void;
  openMenu: (id: string, event: MouseEvent<HTMLButtonElement>) => void;
}) {
  return (
    <section className="notebook-list">
      <div className="notebook-list-header">
        <span>标题</span><span>来源</span><span>创建日期</span><span>角色</span><span />
      </div>
      {entries.map(({ notebook, index, hits }) => (
        <article className="notebook-list-row" key={notebook.id}>
          <button className="notebook-list-title" onClick={() => openNotebook(notebook.id)}>
            <span className="list-icon">{cardIcon(index, notebook)}</span>
            <span>
              <strong>{notebook.name}</strong>
              <SearchHits hits={hits} compact />
            </span>
          </button>
          <button className="notebook-list-cell" onClick={() => openNotebook(notebook.id)}>{notebook.counts.sources ?? 0} 个来源</button>
          <button className="notebook-list-cell" onClick={() => openNotebook(notebook.id)}>{notebook.created_label}</button>
          <button className="notebook-list-cell role-cell" onClick={() => openNotebook(notebook.id)}>Owner</button>
          <button className="list-row-menu" onClick={(event) => openMenu(notebook.id, event)} title="Notebook actions">⋮</button>
        </article>
      ))}
    </section>
  );
}

function SearchHits({ hits, compact }: { hits: SearchHit[]; compact: boolean }) {
  if (!hits.length) return null;
  if (compact) {
    const hit = hits[0];
    return <small>{hit.scope} · {hit.text}</small>;
  }
  return (
    <div className="card-search-hits">
      {hits.slice(0, 3).map((hit, index) => (
        <div key={`${hit.scope}-${index}`}>
          <span>{hit.scope}</span>
          <p>{hit.text}</p>
        </div>
      ))}
    </div>
  );
}

function FormulaView({ latex }: { latex: string }) {
  let html = "";
  try {
    html = katex.renderToString(latex, { throwOnError: false, displayMode: true });
  } catch {
    html = "";
  }
  if (!html) {
    return <pre className="element-formula-raw">{latex}</pre>;
  }
  return <div className="element-formula" dangerouslySetInnerHTML={{ __html: html }} />;
}

// Keep only static table markup; drop scripts/styles and any event handlers.
function sanitizeTableHtml(html: string): string {
  const withoutBlocks = html.replace(/<\/?(script|style)[^>]*>/gi, "");
  const withoutHandlers = withoutBlocks.replace(/\son\w+\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)/gi, "");
  const allowed = /^<\/?(table|thead|tbody|tfoot|tr|td|th|caption)(\s[^>]*)?>$/i;
  // Strip every tag that is not part of the allow-list above.
  return withoutHandlers.replace(/<\/?[a-z][^>]*>/gi, (tag) => (allowed.test(tag) ? tag : ""));
}

function ElementBody({ element }: { element: SourceElement }) {
  if (element.element_type === "formula") {
    return <FormulaView latex={element.text} />;
  }
  if (element.element_type === "table") {
    const html = typeof element.metadata?.table_html === "string" ? element.metadata.table_html : "";
    if (html) {
      return (
        <div
          className="element-table"
          dangerouslySetInnerHTML={{ __html: sanitizeTableHtml(html) }}
        />
      );
    }
  }
  return <p>{element.text}</p>;
}

function EvidenceLine({ evidence }: { evidence: Evidence[] }) {
  if (!evidence.length) return null;
  const first = evidence[0];
  return (
    <div className="citation">
      <strong>Evidence</strong>
      <div>{first.location_label}</div>
      <div>{first.quoted_span}</div>
    </div>
  );
}

function kgEvidenceBody(text?: string | null) {
  const value = (text ?? "").trim();
  return value || "暂无原文片段";
}

function kgConfidenceLabel(confidence?: number) {
  if (typeof confidence !== "number" || !Number.isFinite(confidence)) return "";
  const normalized = confidence > 1 ? confidence : confidence * 100;
  return `置信 ${Math.round(normalized)}%`;
}

function KgEvidenceCard({ evidence, index }: { evidence: EvidenceItem; index: number }) {
  const sourceLabel = evidence.source_title || evidence.source_id || "未知来源";
  const meta = [
    evidence.location_label,
    evidence.element_type,
    kgConfidenceLabel(evidence.confidence)
  ].filter(Boolean);

  return (
    <article className="kg-evidence-card">
      <div className="kg-evidence-header">
        <span className="kg-evidence-index">{index + 1}</span>
        <div className="kg-evidence-source">
          <strong title={sourceLabel}>{sourceLabel}</strong>
          {meta.length > 0 && (
            <div className="kg-evidence-meta">
              {meta.map((item) => <span key={item}>{item}</span>)}
            </div>
          )}
        </div>
      </div>
      <p className="kg-evidence-body">{kgEvidenceBody(evidence.element_text || evidence.quoted_span)}</p>
    </article>
  );
}

function KgOccurrenceCard({ occurrence, index }: { occurrence: KgOccurrence; index: number }) {
  const sourceLabel = occurrence.source_title || occurrence.source_id || "未知来源";
  const meta = [
    occurrence.location_label,
    occurrence.element_type,
    kgConfidenceLabel(occurrence.confidence)
  ].filter(Boolean);

  return (
    <article className="kg-evidence-card">
      <div className="kg-evidence-header">
        <span className="kg-evidence-index">{index + 1}</span>
        <div className="kg-evidence-source">
          <strong title={sourceLabel}>{sourceLabel}</strong>
          {meta.length > 0 && (
            <div className="kg-evidence-meta">
              {meta.map((item) => <span key={item}>{item}</span>)}
            </div>
          )}
        </div>
      </div>
      <p className="kg-evidence-body">{kgEvidenceBody(occurrence.element_text || occurrence.quoted_span)}</p>
    </article>
  );
}

function KgProcedureStepCard({ step, index }: { step: KgProcedureStep; index: number }) {
  return (
    <article className="kg-evidence-card kg-step-card">
      <div className="kg-evidence-header">
        <span className="kg-evidence-index">{index + 1}</span>
        <div className="kg-evidence-source">
          <strong>{step.name || `步骤 ${index + 1}`}</strong>
          <div className="kg-evidence-meta"><span>流程步骤</span></div>
        </div>
      </div>
      <p className="kg-evidence-body">{kgEvidenceBody(step.element_text)}</p>
    </article>
  );
}


function knowledgeHeadline(_kind: KnowledgeKind, item: KnowledgeItem): string {
  if (item.headline) return item.headline;
  return item.title || item.id;
}

// Field-key labels for the generic (case/claim/finding/concept/...) renderer.
const FIELD_LABELS: Record<string, string> = {
  statement: "陈述", claim_type: "类型", measurement_condition: "测量条件",
  limitation: "局限", metric: "指标", condition: "条件", dataset: "数据集",
  term: "术语", definition: "定义", why_it_matters: "意义", related_concepts: "相关概念",
  rationale: "依据", applies_to: "适用范围", problem: "问题", approach: "做法",
  result: "结果", symptom: "症状", context: "背景", root_cause: "根因",
  resolution: "解决", lesson_learned: "经验", required_evidence: "所需证据",
  question: "检查项", related_claims: "相关论断",
  related_formulas: "相关公式", related_procedures: "相关过程"
};

function genericBody(item: KnowledgeItem) {
  const fields = (item.fields ?? []).filter((f) => f.value && f.value !== item.headline);
  if (fields.length === 0) return null;
  return (
    <>
      {fields.map((field) => (
        <p key={field.key}>
          <strong>{FIELD_LABELS[field.key] ?? field.key}：</strong>
          {field.value}
        </p>
      ))}
    </>
  );
}

function knowledgeBody(_kind: KnowledgeKind, item: KnowledgeItem) {
  return genericBody(item);
}

function SchemaRow({
  schema,
  busy,
  onPatch,
  onDelete
}: {
  schema: ObjectSchema;
  busy: boolean;
  onPatch: (t: string, p: Partial<ObjectSchema> & { status?: string }) => void;
  onDelete: (t: string) => void;
}) {
  const [fieldsText, setFieldsText] = useState(schema.fields.join(", "));
  const [label, setLabel] = useState(schema.label);
  const [description, setDescription] = useState(schema.description);
  const dirty =
    fieldsText !== schema.fields.join(", ") ||
    label !== schema.label ||
    description !== schema.description;
  const save = () =>
    onPatch(schema.object_type, {
      fields: fieldsText.split(",").map((f) => f.trim()).filter(Boolean),
      label,
      description
    });
  return (
    <article className={`item ${schema.status === "disabled" ? "knowledge-deprecated" : ""}`}>
      <div className="tag-row">
        <strong>{schema.object_type}</strong>
        <span className="tag">{schema.source}</span>
        <span className={`tag ${schema.status === "active" ? "severity-low" : ""}`}>{schema.status}</span>
      </div>
      <label>显示名
        <input value={label} disabled={busy} onChange={(e) => setLabel(e.target.value)} />
      </label>
      <label>字段（逗号分隔，按顺序）
        <textarea rows={2} value={fieldsText} disabled={busy} onChange={(e) => setFieldsText(e.target.value)} />
      </label>
      <label>说明（用于抽取提示）
        <input value={description} disabled={busy} onChange={(e) => setDescription(e.target.value)} />
      </label>
      <div className="tag-row">
        <button className="sort-button" disabled={busy || !dirty} onClick={save}>保存</button>
        {schema.status === "active" ? (
          <button className="sort-button" disabled={busy} onClick={() => onPatch(schema.object_type, { status: "disabled" })}>停用</button>
        ) : (
          <button className="sort-button" disabled={busy} onClick={() => onPatch(schema.object_type, { status: "active" })}>启用</button>
        )}
        {schema.source !== "builtin" && (
          <button className="sort-button" disabled={busy} onClick={() => onDelete(schema.object_type)}>删除</button>
        )}
      </div>
    </article>
  );
}

function NewSchemaForm({
  busy,
  onCreate
}: {
  busy: boolean;
  onCreate: (p: { object_type: string; label: string; fields: string[]; description: string }) => void;
}) {
  const [objectType, setObjectType] = useState("");
  const [label, setLabel] = useState("");
  const [fieldsText, setFieldsText] = useState("");
  const [description, setDescription] = useState("");
  const submit = () => {
    const fields = fieldsText.split(",").map((f) => f.trim()).filter(Boolean);
    if (!objectType.trim() || fields.length === 0) return;
    onCreate({ object_type: objectType.trim(), label: label.trim(), fields, description: description.trim() });
    setObjectType(""); setLabel(""); setFieldsText(""); setDescription("");
  };
  return (
    <article className="item">
      <p className="section-title">新增自定义类型</p>
      <label>类型 id（snake_case）
        <input value={objectType} disabled={busy} placeholder="例如 process_window" onChange={(e) => setObjectType(e.target.value)} />
      </label>
      <label>显示名<input value={label} disabled={busy} onChange={(e) => setLabel(e.target.value)} /></label>
      <label>字段（逗号分隔）
        <textarea rows={2} value={fieldsText} disabled={busy} placeholder="title, condition, limit" onChange={(e) => setFieldsText(e.target.value)} />
      </label>
      <label>说明<input value={description} disabled={busy} onChange={(e) => setDescription(e.target.value)} /></label>
      <button className="sort-button" disabled={busy} onClick={submit}>新增类型</button>
    </article>
  );
}

function SchemaManager({
  schemas,
  busy,
  canInduce,
  onPatch,
  onCreate,
  onDelete,
  onInduce
}: {
  schemas: ObjectSchema[] | null;
  busy: boolean;
  canInduce: boolean;
  onPatch: (t: string, p: Partial<ObjectSchema> & { status?: string }) => void;
  onCreate: (p: { object_type: string; label: string; fields: string[]; description: string }) => void;
  onDelete: (t: string) => void;
  onInduce: () => void;
}) {
  if (schemas === null) return <p className="tool-hint">加载中…</p>;
  const proposed = schemas.filter((s) => s.status === "proposed");
  const managed = schemas.filter((s) => s.status !== "proposed");
  return (
    <div className="stack">
      <div className="tag-row">
        <button className="sort-button" disabled={busy || !canInduce} onClick={onInduce} title={canInduce ? "" : "先选择一个笔记本"}>
          从当前笔记本归纳候选类型
        </button>
        {busy && <span className="tag">处理中…</span>}
      </div>

      {proposed.length > 0 && (
        <>
          <p className="section-title">归纳候选（建议态，待批准）</p>
          {proposed.map((schema) => (
            <article className="item" key={schema.object_type}>
              <div className="tag-row">
                <strong>{schema.object_type}</strong>
                <span className="tag">induced</span>
              </div>
              {schema.rationale && <p><strong>理由：</strong>{schema.rationale}</p>}
              <p><strong>字段：</strong>{schema.fields.join(", ")}</p>
              <div className="tag-row">
                <button className="sort-button" disabled={busy} onClick={() => onPatch(schema.object_type, { status: "active" })}>批准并启用</button>
                <button className="sort-button" disabled={busy} onClick={() => onDelete(schema.object_type)}>拒绝</button>
              </div>
            </article>
          ))}
        </>
      )}

      <p className="section-title">已有类型（{managed.length}）</p>
      {managed.map((schema) => (
        <SchemaRow key={schema.object_type} schema={schema} busy={busy} onPatch={onPatch} onDelete={onDelete} />
      ))}

      <NewSchemaForm busy={busy} onCreate={onCreate} />
    </div>
  );
}

function KnowledgeBrowser({
  kind,
  items,
  types,
  statusFilter,
  duplicates,
  notebookId,
  onKind,
  setStatusFilter,
  onStatus,
  onOwner,
  onFindDuplicates,
  onMerge,
  reload,
  tier,
  onPropose
}: {
  kind: KnowledgeKind;
  items: KnowledgeItem[] | null;
  types: KnowledgeTypeCount[];
  statusFilter: string;
  duplicates: DuplicateGroup[] | null;
  notebookId: string;
  onKind: (kind: KnowledgeKind) => void;
  setStatusFilter: (value: string) => void;
  onStatus: (id: string, status: string) => void;
  onOwner: (id: string, owner: string) => void;
  onFindDuplicates: () => void;
  onMerge: (sourceId: string, intoId: string) => void;
  reload: () => void;
  tier?: string;
  onPropose?: (id: string) => void;
}) {
  const [ctx, setCtx] = useState<Record<string, NodeContext>>({});
  useEffect(() => { setCtx({}); }, [kind]);
  const statuses = ["all", ...Array.from(new Set((items ?? []).map((item) => item.status).filter(Boolean)))];
  const filtered = (items ?? []).filter((item) => statusFilter === "all" || item.status === statusFilter);
  // Build tabs purely from the dynamic /knowledge-types response.
  const tabs: Array<{ key: string; label: string; count?: number }> = types.map((t) => ({
    key: t.object_type,
    label: t.label,
    count: t.count
  }));
  return (
    <div className="tool-view">
      <div className="knowledge-kind-tabs">
        {tabs.map((tab) => (
          <button
            key={tab.key}
            className={`chat-tab ${kind === tab.key ? "active" : ""}`}
            onClick={() => onKind(tab.key)}
          >{tab.label}{tab.count ? ` (${tab.count})` : ""}</button>
        ))}
      </div>
      <div className="tool-input-row">
        <select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)}>
          {statuses.map((value) => <option key={value} value={value}>{value === "all" ? "全部状态" : value}</option>)}
        </select>
        <button className="sort-button" onClick={reload}>刷新</button>
        <button className="sort-button" onClick={onFindDuplicates}>查重</button>
      </div>
      {duplicates !== null && (
        <div className="knowledge-panel">
          <p className="section-title">重复组（相似度 ≥ 0.6）</p>
          {duplicates.length === 0 ? (
            <p className="tool-hint">未发现重复。</p>
          ) : duplicates.map((group, index) => (
            <article className="item" key={`dup-${index}`}>
              <div className="tag-row"><span className="tag">similarity {group.similarity}</span></div>
              {group.members.map((member, memberIndex) => (
                <div className="dup-member" key={member.id}>
                  <span>{member.headline} <span className="tag">{member.status}</span></span>
                  {memberIndex > 0 && (
                    <button className="sort-button" onClick={() => onMerge(member.id, group.members[0].id)}>
                      合并到第 1 条
                    </button>
                  )}
                </div>
              ))}
            </article>
          ))}
        </div>
      )}
      {items === null ? (
        <p className="tool-hint">加载中…</p>
      ) : filtered.length === 0 ? (
        <p className="tool-hint">暂无条目。在审核队列批准对应类型的候选后会出现在这里。</p>
      ) : (
        <div className="stack">
          {filtered.map((item) => (
            <article className={`item ${item.status === "deprecated" ? "knowledge-deprecated" : ""}`} key={item.id}>
              <div className="knowledge-item-title">
                <KgTypeMark type={item.object_type ?? kind} />
                <span>{kgTypeLabel(item.object_type ?? kind)}</span>
                <h3>{knowledgeHeadline(kind, item)}</h3>
              </div>
              {knowledgeBody(kind, item)}
              <div className="tag-row">
                {item.severity && <span className={`tag severity-${item.severity}`}>{item.severity}</span>}
                {(item.applies_to ?? []).map((scope) => <span className="tag" key={scope}>{scope}</span>)}
              </div>
              <div className="knowledge-govern">
                <label>状态
                  <select value={item.status} onChange={(event) => onStatus(item.id, event.target.value)}>
                    {KNOWLEDGE_STATUS_OPTIONS.map((value) => <option key={value} value={value}>{value}</option>)}
                  </select>
                </label>
                <label>Owner
                  <input
                    defaultValue={item.owner ?? ""}
                    placeholder="未分配"
                    onBlur={(event) => {
                      const next = event.target.value.trim();
                      if (next !== (item.owner ?? "")) onOwner(item.id, next);
                    }}
                  />
                </label>
                {item.last_reviewed && <span className="tag">reviewed {item.last_reviewed.slice(0, 10)}</span>}
                {tier === "personal" && item.status !== "deprecated" && onPropose && (
                  <button
                    className="sort-button"
                    title="提交晋升到基准库"
                    onClick={() => onPropose(item.id)}
                  >
                    ↑ 提交晋升
                  </button>
                )}
              </div>
              <EvidenceLine evidence={item.evidence} />
              {notebookId && !ctx[item.id] && (
                <button className="sort-button" onClick={() => {
                  fetchNodeContext(notebookId, item.id).then((result) => setCtx((previous) => ({ ...previous, [item.id]: result }))).catch(() => { setCtx((m) => ({ ...m, [item.id]: { id: item.id, object_type: item.object_type ?? "", name: "", section_path: "", occurrences: [], definition: null, steps: null } })); });
                }}>展开原文</button>
              )}
              {ctx[item.id] && (
                <>
                  {ctx[item.id].object_type === "procedure" && ctx[item.id].steps && (ctx[item.id].steps ?? []).length > 0 && (
                    <><p className="section-title">流程步骤</p>{(ctx[item.id].steps ?? []).map((s, i) => (
                      <KgProcedureStepCard step={s} index={i} key={`${s.name}-${i}`} />
                    ))}</>
                  )}
                  {(ctx[item.id].occurrences ?? []).length > 0 && (
                    <><p className="section-title">原文出处</p>{(ctx[item.id].occurrences ?? []).slice(0, 5).map((o, i) => (
                      <KgOccurrenceCard occurrence={o} index={i} key={`${o.source_title || o.source_id}-${i}`} />
                    ))}</>
                  )}
                </>
              )}
            </article>
          ))}
        </div>
      )}
    </div>
  );
}

function InlineFormula({ latex }: { latex: string }) {
  let html = "";
  try {
    html = katex.renderToString(latex, { throwOnError: false, displayMode: false });
  } catch {
    html = "";
  }
  if (!html) return <code className="answer-inline-code">{latex}</code>;
  return <span className="answer-inline-formula" dangerouslySetInnerHTML={{ __html: html }} />;
}

function referenceTitle(reference: AnswerReference): string {
  if (reference.anchor) return reference.anchor.name || reference.anchor.label || reference.anchor.key;
  return reference.citation?.label || reference.displayLabel;
}

function referenceSnippet(reference: AnswerReference): string {
  if (reference.anchor) return reference.anchor.definition || reference.anchor.snippet || "";
  return reference.citation?.quoted_span || "";
}

function referenceSource(reference: AnswerReference): string {
  if (reference.anchor) return reference.anchor.source_title || "";
  return reference.citation?.source_id || "";
}

function referenceLocation(reference: AnswerReference): string {
  if (reference.anchor) return reference.anchor.location_label || "";
  return reference.citation?.location_label || "";
}

function CiteChip({
  reference,
  selected,
  onSelect
}: {
  reference: AnswerReference;
  selected: boolean;
  onSelect: (reference: AnswerReference, event: MouseEvent<HTMLButtonElement>) => void;
}) {
  return (
    <span className="cite-chip-wrap">
      <button
        type="button"
        aria-expanded={selected}
        className={`cite-chip ${selected ? "active" : ""}`}
        onClick={(event) => onSelect(reference, event)}
      >{reference.displayLabel}</button>
    </span>
  );
}

function renderInlineAnswerText(
  text: string,
  refsByKey: Record<string, AnswerReference>,
  selectedReferenceId: string | null,
  onSelectReference: (reference: AnswerReference, event: MouseEvent<HTMLButtonElement>) => void
) {
  const nodes = [];
  const tokenPattern = /(`[^`]+`|\$[^$\n]+\$|\[(k\d+)\])/g;
  let lastIndex = 0;
  for (const match of text.matchAll(tokenPattern)) {
    if (match.index == null) continue;
    if (match.index > lastIndex) nodes.push(<span key={`text-${lastIndex}`}>{text.slice(lastIndex, match.index)}</span>);
    const token = match[0];
    const marker = token.match(/^\[(k\d+)\]$/);
    const inlineCode = token.match(/^`([^`]+)`$/);
    const inlineFormula = token.match(/^\$([^$\n]+)\$$/);
    if (marker) {
      const reference = refsByKey[marker[1]];
      nodes.push(reference ? (
        <CiteChip
          key={`cite-${match.index}`}
          reference={reference}
          selected={selectedReferenceId === reference.id}
          onSelect={onSelectReference}
        />
      ) : <span key={`unknown-cite-${match.index}`}>{token}</span>);
    } else if (inlineCode) {
      nodes.push(<code className="answer-inline-code" key={`code-${match.index}`}>{inlineCode[1]}</code>);
    } else if (inlineFormula) {
      nodes.push(<InlineFormula latex={inlineFormula[1]} key={`formula-${match.index}`} />);
    } else {
      nodes.push(<span key={`token-${match.index}`}>{token}</span>);
    }
    lastIndex = match.index + token.length;
  }
  if (lastIndex < text.length) nodes.push(<span key={`text-${lastIndex}`}>{text.slice(lastIndex)}</span>);
  return nodes;
}

function AnswerMarkdown({
  text,
  references,
  selectedReferenceId,
  onSelectReference
}: {
  text: string;
  references: AnswerReference[];
  selectedReferenceId: string | null;
  onSelectReference: (reference: AnswerReference, event: MouseEvent<HTMLButtonElement>) => void;
}) {
  const refsByKey = referenceByAnchorKey(references);
  const blocks = parseMarkdownBlocks(text);
  if (blocks.length === 0) return null;
  return (
    <div className="answer-markdown">
      {blocks.map((block: MarkdownBlock, index: number) => {
        if (block.type === "code") {
          return (
            <pre className="answer-code" data-language={block.language || undefined} key={index}>
              <code>{block.code}</code>
            </pre>
          );
        }
        if (block.type === "formula") return <FormulaView latex={block.latex} key={index} />;
        if (block.type === "table") {
          return (
            <div className="answer-table-wrap" key={index}>
              <table className="answer-table">
                <thead>
                  <tr>{block.headers.map((header, cellIndex) => <th key={cellIndex}>{renderInlineAnswerText(header, refsByKey, selectedReferenceId, onSelectReference)}</th>)}</tr>
                </thead>
                <tbody>
                  {block.rows.map((row, rowIndex) => (
                    <tr key={rowIndex}>{row.map((cell, cellIndex) => <td key={cellIndex}>{renderInlineAnswerText(cell, refsByKey, selectedReferenceId, onSelectReference)}</td>)}</tr>
                  ))}
                </tbody>
              </table>
            </div>
          );
        }
        if (block.type === "unordered-list") {
          return <ul key={index}>{block.items.map((item, itemIndex) => <li key={itemIndex}>{renderInlineAnswerText(item, refsByKey, selectedReferenceId, onSelectReference)}</li>)}</ul>;
        }
        if (block.type === "ordered-list") {
          return <ol key={index}>{block.items.map((item, itemIndex) => <li key={itemIndex}>{renderInlineAnswerText(item, refsByKey, selectedReferenceId, onSelectReference)}</li>)}</ol>;
        }
        return <p key={index}>{renderInlineAnswerText(block.text, refsByKey, selectedReferenceId, onSelectReference)}</p>;
      })}
    </div>
  );
}

async function copyTextToClipboard(text: string) {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.setAttribute("readonly", "true");
  textarea.style.position = "fixed";
  textarea.style.opacity = "0";
  document.body.appendChild(textarea);
  textarea.select();
  document.execCommand("copy");
  document.body.removeChild(textarea);
}

function SelectedReferenceDetail({
  reference,
  onOpenKnowledgeGraph
}: {
  reference: AnswerReference;
  onOpenKnowledgeGraph: (objectId?: string) => void;
}) {
  const objectType = reference.anchor?.object_type || "";
  const title = referenceTitle(reference);
  const snippet = referenceSnippet(reference);
  const source = referenceSource(reference);
  const location = referenceLocation(reference);
  return (
    <aside className="cite-detail-card" aria-live="polite">
      <div className="cite-detail-head">
        <strong>{reference.displayLabel}</strong>
        {objectType && <span><KgTypeMark type={objectType} />{kgTypeLabel(objectType)}</span>}
        <button
          type="button"
          onClick={() => onOpenKnowledgeGraph(reference.anchor?.object_id)}
          disabled={!reference.anchor?.object_id}
          title={reference.anchor?.object_id ? "在知识图谱中定位" : "该引用没有绑定知识节点"}
        >
          <ExternalLink size={14} />
          知识图谱
        </button>
      </div>
      <h4>{title}</h4>
      {snippet && <p>{snippet}</p>}
      {(source || location) && <small>{[source, location].filter(Boolean).join(" · ")}</small>}
    </aside>
  );
}

function ReasoningTracePanel({ steps, live = false }: { steps: ReasoningTraceStep[]; live?: boolean }) {
  const [expanded, setExpanded] = useState(false);
  const summary = getReasoningTraceSummary(steps, live);
  return (
    <div className={`reasoning-trace-panel ${live ? "live" : ""} ${expanded ? "expanded" : "collapsed"}`}>
      <button
        aria-expanded={expanded}
        className="reasoning-trace-summary"
        onClick={() => setExpanded((value) => !value)}
        type="button"
      >
        <Sparkles size={15} />
        <span className="reasoning-trace-title">{summary.title}</span>
        <span className={`reasoning-trace-chip ${summary.latestLabel ? "" : "empty"}`}>{summary.latestLabel || "空"}</span>
        <strong>{summary.latestSummary}</strong>
        <small>{summary.latestDetail}</small>
        <span className="reasoning-trace-count">{summary.stepCountLabel}</span>
        {expanded ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
      </button>
      {expanded && (
        <ol className="reasoning-trace-list">
          {steps.length === 0 ? (
            <li className="reasoning-trace-empty">等待后端事件…</li>
          ) : steps.map((step, index) => (
            <li key={`${step.step_type}-${index}`} className={index === steps.length - 1 && live ? "active" : ""}>
              <span>{TRACE_STEP_LABELS[step.step_type] ?? step.step_type}</span>
              <strong>{step.summary}</strong>
              {getTraceStepDetail(step) && <small>{getTraceStepDetail(step)}</small>}
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

function AnswerView({
  answer,
  feedbackSent,
  onFeedback,
  onOpenKnowledgeGraph
}: {
  answer: AskResponse;
  feedbackSent: string;
  onFeedback: (rating: "useful" | "not_useful") => void;
  onOpenKnowledgeGraph: (objectId?: string) => void;
}) {
  const [copied, setCopied] = useState(false);
  const [selectedReferenceId, setSelectedReferenceId] = useState<string | null>(null);
  const selectedReferenceRef = useRef<HTMLDivElement | null>(null);
  const answerText = answer.answer || answer.conclusion || "";
  const references = useMemo(
    () => buildAnswerReferences(answerText, answer.anchors, answer.citations),
    [answerText, answer.anchors, answer.citations]
  );
  const selectedReference = references.find((reference) => reference.id === selectedReferenceId);
  useEffect(() => {
    setSelectedReferenceId(null);
  }, [answer.answer_id]);
  useEffect(() => {
    if (!selectedReferenceId) return;
    window.setTimeout(() => {
      selectedReferenceRef.current?.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }, 0);
  }, [selectedReferenceId]);
  useEffect(() => {
    if (!copied) return;
    const timer = window.setTimeout(() => setCopied(false), 1400);
    return () => window.clearTimeout(timer);
  }, [copied]);

  async function copyAnswer() {
    await copyTextToClipboard(renderTextWithReferenceNumbers(answerText, references));
    setCopied(true);
  }

  return (
    <div className="chat-answer">
      {(() => {
        const lvl = answer.evidence_level ?? (answer.grounded ? "grounded" : "inferred");
        const meta =
          lvl === "grounded"
            ? { cls: "answer-grounded", label: "有据" }
            : lvl === "overview"
            ? { cls: "answer-overview", label: "概述（仅薄证据，余为推断）" }
            : { cls: "answer-ungrounded", label: "推断（未命中笔记本依据）" };
        return <span className={`tag ${meta.cls}`}>{meta.label}</span>;
      })()}
      <AnswerMarkdown
        text={answerText}
        references={references}
        selectedReferenceId={selectedReferenceId}
        onSelectReference={(reference) => setSelectedReferenceId((current) => current === reference.id ? null : reference.id)}
      />
      {answer.reasoning_trace && answer.reasoning_trace.length > 0 && (
        <ReasoningTracePanel steps={answer.reasoning_trace} />
      )}
      {selectedReference && (
        <div ref={selectedReferenceRef}>
          <SelectedReferenceDetail reference={selectedReference} onOpenKnowledgeGraph={onOpenKnowledgeGraph} />
        </div>
      )}
      <div className="answer-feedback">
        <button
          aria-label="有用"
          className={`answer-action ${feedbackSent === "useful" ? "selected" : ""}`}
          disabled={Boolean(feedbackSent)}
          onClick={() => onFeedback("useful")}
          title="有用"
          type="button"
        ><ThumbsUp size={16} /></button>
        <button
          aria-label="需改进"
          className={`answer-action ${feedbackSent === "not_useful" ? "selected" : ""}`}
          disabled={Boolean(feedbackSent)}
          onClick={() => onFeedback("not_useful")}
          title="需改进"
          type="button"
        ><ThumbsDown size={16} /></button>
        <button
          aria-label={copied ? "已复制" : "复制回答"}
          className={`answer-action ${copied ? "selected" : ""}`}
          onClick={() => copyAnswer().catch(() => undefined)}
          title={copied ? "已复制" : "复制回答"}
          type="button"
        >{copied ? <Check size={16} /> : <Copy size={16} />}</button>
      </div>
    </div>
  );
}
