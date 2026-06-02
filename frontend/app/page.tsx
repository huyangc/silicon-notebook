"use client";

import { ChangeEvent, FormEvent, KeyboardEvent, MouseEvent, useEffect, useMemo, useRef, useState } from "react";
import { ExternalLink, FileText, PanelRightClose, Plus, Search, Sparkles, Trash2 } from "lucide-react";
import katex from "katex";
import "katex/dist/katex.min.css";

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

type Candidate = {
  id: string;
  notebook_id: string;
  source_title: string;
  candidate_type: string;
  status: string;
  payload: Record<string, unknown>;
  evidence: Evidence[];
  created_label: string;
};

type ArticleSummary = {
  id: string;
  notebook_id: string;
  source_id: string;
  title: string;
  status: string;
  summary: string;
};

type AskResponse = {
  answer_id: string;
  conclusion: string;
  related_knowledge: KnowledgeRecord[];
  citations: Citation[];
  llm_mode: string;
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
  headline: string;
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
type ConflictPair = { object_type: string; reason: string; a: KnowledgeRef; b: KnowledgeRef };

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

const RELATION_LABELS: Record<string, string> = {
  related_concepts: "关联概念",
  related_claims: "关联论断",
  related_formulas: "关联公式",
  related_procedures: "关联过程"
};

type StudioOutput = {
  title: string;
  sections: Array<[string, string[]]>;
};

type InfoModal = {
  title: string;
  message: string;
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

// Quick-prompt chips for a notebook: prefer its own expected questions
// (set at creation from the template/user input), else neutral fallbacks.
function promptChipsFor(notebook: NotebookSummary | null): Array<[string, string]> {
  const expected = (notebook?.expected_questions ?? []).map((q) => q.trim()).filter(Boolean);
  if (expected.length > 0) {
    return expected.slice(0, 4).map((q) => [chipLabel(q), q] as [string, string]);
  }
  return GENERIC_PROMPTS;
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
  const [answer, setAnswer] = useState<AskResponse | null>(null);
  const [studioOutput, setStudioOutput] = useState<StudioOutput | null>(null);
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
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [reviewOpen, setReviewOpen] = useState(false);
  const [feedbackSent, setFeedbackSent] = useState("");
  const [feedbackComment, setFeedbackComment] = useState("");
  const [articleModalOpen, setArticleModalOpen] = useState(false);
  const [articles, setArticles] = useState<ArticleSummary[]>([]);
  const [selectedArticleId, setSelectedArticleId] = useState("");
  const [chatMode, setChatMode] = useState<ChatMode>("ask");
  const [knowledgeKind, setKnowledgeKind] = useState<KnowledgeKind>("concept");
  const [knowledge, setKnowledge] = useState<Record<string, KnowledgeItem[] | null>>(EMPTY_KNOWLEDGE);
  const [knowledgeTypes, setKnowledgeTypes] = useState<KnowledgeTypeCount[]>([]);
  const [knowledgeStatusFilter, setKnowledgeStatusFilter] = useState("all");
  const [duplicates, setDuplicates] = useState<DuplicateGroup[] | null>(null);
  const [conflicts, setConflicts] = useState<ConflictPair[] | null>(null);
  const [derivedRules, setDerivedRules] = useState<DerivedRuleCandidate[] | null>(null);
  const [derivedOpen, setDerivedOpen] = useState(false);
  const [analytics, setAnalytics] = useState<NotebookAnalytics | null>(null);
  const [schemaModalOpen, setSchemaModalOpen] = useState(false);
  const [schemas, setSchemas] = useState<ObjectSchema[] | null>(null);
  const [schemaBusy, setSchemaBusy] = useState(false);
  const [graph, setGraph] = useState<KnowledgeGraph | null>(null);
  const [graphOpen, setGraphOpen] = useState(false);
  const [highlightedElementId, setHighlightedElementId] = useState("");
  const pollCountRef = useRef(0);
  const notebookMenuRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    loadNotebookCollection().catch(reportError);
  }, []);

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
          await loadCandidates(currentNotebookId);
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

  // Example prompts / placeholders adapt to the open notebook's template-seeded
  // expected questions and domain, so a new notebook never shows demo examples.
  const promptChips = useMemo(() => promptChipsFor(currentNotebook), [currentNotebook]);
  const askHint = useMemo(() => askPlaceholder(currentNotebook), [currentNotebook]);

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

  function openCreate() {
    setCreateName("");
    setCreateDesc("");
    setCreateOpen(true);
  }

  async function submitCreate() {
    const notebook = await api<NotebookSummary>("/notebooks", {
      method: "POST",
      body: JSON.stringify({ name: createName.trim() || "未命名笔记本", purpose: createDesc.trim() })
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
    setAnswer(null);
    setStudioOutput(null);
    setFeedbackSent("");
    setFeedbackComment("");
    setChatMode("ask");
    setKnowledge(EMPTY_KNOWLEDGE);
    setKnowledgeKind("concept");
    setKnowledgeStatusFilter("all");
    setDuplicates(null);
    setConflicts(null);
    pollCountRef.current = 0;
    await loadCandidates(notebookId);
    window.history.replaceState(null, "", `#notebook=${encodeURIComponent(notebookId)}`);
    window.scrollTo(0, 0);
  }

  async function loadCandidates(notebookId: string) {
    const list = await api<Candidate[]>(`/notebooks/${notebookId}/candidates`);
    setCandidates(list);
  }

  async function loadArticles(notebookId: string) {
    const list = await api<ArticleSummary[]>(`/notebooks/${notebookId}/articles`);
    setArticles(list);
    setSelectedArticleId((previous) =>
      list.some((article) => article.id === previous) ? previous : list[0]?.id ?? ""
    );
    return list;
  }

  async function approveCandidate(candidateId: string) {
    if (!currentNotebookId) return;
    await api<Candidate>(`/candidates/${candidateId}/approve`, { method: "POST" });
    await loadCandidates(currentNotebookId);
    await loadNotebookCollection();
    const refreshed = await api<NotebookSummary>(`/notebooks/${currentNotebookId}`);
    setCurrentNotebook(refreshed);
    if (knowledge[knowledgeKind] != null) await loadKnowledge(knowledgeKind);
    await loadKnowledgeTypes();
    setToast("候选已批准并加入知识库");
  }

  async function rejectCandidate(candidateId: string) {
    if (!currentNotebookId) return;
    await api<Candidate>(`/candidates/${candidateId}/reject`, { method: "POST" });
    await loadCandidates(currentNotebookId);
    setToast("候选已拒绝");
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

  async function submitFeedback(rating: "useful" | "not_useful", comment: string) {
    if (!answer?.answer_id) return;
    await api(`/answers/${answer.answer_id}/feedback`, {
      method: "POST",
      body: JSON.stringify({ rating, comment })
    });
    setFeedbackSent(rating);
    setToast("感谢反馈");
  }

  function showCollection() {
    setCurrentNotebookId(null);
    setCurrentNotebook(null);
    setSources([]);
    setArticles([]);
    setTitleDraft("");
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
    await loadCandidates(currentNotebookId);
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
    if (currentNotebookId) await loadCandidates(currentNotebookId);
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
    await loadCandidates(notebookId);
    await loadNotebookCollection();
    const refreshed = await api<NotebookSummary>(`/notebooks/${notebookId}`);
    setCurrentNotebook(refreshed);
    setKnowledge(EMPTY_KNOWLEDGE);
    setDuplicates(null);
    setConflicts(null);
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
    setStudioOutput(null);
    await loadNotebookCollection();
    const refreshed = await api<NotebookSummary>(`/notebooks/${currentNotebookId}`);
    setCurrentNotebook(refreshed);
    setToast("文章已删除");
  }

  async function runAsk(nextQuestion = question) {
    if (!currentNotebookId) return;
    if (!nextQuestion.trim()) return;
    setChatMode("ask");
    const response = await api<AskResponse>(`/notebooks/${currentNotebookId}/ask`, {
      method: "POST",
      body: JSON.stringify({ question: nextQuestion, scenario: {} })
    });
    setAnswer(response);
    setFeedbackSent("");
    setFeedbackComment("");
    setQuestion(nextQuestion);
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
  }

  async function updateKnowledge(id: string, patch: { status?: string; owner?: string }) {
    if (!currentNotebookId) return;
    await api(`/knowledge/${id}`, { method: "PATCH", body: JSON.stringify(patch) });
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
    setConflicts(null);
    if (knowledge[kind] == null) loadKnowledge(kind).catch(reportError);
  }

  async function findDuplicates(kind: KnowledgeKind) {
    if (!currentNotebookId) return;
    setConflicts(null);
    const response = await api<DuplicateGroup[]>(
      `/notebooks/${currentNotebookId}/duplicates?type=${encodeURIComponent(kind)}`
    );
    setDuplicates(response);
  }

  async function findConflicts() {
    if (!currentNotebookId) return;
    setDuplicates(null);
    const response = await api<ConflictPair[]>(`/notebooks/${currentNotebookId}/conflicts`);
    setConflicts(response);
  }

  async function mergeKnowledge(sourceId: string, intoId: string) {
    if (!currentNotebookId) return;
    await api(`/knowledge/${sourceId}/merge`, {
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
    await api(`/derived-rules/${candidateId}/${decision}`, { method: "POST" });
    await openDerivedRules();
    if (decision === "approve") {
      if (knowledge.rule !== null) await loadKnowledge("rule");
      await loadNotebookCollection();
      setToast("派生规则已批准并加入规则库");
    } else {
      setToast("派生规则候选已拒绝");
    }
  }

  function switchChatMode(mode: ChatMode) {
    setChatMode(mode);
    if (mode === "rules") {
      loadKnowledgeTypes().catch(reportError);
      if (knowledge[knowledgeKind] == null) {
        loadKnowledge(knowledgeKind).catch(reportError);
      }
    }
  }

  async function runStudio(kind: "mindmap" | "infographic") {
    if (!currentNotebookId) return;
    const notebookArticles = articles.length > 0 ? articles : await loadArticles(currentNotebookId);
    if (notebookArticles.length === 0) {
      setStudioOutput({
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
      setStudioOutput({
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
    setStudioOutput({
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
              <button className="new-pill" onClick={openCreate}>＋ 新建</button>
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
                  <button className="notebook-card create-card" onClick={openCreate}>
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
            <div className="workspace-actions">
              <button className="new-pill" onClick={openCreate}>＋ 创建笔记本</button>
              <button className="sort-button" onClick={() => setInfoModal({
                title: "分析",
                message: "第一版提供本机 beta 的分析入口：可以从当前来源生成 Studio 输出，或直接跑一次 evidence-grounded 回答。",
                actions: [
                  { label: "运行思维导图", primary: true, action: () => runStudio("mindmap").catch(reportError) },
                  { label: "运行信息图", action: () => runStudio("infographic").catch(reportError) },
                  { label: "运行对话分析", action: () => runAsk().catch(reportError) }
                ]
              })}>分析</button>
              <button className="sort-button" onClick={() => openAnalytics().catch(reportError)}>看板</button>
              <button className="sort-button" onClick={openSchemas}>Schema</button>
              <button className="sort-button" onClick={() => openGraph().catch(reportError)}>关系图</button>
              <button className="sort-button" onClick={() => setInfoModal({
                title: "分享",
                message: "当前是本机单用户 beta，分享会生成本地 notebook 链接；多人权限后续再接入。",
                actions: [{ label: "复制本机链接", primary: true, action: () => navigator.clipboard?.writeText(window.location.href).then(() => setToast("本机链接已复制")).catch(() => setStatusText(window.location.href)) }]
              })}>分享</button>
              <button className="sort-button" onClick={() => setInfoModal({
                title: "设置",
                message: `${health?.llm_configured ? "LLM 已配置" : "LLM 尚未配置"}。当前设置页先保留状态与 notebook 编辑入口。`,
                actions: [{ label: "编辑当前 notebook", primary: true, action: () => setEditingNotebook(currentNotebook) }]
              })}>设置</button>
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
                <button className="add-source-button review-queue-button" onClick={() => setReviewOpen(true)}>
                  ⚖ 审核队列{candidates.length > 0 ? ` · ${candidates.length}` : ""}
                </button>
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
                <button className="icon-button compact" title="Clear" onClick={() => setInfoModal({
                  title: "对话",
                  message: "当前对话可以清空回到欢迎状态；历史记录和多轮上下文将在后续版本接入。",
                  actions: [{ label: "清空对话", primary: true, action: () => setAnswer(null) }]
                })}>⋮</button>
              </div>
              <div className={`chat-body ${chatMode !== "ask" || answer ? "answer-mode" : ""}`}>
                {chatMode === "ask" && (!answer ? (
                  <div className="welcome">
                    <div className="wave">👋</div>
                    <h2>Build a source-grounded engineering notebook</h2>
                    <p>导入来源后，你可以围绕概念、论断、公式和过程提问。系统会优先展示可追溯的 evidence。</p>
                    <div className="prompt-chips">
                      {promptChips.map(([label, prompt]) => (
                        <button key={label} onClick={() => runAsk(prompt).catch(reportError)}>{label}</button>
                      ))}
                    </div>
                  </div>
                ) : (
                  <AnswerView
                    answer={answer}
                    feedbackSent={feedbackSent}
                    feedbackComment={feedbackComment}
                    setFeedbackComment={setFeedbackComment}
                    onFeedback={(rating) => submitFeedback(rating, feedbackComment).catch(reportError)}
                  />
                ))}

                {chatMode === "rules" && (
                  <KnowledgeBrowser
                    kind={knowledgeKind}
                    items={knowledge[knowledgeKind] ?? null}
                    types={knowledgeTypes}
                    statusFilter={knowledgeStatusFilter}
                    duplicates={duplicates}
                    conflicts={conflicts}
                    onKind={switchKnowledgeKind}
                    setStatusFilter={setKnowledgeStatusFilter}
                    onStatus={(id, status) => updateKnowledge(id, { status }).catch(reportError)}
                    onOwner={(id, owner) => updateKnowledge(id, { owner }).catch(reportError)}
                    onFindDuplicates={() => findDuplicates(knowledgeKind).catch(reportError)}
                    onFindConflicts={() => findConflicts().catch(reportError)}
                    onMerge={(sourceId, intoId) => mergeKnowledge(sourceId, intoId).catch(reportError)}
                    reload={() => loadKnowledge(knowledgeKind).catch(reportError)}
                  />
                )}
              </div>
              {chatMode === "ask" && (
                <div className="chat-input-bar">
                  <textarea className="chat-input" rows={1} placeholder={askHint} value={question} onChange={(event) => setQuestion(event.target.value)} />
                  <span>{sources.length} 个来源</span>
                  <button className="send-button" onClick={() => runAsk().catch(reportError)}>→</button>
                </div>
              )}
            </section>

            <aside className="workspace-panel studio-panel">
              <div className="workspace-panel-header">
                <h2>Studio</h2>
                <span className="panel-count">输出</span>
              </div>
              <div className="studio-body">
                <div className="studio-actions">
                  <button className="studio-tile mindmap" onClick={() => runStudio("mindmap").catch(reportError)}><span>◇</span><strong>思维导图</strong></button>
                  <button className="studio-tile slides" onClick={() => setArticleModalOpen(true)}><span>＋</span><strong>新建文章</strong></button>
                  <button className="studio-tile infographic" onClick={() => runStudio("infographic").catch(reportError)}><span>▤</span><strong>信息图</strong></button>
                  <button className="studio-tile mindmap" onClick={() => openDerivedRules().catch(reportError)}><span>⚖</span><strong>派生规则候选</strong></button>
                </div>
                {articles.length > 0 && (
                  <div className="article-stack">
                    <div className="article-stack-header">
                      <span>文章</span>
                      <span>{articles.length} 篇</span>
                    </div>
                    {articles.map((article) => (
                      <article className="article-row" key={article.id}>
                        <div className="article-row-main">
                          <strong title={article.title}>{article.title}</strong>
                          <span>{article.status} · {article.summary}</span>
                        </div>
                        <button className="article-delete-button" title="删除文章" onClick={() => confirmDeleteArticle(article)}>
                          <Trash2 size={15} />
                        </button>
                      </article>
                    ))}
                  </div>
                )}
                <div className="studio-output">
                  {!studioOutput ? (
                    <div className="studio-empty">
                      <span>✦</span>
                      <strong>Studio 输出将保存在此处。</strong>
                      <p>添加来源后，可生成思维导图和信息图；演示文稿当前不可用。</p>
                    </div>
                  ) : (
                    <div className="stack">
                      <article className="item"><h3>{studioOutput.title}</h3><p>Generated from the current notebook sources.</p></article>
                      {studioOutput.sections.map(([title, values]) => (
                        <article className="item" key={title}>
                          <h3>{title}</h3>
                          <p>{values.join(" / ")}</p>
                        </article>
                      ))}
                    </div>
                  )}
                </div>
              </div>
            </aside>
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

      {reviewOpen && (
        <section className="utility-modal" role="dialog" aria-modal="true" onClick={(event) => { if (event.currentTarget === event.target) setReviewOpen(false); }}>
          <div className="utility-modal-card">
            <div className="source-modal-header">
              <div>
                <h2>审核队列</h2>
                <p>抽取的候选知识需要 curator 审核。批准后会进入知识库，并可在问答中被检索和引用。</p>
              </div>
              <button className="icon-button" onClick={() => setReviewOpen(false)} title="Close">×</button>
            </div>
            <div className="source-detail-body">
              {candidates.length === 0 ? (
                <article className="item">
                  <h3>暂无候选</h3>
                  <p>上传并解析来源后，系统会自动抽取概念、论断、公式、过程候选。</p>
                </article>
              ) : (
                <div className="stack">
                  {candidates.map((candidate) => (
                    <article className="item" key={candidate.id}>
                      <div className="tag-row">
                        <span className="tag">{candidate.candidate_type}</span>
                        <span className={`tag severity-${candidate.status === "needs_review" ? "medium" : "low"}`}>{candidate.status}</span>
                        {candidate.source_title && <span className="tag">{candidate.source_title}</span>}
                      </div>
                      <h3>{candidateHeadline(candidate)}</h3>
                      {candidateDetail(candidate) && <p>{candidateDetail(candidate)}</p>}
                      {candidate.evidence.length > 0 && (
                        <div className="citation">
                          <strong>Evidence</strong>
                          <div>{candidate.evidence[0].location_label}</div>
                          <div>{candidate.evidence[0].quoted_span}</div>
                        </div>
                      )}
                      <div className="modal-actions">
                        <button className="sort-button" onClick={() => rejectCandidate(candidate.id).catch(reportError)}>拒绝</button>
                        <button className="new-pill" onClick={() => approveCandidate(candidate.id).catch(reportError)}>批准</button>
                      </div>
                    </article>
                  ))}
                </div>
              )}
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
              <p className="section-title">审核队列</p>
              <div className="tag-row">
                {Object.entries(analytics.candidate_counts).map(([k, v]) => <span className="tag" key={k}>{k}: {v}</span>)}
                {Object.keys(analytics.candidate_counts).length === 0 && <span className="tool-hint">暂无候选</span>}
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

      {toast && <div className="toast">{toast}</div>}
    </div>
  );
}

function candidateHeadline(candidate: Candidate): string {
  const payload = candidate.payload;
  const key = ["title", "name", "term", "symptom", "question"].find(
    (field) => typeof payload[field] === "string" && (payload[field] as string).trim()
  );
  if (key) return String(payload[key]);
  const first = Object.values(payload).find((value) => typeof value === "string" && value.trim());
  return first ? String(first) : candidate.candidate_type;
}

function candidateDetail(candidate: Candidate): string {
  const payload = candidate.payload;
  const key = ["statement", "definition", "description", "benefit", "root_cause", "required_evidence"].find(
    (field) => typeof payload[field] === "string" && (payload[field] as string).trim()
  );
  return key ? String(payload[key]) : "";
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
  conflicts,
  onKind,
  setStatusFilter,
  onStatus,
  onOwner,
  onFindDuplicates,
  onFindConflicts,
  onMerge,
  reload
}: {
  kind: KnowledgeKind;
  items: KnowledgeItem[] | null;
  types: KnowledgeTypeCount[];
  statusFilter: string;
  duplicates: DuplicateGroup[] | null;
  conflicts: ConflictPair[] | null;
  onKind: (kind: KnowledgeKind) => void;
  setStatusFilter: (value: string) => void;
  onStatus: (id: string, status: string) => void;
  onOwner: (id: string, owner: string) => void;
  onFindDuplicates: () => void;
  onFindConflicts: () => void;
  onMerge: (sourceId: string, intoId: string) => void;
  reload: () => void;
}) {
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
        <button className="sort-button" onClick={onFindConflicts}>冲突</button>
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
      {conflicts !== null && (
        <div className="knowledge-panel">
          <p className="section-title">冲突（同范围、取向相反）</p>
          {conflicts.length === 0 ? (
            <p className="tool-hint">未发现冲突。</p>
          ) : conflicts.map((pair, index) => (
            <article className="item" key={`conf-${index}`}>
              <p>{pair.reason}</p>
              <div className="dup-member"><span>{pair.a.headline} <span className="tag">{pair.a.status}</span></span></div>
              <div className="dup-member"><span>{pair.b.headline} <span className="tag">{pair.b.status}</span></span></div>
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
              <h3>{knowledgeHeadline(kind, item)}</h3>
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
              </div>
              <EvidenceLine evidence={item.evidence} />
            </article>
          ))}
        </div>
      )}
    </div>
  );
}

function AnswerView({
  answer,
  feedbackSent,
  feedbackComment,
  setFeedbackComment,
  onFeedback
}: {
  answer: AskResponse;
  feedbackSent: string;
  feedbackComment: string;
  setFeedbackComment: (value: string) => void;
  onFeedback: (rating: "useful" | "not_useful") => void;
}) {
  return (
    <div className="chat-answer">
      <p>{answer.conclusion}</p>
      <div className="answer-feedback">
        <textarea
          value={feedbackComment}
          disabled={Boolean(feedbackSent)}
          rows={2}
          maxLength={500}
          placeholder="补充反馈（可选）"
          onChange={(event) => setFeedbackComment(event.target.value)}
        />
        <div className="tag-row">
          <button
            className={`sort-button ${feedbackSent === "useful" ? "active" : ""}`}
            disabled={Boolean(feedbackSent)}
            onClick={() => onFeedback("useful")}
          >👍 有用</button>
          <button
            className={`sort-button ${feedbackSent === "not_useful" ? "active" : ""}`}
            disabled={Boolean(feedbackSent)}
            onClick={() => onFeedback("not_useful")}
          >👎 需改进</button>
          {feedbackSent && <span className="tag">已记录反馈</span>}
        </div>
      </div>
      {answer.related_knowledge.length > 0 && (
        <div className="chat-answer-grid">
          <div>
            <p className="section-title">相关知识</p>
            <div className="stack">
              {answer.related_knowledge.map((record) => (
                <article className="item" key={record.id}>
                  <div className="tag-row"><span className="tag">{record.object_type}</span><span className="tag">{record.status}</span></div>
                  <h3>{record.headline}</h3>
                  {record.evidence.length > 0 && (
                    <div className="citation">
                      <strong>Evidence</strong>
                      <div>{record.evidence[0].location_label}</div>
                      <div>{record.evidence[0].quoted_span}</div>
                    </div>
                  )}
                </article>
              ))}
            </div>
          </div>
        </div>
      )}
      <div className="chat-citations">
        <p className="section-title">Citations</p>
        <div className="stack">
          {answer.citations.map((citation, index) => (
            <div className="citation" key={`${citation.label}-${index}`}>
              <strong>{citation.label}</strong>
              <div>{citation.location_label}</div>
              <div>{citation.quoted_span}</div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
