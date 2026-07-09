"use client";

import { ChangeEvent, FormEvent, Fragment, KeyboardEvent, MouseEvent, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { BarChart3, Check, ChevronDown, ChevronRight, Copy, Database, Edit3, ExternalLink, FileText, GitMerge, LayoutDashboard, LogOut, MessageSquareText, Network, PanelLeftClose, PanelLeftOpen, PanelRightClose, Plus, Settings, Share2, Sparkles, Square, ThumbsDown, ThumbsUp, Trash2, Upload, X } from "lucide-react";
import katex from "katex";
import "katex/dist/katex.min.css";
import dynamic from "next/dynamic";
import {
  buildAnswerReferences,
  computeSourceTierCounts,
  renderTextWithReferenceNumbers,
  splitInlineLatex,
  type AnswerReference,
} from "./answer-formatting";
import { AnswerMarkdown } from "./answer-markdown";
import { placeCitationPopover } from "./citation-popover";
import { takeNdjsonLines, type AskStreamEvent, type ReasoningTraceStep } from "./ask-stream";
import { formatDuration, getReasoningTraceSummary, getTraceStepDetail, TRACE_STEP_LABELS } from "./reasoning-trace";
import {
  ASK_MODE_GROUPS, DEFAULT_ASK_MODE, type AskModeId,
  groupOf, modesInGroup, defaultModeForGroup, requiresKg, modeFromTurn,
} from "./ask-modes";
import {
  approvePromotion,
  fetchPromotionQueue,
  proposePromotion,
  rejectPromotion,
  type PromotionCandidate,
} from "./promotion-queue";
import { setNotebookTier, tierActionState } from "./notebook-tier";
import {
  describeScaleIndex, scaleIndexOpConfirm, SCALE_OP_MODE,
  type ScaleIndexOp, type ScaleIndexStatus,
} from "./scale-index";
import {
  shareNotebook,
  unshareNotebook,
  previewShared,
  copyShared,
  joinShared,
  leaveNotebook,
  sharedByMe,
  shareModeLabel,
  parseShareToken,
  buildShareLink,
  type ShareResponse,
  type SharedPreview,
  type SharedByMeItem,
} from "./notebook-share";
import { parseUrlLines } from "./url-sources";
import { fetchEdgeReviewQueue, reviewRelation, type EdgeReviewItem } from "./edge-review-queue";
import { conversationsOlderThan, CLEANUP_PRESETS } from "./conversation-cleanup";
import { API_BASE, authHeaders, clearToken, getToken, fetchMe, logoutUser, type AuthUser } from "./auth";
import {
  MODEL_ROLES, type ModelRole, type ServiceForm,
  buildPutPayload, fetchModelSettings, saveModelSettings, testModelService,
} from "./model-settings.ts";
import { AuthGate } from "./AuthGate";
import { Pagination } from "./Pagination";
import { ReportsPanel, type ReportDetailT, type ReportSummaryT } from "./report-view";
import { usePendingActions, PendingBell, PendingToast, type PendingItem } from "./pending-center";
import { canSeeAdminUsage } from "./admin/usage/format.ts";
import { shouldResumeReviewAll, shouldResumeScaleIndex, shouldResumeKgBuild, kgBuildFinished } from "./in-progress-resume";
import { jobPollDone, newTraceSteps, type AskJobDetail } from "./ask-reconnect";
// react-force-graph-2d uses canvas/window; load client-side only.
const ForceGraph2D = dynamic(() => import("react-force-graph-2d"), { ssr: false });

// 上传支持的扩展名（单一事实来源）：accept 串、stageFiles 校验、标题/文本剥扩展名都从此派生。
// 需与后端 backend/app/api/routes.py 的 SUPPORTED_SOURCE_SUFFIXES 保持一致。
const SUPPORTED_SOURCE_EXTENSIONS: string[] = [
  "pdf", "md", "markdown", "docx", "pptx", "csv", "xlsx", "xlsm",
];
const SUPPORTED_SOURCE_ACCEPT = SUPPORTED_SOURCE_EXTENSIONS.map((ext) => `.${ext}`).join(",");
const SUPPORTED_SOURCE_EXT_GROUP = SUPPORTED_SOURCE_EXTENSIONS.join("|");
// 面向用户的支持列表描述（拒绝 toast 与拖拽区提示共用，避免文案漂移）。
const SUPPORTED_SOURCE_USER_HINT = "PDF / Word(.docx) / PPT(.pptx) / Excel(.xlsx,.xlsm) / Markdown / CSV";
// 旧版二进制 Office 不被 MinerU 支持，给专门提示引导用户另存为 OOXML。
const LEGACY_OFFICE_EXTENSIONS = ["doc", "ppt", "xls"];

function fileExtension(name: string): string {
  const dot = name.lastIndexOf(".");
  return dot >= 0 ? name.slice(dot + 1).toLowerCase() : "";
}

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
  kg_ready?: boolean;
  kg_building?: boolean;
  base_kg_available?: boolean;
  base_notebook_name?: string; // 全局唯一基准库名(所有用户只读可见,分析弹窗顶部展示)
  kg_pending_sources?: number;
  access?: "owner" | "reader"; // "reader" = 只读共享而来(Phase 2)
  shared_from?: string;        // reader 时 = 原 owner 用户名
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
  source_url?: string;
  created_label: string;
  error_message?: string;
  extraction_warning?: string | null;
  kg_extracted?: boolean;
};

type PaginatedSources = {
  items: SourceSummary[];
  total_count: number;
  offset: number;
  limit: number;
};
const SOURCES_PAGE_SIZE = 50;

type PaginatedKnowledge = {
  items: KnowledgeRecord[];
  total_count: number;
  offset: number;
  limit: number;
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
  tier?: string;
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
  mode?: AskModeId;
  model_errors?: { stage: string; model: string; message: string }[];
  index_required?: boolean;
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
  active_job?: { job_id: string; question: string; mode: string; trace: ReasoningTraceStep[] };
};

type Citation = {
  label: string;
  source_id: string;
  element_id: string;
  location_label: string;
  quoted_span: string;
  tier?: string;
};

type ChatMode = "ask" | "rules" | "reports";

const CHAT_MODES: Array<[ChatMode, string]> = [
  ["ask", "问答"],
  ["rules", "知识库"],
  ["reports", "深度报告"]
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
type UnifiedEdge = { source_object_id: string; target_object_id: string; edge_type: string; support_count?: number; source_count?: number };
type UnifiedGraphResp = { nodes: UnifiedConceptNode[]; edges: UnifiedEdge[]; total_nodes?: number; total_edges?: number; truncated?: boolean; viz_building?: boolean };
type EvidenceItem = { source_id: string; source_title: string; element_id: string; element_type: string; location_label: string; quoted_span: string; confidence: number; element_text?: string };
type KgObject = { id: string; object_type: string; payload: { name?: string; section_path?: string; [k: string]: unknown }; evidence: EvidenceItem[]; edge_type?: string };
type ConceptDetailResp = { canonical_id: string; canonical_name: string; members: KgObject[]; attached: KgObject[]; evidence: EvidenceItem[] };
type KgOccurrence = { quoted_span?: string; source_title?: string; source_id?: string; element_text?: string; location_label?: string; element_type?: string; confidence?: number };
type KgProcedureStep = { name: string; element_text: string };
type NodeContext = { id: string; object_type: string; name: string; section_path: string; occurrences: KgOccurrence[]; definition: string | null; steps: KgProcedureStep[] | null };
type PendingMerge = { id: string; canonical_a: string; canonical_b: string; score: number; status: string };
type UnifiedKgStatus = { dirty: boolean; last_rebuild_at: string; objects: number; relations: number; clusters: number; viz_indexed: boolean; viz_nodes: number; viz_edges: number; viz_stale: boolean; viz_building?: boolean };
type MergeReviewSummary = { reviewed: number; confirmed: number; rejected: number; unsure: number };
type MergeReviewJob = { status: string; total: number; done: number; error: string };
type FgNode = { id: string; name: string; type: string; val: number; degree: number; x?: number; y?: number; vx?: number; vy?: number };
type FgLink = { source: string | FgNode; target: string | FgNode; label: string; sourceCount?: number };
type KgSearchHit = { object_id: string; name: string; object_type: string; score: number; match: string };
type KgSearchResp = { query: string; hits: KgSearchHit[] };
type KgNeighborsResp = { nodes: UnifiedConceptNode[]; edges: UnifiedEdge[] };

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

type InfoModal = {
  title: string;
  message: string;
  sections?: Array<[string, string[]]>;
  actions: Array<{
    label: string;
    desc?: string;
    // 按钮旁的上下文注记(如「当前基准库:名字」),渲染为描述下方的小徽标
    note?: string;
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
    // 常见英文虚词/填充词:避免标题里高频的 and/the/with… 被当成 topic(如
    // 「围绕 And 提问」)。正则只取 ≥3 字符词,故 of/in/to/is 等 2 字词天然排除。
    "and", "the", "for", "with", "from", "this", "that", "these", "those",
    "into", "onto", "over", "than", "per", "via", "but", "nor", "yet",
    "they", "them", "their", "there", "our", "your", "its", "his", "her",
    "who", "whom", "whose", "which", "what", "some", "any", "all", "both",
    "other", "another", "are", "was", "were", "been", "being", "has", "have",
    "had", "can", "could", "will", "would", "shall", "should", "might", "must",
    "not", "how", "why", "when", "where", "also", "more", "most", "very",
    "just", "such", "then", "thus", "here", "about", "above", "below", "after",
    "before", "between", "within", "without", "through", "upon", "off",
  ]);
  const counts = new Map<string, { label: string; count: number }>();
  const add = (text: string, weight: number) => {
    const normalized = text
      .replace(new RegExp(`\\.(${SUPPORTED_SOURCE_EXT_GROUP})\\b`, "gi"), " ")
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
  const notebookPurpose = notebook?.purpose?.trim();
  if (sources.length === 0) {
    return {
      title: "导入来源后开始提问",
      description: notebookPurpose || "添加 PDF、Markdown、DOCX 或 PPTX 后，这里会根据来源内容生成可追溯的问题建议。",
      prompts: promptChipsFor(notebook, sources),
    };
  }
  const topic = sourceTopicLabel(notebook, sources);
  return {
    title: `围绕 ${topic} 提问`,
    description: notebookPurpose || `已导入 ${sources.length} 个来源。可以围绕 ${topic} 的概念、论断、公式和过程提问，回答会优先绑定出处。`,
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

function isAbortError(error: unknown): boolean {
  return Boolean(error && typeof error === "object" && "name" in error && error.name === "AbortError");
}


async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const method = (options.method || "GET").toUpperCase();
  const started = performance.now();
  const response = await fetch(`${API_BASE}${path}`, {
    headers: options.body instanceof FormData ? { ...authHeaders(), ...(options.headers || {}) } : { "Content-Type": "application/json", ...authHeaders(), ...(options.headers || {}) },
    ...options
  });
  const elapsed = Math.round(performance.now() - started);
  const requestId = response.headers.get("X-Request-Id") || "";
  // Browser-side trace mirroring the backend request log (DevTools console).
  console.debug(`[api] ${method} ${path} -> ${response.status} ${elapsed}ms${requestId ? ` (${requestId})` : ""}`);
  if (response.status === 401 && getToken()) {
    clearToken();
    if (typeof window !== "undefined") window.location.reload();
  }
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
  signal?: AbortSignal,
  onStart?: (jobId: string) => void,
): Promise<TResponse> {
  const started = performance.now();
  const response = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify(payload),
    signal,
  });
  const elapsed = Math.round(performance.now() - started);
  const requestId = response.headers.get("X-Request-Id") || "";
  console.debug(`[api] POST ${path} -> ${response.status} ${elapsed}ms${requestId ? ` (${requestId})` : ""}`);
  if (response.status === 401 && getToken()) {
    clearToken();
    if (typeof window !== "undefined") window.location.reload();
  }
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
    if (event.event === "started") {
      onStart?.(event.job_id);
    } else if (event.event === "progress") {
      await onProgress(event.step);
      await yieldToPaint();
    } else if (event.event === "final") {
      finalResponse = event.response;
    } else if (event.event === "cancelled") {
      throw new DOMException("已中断回答", "AbortError");  // 走 runAsk 的 isAbortError 干净分支
    } else if (event.event === "error") {
      throw new Error(event.error);
    } else {
      const _exhaustive: never = event;   // union 新增 tag 未处理时 tsc 报错
      throw new Error(`unknown ask stream event: ${JSON.stringify(_exhaustive)}`);
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
const buildKg = (nb: string) => api<{ status: string; notebook_id: string }>(`/notebooks/${nb}/kg/build`, { method: "POST" });
const rebuildKg = (nb: string) => api<{ status: string; notebook_id: string }>(`/notebooks/${nb}/kg/rebuild`, { method: "POST" });
const relinkKg = (nb: string) => api<{ isolated_before: number; edges_added: number; isolated_after: number }>(`/notebooks/${nb}/kg/relink`, { method: "POST" });

// 深度报告(后台 job):类型见 report-view.tsx(与后端 ReportSummary/ReportDetail 对齐)。
const createReport = (nb: string, question: string, depth: number) =>
  api<{ report_id: string }>(`/notebooks/${nb}/reports`, { method: "POST", body: JSON.stringify({ question, depth }) });
const listReports = (nb: string) => api<ReportSummaryT[]>(`/notebooks/${nb}/reports`);
const getReport = (nb: string, rid: string) => api<ReportDetailT>(`/notebooks/${nb}/reports/${rid}`);
const cancelReport = (nb: string, rid: string) =>
  api<{ status: string }>(`/notebooks/${nb}/reports/${rid}/cancel`, { method: "POST" });
const deleteReport = (nb: string, rid: string) =>
  api<{ status: string }>(`/notebooks/${nb}/reports/${rid}`, { method: "DELETE" });
// 两阶段:PATCH 覆盖编辑后的大纲(仅 outline_ready 可改),POST 从 outline_ready 起生成 job。
const updateReportOutline = (nb: string, rid: string, sections: unknown[]) =>
  api<{ status: string; sections: number }>(`/notebooks/${nb}/reports/${rid}/outline`, { method: "PATCH", body: JSON.stringify({ sections }) });
const generateReport = (nb: string, rid: string, depth?: number) =>
  api<{ status: string }>(`/notebooks/${nb}/reports/${rid}/generate`, { method: "POST", body: JSON.stringify(depth != null ? { depth } : {}) });
// 批量导出返回 zip 二进制(非 JSON),api<T> 会 .json() 解析故不适用;
// 走原始 fetch,复用 API_BASE + authHeaders()(与 api()/readAskStream 同款认证),
// ok 时取 blob 触发下载,非 ok 抛出后端错误详情(如 422)。
async function downloadReportsZip(nb: string, reportIds: string[]): Promise<void> {
  const response = await fetch(`${API_BASE}/notebooks/${nb}/reports/export`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ report_ids: reportIds }),
  });
  if (response.status === 401 && getToken()) {
    clearToken();
    if (typeof window !== "undefined") window.location.reload();
  }
  if (!response.ok) {
    let detail = "";
    try {
      const body = await response.clone().json();
      detail = (body && (body.detail || body.message)) || "";
    } catch {
      detail = (await response.text().catch(() => "")) || "";
    }
    const suffix = detail ? ` - ${typeof detail === "string" ? detail : JSON.stringify(detail)}` : "";
    throw new Error(`${response.status} ${response.statusText}${suffix}`);
  }
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "reports.zip";
  a.click();
  URL.revokeObjectURL(url);
}

const rebuildScaleIndex = (nb: string, when: "now" | "idle" = "now", mode: "auto" | "fold" | "full" = "auto") =>
  api<{ status: string; notebook_id: string }>(`/notebooks/${nb}/scale-index/rebuild`, { method: "POST", body: JSON.stringify({ when, mode }) });
// 取消检索索引构建:排队中→出队(cancelled:true);构建中→不可协作打断(cancelled:false,
// reason:"building_not_interruptible",前端应提示「正在构建,完成后自动更新」)。
const cancelScaleIndex = (nb: string) =>
  api<{ cancelled: boolean; state: string; reason: string }>(`/notebooks/${nb}/scale-index/cancel`, { method: "POST" });
const fetchScaleIndexStatus = (nb: string) => api<ScaleIndexStatus>(`/notebooks/${nb}/scale-index/status`);

// 三系统构建状态聚合(kg=抽取/unified_kg=概念合并/scale_index=检索索引)——镜像后端
// index_status() 的返回形状(backend/app/services/sqlite_repository.py)。scale_index
// 原样复用既有 ScaleIndexStatus 类型,避免重复定义漂移。
type IndexStatus = {
  kg: { ready: boolean; building: boolean; pending_sources: number };
  unified_kg: { dirty: boolean; building: boolean; last_rebuild_at: string };
  scale_index: ScaleIndexStatus;
};
const fetchIndexStatus = (nb: string) => api<IndexStatus>(`/notebooks/${nb}/index-status`);

// confirmIndexAction 移到组件内部(见 Home() 内定义)——需要闭包 setInfoModal 才能弹
// 定制样式弹窗,放在模块级够不到 state。

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
// limit>0 只取连接度最高的前 N 个节点(核心子图，避免大图卡顿)；limit=0 取全量。
const fetchUnifiedGraph = (nb: string, limit = 0) =>
  api<UnifiedGraphResp>(`/notebooks/${nb}/unified-kg?level=object${limit > 0 ? `&limit=${limit}` : ""}`);
// 图谱范围档位：核心 80 / 160 / 320 / 全部(0)。打开图谱默认从核心 80 起。
const KG_RANGE_DEFAULT = 80;
const KG_RANGE_STEPS: Array<{ value: number; label: string }> = [
  { value: 80, label: "核心 80" },
  { value: 160, label: "核心 160" },
  { value: 320, label: "核心 320" },
  { value: 0, label: "全部" },
];
// 服务端搜索：FTS5 + ANN 混合，返回命中列表（不再客户端拉全量图）。命中数由服务端 k 参数控制。
const fetchKgSearch = (nb: string, q: string, k = 30) => api<KgSearchResp>(`/notebooks/${nb}/kg/search?q=${encodeURIComponent(q)}&k=${k}`);
// 逐跳展开：返回指定节点的邻居节点+边（bounded）。
const fetchKgNeighbors = (nb: string, oid: string, cap = 50) => api<KgNeighborsResp>(`/notebooks/${nb}/objects/${encodeURIComponent(oid)}/neighbors?cap=${cap}`);
const fetchConceptDetail = (nb: string, cid: string) => api<ConceptDetailResp>(`/notebooks/${nb}/concepts/${encodeURIComponent(cid)}/detail`);
const fetchNodeContext = (nb: string, oid: string) => api<NodeContext>(`/notebooks/${nb}/objects/${encodeURIComponent(oid)}/context`);
const fetchPendingMerges = (nb: string) => api<PendingMerge[]>(`/notebooks/${nb}/unified-kg/pending-merges`);
const fetchUnifiedKgStatus = (nb: string) => api<UnifiedKgStatus>(`/notebooks/${nb}/unified-kg/status`);
const confirmMergeApi = (nb: string, cid: string) => api<{ ok: boolean }>(`/notebooks/${nb}/unified-kg/merges/${encodeURIComponent(cid)}/confirm`, { method: "POST" });
const rejectMergeApi = (nb: string, cid: string) => api<{ ok: boolean }>(`/notebooks/${nb}/unified-kg/merges/${encodeURIComponent(cid)}/reject`, { method: "POST" });
const reviewPendingMergesApi = (nb: string) =>
  api<MergeReviewSummary>(`/notebooks/${nb}/unified-kg/merges/review`, {
    method: "POST",
    body: JSON.stringify({ limit: 50 }),
  });
const reviewAllMergesApi = (nb: string) =>
  api<{ status: string }>(`/notebooks/${nb}/unified-kg/merges/review-all`, { method: "POST" });
const fetchMergeReviewJob = (nb: string) =>
  api<MergeReviewJob>(`/notebooks/${nb}/unified-kg/merges/review-job`);
const fetchAskJob = (nb: string, jobId: string) =>
  api<AskJobDetail>(`/notebooks/${nb}/ask/jobs/${jobId}`);

function mergePairKey(candidate: PendingMerge): string {
  return [candidate.canonical_a, candidate.canonical_b].sort().join("\u0000");
}

function formatFileSize(size: number): string {
  if (!size) return "metadata only";
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${Math.round(size / 1024)} KB`;
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
}

function compactSourceTitle(source: SourceSummary): string {
  const rawTitle = (source.title || source.file_name || "Untitled source").trim();
  const withoutExtension = rawTitle.replace(new RegExp(`\\.(${SUPPORTED_SOURCE_EXT_GROUP})$`, "i"), "");
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
  let label = truncateKgLabel(RELATION_LABELS[link.label] ?? link.label, 18);
  if ((link.sourceCount ?? 1) >= 2) label += ` ×${link.sourceCount}`;
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

function accountInitials(username: string): string {
  const compact = username.trim().replace(/[^a-z0-9]/gi, "");
  return (compact.slice(0, 2) || "SN").toUpperCase();
}

export default function Home() {
  const [currentUser, setCurrentUser] = useState<AuthUser | null>(null);
  const [authChecked, setAuthChecked] = useState(false);
  const [health, setHealth] = useState<Health | null>(null);
  const [notebooks, setNotebooks] = useState<NotebookSummary[]>([]);
  const [searchHits, setSearchHits] = useState<Record<string, SearchHit[]>>({});
  const [currentNotebookId, setCurrentNotebookId] = useState<string | null>(null);
  const [currentNotebook, setCurrentNotebook] = useState<NotebookSummary | null>(null);
  const [sources, setSources] = useState<SourceSummary[]>([]);
  const [sourcesTotal, setSourcesTotal] = useState(0);
  const [sourcesPage, setSourcesPage] = useState(0);
  const [sourcesCollapsed, setSourcesCollapsed] = useState(false);
  const [sourceQuery, setSourceQuery] = useState("");
  const [question, setQuestion] = useState("");
  const [turns, setTurns] = useState<ChatTurn[]>([]);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [sessions, setSessions] = useState<ConversationSummary[]>([]);
  const [asking, setAsking] = useState(false);
  const [pendingQuestion, setPendingQuestion] = useState("");
  const [pendingMode, setPendingMode] = useState<AskModeId>(DEFAULT_ASK_MODE);
  const [pendingTrace, setPendingTrace] = useState<ReasoningTraceStep[]>([]);
  const [filter, setFilter] = useState("mine");
  const [viewMode, setViewMode] = useState("grid");
  const [sortMode, setSortMode] = useState("recent");
  const [searchQuery, setSearchQuery] = useState("");
  const [sortOpen, setSortOpen] = useState(false);
  const [accountMenuOpen, setAccountMenuOpen] = useState(false);
  const [menuNotebookId, setMenuNotebookId] = useState<string | null>(null);
  const [menuPosition, setMenuPosition] = useState<NotebookMenuPosition | null>(null);
  const [editingNotebook, setEditingNotebook] = useState<NotebookSummary | null>(null);
  const [deleteNotebook, setDeleteNotebook] = useState<NotebookSummary | null>(null);
  const [sourceModalOpen, setSourceModalOpen] = useState(false);
  const [linkSectionOpen, setLinkSectionOpen] = useState(false);
  const [urlText, setUrlText] = useState("");
  const [urlBusy, setUrlBusy] = useState(false);
  const [urlRejected, setUrlRejected] = useState<Array<{ url: string; reason: string }>>([]);
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
  const [modelPanelOpen, setModelPanelOpen] = useState(false);
  const [modelForms, setModelForms] = useState<Record<ModelRole, ServiceForm> | null>(null);
  const [modelTesting, setModelTesting] = useState<Record<string, string>>({});
  const [statusText, setStatusText] = useState("connecting");
  const [titleDraft, setTitleDraft] = useState("");
  const [titleSaveInFlight, setTitleSaveInFlight] = useState(false);
  const [feedbackSent, setFeedbackSent] = useState<Record<string, string>>({});
  const [sessionPanelOpen, setSessionPanelOpen] = useState(false);
  const [renamingSessionId, setRenamingSessionId] = useState<string | null>(null);
  const [sessionTitleDraft, setSessionTitleDraft] = useState("");
  const [chatMode, setChatMode] = useState<ChatMode>("ask");
  const [askMode, setAskMode] = useState<AskModeId>(DEFAULT_ASK_MODE);
  const [knowledgeKind, setKnowledgeKind] = useState<KnowledgeKind>("concept");
  const [knowledge, setKnowledge] = useState<Record<string, KnowledgeItem[] | null>>(EMPTY_KNOWLEDGE);
  const [knowledgeTypes, setKnowledgeTypes] = useState<KnowledgeTypeCount[]>([]);
  const [knowledgeStatusFilter, setKnowledgeStatusFilter] = useState("all");
  const [knowledgeTotal, setKnowledgeTotal] = useState<Record<string, number>>({});
  const [knowledgePage, setKnowledgePage] = useState<Record<string, number>>({});
  const [duplicates, setDuplicates] = useState<DuplicateGroup[] | null>(null);
  // Promotion queue modal (Track F governance)
  const [promoQueue, setPromoQueue] = useState<PromotionCandidate[] | null>(null);
  const [promoOpen, setPromoOpen] = useState(false);
  const [promoBusy, setPromoBusy] = useState(false);
  const [edgeQueue, setEdgeQueue] = useState<EdgeReviewItem[] | null>(null);
  const [edgeReviewOpen, setEdgeReviewOpen] = useState(false);
  const [edgeBusy, setEdgeBusy] = useState(false);
  // 分享(owner 侧):shareModal 存分享结果并驱动分享弹窗;shareBusy 覆盖分享/取消分享请求
  const [shareModal, setShareModal] = useState<ShareResponse | null>(null);
  const [shareBusy, setShareBusy] = useState(false);
  // 接收分享(拷贝侧):sharedPreview 存预览并驱动预览弹窗;copyBusy 覆盖拷贝/加入请求
  const [sharedPreview, setSharedPreview] = useState<SharedPreview | null>(null);
  const [copyBusy, setCopyBusy] = useState(false);
  // 只读共享(Phase 2):退出共享请求覆盖;已分享总览 modal 的数据与开关
  const [leaveBusy, setLeaveBusy] = useState(false);
  const [sharedByMeList, setSharedByMeList] = useState<SharedByMeItem[] | null>(null);
  const [sharedByMeOpen, setSharedByMeOpen] = useState(false);
  const [analytics, setAnalytics] = useState<NotebookAnalytics | null>(null);
  const [schemaModalOpen, setSchemaModalOpen] = useState(false);
  const [schemas, setSchemas] = useState<ObjectSchema[] | null>(null);
  const [schemaBusy, setSchemaBusy] = useState(false);
  const [graph, setGraph] = useState<KnowledgeGraph | null>(null);
  const [graphOpen, setGraphOpen] = useState(false);
  const [kgViewOpen, setKgViewOpen] = useState(false);
  const [uGraph, setUGraph] = useState<UnifiedGraphResp | null>(null);
  // 大库首次可视化索引在后台构建时，GET /unified-kg 返回占位 viz_building:true；
  // 这里驱动图区「构建中」提示 + 轮询，直到索引建好后自动换真图。
  const [vizBuilding, setVizBuilding] = useState(false);
  // 服务端搜索结果 — 搜索时用这个叠加层，不再懒加载全量图
  const [kgSearchHits, setKgSearchHits] = useState<KgSearchHit[]>([]);
  const [kgSearchBusy, setKgSearchBusy] = useState(false);
  // 用户点击展开的邻居节点/边（叠加到 uGraph 上）
  const [kgExpandedNodes, setKgExpandedNodes] = useState<UnifiedConceptNode[]>([]);
  const [kgExpandedEdges, setKgExpandedEdges] = useState<UnifiedEdge[]>([]);
  const [kgLimit, setKgLimit] = useState(KG_RANGE_DEFAULT);
  const [pendingMerges, setPendingMerges] = useState<PendingMerge[]>([]);
  const [unifiedKgStatus, setUnifiedKgStatus] = useState<UnifiedKgStatus | null>(null);
  const [kgRefreshBusy, setKgRefreshBusy] = useState(false);
  const [kgRangeBusy, setKgRangeBusy] = useState(false);
  const [buildingKg, setBuildingKg] = useState(false);
  const [relinkingKg, setRelinkingKg] = useState(false);
  const [buildingScaleIndex, setBuildingScaleIndex] = useState(false);
  const [scaleIndexStatus, setScaleIndexStatus] = useState<ScaleIndexStatus | null>(null);
  const [cancelingScaleIndex, setCancelingScaleIndex] = useState(false);
  // 「索引与构建」面板(看板弹窗)的三系统聚合状态——openAnalytics 打开时经 fetchIndexStatus
  // 一次拉齐,面板打开期间任一系统忙碌时轻量轮询保鲜(见下方 effect)。
  const [indexStatus, setIndexStatus] = useState<IndexStatus | null>(null);
  const [pendingReportFocusId, setPendingReportFocusId] = useState<string | null>(null);
  const pending = usePendingActions(Boolean(authChecked && getToken()));
  // 「索引与构建」面板统一确认——三系统(知识图谱/概念合并/检索索引)的破坏性动作
  // (完整重抽/重新合并/构建-更新-全量重建)执行前一律经此弹窗,与「删除来源」「删除
  // 会话」等既有破坏性操作共用同一套 setInfoModal 定制样式弹窗(向上统一,而非退到
  // 原生 window.confirm)。异步/回调式:调用后立即返回,modal 先渲染,onConfirm 在用户
  // 点「确定」时才执行——调用方需以回调改造调用点,不能当同步布尔值 `if (confirm(...))` 用。
  // message 沿用既有「动作名？\n\n后果说明」模板,按首个 \n\n 拆成弹窗的标题/正文。
  // 非破坏性动作(首次构建/补连孤立节点)不经过此函数,直接执行——与既有设计一致
  // (见 relinkFromKgView 注释)。
  function confirmIndexAction(message: string, onConfirm: () => void) {
    const splitAt = message.indexOf("\n\n");
    const title = splitAt >= 0 ? message.slice(0, splitAt).replace(/[？?]$/, "") : "确认操作";
    const body = splitAt >= 0 ? message.slice(splitAt + 2) : message;
    setInfoModal({
      title,
      message: body,
      actions: [
        { label: "取消", action: () => {} },
        { label: "确定", danger: true, action: onConfirm },
      ],
    });
  }
  // Kick off a KG build for `nb`; the effect below then polls until it's ready.
  const startKgBuild = (nb: string) => {
    setBuildingKg(true);
    buildKg(nb)
      .then(() => setToast("已开始构建知识图谱（后台进行，可能需要数分钟）；完成后会自动更新"))
      .catch((e) => { reportError(e); setBuildingKg(false); });
  };
  // Trigger full re-extract: clears existing KG and rebuilds from all sources.
  // 破坏性(清空重抽)——统一确认(与概念合并/检索索引三系统一致的确认机制+文案模板)。
  const startKgRebuild = (nb: string) => {
    confirmIndexAction("完整重抽知识图谱？\n\n将清空现有知识图谱并重新抽取全部来源。后台进行，完成后自动更新。", () => {
      setBuildingKg(true);
      rebuildKg(nb)
        .then(() => setToast("已开始完整重抽（后台进行，可能需要数分钟）；完成后会自动更新"))
        .catch((e) => { reportError(e); setBuildingKg(false); });
    });
  };
  // 侧栏收起状态持久化(localStorage;隐私模式等读写失败静默降级)
  useEffect(() => {
    try {
      if (window.localStorage.getItem("sn.sourcesCollapsed") === "1") setSourcesCollapsed(true);
    } catch { /* ignore */ }
  }, []);
  useEffect(() => {
    try { window.localStorage.setItem("sn.sourcesCollapsed", sourcesCollapsed ? "1" : "0"); } catch { /* ignore */ }
  }, [sourcesCollapsed]);
  // Relink isolated nodes: additive/synchronous, no confirm needed.
  // 补连孤立节点已移入知识图谱视图（relinkFromKgView，完成后按当前范围重拉）。
  // While a build runs, poll the notebook until kg_ready flips — the build can
  // take minutes, so the button reflects real progress instead of a fixed guess.
  // 面板(analytics/「索引与构建」看板)打开时让位给下方聚合轮询 effect 独占轮询——否则
  // 同一构建期间两条 effect 各自 6s 打一次 /notebooks 与 /index-status,服务端开销加倍。
  // 完工检测(flag 复位 + toast)相应搬进聚合 effect;面板关闭后本 effect 照常接管到底。
  useEffect(() => {
    if (!buildingKg || !currentNotebookId || analytics) return;
    const nb = currentNotebookId;
    let cancelled = false;
    const poll = window.setInterval(async () => {
      try {
        const refreshed = await api<NotebookSummary>(`/notebooks/${nb}`);
        if (cancelled) return;
        if (kgBuildFinished(refreshed)) {
          setCurrentNotebook((cur) => (cur && cur.id === nb ? refreshed : cur));
          setBuildingKg(false);
          setToast("知识图谱构建完成 ✓ 可用严格推理");
        }
      } catch { /* transient error; keep polling */ }
    }, 6000);
    // Safety cap so the button never spins forever (failed build / huge corpus).
    const cap = window.setTimeout(() => {
      if (!cancelled) { setBuildingKg(false); setToast("构建仍在后台进行，请稍后刷新查看状态"); }
    }, 20 * 60 * 1000);
    return () => { cancelled = true; window.clearInterval(poll); window.clearTimeout(cap); };
  }, [buildingKg, currentNotebookId, analytics]);
  // Scale-index (CSR graph + ANN) status: only meaningful for base-tier libraries.
  // 任意库选中时拉一次检索索引状态(与 tier 解耦:大个人库也显示/可建);构建中由下方 effect 轮询。
  useEffect(() => {
    const nb = currentNotebookId;
    if (!nb) { setScaleIndexStatus(null); return; }
    let cancelled = false;
    fetchScaleIndexStatus(nb).then((s) => {
      if (cancelled) return;
      setScaleIndexStatus(s);
      if (shouldResumeScaleIndex(s)) setBuildingScaleIndex(true);  // 刷新后接回构建中
    }).catch(() => {});
    return () => { cancelled = true; };
  }, [currentNotebookId]);
  // Mirror the buildingKg poll: while a scale-index rebuild runs, poll status every 6s
  // until building flips false, with a 20min safety cap so the button never spins forever.
  // 同上:面板打开时让位给聚合轮询 effect(见其完工检测),避免与 /scale-index/status 重复轮询。
  useEffect(() => {
    if (!buildingScaleIndex || !currentNotebookId || analytics) return;
    const nb = currentNotebookId;
    let cancelled = false;
    const poll = window.setInterval(async () => {
      try {
        const s = await fetchScaleIndexStatus(nb);
        if (cancelled) return;
        setScaleIndexStatus(s);
        // 终态判定复用 shouldResumeScaleIndex:building 或 state==="queued" 都还没完工，
        // 否则「已排队」在这里会被 !s.building 误判成完成、提前弹「构建完成」toast 并停止轮询。
        if (!shouldResumeScaleIndex(s)) {
          setBuildingScaleIndex(false);
          setToast(s.stale ? "索引重建结束（仍有更新未纳入）" : "检索索引重建完成 ✓");
        }
      } catch { /* transient error; keep polling */ }
    }, 6000);
    const cap = window.setTimeout(() => {
      if (!cancelled) { setBuildingScaleIndex(false); setToast("索引仍在后台构建，请稍后查看"); }
    }, 20 * 60 * 1000);
    return () => { cancelled = true; window.clearInterval(poll); window.clearTimeout(cap); };
  }, [buildingScaleIndex, currentNotebookId, analytics]);
  // 「索引与构建」看板弹窗打开期间,若三系统任一在忙(本地忙碌位,或聚合端点报告的
  // kg/概念合并后台在建),轮询聚合端点(6s)保持面板内三行状态新鲜；modal 关闭或全部
  // 空闲时自动停止,不额外占用轮询。
  // 面板打开期间本 effect 独占轮询(上方 buildingKg/buildingScaleIndex 两条 legacy poll
  // effect 各自加了 `analytics` 跳过守卫,面板开着时不再重复打 /notebooks 与
  // /scale-index/status)——完工检测(flag 复位 + toast)相应搬来这里,否则面板开着时
  // legacy effect 不跑、标志位与完成提示永远等不到。知识图谱完工额外补一次
  // /notebooks 拉全量 NotebookSummary(index-status 只回填 kg_ready/pending 三个字段,
  // 侧栏「补抽 N 篇」等大量读 currentNotebook 全量字段的地方需要真正刷新)。
  useEffect(() => {
    if (!analytics || !currentNotebookId) return;
    const busy = buildingKg || kgRefreshBusy || buildingScaleIndex
      || Boolean(indexStatus?.kg.building) || Boolean(indexStatus?.unified_kg.building);
    if (!busy) return;
    const nb = currentNotebookId;
    let cancelled = false;
    const poll = window.setInterval(() => {
      fetchIndexStatus(nb).then((s) => {
        if (cancelled) return;
        setIndexStatus(s);
        setScaleIndexStatus(s.scale_index);
        if (buildingKg && !s.kg.building) {
          setBuildingKg(false);
          setToast("知识图谱构建完成 ✓ 可用严格推理");
          api<NotebookSummary>(`/notebooks/${nb}`)
            .then((refreshed) => { if (!cancelled) setCurrentNotebook((cur) => (cur && cur.id === nb ? refreshed : cur)); })
            .catch(() => {});
        }
        // 同上:终态需 building 与 state==="queued" 都排除，否则「已排队」的自动 fold
        // 会在这里被误判成完工，提前弹「构建完成」toast 并停止轮询。
        if (buildingScaleIndex && !shouldResumeScaleIndex(s.scale_index)) {
          setBuildingScaleIndex(false);
          setToast(s.scale_index.stale ? "索引重建结束（仍有更新未纳入）" : "检索索引重建完成 ✓");
        }
      }).catch(() => {});
    }, 6000);
    return () => { cancelled = true; window.clearInterval(poll); };
  }, [analytics, currentNotebookId, buildingKg, kgRefreshBusy, buildingScaleIndex, indexStatus?.kg.building, indexStatus?.unified_kg.building]);
  // Mirror the buildingScaleIndex poll: while the KG view's background viz index is
  // building for a large notebook, poll every 6s until it flips false, then swap in the
  // real graph. 20min safety cap so the view never spins forever. Keyed on kgViewOpen too
  // so closing the view (or switching notebooks) cancels the poll.
  useEffect(() => {
    if (!vizBuilding || !kgViewOpen || !currentNotebookId) return;
    const nb = currentNotebookId;
    let cancelled = false;
    const poll = window.setInterval(async () => {
      try {
        const g = await fetchUnifiedGraph(nb, kgLimit);
        if (cancelled) return;
        if (g.viz_building) return;
        setUGraph(g);
        setVizBuilding(false);
        fetchUnifiedKgStatus(nb).then((status) => { if (!cancelled) setUnifiedKgStatus(status); }).catch(() => {});
      } catch { /* transient error; keep polling */ }
    }, 6000);
    const cap = window.setTimeout(() => {
      if (!cancelled) { setVizBuilding(false); setToast("图谱索引仍在后台构建，请稍后重新打开查看"); }
    }, 20 * 60 * 1000);
    return () => { cancelled = true; window.clearInterval(poll); window.clearTimeout(cap); };
  }, [vizBuilding, kgViewOpen, currentNotebookId, kgLimit]);
  // Kick off a scale-index rebuild; the effect above then polls until it's ready.
  const startScaleIndexRebuild = async (nb: string, when: "now" | "idle" = "now", mode: "auto" | "fold" | "full" = "auto") => {
    setBuildingScaleIndex(true);
    try {
      await rebuildScaleIndex(nb, when, mode);
      if (when === "idle") {
        setToast("已排队，将在服务器空闲时（低峰）重建；完成后自动更新");
        // Reflect the queued state right away; the poll effect keeps it fresh.
        fetchScaleIndexStatus(nb).then((s) => setScaleIndexStatus(s)).catch(() => {});
      } else {
        setToast(mode === "fold"
          ? "已开始更新检索索引（增量收录新增来源，后台进行）；完成后自动更新"
          : "已开始构建检索索引（后台进行，可能数分钟）；完成后自动更新");
      }
    } catch (e) { reportError(e); setBuildingScaleIndex(false); }
  };
  // 「检索索引」三个精确动作 —— 各自弹确认(描述具体精确)后立即后台执行:
  //   build  未构建/建议 → 从零构建(full)    update 已过期 → 增量收录新增源(fold)
  //   rebuild 已建成     → 删除现有索引从头全量重建(full)。与 tier 解耦,大库亦可建。
  const runScaleIndexOp = (op: ScaleIndexOp) => {
    const s = scaleIndexStatus;
    if (!currentNotebookId || buildingScaleIndex || !s || s.building || s.state === "queued") return;
    const nb = currentNotebookId;
    confirmIndexAction(scaleIndexOpConfirm(op, s), () => startScaleIndexRebuild(nb, "now", SCALE_OP_MODE[op]));
  };
  // 「空闲时建」—— 与上面三个精确动作同一确认机制,仅 when 改为 idle(排队等服务器空闲/
  // 低峰再建,mode 沿用 "auto" 交给后端按当时状态判断 fold/full)。原 admin 动作列表里的
  // 「空闲时重建检索索引」收敛到这里,是面板对该能力的唯一入口。
  const runScaleIndexIdle = () => {
    const s = scaleIndexStatus;
    if (!currentNotebookId || buildingScaleIndex || !s || s.building || s.state === "queued") return;
    const nb = currentNotebookId;
    confirmIndexAction(
      "空闲时构建检索索引？\n\n排队等待服务器空闲（低峰窗口）时自动构建/更新索引，避开高峰；完成后自动更新。",
      () => startScaleIndexRebuild(nb, "idle")
    );
  };
  // 取消检索索引:排队中→出队成功,刷新聚合状态;构建中(不可协作打断)→按后端 reason 提示。
  const handleCancelScaleIndex = async () => {
    if (!currentNotebookId || cancelingScaleIndex) return;
    const nb = currentNotebookId;
    setCancelingScaleIndex(true);
    try {
      const r = await cancelScaleIndex(nb);
      if (r.reason === "building_not_interruptible") {
        setToast("正在构建，完成后自动更新");
      } else if (r.cancelled) {
        setToast("已取消排队中的检索索引构建");
      }
      const s = await fetchIndexStatus(nb);
      setIndexStatus(s);
      setScaleIndexStatus(s.scale_index);
      if (!shouldResumeScaleIndex(s.scale_index)) setBuildingScaleIndex(false);
    } catch (e) { reportError(e); }
    finally { setCancelingScaleIndex(false); }
  };
  const [kgReviewBusy, setKgReviewBusy] = useState(false);
  const [reviewAllJob, setReviewAllJob] = useState<MergeReviewJob | null>(null);
  const [reviewAllRunning, setReviewAllRunning] = useState(false);
  // 切库/刷新：先查后端预审 job 真相，仍 running 就把进度接回（后端 job 不因前端刷新而停），
  // 否则才清空。避免「后台在跑、前端却显示未运行」。
  useEffect(() => {
    const nb = currentNotebookId;
    if (!nb) { setReviewAllJob(null); setReviewAllRunning(false); return; }
    let cancelled = false;
    fetchMergeReviewJob(nb).then((job) => {
      if (cancelled) return;
      if (shouldResumeReviewAll(job)) { setReviewAllJob(job); setReviewAllRunning(true); }
      else { setReviewAllJob(null); setReviewAllRunning(false); }
    }).catch(() => { if (!cancelled) { setReviewAllJob(null); setReviewAllRunning(false); } });
    return () => { cancelled = true; };
  }, [currentNotebookId]);
  // Mirror the buildingKg poll: while an "全部预审" job runs, poll status every 6s until it
  // leaves "running", with a 20min safety cap. Keying on currentNotebookId (captured as `nb`)
  // means switching notebooks or unmounting cancels this poll instead of clobbering the
  // now-selected notebook's pendingMerges/unifiedKgStatus with stale data.
  useEffect(() => {
    if (!reviewAllRunning || !currentNotebookId) return;
    const nb = currentNotebookId;
    let cancelled = false;
    const poll = window.setInterval(async () => {
      try {
        const job = await fetchMergeReviewJob(nb);
        if (cancelled) return;
        setReviewAllJob(job);
        if (job.status !== "running") {
          window.clearInterval(poll);
          setReviewAllRunning(false);
          const [pend, status] = await Promise.all([
            fetchPendingMerges(nb),
            fetchUnifiedKgStatus(nb),
          ]);
          if (cancelled) return;
          setPendingMerges(pend);
          setUnifiedKgStatus(status);
          setToast(job.status === "failed"
            ? `全部预审中止：${job.error || "未知错误"}（已处理 ${job.done}）`
            : `全部预审完成：已处理 ${job.done} 项`);
        }
      } catch { /* transient error; keep polling */ }
    }, 6000);
    const cap = window.setTimeout(() => {
      if (!cancelled) { setReviewAllRunning(false); setToast("全部预审仍在后台进行，请稍后查看"); }
    }, 20 * 60 * 1000);
    return () => { cancelled = true; window.clearInterval(poll); window.clearTimeout(cap); };
  }, [reviewAllRunning, currentNotebookId]);
  const [selectedKgNodeId, setSelectedKgNodeId] = useState<string | null>(null);
  const [pendingKgFocusId, setPendingKgFocusId] = useState<string | null>(null);
  const [conceptDetail, setConceptDetail] = useState<ConceptDetailResp | null>(null);
  const [nodeCtx, setNodeCtx] = useState<NodeContext | null>(null);
  const [kgSearch, setKgSearch] = useState("");
  const [kgSelectedTypes, setKgSelectedTypes] = useState<string[]>([]);
  const [kgSize, setKgSize] = useState({ width: 720, height: 560 });
  const [highlightedElementId, setHighlightedElementId] = useState("");
  const pollCountRef = useRef(0);
  const sourcesRef = useRef<SourceSummary[]>([]);
  const chatBodyRef = useRef<HTMLDivElement | null>(null);
  const askAbortRef = useRef<AbortController | null>(null);
  const askJobIdRef = useRef<string | null>(null);
  // started 事件落地前(jobId 尚未知晓)点了「停止」:先记下意图,onStart 拿到 jobId 后立即补打 cancel。
  const cancelRequestedRef = useRef(false);
  // 重开会话接回在途 ask job:reconnectJob 驱动轮询 effect,seen 记已渲染步数(防重复追加);
  // reconnectConvIdRef 记正在重连的会话 id,供轮询跑完后 openSession 重载拿最终答案。
  const [reconnectJob, setReconnectJob] = useState<{ jobId: string; seen: number } | null>(null);
  const reconnectConvIdRef = useRef<string | null>(null);
  // 重连轮询:重开一个仍在跑的会话时,每 1.5s 拉 ask_job 详情,增量追加轨迹;
  // 跑完则重载会话显示最终答案,取消/中断则提示并收起在途 turn。
  useEffect(() => {
    if (!reconnectJob || !currentNotebookId) return;
    const nb = currentNotebookId;
    const jobId = reconnectJob.jobId;
    let cancelled = false;
    let seen = reconnectJob.seen;
    const poll = window.setInterval(async () => {
      try {
        const d = await fetchAskJob(nb, jobId);
        if (cancelled) return;
        const fresh = newTraceSteps(d.trace ?? [], seen);
        if (fresh.length) { seen += fresh.length; setPendingTrace((prev) => [...prev, ...fresh]); }
        if (jobPollDone(d.status)) {
          window.clearInterval(poll);
          setReconnectJob(null);
          setAsking(false);
          setPendingQuestion(""); setPendingMode(DEFAULT_ASK_MODE); setPendingTrace([]);
          askJobIdRef.current = null;
          if (d.status === "done") {
            await openSession(reconnectConvIdRef.current ?? "");   // 见注: 重载拿最终答案
          } else if (d.status === "cancelled") {
            setToast("该问答已被取消");
            await loadSessions(nb);   // 后端对取消的首轮会话做了空会话清理,刷新列表去掉幽灵项
          } else {
            setToast(d.status === "interrupted" ? "该问答因服务重启中断" : `该问答失败：${d.error || "未知错误"}`);
            await loadSessions(nb);   // 同上:失败/中断的首轮会话也可能已被后端清理
          }
        }
      } catch { /* transient; keep polling */ }
    }, 1500);
    const cap = window.setTimeout(() => {
      if (!cancelled) { window.clearInterval(poll); setReconnectJob(null); setAsking(false);
        setPendingQuestion(""); setPendingTrace([]); askJobIdRef.current = null;
        setToast("该问答仍在后台进行，请稍后重开查看"); }
    }, 20 * 60 * 1000);
    return () => { cancelled = true; window.clearInterval(poll); window.clearTimeout(cap); };
  }, [reconnectJob, currentNotebookId]);
  const notebookMenuRef = useRef<HTMLDivElement | null>(null);
  const accountMenuRef = useRef<HTMLDivElement | null>(null);
  const sessionPopoverRef = useRef<HTMLDivElement | null>(null);
  const kgCanvasRef = useRef<HTMLDivElement | null>(null);
  const kgDetailRef = useRef<HTMLElement | null>(null);
  const kgGraphRef = useRef<any>(null);
  // 收到分享的 token 缓存 —— 挂载时从 URL 抓到后立即清掉 ?share,故拷贝时从这里取。
  const shareTokenRef = useRef<string | null>(null);

  useEffect(() => {
    if (!getToken()) { setAuthChecked(true); return; }
    fetchMe()
      .then((u) => { setCurrentUser(u); return loadNotebookCollection(); })
      .catch(() => { clearToken(); })
      .finally(() => setAuthChecked(true));
  }, []);

  // 接收分享:挂载时读 ?share=shr-xxx,先清掉参数(避免刷新重弹),再预览打开弹窗。
  // 预览需登录(Bearer),故等 authChecked + 有 token 再拉。
  useEffect(() => {
    if (!authChecked || !getToken()) return;
    const token = parseShareToken(window.location.search);
    if (!token) return;
    shareTokenRef.current = token;
    // 立即清掉 ?share,保留其余 query 与 hash(避免刷新重复弹窗)。
    const cleaned = window.location.search
      .replace(/^\?/, "")
      .split("&")
      .filter((p) => p && p.split("=")[0] !== "share")
      .join("&");
    window.history.replaceState(
      null,
      "",
      window.location.pathname + (cleaned ? `?${cleaned}` : "") + window.location.hash
    );
    previewShared(token)
      .then((preview) => setSharedPreview(preview))
      .catch(() => setToast("分享链接无效或已取消"));
  }, [authChecked]);

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

  useEffect(() => {
    if (!accountMenuOpen) return;

    function closeAccountMenu() {
      setAccountMenuOpen(false);
    }

    function handlePointerDown(event: PointerEvent) {
      const target = event.target;
      if (
        target instanceof Node &&
        accountMenuRef.current?.contains(target)
      ) {
        return;
      }
      closeAccountMenu();
    }

    function handleKeyDown(event: globalThis.KeyboardEvent) {
      if (event.key === "Escape") closeAccountMenu();
    }

    window.addEventListener("pointerdown", handlePointerDown);
    window.addEventListener("keydown", handleKeyDown);
    window.addEventListener("resize", closeAccountMenu);
    return () => {
      window.removeEventListener("pointerdown", handlePointerDown);
      window.removeEventListener("keydown", handleKeyDown);
      window.removeEventListener("resize", closeAccountMenu);
    };
  }, [accountMenuOpen]);

  // 会话历史面板:点面板外部(或按 Esc)关闭。切换按钮(会话/历史/当前会话)排除在外——
  // 交给按钮自己的 onClick 切换,否则 pointerdown 先关、click 再开会「关了又开」。
  useEffect(() => {
    if (!sessionPanelOpen) return;

    function closePanel() {
      setSessionPanelOpen(false);
    }

    function handlePointerDown(event: PointerEvent) {
      const target = event.target;
      if (!(target instanceof Node)) return;
      if (sessionPopoverRef.current?.contains(target)) return;
      if (target instanceof Element &&
          target.closest(".chat-session-toggle, .chat-current-session")) {
        return;
      }
      closePanel();
    }

    function handleKeyDown(event: globalThis.KeyboardEvent) {
      if (event.key === "Escape") closePanel();
    }

    window.addEventListener("pointerdown", handlePointerDown);
    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.removeEventListener("pointerdown", handlePointerDown);
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [sessionPanelOpen]);

  // Keep a live ref of `sources` so the poll loop below reads the latest without
  // re-subscribing (its effect is keyed on the boolean `hasPending`, not the array).
  sourcesRef.current = sources;
  const hasPending = sources.some(
    (source) => !["extracted", "failed"].includes(source.parse_status)
  );

  // Poll non-terminal sources so the UI reflects queued→parsing→…→extracted live.
  // Keyed on the boolean `hasPending` (not the whole `sources` array) and
  // self-scheduling with backoff, so a stuck/slow source does NOT re-run this
  // effect — nor re-render the whole app — on every tick. setSources returns the
  // previous array unchanged when no parse_status actually moved, so an unchanged
  // poll costs zero re-renders (the old code always built a new array → the KB
  // list re-rendered every 1.5s for up to 3min, which is what made it "卡").
  useEffect(() => {
    if (!currentNotebookId || !hasPending) {
      pollCountRef.current = 0;
      return;
    }
    let cancelled = false;
    let timer: number | undefined;
    let delay = 1500;
    // Immediate feedback (the first fetch is one `delay` away).
    const first = sourcesRef.current.filter((s) => !["extracted", "failed"].includes(s.parse_status));
    if (first.length) {
      setStatusText(`处理中（已 ${Math.round((pollCountRef.current * 1500) / 1000)}s）：${first.map((s) => `${s.file_name || s.title}: ${s.parse_status}`).join("，")}`);
    }
    const tick = async () => {
      if (cancelled) return;
      const pending = sourcesRef.current.filter(
        (source) => !["extracted", "failed"].includes(source.parse_status)
      );
      if (pending.length === 0) {
        pollCountRef.current = 0;
        return; // done — nothing to poll
      }
      if (pollCountRef.current > 120) {
        setStatusText("处理超时：来源长时间未完成，请查看后端日志 .local/logs/events.jsonl");
        return; // ~3min safety cap
      }
      pollCountRef.current += 1;
      const elapsedSec = Math.round((pollCountRef.current * 1500) / 1000);
      setStatusText(`处理中（已 ${elapsedSec}s）：${pending.map((s) => `${s.file_name || s.title}: ${s.parse_status}`).join("，")}`);
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
        let changed = false;
        setSources((previous) => {
          const next = previous.map((source) => {
            const item = updated.find((u) => u.id === source.id);
            if (item && item.parse_status !== source.parse_status) changed = true;
            return item ?? source;
          });
          return changed ? next : previous; // no re-render when nothing moved
        });
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
      if (!cancelled) {
        delay = Math.min(Math.round(delay * 1.5), 15000); // backoff 1.5s→…→15s
        timer = window.setTimeout(tick, delay);
      }
    };
    timer = window.setTimeout(tick, delay);
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [currentNotebookId, hasPending]);

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
  const kgAvailable = !!(currentNotebook?.kg_ready || currentNotebook?.base_kg_available);
  const currentSession = useMemo(
    () => sessions.find((session) => session.id === conversationId) ?? null,
    [conversationId, sessions],
  );

  // 合并视图：核心子图 + 用户展开的邻居节点/边（去重）。搜索时改用服务端命中叠加层。
  const uGraphMerged = useMemo((): UnifiedGraphResp | null => {
    if (!uGraph) return null;
    if (kgExpandedNodes.length === 0 && kgExpandedEdges.length === 0) return uGraph;
    const existingNodeIds = new Set(uGraph.nodes.map((n) => n.id));
    const existingEdgeKeys = new Set(uGraph.edges.map((e) => `${e.source_object_id}→${e.target_object_id}→${e.edge_type}`));
    const newNodes = kgExpandedNodes.filter((n) => !existingNodeIds.has(n.id));
    const newEdges = kgExpandedEdges.filter((e) => !existingEdgeKeys.has(`${e.source_object_id}→${e.target_object_id}→${e.edge_type}`));
    return { ...uGraph, nodes: [...uGraph.nodes, ...newNodes], edges: [...uGraph.edges, ...newEdges] };
  }, [uGraph, kgExpandedNodes, kgExpandedEdges]);

  const fgData = useMemo(() => {
    const q = kgSearch.trim();
    if (!uGraphMerged) return { nodes: [] as FgNode[], links: [] as FgLink[], searchHitCount: 0 };

    // 搜索模式：服务端返回的命中节点 + 核心图/展开图中对应节点的已知边。
    if (q && kgSearchHits.length > 0) {
      const deg: Record<string, number> = {};
      uGraphMerged.edges.forEach((e) => { deg[e.source_object_id] = (deg[e.source_object_id] ?? 0) + 1; deg[e.target_object_id] = (deg[e.target_object_id] ?? 0) + 1; });
      const filtered = kgSearchHits.filter((h) => kgSelectedTypes.length === 0 || kgSelectedTypes.includes(h.object_type));
      const nodes: FgNode[] = filtered.map((h) => {
        const degree = deg[h.object_id] ?? 0;
        return { id: h.object_id, name: h.name, type: h.object_type, val: 5 + Math.min(18, degree), degree };
      });
      // 命中节点之间已知的边也渲染出来。
      const keep = new Set(nodes.map((n) => n.id));
      const links: FgLink[] = uGraphMerged.edges
        .filter((e) => keep.has(e.source_object_id) && keep.has(e.target_object_id))
        .map((e) => ({ source: e.source_object_id, target: e.target_object_id, label: e.edge_type, sourceCount: e.source_count }));
      return { nodes, links, searchHitCount: nodes.length };
    }

    // 搜索框有字但还没拿到结果（loading 中）—— 显示空图占位。
    if (q) return { nodes: [] as FgNode[], links: [] as FgLink[], searchHitCount: 0 };

    // 非搜索模式：渲染合并图（核心 + 展开邻居），支持类型过滤。
    const deg: Record<string, number> = {};
    uGraphMerged.edges.forEach((e) => { deg[e.source_object_id] = (deg[e.source_object_id] ?? 0) + 1; deg[e.target_object_id] = (deg[e.target_object_id] ?? 0) + 1; });
    const nodes: FgNode[] = uGraphMerged.nodes
      .filter((n) => kgSelectedTypes.length === 0 || kgSelectedTypes.includes(n.object_type))
      .map((n) => {
        const degree = deg[n.id] ?? 0;
        return { id: n.id, name: kgNodeName(n), type: n.object_type, val: 5 + Math.min(18, degree), degree };
      });
    const keep = new Set(nodes.map((n) => n.id));
    const links: FgLink[] = uGraphMerged.edges
      .filter((e) => keep.has(e.source_object_id) && keep.has(e.target_object_id))
      .map((e) => ({ source: e.source_object_id, target: e.target_object_id, label: e.edge_type, sourceCount: e.source_count }));
    return { nodes, links, searchHitCount: 0 };
  }, [uGraphMerged, kgSearch, kgSearchHits, kgSelectedTypes]);

  const kgSearching = kgSearch.trim().length > 0;
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
    if (!uGraphMerged || !selectedKgNodeId) return null;
    // 也在搜索命中中查找（搜索结果节点可能还未进入 uGraphMerged）
    const fromGraph = uGraphMerged.nodes.find((node) => node.id === selectedKgNodeId);
    if (fromGraph) return fromGraph;
    const hit = kgSearchHits.find((h) => h.object_id === selectedKgNodeId);
    if (hit) return { id: hit.object_id, object_type: hit.object_type, payload: { name: hit.name } } satisfies UnifiedConceptNode;
    return null;
  }, [selectedKgNodeId, uGraphMerged, kgSearchHits]);

  const selectedKgEdges = useMemo(() => {
    if (!uGraphMerged || !selectedKgNodeId) return [];
    const nodeById = new Map(uGraphMerged.nodes.map((node) => [node.id, node]));
    return uGraphMerged.edges
      .filter((edge) => edge.source_object_id === selectedKgNodeId || edge.target_object_id === selectedKgNodeId)
      .map((edge) => ({
        ...edge,
        sourceName: kgNodeName(nodeById.get(edge.source_object_id) ?? { id: edge.source_object_id, object_type: "", payload: { name: edge.source_object_id } }),
        sourceType: nodeById.get(edge.source_object_id)?.object_type ?? "",
        targetName: kgNodeName(nodeById.get(edge.target_object_id) ?? { id: edge.target_object_id, object_type: "", payload: { name: edge.target_object_id } }),
        targetType: nodeById.get(edge.target_object_id)?.object_type ?? ""
      }));
  }, [selectedKgNodeId, uGraphMerged]);

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

  async function loadSourcesPage(notebookId: string, opts: { page?: number; q?: string } = {}) {
    const pageNum = opts.page ?? 0;
    const q = opts.q ?? sourceQuery;
    const offset = pageNum * SOURCES_PAGE_SIZE;
    const result = await api<PaginatedSources>(
      `/notebooks/${notebookId}/sources?offset=${offset}&limit=${SOURCES_PAGE_SIZE}&q=${encodeURIComponent(q)}`,
    );
    setSourcesTotal(result.total_count);
    setSources(result.items);
    setSourcesPage(pageNum);
  }

  async function openNotebook(notebookId: string) {
    const [notebook, sourcesPage] = await Promise.all([
      api<NotebookSummary>(`/notebooks/${notebookId}`),
      api<PaginatedSources>(`/notebooks/${notebookId}/sources?offset=0&limit=${SOURCES_PAGE_SIZE}`)
    ]);
    setCurrentNotebookId(notebookId);
    setCurrentNotebook(notebook);
    setTitleDraft(notebook.name);
    setSources(sourcesPage.items);
    setSourcesTotal(sourcesPage.total_count);
    setSourcesPage(0);
    setSourceQuery("");
    setBuildingKg(shouldResumeKgBuild(notebook));
    setTurns([]);
    setConversationId(null);
    setAsking(false);
    setPendingQuestion("");
    setReconnectJob(null);
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

  // --- Pending center: precise deep-link per item type --------------------
  async function openPendingItem(item: PendingItem) {
    await openNotebook(item.notebook_id);
    if (item.type === "report_outline") {
      switchChatMode("reports");
      if (item.report_id) setPendingReportFocusId(item.report_id);
    } else if (item.type === "governance") {
      if (item.subtype === "edge") { await openEdgeReviewQueue(); }
      else if (item.subtype === "promotion") { await openPromoQueue(); }
      else { setKgViewOpen(true); }  // merge → KG 图谱视图内联「待确认合并」
    } else if (item.type === "index") {
      setKgViewOpen(true);  // 索引状态/重建入口在 KG 视图
    }
  }
  function openDoneItem(d: { notebook_id: string }) {
    openNotebook(d.notebook_id).then(() => setKgViewOpen(true)).catch(reportError);
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
    setTitleDraft("");
    setTurns([]);
    setConversationId(null);
    setSessions([]);
    setAsking(false);
    setPendingQuestion("");
    setPendingMode(DEFAULT_ASK_MODE);
    setPendingTrace([]);
    setReconnectJob(null);
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
    const all = Array.from(event.target.files || []);
    event.target.value = "";
    const picked = all.filter((file) => SUPPORTED_SOURCE_EXTENSIONS.includes(fileExtension(file.name)));
    const rejected = all.filter((file) => !SUPPORTED_SOURCE_EXTENSIONS.includes(fileExtension(file.name)));
    if (rejected.length > 0) {
      const names = rejected.map((file) => file.name).join("、");
      const hasLegacy = rejected.some((file) => LEGACY_OFFICE_EXTENSIONS.includes(fileExtension(file.name)));
      const hint = hasLegacy
        ? "旧版 Office 格式请另存为 .docx / .pptx / .xlsx"
        : `支持：${SUPPORTED_SOURCE_USER_HINT}`;
      setToast(`已跳过不支持的文件：${names}。${hint}`);
    }
    if (picked.length === 0) {
      return;
    }
    // 追加而非覆盖（"继续添加文件"语义）；按 name+size 去重，避免重复入列。
    const merged = [...stagedFiles];
    const mergedTypes = [...stagedDocTypes];
    const added: File[] = [];
    for (const file of picked) {
      if (!merged.some((existing) => existing.name === file.name && existing.size === file.size)) {
        merged.push(file);
        mergedTypes.push("");
        added.push(file);
      }
    }
    setStagedFiles(merged);
    setStagedDocTypes(mergedTypes);
    setSourceModalOpen(true);
    // 对新增的文本类文件做内容检测，预填类型下拉（异步，不阻塞 UI；用户仍可改）。
    void detectStagedTypes(added, merged);
  }

  // 读文本类文件前 8KB → 批量调 /detect-doc-types → 回填仍为空（未手动选）的类型。
  async function detectStagedTypes(added: File[], fullList: File[]) {
    const textFiles = added.filter((file) => /\.(md|markdown|csv|txt)$/i.test(file.name));
    if (textFiles.length === 0) return;
    try {
      const items = await Promise.all(
        textFiles.map(async (file) => ({ name: file.name, sample: await file.slice(0, 8000).text() }))
      );
      const results = await api<Array<{ name: string; doc_type_id: string }>>("/detect-doc-types", {
        method: "POST",
        body: JSON.stringify({ items })
      });
      const byName: Record<string, string> = {};
      results.forEach((r) => { if (r.doc_type_id) byName[r.name] = r.doc_type_id; });
      if (Object.keys(byName).length === 0) return;
      // 按 fullList 的位置回填；只填仍为空的项，不覆盖用户已手动选择的。
      setStagedDocTypes((prev) => prev.map((dt, i) => (dt ? dt : (byName[fullList[i]?.name] ?? dt))));
    } catch {
      // 检测失败不影响上传：保持"自动检测"，用户可手动选。
    }
  }

  function setStagedDocType(index: number, value: string) {
    setStagedDocTypes((prev) => prev.map((dt, i) => (i === index ? value : dt)));
  }

  function setAllStagedDocTypes(value: string) {
    setStagedDocTypes((prev) => prev.map(() => value));
  }

  function removeStagedFile(index: number) {
    setStagedFiles((prev) => prev.filter((_, i) => i !== index));
    setStagedDocTypes((prev) => prev.filter((_, i) => i !== index));
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
    setSourcesTotal((t) => t + uploaded.length);
    await loadNotebookCollection();
    setStagedFiles([]);
    setStagedDocTypes([]);
    setSourceModalOpen(false);
    setToast(`已上传 ${uploaded.length} 个来源`);
  }

  async function submitUrlSources() {
    if (!currentNotebookId) return;
    const urls = parseUrlLines(urlText);
    if (urls.length === 0) {
      setToast("请粘贴至少一个 http/https 链接");
      return;
    }
    setUrlBusy(true);
    setUrlRejected([]);
    try {
      const result = await api<{ created: SourceSummary[]; rejected: Array<{ url: string; reason: string }> }>(
        `/notebooks/${currentNotebookId}/sources/url`,
        { method: "POST", body: JSON.stringify({ urls }) }
      );
      if (result.created.length > 0) {
        setSources((previous) => [
          ...previous.filter((source) => !result.created.some((item) => item.id === source.id)),
          ...result.created,
        ]);
        await loadNotebookCollection();
      }
      setUrlRejected(result.rejected);
      setToast(`已添加 ${result.created.length} 个，被拒 ${result.rejected.length} 个`);
      if (result.rejected.length === 0) {
        setUrlText("");
        setLinkSectionOpen(false);
      }
    } catch (error) {
      reportError(error);
    } finally {
      setUrlBusy(false);
    }
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
    setSourcesTotal((t) => Math.max(0, t - 1));
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

  async function runAsk(nextQuestion = question) {
    if (!currentNotebookId) return;
    if (asking) return;
    const q = nextQuestion.trim();
    if (!q) return;
    if (requiresKg(askMode) && !kgAvailable) {
      setToast("严格推理需先为该 notebook 构建知识图谱");
      return;
    }
    setChatMode("ask");
    setQuestion("");
    setPendingQuestion(q);
    setPendingMode(askMode);
    setPendingTrace([]);
    setAsking(true);
    cancelRequestedRef.current = false;   // 每次新 ask 复位「停止」意图
    const controller = new AbortController();
    askAbortRef.current = controller;
    try {
      const payload = { question: q, conversation_id: conversationId ?? undefined, mode: askMode };
      const response = await readAskStream<AskResponse>(
        `/notebooks/${currentNotebookId}/ask/stream`,
        payload,
        (step) => setPendingTrace((previous) => [...previous, step]),
        controller.signal,
        (jobId) => {
          askJobIdRef.current = jobId;
          if (cancelRequestedRef.current) {
            // started 落地前已点过「停止」:jobId 当时还不存在,补打 cancel 端点真取消 worker。
            cancelRequestedRef.current = false;
            api(`/notebooks/${currentNotebookId}/ask/jobs/${jobId}/cancel`, { method: "POST" }).catch(() => {});
          }
        },
      );
      setTurns((prev) => [...prev, { question: q, response }]);
      setConversationId(response.conversation_id);
    } catch (error) {
      setQuestion(q);
      if (isAbortError(error)) {
        setToast("已中断回答");
        return;
      }
      reportError(error);
    } finally {
      if (askAbortRef.current === controller) askAbortRef.current = null;
      askJobIdRef.current = null;
      cancelRequestedRef.current = false;
      setPendingQuestion("");
      setPendingMode(DEFAULT_ASK_MODE);
      setPendingTrace([]);
      setAsking(false);
    }
    await loadSessions(currentNotebookId);
  }

  function abortAsk() {
    const jobId = askJobIdRef.current;
    const nb = currentNotebookId;
    // 显式取消 = 打 cancel 端点(真取消后端 worker),再 abort 本地流(立即停读)。
    // 离开/刷新页面不会走这里,故 worker 会继续跑到完(WS2a 的核心)。
    if (jobId && nb) {
      api(`/notebooks/${nb}/ask/jobs/${jobId}/cancel`, { method: "POST" }).catch(() => {});
    } else {
      // started 事件还没落地,jobId 未知:记下意图,onStart 拿到 jobId 后补打 cancel(见 runAsk)。
      cancelRequestedRef.current = true;
    }
    askAbortRef.current?.abort();
  }

  function handleAskInputKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      runAsk().catch(reportError);
    }
  }

  async function loadSessions(notebookId: string | null = currentNotebookId) {
    if (!notebookId) return;
    const list = await api<ConversationSummary[]>(`/notebooks/${notebookId}/conversations`);
    setSessions(list);
  }

  async function openSession(id: string) {
    const detail = await api<ConversationDetail>(`/conversations/${id}`);
    setTurns(detail.turns.map((turn) => ({ question: turn.question, response: turn.response })));
    setAskMode(modeFromTurn(detail.turns[detail.turns.length - 1]));
    setConversationId(id);
    setPendingQuestion("");
    setPendingMode(DEFAULT_ASK_MODE);
    setPendingTrace([]);
    setChatMode("ask");
    setSessionPanelOpen(false);
    setRenamingSessionId(null);
    const active = detail.active_job;
    if (active) {
      // 把在途 turn 渲染成「生成中」并接回实时轨迹(仿正在 ask 的 UI)。
      setPendingQuestion(active.question);
      setPendingMode(modeFromTurn({ response: { mode: active.mode } }));
      setPendingTrace(active.trace ?? []);
      setAsking(true);
      askJobIdRef.current = active.job_id;                  // 「停止」可作用于重连的 job
      reconnectConvIdRef.current = id;
      setReconnectJob({ jobId: active.job_id, seen: (active.trace ?? []).length });
    } else {
      setReconnectJob(null);
    }
  }

  function startNewSession() {
    setTurns([]);
    setConversationId(null);
    setAskMode(DEFAULT_ASK_MODE);
    setPendingQuestion("");
    setPendingMode(DEFAULT_ASK_MODE);
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
      setPendingMode(DEFAULT_ASK_MODE);
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

  function requestBulkCleanup(days: number) {
    const victims = conversationsOlderThan(sessions, days);
    if (victims.length === 0) return;
    setInfoModal({
      title: "批量清理会话",
      message: `将删除 ${victims.length} 条最近 ${days} 天内无活动的会话，对应的历史问答会一起移除。`,
      actions: [
        { label: "取消", action: () => undefined },
        { label: "删除", danger: true, action: () => { bulkCleanup(days, victims).catch(reportError); } },
      ],
    });
  }

  async function bulkCleanup(days: number, victims: ConversationSummary[]) {
    const { deleted } = await api<{ deleted: number }>(
      `/notebooks/${currentNotebookId}/conversations?older_than_days=${days}`,
      { method: "DELETE" },
    );
    if (conversationId && victims.some((s) => s.id === conversationId)) {
      setTurns([]);
      setConversationId(null);
      setPendingQuestion("");
      setPendingMode(DEFAULT_ASK_MODE);
      setPendingTrace([]);
    }
    await loadSessions(currentNotebookId);
    setToast(`已删除 ${deleted} 条会话`);
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


  async function loadKnowledge(kind: KnowledgeKind, opts: { status?: string; page?: number } = {}) {
    if (!currentNotebookId) return;
    const pageNum = opts.page ?? 0;
    const statusParam = (opts.status && opts.status !== "all") ? opts.status : "";
    const result = await api<PaginatedKnowledge>(
      `/notebooks/${currentNotebookId}/knowledge?type=${encodeURIComponent(kind)}&status=${encodeURIComponent(statusParam)}&offset=${pageNum * 50}&limit=50`
    );
    const items: KnowledgeItem[] = result.items.map((record) => ({
      id: record.id,
      status: record.status,
      owner: record.owner,
      last_reviewed: record.last_reviewed,
      evidence: record.evidence,
      headline: record.headline,
      object_type: record.object_type,
      fields: record.fields
    }));
    setKnowledge((prev) => ({ ...prev, [kind]: items }));
    setKnowledgeTotal((prev) => ({ ...prev, [kind]: result.total_count }));
    setKnowledgePage((prev) => ({ ...prev, [kind]: pageNum }));
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
    await loadKnowledge(knowledgeKind, { status: knowledgeStatusFilter, page: 0 });
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
    loadKnowledge(kind, { status: "all", page: 0 }).catch(reportError);
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
    await loadKnowledge(knowledgeKind, { status: knowledgeStatusFilter, page: 0 });
    await loadKnowledgeTypes();
    await findDuplicates(knowledgeKind);
    setToast("已合并，源条目置为 deprecated");
  }


  async function openAnalytics() {
    if (!currentNotebookId) return;
    const nb = currentNotebookId;
    const response = await api<NotebookAnalytics>(`/notebooks/${nb}/analytics`);
    setAnalytics(response);
    // 看板打开时经聚合端点一次拉齐三系统(kg/概念合并/检索索引)当前态(而非上次切库的
    // 快照),取代原先仅单独拉检索索引状态;顺带回填 scaleIndexStatus(供既有
    // runScaleIndexOp 等复用)与忙碌位(供既有轮询接回跨会话发起的构建)。
    fetchIndexStatus(nb).then((s) => {
      setIndexStatus(s);
      setScaleIndexStatus(s.scale_index);
      if (s.kg.building) setBuildingKg(true);
      if (shouldResumeScaleIndex(s.scale_index)) setBuildingScaleIndex(true);
    }).catch(() => {});
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
    setKgSearch(""); setKgSearchHits([]); setKgSearchBusy(false);
    setKgExpandedNodes([]); setKgExpandedEdges([]);
    setKgSelectedTypes([]);
    setKgLimit(KG_RANGE_DEFAULT);                 // 每次打开从核心范围起，避免一上来渲染全量
    setPendingKgFocusId(targetNodeId ?? null);
    try {
      const [g, pend, status] = await Promise.all([
        fetchUnifiedGraph(currentNotebookId, KG_RANGE_DEFAULT),
        fetchPendingMerges(currentNotebookId),
        fetchUnifiedKgStatus(currentNotebookId),
      ]);
      setUGraph(g); setPendingMerges(pend); setUnifiedKgStatus(status);
      setVizBuilding(Boolean(g.viz_building));
    } catch (err) { reportError(err); }
  }

  // 防抖定时器 ref — 搜索词变化后延迟 300ms 再发请求。
  const kgSearchTimerRef = useRef<number | null>(null);

  // 服务端搜索：输入词变化时防抖触发 /kg/search；清空词时还原为核心子图。
  function handleKgSearchChange(value: string) {
    setKgSearch(value);
    if (kgSearchTimerRef.current !== null) clearTimeout(kgSearchTimerRef.current);
    if (!value.trim()) { setKgSearchHits([]); setKgSearchBusy(false); return; }
    setKgSearchBusy(true);
    kgSearchTimerRef.current = window.setTimeout(async () => {
      if (!currentNotebookId) { setKgSearchBusy(false); return; }
      try {
        const resp = await fetchKgSearch(currentNotebookId, value.trim());
        setKgSearchHits(resp.hits);
      } catch (err) { reportError(err); setKgSearchHits([]); }
      finally { setKgSearchBusy(false); }
    }, 300);
  }

  // 切换图谱范围档位：按新 limit 重拉子图（核心 N / 全部）。
  async function changeKgRange(limit: number) {
    if (!currentNotebookId) return;
    setKgLimit(limit);
    setKgRangeBusy(true);
    try {
      const g = await fetchUnifiedGraph(currentNotebookId, limit);
      setUGraph(g);
      setVizBuilding(Boolean(g.viz_building));
    } catch (err) { reportError(err); }
    finally { setKgRangeBusy(false); }
  }

  // KG 视图内补连孤立节点：同步返回，完成后按当前范围重拉图谱与状态。
  async function relinkFromKgView() {
    if (!currentNotebookId) return;
    setRelinkingKg(true);
    try {
      const r = await relinkKg(currentNotebookId);
      setToast(`已补连 ${r.edges_added} 条边，剩余孤立节点 ${r.isolated_after}`);
      const [g, status] = await Promise.all([
        fetchUnifiedGraph(currentNotebookId, kgLimit),
        fetchUnifiedKgStatus(currentNotebookId),
      ]);
      setUGraph(g); setKgExpandedNodes([]); setKgExpandedEdges([]); setUnifiedKgStatus(status);
      setVizBuilding(Boolean(g.viz_building));
    } catch (err) { reportError(err); }
    finally { setRelinkingKg(false); }
  }

  async function refreshUnifiedKg() {
    if (!currentNotebookId) return;
    setKgRefreshBusy(true);
    try {
      await rebuildUnifiedKg(currentNotebookId);
      const [g, pend, status] = await Promise.all([
        fetchUnifiedGraph(currentNotebookId, kgLimit),
        fetchPendingMerges(currentNotebookId),
        fetchUnifiedKgStatus(currentNotebookId),
      ]);
      setUGraph(g); setKgExpandedNodes([]); setKgExpandedEdges([]); setPendingMerges(pend); setUnifiedKgStatus(status);
      setVizBuilding(Boolean(g.viz_building));
    } catch (err) { reportError(err); }
    finally { setKgRefreshBusy(false); }
  }

  // 「重新合并」唯一入口(看板「索引与构建」面板 + 知识图谱视图共用):先统一确认再重建。
  function confirmRefreshUnifiedKg() {
    if (kgRefreshBusy || buildingKg) return;
    confirmIndexAction("重新合并知识图谱？\n\n将重算跨文档概念聚类并刷新图谱索引（不重新抽取来源）。后台进行，完成后自动更新。", () => refreshUnifiedKg());
  }

  async function reviewPendingMerges() {
    if (!currentNotebookId) return;
    setKgReviewBusy(true);
    setToast("正在 LLM 预审（约 1 分钟，请稍候）…");
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

  async function reviewAllMerges() {
    if (!currentNotebookId) return;
    const nb = currentNotebookId;
    try {
      await reviewAllMergesApi(nb);
      setReviewAllJob({ status: "running", total: pendingMerges.length, done: 0, error: "" });
      setReviewAllRunning(true);
    } catch (err) { reportError(err); }
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
    // 逐跳展开：拉取邻居节点/边并合并进视图（去重由 uGraphMerged 处理）。
    try {
      const neighbors = await fetchKgNeighbors(currentNotebookId, nodeId);
      if (neighbors.nodes.length > 0 || neighbors.edges.length > 0) {
        setKgExpandedNodes((prev) => {
          const existing = new Set(prev.map((n) => n.id));
          const fresh = neighbors.nodes.filter((n) => !existing.has(n.id));
          return fresh.length > 0 ? [...prev, ...fresh] : prev;
        });
        setKgExpandedEdges((prev) => {
          const existing = new Set(prev.map((e) => `${e.source_object_id}→${e.target_object_id}→${e.edge_type}`));
          const fresh = neighbors.edges.filter((e) => !existing.has(`${e.source_object_id}→${e.target_object_id}→${e.edge_type}`));
          return fresh.length > 0 ? [...prev, ...fresh] : prev;
        });
      }
    } catch { /* 邻居展开 best-effort，不阻断主流程 */ }
    const node = uGraphMerged?.nodes.find((item) => item.id === nodeId);
    if (node?.object_type !== "concept") setConceptDetail(null);
    else { try { setConceptDetail(await fetchConceptDetail(currentNotebookId, nodeId)); } catch (err) { setConceptDetail(null); reportError(err); } }
    try { setNodeCtx(await fetchNodeContext(currentNotebookId, nodeId)); } catch { /* node context best-effort */ }
  }

  async function decideMerge(candidate: PendingMerge, confirm: boolean) {
    if (!currentNotebookId) return;
    try {
      const decidedPairKey = mergePairKey(candidate);
      if (confirm) await confirmMergeApi(currentNotebookId, candidate.id);
      else await rejectMergeApi(currentNotebookId, candidate.id);
      setPendingMerges((items) =>
        items.filter((item) => item.id !== candidate.id && mergePairKey(item) !== decidedPairKey)
      );
      await rebuildUnifiedKg(currentNotebookId);
      const [g, pend] = await Promise.all([fetchUnifiedGraph(currentNotebookId, kgLimit), fetchPendingMerges(currentNotebookId)]);
      setUGraph(g); setKgExpandedNodes([]); setKgExpandedEdges([]);
      setPendingMerges(
        pend.filter((item) => item.id !== candidate.id && mergePairKey(item) !== decidedPairKey)
      );
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

  // --- Two-tier federation: mark notebook base / personal -----------------
  async function handleTierAction() {
    if (!currentNotebook) return;
    const state = tierActionState(currentNotebook, notebooks);
    if (state.action === "replace") {
      const ok = window.confirm(
        `当前基准库是「${state.otherBaseName}」。基准库全局唯一 —— 替换为「${currentNotebook.name}」？`
      );
      if (!ok) return;
    }
    const target = state.action === "unset" ? "personal" : "base";
    const updated = await setNotebookTier(currentNotebook.id, target);
    setCurrentNotebook(updated as NotebookSummary);
    await loadNotebookCollection();
    setToast(
      target === "base"
        ? "已设为基准库 — 该知识库将作为全局唯一的权威参考层参与检索与冲突仲裁"
        : "已取消基准库，恢复为个人层"
    );
  }

  // --- 分享与拷贝(Phase 1) --------------------------------------------
  // A. 分享(owner 侧):开启分享 → 拿 token → 打开分享弹窗
  async function openShareModal() {
    if (!currentNotebook) return;
    setShareBusy(true);
    try {
      const result = await shareNotebook(currentNotebook.id);
      setShareModal(result);
    } finally {
      setShareBusy(false);
    }
  }

  // 复制分享链接到剪贴板(退化时至少把链接抛到状态栏)
  async function copyShareLink() {
    if (!shareModal) return;
    const link = buildShareLink(shareModal.share_token, window.location.origin);
    try {
      await navigator.clipboard?.writeText(link);
      setToast("分享链接已复制");
    } catch {
      setStatusText(link);
      setToast("复制失败，链接已显示在状态栏");
    }
  }

  // 取消分享:撤销 token → 关弹窗
  async function handleUnshare() {
    if (!currentNotebook) return;
    setShareBusy(true);
    try {
      await unshareNotebook(currentNotebook.id);
      setShareModal(null);
      setToast("已取消分享，链接立即失效");
    } finally {
      setShareBusy(false);
    }
  }

  // B. 接收分享(拷贝侧):拷贝分享库到当前用户空间 → 选中新库 → 关弹窗
  async function handleCopyShared(token: string) {
    setCopyBusy(true);
    try {
      const created = await copyShared(token);
      await loadNotebookCollection();
      setCurrentNotebookId(String(created.id));
      setSharedPreview(null);
      setToast("已拷贝到你的空间");
    } finally {
      setCopyBusy(false);
    }
  }

  // --- 只读共享(Phase 2)-----------------------------------------------
  // C. 加入(只读):大库不能拷贝 → 加为只读成员 → 选中该库 → 关弹窗
  async function handleJoinShared(token: string) {
    setCopyBusy(true);
    try {
      const joined = await joinShared(token);
      await loadNotebookCollection();
      setCurrentNotebookId(String(joined.id));
      setSharedPreview(null);
      setToast("已加入只读共享");
    } finally {
      setCopyBusy(false);
    }
  }

  // D. 退出共享(只读成员移除自己):刷新列表;若当前被移除则切到第一个自有库
  async function handleLeaveShared() {
    if (!currentNotebook) return;
    const leftId = currentNotebook.id;
    setLeaveBusy(true);
    try {
      await leaveNotebook(leftId);
      const remaining = await api<NotebookSummary[]>("/notebooks");
      setNotebooks(remaining);
      const stillThere = remaining.some((n) => n.id === leftId);
      if (!stillThere) {
        const firstOwned = remaining.find((n) => (n.access ?? "owner") === "owner");
        if (firstOwned) {
          setCurrentNotebookId(firstOwned.id);
          setCurrentNotebook(firstOwned);
          setTitleDraft(firstOwned.name);
        } else {
          setCurrentNotebookId(null);
          setCurrentNotebook(null);
        }
      }
      setToast("已退出只读共享");
    } finally {
      setLeaveBusy(false);
    }
  }

  // E. owner「已分享总览」:拉取所有我 owner 且已分享的库 → 打开 modal
  async function openSharedByMe() {
    setSharedByMeList(null);
    setSharedByMeOpen(true);
    const items = await sharedByMe();
    setSharedByMeList(items);
  }

  // 总览里「取消分享」:撤销 token(踢全员)→ 重拉总览刷新
  async function handleUnshareFromOverview(notebookId: string) {
    setShareBusy(true);
    try {
      await unshareNotebook(notebookId);
      const items = await sharedByMe();
      setSharedByMeList(items);
      await loadNotebookCollection();
      setToast("已取消分享，链接立即失效");
    } finally {
      setShareBusy(false);
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

  // --- Track E: edge review queue ----------------------------------------
  async function openEdgeReviewQueue() {
    if (!currentNotebookId) return;
    const queue = await fetchEdgeReviewQueue(currentNotebookId);
    setEdgeQueue(queue);
    setEdgeReviewOpen(true);
  }

  async function decideEdge(relId: string, status: "verified" | "rejected") {
    if (!currentNotebookId) return;
    setEdgeBusy(true);
    try {
      await reviewRelation(currentNotebookId, relId, status);
      setToast(status === "verified" ? "关系已确认" : "关系已拒绝，后续图推理将忽略它");
      const queue = await fetchEdgeReviewQueue(currentNotebookId);
      setEdgeQueue(queue);
    } finally {
      setEdgeBusy(false);
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
          loadKnowledge(knowledgeKind, { status: "all", page: 0 }).catch(reportError);
        }
      }).catch(reportError);
    }
  }

  function reportError(error: unknown) {
    const message = error instanceof Error ? error.message : String(error);
    setStatusText(`API error: ${message}`);
  }

  async function handleLogout() {
    setAccountMenuOpen(false);
    await logoutUser();
    setCurrentUser(null);
  }

  const ROLE_LABELS: Record<ModelRole, string> = {
    llm: "主 LLM", reasoning_llm: "推理 LLM", rewrite_llm: "改写 LLM",
    kg_llm: "构图 LLM", rerank: "重排 Rerank",
  };
  // Base URL 示例：只填到服务根地址（/v1），由后端自动拼接具体接口路径。
  // 前四个走 OpenAI 兼容 /chat/completions；rerank 走 DashScope 原生服务路径。
  const BASE_URL_PLACEHOLDERS: Record<ModelRole, string> = {
    llm: "https://api.openai.com/v1",
    reasoning_llm: "https://api.deepseek.com/v1",
    rewrite_llm: "https://api.openai.com/v1",
    kg_llm: "https://api.openai.com/v1",
    rerank: "https://dashscope.aliyuncs.com/api/v1",
  };

  async function openModelPanel() {
    setModelPanelOpen(true);
    try {
      const view = await fetchModelSettings();
      const forms = Object.fromEntries(MODEL_ROLES.map((r) => [r, {
        base_url: view[r].base_url, model: view[r].model,
        api_key: "", keyDirty: false,
      }])) as Record<ModelRole, ServiceForm>;
      setModelForms(forms);
    } catch (e) { reportError(e); }
  }

  async function saveModelPanel() {
    if (!modelForms) return;
    try {
      await saveModelSettings(buildPutPayload(modelForms));
      setModelPanelOpen(false);
      setToast("模型服务配置已保存");
    } catch (e) { reportError(e); }
  }

  async function runModelTest(role: ModelRole) {
    if (!modelForms) return;
    const f = modelForms[role];
    setModelTesting((m) => ({ ...m, [role]: "测试中…" }));
    try {
      const r = await testModelService(role, f.base_url.trim(), f.model.trim(),
        f.keyDirty ? f.api_key : null);
      setModelTesting((m) => ({ ...m, [role]: r.ok ? `通 ${r.latency_ms}ms` : `失败：${r.error}` }));
    } catch (e) {
      setModelTesting((m) => ({ ...m, [role]: "失败" })); reportError(e);
    }
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
  // 只读共享库(Phase 2):无写权,门控写按钮 + 显示只读徽章/退出入口。
  const isReader = currentNotebook?.access === "reader";
  const menuNotebook = menuNotebookId
    ? notebooks.find((item) => item.id === menuNotebookId) ?? null
    : null;

  if (!authChecked) return <div className="auth-gate"><div className="auth-card">加载中…</div></div>;
  if (!currentUser) {
    return <AuthGate onAuthenticated={(u) => {
      setCurrentUser(u);
      setStatusText("");
      loadNotebookCollection().catch(reportError);
    }} />;
  }

  const accountName = currentUser.username;
  const accountRole = currentUser.role === "admin" ? "管理员" : "用户";
  const accountBadge = accountInitials(accountName);

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
        <div className="topbar-right">
          <div className="status"><span className="status-dot" /><span>{statusText}</span></div>
          <PendingBell
            snapshot={pending.snapshot}
            doneItems={pending.doneItems}
            userId={currentUser?.id}
            onOpenItem={(it) => openPendingItem(it).catch(reportError)}
            onOpenDone={openDoneItem}
            onDismissDone={pending.dismissDone}
          />
          <div className="user-menu" ref={accountMenuRef}>
            <button
              className="user-menu-trigger"
              type="button"
              aria-haspopup="menu"
              aria-expanded={accountMenuOpen}
              title="账户菜单"
              onClick={() => setAccountMenuOpen((open) => !open)}
            >
              <span className="user-avatar">{accountBadge}</span>
              <span className="user-name">{accountName}{currentUser.role === "admin" ? "（管理员）" : ""}</span>
              <ChevronDown size={14} className="user-menu-chevron" />
            </button>
            {accountMenuOpen && (
              <div className="user-menu-popover" role="menu" aria-label="账户菜单">
                <div className="user-menu-profile">
                  <span className="user-avatar large">{accountBadge}</span>
                  <div>
                    <strong>{accountName}</strong>
                    <small>{accountRole}</small>
                  </div>
                </div>
                {canSeeAdminUsage(currentUser.role) && (
                  <a className="user-logout" role="menuitem" href="/admin/usage" title="用户使用总览">
                    <BarChart3 size={16} />
                    <span>用户总览</span>
                  </a>
                )}
                <button className="user-logout" type="button" role="menuitem" onClick={() => handleLogout().catch(reportError)}>
                  <LogOut size={16} />
                  <span>退出登录</span>
                </button>
              </div>
            )}
          </div>
        </div>
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
              <button className="sort-button" title="查看我分享出去的知识库及其只读成员" onClick={() => openSharedByMe().catch(reportError)}>
                <Share2 size={15} /> 已分享
              </button>
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
              <div className="workspace-title-main">
                {isReader ? (
                  <div className="tag-row" style={{ alignItems: "center", gap: 8 }}>
                    <h1 className="notebook-title-input" style={{ margin: 0 }}>{currentNotebook.name}</h1>
                    <span className="new-pill" title="只读共享,无写权限">
                      只读 · 来自 {currentNotebook.shared_from || "他人"}
                    </span>
                    <button
                      className="sort-button"
                      disabled={leaveBusy}
                      title="退出该只读共享（仅移除你自己的访问）"
                      onClick={() => handleLeaveShared().catch(reportError)}
                    >
                      {leaveBusy ? "退出中…" : "退出共享"}
                    </button>
                  </div>
                ) : (
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
                )}
              </div>
            </div>
            <div className="workspace-toolbar" aria-label="Notebook actions">
              <button className="workspace-primary-action" onClick={() => openCreate().catch(reportError)}>
                <Plus size={18} strokeWidth={2.8} />
                <span>创建笔记本</span>
              </button>
              <div className="workspace-nav-group">
                {!isReader && (
                  <button className="workspace-nav-button" onClick={() => {
                    const tier = tierActionState(currentNotebook, notebooks);
                    // 基准库名走后端全局字段 base_notebook_name(所有用户只读可见,不依赖它出现在
                    // 自己的库列表里)。弹窗顶部只读展示「当前基准库」,非管理员也能看到是哪个、但改不了。
                    const baseName = currentNotebook?.base_notebook_name || "";
                    setInfoModal({
                    title: "分析",
                    message: "对当前 notebook 的知识图谱与基准库做治理与审查（部分操作仅管理员）。输出在弹窗中呈现。",
                    sections: baseName ? [["当前基准库", [baseName]] as [string, string[]]] : undefined,
                    actions: [
                      ...(currentUser?.role === "admin" ? [{ label: "晋升队列", desc: "审核待晋升进基准库的内容（管理员）", action: () => openPromoQueue().catch(reportError) }] : []),
                      ...(currentUser?.role === "admin" ? [{ label: tier.label, desc: "把当前知识库设为全局唯一的权威参考层，供检索时优先参考（管理员）", action: () => handleTierAction().catch(reportError) }] : []),
                      // 检索索引的立即/空闲时重建已收敛进「看板 → 索引与构建」面板(检索索引行,
                      // 与 tier 解耦、大库亦可建)，此处不再重复列出，避免同一动作多处入口各自确认。
                      { label: "边审查队列", desc: "审核知识图谱中待人工确认的实体关系边", action: () => openEdgeReviewQueue().catch(reportError) }
                    ]
                    });
                  }}>
                    <BarChart3 size={17} />
                    <span>分析</span>
                  </button>
                )}
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
                {!isReader && (
                  <button className="workspace-nav-button" disabled={shareBusy} onClick={() => openShareModal().catch(reportError)}>
                    <Share2 size={17} />
                    <span>分享</span>
                  </button>
                )}
                <button className="workspace-nav-button" onClick={() => openModelPanel()}>
                  <span>模型服务</span>
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

          <section className={`workspace-grid${sourcesCollapsed ? " sources-collapsed" : ""}`}>
            <aside className="workspace-panel sources-panel">
              <div className="workspace-panel-header">
                <h2>Source Stack</h2>
                <div className="sources-header-right">
                  <span className="panel-count">{sourcesTotal} 个来源</span>
                  <button
                    type="button"
                    className="sources-collapse-handle"
                    aria-label="收起来源栏"
                    title="收起来源栏"
                    onClick={() => setSourcesCollapsed(true)}
                  >
                    <PanelLeftClose size={18} />
                  </button>
                </div>
              </div>
              <div className="workspace-panel-body sources-body">
                {!isReader && (
                  <button type="button" className="add-source-button" onClick={() => { setLinkSectionOpen(false); setSourceModalOpen(true); }}>
                    <Plus size={20} strokeWidth={2.7} /> 添加来源
                  </button>
                )}
                {!isReader && currentNotebookId && sources.length > 0 && (
                  currentNotebook?.kg_ready
                    ? (
                      <>
                        {(currentNotebook?.kg_pending_sources ?? 0) > 0
                          ? (
                            <>
                              <button
                                type="button"
                                className="add-source-button"
                                disabled={buildingKg}
                                title="有新增来源尚未入图，点击增量抽取并合并至知识图谱"
                                onClick={() => { if (currentNotebookId) startKgBuild(currentNotebookId); }}
                              >
                                <Network size={20} strokeWidth={2.7} /> {buildingKg ? "补抽中…" : `补抽 ${currentNotebook.kg_pending_sources ?? "?"} 篇新增并合并`}
                              </button>
                              <p className="tool-hint" style={{ margin: "2px 2px 8px" }}>
                                知识图谱已构建 · 有 {currentNotebook.kg_pending_sources ?? "?"} 篇来源未入图
                              </p>
                            </>
                          )
                          : (
                            <p className="tool-hint" style={{ margin: "2px 2px 8px" }}>
                              ✓ 知识图谱已构建 · 可用严格推理（推理 / 图谱）
                            </p>
                          )
                        }
                      </>
                    )
                    : (
                      <>
                        <button
                          type="button"
                          className="add-source-button"
                          disabled={buildingKg}
                          title={currentNotebook?.base_kg_available
                            ? "本库尚未建图，严格推理会借用底层库（base）；点击为本库单独构建知识图谱"
                            : "默认问答（通用）不需要；严格推理（推理 / 图谱）需先构建知识图谱"}
                          onClick={() => { if (currentNotebookId) startKgBuild(currentNotebookId); }}
                        >
                          <Network size={20} strokeWidth={2.7} /> {buildingKg ? "全量构建中…" : "全量构建知识图谱"}
                        </button>
                        <p className="tool-hint" style={{ margin: "2px 2px 8px" }}>
                          {currentNotebook?.base_kg_available
                            ? "本库未建图，严格推理将借用底层库（base）"
                            : "默认问答无需；严格推理（推理 / 图谱）需要先构建"}
                        </p>
                      </>
                    )
                )}
                {scaleIndexStatus && (scaleIndexStatus.eligible || scaleIndexStatus.exists) && (() => {
                  // 检索索引与 tier 解耦：任意达标库（含大个人库）均显示。徽章紧凑,只做主
                  // 动作(构建/更新);全量重建入口在看板卡片。状态语义统一走 describeScaleIndex。
                  const s = scaleIndexStatus;
                  const v = describeScaleIndex(s);
                  const clickable = v.primaryOp !== null;
                  const color = v.tone === "warn" ? "var(--color-warn, #b97a00)"
                    : v.tone === "ok" ? "var(--color-ok, #1a7f5a)" : undefined;
                  const label = `检索索引：${v.stateLabel}${v.state === "indexed" ? ` · ${s.n_nodes} 节点` : ""}`;
                  return (
                    <p
                      className="tool-hint"
                      role={clickable ? "button" : undefined}
                      tabIndex={clickable ? 0 : undefined}
                      title={clickable
                        ? (v.primaryOp === "update" ? "点击更新检索索引（会先确认）" : v.primaryOp === "rebuild" ? "点击全量重建检索索引（会先确认）" : "点击构建检索索引（会先确认）")
                        : (s.eligible ? "" : "库较小，暂不需要检索索引（走暴力检索）")}
                      onClick={clickable ? () => runScaleIndexOp(v.primaryOp!) : undefined}
                      onKeyDown={clickable ? ((e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); runScaleIndexOp(v.primaryOp!); } }) : undefined}
                      style={{ margin: "2px 2px 8px", cursor: clickable ? "pointer" : "default", color }}
                    >
                      {label}
                      {s.exists && !s.delta_searchable && (s.unindexed_sources ?? 0) > 0 && (
                        <span title="未索引部分不参与检索与推理（chunk/KG对象/关系/图谱漫游）；点「更新索引」或等待自动增量收进后可见">
                          {` · ${s.unindexed_sources} 源待索引`}
                        </span>
                      )}
                    </p>
                  );
                })()}
                <input
                  className="source-search"
                  type="search"
                  placeholder="搜索来源（标题/文件名）"
                  value={sourceQuery}
                  onChange={(e) => setSourceQuery(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && currentNotebookId) {
                      loadSourcesPage(currentNotebookId, { page: 0, q: sourceQuery }).catch(reportError);
                    }
                  }}
                />
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
                        <div className="source-row-actions">
                          {currentNotebook?.kg_ready && (
                            <span
                              className={`source-kg-badge${source.kg_extracted ? " source-kg-badge--in" : ""}`}
                              title={source.kg_extracted ? "已入图：该来源已完成 KG 抽取" : "未入图：该来源尚未加入知识图谱"}
                            >
                              {source.kg_extracted ? "已入图" : "未入图"}
                            </span>
                          )}
                          {source.source_url ? (
                            <a className="source-link-button" href={source.source_url} target="_blank" rel="noreferrer" title={source.source_url} onClick={(e) => e.stopPropagation()}>
                              <ExternalLink size={13} />
                            </a>
                          ) : null}
                          {!isReader && (
                            <button className="source-delete-button" title="删除来源" onClick={() => confirmDeleteSource(source)}>
                              <Trash2 size={15} />
                            </button>
                          )}
                        </div>
                      </div>
                    ))
                  )}
                  <Pagination
                    page={sourcesPage}
                    pageSize={SOURCES_PAGE_SIZE}
                    total={sourcesTotal}
                    onPage={(p) => { if (currentNotebookId) loadSourcesPage(currentNotebookId, { page: p, q: sourceQuery }).catch(reportError); }}
                  />
                </div>
              </div>
            </aside>

            {sourcesCollapsed && (
              <button
                type="button"
                className="sources-reveal-rail"
                aria-label="展开来源栏"
                title="展开来源栏"
                onClick={() => setSourcesCollapsed(false)}
              >
                <PanelLeftOpen size={18} />
              </button>
            )}

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
                <div className="chat-session-popover" role="dialog" aria-label="会话管理" ref={sessionPopoverRef}>
                  <div className="chat-session-popover-top">
                    <div className="chat-session-popover-head">
                      <div>
                        <strong>会话</strong>
                        <small>切换历史问答，不压缩当前回答区域。</small>
                      </div>
                      <button className="icon-button compact" type="button" onClick={() => setSessionPanelOpen(false)} title="关闭">
                        <X size={15} />
                      </button>
                    </div>
                    {sessions.length > 0 && (
                      <div className="chat-session-cleanup">
                        <span>批量清理</span>
                        {CLEANUP_PRESETS.map((days) => {
                          const n = conversationsOlderThan(sessions, days).length;
                          return (
                            <button
                              key={days}
                              type="button"
                              className="chat-session-cleanup-btn"
                              disabled={n === 0}
                              title={`删除最近 ${days} 天内无活动的会话`}
                              onClick={() => requestBulkCleanup(days)}
                            >
                              {days} 天前{n > 0 ? ` (${n})` : ""}
                            </button>
                          );
                        })}
                      </div>
                    )}
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
                            notebookId={currentNotebookId}
                            onBuildScaleIndex={() => runScaleIndexOp("build")}
                            buildingScaleIndex={buildingScaleIndex}
                          />
                        </div>
                      </div>
                    ))}
                    {asking && (
                      <div className="chat-turn">
                        {pendingQuestion && <div className="chat-user">{pendingQuestion}</div>}
                        <div className="chat-assistant chat-thinking">
                          {groupOf(pendingMode) === "strict" ? (
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
                    onStatus={(id, status) => updateKnowledge(id, { status }).catch(reportError)}
                    onOwner={(id, owner) => updateKnowledge(id, { owner }).catch(reportError)}
                    onFindDuplicates={() => findDuplicates(knowledgeKind).catch(reportError)}
                    onMerge={(sourceId, intoId) => mergeKnowledge(sourceId, intoId).catch(reportError)}
                    reload={() => loadKnowledge(knowledgeKind, { status: knowledgeStatusFilter, page: 0 }).catch(reportError)}
                    tier={currentNotebook?.tier}
                    onPropose={(id) => submitPromotion(id).catch(reportError)}
                    total={knowledgeTotal[knowledgeKind] ?? 0}
                    page={knowledgePage[knowledgeKind] ?? 0}
                    onPage={(p) => loadKnowledge(knowledgeKind, { status: knowledgeStatusFilter, page: p }).catch(reportError)}
                    onStatusFilter={(s) => {
                      setKnowledgeStatusFilter(s);
                      loadKnowledge(knowledgeKind, { status: s, page: 0 }).catch(reportError);
                    }}
                  />
                )}

                {chatMode === "reports" && currentNotebookId && (
                  <ReportsPanel
                    notebookId={currentNotebookId}
                    listReports={listReports}
                    getReport={getReport}
                    createReport={createReport}
                    updateReportOutline={updateReportOutline}
                    generateReport={generateReport}
                    cancelReport={cancelReport}
                    deleteReport={deleteReport}
                    downloadReportsZip={downloadReportsZip}
                    setToast={setToast}
                    focusReportId={pendingReportFocusId}
                    onFocusConsumed={() => setPendingReportFocusId(null)}
                  />
                )}
              </div>
              {chatMode === "ask" && (
                <div className="chat-input-bar">
                  <textarea
                    className="chat-input"
                    rows={1}
                    placeholder={askHint}
                    value={question}
                    disabled={asking}
                    onChange={(event) => setQuestion(event.target.value)}
                    onKeyDown={handleAskInputKeyDown}
                  />
                  <span>{sources.length} 个来源</span>
                  <div className="ask-mode-control" role="group" aria-label="问答模式">
                    {ASK_MODE_GROUPS.map((g) => (
                      <button
                        key={g.id}
                        type="button"
                        className={`mode-tab${groupOf(askMode) === g.id ? " active" : ""}`}
                        disabled={asking}
                        onClick={() => setAskMode(defaultModeForGroup(g.id))}
                      >
                        {g.label}
                      </button>
                    ))}
                    {groupOf(askMode) === "strict" && (
                      <span className="mode-engines">
                        {modesInGroup("strict").map((m) => (
                          <button
                            key={m.id}
                            type="button"
                            className={`mode-engine${askMode === m.id ? " active" : ""}`}
                            title={m.desc}
                            disabled={asking}
                            onClick={() => setAskMode(m.id)}
                          >
                            {m.label}
                          </button>
                        ))}
                      </span>
                    )}
                    {groupOf(askMode) === "strict" && !kgAvailable && (
                      <span className="mode-hint">
                        该 notebook 尚无知识图谱，严格推理需先构建
                        <button
                          type="button"
                          className="mode-engine"
                          style={{ marginLeft: 6 }}
                          disabled={buildingKg || asking}
                          onClick={() => { if (currentNotebookId) startKgBuild(currentNotebookId); }}
                        >
                          {buildingKg ? "全量构建中…" : "全量构建知识图谱"}
                        </button>
                      </span>
                    )}
                    {groupOf(askMode) === "strict" && kgAvailable && currentNotebook?.base_kg_available && !currentNotebook?.kg_ready && (
                      <span className="mode-hint">本笔记本无图，将使用底层库（base）推理</span>
                    )}
                  </div>
                  <button
                    className={`send-button ${asking ? "stop" : ""}`}
                    type="button"
                    aria-label={asking ? "中断生成" : "发送"}
                    title={asking ? "中断生成" : "发送"}
                    disabled={!asking && !question.trim()}
                    onClick={() => asking ? abortAsk() : runAsk().catch(reportError)}
                  >
                    {asking ? <Square size={16} strokeWidth={2.5} /> : "→"}
                  </button>
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
          {menuNotebook.access === "reader" ? (
            <button
              className="danger"
              onClick={() => {
                const target = menuNotebook;
                setMenuNotebookId(null);
                setMenuPosition(null);
                leaveNotebook(target.id)
                  .then(() => loadNotebookCollection())
                  .then(() => setToast("已退出只读共享"))
                  .catch(reportError);
              }}
            >退出共享</button>
          ) : (
            <>
              <button onClick={() => { setEditingNotebook(menuNotebook); setMenuNotebookId(null); setMenuPosition(null); }}>编辑信息</button>
              <button className="danger" onClick={() => { setDeleteNotebook(menuNotebook); setMenuNotebookId(null); setMenuPosition(null); }}>删除 notebook</button>
            </>
          )}
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

      {shareModal && currentNotebook && (
        <section className="utility-modal" role="dialog" aria-modal="true" onClick={(event) => { if (event.currentTarget === event.target) setShareModal(null); }}>
          <div className="utility-modal-card">
            <div className="source-modal-header">
              <div>
                <h2>分享「{currentNotebook.name}」</h2>
                <p>{shareModal.copyable
                  ? "拿到链接的登录用户可将这个知识库整份拷贝到自己的空间。"
                  : "此库较大，拿到链接的登录用户可以只读方式加入(浏览/问答/看图，不能修改)。"}</p>
              </div>
              <button className="icon-button" onClick={() => setShareModal(null)} title="Close">×</button>
            </div>
            <div className="source-detail-body">
              <label>分享链接
                <div className="tag-row" style={{ marginTop: 6 }}>
                  <input
                    readOnly
                    value={buildShareLink(shareModal.share_token, window.location.origin)}
                    onFocus={(event) => event.currentTarget.select()}
                    style={{ flex: 1 }}
                  />
                  <button className="sort-button" onClick={() => copyShareLink().catch(reportError)}>复制</button>
                </div>
              </label>
              <p className="tool-hint" style={{ margin: "2px 0 0" }}>
                {shareModal.copyable ? "他人可拷贝" : "库较大，他人可只读加入"}
                {` · ${shareModal.size.sources} 来源 · ${shareModal.size.nodes} 节点 · ${shareModal.size.edges} 边 · ${formatFileSize(shareModal.size.bytes)}`}
              </p>
              <div className="tag-row">
                <button className="sort-button" disabled={shareBusy} onClick={() => handleUnshare().catch(reportError)}>取消分享</button>
                <button className="new-pill" onClick={() => setShareModal(null)}>完成</button>
              </div>
            </div>
          </div>
        </section>
      )}

      {sharedPreview && (
        <section className="utility-modal" role="dialog" aria-modal="true" onClick={(event) => { if (event.currentTarget === event.target) setSharedPreview(null); }}>
          <div className="utility-modal-card">
            <div className="source-modal-header">
              <div>
                <h2>{sharedPreview.name}</h2>
                <p>由 {sharedPreview.owner_display} 分享 · {sharedPreview.source_count} 来源 · {sharedPreview.node_count} 节点 · {sharedPreview.edge_count} 边</p>
              </div>
              <button className="icon-button" onClick={() => setSharedPreview(null)} title="Close">×</button>
            </div>
            <div className="source-detail-body">
              {sharedPreview.source_titles.length > 0 && (
                <div className="stack">
                  <span className="section-title">来源</span>
                  {sharedPreview.source_titles.map((title, index) => (
                    <div className="checklist-row" key={`${title}-${index}`}>
                      <span style={{ flex: 1, wordBreak: "break-word" }}>{title}</span>
                    </div>
                  ))}
                </div>
              )}
              {sharedPreview.mode === "readonly" && (
                <p className="tool-hint" style={{ margin: "2px 0 0" }}>
                  此库较大，将以只读方式加入——你可浏览来源、问答、查看知识图谱，但不能修改。
                </p>
              )}
              <div className="tag-row">
                {sharedPreview.mode === "copy" ? (
                  <button
                    className="new-pill"
                    disabled={copyBusy}
                    onClick={() => {
                      const token = shareTokenRef.current;
                      if (token) handleCopyShared(token).catch(reportError);
                    }}
                  >
                    {copyBusy ? "拷贝中…" : "拷贝到我的空间"}
                  </button>
                ) : (
                  <button
                    className="new-pill"
                    disabled={copyBusy}
                    onClick={() => {
                      const token = shareTokenRef.current;
                      if (token) handleJoinShared(token).catch(reportError);
                    }}
                  >
                    {copyBusy ? "加入中…" : "加入(只读)"}
                  </button>
                )}
                <button className="sort-button" onClick={() => setSharedPreview(null)}>取消</button>
              </div>
            </div>
          </div>
        </section>
      )}

      {sharedByMeOpen && (
        <section className="utility-modal" role="dialog" aria-modal="true" onClick={(event) => { if (event.currentTarget === event.target) setSharedByMeOpen(false); }}>
          <div className="utility-modal-card">
            <div className="source-modal-header">
              <div>
                <h2>已分享</h2>
                <p>你分享出去的知识库。小库可被他人拷贝为独立副本;大库以只读方式共享,下方列出已加入的只读成员。</p>
              </div>
              <button className="icon-button" onClick={() => setSharedByMeOpen(false)} title="Close">×</button>
            </div>
            <div className="source-detail-body">
              {sharedByMeList === null ? (
                <p className="tool-hint">加载中…</p>
              ) : sharedByMeList.length === 0 ? (
                <article className="source-empty">
                  <div>▧</div>
                  <strong>尚未分享任何知识库</strong>
                  <p>在某个知识库里点「分享」即可生成链接。</p>
                </article>
              ) : (
                <div className="stack">
                  {sharedByMeList.map((item) => (
                    <div className="checklist-row" key={item.id} style={{ flexDirection: "column", alignItems: "stretch", gap: 6 }}>
                      <div className="tag-row" style={{ alignItems: "center", gap: 8 }}>
                        <span style={{ flex: 1, wordBreak: "break-word", fontWeight: 600 }}>{item.name}</span>
                        <span className="new-pill" title={item.mode === "readonly" ? "库较大,只读共享" : "库较小,可被拷贝"}>
                          {shareModeLabel(item.mode)}
                        </span>
                      </div>
                      <div className="tag-row" style={{ marginTop: 2 }}>
                        <input
                          readOnly
                          value={buildShareLink(item.share_token, window.location.origin)}
                          onFocus={(event) => event.currentTarget.select()}
                          style={{ flex: 1 }}
                        />
                        <button
                          className="sort-button"
                          onClick={() => {
                            const link = buildShareLink(item.share_token, window.location.origin);
                            navigator.clipboard?.writeText(link)
                              .then(() => setToast("分享链接已复制"))
                              .catch(() => { setStatusText(link); setToast("复制失败，链接已显示在状态栏"); });
                          }}
                        >复制</button>
                      </div>
                      <p className="tool-hint" style={{ margin: "0" }}>
                        {`${item.size.sources} 来源 · ${item.size.nodes} 节点 · ${item.size.edges} 边 · ${formatFileSize(item.size.bytes)}`}
                      </p>
                      {item.mode === "readonly" && (
                        <p className="tool-hint" style={{ margin: "0" }}>
                          只读成员:{item.members.length > 0 ? item.members.map((m) => m.username).join("，") : "暂无成员"}
                        </p>
                      )}
                      <div className="tag-row" style={{ marginTop: 2 }}>
                        <button
                          className="sort-button"
                          disabled={shareBusy}
                          title="撤销分享链接并移除所有只读成员"
                          onClick={() => handleUnshareFromOverview(item.id).catch(reportError)}
                        >取消分享</button>
                      </div>
                    </div>
                  ))}
                </div>
              )}
              <div className="tag-row">
                <button className="new-pill" onClick={() => setSharedByMeOpen(false)}>完成</button>
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
                <p>上传文件或添加链接；文件可为每个指定文档类型（默认自动检测），类型决定抽取 schema。</p>
              </div>
              <button className="icon-button" onClick={() => { setStagedFiles([]); setStagedDocTypes([]); setLinkSectionOpen(false); setSourceModalOpen(false); }} title="Close">×</button>
            </div>
            <label className="drop-zone">
              <input type="file" multiple accept={SUPPORTED_SOURCE_ACCEPT} onChange={stageFiles} />
              <span className="drop-plus">＋</span>
              <strong>{stagedFiles.length > 0 ? "继续添加文件" : "或拖放文件"}</strong>
              <small>支持 {SUPPORTED_SOURCE_USER_HINT}；图片与 OCR 暂不处理。</small>
            </label>
            <div className="source-action-row">
              <label className="source-action-button">
                <Upload size={18} strokeWidth={2.5} /> 上传文件
                <input type="file" multiple accept={SUPPORTED_SOURCE_ACCEPT} onChange={stageFiles} />
              </label>
              <button
                type="button"
                className={`source-action-button${linkSectionOpen ? " is-active" : ""}`}
                onClick={() => { setUrlRejected([]); setLinkSectionOpen((open) => !open); }}
              >
                <ExternalLink size={18} strokeWidth={2.5} /> 添加链接
              </button>
            </div>
            {linkSectionOpen && (
              <div className="source-detail-body">
                <p className="tool-hint" style={{ margin: "0 0 6px" }}>每行一个可直链的 PDF；非 PDF 会被直接拒绝。由 MinerU 解析（本地已配置则优先本地，否则走 mineru.net 云端）。</p>
                <textarea
                  rows={5}
                  value={urlText}
                  placeholder={"https://arxiv.org/pdf/2401.00001\nhttps://example.com/paper.pdf"}
                  onChange={(event) => setUrlText(event.target.value)}
                />
                {urlRejected.length > 0 && (
                  <div className="stack" style={{ marginTop: 8 }}>
                    <span className="section-title">被拒链接</span>
                    {urlRejected.map((item, index) => (
                      <div className="checklist-row" key={`${item.url}-${index}`}>
                        <span style={{ flex: 1, wordBreak: "break-all" }}>{item.url}</span>
                        <small style={{ color: "var(--danger, #c0392b)" }}>{item.reason}</small>
                      </div>
                    ))}
                  </div>
                )}
                <div className="tag-row">
                  <button className="new-pill" disabled={urlBusy} onClick={() => submitUrlSources().catch(reportError)}>
                    {urlBusy ? "添加中…" : "添加并解析"}
                  </button>
                  <button className="sort-button" onClick={() => { setUrlText(""); setUrlRejected([]); setLinkSectionOpen(false); }}>取消</button>
                </div>
              </div>
            )}
            {stagedFiles.length > 0 && (
              <div className="source-detail-body">
                <div className="staged-head">
                  <span className="section-title">{stagedFiles.length} 个待上传文件</span>
                  <label className="staged-bulk">全部设为
                    <select value="__none__" onChange={(event) => { if (event.target.value !== "__none__") setAllStagedDocTypes(event.target.value); }}>
                      <option value="__none__">— 批量设置 —</option>
                      {docTypeOptions.map((opt) => <option key={opt.id || "auto"} value={opt.id}>{opt.label}</option>)}
                    </select>
                  </label>
                </div>
                <div className="stack">
                  {stagedFiles.map((file, index) => (
                    <div className="staged-file-row" key={`${file.name}-${index}`}>
                      <span className="staged-file-name" title={file.name}>{file.name}</span>
                      <select
                        className="staged-file-type"
                        value={stagedDocTypes[index] ?? ""}
                        onChange={(event) => setStagedDocType(index, event.target.value)}
                      >
                        {docTypeOptions.length === 0 && <option value="">自动检测</option>}
                        {docTypeOptions.map((opt) => <option key={opt.id || "auto"} value={opt.id}>{opt.label}</option>)}
                      </select>
                      <button
                        type="button"
                        className="staged-file-remove"
                        title="移除此文件"
                        aria-label={`移除 ${file.name}`}
                        onClick={() => removeStagedFile(index)}
                      >
                        ×
                      </button>
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
        <section className="utility-modal utility-modal-top" role="dialog" aria-modal="true">
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
              {infoModal.actions.some((action) => action.desc || action.note) ? (
                // 带描述的动作(分析弹窗):网格布局 —— 按钮列共享最宽标签宽度做到等宽对齐,描述/注记跟随右列
                <div className="info-action-grid">
                  {infoModal.actions.map((action) => (
                    <Fragment key={action.label}>
                      <button
                        className={action.danger ? "new-pill danger-pill" : action.primary ? "new-pill" : "sort-button"}
                        onClick={() => { setInfoModal(null); action.action(); }}
                      >
                        {action.label}
                      </button>
                      <div className="info-action-desc-cell">
                        {action.desc && <span className="info-action-desc">{action.desc}</span>}
                        {action.note && <span className="info-action-note">{action.note}</span>}
                      </div>
                    </Fragment>
                  ))}
                </div>
              ) : (
                infoModal.actions.map((action) => (
                  <button
                    key={action.label}
                    className={action.danger ? "new-pill danger-pill" : action.primary ? "new-pill" : "sort-button"}
                    onClick={() => { setInfoModal(null); action.action(); }}
                  >
                    {action.label}
                  </button>
                ))
              )}
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

      {analytics && (
        <section className="utility-modal" role="dialog" aria-modal="true" onClick={(event) => { if (event.currentTarget === event.target) setAnalytics(null); }}>
          <div className="utility-modal-card">
            <div className="source-modal-header">
              <div>
                <h2>知识分析看板</h2>
                <p>回答质量、审核进度、知识覆盖、来源状态与索引构建状态的本机统计。</p>
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
              <p className="section-title">索引与构建</p>
              {indexStatus ? (
                <div className="stack">
                  {/* 知识图谱行:状态取 indexStatus.kg,动作复用既有 startKgBuild/startKgRebuild/relinkFromKgView。 */}
                  {(() => {
                    const kg = indexStatus.kg;
                    const busy = kg.building || buildingKg;
                    const stateLabel = busy ? "抽取中…" : !kg.ready ? "未建" : kg.pending_sources > 0 ? `${kg.pending_sources} 篇待抽` : "就绪";
                    const tone: "ok" | "warn" = !busy && kg.ready && kg.pending_sources === 0 ? "ok" : "warn";
                    const color = tone === "ok" ? "var(--color-ok, #1a7f5a)" : "var(--color-warn, #b97a00)";
                    const sub = !kg.ready
                      ? "从来源抽取知识对象与关系；严格推理（推理 / 图谱）需要先构建"
                      : kg.pending_sources > 0
                        ? "有新增来源尚未入图，可增量抽取并合并进现有图谱"
                        : "已构建，可用严格推理（推理 / 图谱）";
                    return (
                      <div className={`index-card index-tone-${tone}`}>
                        <span className="index-ic" aria-hidden="true"><Network size={19} /></span>
                        <div className="index-main">
                          <div className="tag-row" style={{ alignItems: "center" }}>
                            <span className="index-state">知识图谱</span>
                            <span className="tag" style={{ color }}>{stateLabel}</span>
                          </div>
                          <div className="index-sub">{sub}</div>
                        </div>
                        {!busy && !isReader && (
                          <div className="index-ctas">
                            {(!kg.ready || kg.pending_sources > 0) && (
                              <button
                                type="button"
                                className="index-cta primary"
                                onClick={() => { if (currentNotebookId) startKgBuild(currentNotebookId); }}
                              >
                                {kg.ready ? `补抽 ${kg.pending_sources} 篇` : "构建"}
                              </button>
                            )}
                            {kg.ready && kg.pending_sources === 0 && (
                              <>
                                <button
                                  type="button"
                                  className="index-cta"
                                  onClick={() => { if (currentNotebookId) startKgRebuild(currentNotebookId); }}
                                >
                                  完整重抽
                                </button>
                                <button type="button" className="index-cta" onClick={relinkFromKgView}>补连孤立节点</button>
                              </>
                            )}
                          </div>
                        )}
                      </div>
                    );
                  })()}
                  {/* 概念合并行:状态取 indexStatus.unified_kg,唯一动作 = 既有 refreshUnifiedKg(经统一确认)。 */}
                  {(() => {
                    const uk = indexStatus.unified_kg;
                    const busy = uk.building || kgRefreshBusy;
                    const stateLabel = busy ? "重建中…" : uk.dirty ? "待重建" : "最新";
                    const tone: "ok" | "warn" = busy || uk.dirty ? "warn" : "ok";
                    const color = tone === "ok" ? "var(--color-ok, #1a7f5a)" : "var(--color-warn, #b97a00)";
                    const tsSuffix = uk.last_rebuild_at ? ` · 上次 ${formatRelativeTime(uk.last_rebuild_at)}` : "";
                    return (
                      <div className={`index-card index-tone-${tone}`}>
                        <span className="index-ic" aria-hidden="true"><GitMerge size={19} /></span>
                        <div className="index-main">
                          <div className="tag-row" style={{ alignItems: "center" }}>
                            <span className="index-state">概念合并</span>
                            <span className="tag" style={{ color }}>{stateLabel}{tsSuffix}</span>
                          </div>
                          <div className="index-sub">跨文档概念聚类；重算并刷新图谱索引（不重新抽取来源）</div>
                        </div>
                        {!busy && !isReader && (
                          <div className="index-ctas">
                            <button type="button" className={`index-cta${uk.dirty ? " primary" : ""}`} onClick={confirmRefreshUnifiedKg}>重新合并</button>
                          </div>
                        )}
                      </div>
                    );
                  })()}
                  {/* 检索索引行:状态取 indexStatus.scale_index,忙碌(排队/构建中)时唯一动作变为「取消」。 */}
                  {(() => {
                    const s = indexStatus.scale_index;
                    const v = describeScaleIndex(s);
                    // 本地忙碌位兜底(与知识图谱/概念合并两行一致):server-derived state 靠聚合轮询
                    // 6s 才刷新一次,点击构建/更新/全量重建的瞬间先靠本地 buildingScaleIndex 立即
                    // 切到「取消」态,避免残留可点的旧按钮。
                    const busy = buildingScaleIndex || v.state === "building" || v.state === "queued";
                    const tsSuffix = s.last_built_at ? ` · 上次 ${formatRelativeTime(s.last_built_at)}` : "";
                    const color = v.tone === "warn" ? "var(--color-warn, #b97a00)" : v.tone === "ok" ? "var(--color-ok, #1a7f5a)" : undefined;
                    const desc = v.tone === "muted" ? "库较小，走暴力检索即可，无需索引"
                      : v.state === "building" ? "后台进行，完成后自动更新"
                      : v.state === "queued" ? "已排队，将在服务器空闲时构建；完成后自动更新"
                      : v.state === "stale" ? "新增内容未纳入索引，暂不参与检索与推理"
                      : v.state === "indexed" ? "已建成且为最新"
                      : "从零为本库构建向量检索索引（CSR 图 + KG/chunk ANN），加速语义检索与严格推理";
                    return (
                      <div className={`index-card index-tone-${v.tone}`}>
                        <span className="index-ic" aria-hidden="true"><Database size={19} /></span>
                        <div className="index-main">
                          <div className="tag-row" style={{ alignItems: "center" }}>
                            <span className="index-state">检索索引</span>
                            <span className="tag" style={{ color }}>
                              {v.stateLabel}
                              {v.state === "stale" && (s.unindexed_sources ?? 0) > 0 ? ` · ${s.unindexed_sources} 源待索引` : ""}
                              {tsSuffix}
                            </span>
                          </div>
                          <div className="index-sub">{desc}</div>
                          {v.state === "indexed" && (
                            <div className="mini-tags">
                              <span className="tag">{s.n_nodes} 节点</span>
                              <span className="tag">{s.n_chunks} chunks</span>
                              {s.has_chunk_ann && <span className="tag">chunk ANN ✓</span>}
                            </div>
                          )}
                        </div>
                        {!isReader && (
                          <div className="index-ctas">
                            {busy ? (
                              <button type="button" className="index-cta" disabled={cancelingScaleIndex} onClick={handleCancelScaleIndex}>
                                {cancelingScaleIndex ? "取消中…" : "取消"}
                              </button>
                            ) : (
                              <>
                                {v.primaryOp === "build" && <button type="button" className="index-cta primary" onClick={() => runScaleIndexOp("build")}>构建索引</button>}
                                {v.primaryOp === "update" && <button type="button" className="index-cta primary" onClick={() => runScaleIndexOp("update")}>更新索引</button>}
                                {v.primaryOp === "rebuild" && <button type="button" className="index-cta primary" onClick={() => runScaleIndexOp("rebuild")}>全量重建</button>}
                                {v.canRebuild && v.primaryOp !== "rebuild" && <button type="button" className="index-cta" onClick={() => runScaleIndexOp("rebuild")}>全量重建</button>}
                                {v.tone !== "muted" && <button type="button" className="index-cta" onClick={runScaleIndexIdle}>空闲时建</button>}
                              </>
                            )}
                          </div>
                        )}
                      </div>
                    );
                  })()}
                </div>
              ) : (
                <p className="tool-hint">加载索引与构建状态…</p>
              )}
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
                        <strong><LatexText text={from?.headline ?? edge.from_id} isFormula={from?.object_type === "formula"} /></strong>
                        <span className="tag">{RELATION_LABELS[edge.relation] ?? edge.relation}</span>
                        → <strong><LatexText text={to?.headline ?? edge.to_id} isFormula={to?.object_type === "formula"} /></strong>
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
              <input className="kg-search" placeholder="搜索节点名称或类型…" value={kgSearch} onChange={(e) => handleKgSearchChange(e.target.value)} />
              {!isReader && (
              <div className="kg-rail-section">
                <h3>图谱处理</h3>
                <div className="kg-action-stack">
                  <button
                    type="button"
                    className="sort-button"
                    disabled={relinkingKg || buildingKg}
                    title="为孤立节点补连边（快速、确定性，不覆盖现有图）"
                    onClick={relinkFromKgView}
                  >
                    {relinkingKg ? "补连中…" : "补连孤立节点"}
                  </button>
                  <button
                    type="button"
                    className="sort-button"
                    disabled={kgRefreshBusy || buildingKg}
                    title="对现有节点重新聚类 / 跨文档合并并刷新（不重新抽取来源，会先确认）"
                    onClick={confirmRefreshUnifiedKg}
                  >
                    {kgRefreshBusy ? "合并中…" : "重新合并"}
                  </button>
                  <button
                    type="button"
                    className="sort-button kg-action-danger"
                    disabled={buildingKg}
                    title="清空现有知识图谱并重新抽取全部来源（后台任务，可能数分钟）"
                    onClick={() => { if (currentNotebookId) startKgRebuild(currentNotebookId); }}
                  >
                    {buildingKg ? "重抽中…" : "完整重抽"}
                  </button>
                </div>
              </div>
              )}
              <div className="kg-rail-section">
                <h3>当前视图</h3>
                <div className="tag-row">
                  <span className="tag">节点 {fgData.nodes.length}{!kgSearching && uGraphMerged ? ` / ${uGraphMerged.nodes.length}` : ""}</span>
                  <span className="tag">边 {fgData.links.length}{!kgSearching && uGraphMerged ? ` / ${uGraphMerged.edges.length}` : ""}</span>
                </div>
                <label className="kg-range">
                  <span>范围</span>
                  <select value={kgLimit} disabled={kgRangeBusy || kgSearching} onChange={(e) => changeKgRange(Number(e.target.value))}>
                    {KG_RANGE_STEPS
                      .filter((opt) => {
                        // index 索引库（base_kg_available）用搜索+展开代替全量拉取，隐藏「全部」。
                        if (opt.value === 0 && currentNotebook?.base_kg_available) return false;
                        return true;
                      })
                      .map((opt) => <option key={opt.value} value={opt.value}>{opt.label}</option>)}
                  </select>
                </label>
                {kgSearching ? (
                  <p className="tool-hint" style={{ margin: "4px 2px 0" }}>
                    {kgSearchBusy
                      ? "搜索中…"
                      : `命中 ${fgData.searchHitCount} 个节点`}
                  </p>
                ) : uGraph && (
                  <p className="tool-hint" style={{ margin: "4px 2px 0" }}>
                    {kgRangeBusy
                      ? "加载中…"
                      : uGraph.truncated
                        ? `已载 ${uGraph.nodes.length} / 共 ${uGraph.total_nodes ?? uGraph.nodes.length} 节点 · 按连接度，可扩大范围`
                        : `共 ${uGraph.total_nodes ?? uGraph.nodes.length} 节点（已全部显示）`}
                  </p>
                )}
                {unifiedKgStatus && (
                  <div className="tag-row" style={{ marginTop: 4 }}>
                    {/* 纯状态展示,非交互——唯一动作入口是上方「重新合并」按钮(去重复,见其 title)。 */}
                    <span
                      className="tag"
                      title="概念合并状态；点击上方「重新合并」按钮可手动刷新"
                      style={{ color: unifiedKgStatus.dirty ? "var(--color-warn, #b97a00)" : undefined }}
                    >
                      {kgRefreshBusy ? "重建中…" : unifiedKgStatus.dirty ? "待重建" : "最新"}
                    </span>
                    {unifiedKgStatus.last_rebuild_at && (
                      <span className="tag">上次重建 · {formatRelativeTime(unifiedKgStatus.last_rebuild_at)}</span>
                    )}
                    {scaleIndexStatus && (() => {
                      const s = scaleIndexStatus;
                      const v = describeScaleIndex(s);
                      const clickable = v.primaryOp !== null && !isReader;
                      const color = v.tone === "warn" ? "var(--color-warn, #b97a00)"
                        : v.tone === "ok" ? "var(--color-ok, #1a7f5a)" : undefined;
                      const label = `检索索引：${v.stateLabel}${v.state === "indexed" ? ` · ${s.n_nodes} 节点` : ""}`;
                      return (
                        <span
                          className="tag"
                          role={clickable ? "button" : undefined}
                          tabIndex={clickable ? 0 : undefined}
                          title={clickable
                            ? (v.primaryOp === "update" ? "点击更新检索索引（会先确认）" : v.primaryOp === "rebuild" ? "点击全量重建检索索引（会先确认）" : "点击构建检索索引（会先确认）")
                            : (s.eligible ? "" : "库较小，暂不需要检索索引（走暴力检索）")}
                          onClick={clickable ? () => runScaleIndexOp(v.primaryOp!) : undefined}
                          onKeyDown={clickable ? ((e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); runScaleIndexOp(v.primaryOp!); } }) : undefined}
                          style={{ cursor: clickable ? "pointer" : "default", color }}
                        >
                          {label}
                          {s.exists && !s.delta_searchable && (s.unindexed_sources ?? 0) > 0 && (
                            <span title="未索引部分不参与检索与推理（chunk/KG对象/关系/图谱漫游）；点「更新索引」或等待自动增量收进后可见">
                              {` · ${s.unindexed_sources} 源待索引`}
                            </span>
                          )}
                        </span>
                      );
                    })()}
                  </div>
                )}
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
                  {kgReviewBusy ? "预审中…" : "LLM 预审"}
                </button>
                <button
                  className="ghost-button"
                  onClick={reviewAllMerges}
                  disabled={!pendingMerges.length || reviewAllJob?.status === "running"}
                >
                  {reviewAllJob?.status === "running"
                    ? `全部预审中… ${reviewAllJob.done}/${reviewAllJob.total}`
                    : "全部预审"}
                </button>
                {pendingMerges.length === 0 ? <p className="tool-hint">无</p> : pendingMerges.map((m) => (
                  <div className="kg-merge-row" key={m.id}>
                    <span>{m.canonical_a.replace(/^K-/, "")} ↔ {m.canonical_b.replace(/^K-/, "")} <em>({m.score.toFixed(2)})</em></span>
                    <span className="kg-merge-actions">
                      <button onClick={() => decideMerge(m, true)}>合并</button>
                      <button onClick={() => decideMerge(m, false)}>拒绝</button>
                    </span>
                  </div>
                ))}
              </div>
            </aside>
            <div className="kg-canvas" ref={kgCanvasRef}>
              {uGraph === null ? <p className="tool-hint kg-canvas-empty">加载中…</p> : vizBuilding ? (
                <div className="tool-hint kg-canvas-empty">
                  <strong>图谱索引构建中，首次构建大库可能需要几分钟…</strong>
                  <p style={{ marginTop: 6 }}>建成后会自动刷新为完整图谱</p>
                </div>
              ) : fgData.nodes.length === 0 ? (
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
                  linkWidth={(link: any) => 1.35 + Math.min(((link.sourceCount ?? 1) - 1), 4) * 0.5}
                  linkLabel={(link: any) => {
                    const base = RELATION_LABELS[link.label] ?? link.label;
                    return (link.sourceCount ?? 1) >= 2 ? `${base} · ${link.sourceCount} 源支持` : base;
                  }}
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
                    <h3><LatexText text={kgNodeName(selectedKgNode)} isFormula={selectedKgNode.object_type === "formula"} /></h3>
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
                            {edge.source_count && edge.source_count >= 2 ? (
                              <span className="kg-relation-mid">
                                <strong>{RELATION_LABELS[edge.edge_type] ?? edge.edge_type}</strong>
                                <span className="tag">×{edge.source_count}源</span>
                              </span>
                            ) : (
                              <strong>{RELATION_LABELS[edge.edge_type] ?? edge.edge_type}</strong>
                            )}
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
                                  <span><KgTypeMark type={node.object_type} /><LatexText text={String(node.payload.name ?? "")} isFormula={node.object_type === "formula"} /></span>
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

      {edgeReviewOpen && (
        <section
          className="utility-modal"
          role="dialog"
          aria-modal="true"
          onClick={(event) => { if (event.currentTarget === event.target) setEdgeReviewOpen(false); }}
        >
          <div className="utility-modal-card">
            <div className="source-modal-header">
              <div>
                <h2>边审查队列</h2>
                <p>按「高中心性 × 低可信」排序的关系。确认可信边，或拒绝错误边（被拒边将从所有图推理遍历中排除）。</p>
              </div>
              <button className="icon-button" onClick={() => setEdgeReviewOpen(false)} title="Close">×</button>
            </div>
            <div className="source-detail-body">
              {(edgeQueue ?? []).length === 0 ? (
                <p className="tool-hint">暂无待审关系。</p>
              ) : (
                <div className="stack">
                  {(edgeQueue ?? []).map((edge) => (
                    <article className="item" key={edge.rel_id}>
                      <div className="tag-row">
                        <span className="tag">{edge.edge_type}</span>
                        <span className="tag">{edge.review_status}</span>
                        <span className="tag">可信 {edge.trust_score.toFixed(2)}</span>
                        <span className="tag">优先级 {edge.review_priority.toFixed(2)}</span>
                      </div>
                      <h3>{(edge.source_name || edge.source_object_id)} → {(edge.target_name || edge.target_object_id)}</h3>
                      {edge.review_status !== "rejected" && (
                        <div className="modal-actions">
                          <button
                            className="sort-button"
                            disabled={edgeBusy}
                            onClick={() => decideEdge(edge.rel_id, "rejected").catch(reportError)}
                          >
                            拒绝
                          </button>
                          <button
                            className="new-pill"
                            disabled={edgeBusy}
                            onClick={() => decideEdge(edge.rel_id, "verified").catch(reportError)}
                          >
                            确认可信
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
      <PendingToast toast={pending.toast} onClose={() => pending.setToast(null)}
        onClick={() => { if (pending.toast) openDoneItem(pending.toast); }} />

      {modelPanelOpen && modelForms && (
        <section className="utility-modal" role="dialog" aria-modal="true"
          onClick={(e) => { if (e.currentTarget === e.target) setModelPanelOpen(false); }}>
          <div className="utility-modal-card">
            <div className="source-modal-header">
              <div><h2>模型服务</h2><p>留空则使用系统默认；API Key 只写不回显</p></div>
              <button className="icon-button" onClick={() => setModelPanelOpen(false)}>×</button>
            </div>
            <div className="source-detail-body">
              <p className="tool-hint" style={{ margin: "0 0 12px" }}>
                Base URL 只需填到服务根地址（通常以 <code>/v1</code> 结尾），系统会自动拼接 <code>/chat/completions</code> 等接口路径。
                例如填 <code>https://api.openai.com/v1</code>，而不是 <code>https://api.openai.com/v1/chat/completions</code>。
              </p>
              {MODEL_ROLES.map((role) => (
                <fieldset key={role} className="edit-form" style={{ marginBottom: 12 }}>
                  <legend>{ROLE_LABELS[role]}</legend>
                  <label>Base URL
                    <input value={modelForms[role].base_url}
                      placeholder={BASE_URL_PLACEHOLDERS[role]}
                      onChange={(e) => setModelForms((s) => s && ({ ...s, [role]: { ...s[role], base_url: e.target.value } }))} />
                  </label>
                  <label>Model
                    <input value={modelForms[role].model}
                      onChange={(e) => setModelForms((s) => s && ({ ...s, [role]: { ...s[role], model: e.target.value } }))} />
                  </label>
                  <label>API Key
                    <input type="password" placeholder="未改动则保留原 key"
                      value={modelForms[role].api_key}
                      onChange={(e) => setModelForms((s) => s && ({ ...s, [role]: { ...s[role], api_key: e.target.value, keyDirty: true } }))} />
                  </label>
                  <div className="modal-actions">
                    <button type="button" className="sort-button" onClick={() => runModelTest(role)}>测试</button>
                    <span style={{ fontSize: 12, opacity: 0.8 }}>{modelTesting[role] || ""}</span>
                  </div>
                </fieldset>
              ))}
              <div className="modal-actions">
                <button className="sort-button" onClick={() => setModelPanelOpen(false)}>取消</button>
                <button className="new-pill" onClick={() => saveModelPanel()}>保存</button>
              </div>
            </div>
          </div>
        </section>
      )}
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
    <article className={`item schema-card ${schema.status === "disabled" ? "knowledge-deprecated" : ""}`}>
      <div className="schema-card-head">
        <strong>{schema.object_type}</strong>
        <span className="tag">{schema.source}</span>
        <span className={`tag ${schema.status === "active" ? "severity-low" : ""}`}>{schema.status}</span>
      </div>
      <label className="schema-field">
        <span>显示名</span>
        <input value={label} disabled={busy} onChange={(e) => setLabel(e.target.value)} />
      </label>
      <label className="schema-field">
        <span>字段（逗号分隔，按顺序）</span>
        <textarea rows={2} value={fieldsText} disabled={busy} onChange={(e) => setFieldsText(e.target.value)} />
      </label>
      <label className="schema-field">
        <span>说明（用于抽取提示）</span>
        <input value={description} disabled={busy} onChange={(e) => setDescription(e.target.value)} />
      </label>
      <div className="schema-actions">
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
    <article className="item schema-card">
      <p className="section-title">新增自定义类型</p>
      <label className="schema-field">
        <span>类型 id（snake_case）</span>
        <input value={objectType} disabled={busy} placeholder="例如 process_window" onChange={(e) => setObjectType(e.target.value)} />
      </label>
      <label className="schema-field">
        <span>显示名</span>
        <input value={label} disabled={busy} onChange={(e) => setLabel(e.target.value)} />
      </label>
      <label className="schema-field">
        <span>字段（逗号分隔）</span>
        <textarea rows={2} value={fieldsText} disabled={busy} placeholder="title, condition, limit" onChange={(e) => setFieldsText(e.target.value)} />
      </label>
      <label className="schema-field">
        <span>说明</span>
        <input value={description} disabled={busy} onChange={(e) => setDescription(e.target.value)} />
      </label>
      <div className="schema-actions">
        <button className="sort-button" disabled={busy} onClick={submit}>新增类型</button>
      </div>
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
  onStatusFilter,
  onStatus,
  onOwner,
  onFindDuplicates,
  onMerge,
  reload,
  tier,
  onPropose,
  total,
  page,
  onPage,
}: {
  kind: KnowledgeKind;
  items: KnowledgeItem[] | null;
  types: KnowledgeTypeCount[];
  statusFilter: string;
  duplicates: DuplicateGroup[] | null;
  notebookId: string;
  onKind: (kind: KnowledgeKind) => void;
  onStatusFilter: (value: string) => void;
  onStatus: (id: string, status: string) => void;
  onOwner: (id: string, owner: string) => void;
  onFindDuplicates: () => void;
  onMerge: (sourceId: string, intoId: string) => void;
  reload: () => void;
  tier?: string;
  onPropose?: (id: string) => void;
  total: number;
  page: number;
  onPage: (p: number) => void;
}) {
  const [ctx, setCtx] = useState<Record<string, NodeContext>>({});
  useEffect(() => { setCtx({}); }, [kind]);
  const statuses = ["all", ...KNOWLEDGE_STATUS_OPTIONS];
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
        <select value={statusFilter} onChange={(event) => onStatusFilter(event.target.value)}>
          {statuses.map((value) => <option key={value} value={value}>{value === "all" ? "全部状态" : value}</option>)}
        </select>
        <button className="sort-button" onClick={reload}>刷新</button>
        <button className="sort-button" onClick={onFindDuplicates}>查重</button>
      </div>
      {duplicates !== null && (
        <div className="knowledge-panel">
          <p className="section-title">重复组（规范名归一化后相同）</p>
          {duplicates.length === 0 ? (
            <p className="tool-hint">未发现重复。</p>
          ) : duplicates.map((group, index) => (
            <article className="item" key={`dup-${index}`}>
              <div className="tag-row"><span className="tag">similarity {group.similarity}</span></div>
              {group.members.map((member, memberIndex) => (
                <div className="dup-member" key={member.id}>
                  <span><LatexText text={member.headline} isFormula={(member.object_type || group.object_type) === "formula"} /> <span className="tag">{member.status}</span></span>
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
      ) : items.length === 0 ? (
        <p className="tool-hint">暂无条目。在审核队列批准对应类型的候选后会出现在这里。</p>
      ) : (
        <div className="stack">
          {items.map((item) => (
            <article className={`item ${item.status === "deprecated" ? "knowledge-deprecated" : ""}`} key={item.id}>
              <div className="knowledge-item-title">
                <KgTypeMark type={item.object_type ?? kind} />
                <span>{kgTypeLabel(item.object_type ?? kind)}</span>
                <h3><LatexText text={knowledgeHeadline(kind, item)} isFormula={(item.object_type ?? kind) === "formula"} /></h3>
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
      <Pagination page={page} pageSize={50} total={total} onPage={onPage} />
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

// Render a formula-name / headline string with KaTeX.
//   isFormula=true  -> the WHOLE text is a Formula object's name (no $ delimiters),
//                      rendered as one inline formula (raw fallback on katex error).
//   isFormula=false -> prose: inline-render `$...$` and `\( ... \)` spans, leave the
//                      rest as plain text (delimiter-free text is never mangled).
function LatexText({ text, isFormula = false }: { text: string; isFormula?: boolean }) {
  if (!text) return null;
  if (isFormula) return <InlineFormula latex={text} />;
  const segments = splitInlineLatex(text);
  if (segments.length === 1 && segments[0].type === "text") return <>{segments[0].value}</>;
  return (
    <>
      {segments.map((segment, index) =>
        segment.type === "math"
          ? <InlineFormula latex={segment.value} key={`m-${index}`} />
          : <span key={`t-${index}`}>{segment.value}</span>
      )}
    </>
  );
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

function referenceTier(reference: AnswerReference): string {
  return reference.anchor?.tier || "";
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
  const tier = referenceTier(reference);
  return (
    <aside className="cite-detail-card" aria-live="polite">
      <div className="cite-detail-head">
        <strong>{reference.displayLabel}</strong>
        {objectType && <span><KgTypeMark type={objectType} />{kgTypeLabel(objectType)}</span>}
        {tier && (
          <span className={`tier-badge tier-${tier}`} title={tier === "base" ? "来自基准库（权威参考层）" : "来自个人层"}>
            {tier === "base" ? "base" : "personal"}
          </span>
        )}
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
      <h4><LatexText text={title} isFormula={objectType === "formula"} /></h4>
      {snippet && <p><LatexText text={snippet} /></p>}
      {(source || location) && <small>{[source, location].filter(Boolean).join(" · ")}</small>}
    </aside>
  );
}

// 点击引用徽章 → 在徽章处弹出的浮层(替代原来底部固定卡 + 滚动定位,长答案不再来回跳)。
// fixed 定位锚定被点徽章;点外部 / Esc / 滚动即关闭。
function CitationPopover({
  reference,
  anchorRect,
  onClose,
  onOpenKnowledgeGraph,
}: {
  reference: AnswerReference;
  anchorRect: DOMRect;
  onClose: () => void;
  onOpenKnowledgeGraph: (objectId?: string) => void;
}) {
  const ref = useRef<HTMLDivElement | null>(null);
  const [pos, setPos] = useState<{ top: number; left: number }>(
    () => ({ top: anchorRect.bottom + 6, left: anchorRect.left })
  );
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    setPos(placeCitationPopover(
      { top: anchorRect.top, bottom: anchorRect.bottom, left: anchorRect.left },
      { width: r.width, height: r.height },
      { width: window.innerWidth, height: window.innerHeight },
    ));
  }, [anchorRect]);
  useEffect(() => {
    const onDown = (e: PointerEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose();
    };
    const onKey = (e: globalThis.KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    const onScroll = () => onClose();
    window.addEventListener("pointerdown", onDown, true);
    window.addEventListener("keydown", onKey);
    window.addEventListener("scroll", onScroll, true);
    return () => {
      window.removeEventListener("pointerdown", onDown, true);
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("scroll", onScroll, true);
    };
  }, [onClose]);
  return (
    <div ref={ref} className="cite-popover" role="dialog"
         style={{ position: "fixed", top: pos.top, left: pos.left }}>
      <SelectedReferenceDetail reference={reference} onOpenKnowledgeGraph={onOpenKnowledgeGraph} />
    </div>
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
        <span className="reasoning-trace-count">
          {summary.stepCountLabel}{summary.totalLabel ? ` · ${summary.totalLabel}` : ""}
        </span>
        {expanded ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
      </button>
      {expanded && (
        <ol className="reasoning-trace-list">
          {steps.length === 0 ? (
            <li className="reasoning-trace-empty">等待后端事件…</li>
          ) : steps.map((step, index) => {
            const detail = getTraceStepDetail(step);
            const hasTime = typeof step.duration_ms === "number";
            return (
              <li key={`${step.step_type}-${index}`} className={index === steps.length - 1 && live ? "active" : ""}>
                <span>{TRACE_STEP_LABELS[step.step_type] ?? step.step_type}</span>
                <strong>{step.summary}</strong>
                {(detail || hasTime) && (
                  <div className="reasoning-trace-meta">
                    {detail && <small>{detail}</small>}
                    {hasTime && (
                      <time className={`reasoning-trace-time ${(step.duration_ms ?? 0) >= 10000 ? "slow" : ""}`}>
                        {formatDuration(step.duration_ms ?? 0)}
                      </time>
                    )}
                  </div>
                )}
              </li>
            );
          })}
        </ol>
      )}
    </div>
  );
}

function AnswerView({
  answer,
  feedbackSent,
  onFeedback,
  onOpenKnowledgeGraph,
  notebookId,
  onBuildScaleIndex,
  buildingScaleIndex
}: {
  answer: AskResponse;
  feedbackSent: string;
  onFeedback: (rating: "useful" | "not_useful") => void;
  onOpenKnowledgeGraph: (objectId?: string) => void;
  notebookId: string | null;
  onBuildScaleIndex: (nb: string) => void;
  buildingScaleIndex: boolean;
}) {
  const [copied, setCopied] = useState(false);
  // 点击引用 → 在点击处弹浮层(reference + 被点徽章的 rect);点外部关闭。替代原底部固定卡 + 滚动。
  const [citePopover, setCitePopover] = useState<{ reference: AnswerReference; rect: DOMRect } | null>(null);
  const answerText = answer.answer || answer.conclusion || "";
  const references = useMemo(
    () => buildAnswerReferences(answerText, answer.anchors, answer.citations),
    [answerText, answer.anchors, answer.citations]
  );
  useEffect(() => {
    setCitePopover(null);
  }, [answer.answer_id]);
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
      {answer.model_errors && answer.model_errors.length > 0 && (() => {
        const labelOf = (s: string) =>
          ({ embed: "向量模型", rerank: "重排模型", answer: "答案模型", rewrite: "改写模型" } as Record<string, string>)[s] ?? s;
        const names = Array.from(new Set(answer.model_errors.map((e) => labelOf(e.stage)))).join("、");
        return (
          <div className="answer-model-error" title={answer.model_errors[0]?.message ?? ""}>
            ⚠️ 部分模型调用失败（{names}），本次为降级输出，可能不完整或未接地。请检查 API key / 模型服务可用性。
          </div>
        );
      })()}
      {answer.index_required && (
        <div className="answer-model-error" title="大库检索强制走索引;未建索引时仅有降级结果">
          <span>此知识库较大且尚未建立检索索引，当前检索能力受限。</span>
          <button
            type="button"
            className="mode-engine"
            style={{ marginLeft: 6 }}
            disabled={buildingScaleIndex}
            onClick={() => { if (notebookId) onBuildScaleIndex(notebookId); }}
          >
            {buildingScaleIndex ? "构建中…" : "构建索引"}
          </button>
        </div>
      )}
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
      {(() => {
        const { personal, base } = computeSourceTierCounts(references);
        if (personal + base === 0) return null;
        return (
          <span className="tag source-dist" title="本次引用的来源分布（个人层 / 基准库）">
            来源 · 个人 {personal}
            {base > 0 && <> · <strong className="source-dist-base">基准库 {base}</strong></>}
          </span>
        );
      })()}
      <AnswerMarkdown
        answer={answerText}
        anchors={answer.anchors}
        citations={answer.citations}
        selectedReferenceId={citePopover?.reference.id ?? null}
        onReferenceClick={(reference, event) => setCitePopover({ reference, rect: event.currentTarget.getBoundingClientRect() })}
      />
      {answer.reasoning_trace && answer.reasoning_trace.length > 0 && (
        <ReasoningTracePanel steps={answer.reasoning_trace} />
      )}
      {citePopover && (
        <CitationPopover
          reference={citePopover.reference}
          anchorRect={citePopover.rect}
          onClose={() => setCitePopover(null)}
          onOpenKnowledgeGraph={onOpenKnowledgeGraph}
        />
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
