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
  related_rules: Array<{
    id: string;
    title: string;
    statement: string;
    severity: string;
    status: string;
  }>;
  related_cases: Array<{
    id: string;
    symptom: string;
    root_cause: string;
  }>;
  checklist: string[];
  missing_information: string[];
  potential_risks: string[];
  citations: Array<{
    label: string;
    source_id: string;
    element_id: string;
    location_label: string;
    quoted_span: string;
  }>;
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

type RuleCard = {
  id: string;
  title: string;
  statement: string;
  applies_to: string[];
  recommendation: string;
  risk_if_ignored: string;
  severity: string;
  status: string;
  evidence: Evidence[];
};

type CaseCard = {
  id: string;
  symptom: string;
  context: string;
  root_cause: string;
  resolution: string;
  lesson_learned: string;
  evidence: Evidence[];
};

type ChecklistItem = {
  question: string;
  severity: string;
  required_evidence: string;
  related_rule_ids: string[];
  citations: Citation[];
};

type ScenarioForm = {
  domain: string;
  block_type: string;
  design_stage: string;
  package_type: string;
  signal_type: string;
  concern: string;
  constraint: string;
  process_or_node: string;
  application: string;
};

type ChatMode = "ask" | "scenario" | "case" | "checklist" | "rules";

const EMPTY_SCENARIO: ScenarioForm = {
  domain: "",
  block_type: "",
  design_stage: "",
  package_type: "",
  signal_type: "",
  concern: "",
  constraint: "",
  process_or_node: "",
  application: ""
};

const SCENARIO_FIELDS: Array<[keyof ScenarioForm, string, string]> = [
  ["domain", "领域", "Analog IC"],
  ["block_type", "电路模块", "Low-noise AFE"],
  ["design_stage", "设计阶段", "Package review"],
  ["package_type", "封装类型", "Wirebond / QFN"],
  ["signal_type", "信号类型", "Low-noise analog"],
  ["concern", "关注点", "Noise / ESD / parasitic"],
  ["constraint", "约束", "Cost / area / schedule"],
  ["process_or_node", "工艺/节点", "180nm BCD"],
  ["application", "应用", "Sensor frontend"]
];

const CHAT_MODES: Array<[ChatMode, string]> = [
  ["ask", "问答"],
  ["scenario", "场景查询"],
  ["case", "案例检索"],
  ["checklist", "Checklist"],
  ["rules", "知识库"]
];

type KnowledgeKind = "rule" | "method" | "risk" | "glossary";

// kind -> [label, REST path]
const KNOWLEDGE_KINDS: Array<[KnowledgeKind, string, string]> = [
  ["rule", "规则", "rules"],
  ["method", "方法", "methods"],
  ["risk", "风险", "risks"],
  ["glossary", "术语", "glossary"]
];

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
};

const EMPTY_KNOWLEDGE: Record<KnowledgeKind, KnowledgeItem[] | null> = {
  rule: null,
  method: null,
  risk: null,
  glossary: null
};

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

type RuleExplanation = {
  rule: { id: string; title: string; statement: string; status: string; owner?: string };
  origin: Citation[];
  applicable_scenario: string[];
  exception: string;
  related_cases: Array<{ id: string; symptom: string; root_cause: string }>;
  related_risks: Array<{ id: string; title: string; description: string }>;
  related_checklist: string[];
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

const prompts = [
  ["生成 package review checklist", "请基于当前来源生成 package review checklist，并给出引用。"],
  ["查询相似 debug case", "请查询和当前场景相似的 debug case，并说明 root cause 与 resolution。"],
  ["解释某条设计规则", "请解释低噪声模拟前端 wirebond pin assignment 相关的关键设计规则。"],
  ["分析文章对规则库的影响", "请分析这篇文章或来源对现有规则库的影响，并列出候选规则。"]
];

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
  const withoutExtension = rawTitle.replace(/\.(pdf|md|markdown|docx|pptx)$/i, "");
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
  if ((notebook.counts.article_claims ?? 0) > 0) return "🤖";
  if (notebook.primary_domain.toLowerCase().includes("esd")) return "▣";
  return ["◇", "📒", "📈", "▤", "▧"][index % 5];
}

export default function Home() {
  const [health, setHealth] = useState<Health | null>(null);
  const [notebooks, setNotebooks] = useState<NotebookSummary[]>([]);
  const [templates, setTemplates] = useState<Array<{ id: string; label: string }>>([]);
  const [searchHits, setSearchHits] = useState<Record<string, SearchHit[]>>({});
  const [currentNotebookId, setCurrentNotebookId] = useState<string | null>(null);
  const [currentNotebook, setCurrentNotebook] = useState<NotebookSummary | null>(null);
  const [sources, setSources] = useState<SourceSummary[]>([]);
  const [question, setQuestion] = useState("低噪声模拟前端使用 wirebond 封装时，pin assignment 需要注意什么？");
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
  const [scenarioForm, setScenarioForm] = useState<ScenarioForm>(EMPTY_SCENARIO);
  const [caseQuery, setCaseQuery] = useState("");
  const [caseResults, setCaseResults] = useState<CaseCard[] | null>(null);
  const [checklistScenario, setChecklistScenario] = useState("");
  const [checklistResults, setChecklistResults] = useState<ChecklistItem[] | null>(null);
  const [knowledgeKind, setKnowledgeKind] = useState<KnowledgeKind>("rule");
  const [knowledge, setKnowledge] = useState<Record<KnowledgeKind, KnowledgeItem[] | null>>(EMPTY_KNOWLEDGE);
  const [knowledgeStatusFilter, setKnowledgeStatusFilter] = useState("all");
  const [duplicates, setDuplicates] = useState<DuplicateGroup[] | null>(null);
  const [conflicts, setConflicts] = useState<ConflictPair[] | null>(null);
  const [ruleExplanation, setRuleExplanation] = useState<RuleExplanation | null>(null);
  const [derivedRules, setDerivedRules] = useState<DerivedRuleCandidate[] | null>(null);
  const [derivedOpen, setDerivedOpen] = useState(false);
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
          const featured =
            notebook.status === "beta-demo" ||
            (notebook.counts.rules ?? 0) > 0 ||
            (notebook.counts.cases ?? 0) > 0 ||
            (notebook.counts.article_claims ?? 0) > 0;
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

  async function loadNotebookCollection() {
    const healthResponse = await api<Health>("/health");
    const notebookResponse = await api<NotebookSummary[]>("/notebooks");
    setHealth(healthResponse);
    setStatusText(`API ${healthResponse.status}; LLM configured: ${healthResponse.llm_configured}`);
    setNotebooks(notebookResponse);
    if (templates.length === 0) {
      api<Array<{ id: string; label: string }>>("/notebook-templates")
        .then(setTemplates)
        .catch(() => undefined);
    }
  }

  async function createNotebook(template = "") {
    const notebook = await api<NotebookSummary>("/notebooks", {
      method: "POST",
      body: JSON.stringify(template ? { template } : {})
    });
    await loadNotebookCollection();
    await openNotebook(notebook.id);
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
    setScenarioForm(EMPTY_SCENARIO);
    setCaseQuery("");
    setCaseResults(null);
    setChecklistScenario("");
    setChecklistResults(null);
    setKnowledge(EMPTY_KNOWLEDGE);
    setKnowledgeKind("rule");
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
    if (knowledge[knowledgeKind] !== null) await loadKnowledge(knowledgeKind);
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

  async function uploadSources(event: ChangeEvent<HTMLInputElement>) {
    const files = Array.from(event.target.files || []);
    if (!currentNotebookId || files.length === 0) return;
    const supported = files.filter((file) => /\.(pdf|md|markdown|docx|pptx)$/i.test(file.name));
    if (supported.length === 0) {
      setStatusText("Select PDF, Markdown, DOCX, or PPTX files");
      return;
    }
    const formData = new FormData();
    supported.forEach((file) => formData.append("files", file));
    const uploaded = await api<SourceSummary[]>(`/notebooks/${currentNotebookId}/sources`, {
      method: "POST",
      body: formData
    });
    setSources((previous) => [...previous.filter((source) => !uploaded.some((item) => item.id === source.id)), ...uploaded]);
    await loadNotebookCollection();
    await loadCandidates(currentNotebookId);
    event.target.value = "";
    setSourceModalOpen(false);
    setToast(`Imported ${uploaded.length} source file(s)`);
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

  async function runScenario() {
    if (!currentNotebookId) return;
    const response = await api<AskResponse>(`/notebooks/${currentNotebookId}/scenario-query`, {
      method: "POST",
      body: JSON.stringify(scenarioForm)
    });
    setAnswer(response);
    setFeedbackSent("");
    setFeedbackComment("");
  }

  async function runCaseSearch() {
    if (!currentNotebookId) return;
    const response = await api<CaseCard[]>(`/notebooks/${currentNotebookId}/case-search`, {
      method: "POST",
      body: JSON.stringify({ query: caseQuery, context: {} })
    });
    setCaseResults(response);
  }

  async function runChecklist() {
    if (!currentNotebookId) return;
    const response = await api<ChecklistItem[]>(`/notebooks/${currentNotebookId}/checklist`, {
      method: "POST",
      body: JSON.stringify({ scenario: checklistScenario })
    });
    setChecklistResults(response);
  }

  async function loadKnowledge(kind: KnowledgeKind) {
    if (!currentNotebookId) return;
    const path = KNOWLEDGE_KINDS.find(([k]) => k === kind)?.[2] ?? "rules";
    const response = await api<KnowledgeItem[]>(`/notebooks/${currentNotebookId}/${path}`);
    setKnowledge((prev) => ({ ...prev, [kind]: response }));
  }

  async function updateKnowledge(id: string, patch: { status?: string; owner?: string }) {
    if (!currentNotebookId) return;
    await api(`/knowledge/${id}`, { method: "PATCH", body: JSON.stringify(patch) });
    await loadKnowledge(knowledgeKind);
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
    if (knowledge[kind] === null) loadKnowledge(kind).catch(reportError);
  }

  async function findDuplicates(kind: KnowledgeKind) {
    if (!currentNotebookId) return;
    setConflicts(null);
    const path = KNOWLEDGE_KINDS.find(([k]) => k === kind)?.[2] ?? "rules";
    const response = await api<DuplicateGroup[]>(
      `/notebooks/${currentNotebookId}/duplicates?type=${path}`
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
    await findDuplicates(knowledgeKind);
    setToast("已合并，源条目置为 deprecated");
  }

  async function explainRule(ruleId: string) {
    if (!currentNotebookId) return;
    const response = await api<RuleExplanation>(
      `/notebooks/${currentNotebookId}/rules/${ruleId}/explain`
    );
    setRuleExplanation(response);
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
    if (mode === "rules" && knowledge[knowledgeKind] === null) {
      loadKnowledge(knowledgeKind).catch(reportError);
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
              {templates.length > 0 && (
                <select
                  className="sort-button"
                  value=""
                  onChange={(event) => {
                    const tid = event.target.value;
                    if (tid) createNotebook(tid).catch(reportError);
                    event.currentTarget.value = "";
                  }}
                  title="从模板新建"
                >
                  <option value="">从模板…</option>
                  {templates.map((tpl) => <option key={tpl.id} value={tpl.id}>{tpl.label}</option>)}
                </select>
              )}
              <button className="new-pill" onClick={() => createNotebook().catch(reportError)}>＋ 新建</button>
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
                  <button className="notebook-card create-card" onClick={() => createNotebook().catch(reportError)}>
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
              <button className="new-pill" onClick={() => createNotebook().catch(reportError)}>＋ 创建笔记本</button>
              <button className="sort-button" onClick={() => setInfoModal({
                title: "分析",
                message: "第一版提供本机 beta 的分析入口：可以从当前来源生成 Studio 输出，或直接跑一次 evidence-grounded 回答。",
                actions: [
                  { label: "运行思维导图", primary: true, action: () => runStudio("mindmap").catch(reportError) },
                  { label: "运行信息图", action: () => runStudio("infographic").catch(reportError) },
                  { label: "运行对话分析", action: () => runAsk().catch(reportError) }
                ]
              })}>分析</button>
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
                  <input type="file" multiple accept=".pdf,.md,.markdown,.docx,.pptx" onChange={(event) => uploadSources(event).catch(reportError)} />
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
                    <p>导入来源后，你可以围绕规则、案例、风险、文章 claim 和 checklist 提问。系统会优先展示可追溯的 evidence。</p>
                    <div className="prompt-chips">
                      {prompts.map(([label, prompt]) => (
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

                {chatMode === "scenario" && (
                  <div className="tool-view">
                    <div className="scenario-grid">
                      {SCENARIO_FIELDS.map(([key, label, placeholder]) => (
                        <label key={key}>{label}
                          <input
                            value={scenarioForm[key]}
                            placeholder={placeholder}
                            maxLength={120}
                            onChange={(event) => setScenarioForm((prev) => ({ ...prev, [key]: event.target.value }))}
                          />
                        </label>
                      ))}
                    </div>
                    <div className="modal-actions">
                      <button className="sort-button" onClick={() => setScenarioForm(EMPTY_SCENARIO)}>清空</button>
                      <button className="new-pill" onClick={() => runScenario().catch(reportError)}>生成场景化回答</button>
                    </div>
                    {answer && (
                      <AnswerView
                        answer={answer}
                        feedbackSent={feedbackSent}
                        feedbackComment={feedbackComment}
                        setFeedbackComment={setFeedbackComment}
                        onFeedback={(rating) => submitFeedback(rating, feedbackComment).catch(reportError)}
                      />
                    )}
                  </div>
                )}

                {chatMode === "case" && (
                  <div className="tool-view">
                    <div className="tool-input-row">
                      <input
                        value={caseQuery}
                        placeholder="描述症状或现象，如：实验室噪声比仿真高，怀疑和封装有关"
                        onChange={(event) => setCaseQuery(event.target.value)}
                        onKeyDown={(event) => { if (event.key === "Enter") runCaseSearch().catch(reportError); }}
                      />
                      <button className="new-pill" onClick={() => runCaseSearch().catch(reportError)}>检索案例</button>
                    </div>
                    <CaseList results={caseResults} />
                  </div>
                )}

                {chatMode === "checklist" && (
                  <div className="tool-view">
                    <div className="tool-input-row">
                      <input
                        value={checklistScenario}
                        placeholder="描述要 review 的场景，如：低噪声模拟前端的 QFN 封装方案"
                        onChange={(event) => setChecklistScenario(event.target.value)}
                        onKeyDown={(event) => { if (event.key === "Enter") runChecklist().catch(reportError); }}
                      />
                      <button className="new-pill" onClick={() => runChecklist().catch(reportError)}>生成 checklist</button>
                    </div>
                    <ChecklistList results={checklistResults} />
                  </div>
                )}

                {chatMode === "rules" && (
                  <KnowledgeBrowser
                    kind={knowledgeKind}
                    items={knowledge[knowledgeKind]}
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
                    onExplain={(ruleId) => explainRule(ruleId).catch(reportError)}
                    reload={() => loadKnowledge(knowledgeKind).catch(reportError)}
                  />
                )}
              </div>
              {chatMode === "ask" && (
                <div className="chat-input-bar">
                  <textarea className="chat-input" rows={1} value={question} onChange={(event) => setQuestion(event.target.value)} />
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

      {sourceModalOpen && (
        <section className="source-modal" role="dialog" aria-modal="true" onClick={(event) => { if (event.currentTarget === event.target) setSourceModalOpen(false); }}>
          <div className="source-modal-card">
            <div className="source-modal-header">
              <div>
                <h2>添加来源</h2>
                <p>选择现有 PDF、Markdown、DOCX 或 PPTX。系统会保存文件、解析文本元素，并生成 source summary。</p>
              </div>
              <button className="icon-button" onClick={() => setSourceModalOpen(false)} title="Close">×</button>
            </div>
            <label className="drop-zone">
              <input type="file" multiple accept=".pdf,.md,.markdown,.docx,.pptx" onChange={(event) => uploadSources(event).catch(reportError)} />
              <span className="drop-plus">＋</span>
              <strong>选择来源文件</strong>
              <small>当前版本会立即解析文本元素；图片和 OCR 暂不处理。</small>
            </label>
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
                  <p>上传并解析来源后，系统会自动抽取规则、方法、风险、案例、checklist 和术语候选。</p>
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

      {ruleExplanation && (
        <section className="utility-modal" role="dialog" aria-modal="true" onClick={(event) => { if (event.currentTarget === event.target) setRuleExplanation(null); }}>
          <div className="utility-modal-card">
            <div className="source-modal-header">
              <div>
                <h2>为什么有这条规则</h2>
                <p>{ruleExplanation.rule.title || ruleExplanation.rule.id}</p>
              </div>
              <button className="icon-button" onClick={() => setRuleExplanation(null)} title="Close">×</button>
            </div>
            <div className="source-detail-body">
              <p>{ruleExplanation.rule.statement}</p>
              {ruleExplanation.applicable_scenario.length > 0 && (
                <div className="tag-row">
                  {ruleExplanation.applicable_scenario.map((scope) => <span className="tag" key={scope}>{scope}</span>)}
                </div>
              )}
              {ruleExplanation.exception && <p><strong>例外：</strong>{ruleExplanation.exception}</p>}
              <p className="section-title">来源 / 形成依据</p>
              {ruleExplanation.origin.length > 0 ? ruleExplanation.origin.map((citation, index) => (
                <div className="citation" key={`${citation.label}-${index}`}>
                  <strong>{citation.label}</strong>
                  <div>{citation.location_label}</div>
                  <div>{citation.quoted_span}</div>
                </div>
              )) : <p className="tool-hint">该规则暂无可追溯的来源证据。</p>}
              {ruleExplanation.related_cases.length > 0 && (
                <>
                  <p className="section-title">相关案例</p>
                  <div className="stack">
                    {ruleExplanation.related_cases.map((caseCard) => (
                      <article className="item" key={caseCard.id}>
                        <h3>{caseCard.symptom || caseCard.id}</h3>
                        {caseCard.root_cause && <p><strong>根因：</strong>{caseCard.root_cause}</p>}
                      </article>
                    ))}
                  </div>
                </>
              )}
              {ruleExplanation.related_risks.length > 0 && (
                <>
                  <p className="section-title">相关风险</p>
                  <div className="tag-row">{ruleExplanation.related_risks.map((risk) => <span className="tag" key={risk.id}>{risk.title}</span>)}</div>
                </>
              )}
              {ruleExplanation.related_checklist.length > 0 && (
                <>
                  <p className="section-title">相关检查项</p>
                  <div className="stack">{ruleExplanation.related_checklist.map((q) => <div className="checklist-row" key={q}>{q}</div>)}</div>
                </>
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

function CaseList({ results }: { results: CaseCard[] | null }) {
  if (results === null) {
    return <p className="tool-hint">输入症状或现象后检索相似历史案例。</p>;
  }
  if (results.length === 0) {
    return <p className="tool-hint">没有匹配的案例。上传并审核 case 候选后可检索。</p>;
  }
  return (
    <div className="stack">
      {results.map((caseCard) => (
        <article className="item" key={caseCard.id}>
          <h3>{caseCard.symptom || caseCard.id}</h3>
          {caseCard.context && <p><strong>背景：</strong>{caseCard.context}</p>}
          {caseCard.root_cause && <p><strong>根因：</strong>{caseCard.root_cause}</p>}
          {caseCard.resolution && <p><strong>解决：</strong>{caseCard.resolution}</p>}
          {caseCard.lesson_learned && <p><strong>经验：</strong>{caseCard.lesson_learned}</p>}
          <EvidenceLine evidence={caseCard.evidence} />
        </article>
      ))}
    </div>
  );
}

function ChecklistList({ results }: { results: ChecklistItem[] | null }) {
  if (results === null) {
    return <p className="tool-hint">描述 review 场景后生成可执行 checklist。</p>;
  }
  if (results.length === 0) {
    return <p className="tool-hint">没有生成 checklist。上传并审核 checklist 或 rule 候选后可生成。</p>;
  }
  return (
    <div className="stack">
      {results.map((item, index) => (
        <article className="item" key={`${item.question}-${index}`}>
          <h3>{item.question}</h3>
          <div className="tag-row">
            <span className={`tag severity-${item.severity}`}>{item.severity}</span>
            {item.related_rule_ids.map((id) => <span className="tag" key={id}>{id}</span>)}
          </div>
          {item.required_evidence && <p><strong>需要证据：</strong>{item.required_evidence}</p>}
          {item.citations.map((citation, citationIndex) => (
            <div className="citation" key={`${citation.label}-${citationIndex}`}>
              <strong>{citation.label}</strong>
              <div>{citation.location_label}</div>
              <div>{citation.quoted_span}</div>
            </div>
          ))}
        </article>
      ))}
    </div>
  );
}

function knowledgeHeadline(kind: KnowledgeKind, item: KnowledgeItem): string {
  if (kind === "method") return item.name || item.id;
  if (kind === "glossary") return item.term || item.id;
  return item.title || item.id; // rule / risk
}

function knowledgeBody(kind: KnowledgeKind, item: KnowledgeItem) {
  if (kind === "rule") {
    return (
      <>
        {item.statement && <p>{item.statement}</p>}
        {item.recommendation && <p><strong>建议：</strong>{item.recommendation}</p>}
        {item.risk_if_ignored && <p><strong>忽略风险：</strong>{item.risk_if_ignored}</p>}
      </>
    );
  }
  if (kind === "method") {
    return (
      <>
        {item.use_when && <p><strong>适用：</strong>{item.use_when}</p>}
        {item.benefit && <p><strong>收益：</strong>{item.benefit}</p>}
        {item.limitation && <p><strong>限制：</strong>{item.limitation}</p>}
      </>
    );
  }
  if (kind === "risk") return item.description ? <p>{item.description}</p> : null;
  return item.definition ? <p>{item.definition}</p> : null; // glossary
}

function KnowledgeBrowser({
  kind,
  items,
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
  onExplain,
  reload
}: {
  kind: KnowledgeKind;
  items: KnowledgeItem[] | null;
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
  onExplain: (ruleId: string) => void;
  reload: () => void;
}) {
  const statuses = ["all", ...Array.from(new Set((items ?? []).map((item) => item.status).filter(Boolean)))];
  const filtered = (items ?? []).filter((item) => statusFilter === "all" || item.status === statusFilter);
  return (
    <div className="tool-view">
      <div className="knowledge-kind-tabs">
        {KNOWLEDGE_KINDS.map(([k, label]) => (
          <button
            key={k}
            className={`chat-tab ${kind === k ? "active" : ""}`}
            onClick={() => onKind(k)}
          >{label}</button>
        ))}
      </div>
      <div className="tool-input-row">
        <select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)}>
          {statuses.map((value) => <option key={value} value={value}>{value === "all" ? "全部状态" : value}</option>)}
        </select>
        <button className="sort-button" onClick={reload}>刷新</button>
        <button className="sort-button" onClick={onFindDuplicates}>查重</button>
        {kind === "rule" && <button className="sort-button" onClick={onFindConflicts}>冲突</button>}
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
                {kind === "rule" && (
                  <button className="sort-button" onClick={() => onExplain(item.id)}>解释</button>
                )}
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
      <div className="chat-answer-grid">
        <div>
          <p className="section-title">Related Rules</p>
          <div className="stack">
            {answer.related_rules.map((rule) => (
              <article className="item" key={rule.id}>
                <h3>{rule.id}: {rule.title}</h3>
                <p>{rule.statement}</p>
                <div className="tag-row"><span className={`tag severity-${rule.severity}`}>{rule.severity}</span><span className="tag">{rule.status}</span></div>
              </article>
            ))}
          </div>
          <p className="section-title">Related Cases</p>
          <div className="stack">
            {answer.related_cases.map((caseCard) => (
              <article className="item" key={caseCard.id}>
                <h3>{caseCard.id}</h3>
                <p>{caseCard.symptom}</p>
                <p><strong>Root cause:</strong> {caseCard.root_cause}</p>
              </article>
            ))}
          </div>
          <p className="section-title">Checklist</p>
          <div className="stack">{answer.checklist.map((item) => <div className="checklist-row" key={item}>{item}</div>)}</div>
        </div>
        <div>
          <p className="section-title">Missing Information</p>
          <div className="tag-row">{answer.missing_information.map((item) => <span className="tag" key={item}>{item}</span>)}</div>
          <p className="section-title">Risks</p>
          <div className="tag-row">{answer.potential_risks.map((item) => <span className="tag" key={item}>{item}</span>)}</div>
        </div>
      </div>
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
