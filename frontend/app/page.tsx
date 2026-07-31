"use client";

import { ChangeEvent, FormEvent, Fragment, KeyboardEvent as ReactKeyboardEvent, MouseEvent, useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle, ArrowLeft, BarChart3, Check, ChevronRight, Cpu, Database, Edit3, ExternalLink, FileText, GitMerge, LayoutDashboard, LayoutGrid, List as ListIcon, Loader2, Network, PanelLeftClose, PanelLeftOpen, Plus, Settings, Share2, Sparkles, Table2, Trash2, Upload, User, X } from "lucide-react";
import "katex/dist/katex.min.css";
import dynamic from "next/dynamic";
import type { ReasoningTraceStep } from "./ask-stream";
import { AnswerView, LatexText, ReasoningTracePanel } from "./answer-panel";
import { AuthedImage } from "./authed-image";
import { FormulaView } from "./formula-view";
import { KgEvidenceBody } from "./kg-evidence-body";
import { MemoryPanel, MemorySaveDialog } from "./memory-panel";
import { KnowhowPanel } from "./knowhow-panel";
import { ContentOverviewCards } from "./content-overview-cards";
import { AnalyticsLoadScope, startAnalyticsLoads } from "./analytics-loaders";
import { sourceAnomalies } from "./anomaly-severity";
import { AnomalyBadge } from "./anomaly-badge";
import {
  answerIdBatches,
  collectSavedAnswerFlags,
  memoryHash,
  notebookHash,
  parseMemoryHash,
  parseWorkspaceHash,
  type MemoryNavigationTarget,
} from "./memory-model";
import { fetchKnowhowTables, type KnowhowHealthFilter } from "./knowhow-model";
import {
  CLOSED_KNOWHOW_NAVIGATION,
  closeKnowhowNavigation,
  openKnowhowNavigation,
} from "./knowhow-navigation";
import { KG_TYPE_STYLE, KgTypeMark, kgTypeLabel } from "./kg-type-mark";
import { KgAnalysisView } from "./kg-analysis-view";
import { kgBandTarget, kgBandVelocity, kgTypeBandTargets } from "./kg-layout";
import { withoutDecidedMerge } from "./kg-merge-model";
import {
  ASK_MODE_GROUPS, DEFAULT_ASK_MODE, type AskModeId,
  groupOf, groupLabel, modesInGroup, defaultModeForGroup, requiresKg, modeFromTurn,
  streamsTrace,
} from "./ask-modes";
import {
  ASK_RETRIEVAL_EFFORT_OPTIONS,
  DEFAULT_ASK_RETRIEVAL_EFFORT,
  retrievalEffortFromTurn,
  type AskRetrievalEffortId,
} from "./ask-retrieval-effort";
import { EffortPicker } from "./effort-picker";
import {
  approvePromotion,
  fetchPromotionQueue,
  proposePromotion,
  rejectPromotion,
  type PromotionCandidate,
} from "./promotion-queue";
import { promotionReviewSections } from "./promotion-review";
import { setNotebookTier, tierActionState } from "./notebook-tier";
import {
  groupMountable,
  listBases,
  listMountable,
  mergeMountCandidates,
  mountCostHint,
  mountedByCount,
  resolvePromotionTarget,
  setBases,
  shouldShowBorrowedBaseHint,
  toMountedBases,
  type MountedBase,
  type NotebookRef,
} from "./notebook-bases";
import {
  describeScaleIndex, latestScaleIndexDoneKey, queuedScaleIndexImmediateOp, scaleIndexOpConfirm, SCALE_OP_MODE, UNINDEXED_SCOPE_HINT,
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
import {
  defaultNotebookPayload,
  namedNotebookPayload,
  normalizedNotebookName,
} from "./notebook-creation";
import { fetchEdgeReviewQueue, reviewRelation, type EdgeReviewItem } from "./edge-review-queue";
import {
  conversationCleanupToast,
  conversationsOlderThan,
  CLEANUP_PRESETS,
  reconcileConversationCleanup,
  runOwnedConversationCleanup,
} from "./conversation-cleanup";
import { fetchMe, logoutUser, type AuthUser } from "./auth";
import { API_BASE } from "./api-config";
import { clearToken, getToken } from "./auth-session";
import { logDiagnostic, toUserMessage } from "./errors.ts";
import { fetchDocumentTypes, fetchHealth, fetchSystemConfiguration, probeReady, type ReadySnapshot } from "./system-api";
import { backfillPaperMetadata, createNotebook, deleteNotebook as deleteNotebookRequest, fetchNotebookAnalytics, fetchNotebookContentOverview, getNotebook, listNotebooks, updateNotebook } from "./notebook-api";
import { deleteSource as deleteSourceRequest, detectSourceTypes, getSource, getSourceElements, getNotebookSource, getNotebookSourceElements, importUrlSources, listSources, parseSource, uploadSources, fetchCheckup, reparseSources, backfillVectors } from "./source-api";
import { compactStagedFileName, summarizeUpload, uploadDocTypeFields, fillAutoDetectedTypes, markTouched, markAllTouched, applyTouchedUpdate, sourceUploadSizeLabel, splitFilesByUploadSize } from "./source-upload.ts";
import { sourceHealthGroups, checkupCount, checkupAlertSignature, repairRelease, isRepairing, type RepairRelease } from "./checkup-view";
import { bulkDeleteConversations, cancelAskJob, deleteConversation, fetchAnswerMemoryLinks, getAskJob, getConversation, listConversations, previewAskIntent, renameConversation, runAskStream, searchNotebook, submitFeedback as submitAnswerFeedback } from "./ask-api";
import { createObjectSchema, deleteObjectSchema, findDuplicates as findKnowledgeDuplicates, getKnowledgeGraph, listKnowledge, listKnowledgeTypes, listObjectSchemas, mergeKnowledge as mergeKnowledgeRecords, proposeObjectSchemas, updateKnowledge as updateKnowledgeRecord, updateObjectSchema } from "./knowledge-api";
import { cancelReport, confirmReportIntent, createReport, deleteReport, downloadReportsZip, generateReport, getReport, listReports, updateReportOutline } from "./report-api";
import { buildKg, cancelScaleIndex, confirmMerge, fetchConceptDetail, fetchIndexStatus, fetchKgNeighbors, fetchKgSearch, fetchMergeReviewJob, fetchNodeContext, fetchPendingMerges, fetchScaleIndexStatus, fetchUnifiedGraph, fetchUnifiedKgStatus, rebuildKg, rebuildScaleIndex, rebuildUnifiedKg, rejectMerge, relinkKg, reviewAllMerges as reviewAllMergesRequest, reviewMerges, type IndexStatus } from "./kg-api";
import { prepareKgFocus } from "./kg-focus";
import {
  type ModelServiceStatusItem,
  type ModelServicesStatus,
  fetchModelServiceStatus,
  testAllSystemModelServices,
  testSystemModelService,
} from "./model-services.ts";
import { FloatingModalCard } from "./floating-modal-card";
import { ModelServicePanel, ModelServiceSummaryButton } from "./model-service-panel";
import {
  ModelTestCoordinator,
  acceptModelServiceStatusSnapshot,
  applyModelServiceTestResult,
  deriveModelServiceSummaryView,
  type ModelTestActivity,
} from "./model-service-orchestration.ts";
import { AuthGate } from "./AuthGate";
import { AccountMenu } from "./account-menu";
import { AskComposer } from "./ask-composer";
import { quotedPhraseHint } from "./query-syntax";
import { AskIntentReview } from "./ask-intent-review";
import {
  buildAskIntentConfirmation,
  type AskIntentConfirmation,
  type QueryIntentContract,
} from "./ask-intent-model";
import {
  elapsedMs,
  handOffIntentTrace,
  intentClarifyStep,
  intentConfirmedStep,
  intentUnderstandingStep,
  intentUnderstoodStep,
  replaceLastIntentStep,
} from "./ask-intent-trace";
import { isAskBlocked } from "./ask-availability";
import { AskSessionHeaderActions } from "./ask-session-header";
import { mergeSessionListFallback, recordStartedConversation } from "./ask-session-state";
import { ChatTurnNav, chatTurnDomId } from "./chat-turn-nav";
import { ChatQuestion } from "./chat-question";
import { ChatAnswer } from "./chat-answer";
import { Pagination } from "./Pagination";
import { ReportsPanel, type ReportDetailT, type ReportSummaryT } from "./report-view";
import { SourceDetailWindow } from "./source-detail-window";
import { usePendingActions, PendingBell, PendingToast, type PendingItem } from "./pending-center";
import { canSeeAdminUsage } from "./admin/usage/format.ts";
import { shouldResumeReviewAll, shouldResumeScaleIndex, shouldResumeKgBuild, kgBuildFinished } from "./in-progress-resume";
import {
  canContinueKgBuild,
  isTrackedKgTerminal,
  reconcileTrackedKgPoll,
  ownsKgBuildRequest,
  kgBuildPresentation,
  kgBuildTerminalToast,
} from "./kg-build-status";
import { jobPollDone, newTraceSteps, type AskJobDetail } from "./ask-reconnect";
import { sourceImageAssetUrl } from "./source-image";
import { crossLibrarySourceNotebookId } from "./source-scope";
import {
  claimSourceDeleteRefresh,
  filterDeletedSourceItems,
  ownsSourceDeleteRefresh,
  readStableSourceSnapshot,
} from "./source-delete-state";
import { clampSourcePage, sourcePageRequestIsCurrent } from "./source-page-state";
import {
  doneItemDestination,
  followLatestNotebookRequest,
  historyModeForTransition,
  NOTEBOOK_PRIVATE_MEMORY_DELETE_WARNING,
  notebookIsActive,
  openMemoryDeepLink,
  ownsWorkspaceRun,
  restoreLatestConversation,
  sessionListRequestIsCurrent,
  workspaceCapabilities,
  workspaceRequestIsCurrent,
} from "./workspace-transitions";
import {
  CHAT_MODES,
  EMPTY_KNOWLEDGE,
  KNOWLEDGE_STATUS_OPTIONS,
  SOURCES_PAGE_SIZE,
  type AskResponse,
  type ChatMode,
  type ChatTurn,
  type ConceptDetailResp,
  type ConversationDetail,
  type ConversationSummary,
  type DuplicateGroup,
  type Evidence,
  type EvidenceItem,
  type FgLink,
  type FgNode,
  type Health,
  type KgNeighborsResp,
  type KgBuildJobStatus,
  type KgObject,
  type KgOccurrence,
  type KgProcedureStep,
  type KgSearchHit,
  type KgSearchResp,
  type KnowledgeGraph,
  type KnowledgeItem,
  type KnowledgeKind,
  type KnowledgeRecord,
  type KnowledgeTypeCount,
  type MergeReviewJob,
  type MergeReviewSummary,
  type MemoryRecord,
  type NodeContext,
  type CheckupResponse,
  type NotebookAnalytics,
  type NotebookContentOverview,
  type NotebookSummary,
  type ObjectSchema,
  type PaginatedKnowledge,
  type PaginatedSources,
  type PendingMerge,
  type SearchHit,
  type SourceElement,
  type SourceSummary,
  type UnifiedConceptNode,
  type UnifiedEdge,
  type UnifiedGraphResp,
  type UnifiedKgStatus,
} from "./workspace-model";
import { documentUploadBlockReason, resolveDocumentCapacity } from "./document-limit";
import { label, PARSE_STATUS, ELEMENT_TYPE, KNOWLEDGE_STATUS, PROMOTION_STATUS, SEVERITY, CHECKUP_FIX, CHECKUP_FIX_BUSY } from "./vocabulary";
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


// 图谱边类型 → 中文。取值真源:prompts.py 列出的 edge_type 词表(supports /
// depends_on / contrasts_with / about / defines / used_in / composed_of / mixed,
// 外加可传递的 derived_from / kind_of / prerequisite_of / precedes / part_of)。
// 此前有 8 个值只是把英文 id 抄了一遍(about: "about"),另有 5 个值压根没进表、
// 靠 `?? edge_type` 直接把英文渲染给用户——两条路都是英文外泄,一并补齐。
const RELATION_LABELS: Record<string, string> = {
  related_concepts: "关联概念",
  related_claims: "关联论断",
  related_formulas: "关联公式",
  related_procedures: "关联过程",
  about: "关于",
  defines: "定义",
  supports: "支持",
  depends_on: "依赖",
  composed_of: "包含",
  part_of: "属于",
  precedes: "先于",
  contrasts_with: "对比",
  used_in: "用于",
  derived_from: "推导自",
  kind_of: "是一种",
  prerequisite_of: "前置于",
  mixed: "多种关联"
};

/**
 * 边类型的界面名。未映射时退到中性的「关联」,**绝不回落成 edge_type 原值**——
 * 后端每加一个边类型,`RELATION_LABELS[t] ?? t` 那种写法都会把英文 id 直接画到
 * 图上(used_in / mixed 等 5 个值就是这么泄出去的)。label() 强制传兜底词,并在
 * 开发期把未映射的值 console.error 出来,让新值被发现而不是被静默渲染。
 */
function relationLabel(edgeType: string): string {
  return label(RELATION_LABELS, edgeType, "关联");
}

const KG_TYPE_ORDER = ["concept", "claim", "formula", "procedure"];

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

function welcomeCopyFor(notebook: NotebookSummary | null, sources: SourceSummary[], total: number): WelcomeCopy {
  const notebookPurpose = notebook?.purpose?.trim();
  // 对话被硬约束禁用时(后端判定无任何可检索证据):引导添加来源或挂参考库,且不给
  // 可点的提问建议(点了也会被 runAsk 挡下)。判据是后端 ask_available,与来源搜索是否
  // 过滤、当前页 sources 是否为空无关。
  if (isAskBlocked(notebook)) {
    return {
      title: "先添加来源，再开始对话",
      description: notebookPurpose
        || "上传 PDF、Markdown、DOCX 或 PPTX，或在「设置 → 编辑当前笔记本」里挂载一个参考库，就能基于内容对话了。",
      prompts: [],
    };
  }
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
    description: notebookPurpose || `已导入 ${total} 个来源。可以围绕 ${topic} 的概念、论断、公式和过程提问，回答会优先绑定出处。`,
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


// ---- 启动就绪门 ----

// 依据就绪快照给出一句克制、不惊扰的中文阶段文案。
function startupPhaseText(snap: ReadySnapshot | null): string {
  const phase = snap?.phase ?? "starting";
  if (phase === "migrating") return "正在迁移数据库…";
  if (phase === "warming") {
    const warmed = snap?.warmed_notebooks ?? 0;
    const total = snap?.total_notebooks ?? 0;
    return `正在预热 (${warmed}/${total} 笔记本)…`;
  }
  if (phase === "preloading_indexes") {
    const loaded = snap?.preloaded_indexes ?? 0;
    const total = snap?.total_indexes ?? 0;
    return `正在加载大库检索索引 (${loaded}/${total})…`;
  }
  // ⚠别把 snap.error 直出:它是后端的原始异常串。原文已在 probeReady() 里进
  // console,这里只给稳定文案。
  if (phase === "error") return "启动时遇到问题，正在重试…";
  return "服务启动中…";
}

// 就绪前的启动屏:复用 auth-gate / auth-card 样式,居中卡片 + 旋转指示 + 阶段文案。
function StartingScreen({ snapshot, onRetry }: { snapshot: ReadySnapshot | null; onRetry: () => void }) {
  const isError = snapshot?.phase === "error";
  return (
    <div className="auth-gate">
      <div className="auth-card startup-card">
        <div className="auth-brand">silicon-notebook</div>
        <div className={`startup-spinner${isError ? " error" : ""}`} aria-hidden />
        <div className="startup-phase">{startupPhaseText(snapshot)}</div>
        {isError ? (
          <>
            <div className="startup-sub">将自动重试，也可手动重试。</div>
            <button type="button" className="auth-submit startup-retry" onClick={onRetry}>重试</button>
          </>
        ) : (
          <div className="startup-sub">首次启动需迁移、预热并加载检索索引，请稍候…</div>
        )}
      </div>
    </div>
  );
}

// 取消检索索引构建:排队中→出队(cancelled:true);构建中→不可协作打断(cancelled:false,
// reason:"building_not_interruptible",前端应提示「正在构建,完成后自动更新」)。

// 三系统构建状态聚合(kg=抽取/unified_kg=概念合并/scale_index=检索索引)——镜像后端
// index_status() 的返回形状(backend/app/services/sqlite_repository.py)。scale_index
// 原样复用既有 ScaleIndexStatus 类型,避免重复定义漂移。

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
// 图谱范围档位：核心 80 / 160 / 320 / 全部(0)。打开图谱默认从核心 80 起。
const KG_RANGE_DEFAULT = 80;
const KG_RANGE_STEPS: Array<{ value: number; label: string }> = [
  { value: 80, label: "核心 80" },
  { value: 160, label: "核心 160" },
  { value: 320, label: "核心 320" },
  { value: 0, label: "全部" },
];
// 服务端搜索：FTS5 + ANN 混合，返回命中列表（不再客户端拉全量图）。命中数由服务端 k 参数控制。
// 逐跳展开：返回指定节点的邻居节点+边（bounded）。

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

function kgTypeBandForce(width: number, height: number, activeTypes: string[]) {
  let nodes: FgNode[] = [];
  // 取值/积分都走 kg-layout.ts 的纯函数:原来这里是 `targets[node.type] ?? center`,
  // node.type="constructor" 会拿到继承的构造函数(truthy,`??` 不接管)→ target[0] 为
  // undefined → vx 变 NaN。kgBandTarget 用 Object.hasOwn 封死,并由 kg-layout.test.mjs
  // 的有限坐标回归测试钉住(page.tsx 是 .tsx,node --test 不能直接 import)。
  const targets = kgTypeBandTargets(width, height);
  const force = (alpha: number) => {
    const useTypeBands = activeTypes.length !== 1;
    nodes.forEach((node) => {
      const target = kgBandTarget(targets, node.type, width, height, useTypeBands);
      const next = kgBandVelocity(node, target, alpha);
      node.vx = next.vx;
      node.vy = next.vy;
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
  // Object.hasOwn 而非 KG_TYPE_STYLE[node.type]:后者走原型链,node.type 为自定义类型
  // "constructor"/"__proto__" 时命中继承属性(函数)→ style.color/glyph 变 undefined、
  // 图谱节点渲染异常。与 kg-type-mark.tsx 的 KgTypeMark 同款防护(PR A 原型链教训)。
  const style = Object.hasOwn(KG_TYPE_STYLE, node.type) ? KG_TYPE_STYLE[node.type] : { color: "#64748b", border: "#334155", text: node.type.slice(0, 2).toUpperCase(), glyph: "circle" };
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
  let label = truncateKgLabel(relationLabel(link.label), 18);
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
  // 提问模式的显示名唯一真源是 ask-modes.ts —— 散文提到它时插值引用本常量,
  // 不写死中文字面量(改名只改注册表;ask-modes.test.mjs 的散落守卫强制这一点)。
  const strictLabel = groupLabel("strict");
  // 启动就绪门:后端预热完成前(serviceReady=false)遮住登录/加载,轮询 /api/ready。
  const [serviceReady, setServiceReady] = useState(false);
  const [readySnapshot, setReadySnapshot] = useState<ReadySnapshot | null>(null);
  const [readyRetry, setReadyRetry] = useState(0);
  const [currentUser, setCurrentUser] = useState<AuthUser | null>(null);
  const [authChecked, setAuthChecked] = useState(false);
  const [health, setHealth] = useState<Health | null>(null);
  const [notebooks, setNotebooks] = useState<NotebookSummary[]>([]);
  const [searchHits, setSearchHits] = useState<Record<string, SearchHit[]>>({});
  const [currentNotebookId, setCurrentNotebookId] = useState<string | null>(null);
  const [currentNotebook, setCurrentNotebook] = useState<NotebookSummary | null>(null);
  const [outerView, setOuterView] = useState<"notebooks" | "memory">("notebooks");
  const [sources, setSources] = useState<SourceSummary[]>([]);
  const [sourcesTotal, setSourcesTotal] = useState(0);
  // sourcesTotal follows the source-list filter (it holds the search-matched count
  // while a source search is active, which pagination needs). The Ask composer / welcome
  // copy instead want the notebook's imported-source total regardless of that filter, so
  // track it separately: updated only on unfiltered loads and on source mutations.
  // 文档上限门控也复用它：notebookSourceTotal 是不受来源搜索过滤影响的可见文档真实
  // 总数（与后端 list_sources_page 无过滤时的 total_count 同口径，排除 memory/knowhow）。
  const [notebookSourceTotal, setNotebookSourceTotal] = useState(0);
  const [sourcesPage, setSourcesPage] = useState(0);
  const [sourcesCollapsed, setSourcesCollapsed] = useState(false);
  const [sourceQuery, setSourceQuery] = useState("");
  const [question, setQuestion] = useState("");
  const [turns, setTurns] = useState<ChatTurn[]>([]);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [sessions, setSessions] = useState<ConversationSummary[]>([]);
  const [asking, setAsking] = useState(false);
  const [intentChecking, setIntentChecking] = useState(false);
  const [askIntentReview, setAskIntentReview] = useState<{
    notebookId: string;
    conversationId: string | null;
    question: string;
    contract: QueryIntentContract;
    understandingMs: number;
    askedAt: string;
  } | null>(null);
  const [sessionLoading, setSessionLoading] = useState(false);
  const [pendingQuestion, setPendingQuestion] = useState("");
  const [pendingAskedAt, setPendingAskedAt] = useState("");
  const [pendingMode, setPendingMode] = useState<AskModeId>(DEFAULT_ASK_MODE);
  const [pendingTrace, setPendingTrace] = useState<ReasoningTraceStep[]>([]);
  const [filter, setFilter] = useState("mine");
  const [viewMode, setViewMode] = useState("grid");
  const [sortMode, setSortMode] = useState("recent");
  const [searchQuery, setSearchQuery] = useState("");
  const [sortOpen, setSortOpen] = useState(false);
  const [menuNotebookId, setMenuNotebookId] = useState<string | null>(null);
  const [menuPosition, setMenuPosition] = useState<NotebookMenuPosition | null>(null);
  const [editingNotebook, setEditingNotebook] = useState<NotebookSummary | null>(null);
  // 多领域基准库:编辑弹窗里的参考库多选(mountable=候选,mountedIds=当前勾选,
  // mountEdges=拉取时的挂载边快照,用于渲染失效边置灰)。三者只在打开编辑弹窗时
  // (openNotebookEditor)拉取——bases/mountable 两个端点是 owner-only,访客 404。
  const [mountable, setMountable] = useState<NotebookRef[]>([]);
  const [mountedIds, setMountedIds] = useState<string[]>([]);
  const [mountEdges, setMountEdges] = useState<MountedBase[]>([]);
  const [deleteNotebook, setDeleteNotebook] = useState<NotebookSummary | null>(null);
  // 必办 4(spec §6):删除确认弹窗要显示"N 个笔记本正在把它作为参考库"—— CASCADE
  // 会连同这些边一起清空且不可撤销。只在打开删除确认弹窗时才拉取(openDeleteConfirm)。
  const [deleteMountedByCount, setDeleteMountedByCount] = useState(0);
  const [sourceModalOpen, setSourceModalOpen] = useState(false);
  const [linkSectionOpen, setLinkSectionOpen] = useState(false);
  const [urlText, setUrlText] = useState("");
  const [urlBusy, setUrlBusy] = useState(false);
  const [urlRejected, setUrlRejected] = useState<Array<{ url: string; reason: string }>>([]);
  const [createOpen, setCreateOpen] = useState(false);
  const [createName, setCreateName] = useState("");
  const [createDesc, setCreateDesc] = useState("");
  const [docTypeOptions, setDocTypeOptions] = useState<Array<{ id: string; label: string }>>([]);
  // Authenticated system configuration is the browser mirror of Settings. Null is
  // only the short initial fetch window; the server remains the final 413 guard.
  const [sourceUploadMaxBytes, setSourceUploadMaxBytes] = useState<number | null>(null);
  const [sourceUploadMaxFilesPerBatch, setSourceUploadMaxFilesPerBatch] = useState<number | null>(null);
  const [stagedFiles, setStagedFiles] = useState<File[]>([]);
  // 上传在飞:multipart 传大 PDF 可能几十秒,期间「上传 N 个文件」必须禁用改文案。后端按
  // 内容哈希在同 notebook 内去重,重复提交不会建出重复来源,但会白传一遍并再跑一次解析。
  const [uploadBusy, setUploadBusy] = useState(false);
  const [stagedDocTypes, setStagedDocTypes] = useState<string[]>([]);
  // 与 stagedDocTypes 同序等长：用户是否**手动**动过这一项的类型下拉框。auto-detect
  // 自动回填不置位（那是系统建议、非用户表态）；上传时据此发 doc_type_explicit，让后端
  // 只在用户显式表态时才改/重置复用来源的类型（见 uploadDocTypeFields / 后端 reuse 路径）。
  const [stagedDocTypeTouched, setStagedDocTypeTouched] = useState<boolean[]>([]);
  // detectStagedTypes 是异步的：检测在飞时用户改了下拉框，回填必须看**最新**的 touched，
  // 而闭包捕获的 stagedDocTypeTouched 是发起那一刻的旧值。用 ref 镜像最新值，回填时读它。
  // ref 由改动 touched 的每个 handler 经 applyTouchedUpdate **同步**更新（不等这个
  // useEffect 提交），否则检测恰在「置 touched」与「useEffect 写 ref」的间隙里 resolve
  // 就会读到旧 ref、覆盖用户显式选的自动检测。下面的 useEffect 只作兜底（提交后拨齐）。
  const stagedDocTypeTouchedRef = useRef<boolean[]>([]);
  useEffect(() => {
    stagedDocTypeTouchedRef.current = stagedDocTypeTouched;
  }, [stagedDocTypeTouched]);
  const [sourceDetail, setSourceDetail] = useState<SourceSummary | null>(null);
  // 删除是一个可能触发大量级联清理的同步请求。按 source id 记录进行态，让列表与详情
  // 共用同一把锁；ref 在 React 提交 state 前就同步占位，防住确认框/两个入口的连点竞态。
  const [deletingSourceIds, setDeletingSourceIds] = useState<Set<string>>(() => new Set());
  const deletingSourceIdsRef = useRef<Set<string>>(new Set());
  // 来源详情的「重新解析」是**同步等完**的整篇重解析(走 MinerU/解析器,大 PDF 可能数分钟),
  // 不是后台 job——期间图标按钮必须禁用并换成转圈,否则用户只看到一个毫无反应的按钮、
  // 反复点就会把同一篇重复解析若干遍。
  const [reparsingSource, setReparsingSource] = useState(false);
  const [sourceElements, setSourceElements] = useState<SourceElement[]>([]);
  const [infoModal, setInfoModal] = useState<InfoModal | null>(null);
  const [toast, setToast] = useState("");
  const [modelPanelOpen, setModelPanelOpen] = useState(false);
  const [modelStatusState, setModelStatusState] = useState({
    status: null as ModelServicesStatus | null,
    unavailable: false,
  });
  const modelStatus = modelStatusState.status;
  const modelStatusUnavailable = modelStatusState.unavailable;
  const [highlightedModelServiceId, setHighlightedModelServiceId] = useState<string | null>(null);
  const [modelTestActivity, setModelTestActivity] = useState<ModelTestActivity>({
    services: {}, all: false,
  });
  const [statusText, setStatusText] = useState("连接中");
  const [titleDraft, setTitleDraft] = useState("");
  const [titleSaveInFlight, setTitleSaveInFlight] = useState(false);
  const [feedbackSent, setFeedbackSent] = useState<Record<string, string>>({});
  const [memoryAnswerId, setMemoryAnswerId] = useState<string | null>(null);
  const [memorySavedAnswers, setMemorySavedAnswers] = useState<Record<string, boolean>>({});
  const [sessionPanelOpen, setSessionPanelOpen] = useState(false);
  const [renamingSessionId, setRenamingSessionId] = useState<string | null>(null);
  const [sessionTitleDraft, setSessionTitleDraft] = useState("");
  const [chatMode, setChatMode] = useState<ChatMode>("ask");
  const [askMode, setAskMode] = useState<AskModeId>(DEFAULT_ASK_MODE);
  const [askRetrievalEffort, setAskRetrievalEffort] = useState<AskRetrievalEffortId>(
    DEFAULT_ASK_RETRIEVAL_EFFORT,
  );
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
  // 多领域基准库:提交晋升前需要知道本笔记本挂了几个公共知识库(resolvePromotionTarget:
  // 0 个禁用按钮/1 个直接用/>1 个弹选择器)。只在进入「Rules」知识浏览 tab 时按 owner
  // 门控拉取(switchChatMode),不在打开笔记本时无条件调用。
  const [currentNotebookBases, setCurrentNotebookBases] = useState<MountedBase[]>([]);
  // 挂了 >1 个公共知识库时,点「提交晋升」先记下待定的知识对象 id,弹选择器要求选一个。
  const [pendingPromotionObjectId, setPendingPromotionObjectId] = useState<string | null>(null);
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
  const [contentOverview, setContentOverview] = useState<NotebookContentOverview | null>(null);
  const [contentOverviewLoading, setContentOverviewLoading] = useState(false);
  const [contentOverviewError, setContentOverviewError] = useState("");
  const [memoryNavigationTarget, setMemoryNavigationTarget] = useState<MemoryNavigationTarget>({});
  const [schemaModalOpen, setSchemaModalOpen] = useState(false);
  const [schemas, setSchemas] = useState<ObjectSchema[] | null>(null);
  const [schemaBusy, setSchemaBusy] = useState(false);
  const [graph, setGraph] = useState<KnowledgeGraph | null>(null);
  const [graphOpen, setGraphOpen] = useState(false);
  const [kgViewOpen, setKgViewOpen] = useState(false);
  // 「图谱分析」只读报告(kg-analysis-view.tsx)。渲染在知识图谱视图内部,但**卸载不等于
  // 复位** —— 这个 state 挂在父层,子组件被卸载后它仍是 true。所以父视图的开与关都必须
  // 显式把它推回 false,否则下次打开知识图谱会立刻自弹一次分析窗,而且 notebookId 取的是
  // 当时的 currentNotebookId —— 中间换过库的话,弹出来的是**另一个库**的报告。
  // 复位点恰好两处:openKgView(打开/重开)与 closeKgView(关闭),由
  // kg-analysis-view-toggle.test.mjs 钉住。
  const [kgAnalysisOpen, setKgAnalysisOpen] = useState(false);
  const [knowhowNavigation, setKnowhowNavigation] = useState(CLOSED_KNOWHOW_NAVIGATION);
  // Task 12（引用跳转）：ask 引用命中 knowhow 格子时的跳转目标——非 null 时
  // KnowhowPanel 挂载即定位到该表该行的抽屉（见 openKnowhowAt）。
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
  const [trackedKgJobId, setTrackedKgJobId] = useState<string | null>(null);
  const [backfillingMeta, setBackfillingMeta] = useState(false);
  const [relinkingKg, setRelinkingKg] = useState(false);
  const [buildingScaleIndex, setBuildingScaleIndex] = useState(false);
  const [scaleIndexStatus, setScaleIndexStatus] = useState<ScaleIndexStatus | null>(null);
  const scaleIndexDoneRequestRef = useRef(0);
  const [cancelingScaleIndex, setCancelingScaleIndex] = useState(false);
  // 「索引与构建」面板(看板弹窗)的三系统聚合状态——openAnalytics 打开时经 fetchIndexStatus
  // 一次拉齐,面板打开期间任一系统忙碌时轻量轮询保鲜(见下方 effect)。
  const [indexStatus, setIndexStatus] = useState<IndexStatus | null>(null);
  // 流水线体检(P2):看板弹窗打开时随 indexStatus 一起拉取,驱动「来源状态」块的
  // 源级问题(H2–H6)、「索引与构建」块的索引可信度(H7/H8),以及铃铛的一条聚合提醒。
  const [checkup, setCheckup] = useState<CheckupResponse | null>(null);
  // 修复触发后短暂轮询 checkup 到此刻(反映 count 下降):reparse/backfill 是后台 job、
  // 无 build 忙碌位,不走三系统聚合 poll,故用这个有界窗口驱动一条专门的 checkup 轮询。
  const [checkupRepairPollUntil, setCheckupRepairPollUntil] = useState(0);
  // 已触发、后台仍在跑的体检修复:分组 key → 解除条件。解除条件按**修复的形状**分两种
  // (reparse 逐轮修一批 / backfill_vectors 一次修全库),规则与判定都在 checkup-view.ts
  // 的 repairRelease / isRepairing,那里也记着一条刻意保留的取舍——改之前先读那段注释。
  // 有界轮询窗口结束 / 切库是两种共用的兜底,保证按钮不会永久卡死。
  const [repairingFix, setRepairingFix] = useState<Record<string, RepairRelease>>({});
  const analyticsLoadScopeRef = useRef(new AnalyticsLoadScope());
  const modelStatusRequestRef = useRef(0);
  const modelTestCoordinatorRef = useRef(new ModelTestCoordinator());
  const modelPanelReturnFocusRef = useRef<HTMLElement | null>(null);
  const [pendingReportFocusId, setPendingReportFocusId] = useState<string | null>(null);
  const pending = usePendingActions(Boolean(authChecked && getToken()));
  const latestScaleIndexDoneEventKey = latestScaleIndexDoneKey(
    pending.doneItems,
    currentNotebookId,
  );
  // Notebook switches invalidate an event-driven refresh even if the next
  // notebook has no completion item.  Dismissing an item does not increment
  // this generation, so it cannot cancel an already-started authoritative GET.
  useEffect(() => {
    scaleIndexDoneRequestRef.current += 1;
  }, [currentNotebookId]);
  // The foreground status poll intentionally stops after 20 minutes, but an
  // off-peak queue or a very large build may finish later.  The pending-action
  // SSE completion event is therefore the authoritative wake-up: re-fetch the
  // active notebook's live scale status so historical index_required banners
  // disappear without requiring navigation or a page refresh.
  useEffect(() => {
    if (!latestScaleIndexDoneEventKey || !currentNotebookId) return;
    const nb = currentNotebookId;
    const request = ++scaleIndexDoneRequestRef.current;
    fetchScaleIndexStatus(nb).then((status) => {
      if (
        activeNotebookIdRef.current !== nb
        || scaleIndexDoneRequestRef.current !== request
      ) return;
      setScaleIndexStatus(status);
      setBuildingScaleIndex(shouldResumeScaleIndex(status));
    }).catch(() => {});
  }, [latestScaleIndexDoneEventKey, currentNotebookId]);
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
    const owner = {
      notebookId: nb,
      workspaceEpoch: workspaceEpochRef.current,
      requestEpoch: ++kgBuildRequestEpochRef.current,
    };
    const stillOwned = () => ownsKgBuildRequest(
      owner,
      activeNotebookIdRef.current,
      workspaceEpochRef.current,
      kgBuildRequestEpochRef.current,
    );
    setBuildingKg(true);
    buildKg(nb)
      .then((started) => {
        if (!stillOwned()) return;
        setTrackedKgJobId(started.job_id);
        setToast("已开始整理知识图谱；完成后会自动更新");
        getNotebook(nb)
          .then((refreshed) => {
            if (stillOwned()) {
              setCurrentNotebook(refreshed);
              if (
                isTrackedKgTerminal(started.job_id, refreshed.kg_build)
              ) {
                setBuildingKg(false);
                setTrackedKgJobId(null);
                const message = kgBuildTerminalToast(refreshed.kg_build);
                if (message) setToast(message);
              } else {
                setBuildingKg(shouldResumeKgBuild(refreshed));
              }
            }
          })
          .catch(() => {});
      })
      .catch((e) => {
        if (!stillOwned()) return;
        reportError(e);
        setBuildingKg(false);
      });
  };
  // Trigger full re-extract: clears existing KG and rebuilds from all sources.
  // 破坏性(清空重抽)——统一确认(与概念合并/检索索引三系统一致的确认机制+文案模板)。
  const startKgRebuild = (nb: string) => {
    confirmIndexAction("全部重新分析？\n\n将清空现有知识图谱并重新分析全部来源。后台进行，完成后自动更新。", () => {
      if (activeNotebookIdRef.current !== nb) return;
      const owner = {
        notebookId: nb,
        workspaceEpoch: workspaceEpochRef.current,
        requestEpoch: ++kgBuildRequestEpochRef.current,
      };
      const stillOwned = () => ownsKgBuildRequest(
        owner,
        activeNotebookIdRef.current,
        workspaceEpochRef.current,
        kgBuildRequestEpochRef.current,
      );
      setBuildingKg(true);
      rebuildKg(nb)
        .then((started) => {
          if (!stillOwned()) return;
          setTrackedKgJobId(started.job_id);
          setToast("已开始全部重新分析；完成后会自动更新");
          getNotebook(nb)
            .then((refreshed) => {
              if (stillOwned()) {
                setCurrentNotebook(refreshed);
                if (
                  isTrackedKgTerminal(started.job_id, refreshed.kg_build)
                ) {
                  setBuildingKg(false);
                  setTrackedKgJobId(null);
                  const message = kgBuildTerminalToast(
                    refreshed.kg_build,
                  );
                  if (message) setToast(message);
                } else {
                  setBuildingKg(shouldResumeKgBuild(refreshed));
                }
              }
            })
            .catch(() => {});
        })
        .catch((e) => {
          if (!stillOwned()) return;
          reportError(e);
          setBuildingKg(false);
        });
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
  // The file picker must never guess a deployment cap. If the initial collection
  // load missed the lightweight config endpoint, retry while the add-source dialog
  // is open and keep only the file inputs disabled until the authoritative value
  // arrives. URL import has a separate server-side size contract and stays usable.
  useEffect(() => {
    if (
      !sourceModalOpen
      || (sourceUploadMaxBytes !== null && sourceUploadMaxFilesPerBatch !== null)
    ) return;
    let cancelled = false;
    let retryTimer = 0;
    const loadUploadLimit = async () => {
      try {
        const config = await fetchSystemConfiguration();
        if (!cancelled) {
          setSourceUploadMaxBytes(config.source_upload_max_bytes);
          setSourceUploadMaxFilesPerBatch(config.source_upload_max_files_per_batch);
        }
      } catch {
        if (!cancelled) retryTimer = window.setTimeout(loadUploadLimit, 2000);
      }
    };
    void loadUploadLimit();
    return () => { cancelled = true; window.clearTimeout(retryTimer); };
  }, [sourceModalOpen, sourceUploadMaxBytes, sourceUploadMaxFilesPerBatch]);
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
        const refreshed = await getNotebook(nb);
        if (cancelled) return;
        setCurrentNotebook((cur) => (cur && cur.id === nb ? refreshed : cur));
        const tracked = reconcileTrackedKgPoll(
          trackedKgJobId,
          refreshed.kg_build,
        );
        if (tracked.terminal) {
          setBuildingKg(false);
          setTrackedKgJobId(null);
          const message = kgBuildTerminalToast(refreshed.kg_build);
          if (message) setToast(message);
        } else if (tracked.trackedJobId !== trackedKgJobId) {
          setTrackedKgJobId(tracked.trackedJobId);
        } else if (!refreshed.kg_build && kgBuildFinished(refreshed)) {
          setBuildingKg(false);
          setToast(`知识图谱构建完成 ✓ 可用${strictLabel}`);
        }
      } catch { /* transient error; keep polling */ }
    }, 6000);
    return () => { cancelled = true; window.clearInterval(poll); };
  }, [buildingKg, currentNotebookId, analytics, trackedKgJobId]);
  // Backfill completion polling: same shape as the buildingKg poll above, for the
  // "补全论文信息"后台任务(paper-meta backfill)。同样在「索引与构建」面板打开时让位
  // (该面板本就不覆盖 paper_meta,不存在重复轮询的问题——只是保持与既有三条 legacy
  // poll 一致的让位约定)。完工不弹 toast：那是待确认中心铃铛(paper_meta_done 事件)
  // 的职责,本 effect 只负责本页仍开着时把按钮文案 / 来源徽章刷新为最新态。
  useEffect(() => {
    if (!backfillingMeta || !currentNotebookId || analytics) return;
    const nb = currentNotebookId;
    const workspaceEpoch = workspaceEpochRef.current;
    let cancelled = false;
    const poll = window.setInterval(async () => {
      try {
        const refreshed = await getNotebook(nb);
        if (cancelled) return;
        if (!refreshed.paper_meta_backfilling) {
          setCurrentNotebook((cur) => (cur && cur.id === nb ? refreshed : cur));
          setBackfillingMeta(false);
          // 用 ref 读最新翻页/搜索,避免用户在补抽期间切页/搜索后被 closure 里
          // 陈旧的 sourcesPage/sourceQuery 拉回 page 0 或空搜索。
          // cancelled 只拦「尚未发起」,拦不住已在途的请求回来后落状态,故按房内
          // workspaceEpoch 约定再加一道 guard(见 workspaceEpochRef 处注释)。
          loadSourcesPage(nb, {
            page: sourcesPageRef.current,
            q: sourceQueryRef.current,
            guard: () => workspaceRequestIsCurrent(
              cancelled,
              workspaceEpoch,
              workspaceEpochRef.current,
              nb,
              activeNotebookIdRef.current,
            ),
          }).catch(() => {});
        }
      } catch { /* transient error; keep polling */ }
    }, 6000);
    // Safety cap so the button never spins forever (failed job / huge corpus).
    const cap = window.setTimeout(() => {
      if (!cancelled) setBackfillingMeta(false);
    }, 20 * 60 * 1000);
    return () => { cancelled = true; window.clearInterval(poll); window.clearTimeout(cap); };
  }, [backfillingMeta, currentNotebookId, analytics]);
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
    // 打开分析看板期间 paper-meta 补抽也算「忙」——否则新独立的 backfill poll
    // 因 analytics gate 让位不跑、聚合 poll 又不知道它在忙,补抽在看板打开期间
    // 完成就形成状态死区(按钮永远显示「补全中…」,来源列表不刷新)。
    const busy = buildingKg || kgRefreshBusy || buildingScaleIndex || backfillingMeta
      || Boolean(indexStatus?.kg.building) || Boolean(indexStatus?.unified_kg.building);
    if (!busy) return;
    const nb = currentNotebookId;
    const workspaceEpoch = workspaceEpochRef.current;
    let cancelled = false;
    const poll = window.setInterval(() => {
      fetchIndexStatus(nb).then((s) => {
        if (cancelled) return;
        setIndexStatus(s);
        setScaleIndexStatus(s.scale_index);
        if (buildingKg && !s.kg.building) {
          getNotebook(nb)
            .then((refreshed) => {
              if (cancelled) return;
              setCurrentNotebook((cur) => (
                cur && cur.id === nb ? refreshed : cur
              ));
              const tracked = reconcileTrackedKgPoll(
                trackedKgJobId,
                refreshed.kg_build,
              );
              if (tracked.terminal) {
                setBuildingKg(false);
                setTrackedKgJobId(null);
                const message = kgBuildTerminalToast(refreshed.kg_build);
                if (message) setToast(message);
              } else if (tracked.trackedJobId !== trackedKgJobId) {
                setTrackedKgJobId(tracked.trackedJobId);
              } else if (
                !refreshed.kg_build
                && kgBuildFinished(refreshed)
              ) {
                setBuildingKg(false);
                setToast(`知识图谱构建完成 ✓ 可用${strictLabel}`);
              }
            })
            .catch(() => {});
        }
        // 同上:终态需 building 与 state==="queued" 都排除，否则「已排队」的自动 fold
        // 会在这里被误判成完工，提前弹「构建完成」toast 并停止轮询。
        if (buildingScaleIndex && !shouldResumeScaleIndex(s.scale_index)) {
          setBuildingScaleIndex(false);
          setToast(s.scale_index.stale ? "索引重建结束（仍有更新未纳入）" : "检索索引重建完成 ✓");
        }
      }).catch(() => {});
      // paper-meta 完成检测:index-status 不覆盖 paper_meta_backfilling,单独拉
      // NotebookSummary(同上方 buildingKg-done 分支的既有做法)。完工不弹 toast:
      // 那是待确认中心铃铛(paper_meta_done 事件)的职责,聚合 poll 与主 poll
      // 保持一致(见 :984-1009 独立 backfill poll)。
      if (backfillingMeta) {
        getNotebook(nb).then((refreshed) => {
          if (cancelled) return;
          if (!refreshed.paper_meta_backfilling) {
            setCurrentNotebook((cur) => (cur && cur.id === nb ? refreshed : cur));
            setBackfillingMeta(false);
            // 同独立 backfill poll:cancelled 拦不住已在途的请求,补 epoch guard。
            loadSourcesPage(nb, {
              page: sourcesPageRef.current,
              q: sourceQueryRef.current,
              guard: () => workspaceRequestIsCurrent(
                cancelled,
                workspaceEpoch,
                workspaceEpochRef.current,
                nb,
                activeNotebookIdRef.current,
              ),
            }).catch(() => {});
          }
        }).catch(() => {});
      }
    }, 6000);
    return () => { cancelled = true; window.clearInterval(poll); };
  }, [analytics, currentNotebookId, buildingKg, kgRefreshBusy, buildingScaleIndex, backfillingMeta, trackedKgJobId, indexStatus?.kg.building, indexStatus?.unified_kg.building]);
  // 切库即清空旧库体检结果(per-notebook,旧库结论不能跨库显示)+ 对新库**立即拉一次**
  // 体检——不等看板打开(codex P2:否则铃铛在用户打开看板前无从提醒,违背主动提醒初衷)。
  // best-effort、只读、无模型;仅当仍是当前库时落状态。
  useEffect(() => {
    setCheckup(null);
    setCheckupRepairPollUntil(0);
    setRepairingFix({});  // 忙碌位是 per-notebook 的,跟体检结果一起清
    const nb = currentNotebookId;
    if (!nb) return;
    void fetchCheckup(nb).then((c) => {
      if (activeNotebookIdRef.current === nb) setCheckup(c);
    }).catch(() => {});
  }, [currentNotebookId]);
  // 修复忙碌位的兜底解除:有界轮询窗口一关(健康即停 / 10 分钟到期),就不再有任何东西
  // 能把「修复中…」改回来了——按证据解除(count 变了)是主路径,见 repairingFix 声明处;
  // 这条兜底保证按钮永不会因为轮询提前结束而卡在禁用态。
  useEffect(() => {
    if (checkupRepairPollUntil > 0) return;
    setRepairingFix((previous) => (Object.keys(previous).length === 0 ? previous : {}));
  }, [checkupRepairPollUntil]);
  // 体检结果一变,就把**已经不在结果里**的分组的忙碌位摘掉。这条同时干两件事:
  //   ① group-gone 的正常解除路径 —— 组没了 = 那一轮修复见效(卡片也随之卸载)。
  //   ② 防止旧条目被后来的新问题继承(codex 第 4 轮 P2)。没有这一步,条目只会在
  //      轮询窗口结束时才清:H4 修好后若窗口仍因别的项(比如 H2)开着,期间新上传
  //      又造出缺向量、H4 重新出现,那条陈旧条目会把新卡片直接锁死——而此刻根本
  //      没有 backfill job 在跑。按「当前还活着的分组键」收敛即可,不必引入代次号。
  useEffect(() => {
    const alive = new Set(sourceHealthGroups(checkup).map((g) => g.key));
    setRepairingFix((previous) => {
      const keys = Object.keys(previous);
      if (keys.every((key) => alive.has(key))) return previous;  // 无变化时保持引用,不触发重渲染
      const next: Record<string, RepairRelease> = {};
      for (const key of keys) if (alive.has(key)) next[key] = previous[key];
      return next;
    });
  }, [checkup]);
  // 体检修复触发后的有界轮询:reparse/backfill(乃至 H6/H7/H8 的既有构建入口)都是
  // 后台 job,点完不会立即反映到 count。看板开着且 checkupRepairPollUntil 未到期时,
  // 每 8s 重拉一次 checkup,直到健康(healthy)或到期即停——不做无界轮询(承效率约束:
  // 体检含 H8 磁盘探针,只在用户显式修复的窗口内刷新)。
  useEffect(() => {
    // 过期但没归零的时间戳必须在这里显式清掉(codex 第 1 轮 P2)。场景:修复挂起期间用户
    // 把看板关了,过了 10 分钟窗口才重开——下面那行会因「已过期」早退,不装 interval,
    // 于是再没有任何东西会把时间戳清零,上面那条兜底 effect 也就永远不会重跑,
    // repairingFix 里那条记录把按钮**无限期**锁在「修复中…」。归零后由兜底 effect 接手。
    if (checkupRepairPollUntil > 0 && checkupRepairPollUntil <= Date.now()) {
      setCheckupRepairPollUntil(0);
      return;
    }
    if (!analytics || !currentNotebookId || checkupRepairPollUntil <= Date.now()) return;
    const nb = currentNotebookId;
    let cancelled = false;
    const poll = window.setInterval(() => {
      if (Date.now() > checkupRepairPollUntil) { setCheckupRepairPollUntil(0); return; }
      fetchCheckup(nb).then((c) => {
        if (cancelled) return;
        setCheckup(c);
        if (c.healthy) setCheckupRepairPollUntil(0);
      }).catch(() => {});
    }, 8000);
    return () => { cancelled = true; window.clearInterval(poll); };
  }, [analytics, currentNotebookId, checkupRepairPollUntil]);
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
    if (when === "now") {
      // queued → now is an explicit upgrade. Reflect the claim immediately so
      // the still-queued server snapshot cannot leave a duplicate-submit gap.
      setScaleIndexStatus((current) => current
        ? { ...current, building: true, state: "building" }
        : current);
    }
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
    } catch (e) {
      reportError(e);
      setBuildingScaleIndex(false);
      fetchScaleIndexStatus(nb).then((s) => setScaleIndexStatus(s)).catch(() => {});
    }
  };
  // 「检索索引」三个精确动作 —— 各自弹确认(描述具体精确)后立即后台执行:
  //   build  未构建/建议 → 从零构建(full)    update 已过期 → 增量收录新增源(fold)
  //   rebuild 已建成     → 删除现有索引从头全量重建(full)。与 tier 解耦,大库亦可建。
  const runScaleIndexOp = (op: ScaleIndexOp, onStarted?: () => void) => {
    const s = scaleIndexStatus;
    const queuedUpgrade = s?.state === "queued" && !s.building;
    if (!currentNotebookId || !s || s.building || (buildingScaleIndex && !queuedUpgrade)) return;
    const nb = currentNotebookId;
    // onStarted 只在**确认弹窗点确定后**跑(放进 onConfirm 内)——H8 修复 CTA 用它 arm
    // checkup 轮询;取消确认/早退守卫命中时都不 arm,免得空转 10min(评审)。
    confirmIndexAction(scaleIndexOpConfirm(op, s), () => {
      startScaleIndexRebuild(nb, "now", SCALE_OP_MODE[op]);
      onStarted?.();
    });
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
  // ---- 流水线体检修复(P2)----
  // 重拉 checkup(修复后反映 count 下降);仅当仍是当前库时落状态。
  const reloadCheckup = (nb: string) => {
    fetchCheckup(nb)
      .then((c) => { if (activeNotebookIdRef.current === nb) setCheckup(c); })
      .catch(() => {});
  };
  // 修复是后台 job、count 不会立即下降,故点完开一段有界的 checkup 轮询窗口(见上方
  // effect,健康即停,10 分钟封顶)。所有修复 CTA(重新解析/补齐向量,及复用的分析/
  // 更新/重建索引)共用它。
  const bumpCheckupRepairPoll = () => setCheckupRepairPollUntil(Date.now() + 10 * 60 * 1000);
  // H2/H3 修复:批量重新解析命中项样本(source_ids 来自 checkup.sample,后端按 notebook
  // 作用域过滤)。样本有上界(≤20),命中更多时逐轮修复、每轮 count 下降。
  //
  // 返回值 = 「后台真的在跑了吗」,给按钮的忙碌态用:只有 true 才把这一组标成修复中并
  // 禁用按钮。未受理/报错必须回 false,否则按钮会锁在「解析中…」而后台其实什么都没干。
  const runReparse = async (nb: string, sourceIds: string[]): Promise<boolean> => {
    if (sourceIds.length === 0) return false;
    try {
      const r = await reparseSources(nb, sourceIds);
      // 一篇都没排上是**合法**结果:样本是体检那一刻的快照,期间来源可能已被删除,或
      // 后端按类型排除(memory/knowhow 合成源)。此时既不能说「已开始重新解析 0 篇」,
      // 也不能 arm 轮询/置忙碌位——后台根本没活在跑,按钮会白锁到窗口结束(codex 第 2
      // 轮 P2)。只重拉体检与来源列表把陈旧的 count 拨正。
      if (r.scheduled.length === 0) {
        setToast("这些来源已不在或无需重新解析；已刷新状态");
        reloadCheckup(nb);
        loadSourcesPage(nb, { page: sourcesPageRef.current, q: sourceQueryRef.current }).catch(() => {});
        return false;
      }
      setToast(`已开始重新解析 ${r.scheduled.length} 篇来源；完成后会自动更新`);
      bumpCheckupRepairPoll();
      reloadCheckup(nb);
      // 让来源列表的状态标签也及时反映(分析中/已就绪)。
      loadSourcesPage(nb, { page: sourcesPageRef.current, q: sourceQueryRef.current }).catch(() => {});
      return true;
    } catch (e) { reportError(e); return false; }
  };
  // H4/H5 修复:后台补齐该 notebook 缺失的检索向量(只补缺失、幂等)。
  const runBackfillVectors = async (nb: string): Promise<boolean> => {
    try {
      const r = await backfillVectors(nb);
      if (!r.accepted) {
        // 后端未配嵌入服务 → 不受理(codex):提示配置、**不** arm 轮询——否则空转 10min、
        // 而缺向量永远补不上、H4/H5 清不掉。同理不置忙碌位:按钮要立刻能再点。
        setToast("未配置嵌入服务，无法补齐检索向量");
        return false;
      }
      setToast("已开始补齐检索向量；完成后会自动更新");
      bumpCheckupRepairPoll();
      reloadCheckup(nb);
      return true;
    } catch (e) { reportError(e); return false; }
  };
  const [kgReviewBusy, setKgReviewBusy] = useState(false);
  // 正在处理的单条待确认合并(候选 id + 这次点的是合并还是拒绝)。decideMerge 落决定后会
  // 顺带跑一次 rebuildUnifiedKg + 重拉整张图,是这一列里最贵的一步;不锁住的话用户在
  // 若干行上连点就会并发排出若干次全量概念合并重建。整列一起禁用(不只是被点的那行)。
  const [decidingMerge, setDecidingMerge] = useState<{ id: string; confirm: boolean } | null>(null);
  const [reviewAllJob, setReviewAllJob] = useState<MergeReviewJob | null>(null);
  // 「全部自动判重」的 POST 在飞(还没拿到 job)。见 reviewAllMerges 里的注释。
  const [reviewAllStarting, setReviewAllStarting] = useState(false);
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
            // job.error 是后端的 `f"{type(exc).__name__}: {exc}"`
            // (services/knowledge_governance.py),不直出;原文进 console。
            ? `全部自动判重中止：${toUserMessage(job.error ? new Error(job.error) : null, "出了点问题")}（已处理 ${job.done}）`
            : `全部自动判重完成：已处理 ${job.done} 项`);        }
      } catch { /* transient error; keep polling */ }
    }, 6000);
    const cap = window.setTimeout(() => {
      if (!cancelled) { setReviewAllRunning(false); setToast("自动判重仍在后台进行，请稍后查看"); }
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
  const sourceDetailRef = useRef<SourceSummary | null>(null);
  // Live refs for source paging/query so long-lived poll effects (paper-meta
  // backfill 完成检测、聚合看板 poll)读到用户最新翻页/搜索结果——把它们放进
  // useEffect 依赖会在切页/搜索时重启定时器(重置 6s 心跳与 20min 安全上限
  // 相对于「最后一次翻页」而非「补抽启动」),用 ref 是同 sourcesRef 的既有
  // 轻量方案。
  const sourcesPageRef = useRef(0);
  const sourceQueryRef = useRef("");
  // 所有来源分页/搜索/删除后重拉共享一个 latest-wins generation。删除成功会启动一个
  // 新请求并立刻作废删除前仍在途的旧翻页/搜索，避免旧响应把已删来源重新塞回列表。
  const sourcePageRequestRef = useRef(0);
  const chatBodyRef = useRef<HTMLDivElement | null>(null);
  const askAbortRef = useRef<AbortController | null>(null);
  const askIntentAbortRef = useRef<AbortController | null>(null);
  const askIntentFlowRef = useRef<"idle" | "preview" | "review" | "submitting">("idle");
  // 问题理解阶段合成的轨迹前缀(理解中→已理解/待澄清→已确认)。它先于持久 job
  // 存在,后端无从产出,所以由前端拼在后端流下来的步骤之前。用 ref 而不是只读
  // pendingTrace:提交时要把这段前缀原样交给 executeAsk,闭包里读 state 会拿到旧值。
  const askIntentTraceRef = useRef<ReasoningTraceStep[]>([]);
  // 理解阶段会清空输入框(问题改以在途 turn 的形式先显示出来)。这里留一份草稿,
  // 供「停止」「返回修改」「预检失败」「提交被守卫拦下」几条留在原地的路径原样
  // 还给输入框。
  const askIntentDraftRef = useRef("");
  // 上面那个草稿槽是全局共享的,而清理发生在 `await executeAsk` 之后 —— 期间用户
  // 完全可能切会话(那会把 flow 置回 idle、放行新一轮 preview)。没有归属令牌的话,
  // 旧 run 返回时会把**新一轮**的草稿抹掉,新一轮再取消就无从退回。令牌只由发起
  // 那一轮持有,谁持有谁才有权清理。
  const askIntentDraftOwnerRef = useRef<object | null>(null);
  const memoryLinksAbortRef = useRef<AbortController | null>(null);
  const memorySessionAbortRef = useRef(new AbortController());
  const askJobIdRef = useRef<string | null>(null);
  const askNotebookIdRef = useRef<string | null>(null);
  // Every notebook/session transition advances the workspace epoch. Async
  // callbacks may update UI only while both their run and workspace epochs
  // still match, preventing cross-user/notebook/conversation state bleed.
  const activeNotebookIdRef = useRef<string | null>(null);
  const activeConversationIdRef = useRef<string | null>(conversationId);
  const activeAskModeRef = useRef<AskModeId>(askMode);
  activeConversationIdRef.current = conversationId;
  activeAskModeRef.current = askMode;
  const workspaceEpochRef = useRef(0);
  // notebook scoped：同库并发删除各自都会启动 collection/detail/checkup 校准，只有
  // 最后一个成功响应对应的 generation 可以落状态，防慢的旧快照盖掉新的删除结果。
  const sourceDeleteRefreshGenerationRef = useRef<Map<string, number>>(new Map());
  // A successful DELETE may return while its notebook is between navigation
  // epochs (active id is temporarily null). Keep notebook-scoped tombstones so
  // an older direct list response can never resurrect that source.
  const deletedSourceIdsByNotebookRef = useRef<Map<string, Set<string>>>(new Map());
  const kgBuildRequestEpochRef = useRef(0);
  const kgOpenRequestRef = useRef(0);
  const kgNodeNotebookRef = useRef<Map<string, string>>(new Map());
  const kgNodeContextObjectRef = useRef<Map<string, string>>(new Map());
  const askRunEpochRef = useRef(0);
  const sessionListRequestRef = useRef(0);
  const latestSessionListRef = useRef<{
    notebookId: string;
    requestId: number;
    promise: Promise<ConversationSummary[]>;
  } | null>(null);
  const optimisticConversationIdsRef = useRef<Set<string>>(new Set());
  // started 事件落地前(jobId 尚未知晓)点了「停止」:保留该 run 的 controller，
  // 继续读到 started 拿到 jobId 后补打 cancel，再中止本地流。Set 而非单一
  // boolean：切会话后可能又启动新 run，取消意图不能串台。
  const cancelRequestedControllersRef = useRef<Set<AbortController>>(new Set());
  useEffect(() => {
    abortIntentPreview();
    setAskIntentReview(null);
  }, [currentNotebookId, conversationId, askMode]);
  // 重开会话接回在途 ask job:reconnectJob 驱动轮询 effect,seen 记已渲染步数(防重复追加);
  // reconnectConvIdRef 记正在重连的会话 id,供轮询跑完后 openSession 重载拿最终答案。
  const [reconnectJob, setReconnectJob] = useState<{ jobId: string; seen: number } | null>(null);
  const reconnectConvIdRef = useRef<string | null>(null);

  useEffect(() => {
    memoryLinksAbortRef.current?.abort();
    const notebookId = currentNotebookId;
    const batches = answerIdBatches(turns.map((turn) => turn.response.answer_id));
    if (!notebookId || batches.length === 0) {
      setMemorySavedAnswers({});
      return;
    }
    const workspaceEpoch = workspaceEpochRef.current;
    const controller = new AbortController();
    memoryLinksAbortRef.current = controller;
    collectSavedAnswerFlags(
      batches,
      (batch) => fetchAnswerMemoryLinks(notebookId, batch, controller.signal),
      controller.signal,
    )
      .then((savedFlags) => {
        if (
          savedFlags === null
          || controller.signal.aborted
          || activeNotebookIdRef.current !== notebookId
          || workspaceEpochRef.current !== workspaceEpoch
        ) return;
        setMemorySavedAnswers(savedFlags);
      })
      .catch((error) => {
        if (!isAbortError(error)) reportError(error);
      })
      .finally(() => {
        if (memoryLinksAbortRef.current === controller) memoryLinksAbortRef.current = null;
      });
    return () => controller.abort();
  }, [currentNotebookId, turns]);
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
        const d = await getAskJob(nb, jobId);
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
            await loadSessions(nb);                                // 同步终态轮数/reasoning 摘要
          } else if (d.status === "cancelled") {
            setToast("该问答已被取消");
            await loadSessions(nb);   // 后端对取消的首轮会话做了空会话清理,刷新列表去掉幽灵项
          } else {
            // d.error 是后端持久化的英文技术串(供日志 / MCP),不直出给用户;
            // 后端写成中文的失败原因才透传。原始值进 console。
            setToast(d.status === "interrupted"
              ? "该问答因服务重启中断"
              : toUserMessage(d.error ? new Error(d.error) : null, "该问答失败，请稍后重试"));
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
  const sessionPopoverRef = useRef<HTMLDivElement | null>(null);
  const kgCanvasRef = useRef<HTMLDivElement | null>(null);
  const kgDetailRef = useRef<HTMLElement | null>(null);
  const kgGraphRef = useRef<any>(null);
  // 收到分享的 token 缓存 —— 挂载时从 URL 抓到后立即清掉 ?share,故拷贝时从这里取。
  const shareTokenRef = useRef<string | null>(null);

  // 启动就绪轮询:挂载即探 /api/ready,未就绪则每 ~1.5s 重探,直到显式 {ready:true}。
  // 探针永不抛错(见 probeReady),故预热期的 503/网络错都只是「继续等」,不会掉进登录态。
  useEffect(() => {
    if (serviceReady) return;
    let cancelled = false;
    let timer: number | undefined;
    const tick = async () => {
      const snap = await probeReady();
      if (cancelled) return;
      if (snap) setReadySnapshot(snap);
      if (snap?.ready) { setServiceReady(true); return; }   // 就绪:停止轮询,放行既有流程
      timer = window.setTimeout(tick, 1500);
    };
    tick();
    return () => { cancelled = true; if (timer !== undefined) window.clearTimeout(timer); };
  }, [serviceReady, readyRetry]);

  // 既有的挂载认证流程:仅在服务就绪后运行,预热期的 503 才不会把用户弹回登录页。
  useEffect(() => {
    if (!serviceReady) return;
    if (!getToken()) { setAuthChecked(true); return; }
    fetchMe()
      .then(async (u) => {
        setCurrentUser(u);
        await loadNotebookCollection();
        const target = parseMemoryHash(window.location.hash);
        if (target?.scope === "global") {
          showGlobalMemory({
            notebookId: target.filterNotebookId,
            status: target.status,
            itemId: target.itemId,
          });
        } else if (target?.scope === "notebook" && target.notebookId) {
          await openMemoryDeepLink(
            target.notebookId,
            openNotebookMemory,
            () => {
            showCollection();
            setToast("该记忆链接不可用或已失效");
            },
          );
        } else {
          // 裸 #notebook=<id>:刷新回到笔记本(此前这条 hash 只写不读,刷新必回集合页)。
          const workspace = parseWorkspaceHash(window.location.hash);
          if (workspace) {
            try {
              await openNotebook(workspace.notebookId, "none");
            } catch {
              showCollection();
              setToast("笔记本链接不可用或已失效");
            }
          }
        }
      })
      .catch(() => { clearToken(); })
      .finally(() => setAuthChecked(true));
  }, [serviceReady]);

  // 浏览器返回/前进:hash 是唯一的真相源,读它切视图。一律传 "none"——
  // 浏览器已经改过 URL,任何再写都会污染历史栈。
  useEffect(() => {
    if (!authChecked) return;
    function onPopState() {
      const hash = window.location.hash;
      const memory = parseMemoryHash(hash);
      if (memory?.scope === "global") {
        showGlobalMemory({
          notebookId: memory.filterNotebookId,
          status: memory.status,
          itemId: memory.itemId,
        });
        return;
      }
      if (memory?.scope === "notebook" && memory.notebookId) {
        openNotebookMemory(memory.notebookId).catch(() => {
          showCollection();
          setToast("该记忆链接不可用或已失效");
        });
        return;
      }
      const workspace = parseWorkspaceHash(hash);
      if (workspace) {
        openNotebook(workspace.notebookId, "none").catch(() => {
          showCollection();
          setToast("笔记本链接不可用或已失效");
        });
        return;
      }
      // showCollection 自己的 replaceState 写的就是当前 URL(无 hash),是个 no-op。
      showCollection();
    }
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, [authChecked]);

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
          const response = await searchNotebook(notebook.id, searchQuery);
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
    // 找不到目标元素(如后端返回的 element_id 已不在当前 sourceElements 里)时
    // ?.scrollIntoView 静默 no-op——这是既有行为,不新增报错/提示。
    const scrollTimer = window.setTimeout(() => {
      document
        .getElementById(sourceElementDomId(highlightedElementId))
        ?.scrollIntoView({ block: "center" });
    }, 80);
    // 一次性高亮(双评审 P2-7):目标卡片的高亮态由 element.id === highlightedElementId
    // 驱动(见下方 source-element-card 的 className),这里只负责几秒后自动清空,
    // 让高亮态"消失"而不是无限期挂着——关闭来源详情窗口(onClose)是另一条清空
    // 路径,两条中的任意一条先发生都行。
    const clearTimer = window.setTimeout(() => setHighlightedElementId(""), 2600);
    return () => { window.clearTimeout(scrollTimer); window.clearTimeout(clearTimer); };
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

  // 会话历史面板:点面板外部(或按 Esc)关闭。历史切换按钮排除在外——
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
      if (target instanceof Element && target.closest(".chat-session-toggle")) {
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
  sourceDetailRef.current = sourceDetail;
  sourcesPageRef.current = sourcesPage;
  sourceQueryRef.current = sourceQuery;
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
      setStatusText(`正在处理来源（已 ${Math.round((pollCountRef.current * 1500) / 1000)}s · ${first.length} 个）`);
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
        setStatusText("处理超时：部分来源长时间未完成，请稍后重试");
        return; // ~3min safety cap
      }
      pollCountRef.current += 1;
      const elapsedSec = Math.round((pollCountRef.current * 1500) / 1000);
      setStatusText(`正在处理来源（已 ${elapsedSec}s · ${pending.length} 个）`);
      try {
        const updated = await Promise.all(
          pending.map((source) => getSource(source.id))
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
          // source.error_message 由后端写成 `ValueError: ...` / MinerU 的英文
          // 提示(见 source_ingestion.py),是给日志和 MCP 看的。只有它本身就是
          // 中文可展示文案时才附给用户;否则原文只进 console(兜底传空串)。
          const failureHint = justFailed.error_message
            ? toUserMessage(new Error(justFailed.error_message), "")
            : "";
          setStatusText(`来源处理失败：${justFailed.file_name || justFailed.title}${failureHint ? ` — ${failureHint}` : ""}`);
        }
        if (reachedExtracted && currentNotebookId) {
          await loadNotebookCollection();
          const refreshed = await getNotebook(currentNotebookId);
          if (!cancelled) setCurrentNotebook(refreshed);
          // 源达终态(extracted/failed)可能新增 H2–H6:刷新体检,新损坏才会冒进铃铛而非等用户手动
          // 打开看板(codex 第5轮 P2:proactive fetch 只在 notebook ID 变时跑,漏了上传后 parse 完成)。
          if (!cancelled) reloadCheckup(currentNotebookId);
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
  const welcomeCopy = useMemo(() => welcomeCopyFor(currentNotebook, sources, notebookSourceTotal), [currentNotebook, sources, notebookSourceTotal]);
  const askHint = useMemo(() => askPlaceholder(currentNotebook), [currentNotebook]);
  // 硬约束:后端判定该库无任何可检索证据时,锁死对话框(输入/发送/快捷提问),占位改为
  // 引导文案。判据单一真源见 ask-availability(读后端 ask_available)。
  const askBlocked = isAskBlocked(currentNotebook);
  const askPlaceholderText = askBlocked ? "请先添加来源或挂载参考库，再开始对话" : askHint;
  // 「英文双引号 = 整体检索」的即时回执。识别规则有边界(太短、引号太密都不算),
  // 不当场回执的话,没被识别就是一次静默失败:用户以为下了约束,检索侧当普通词处理。
  const askQuotedPhraseHint = useMemo(() => quotedPhraseHint(question), [question]);
  // 「这次提问还在进行中」——问题理解阶段(尚无持久 job)、等用户补充澄清、以及
  // 真正在跑的 ask 都算。在途 turn 从提交那一刻就要出现,理解阶段的轨迹才有地方
  // 落;只看 asking 会让用户在整段问题理解里对着空会话等待。
  // 待澄清也必须算在内:预检返回后 intentChecking 就复位了,若不认 askIntentReview,
  // 轨迹会恰好在「等待你补充」这一步上消失(空会话还会退回欢迎页),而下方的确认
  // 卡片仍然摆在那里 —— 这正是本次改动要消除的那种割裂。
  const askInFlight = asking || intentChecking || Boolean(askIntentReview);
  // ask_available 是 get_notebook 的快照;在别的页签/覆盖层增删证据不会刷新它。以下把它
  // 在"重新看到问答框"时与后端对齐,且**双向**——证据增则解禁、证据减则重新禁用(codex
  // PR#334 第5轮 P1:此前只在被禁时重拉,漏了 true→false)。来源增删这条路已各自覆盖
  // (处理轮询 reachedExtracted 分支 / deleteSource 末尾重拉),不在此列。
  //
  // 无条件重拉一次:既捕获 Memory 页签的证据增,也捕获其删除(true→false)。
  function revalidateAskAvailability() {
    const nb = activeNotebookIdRef.current;
    if (!nb) return;
    getNotebook(nb)
      .then((refreshed) => {
        if (activeNotebookIdRef.current === nb) {
          setCurrentNotebook((cur) => (cur && cur.id === nb ? refreshed : cur));
        }
      })
      .catch(reportError);
  }
  // 关闭 knowhow 抽屉:抽屉是覆盖层、不改 chatMode,故 chatMode effect 不会触发。knowhow
  // 投影是防抖后台任务,建/改/清/删格子都可能在落库后**双向**改变可检索证据。轮询判据取
  // **投影状态**(终态)而非 ask_available 快照:只要还有表在投影(projectionPending>0)就
  // 退避轮询,直到无 pending(终态)再停——每拍都先重拉,故停时快照即终态真值。这样既能等
  // 到新建/替换表的 chunk 落库解禁,也能捕获"清掉最后一格→旧 chunk 被投影删除→重新禁用"
  // (codex PR#334 第7轮 P1:旧条件只在被禁时轮询,漏了 true→false 这一向)。封顶 ~3min
  // (同来源处理轮询),瞬时失败退避重试。
  function revalidateAskAvailabilityAfterKnowhow() {
    const nb = activeNotebookIdRef.current;
    if (!nb) return;
    let delay = 1500;
    let elapsed = 0;
    const CAP = 180000; // ~3min
    const schedule = () => {
      if (activeNotebookIdRef.current !== nb || elapsed >= CAP) return;
      elapsed += delay;
      const wait = delay;
      delay = Math.min(Math.round(delay * 1.5), 15000);
      window.setTimeout(tick, wait);
    };
    function tick() {
      if (!nb || activeNotebookIdRef.current !== nb) return; // 已切库,放弃(!nb 兼作收窄)
      // 先只查投影状态:仍有 pending → 继续轮询,此刻**不**拉/信任 notebook 快照。只有确认
      // 无 pending(终态)后才拉权威快照——否则两个并发请求可能跨越投影完成点,getNotebook
      // 返回投影前的 ask_available、tables 已报 pending=0,被当成一致快照而停在陈旧值
      // (codex PR#334 第8轮 P2 的竞态)。knowhow 已关闭、无新编辑,故 no-pending 稳定,
      // 随后的 getNotebook 必为 post-projection。
      fetchKnowhowTables(nb)
        .then((tables) => {
          if (activeNotebookIdRef.current !== nb) return;
          if (tables.some((table) => table.projectionPending > 0)) {
            schedule(); // 还有投影在跑 → 继续退避轮询
            return;
          }
          return getNotebook(nb).then((refreshed) => {
            if (activeNotebookIdRef.current === nb) {
              setCurrentNotebook((cur) => (cur && cur.id === nb ? refreshed : cur));
            }
          });
        })
        .catch(schedule); // 任一请求瞬时失败不终止 → 退避重试
    }
    tick(); // 立即首拉:双向对齐 + 按投影状态决定是否继续轮询
  }
  // 从别的页签切回 Ask 时与后端对齐(捕获在 Memory 等页签的证据增删);初次进入不重复拉。
  const prevAskChatModeRef = useRef<ChatMode>(chatMode);
  useEffect(() => {
    const wasAsk = prevAskChatModeRef.current === "ask";
    prevAskChatModeRef.current = chatMode;
    if (chatMode === "ask" && !wasAsk) revalidateAskAvailability();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chatMode]);
  const kgAvailable = !!(currentNotebook?.kg_ready || currentNotebook?.base_kg_available);
  const currentKgBuildView = kgBuildPresentation(
    currentNotebook?.kg_build,
    currentNotebook?.kg_pending_sources ?? 0,
    Boolean(currentNotebook?.kg_ready),
  );
  // 多领域基准库(Task 14):引用徽章要把 citation/anchor 的 notebook_id 解成人话
  // 库名——id→name 映射从 notebooks(自己的库集合)与当前笔记本挂载的参考库
  // (base_notebooks,覆盖别人创建、不在自己集合里的公共知识库)合并得到,逐 turn
  // 复用同一份而非每条引用各建一次。
  const notebookNames = useMemo(() => {
    const names: Record<string, string> = {};
    for (const nb of notebooks) names[nb.id] = nb.name;
    for (const base of currentNotebook?.base_notebooks ?? []) names[base.id] = base.name;
    return names;
  }, [notebooks, currentNotebook]);
  // 多领域基准库(Task 14 追加项):Memory 晋升按钮要复用与知识条目同一套
  // resolvePromotionTarget(0 个禁用/1 个直接用/>1 个弹选择器)。数据源刻意不用
  // owner-only 的 /bases 端点(currentNotebookBases 只在进「Rules」tab 时按
  // canGovernKnowledge 门控拉取,只读访客会 404)——改用打开笔记本就有、
  // owner/reader 都能看到的 NotebookSummary.base_notebooks;该查询本就只回填
  // 当前生效的挂载边,天然等价于 active=true,不需要再喊一次接口。
  const notebookPromotionBases: MountedBase[] = useMemo(
    () => toMountedBases(currentNotebook?.base_notebooks ?? []),
    [currentNotebook?.base_notebooks],
  );
  // codex 对 PR#304 的审查(2026-07-19)P2 #2:全局 Memory 页(scope==="global")
  // 一条记忆可能来自任意 notebook,不能像上面 notebookPromotionBases 那样共用
  // 同一份——按 memory.notebook_id 各自查。数据源同上一个 useMemo 的理由:不喊
  // owner-only 的 /bases,改用 notebooks(list_for_user 已覆盖 owner∪reader,
  // 涵盖一切可能出现在这里的 memory.notebook_id)里每条自带的 base_notebooks,
  // 一次性建好全量映射。
  const notebookBasesById: Record<string, MountedBase[]> = useMemo(() => {
    const map: Record<string, MountedBase[]> = {};
    for (const nb of notebooks) map[nb.id] = toMountedBases(nb.base_notebooks ?? []);
    return map;
  }, [notebooks]);
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

  async function refreshModelStatus(): Promise<ModelServicesStatus | null> {
    const requestId = ++modelStatusRequestRef.current;
    try {
      const snapshot = await fetchModelServiceStatus();
      if (requestId !== modelStatusRequestRef.current) return snapshot;
      setModelStatusState(acceptModelServiceStatusSnapshot(snapshot));
      return snapshot;
    } catch {
      if (requestId === modelStatusRequestRef.current) {
        setModelStatusState((current) => ({ ...current, unavailable: true }));
      }
      return null;
    }
  }

  // While the model-service panel is open, keep 并发/排队/最久等待 relatively live by
  // re-reading the status snapshot every 2s. The GET is in-memory only (no model calls),
  // so it stays cheap; it is gated to panel-open and torn down on close. 延迟/上次检查
  // stay probe-driven by design — making those live would mean actively pinging models.
  // Serialize: schedule the next poll only after the current one settles (not a fixed
  // interval), so a request slower than 2s can't pile up overlapping in-flight polls —
  // which, because refreshModelStatus bumps modelStatusRequestRef at request start, would
  // otherwise keep discarding every response and never update under sustained latency.
  useEffect(() => {
    if (!modelPanelOpen) return;
    let cancelled = false;
    let timer = 0;
    const scheduleNext = () => {
      timer = window.setTimeout(async () => {
        await refreshModelStatus();
        if (!cancelled) scheduleNext();
      }, 2000);
    };
    scheduleNext();
    return () => { cancelled = true; window.clearTimeout(timer); };
    // refreshModelStatus only touches refs + setState (stable); gate solely on open.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [modelPanelOpen]);

  async function loadNotebookCollection(opts: { guard?: () => boolean } = {}) {
    // The model status request reads only the persisted local snapshot. It is
    // deliberately detached so a missing status endpoint cannot hide notebooks.
    void refreshModelStatus();
    const [healthResponse, notebookResponse, systemConfiguration] = await Promise.all([
      fetchHealth(),
      listNotebooks(),
      // A transient config failure must not hide the notebook collection. Until a
      // later successful refresh, do not guess a local cap; the upload route's
      // authoritative 413 remains the safe fallback.
      fetchSystemConfiguration().catch(() => null),
    ]);
    // 删除后的后台校准可能与切库/退出工作区竞速。调用方提供 guard 时，整个响应
    // 必须原子地放弃，不能只守 notebook detail、却让 collection/status 落进新工作区。
    if (opts.guard && !opts.guard()) return;
    setHealth(healthResponse);
    setStatusText(
      healthResponse.status !== "ok"
        ? "服务连接异常"
        : healthResponse.llm_configured
        ? "服务正常"
          : "服务正常 · 模型服务不可用",
    );
    setNotebooks(notebookResponse);
    if (systemConfiguration) {
      setSourceUploadMaxBytes(systemConfiguration.source_upload_max_bytes);
      setSourceUploadMaxFilesPerBatch(systemConfiguration.source_upload_max_files_per_batch);
    }
    if (docTypeOptions.length === 0) {
      fetchDocumentTypes()
        .then((options) => {
          if (!opts.guard || opts.guard()) setDocTypeOptions(options);
        })
        .catch(() => undefined);
    }
  }

  async function openCreate() {
    const notebook = await createNotebook(defaultNotebookPayload());
    await loadNotebookCollection();
    await openNotebook(notebook.id);
    setStagedFiles([]);
    setStagedDocTypes([]);
    applyTouchedUpdate(stagedDocTypeTouchedRef, setStagedDocTypeTouched, []);
    setSourceModalOpen(true);
  }

  async function submitCreate() {
    const notebook = await createNotebook(namedNotebookPayload(createName, createDesc));
    setCreateOpen(false);
    await loadNotebookCollection();
    await openNotebook(notebook.id);
    setStagedFiles([]);
    setStagedDocTypes([]);
    applyTouchedUpdate(stagedDocTypeTouchedRef, setStagedDocTypeTouched, []);
    setSourceModalOpen(true);
  }

  async function loadSourcesPage(
    notebookId: string,
    opts: { page?: number; q?: string; guard?: () => boolean } = {},
  ) {
    const requestId = ++sourcePageRequestRef.current;
    let pageNum = opts.page ?? 0;
    const q = opts.q ?? sourceQuery;
    const isCurrent = () => sourcePageRequestIsCurrent(
      requestId,
      sourcePageRequestRef.current,
      notebookId,
      activeNotebookIdRef.current,
      !opts.guard || opts.guard(),
    );
    let result = await listSources(notebookId, pageNum * SOURCES_PAGE_SIZE, SOURCES_PAGE_SIZE, q);
    // 后台轮询发起的刷新可能在途期间用户已切库/切会话——那时落状态会把新库的
    // 来源列表覆盖成旧库的。除此之外，所有来源读取共用 request generation：一个
    // 删除后权威重拉或更新的搜索/翻页一启动，更旧的响应就没有资格复活旧行。
    if (!isCurrent()) return;
    const clampedPage = clampSourcePage(pageNum, result.total_count, SOURCES_PAGE_SIZE);
    if (clampedPage !== pageNum) {
      pageNum = clampedPage;
      result = await listSources(
        notebookId,
        pageNum * SOURCES_PAGE_SIZE,
        SOURCES_PAGE_SIZE,
        q,
      );
      if (!isCurrent()) return;
    }
    const filtered = filterDeletedSourceItems(
      result.items,
      deletedSourceIdsByNotebookRef.current.get(notebookId),
    );
    const visibleTotal = Math.max(0, result.total_count - filtered.removedCount);
    setSourcesTotal(visibleTotal);
    // Only an unfiltered page reflects the notebook's true source total; a search query
    // returns the matched subset, which must not become the Ask surfaces' count.
    if (!q) setNotebookSourceTotal(visibleTotal);
    setSources(filtered.items);
    setSourcesPage(pageNum);
  }

  async function openNotebook(notebookId: string, history: "push" | "none" = "push"): Promise<boolean> {
    const historyMode = history === "push"
      ? historyModeForTransition(currentNotebookId, notebookId)
      : null;
    closeAnalytics();
    closeKnowhow();
    setInfoModal(null);
    setSourceDetail(null);
    setSourceElements([]);
    setHighlightedElementId("");
    const workspaceEpoch = ++workspaceEpochRef.current;
    sourcePageRequestRef.current += 1;
    setSessionLoading(true);
    try {
      askRunEpochRef.current += 1;
    abortIntentPreview();
    memoryLinksAbortRef.current?.abort();
    memoryLinksAbortRef.current = null;
    activeNotebookIdRef.current = null;
    optimisticConversationIdsRef.current.clear();
    askAbortRef.current = null;
    askJobIdRef.current = null;
    askNotebookIdRef.current = null;
    setAsking(false);
    setPendingQuestion("");
    setPendingMode(DEFAULT_ASK_MODE);
    setPendingTrace([]);
    setReconnectJob(null);
    setMemoryAnswerId(null);
    setMemorySavedAnswers({});
    // A DELETE may complete while this transition deliberately holds the active
    // notebook id at null. Keep reading both snapshots until one full read sees
    // a stable delete generation; tombstones alone cannot repair ask_available.
    const [notebook, sourcesPage] = await readStableSourceSnapshot(
      () => sourceDeleteRefreshGenerationRef.current.get(notebookId) ?? 0,
      () => Promise.all([
        getNotebook(notebookId),
        listSources(notebookId, 0, SOURCES_PAGE_SIZE),
      ]),
    );
    if (workspaceEpochRef.current !== workspaceEpoch) return false;
    activeNotebookIdRef.current = notebookId;
    setCurrentNotebookId(notebookId);
    setCurrentNotebook(notebook);
    setTitleDraft(notebook.name);
    const filteredSourcesPage = filterDeletedSourceItems(
      sourcesPage.items,
      deletedSourceIdsByNotebookRef.current.get(notebookId),
    );
    const visibleSourcesTotal = Math.max(
      0,
      sourcesPage.total_count - filteredSourcesPage.removedCount,
    );
    setSources(filteredSourcesPage.items);
    setSourcesTotal(visibleSourcesTotal);
    setNotebookSourceTotal(visibleSourcesTotal);
    setSourcesPage(0);
    setSourceQuery("");
    setBuildingKg(shouldResumeKgBuild(notebook));
    setTrackedKgJobId(
      notebook.kg_build?.status === "running"
        ? notebook.kg_build.job_id
        : null,
    );
    setBackfillingMeta(Boolean(notebook.paper_meta_backfilling));
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
    setOuterView("notebooks");
    setKnowledge(EMPTY_KNOWLEDGE);
    setKnowledgeKind("concept");
    setKnowledgeStatusFilter("all");
    setDuplicates(null);
    setCurrentNotebookBases([]);
    setSessions([]);
    pollCountRef.current = 0;
    const sessionList = await loadSessions(notebookId);
    if (workspaceEpochRef.current !== workspaceEpoch) return false;
    // 落在最近一条对话(列表已按 updated_at DESC 排序)而非空白新会话。
    // 沿用本次 openNotebook 自己的 epoch:openSession 会新推一个 epoch,
    // 那会让下面的守卫立刻失配。零对话的库自然跳过,维持新会话现状。
    await restoreLatestConversation(
      sessionList ?? [],
      (id) => applySessionDetail(id, workspaceEpoch),
    );
    if (workspaceEpochRef.current !== workspaceEpoch) return false;
    // "none" = 挂载还原 / popstate:浏览器已经把 URL 摆对了,再写一次只会多一个
    // 死条目(用户按返回没反应)。默认 "push" 让返回键能退出 notebook。
    if (historyMode === "push") {
      window.history.pushState(null, "", notebookHash(notebookId));
    } else if (historyMode === "replace") {
      window.history.replaceState(null, "", notebookHash(notebookId));
    }
      window.scrollTo(0, 0);
      return true;
    } finally {
      if (workspaceEpochRef.current === workspaceEpoch) setSessionLoading(false);
    }
  }

  async function openNotebookMemory(notebookId: string) {
    // 传 "none" 让 openNotebook 别写 history,自己下面这次 replaceState 独占写入——
    // 与本函数改动前的净效果逐字一致(旧代码是 replace 再 replace)。
    if (!await openNotebook(notebookId, "none")) return;
    setChatMode("memory");
    window.history.replaceState(null, "", memoryHash(notebookId));
  }

  // --- Pending center: precise deep-link per item type --------------------
  async function openPendingItem(item: PendingItem) {
    if (!await openNotebook(item.notebook_id)) return;
    if (item.type === "report_outline") {
      switchChatMode("reports");
      if (item.report_id) setPendingReportFocusId(item.report_id);
    } else if (item.type === "governance") {
      if (item.subtype === "edge") { await openEdgeReviewQueue(item.notebook_id); }
      else if (item.subtype === "promotion") { await openPromoQueue(); }
      else { await openKgView(undefined, item.notebook_id); }
    } else if (item.type === "index") {
      await openKgView(undefined, item.notebook_id);
    }
  }
  async function openDoneItem(d: { notebook_id: string; kind?: string }) {
    // 已经在这个库里就别再 push 一条重复的 #notebook=<同一 id>——按返回键会
    // 触发 openNotebook(id, "none") 全量重载,把用户从当前视图甩回 ask 聊天。
    const history = activeNotebookIdRef.current === d.notebook_id ? "none" : "push";
    if (!await openNotebook(d.notebook_id, history)) return;
    // 论文元数据补全完成应停在来源面板(设计稿 §3.3:作者/机构就在来源列表与详情
    // 里),别把用户甩进知识图谱。kind 缺省视作索引完成,索引路径行为逐字不变。
    if (doneItemDestination(d.kind) === "kg") {
      await openKgView(undefined, d.notebook_id);
    }
  }

  async function submitFeedback(answerId: string, rating: "useful" | "not_useful", comment: string) {
    if (!answerId) return;
    await submitAnswerFeedback(answerId, rating, comment);
    setFeedbackSent((prev) => ({ ...prev, [answerId]: rating }));
    setToast("感谢反馈");
  }

  function showCollection() {
    closeAnalytics();
    closeKnowhow();
    setInfoModal(null);
    setSourceDetail(null);
    setSourceElements([]);
    setHighlightedElementId("");
    workspaceEpochRef.current += 1;
    sourcePageRequestRef.current += 1;
    askRunEpochRef.current += 1;
    abortIntentPreview();
    memoryLinksAbortRef.current?.abort();
    memoryLinksAbortRef.current = null;
    activeNotebookIdRef.current = null;
    optimisticConversationIdsRef.current.clear();
    askAbortRef.current = null;
    askJobIdRef.current = null;
    askNotebookIdRef.current = null;
    setCurrentNotebookId(null);
    setCurrentNotebook(null);
    setSources([]);
    setTitleDraft("");
    setTurns([]);
    setConversationId(null);
    setSessions([]);
    setAsking(false);
    setSessionLoading(false);
    setPendingQuestion("");
    setPendingMode(DEFAULT_ASK_MODE);
    setPendingTrace([]);
    setReconnectJob(null);
    setMemoryAnswerId(null);
    setMemorySavedAnswers({});
    setOuterView("notebooks");
    window.history.replaceState(null, "", window.location.pathname + window.location.search);
    window.scrollTo(0, 0);
  }

  function showGlobalMemory(target: MemoryNavigationTarget = {}) {
    showCollection();
    setMemoryNavigationTarget(target);
    setOuterView("memory");
    window.history.replaceState(null, "", memoryHash(null, target));
  }

  async function handleMemorySaved(memory: MemoryRecord) {
    if (!memoryAnswerId || memory.notebook_id !== currentNotebookId) return;
    const savedAnswerId = memoryAnswerId;
    memoryLinksAbortRef.current?.abort();
    memoryLinksAbortRef.current = null;
    setMemorySavedAnswers((previous) => ({ ...previous, [savedAnswerId]: true }));
    setMemoryAnswerId(null);
    setToast("已保存到记忆");
    await loadNotebookCollection();
    if (currentNotebookId) {
      const refreshed = await getNotebook(currentNotebookId);
      if (activeNotebookIdRef.current === currentNotebookId) setCurrentNotebook(refreshed);
    }
  }

  async function saveInlineNotebookName() {
    if (!currentNotebook || titleSaveInFlight) return;
    const nextName = normalizedNotebookName(titleDraft);
    setTitleDraft(nextName);
    if (nextName === currentNotebook.name) return;
    setTitleSaveInFlight(true);
    try {
      const updated = await updateNotebook(currentNotebook.id, { name: nextName });
      setCurrentNotebook(updated);
      setTitleDraft(updated.name);
      await loadNotebookCollection();
      setToast("笔记本名称已更新");
    } catch (error) {
      setTitleDraft(currentNotebook.name);
      reportError(error);
    } finally {
      setTitleSaveInFlight(false);
    }
  }

  // 打开编辑弹窗:先拉可挂候选 + 当前挂载边,弹窗渲染时已有数据,不会先空白闪一下。
  // listMountable/listBases 是 owner-only 端点(访客 404)——编辑弹窗本身就是 owner-only
  // 界面,在这里(而非打开笔记本时)才拉取是安全的边界。
  const openNotebookEditor = async (nb: NotebookSummary) => {
    const [cands, edges] = await Promise.all([listMountable(nb.id), listBases(nb.id)]);
    setMountable(cands);
    setMountEdges(edges);
    setMountedIds(edges.map((e) => e.id));
    setEditingNotebook(nb);
  };

  async function saveNotebookEdit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!editingNotebook) return;
    const formData = new FormData(event.currentTarget);
    const splitLines = (value: string) =>
      value.split(/[\n;,，；]/).map((s) => s.trim()).filter(Boolean);
    await updateNotebook(editingNotebook.id, {
        name: formData.get("name"),
        purpose: formData.get("purpose"),
        primary_domain: formData.get("primary_domain"),
        target_users: String(formData.get("target_users") || ""),
        access_scope: String(formData.get("access_scope") || ""),
        expected_questions: splitLines(String(formData.get("expected_questions") || "")),
        source_types: splitLines(String(formData.get("source_types") || "")),
        taxonomy: splitLines(String(formData.get("taxonomy") || ""))
      });
    // 参考库挂载在同一次保存里一并写(全量替换);随后重新拉一次 notebook——PATCH 的
    // 响应是挂载写之前的快照,base_notebooks/base_kg_available 要靠这次重拉才最新。
    const bases = await setBases(editingNotebook.id, mountedIds);
    const updated = await getNotebook(editingNotebook.id);
    setEditingNotebook(null);
    if (currentNotebookId === updated.id) {
      setCurrentNotebook(updated);
      setTitleDraft(updated.name);
      setCurrentNotebookBases(bases);
    }
    await loadNotebookCollection();
    setToast("笔记本信息已更新");
  }

  // 打开删除确认弹窗前先查"有多少笔记本正在把它作为参考库"(spec §6)——CASCADE
  // 不可逆,用户点删除前必须看到影响面。镜像 openNotebookEditor 的"先拉数据再开
  // 弹窗"惯例,避免弹窗先显示 0 再跳成真实数字的那一下闪烁。
  const openDeleteConfirm = async (nb: NotebookSummary) => {
    const { count } = await mountedByCount(nb.id);
    setDeleteMountedByCount(count);
    setDeleteNotebook(nb);
  };

  async function confirmDeleteNotebook() {
    if (!deleteNotebook) return;
    await deleteNotebookRequest(deleteNotebook.id);
    if (currentNotebookId === deleteNotebook.id) {
      showCollection();
    }
    setDeleteNotebook(null);
    await loadNotebookCollection();
    setToast("笔记本已删除");
  }

  // Stage selected files so the user can pick a document type per file before
  // uploading (auto-detect by default).
  function stageFiles(event: ChangeEvent<HTMLInputElement>) {
    const all = Array.from(event.target.files || []);
    event.target.value = "";
    const supported = all.filter((file) => SUPPORTED_SOURCE_EXTENSIONS.includes(fileExtension(file.name)));
    const rejected = all.filter((file) => !SUPPORTED_SOURCE_EXTENSIONS.includes(fileExtension(file.name)));
    const { accepted: picked, rejected: oversized } = splitFilesByUploadSize(
      supported,
      sourceUploadMaxBytes,
    );
    const notices: string[] = [];
    if (rejected.length > 0) {
      const names = rejected.map((file) => file.name).join("、");
      const hasLegacy = rejected.some((file) => LEGACY_OFFICE_EXTENSIONS.includes(fileExtension(file.name)));
      const hint = hasLegacy
        ? "旧版 Office 格式请另存为 .docx / .pptx / .xlsx"
        : `支持：${SUPPORTED_SOURCE_USER_HINT}`;
      notices.push(`已跳过不支持的文件：${names}。${hint}`);
    }
    if (oversized.length > 0 && sourceUploadMaxBytes !== null) {
      const names = oversized.map((file) => file.name).join("、");
      const limit = sourceUploadSizeLabel(sourceUploadMaxBytes);
      notices.push(
        `已跳过超过单文件上限（${limit}）的文件：${names}。请选择不超过 ${limit} 的文件，或联系管理员调整上限。`,
      );
    }
    if (notices.length > 0) setToast(notices.join("；"));
    if (picked.length === 0) {
      return;
    }
    // 追加而非覆盖（"继续添加文件"语义）；按 name+size 去重，避免重复入列。
    const merged = [...stagedFiles];
    const mergedTypes = [...stagedDocTypes];
    const mergedTouched = [...stagedDocTypeTouched];
    const added: File[] = [];
    const batchOverflow: File[] = [];
    for (const file of picked) {
      if (!merged.some((existing) => existing.name === file.name && existing.size === file.size)) {
        if (
          sourceUploadMaxFilesPerBatch !== null
          && merged.length >= sourceUploadMaxFilesPerBatch
        ) {
          batchOverflow.push(file);
          continue;
        }
        merged.push(file);
        mergedTypes.push("");        // 新文件默认「自动检测」
        mergedTouched.push(false);   // 且尚未被用户手动设置（与 mergedTypes 同步增长）
        added.push(file);
      }
    }
    if (batchOverflow.length > 0 && sourceUploadMaxFilesPerBatch !== null) {
      notices.push(
        `单次最多上传 ${sourceUploadMaxFilesPerBatch} 个文件，已跳过其余 ${batchOverflow.length} 个。请先上传当前批次，再继续添加。`,
      );
      setToast(notices.join("；"));
    }
    setStagedFiles(merged);
    setStagedDocTypes(mergedTypes);
    // 同步入 ref：紧接着的 detectStagedTypes 是异步的，它 resolve 时读 ref 决定回填哪些项；
    // ref 必须已反映这批新文件的 touched（全 false），不能等 useEffect。
    applyTouchedUpdate(stagedDocTypeTouchedRef, setStagedDocTypeTouched, mergedTouched);
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
      const results = await detectSourceTypes(items);
      const byName: Record<string, string> = {};
      results.forEach((r) => { if (r.doc_type_id) byName[r.name] = r.doc_type_id; });
      if (Object.keys(byName).length === 0) return;
      // 按 fullList 的位置回填；只填**未被用户表态过**的项（值空且未 touched），既不
      // 覆盖已选的具体类型、也不覆盖「检测在飞时用户改回自动检测」这种空但 touched 的
      // 显式选择。touched 读 ref 的最新值（闭包里的 stagedDocTypeTouched 是发起那刻的旧
      // 值）。**绝不动 stagedDocTypeTouched**——auto-detect 是系统建议、不是用户表态。
      const detected = fullList.map((file) => byName[file.name]);
      setStagedDocTypes((prev) =>
        fillAutoDetectedTypes(prev, detected, stagedDocTypeTouchedRef.current),
      );
    } catch {
      // 检测失败不影响上传：保持"自动检测"，用户可手动选。
    }
  }

  function setStagedDocType(index: number, value: string) {
    setStagedDocTypes((prev) => prev.map((dt, i) => (i === index ? value : dt)));
    // 用户手动改了这一项 → 标记为显式设置（哪怕选回「自动检测」也是一次显式表态）。
    // 同步更新 ref（不等 useEffect）：否则检测同 tick resolve 会读到旧 touched、覆盖它。
    applyTouchedUpdate(stagedDocTypeTouchedRef, setStagedDocTypeTouched, (prev) => markTouched(prev, index));
  }

  function setAllStagedDocTypes(value: string) {
    setStagedDocTypes((prev) => prev.map(() => value));
    applyTouchedUpdate(stagedDocTypeTouchedRef, setStagedDocTypeTouched, (prev) => markAllTouched(prev));
  }

  function removeStagedFile(index: number) {
    setStagedFiles((prev) => prev.filter((_, i) => i !== index));
    setStagedDocTypes((prev) => prev.filter((_, i) => i !== index));
    applyTouchedUpdate(stagedDocTypeTouchedRef, setStagedDocTypeTouched, (prev) => prev.filter((_, i) => i !== index));
  }

  async function confirmUpload() {
    if (!currentNotebookId || stagedFiles.length === 0 || uploadBusy) return;
    if (
      sourceUploadMaxFilesPerBatch !== null
      && stagedFiles.length > sourceUploadMaxFilesPerBatch
    ) {
      setToast(`单次最多上传 ${sourceUploadMaxFilesPerBatch} 个来源文件。请先移除多余文件。`);
      return;
    }
    const { rejected: oversized } = splitFilesByUploadSize(stagedFiles, sourceUploadMaxBytes);
    if (oversized.length > 0 && sourceUploadMaxBytes !== null) {
      const limit = sourceUploadSizeLabel(sourceUploadMaxBytes);
      setToast(
        `有 ${oversized.length} 个待上传文件超过单个文件上限（${limit}）。请先移除它们，或联系管理员调整上限。`,
      );
      return;
    }
    const blockedReason = documentUploadBlockReason(docCapacity, stagedFiles.length);
    if (blockedReason) {
      setToast(blockedReason);
      return;
    }
    setUploadBusy(true);
    try {
      await confirmUploadInner();
    } finally {
      setUploadBusy(false);
    }
  }

  async function confirmUploadInner() {
    if (!currentNotebookId || stagedFiles.length === 0) return;
    const formData = new FormData();
    stagedFiles.forEach((file) => formData.append("files", file));
    // 每个文件发两个并列字段：doc_types（原值，"" = 自动检测）+ doc_type_explicit
    // （"1"/"0"，用户是否手动动过下拉框）。后端 reuse 路径只在显式时才改/重置既有来源的
    // 类型——auto-detect 自动填的建议值发 "0"，重传不会静默把既有类型抹掉（uploadDocTypeFields）。
    uploadDocTypeFields(stagedDocTypes, stagedDocTypeTouched).forEach(({ docType, explicit }) => {
      formData.append("doc_types", docType);
      formData.append("doc_type_explicit", explicit);
    });
    // 上传前各来源的文档类型：内容判重会沿用既有来源，但用户新选的类型仍会写进
    // 去并触发按新类型重抽——只有对着上传前的值才看得出这次到底改没改。
    const docTypesBefore = new Map(sources.map((source) => [source.id, source.doc_type ?? ""]));
    const uploaded = await uploadSources(currentNotebookId, formData);
    const outcome = summarizeUpload(uploaded, docTypesBefore);
    // 用折叠去重后的 outcome.sources，不是原始 uploaded：一次上传里两个内容相同的
    // 文件会让后端对同一个 id 返回两条（新建 + 命中它的 reused），直接铺进 state 会
    // 渲染出重复卡片，直到重开笔记本才消失。
    setSources((previous) => [...previous.filter((source) => !outcome.sources.some((item) => item.id === source.id)), ...outcome.sources]);
    // 只加新建的那些：沿用的既有来源本来就已经在总数里了，重复计入会让
    //「N 个来源」和分页总数一直偏大到重新打开笔记本为止。outcome.added 已按 id 去重，
    // 批内重复的回声也只计一次。
    setSourcesTotal((t) => t + outcome.added.length);
    setNotebookSourceTotal((t) => t + outcome.added.length);
    await loadNotebookCollection();
    // P1(codex PR#334 第9轮):快传小文件可能后台已解析完(返回即 extracted),hasPending 保持
    // false → 处理轮询的 reachedExtracted 分支不触发 → ask_available 陈旧为假、对话框空锁。显式
    // 重拉解禁;仍在解析中的慢路径由处理轮询覆盖。
    revalidateAskAvailability();
    reloadCheckup(currentNotebookId);  // 新源可能立即/后续成 H2–H6:刷新体检铃铛(codex 第5轮 P2)
    setStagedFiles([]);
    setStagedDocTypes([]);
    applyTouchedUpdate(stagedDocTypeTouchedRef, setStagedDocTypeTouched, []);
    setSourceModalOpen(false);
    setToast(outcome.toast);
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
      const result = await importUrlSources(currentNotebookId, urls);
      if (result.created.length > 0) {
        setSources((previous) => [
          ...previous.filter((source) => !result.created.some((item) => item.id === source.id)),
          ...result.created,
        ]);
        // Maintain only the Ask surfaces' count (notebookSourceTotal, unfiltered) optimistically.
        // sourcesTotal (source-list pagination + Source Stack header) is deliberately left to
        // re-sync on the next source-page fetch: it tracks the *applied* source-search filter, and
        // there is no reliable applied-query signal here — sourceQuery is the editable draft (search
        // applies only on Enter), not necessarily what produced the current page — so optimistically
        // bumping it risks either a stale count or a phantom filtered page. This matches the pre-#332
        // baseline (URL import never touched sourcesTotal); the file-upload path's own unconditional
        // bump is pre-existing and out of scope here. 文档上限门控也读 notebookSourceTotal，
        // 故链接导入 +N 后满额判定同步跟进。
        setNotebookSourceTotal((t) => t + result.created.length);
        await loadNotebookCollection();
        revalidateAskAvailability(); // P1:导入即产出可检索证据时解禁对话框(同 confirmUpload)
        reloadCheckup(currentNotebookId);  // 同理刷新体检铃铛(codex 第5轮 P2)
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
    await openSourceById(source.id, "");
  }

  // 供来源列表(openSourceDetail,不带目标元素)与 Ask 清单结果卡「查看来源」跳转
  // (onOpenSourceElement,带目标元素 id)共用同一条打开路径。elementId 喂给上面
  // 已声明但此前从未被真正置位的 highlightedElementId 高亮/滚动效果——PR-2 T6 是
  // 它第一次真正被点亮。空字符串代表「打开来源、不指向具体元素」,与既有
  // openSourceDetail 的行为逐字一致。
  // 走 active 笔记本维度的代理读取端点(getNotebookSource/-Elements):路径里的
  // notebook 是当前 active 库、权限按它判,来源本身可以属于它有效挂载的任一参考库
  // ——挂载参考库不等于该库的直接成员权限(红线),浏览器因此绝不直连另一个库。参与集
  // 首项恒为 active 自身,本库来源与跨库来源共用这一条路径,不分叉。currentNotebookId
  // 缺席(理论上打不开任何来源)时退回旧的 owner∪member 端点,保持既有行为。
  async function openSourceById(sourceId: string, elementId: string) {
    const notebookId = currentNotebookId;
    const workspaceEpoch = workspaceEpochRef.current;
    const [detail, elements] = await Promise.all([
      notebookId ? getNotebookSource(notebookId, sourceId) : getSource(sourceId),
      notebookId ? getNotebookSourceElements(notebookId, sourceId) : getSourceElements(sourceId)
    ]);
    if (
      (notebookId && activeNotebookIdRef.current !== notebookId)
      || workspaceEpochRef.current !== workspaceEpoch
      || deletedSourceIdsByNotebookRef.current.get(detail.notebook_id)?.has(sourceId)
    ) return;
    setSourceDetail(detail);
    setSourceElements(elements);
    setHighlightedElementId(elementId);
  }

  // Ask 清单结果卡(answer-panel.tsx CollectionResultCard)元素条目「查看来源」/
  // 「在来源详情查看完整表格」按钮的回调:elementId 缺省时只打开来源、不高亮任何
  // 元素；KG 知识对象清单在有原文 citation 时同样用它精确跳转。
  function onOpenSourceElement(sourceId: string, elementId?: string) {
    openSourceById(sourceId, elementId || "").catch(reportError);
  }

  async function reparseSource() {
    if (!sourceDetail || reparsingSource) return;
    // 防御性复检:参考库来源是只读的(按钮本就不渲染),重新解析是 owner-only 的写入。
    if (crossLibrarySourceNotebookId(sourceDetail.notebook_id, currentNotebookId)) return;
    const notebookId = currentNotebookId ?? sourceDetail.notebook_id;
    setReparsingSource(true);
    try {
      await reparseSourceInner(sourceDetail.id, notebookId);
    } finally {
      setReparsingSource(false);
    }
  }

  async function reparseSourceInner(sourceId: string, notebookId: string | null) {
    const updated = await parseSource(sourceId);
    setSources((previous) => previous.map((source) => source.id === updated.id ? updated : source));
    await openSourceDetail(updated);
    await loadNotebookCollection();
    // 重新解析可能**改变可检索证据**:唯一来源重解析成 0 chunk(如扫描版 PDF 无文字)会把
    // ask_available 由真转假。此路径是同步终态、不经处理轮询,故须显式重拉 notebook 快照,
    // 否则对话框陈旧为可用、能向空库提问(codex PR#334 第7轮 P2)。
    if (notebookId) {
      const refreshed = await getNotebook(notebookId);
      if (activeNotebookIdRef.current === notebookId) setCurrentNotebook(refreshed);
    }
    setToast("Source 已重新解析");
  }

  function confirmDeleteSource(source: SourceSummary) {
    // 同上:参考库来源只读,删除是 owner-only 的写入,连确认框都不该弹。
    if (crossLibrarySourceNotebookId(source.notebook_id, currentNotebookId)) return;
    if (deletingSourceIdsRef.current.has(source.id)) return;
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
    const notebookId = source.notebook_id;
    if (!notebookId || deletingSourceIdsRef.current.has(source.id)) return;
    deletingSourceIdsRef.current.add(source.id);
    setDeletingSourceIds((previous) => new Set(previous).add(source.id));
    try {
      await deleteSourceRequest(source.id);
      const deletedIds = deletedSourceIdsByNotebookRef.current.get(notebookId)
        ?? new Set<string>();
      deletedIds.add(source.id);
      deletedSourceIdsByNotebookRef.current.set(notebookId, deletedIds);
      // Detail ownership is source-scoped rather than workspace-scoped. Close
      // it even if navigation temporarily made activeNotebookId null; otherwise
      // a successful delete can leave an actionable 404-only detail behind.
      if (sourceDetailRef.current?.id === source.id) {
        setSourceDetail(null);
        setSourceElements([]);
        setHighlightedElementId("");
      }
      const refreshGeneration = (sourceDeleteRefreshGenerationRef.current.get(notebookId) ?? 0) + 1;
      sourceDeleteRefreshGenerationRef.current.set(notebookId, refreshGeneration);
      const refreshOwner = claimSourceDeleteRefresh(
        notebookId,
        activeNotebookIdRef.current,
        workspaceEpochRef.current,
        refreshGeneration,
      );
      if (!refreshOwner) return;
      const isCurrent = () => ownsSourceDeleteRefresh(
        refreshOwner,
        activeNotebookIdRef.current,
        workspaceEpochRef.current,
        sourceDeleteRefreshGenerationRef.current.get(notebookId) ?? 0,
      );
      // DELETE 成功就是用户等待的终点：先同步提交当前 notebook 的列表、计数与详情，
      // collection/notebook/checkup 的权威校准随后并行进行，不再把网络瀑布算进“删除中”。
      const wasVisible = sourcesRef.current.some((item) => item.id === source.id);
      setSources((previous) => previous.filter((item) => item.id !== source.id));
      if (wasVisible) setSourcesTotal((total) => Math.max(0, total - 1));
      setNotebookSourceTotal((total) => Math.max(0, total - 1));
      setKnowledge(EMPTY_KNOWLEDGE);
      setDuplicates(null);
      setToast("来源已删除");

      void Promise.allSettled([
        loadSourcesPage(notebookId, {
          page: sourcesPageRef.current,
          q: sourceQueryRef.current,
          guard: isCurrent,
        }),
        loadNotebookCollection({ guard: isCurrent }),
        getNotebook(notebookId).then((refreshed) => {
          if (isCurrent()) {
            setCurrentNotebook((current) => current?.id === notebookId ? refreshed : current);
          }
        }),
        fetchCheckup(notebookId).then((refreshedCheckup) => {
          if (isCurrent()) setCheckup(refreshedCheckup);
        }),
      ]);
    } finally {
      deletingSourceIdsRef.current.delete(source.id);
      setDeletingSourceIds((previous) => {
        if (!previous.has(source.id)) return previous;
        const next = new Set(previous);
        next.delete(source.id);
        return next;
      });
    }
  }

  // 收起在途 turn(问题气泡 + 轨迹面板)。
  function clearPendingTurn() {
    setPendingQuestion("");
    setPendingAskedAt("");
    setPendingMode(DEFAULT_ASK_MODE);
    setPendingTrace([]);
  }

  // 问题理解阶段的统一收尾:中止在途预检、退出「理解中」占位态、收起在途 turn。
  // 切库/切会话/新建会话/登出都必须走这里 —— 只 abort controller 而不复位
  // intentChecking 会让占位态永久卡住(runAsk 的 finally 按 controller 身份认领,
  // 别人抢先置空 ref 后它就不再代劳),此后 Ask 输入区一直禁用、runAsk 直接早退。
  function abortIntentPreview() {
    // 阶段判据必须是流程状态,不能是「草稿 ref 还非空」:草稿要留到 executeAsk
    // 确认接下这次运行为止,而新会话第一轮的 started 回调会 setConversationId、
    // 触发上面那个 effect —— 用草稿当标记就会在整轮生成期间把问题气泡和轨迹
    // 一起清掉。submitting 之后已经不是理解阶段,那时的在途 turn 归 executeAsk。
    const wasIntentPhase = askIntentFlowRef.current === "preview"
      || askIntentFlowRef.current === "review";
    askIntentAbortRef.current?.abort();
    askIntentAbortRef.current = null;
    askIntentFlowRef.current = "idle";
    askIntentTraceRef.current = [];
    askIntentDraftRef.current = "";
    askIntentDraftOwnerRef.current = null;
    setIntentChecking(false);
    // 只收自己那半边:真正在跑的 ask 有自己的在途 turn,不能被理解阶段的收尾误清。
    if (wasIntentPhase) clearPendingTurn();
  }

  // 释放草稿槽,并回报本轮是否真的持有它。清理点分散在 `await executeAsk` 之后与
  // catch 之中,逐点手写条件极易漏一处 —— 第 9 轮那个「旧 run 抹掉新一轮草稿」的
  // 竞态就是这么来的。统一收敛到这里,守卫据此禁止在 runAsk/confirmAskIntent 里
  // 裸清草稿槽。
  function releaseIntentDraft(token: object): boolean {
    if (askIntentDraftOwnerRef.current !== token) return false;
    askIntentDraftRef.current = "";
    askIntentDraftOwnerRef.current = null;
    return true;
  }

  async function runAsk(nextQuestion = question) {
    if (
      !currentNotebookId || asking || intentChecking || sessionLoading
      || askIntentReview || askIntentFlowRef.current !== "idle"
    ) return;
    const q = nextQuestion.trim();
    if (!q) return;
    if (isAskBlocked(currentNotebook)) {
      setToast("请先添加来源，或在「设置 → 编辑当前笔记本」里挂载一个参考库，再开始对话。");
      return;
    }
    if (requiresKg(askMode) && !kgAvailable) {
      setToast(`${strictLabel}需要知识图谱 — 可在「设置 → 编辑当前笔记本」里挂一个参考库，或先整理该笔记本的知识图谱`);
      return;
    }
    const askedAt = new Date().toISOString();
    if (askMode !== "reasoning") {
      await executeAsk(q, askMode, undefined, [], askedAt);
      return;
    }

    const notebookId = currentNotebookId;
    const conversationIdAtStart = conversationId;
    const workspaceEpoch = workspaceEpochRef.current;
    const controller = new AbortController();
    askIntentFlowRef.current = "preview";
    askIntentAbortRef.current = controller;
    // 理解阶段就把问题挂上会话流,并以轨迹的第一步显示「正在理解问题」——
    // 用户在这段(一次不读语料的模型调用)看到的是同一条轨迹在推进,而不是
    // 一条与轨迹无关的灰色提示条,也不用猜检索到底开始了没有。
    const draftToken = {};
    askIntentDraftRef.current = q;
    askIntentDraftOwnerRef.current = draftToken;
    askIntentTraceRef.current = [intentUnderstandingStep()];
    setQuestion("");
    setPendingQuestion(q);
    setPendingAskedAt(askedAt);
    setPendingMode("reasoning");
    setPendingTrace(askIntentTraceRef.current);
    setIntentChecking(true);
    const understandingStartedAt = Date.now();
    try {
      const contract = await previewAskIntent(
        notebookId, q, conversationIdAtStart, controller.signal,
      );
      if (
        controller.signal.aborted
        || workspaceEpochRef.current !== workspaceEpoch
        || activeNotebookIdRef.current !== notebookId
        || activeConversationIdRef.current !== conversationIdAtStart
        || activeAskModeRef.current !== "reasoning"
      ) return;
      const understandingMs = elapsedMs(understandingStartedAt, Date.now());
      if (contract.needs_clarification) {
        askIntentTraceRef.current = replaceLastIntentStep(
          askIntentTraceRef.current, intentClarifyStep(contract, understandingMs),
        );
        setPendingTrace(askIntentTraceRef.current);
        askIntentFlowRef.current = "review";
        setAskIntentReview({
          notebookId,
          conversationId: conversationIdAtStart,
          question: q,
          contract,
          understandingMs,
          askedAt,
        });
        setToast("问题存在会改变检索方向的歧义，请先补充确认");
        return;
      }
      askIntentTraceRef.current = replaceLastIntentStep(
        askIntentTraceRef.current, intentUnderstoodStep(contract, understandingMs),
      );
      askIntentAbortRef.current = null;
      setIntentChecking(false);
      askIntentFlowRef.current = "submitting";
      // 草稿留到 executeAsk 真的接下这次运行为止:理解跑完的这段时间里证据/图谱
      // 状态可能已经变化,被它的可用性守卫拦下时输入框已空、在途 turn 也已隐藏,
      // 不退回就等于把用户打的问题悄悄吞掉。
      const started = await executeAsk(
        q,
        "reasoning",
        buildAskIntentConfirmation(
          contract, contract.resolved_question, {}, understandingMs,
        ),
        handOffIntentTrace(askIntentTraceRef.current),
        askedAt,
      );
      // 只有仍持有草稿槽的那一轮才有权清理:期间用户可能已经切会话并开了新一轮
      // 预检,那份草稿不归本轮处置(codex 第 9 轮 P2)。
      if (releaseIntentDraft(draftToken) && !started) {
        setQuestion(q);
        askIntentTraceRef.current = [];
        clearPendingTurn();
      }
    } catch (error) {
      if (!isAbortError(error)) reportError(error);
      // 预检失败/被中止:收起在途 turn 并把草稿还给输入框。别人已经接管(切库、
      // 切会话)时 ref 已不是本 controller,由接管方负责清理,这里不越权。
      // 同上,草稿槽的归属也要认令牌:抛错前若已切会话并开了新一轮预检,那份草稿
      // 不归本轮处置。
      const draft = askIntentDraftRef.current;
      if (askIntentAbortRef.current === controller && releaseIntentDraft(draftToken)) {
        setQuestion(draft || q);
        askIntentTraceRef.current = [];
        clearPendingTurn();
      }
    } finally {
      if (askIntentAbortRef.current === controller) {
        askIntentAbortRef.current = null;
        setIntentChecking(false);
      }
      if (askIntentFlowRef.current !== "review") {
        askIntentFlowRef.current = "idle";
      }
    }
  }

  async function executeAsk(
    nextQuestion: string,
    selectedMode: AskModeId,
    intent?: AskIntentConfirmation,
    // 理解阶段合成的轨迹前缀。后端流下来的步骤追加在它后面,用户看到的是一条
    // 从「理解问题」一路走到「作答」的连续轨迹,而不是从中途冒出来的半截。
    traceSeed: ReasoningTraceStep[] = [],
    askedAt = new Date().toISOString(),
    // 返回「本次运行有没有被真正接下」。下面这几道守卫可能在问题理解跑完之后
    // 才拦下来(那段时间里证据/图谱状态会变),调用方要据此把草稿退回输入框 ——
    // 否则输入框已清空、在途 turn 也已隐藏,用户的问题就此无声消失。
  ): Promise<boolean> {
    if (!currentNotebookId) return false;
    if (asking || sessionLoading) return false;
    const q = nextQuestion.trim();
    if (!q) return false;
    // 硬约束:后端判定无可检索证据时禁止提问(也挡住快捷提问 chip 这条旁路)。
    if (isAskBlocked(currentNotebook)) {
      setToast("请先添加来源，或在「设置 → 编辑当前笔记本」里挂载一个参考库，再开始对话。");
      return false;
    }
    if (requiresKg(selectedMode) && !kgAvailable) {
      setToast(`${strictLabel}需要知识图谱 — 可在「设置 → 编辑当前笔记本」里挂一个参考库，或先整理该笔记本的知识图谱`);
      return false;
    }
    const notebookId = currentNotebookId;
    const conversationIdAtStart = conversationId;
    let startedConversationId = conversationIdAtStart;
    const workspaceEpoch = workspaceEpochRef.current;
    const runEpoch = ++askRunEpochRef.current;
    const ownsRun = () => ownsWorkspaceRun(
      runEpoch,
      askRunEpochRef.current,
      workspaceEpoch,
      workspaceEpochRef.current,
      notebookId,
      activeNotebookIdRef.current,
    );
    setChatMode("ask");
    setQuestion("");
    setPendingQuestion(q);
    setPendingAskedAt(askedAt);
    setPendingMode(selectedMode);
    setPendingTrace(traceSeed);
    setAsking(true);
    const controller = new AbortController();
    // Bind this controller to a not-yet-started run. A job id from a detached
    // previous notebook/session must never be paired with the new transport.
    askJobIdRef.current = null;
    askAbortRef.current = controller;
    askNotebookIdRef.current = notebookId;
    try {
      const payload = {
        question: q,
        asked_at: askedAt,
        conversation_id: conversationId ?? undefined,
        mode: selectedMode,
        retrieval_effort: askRetrievalEffort,
        ...(intent ? { intent } : {}),
      };
      const response = await runAskStream<AskResponse>(
        notebookId,
        payload,
        (step) => {
          if (ownsRun()) setPendingTrace((previous) => [...previous, step]);
        },
        controller.signal,
        async (jobId, durableConversationId) => {
          startedConversationId = durableConversationId;
          if (cancelRequestedControllersRef.current.delete(controller)) {
            // 不能在 jobId 出现前 abort，否则 started 永远无法被读取，后端 detached
            // worker 也就无法被显式取消。runAskStream 会 await 此回调，所以取消完成前
            // 不会继续消费 progress/final。
            await cancelAskJob(notebookId, jobId).catch(() => {});
            controller.abort();
            await refreshActiveSessions(notebookId).catch(() => {});
            return;
          }

          const ownsVisibleRun = ownsRun();
          if (ownsVisibleRun) {
            askJobIdRef.current = jobId;
            setConversationId(durableConversationId);
          }
          if (notebookIsActive(notebookId, activeNotebookIdRef.current)) {
            optimisticConversationIdsRef.current.add(durableConversationId);
            setSessions((current) => recordStartedConversation(current, {
              conversationId: durableConversationId,
              question: q,
              startedAt: new Date().toISOString(),
            }));
            // 历史归属于 notebook，不归属于当前展示的 run。即使 started 前已切到
            // 同库旧会话，也必须发布这条可重开的 durable conversation。
            loadSessions(notebookId).catch(() => {
              // 乐观历史项已可用，短暂列表失败不终止 Ask stream。
            });
          }
          if (!ownsVisibleRun) return;
        },
      );
      if (!ownsRun()) {
        await refreshActiveSessions(notebookId).catch(() => {});
        return true;
      }
      setTurns((prev) => [...prev, { question: q, response, askedAt }]);
      setConversationId(response.conversation_id);
    } catch (error) {
      if (!ownsRun()) {
        await refreshActiveSessions(notebookId).catch(() => {});
        return true;
      }
      setQuestion(q);
      if (startedConversationId !== conversationIdAtStart) {
        setConversationId(conversationIdAtStart);
      }
      if (isAbortError(error)) {
        setToast("已中断回答");
        return true;
      }
      reportError(error);
    } finally {
      if (ownsRun()) {
        if (askAbortRef.current === controller) askAbortRef.current = null;
        askJobIdRef.current = null;
        askNotebookIdRef.current = null;
        askIntentTraceRef.current = [];
        clearPendingTurn();
        setAsking(false);
      }
      cancelRequestedControllersRef.current.delete(controller);
    }
    if (ownsRun()) await loadSessions(notebookId);
    // 走到这里本次运行已经被接下(不论最终成功、失败还是被中断)——那几条路径都
    // 自己处理过草稿,调用方不该再退回一次。
    return true;
  }

  async function confirmAskIntent(confirmation: AskIntentConfirmation) {
    const review = askIntentReview;
    if (!review || askIntentFlowRef.current !== "review") return;
    // 定稿这一轮接手草稿槽的归属(预检那一轮的令牌到此为止)。
    const draftToken = askIntentDraftOwnerRef.current ?? {};
    askIntentDraftOwnerRef.current = draftToken;
    if (
      review.notebookId !== currentNotebookId
      || review.conversationId !== conversationId
      || askMode !== "reasoning"
    ) {
      setAskIntentReview(null);
      askIntentTraceRef.current = [];
      releaseIntentDraft(draftToken);
      clearPendingTurn();
      setToast("问题上下文已经变化，请重新提交");
      return;
    }
    askIntentFlowRef.current = "submitting";
    setAskIntentReview(null);
    // 定稿也是理解阶段的一步:让轨迹如实记下最终问题与用户补充了几条说明。
    const traceSeed = [
      ...askIntentTraceRef.current,
      intentConfirmedStep(confirmation.resolved_question, confirmation.answers.length),
    ];
    askIntentTraceRef.current = traceSeed;
    try {
      // 同 runAsk:草稿留到 executeAsk 真的接下这次运行为止,且只有仍持有草稿槽的
      // 那一轮才有权清理(codex 第 9 轮 P2)。
      const started = await executeAsk(
        review.question,
        "reasoning",
        confirmation,
        handOffIntentTrace(traceSeed),
        review.askedAt,
      );
      if (releaseIntentDraft(draftToken) && !started) {
        setQuestion(review.question);
        askIntentTraceRef.current = [];
        clearPendingTurn();
      }
    } finally {
      askIntentFlowRef.current = "idle";
    }
  }

  function cancelAskIntentReview() {
    askIntentFlowRef.current = "idle";
    setAskIntentReview(null);
    // 用户要回去改问题:把草稿还给输入框,并收起理解阶段的在途 turn。
    setQuestion(askIntentDraftRef.current || askIntentReview?.question || "");
    askIntentDraftRef.current = "";
    askIntentDraftOwnerRef.current = null;
    askIntentTraceRef.current = [];
    clearPendingTurn();
    setToast("已返回修改问题");
  }

  function abortAsk() {
    if (intentChecking) {
      const draft = askIntentDraftRef.current;
      abortIntentPreview();
      if (draft) setQuestion(draft);   // 停在理解阶段:原样退回草稿,不丢用户输入
      setToast("已取消问题理解");
      return;
    }
    const jobId = askJobIdRef.current;
    const nb = askNotebookIdRef.current;
    const controller = askAbortRef.current;
    // 显式取消 = 打 cancel 端点(真取消后端 worker),再 abort 本地流(立即停读)。
    // 离开/刷新页面不会走这里,故 worker 会继续跑到完(WS2a 的核心)。
    if (jobId && nb) {
      cancelAskJob(nb, jobId)
        .then(() => refreshActiveSessions(nb))
        .catch(() => {});
      controller?.abort();
    } else if (controller) {
      // 先不 abort：继续读取第一条 started，回调中补打后端 cancel 后再 abort。
      // 否则请求已到后端但首 chunk 未达时，worker 会脱离客户端继续执行。
      cancelRequestedControllersRef.current.add(controller);
      // UI 立即退出占用态并恢复草稿；后台 stream 仍由该 controller 的闭包继续
      // 读到 started 完成取消握手。递增 run epoch 防止旧 final 回写回答区。
      askRunEpochRef.current += 1;
      if (askAbortRef.current === controller) askAbortRef.current = null;
      askJobIdRef.current = null;
      askNotebookIdRef.current = null;
      setQuestion(pendingQuestion);
      setPendingQuestion("");
      setPendingMode(DEFAULT_ASK_MODE);
      setPendingTrace([]);
      setAsking(false);
      setToast("正在中断回答");
    }
  }

  function refreshActiveSessions(notebookId: string): Promise<ConversationSummary[] | null> {
    if (!notebookIsActive(notebookId, activeNotebookIdRef.current)) {
      return Promise.resolve(null);
    }
    return loadSessions(notebookId);
  }

  async function loadSessions(
    notebookId: string | null = currentNotebookId,
  ): Promise<ConversationSummary[] | null> {
    if (!notebookId) return null;
    // 历史列表是 notebook 级状态，同库切会话不应使有效响应过期；旧 notebook
    // 的延迟刷新则不应发请求，也不能递增 generation。
    if (!notebookIsActive(notebookId, activeNotebookIdRef.current)) return null;
    const requestId = ++sessionListRequestRef.current;
    const request = {
      notebookId,
      requestId,
      promise: listConversations(notebookId),
    };
    latestSessionListRef.current = request;
    const resolved = await followLatestNotebookRequest(
      request,
      () => latestSessionListRef.current,
      () => notebookIsActive(notebookId, activeNotebookIdRef.current),
    );
    if (!resolved) return null;
    if (sessionListRequestIsCurrent(
      resolved.generationId,
      sessionListRequestRef.current,
      notebookId,
      activeNotebookIdRef.current,
    )) {
      if (resolved.requestId === resolved.generationId) {
        optimisticConversationIdsRef.current.clear();
        setSessions(resolved.value);
      } else {
        setSessions((current) => mergeSessionListFallback(
          current,
          resolved.value,
          optimisticConversationIdsRef.current,
        ));
      }
    }
    // Even when a superseding request failed, orchestration callers can use
    // this valid same-notebook fallback; stale data is never published here.
    return resolved.value;
  }

  // 会话详情 → state 的灌入内核。刻意不碰 workspaceEpochRef:调用方各自持有
  // 自己的 epoch(openSession 新推一个、openNotebook 沿用自己的),内核只做校验。
  // 这是 openNotebook 能复用它而不自撞守卫的唯一原因。
  async function applySessionDetail(id: string, expectedWorkspaceEpoch: number) {
    const detail = await getConversation(id);
    if (workspaceEpochRef.current !== expectedWorkspaceEpoch) return;
    const summary: ConversationSummary = {
      id: detail.id,
      title: detail.title,
      updated_at: detail.updated_at,
      turn_count: detail.turn_count,
      used_reasoning: detail.used_reasoning ?? Boolean(
        detail.turns[detail.turns.length - 1]?.response.reasoning_trace?.length,
      ),
    };
    setSessions((current) => current.some((session) => session.id === detail.id)
      ? current.map((session) => session.id === detail.id ? summary : session)
      : [summary, ...current]);
    setTurns(detail.turns.map((turn) => ({
      question: turn.question,
      response: turn.response,
      askedAt: turn.asked_at,
    })));
    setAskMode(modeFromTurn(detail.turns[detail.turns.length - 1]));
    setAskRetrievalEffort(retrievalEffortFromTurn(detail.turns[detail.turns.length - 1]));
    setConversationId(id);
    setPendingQuestion("");
    setPendingAskedAt("");
    setPendingMode(DEFAULT_ASK_MODE);
    setPendingTrace([]);
    setChatMode("ask");
    setSessionPanelOpen(false);
    setRenamingSessionId(null);
    // Reconnected jobs do not own a local fetch controller. Drop any detached
    // stream controller from a previously viewed run so cancel cannot pair B's
    // job id with A's transport; A's closure/Set still owns its own controller.
    askAbortRef.current = null;
    const active = detail.active_job;
    if (active) {
      // 把在途 turn 渲染成「生成中」并接回实时轨迹(仿正在 ask 的 UI)。
      setPendingQuestion(active.question);
      setPendingAskedAt(active.asked_at);
      setPendingMode(modeFromTurn({ response: { mode: active.mode } }));
      setPendingTrace(active.trace ?? []);
      setAsking(true);
      askJobIdRef.current = active.job_id;                  // 「停止」可作用于重连的 job
      askNotebookIdRef.current = activeNotebookIdRef.current;
      reconnectConvIdRef.current = id;
      setReconnectJob({ jobId: active.job_id, seen: (active.trace ?? []).length });
    } else {
      setReconnectJob(null);
      setAsking(false);
      askJobIdRef.current = null;
      askNotebookIdRef.current = null;
    }
  }

  async function openSession(id: string) {
    const workspaceEpoch = ++workspaceEpochRef.current;
    askRunEpochRef.current += 1;
    abortIntentPreview();
    setSessionLoading(true);
    memoryLinksAbortRef.current?.abort();
    memoryLinksAbortRef.current = null;
    setAsking(false);
    setReconnectJob(null);
    setMemoryAnswerId(null);
    askAbortRef.current = null;
    askJobIdRef.current = null;
    askNotebookIdRef.current = null;
    try {
      await applySessionDetail(id, workspaceEpoch);
    } finally {
      if (workspaceEpochRef.current === workspaceEpoch) setSessionLoading(false);
    }
  }

  function startNewSession() {
    workspaceEpochRef.current += 1;
    askRunEpochRef.current += 1;
    abortIntentPreview();
    memoryLinksAbortRef.current?.abort();
    memoryLinksAbortRef.current = null;
    setAsking(false);
    setSessionLoading(false);
    setReconnectJob(null);
    setMemoryAnswerId(null);
    askAbortRef.current = null;
    askJobIdRef.current = null;
    askNotebookIdRef.current = null;
    setTurns([]);
    setMemorySavedAnswers({});
    setConversationId(null);
    setAskMode(DEFAULT_ASK_MODE);
    setAskRetrievalEffort(DEFAULT_ASK_RETRIEVAL_EFFORT);
    setPendingQuestion("");
    setPendingMode(DEFAULT_ASK_MODE);
    setPendingTrace([]);
    setChatMode("ask");
    setSessionPanelOpen(false);
    setRenamingSessionId(null);
  }

  async function deleteSession(id: string) {
    await deleteConversation(id);
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
        { label: "删除", danger: true, action: () => { bulkCleanup(days).catch(reportError); } },
      ],
    });
  }

  async function bulkCleanup(days: number) {
    const notebookId = currentNotebookId;
    if (!notebookId) return;
    const workspaceEpoch = workspaceEpochRef.current;
    const conversationIdAtStart = conversationId;
    const isCurrent = () => workspaceRequestIsCurrent(
      false,
      workspaceEpoch,
      workspaceEpochRef.current,
      notebookId,
      activeNotebookIdRef.current,
    );
    await runOwnedConversationCleanup(
      bulkDeleteConversations(notebookId, days),
      isCurrent,
      ({ deleted_ids: deletedIds }) => {
        setSessions((currentSessions) => reconcileConversationCleanup(
          currentSessions,
          conversationIdAtStart,
          deletedIds,
        ).sessions);
        const { currentDeleted } = reconcileConversationCleanup(
          [],
          conversationIdAtStart,
          deletedIds,
        );
        if (currentDeleted) {
          setTurns([]);
          setConversationId(null);
          setPendingQuestion("");
          setPendingMode(DEFAULT_ASK_MODE);
          setPendingTrace([]);
        }
      },
      async () => {
        await loadSessions(notebookId);
      },
      ({ deleted }) => {
        setToast(conversationCleanupToast(deleted));
      },
    );
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
    await renameConversation(sessionId, next);
    await loadSessions(currentNotebookId);
    setRenamingSessionId(null);
    setToast("会话已重命名");
  }


  async function loadKnowledge(kind: KnowledgeKind, opts: { status?: string; page?: number } = {}) {
    if (!currentNotebookId) return;
    const pageNum = opts.page ?? 0;
    const statusParam = (opts.status && opts.status !== "all") ? opts.status : "";
    const result = await listKnowledge(currentNotebookId, kind, statusParam, pageNum * 50, 50);
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
    const types = await listKnowledgeTypes(currentNotebookId);
    setKnowledgeTypes(types);
    return types;
  }

  async function updateKnowledge(id: string, patch: { status?: string; owner?: string }) {
    if (!currentNotebookId) return;
    await updateKnowledgeRecord(currentNotebookId, id, patch);
    await loadKnowledge(knowledgeKind, { status: knowledgeStatusFilter, page: 0 });
    await loadKnowledgeTypes();
    await loadNotebookCollection();
    const refreshed = await getNotebook(currentNotebookId);
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
    const response = await findKnowledgeDuplicates(currentNotebookId, kind);
    setDuplicates(response);
  }

  async function mergeKnowledge(sourceId: string, intoId: string) {
    if (!currentNotebookId) return;
    await mergeKnowledgeRecords(currentNotebookId, sourceId, intoId);
    await loadKnowledge(knowledgeKind, { status: knowledgeStatusFilter, page: 0 });
    await loadKnowledgeTypes();
    await findDuplicates(knowledgeKind);
    setToast("已合并，原条目已弃用");
  }


  function closeAnalytics() {
    analyticsLoadScopeRef.current.cancel();
    setAnalytics(null);
    setContentOverview(null);
    setContentOverviewError("");
    setContentOverviewLoading(false);
  }

  function closeKnowhow() {
    setKnowhowNavigation((current) => closeKnowhowNavigation(current));
    // knowhow 抽屉是覆盖层、不改 chatMode,故上面的 chatMode effect 不会触发。关闭回到
    // 问答框时与后端对齐:删表→重新禁用、建表(投影落库后)→解禁,长投影退避轮询到位。
    revalidateAskAvailabilityAfterKnowhow();
  }

  async function openAnalytics() {
    if (!currentNotebookId) return;
    const nb = currentNotebookId;
    const owner = analyticsLoadScopeRef.current.begin(nb);
    const isCurrent = () => analyticsLoadScopeRef.current.isCurrent(owner, activeNotebookIdRef.current);
    setContentOverview(null);
    setContentOverviewLoading(true);
    setContentOverviewError("");
    const loads = startAnalyticsLoads({
      analytics: () => fetchNotebookAnalytics(nb),
      indexStatus: () => fetchIndexStatus(nb),
      contentOverview: () => fetchNotebookContentOverview(nb),
    });
    // 体检:看板打开时随三系统状态一起拉一次(只读、无模型)。命中问题时驱动「来源
    // 状态」块(H2–H6 源级)、「索引与构建」块(H7/H8 索引可信度)与铃铛的一条聚合
    // 提醒。best-effort:失败不拦看板其余部分。
    void fetchCheckup(nb).then((result) => {
      if (isCurrent()) setCheckup(result);
    }).catch(() => {});
    // 看板打开时经聚合端点一次拉齐三系统(kg/概念合并/检索索引)当前态(而非上次切库的
    // 快照),取代原先仅单独拉检索索引状态;顺带回填 scaleIndexStatus(供既有
    // runScaleIndexOp 等复用)与忙碌位(供既有轮询接回跨会话发起的构建)。
    void loads.indexStatus.then((result) => {
      if (!result.ok || !isCurrent()) return;
      const status = result.value;
      setIndexStatus(status);
      setScaleIndexStatus(status.scale_index);
      if (status.kg.job?.status === "running") {
        setTrackedKgJobId(status.kg.job.job_id);
      }
      if (status.kg.building) setBuildingKg(true);
      if (shouldResumeScaleIndex(status.scale_index)) setBuildingScaleIndex(true);
    });
    void loads.contentOverview.then((result) => {
      if (!isCurrent()) return;
      if (result.ok) {
        setContentOverview(result.value);
        setContentOverviewError("");
      } else {
        setContentOverview(null);
        setContentOverviewError("内容资产暂时不可用");
      }
      setContentOverviewLoading(false);
    });
    try {
      const response = await loads.analytics;
      if (isCurrent()) setAnalytics(response);
    } catch (error) {
      if (isCurrent()) throw error;
    }
  }

  function openAnalyticsMemory(
    status: "candidate" | "confirmed" | null,
    itemId: string | null,
  ) {
    if (!currentNotebookId) return;
    const target = { notebookId: currentNotebookId, status, itemId };
    closeAnalytics();
    showGlobalMemory(target);
  }

  function openAnalyticsKnowhow(
    filter: KnowhowHealthFilter,
    tableId: string | null,
  ) {
    closeAnalytics();
    setKnowhowNavigation(openKnowhowNavigation({
      healthFilter: filter,
      jumpTarget: tableId ? { tableId, rowId: null } : null,
    }));
  }

  async function loadSchemas() {
    const response = await listObjectSchemas();
    setSchemas(response);
  }

  function openSchemas() {
    setSchemaModalOpen(true);
    loadSchemas().catch(reportError);
  }

  async function patchSchema(objectType: string, patch: Partial<ObjectSchema> & { status?: string }) {
    setSchemaBusy(true);
    try {
      await updateObjectSchema(objectType, patch);
      await loadSchemas();
      setToast("类型已更新");
    } finally {
      setSchemaBusy(false);
    }
  }

  async function createSchema(payload: { object_type: string; label: string; fields: string[]; description: string }) {
    setSchemaBusy(true);
    try {
      await createObjectSchema(payload);
      await loadSchemas();
      setToast("已新增类型");
    } finally {
      setSchemaBusy(false);
    }
  }

  async function deleteSchema(objectType: string) {
    setSchemaBusy(true);
    try {
      await deleteObjectSchema(objectType);
      await loadSchemas();
      setToast("类型已删除");
    } finally {
      setSchemaBusy(false);
    }
  }

  async function openGraph() {
    if (!currentNotebookId) return;
    setGraphOpen(true);
    const response = await getKnowledgeGraph(currentNotebookId);
    setGraph(response);
  }

  // Task 12（引用跳转）：ask 引用命中 knowhow 格子时「在表格中查看」的落点——
  // 打开 Knowhow 面板并记下目标表/行，KnowhowPanel 自己负责挂载后定位到该
  // 行的抽屉（含目标表/行已被删除时的兜底提示，见 knowhow-panel.tsx）。
  function openKnowhowAt(tableId: string, rowId: string) {
    setKnowhowNavigation(openKnowhowNavigation({ jumpTarget: { tableId, rowId } }));
  }

  async function openKgView(
    targetNodeId?: string,
    notebookId: string | null = currentNotebookId,
    sourceNotebookId: string | null = notebookId,
  ) {
    if (!notebookId) return;
    const requestId = ++kgOpenRequestRef.current;
    const workspaceEpoch = workspaceEpochRef.current;
    setKgViewOpen(true);
    setKgAnalysisOpen(false);                     // 上次留下的分析窗不得跟着重开(见 state 声明处)
    setSelectedKgNodeId(null); setConceptDetail(null); setNodeCtx(null);
    setKgSearch(""); setKgSearchHits([]); setKgSearchBusy(false);
    setKgExpandedNodes([]); setKgExpandedEdges([]);
    setKgSelectedTypes([]);
    setKgLimit(KG_RANGE_DEFAULT);                 // 每次打开从核心范围起，避免一上来渲染全量
    try {
      const [g, [pend, status]] = await Promise.all([
        fetchUnifiedGraph(notebookId, KG_RANGE_DEFAULT),
        Promise.all([
          fetchPendingMerges(notebookId),
          fetchUnifiedKgStatus(notebookId),
        ]),
      ]);
      // 引用携带的是原始 knowledge_object id，而图中 Concept 可能已经折叠为
      // canonical K-* id；同时大库核心图只含高连接度节点。定向拉一跳邻域既把
      // 核心范围外的目标补进来，也由后端返回真正应选中的 canonical id。
      let neighborhood: KgNeighborsResp | null = null;
      if (targetNodeId) {
        try {
          neighborhood = await fetchKgNeighbors(
            notebookId,
            targetNodeId,
            50,
            sourceNotebookId || notebookId,
          );
        } catch { /* 核心图仍可展示；下面按 focus 是否可达给出定位提示。 */ }
      }
      if (
        requestId !== kgOpenRequestRef.current
        || activeNotebookIdRef.current !== notebookId
        || workspaceEpochRef.current !== workspaceEpoch
      ) return;
      const focus = prepareKgFocus(g, targetNodeId, neighborhood);
      const nodeNotebookIds = new Map<string, string>();
      const resolvedSourceNotebookId = neighborhood?.source_notebook_id
        || sourceNotebookId
        || notebookId;
      if (targetNodeId) {
        for (const node of neighborhood?.nodes ?? []) {
          nodeNotebookIds.set(node.id, resolvedSourceNotebookId);
        }
        if (focus.focusId) nodeNotebookIds.set(focus.focusId, resolvedSourceNotebookId);
      }
      kgNodeNotebookRef.current = nodeNotebookIds;
      const nodeContextObjectIds = new Map<string, string>();
      if (focus.focusId && focus.contextObjectId) {
        nodeContextObjectIds.set(focus.focusId, focus.contextObjectId);
      }
      kgNodeContextObjectRef.current = nodeContextObjectIds;
      setUGraph(g); setPendingMerges(pend); setUnifiedKgStatus(status);
      setKgExpandedNodes(focus.expandedNodes);
      setKgExpandedEdges(focus.expandedEdges);
      setPendingKgFocusId(focus.focusId);
      setVizBuilding(Boolean(g.viz_building));
      if (targetNodeId && neighborhood?.locating_unavailable) {
        setToast("图谱索引正在构建，暂时无法定位该引用节点；完成后请重试");
      } else if (targetNodeId && !focus.focusId) {
        setToast("知识图谱已打开，但引用节点定位失败，请重试");
      }
    } catch (err) { reportError(err); }
  }

  // 关闭知识图谱视图。刻意做成具名函数而不是内联 `() => setKgViewOpen(false)`:
  // 视图内还挂着「图谱分析」弹窗的开关,它必须和父视图一起归零(见 kgAnalysisOpen 声明处),
  // 而"关闭时要一起做的事"散在内联箭头里就迟早会漏掉一条。
  function closeKgView() {
    setKgViewOpen(false);
    setKgAnalysisOpen(false);
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
      setToast(`已补上 ${r.edges_added} 条关联，还有 ${r.isolated_after} 项内容没建立关联`);
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
    confirmIndexAction("重新合并知识图谱？\n\n将重算跨文档概念聚类并刷新图谱索引（不重新分析来源）。后台进行，完成后自动更新。", () => refreshUnifiedKg());
  }

  async function reviewPendingMerges() {
    if (!currentNotebookId) return;
    setKgReviewBusy(true);
    setToast("正在自动判重（约 1 分钟，请稍候）…");
    try {
      const summary = await reviewMerges(currentNotebookId);
      setToast(`已判重 ${summary.reviewed} 项：合并 ${summary.confirmed}，分开 ${summary.rejected}，保留 ${summary.unsure}`);
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
    if (!currentNotebookId || reviewAllStarting) return;
    const nb = currentNotebookId;
    // 忙碌位必须在 await **之前**置(对齐 reviewPendingMerges 的 setKgReviewBusy):
    // 按钮原本只看 reviewAllJob?.status,而那个要等 POST 回来才写,中间这段窗口按钮
    // 仍可点、点几下就排几个全量预审 job。不复用 reviewAllRunning 是为了不让轮询
    // effect 去追一个还不存在的 job。
    setReviewAllStarting(true);
    try {
      await reviewAllMergesRequest(nb);
      setReviewAllJob({ status: "running", total: pendingMerges.length, done: 0, error: "" });
      setReviewAllRunning(true);
    } catch (err) { reportError(err); }
    finally { setReviewAllStarting(false); }
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
    const nodeNotebookId = kgNodeNotebookRef.current.get(nodeId) || currentNotebookId;
    setSelectedKgNodeId(nodeId);
    setNodeCtx(null);
    focusKgGraphNode(nodeId);
    window.setTimeout(() => {
      kgDetailRef.current?.scrollTo({ top: 0, behavior: "smooth" });
    }, 0);
    let resolvedNodeNotebookId = nodeNotebookId;
    // 逐跳展开：拉取邻居节点/边并合并进视图（去重由 uGraphMerged 处理）。
    try {
      const neighbors = await fetchKgNeighbors(
        currentNotebookId,
        nodeId,
        50,
        nodeNotebookId,
      );
      resolvedNodeNotebookId = neighbors.source_notebook_id || nodeNotebookId;
      if (
        neighbors.focus_id
        && neighbors.focus_object_id
        && (
          !kgNodeContextObjectRef.current.has(neighbors.focus_id)
          || neighbors.focus_object_id !== neighbors.focus_id
        )
      ) {
        kgNodeContextObjectRef.current.set(
          neighbors.focus_id,
          neighbors.focus_object_id,
        );
      }
      if (neighbors.nodes.length > 0 || neighbors.edges.length > 0) {
        for (const node of neighbors.nodes) {
          if (!kgNodeNotebookRef.current.has(node.id)) {
            kgNodeNotebookRef.current.set(node.id, resolvedNodeNotebookId);
          }
        }
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
    else { try { setConceptDetail(await fetchConceptDetail(currentNotebookId, nodeId, resolvedNodeNotebookId)); } catch (err) { setConceptDetail(null); reportError(err); } }
    const contextObjectId = kgNodeContextObjectRef.current.get(nodeId) || nodeId;
    try { setNodeCtx(await fetchNodeContext(currentNotebookId, contextObjectId, resolvedNodeNotebookId)); } catch { /* node context best-effort */ }
  }

  async function decideMerge(candidate: PendingMerge, confirm: boolean) {
    if (!currentNotebookId || decidingMerge) return;
    setDecidingMerge({ id: candidate.id, confirm });
    try {
      if (confirm) await confirmMerge(currentNotebookId, candidate.id);
      else await rejectMerge(currentNotebookId, candidate.id);
      setPendingMerges((items) => withoutDecidedMerge(items, candidate));
      await rebuildUnifiedKg(currentNotebookId);
      const [g, pend] = await Promise.all([fetchUnifiedGraph(currentNotebookId, kgLimit), fetchPendingMerges(currentNotebookId)]);
      setUGraph(g); setKgExpandedNodes([]); setKgExpandedEdges([]);
      setPendingMerges(withoutDecidedMerge(pend, candidate));
      const selected = selectedKgNodeId ? g.nodes.find((node) => node.id === selectedKgNodeId) : null;
      if (selected?.object_type === "concept") setConceptDetail(await fetchConceptDetail(currentNotebookId, selected.id).catch(() => null));
      else setConceptDetail(null);
      if (!selected) setNodeCtx(null);
    } catch (err) { reportError(err); }
    finally { setDecidingMerge(null); }
  }

  async function induceSchemas() {
    if (!currentNotebookId) return;
    setSchemaBusy(true);
    try {
      const proposals = await proposeObjectSchemas(currentNotebookId);
      await loadSchemas();
      setToast(proposals.length ? `归纳出 ${proposals.length} 个候选类型` : "未发现可补充的新类型（或模型服务暂不可用）");
    } finally {
      setSchemaBusy(false);
    }
  }

  // --- Two-tier federation: mark notebook base / personal -----------------
  async function handleTierAction() {
    if (!currentNotebook) return;
    const state = tierActionState(currentNotebook);
    const target = state.action === "unset" ? "personal" : "base";
    const updated = await setNotebookTier(currentNotebook.id, target);
    setCurrentNotebook(updated as NotebookSummary);
    await loadNotebookCollection();
    setToast(
      target === "base"
        ? "已设为公共知识库 — 其他笔记本可以在设置里把它挂为参考库"
        : "已取消公共知识库，恢复为个人知识库"
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
      await openNotebook(String(created.id));
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
      await openNotebook(String(joined.id));
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
      const remaining = await listNotebooks();
      setNotebooks(remaining);
      const stillThere = remaining.some((n) => n.id === leftId);
      if (!stillThere) {
        const firstOwned = remaining.find((n) => (n.access ?? "owner") === "owner");
        if (firstOwned) {
          // 传 "none" 让 openNotebook 别写 history,自己 replaceState 顶替被退出的
          // #notebook=<leftId>——否则那条历史条目存活,按返回键会撞上已经 403 的旧笔记本。
          await openNotebook(firstOwned.id, "none");
          window.history.replaceState(null, "", notebookHash(firstOwned.id));
        } else {
          showCollection();
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

  // targetBaseId 未传时按 promotionTarget(渲染时用 currentNotebookBases 算出)三态分派:
  // none(0 个公共库挂载)拒绝、auto(1 个)直接用、choose(>1 个)转去弹选择器,选好后
  // 选择器自己会带着 targetBaseId 回调本函数——这一次不再重新分派,直接提交。
  async function submitPromotion(objectId: string, targetBaseId?: string) {
    if (!currentNotebookId) return;
    if (!targetBaseId) {
      if (promotionTarget.kind === "none") {
        setToast("需先挂载一个公共知识库，才能贡献内容");
        return;
      }
      if (promotionTarget.kind === "choose") {
        setPendingPromotionObjectId(objectId);
        return;
      }
      targetBaseId = promotionTarget.baseId;
    }
    await proposePromotion(currentNotebookId, objectId, targetBaseId);
    setToast("已提交贡献申请");
  }

  async function decidePromotion(candidateId: string, decision: "approve" | "reject", reason = "") {
    setPromoBusy(true);
    try {
      if (decision === "approve") {
        const result = await approvePromotion(candidateId);
        const merged = result.merged_into ? `（与 ${result.merged_into.slice(0, 8)} 合并）` : "";
        setToast(`已批准收录${merged}，内容已加入公共知识库`);
      } else {
        await rejectPromotion(candidateId, reason);
        setToast("贡献未采纳，个人内容保持不变");
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
  async function openEdgeReviewQueue(notebookId: string | null = currentNotebookId) {
    if (!notebookId) return;
    const queue = await fetchEdgeReviewQueue(notebookId);
    if (activeNotebookIdRef.current !== notebookId) return;
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
    if (currentNotebookId) {
      window.history.replaceState(
        null,
        "",
        mode === "memory" ? memoryHash(currentNotebookId) : notebookHash(currentNotebookId),
      );
    }
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
      // 提交晋升要知道本笔记本挂了几个公共知识库(resolvePromotionTarget)。/bases
      // 是 owner-only 端点,非 owner 404(见 notebook-bases.ts 顶部注释)——不能像
      // loadKnowledgeTypes 那样对所有访客无条件调用,这里显式门控 canGovernKnowledge。
      if (currentNotebookId && capabilities.canGovernKnowledge) {
        listBases(currentNotebookId).then(setCurrentNotebookBases).catch(reportError);
      }
    }
  }

  // 全工作区 catch 分支的统一出口(90+ 调用点)。此前它直出 err.message,于是
  // fetch 自身 reject 时用户看到的是「服务异常：Failed to fetch」——那条路径
  // 根本进不了 throwHumanizedHttpError。现在一律过人话层:已翻译的中文原样保留
  // (保住 401/403/404/409 的语义差别),英文技术串只进 console。
  function reportError(error: unknown) {
    setStatusText(toUserMessage(error, "服务出了点问题，请稍后重试"));
  }

  async function handleLogout() {
    workspaceEpochRef.current += 1;
    sourcePageRequestRef.current += 1;
    askRunEpochRef.current += 1;
    activeNotebookIdRef.current = null;
    abortIntentPreview();
    askAbortRef.current?.abort();
    memorySessionAbortRef.current.abort();
    memoryLinksAbortRef.current?.abort();
    memoryLinksAbortRef.current = null;
    setMemoryAnswerId(null);
    await logoutUser();
    setCurrentUser(null);
    window.location.reload();
  }

  function openModelPanel(serviceId: string | null = null) {
    modelPanelReturnFocusRef.current = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
    setHighlightedModelServiceId(serviceId);
    setModelPanelOpen(true);
    void refreshModelStatus();
  }

  function closeModelPanel() {
    setModelPanelOpen(false);
    setHighlightedModelServiceId(null);
  }

  async function runSystemModelTest(serviceId: string): Promise<ModelServiceStatusItem | null> {
    const coordinator = modelTestCoordinatorRef.current;
    const ticket = coordinator.beginOne(serviceId);
    if (!ticket) return null;
    setModelTestActivity(coordinator.snapshot());
    try {
      const item = await testSystemModelService(serviceId);
      if (!coordinator.isCurrent(ticket)) return null;
      modelStatusRequestRef.current += 1;
      setModelStatusState((current) => applyModelServiceTestResult(current, item, "single"));
      return item;
    } catch (e) {
      if (!coordinator.isCurrent(ticket)) return null;
      reportError(e);
      throw e;
    } finally {
      coordinator.finish(ticket);
      setModelTestActivity(coordinator.snapshot());
    }
  }

  async function runAllSystemModelTests() {
    const coordinator = modelTestCoordinatorRef.current;
    const ticket = coordinator.beginAll();
    if (!ticket) return;
    setModelTestActivity(coordinator.snapshot());
    try {
      const result = await testAllSystemModelServices();
      if (!coordinator.isCurrent(ticket)) return;
      modelStatusRequestRef.current += 1;
      setModelStatusState((current) => applyModelServiceTestResult(current, result, "all"));
    } catch (e) {
      if (coordinator.isCurrent(ticket)) reportError(e);
    } finally {
      coordinator.finish(ticket);
      setModelTestActivity(coordinator.snapshot());
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
  // 文档数量上限:管理员豁免(写路径 owner-only ⇒ 当前用户即 owner);document_limit
  // 只在 getNotebook 详情里是真值(列表投影是 0 哨兵),故 0/缺失当「未知」不门控。
  // 计数用 notebookSourceTotal(不受来源搜索过滤影响的真实可见文档总数),不用 sourcesTotal
  // ——后者在搜索态是匹配子集计数,会让满额笔记本在搜索时误判未满。
  const docCapacity = resolveDocumentCapacity({
    isAdmin: currentUser?.role === "admin",
    documentLimit: currentNotebook?.document_limit,
    documentCount: notebookSourceTotal,
  });
  const atDocCapacityHint = "已达该笔记本的文档数量上限，无法继续添加文档。";
  const sourceUploadConfigLoading = sourceUploadMaxBytes === null
    || sourceUploadMaxFilesPerBatch === null;
  const sourceBatchAtCapacity = sourceUploadMaxFilesPerBatch !== null
    && stagedFiles.length >= sourceUploadMaxFilesPerBatch;
  const sourceFilePickerDisabled = docCapacity.atCapacity
    || sourceUploadConfigLoading
    || sourceBatchAtCapacity;
  const sourceFilePickerHint = docCapacity.atCapacity
    ? atDocCapacityHint
    : sourceUploadConfigLoading
      ? "正在读取单文件上传上限…"
      : sourceBatchAtCapacity
        ? `单次最多上传 ${sourceUploadMaxFilesPerBatch} 个文件，请先上传当前批次。`
        : undefined;
  const stagedUploadBlockedReason = documentUploadBlockReason(docCapacity, stagedFiles.length);
  const capabilities = workspaceCapabilities(
    currentNotebook?.access,
    currentUser?.role ?? "",
  );
  // 挂了几个公共知识库决定「提交晋升」按钮的行为(none=禁用/auto=直接用/choose=弹选择器)。
  const promotionTarget = resolvePromotionTarget(currentNotebookBases);
  const menuNotebook = menuNotebookId
    ? notebooks.find((item) => item.id === menuNotebookId) ?? null
    : null;
  const modelSummaryView = deriveModelServiceSummaryView({
    apiStatus: health?.status ?? null,
    statusText,
    modelStatus,
    modelStatusUnavailable,
  });
  // 来源详情弹窗的异常徽标(anomaly-tiers spec)。算一次给下方两处用(是否渲染
  // 容器 div + map 渲染),避免重复调用。
  const sourceDetailAnomalies = sourceDetail ? sourceAnomalies(sourceDetail) : [];
  const sourceDetailDeleting = Boolean(sourceDetail && deletingSourceIds.has(sourceDetail.id));
  // 打开的来源属于挂载的参考库而非当前库时,只读:代理读取只给「看」,不给「改」。
  const sourceDetailBaseId = sourceDetail
    ? crossLibrarySourceNotebookId(sourceDetail.notebook_id, currentNotebookId)
    : "";
  // 库名查不到就退回泛化文案,绝不吐裸 notebook id(与清单卡的 CrossLibraryBadge 同口径)。
  const sourceDetailBaseLabel = !sourceDetailBaseId
    ? ""
    : notebookNames[sourceDetailBaseId]
      ? `来自参考库《${notebookNames[sourceDetailBaseId]}》`
      : "来自参考库";

  // 启动就绪门:在认证/加载分支之前拦截。未就绪时只展示启动屏,绝不露出登录表单或空白挂起。
  if (!serviceReady) return <StartingScreen snapshot={readySnapshot} onRetry={() => setReadyRetry((n) => n + 1)} />;
  if (!authChecked) return <div className="auth-gate"><div className="auth-card">加载中…</div></div>;
  if (!currentUser) {
    return <AuthGate onAuthenticated={(u) => {
      setCurrentUser(u);
      setStatusText("");
      loadNotebookCollection().catch(reportError);
    }} />;
  }

  const accountName = currentUser.username;
  const accountBadge = accountInitials(accountName);

  return (
    <div className={`app ${isWorkspace ? "workspace-mode" : ""}`}>
      <header className="topbar">
        <div className="brand">
          <button className="brand-mark" onClick={showCollection} title="笔记本列表">SN</button>
          <div>
            <div className="brand-title">silicon-notebook</div>
            <div className="brand-subtitle">{isWorkspace ? "笔记本工作区" : outerView === "memory" ? "私有记忆" : "笔记本列表"}</div>
          </div>
        </div>
        <div className="topbar-right">
          <ModelServiceSummaryButton
            text={modelSummaryView.text}
            tone={modelSummaryView.tone}
            title={modelSummaryView.title}
            onOpen={() => { void openModelPanel(); }}
          />
          <PendingBell
            snapshot={pending.snapshot}
            doneItems={pending.doneItems}
            userId={currentUser?.id}
            checkup={(() => {
              // 体检一条聚合提醒:命中问题即冒、点击直达看板。不复制体检详情、不新增
              // 待办类型(见 pending-center.tsx CheckupAlert)。签名随命中集合变化。
              // ⚠ 只读成员不冒此提醒:他们能看健康信息但所有修复 CTA 都 !isReader 隐藏了,
              // 「发现可修复的问题」对他们是无可点动作的噪音(评审)。
              const sig = checkupAlertSignature(checkup);
              if (!sig || !currentNotebook || isReader) return null;
              return {
                sig,
                label: `「${currentNotebook.name}」发现可修复的问题`,
                onOpen: () => { void openAnalytics(); },
              };
            })()}
            onOpenItem={(it) => openPendingItem(it).catch(reportError)}
            onOpenDone={openDoneItem}
            onDismissDone={pending.dismissDone}
          />
          <AccountMenu
            username={accountName}
            role={currentUser.role}
            initials={accountBadge}
            memoryActive={outerView === "memory"}
            showAdminUsage={canSeeAdminUsage(currentUser.role)}
            onOpenMemory={showGlobalMemory}
            onLogout={() => handleLogout().catch(reportError)}
          />
        </div>
      </header>

      {!isWorkspace && outerView === "notebooks" && (
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
                <input value={searchQuery} onChange={(event) => setSearchQuery(event.target.value)} type="search" placeholder="搜索笔记本、来源、元素" />
              </div>
              <div className="segmented" aria-label="View mode">
                {[
                  { id: "grid", icon: <LayoutGrid size={16} />, title: "卡片视图" },
                  { id: "list", icon: <ListIcon size={16} />, title: "列表视图" },
                ].map(({ id, icon, title }) => (
                  <button key={id} className={viewMode === id ? "active" : ""} title={title} aria-label={title} onClick={() => setViewMode(id)}>
                    {icon}
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
              <button className="sort-button" title="查看我分享出去的笔记本及其只读成员" onClick={() => openSharedByMe().catch(reportError)}>
                <Share2 size={15} /> 已分享
              </button>
              <button className="new-pill" onClick={() => openCreate().catch(reportError)}>＋ 新建</button>
            </div>
          </section>

          <section className="collection-title">
            <h1>我的笔记本</h1>
            {searchQuery && <p>{visibleNotebooks.length} 个笔记本，搜索 “{searchQuery}”</p>}
          </section>

          <section className={`notebook-grid view-${viewMode}`}>
            {viewMode === "list" ? (
              <NotebookList
                entries={visibleNotebooks}
                openNotebook={(id) => openNotebook(id).catch(reportError)}
                openMemory={(id) => openNotebookMemory(id).catch(reportError)}
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
                    <button className="card-menu" onClick={(event) => openNotebookMenu(notebook.id, event)} title="笔记本操作">⋮</button>
                    <button className="notebook-card-main" onClick={() => openNotebook(notebook.id).catch(reportError)}>
                      <div className="card-icon">{cardIcon(index, notebook)}</div>
                      <div>
                        <h2>{notebook.name}</h2>
                        <p>{notebook.purpose || "No purpose set yet."}</p>
                      </div>
                      <SearchHits hits={hits} compact={false} />
                    </button>
                    <div className="notebook-card-footer">
                      <p className="notebook-card-meta">{notebook.created_label} · {notebook.counts.sources ?? 0} 个来源</p>
                      <div className="notebook-card-footer-actions">
                        {notebook.access !== "reader" && notebook.is_shared && (
                          <span className="notebook-shared-badge" title="已分享" aria-label="已分享"><User size={14} /></span>
                        )}
                        <button
                          type="button"
                          className="notebook-memory-link"
                          onClick={() => openNotebookMemory(notebook.id).catch(reportError)}
                        >{notebook.counts.memories ?? 0} 条记忆</button>
                      </div>
                    </div>
                  </article>
                ))}
              </>
            )}
            {visibleNotebooks.length === 0 && (
              <article className="empty-state">
                <strong>没有找到笔记本</strong>
                <p>换一个关键词，或回到“我的笔记本”创建新的笔记本。</p>
              </article>
            )}
          </section>
        </main>
      )}

      {!isWorkspace && outerView === "memory" && (
        <main className="page memory-view">
          <MemoryPanel
            scope="global"
            notebookId={null}
            notebookBases={notebookBasesById}
            sessionSignal={memorySessionAbortRef.current.signal}
            initialNotebookId={memoryNavigationTarget.notebookId}
            initialStatus={memoryNavigationTarget.status}
            initialMemoryId={memoryNavigationTarget.itemId}
          />
        </main>
      )}

      {isWorkspace && currentNotebook && (
        <main className="notebook-view">
          <section className="workspace-header">
            <div className="workspace-title">
              <button className="notebook-home" onClick={() => showCollection()}>
                <ArrowLeft size={16} />
                <span>返回主页</span>
              </button>
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
                    aria-label="笔记本名称"
                    maxLength={80}
                    onChange={(event) => setTitleDraft(event.target.value)}
                    onBlur={() => saveInlineNotebookName().catch(reportError)}
                    onKeyDown={(event: ReactKeyboardEvent<HTMLInputElement>) => {
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
            <div className="workspace-toolbar" aria-label="笔记本操作">
              <button className="workspace-primary-action" onClick={() => openCreate().catch(reportError)}>
                <Plus size={18} strokeWidth={2.8} />
                <span>创建笔记本</span>
              </button>
              <div className="workspace-nav-group">
                {!isReader && (
                  <button className="workspace-nav-button" onClick={() => {
                    const tier = tierActionState(currentNotebook);
                    // 参考库列表随 NotebookSummary.base_notebooks 一起返回(owner/reader 都能看到,
                    // 权威只读)。弹窗顶部只读展示本笔记本挂了哪些参考库;没挂时不显示该段落。
                    const baseNames = (currentNotebook?.base_notebooks ?? []).map((b) => b.name);
                    setInfoModal({
                    title: "分析",
                    message: "对当前笔记本的知识图谱与参考库做治理与审查（部分操作仅管理员）。输出在弹窗中呈现。",
                    sections: baseNames.length ? [["本笔记本的参考库", baseNames] as [string, string[]]] : undefined,
                    actions: [
                      ...(currentUser?.role === "admin" ? [{ label: "内容审核", desc: "审核待收录进公共知识库的内容（管理员）", action: () => openPromoQueue().catch(reportError) }] : []),
                      ...(currentUser?.role === "admin" ? [{ label: tier.label, desc: "把当前笔记本设为公共知识库，供其他笔记本挂为参考库（管理员）", action: () => handleTierAction().catch(reportError) }] : []),
                      // 检索索引的立即/空闲时重建已收敛进「看板 → 索引与构建」面板(检索索引行,
                      // 与 tier 解耦、大库亦可建)，此处不再重复列出，避免同一动作多处入口各自确认。
                      { label: "关系审核队列", desc: "审核知识图谱中待人工确认的实体关联", action: () => openEdgeReviewQueue().catch(reportError) }
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
                {/* 图谱 Schema(原「内容类型」)已并入知识图谱视图,入口挪到 kg-view 头部
                    的「图谱 Schema」按钮(仍 admin 门控),此处不再单列顶层导航项。 */}
                <button className="workspace-nav-button" onClick={() => openKgView()}>
                  <Network size={17} />
                  <span>知识图谱</span>
                </button>
                <button className="workspace-nav-button" onClick={() => setKnowhowNavigation(openKnowhowNavigation())}>
                  <Table2 size={17} />
                  <span>Knowhow 表</span>
                </button>
                {!isReader && (
                  <button className="workspace-nav-button" disabled={shareBusy} onClick={() => openShareModal().catch(reportError)}>
                    <Share2 size={17} />
                    <span>分享</span>
                  </button>
                )}
                <button className="workspace-nav-button" onClick={() => openModelPanel()}>
                  <Cpu size={17} />
                  <span>模型服务</span>
                </button>
                {/* 设置直接进入笔记本编辑器(去掉原先「设置弹窗 → 编辑当前笔记本」的二级跳转);
                    只读访客拿不到 editor 数据(listMountable/listBases 是 owner-only,访客 404),
                    故只读时退回一句只读说明,不打开编辑器。 */}
                <button className="workspace-nav-button" onClick={() => {
                  if (capabilities.canWriteNotebook) {
                    openNotebookEditor(currentNotebook).catch(reportError);
                  } else {
                    setInfoModal({
                      title: "设置",
                      message: "当前笔记本为只读；模型服务由系统统一管理。",
                      actions: []
                    });
                  }
                }}>
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
                {!isReader && (
                  <button
                    type="button"
                    className="button secondary"
                    disabled={backfillingMeta}
                    title="为已上传的论文补齐作者、机构等信息"
                    onClick={async () => {
                      if (!currentNotebookId || backfillingMeta) return;
                      // POST 是 fire-and-forget:后端立刻返回 queued,真正的补抽在
                      // background_jobs 线程里跑。仿 startKgBuild 的不对称模式——
                      // 成功且 queued>0 时保持 backfillingMeta=true,交给下方轮询
                      // effect(或聚合看板 poll)检测完成;queued===0 立刻复位
                      // (无事可做);出错也复位。原本的 finally { false } 让 flag
                      // 在 HTTP 往返几百毫秒内就被清 0,轮询 gate 从来没机会跑。
                      setBackfillingMeta(true);
                      try {
                        const res = await backfillPaperMetadata(currentNotebookId);
                        if (res.queued === 0) {
                          setBackfillingMeta(false);
                          setToast("论文信息已是最新，无需补全");
                        } else {
                          setToast(`已提交 ${res.queued} 篇论文的信息补全`);
                          // 保持 backfillingMeta=true;完成检测交给轮询 effect。
                        }
                      } catch (err) {
                        setBackfillingMeta(false);
                        reportError(err);
                      }
                    }}
                  >
                    {backfillingMeta ? "补全中…" : "补全论文信息"}
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
                                title="有新增来源尚未分析，点击分析新增内容并合并进知识图谱"
                                onClick={() => { if (currentNotebookId) startKgBuild(currentNotebookId); }}
                              >
                                <Network size={20} strokeWidth={2.7} /> {buildingKg ? "分析中…" : `分析新增 ${currentNotebook.kg_pending_sources ?? "?"} 篇并合并`}
                              </button>
                              <p className="tool-hint" style={{ margin: "2px 2px 8px" }}>
                                知识图谱已就绪 · 有 {currentNotebook.kg_pending_sources ?? "?"} 篇来源待分析
                              </p>
                            </>
                          )
                          : (
                            <p className="tool-hint" style={{ margin: "2px 2px 8px" }}>
                              {`✓ 知识图谱已就绪 · 可用「${strictLabel}」`}
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
                            ? `本笔记本尚未整理知识图谱，${strictLabel}会借用已挂载的参考库；点击为本笔记本单独整理`
                            : `本笔记本尚未整理知识图谱，也没挂参考库；「${strictLabel}」需先整理知识图谱或挂一个参考库`}
                          onClick={() => { if (currentNotebookId) startKgBuild(currentNotebookId); }}
                        >
                          <Network size={20} strokeWidth={2.7} /> {buildingKg ? "整理中…" : "整理知识图谱"}
                        </button>
                        <p className="tool-hint" style={{ margin: "2px 2px 8px" }}>
                          {currentNotebook?.base_kg_available
                            ? `本笔记本尚未整理知识图谱，${strictLabel}将借用已挂载的参考库`
                            : `本笔记本尚未整理知识图谱，也没挂参考库；「${strictLabel}」需要先整理或挂一个参考库`}
                        </p>
                      </>
                    )
                )}
                {currentNotebook?.kg_build && (
                  <div
                    className={`kg-build-status kg-build-tone-${currentKgBuildView.tone}`}
                    role="status"
                  >
                    <strong>{currentKgBuildView.label}</strong>
                    <span>{currentKgBuildView.detail}</span>
                    {canContinueKgBuild(
                      currentKgBuildView.actionLabel,
                      buildingKg,
                      isReader,
                    ) && (
                      <button
                        type="button"
                        onClick={() => {
                          if (currentNotebookId) {
                            startKgBuild(currentNotebookId);
                          }
                        }}
                      >
                        {currentKgBuildView.actionLabel}
                      </button>
                    )}
                  </div>
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
                        : (s.eligible ? "" : "内容较少，暂不需要检索索引（直接搜索已够快）")}
                      onClick={clickable ? () => runScaleIndexOp(v.primaryOp!) : undefined}
                      onKeyDown={clickable ? ((e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); runScaleIndexOp(v.primaryOp!); } }) : undefined}
                      style={{ margin: "2px 2px 8px", cursor: clickable ? "pointer" : "default", color }}
                    >
                      {label}
                      {s.exists && !s.delta_searchable && (s.unindexed_sources ?? 0) > 0 && (
                        <span title={UNINDEXED_SCOPE_HINT}>
                          {` · ${s.unindexed_sources} 源待索引`}
                        </span>
                      )}
                    </p>
                  );
                })()}
                <input
                  className="source-search"
                  type="search"
                  placeholder="搜索来源（标题/作者/文件名）"
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
                    sources.map((source) => {
                      const deletingSource = deletingSourceIds.has(source.id);
                      return (
                      <div
                        key={source.id}
                        className={`source-row compact-source-row${deletingSource ? " source-row--deleting" : ""}`}
                        title={source.title}
                        aria-busy={deletingSource || undefined}
                      >
                        <button
                          className="source-row-main"
                          disabled={deletingSource}
                          onClick={() => openSourceDetail(source).catch(reportError)}
                        >
                          <FileText className="source-file-icon" size={20} />
                          <span className="source-title-short">{compactSourceTitle(source)}</span>
                          <span className="source-row-status">
                            <span className={`source-status-dot status-${source.parse_status || source.status}`} />
                            {sourceAnomalies(source).filter((a) => a.severity !== "info").map((anomaly, i) => (
                              <AnomalyBadge key={`${anomaly.severity}-${i}`} anomaly={anomaly} />
                            ))}
                          </span>
                        </button>
                        <div className="source-row-actions">
                          {currentNotebook?.kg_ready && (
                            <span
                              className={`source-kg-badge${source.kg_extracted ? " source-kg-badge--in" : ""}`}
                              title={source.kg_extracted ? "已分析：该来源已完成知识图谱分析" : "待分析：该来源尚未加入知识图谱"}
                            >
                              {source.kg_extracted ? "已分析" : "待分析"}
                            </span>
                          )}
                          {source.source_url ? (
                            <a
                              className="source-link-button"
                              href={source.source_url}
                              target="_blank"
                              rel="noreferrer"
                              title={source.source_url}
                              aria-label="打开原始链接"
                              aria-disabled={deletingSource || undefined}
                              tabIndex={deletingSource ? -1 : undefined}
                              onClick={(e) => {
                                e.stopPropagation();
                                if (deletingSource) e.preventDefault();
                              }}
                            >
                              <ExternalLink size={13} />
                            </a>
                          ) : null}
                          {!isReader && (
                            <button
                              className="source-delete-button"
                              disabled={deletingSource}
                              title="删除来源"
                              aria-label={deletingSource ? `正在删除来源：${source.title}` : `删除来源：${source.title}`}
                              onClick={() => confirmDeleteSource(source)}
                            >
                              {deletingSource
                                ? <Loader2 size={15} className="busy-spin" aria-hidden="true" />
                                : <Trash2 size={15} />}
                            </button>
                          )}
                        </div>
                      </div>
                      );
                    })
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
                    <AskSessionHeaderActions
                      sessionCount={sessions.length}
                      sessionPanelOpen={sessionPanelOpen}
                      onToggleSessionPanel={() => setSessionPanelOpen(open => !open)}
                      onStartNewSession={startNewSession}
                    />
                  )}
                </div>
              </div>
              {chatMode === "ask" && sessionPanelOpen && (
                <div
                  id="ask-session-manager"
                  className="chat-session-popover"
                  role="dialog"
                  aria-label="会话管理"
                  ref={sessionPopoverRef}
                >
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
                                {session.used_reasoning && <span className="chat-session-reasoning-badge">{`✦ ${strictLabel}`}</span>}
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
              <div ref={chatBodyRef} className={`chat-body ${chatMode !== "ask" || turns.length > 0 || askInFlight ? "answer-mode" : ""}`}>
                {chatMode === "ask" && (turns.length === 0 && !askInFlight ? (
                  <div className="welcome">
                    <div className="wave">👋</div>
                    <h2>{welcomeCopy.title}</h2>
                    <p>{welcomeCopy.description}</p>
                    {welcomeCopy.prompts.length > 0 && (
                      <div className="prompt-chips">
                        {welcomeCopy.prompts.map(([label, prompt]) => (
                          <button key={label} onClick={() => runAsk(prompt).catch(reportError)}>{label}</button>
                        ))}
                      </div>
                    )}
                  </div>
                ) : (
                  <div className="chat-thread">
                    {turns.map((turn, index) => (
                      <div className="chat-turn" id={chatTurnDomId(index)} key={turn.response.answer_id || index}>
                        <ChatQuestion question={turn.question} askedAt={turn.askedAt} />
                        <ChatAnswer answeredAt={turn.response.answered_at}>
                          <AnswerView
                            answer={turn.response}
                            feedbackSent={feedbackSent[turn.response.answer_id] ?? ""}
                            onFeedback={(rating) => submitFeedback(turn.response.answer_id, rating, "").catch(reportError)}
                            onOpenKnowledgeGraph={(objectId, sourceNotebookId) => openKgView(
                              objectId,
                              currentNotebookId,
                              sourceNotebookId || currentNotebookId,
                            )}
                            onOpenKnowhowRow={openKnowhowAt}
                            onOpenSource={onOpenSourceElement}
                            notebookId={currentNotebookId}
                            notebookNames={notebookNames}
                            onBuildScaleIndex={() => runScaleIndexOp("build")}
                            buildingScaleIndex={buildingScaleIndex}
                            scaleIndexStatus={scaleIndexStatus}
                            onSaveMemory={(answerId) => setMemoryAnswerId(answerId)}
                            memorySaved={Boolean(memorySavedAnswers[turn.response.answer_id])}
                            onTestModel={currentUser.role === "admin" ? runSystemModelTest : undefined}
                            onOpenModelStatus={(serviceId) => { openModelPanel(serviceId); }}
                            testingModelServices={modelTestActivity.services}
                            testingAllModels={modelTestActivity.all}
                          />
                        </ChatAnswer>
                      </div>
                    ))}
                    {askInFlight && (
                      <div className="chat-turn" id={pendingQuestion ? chatTurnDomId(turns.length) : undefined}>
                        {pendingQuestion && <ChatQuestion question={pendingQuestion} askedAt={pendingAskedAt} />}
                        <div className="chat-assistant chat-thinking">
                          {/* 按引擎是否流式推轨迹判断,不按分组:深入分析组里只有
                              逐步推理会流轨迹,关联追溯挂上去只会让用户从头到尾
                              盯着一句「等待后端事件…」。 */}
                          {streamsTrace(pendingMode) ? (
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
                    proposeDisabledReason={promotionTarget.kind === "none" ? "需先挂载一个公共知识库" : undefined}
                    total={knowledgeTotal[knowledgeKind] ?? 0}
                    page={knowledgePage[knowledgeKind] ?? 0}
                    onPage={(p) => loadKnowledge(knowledgeKind, { status: knowledgeStatusFilter, page: p }).catch(reportError)}
                    onStatusFilter={(s) => {
                      setKnowledgeStatusFilter(s);
                      loadKnowledge(knowledgeKind, { status: s, page: 0 }).catch(reportError);
                    }}
                    readOnly={!capabilities.canGovernKnowledge}
                  />
                )}

                {chatMode === "reports" && currentNotebookId && (
                  <ReportsPanel
                    notebookId={currentNotebookId}
                    listReports={listReports}
                    getReport={getReport}
                    createReport={createReport}
                    confirmReportIntent={confirmReportIntent}
                    updateReportOutline={updateReportOutline}
                    generateReport={generateReport}
                    cancelReport={cancelReport}
                    deleteReport={deleteReport}
                    downloadReportsZip={downloadReportsZip}
                    setToast={setToast}
                    focusReportId={pendingReportFocusId}
                    onFocusConsumed={() => setPendingReportFocusId(null)}
                    readOnly={!capabilities.canManageReports}
                  />
                )}

                {chatMode === "memory" && currentNotebookId && (
                  <MemoryPanel scope="notebook" notebookId={currentNotebookId} bases={notebookPromotionBases} sessionSignal={memorySessionAbortRef.current.signal} />
                )}
              </div>
              {chatMode === "ask" && (
                <>
                  {/* 问题理解阶段不再另起一条灰色提示 —— 它已经是上方在途 turn
                      里轨迹的第一步,同一条轨迹从理解一路走到作答。 */}
                  {askIntentReview && (
                    <AskIntentReview
                      contract={askIntentReview.contract}
                      understandingMs={askIntentReview.understandingMs}
                      onConfirm={(confirmation) => confirmAskIntent(confirmation)}
                      onCancel={cancelAskIntentReview}
                    />
                  )}
                <AskComposer
                  value={question}
                  placeholder={askPlaceholderText}
                  onChange={setQuestion}
                  onSubmit={() => runAsk().catch(reportError)}
                  onAbort={abortAsk}
                  running={asking || intentChecking}
                  abortLabel={intentChecking ? "取消问题理解" : "中断生成"}
                  disabled={askBlocked || sessionLoading || Boolean(askIntentReview)}
                >
                  <span>{notebookSourceTotal} 个来源</span>
                  {/* 只在用户真的敲了英文双引号时才出现:确认哪几段被当成完整短语,
                      或说明为什么这次没识别。判据与后端同一份规则的镜像。 */}
                  {askQuotedPhraseHint && (
                    <span className="chat-hint">{askQuotedPhraseHint}</span>
                  )}
                  <div className="ask-mode-control" role="group" aria-label="问答模式">
                    {ASK_MODE_GROUPS.map((g) => (
                      <button
                        key={g.id}
                        type="button"
                        className={`mode-tab${groupOf(askMode) === g.id ? " active" : ""}`}
                        disabled={asking || intentChecking || sessionLoading || Boolean(askIntentReview)}
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
                            disabled={asking || intentChecking || sessionLoading || Boolean(askIntentReview)}
                            onClick={() => setAskMode(m.id)}
                          >
                            {m.label}
                          </button>
                        ))}
                      </span>
                    )}
                    {askMode === "reasoning" && (
                      <span className="ask-retrieval-effort">
                        {/* 与深度报告的「研究深度」共用 EffortPicker：同一套档名理应是同一个控件。
                            popover 只给该档一句说明，不再铺开每档的阈值数字。 */}
                        <EffortPicker
                          chipLabel="档位"
                          title="检索档位"
                          options={ASK_RETRIEVAL_EFFORT_OPTIONS}
                          value={askRetrievalEffort}
                          onChange={(id) => setAskRetrievalEffort(id as AskRetrievalEffortId)}
                          disabled={asking || intentChecking || sessionLoading || Boolean(askIntentReview)}
                          compact
                        />
                      </span>
                    )}
                    {groupOf(askMode) === "strict" && !kgAvailable && (
                      <span className="mode-hint">
                        {`该笔记本尚无知识图谱，${strictLabel}需先整理`}
                        <button
                          type="button"
                          className="mode-engine"
                          style={{ marginLeft: 6 }}
                          disabled={buildingKg || asking || sessionLoading}
                          onClick={() => { if (currentNotebookId) startKgBuild(currentNotebookId); }}
                        >
                          {buildingKg ? "整理中…" : "整理知识图谱"}
                        </button>
                      </span>
                    )}
                    {shouldShowBorrowedBaseHint({
                      strict: groupOf(askMode) === "strict",
                      kgAvailable,
                      baseKgAvailable: Boolean(currentNotebook?.base_kg_available),
                      kgReady: Boolean(currentNotebook?.kg_ready),
                      baseCount: currentNotebook?.base_notebooks?.length ?? 0,
                    }) && (
                      <span className="chat-hint">本笔记本尚无知识图谱，将借用参考库「{currentNotebook?.base_notebooks?.map((b) => b.name).join("、")}」推理</span>
                    )}
                  </div>
                </AskComposer>
                </>
              )}
              {chatMode === "ask" && (
                <ChatTurnNav
                  questions={
                    // 判据与上面渲染在途 turn 的那个 id 一致(askInFlight &&
                    // pendingQuestion):理解阶段那一轮同样在 DOM 里、同样有
                    // chatTurnDomId,只按 asking 算就会漏掉它 —— 轮次在,却没有
                    // 对应的刻度可跳。
                    askInFlight && pendingQuestion
                      ? [...turns.map((turn) => turn.question), pendingQuestion]
                      : turns.map((turn) => turn.question)
                  }
                  scrollRef={chatBodyRef}
                  sessionId={conversationId}
                />
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
              <button onClick={() => { openNotebookEditor(menuNotebook).catch(reportError); setMenuNotebookId(null); setMenuPosition(null); }}>编辑信息</button>
              <button className="danger" onClick={() => { openDeleteConfirm(menuNotebook).catch(reportError); setMenuNotebookId(null); setMenuPosition(null); }}>删除笔记本</button>
            </>
          )}
        </div>
      )}

      {createOpen && (
        <section className="utility-modal" role="dialog" aria-modal="true" onClick={(event) => { if (event.currentTarget === event.target) setCreateOpen(false); }}>
          <FloatingModalCard storageKey="notebook.create.window" className="utility-modal-card">
            {(floating) => (<>
            <div className="source-modal-header" {...floating.dragHandleProps}>
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
            </>)}
          </FloatingModalCard>
        </section>
      )}

      {shareModal && currentNotebook && (
        <section className="utility-modal" role="dialog" aria-modal="true" onClick={(event) => { if (event.currentTarget === event.target) setShareModal(null); }}>
          <FloatingModalCard storageKey="notebook.share.window" className="utility-modal-card">
            {(floating) => (<>
            <div className="source-modal-header" {...floating.dragHandleProps}>
              <div>
                <h2>分享「{currentNotebook.name}」</h2>
                <p>{shareModal.copyable
                  ? "拿到链接的登录用户可将这个笔记本整份拷贝到自己的空间。"
                  : "此笔记本较大，拿到链接的登录用户可以只读方式加入(浏览/问答/看图，不能修改)。"}</p>
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
                {shareModal.copyable ? "他人可拷贝" : "笔记本较大，他人可只读加入"}
                {` · ${shareModal.size.sources} 来源 · ${shareModal.size.nodes} 节点 · ${shareModal.size.edges} 边 · ${formatFileSize(shareModal.size.bytes)}`}
              </p>
              <div className="tag-row">
                <button className="sort-button" disabled={shareBusy} onClick={() => handleUnshare().catch(reportError)}>取消分享</button>
                <button className="new-pill" onClick={() => setShareModal(null)}>完成</button>
              </div>
            </div>
            </>)}
          </FloatingModalCard>
        </section>
      )}

      {sharedPreview && (
        <section className="utility-modal" role="dialog" aria-modal="true" onClick={(event) => { if (event.currentTarget === event.target) setSharedPreview(null); }}>
          <FloatingModalCard storageKey="notebook.sharedPreview.window" className="utility-modal-card">
            {(floating) => (<>
            <div className="source-modal-header" {...floating.dragHandleProps}>
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
                  此笔记本较大，将以只读方式加入——你可浏览来源、问答、查看知识图谱，但不能修改。
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
            </>)}
          </FloatingModalCard>
        </section>
      )}

      {sharedByMeOpen && (
        <section className="utility-modal" role="dialog" aria-modal="true" onClick={(event) => { if (event.currentTarget === event.target) setSharedByMeOpen(false); }}>
          <FloatingModalCard storageKey="notebook.sharedByMe.window" className="utility-modal-card">
            {(floating) => (<>
            <div className="source-modal-header" {...floating.dragHandleProps}>
              <div>
                <h2>已分享</h2>
                <p>你分享出去的笔记本。较小的可被他人拷贝为独立副本;较大的以只读方式共享,下方列出已加入的只读成员。</p>
              </div>
              <button className="icon-button" onClick={() => setSharedByMeOpen(false)} title="Close">×</button>
            </div>
            <div className="source-detail-body">
              {sharedByMeList === null ? (
                <p className="tool-hint">加载中…</p>
              ) : sharedByMeList.length === 0 ? (
                <article className="source-empty">
                  <div>▧</div>
                  <strong>尚未分享任何笔记本</strong>
                  <p>在某个笔记本里点「分享」即可生成链接。</p>
                </article>
              ) : (
                <div className="stack">
                  {sharedByMeList.map((item) => (
                    <div className="checklist-row" key={item.id} style={{ flexDirection: "column", alignItems: "stretch", gap: 6 }}>
                      <div className="tag-row" style={{ alignItems: "center", gap: 8 }}>
                        <span style={{ flex: 1, wordBreak: "break-word", fontWeight: 600 }}>{item.name}</span>
                        <span className="new-pill" title={item.mode === "readonly" ? "笔记本较大,只读共享" : "笔记本较小,可被拷贝"}>
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
            </>)}
          </FloatingModalCard>
        </section>
      )}

      {sourceModalOpen && (
        <section className="source-modal" role="dialog" aria-modal="true" onClick={(event) => { if (event.currentTarget === event.target) setSourceModalOpen(false); }}>
          <FloatingModalCard storageKey="source.add.window" className="source-modal-card">
            {(floating) => (<>
            <div className="source-modal-header" {...floating.dragHandleProps}>
              <div>
                <h2>添加来源</h2>
                <p>上传文件或添加链接；文件可为每个指定文档类型（默认自动检测），类型决定要分析出哪些字段。</p>
              </div>
              <button className="icon-button" onClick={() => { setStagedFiles([]); setStagedDocTypes([]); applyTouchedUpdate(stagedDocTypeTouchedRef, setStagedDocTypeTouched, []); setLinkSectionOpen(false); setSourceModalOpen(false); }} title="Close">×</button>
            </div>
            {docCapacity.show && (
              <div className={`source-doc-capacity${docCapacity.atCapacity ? " is-full" : ""}`}>
                <span className="source-doc-capacity-count">文档 {docCapacity.count} / {docCapacity.limit}</span>
                {docCapacity.atCapacity && (
                  <span className="source-doc-capacity-hint">
                    已达该笔记本的文档数量上限，如需继续添加，请先删除部分文档，或联系管理员调整上限。
                  </span>
                )}
              </div>
            )}
            <label className={`drop-zone${sourceFilePickerDisabled ? " is-disabled" : ""}`} title={sourceFilePickerHint}>
              <input type="file" multiple accept={SUPPORTED_SOURCE_ACCEPT} onChange={stageFiles} disabled={sourceFilePickerDisabled} />
              <span className="drop-plus">＋</span>
              <strong>{stagedFiles.length > 0 ? "继续添加文件" : "或拖放文件"}</strong>
              <small>{sourceUploadConfigLoading ? "正在读取上传限制…" : `支持 ${SUPPORTED_SOURCE_USER_HINT}；图片与 OCR 暂不处理。单个文件最大 ${sourceUploadSizeLabel(sourceUploadMaxBytes)}，单次最多 ${sourceUploadMaxFilesPerBatch} 个。`}</small>
            </label>
            <div className="source-action-row">
              <label className={`source-action-button${sourceFilePickerDisabled ? " is-disabled" : ""}`} title={sourceFilePickerHint}>
                <Upload size={18} strokeWidth={2.5} /> 上传文件
                <input type="file" multiple accept={SUPPORTED_SOURCE_ACCEPT} onChange={stageFiles} disabled={sourceFilePickerDisabled} />
              </label>
              <button
                type="button"
                className={`source-action-button${linkSectionOpen ? " is-active" : ""}`}
                disabled={docCapacity.atCapacity}
                title={docCapacity.atCapacity ? atDocCapacityHint : undefined}
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
                  <button className="new-pill" disabled={urlBusy || docCapacity.atCapacity} title={docCapacity.atCapacity ? atDocCapacityHint : undefined} onClick={() => submitUrlSources().catch(reportError)}>
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
                      <span className="staged-file-name" title={file.name}>{compactStagedFileName(file.name)}</span>
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
                {stagedUploadBlockedReason && (
                  <p id="staged-upload-blocked-reason" className="source-upload-blocked-hint" role="alert">
                    {stagedUploadBlockedReason}
                  </p>
                )}
                <div className="tag-row">
                  <button
                    className="new-pill"
                    disabled={uploadBusy || Boolean(stagedUploadBlockedReason)}
                    title={stagedUploadBlockedReason ?? undefined}
                    aria-describedby={stagedUploadBlockedReason ? "staged-upload-blocked-reason" : undefined}
                    onClick={() => confirmUpload().catch(reportError)}
                  >{uploadBusy ? "上传中…" : `上传 ${stagedFiles.length} 个文件`}</button>
                  <button className="sort-button" disabled={uploadBusy} onClick={() => { setStagedFiles([]); setStagedDocTypes([]); applyTouchedUpdate(stagedDocTypeTouchedRef, setStagedDocTypeTouched, []); }}>清空</button>
                </div>
              </div>
            )}
            </>)}
          </FloatingModalCard>
        </section>
      )}

      {memoryAnswerId && currentNotebookId && (
        <MemorySaveDialog
          answerId={memoryAnswerId}
          notebookId={currentNotebookId}
          sessionSignal={memorySessionAbortRef.current.signal}
          onClose={() => setMemoryAnswerId(null)}
          onSaved={(memory) => handleMemorySaved(memory).catch(reportError)}
        />
      )}

      {editingNotebook && (
        <section className="utility-modal" role="dialog" aria-modal="true">
          <FloatingModalCard storageKey="notebook.edit.window" className="utility-modal-card notebook-edit-card">
            {(floating) => (<>
            <div className="source-modal-header" {...floating.dragHandleProps}>
              <div>
                <h2>笔记本设置</h2>
                <p>编辑当前笔记本的信息与参考库。模型服务由系统统一管理。</p>
              </div>
              <button className="icon-button" onClick={() => setEditingNotebook(null)} title="Close">×</button>
            </div>
            <form className="edit-form notebook-settings-form" onSubmit={(event) => saveNotebookEdit(event).catch(reportError)}>
              <section className="settings-section">
                <div className="settings-section-head"><h3>基本信息</h3></div>
                <label>标题<input name="name" defaultValue={editingNotebook.name} maxLength={80} required /></label>
                <label>描述<textarea name="purpose" defaultValue={editingNotebook.purpose} rows={3} maxLength={260} /></label>
                <label>领域关键词<input name="primary_domain" defaultValue={editingNotebook.primary_domain} maxLength={80} /></label>
              </section>
              <section className="settings-section">
                <div className="settings-section-head">
                  <h3>参考库</h3>
                  <p>检索时会一并搜索这些知识库。不选则只搜本笔记本。</p>
                </div>
                <div className="base-picker">
                {(() => {
                  // 最终整支审查 BLOCKER 1:必须渲染 mountable ∪ mountEdges 的并集,而不是
                  // 只渲染 mountable——失效边(active=false)按 MOUNT_VALID_EXPR 定义永远不在
                  // mountable 候选里,只渲染 mountable 会让那一行永久消失,用户也没法取消勾选
                  // 它、保存表单会被后端 400 拒绝(见 notebook-bases.ts mergeMountCandidates
                  // 与 routes.py set_notebook_bases_route 的联动说明)。
                  const groups = groupMountable(mergeMountCandidates(mountable, mountEdges));
                  const render = (title: string, list: MountedBase[], variant: "public" | "mine") =>
                    list.length === 0 ? null : (
                      <div className={`base-picker-group base-picker-group--${variant}`} key={title}>
                        <span className="base-picker-group-title">{title}</span>
                        {list.map((n) => {
                          const dead = !n.active;
                          return (
                            <label className={`base-picker-row${dead ? " base-picker-row-dead" : ""}`} key={n.id}>
                              <input
                                type="checkbox"
                                checked={mountedIds.includes(n.id)}
                                onChange={(e) =>
                                  setMountedIds((prev) =>
                                    e.target.checked
                                      ? [...prev, n.id]
                                      : prev.filter((id) => id !== n.id)
                                  )
                                }
                              />
                              {/* 审查 M3:失效原因文案挪到库名正下方(而不是网格最后一列被推到行最右)——
                                  同一个 wrapper 里纵向堆叠,让原因读起来明显是在注解上面那个库名。 */}
                              <span className="base-picker-name-block">
                                <span className="base-picker-name" title={n.name}>{n.name}</span>
                                {dead && <span className="base-picker-dead-note">{n.inactive_reason}</span>}
                              </span>
                            </label>
                          );
                        })}
                      </div>
                    );
                  return (
                    <>
                      {render("公共知识库", groups.public, "public")}
                      {render("我的笔记本", groups.mine, "mine")}
                      {groups.public.length === 0 && groups.mine.length === 0 && (
                        <p className="base-picker-empty">暂无可挂载的知识库。</p>
                      )}
                    </>
                  );
                })()}
                {mountCostHint(mountedIds.length) && (
                  <p className="base-picker-hint">{mountCostHint(mountedIds.length)}</p>
                )}
                </div>
              </section>
              <section className="settings-section">
                <div className="settings-section-head"><h3>更多信息</h3></div>
                <div className="settings-grid-2">
                  <label>目标用户<input name="target_users" defaultValue={editingNotebook.target_users ?? ""} maxLength={120} /></label>
                  <label>访问范围<input name="access_scope" defaultValue={editingNotebook.access_scope ?? ""} maxLength={80} /></label>
                </div>
                <label>预期问题（每行/逗号一条）<textarea name="expected_questions" defaultValue={(editingNotebook.expected_questions ?? []).join("\n")} rows={2} /></label>
                <label>来源类型（每行/逗号一条）<input name="source_types" defaultValue={(editingNotebook.source_types ?? []).join(", ")} /></label>
                <label>分类（每行/逗号一条）<input name="taxonomy" defaultValue={(editingNotebook.taxonomy ?? []).join(", ")} /></label>
              </section>
              <div className="modal-actions settings-footer">
                <button type="button" className="sort-button" onClick={() => setEditingNotebook(null)}>取消</button>
                <button type="submit" className="new-pill">保存</button>
              </div>
            </form>
            </>)}
          </FloatingModalCard>
        </section>
      )}

      {deleteNotebook && (
        <section className="utility-modal" role="dialog" aria-modal="true">
          <FloatingModalCard storageKey="notebook.delete.window" className="utility-modal-card narrow">
            {(floating) => (<>
            <div className="source-modal-header" {...floating.dragHandleProps}>
              <div>
                <h2>删除笔记本</h2>
                <p>确定删除 “{deleteNotebook.name}” 吗？这个本机 beta 会同时移除它的来源和深度报告；{NOTEBOOK_PRIVATE_MEMORY_DELETE_WARNING}</p>
                {deleteMountedByCount > 0 && (
                  <p className="delete-mount-warning">
                    {deleteMountedByCount} 个笔记本正在把它作为参考库，删除后这些笔记本会立即失去这条参考库——此操作不可撤销。
                  </p>
                )}
              </div>
              <button className="icon-button" onClick={() => setDeleteNotebook(null)} title="Close">×</button>
            </div>
            <div className="modal-actions padded">
              <button className="sort-button" onClick={() => setDeleteNotebook(null)}>取消</button>
              <button className="new-pill danger-pill" onClick={() => confirmDeleteNotebook().catch(reportError)}>确认</button>
            </div>
            </>)}
          </FloatingModalCard>
        </section>
      )}

      {infoModal && (
        <section className="utility-modal utility-modal-top" role="dialog" aria-modal="true">
          <FloatingModalCard storageKey="info.window" className="utility-modal-card narrow">
            {(floating) => (<>
            <div className="source-modal-header" {...floating.dragHandleProps}>
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
            </>)}
          </FloatingModalCard>
        </section>
      )}

      {sourceDetail && (
        <SourceDetailWindow onClose={() => { setSourceDetail(null); setHighlightedElementId(""); }}>
          <div className="source-detail-title-row">
                <h1 title={sourceDetail.title}>{sourceDetail.title}</h1>
                {sourceDetailBaseId ? (
                  <span
                    className="tier-badge tier-base source-detail-base-badge"
                    title={sourceDetailBaseLabel}
                  >
                    {sourceDetailBaseLabel}
                  </span>
                ) : (
                <div className="source-detail-actions">
                  <button
                    className="icon-button subtle-icon"
                    disabled={reparsingSource || sourceDetailDeleting}
                    onClick={() => reparseSource().catch(reportError)}
                    title={sourceDetailDeleting ? "来源删除中，暂时无法重新解析" : reparsingSource ? "重新解析中…" : "重新解析"}
                    aria-label={sourceDetailDeleting ? "来源删除中，暂时无法重新解析" : reparsingSource ? "重新解析中…" : "重新解析"}
                  >
                    {reparsingSource
                      ? <Loader2 size={23} className="busy-spin" aria-hidden="true" />
                      : <ExternalLink size={23} />}
                  </button>
                  <button
                    className="icon-button subtle-icon danger-icon"
                    disabled={reparsingSource || sourceDetailDeleting}
                    onClick={() => confirmDeleteSource(sourceDetail)}
                    title={sourceDetailDeleting ? "来源删除中…" : "删除来源"}
                    aria-label={sourceDetailDeleting ? `正在删除来源：${sourceDetail.title}` : `删除来源：${sourceDetail.title}`}
                  >
                    {sourceDetailDeleting
                      ? <Loader2 size={20} className="busy-spin" aria-hidden="true" />
                      : <Trash2 size={20} />}
                  </button>
                </div>
                )}
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
                <span className="tag">{label(PARSE_STATUS, sourceDetail.parse_status || sourceDetail.status, "处理中")}</span>
                <span className="tag">{formatFileSize(sourceDetail.file_size)}</span>
                <span className="tag">{sourceElements.length} 个元素</span>
              </div>
              {sourceDetail.paper_meta_status === "has_meta" && sourceDetail.paper_meta && (
                <div className="source-detail-paper">
                  {sourceDetail.paper_meta.title && (
                    <p className="paper-title">{sourceDetail.paper_meta.title}</p>
                  )}
                  {sourceDetail.paper_meta.authors.length > 0 && (
                    <p className="paper-authors">
                      {sourceDetail.paper_meta.authors.map((a) => (
                        <span key={a.name} className="paper-author"
                              title={a.affiliation || undefined}>
                          {a.name}
                        </span>
                      ))}
                    </p>
                  )}
                  {(sourceDetail.paper_meta.venue || sourceDetail.paper_meta.year) && (
                    <p className="paper-venue">
                      {[sourceDetail.paper_meta.venue, sourceDetail.paper_meta.year]
                        .filter(Boolean).join(" · ")}
                    </p>
                  )}
                  {sourceDetail.paper_meta.doi && (
                    <a className="paper-doi" target="_blank" rel="noreferrer"
                       href={`https://doi.org/${sourceDetail.paper_meta.doi.split("/").map(encodeURIComponent).join("/")}`}>
                      DOI: {sourceDetail.paper_meta.doi}
                    </a>
                  )}
                  {sourceDetail.paper_meta.keywords.length > 0 && (
                    <p className="paper-keywords">
                      {sourceDetail.paper_meta.keywords.map((k) => (
                        <span key={k} className="tag">{k}</span>
                      ))}
                    </p>
                  )}
                </div>
              )}
              {sourceDetailAnomalies.length > 0 && (
                <div className="source-detail-anomalies">
                  {sourceDetailAnomalies.map((anomaly, i) => (
                    <AnomalyBadge key={`${anomaly.severity}-${i}`} anomaly={anomaly} block />
                  ))}
                </div>
              )}
              <div className="source-element-stack">
                {sourceElements.length > 0 ? sourceElements.map((element) => (
                  <article
                    className={`item source-element-card${element.id === highlightedElementId ? " source-element-card--highlighted" : ""}`}
                    key={element.id}
                    id={sourceElementDomId(element.id)}
                  >
                    <div className="element-head">
                      <h3>{element.location_label}</h3>
                      <span className="tag element-type-tag">{label(ELEMENT_TYPE, element.element_type, "内容")}</span>
                    </div>
                    <ElementBody element={element} notebookId={currentNotebookId ?? ""} />
                  </article>
                )) : (
                  <article className="item">
                    <h3>等待解析</h3>
                    {/* 只按「有没有失败」二选一给稳定文案,绝不直出后端的原始异常串。
                        代理读取端点(openSourceById 的主路径)刻意只回 parse_failed 布尔
                        ——原文可能带服务端绝对路径,跨库读取不该拿到;老的 /sources/{id}
                        仍回 error_message,兜底路径与轮询路径靠它。原文在来源轮询那条
                        路径上已经过 toUserMessage 落进 console(见 justFailed);渲染期
                        不调 toUserMessage:它会随重渲染反复刷日志。 */}
                    <p>{(sourceDetail.parse_failed ?? Boolean(sourceDetail.error_message))
                      ? "这个来源没能解析成功，可以删除后重新上传。"
                      : "当前来源还没有解析出元素。"}</p>
                  </article>
                )}
          </div>
        </SourceDetailWindow>
      )}

      {analytics && (
        <section className="utility-modal" role="dialog" aria-modal="true" onClick={(event) => { if (event.currentTarget === event.target) closeAnalytics(); }}>
          <FloatingModalCard storageKey="analytics.window" className="utility-modal-card">
            {(floating) => (<>
            <div className="source-modal-header" {...floating.dragHandleProps}>
              <div>
                <h2>知识分析看板</h2>
                <p>回答质量、审核进度、知识覆盖、来源状态与索引构建状态的本机统计。</p>
              </div>
              <button className="icon-button" onClick={closeAnalytics} title="Close">×</button>
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
                {Object.entries(analytics.source_status_counts).map(([k, v]) => <span className="tag" key={k}>{label(PARSE_STATUS, k, "其他")} {v}</span>)}
              </div>
              {/* 体检:无异常时不打扰(上方中性 tag 行照旧);命中 H2–H6 源级问题时,列出
                  问题(界面词标签 + 数量 + 命中样本标题)+ 对应一键修复 CTA。*/}
              {(() => {
                const groups = sourceHealthGroups(checkup);
                if (groups.length === 0) return null;
                const titleOf = (id: string) => {
                  const s = sources.find((x) => x.id === id);
                  return s ? (s.title || s.file_name || "") : "";
                };
                return (
                  <div className="stack" style={{ marginTop: 8 }}>
                    <p className="tool-hint">部分来源需要处理：</p>
                    {groups.map((g) => {
                      const titles = g.sample.map(titleOf).filter(Boolean).slice(0, 6);
                      // 「这一组的修复还在后台跑」——判定规则在 checkup-view.ts::isRepairing。
                      //
                      // extract_kg 是例外:它**只**看 buildingKg,不进 repairingFix(见下方
                      // runFix 里的理由)。isRepairing 对它恒为 false(从没写过那个键),
                      // 忙碌态完全由 buildingKg 表达——那个位失败时会被 startKgBuild 清掉。
                      const repairing = isRepairing(repairingFix[g.key], g.count)
                        || (g.fix === "extract_kg" && buildingKg);
                      const runFix = async () => {
                        const nb = currentNotebookId;
                        if (!nb || repairing) return;
                        // extract_kg **只**认 buildingKg,不另记 repairingFix(codex 第 1 轮 P2)。
                        // startKgBuild 是 fire-and-forget:同步置 buildingKg,POST 失败时自己在
                        // 异步回调里清掉,但**不回传受理结果**。若这里也记一份 repairingFix,
                        // 那份记录在建图请求被拒时没人清得掉(count 没变、buildingKg 已归位),
                        // 按钮会一直锁到轮询窗口结束。少记一份 = 少一个解不开的锁。
                        if (g.fix === "extract_kg") {
                          startKgBuild(nb);
                          bumpCheckupRepairPoll();
                          return;
                        }
                        // 先乐观置忙碌位(POST 在飞的这段也不能再点),未受理/报错再撤销。
                        setRepairingFix((previous) => ({ ...previous, [g.key]: repairRelease(g.fix, g.count) }));
                        let started = false;
                        try {
                          if (g.fix === "reparse") started = await runReparse(nb, g.sample);
                          else if (g.fix === "backfill_vectors") started = await runBackfillVectors(nb);
                        } finally {
                          if (!started) {
                            setRepairingFix((previous) => {
                              const next = { ...previous };
                              delete next[g.key];
                              return next;
                            });
                          }
                        }
                      };
                      return (
                        <div className="index-card index-tone-warn" key={g.key}>
                          <span className="index-ic" aria-hidden="true"><AlertTriangle size={19} /></span>
                          <div className="index-main">
                            <div className="tag-row" style={{ alignItems: "center" }}>
                              <span className="index-state">{g.label}</span>
                              <span className="tag" style={{ color: "var(--color-warn, #b97a00)" }}>{g.count} {g.unit}</span>
                            </div>
                            {titles.length > 0 && (
                              <div className="index-sub">{titles.join("、")}{g.count > titles.length ? " 等" : ""}</div>
                            )}
                          </div>
                          {!isReader && (
                            <div className="index-ctas">
                              <button
                                type="button"
                                className="index-cta primary"
                                disabled={repairing}
                                onClick={() => { void runFix(); }}
                              >
                                {repairing
                                  ? label(CHECKUP_FIX_BUSY, g.fix, "处理中…")
                                  : label(CHECKUP_FIX, g.fix, "处理")}
                              </button>
                            </div>
                          )}
                        </div>
                      );
                    })}
                  </div>
                );
              })()}
              {analytics.paper_meta_counts && (
                <>
                  <p className="section-title">论文元数据</p>
                  <div className="tag-row">
                    {analytics.paper_meta_counts.has_meta + analytics.paper_meta_counts.missing + analytics.paper_meta_counts.marker > 0 ? (
                      <>
                        <span className="tag" style={{ color: "var(--color-ok, #1a7f5a)" }}>有元数据 {analytics.paper_meta_counts.has_meta}</span>
                        {analytics.paper_meta_counts.missing > 0 && (
                          <span className="tag" style={{ color: "var(--color-warn, #b97a00)" }}>缺失 {analytics.paper_meta_counts.missing}</span>
                        )}
                        {analytics.paper_meta_counts.marker > 0 && (
                          <span className="tag" style={{ opacity: 0.6 }}>非论文 {analytics.paper_meta_counts.marker}</span>
                        )}
                      </>
                    ) : (
                      <span className="tool-hint">暂无论文来源</span>
                    )}
                  </div>
                </>
              )}
              <ContentOverviewCards
                overview={contentOverview}
                loading={contentOverviewLoading}
                error={contentOverviewError}
                readOnly={isReader}
                onOpenMemory={openAnalyticsMemory}
                onOpenKnowhow={openAnalyticsKnowhow}
              />
              <p className="section-title">索引与构建</p>
              {indexStatus ? (
                <div className="stack">
                  {/* 知识图谱行:状态取 indexStatus.kg,动作复用既有 startKgBuild/startKgRebuild/relinkFromKgView。 */}
                  {(() => {
                    const kg = indexStatus.kg;
                    const view = kgBuildPresentation(
                      kg.job,
                      kg.pending_sources,
                      kg.ready,
                    );
                    const busy = kg.job?.status === "running"
                      || kg.building
                      || buildingKg;
                    const tone = view.tone === "success"
                      ? "ok"
                      : view.tone === "neutral"
                        ? "muted"
                        : view.tone === "error"
                          ? "error"
                          : "warn";
                    const color = tone === "ok"
                      ? "var(--color-ok, #1a7f5a)"
                      : tone === "error"
                        ? "var(--color-danger, #b42318)"
                        : tone === "muted"
                          ? "var(--muted)"
                          : "var(--color-warn, #b97a00)";
                    return (
                      <div className={`index-card index-tone-${tone}`}>
                        <span className="index-ic" aria-hidden="true"><Network size={19} /></span>
                        <div className="index-main">
                          <div className="tag-row" style={{ alignItems: "center" }}>
                            <span className="index-state">知识图谱</span>
                            <span className="tag" style={{ color }}>{view.label}</span>
                          </div>
                          <div className="index-sub">{view.detail}</div>
                        </div>
                        {!busy && !isReader && (
                          <div className="index-ctas">
                            {(!kg.ready || kg.pending_sources > 0) && (
                              <button
                                type="button"
                                className="index-cta primary"
                                onClick={() => { if (currentNotebookId) startKgBuild(currentNotebookId); }}
                              >
                                {view.actionLabel
                                  ?? (kg.ready
                                    ? `分析新增 ${kg.pending_sources} 篇`
                                    : "整理")}
                              </button>
                            )}
                            {kg.ready && (
                              <button
                                type="button"
                                className="index-cta"
                                onClick={() => { if (currentNotebookId) startKgRebuild(currentNotebookId); }}
                              >
                                全部重新分析
                              </button>
                            )}
                            {kg.ready && kg.pending_sources === 0 && (
                              <>
                                {/* 补连是**同步等完**的(relinkFromKgView 里 await),而上面那个 busy
                                    只看 KG 构建、不含 relinkingKg——所以这里必须自己带忙碌位。
                                    与知识图谱视图侧栏的同一动作(disabled + 「补连中…」)保持一致。 */}
                                <button
                                  type="button"
                                  className="index-cta"
                                  disabled={relinkingKg}
                                  onClick={relinkFromKgView}
                                >
                                  {relinkingKg ? "补连中…" : "补上关联"}
                                </button>
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
                          <div className="index-sub">跨文档概念聚类；重算并刷新图谱索引（不重新分析来源）</div>
                        </div>
                        {!busy && !isReader && (
                          <div className="index-ctas">
                            <button type="button" className={`index-cta${uk.dirty ? " primary" : ""}`} onClick={confirmRefreshUnifiedKg}>重新合并</button>
                          </div>
                        )}
                      </div>
                    );
                  })()}
                  {/* 检索索引行:状态取 indexStatus.scale_index,忙碌(排队/构建中)时唯一动作变为「取消」。
                      体检 H8(索引产物损坏)优先:这是「索引与构建」里唯一会静默出错的一格——索引存在
                      且不过期、产物却坏了,普通状态卡会照常显示绿色「最新」。命中即整格换成红色「已损坏」
                      告警 +「重建索引」(复用既有全量重建 runScaleIndexOp("rebuild")),避免与「最新」并列
                      自相矛盾。H7 索引过期不另设:下方正常态本就按 scale_index.state 渲染「已过期 → 更新
                      索引」(与 H7 同源),重复一遍反成冗余控件。*/}
                  {checkupCount(checkup, "H8") > 0 ? (() => {
                    // H8 这一格不像下面的正常态那样「忙碌时整排 CTA 换成取消」——它常驻显示
                    // 告警,所以按钮得自己带忙碌位,否则点完重建后按钮外观毫无变化,用户会一直
                    // 点(runScaleIndexOp 内部虽有早退守卫拦住重复提交,但界面上看不出来)。
                    const rebuilding = buildingScaleIndex
                      || indexStatus.scale_index.building
                      || indexStatus.scale_index.state === "queued";
                    return (
                    <div className="index-card index-tone-error">
                      <span className="index-ic" aria-hidden="true"><AlertTriangle size={19} /></span>
                      <div className="index-main">
                        <div className="tag-row" style={{ alignItems: "center" }}>
                          <span className="index-state">检索索引</span>
                          <span className="tag" style={{ color: "var(--color-danger, #b42318)" }}>已损坏</span>
                        </div>
                        <div className="index-sub">检索索引已损坏，检索与{strictLabel}结果可能不完整，请重建索引。</div>
                      </div>
                      {!isReader && (
                        <div className="index-ctas">
                          <button type="button" className="index-cta primary"
                                  disabled={rebuilding}
                                  onClick={() => runScaleIndexOp("rebuild", bumpCheckupRepairPoll)}>
                            {rebuilding ? "重建中…" : "重建索引"}
                          </button>
                        </div>
                      )}
                    </div>
                    );
                  })() : (() => {
                    const s = indexStatus.scale_index;
                    const v = describeScaleIndex(s);
                    // 本地忙碌位兜底(与知识图谱/概念合并两行一致):server-derived state 靠聚合轮询
                    // 6s 才刷新一次,点击构建/更新/全量重建的瞬间先靠本地 buildingScaleIndex 立即
                    // 切到「取消」态,避免残留可点的旧按钮。
                    const busy = buildingScaleIndex || v.state === "building" || v.state === "queued";
                    const queuedImmediateOp = v.state === "queued"
                      ? queuedScaleIndexImmediateOp(s)
                      : null;
                    const tsSuffix = s.last_built_at ? ` · 上次 ${formatRelativeTime(s.last_built_at)}` : "";
                    const color = v.tone === "warn" ? "var(--color-warn, #b97a00)" : v.tone === "ok" ? "var(--color-ok, #1a7f5a)" : undefined;
                    const desc = v.tone === "muted" ? "内容较少，直接搜索已够快，无需索引"
                      : v.state === "building" ? "后台进行，完成后自动更新"
                      : v.state === "queued" ? "已排队，将在服务器空闲时构建；完成后自动更新"
                      : v.state === "stale" ? "新增内容未纳入索引，暂不参与检索与推理"
                      : v.state === "indexed" ? "已建成且为最新"
                      : `为本笔记本的内容建立快速查找结构，加速语义检索与${strictLabel}`;
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
                              <span className="tag">{s.n_chunks} 段</span>
                              {s.has_chunk_ann && <span className="tag">段落索引 ✓</span>}
                            </div>
                          )}
                        </div>
                        {!isReader && (
                          <div className="index-ctas">
                            {v.state === "queued" && !s.building ? (
                              <>
                                <button
                                  type="button"
                                  className="index-cta primary"
                                  onClick={() => runScaleIndexOp(queuedImmediateOp!)}
                                >
                                  {queuedImmediateOp === "build"
                                    ? "立即构建"
                                    : queuedImmediateOp === "update"
                                      ? "立即更新"
                                      : "立即重建"}
                                </button>
                                <button type="button" className="index-cta" disabled={cancelingScaleIndex} onClick={handleCancelScaleIndex}>
                                  {cancelingScaleIndex ? "取消中…" : "取消"}
                                </button>
                              </>
                            ) : busy ? (
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
            </>)}
          </FloatingModalCard>
        </section>
      )}

      {schemaModalOpen && capabilities.canManageSchemas && (
        <section className="utility-modal" role="dialog" aria-modal="true" onClick={(event) => { if (event.currentTarget === event.target) setSchemaModalOpen(false); }}>
          <FloatingModalCard storageKey="schema.window" className="utility-modal-card">
            {(floating) => (<>
            <div className="source-modal-header" {...floating.dragHandleProps}>
              <div>
                <h2>图谱 Schema</h2>
                <p>管理要从来源中分析出的知识对象类型与字段。内置类型可改字段/标签/停用；可新增自定义类型；也可从当前笔记本内容归纳候选类型（建议态，需人工批准）。</p>
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
            </>)}
          </FloatingModalCard>
        </section>
      )}

      {graphOpen && (
        <section className="utility-modal" role="dialog" aria-modal="true" onClick={(event) => { if (event.currentTarget === event.target) setGraphOpen(false); }}>
          <FloatingModalCard storageKey="graph.window" className="utility-modal-card">
            {(floating) => (<>
            <div className="source-modal-header" {...floating.dragHandleProps}>
              <div>
                <h2>知识关系图</h2>
                <p>由各知识对象的关系字段（related_concepts / claims / formulas / procedures）解析出的关联，供蕴含分析与冲突检测使用。</p>
              </div>
              <button className="icon-button" onClick={() => setGraphOpen(false)} title="Close">×</button>
            </div>
            <div className="source-detail-body">
              {graph === null ? (
                <p className="tool-hint">加载中…</p>
              ) : graph.edges.length === 0 ? (
                <p className="tool-hint">暂无关联。当分析/审核的对象在 related_* 字段引用了同一笔记本的其它对象时，这里会出现连线。</p>
              ) : (
                <div className="stack">
                  <div className="tag-row"><span className="tag">节点 {graph.nodes.length}</span><span className="tag">边 {graph.edges.length}</span></div>
                  {graph.edges.map((edge, index) => {
                    const from = graph.nodes.find((n) => n.id === edge.from_id);
                    const to = graph.nodes.find((n) => n.id === edge.to_id);
                    return (
                      <div className="checklist-row" key={`edge-${index}`}>
                        <strong><LatexText text={from?.headline ?? edge.from_id} isFormula={from?.object_type === "formula"} /></strong>
                        <span className="tag">{relationLabel(edge.relation)}</span>
                        → <strong><LatexText text={to?.headline ?? edge.to_id} isFormula={to?.object_type === "formula"} /></strong>
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
            </>)}
          </FloatingModalCard>
        </section>
      )}

      {kgViewOpen && (
        <section className="kg-view" role="dialog" aria-modal="true">
          <div className="kg-view-header">
            <div><h2>知识图谱</h2><p>Object 级知识图谱：Concept / Claim / Formula / Procedure 同屏展示。节点名称、类型形状和边标签直接画在主视图中。</p></div>
            <div className="kg-view-header-actions">
              {/* 「图谱分析」= 只读诊断报告(对象构成 / 合并收敛 / 主题板块 / 板块俯瞰图 /
                  关联稀疏的来源)。后端两个端点走 require_notebook_read,只读成员也能看,
                  所以这里不做 admin 门控;面板本身不含任何写动作。 */}
              <button
                type="button"
                className="sort-button kg-schema-button"
                onClick={() => setKgAnalysisOpen(true)}
                title="查看这个知识库的构成、合并收敛与主题板块分布"
              >
                <BarChart3 size={16} /> 图谱分析
              </button>
              {/* 「图谱 Schema」= 原顶层导航「内容类型」,已并入本视图(仍 admin 门控):
                  定义要从来源中分析出的知识对象类型与字段。打开的是既有 SchemaManager
                  弹窗(utility-modal z:60 > kg-view z:50,正确叠在本视图之上)。 */}
              {capabilities.canManageSchemas && (
                <button
                  type="button"
                  className="sort-button kg-schema-button"
                  onClick={openSchemas}
                  title="定义要从来源中分析出的知识对象类型与字段"
                >
                  <Database size={16} /> 图谱 Schema
                </button>
              )}
              <button className="icon-button" onClick={() => closeKgView()} title="Close">×</button>
            </div>
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
                    title="为没建立关联的内容补上关联（快速、确定性，不覆盖现有图）"
                    onClick={relinkFromKgView}
                  >
                    {relinkingKg ? "补连中…" : "补上关联"}
                  </button>
                  <button
                    type="button"
                    className="sort-button"
                    disabled={kgRefreshBusy || buildingKg}
                    title="对现有概念重新聚类 / 跨文档合并并刷新（不重新分析来源，会先确认）"
                    onClick={confirmRefreshUnifiedKg}
                  >
                    {kgRefreshBusy ? "合并中…" : "重新合并"}
                  </button>
                  <button
                    type="button"
                    className="sort-button kg-action-danger"
                    disabled={buildingKg}
                    title="清空现有知识图谱并重新分析全部来源（后台任务，可能数分钟）"
                    onClick={() => { if (currentNotebookId) startKgRebuild(currentNotebookId); }}
                  >
                    {buildingKg ? "分析中…" : "全部重新分析"}
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
                            : (s.eligible ? "" : "内容较少，暂不需要检索索引（直接搜索已够快）")}
                          onClick={clickable ? () => runScaleIndexOp(v.primaryOp!) : undefined}
                          onKeyDown={clickable ? ((e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); runScaleIndexOp(v.primaryOp!); } }) : undefined}
                          style={{ cursor: clickable ? "pointer" : "default", color }}
                        >
                          {label}
                          {s.exists && !s.delta_searchable && (s.unindexed_sources ?? 0) > 0 && (
                            <span title={UNINDEXED_SCOPE_HINT}>
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
                  {kgReviewBusy ? "判重中…" : "自动判重"}
                </button>
                <button
                  className="ghost-button"
                  onClick={reviewAllMerges}
                  disabled={!pendingMerges.length || reviewAllStarting || reviewAllJob?.status === "running"}
                >
                  {reviewAllJob?.status === "running"
                    ? `全部判重中… ${reviewAllJob.done}/${reviewAllJob.total}`
                    : reviewAllStarting
                      ? "全部判重中…"
                      : "全部自动判重"}
                </button>
                {pendingMerges.length === 0 ? <p className="tool-hint">无</p> : pendingMerges.map((m) => (
                  <div className="kg-merge-row" key={m.id}>
                    <span>{m.canonical_a.replace(/^K-/, "")} ↔ {m.canonical_b.replace(/^K-/, "")} <em>({m.score.toFixed(2)})</em></span>
                    <span className="kg-merge-actions">
                      {/* 落决定会连带跑一次全量概念合并重建 + 重拉整张图,是这一列里最贵的
                          一步。任一行在处理中就把整列锁住,只有被点的那颗改文案。 */}
                      <button disabled={decidingMerge !== null} onClick={() => decideMerge(m, true)}>
                        {decidingMerge?.id === m.id && decidingMerge.confirm ? "合并中…" : "合并"}
                      </button>
                      <button disabled={decidingMerge !== null} onClick={() => decideMerge(m, false)}>
                        {decidingMerge?.id === m.id && !decidingMerge.confirm ? "分开中…" : "拒绝"}
                      </button>
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
                    const base = relationLabel(link.label);
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
                        <p key={key}><strong>{fieldLabel(key)}：</strong>{kgPayloadValue(value)}</p>
                      ))}
                    {selectedKgEdges.length > 0 && (
                      <>
                        <h4>相邻关系</h4>
                        {selectedKgEdges.slice(0, 24).map((edge, index) => (
                          <div className="kg-relation-row" key={`${edge.source_object_id}-${edge.target_object_id}-${index}`}>
                            <span className="kg-relation-node"><KgTypeMark type={edge.sourceType} /><span>{truncateKgLabel(edge.sourceName, 28)}</span></span>
                            {edge.source_count && edge.source_count >= 2 ? (
                              <span className="kg-relation-mid">
                                <strong>{relationLabel(edge.edge_type)}</strong>
                                <span className="tag">×{edge.source_count}源</span>
                              </span>
                            ) : (
                              <strong>{relationLabel(edge.edge_type)}</strong>
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
                                  {node.edge_type ? <em>{relationLabel(node.edge_type)}</em> : null}
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
          {kgAnalysisOpen && currentNotebookId && (
            <KgAnalysisView notebookId={currentNotebookId} onClose={() => setKgAnalysisOpen(false)} />
          )}
        </section>
      )}

      {knowhowNavigation.isOpen && currentNotebookId && (
        <KnowhowPanel
          notebookId={currentNotebookId}
          apiBase={API_BASE}
          canEdit={!isReader}
          onClose={closeKnowhow}
          initialTableId={knowhowNavigation.jumpTarget?.tableId}
          initialRowId={knowhowNavigation.jumpTarget?.rowId}
          initialHealthFilter={knowhowNavigation.healthFilter}
        />
      )}

      {promoOpen && (
        <section
          className="utility-modal"
          role="dialog"
          aria-modal="true"
          onClick={(event) => { if (event.currentTarget === event.target) setPromoOpen(false); }}
        >
          <FloatingModalCard storageKey="promotion.window" className="utility-modal-card">
            {(floating) => (<>
            <div className="source-modal-header" {...floating.dragHandleProps}>
              <div>
                <h2>内容审核</h2>
                <p>个人知识库中的内容与记忆候选申请收录到公共知识库。批准后会合并重复并加入所选的目标公共知识库。</p>
              </div>
              <button className="icon-button" onClick={() => setPromoOpen(false)} title="Close">×</button>
            </div>
            <div className="source-detail-body">
              {(promoQueue ?? []).length === 0 ? (
                <p className="tool-hint">暂无待审核的收录申请。</p>
              ) : (
                <div className="stack">
                  {(promoQueue ?? []).map((cand) => {
                    const review = promotionReviewSections(cand);
                    return (
                    <article className="item" key={cand.id}>
                      <div className="tag-row">
                        <span className="tag">{label(PROMOTION_STATUS, cand.status, "处理中")}</span>
                        <span className="tag">{cand.object_type}</span>
                        {cand.source_kind === "memory" && <span className="tag">记忆提取候选</span>}
                        {cand.source_kind === "memory" && review.sourceRevision > 0 && (
                          <span className="tag">固定修订 #{review.sourceRevision}</span>
                        )}
                        {cand.base_match_id && (
                          <span className="tag conflict">疑似重复: {cand.base_match_id.slice(0, 10)}</span>
                        )}
                      </div>
                      <h3>{String((cand.payload as Record<string, unknown>).name ?? (cand.payload as Record<string, unknown>).title ?? cand.object_id)}</h3>
                      {cand.source_kind === "memory" && review.candidates.length > 0 && (
                        <div className="stack" aria-label="记忆待审知识对象">
                          {review.candidates.map((item, index) => (
                            <section className="item" key={`${cand.id}-${item.objectType}-${index}`}>
                              <strong>{item.objectType}</strong>
                              {item.fields.map(([label, value]) => (
                                <div key={label}>
                                  <span className="tool-hint">{label}</span>
                                  <div style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{value}</div>
                                </div>
                              ))}
                            </section>
                          ))}
                        </div>
                      )}
                      <p className="tool-hint">来源笔记本: {cand.notebook_id.slice(0, 10)}</p>
                      {cand.target_base_id && (
                        <p className="tool-hint">
                          {/* Task 13 审查 #4:优先用后端 join 出来的 target_base_name(策展人不一定是
                              目标库 owner,notebooks 只覆盖自有∪只读加入,猜不出别人创建的公共库真名)；
                              查不到再回退旧写法(notebooks.find),最后兜底截断 id。 */}
                          目标公共知识库: {cand.target_base_name || notebooks.find((n) => n.id === cand.target_base_id)?.name || cand.target_base_id.slice(0, 10)}
                        </p>
                      )}
                      {review.evidence.length > 0 && (
                        <div className="stack" aria-label="服务端校验证据">
                          <strong>证据</strong>
                          {review.evidence.map((evidence, index) => (
                            <article className="item" key={`${cand.id}-evidence-${index}`}>
                              <div className="tool-hint">
                                {evidence.sourceTitle || "来源"}
                                {evidence.locationLabel ? ` · ${evidence.locationLabel}` : ""}
                              </div>
                              <div style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
                                {evidence.quotedSpan}
                              </div>
                            </article>
                          ))}
                        </div>
                      )}
                      {cand.base_match_id && (
                        <p className="conflict-note">公共知识库中已有相似内容 — 批准后将合并。</p>
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
                            批准收录
                          </button>
                        </div>
                      )}
                    </article>
                    );
                  })}
                </div>
              )}
            </div>
            </>)}
          </FloatingModalCard>
        </section>
      )}

      {pendingPromotionObjectId && (
        <section
          className="utility-modal"
          role="dialog"
          aria-modal="true"
          onClick={(event) => { if (event.currentTarget === event.target) setPendingPromotionObjectId(null); }}
        >
          <FloatingModalCard storageKey="promotionTarget.window" className="utility-modal-card narrow">
            {(floating) => (<>
            <div className="source-modal-header" {...floating.dragHandleProps}>
              <div>
                <h2>选择贡献目标</h2>
                <p>本笔记本挂载了多个公共知识库，请选择这条知识要进入哪一个。</p>
              </div>
              <button className="icon-button" onClick={() => setPendingPromotionObjectId(null)} title="Close">×</button>
            </div>
            <div className="promotion-target-list">
              {(promotionTarget.kind === "choose" ? promotionTarget.options : []).map((base) => (
                <button
                  key={base.id}
                  type="button"
                  className="sort-button promotion-target-option"
                  onClick={() => {
                    const objectId = pendingPromotionObjectId;
                    setPendingPromotionObjectId(null);
                    if (objectId) submitPromotion(objectId, base.id).catch(reportError);
                  }}
                >
                  <span className="promotion-target-name" title={base.name}>{base.name}</span>
                </button>
              ))}
            </div>
            </>)}
          </FloatingModalCard>
        </section>
      )}

      {edgeReviewOpen && (
        <section
          className="utility-modal"
          role="dialog"
          aria-modal="true"
          onClick={(event) => { if (event.currentTarget === event.target) setEdgeReviewOpen(false); }}
        >
          <FloatingModalCard storageKey="edgeReview.window" className="utility-modal-card">
            {(floating) => (<>
            <div className="source-modal-header" {...floating.dragHandleProps}>
              <div>
                <h2>关系审核队列</h2>
                <p>按「高中心性 × 低可信」排序的关系。确认可信的关联，或拒绝错误的关联（被拒的关联将从所有图推理遍历中排除）。</p>
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
            </>)}
          </FloatingModalCard>
        </section>
      )}

      {toast && <div className="toast">{toast}</div>}
      <PendingToast toast={pending.toast} onClose={() => pending.setToast(null)}
        onClick={() => { if (pending.toast) openDoneItem(pending.toast); }} />

      {modelPanelOpen && (
        <ModelServicePanel
          status={modelStatus}
          highlightedServiceId={highlightedModelServiceId}
          isAdmin={currentUser.role === "admin"}
          onTestOne={async (serviceId) => { await runSystemModelTest(serviceId); }}
          onTestAll={runAllSystemModelTests}
          onClose={closeModelPanel}
          testingServiceIds={modelTestActivity.services}
          allTesting={modelTestActivity.all}
          returnFocusTo={modelPanelReturnFocusRef.current}
        />
      )}
    </div>
  );
}
function NotebookList({
  entries,
  openNotebook,
  openMemory,
  openMenu
}: {
  entries: Array<{ notebook: NotebookSummary; index: number; hits: SearchHit[] }>;
  openNotebook: (id: string) => void;
  openMemory: (id: string) => void;
  openMenu: (id: string, event: MouseEvent<HTMLButtonElement>) => void;
}) {
  return (
    <section className="notebook-list">
      <div className="notebook-list-header">
        <span>标题</span><span>来源</span><span>记忆</span><span>创建日期</span><span>角色</span><span />
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
          <button className="notebook-list-cell notebook-memory-link" onClick={() => openMemory(notebook.id)}>{notebook.counts.memories ?? 0} 条</button>
          <button className="notebook-list-cell" onClick={() => openNotebook(notebook.id)}>{notebook.created_label}</button>
          <button className="notebook-list-cell role-cell" onClick={() => openNotebook(notebook.id)}>Owner</button>
          <button className="list-row-menu" onClick={(event) => openMenu(notebook.id, event)} title="笔记本操作">⋮</button>
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

// Keep only static table markup; drop scripts/styles and any event handlers.
function sanitizeTableHtml(html: string): string {
  const withoutBlocks = html.replace(/<\/?(script|style)[^>]*>/gi, "");
  const withoutHandlers = withoutBlocks.replace(/\son\w+\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)/gi, "");
  const allowed = /^<\/?(table|thead|tbody|tfoot|tr|td|th|caption)(\s[^>]*)?>$/i;
  // Strip every tag that is not part of the allow-list above.
  return withoutHandlers.replace(/<\/?[a-z][^>]*>/gi, (tag) => (allowed.test(tag) ? tag : ""));
}

function ElementBody({ element, notebookId }: { element: SourceElement; notebookId: string }) {
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
  if (element.element_type === "image") {
    const assetId = typeof element.metadata?.asset_id === "string" ? element.metadata.asset_id : "";
    const caption = typeof element.metadata?.caption === "string" ? element.metadata.caption : "";
    const url = sourceImageAssetUrl(API_BASE, notebookId, assetId);
    if (url) {
      return (
        <figure className="element-image-figure">
          <AuthedImage url={url} alt={caption || "figure"} />
          {caption ? <figcaption>{caption}</figcaption> : null}
        </figure>
      );
    }
    if (caption) {
      return (
        <figure className="element-image-figure">
          <figcaption>{caption}</figcaption>
        </figure>
      );
    }
    // 无可渲染图片资源(如 MINERU_RETURN_IMAGES=0 关闭图片)且无 caption 时，
    // 回退到与其余元素类型一致的纯文本展示，避免空的占位边框。
    return <p>{element.text}</p>;
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

function kgConfidenceLabel(confidence?: number) {
  if (typeof confidence !== "number" || !Number.isFinite(confidence)) return "";
  const normalized = confidence > 1 ? confidence : confidence * 100;
  return `置信 ${Math.round(normalized)}%`;
}

function KgEvidenceCard({ evidence, index }: { evidence: EvidenceItem; index: number }) {
  const sourceLabel = evidence.source_title || evidence.source_id || "未知来源";
  const meta = [
    evidence.location_label,
    label(ELEMENT_TYPE, evidence.element_type, ""),
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
      <KgEvidenceBody
        elementType={evidence.element_type}
        text={evidence.element_text || evidence.quoted_span}
      />
    </article>
  );
}

function KgOccurrenceCard({ occurrence, index }: { occurrence: KgOccurrence; index: number }) {
  const sourceLabel = occurrence.source_title || occurrence.source_id || "未知来源";
  const meta = [
    occurrence.location_label,
    label(ELEMENT_TYPE, occurrence.element_type ?? "", ""),
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
      <KgEvidenceBody
        elementType={occurrence.element_type}
        text={occurrence.element_text || occurrence.quoted_span}
      />
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
      <KgEvidenceBody text={step.element_text} />
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

/**
 * 知识对象字段名的界面名。未命中时**刻意**原样显示 key —— 与 relationLabel 的中性
 * 兜底相反,理由是 object schema 允许用户自建类型与字段(「图谱 Schema」里的新增
 * 类型 / 归纳候选),此时 key 就是用户自己起的名字,原样显示才诚实;换成中性词反而
 * 把用户唯一能辨认这个字段的信息抹掉。故它不是「兜底即原值」那个 bug,而是一条经
 * 评审的透出路径,写法与 kg-type-mark.tsx 透出自定义 object_type 保持一致。
 *
 * 用 Object.hasOwn 而非 `FIELD_LABELS[k] ?? k`:后者走原型链,key 撞上
 * "constructor"/"toString"/"__proto__" 时会返回函数/对象,渲染进 JSX 就是
 * "Objects are not valid as a React child" 白屏(vocabulary.ts 的 label() 记着同一个
 * 坑)。字段名由用户自定义,撞上这些词完全可能。
 */
function fieldLabel(key: string): string {
  return Object.hasOwn(FIELD_LABELS, key) ? FIELD_LABELS[key] : key;
}

function genericBody(item: KnowledgeItem) {
  const fields = (item.fields ?? []).filter((f) => f.value && f.value !== item.headline);
  if (fields.length === 0) return null;
  return (
    <>
      {fields.map((field) => (
        <p key={field.key}>
          <strong>{fieldLabel(field.key)}：</strong>
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
        <span>说明（用于分析提示）</span>
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
  proposeDisabledReason,
  total,
  page,
  onPage,
  readOnly,
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
  // 返回 Promise:面板据此在扫描期间禁用「查重」并改文案(全库规范名归一化比对,大库不是
  // 瞬时的)。父层的 `() => findDuplicates(kind).catch(reportError)` 本就返回 Promise。
  onFindDuplicates: () => void | Promise<void>;
  // 同上,返回 Promise 供面板做「合并中…」的忙碌态。
  onMerge: (sourceId: string, intoId: string) => void | Promise<void>;
  reload: () => void;
  tier?: string;
  onPropose?: (id: string) => void;
  proposeDisabledReason?: string;
  total: number;
  page: number;
  onPage: (p: number) => void;
  readOnly?: boolean;
}) {
  const [ctx, setCtx] = useState<Record<string, NodeContext>>({});
  const [dupBusy, setDupBusy] = useState(false);
  // 正在合并的重复条目 id。onMerge 会连着重拉知识列表、类型统计并**重跑一次查重**,
  // 是这个面板里最慢的一步;不锁住的话在几个重复组上连点会并发排出若干次全量重扫。
  const [mergingId, setMergingId] = useState<string | null>(null);
  useEffect(() => { setCtx({}); }, [kind]);
  const runFindDuplicates = async () => {
    if (dupBusy) return;
    setDupBusy(true);
    try { await onFindDuplicates(); } finally { setDupBusy(false); }
  };
  const runMerge = async (sourceId: string, intoId: string) => {
    if (mergingId) return;
    setMergingId(sourceId);
    try { await onMerge(sourceId, intoId); } finally { setMergingId(null); }
  };
  const statuses = ["all", ...KNOWLEDGE_STATUS_OPTIONS];
  // Build tabs purely from the dynamic /knowledge-types response.
  const tabs: Array<{ key: string; label: string; count?: number }> = types.map((t) => ({
    key: t.object_type,
    label: t.label,
    count: t.count
  }));
  // 条目类型名优先用 API label(types 里含中文名,自定义类型也对);拿不到再退小表。
  // 修「顶部 tab 用 t.label 显中文、条目却用 kgTypeLabel 显英文」的不一致。
  const typeLabelBy = new Map(types.map((t) => [t.object_type, t.label]));
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
          {statuses.map((value) => <option key={value} value={value}>{value === "all" ? "全部状态" : label(KNOWLEDGE_STATUS, value, "其他")}</option>)}
        </select>
        <button className="sort-button" onClick={reload}>刷新</button>
        {!readOnly && (
          <button className="sort-button" disabled={dupBusy} onClick={() => { void runFindDuplicates(); }}>
            {dupBusy ? "查重中…" : "查重"}
          </button>
        )}
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
                  <span><LatexText text={member.headline} isFormula={(member.object_type || group.object_type) === "formula"} /> <span className="tag">{label(KNOWLEDGE_STATUS, member.status, "其他")}</span></span>
                  {!readOnly && memberIndex > 0 && (
                    <button
                      className="sort-button"
                      disabled={mergingId !== null}
                      onClick={() => { void runMerge(member.id, group.members[0].id); }}
                    >
                      {mergingId === member.id ? "合并中…" : "合并到第 1 条"}
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
                <span>{typeLabelBy.get(item.object_type ?? kind) ?? kgTypeLabel(item.object_type ?? kind)}</span>
                <h3><LatexText text={knowledgeHeadline(kind, item)} isFormula={(item.object_type ?? kind) === "formula"} /></h3>
              </div>
              {knowledgeBody(kind, item)}
              <div className="tag-row">
                {item.severity && <span className={`tag severity-${item.severity}`}>{label(SEVERITY, item.severity, "—")}</span>}
                {(item.applies_to ?? []).map((scope) => <span className="tag" key={scope}>{scope}</span>)}
              </div>
              <div className="knowledge-govern">
                {readOnly ? (
                  <>
                    <span className="tag">{label(KNOWLEDGE_STATUS, item.status, "其他")}</span>
                    {item.owner && <span className="tag">Owner {item.owner}</span>}
                  </>
                ) : (
                  <>
                    <label>状态
                      <select value={item.status} onChange={(event) => onStatus(item.id, event.target.value)}>
                        {KNOWLEDGE_STATUS_OPTIONS.map((value) => <option key={value} value={value}>{label(KNOWLEDGE_STATUS, value, "其他")}</option>)}
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
                  </>
                )}
                {item.last_reviewed && <span className="tag">reviewed {item.last_reviewed.slice(0, 10)}</span>}
                {!readOnly && tier === "personal" && item.status !== "deprecated" && onPropose && (
                  <button
                    className="sort-button"
                    title={proposeDisabledReason || "贡献到公共知识库"}
                    disabled={Boolean(proposeDisabledReason)}
                    onClick={() => onPropose(item.id)}
                  >
                    ↑ 提交贡献
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
