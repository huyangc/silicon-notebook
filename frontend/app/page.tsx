"use client";

import { ChangeEvent, DragEvent as ReactDragEvent, FormEvent, Fragment, KeyboardEvent as ReactKeyboardEvent, MouseEvent, useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle, ArrowLeft, BarChart3, Check, ChevronRight, Cpu, Database, Edit3, ExternalLink, FileText, GitMerge, LayoutDashboard, LayoutGrid, Link2, List as ListIcon, Loader2, Network, PanelLeftClose, PanelLeftOpen, Plus, Settings, Share2, Sparkles, Table2, Trash2, Upload, User, Users, X } from "lucide-react";
import "katex/dist/katex.min.css";
import dynamic from "next/dynamic";
import { AnswerView, LatexText, ReasoningTracePanel } from "./answer-panel";
import { AuthedImage } from "./authed-image";
import { FormulaView } from "./formula-view";
import {
  currentPreviewImage,
  type AnswerImagePreviewRequest,
} from "./image-preview";
import { KgEvidenceBody } from "./kg-evidence-body";
import { MemoryPanel, MemorySaveDialog } from "./memory-panel";
import { KnowhowPanel } from "./knowhow-panel";
import { ContentOverviewCards } from "./content-overview-cards";
import { AnalyticsLoadScope, startAnalyticsLoads } from "./analytics-loaders";
import { sourceAnomalies } from "./anomaly-severity";
import {
  baseIsSelected,
  baseScopePayload,
  defaultBaseScopeSelection,
  defaultSourceScopeSelection,
  localScopeIsEmpty,
  retrievalScopeSummary,
  selectedBaseIds,
  selectedSourceCount,
  sourceIsSelected,
  sourceScopePayload,
  toggleBaseSelection,
  type BaseScopeSelection,
} from "./source-scope";
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
import { AgentProfilePanel } from "./agent-profile-panel";
import { kgBandTarget, kgBandVelocity, kgTypeBandTargets } from "./kg-layout";
import { withoutDecidedMerge } from "./kg-merge-model";
import {
  ASK_MODE_GROUPS,
  groupOf, groupLabel, modesInGroup, defaultModeForGroup,
  requiresKg, streamsTrace,
} from "./ask-modes";
import {
  ASK_RETRIEVAL_EFFORT_OPTIONS,
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
  mergeMountCandidates,
  mountCostHint,
  resolvePromotionTarget,
  shouldShowBorrowedBaseHint,
  toMountedBases,
  type MountedBase,
} from "./notebook-bases";
import {
  describeScaleIndex, latestScaleIndexDoneKey, queuedScaleIndexImmediateOp, queuedScheduleHint, scaleIndexOpConfirm, SCALE_OP_MODE, UNINDEXED_SCOPE_HINT,
  type ScaleIndexOp, type ScaleIndexStatus,
} from "./scale-index";
import {
  getShareState,
  shareNotebook,
  unshareNotebook,
  previewShared,
  copyShared,
  joinShared,
  leaveNotebook,
  sharedByMe,
  shareModeLabel,
  shareLinkCopyToast,
  parseShareToken,
  buildShareLink,
  type ShareState,
  type SharedPreview,
  type SharedByMeItem,
} from "./notebook-share";
import { parseUrlLines } from "./url-sources";
import {
  normalizedNotebookName,
} from "./notebook-creation";
import { fetchEdgeReviewQueue, reviewRelation, type EdgeReviewItem } from "./edge-review-queue";
import { conversationsOlderThan, CLEANUP_PRESETS } from "./conversation-cleanup";
import { fetchMe, logoutUser, updateUiMode, type AuthUser } from "./auth";
import { autoModeAskPlaceholder, isAdvanced, normalizeUiMode, type UiMode } from "./ui-mode.ts";
import {
  describeIndexingPipelineState,
  indexingPipelineConfirmMessage,
  indexingPipelineIdsEqual,
  indexingPipelineReadOnlySummary,
  notebookIndexingPipelineReadOnlySummary,
  selectedIndexingPipelineOption,
} from "./indexing-pipeline-settings.ts";
import { useSourceLibrary } from "./use-source-library.ts";
import { useAskSession } from "./use-ask-session.ts";
import { useReportWorkspace } from "./use-report-workspace.ts";
import { useKgWorkspace } from "./use-kg-workspace.ts";
import { useNotebookCollection, type NotebookEditorPatch } from "./use-notebook-collection.ts";
import {
  useRootModalCoordinator,
  type RootModalCloseReason,
  type RootModalLease,
  type RootModalOwner,
  type RootModalSlot,
} from "./use-root-modal-coordinator.ts";
import { KG_RANGE_DEFAULT, KG_RANGE_STEPS } from "./kg-workspace-model.ts";
import { API_BASE } from "./api-config";
import { clearToken, getToken } from "./auth-session";
import { copyTextSafely } from "./copy-text";
import { useCopyResult } from "./copy-result";
import { httpErrorStatus, logDiagnostic, toUserMessage } from "./errors.ts";
import { DEFAULT_SUPPORTED_SOURCE_EXTENSIONS, fetchDocumentTypes, fetchHealth, fetchSystemConfiguration, probeReady, type ParserEngineCapability, type ReadySnapshot } from "./system-api";
import { backfillPaperMetadata, fetchNotebookAnalytics, fetchNotebookContentOverview, fetchNotebookIndexingPipeline, getNotebook, listNotebooks } from "./notebook-api";
import { detectSourceTypes, importUrlSources, listSources, uploadSources, fetchCheckup, reparseSources, backfillVectors } from "./source-api";
import { sourceKgBadge } from "./source-kg-badge.ts";
import { classifyStagedFiles, compactStagedFileName, mergeLiveStagedFileWarnings, scanStandaloneMarkdownImageWarnings, summarizeUpload, uploadDocTypeFields, fillAutoDetectedTypes, markTouched, markAllTouched, sourceUploadSizeLabel, splitFilesByUploadSize, type SkippedStagedFile, type StagedFileWarning } from "./source-upload.ts";
import { emptyStagedList, mergeStagedFiles, stagedFileKey, type StagedList } from "./staged-files.ts";
import {
  bundleCapsFrom, bundleDirTotalBytesLimit, bundleErrorMessage, bundleFileNamesFor,
  bundleImagesEffectivelyEnabled,
  classifyBundleContents, collectDirectoryFiles, directoryHasMarkdown,
  directoryTooLargeMessage, directoryTruncatedMessage, inlineTooLargeImageLines,
  inlineTooLargeMessage, notStagedNote, processBundleCandidates,
  readDirectoryAsBundleFiles, unpackZipFile,
  ALREADY_STAGED_REASON, BUNDLE_IMAGES_DISABLED_NOTE, BUNDLE_READ_FAILED_REASON,
  BUNDLE_STAGE_FALLBACK_MAX_FILES_PER_BATCH,
  DIRECTORY_READ_FAILED_REASON, NO_MARKDOWN_IN_BUNDLE_REASON,
} from "./bundle-intake.ts";
import { BundleChoicePanel, BundleReceiptsPanel, type BundleReceiptEntry } from "./bundle-upload-panels.tsx";
import type { BundleFile, InlineReceipt } from "./md-bundle.ts";
import { sourceHealthGroups, checkupCount, checkupAlertSignature, repairRelease, isRepairing, type RepairRelease } from "./checkup-view";
import { askQuestionLimitHint, fetchAnswerMemoryLinks } from "./ask-api";
import { buildPublicReportLink } from "./public-report";
import { cancelScaleIndex, fetchIndexStatus, fetchScaleIndexStatus, rebuildScaleIndex, type IndexStatus } from "../features/kg-maintenance/kg-api";
import {
  type ModelServiceStatusItem,
  type ModelServicesStatus,
  fetchModelServiceStatus,
  testAllSystemModelServices,
  testSystemModelService,
} from "./model-services.ts";
import { FloatingModalCard } from "./floating-modal-card";
import { ImagePreviewModal } from "./image-preview-modal";
import { ConversationShareModal } from "./conversation-share-modal";
import {
  CommandCatalogReview,
  CommandCatalogSection,
  type CatalogConfirmRequest,
  type CatalogReviewRequest,
} from "./command-catalog-panel";
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
import { GroupsPage } from "./groups-page";
import { NotebookGroupShare, BORROWED_BASE_SHARE_WARNING } from "./notebook-group-share";
import {
  joinGroupInvite,
  grantedViaLabel,
  groupsHash,
  isGroupGranted,
  parseGroupsHash,
  parseGroupInviteToken,
  partitionByGrant,
  type GroupPageTab,
} from "./group-api";
import { NotebookMenuActions, ReaderNotebookBadge } from "./notebook-reader-actions";
import { PasswordChangeModal } from "./password-change-modal";
import { SearchProfileModal } from "./search-profile-modal";
import { AskComposer } from "./ask-composer";
import { quotedPhraseHint } from "./query-syntax";
import { AskIntentReview } from "./ask-intent-review";
import {
  borrowedKgBaseNames,
  hasLocalEvidence,
  isAskBlocked,
  kgAvailableForScope,
  kgBlockedByBaseScope,
} from "./ask-availability";
import { AskSessionHeaderActions } from "./ask-session-header";
import { ChatTurnNav, chatTurnDomId } from "./chat-turn-nav";
import { ChatQuestion } from "./chat-question";
import { ChatAnswer } from "./chat-answer";
import { Pagination } from "./Pagination";
import { downloadReportArchive, downloadReportMarkdown, ReportsPanel } from "./report-view";
import {
  DEFAULT_REPORT_MAX_SECTIONS,
  DEFAULT_REPORT_MAX_SUBQUERIES_PER_SECTION,
} from "./report-outline-model";
import { SourceDetailWindow } from "./source-detail-window";
import { useWorkspaceExtensions } from "./use-workspace-extensions";
import { createOwnedWorkspaceExtensionActions } from "../features/extension-sdk/actions";
import { WorkspaceExtensionOutlet } from "../features/extension-sdk/host";
import { WORKSPACE_UI_CONTRIBUTIONS } from "../features/extension-sdk/workspace-registry";
import { sourceElementDomId } from "./source-detail-state";
import { SchemaManager, type SchemaView } from "./schema-manager";
import { usePendingActions, PendingBell, PendingToast, type PendingItem } from "./pending-center";
import { canSeeAdminUsage } from "./admin/usage/format.ts";
import { shouldResumeScaleIndex } from "./in-progress-resume";
import {
  canContinueKgBuild,
  kgBuildPresentation,
} from "./kg-build-status";
import { sourceImageAssetUrl } from "./source-image";
import { crossLibrarySourceNotebookId } from "./source-scope";
import {
  readStableSourceSnapshot,
} from "./source-delete-state";
import {
  runNotebookTransition,
  transitionStep,
  type TransitionStep,
} from "./notebook-transition";
import {
  beginOpen,
  settleOpen,
  shouldCoalesceOpen,
  type NotebookOpenGuard,
} from "./notebook-open-guard";
import {
  doneItemDestination,
  historyModeForTransition,
  NOTEBOOK_PRIVATE_MEMORY_DELETE_WARNING,
  notebookRoleText,
  openMemoryDeepLink,
  workspaceCapabilities,
  workspaceRequestIsCurrent,
} from "./workspace-transitions";
import {
  CHAT_MODES,
  KNOWLEDGE_STATUS_OPTIONS,
  SOURCES_PAGE_SIZE,
  showPaperMetaBackfill,
  type ChatMode,
  type ConversationSummary,
  type DuplicateGroup,
  type Evidence,
  type EvidenceItem,
  type FgLink,
  type FgNode,
  type Health,
  type KgBuildJobStatus,
  type KgObject,
  type KgOccurrence,
  type KgProcedureStep,
  type KnowledgeItem,
  type KnowledgeKind,
  type KnowledgeTypeCount,
  type MemoryRecord,
  type NodeContext,
  type CheckupResponse,
  type NotebookAnalytics,
  type NotebookContentOverview,
  type NotebookSummary,
  type PendingMerge,
  type SearchHit,
  type SourceElement,
  type SourceSummary,
  type UnifiedConceptNode,
} from "./workspace-model";
import { documentUploadBlockReason, resolveDocumentCapacity } from "./document-limit";
import { label, PARSE_STATUS, ELEMENT_TYPE, KNOWLEDGE_STATUS, PROMOTION_STATUS, SEVERITY, CHECKUP_FIX, CHECKUP_FIX_BUSY } from "./vocabulary";

/**
 * 标签页重新可见时,两次「访问权复核」之间至少隔这么久。
 *
 * 它只是节流,不是新鲜度承诺:密集 alt-tab 不该每切回一次就发一次 `listNotebooks()`。
 * 复核本身是尽力而为的——没有推送通道,一直停在前台不动的标签页仍然要等下一次交互
 * 撞上 403(见 page.tsx 里那个 visibilitychange effect 的说明)。
 */
// react-force-graph-2d uses canvas/window; load client-side only.
const ForceGraph2D = dynamic(() => import("react-force-graph-2d"), { ssr: false });

// 标题清理需要在系统配置返回前可用，因此使用与后端注册表配套的兼容默认值；上传
// 校验、accept 与可见格式列表则使用 /system/config 下发的权威注册表投影。
const SUPPORTED_SOURCE_EXT_GROUP = DEFAULT_SUPPORTED_SOURCE_EXTENSIONS.join("|");
// 旧版二进制 Office 不被 MinerU 支持，给专门提示引导用户另存为 OOXML。.xls 是例外：
// xlrd 纯 Python 可读，注册表 builtin 引擎已直接放行，这里只剩 doc/ppt 两个仍需
// 引导另存为的旧格式。
const LEGACY_OFFICE_EXTENSIONS = ["doc", "ppt"];

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

// --- 打开笔记本的单一 transition：共享类型 ---------------------------------
// 编排本身在 `notebook-transition.ts`（纯逻辑）；这里只声明本页面往里填的两个
// 载荷类型与来源库那一步的哨兵 ticket。

/** 打开一本笔记本要并行取回的两份快照。请求数恒为 2，且恒并行。 */
type NotebookOpenLoad = {
  readonly notebook: NotebookSummary;
  readonly sourcesPage: Awaited<ReturnType<typeof listSources>>;
};

/** 提交成功后交给每个 owner 的结果。`null` 代表这次打开被放弃（回滚）。 */
type NotebookOpenOutcome = {
  readonly actorId: string;
  readonly notebookId: string;
  readonly workspaceEpoch: number;
  readonly notebook: NotebookSummary;
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
  const workspaceActorIdRef = useRef<string | null>(currentUser?.id ?? null);
  workspaceActorIdRef.current = currentUser?.id ?? null;
  // 自动/高级界面模式的唯一判据——所有隐藏/显示分支与请求侧 scope 强制都读这
  // 一个值(ui-mode.ts)，不散落第二份布尔。未登录/字段缺失时归一成 "auto"。
  const uiMode: UiMode = normalizeUiMode(currentUser?.ui_mode);
  const [authChecked, setAuthChecked] = useState(false);
  const [health, setHealth] = useState<Health | null>(null);
  const [currentNotebookId, setCurrentNotebookId] = useState<string | null>(null);
  const [currentNotebook, setCurrentNotebook] = useState<NotebookSummary | null>(null);
  const [outerView, setOuterView] = useState<"notebooks" | "memory" | "groups">("notebooks");
  const [groupNavigation, setGroupNavigation] = useState<{ groupId: string; tab: GroupPageTab }>({
    groupId: "",
    tab: "notebooks",
  });
  // 挂载参考库的检索范围。与来源那一维**分开**存：粒度不同（整个库 vs 单篇来源）、
  // 生命周期也不同（挂载边在设置里改，来源在这个页签改），后端也是分开校验的。
  const [baseScopeSelection, setBaseScopeSelection] = useState<BaseScopeSelection>(
    defaultBaseScopeSelection,
  );
  const [linkSectionOpen, setLinkSectionOpen] = useState(false);
  const [urlText, setUrlText] = useState("");
  const [urlBusy, setUrlBusy] = useState(false);
  const urlRequestOwnerRef = useRef<object | null>(null);
  const [urlRejected, setUrlRejected] = useState<Array<{ url: string; reason: string }>>([]);
  const [docTypeOptions, setDocTypeOptions] = useState<Array<{ id: string; label: string }>>([]);
  // Authenticated system configuration is the browser mirror of Settings. Null is
  // only the short initial fetch window; the server remains the final 413 guard.
  const [sourceUploadMaxBytes, setSourceUploadMaxBytes] = useState<number | null>(null);
  const [sourceUploadMaxFilesPerBatch, setSourceUploadMaxFilesPerBatch] = useState<number | null>(null);
  // 压缩包/文件夹上传的图片配对预检护栏(§3.3):`null` = 拿不到这个上限,不做本地
  // 预检；`sourceImagesEnabled` 缺省按 true(旧后端从未关闭过图片存储)。
  const [sourceImageMaxBytes, setSourceImageMaxBytes] = useState<number | null>(null);
  const [sourceImageMaxPerSource, setSourceImageMaxPerSource] = useState<number | null>(null);
  const [sourceImagesEnabled, setSourceImagesEnabled] = useState(true);
  // 有效关闭态:总开关 false,**或**任一上限被显式配成 `0`(合法部署值,语义是「一张
  // 都不存」)。零值部署此前会走完整内联并报「N 张已内联」,而服务端把资产全部丢弃
  // (codex #518 R1 P2)。内联与面板顶部提示读同一个判据,不各写一份。
  const sourceImagePairingEnabled = bundleImagesEffectivelyEnabled({
    imagesEnabled: sourceImagesEnabled,
    imageMaxBytes: sourceImageMaxBytes,
    maxImagesPerSource: sourceImageMaxPerSource,
  });
  const [supportedSourceExtensions, setSupportedSourceExtensions] = useState<string[]>(
    DEFAULT_SUPPORTED_SOURCE_EXTENSIONS,
  );
  const [parserEngines, setParserEngines] = useState<ParserEngineCapability[]>([]);
  const supportedSourceAccept = useMemo(
    () => supportedSourceExtensions.map((ext) => `.${ext}`).join(","),
    [supportedSourceExtensions],
  );
  const supportedSourceUserHint = useMemo(
    () => supportedSourceExtensions.map((ext) => ext.toUpperCase()).join(" / "),
    [supportedSourceExtensions],
  );
  const [reportMaxSections, setReportMaxSections] = useState(
    DEFAULT_REPORT_MAX_SECTIONS,
  );
  const [reportMaxSubqueriesPerSection, setReportMaxSubqueriesPerSection] = useState(
    DEFAULT_REPORT_MAX_SUBQUERIES_PER_SECTION,
  );
  // 「我的回答偏好」入口的部署总闸(账户菜单)。默认 true——反方向:配置还没读回来、
  // 或后端是没升级到这一批的旧版本时仍给入口(system-api.ts 同样把字段缺失解析成
  // true);真正的写路径仍由 PATCH /me/search-profile 的 409 兜底。
  // codex #535 R3 P2:初始 false——config 未返回/瞬态失败/旧后端时不得先亮出
  // 一个 PATCH 端点不存在的入口;system-api 对缺失字段也归一成 false,两侧同向。
  const [userSearchProfileEnabled, setUserSearchProfileEnabled] = useState(false);
  // 待上传列表：文件 + 每项文档类型 + 每项是否被用户显式表态，**一个** state 对象。
  // 三条数组必须逐项对齐（uploadDocTypeFields 按下标配对），而入列会被跨 await 的
  // 异步链触发（文件夹遍历或兼容 ZIP intake）——拆成三个 state 就只能各自 setState，等长
  // 不变量没有任何一处能一次性维护。合并语义与等长不变量的真源在 staged-files.ts。
  const [staged, setStaged] = useState<StagedList>(() => emptyStagedList());
  // 同步 ref 镜像：updateStaged 是**唯一**写入口，写 state 的同时先把新值写进 ref，
  // 于是「从最新值起算」不再依赖 React 何时提交。这正是 zip/pdf 同批那个静默丢失的
  // 根因——旧实现从 render 闭包读 stagedFiles 拼好数组再整体写回，zip 解完（跨了
  // await）时读到的是「还没有 pdf」的旧闭包，把 pdf 覆盖没了。沿用本文件既有的
  // applyTouchedUpdate 惯例（同一条理由）。
  const stagedRef = useRef<StagedList>(staged);
  const stagedFiles = staged.files;
  const stagedDocTypes = staged.docTypes;
  const stagedDocTypeTouched = staged.touched;
  // 兜底：万一将来有路径绕开 updateStaged 直接 setStaged，提交后把 ref 拨回一致。
  useEffect(() => {
    stagedRef.current = staged;
  }, [staged]);
  function updateStaged(next: StagedList | ((prev: StagedList) => StagedList)): StagedList {
    const value = typeof next === "function" ? next(stagedRef.current) : next;
    stagedRef.current = value;
    setStaged(value);
    return value;
  }
  // 最近一次「添加文件」里被跳过的文件（类型不支持/超大小/超批量），在弹窗内持久展示。
  // 不能只发 toast：它 2.2 秒即逝，批量选文件时用户根本来不及看清哪些没进列表。
  const [stagedSkipped, setStagedSkipped] = useState<SkippedStagedFile[]>([]);
  const [stagedWarnings, setStagedWarnings] = useState<StagedFileWarning[]>([]);
  // 文件夹兼容 intake（遍历目录、内联图片）或旧 ZIP intake 在飞：非 null 时整个
  // 添加文件入口必须禁用并换成该动作语义的进行态文案（普通 ZIP 上传不经过这里）。
  const [bundleBusyLabel, setBundleBusyLabel] = useState<string | null>(null);
  // 拖入文件夹里有多个 markdown 时，等待用户勾选要添加的文件；旧 ZIP intake 也沿用
  // 此状态。非 null 期间同样视为「忙碌」，避免新的文件夹覆盖当前选择。
  // selected 默认全选（设计文档 §3.1 第 2 条）。
  const [bundleChoice, setBundleChoice] = useState<{
    label: string;
    files: BundleFile[];
    candidates: BundleFile[];
    selected: Set<string>;
    // 这个勾选面板所属那条异步链在起跑时捕获的世代——confirmBundleChoice 落盘
    // 时要拿它去比对 bundleIntakeGenerationRef，而不是比对"确认那一刻"的世代
    // （两者通常相同，但保持「捕获于链起点」的统一语义，见 bundleIntakeGenerationRef）。
    generation: number;
  } | null>(null);
  // 兼容 intake 按到达顺序串行处理；命中「多个 markdown 需勾选」时，本 resolver
  // 让异步链等到用户确认/取消才继续，避免新的文件夹覆盖尚未完成的选择。
  const bundleChoiceResolveRef = useRef<(() => void) | null>(null);
  // 忙碌文案是一个**栈**而不是单值：兼容 intake 可能嵌套，单值形态下内层清零会把
  // 外层进行态一起抹掉，导致入口在链仍运行时提前恢复可用。
  const bundleBusyStackRef = useRef<string[]>([]);
  // 用户主动关闭「添加来源」弹窗后，还在飞的文件夹 intake 不得把弹窗强行弹回来。
  // 重新打开弹窗时清零。
  const sourceModalDismissedRef = useRef(false);
  // 暂存批次的世代计数器：resetStagedIntake()（关弹窗/清空/上传成功/新建笔记本）
  // 与 openNotebook()（切换笔记本）各自 ++。文件夹 intake 这类异步链在自己
  // "起跑"那一刻捕获当前世代；写回暂存列表/回执/跳过记录（落盘）前重新比对，
  // 世代已变说明这批数据在链跑的这段时间里被用户取消或随切库作废，整条链的
  // 结果必须整体静默丢弃（含回执）——不能把已取消的文件复活进当前弹窗。
  // `sourceModalDismissedRef` 只挡弹窗重新弹出这一件事，挡不住数据本身被写回。
  const bundleIntakeGenerationRef = useRef(0);
  // 压缩包/文件夹图片配对回执：每个成功入列的 md 一条，弹窗内持久显示，直至弹窗重置
  // （关闭/清空/上传成功）——对齐「被跳过文件逐条持久列出、不许只发即逝 toast」的
  // 既有红线（stagedSkipped 同一精神，这里是配对细节，独立一块面板，见
  // bundle-upload-panels.tsx）。
  const [bundleReceipts, setBundleReceipts] = useState<BundleReceiptEntry[]>([]);
  // 拖放悬停高亮（仅大拖放区）。
  const [dropZoneDragActive, setDropZoneDragActive] = useState(false);
  // 上传在飞:multipart 传大 PDF 可能几十秒,期间「上传 N 个文件」必须禁用改文案。后端按
  // 内容哈希在同 notebook 内去重,重复提交不会建出重复来源,但会白传一遍并再跑一次解析。
  const [uploadBusy, setUploadBusy] = useState(false);
  const uploadRequestOwnerRef = useRef<object | null>(null);
  // Workspace identity remains shell-owned. The source hook receives explicit
  // prepare/commit transitions so opening a notebook can keep the existing stable
  // getNotebook + listSources snapshot instead of adding an effect-driven request.
  const activeNotebookIdRef = useRef<string | null>(null);
  const workspaceEpochRef = useRef(0);
  // 同库连点合并:大库 openNotebook 的 load 相位要数秒,期间用户对同一张卡片连点
  // 会叠出多组并行请求。guard 只记「当前在途的是哪本笔记本、哪一代」,异 id 仍然
  // 放行(沿用 workspaceEpoch 顶替语义),settle 只清自己那一代——见
  // notebook-open-guard.ts 顶部的语义说明。openingNotebookId 是渲染用的镜像。
  const notebookOpenGuardRef = useRef<NotebookOpenGuard>(null);
  const [openingNotebookId, setOpeningNotebookId] = useState<string | null>(null);
  const canWriteSources = workspaceCapabilities(
    currentNotebook?.access,
    currentUser?.role ?? "",
    currentNotebook?.can_manage_content ?? false,
  ).canWriteNotebook;
  const workspaceExtensions = useWorkspaceExtensions(
    currentUser?.id ?? null,
    currentNotebookId,
  );
  const workspaceExtensionProjection = workspaceExtensions.projection;
  // 谁认领了 root-dialog 那唯一一格 `extension` slot——存的是 **contribution id**（不是
  // plugin id：同一个插件可以注册多条 contribution，按 plugin 判会让它们一起挂出弹窗，
  // codex #578 R1 P2）。协调器只知道「那一格开着」，分不出是谁开的——插件名绝不进核心的
  // slot 联合类型（零补丁红线），所以持有者记在这里，由 host 按 `contribution.id` 过滤
  // 出每条 contribution 的 dialog view。
  // 清空只有一条路：协调器的 `onClosed`（关闭、冲突、切库、换用户走的都是它）。
  //
  // ⚠ 关闭请求要按**当时**的持有者判，所以另留一份 ref：插件留着一个旧 `closeDialog()`
  // （setTimeout、迟到的请求回调、没卸载干净的组件）而那一格已经易主时，按发放回调那
  // 一帧的闭包值判会让插件 A 关掉插件 B 刚开的弹窗（codex #578 R1 P2）。两个写入点各自
  // 同时更新 state 与 ref，不在渲染期改 ref。
  const [extensionDialogHolder, setExtensionDialogHolder] = useState<string | null>(null);
  const extensionDialogHolderRef = useRef<string | null>(null);
  const sourceLibrary = useSourceLibrary({
    actorId: currentUser?.id ?? null,
    canWriteSources,
    effects: {
      setStatusText: (message) => setStatusText(message),
      reportError,
      setToast: (message) => setToast(message),
      invalidateKnowledge: () => {
        kgWorkspace.invalidateKnowledge();
      },
      refreshCollection: async (guard) => loadNotebookCollection({ guard }),
      refreshNotebook: async (notebookId, guard) => {
        const refreshed = await getNotebook(notebookId);
        if (guard()) {
          setCurrentNotebook((current) => current?.id === notebookId ? refreshed : current);
        }
      },
      refreshCheckup: async (notebookId, guard) => {
        const refreshed = await fetchCheckup(notebookId);
        if (guard()) setCheckup(refreshed);
      },
    },
  });
  const {
    sources,
    sourceScopeSelection,
    sourcesTotal,
    notebookSourceTotal,
    sourcesPage,
    sourcesCollapsed,
    sourceQuery,
    sourceDetail,
    deletingSourceIds,
    reparsingSource,
    sourceElements,
    sourceElementsTotal,
    sourceElementStartOffset,
    sourceElementsLoading,
    highlightedElementId,
  } = sourceLibrary;
  const loadSourceElementPage = sourceLibrary.loadSourceElementPage;
  const notebookCollection = useNotebookCollection({
    actorId: currentUser?.id ?? null,
    effects: {
      reportError,
      notify: (message) => setToast(message),
      refreshComposite: async (guard) => loadNotebookCollection({ guard }),
      onNotebookCreated: async (notebook) => {
        if (!await openNotebook(notebook.id)) return;
        resetStagedIntake();
        openSourceModal();
      },
      onNotebookUpdated: (notebook, bases) => {
        if (activeNotebookIdRef.current !== notebook.id) return;
        setCurrentNotebook((current) => current?.id === notebook.id ? notebook : current);
        setTitleDraft(notebook.name);
        setCurrentNotebookBases(bases);
      },
      onNotebookDeleted: (notebookId) => {
        if (activeNotebookIdRef.current === notebookId) showCollection();
      },
      captureNavigationEpoch: () => workspaceEpochRef.current,
      reconcileAccess: (rows, navigationEpoch) => reconcileOpenNotebook(rows, navigationEpoch),
    },
  });
  const [infoModal, setInfoModal] = useState<InfoModal | null>(null);
  const [answerImagePreview, setAnswerImagePreview] = useState<AnswerImagePreviewRequest | null>(null);
  // 快照里当前这一张。打开时冻结整份清单,左右切换只动 index(见 image-preview.ts)。
  const answerImagePreviewImage = currentPreviewImage(answerImagePreview);
  // 命令目录审阅弹窗:提升到 page 根层渲染(P0 修复,见 command-catalog-panel.tsx
  // 里 CatalogReviewRequest 的注释)。CommandCatalogSection 只请求打开,真正的
  // 开关状态与渲染都在这里,与成本预告 `infoModal`/`confirmCommandCatalog` 同构。
  const [catalogReview, setCatalogReview] = useState<CatalogReviewRequest | null>(null);
  const [catalogReviewLease, setCatalogReviewLease] = useState<RootModalLease<"catalog-review"> | null>(null);
  // R8(codex PR #412 评审 P1):审阅弹窗每完成一次确认/跳过就 +1,入口卡片据此
  // 重新读一次 job。两者之间没有共享状态(弹窗在根层、卡片在来源详情里),而
  // 卡片手里的 job 快照带着「重新识别」的拦截判据 pending_candidates —— 候选全
  // 部审完之后不重读,那颗按钮会一直禁用到用户重开来源详情为止。
  const [catalogReviewSeq, setCatalogReviewSeq] = useState(0);
  // 审阅弹窗不再是来源详情的子树,不会随它一起卸载——必须自己在来源切换/来源
  // 详情关闭/切换笔记本时跟着关,否则会有一个指向旧来源的审阅弹窗孤零零地浮在
  // 界面上。openSourceById 是打开任意来源的唯一入口(见其注释),换源与关闭都会
  // 改变或清空 sourceDetail?.id;currentNotebookId 一并纳入依赖,和 lift 之前
  // CommandCatalogSection 内部那条 `[notebookId, sourceId]` 重置 effect 同一口径。
  // 计数器与弹窗同一次归零:它是「本次来源里做过几次审阅动作」,换源后旧来源的
  // 次数对新来源没有意义。归零也走 CommandCatalogSection 的 `reviewSeq <= 0`
  // 提前 return,不会给刚切过去的来源白发一次请求。
  useEffect(() => {
    setCatalogReviewSeq(0);
  }, [sourceDetail?.id, currentNotebookId]);
  const [toast, setToast] = useState("");
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
  const titleSaveOperationRef = useRef<object | null>(null);
  const [memoryAnswerId, setMemoryAnswerId] = useState<string | null>(null);
  const [memorySavedAnswers, setMemorySavedAnswers] = useState<Record<string, boolean>>({});
  // 会话公开分享弹窗：null=未打开，否则是正在分享的那条会话（按 id 作 key 重挂，
  // 切会话即重置弹窗态，避免把上一条的分享态按到新会话头上）。
  // 会话分享弹窗的目标。**不是**裸 ConversationSummary:每条回答下面的分享按钮要求把
  // 发布边界一起带上(`throughAnswerId` = 分享到这条答案为止),而会话列表里那个按钮
  // 传空串保持既有语义(整条会话 /「更新到最新」)。标题只作弹窗抬头显示。
  const [sharingSession, setSharingSession] = useState<
    { id: string; title: string; throughAnswerId: string } | null
  >(null);
  const [chatMode, setChatMode] = useState<ChatMode>("ask");
  // Promotion queue modal (Track F governance)
  const [promoQueue, setPromoQueue] = useState<PromotionCandidate[] | null>(null);
  const [promoBusy, setPromoBusy] = useState(false);
  const promoOperationRef = useRef<object | null>(null);
  // 多领域基准库:提交晋升前需要知道本笔记本挂了几个公共知识库(resolvePromotionTarget:
  // 0 个禁用按钮/1 个直接用/>1 个弹选择器)。只在进入「Rules」知识浏览 tab 时按 owner
  // 门控拉取(switchChatMode),不在打开笔记本时无条件调用。
  const [currentNotebookBases, setCurrentNotebookBases] = useState<MountedBase[]>([]);
  // 挂了 >1 个公共知识库时,点「提交晋升」先记下待定的知识对象 id,弹选择器要求选一个。
  const [pendingPromotionObjectId, setPendingPromotionObjectId] = useState<string | null>(null);
  const [edgeQueue, setEdgeQueue] = useState<EdgeReviewItem[] | null>(null);
  const [edgeBusy, setEdgeBusy] = useState(false);
  const edgeOperationRef = useRef<object | null>(null);
  // 分享(owner 侧):shareModal 存**当前**分享状态并驱动分享弹窗。它现在由
  // `GET .../share` 填充(只读),而不是打开弹窗就 POST 一条链接出来——弹窗里还有
  // 「共享给群组」一节,只想共享给群组的用户不该顺带被发一条分享链接(P1-T4)。
  // `share_token` 为空即「还没有链接」,链接区渲染成「开启链接分享」按钮。
  // shareBusy 覆盖开启/取消分享请求。
  const [shareModal, setShareModal] = useState<ShareState | null>(null);
  const [shareBusy, setShareBusy] = useState(false);
  const shareOperationRef = useRef<object | null>(null);
  // 分享链接复制的结果态。它此前只落在页面顶部的 toast 上——横幅离按钮很远、还会滚出
  // 视口，用户看到的是「按钮纹丝不动」。toast 仍然保留(失败时它带着链接原文)，但结果
  // 首先要画在按下的那一颗上。key 一律是**被复制的那条链接的 token**:「已分享」弹窗
  // 每行一颗按钮要分格，而且 token 换掉(重开分享、重新生成)时结果必须自动失配回
  // idle，不能让新链接顶着上一条的「已复制」出现。
  const shareLinkCopy = useCopyResult();
  const shareLinkInputs = useRef(new Map<string, HTMLInputElement | null>());
  // 接收分享(拷贝侧):sharedPreview 存预览并驱动预览弹窗;copyBusy 覆盖拷贝/加入请求
  const [sharedPreview, setSharedPreview] = useState<SharedPreview | null>(null);
  const [copyBusy, setCopyBusy] = useState(false);
  // 只读共享(Phase 2):退出共享请求覆盖;已分享总览 modal 的数据与开关
  const [leaveBusy, setLeaveBusy] = useState(false);
  const [sharedByMeList, setSharedByMeList] = useState<SharedByMeItem[] | null>(null);
  // 总览里正在被撤销的那一本(codex #631 R2 P2)。`shareBusy` 是全局的忙碌闸,拿它当**文案**
  // 判据会让每一行的按钮都写「取消中…」——分享得多的用户读到的是「全都在取消」。闸(disabled)
  // 仍然全局:一次只允许一个分享写操作;进行态文案只落在真正在动的那一行。
  const [unsharingNotebookId, setUnsharingNotebookId] = useState<string | null>(null);
  const [analytics, setAnalytics] = useState<NotebookAnalytics | null>(null);
  const [contentOverview, setContentOverview] = useState<NotebookContentOverview | null>(null);
  const [contentOverviewLoading, setContentOverviewLoading] = useState(false);
  const [contentOverviewError, setContentOverviewError] = useState("");
  const [memoryNavigationTarget, setMemoryNavigationTarget] = useState<MemoryNavigationTarget>({});
  const kgWorkspaceCapabilities = workspaceCapabilities(
    currentNotebook?.access,
    currentUser?.role ?? "",
    currentNotebook?.can_manage_content ?? false,
  );
  const kgWorkspace = useKgWorkspace({
    actorId: currentUser?.id ?? null,
    notebookId: currentNotebookId,
    policy: {
      canGovernKnowledge: kgWorkspaceCapabilities.canGovernKnowledge,
      canManageNotebookSchemas: kgWorkspaceCapabilities.canManageNotebookSchemas,
      canManageGlobalSchemas: kgWorkspaceCapabilities.canManageGlobalSchemas,
      canWriteKg: kgWorkspaceCapabilities.canWriteNotebook,
      externalBuildPolling: analytics !== null,
    },
    effects: {
      notify: setToast,
      reportError,
      refreshCollection: async (guard) => loadNotebookCollection({ guard }),
      refreshNotebook: async (targetNotebookId, guard) => {
        const refreshed = await getNotebook(targetNotebookId);
        if (guard() && activeNotebookIdRef.current === targetNotebookId) {
          setCurrentNotebook((current) => current?.id === targetNotebookId ? refreshed : current);
        }
        return refreshed;
      },
      focusGraphNode: (nodeId) => focusKgGraphNode(nodeId),
    },
  });
  const { knowledge: kgKnowledge, schema: kgSchema, graph: kgGraph } = kgWorkspace;
  // 为什么下面每个 root modal 曲面都逐字重复 `rootModals.view("<slot>")` 三四遍,
  // 而不像上面 kgWorkspace 那样抽出一个 `modalA11y(slot)` helper 一次性返回
  // `{ topmost, zIndex, ... }`:`root-modal-boundary.test.mjs` 按四元组
  // (aria-modal/aria-hidden/inert/zIndex)的**字面** `rootModals\.view\("<slot>"\)`
  // 正则钉住每个曲面的形状(见其"every coordinated root surface..."用例),抽个
  // helper 会让四处调用点全部换成 `view.topmost`/`view.zIndex` 这类新写法,守卫的
  // 正则不会跟着自动更新。要做这个简化,必须同一个 PR 里把守卫也改成认 helper 调用
  // 而不是认 `rootModals.view(...)` 字面链——这本身工作量不小、且与本次「去掉三段
  // reflatten shim」是两件不同的事,留作独立一件事,不要顺手在这里抽。
  // 「AI 对这个库的理解」弹窗(P1-T7)。入口由 workspace side-panel 插件提供，
  // 弹窗本身仍渲染在视图外层——它是独立的浮动窗,关掉知识图谱不必连它一起收。
  // 它的业务数据仍由 AgentProfilePanel 自持；根层是否呈现及切库同步失效由
  // useRootModalCoordinator 的 workspace lease 负责。
  const rootModals = useRootModalCoordinator({
    actorId: currentUser?.id ?? null,
    sourceId: sourceDetail?.id ?? null,
    onClosed: handleRootModalClosed,
  });
  // Domain owners may clear their payload after a successful write, permission
  // downgrade, or workspace transition.  Mirror that close into the
  // presentation coordinator so a hidden lease never remains topmost.
  useEffect(() => {
    if (!notebookCollection.editor?.target && rootModals.activeLease("notebook-editor")) {
      rootModals.requestClose("notebook-editor", "button");
    }
    if (!notebookCollection.deletion?.target && rootModals.activeLease("notebook-delete")) {
      rootModals.requestClose("notebook-delete", "button");
    }
    if (!sourceDetail && rootModals.activeLease("source-detail")) {
      rootModals.requestClose("source-detail", "button");
    }
    if (!kgSchema.open && rootModals.activeLease("kg-schema")) {
      rootModals.requestClose("kg-schema", "button");
    }
    if (!kgGraph.analysisOpen && rootModals.activeLease("kg-analysis")) {
      rootModals.requestClose("kg-analysis", "button");
    }
  }, [notebookCollection.editor?.target?.id, notebookCollection.deletion?.target?.id, sourceDetail?.id, kgSchema.open, kgGraph.analysisOpen]); // eslint-disable-line react-hooks/exhaustive-deps
  const [knowhowNavigation, setKnowhowNavigation] = useState(CLOSED_KNOWHOW_NAVIGATION);
  // Task 12（引用跳转）：ask 引用命中 knowhow 格子时的跳转目标——非 null 时
  // KnowhowPanel 挂载即定位到该表该行的抽屉（见 openKnowhowAt）。
  const [backfillingMeta, setBackfillingMeta] = useState(false);
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
    openInfoModal({
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
    if (activeNotebookIdRef.current === nb) void kgWorkspace.startKgBuild(false);
  };
  // Trigger full re-extract: clears existing KG and rebuilds from all sources.
  // 破坏性(清空重抽)——统一确认(与概念合并/检索索引三系统一致的确认机制+文案模板)。
  const startKgRebuild = (nb: string) => {
    confirmIndexAction("全部重新分析？\n\n将清空现有知识图谱并重新分析全部来源。后台进行，完成后自动更新。", () => {
      if (activeNotebookIdRef.current === nb) void kgWorkspace.startKgBuild(true);
    });
  };
  // 侧栏收起状态持久化(localStorage;隐私模式等读写失败静默降级)
  useEffect(() => {
    try {
      if (window.localStorage.getItem("sn.sourcesCollapsed") === "1") sourceLibrary.setSourcesCollapsed(true);
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
      !rootModals.view("source-add").open
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
          setSupportedSourceExtensions(config.supported_source_extensions);
          setParserEngines(config.parser_engines);
          setReportMaxSections(config.report_max_sections);
          setReportMaxSubqueriesPerSection(config.report_max_subqueries_per_section);
          setSourceImageMaxBytes(config.source_image_max_bytes);
          setSourceImageMaxPerSource(config.source_image_max_per_source);
          setSourceImagesEnabled(config.source_images_enabled);
        }
      } catch {
        if (!cancelled) retryTimer = window.setTimeout(loadUploadLimit, 2000);
      }
    };
    void loadUploadLimit();
    return () => { cancelled = true; window.clearTimeout(retryTimer); };
  }, [rootModals.view("source-add").open, sourceUploadMaxBytes, sourceUploadMaxFilesPerBatch]);
  // Relink isolated nodes: additive/background, no confirm needed.
  // 补连孤立节点已移入知识图谱视图（relinkFromKgView + 它下面那条 relink/status 轮询，
  // 终态时按当前范围重拉）。
  // Backfill completion polling: same shape as the kgGraph.buildingKg poll above, for the
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
            ...sourceLibrary.currentPageRequest(),
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
  // Mirror the kgGraph.buildingKg poll: while a scale-index rebuild runs, poll status every 6s
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
  // 面板打开期间本 effect 独占轮询(上方 kgGraph.buildingKg/buildingScaleIndex 两条 legacy poll
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
    const busy = kgGraph.buildingKg || kgGraph.rebuilding || buildingScaleIndex || backfillingMeta
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
        if (kgGraph.buildingKg && !s.kg.building) {
          getNotebook(nb)
            .then((refreshed) => {
              if (cancelled) return;
              setCurrentNotebook((cur) => (
                cur && cur.id === nb ? refreshed : cur
              ));
              kgWorkspace.observeNotebook(refreshed);
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
      // NotebookSummary(同上方 kgGraph.buildingKg-done 分支的既有做法)。完工不弹 toast:
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
              ...sourceLibrary.currentPageRequest(),
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
  }, [analytics, currentNotebookId, kgGraph.buildingKg, kgGraph.rebuilding, buildingScaleIndex, backfillingMeta, kgGraph.trackedKgJobId, indexStatus?.kg.building, indexStatus?.unified_kg.building]);
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
      const result = await rebuildScaleIndex(nb, when, mode);
      if (when === "idle") {
        setToast("已排队，将在服务器空闲时（低峰）重建；完成后自动更新");
        // Reflect the queued state right away; the poll effect keeps it fresh.
        fetchScaleIndexStatus(nb).then((s) => setScaleIndexStatus(s)).catch(() => {});
      } else if (result.status === "queued") {
        // 构建位已满：后端把这次「立即构建」停进 slot 等待队列（codex #627 R5 P2）。
        // 如实说「已排队」而不是「已开始」——上面乐观置的 building 在这里立刻用服务端
        // 真实快照纠正；轮询把 queued 视作未完工（见 shouldResumeScaleIndex），轮到
        // slot 自动开跑直至完成。
        setToast("构建位已满，已排队；轮到后自动开始构建，完成后自动更新");
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
        loadSourcesPage(nb, sourceLibrary.currentPageRequest()).catch(() => {});
        return false;
      }
      setToast(`已开始重新解析 ${r.scheduled.length} 篇来源；完成后会自动更新`);
      bumpCheckupRepairPoll();
      reloadCheckup(nb);
      // 让来源列表的状态标签也及时反映(分析中/已就绪)。
      loadSourcesPage(nb, sourceLibrary.currentPageRequest()).catch(() => {});
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
  const [kgSize, setKgSize] = useState({ width: 720, height: 560 });
  const chatBodyRef = useRef<HTMLDivElement | null>(null);
  const memoryLinksAbortRef = useRef<AbortController | null>(null);
  const memorySessionAbortRef = useRef(new AbortController());
  const sessionPopoverRef = useRef<HTMLDivElement | null>(null);
  const kgCanvasRef = useRef<HTMLDivElement | null>(null);
  const kgDetailRef = useRef<HTMLElement | null>(null);
  const kgGraphRef = useRef<any>(null);
  // 收到分享的 token 缓存 —— 挂载时从 URL 抓到后立即清掉 ?share,故拷贝时从这里取。
  const shareTokenRef = useRef<string | null>(null);
  const groupInviteClaimedRef = useRef("");

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
        activateWorkspaceOwners(u.id);
        setCurrentUser(u);
        await loadNotebookCollection();
        const groupTarget = parseGroupsHash(window.location.hash);
        const target = parseMemoryHash(window.location.hash);
        if (groupTarget) {
          showGroups(groupTarget, "none");
        } else if (target?.scope === "global") {
          showGlobalMemory({
            notebookId: target.filterNotebookId,
            status: target.status,
            itemId: target.itemId,
          });
        } else if (target?.scope === "notebook" && target.notebookId) {
          await openMemoryDeepLink(
            target.notebookId,
            (notebookId) => openNotebookMemory(notebookId, u.id),
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
              await openNotebook(workspace.notebookId, "none", u.id);
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
      const groupTarget = parseGroupsHash(hash);
      if (groupTarget) {
        showGroups(groupTarget, "none");
        return;
      }
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
        // coalesce:false —— hash 是唯一真相源(见本 effect 顶部注释)。用户在
        // #memory=<id> 按返回时,在飞的 openNotebookMemory 打的是同一本库:合并早退会
        // 让这次 popstate 什么都不做、也不自增 epoch,于是那次 memory 打开随后
        // committed,把 URL 用 replaceState 改写回 #memory——返回键失效。必须顶替它。
        // 不改用 intent:popstate 要顶替的是「hash 指向的目的地」,不是「某个固定
        // intent」——它可能顶替 open,也可能顶替 memory,intent 维度在这里没有一个
        // 固定值可填,必须继续用 coalesce:false 无条件顶替。
        openNotebook(workspace.notebookId, "none", undefined, { coalesce: false }).catch(() => {
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
  // The handler calls openNotebook, whose Ask/source owner must use the live
  // authenticated actor. Rebind after an in-page login/logout; otherwise the
  // listener installed while currentUser was null retains an empty actor and
  // Back/Forward can leave the URL and visible workspace out of sync.
  }, [authChecked, currentUser?.id]);

  // A group invitation survives the login/register gate in the query string.
  // Once authenticated, redeem it exactly once, then remove the bearer token
  // from browser history before navigating to the joined group workspace.
  useEffect(() => {
    if (!authChecked || !currentUser || !getToken()) return;
    const token = parseGroupInviteToken(window.location.search);
    if (!token || groupInviteClaimedRef.current === token) return;
    groupInviteClaimedRef.current = token;
    const params = new URLSearchParams(window.location.search);
    params.delete("group_invite");
    const cleaned = params.toString();
    window.history.replaceState(
      null,
      "",
      window.location.pathname + (cleaned ? `?${cleaned}` : "") + window.location.hash,
    );
    joinGroupInvite(token)
      .then((group) => {
        setToast(`已加入群组「${group.name}」`);
        showGroups({ groupId: group.id, tab: "members" }, "replace");
        // The membership commit is already durable at this point. Refreshing
        // the collection is follow-up reconciliation only: a transient health
        // or notebook-list failure must not turn a successful redemption into
        // an error, nor strand the user after the bearer token was scrubbed.
        void loadNotebookCollection().catch(() => {});
      })
      .catch((error) => {
        setToast(toUserMessage(error, "加入群组失败，请重新打开邀请链接重试"));
      });
  }, [authChecked, currentUser]); // eslint-disable-line react-hooks/exhaustive-deps

  // 接收分享:挂载时读 ?share=shr-xxx,先清掉参数(避免刷新重弹),再预览打开弹窗。
  // 预览需登录(Bearer),故等 authChecked + 有 token 再拉。
  useEffect(() => {
    if (!authChecked || !currentUser?.id || !getToken()) return;
    const token = parseShareToken(window.location.search);
    if (!token) return;
    const modalLease = rootModals.issue("shared-preview", rootModals.captureActorOwner());
    if (!modalLease) return;
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
      .then((preview) => {
        if (rootModals.publish(modalLease)) setSharedPreview(preview);
      })
      .catch(() => {
        if (rootModals.leaseIsCurrent(modalLease)) setToast("分享链接无效或已取消");
      });
  }, [authChecked, currentUser?.id]);

  useEffect(() => {
    if (!toast) return;
    const timer = window.setTimeout(() => setToast(""), 2200);
    return () => window.clearTimeout(timer);
  }, [toast]);

  // Example prompts / placeholders adapt to the open notebook's imported sources,
  // so a new notebook never shows demo examples.
  const welcomeCopy = useMemo(() => welcomeCopyFor(currentNotebook, sources, notebookSourceTotal), [currentNotebook, sources, notebookSourceTotal]);
  const askHint = useMemo(() => askPlaceholder(currentNotebook), [currentNotebook]);
  // 硬约束:后端判定该库无任何可检索证据时,锁死对话框(输入/发送/快捷提问),占位改为
  // 引导文案。判据单一真源见 ask-availability(读后端 ask_available)。
  //
  // 自动模式隐藏了来源/参考库勾选框，但 sourceScopeSelection / baseScopeSelection
  // 这两份 state 仍可能留着用户此前在高级模式下收窄过的选择（切回自动模式不清空，
  // 免得再切回高级模式时白白丢失偏好）。因此**所有**读取范围的地方——计数、空判、
  // 提交给后端的 payload——一律经这两个 effective 值，不直接读原始 state：自动模式
  // 下它们恒等于「全选」，与隐藏掉的勾选框在视觉上应该表达的状态一致，也保证发出
  // 的请求不会静默沿用一份用户已经看不到、改不了的收窄状态。
  // useMemo 而不是直接调用 default*ScopeSelection()：那两个纯函数每次都返回一个
  // 新对象字面量（`{ allSelected: true, ids: new Set() }`），自动模式下若不缓存，
  // 每次渲染都会产出一个新引用，把下游依赖它做 identity 比较的 useMemo（例如
  // selectedBaseNotebookIds）逐帧打穿、白白重算。
  const effectiveSourceScopeSelection = useMemo(
    () => (isAdvanced(uiMode) ? sourceScopeSelection : defaultSourceScopeSelection()),
    [uiMode, sourceScopeSelection],
  );
  const effectiveBaseScopeSelection = useMemo(
    () => (isAdvanced(uiMode) ? baseScopeSelection : defaultBaseScopeSelection()),
    [uiMode, baseScopeSelection],
  );
  const selectedLocalSourceCount = selectedSourceCount(
    effectiveSourceScopeSelection,
    notebookSourceTotal,
  );
  // 挂载的参考库。owner 与只读访客都能从 NotebookSummary 拿到这一份（notebook-bases.ts
  // 顶部说明：listBases 是 owner-only，访客的读路径就是这里），所以范围勾选不需要新端点。
  const mountedBases = useMemo(
    () => currentNotebook?.base_notebooks ?? [],
    [currentNotebook],
  );
  const mountedBaseIds = useMemo(
    () => mountedBases.map((base) => base.id),
    [mountedBases],
  );
  const hasMountedBase = mountedBases.length > 0;
  // 本轮真正参与检索的参考库 id。计数由它派生 —— 「勾了几个」与「勾了哪几个」出自
  // 同一次求值,不可能各说各的(严格推理的可用性判定读的正是这一份)。
  const selectedBaseNotebookIds = useMemo(
    () => selectedBaseIds(effectiveBaseScopeSelection, mountedBaseIds),
    [effectiveBaseScopeSelection, mountedBaseIds],
  );
  const selectedBaseNotebookCount = selectedBaseNotebookIds.length;
  // 真机事故（本次改动的起因）：勾定单篇文章提问，16 条引用全部来自那个 84 篇论文的
  // 参考库 —— 因为参考库当时无条件全量参与。所以「范围为空」必须**两维同时**为空
  // 才算无得可搜；把参考库当成恒真的兜底，正是那条 bug 的翻版。
  //
  // 本地那一维的空判交给 localScopeIsEmpty（后端 has_local 的镜像）：不能拿「勾了几个
  // 可见来源」代表本地证据宇宙 —— Knowhow 表与已确认 Memory 没有可见来源，那样的库
  // 计数恒为 0，会被这道门连同问答输入框和新建报告一起锁死。
  const sourceScopeBlocked = (
    localScopeIsEmpty(
      selectedLocalSourceCount,
      notebookSourceTotal,
      hasLocalEvidence(currentNotebook),
    )
    && selectedBaseNotebookCount === 0
  );
  const askBlocked = isAskBlocked(currentNotebook) || sourceScopeBlocked;
  // 自动模式下被硬锁时，文档还在解析中是最常见的原因——直接说清楚还剩几篇，
  // 免得用户以为要去手动配置什么。有其他兜底文案（范围为空/无来源）时优先级更高。
  //
  // 这个数只能在 sources 代表「笔记本全部可见来源」时才可信：搜索框非空或分页未
  // 加载全部来源时，当前这份 sources 只是子集/某一页，按它数出来的 0 不能说明
  // 真的没有来源在处理——那会把「用户正在搜索」误判成「没有处理中的文档」。
  const pendingSourceCount = sourceQuery.trim() === "" && sources.length === notebookSourceTotal
    ? sources.filter((source) => !["extracted", "failed"].includes(source.parse_status)).length
    : null;
  // 自动模式没有勾选框可选——sourceScopeBlocked 在自动模式下恒等于「本地与参考库
  // 证据全空」（effective 选择恒为全选，为空只能是笔记本本身零来源零挂载库），所以
  // 那句面向勾选框的兜底文案在自动模式下答非所问，改落到「添加来源」口径。
  const askPlaceholderText = sourceScopeBlocked
    ? (isAdvanced(uiMode)
      ? "请至少选择一个来源或参考库，再开始对话"
      : "请先添加来源，再开始对话")
    : askBlocked
      ? (isAdvanced(uiMode)
        ? "请先添加来源或挂载参考库，再开始对话"
        : autoModeAskPlaceholder(pendingSourceCount, "请先添加来源或挂载参考库，再开始对话"))
      : askHint;
  const currentSourceScope = sourceScopePayload(
    effectiveSourceScopeSelection,
    notebookSourceTotal,
    sourceQuery.trim() === "" && sources.length === notebookSourceTotal
      ? sources.map((source) => source.id)
      : undefined,
  );
  // 一个库都没挂时**照样提交**空快照（codex #438 R1）。省略这一维等于不冻结它，而
  // 「创建时零个库」本身就是需要被冻结的事实：报告的范围在创建时定格、跨确认与生成两
  // 个阶段复用，中途挂上的库会静默加入一份早已创建的报告；Ask 同理，它的 job 脱离连接
  // 后台跑完，提交与实际检索之间同样有窗口。
  //
  // 代价为零：空快照冻结后 selected 与全集都是 0 ⇒ narrowed 为假 ⇒ 不进限定模式、
  // 不关任何通道、不产生回执，未挂库笔记本的行为逐位不变。
  const currentBaseScope = baseScopePayload(effectiveBaseScopeSelection, mountedBaseIds);
  // 工具条与输入框上方是同一句话的两处显示；共用这**一个**计算结果，不各写各的
  // 字面量 —— 两处分叉在结构上因此不可能发生。
  const retrievalScopeText = retrievalScopeSummary(
    { selected: selectedLocalSourceCount, total: notebookSourceTotal },
    hasMountedBase
      ? { selected: selectedBaseNotebookCount, total: mountedBases.length }
      : null,
  );
  // ask_available 是 get_notebook 的快照;在别的页签/覆盖层增删证据不会刷新它。以下把它
  // 在"重新看到问答框"时与后端对齐,且**双向**——证据增则解禁、证据减则重新禁用(codex
  // PR#334 第5轮 P1:此前只在被禁时重拉,漏了 true→false)。来源增删这条路已各自覆盖
  // (处理轮询 reachedExtracted 分支 / deleteSource 末尾重拉),不在此列。
  //
  // 重取**当前**笔记本详情并替换 currentNotebook —— 单库刷新的唯一取数路径。
  //
  // 两道守卫都不可省(既有惯例):切库之后落地的响应必须整份丢弃,所以先比
  // activeNotebookIdRef,再在函数式更新里复核 `cur.id === nb`(两次之间仍可能切库)。
  async function refreshActiveNotebook() {
    const nb = activeNotebookIdRef.current;
    if (!nb) return;
    const refreshed = await getNotebook(nb);
    if (activeNotebookIdRef.current !== nb) return;
    setCurrentNotebook((cur) => (cur && cur.id === nb ? refreshed : cur));
  }

  // 共享面变更(加/撤群组授权、开启/取消链接分享)之后的统一刷新。
  //
  // ⚠ **两份都要刷,不能只刷集合列表**:这类变更会翻转「未共享门」——本笔记本一旦被
  // 共享出去,它**借来的**参考库当场失效(设计文档 §6.1)。只刷列表的话
  // `currentNotebook.base_notebooks` 还是旧的,检索范围控件会继续列出、并允许勾选一个
  // 这轮根本取不到的参考库,Ask 与深度报告随之提交一份无效(甚至空)的范围,直到用户
  // 重开笔记本才恢复。
  //
  // 两条刷新写的是不相干的 state(集合列表 / 当前笔记本详情),彼此无竞态;顺序取
  // 「先列表后详情」只是让卡片徽标与顶栏尽早一致。详情那条自带切库守卫。
  async function handleSharingChanged() {
    await loadNotebookCollection();
    await refreshActiveNotebook();
  }

  // 无条件重拉一次:既捕获 Memory 页签的证据增,也捕获其删除(true→false)。
  function revalidateAskAvailability() {
    refreshActiveNotebook().catch(reportError);
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
  // 严格推理(深入分析 / 知识图谱)本轮取不取得到图谱。判据必须是「本库有图 **或**
  // 本次勾选的参考库里有带图的」——读聚合的 base_kg_available 会在「本库无图 + 唯一
  // 带图的参考库被取消勾选」时放行一个这轮根本搜不到图的模式(codex #438 R2)。后端的
  // 知识图谱可用性闸早已按库维度收窄,界面不能比它宽。判据只有 kgAvailableForScope
  // 一处实现,别在这里另写一份。
  const kgAvailable = kgAvailableForScope(currentNotebook, selectedBaseNotebookIds);
  const askSession = useAskSession({
    actorId: currentUser?.id ?? null,
    notebookId: currentNotebookId,
    policy: {
      advanced: isAdvanced(uiMode),
      askUnavailable: isAskBlocked(currentNotebook),
      scopeBlocked: sourceScopeBlocked,
      kgAvailable,
      sourceScope: currentSourceScope,
      baseScope: currentBaseScope,
    },
    effects: {
      notify: setToast,
      reportError,
      ensureAskVisible: () => setChatMode("ask"),
    },
  });
  const reportWorkspace = useReportWorkspace({
    actorId: currentUser?.id ?? null,
    notebookId: currentNotebookId,
    active: chatMode === "reports",
    policy: {
      advanced: isAdvanced(uiMode),
      canManageReports: workspaceCapabilities(
        currentNotebook?.access,
        currentUser?.role ?? "",
        currentNotebook?.can_manage_content ?? false,
      ).canManageReports,
      creationDisabled: sourceScopeBlocked,
      sourceScope: currentSourceScope,
      baseScope: currentBaseScope,
    },
    effects: {
      notify: setToast,
      downloadMarkdown: downloadReportMarkdown,
      downloadArchive: downloadReportArchive,
      announceShareLink: async (token) => {
        const link = buildPublicReportLink(token, window.location.origin);
        const copied = await copyTextSafely(link);
        setToast(copied
          ? `分享链接已复制：${link}`
          : `分享链接：${link}（自动复制失败，请手动复制）`);
        // 报告工具栏没有紧邻的只读链接框可供选中，失败时链接由上面这条 toast 带出。
        return copied;
      },
    },
  });

  // ---------------------------------------------------------------------
  // Owner hook 生命周期扇出的唯一入口。这七个 hook——notebookCollection /
  // workspaceExtensions / rootModals / kgWorkspace / reportWorkspace /
  // askSession / sourceLibrary——各自独立拥有一段 workspace/actor 状态；新增
  // 第八个 owner hook 时只改下面这三个函数（回归门
  // workspace-owner-transition-guard 会钉住散落调用点）。
  //
  // activateWorkspaceOwners：登录成功（挂载认证 / AuthGate 两个调用点，顺序
  //   逐字相同）时把 actor 接上全部 owner hook。
  // leaveWorkspaceOwners：回集合页（showCollection）——放弃 workspace、保留
  //   actor，故不含 notebookCollection（它本就不属于单个 workspace）与
  //   sourceLibrary（这条路径改用 beginTransition()，与其余 upload/url busy
  //   态的 ref 重置是同一组，原地保留、不纳入本扇出）。
  // leaveActorOwners：登出（handleLogout）——放弃 actor 本身，因此包含
  //   notebookCollection.leaveActor() 与 rootModals.leaveActor()；
  //   workspaceExtensions/reportWorkspace/kgWorkspace 在这条路径上仍只有
  //   leaveWorkspace()（这几个 hook 没有单独的 leaveActor 语义），
  //   askSession 用它专属的 abortForLogout()。showCollection 里没有调用过
  //   leaveActor/abortForLogout，因此下面 (b) 的“现状放行”分支未触发。
  // ---------------------------------------------------------------------
  function activateWorkspaceOwners(actorId: string): void {
    notebookCollection.activateActor(actorId);
    workspaceExtensions.activateActor(actorId);
    rootModals.activateActor(actorId);
    kgWorkspace.activateActor(actorId);
    reportWorkspace.activateActor(actorId);
    askSession.activateActor(actorId);
    sourceLibrary.activateActor(actorId);
  }

  function leaveWorkspaceOwners(): void {
    rootModals.leaveWorkspace();
    workspaceExtensions.leaveWorkspace();
    askSession.leaveWorkspace();
    reportWorkspace.leaveWorkspace();
    kgWorkspace.leaveWorkspace();
  }

  function leaveActorOwners(): void {
    notebookCollection.leaveActor();
    rootModals.leaveActor();
    workspaceExtensions.leaveWorkspace();
    askSession.abortForLogout();
    reportWorkspace.leaveWorkspace();
    kgWorkspace.leaveWorkspace();
  }

  useEffect(() => {
    if (!kgGraph.open) return;
    const element = kgCanvasRef.current;
    if (!element) return;
    const updateSize = () => {
      const rect = element.getBoundingClientRect();
      setKgSize({
        width: Math.max(320, Math.floor(rect.width)),
        height: Math.max(360, Math.floor(rect.height)),
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
  }, [kgGraph.open]);
  const {
    question,
    turns,
    conversationId,
    sessions,
    asking,
    intentChecking,
    intentReview: askIntentReview,
    sessionLoading,
    pendingQuestion,
    pendingAskedAt,
    pendingMode,
    pendingTrace,
    askModes,
    mode: askMode,
    retrievalEffort: askRetrievalEffort,
    sessionPanelOpen,
    renamingSessionId,
    sessionTitleDraft,
    sessionTitleOverLimit,
    feedbackSent,
    inFlight: askInFlight,
  } = askSession;
  // 「英文双引号 = 整体检索」的即时回执。识别规则有边界(太短、引号太密都不算),
  // 不当场回执的话,没被识别就是一次静默失败:用户以为下了约束,检索侧当普通词处理。
  const askQuotedPhraseHint = useMemo(() => quotedPhraseHint(question), [question]);
  // 前后端都按 trim 后的 Unicode 码点计数；这里只禁用提交，不截断草稿。
  const askQuestionOverLimit = useMemo(
    () => askQuestionLimitHint(question.trim()),
    [question],
  );

  // Answer→Memory 的批量派生仍由跨域 shell 拥有。Ask hook 只发布 turns，不取得
  // Memory API/状态；切用户、切库或切会话时沿用 workspace epoch 丢弃迟到结果。
  useEffect(() => {
    memoryLinksAbortRef.current?.abort();
    const activeNotebook = currentNotebookId;
    const batches = answerIdBatches(turns.map((turn) => turn.response.answer_id));
    if (!activeNotebook || batches.length === 0) {
      // Functional update, not a bare `{}` literal: even if an upstream
      // dependency (e.g. `turns`) were ever unstable again, replacing an
      // already-empty map with a fresh empty object every render would be
      // the other half of a self-triggering effect loop. Only write when
      // there is something to actually clear.
      setMemorySavedAnswers((prev) => (Object.keys(prev).length ? {} : prev));
      return;
    }
    const workspaceEpoch = workspaceEpochRef.current;
    const controller = new AbortController();
    memoryLinksAbortRef.current = controller;
    collectSavedAnswerFlags(
      batches,
      (batch) => fetchAnswerMemoryLinks(activeNotebook, batch, controller.signal),
      controller.signal,
    )
      .then((savedFlags) => {
        if (
          savedFlags === null
          || controller.signal.aborted
          || activeNotebookIdRef.current !== activeNotebook
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

  useEffect(() => {
    const element = chatBodyRef.current;
    if (!element) return;
    element.scrollTop = element.scrollHeight;
  }, [turns.length, asking]);

  // 会话历史面板:点面板外部(或按 Esc)关闭。历史切换按钮排除在外——
  // 交给按钮自己的 onClick 切换,否则 pointerdown 先关、click 再开会「关了又开」。
  useEffect(() => {
    if (!sessionPanelOpen) return;

    function handlePointerDown(event: PointerEvent) {
      const target = event.target;
      if (!(target instanceof Node)) return;
      if (sessionPopoverRef.current?.contains(target)) return;
      if (target instanceof Element && target.closest(".chat-session-toggle")) return;
      askSession.closeSessionPanel();
    }

    function handleKeyDown(event: globalThis.KeyboardEvent) {
      if (event.key === "Escape") askSession.closeSessionPanel();
    }

    window.addEventListener("pointerdown", handlePointerDown);
    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.removeEventListener("pointerdown", handlePointerDown);
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [sessionPanelOpen]);
  // 「将借用参考库「…」推理」该点名谁:同一条判据的名称版。过去这里 join 的是**全部
  // 挂载库名**,会当着用户的面点名一个这次不参与、或压根没建图的库。
  const borrowedBaseNames = useMemo(
    () => borrowedKgBaseNames(currentNotebook, selectedBaseNotebookIds),
    [currentNotebook, selectedBaseNotebookIds],
  );
  // 上面那道门开始按勾选集拦人之后,「取不到图谱」有了两种成因,出路差着真金白银:
  // 挂了带图的参考库、只是这次没勾 → 把勾点回来即可;一个都没挂 → 才真需要为本笔记本
  // 整理一次图谱(整库的模型调用)。不分开就会在只需点回一个复选框时劝用户去跑整理。
  const kgBlockedByScope = kgBlockedByBaseScope(currentNotebook, selectedBaseNotebookIds);
  const currentKgBuildView = kgBuildPresentation(
    currentNotebook?.kg_build,
    currentNotebook?.kg_pending_sources ?? 0,
    Boolean(currentNotebook?.kg_ready),
  );
  // 多领域基准库(Task 14):引用徽章要把 citation/anchor 的 notebook_id 解成人话
  // 库名——id→name 映射从 notebookCollection.rows(自己的库集合)与当前笔记本挂载的
  // 参考库(base_notebooks,覆盖别人创建、不在自己集合里的公共知识库)合并得到,逐 turn
  // 复用同一份而非每条引用各建一次。
  const notebookNames = useMemo(() => {
    const names: Record<string, string> = {};
    for (const nb of notebookCollection.rows) names[nb.id] = nb.name;
    for (const base of currentNotebook?.base_notebooks ?? []) names[base.id] = base.name;
    return names;
  }, [notebookCollection.rows, currentNotebook]);
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
  // owner-only 的 /bases,改用 notebookCollection.rows(list_for_user 已覆盖
  // owner∪reader,涵盖一切可能出现在这里的 memory.notebook_id)里每条自带的
  // base_notebooks,一次性建好全量映射。
  const notebookBasesById: Record<string, MountedBase[]> = useMemo(() => {
    const map: Record<string, MountedBase[]> = {};
    for (const nb of notebookCollection.rows) map[nb.id] = toMountedBases(nb.base_notebooks ?? []);
    return map;
  }, [notebookCollection.rows]);
  const fgData = useMemo(() => {
    const q = kgGraph.search.trim();
    if (!kgGraph.merged) return { nodes: [] as FgNode[], links: [] as FgLink[], searchHitCount: 0 };

    // 搜索模式：服务端返回的命中节点 + 核心图/展开图中对应节点的已知边。
    if (q && kgGraph.searchHits.length > 0) {
      const deg: Record<string, number> = {};
      kgGraph.merged.edges.forEach((e) => { deg[e.source_object_id] = (deg[e.source_object_id] ?? 0) + 1; deg[e.target_object_id] = (deg[e.target_object_id] ?? 0) + 1; });
      const filtered = kgGraph.searchHits.filter((h) => kgGraph.selectedTypes.length === 0 || kgGraph.selectedTypes.includes(h.object_type));
      const nodes: FgNode[] = filtered.map((h) => {
        const degree = deg[h.object_id] ?? 0;
        return { id: h.object_id, name: h.name, type: h.object_type, val: 5 + Math.min(18, degree), degree };
      });
      // 命中节点之间已知的边也渲染出来。
      const keep = new Set(nodes.map((n) => n.id));
      const links: FgLink[] = kgGraph.merged.edges
        .filter((e) => keep.has(e.source_object_id) && keep.has(e.target_object_id))
        .map((e) => ({ source: e.source_object_id, target: e.target_object_id, label: e.edge_type, sourceCount: e.source_count }));
      return { nodes, links, searchHitCount: nodes.length };
    }

    // 搜索框有字但还没拿到结果（loading 中）—— 显示空图占位。
    if (q) return { nodes: [] as FgNode[], links: [] as FgLink[], searchHitCount: 0 };

    // 非搜索模式：渲染合并图（核心 + 展开邻居），支持类型过滤。
    const deg: Record<string, number> = {};
    kgGraph.merged.edges.forEach((e) => { deg[e.source_object_id] = (deg[e.source_object_id] ?? 0) + 1; deg[e.target_object_id] = (deg[e.target_object_id] ?? 0) + 1; });
    const nodes: FgNode[] = kgGraph.merged.nodes
      .filter((n) => kgGraph.selectedTypes.length === 0 || kgGraph.selectedTypes.includes(n.object_type))
      .map((n) => {
        const degree = deg[n.id] ?? 0;
        return { id: n.id, name: kgNodeName(n), type: n.object_type, val: 5 + Math.min(18, degree), degree };
      });
    const keep = new Set(nodes.map((n) => n.id));
    const links: FgLink[] = kgGraph.merged.edges
      .filter((e) => keep.has(e.source_object_id) && keep.has(e.target_object_id))
      .map((e) => ({ source: e.source_object_id, target: e.target_object_id, label: e.edge_type, sourceCount: e.source_count }));
    return { nodes, links, searchHitCount: 0 };
  }, [kgGraph.merged, kgGraph.search, kgGraph.searchHits, kgGraph.selectedTypes]);

  const kgSearching = kgGraph.search.trim().length > 0;
  const kgDenseView = kgGraph.selectedTypes.length === 0 && !kgGraph.search.trim() && fgData.nodes.length > 36;

  const kgTypeCounts = useMemo(() => {
    if (!kgGraph.graph) return [] as Array<{ type: string; label: string; count: number }>;
    const counts = new Map<string, number>();
    kgGraph.graph.nodes.forEach((node) => counts.set(node.object_type, (counts.get(node.object_type) ?? 0) + 1));
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
  }, [kgGraph.graph]);

  const selectedKgNode = useMemo(() => {
    if (!kgGraph.merged || !kgGraph.selectedNodeId) return null;
    // 也在搜索命中中查找（搜索结果节点可能还未进入 kgGraph.merged）
    const fromGraph = kgGraph.merged.nodes.find((node) => node.id === kgGraph.selectedNodeId);
    if (fromGraph) return fromGraph;
    const hit = kgGraph.searchHits.find((h) => h.object_id === kgGraph.selectedNodeId);
    if (hit) return { id: hit.object_id, object_type: hit.object_type, payload: { name: hit.name } } satisfies UnifiedConceptNode;
    return null;
  }, [kgGraph.selectedNodeId, kgGraph.merged, kgGraph.searchHits]);

  const selectedKgEdges = useMemo(() => {
    if (!kgGraph.merged || !kgGraph.selectedNodeId) return [];
    const nodeById = new Map(kgGraph.merged.nodes.map((node) => [node.id, node]));
    return kgGraph.merged.edges
      .filter((edge) => edge.source_object_id === kgGraph.selectedNodeId || edge.target_object_id === kgGraph.selectedNodeId)
      .map((edge) => ({
        ...edge,
        sourceName: kgNodeName(nodeById.get(edge.source_object_id) ?? { id: edge.source_object_id, object_type: "", payload: { name: edge.source_object_id } }),
        sourceType: nodeById.get(edge.source_object_id)?.object_type ?? "",
        targetName: kgNodeName(nodeById.get(edge.target_object_id) ?? { id: edge.target_object_id, object_type: "", payload: { name: edge.target_object_id } }),
        targetType: nodeById.get(edge.target_object_id)?.object_type ?? ""
      }));
  }, [kgGraph.selectedNodeId, kgGraph.merged]);

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
    if (!kgGraph.conceptDetail) return [] as Array<{ type: string; label: string; nodes: KgObject[] }>;
    const byType = new Map<string, KgObject[]>();
    kgGraph.conceptDetail.attached.forEach((node) => {
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
  }, [kgGraph.conceptDetail]);

  function toggleKgType(type: string) {
    const allTypes = kgTypeCounts.map((item) => item.type);
    if (allTypes.length === 0) return;
    if (!kgGraph.selectedTypes.includes(type) && kgGraph.selectedTypes.length + 1 === allTypes.length) {
      kgWorkspace.clearTypes();
      return;
    }
    kgWorkspace.toggleType(type);
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
    if (!kgGraph.open || fgData.nodes.length === 0) return;
    const graph = kgGraphRef.current;
    graph?.d3Force?.("link")?.distance?.(kgDenseView ? 128 : 96);
    graph?.d3Force?.("charge")?.strength?.(kgDenseView ? -310 : -190);
    graph?.d3Force?.("typeBand", kgTypeBandForce(kgSize.width, kgSize.height, kgGraph.selectedTypes));
    graph?.d3ReheatSimulation?.();
    const timer = window.setTimeout(() => {
      fitKgGraphView(450);
    }, 700);
    return () => window.clearTimeout(timer);
  }, [fgData.nodes.length, fgData.links.length, kgDenseView, kgGraph.selectedTypes, kgSize.height, kgSize.width, kgGraph.open]);

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
    if (!rootModals.view("model-service").open) return;
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
  }, [rootModals.view("model-service").open]);

  async function loadNotebookCollection(opts: { guard?: () => boolean } = {}) {
    // The collection hook owns the issued/published watermarks.  The shell keeps
    // this composite read so notebook rows still publish only after the health
    // and configuration sidecars have settled, preserving the existing bundle.
    const listRead = notebookCollection.beginListRead();
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
    notebookCollection.commitListSnapshot(listRead, notebookResponse);
    if (systemConfiguration) {
      setSourceUploadMaxBytes(systemConfiguration.source_upload_max_bytes);
      setSourceUploadMaxFilesPerBatch(systemConfiguration.source_upload_max_files_per_batch);
      setSupportedSourceExtensions(systemConfiguration.supported_source_extensions);
      setParserEngines(systemConfiguration.parser_engines);
      setReportMaxSections(systemConfiguration.report_max_sections);
      setReportMaxSubqueriesPerSection(
        systemConfiguration.report_max_subqueries_per_section,
      );
      setSourceImageMaxBytes(systemConfiguration.source_image_max_bytes);
      setSourceImageMaxPerSource(systemConfiguration.source_image_max_per_source);
      setSourceImagesEnabled(systemConfiguration.source_images_enabled);
      setUserSearchProfileEnabled(systemConfiguration.user_search_profile_enabled);
    }
    if (docTypeOptions.length === 0) {
      fetchDocumentTypes()
        .then((options) => {
          if (!opts.guard || opts.guard()) setDocTypeOptions(options);
        })
        .catch(() => undefined);
    }
  }

  async function loadSourcesPage(
    notebookId: string,
    opts: { page?: number; q?: string; guard?: () => boolean } = {},
  ) {
    await sourceLibrary.loadSourcesPage({ notebookId, ...opts });
  }

  /**
   * 打开一本笔记本时被一起挪动的 owner hook —— **这份列表是唯一的登记点**。
   *
   * 接入前，同一个 owner 的生命周期散在 `openNotebook` 的四个位置上：开头一串
   * `begin`、中间「谁拒绝了就整体放弃」那段分支里的一次 `finish(false)`、成功路径上
   * 的 `commit`，以及 `finally` 里带 `opened` 布尔的另一次 `finish`。新增一个 owner
   * 要在四处各补一笔，漏掉任意一处都是静默失败（owner 永远停在 suspended 态，或者一
   * 次被顶替的切换把状态提交给了新工作区）。现在只需在本列表加一项。
   *
   * 声明顺序 = begin 顺序，逐字保持接入前的顺序（rootModals 必须最先——它同步撤销旧的
   * source-add lease，其 close sink 跑 resetStagedIntake，是暂存文件 / bundle 勾选
   * resolver / 迟到解包世代的唯一清理路径）。settle 由编排器按**逆 begin 序**执行。
   *
   * ⚠ settle 顺序与接入前那份固定顺序（ask → ext → report → kg → rootModals）不同，
   * 这是可证明惰性的：五个 finish 各自只按自己 hook 的 generation ref 守门、只写自己
   * hook 的 state，彼此不互读，同一 React 批次内提交出的渲染结果相同。
   *
   * ⚠ 第三处重排：`commitNotebookSnapshot`（来源库那一步唯一的 commit 相位）相对
   * `applyOpenedNotebook` 里那五个 setState（`setBaseScopeSelection`/
   * `setBackfillingMeta`/`setChatMode`/`setOuterView`/`setCurrentNotebookBases`），
   * 接入前排在它们**之前**（夹在 `setTitleDraft` 与这五个之间——八个 setState 里的
   * 「中间」位置）；现在编排器先跑完整个 `apply()`（八个 setState 全部同步发出）再
   * 进入 commit 循环，`commitNotebookSnapshot` 因此挪到了这五个**之后**。同样可证明
   * 惰性：它只读写 sourceLibrary 自己 hook 的状态，不读也不写这五个 setState 写的
   * page 状态，八者仍在同一次 React 批次内提交，最终渲染结果不变。
   *
   * ⚠ step 顺序也决定 commit 顺序（编排器按声明序逐个 await 有 Promise 返回值的
   * commit）：source-library 必须排在 ask-session 之前——它的 commit
   * （`commitNotebookSnapshot`）是同步的，ask-session 的 commit（`restoreNotebook`）
   * 返回 Promise、会被 await。commit 循环按声明序执行，source-library 排在
   * ask-session 之前保证快照的同步提交先发生；挪到之后，快照提交就会跨过
   * ask-session 那次 await 插入的、可被顶替的微任务窗口。
   */
  function notebookTransitionSteps(
    owner: { actorId: string; notebookId: string; workspaceEpoch: number },
  ): TransitionStep<NotebookOpenLoad, NotebookOpenOutcome>[] {
    return [
      // Root-modal transition synchronously closes the old source-add lease and
      // its close sink runs resetStagedIntake. This is the single cleanup path
      // for staged files, bundle-choice resolvers and late unpack generations.
      transitionStep({
        name: "root-modals",
        begin: () => rootModals.beginWorkspaceTransition(),
        settle: (ticket, outcome) => rootModals.finishWorkspaceTransition(
          ticket,
          outcome && {
            actorId: outcome.actorId,
            notebookId: outcome.notebookId,
            workspaceEpoch: outcome.workspaceEpoch,
          },
        ),
      }),
      transitionStep({
        name: "workspace-extensions",
        begin: () => workspaceExtensions.beginNotebookTransition(owner),
        settle: (ticket, outcome) => workspaceExtensions.finishNotebookTransition(
          ticket,
          outcome !== null,
        ),
      }),
      transitionStep({
        name: "source-library",
        // 来源库的 begin 不发有意义的 ticket（它靠自己的 generation ref 守门），只需要
        // 一个非空返回值区分「已建立」与「拒绝」。每次现铸新对象——不用共享的模块级
        // 哨兵——让不变量③（一个 run 只 settle 自己那批 ticket）对它也成为真命题，
        // 而不是仅仅因为 settle 忽略了 ticket 值才凑巧成立。
        begin: () => {
          sourceLibrary.beginTransition();
          return {};
        },
        // 来源列表由 page 在取数之后**显式提交稳定快照**（红线：不得改成 effect
        // 再拉一次）。它是唯一一个有 commit 相位的 owner。
        commit: (_ticket, { loaded }) => {
          sourceLibrary.commitNotebookSnapshot({ ...owner, page: loaded.sourcesPage });
        },
        settle: () => undefined,
      }),
      transitionStep({
        name: "report-workspace",
        begin: () => reportWorkspace.beginNotebookTransition(),
        settle: (ticket, outcome) => reportWorkspace.finishNotebookTransition(
          ticket,
          outcome !== null,
        ),
      }),
      transitionStep({
        name: "kg-workspace",
        begin: () => kgWorkspace.beginNotebookTransition(),
        settle: (ticket, outcome) => kgWorkspace.finishNotebookTransition(
          ticket,
          outcome && outcome.notebook,
        ),
      }),
      transitionStep({
        name: "ask-session",
        begin: () => askSession.beginNotebookTransition(owner),
        commit: (ticket) => askSession.restoreNotebook(ticket),
        settle: (ticket, outcome) => askSession.finishNotebookTransition(
          ticket,
          outcome !== null,
        ),
      }),
    ];
  }

  /** `enter` 相位：全部 begin 成功之后、取数之前的一次性清理。被拒绝时不执行。 */
  function enterNotebookTransition() {
    memoryLinksAbortRef.current?.abort();
    memoryLinksAbortRef.current = null;
    activeNotebookIdRef.current = null;
    setMemoryAnswerId(null);
    setMemorySavedAnswers({});
  }

  /** `load` 相位：notebook + 首个来源页**并行一次**，绝不改成 effect 再拉一次。 */
  async function openNotebookSnapshot(notebookId: string): Promise<NotebookOpenLoad> {
    // A DELETE may complete while this transition deliberately holds the active
    // notebook id at null. Keep reading both snapshots until one full read sees
    // a stable delete generation; tombstones alone cannot repair ask_available.
    const [notebook, sourcesPage] = await readStableSourceSnapshot(
      () => sourceLibrary.deleteGeneration(notebookId),
      () => Promise.all([
        getNotebook(notebookId),
        listSources(notebookId, 0, SOURCES_PAGE_SIZE),
      ]),
    );
    return { notebook, sourcesPage };
  }

  /** `apply` 相位：同步写入 page 自己持有的工作区视图状态，产出交给各 owner 的结果。 */
  function applyOpenedNotebook(
    owner: { actorId: string; notebookId: string; workspaceEpoch: number },
    notebook: NotebookSummary,
  ): NotebookOpenOutcome {
    activeNotebookIdRef.current = owner.notebookId;
    setCurrentNotebookId(owner.notebookId);
    setCurrentNotebook(notebook);
    setTitleDraft(notebook.name);
    // 参考库的选择状态挂在**上一个**笔记本的挂载集上，不一起重置就会把旧库 id 带进
    // 新笔记本的请求（422），或反过来悄悄沿用旧的排除项。
    setBaseScopeSelection(defaultBaseScopeSelection());
    setBackfillingMeta(Boolean(notebook.paper_meta_backfilling));
    setChatMode("ask");
    setOuterView("notebooks");
    setCurrentNotebookBases([]);
    return { ...owner, notebook };
  }

  /** `conclude` 相位：所有 owner commit 之后的最后一次守门 + 历史/滚动副作用。 */
  function concludeOpenNotebook(
    notebookId: string,
    historyMode: "push" | "replace" | null,
    isCurrent: () => boolean,
  ): boolean {
    if (!isCurrent()) return false;
    // "none" = 挂载还原 / popstate:浏览器已经把 URL 摆对了,再写一次只会多一个
    // 死条目(用户按返回没反应)。默认 "push" 让返回键能退出 notebook。
    if (historyMode === "push") {
      window.history.pushState(null, "", notebookHash(notebookId));
    } else if (historyMode === "replace") {
      window.history.replaceState(null, "", notebookHash(notebookId));
    }
    window.scrollTo(0, 0);
    return true;
  }

  /**
   * 作废在途打开的 guard 与它的忙碌态。
   *
   * `openNotebook` 之外的每一处 `workspaceEpochRef` 自增都必须紧跟着调用它:那些导航
   * 把在途的那次 open 顶死了(`isCurrent()` 从此恒假,永远不会 apply/conclude),可它的
   * guard 还挂着——不清掉的话,同一本笔记本之后的点击会被这个僵尸 guard 吞到那次死跑
   * 的请求收尾为止(请求挂死就是永远打不开),卡片还一直挂着假的「打开中…」。
   *
   * 与 `openNotebook` 自己 `finally` 里的 settle 不冲突:settle 只清自己那一代,guard
   * 已被这里清空时 `settleOpen(null, ·)` 保持 null,state 也已经是 null,幂等。
   */
  function invalidateNotebookOpenGuard() {
    notebookOpenGuardRef.current = null;
    setOpeningNotebookId(null);
  }

  async function openNotebook(
    notebookId: string,
    history: "push" | "none" = "push",
    actorIdOverride?: string,
    // `coalesce: false` = 这次调用**不是**重复点击,而是一个新的目的地(见下面各调用点
    // 的理由)。跳过的只有合并早退这一件事:仍然自增 epoch 顶替在途那次、仍然
    // beginOpen 登记 guard,后续同 id 同 intent 的卡片点击照样能合并到它上面。
    // `intent` 默认 "open"(普通打开);目的地不同的调用(如 openNotebookMemory)传
    // 自己的 intent——合并判据从「同 id」升级为「同 id + 同 intent」(见
    // notebook-open-guard.ts 顶部语义⑤),不必再靠 coalesce:false 强制放行。
    opts?: { coalesce?: boolean; intent?: string },
  ): Promise<boolean> {
    // 同库同 intent 连点合并:在途的是同一本笔记本、同一代、同一个目的地,就直接放弃,
    // 什么都不做——不重置任何状态、不发请求。已经在飞的那一次自己会收尾。异 id、
    // 异 intent(语义⑤,换目的地)必须放行,被外部导航顶过代的僵尸 guard 也必须放行
    // (见 notebook-open-guard.ts 顶部说明)。
    // ⚠ 这一句必须留在函数体第一位:挪到下面的 `++workspaceEpochRef.current` 之后,
    // 双击就会自己把自己顶死(epoch 已经变了 → 不合并 → 但 isCurrent 恒假),那本库
    // 从此永远打不开。
    if (
      (opts?.coalesce ?? true)
      && shouldCoalesceOpen(notebookOpenGuardRef.current, notebookId, workspaceEpochRef.current, opts?.intent ?? "open")
    ) {
      return false;
    }
    const intent = opts?.intent ?? "open";
    const historyMode = history === "push"
      ? historyModeForTransition(currentNotebookId, notebookId)
      : null;
    // ⚠ 这段 page 自己的清理提到了全部 owner begin 之前（接入前它夹在 rootModals 的
    // begin 与 askSession 的 begin 之间）。可证明惰性：六个 begin 无一读取这些 ref /
    // state，rootModals 的 begin 只撤销 modal lease，它的 close sink
    // （resetStagedIntake）也不碰 uploadBusy / urlBusy / uploadRequestOwnerRef。
    titleSaveOperationRef.current = null;
    setTitleSaveInFlight(false);
    closeKnowhow();
    uploadRequestOwnerRef.current = null;
    urlRequestOwnerRef.current = null;
    setUploadBusy(false);
    setUrlBusy(false);
    const workspaceEpoch = ++workspaceEpochRef.current;
    // guard 借用同一个 workspaceEpoch 当自己的代号:每次调用都拿到独一无二的值,
    // 天然满足「settle 只清自己那一代」,不必另起一套计数器。
    notebookOpenGuardRef.current = beginOpen(notebookId, workspaceEpoch, intent);
    setOpeningNotebookId(notebookId);
    const transitionOwner = {
      actorId: actorIdOverride ?? currentUser?.id ?? "",
      notebookId,
      workspaceEpoch,
    };
    const isCurrent = () => workspaceEpochRef.current === workspaceEpoch;
    try {
      const result = await runNotebookTransition<NotebookOpenLoad, NotebookOpenOutcome>({
        steps: notebookTransitionSteps(transitionOwner),
        enter: enterNotebookTransition,
        load: () => openNotebookSnapshot(notebookId),
        isCurrent,
        apply: ({ notebook }) => applyOpenedNotebook(transitionOwner, notebook),
        conclude: () => concludeOpenNotebook(notebookId, historyMode, isCurrent),
      });
      return result.status === "committed";
    } finally {
      // 只有本代的 settle 才清 guard/state——被更晚一次点击顶替的 guard 原样保留,
      // 这次(迟到的)收尾不许抹掉新一次点击刚建立的忙碌态。
      notebookOpenGuardRef.current = settleOpen(notebookOpenGuardRef.current, workspaceEpoch);
      if (notebookOpenGuardRef.current === null) {
        setOpeningNotebookId(null);
      }
    }
  }

  async function openNotebookMemory(notebookId: string, actorIdOverride?: string) {
    // 传 "none" 让 openNotebook 别写 history,自己下面这次 replaceState 独占写入——
    // 与本函数改动前的净效果逐字一致(旧代码是 replace 再 replace)。
    // intent: "memory" —— 目的地差异现在由 intent 表达,不再用 coalesce:false 硬顶替:
    // 普通打开(intent "open")在途时点「N 条记忆」照常顶替它(intent 不同,语义⑤),
    // 不会静默丢弃用户「先点主体、再点记忆」的第二次点击;而连点同一张卡片的「N 条
    // 记忆」(intent 都是 "memory")会被合并,不会像 coalesce:false 那样每次连点都
    // 叠出一整套 getNotebook+listSources——这正是这颗 guard 本来要防的过载。
    if (!await openNotebook(notebookId, "none", actorIdOverride, { intent: "memory" })) return;
    setChatMode("memory");
    window.history.replaceState(null, "", memoryHash(notebookId));
  }

  // --- Pending center: precise deep-link per item type --------------------
  async function openPendingItem(item: PendingItem) {
    // 共享申请是**组维度**的待办,没有 notebook——直接打开独立群组页,组管理员在
    // 「待审批申请」区批准/驳回。放在 openNotebook 之前:它没有 notebook_id 可开。
    if (item.type === "share_request") {
      showGroups({ groupId: item.group_id || "", tab: "requests" }, "push");
      return;
    }
    // coalesce:false —— 待办项的目的地是报告/治理/索引里的某个具体位置,不是「再点一次
    // 同一张卡片」。被合并掉会静默丢弃这次深链意图,用户停在原视图。
    // 不改用 intent:不同待办项(report_outline/governance/index)可能指向同一本库、
    // 且这个函数本身没有区分它们的 intent 字符串可用——同库同类型的两条不同待办项
    // 之间 intent 也无法安全区分「是不是同一个目的地」,必须继续用 coalesce:false
    // 强制顶替。
    if (!item.notebook_id || !await openNotebook(item.notebook_id, "push", undefined, { coalesce: false })) return;
    if (item.type === "report_outline") {
      switchChatMode("reports");
      if (item.report_id) reportWorkspace.focusReport(item.report_id);
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
    // coalesce:false —— 同上,已完成项的目的地是来源面板或知识图谱,不是重复点击。
    // 不改用 intent:同一本库连续两条已完成项(比如两次 index 完成通知)可能同 kind,
    // intent 无法安全区分「这次和在途那次是不是同一个目的地」,必须继续用
    // coalesce:false 强制顶替。
    if (!await openNotebook(d.notebook_id, history, undefined, { coalesce: false })) return;
    // 论文元数据补全完成应停在来源面板(设计稿 §3.3:作者/机构就在来源列表与详情
    // 里),别把用户甩进知识图谱。kind 缺省视作索引完成,索引路径行为逐字不变。
    if (doneItemDestination(d.kind) === "kg") {
      await openKgView(undefined, d.notebook_id);
    }
  }

  function showCollection() {
    leaveWorkspaceOwners();
    closeKnowhow();
    workspaceEpochRef.current += 1;
    invalidateNotebookOpenGuard();
    sourceLibrary.beginTransition();
    uploadRequestOwnerRef.current = null;
    urlRequestOwnerRef.current = null;
    setUploadBusy(false);
    setUrlBusy(false);
    memoryLinksAbortRef.current?.abort();
    memoryLinksAbortRef.current = null;
    activeNotebookIdRef.current = null;
    setCurrentNotebookId(null);
    setCurrentNotebook(null);
    titleSaveOperationRef.current = null;
    setTitleSaveInFlight(false);
    setBaseScopeSelection(defaultBaseScopeSelection());
    setTitleDraft("");
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

  function showGroups(
    target: { groupId?: string; tab?: GroupPageTab } = {},
    history: "push" | "replace" | "none" = "replace",
  ) {
    showCollection();
    const next: { groupId: string; tab: GroupPageTab } = {
      groupId: target.groupId || "",
      tab: target.tab || "notebooks",
    };
    setGroupNavigation(next);
    setOuterView("groups");
    window.history[history === "push" ? "pushState" : "replaceState"](
      null,
      "",
      groupsHash(next.groupId, next.tab),
    );
    window.scrollTo(0, 0);
  }

  async function handleMemorySaved(memory: MemoryRecord) {
    if (!memoryAnswerId || memory.notebook_id !== currentNotebookId) return;
    const savedAnswerId = memoryAnswerId;
    memoryLinksAbortRef.current?.abort();
    memoryLinksAbortRef.current = null;
    setMemorySavedAnswers((previous) => ({ ...previous, [savedAnswerId]: true }));
    rootModals.requestClose("memory-save", "button");
    setToast("已保存到记忆");
    await loadNotebookCollection();
    if (currentNotebookId) {
      const refreshed = await getNotebook(currentNotebookId);
      if (activeNotebookIdRef.current === currentNotebookId) setCurrentNotebook(refreshed);
    }
  }

  async function saveInlineNotebookName() {
    if (!currentNotebook || titleSaveInFlight) return;
    const notebookId = currentNotebook.id;
    const workspaceEpoch = workspaceEpochRef.current;
    const nextName = normalizedNotebookName(titleDraft);
    setTitleDraft(nextName);
    if (nextName === currentNotebook.name) return;
    const operation = {};
    titleSaveOperationRef.current = operation;
    setTitleSaveInFlight(true);
    try {
      const updated = await notebookCollection.renameNotebook(notebookId, nextName);
      if (!updated || workspaceEpochRef.current !== workspaceEpoch
        || activeNotebookIdRef.current !== notebookId) return;
      setCurrentNotebook(updated);
      setTitleDraft(updated.name);
    } catch (error) {
      if (workspaceEpochRef.current === workspaceEpoch
        && activeNotebookIdRef.current === notebookId) {
        setTitleDraft(currentNotebook.name);
        reportError(error);
      }
    } finally {
      if (titleSaveOperationRef.current === operation
        && workspaceEpochRef.current === workspaceEpoch
        && activeNotebookIdRef.current === notebookId) setTitleSaveInFlight(false);
      if (titleSaveOperationRef.current === operation) titleSaveOperationRef.current = null;
    }
  }

  function submitNotebookEditor(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!notebookCollection.editor?.target || !notebookCollection.editor) return;
    const formData = new FormData(event.currentTarget);
    const splitLines = (value: string) =>
      value.split(/[\n;,，；]/).map((s) => s.trim()).filter(Boolean);
    const nextPipelineId = notebookCollection.editor.selectedPipelineId || null;
    if (
      notebookCollection.editor.canManageContent
      && !indexingPipelineIdsEqual(
        notebookCollection.editor.indexingPipeline?.pipeline_id,
        nextPipelineId,
      )
    ) {
      const selectedOption = selectedIndexingPipelineOption(
        notebookCollection.editor.indexingPipeline,
        nextPipelineId,
      );
      if (!window.confirm(indexingPipelineConfirmMessage(selectedOption))) return;
    }
    const patch: NotebookEditorPatch = {
      name: formData.get("name"),
      purpose: formData.get("purpose"),
      primary_domain: formData.get("primary_domain"),
      target_users: String(formData.get("target_users") || ""),
      access_scope: String(formData.get("access_scope") || ""),
      expected_questions: splitLines(String(formData.get("expected_questions") || "")),
      source_types: splitLines(String(formData.get("source_types") || "")),
      taxonomy: splitLines(String(formData.get("taxonomy") || "")),
      indexing_pipeline_id: nextPipelineId,
    };
    void notebookCollection.saveEditor(patch);
  }

  async function presentNotebookEditor(notebookId: string) {
    const lease = rootModals.issue("notebook-editor", rootModals.captureActorOwner());
    if (!lease) return;
    if (await notebookCollection.openEditor(notebookId)) rootModals.publish(lease);
  }

  async function openReadOnlyNotebookSettings() {
    const notebookId = currentNotebook?.id ?? null;
    // await 之前冻结 modal owner(codex #602 R6 P2):只比 notebook id 会放过
    // A→B→A——回到同一本库时 workspace 世代已换,迟到响应不该在新世代里开弹窗。
    // 冻结的 owner 交给 openInfoModal → rootModals.open 按世代拒绝陈旧认领。
    const owner = rootModals.captureWorkspaceOwner() ?? rootModals.captureActorOwner();
    // 先按 NotebookSummary 派生兜底文案,再尽力换成实时投影:摘要只有 pending
    // 布尔,分不出「重建中」与「重建失败」——失败后 job authority 保留(那是重试
    // 入口),只看摘要会对只读成员永远显示「正在重建」(codex #602 R3 P2)。
    // 投影端点 reader 可读;取不到就用兜底,一次网络抖动不该挡住设置说明。
    let summary = notebookIndexingPipelineReadOnlySummary(currentNotebook);
    if (notebookId) {
      try {
        const projection = await fetchNotebookIndexingPipeline(notebookId);
        if (activeNotebookIdRef.current !== notebookId) return;
        summary = indexingPipelineReadOnlySummary(projection);
      } catch {
        if (activeNotebookIdRef.current !== notebookId) return;
      }
    }
    openInfoModal({
      title: "设置",
      message: `当前笔记本为只读。当前索引管线：${summary.label}。${summary.detail} 参考库挂载与链接分享由库主管理。`,
      actions: [],
    }, owner);
  }

  async function presentNotebookDelete(notebookId: string) {
    const lease = rootModals.issue("notebook-delete", rootModals.captureActorOwner());
    if (!lease) return;
    if (await notebookCollection.openDelete(notebookId)) rootModals.publish(lease);
  }

  // 三个笔记本菜单动作的 curry 工厂:notebookCollection.menu.notebook 只在 JSX 渲染
  // 这一层(未跨闭包边界)被 TypeScript 窄化为非空，deferred 到 onClick 闭包内部
  // 再读同一条属性链会丢失窄化；改成在渲染时把 id 读出来、绑进闭包捕获的普通字符
  // 串参数，闭包内部就不用再碰可能为 null 的属性链。
  function editMenuNotebook(notebookId: string) {
    return () => { void presentNotebookEditor(notebookId); notebookCollection.closeMenu(); };
  }

  function deleteMenuNotebook(notebookId: string) {
    return () => { void presentNotebookDelete(notebookId); notebookCollection.closeMenu(); };
  }

  function leaveMenuNotebook(notebookId: string) {
    return () => {
      notebookCollection.closeMenu();
      leaveNotebook(notebookId)
        .then(() => loadNotebookCollection())
        .then(() => setToast("已退出只读共享"))
        .catch(reportError);
    };
  }

  // Stage selected files so the user can pick a document type per file before
  // uploading (auto-detect by default).
  function stageFiles(event: ChangeEvent<HTMLInputElement>) {
    const all = Array.from(event.target.files || []);
    event.target.value = "";
    stageIncomingFiles(all).catch(reportError);
  }

  /** 打开「添加来源」弹窗：清掉「用户已主动关闭」的标记，让后续入列可以正常揭示弹窗。 */
  function openSourceModal() {
    if (!rootModals.open("source-add", rootModals.captureWorkspaceOwner())) return;
    sourceModalDismissedRef.current = false;
  }

  /** 弹窗内所有暂存态的统一清空点（新建笔记本 / 关闭弹窗 / 清空 / 上传成功共用）。
   *  世代先 ++ 再清空：还在飞的异步解包链此后落盘时一律比对失败、整条结果丢弃
   *  （见 bundleIntakeGenerationRef）。同时结清挂起的 bundleChoice 勾选面板——
   *  世代递增只挡了迟到落盘，挂起的勾选面板、它的 resolver 与忙碌栈帧不会因此
   *  自动消失；不结清就会让「清空」「上传成功」这类直接调用本函数的入口，让用户
   *  已经点了确认的勾选被 stageBundleCandidates 的世代比对静默丢弃
   *  （codex #518 R4 P2）。切库先由 root-modal transition 同步撤销 source-add
   *  lease，再经同一个 close sink 调本函数；所有清理因此共用这一处世代递增与
   *  bundleChoice 结清，是结构性不变量。 */
  function resetStagedIntake() {
    bundleIntakeGenerationRef.current += 1;
    updateStaged(emptyStagedList());
    setStagedSkipped([]);
    setStagedWarnings([]);
    setBundleReceipts([]);
    cancelBundleChoice();
  }

  /** 关闭「添加来源」弹窗。× 与遮罩都经 coordinator 的同一 close sink，
   *  因而必定结清 bundle choice、暂存批次和迟到解包 generation。 */
  function closeSourceModal(reason: "button" | "backdrop" = "button") {
    rootModals.requestClose("source-add", reason);
  }

  // 选择器与拖放共用的入列逻辑（**同步**部分）。被跳过的文件（类型不支持/超大小/
  // 超批量）写进 stagedSkipped 在弹窗内持久展示，绝不静默丢弃；注册表准入的 .zip
  // 与 PDF/PPTX 一样作为普通来源原样入列，由后端解包、解析并持久化相对图片。
  //
  // 合并从 stagedRef.current 起算并经 updateStaged 一次性写回：三条并行数组的等长
  // 不变量在 mergeStagedFiles 里维护，跨 await 的异步链不会再读到发起那一刻的旧闭包
  // （旧形态下 pdf + zip 同批时 pdf 会被 zip 的入列静默覆盖掉）。
  //
  // `expectedGeneration`：调用方是异步链（文件夹遍历/压缩包解包）时传入它在自己
  // 起跑那一刻捕获的世代；直接由用户操作触发（文件选择器/拖放普通文件）时省略——
  // 那类调用本身就在当前世代，无需检查。世代已变（用户取消/切库）时整批结果连同
  // 跳过记录一起静默丢弃，不落盘、不弹窗（评审 F2）。
  function stageIncomingFilesSync(
    all: File[], expectedGeneration?: number,
  ): { added: File[]; skipped: SkippedStagedFile[]; duplicates: File[]; bundles: File[] } {
    if (all.length === 0) return { added: [], skipped: [], duplicates: [], bundles: [] };
    if (expectedGeneration !== undefined && expectedGeneration !== bundleIntakeGenerationRef.current) {
      return { added: [], skipped: [], duplicates: [], bundles: [] };
    }
    const { accepted: picked, skipped, bundles } = classifyStagedFiles(all, {
      supportedExtensions: supportedSourceExtensions,
      legacyOfficeExtensions: LEGACY_OFFICE_EXTENSIONS,
      maxBytes: sourceUploadMaxBytes,
      supportedHint: supportedSourceUserHint,
    });
    // 追加而非覆盖（"继续添加文件"语义）；按 name+size 去重，避免重复入列。
    const merge = mergeStagedFiles(stagedRef.current, picked, {
      maxFilesPerBatch: sourceUploadMaxFilesPerBatch,
    });
    const allSkipped = [...skipped, ...merge.skipped];
    appendStagedSkipped(allSkipped);
    if (merge.added.length === 0) {
      return { added: [], skipped: allSkipped, duplicates: merge.duplicates, bundles };
    }
    updateStaged(merge.next);
    // 用户已经主动关掉弹窗时不再弹回来（异步解包链完成得比用户慢是常态）。
    if (!sourceModalDismissedRef.current) {
      rootModals.open("source-add", rootModals.captureWorkspaceOwner());
    }
    // 对新增的文本类文件做内容检测，预填类型下拉（异步，不阻塞 UI；用户仍可改）。
    void detectStagedTypes(merge.added);
    return { added: merge.added, skipped: allSkipped, duplicates: merge.duplicates, bundles };
  }

  /** 入列文件。调用方必须 `await` 或 `.catch(reportError)`，因为拖入文件夹的兼容
   *  管线仍会沿用这个异步边界。注册表准入的 ZIP 已进入 `accepted`，不会出现在
   *  `bundles`；保留后半段只为兼容旧分类结果，不能把正常 ZIP 改回浏览器解包。 */
  async function stageIncomingFiles(all: File[], expectedGeneration?: number): Promise<void> {
    const generation = expectedGeneration ?? bundleIntakeGenerationRef.current;
    const { added, bundles } = stageIncomingFilesSync(all, expectedGeneration);
    if (added.length > 0) {
      const warnings = await scanStandaloneMarkdownImageWarnings(added);
      if (generation === bundleIntakeGenerationRef.current && warnings.length > 0) {
        const liveFiles = stagedRef.current.files;
        setStagedWarnings((previous) => mergeLiveStagedFileWarnings(previous, warnings, liveFiles));
      }
    }
    if (bundles.length > 0) await ingestBundleSources(bundles);
  }

  // 拖放必须由我们接管：不 preventDefault 的话文件会落在铺满拖放区的原生
  // <input type=file accept=…> 上，浏览器按 accept **静默**过滤不支持的文件——
  // stageIncomingFiles 根本收不到它们，用户也就得不到任何「已跳过」提示。
  // 累积追加（按 name+reason 去重）：弹窗开着期间的警告不能被下一次「全合法」的
  // 添加静默清掉（codex #485 R1 P2）；只有「知道了」/清空/关闭/上传成功才清。
  function appendStagedSkipped(entries: SkippedStagedFile[]) {
    if (entries.length === 0) return;
    setStagedSkipped((previous) => {
      const combined = [...previous];
      for (const item of entries) {
        if (!combined.some((existing) => existing.name === item.name && existing.reason === item.reason)) {
          combined.push(item);
        }
      }
      return combined;
    });
  }

  // preventDefault 必须**无条件**先做（含禁用态）：不取消默认行为，落在页面上的
  // drop 会让浏览器直接导航打开该文件，整批已暂存文件随页面一起丢掉。是否入列
  // 由禁用判断在取消默认行为**之后**决定（codex #485 R1 P1）。
  function handleStageDragOver(event: ReactDragEvent<HTMLElement>) {
    event.preventDefault();
  }
  function handleStageDrop(event: ReactDragEvent<HTMLElement>) {
    event.preventDefault();
    setDropZoneDragActive(false);
    if (sourceFilePickerDisabled) {
      // 禁用态（容量满/批次满/配置加载中/zip 或文件夹处理中）下拖入的文件不入列，
      // 但必须在「已跳过」列表逐条留痕（codex #485 R2 P2），不能无声消失。文件夹本身
      // 在 dataTransfer.files 里没有条目（浏览器不为目录生成 File），这里只报得出
      // 同批里被拖入的普通文件，与改动前一致。
      const dropped = Array.from(event.dataTransfer?.files ?? []);
      appendStagedSkipped(dropped.map((file) => ({ name: file.name, reason: sourceFilePickerSkipReason })));
      return;
    }
    const entries = extractDroppedEntries(event.dataTransfer?.items ?? null);
    // entries 为空数组说明 webkitGetAsEntry 每一项都返回了 null（个别浏览器/来源在
    // 某些拖放形态下如此），此时 dataTransfer.files 仍可能有内容——不回退就等于把
    // 整批拖入静默吞掉。判据必须是「拿到了至少一个条目」而不是「API 可用」。
    if (entries !== null && entries.length > 0) {
      dispatchDroppedEntries(entries).catch(reportError);
      return;
    }
    // File and Directory Entries API 不可用（极少见的浏览器/环境）：回退到改动前的
    // 扁平文件列表——文件夹在这条路径下本来就拿不到内容，不是新的回归。
    stageIncomingFiles(Array.from(event.dataTransfer?.files ?? [])).catch(reportError);
  }

  // `webkitGetAsEntry` 是 File and Directory Entries API 的 WebKit 前缀方法，主流
  // 浏览器都实现但不是强制标准；任一项拿不到就整体判「不可用」，统一回退扁平文件列表，
  // 不做「部分用 entry、部分用 file」的混合语义。
  function extractDroppedEntries(items: DataTransferItemList | null): FileSystemEntry[] | null {
    if (!items) return null;
    const entries: FileSystemEntry[] = [];
    for (const item of Array.from(items)) {
      if (typeof item.webkitGetAsEntry !== "function") return null;
      const entry = item.webkitGetAsEntry();
      if (entry) entries.push(entry);
    }
    return entries;
  }

  // 拖入的条目按到达顺序串行处理：文件夹递归遍历（见 ingestDroppedDirectory）；普通
  // 文件先攒起来，最后一次性交给 stageIncomingFiles；其中的 .zip 原样上传后台。
  async function dispatchDroppedEntries(entries: FileSystemEntry[]) {
    const plainFiles: File[] = [];
    const unreadable: SkippedStagedFile[] = [];
    for (const entry of entries) {
      if (entry.isDirectory) {
        await ingestDroppedDirectory(entry as FileSystemDirectoryEntry);
        continue;
      }
      if (!entry.isFile) continue;
      try {
        const file = await new Promise<File>((resolve, reject) => {
          (entry as FileSystemFileEntry).file(resolve, reject);
        });
        plainFiles.push(file);
      } catch {
        // 读取失败（极少见，如条目在拖放后被移动/删除）：这一项不阻塞同批其余条目，
        // 但必须逐条留痕——静默吞掉就是「拖了没反应」，用户无从知道少了哪个文件。
        unreadable.push({ name: entry.name, reason: BUNDLE_READ_FAILED_REASON });
      }
    }
    appendStagedSkipped(unreadable);
    if (plainFiles.length > 0) await stageIncomingFiles(plainFiles);
  }

  // 忙碌文案的栈式持有：push/pop 成对，pop 后恢复外层那一帧的文案而不是一律清零
  // （bundleBusyStackRef 声明处有完整说明）。
  function pushBundleBusy(label: string) {
    bundleBusyStackRef.current.push(label);
    setBundleBusyLabel(label);
  }
  function replaceBundleBusy(label: string) {
    const stack = bundleBusyStackRef.current;
    if (stack.length > 0) stack[stack.length - 1] = label; else stack.push(label);
    setBundleBusyLabel(label);
  }
  function popBundleBusy() {
    bundleBusyStackRef.current.pop();
    syncBundleBusyLabel();
  }
  function syncBundleBusyLabel() {
    const stack = bundleBusyStackRef.current;
    setBundleBusyLabel(stack.length > 0 ? stack[stack.length - 1] : null);
  }

  // 一批里可能有多个 zip（多选文件选择器 / 一次拖放多个 zip）：严格串行处理，避免
  // 第二个 zip 的「多个 markdown 需勾选」弹窗覆盖第一个还没确认的选择
  // （bundleChoiceResolveRef 声明处有完整说明）。
  async function ingestBundleSources(files: File[]) {
    for (const file of files) {
      await ingestZipFile(file);
    }
  }

  async function ingestZipFile(file: File) {
    // 这条链自己的世代：起跑那一刻捕获，解压这段真实 I/O 跑完后重新比对
    // bundleIntakeGenerationRef——不等就说明用户在解压期间取消/切库了，整条
    // 结果（含跳过记录）静默丢弃，不落盘（评审 F2）。
    const generation = bundleIntakeGenerationRef.current;
    pushBundleBusy("解析压缩包…");
    try {
      const result = await unpackZipFile(file, bundleCapsFrom(sourceUploadMaxBytes));
      if (generation !== bundleIntakeGenerationRef.current) return;
      if (!result.ok) {
        appendStagedSkipped([{ name: file.name, reason: bundleErrorMessage(result.error) }]);
        return;
      }
      await handleBundleFiles(file.name, result.files, generation);
    } catch {
      // 读字节/解包本身抛异常（条目已被移动、权限变化、内存不足…）：这一个压缩包
      // 当作没进来，逐条留痕而不是让整条链带着一个未处理 rejection 静默死掉。
      if (generation !== bundleIntakeGenerationRef.current) return;
      appendStagedSkipped([{ name: file.name, reason: BUNDLE_READ_FAILED_REASON }]);
    } finally {
      popBundleBusy();
    }
  }

  // 递归遍历一个被拖入的文件夹：含 markdown 时按 zip 同一配对/内联流程处理；不含
  // markdown 时保持既有拖拽行为——文件夹里的文件按普通文件逐个入列，走常规的
  // 类型/大小/批量校验（红线：不含 md 的文件夹不能回归成「拖了没反应」）。
  async function ingestDroppedDirectory(entry: FileSystemDirectoryEntry) {
    // 同上：这条链自己的世代，遍历/读字节这段真实 I/O 之后重新比对再决定是否
    // 落盘（评审 F2）。
    const generation = bundleIntakeGenerationRef.current;
    pushBundleBusy("读取文件夹…");
    try {
      // 总字节预算是**零 I/O** 预检：只累加条目的 File.size 元数据，超限就地止损，
      // 一个字节都不读（zip 那条路由 parseZipBundle 的解压护栏把关，这里是它的对偶）。
      // bundleDirTotalBytesLimit 恒返回有限数（复用 zip 输入侧同一个绝对顶，但不加
      // 容器余量）——顶配部署下裸 bundleTotalBytesLimit 会放行 4 GiB，
      // readDirectoryAsBundleFiles 的 Promise.all 会把它们整读进内存耗死标签页
      // （codex #518 R6 P2）。
      const totalLimit = bundleDirTotalBytesLimit(sourceUploadMaxBytes);
      const { entries, truncated, overBudget } = await collectDirectoryFiles(entry, {
        maxTotalBytes: totalLimit,
      });
      if (generation !== bundleIntakeGenerationRef.current) return;
      if (truncated) {
        appendStagedSkipped([{ name: entry.name, reason: directoryTruncatedMessage() }]);
        return;
      }
      if (overBudget) {
        // bundleDirTotalBytesLimit 恒返回有限数（不像裸 bundleTotalBytesLimit 那样
        // 可能是 null），totalLimit 因此不需要再做 `?? 0` 类型收窄。
        appendStagedSkipped([{ name: entry.name, reason: directoryTooLargeMessage(totalLimit) }]);
        return;
      }
      // 只看路径（零 I/O）判断有没有 markdown：不含 md 的文件夹没必要把里面可能
      // 几十上百个文件的全部字节都读一遍，只为了发现用不上。
      if (!directoryHasMarkdown(entries)) {
        // 必须 await：保持文件夹 intake 的异步生命周期完整；其中的 zip 作为普通来源
        // 原样入列，不再由浏览器解包。
        await stageIncomingFiles(entries.map((item) => item.file), generation);
        return;
      }
      replaceBundleBusy("读取文件夹图片…");
      const bundleFiles = await readDirectoryAsBundleFiles(entries);
      if (generation !== bundleIntakeGenerationRef.current) return;
      await handleBundleFiles(entry.name, bundleFiles, generation);
    } catch {
      // 遍历/读字节失败：同上，整个文件夹当作没进来并留痕。
      if (generation !== bundleIntakeGenerationRef.current) return;
      appendStagedSkipped([{ name: entry.name, reason: DIRECTORY_READ_FAILED_REASON }]);
    } finally {
      popBundleBusy();
    }
  }

  // 虚拟文件集（拖入文件夹遍历产出；旧 ZIP 解包入口仅为兼容保留）→ 按 markdown 数量分派：零个报错、一个
  // 直接处理、多个弹出勾选并挂起，直到用户确认/取消（设计文档 §3.1 第 2 条）。
  // `generation` 是调用方（ingestZipFile/ingestDroppedDirectory）自己那条链起跑
  // 时捕获的世代——这里是同一条链的延续,不重新捕获。世代已变(用户取消/切库)
  // 就整体不落盘,连"多个 markdown 需勾选"的弹窗都不弹出(评审 F2)。
  async function handleBundleFiles(label: string, files: BundleFile[], generation: number) {
    if (generation !== bundleIntakeGenerationRef.current) return;
    const classification = classifyBundleContents(files);
    if (classification.kind === "empty") {
      appendStagedSkipped([{ name: label, reason: NO_MARKDOWN_IN_BUNDLE_REASON }]);
      return;
    }
    if (classification.kind === "single") {
      stageBundleCandidates(label, [classification.file], files, generation);
      return;
    }
    // 同一时刻只允许存在一个挂起的 resolver。走到这里说明上一条链的 resolver 还没
    // 被结清（入口的 bundleProcessing 禁用 + 嵌套链一律 await 之后，理论上到不了）；
    // 真到了也必须先把它 resolve 掉再替换——直接覆盖会让上一条链的 await 永远挂起，
    // 它的忙碌位也就再也不会释放，添加来源入口从此锁死。
    const stale = bundleChoiceResolveRef.current;
    if (stale) {
      bundleChoiceResolveRef.current = null;
      stale();
    }
    await new Promise<void>((resolve) => {
      bundleChoiceResolveRef.current = resolve;
      setBundleChoice({
        label,
        files,
        candidates: classification.candidates,
        selected: new Set(classification.candidates.map((item) => item.path)),
        generation,
      });
      // 勾选等待不是「正在解析」：清掉忙碌文案（栈本身保持不变，链仍持有这一帧），
      // 让 sourceFilePickerHint 的「请先在下方选择要添加的 Markdown 文件」那一支真正
      // 可达。入口在此期间仍被 bundleChoice !== null 禁用，不会因为文案清空而放行。
      setBundleBusyLabel(null);
    });
    // 用户确认/取消后立刻把本帧的进行态文案恢复回来（下面还要接着内联/处理下一个包）。
    syncBundleBusyLabel();
  }

  // 一个虚拟文件集里被选中的那几个 md：逐个内联 → 攒批一次性入列 → 按**实际入列
  // 结果**落持久回执。
  //
  // 攒批而不是逐个入列，是因为入列会写同一份待上传列表；回执在入列**之后**才落，
  // 是因为一条回执若在文件其实没进列表时仍写着「3 张已内联」，就是在伪装成功——
  // 被拒（内联后超限）、被去重、被单次上限挡下的那几份必须带上「未加入待上传列表」
  // 及其原因。
  //
  // `generation`：所属那条异步链起跑时捕获的世代（`confirmBundleChoice` 传的是
  // 挂起面板自己那份，`handleBundleFiles` 单文件分支直接透传）。本函数体全程
  // 同步、无 await，所以在入口比对一次就覆盖了下面全部落盘点（含跳过记录/入列/
  // 回执）——世代已变即整体不落盘（评审 F2）。
  function stageBundleCandidates(
    bundleLabel: string, candidates: BundleFile[], files: BundleFile[], generation: number,
  ) {
    if (generation !== bundleIntakeGenerationRef.current) return;
    // 同批同名 md 的消歧命名（a/README.md 与 b/README.md）：不消歧会被待上传列表的
    // 「同名同大小」去重静默折叠成一份。
    const names = bundleFileNamesFor(candidates.map((item) => item.path));
    const built: File[] = [];
    const pending: Array<
      { fileName: string; receipt: InlineReceipt; notes: string[]; pairingSkipped: boolean }
    > = [];
    const rejected: SkippedStagedFile[] = [];
    // 单次上传数量上限必须在**内联之前**生效（判据与循环都在 bundle-intake 的
    // processBundleCandidates 里，可在纯函数层单测）：候选默认全选，而 mergeStagedFiles
    // 那道权威闸只在入列时才截断，「先全部内联、再截断」在一个合法的两千条目压缩包上
    // 等于白分配 GB 级 base64（codex #518 R1 P1）。
    //
    // 名额从 stagedRef.current（同步镜像，跨 await 安全）起算——与 stageIncomingFilesSync
    // 的合并基准同一份值，不读 render 闭包里的 state。
    const batchCap = sourceUploadMaxFilesPerBatch ?? BUNDLE_STAGE_FALLBACK_MAX_FILES_PER_BATCH;
    const batch = processBundleCandidates(
      candidates,
      files,
      {
        uploadMaxBytes: sourceUploadMaxBytes ?? 0,
        imageMaxBytes: sourceImageMaxBytes,
        maxImagesPerSource: sourceImageMaxPerSource,
        imagesEnabled: sourceImagePairingEnabled,
      },
      {
        names,
        remainingSlots: Math.max(0, batchCap - stagedRef.current.files.length),
        batchCap,
      },
    );
    // 名额耗尽、根本没被处理的那些：只落跳过记录，不造回执（空回执会被渲染成
    // 「未在正文中发现本地图片」——一句没发生过的事实断言）。
    rejected.push(...batch.skipped);
    for (const processed of batch.processed) {
      if (!processed.ok) {
        // 零张实际内联（正文本身已超限，或图片配对整个被跳过/没有本地图片）时，
        // 「请精简图片」是对不上症状的建议——超限的是正文，改说「拆分文档」。
        const reason = inlineTooLargeMessage(
          processed.bytes,
          processed.limit,
          processed.receipt.inlined.length,
        );
        rejected.push({ name: processed.fileName, reason });
        pending.push({
          fileName: processed.fileName,
          receipt: processed.receipt,
          // 只报「总体积超了」，用户唯一能做的就是把整份文档拆开重试；点名最大的
          // 那几张图片才是可操作的那部分信息。
          notes: [notStagedNote(reason), ...inlineTooLargeImageLines(processed.images)],
          pairingSkipped: processed.pairingSkipped,
        });
        continue;
      }
      built.push(new File([processed.rewritten], processed.fileName, { type: "text/markdown" }));
      pending.push({
        fileName: processed.fileName,
        receipt: processed.receipt,
        notes: [],
        pairingSkipped: processed.pairingSkipped,
      });
    }
    appendStagedSkipped(rejected);
    // 内联产物都是 .md，不会再含 bundle，所以走同步入列这一半即可（也因此能拿到
    // 「哪些真进了列表」来决定回执上的标注）。
    const outcome = built.length > 0
      ? stageIncomingFilesSync(built)
      : { added: [] as File[], skipped: [] as SkippedStagedFile[], duplicates: [] as File[] };
    const addedNames = new Set(outcome.added.map((file) => file.name));
    const duplicateNames = new Set(outcome.duplicates.map((file) => file.name));
    const skippedReasons = new Map(outcome.skipped.map((item) => [item.name, item.reason]));
    setBundleReceipts((previous) => [
      ...previous,
      ...pending.map((entry, index) => ({
        key: `${bundleLabel}::${entry.fileName}::${previous.length + index}`,
        bundleLabel,
        fileName: entry.fileName,
        receipt: entry.receipt,
        notes: entry.notes.length > 0
          ? entry.notes
          : stagedOutcomeNotes(entry.fileName, addedNames, duplicateNames, skippedReasons),
        pairingSkipped: entry.pairingSkipped,
      })),
    ]);
  }

  /** 成功内联却没进待上传列表时的回执标注（去重 / 单次上限 / 类型或大小被挡下）。 */
  function stagedOutcomeNotes(
    fileName: string,
    added: ReadonlySet<string>,
    duplicates: ReadonlySet<string>,
    skippedReasons: ReadonlyMap<string, string>,
  ): string[] {
    if (added.has(fileName)) return [];
    if (duplicates.has(fileName)) return [notStagedNote(ALREADY_STAGED_REASON)];
    const reason = skippedReasons.get(fileName);
    return [reason ? notStagedNote(reason) : notStagedNote(ALREADY_STAGED_REASON)];
  }

  function toggleBundleCandidate(path: string) {
    setBundleChoice((previous) => {
      if (!previous) return previous;
      const selected = new Set(previous.selected);
      if (selected.has(path)) selected.delete(path); else selected.add(path);
      return { ...previous, selected };
    });
  }

  function confirmBundleChoice() {
    if (!bundleChoice) return;
    const { label, files, candidates, selected, generation } = bundleChoice;
    setBundleChoice(null);
    // 与 setBundleChoice(null) 同一个事件处理里把进行态文案恢复回来（下面还要内联、
    // 还可能接着处理同批的下一个包）：留给 await 之后再恢复会空出一帧「既没有勾选
    // 面板、也没有进行态」的可点窗口。
    syncBundleBusyLabel();
    // generation 是这个面板所属链起跑时捕获的那份，不是"现在"——传给
    // stageBundleCandidates 让它按同一条链的世代把关（评审 F2）。
    stageBundleCandidates(
      label, candidates.filter((item) => selected.has(item.path)), files, generation,
    );
    bundleChoiceResolveRef.current?.();
    bundleChoiceResolveRef.current = null;
  }

  function cancelBundleChoice() {
    setBundleChoice(null);
    syncBundleBusyLabel();
    bundleChoiceResolveRef.current?.();
    bundleChoiceResolveRef.current = null;
  }

  // 读文本类文件前 8KB → 批量调 /detect-doc-types → 回填仍为空（未手动选）的类型。
  async function detectStagedTypes(added: File[]) {
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
      // 按**回填这一刻**的列表定位（不是发起时的快照）：检测是异步的，期间用户可能已经
      // 移除了几项或又添了几项。只填**未被用户表态过**的项（值空且未 touched），既不
      // 覆盖已选的具体类型、也不覆盖「检测在飞时用户改回自动检测」这种空但 touched 的
      // 显式选择。**绝不动 touched**——auto-detect 是系统建议、不是用户表态。
      updateStaged((prev) => ({
        ...prev,
        docTypes: fillAutoDetectedTypes(
          prev.docTypes,
          prev.files.map((file) => byName[file.name]),
          prev.touched,
        ),
      }));
    } catch {
      // 检测失败不影响上传：保持"自动检测"，用户可手动选。
    }
  }

  function setStagedDocType(index: number, value: string) {
    // 用户手动改了这一项 → 同时标记为显式设置（哪怕选回「自动检测」也是一次显式表态）。
    // 类型与 touched 在同一次更新里改：拆成两次 setState 会在两者之间留出一个检测可以
    // resolve 进来的间隙，把用户显式选的空「自动检测」当没表态覆盖掉。
    updateStaged((prev) => ({
      ...prev,
      docTypes: prev.docTypes.map((dt, i) => (i === index ? value : dt)),
      touched: markTouched(prev.touched, index),
    }));
  }

  function setAllStagedDocTypes(value: string) {
    updateStaged((prev) => ({
      ...prev,
      docTypes: prev.docTypes.map(() => value),
      touched: markAllTouched(prev.touched),
    }));
  }

  function removeStagedFile(index: number) {
    const removed = stagedRef.current.files[index];
    updateStaged((prev) => ({
      files: prev.files.filter((_, i) => i !== index),
      docTypes: prev.docTypes.filter((_, i) => i !== index),
      touched: prev.touched.filter((_, i) => i !== index),
    }));
    if (removed) {
      const removedKey = stagedFileKey(removed);
      setStagedWarnings((rows) => rows.filter((row) => stagedFileKey(row) !== removedKey));
    }
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
    const requestOwner = {};
    uploadRequestOwnerRef.current = requestOwner;
    try {
      await confirmUploadInner();
    } catch (error) {
      if (uploadRequestOwnerRef.current === requestOwner) reportError(error);
    } finally {
      if (uploadRequestOwnerRef.current === requestOwner) {
        uploadRequestOwnerRef.current = null;
        setUploadBusy(false);
      }
    }
  }

  async function confirmUploadInner() {
    const owner = sourceLibrary.captureOwner();
    if (!owner || stagedFiles.length === 0) return;
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
    const uploaded = await uploadSources(owner.notebookId, formData);
    const outcome = summarizeUpload(uploaded, docTypesBefore);
    // 用折叠去重后的 outcome.sources，不是原始 uploaded：一次上传里两个内容相同的
    // 文件会让后端对同一个 id 返回两条（新建 + 命中它的 reused），直接铺进 state 会
    // 渲染出重复卡片，直到重开笔记本才消失。
    if (!sourceLibrary.commitUploadedSources(owner, outcome.sources, outcome.added.length)) return;
    const stillOwned = () => sourceLibrary.captureOwner() === owner;
    await loadNotebookCollection({ guard: stillOwned });
    if (!stillOwned()) return;
    // P1(codex PR#334 第9轮):快传小文件可能后台已解析完(返回即 extracted),hasPending 保持
    // false → 处理轮询的 reachedExtracted 分支不触发 → ask_available 陈旧为假、对话框空锁。显式
    // 重拉解禁;仍在解析中的慢路径由处理轮询覆盖。
    revalidateAskAvailability();
    reloadCheckup(owner.notebookId);  // 新源可能立即/后续成 H2–H6:刷新体检铃铛(codex 第5轮 P2)
    rootModals.requestClose("source-add", "button");
    setToast(outcome.toast);
  }

  // 「新建来源」成功后的收尾半——commitUrlSources + 刷新清单/对话可用性/体检
  // 铃铛。抽成独立函数是给 importGapSuggestion(X9 PR-A T3,站外来源建议的
  // 「导入」按钮)复用同一条链路,行为逐字不变:三处早退(commit 失败 / 刷新期间
  // owner 已变)在这里仍然只是让本函数提前返回 false,调用方(submitUrlSources)
  // 拿到 false 时同样直接 return,与重构前「在 if 块内直接 return」的控制流
  // 完全一致。
  async function applyImportedUrlSources(
    owner: NonNullable<ReturnType<typeof sourceLibrary.captureOwner>>,
    created: readonly SourceSummary[],
  ): Promise<boolean> {
    if (!sourceLibrary.commitUrlSources(owner, created)) return false;
    // Maintain only the Ask surfaces' count (notebookSourceTotal, unfiltered) optimistically.
    // sourcesTotal (source-list pagination + Source Stack header) is deliberately left to
    // re-sync on the next source-page fetch: it tracks the *applied* source-search filter, and
    // there is no reliable applied-query signal here — sourceQuery is the editable draft (search
    // applies only on Enter), not necessarily what produced the current page — so optimistically
    // bumping it risks either a stale count or a phantom filtered page. This matches the pre-#332
    // baseline (URL import never touched sourcesTotal); the file-upload path's own unconditional
    // bump is pre-existing and out of scope here. 文档上限门控也读 notebookSourceTotal，
    // 故链接导入 +N 后满额判定同步跟进。
    const stillOwned = () => sourceLibrary.captureOwner() === owner;
    await loadNotebookCollection({ guard: stillOwned });
    if (!stillOwned()) return false;
    revalidateAskAvailability(); // P1:导入即产出可检索证据时解禁对话框(同 confirmUpload)
    reloadCheckup(owner.notebookId);  // 同理刷新体检铃铛(codex 第5轮 P2)
    return true;
  }

  async function submitUrlSources() {
    const owner = sourceLibrary.captureOwner();
    if (!owner) return;
    const urls = parseUrlLines(urlText);
    if (urls.length === 0) {
      setToast("请粘贴至少一个 http/https 链接");
      return;
    }
    setUrlBusy(true);
    const requestOwner = {};
    urlRequestOwnerRef.current = requestOwner;
    setUrlRejected([]);
    try {
      const result = await importUrlSources(owner.notebookId, urls);
      if (result.created.length > 0) {
        if (!(await applyImportedUrlSources(owner, result.created))) return;
      }
      if (sourceLibrary.captureOwner() !== owner) return;
      setUrlRejected(result.rejected);
      setToast(`已添加 ${result.created.length} 个，被拒 ${result.rejected.length} 个`);
      if (result.rejected.length === 0) {
        setUrlText("");
        setLinkSectionOpen(false);
      }
    } catch (error) {
      if (urlRequestOwnerRef.current === requestOwner) reportError(error);
    } finally {
      if (urlRequestOwnerRef.current === requestOwner) {
        urlRequestOwnerRef.current = null;
        setUrlBusy(false);
      }
    }
  }

  // 站外来源建议的「导入」按钮(ask.gap_consult,X9 PR-A T3):把这一条建议的
  // URL 当一次普通链接来源添加进当前笔记本——核心 URL 端点,不打插件路由。
  // 复用 applyImportedUrlSources 而不是自己重写一遍,是为了让「导入即解禁
  // 对话框 / 刷新体检铃铛」这条既有链路对两个入口保持同一份实现。
  // 站外建议导入的按笔记本单飞（codex #584 R6/R11）：后端的容量检查与插入
  // 不在一个原子里，同库并发两次导入能在只剩一个名额时各自快照到 capacity=1、
  // 双双入库越过上限。串行化关不掉跨标签页/跨用户的同一竞态（那是既有的
  // 全端点性质，另行登记），它关掉的是这块新面板让并发变得容易的那扇门；
  // in-flight 期间同库其余按钮经 importGapSuggestionDisabledReason 置灰并写明
  // 原因。忙碌位存的是「哪个库在忙」而不是裸布尔（与「补上关联/重新合并」
  // 同一条红线）：容量竞态是按库的，A 库在飞的导入不该灰掉 B 库的按钮，
  // 清除也只清自己那一格——迟到的 finally 绝不抹掉别的库刚建立的占用。
  // ref 是权威判据（state 更新是异步的，双击会在同一帧里读到旧 state）；
  // 集合而不是单格——A 库导入在飞时切到 B 库再导入，两个占用互不覆盖，
  // 各自的 finally 只删自己的键。
  const gapImportInFlightRef = useRef<Set<string>>(new Set());
  const [gapImportInFlight, setGapImportInFlight] = useState<ReadonlySet<string>>(
    () => new Set(),
  );

  async function importGapSuggestion(url: string): Promise<{ ok: boolean; message?: string }> {
    const owner = sourceLibrary.captureOwner();
    if (!owner) return { ok: false, message: "未能添加这个链接" };
    if (gapImportInFlightRef.current.has(owner.notebookId)) {
      return { ok: false, message: "另一条建议正在导入，请稍候再试" };
    }
    gapImportInFlightRef.current.add(owner.notebookId);
    setGapImportInFlight(new Set(gapImportInFlightRef.current));
    try {
      const result = await importUrlSources(owner.notebookId, [url]);
      if (result.created.length > 0) {
        // 有意丢弃 applyImportedUrlSources 的布尔返回值:它只在 await 期间 owner
        // 已换(切库/切用户)时才回 false,那种情形下服务端确实已经把来源添加
        // 成功了——只是这个面板已经不在屏幕上,没有人会看到失败提示。报失败
        // 反而是一次假红(用户会以为链接没导入,实际上已经进了笔记本)。
        await applyImportedUrlSources(owner, result.created);
        return { ok: true };
      }
      return { ok: false, message: result.rejected[0]?.reason || "未能添加这个链接" };
    } catch (error) {
      // ⚠ 不读 error.message/.error——errors.ts 是错误人话层唯一的翻译入口。
      return { ok: false, message: toUserMessage(error, "未能添加这个链接") };
    } finally {
      // 只删自己那一格：迟到的 settle 绝不抹掉别的库刚建立的占用。
      gapImportInFlightRef.current.delete(owner.notebookId);
      setGapImportInFlight(new Set(gapImportInFlightRef.current));
    }
  }

  async function openSourceDetailById(sourceId: string, elementId = "") {
    const lease = rootModals.issue("source-detail", rootModals.captureWorkspaceOwner());
    if (!lease) return;
    const opened = await sourceLibrary.openSourceById(sourceId, elementId);
    if (!opened) return;
    if (!rootModals.publish(lease)) sourceLibrary.closeSourceDetail();
  }

  async function openSourceDetail(source: SourceSummary) {
    await openSourceDetailById(source.id);
  }

  function onOpenSourceElement(sourceId: string, elementId?: string) {
    openSourceDetailById(sourceId, elementId || "").catch(reportError);
  }

  async function reparseSource() {
    await sourceLibrary.reparseSource();
  }

  function confirmDeleteSource(source: SourceSummary) {
    if (crossLibrarySourceNotebookId(source.notebook_id, currentNotebookId)) return;
    if (deletingSourceIds.has(source.id)) return;
    // The list-card action has no source detail open, so it must remain a
    // workspace-owned confirmation.  Detail actions use the narrower source
    // lease so a source switch also invalidates the prompt.
    const modalOwner = sourceDetail?.id === source.id
      ? rootModals.captureSourceOwner()
      : rootModals.captureWorkspaceOwner();
    openInfoModal({
      title: "删除来源",
      message: `确定删除“${source.title}”吗？它的解析元素、候选知识和由该来源生成的已批准知识也会一起移除。`,
      actions: [
        { label: "取消", action: () => {} },
        {
          label: "删除来源",
          danger: true,
          action: () => sourceLibrary.deleteSource(source).catch(reportError),
        },
      ],
    }, modalOwner);
  }

  async function openAskSession(id: string) {
    const workspaceEpoch = ++workspaceEpochRef.current;
    invalidateNotebookOpenGuard();
    const rootTransition = rootModals.beginWorkspaceTransition();
    memoryLinksAbortRef.current?.abort();
    memoryLinksAbortRef.current = null;
    try {
      await askSession.openSession(id, workspaceEpoch);
    } finally {
      const actorId = currentUser?.id ?? "";
      const notebookId = activeNotebookIdRef.current;
      rootModals.finishWorkspaceTransition(
        rootTransition,
        actorId && notebookId ? { actorId, notebookId, workspaceEpoch } : null,
      );
    }
  }

  function startNewAskSession() {
    const workspaceEpoch = ++workspaceEpochRef.current;
    invalidateNotebookOpenGuard();
    const rootTransition = rootModals.beginWorkspaceTransition();
    memoryLinksAbortRef.current?.abort();
    memoryLinksAbortRef.current = null;
    setMemorySavedAnswers({});
    askSession.startNewSession(workspaceEpoch);
    const actorId = currentUser?.id ?? "";
    const notebookId = activeNotebookIdRef.current;
    rootModals.finishWorkspaceTransition(
      rootTransition,
      actorId && notebookId ? { actorId, notebookId, workspaceEpoch } : null,
    );
  }

  function requestDeleteSession(session: ConversationSummary) {
    openInfoModal({
      title: "删除会话",
      message: `确定删除“${session.title || "未命名会话"}”吗？对应的历史问答会一起移除。`,
      actions: [
        { label: "取消", action: () => undefined },
        {
          label: "删除",
          danger: true,
          action: () => { askSession.deleteSession(session.id).catch(reportError); },
        },
      ],
    });
  }

  function requestBulkCleanup(days: number) {
    const victims = conversationsOlderThan(sessions, days);
    if (victims.length === 0) return;
    openInfoModal({
      title: "批量清理会话",
      message: `将删除 ${victims.length} 条最近 ${days} 天内无活动的会话，对应的历史问答会一起移除。`,
      actions: [
        { label: "取消", action: () => undefined },
        {
          label: "删除",
          danger: true,
          action: () => { askSession.bulkCleanup(days).catch(reportError); },
        },
      ],
    });
  }
  function clearAnalyticsData() {
    analyticsLoadScopeRef.current.cancel();
    setAnalytics(null);
    setContentOverview(null);
    setContentOverviewError("");
    setContentOverviewLoading(false);
  }

  function closeAnalytics(reason: "button" | "backdrop" = "button") {
    rootModals.requestClose("analytics", reason);
  }

  function closeKnowhow() {
    setKnowhowNavigation((current) => closeKnowhowNavigation(current));
    // knowhow 抽屉是覆盖层、不改 chatMode,故上面的 chatMode effect 不会触发。关闭回到
    // 问答框时与后端对齐:删表→重新禁用、建表(投影落库后)→解禁,长投影退避轮询到位。
    revalidateAskAvailabilityAfterKnowhow();
  }

  async function openAnalytics() {
    if (!currentNotebookId) return;
    const modalLease = rootModals.issue("analytics", rootModals.captureWorkspaceOwner());
    if (!modalLease) return;
    const nb = currentNotebookId;
    const owner = analyticsLoadScopeRef.current.begin(nb);
    const isCurrent = () => (
      analyticsLoadScopeRef.current.isCurrent(owner, activeNotebookIdRef.current)
      && rootModals.leaseIsCurrent(modalLease)
    );
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
      kgWorkspace.observeKgBuild(status.kg.job, status.kg.building);
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
      if (isCurrent() && rootModals.publish(modalLease)) setAnalytics(response);
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

  // Task 12（引用跳转）：ask 引用命中 knowhow 格子时「在表格中查看」的落点——
  // 打开 Knowhow 面板并记下目标表/行，KnowhowPanel 自己负责挂载后定位到该
  // 行的抽屉（含目标表/行已被删除时的兜底提示，见 knowhow-panel.tsx）。
  function openKnowhowAt(tableId: string, rowId: string) {
    setKnowhowNavigation(openKnowhowNavigation({ jumpTarget: { tableId, rowId } }));
  }

  // 命令目录确认落库后的「去看这张表」落点：只定位到表，不指定行（刚写入的是一批
  // 行，挑其中任意一行当落点都是随意的）。KnowhowJumpTarget.rowId 本就允许 null。
  function openKnowhowTable(tableId: string) {
    rootModals.requestClose("source-detail", "button");
    setKnowhowNavigation(openKnowhowNavigation({ jumpTarget: { tableId, rowId: null } }));
  }

  // 命令目录入口的成本预告确认：复用本文件既有的 infoModal（已经是可拖动的
  // FloatingModalCard），不让面板自己再造一套居中确认框。
  function confirmCommandCatalog(request: CatalogConfirmRequest) {
    openInfoModal({
      title: request.title,
      message: request.body,
      sections: request.sections,
      actions: [
        { label: "取消", action: () => {} },
        { label: request.confirmLabel, primary: true, action: request.onConfirm },
      ],
    }, rootModals.captureSourceOwner());
  }

  async function openKgView(
    targetNodeId?: string,
    notebookId: string | null = currentNotebookId,
    sourceNotebookId: string | null = notebookId,
  ) {
    if (!notebookId || notebookId !== currentNotebookId) return;
    await kgWorkspace.openGraph(targetNodeId, sourceNotebookId || notebookId);
  }

  // 关闭知识图谱视图。刻意做成具名函数而不是内联 `() => kgWorkspace.closeGraph()`:
  // 视图内还挂着「图谱分析」弹窗的开关,它必须和父视图一起归零(见 rootModals 的
  // "kg-analysis" slot),而"关闭时要一起做的事"散在内联箭头里就迟早会漏掉一条。
  function closeKgView() {
    rootModals.requestClose("kg-schema", "button");
    rootModals.requestClose("kg-analysis", "button");
    kgWorkspace.closeGraph();
  }

  function openKgSchemas() {
    if (!rootModals.open("kg-schema", rootModals.captureWorkspaceOwner())) return;
    kgWorkspace.openSchemas();
  }

  function closeKgSchemas(reason: "button" | "backdrop" = "button") {
    rootModals.requestClose("kg-schema", reason);
  }

  function openKgAnalysis() {
    if (!rootModals.open("kg-analysis", rootModals.captureWorkspaceOwner())) return;
    kgWorkspace.openAnalysis();
  }

  function closeKgAnalysis() {
    rootModals.requestClose("kg-analysis", "button");
  }

  // 服务端搜索：输入词变化时防抖触发 /kg/search；清空词时还原为核心子图。
  function handleKgSearchChange(value: string) {
    kgWorkspace.updateGraphSearch(value);
  }

  // 切换图谱范围档位：按新 limit 重拉子图（核心 N / 全部）。
  async function changeKgRange(limit: number) {
    await kgWorkspace.changeRange(limit);
  }

  // KG 视图内补连孤立节点：后台任务。POST 只认领任务槽（服务端按笔记本单飞，重复点回
  // 409），真正的统计要等下面那条轮询读到终态才有——所以这里**不能**拿 POST 的返回值
  // 编一个「已补上 N 条」出来。忙碌位在 await 之前就置上（长任务按钮红线），由轮询在
  // 终态解除；POST 自己失败时当场解除，否则按钮会锁在一个根本没起来的任务上。
  // codex R4 P2(B):「重新合并」与「补上关联」共用服务端同一把按笔记本单飞锁，早退必须
  // 认「任一忙碌位为真即忙」（kgGraph.relinking || kgGraph.rebuilding）——只看自己那
  // 一位会在对方占槽期间仍然发出请求，白撞一次 409。
  // codex R8:POST 撞 409 说明服务端确有一个维护任务在跑(可能是另一个标签页/会话
  // 在「一次性状态探测之后、这次点击之前」发起的)。把它当提交失败清掉本地位,轮询就
  // 永远不会领养那个任务——按钮回到可点、图谱在任务完成后保持陈旧。正确动作是查两个
  // 状态、领养正在跑的那一个;两个都不在跑(竞态已消散)才如实清位。
  // codex R9:上一版逐个 `.catch(() => null)` 把"这次探测确实失败"和"探测成功查到没在
  // 跑"混成同一个 null——网络抖动导致的假阴性会被当成"两个都不在跑",于是调用方把自己的
  // 忙碌位当竞态已消散清掉,而服务端那个任务其实还在跑、没人再轮询。改用 Promise.allSettled
  // 把两次探测的失败与成功分开看。
  // codex R18:R9~R17 的写法是"任一探测 rejected 就整体判 unknown、不碰任何忙碌位"——两个
  // 探测都活着时没问题,但只要其中一个偶发失败,另一个探测明明已经确认**对面** kind 在跑,
  // 也会被这条整体短路拖累成"什么都不领养":调用方保留自己那一位,而自己这种在服务端视图
  // 里其实一直是 idle,轮询永远等不到需要观察的终态——真任务完成也不会刷新。改成**逐 kind
  // 独立处置**:每个探测只对自己那个 kind 的忙碌位负责——fulfilled 且 running 才 claim,
  // fulfilled 且非 running 才 release;rejected 的那一侧忙碌位原样不动(信息不足,交给
  // 调用方自己的轮询继续查,瞬时错误自愈)。verdict 由两侧观察共同决定:任一侧观察到
  // running 就是 "adopted"(哪怕另一侧探测失败,那一侧的忙碌位也已经按上面的规则原样
  // 保留,不需要靠 verdict 补救);否则只要还有一侧探测失败就是 "unknown"(调用方据此
  // 保留自己的位、不做进一步判断);两侧都成功且都没在跑才是 "idle"。
  async function relinkFromKgView() {
    await kgWorkspace.startRelink();
  }

  // 「重新合并」：后台任务。POST 只认领任务槽（服务端与「补上关联」共用同一把按笔记本
  // 的单飞锁，重复点或另一件在跑都回 409），聚类数要等下面那条轮询读到终态才有——所以
  // 这里**不能**拿 POST 的返回值编一个「共 N 组概念」出来。忙碌位在 await 之前就置上
  // （长任务按钮红线），由轮询在终态解除；POST 自己失败时当场解除，否则按钮会锁在一个
  // 根本没起来的任务上。
  // codex R4 P2(B):早退必须认「任一忙碌位为真即忙」（kgGraph.rebuilding ||
  // kgGraph.relinking）——「补上关联」在跑时占的是同一把服务端锁，只看自己那一位仍
  // 会发出请求、白撞一次 409。
  async function refreshUnifiedKg() {
    await kgWorkspace.startRebuild();
  }

  // 「重新合并」唯一入口(看板「索引与构建」面板 + 知识图谱视图共用):先统一确认再重建。
  // codex R4 P2(B):同样认「任一忙碌位为真即忙」，与 refreshUnifiedKg 的早退同口径。
  function confirmRefreshUnifiedKg() {
    if (kgGraph.rebuilding || kgGraph.relinking || kgGraph.buildingKg) return;
    confirmIndexAction("重新合并知识图谱？\n\n将重算跨文档概念聚类并刷新图谱索引（不重新分析来源）。后台进行，完成后自动更新。", () => refreshUnifiedKg());
  }

  // 图谱分析页的动作与「重新合并」复用同一条后台任务，只把用户目标说成报告生成，
  // 避免让人自己猜“尚未生成”的五份数据究竟由哪个维护动作产出。
  function confirmGenerateKgAnalysis() {
    if (kgGraph.rebuilding || kgGraph.relinking || kgGraph.buildingKg) return;
    confirmIndexAction(
      "生成或更新图谱分析？\n\n将重算跨文档概念合并、主题板块和质量统计（不重新分析来源）。后台进行，完成后分析页会自动刷新。",
      () => refreshUnifiedKg(),
    );
  }

  async function reviewPendingMerges() {
    await kgWorkspace.reviewPendingMerges();
  }

  async function reviewAllMerges() {
    await kgWorkspace.reviewAllMerges();
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
    window.setTimeout(() => {
      kgDetailRef.current?.scrollTo({ top: 0, behavior: "smooth" });
    }, 0);
    await kgWorkspace.selectNode(nodeId);
  }

  // 落一条合并决定。只有「合并」会启动重新合并让图谱跟上；「分开」只写入
  // durable cannot-link，当前 concept_clusters 没有变化，因此不应为它全库重建。
  //
  // 确认分支后台化之后不再等它跑完：POST 立刻回来，图谱与待确认列表由上面那条
  // rebuild/status 轮询在终态重拉（所以这里的忙碌位也要走同一个集合，否则轮询 effect
  // 根本不会开）。409 = 共槽已经有一次重新合并/补上关联在跑：确认本身已经落库，不是
  // 失败，忙碌位要**保留**。但那次占槽任务(重新合并或补上关联,谁先跑起来的都可能)
  // 未必会让图谱看见这条确认——它可能在确认落库**之前**就已经捕获了输入版本与
  // decided pairs,跑完发布的还是旧聚类;占槽的若是补上关联,rebuild/status 对它如实
  // 回 idle(两个状态视图只认自己的 kind),轮询几乎立刻收工并刷新一次,拿到的同样是
  // 确认生效前的图。所以这里在 pendingRebuildNotebookIds 给这个库留一个「待补发」
  // 标记,由 rebuild 轮询的终态收尾消费:那次占槽任务结束后自动补发一次
  // rebuildUnifiedKg,不需要用户自己记得再点。落确认本身失败才是真失败，忙碌位当场
  // 清掉。
  //
  // codex P2-1(边界登记,非缺陷):这份自动补发是**客户端 best-effort 承诺**，标记只活在
  // 这个标签页的内存里。作用域=标签页保持打开,或占槽任务仍在跑、图谱仍 dirty 时重载/
  // 重开页面(那种情况下上面 [currentNotebookId] 那条恢复 effect 会从服务端状态重建
  // 标记)。若重载/重开发生在占槽任务已经收工**之后**——标记已经连同其余前端 state 一起
  // 丢了,没有任何东西会去补发这条已经落库的确认,直到用户自己想起来手动点一次
  // 「重新合并」;既有的「待重建」dirty 标签就是这个缺口的持久信号,不需要额外 UI。补上
  // 这个缺口需要服务端持久重试队列(而不是纯前端标记)——已登记为后续独立立项,本次刻意
  // 不做(超出这一批的范围,且值不值得为这条边界换一套持久化基础设施还需要真实发生频率
  // 的数据支撑)。
  async function decideMerge(candidate: PendingMerge, confirm: boolean) {
    await kgWorkspace.decideMerge(candidate, confirm);
  }

  // --- Two-tier federation: mark notebook base / personal -----------------
  async function handleTierAction() {
    if (!currentNotebook) return;
    const state = tierActionState(currentNotebook);
    const target = state.action === "unset" ? "personal" : "base";
    const notebookId = currentNotebook.id;
    const workspaceEpoch = workspaceEpochRef.current;
    const stillCurrent = () => workspaceRequestIsCurrent(
      false,
      workspaceEpoch,
      workspaceEpochRef.current,
      notebookId,
      activeNotebookIdRef.current,
    );
    let updated: Awaited<ReturnType<typeof setNotebookTier>>;
    try {
      updated = await setNotebookTier(notebookId, target);
    } catch (error) {
      // The caller owns user-visible error reporting.  Do not let a rejected
      // request from a workspace the user has already left reach that caller.
      if (stillCurrent()) throw error;
      return;
    }
    if (!stillCurrent()) return;
    setCurrentNotebook((current) => (
      current?.id === notebookId ? updated as NotebookSummary : current
    ));
    try {
      await loadNotebookCollection({ guard: stillCurrent });
    } catch (error) {
      if (stillCurrent()) throw error;
      return;
    }
    if (!stillCurrent()) return;
    setToast(
      target === "base"
        ? "已设为公共知识库 — 其他笔记本可以在设置里把它挂为参考库"
        : "已取消公共知识库，恢复为个人知识库"
    );
  }

  // --- 分享与拷贝(Phase 1) --------------------------------------------
  // A. 打开分享弹窗(owner 侧):**只读**当前状态,零副作用。
  //
  // 这里刻意不 POST(P1-T4):弹窗里除了链接还有「共享给群组」一节,而 POST 会当场
  // 铸出一条分享链接——一次纯查看的动作产生了持久副作用,只想共享给群组的用户会
  // 莫名其妙多出一条链接。链接改由用户显式点「开启链接分享」时才发 POST。
  async function openShareModal() {
    if (!currentNotebook || shareOperationRef.current) return;
    const modalLease = rootModals.issue("notebook-share", rootModals.captureWorkspaceOwner());
    if (!modalLease || modalLease.owner.kind !== "workspace") return;
    // 链接分享(GET/POST/DELETE /share)是 notebook:configure(**恒 owner**,P2-T2 评审
    // P0)。组管理员只到「共享给群组」一节(授权边管理 = notebook:manage),不该也不能
    // 读链接态——`getShareState` 对他会 404。所以非 owner 直接渲染空链接态(不发 GET),
    // 弹窗里的链接分享区另由 canConfigureNotebook 收起,只留群组授权区(它自己拉 grants)。
    if (!capabilities.canConfigureNotebook) {
      if (rootModals.publish(modalLease)) {
        setShareModal({ share_token: "", copyable: false, size: {} });
      }
      return;
    }
    const operation = {};
    shareOperationRef.current = operation;
    setShareBusy(true);
    try {
      const state = await getShareState(modalLease.owner.notebookId);
      if (rootModals.publish(modalLease)) setShareModal(state);
    } catch (error) {
      if (rootModals.leaseIsCurrent(modalLease)) throw error;
    } finally {
      if (shareOperationRef.current === operation) {
        shareOperationRef.current = null;
        setShareBusy(false);
      }
    }
  }

  // 显式开启链接分享。后端幂等(已有 token 原样返回),所以重复点不会换链接。
  async function enableShareLink() {
    const modalLease = rootModals.activeLease("notebook-share");
    if (!modalLease || modalLease.owner.kind !== "workspace" || shareOperationRef.current) return;
    const operation = {};
    shareOperationRef.current = operation;
    setShareBusy(true);
    try {
      const state = await shareNotebook(modalLease.owner.notebookId);
      if (!rootModals.owns(modalLease)) return;
      setShareModal(state);
      await handleSharingChanged();
    } catch (error) {
      if (rootModals.owns(modalLease)) throw error;
    } finally {
      if (shareOperationRef.current === operation) {
        shareOperationRef.current = null;
        setShareBusy(false);
      }
    }
  }

  // 复制分享链接到剪贴板。结果先落在按下的那一颗按钮上(`key` 标识是哪一颗),失败时
  // 顺手选中紧邻的只读链接——用户当场就能 ⌘C/Ctrl+C,不用先读懂提示再自己框选。toast
  // 保留:失败那条带着链接原文,是横幅仍然有用的地方。
  async function handleShareLinkCopy(key: string, link: string) {
    const copied = await copyTextSafely(link);
    shareLinkCopy.report(key, copied);
    if (!copied) {
      const input = shareLinkInputs.current.get(key);
      input?.focus();
      input?.select();
    }
    setToast(shareLinkCopyToast(copied));
  }

  async function copyShareLink() {
    if (!shareModal) return;
    const link = buildShareLink(shareModal.share_token, window.location.origin);
    await handleShareLinkCopy(shareModal.share_token, link);
  }

  // 取消分享:撤销 token 并踢掉只读成员。**弹窗不关**——它同时是「共享给群组」的
  // 界面,而取消链接对群组授权是空操作,关掉整个弹窗会让用户以为群组共享也没了。
  // 只把链接态清空,链接区回到「开启链接分享」。
  async function handleUnshare() {
    const modalLease = rootModals.activeLease("notebook-share");
    if (!modalLease || modalLease.owner.kind !== "workspace" || shareOperationRef.current) return;
    const operation = {};
    shareOperationRef.current = operation;
    setShareBusy(true);
    try {
      await unshareNotebook(modalLease.owner.notebookId);
      if (!rootModals.owns(modalLease)) return;
      setShareModal({ share_token: "", copyable: false, size: {} });
      await handleSharingChanged();
      if (!rootModals.owns(modalLease)) return;
      setToast("已取消链接分享，链接立即失效");
    } catch (error) {
      if (rootModals.owns(modalLease)) throw error;
    } finally {
      if (shareOperationRef.current === operation) {
        shareOperationRef.current = null;
        setShareBusy(false);
      }
    }
  }

  // B. 接收分享(拷贝侧):拷贝分享库到当前用户空间 → 选中新库 → 关弹窗
  async function handleCopyShared(token: string) {
    setCopyBusy(true);
    try {
      const created = await copyShared(token);
      await loadNotebookCollection();
      await openNotebook(String(created.id));
      rootModals.requestClose("shared-preview", "button");
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
      rootModals.requestClose("shared-preview", "button");
      setToast("已加入只读共享");
    } finally {
      setCopyBusy(false);
    }
  }

  // D. 退出共享(只读成员移除自己):刷新列表;若当前被移除则切到第一个自有库
  /**
   * 访问权变动之后,把**我正站在哪本库里**也对一遍账。
   *
   * 只刷新清单是不够的:工作区开在 `currentNotebook` 上,它是另一份 state,清单变了
   * 它一动不动。于是「退出群组 / 被移出群组 / 组被删 / 共享被撤销」之后,人还站在一本
   * 自己已经读不到的库里,整屏毫无反应,只能手动刷新——用户反馈的正是这条(2026-08-20)。
   *
   * 判据是「重取回来的清单里还有没有它」而不是「刚才那个动作针对的是不是它」:同一本库
   * 可以经**多条**路进来(另一个群组、直接成员、everyone 授权边),退掉其中一条并不必然
   * 失去访问。清单是服务端算过权限的那一份,照它走既不会误踢也不会漏踢。
   *
   * 落点沿用「退出只读共享」既有的那套:优先跳到自己的第一本库,一本都没有才回集合页。
   * `replaceState` 顶掉旧的 `#notebook=<id>`,否则那条历史条目还活着,按返回键会撞上
   * 一本已经 403 的库。
   */
  async function reconcileOpenNotebook(remaining: NotebookSummary[], navEpoch: number) {
    // ⚠ 世代闸:用户可能在重取清单在飞的那段时间里自己切库或回集合页,两者都会**同步**
    // 递增 `workspaceEpochRef`。世代变了就说明导航已经不归这次对账管,整条放弃。
    //
    // 换用 `activeNotebookIdRef` 之后(见下),这道闸挡的**只剩**一种情形:快照之后发起
    // 的那次导航已经**落地**了。此时 `activeNotebookIdRef` 是那本新库、不是 null,而
    // `remaining` 是导航之前取的——一份可能还没有它的旧清单,照着判会把用户刚打开的库
    // 判成「已失去访问」并再次跳走。世代不等即放弃,是让那次导航自己说了算。
    //
    // 反过来,导航**尚未**落地那一半由下面的 `null` 判据覆盖(codex #529 R2 / R9 P2)。
    if (workspaceEpochRef.current !== navEpoch) return;
    // ⚠ 判据用 `activeNotebookIdRef` 而**不是** `currentNotebookIdRef`。后者是渲染期从
    // state 抄下来的,在一次切库**落地之前**始终还指着上一本库;于是「用户在弹窗还在飞
    // 的时候关掉它、点开另一本库」这条时序里,对账会读到那本刚被撤销的旧库、判定需要
    // 跳走,反过来把用户自己发起的导航作废掉——而上面那道世代闸挡不住它:导航是在世代
    // 快照**之前**发起的,两边世代相等(codex #529 R9 P2)。
    //
    // `activeNotebookIdRef` 是权威的「此刻真正装载着哪一本」:`openNotebook` 一进门就把
    // 它置 null,只有取数成功且世代仍匹配才写上新 id;`showCollection`/登出同样置 null。
    // 所以 null 恰好覆盖「导航在飞」与「人在集合页」两种「无库可对账」的情形,直接返回
    // 就是对的——用户马上要落地的那本库由 `openNotebook` 自己负责,轮不到这里替他决定。
    const openId = activeNotebookIdRef.current;
    if (!openId) return;
    if (remaining.some((n) => n.id === openId)) {
      // 还在清单里 ≠ 什么都没变。同一本库可以经多条路进来,撤掉其中一条之后访问权还在,
      // 但**档位可能降了**:组管理员被移出那个组、只剩另一个组的只读边,`can_manage_content`
      // 与 `granted_via` 都变了。工作区渲染自 `currentNotebook` 这份独立 state,不跟着刷
      // 就会继续亮着一整屏写入口、挂着旧的来源徽章,而每一次写都会在 API 上被拒
      // (codex #529 R11 P2)。
      //
      // 走既有的 `refreshActiveNotebook`(共享面变更一直用它),而不是拿列表行去合并:
      // 列表投影里 `document_limit` 是 0 哨兵、`paper_meta_missing` 恒 null,合进详情会把
      // 那些字段一起改坏。也刻意不先比对字段再决定要不要刷——这个 PR 里每一个「聪明的
      // 代理判据」最后都被证明选错了字段,一次点查换掉一整类这种错。
      await refreshActiveNotebook();
      return;
    }
    const firstOwned = remaining.find((n) => (n.access ?? "owner") === "owner");
    if (firstOwned) {
      try {
        // ⚠ `openNotebook` 返回 false = 这次切换在飞行中被顶替(用户自己点去了别处,
        // epoch guard 弃掉了结果)。那时**不能**再写 history:界面停在用户新选的库上,
        // 而地址栏指向 firstOwned,两者从此对不上(codex #529 R1 P2)。同文件的
        // `openNotebookMemory` 早就是这个写法,这里对齐它。
        if (!(await openNotebook(firstOwned.id, "none"))) return;
      } catch {
        // ⚠ 兜底导航自己失败(瞬时抖动)时**不能就地停手**:`openNotebook` 一进门就把
        // `activeNotebookIdRef` 置成了 null,而屏幕上还留着那本已经读不到的库。之后每
        // 一次复核都会因为「无库可对账」在上面直接返回,人被永久钉在一个陈旧工作区里,
        // 只能自己手动导航才出得来(codex #529 R12 P2)。
        //
        // 退回集合页:那本来就是这条路径的另一半落点(没有自有库时走的就是它),而且
        // 状态可自恢复。带一句说明,免得人莫名其妙被弹出工作区。
        showCollection();
        setToast("你已失去这本笔记本的访问权");
        return;
      }
      window.history.replaceState(null, "", notebookHash(firstOwned.id));
    } else {
      showCollection();
    }
  }

  async function handleLeaveShared() {
    if (!currentNotebook) return;
    const leftId = currentNotebook.id;
    const navEpoch = workspaceEpochRef.current;
    setLeaveBusy(true);
    try {
      await leaveNotebook(leftId);
      // 走同一条收口:重取、请求世代闸、对账都别再抄一份(抄一份就会漏掉其中一道闸——
      // 这次漏的正是请求世代)。
      await notebookCollection.refreshAfterAccessChange(navEpoch);
      setToast("已退出只读共享");
    } finally {
      setLeaveBusy(false);
    }
  }

  // E. owner「已分享总览」:拉取所有我 owner 且已分享的库 → 打开 modal
  async function openSharedByMe() {
    if (shareOperationRef.current) return;
    const modalLease = rootModals.issue("shared-by-me", rootModals.captureActorOwner());
    if (!modalLease) return;
    setSharedByMeList(null);
    try {
      const items = await sharedByMe();
      if (rootModals.publish(modalLease)) setSharedByMeList(items);
    } catch (error) {
      if (rootModals.leaseIsCurrent(modalLease)) throw error;
    }
  }

  // 总览里「取消链接分享」的确认。撤销不可逆:旧 token 永久失效,已通过链接加入的只读
  // 成员当场被移除,重新开启只会铸出一条新链接。所以它与「删除来源」「删除会话」走同一套
  // 确认弹窗(info 层,z:80,与本总览共存),而不是点一下就执行。
  // 后果句刻意**不报人数**(codex #631 R5 P2)。`sharedByMeList` 是打开弹窗那一刻的快照:
  // 之后有人拿链接加入,按快照报数就会少说;而 copy 档的行后端根本不带成员名单(库缩小后
  // 由 readonly 转 copy,旧的只读成员身份还在),按它报数会报出 0 —— 两种偏差都是**少说**,
  // 正是破坏性确认最不该犯的错。改成一句与人数无关、永远成立的话。
  function confirmUnshareFromOverview(item: SharedByMeItem) {
    const consequences = [
      "链接立即失效，拿到旧链接的人不能再打开；已通过这条链接加入的只读成员会被一并移除。",
      "重新开启会生成另一条链接，旧链接不会恢复。",
      item.group_count > 0 ? "共享给群组的部分不受影响。" : "",
    ].filter(Boolean);
    openInfoModal({
      title: `取消「${item.name}」的链接分享`,
      message: consequences.join(""),
      actions: [
        { label: "保留链接", action: () => {} },
        { label: "取消链接分享", danger: true, action: () => { handleUnshareFromOverview(item.id).catch(reportError); } },
      ],
    }, rootModals.captureActorOwner());
  }

  // 总览里「取消分享」:撤销 token(踢全员)→ 重拉总览刷新
  async function handleUnshareFromOverview(notebookId: string) {
    const modalLease = rootModals.activeLease("shared-by-me");
    if (!modalLease || shareOperationRef.current) return;
    const operation = {};
    shareOperationRef.current = operation;
    setShareBusy(true);
    setUnsharingNotebookId(notebookId);
    try {
      await unshareNotebook(notebookId);
      if (!rootModals.owns(modalLease)) return;
      const items = await sharedByMe();
      if (!rootModals.owns(modalLease)) return;
      setSharedByMeList(items);
      await loadNotebookCollection();
      if (!rootModals.owns(modalLease)) return;
      setToast("已取消链接分享，链接立即失效");
    } catch (error) {
      if (rootModals.owns(modalLease)) throw error;
    } finally {
      // 进行态文案与忙碌闸在同一个判据下解除:两者必须同生同灭,否则会留下一行写着
      // 「取消中…」却已经能点的按钮(或反过来:文案先掉、按钮还禁着)。
      if (shareOperationRef.current === operation) {
        shareOperationRef.current = null;
        setShareBusy(false);
        setUnsharingNotebookId(null);
      }
    }
  }

  // --- Governance: promotion queue (Track F) ---------------------------
  async function openPromoQueue() {
    if (promoOperationRef.current) return;
    const modalLease = rootModals.issue("promotion-queue", rootModals.captureActorOwner());
    if (!modalLease) return;
    try {
      const queue = await fetchPromotionQueue();
      if (rootModals.publish(modalLease)) setPromoQueue(queue);
    } catch (error) {
      if (rootModals.leaseIsCurrent(modalLease)) throw error;
    }
  }

  // targetBaseId 未传时按 promotionTarget(渲染时用 currentNotebookBases 算出)三态分派:
  // none(0 个公共库挂载)拒绝、auto(1 个)直接用、choose(>1 个)转去弹选择器,选好后
  // 选择器自己会带着 targetBaseId 回调本函数——这一次不再重新分派,直接提交。
  async function submitPromotion(objectId: string, targetBaseId?: string) {
    const actorId = currentUser?.id ?? "";
    const notebookId = currentNotebookId;
    const workspaceEpoch = workspaceEpochRef.current;
    if (!actorId || !notebookId) return;
    const isCurrent = () => currentUser?.id === actorId
      && activeNotebookIdRef.current === notebookId
      && workspaceEpochRef.current === workspaceEpoch;
    if (!targetBaseId) {
      if (promotionTarget.kind === "none") {
        setToast("需先挂载一个公共知识库，才能贡献内容");
        return;
      }
      if (promotionTarget.kind === "choose") {
        if (rootModals.open("promotion-target", rootModals.captureWorkspaceOwner())) {
          setPendingPromotionObjectId(objectId);
        }
        return;
      }
      targetBaseId = promotionTarget.baseId;
    }
    try {
      await proposePromotion(notebookId, objectId, targetBaseId);
      if (isCurrent()) setToast("已提交贡献申请");
    } catch (error) {
      if (isCurrent()) throw error;
    }
  }

  async function decidePromotion(candidateId: string, decision: "approve" | "reject", reason = "") {
    const modalLease = rootModals.activeLease("promotion-queue");
    if (!modalLease || promoOperationRef.current) return;
    const operation = {};
    promoOperationRef.current = operation;
    setPromoBusy(true);
    try {
      if (decision === "approve") {
        const result = await approvePromotion(candidateId);
        if (!rootModals.owns(modalLease)) return;
        const merged = result.merged_into ? `（与 ${result.merged_into.slice(0, 8)} 合并）` : "";
        setToast(`已批准收录${merged}，内容已加入公共知识库`);
      } else {
        await rejectPromotion(candidateId, reason);
        if (!rootModals.owns(modalLease)) return;
        setToast("贡献未采纳，个人内容保持不变");
      }
      if (!rootModals.owns(modalLease)) return;
      // Refresh queue, then any loaded notebook collection / knowledge list.
      const queue = await fetchPromotionQueue();
      if (!rootModals.owns(modalLease)) return;
      setPromoQueue(queue);
      await loadNotebookCollection();
    } catch (error) {
      if (rootModals.owns(modalLease)) throw error;
    } finally {
      if (promoOperationRef.current === operation) {
        promoOperationRef.current = null;
        setPromoBusy(false);
      }
    }
  }

  // --- Track E: edge review queue ----------------------------------------
  async function openEdgeReviewQueue(notebookId: string | null = currentNotebookId) {
    if (!notebookId || edgeOperationRef.current) return;
    const modalLease = rootModals.issue("edge-review", rootModals.captureWorkspaceOwner());
    if (!modalLease || modalLease.owner.kind !== "workspace" || modalLease.owner.notebookId !== notebookId) return;
    try {
      const queue = await fetchEdgeReviewQueue(notebookId);
      if (rootModals.publish(modalLease)) setEdgeQueue(queue);
    } catch (error) {
      if (rootModals.leaseIsCurrent(modalLease)) throw error;
    }
  }

  async function decideEdge(relId: string, status: "verified" | "rejected") {
    const modalLease = rootModals.activeLease("edge-review");
    if (!modalLease || modalLease.owner.kind !== "workspace" || edgeOperationRef.current) return;
    const operation = {};
    edgeOperationRef.current = operation;
    setEdgeBusy(true);
    try {
      await reviewRelation(modalLease.owner.notebookId, relId, status);
      if (!rootModals.owns(modalLease)) return;
      setToast(status === "verified" ? "关系已确认" : "关系已拒绝，后续图推理将忽略它");
      const queue = await fetchEdgeReviewQueue(modalLease.owner.notebookId);
      if (!rootModals.owns(modalLease)) return;
      setEdgeQueue(queue);
    } catch (error) {
      if (rootModals.owns(modalLease)) throw error;
    } finally {
      if (edgeOperationRef.current === operation) {
        edgeOperationRef.current = null;
        setEdgeBusy(false);
      }
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
      void kgWorkspace.enterKnowledge();
      // 提交晋升要知道本笔记本挂了几个公共知识库(resolvePromotionTarget)。/bases
      // 是 owner-only 端点,非 owner 404(见 notebook-bases.ts 顶部注释)——不能像
      // loadKnowledgeTypes 那样对所有访客无条件调用,这里显式门控 canGovernKnowledge。
      if (currentNotebookId && capabilities.canGovernKnowledge) {
        const notebookId = currentNotebookId;
        const actorId = currentUser?.id ?? null;
        const workspaceEpoch = workspaceEpochRef.current;
        listBases(notebookId).then((bases) => {
          if (
            actorId
            && workspaceActorIdRef.current === actorId
            && activeNotebookIdRef.current === notebookId
            && workspaceEpochRef.current === workspaceEpoch
          ) {
            setCurrentNotebookBases(bases);
          }
        }).catch((error) => {
          if (
            actorId
            && workspaceActorIdRef.current === actorId
            && activeNotebookIdRef.current === notebookId
            && workspaceEpochRef.current === workspaceEpoch
          ) {
            reportError(error);
          }
        });
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

  /**
   * Root-modal coordinator owns only presentation leases.  Domain payload and
   * cleanup remain here (or in their existing hook/component owner), and this
   * exhaustive close sink is deliberately forbidden from issuing HTTP or
   * executing a business command.
   */
  function handleRootModalClosed(slot: RootModalSlot, _reason: RootModalCloseReason) {
    switch (slot) {
      case "password-change":
      case "search-profile":
      case "understanding":
        return;
      case "notebook-editor":
        notebookCollection.closeEditor();
        return;
      case "notebook-delete":
        notebookCollection.closeDelete();
        return;
      case "source-add":
        resetStagedIntake();
        setLinkSectionOpen(false);
        sourceModalDismissedRef.current = true;
        return;
      case "source-detail":
        sourceLibrary.closeSourceDetail();
        return;
      case "info":
        setInfoModal(null);
        return;
      case "model-service":
        setHighlightedModelServiceId(null);
        return;
      case "notebook-share":
        setShareModal(null);
        return;
      case "shared-preview":
        setSharedPreview(null);
        return;
      case "shared-by-me":
        setSharedByMeList(null);
        return;
      case "memory-save":
        setMemoryAnswerId(null);
        return;
      case "catalog-review":
        setCatalogReview(null);
        setCatalogReviewLease(null);
        return;
      case "conversation-share":
        setSharingSession(null);
        return;
      case "analytics":
        clearAnalyticsData();
        return;
      case "kg-schema":
        kgWorkspace.closeSchemas();
        return;
      case "kg-analysis":
        kgWorkspace.closeAnalysis();
        return;
      case "promotion-queue":
        setPromoQueue(null);
        return;
      case "promotion-target":
        setPendingPromotionObjectId(null);
        return;
      case "edge-review":
        setEdgeQueue(null);
        return;
      case "answer-image-preview":
        setAnswerImagePreview(null);
        return;
      // 插件弹窗的持有者只在这里清空——关闭、被别的 primary 冲突顶掉、切库、换用户
      // 走的都是这条 close sink。少了它，一个已经没有 lease 的持有者会一直留在 state
      // 里（今天只是脏数据，但它正是「那一格归谁」的唯一记录，必须与 lease 同生共死）。
      case "extension":
        extensionDialogHolderRef.current = null;
        setExtensionDialogHolder(null);
        return;
    }
  }

  function openInfoModal(
    modal: InfoModal,
    owner: RootModalOwner | null = rootModals.captureWorkspaceOwner() ?? rootModals.captureActorOwner(),
  ): RootModalLease<"info"> | null {
    const lease = rootModals.open("info", owner);
    if (!lease) return null;
    setInfoModal(modal);
    return lease;
  }

  function closeInfoModal() {
    rootModals.requestClose("info", "button");
  }

  function openMemorySave(answerId: string) {
    if (rootModals.open("memory-save", rootModals.captureWorkspaceOwner())) {
      setMemoryAnswerId(answerId);
    }
  }

  function openAnswerImagePreview(request: AnswerImagePreviewRequest) {
    if (rootModals.open("answer-image-preview", rootModals.captureWorkspaceOwner())) {
      setAnswerImagePreview(request);
    }
  }

  function openCatalogReview(request: CatalogReviewRequest) {
    const lease = rootModals.open("catalog-review", rootModals.captureSourceOwner());
    if (lease) {
      setCatalogReviewLease(lease);
      setCatalogReview(request);
    }
  }

  function openConversationShare(target: { id: string; title: string; throughAnswerId: string }) {
    if (rootModals.open("conversation-share", rootModals.captureWorkspaceOwner())) {
      setSharingSession(target);
    }
  }

  // 头像菜单「高级模式」开关：调 PATCH /me/ui-mode 切换，成功后用返回的完整用户
  // 档案覆盖本地态(normalizeUiMode 已在 auth.ts 里做过，这里不必再判)；失败保持
  // 原态不切、走既有 toast 错误通道——绝不能让界面显示的开关态与服务端不一致。
  async function handleToggleAdvancedMode() {
    if (!currentUser) return;
    const next: UiMode = isAdvanced(uiMode) ? "auto" : "advanced";
    try {
      const updated = await updateUiMode(next);
      setCurrentUser(updated);
    } catch (error) {
      setToast(toUserMessage(error, "切换界面模式没成功，请稍后重试"));
    }
  }

  async function handleLogout() {
    workspaceEpochRef.current += 1;
    invalidateNotebookOpenGuard();
    leaveActorOwners();
    titleSaveOperationRef.current = null;
    setTitleSaveInFlight(false);
    sourceLibrary.beginTransition();
    uploadRequestOwnerRef.current = null;
    urlRequestOwnerRef.current = null;
    activeNotebookIdRef.current = null;
    memorySessionAbortRef.current.abort();
    memoryLinksAbortRef.current?.abort();
    memoryLinksAbortRef.current = null;
    setMemoryAnswerId(null);
    await logoutUser();
    setCurrentUser(null);
    window.location.reload();
  }

  function openModelPanel(serviceId: string | null = null) {
    if (!rootModals.open("model-service", rootModals.captureActorOwner())) return;
    setHighlightedModelServiceId(serviceId);
    void refreshModelStatus();
  }

  function closeModelPanel(reason: "button" | "backdrop" | "escape" = "button") {
    rootModals.requestClose("model-service", reason);
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
    notebookCollection.openMenu(notebookId, event.currentTarget.getBoundingClientRect());
  }

  const isWorkspace = Boolean(currentNotebookId && currentNotebook);
  // 共享进来的库(Phase 2):`access === "reader"`。⚠ 它现在**只**决定「顶栏要不要
  // 显示来源徽章/退出入口」这类**身份**呈现,不再等同于「不能写」——群组知识共享 P2
  // 之后,组管理员打开被共享进本组的库时 access 仍是 reader,却持有内容管理权。
  // 写入口一律看下面的 `readOnlyWorkspace`(由 workspaceCapabilities 派生)。
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
  // 文件夹/兼容 intake 在飞，或正等待用户在下方勾选要添加的 markdown：两者都必须禁用整个
  // 添加文件入口（红线：长任务按钮的忙碌态）——后者同样要禁，否则再拖入一个 zip 会
  // 覆盖用户还没确认完的第一次选择（bundleChoiceResolveRef 声明处有完整说明）。
  const bundleProcessing = bundleBusyLabel !== null || bundleChoice !== null;
  const sourceFilePickerDisabled = docCapacity.atCapacity
    || sourceUploadConfigLoading
    || sourceBatchAtCapacity
    || bundleProcessing;
  const sourceFilePickerHint = docCapacity.atCapacity
    ? atDocCapacityHint
    : sourceUploadConfigLoading
      ? "正在读取单文件上传上限…"
      : sourceBatchAtCapacity
        ? `单次最多上传 ${sourceUploadMaxFilesPerBatch} 个文件，请先上传当前批次。`
        : bundleBusyLabel
          ? bundleBusyLabel
          : bundleChoice
            ? "请先在下方选择要添加的 Markdown 文件"
            : undefined;
  // 禁用态下拖入文件时写进「已跳过」的原因。刻意与 sourceFilePickerHint 分开：那份
  // 提示在解包途中是一句进行态文案（「解析压缩包…」），把它当跳过原因写进持久列表，
  // 用户读到的是「这个文件被跳过了，原因：解析压缩包…」——答非所问。
  const sourceFilePickerSkipReason = docCapacity.atCapacity
    ? atDocCapacityHint
    : sourceUploadConfigLoading
      ? "正在读取上传限制，请稍后重试"
      : sourceBatchAtCapacity
        ? `单次最多上传 ${sourceUploadMaxFilesPerBatch} 个文件，请先上传当前批次`
        : bundleProcessing
          ? "正在处理已拖入的压缩包或文件夹，请稍后重试"
          : "当前无法添加文件";
  const stagedUploadBlockedReason = documentUploadBlockReason(docCapacity, stagedFiles.length);
  const capabilities = workspaceCapabilities(
    currentNotebook?.access,
    currentUser?.role ?? "",
    currentNotebook?.can_manage_content ?? false,
  );
  const notebookEditorIndexingNotice = describeIndexingPipelineState(
    notebookCollection.editor?.indexingPipeline ?? null,
  );
  const notebookEditorSelectedPipeline = selectedIndexingPipelineOption(
    notebookCollection.editor?.indexingPipeline ?? null,
    notebookCollection.editor?.selectedPipelineId,
  );
  const workspaceExtensionPermissions = {
    notebookRead: Boolean(currentNotebookId && currentNotebook),
    notebookWrite: capabilities.canWriteNotebook,
    notebookConfigure: capabilities.canConfigureNotebook,
    sourceRead: false,
    sourceWrite: false,
    systemAdmin: capabilities.canManageGlobalSchemas,
  } as const;
  const workspaceExtensionActions = createOwnedWorkspaceExtensionActions(
    workspaceExtensions.owner,
    workspaceExtensions.owns,
    () => {
      rootModals.open("understanding", rootModals.captureWorkspaceOwner());
    },
    // 只用 sourceLibrary 自己的具名 command，不顺带 loadNotebookCollection /
    // revalidateAskAvailability / reloadCheckup——一个插件新增来源后 parse_status
    // 未到 extracted 时，use-source-library.ts 里由 `hasPending` 门控的解析轮询
    // effect 会接手，并在它的 `reachedExtracted` 分支里跑那三个刷新；这里再塞一遍
    // 会让插件一次动作变成四个请求，还与 source-poll-refresh-guard 钉的既有节奏重复。
    () => sourceLibrary.loadSourcesPage(sourceLibrary.currentPageRequest()),
    // 插件弹窗认领那唯一一格 `extension` slot：**先由协调器裁决，成功了才记持有者**。
    // 反过来先 setHolder 会留下一个永远等不到 lease 的持有者（弹窗不会开，而那一格
    // 在界面上已经"归它了"）。持有者**换人**时（codex #578 R7 P2）：owner 没变，
    // `rootModals.open()` 会把旧持有者的 lease 原样递回来——那份 lease 捕获的
    // `returnFocus` 还是旧持有者的触发按钮，新弹窗关闭后焦点会跑回旧持有者而不是
    // 刚点开它的那个按钮。所以先按当时持有者 `requestClose`，让协调器把此刻的
    // `document.activeElement`（新持有者刚点的按钮）记成新的归还目标，再申请一份
    // 属于新持有者的 lease；`requestClose` 同步触发 `onClosed`（`removeWithoutRender`
    // 的 `notify=true` 分支直接调用，没有 effect/微任务缝隙），下一行执行时 ref
    // 已经清空，不会把这次 open 误判成同一持有者的幂等重开。
    (contributionId: string) => {
      if (extensionDialogHolderRef.current !== null && extensionDialogHolderRef.current !== contributionId) {
        rootModals.requestClose("extension", "button");
      }
      if (rootModals.open("extension", rootModals.captureWorkspaceOwner())) {
        extensionDialogHolderRef.current = contributionId;
        setExtensionDialogHolder(contributionId);
      }
    },
    // 关闭只经协调器，且**只有当时的持有者关得掉**（codex #578 R1 P2）：一次迟到的
    // `closeDialog()` 否则会关掉别的插件刚开的弹窗。判据读 ref 而不是渲染闭包里的
    // state——那份闭包停在发放回调的那一帧，正好看不见「已经易主」这件事。持有者的
    // 清空仍挂在 `handleRootModalClosed` 那条唯一出口上，这里不额外 setHolder(null)。
    (contributionId, reason) => {
      if (extensionDialogHolderRef.current !== contributionId) return;
      rootModals.requestClose("extension", reason);
    },
  );
  // 内容管理入口的**唯一**判据(群组知识共享 P2)。此前这些入口写的是 `!isReader`,
  // 而 P2 把六个内容管理能力从 owner-only 翻成「owner ∪ 组管理边」——再按 access 判
  // 就会让组管理员看到一个 API 全部允许、界面却全部藏起来的只读工作区。
  // 判据统一走 workspaceCapabilities,不在这里第二次拼 access/can_manage_content。
  const readOnlyWorkspace = !capabilities.canWriteNotebook;
  // 挂了几个公共知识库决定「提交晋升」按钮的行为(none=禁用/auto=直接用/choose=弹选择器)。
  const promotionTarget = resolvePromotionTarget(currentNotebookBases);
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

  // 笔记本列表的两个分区(设计决策 10):自有/只读共享 一区,经**群组**共享进来的
  // 单独一区。判据是 granted_via 非空而不是 access —— 只读共享同样是 reader,但它
  // 有「退出共享」这个用户自己能按的出口,群组共享没有(那个按钮打的是成员表,对
  // 授权边一点作用都没有),两者必须分开。
  const notebookPartition = partitionByGrant(notebookCollection.visibleRows);

  // 两个分区渲染的是同一种卡片,所以抽成一个函数 —— 复制一份 JSX 必然分叉。
  const renderNotebookCard = (
    { notebook, hits }: { notebook: NotebookSummary; hits: SearchHit[] },
    index: number,
  ) => (
    <article key={notebook.id} className={`notebook-card ${cardTone(index)}`}>
      <button className="card-menu" onClick={(event) => openNotebookMenu(notebook.id, event)} title="笔记本操作">⋮</button>
      {/* 动作发出后立即禁用 + 忙碌指示(docs/development.md「长任务控件」硬约束)。
          禁用后基线 :active 反馈按设计不再适用——spinner + 「打开中…」就是反馈。
          ⋮ 菜单与「N 条记忆」是另外的动作,不在这颗按钮里,照旧可点。 */}
      <button
        className={`notebook-card-main${openingNotebookId === notebook.id ? " is-opening" : ""}`}
        aria-busy={openingNotebookId === notebook.id || undefined}
        disabled={openingNotebookId === notebook.id}
        onClick={() => openNotebook(notebook.id).catch(reportError)}
      >
        <div className="card-icon">{cardIcon(index, notebook)}</div>
        <div>
          <h2>{notebook.name}</h2>
          <p>{notebook.purpose || "No purpose set yet."}</p>
          {openingNotebookId === notebook.id && (
            <p className="notebook-card-open-status">
              <span className="notebook-card-open-spinner" aria-hidden="true" />
              打开中…
            </p>
          )}
          {isGroupGranted(notebook) && (
            <p className="notebook-card-meta">{grantedViaLabel(notebook)}</p>
          )}
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
  );

  // 启动就绪门:在认证/加载分支之前拦截。未就绪时只展示启动屏,绝不露出登录表单或空白挂起。
  if (!serviceReady) return <StartingScreen snapshot={readySnapshot} onRetry={() => setReadyRetry((n) => n + 1)} />;
  if (!authChecked) return <div className="auth-gate"><div className="auth-card">加载中…</div></div>;
  if (!currentUser) {
    return <AuthGate onAuthenticated={(u) => {
      activateWorkspaceOwners(u.id);
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
            {/* 群组页把这一格当作页面标题:它是独立的集合层页面,标题「群组」原本以 28px
                躺在顶栏下方 44px 空白之后,而顶栏同时还在说「群组工作台」——同一句话说了
                两遍、中间隔着一大块空白。移上来之后页面里就没有别的标题了,所以这里必须
                真的渲染成 <h1>(而不是看起来像标题的 div),否则整页没有标题层级。
                字号不变,仍是 .brand-title 的 16px。 */}
            {outerView === "groups" && !isWorkspace
              ? <h1 className="brand-title">群组</h1>
              : <div className="brand-title">silicon-notebook</div>}
            <div className="brand-subtitle">{isWorkspace ? "笔记本工作区" : outerView === "memory" ? "私有记忆" : outerView === "groups" ? "成员、共享与审批" : "笔记本列表"}</div>
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
              // ⚠ 只读成员不冒此提醒:他们能看健康信息但所有修复 CTA 都 !readOnlyWorkspace 隐藏了,
              // 「发现可修复的问题」对他们是无可点动作的噪音(评审)。
              const sig = checkupAlertSignature(checkup);
              if (!sig || !currentNotebook || readOnlyWorkspace) return null;
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
            canChangePassword={currentUser.id !== "user-local"}
            advancedMode={isAdvanced(uiMode)}
            searchProfileEnabled={userSearchProfileEnabled}
            onOpenMemory={showGlobalMemory}
            onOpenGroups={() => showGroups({}, "push")}
            onToggleAdvancedMode={() => handleToggleAdvancedMode().catch(reportError)}
            onOpenSearchProfile={() => {
              // codex #535 R4→R10 P2:先等 /me 刷新**落定**再开弹窗——边开边刷
              // 会让用户在慢网络下基于陈旧值完成「设为你的选择」,把旧推断值
              // 以 user 来源写回,或让迟到的刷新盖掉 PATCH 刚返回的本地态。
              // 刷新失败照常打开(fail-open,设置入口不依赖一次网络往返成功)。
              const lease = rootModals.issue("search-profile", rootModals.captureActorOwner());
              if (!lease) return;
              void fetchMe()
                .then((user) => {
                  if (rootModals.leaseIsCurrent(lease)) setCurrentUser(user);
                })
                .catch(() => undefined)
                .finally(() => { rootModals.publish(lease); });
            }}
            onChangePassword={() => { rootModals.open("password-change", rootModals.captureActorOwner()); }}
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
                <button key={id} className={`tab ${notebookCollection.filter === id ? "active" : ""}`} onClick={() => notebookCollection.selectFilter(id)}>
                  {label}
                </button>
              ))}
            </div>
            <div className="library-actions">
              <div className={`collection-search ${notebookCollection.searchQuery ? "search-open" : ""}`}>
                <button className="icon-button" title="Search">⌕</button>
                <input value={notebookCollection.searchQuery} onChange={(event) => notebookCollection.updateSearchQuery(event.target.value)} type="search" placeholder="搜索笔记本、来源、元素" />
              </div>
              <div className="segmented" aria-label="View mode">
                {[
                  { id: "grid", icon: <LayoutGrid size={16} />, title: "卡片视图" },
                  { id: "list", icon: <ListIcon size={16} />, title: "列表视图" },
                ].map(({ id, icon, title }) => (
                  <button key={id} className={notebookCollection.viewMode === id ? "active" : ""} title={title} aria-label={title} onClick={() => notebookCollection.selectView(id)}>
                    {icon}
                  </button>
                ))}
              </div>
              <div className="sort-menu-wrap">
                <button className="sort-button" onClick={notebookCollection.toggleSort}>
                  {notebookCollection.sortMode === "name" ? "名称 ▾" : notebookCollection.sortMode === "sources" ? "来源 ▾" : "最近 ▾"}
                </button>
                <div className={`popover sort-menu ${notebookCollection.sortOpen ? "" : "hidden"}`}>
                  {[
                    ["recent", "最近创建"],
                    ["name", "名称"],
                    ["sources", "来源数量"]
                  ].map(([id, label]) => (
                    <button key={id} onClick={() => notebookCollection.selectSort(id)}>{label}</button>
                  ))}
                </div>
              </div>
              <button className="sort-button" title="查看我分享出去的笔记本及其只读成员" onClick={() => openSharedByMe().catch(reportError)}>
                <Share2 size={15} /> 已分享
              </button>
              <button className="new-pill" disabled={notebookCollection.creating} onClick={() => { void notebookCollection.createDefaultNotebook(); }}>＋ 新建</button>
            </div>
          </section>

          <section className="collection-title">
            <h1>我的笔记本</h1>
            {notebookCollection.searchQuery && <p>{notebookCollection.visibleRows.length} 个笔记本，搜索 “{notebookCollection.searchQuery}”</p>}
          </section>

          <section className={`notebook-grid view-${notebookCollection.viewMode}`}>
            {notebookCollection.viewMode === "list" ? (
              <NotebookList
                entries={notebookPartition.personal}
                openingNotebookId={openingNotebookId}
                openNotebook={(id) => openNotebook(id).catch(reportError)}
                openMemory={(id) => openNotebookMemory(id).catch(reportError)}
                openMenu={openNotebookMenu}
              />
            ) : (
              <>
                {!notebookCollection.searchQuery && notebookCollection.filter !== "featured" && (
                  <button className="notebook-card create-card" disabled={notebookCollection.creating} onClick={() => { void notebookCollection.createDefaultNotebook(); }}>
                    <div className="create-circle">＋</div>
                    <h2>新建笔记本</h2>
                  </button>
                )}
                {notebookPartition.personal.map(renderNotebookCard)}
              </>
            )}
            {notebookCollection.visibleRows.length === 0 && (
              <article className="empty-state">
                <strong>没有找到笔记本</strong>
                <p>换一个关键词，或回到“我的笔记本”创建新的笔记本。</p>
              </article>
            )}
          </section>

          {/* 「群组」分区(设计决策 10)。经群组共享进来的库单独成一区,而不是混进
              上面那批——它们不是「我的」,也没有「退出共享」这个自己能按的出口。 */}
          {notebookPartition.group.length > 0 && (
            <>
              <section className="collection-title">
                <h1>群组</h1>
                <p>经群组共享给你的知识库。可以打开、提问、写自己的深度报告，也可以挂为参考库；由组管理员管理。</p>
              </section>
              <section className={`notebook-grid view-${notebookCollection.viewMode}`}>
                {notebookCollection.viewMode === "list" ? (
                  <NotebookList
                    entries={notebookPartition.group}
                    roleText="群组成员"
                    openingNotebookId={openingNotebookId}
                    openNotebook={(id) => openNotebook(id).catch(reportError)}
                    openMemory={(id) => openNotebookMemory(id).catch(reportError)}
                    openMenu={openNotebookMenu}
                  />
                ) : (
                  notebookPartition.group.map(renderNotebookCard)
                )}
              </section>
            </>
          )}
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

      {!isWorkspace && outerView === "groups" && currentUser && (
        <GroupsPage
          currentUserId={currentUser.id}
          isSystemAdmin={currentUser.role === "admin"}
          notebooks={notebookCollection.rows}
          initialGroupId={groupNavigation.groupId}
          initialTab={groupNavigation.tab}
          onBack={showCollection}
          onChanged={() => { notebookCollection.refreshAfterAccessChange().catch(reportError); }}
          // 群组页与集合页同权的忙碌反馈。
          openingNotebookId={openingNotebookId}
          onOpenNotebook={(notebookId) => { void openNotebook(notebookId); }}
          onNavigate={(groupId, nextTab) => {
            setGroupNavigation({ groupId, tab: nextTab });
            window.history.replaceState(null, "", groupsHash(groupId, nextTab));
          }}
        />
      )}

      {isWorkspace && currentNotebook && (
        <main className="notebook-view">
          <section className="workspace-header">
            <div className="workspace-title">
              <button className="back-home-button" onClick={() => showCollection()}>
                <ArrowLeft size={16} />
                <span>返回主页</span>
              </button>
              <div className="workspace-title-main">
                {isReader ? (
                  <ReaderNotebookBadge
                    notebook={currentNotebook}
                    leaveBusy={leaveBusy}
                    onLeave={() => { handleLeaveShared().catch(reportError); }}
                    // 组管理员(can_manage_content)可在顶栏改名(PATCH-only,notebook:manage)。
                    // 徽章按 can_manage_content 才渲染成可编辑;纯只读成员传了也只显示 h1。
                    rename={{
                      value: titleDraft,
                      saving: titleSaveInFlight,
                      onChange: setTitleDraft,
                      onCommit: () => { saveInlineNotebookName().catch(reportError); },
                      onReset: () => setTitleDraft(currentNotebook.name),
                    }}
                  />
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
              <button className="workspace-primary-action" disabled={notebookCollection.creating} onClick={() => { void notebookCollection.createDefaultNotebook(); }}>
                <Plus size={18} strokeWidth={2.8} />
                <span>创建笔记本</span>
              </button>
              <div className="workspace-nav-group">
                {!readOnlyWorkspace && (
                  <button className="workspace-nav-button" onClick={() => {
                    const tier = tierActionState(currentNotebook);
                    // 参考库列表随 NotebookSummary.base_notebooks 一起返回(owner/reader 都能看到,
                    // 权威只读)。弹窗顶部只读展示本笔记本挂了哪些参考库;没挂时不显示该段落。
                    const baseNames = (currentNotebook?.base_notebooks ?? []).map((b) => b.name);
                    openInfoModal({
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
                {!readOnlyWorkspace && (
                  <button className="workspace-nav-button" disabled={shareBusy} onClick={() => openShareModal().catch(reportError)}>
                    <Share2 size={17} />
                    <span>分享</span>
                  </button>
                )}
                <button className="workspace-nav-button" onClick={() => openModelPanel()}>
                  <Cpu size={17} />
                  <span>模型服务</span>
                </button>
                {/* 设置弹窗现在分成两档：内容管理者可改笔记本信息与索引管线，owner 额外
                    看到参考库挂载。这样 group admin 能完成 indexing-pipeline 选择，而
                    owner-only 的 configure I/O 仍不会对他发起。 */}
                <button className="workspace-nav-button" onClick={() => {
                  if (capabilities.canWriteNotebook) {
                    void presentNotebookEditor(currentNotebook.id);
                  } else {
                    void openReadOnlyNotebookSettings();
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
                    onClick={() => sourceLibrary.setSourcesCollapsed(true)}
                  >
                    <PanelLeftClose size={18} />
                  </button>
                </div>
              </div>
              <div className="workspace-panel-body sources-body">
                {!readOnlyWorkspace && (
                  <button type="button" className="add-source-button" onClick={() => { setLinkSectionOpen(false); openSourceModal(); }}>

                    <Plus size={20} strokeWidth={2.7} /> 添加来源
                  </button>
                )}
                {/* 显示门:仅当存在信息不完整的论文(或补全任务进行中)才显示;
                    后端单库快照 paper_meta_missing 为 false 且可见页无 missing 来源
                    时隐藏。判据是纯函数 showPaperMetaBackfill(有单测)。 */}
                {!readOnlyWorkspace && showPaperMetaBackfill(currentNotebook, sources, backfillingMeta) && (
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
                      // 请求发出前就捕获 notebook/epoch:响应回来时用户可能已切到
                      // 别的库并发起了它自己的补全,迟到的 queued===0 / 失败若不加
                      // 守卫会把**那个库**的 backfillingMeta 清掉、停掉它的完成轮询
                      // (codex R3 P2);快照/来源页刷新(R1/R2)也复用同一份捕获。
                      const nb = currentNotebookId;
                      const workspaceEpoch = workspaceEpochRef.current;
                      const stillCurrent = () => workspaceRequestIsCurrent(
                        false,
                        workspaceEpoch,
                        workspaceEpochRef.current,
                        nb,
                        activeNotebookIdRef.current,
                      );
                      setBackfillingMeta(true);
                      try {
                        const res = await backfillPaperMetadata(nb);
                        if (!stillCurrent()) return;
                        if (res.queued === 0) {
                          setBackfillingMeta(false);
                          setToast("论文信息已是最新，无需补全");
                          // 显示门数据已漂移(按钮显示着但确无活可干):单库快照与可见
                          // 来源页**都**要刷——showPaperMetaBackfill 取两者并集,别的
                          // 标签页补完时本页 sources 里陈旧的 paper_meta_status=
                          // "missing" 行会压过 paper_meta_missing=false,只刷快照按钮
                          // 收不起来(codex R1 P2)。翻页/搜索/切库守卫与上方补全完成
                          // 轮询同一套(refs + workspaceEpoch,codex R2 P2)。
                          getNotebook(nb).then((refreshed) => {
                            if (!stillCurrent()) return;
                            setCurrentNotebook((cur) => (cur && cur.id === refreshed.id ? refreshed : cur));
                          }).catch(() => {});
                          loadSourcesPage(nb, {
                            ...sourceLibrary.currentPageRequest(),
                            guard: stillCurrent,
                          }).catch(() => {});
                        } else {
                          setToast(`已提交 ${res.queued} 篇论文的信息补全`);
                          // 保持 backfillingMeta=true;完成检测交给轮询 effect。
                        }
                      } catch (err) {
                        if (!stillCurrent()) return;
                        setBackfillingMeta(false);
                        reportError(err);
                      }
                    }}
                  >
                    {backfillingMeta ? "补全中…" : "补全论文信息"}
                  </button>
                )}
                {!readOnlyWorkspace && currentNotebookId && sources.length > 0 && (
                  currentNotebook?.kg_ready
                    ? (
                      <>
                        {(currentNotebook?.kg_pending_sources ?? 0) > 0
                          ? (
                            <>
                              <button
                                type="button"
                                className="add-source-button"
                                disabled={kgGraph.buildingKg}
                                title="有新增来源尚未分析，点击分析新增内容并合并进知识图谱"
                                onClick={() => { if (currentNotebookId) startKgBuild(currentNotebookId); }}
                              >
                                <Network size={20} strokeWidth={2.7} /> {kgGraph.buildingKg ? "分析中…" : `分析新增 ${currentNotebook.kg_pending_sources ?? "?"} 篇并合并`}
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
                          disabled={kgGraph.buildingKg}
                          title={currentNotebook?.base_kg_available
                            ? `本笔记本尚未整理知识图谱，${strictLabel}会借用已挂载的参考库；点击为本笔记本单独整理`
                            : `本笔记本尚未整理知识图谱，也没挂参考库；「${strictLabel}」需先整理知识图谱或挂一个参考库`}
                          onClick={() => { if (currentNotebookId) startKgBuild(currentNotebookId); }}
                        >
                          <Network size={20} strokeWidth={2.7} /> {kgGraph.buildingKg ? "整理中…" : "整理知识图谱"}
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
                      kgGraph.buildingKg,
                      readOnlyWorkspace,
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
                        : v.state === "queued" ? queuedScheduleHint(s, new Date())
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
                {/* workspace.side_panel 扩展点的落点。三条理由:
                    ① **不再是工作区的第三列**——一整列只放一个入口,其余整列空白,
                       还把问答面板挤窄;这里它与「添加来源」「整理知识图谱」同属
                       笔记本级动作,视觉上本来就是一排。
                    ② **落在 .sources-body 的固定区、且排在 `.source-list` 之前**:
                       唯一会滚的容器是 `.source-list` 自己(flex:1 1 auto / overflow:auto),
                       只有放进它内部才会跟着来源行滚走;排在它之前是为了与「添加来源」
                       同排、短视口下也不会先被裁掉(列表才是让位的那个)。
                    ③ **排在检索范围之上**:从 .source-scope-toolbar 往下(检索范围 →
                       参考库 → 本库来源)是一整组「范围」,插进中间会把它切断。
                    只读成员同样看得见——后端四个端点都走 require_notebook_read,
                    「本人那一份」恰是只读成员唯一能写的东西(可见性判据仍在 host
                    的 permission/availability 门里,不在这里)。来源栏收起时入口随
                    整栏一起隐藏(视觉隐藏、仍可 Tab 聚焦——与整个来源栏既有机制一致):
                    这是收起来源栏本来的语义,已登记接受。 */}
                {currentUser && currentNotebook && workspaceExtensions.ownerKey && (
                  <WorkspaceExtensionOutlet
                    slot="workspace.side_panel"
                    registry={WORKSPACE_UI_CONTRIBUTIONS}
                    projection={workspaceExtensionProjection}
                    ownerKey={workspaceExtensions.ownerKey}
                    actions={workspaceExtensionActions}
                    dialog={rootModals.view("extension")}
                    dialogHolder={extensionDialogHolder}
                    context={{
                      slot: "workspace.side_panel",
                      actor: {
                        id: currentUser.id,
                        username: currentUser.username,
                        displayName: currentUser.display_name,
                      },
                      notebook: { id: currentNotebook.id, name: currentNotebook.name },
                      source: null,
                      uiMode,
                      permissions: workspaceExtensionPermissions,
                    }}
                  />
                )}
                <div className="source-scope-toolbar" role="group" aria-label="问答与深度报告检索范围">
                  {/* title 带上完整计数：窄面板下这行会被省略号截断，不重复一遍就没处看。 */}
                  <span
                    className="retrieval-scope-count"
                    title={`检索范围 · ${retrievalScopeText}（勾选的来源与参考库才会参与问答和深度报告检索）`}
                  >
                    检索范围 · {retrievalScopeText}
                  </span>
                  {/* 自动模式固定全选、不给这两个按钮：勾选/清空的操作面就是要隐藏的
                      那部分「配置」。范围计数文案仍显示（effective 值恒为全选，与隐藏掉
                      的勾选框视觉上一致），只是没有交互入口。 */}
                  {isAdvanced(uiMode) && (
                    <>
                      <button
                        type="button"
                        disabled={askInFlight || (notebookSourceTotal === 0 && !hasMountedBase)}
                        onClick={() => {
                          sourceLibrary.selectAllSources();
                          setBaseScopeSelection(defaultBaseScopeSelection());
                        }}
                      >全选</button>
                      {/* 「全选」/「清空」必须一并管参考库 —— 只清本库来源就是本次事故的
                          翻版：用户以为范围空了，参考库还在整份参与检索。 */}
                      <button
                        type="button"
                        disabled={askInFlight || (
                          selectedLocalSourceCount === 0 && selectedBaseNotebookCount === 0
                        )}
                        onClick={() => {
                          sourceLibrary.clearSourceSelection();
                          setBaseScopeSelection({ allSelected: false, ids: new Set() });
                        }}
                      >清空</button>
                    </>
                  )}
                </div>
                {hasMountedBase && isAdvanced(uiMode) && (
                  <section className="scope-group" aria-label="参考库检索范围">
                    <h3 className="scope-group-title">参考库</h3>
                    <div className="base-scope-list">
                      {mountedBases.map((base) => {
                        const included = baseIsSelected(baseScopeSelection, base.id);
                        return (
                          <label
                            key={base.id}
                            className="base-scope-row"
                            title={`${base.name}${included
                              ? " — 此参考库会参与问答与深度报告检索"
                              : " — 此参考库不会参与问答与深度报告检索"}`}
                          >
                            <input
                              type="checkbox"
                              checked={included}
                              disabled={askInFlight}
                              aria-label={`检索参考库：${base.name}`}
                              onChange={() => setBaseScopeSelection((previous) => (
                                toggleBaseSelection(previous, base.id)
                              ))}
                            />
                            <span className="base-scope-name">{base.name}</span>
                            {base.tier === "base" && (
                              <span className="base-scope-badge" title="公共知识库">公共</span>
                            )}
                          </label>
                        );
                      })}
                    </div>
                  </section>
                )}
                {/* 标题 + 搜索框成组，但**不**把 .source-list 包进来：那个列表靠
                    .sources-body 上的 flex:1 1 auto / min-height:0 / overflow:auto 拿到
                    剩余高度并自己滚动，套一层就得把这套算术原样复制一遍。列表用
                    aria-labelledby 挂回标题，分组语义不丢。
                    搜索框是从整个面板最顶上挪下来的：它过去悬在参考库之上，让人误以为
                    能一并搜到参考库里的内容，而它只查当前笔记本。 */}
                <div className="scope-group">
                  <h3 className="scope-group-title" id="local-source-scope-title">本库来源</h3>
                  <input
                    className="source-search"
                    type="search"
                    placeholder="搜索来源（标题/作者/文件名）"
                    value={sourceQuery}
                    onChange={(e) => sourceLibrary.setSourceQuery(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" && currentNotebookId) {
                        loadSourcesPage(currentNotebookId, { page: 0, q: sourceQuery }).catch(reportError);
                      }
                    }}
                  />
                </div>
                {/* role="group" 是 aria-labelledby 的生效条件：挂在无 role 的通用 div
                    上，辅助技术基本会忽略这条标注，分组语义等于没接。它不影响布局
                    （这个 div 的滚动算术在 .sources-body 上，见上面那段注释）。 */}
                <div className="source-list" role="group" aria-labelledby="local-source-scope-title">
                  {sources.length === 0 ? (
                    <article className="source-empty">
                      <div>▧</div>
                      <strong>已保存的来源将显示在此处</strong>
                      <p>点击上方的“添加来源”导入 PDF、Markdown、DOCX 或 PPTX。</p>
                    </article>
                  ) : (
                    sources.map((source) => {
                      const deletingSource = deletingSourceIds.has(source.id);
                      const kgBadge = sourceKgBadge(source);
                      return (
                      <div
                        key={source.id}
                        className={`source-row compact-source-row${isAdvanced(uiMode) ? "" : " source-row--no-select"}${deletingSource ? " source-row--deleting" : ""}`}
                        title={source.title}
                        aria-busy={deletingSource || undefined}
                      >
                        {isAdvanced(uiMode) && (
                          <label
                            className="source-scope-check"
                            title={sourceIsSelected(sourceScopeSelection, source.id)
                              ? "此来源会参与问答与深度报告检索"
                              : "此来源不会参与问答与深度报告检索"}
                          >
                            <input
                              type="checkbox"
                              checked={sourceIsSelected(sourceScopeSelection, source.id)}
                              disabled={deletingSource || askInFlight}
                              aria-label={`检索来源：${source.title}`}
                              onChange={() => sourceLibrary.toggleSource(source.id)}
                            />
                          </label>
                        )}
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
                              className={kgBadge.className}
                              title={kgBadge.title}
                            >
                              {kgBadge.label}
                            </span>
                          )}
                          {source.agent_created && (
                            <span
                              className="source-agent-badge"
                              title="由 Agent 通过接入通道添加的来源"
                            >
                              Agent 添加
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
                          {!readOnlyWorkspace && (
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
                onClick={() => sourceLibrary.setSourcesCollapsed(false)}
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
                      className={`chat-tab ${chatMode === mode ? "active" : ""}${mode === "reports" && sourceScopeBlocked ? " scope-disabled" : ""}`}
                      aria-disabled={mode === "reports" && sourceScopeBlocked || undefined}
                      title={mode === "reports" && sourceScopeBlocked
                        ? "当前检索范围为空；仍可查看已有报告，但不能生成新报告"
                        : undefined}
                      onClick={() => switchChatMode(mode)}
                    >{label}</button>
                  ))}
                </div>
                <div className="chat-header-actions">
                  {chatMode === "ask" && (
                    <AskSessionHeaderActions
                      sessionCount={sessions.length}
                      sessionPanelOpen={sessionPanelOpen}
                      onToggleSessionPanel={askSession.toggleSessionPanel}
                      onStartNewSession={startNewAskSession}
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
                      <button className="icon-button compact" type="button" onClick={askSession.closeSessionPanel} title="关闭">
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
                    <button className={`chat-session-card new ${conversationId == null ? "active" : ""}`} type="button" onClick={startNewAskSession}>
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
                            {/* 刻意没有 maxLength:那把尺数的是 UTF-16 code unit,与后端
                                Pydantic 的码点口径对不上,而且它是**静默裁剪**——两条都
                                是红线(codex #525 R2/R3)。超限时输入框保持可编辑,用户
                                正要做的就是把它改短。 */}
                            <input
                              autoFocus
                              value={sessionTitleDraft}
                              aria-invalid={sessionTitleOverLimit !== null}
                              onChange={(event) => askSession.updateSessionTitleDraft(event.target.value)}
                              onKeyDown={(event) => {
                                if (event.key === "Enter" && !sessionTitleOverLimit) askSession.commitRenameSession(session.id).catch(reportError);
                                if (event.key === "Escape") askSession.cancelRenameSession();
                              }}
                            />
                            <button type="button" title="保存" disabled={sessionTitleOverLimit !== null} onClick={() => askSession.commitRenameSession(session.id).catch(reportError)}><Check size={15} /></button>
                            <button type="button" title="取消" onClick={askSession.cancelRenameSession}><X size={15} /></button>
                            {/* 此刻唯一挡着保存键的东西,不写出来用户只看到按钮变灰。 */}
                            {sessionTitleOverLimit && (
                              <span className="chat-session-rename-hint">{sessionTitleOverLimit}</span>
                            )}
                          </div>
                        ) : (
                          <>
                            <button className="chat-session-card-main" type="button" onClick={() => openAskSession(session.id).catch(reportError)}>
                              <span>{session.title || "未命名会话"}</span>
                              <small>
                                {formatRelativeTime(session.updated_at)} · {session.turn_count} 轮
                                {session.used_reasoning && <span className="chat-session-reasoning-badge">{`✦ ${strictLabel}`}</span>}
                              </small>
                            </button>
                            <div className="chat-session-card-actions">
                              <button type="button" title="分享" onClick={() => openConversationShare({ id: session.id, title: session.title || "", throughAnswerId: "" })}><Share2 size={14} /></button>
                              <button type="button" title="重命名" onClick={() => askSession.beginRenameSession(session)}><Edit3 size={14} /></button>
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
                          <button key={label} onClick={() => askSession.submit(prompt).catch(reportError)}>{label}</button>
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
                            onFeedback={(rating) => askSession.submitFeedback(turn.response.answer_id, rating, "").catch(reportError)}
                            onOpenKnowledgeGraph={(objectId, sourceNotebookId) => openKgView(
                              objectId,
                              currentNotebookId,
                              sourceNotebookId || currentNotebookId,
                            )}
                            onOpenKnowhowRow={openKnowhowAt}
                            onOpenSource={onOpenSourceElement}
                            onPreviewImage={openAnswerImagePreview}
                            imagePreviewOpen={rootModals.view("answer-image-preview").open}
                            notebookId={currentNotebookId}
                            notebookNames={notebookNames}
                            // 构建索引的 POST 走 kg:write(admin 档),只读成员点了必 403:
                            // 不下发承接方,AnswerView 会保留横幅诊断、只收起按钮。
                            onBuildScaleIndex={readOnlyWorkspace ? undefined : (() => runScaleIndexOp("build"))}
                            buildingScaleIndex={buildingScaleIndex}
                            scaleIndexStatus={scaleIndexStatus}
                            onSaveMemory={openMemorySave}
                            // 分享到这条回答为止。会话 id 是弹窗的寻址依据,还没落库的
                            // 新会话(conversationId 为 null)不给按钮——那种情形连
                            // `answer_id` 都还没有,弹窗打开也只能 404。
                            onShare={conversationId ? ((answerId) => openConversationShare({
                              id: conversationId,
                              title: sessions.find((session) => session.id === conversationId)?.title || "",
                              throughAnswerId: answerId,
                            })) : undefined}
                            memorySaved={Boolean(memorySavedAnswers[turn.response.answer_id])}
                            onImportGapSuggestion={readOnlyWorkspace ? undefined : importGapSuggestion}
                            // 站外来源建议导入复用「添加来源」弹窗同一份 docCapacity 判据
                            // （红线：确认上传前必须把批次计入文档上限，超额时按钮直接置灰
                            // 并写明原因）——不必先发一次远端 PDF 探测再撞后端必然的容量
                            // 拒绝。onImportGapSuggestion 为 undefined（只读工作区）时按钮
                            // 压根不渲染，这个 prop 传不传都不影响。
                            importGapSuggestionDisabledReason={
                              docCapacity.atCapacity
                                ? atDocCapacityHint
                                // 按笔记本单飞（codex #584 R6/R11）：只有**当前库**有
                                // 建议在导入时才置灰其余按钮——容量竞态是按库的，
                                // A 库在飞的导入不该灰掉 B 库的按钮。
                                : gapImportInFlight.has(currentNotebook.id)
                                  ? "另一条建议正在导入，请稍候"
                                  : undefined
                            }
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
                          {/* 按引擎是否流式推轨迹判断,不按分组:同一分组里未来若混入
                              不流轨迹的模式,挂轨迹面板只会让用户从头到尾盯着一句
                              「等待后端事件…」。 */}
                          {streamsTrace(pendingMode, askModes) ? (
                            <ReasoningTracePanel steps={pendingTrace} live />
                          ) : "思考中…"}
                        </div>
                      </div>
                    )}
                  </div>
                ))}

                {chatMode === "rules" && (
                  <KnowledgeBrowser
                    kind={kgKnowledge.kind}
                    items={kgKnowledge.items}
                    types={kgKnowledge.types}
                    statusFilter={kgKnowledge.statusFilter}
                    duplicates={kgKnowledge.duplicates}
                    contexts={kgKnowledge.contexts}
                    onLoadContext={kgWorkspace.loadKnowledgeContext}
                    onKind={kgWorkspace.selectKnowledgeKind}
                    onStatus={(id, status) => kgWorkspace.updateKnowledge(id, { status })}
                    onOwner={(id, owner) => kgWorkspace.updateKnowledge(id, { owner })}
                    onFindDuplicates={kgWorkspace.findDuplicates}
                    onMerge={kgWorkspace.mergeKnowledge}
                    reload={kgWorkspace.refreshKnowledge}
                    tier={currentNotebook?.tier}
                    onPropose={(id) => submitPromotion(id).catch(reportError)}
                    proposeDisabledReason={promotionTarget.kind === "none" ? "需先挂载一个公共知识库" : undefined}
                    total={kgKnowledge.total}
                    page={kgKnowledge.page}
                    onPage={kgWorkspace.goToKnowledgePage}
                    onStatusFilter={kgWorkspace.selectKnowledgeStatus}
                    readOnly={!capabilities.canGovernKnowledge}
                  />
                )}

                {chatMode === "reports" && currentNotebookId && (
                  <ReportsPanel
                    notebookId={currentNotebookId}
                    workspace={reportWorkspace}
                    uiMode={uiMode}
                    setToast={setToast}
                    readOnly={!capabilities.canManageReports}
                    creationDisabled={sourceScopeBlocked}
                    creationDisabledReason={isAdvanced(uiMode)
                      ? "当前检索范围为空，请至少选择一个来源或参考库"
                      : "请先添加来源，再开始生成报告"}
                    maxSections={reportMaxSections}
                    maxSubqueriesPerSection={reportMaxSubqueriesPerSection}
                  />
                )}

                {chatMode === "memory" && currentNotebookId && (
                  <MemoryPanel
                    scope="notebook"
                    notebookId={currentNotebookId}
                    bases={notebookPromotionBases}
                    sessionSignal={memorySessionAbortRef.current.signal}
                    onOpenSource={onOpenSourceElement}
                  />
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
                      onConfirm={(confirmation) => askSession.confirmIntent(confirmation)}
                      onCancel={askSession.cancelIntent}
                    />
                  )}
                <AskComposer
                  value={question}
                  placeholder={askPlaceholderText}
                  onChange={askSession.releaseQuestion}
                  onSubmit={() => askSession.submit().catch(reportError)}
                  onAbort={askSession.abort}
                  running={asking || intentChecking}
                  abortLabel={intentChecking ? "取消问题理解" : "中断生成"}
                  disabled={askBlocked || sessionLoading || Boolean(askIntentReview)}
                  submitBlocked={askQuestionOverLimit !== null}
                >
                  {/* 与来源页签工具条同一句话的第二处显示 —— 共用 retrievalScopeText，
                      两处不一致在结构上就不可能发生。 */}
                  <span className="retrieval-scope-count" title="勾选的来源与参考库才会参与本次检索">
                    检索范围 · {retrievalScopeText}
                  </span>
                  {/* 超限提示优先于引号回执:它是此刻唯一挡着提交的东西。
                      只在用户真的敲了英文双引号时才出现引号回执:确认哪几段被当成
                      完整短语,或说明为什么这次没识别。判据与后端同一份规则的镜像。 */}
                  {askQuestionOverLimit ? (
                    <span className="chat-hint over-limit">{askQuestionOverLimit}</span>
                  ) : askQuotedPhraseHint ? (
                    <span className="chat-hint">{askQuotedPhraseHint}</span>
                  ) : null}
                  {/* 自动模式只保留问答框：模式、引擎、档位以及相应提示整组不挂载。
                      高级模式继续完整消费用户在前端做出的选择。 */}
                  {isAdvanced(uiMode) && (
                  <div className="ask-mode-control" role="group" aria-label="问答模式">
                    {ASK_MODE_GROUPS.filter((group) => (
                      group.id !== "extension"
                      || (isAdvanced(uiMode) && modesInGroup("extension", askModes).length > 0)
                    )).map((g) => (
                      <button
                        key={g.id}
                        type="button"
                        className={`mode-tab${groupOf(askMode, askModes) === g.id ? " active" : ""}`}
                        disabled={asking || intentChecking || sessionLoading || Boolean(askIntentReview)}
                        onClick={() => askSession.selectMode(defaultModeForGroup(g.id, askModes))}
                      >
                        {g.label}
                      </button>
                    ))}
                    {/* 引擎子切换与检索档位只属于高级模式。 */}
                    {isAdvanced(uiMode) && (
                      groupOf(askMode, askModes) === "strict"
                      || groupOf(askMode, askModes) === "extension"
                    ) && (
                      <span className="mode-engines">
                        {modesInGroup(groupOf(askMode, askModes), askModes).map((m) => (
                          <button
                            key={m.id}
                            type="button"
                            className={`mode-engine${askMode === m.id ? " active" : ""}`}
                            title={m.desc}
                            disabled={
                              asking || intentChecking || sessionLoading
                              || Boolean(askIntentReview)
                              || (m.requiresKg && !kgAvailable)
                            }
                            onClick={() => askSession.selectMode(m.id)}
                          >
                            {m.label}
                          </button>
                        ))}
                      </span>
                    )}
                    {isAdvanced(uiMode) && askMode === "reasoning" && (
                      <span className="ask-retrieval-effort">
                        {/* 与深度报告的「研究深度」共用 EffortPicker：同一套档名理应是同一个控件。
                            popover 只给该档一句说明，不再铺开每档的阈值数字。 */}
                        <EffortPicker
                          chipLabel="档位"
                          title="检索档位"
                          options={ASK_RETRIEVAL_EFFORT_OPTIONS}
                          value={askRetrievalEffort}
                          onChange={(id) => askSession.selectRetrievalEffort(id as AskRetrievalEffortId)}
                          disabled={asking || intentChecking || sessionLoading || Boolean(askIntentReview)}
                          compact
                        />
                      </span>
                    )}
                    {groupOf(askMode, askModes) === "strict" && !kgAvailable && (
                      kgBlockedByScope ? (
                        // 出路是把勾选点回来,不是花钱整理一次整库图谱 —— 这一支
                        // 刻意不给「整理知识图谱」按钮。
                        <span className="mode-hint">
                          {`已整理知识图谱的参考库这次都没勾选，${strictLabel}取不到图谱；在来源面板重新勾选即可`}
                        </span>
                      ) : (
                        <span className="mode-hint">
                          {`该笔记本尚无知识图谱，${strictLabel}需先整理`}
                          <button
                            type="button"
                            className="mode-engine"
                            style={{ marginLeft: 6 }}
                            disabled={kgGraph.buildingKg || asking || sessionLoading}
                            onClick={() => { if (currentNotebookId) startKgBuild(currentNotebookId); }}
                          >
                            {kgGraph.buildingKg ? "整理中…" : "整理知识图谱"}
                          </button>
                        </span>
                      )
                    )}
                    {groupOf(askMode, askModes) === "extension"
                      && requiresKg(askMode, askModes)
                      && !kgAvailable && (
                        <span className="chat-hint">
                          当前扩展引擎需要知识图谱，请先整理本笔记本或重新勾选带图谱的参考库
                        </span>
                    )}
                    {shouldShowBorrowedBaseHint({
                      strict: groupOf(askMode, askModes) === "strict",
                      kgAvailable,
                      // 两个参考库维度的入参都按**本轮勾选集**给:借用的是这次真的
                      // 参与、且已建图谱的那几个库,不是「挂了几个」。
                      baseKgAvailable: borrowedBaseNames.length > 0,
                      kgReady: Boolean(currentNotebook?.kg_ready),
                      baseCount: selectedBaseNotebookCount,
                    }) && (
                      <span className="chat-hint">本笔记本尚无知识图谱，将借用参考库「{borrowedBaseNames.join("、")}」推理</span>
                    )}
                  </div>
                  )}
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

      {notebookCollection.menu.notebook && notebookCollection.menu.position && (
        <div
          ref={notebookCollection.menu.ref}
          className="popover notebook-menu"
          style={{ left: notebookCollection.menu.position.left, top: notebookCollection.menu.position.top }}
        >
          <NotebookMenuActions
            notebook={notebookCollection.menu.notebook}
            onLeave={leaveMenuNotebook(notebookCollection.menu.notebook.id)}
            onEdit={editMenuNotebook(notebookCollection.menu.notebook.id)}
            onDelete={deleteMenuNotebook(notebookCollection.menu.notebook.id)}
          />
        </div>
      )}

      {rootModals.view("notebook-share").open && shareModal && currentNotebook && (
        <section className="utility-modal" role="dialog" aria-modal={rootModals.view("notebook-share").topmost} aria-hidden={!rootModals.view("notebook-share").topmost} inert={rootModals.view("notebook-share").topmost ? undefined : true} style={{ zIndex: rootModals.view("notebook-share").zIndex }} onClick={(event) => { if (event.currentTarget === event.target) rootModals.requestClose("notebook-share", "backdrop"); }}>
          <FloatingModalCard storageKey="notebook.share.window" className="utility-modal-card">
            {(floating) => (<>
            <div className="source-modal-header" {...floating.dragHandleProps}>
              <div>
                <h2>分享「{currentNotebook.name}」</h2>
                <p>{capabilities.canConfigureNotebook
                  ? "两种方式：发一条链接给具体的人，或者共享给一个群组（随成员进出自动生效与失效）。"
                  : "共享给一个群组（随成员进出自动生效与失效）。链接分享由库主管理。"}</p>
              </div>
              <button className="icon-button" onClick={() => rootModals.requestClose("notebook-share", "button")} title="Close">×</button>
            </div>
            <div className="source-detail-body">
              {/* 链接分享区只对 owner 渲染(notebook:configure,恒 owner,P2-T2 评审 P0):
                  链接是库主对外处置,组管理员即便打开这个弹窗(为了下面的「共享给群组」)
                  也看不到/动不了链接。组管理员的 openShareModal 甚至不发 GET /share。 */}
              {capabilities.canConfigureNotebook && (<>
              <span className="section-title">链接分享</span>
              {shareModal.share_token ? (<>
                <label>分享链接
                  {/* 链接框与「已分享」总览用同一个 `.share-link-field`:同一件东西
                      (一条分享链接 + 复制)在两个弹窗里必须长同一个样,而原生方角
                      input 紧挨 999px 圆角按钮是这套界面里最扎眼的一处形状冲突。 */}
                  <div className="share-link-field" style={{ marginTop: 6 }}>
                    <input
                      ref={(node) => { shareLinkInputs.current.set(shareModal.share_token, node); }}
                      readOnly
                      value={buildShareLink(shareModal.share_token, window.location.origin)}
                      onFocus={(event) => event.currentTarget.select()}
                    />
                    <button
                      className={shareLinkCopy.resultFor(shareModal.share_token) === "copied" ? "sort-button copy-result-copied" : shareLinkCopy.resultFor(shareModal.share_token) === "failed" ? "sort-button copy-result-failed" : "sort-button"}
                      onClick={() => copyShareLink().catch(reportError)}
                    >
                      {shareLinkCopy.resultFor(shareModal.share_token) === "copied" ? "已复制" : shareLinkCopy.resultFor(shareModal.share_token) === "failed" ? "复制失败" : "复制"}
                    </button>
                  </div>
                </label>
                <p className="tool-hint" style={{ margin: "2px 0 0" }}>
                  {shareModal.copyable ? "他人可拷贝" : "笔记本较大，他人可只读加入"}
                  {` · ${shareModal.size.sources ?? 0} 来源 · ${shareModal.size.nodes ?? 0} 节点 · ${shareModal.size.edges ?? 0} 边 · ${formatFileSize(shareModal.size.bytes ?? 0)}`}
                </p>
                <div className="tag-row">
                  <button className="sort-button" disabled={shareBusy} onClick={() => handleUnshare().catch(reportError)}>
                    {shareBusy ? "取消中…" : "取消链接分享"}
                  </button>
                </div>
              </>) : (<>
                <p className="tool-hint" style={{ margin: "2px 0 0" }}>
                  还没有分享链接。开启后，拿到链接的登录用户可以拷贝这个笔记本，或（笔记本较大时）以只读方式加入。
                </p>
                {/* 借入参考库的「未共享门」:一旦这本笔记本被共享出去,它借来的参考库
                    就停止参与检索(设计文档 §6.1)。提示挨着**将要触发它的那个按钮**,
                    而不是事后在失效边上解释——那时用户已经不知道是哪一步造成的。 */}
                <p className="tool-hint" style={{ margin: "2px 0 0" }}>{BORROWED_BASE_SHARE_WARNING}</p>
                <div className="tag-row">
                  <button className="new-pill" disabled={shareBusy} onClick={() => enableShareLink().catch(reportError)}>
                    {shareBusy ? "开启中…" : "开启链接分享"}
                  </button>
                </div>
              </>)}
              </>)}
              <NotebookGroupShare
                notebookId={currentNotebook.id}
                onChanged={() => { handleSharingChanged().catch(reportError); }}
              />
              <div className="tag-row">
                <button className="new-pill" onClick={() => rootModals.requestClose("notebook-share", "button")}>完成</button>
              </div>
            </div>
            </>)}
          </FloatingModalCard>
        </section>
      )}

      {rootModals.view("shared-preview").open && sharedPreview && (
        <section className="utility-modal" role="dialog" aria-modal={rootModals.view("shared-preview").topmost} aria-hidden={!rootModals.view("shared-preview").topmost} inert={rootModals.view("shared-preview").topmost ? undefined : true} style={{ zIndex: rootModals.view("shared-preview").zIndex }} onClick={(event) => { if (event.currentTarget === event.target) rootModals.requestClose("shared-preview", "backdrop"); }}>
          <FloatingModalCard storageKey="notebook.sharedPreview.window" className="utility-modal-card">
            {(floating) => (<>
            <div className="source-modal-header" {...floating.dragHandleProps}>
              <div>
                <h2>{sharedPreview.name}</h2>
                <p>由 {sharedPreview.owner_display} 分享 · {sharedPreview.source_count} 来源 · {sharedPreview.node_count} 节点 · {sharedPreview.edge_count} 边</p>
              </div>
              <button className="icon-button" onClick={() => rootModals.requestClose("shared-preview", "button")} title="Close">×</button>
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
                <button className="sort-button" onClick={() => rootModals.requestClose("shared-preview", "button")}>取消</button>
              </div>
            </div>
            </>)}
          </FloatingModalCard>
        </section>
      )}

      {rootModals.view("shared-by-me").open && (
        <section className="utility-modal" role="dialog" aria-modal={rootModals.view("shared-by-me").topmost} aria-hidden={!rootModals.view("shared-by-me").topmost} inert={rootModals.view("shared-by-me").topmost ? undefined : true} style={{ zIndex: rootModals.view("shared-by-me").zIndex }} onClick={(event) => { if (event.currentTarget === event.target) rootModals.requestClose("shared-by-me", "backdrop"); }}>
          <FloatingModalCard storageKey="notebook.sharedByMe.window" className="utility-modal-card share-overview-card">
            {(floating) => (<>
            <div className="source-modal-header" {...floating.dragHandleProps}>
              <div>
                <h2>已分享</h2>
                <p>你分享出去的笔记本，以及每一本此刻正通过哪种方式对外可见。链接分享可以在这里复制与撤销；共享给群组的部分在别处管理。</p>
              </div>
              <button className="icon-button" onClick={() => rootModals.requestClose("shared-by-me", "button")} title="Close">×</button>
            </div>
            <div className="source-detail-body">
              {sharedByMeList === null ? (
                <p className="share-overview-loading"><Loader2 size={15} className="busy-spin" /> 正在读取分享状态…</p>
              ) : sharedByMeList.length === 0 ? (
                <article className="source-empty">
                  <div><Share2 size={38} strokeWidth={1.4} /></div>
                  <strong>还没有分享出去的笔记本</strong>
                  <p>在笔记本里点「分享」，可以发一条链接给具体的人，或共享给一个群组。</p>
                </article>
              ) : (
                <div className="share-overview">
                  {sharedByMeList.map((item) => (
                    <article className="share-overview-item" key={item.id}>
                      <header className="share-overview-head">
                        <h3 className="share-overview-name">{item.name}</h3>
                        {/* 顶部这排 chip 是**事实**不是动作:一眼看清这本库现在开着哪几条
                            对外通道。只因群组共享而出现的行没有分享链接(share_token 为空),
                            它的分享模式(可拷贝/只读)对读者没有意义,不显示。 */}
                        {item.share_token && (
                          <span className="share-channel-chip is-link" title={item.mode === "readonly" ? "笔记本较大,只读共享" : "笔记本较小,可被拷贝"}>
                            <Link2 size={12} /> 链接 · {shareModeLabel(item.mode)}
                          </span>
                        )}
                        {item.group_count > 0 && (
                          <span className="share-channel-chip is-group">
                            <Users size={12} /> 共享给 {item.group_count} 个群组
                          </span>
                        )}
                      </header>
                      {item.share_token && (
                        <div className="share-overview-channel is-link">
                          <div className="share-link-field">
                            <input
                              ref={(node) => { shareLinkInputs.current.set(item.share_token, node); }}
                              readOnly
                              value={buildShareLink(item.share_token, window.location.origin)}
                              onFocus={(event) => event.currentTarget.select()}
                            />
                            <button
                              className={shareLinkCopy.resultFor(item.share_token) === "copied" ? "sort-button copy-result-copied" : shareLinkCopy.resultFor(item.share_token) === "failed" ? "sort-button copy-result-failed" : "sort-button"}
                              onClick={() => {
                                const link = buildShareLink(item.share_token, window.location.origin);
                                handleShareLinkCopy(item.share_token, link).catch(reportError);
                              }}
                            >{shareLinkCopy.resultFor(item.share_token) === "copied" ? "已复制" : shareLinkCopy.resultFor(item.share_token) === "failed" ? "复制失败" : "复制"}</button>
                          </div>
                          {/* 复制按钮**旁边**必须写清这条链接此刻交出去的是什么:两种模式给出的
                              东西根本不同(一份快照 vs 一直看得到的实时内容),而这正是决定
                              「能不能发给这个人」的那句话。 */}
                          <p className="share-overview-note">
                            {item.mode === "readonly"
                              ? "拿到链接的人可以只读加入：他们看得到这本笔记本此刻及以后的内容，但不能修改。"
                              : "拿到链接的人可以把此刻的内容拷贝成自己的独立副本；之后你这边的改动不会同步过去。"}
                          </p>
                          {/* 规模统计只在有链接时算(纯群组共享的行后端刻意不跑那次统计,
                              省掉 N 次新鲜复核),所以也只在有链接时显示。 */}
                          <p className="share-overview-stats">
                            <span>{item.size.sources ?? 0} 来源</span>
                            <span>{item.size.nodes ?? 0} 节点</span>
                            <span>{item.size.edges ?? 0} 边</span>
                            <span>{formatFileSize(item.size.bytes ?? 0)}</span>
                          </p>
                          {item.mode === "readonly" && (item.members.length > 0 ? (
                            <div className="share-overview-members">
                              <span className="share-overview-members-label">已加入 {item.members.length} 人</span>
                              {item.members.map((member) => (
                                <span className="share-member-chip" key={member.username}>{member.username}</span>
                              ))}
                            </div>
                          ) : (
                            <p className="share-overview-note">还没有人通过这条链接加入。</p>
                          ))}
                          {/* 「取消分享」只撤销链接与只读成员,对群组授权是空操作——所以
                              只因群组共享而出现的行不给这个按钮(点了会成功却什么都没变)。
                              撤销不可逆(重开会铸出新 token、旧链接永久失效,已加入的人被踢),
                              因此与「删除来源」等破坏性动作一样先过确认弹窗。 */}
                          <div className="share-overview-actions">
                            <button
                              className="sort-button danger-text"
                              disabled={shareBusy}
                              onClick={() => confirmUnshareFromOverview(item)}
                            >{unsharingNotebookId === item.id ? "取消中…" : "取消链接分享"}</button>
                          </div>
                        </div>
                      )}
                      {item.group_count > 0 && (
                        <div className="share-overview-channel is-group">
                          <p className="share-overview-note">
                            组内成员随进出自动获得与失去只读权限。要看是哪些群组或撤销，去这本笔记本的「分享」，或由组管理员在「群组」里撤销。
                          </p>
                        </div>
                      )}
                    </article>
                  ))}
                </div>
              )}
            </div>
            <div className="share-overview-footer">
              <button className="new-pill" onClick={() => rootModals.requestClose("shared-by-me", "button")}>完成</button>
            </div>
            </>)}
          </FloatingModalCard>
        </section>
      )}

      {rootModals.view("source-add").open && (
        <section className="source-modal" role="dialog" aria-modal={rootModals.view("source-add").topmost} aria-hidden={!rootModals.view("source-add").topmost} inert={rootModals.view("source-add").topmost ? undefined : true} style={{ zIndex: rootModals.view("source-add").zIndex }} onClick={(event) => { if (event.currentTarget === event.target) closeSourceModal("backdrop"); }}>
          <FloatingModalCard storageKey="source.add.window" className="source-modal-card">
            {(floating) => (<>
            <div className="source-modal-header" {...floating.dragHandleProps}>
              <div>
                <h2>添加来源</h2>
                <p>上传文件或添加链接；文件可为每个指定文档类型（默认自动检测），类型决定要分析出哪些字段。</p>
              </div>
              <button className="icon-button" onClick={() => closeSourceModal("button")} title="Close">×</button>
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
            <label
              className={`drop-zone${sourceFilePickerDisabled ? " is-disabled" : ""}${dropZoneDragActive ? " is-dragover" : ""}`}
              title={sourceFilePickerHint}
              onDragOver={handleStageDragOver}
              onDragEnter={() => { if (!sourceFilePickerDisabled) setDropZoneDragActive(true); }}
              onDragLeave={() => setDropZoneDragActive(false)}
              onDrop={handleStageDrop}
            >
              <input type="file" multiple accept={supportedSourceAccept} onChange={stageFiles} disabled={sourceFilePickerDisabled} />
              <span className="drop-plus">＋</span>
              <strong>{stagedFiles.length > 0 ? "继续添加文件" : "或拖放文件"}</strong>
              <small>
                {bundleBusyLabel
                  ? bundleBusyLabel
                  : sourceUploadConfigLoading
                    ? "正在读取上传限制…"
                    : `支持 ${supportedSourceUserHint}。单个文件最大 ${sourceUploadSizeLabel(sourceUploadMaxBytes)}，单次最多 ${sourceUploadMaxFilesPerBatch} 个。Markdown 图片压缩包会上传到后台解析并保存图片。`}
              </small>
            </label>
            {stagedSkipped.length > 0 && (
              <div className="staged-skipped" role="alert">
                <div className="staged-skipped-head">
                  <span>已跳过 {stagedSkipped.length} 个文件（不会上传）</span>
                  <button type="button" className="sort-button" onClick={() => setStagedSkipped([])}>知道了</button>
                </div>
                <div className="staged-skipped-rows">
                  {stagedSkipped.map((item, index) => (
                    <div className="staged-skipped-row" key={`${item.name}-${index}`}>
                      <span className="staged-skipped-name" title={item.name}>{compactStagedFileName(item.name)}</span>
                      <small className="staged-skipped-reason">{item.reason}</small>
                    </div>
                  ))}
                </div>
              </div>
            )}
            {stagedWarnings.length > 0 && (
              <div className="staged-skipped staged-warnings" role="status">
                <div className="staged-skipped-head">
                  <span>图片提示 {stagedWarnings.length} 条（文件仍可上传）</span>
                  <button type="button" className="sort-button" onClick={() => setStagedWarnings([])}>知道了</button>
                </div>
                <div className="staged-skipped-rows">
                  {stagedWarnings.map((item, index) => (
                    <div className="staged-skipped-row" key={`${item.name}-${index}`}>
                      <span className="staged-skipped-name" title={item.name}>{compactStagedFileName(item.name)}</span>
                      <small className="staged-skipped-reason">{item.reason}</small>
                    </div>
                  ))}
                </div>
              </div>
            )}
            {bundleChoice && (
              <BundleChoicePanel
                choice={bundleChoice}
                onToggle={toggleBundleCandidate}
                onConfirm={confirmBundleChoice}
                onCancel={cancelBundleChoice}
              />
            )}
            <BundleReceiptsPanel
              receipts={bundleReceipts}
              onDismiss={() => setBundleReceipts([])}
              imagesDisabledNote={sourceImagePairingEnabled ? null : BUNDLE_IMAGES_DISABLED_NOTE}
            />
            <div className="source-action-row">
              <label
                className={`source-action-button${sourceFilePickerDisabled ? " is-disabled" : ""}`}
                title={sourceFilePickerHint}
                onDragOver={handleStageDragOver}
                onDrop={handleStageDrop}
              >
                <Upload size={18} strokeWidth={2.5} /> 上传文件
                <input type="file" multiple accept={supportedSourceAccept} onChange={stageFiles} disabled={sourceFilePickerDisabled} />
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
                <p className="tool-hint" style={{ margin: "0 0 6px" }}>每行一个可直链的 PDF；非 PDF 会被直接拒绝。</p>
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
                  <button className="sort-button" disabled={uploadBusy} onClick={resetStagedIntake}>清空</button>
                </div>
              </div>
            )}
            </>)}
          </FloatingModalCard>
        </section>
      )}

      {rootModals.view("memory-save").open && memoryAnswerId && currentNotebookId && (
        <MemorySaveDialog
          answerId={memoryAnswerId}
          notebookId={currentNotebookId}
          sessionSignal={memorySessionAbortRef.current.signal}
          onClose={() => rootModals.requestClose("memory-save", "button")}
          onSaved={(memory) => handleMemorySaved(memory).catch(reportError)}
          interactive={rootModals.view("memory-save").topmost}
          zIndex={rootModals.view("memory-save").zIndex}
        />
      )}

      {rootModals.view("notebook-editor").open && notebookCollection.editor?.target && (
        <section className="utility-modal" role="dialog" aria-modal={rootModals.view("notebook-editor").topmost} aria-hidden={!rootModals.view("notebook-editor").topmost} inert={rootModals.view("notebook-editor").topmost ? undefined : true} style={{ zIndex: rootModals.view("notebook-editor").zIndex }}>
          <FloatingModalCard storageKey="notebook.edit.window" className="utility-modal-card notebook-edit-card">
            {(floating) => (<>
            <div className="source-modal-header" {...floating.dragHandleProps}>
              <div>
                <h2>笔记本设置</h2>
                <p>{notebookCollection.editor?.canConfigureNotebook
                  ? "编辑当前笔记本的信息、索引管线与参考库。模型服务由系统统一管理。"
                  : "编辑当前笔记本的信息与索引管线。参考库挂载仍由库主配置。"}
                </p>
              </div>
              {/* 关闭入口在保存期间**不禁用**:这个 slot 不接 Escape、也不接遮罩点击
                  (ROOT_MODAL_POLICIES 里 escape/backdrop 都是 false),遮罩又铺满整屏,
                  所以一旦把它连同「取消」「保存」一起 disable,一个永不 settle 的请求
                  就把用户锁死在弹窗里,只能刷新页面。按下 = 显式取消(closeEditor 的既有
                  语义:后续写入步骤停在下一个检查点),并由它自己发一条说明已提交部分
                  不会撤销的提示。 */}
              <button className="icon-button" onClick={() => rootModals.requestClose("notebook-editor", "button")} title="Close">×</button>
            </div>
            <form className="edit-form notebook-settings-form" onSubmit={submitNotebookEditor}>
              <section className="settings-section">
                <div className="settings-section-head"><h3>基本信息</h3></div>
                <label>标题<input name="name" defaultValue={notebookCollection.editor?.target?.name} maxLength={80} required /></label>
                <label>描述<textarea name="purpose" defaultValue={notebookCollection.editor?.target?.purpose} rows={3} maxLength={260} /></label>
                <label>领域关键词<input name="primary_domain" defaultValue={notebookCollection.editor?.target?.primary_domain} maxLength={80} /></label>
              </section>
              <section className="settings-section">
                <div className="settings-section-head">
                  <h3>索引管线</h3>
                  <p>按笔记本选择用于分块与索引构建的管线；parser 仍由系统自动路由，不在这里配置。</p>
                </div>
                {notebookEditorIndexingNotice && (
                  <div className={`extension-alert extension-alert--${notebookEditorIndexingNotice.tone}`}>
                    <p>{notebookEditorIndexingNotice.detail}</p>
                    {notebookEditorIndexingNotice.canRetry && (
                      <button
                        type="button"
                        className="sort-button compact"
                        disabled={notebookCollection.editor?.busy}
                        onClick={() => {
                          const projection = notebookCollection.editor?.indexingPipeline ?? null;
                          const option = selectedIndexingPipelineOption(projection);
                          if (!window.confirm(indexingPipelineConfirmMessage(option))) return;
                          void notebookCollection.retryIndexingPipelineRebuild();
                        }}
                      >
                        {notebookCollection.editor?.busy ? "提交重建中…" : "重试重建"}
                      </button>
                    )}
                    {notebookEditorIndexingNotice.canRevert && (
                      <button
                        type="button"
                        className="sort-button compact"
                        disabled={notebookCollection.editor?.busy}
                        onClick={() => {
                          const projection = notebookCollection.editor?.indexingPipeline ?? null;
                          const option = selectedIndexingPipelineOption(projection, null);
                          if (!window.confirm(indexingPipelineConfirmMessage(option))) return;
                          void notebookCollection.revertIndexingPipelineToBuiltin();
                        }}
                      >
                        {notebookCollection.editor?.busy ? "切回中…" : "切回内建"}
                      </button>
                    )}
                  </div>
                )}
                <div className="indexing-pipeline-list" role="radiogroup" aria-label="索引管线">
                  {(notebookCollection.editor?.indexingPipeline?.options ?? []).map((option) => {
                    const optionId = option.pipeline_id ?? "";
                    const checked = optionId === (notebookCollection.editor?.selectedPipelineId ?? "");
                    return (
                      <label
                        className={`indexing-pipeline-option${checked ? " is-selected" : ""}${option.available === false ? " is-unavailable" : ""}`}
                        key={`${option.pipeline_id ?? "builtin"}:${option.version}`}
                      >
                        <input
                          type="radio"
                          name="indexing-pipeline"
                          checked={checked}
                          // 重建进行中整组禁用:后端 begin() 对活跃 rebuild 一律 409
                          // (改选会作废正在跑的整轮重建),界面不给一条必然失败的路。
                          disabled={
                            notebookCollection.editor?.busy
                            || option.available === false
                            || notebookCollection.editor?.indexingPipeline?.rebuild_status === "pending"
                          }
                          onChange={() => notebookCollection.selectIndexingPipeline(option.pipeline_id ?? null)}
                        />
                        <span className="indexing-pipeline-copy">
                          <span className="indexing-pipeline-head">
                            <strong>{option.label}</strong>
                            <small>v{option.version}</small>
                          </span>
                          <span>{option.description}</span>
                          <span className="indexing-pipeline-flags">
                            {option.overrides_chunking ? "自定义分块" : "内建分块"}
                            {option.overrides_kg_extraction ? " · 自定义知识分析" : ""}
                            {option.available === false ? " · 当前不可用" : ""}
                          </span>
                        </span>
                      </label>
                    );
                  })}
                </div>
                <p className="base-picker-hint">
                  {indexingPipelineIdsEqual(
                    notebookCollection.editor?.indexingPipeline?.pipeline_id,
                    notebookCollection.editor?.selectedPipelineId,
                  )
                    ? "当前选择保持不变时，不会触发额外重建。"
                    : `保存后将切换到“${notebookEditorSelectedPipeline?.label ?? "所选索引管线"}”，并重建全库索引。`}
                </p>
              </section>
              {notebookCollection.editor?.canConfigureNotebook && (
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
                  const groups = groupMountable(mergeMountCandidates(
                    notebookCollection.editor?.mountable ?? [],
                    notebookCollection.editor?.mountEdges ?? [],
                  ));
                  const render = (title: string, list: MountedBase[], variant: "public" | "mine" | "shared") =>
                    list.length === 0 ? null : (
                      <div className={`base-picker-group base-picker-group--${variant}`} key={title}>
                        <span className="base-picker-group-title">{title}</span>
                        {list.map((n) => {
                          const dead = !n.active;
                          return (
                            <label className={`base-picker-row${dead ? " base-picker-row-dead" : ""}`} key={n.id}>
                              <input
                                type="checkbox"
                                checked={(notebookCollection.editor?.mountedIds ?? []).includes(n.id)}
                                disabled={notebookCollection.editor?.busy}
                                onChange={(e) => notebookCollection.toggleMountedBase(n.id, e.target.checked)}
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
                      {/* 第三组是群组共享放开「读权 ⇒ 可挂载」之后新出现的:别人 owner
                          的库。此前它们会被归进「我的笔记本」,一句事实错误的标签。 */}
                      {render("共享给我的", groups.shared, "shared")}
                      {groups.public.length === 0 && groups.mine.length === 0
                        && groups.shared.length === 0 && (
                        <p className="base-picker-empty">暂无可挂载的知识库。</p>
                      )}
                    </>
                  );
                })()}
                {mountCostHint((notebookCollection.editor?.mountedIds ?? []).length) && (
                  <p className="base-picker-hint">{mountCostHint((notebookCollection.editor?.mountedIds ?? []).length)}</p>
                )}
                </div>
                </section>
              )}
              <section className="settings-section">
                <div className="settings-section-head"><h3>更多信息</h3></div>
                <div className="settings-grid-2">
                  <label>目标用户<input name="target_users" defaultValue={notebookCollection.editor?.target?.target_users ?? ""} maxLength={120} /></label>
                  <label>访问范围<input name="access_scope" defaultValue={notebookCollection.editor?.target?.access_scope ?? ""} maxLength={80} /></label>
                </div>
                <label>预期问题（每行/逗号一条）<textarea name="expected_questions" defaultValue={(notebookCollection.editor?.target?.expected_questions ?? []).join("\n")} rows={2} /></label>
                <label>来源类型（每行/逗号一条）<input name="source_types" defaultValue={(notebookCollection.editor?.target?.source_types ?? []).join(", ")} /></label>
                <label>分类（每行/逗号一条）<input name="taxonomy" defaultValue={(notebookCollection.editor?.target?.taxonomy ?? []).join(", ")} /></label>
              </section>
              <div className="modal-actions settings-footer">
                <button type="button" className="sort-button" onClick={() => rootModals.requestClose("notebook-editor", "button")}>{notebookCollection.editor?.busy ? "停止等待" : "取消"}</button>
                <button type="submit" className="new-pill" disabled={notebookCollection.editor?.busy}>{notebookCollection.editor?.busy ? "保存中…" : "保存"}</button>
              </div>
            </form>
            </>)}
          </FloatingModalCard>
        </section>
      )}

      {rootModals.view("notebook-delete").open && notebookCollection.deletion?.target && (
        <section className="utility-modal" role="dialog" aria-modal={rootModals.view("notebook-delete").topmost} aria-hidden={!rootModals.view("notebook-delete").topmost} inert={rootModals.view("notebook-delete").topmost ? undefined : true} style={{ zIndex: rootModals.view("notebook-delete").zIndex }}>
          <FloatingModalCard storageKey="notebook.delete.window" className="utility-modal-card narrow">
            {(floating) => (<>
            <div className="source-modal-header" {...floating.dragHandleProps}>
              <div>
                <h2>删除笔记本</h2>
                <p>确定删除 “{notebookCollection.deletion?.target?.name}” 吗？这个本机 beta 会同时移除它的来源和深度报告；{NOTEBOOK_PRIVATE_MEMORY_DELETE_WARNING}</p>
                {(notebookCollection.deletion?.mountedByCount ?? 0) > 0 && (
                  <p className="delete-mount-warning">
                    {notebookCollection.deletion?.mountedByCount ?? 0} 个笔记本正在把它作为参考库，删除后这些笔记本会立即失去这条参考库——此操作不可撤销。
                  </p>
                )}
              </div>
              {/* 同 notebook-editor:这个 slot 也不接 Escape/遮罩点击,关闭入口不能被
                  busy 禁用,否则一个挂死的 DELETE 就没有出口。已在飞的 DELETE 不因关框
                  而撤销——它的成功/失败两路都绑在 actor 上,关框后照样落地并出提示。 */}
              <button className="icon-button" onClick={() => rootModals.requestClose("notebook-delete", "button")} title="Close">×</button>
            </div>
            <div className="modal-actions padded">
              <button className="sort-button" onClick={() => rootModals.requestClose("notebook-delete", "button")}>{notebookCollection.deletion?.busy ? "关闭" : "取消"}</button>
              <button className="new-pill danger-pill" disabled={notebookCollection.deletion?.busy} onClick={() => { void notebookCollection.confirmDelete(); }}>{notebookCollection.deletion?.busy ? "删除中…" : "确认"}</button>
            </div>
            </>)}
          </FloatingModalCard>
        </section>
      )}

      {rootModals.view("password-change").open && (
        <PasswordChangeModal
          onClose={() => rootModals.requestClose("password-change", "button")}
          interactive={rootModals.view("password-change").topmost}
          zIndex={rootModals.view("password-change").zIndex}
        />
      )}

      {rootModals.view("search-profile").open && currentUser && (
        <SearchProfileModal
          currentUser={currentUser}
          onSaved={setCurrentUser}
          onClose={() => rootModals.requestClose("search-profile", "button")}
          interactive={rootModals.view("search-profile").topmost}
          zIndex={rootModals.view("search-profile").zIndex}
        />
      )}

      {rootModals.view("info").open && infoModal && (
        <section className="utility-modal utility-modal-top" role="dialog" aria-modal={rootModals.view("info").topmost} aria-hidden={!rootModals.view("info").topmost} inert={rootModals.view("info").topmost ? undefined : true} style={{ zIndex: rootModals.view("info").zIndex }}>
          <FloatingModalCard storageKey="info.window" className="utility-modal-card narrow">
            {(floating) => (<>
            <div className="source-modal-header" {...floating.dragHandleProps}>
              <div>
                <h2>{infoModal.title}</h2>
                <p>{infoModal.message}</p>
              </div>
              <button className="icon-button" onClick={closeInfoModal} title="Close">×</button>
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
                        onClick={() => { if (rootModals.requestClose("info", "button")) action.action(); }}
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
                    onClick={() => { if (rootModals.requestClose("info", "button")) action.action(); }}
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
      {rootModals.view("answer-image-preview").open && answerImagePreview && answerImagePreviewImage && currentNotebookId && (
        <ImagePreviewModal
          referenceLabel={answerImagePreviewImage.referenceLabel}
          interactive={rootModals.view("answer-image-preview").topmost}
          zIndex={rootModals.view("answer-image-preview").zIndex}
          imageIndex={answerImagePreview.index}
          imageCount={answerImagePreview.items.length}
          // 左右切换只在打开时冻结的那份快照里走,不重取回答:换的只是 index。
          onSelectImage={(index) => setAnswerImagePreview((prev) => (prev ? { ...prev, index } : prev))}
          onClose={(reason) => rootModals.requestClose("answer-image-preview", reason)}
        >
          <AuthedImage
            url={sourceImageAssetUrl(API_BASE, currentNotebookId, answerImagePreviewImage.assetId)}
            alt={answerImagePreviewImage.alt}
          />
        </ImagePreviewModal>
      )}

      {rootModals.view("source-detail").open && sourceDetail && (
        <SourceDetailWindow
          onClose={() => rootModals.requestClose("source-detail", "button")}
          interactive={rootModals.view("source-detail").topmost}
          zIndex={rootModals.view("source-detail").zIndex}
        >
          <div className="source-detail-title-row">
                <h1 title={sourceDetail.title}>{sourceDetail.title}</h1>
                {sourceDetailBaseId ? (
                  <span
                    className="tier-badge tier-base source-detail-base-badge"
                    title={sourceDetailBaseLabel}
                  >
                    {sourceDetailBaseLabel}
                  </span>
                ) : !readOnlyWorkspace ? (
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
                ) : null}
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
                <span className="tag">{sourceElementsTotal || sourceDetail.element_count} 个元素</span>
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
              {sourceDetail.parse_quality_warning && (
                <section className="source-pdf-fallback-warning" aria-label="降级解析提示">
                  <div>
                    <strong>当前内容由本地解析器生成</strong>
                    <p>MinerU 重试后仍未成功，版面、公式、表格或扫描内容可能存在异常。请先检查内容；MinerU 恢复后可重新解析，不满意也可以删除该来源。</p>
                  </div>
                  {!sourceDetailBaseId && !readOnlyWorkspace && (
                    <div className="source-pdf-fallback-actions">
                      <button
                        type="button"
                        className="button secondary"
                        aria-label="重新解析降级来源"
                        disabled={reparsingSource || sourceDetailDeleting}
                        onClick={() => reparseSource().catch(reportError)}
                      >
                        {reparsingSource ? "重新解析中…" : "重新解析"}
                      </button>
                      <button
                        type="button"
                        className="button secondary source-pdf-fallback-delete"
                        aria-label="删除降级来源"
                        disabled={reparsingSource || sourceDetailDeleting}
                        onClick={() => confirmDeleteSource(sourceDetail)}
                      >
                        {sourceDetailDeleting ? "删除中…" : "删除来源"}
                      </button>
                    </div>
                  )}
                </section>
              )}
              {/* 命令目录（方案 C）：只对本笔记本自己的来源渲染。参考库来源是只读的，
                  发起/取消/确认在后端都是 owner-only 且按 notebook 收窄，对一个只是被
                  挂载进来的库发起会花这个库的钱、写那个库的知识——授权语义是错的，
                  入口连出现都不该出现（与上方重新解析/删除同一条判据）。 */}
              {!sourceDetailBaseId && currentNotebookId && (
                <CommandCatalogSection
                  notebookId={currentNotebookId}
                  sourceId={sourceDetail.id}
                  sourceTitle={sourceDetail.title}
                  canEdit={!readOnlyWorkspace}
                  onConfirm={confirmCommandCatalog}
                  onOpenReview={openCatalogReview}
                  reviewSeq={catalogReviewSeq}
                />
              )}
              {currentUser && currentNotebook && sourceDetail && workspaceExtensions.ownerKey && (
                <WorkspaceExtensionOutlet
                  slot="source.detail_section"
                  registry={WORKSPACE_UI_CONTRIBUTIONS}
                  projection={workspaceExtensionProjection}
                  ownerKey={workspaceExtensions.ownerKey}
                  actions={workspaceExtensionActions}
                  dialog={rootModals.view("extension")}
                  dialogHolder={extensionDialogHolder}
                  context={{
                    slot: "source.detail_section",
                    actor: {
                      id: currentUser.id,
                      username: currentUser.username,
                      displayName: currentUser.display_name,
                    },
                    notebook: { id: currentNotebook.id, name: currentNotebook.name },
                    source: {
                      id: sourceDetail.id,
                      notebookId: sourceDetail.notebook_id,
                      title: sourceDetail.title,
                    },
                    uiMode,
                    permissions: {
                      ...workspaceExtensionPermissions,
                      sourceRead: true,
                      sourceWrite: !sourceDetailBaseId && capabilities.canWriteNotebook,
                    },
                  }}
                />
              )}
              <div className="source-element-stack">
                {sourceElementStartOffset > 0 && (
                  <button
                    type="button"
                    className="button secondary source-elements-page-button"
                    disabled={sourceElementsLoading}
                    onClick={() => loadSourceElementPage("previous").catch(reportError)}
                  >{sourceElementsLoading ? "加载中…" : `加载前面的元素（已显示 ${sourceElements.length}/${sourceElementsTotal}）`}</button>
                )}
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
                {sourceElementStartOffset + sourceElements.length < sourceElementsTotal && (
                  <button
                    type="button"
                    className="button secondary source-elements-page-button"
                    disabled={sourceElementsLoading}
                    onClick={() => loadSourceElementPage("next").catch(reportError)}
                  >{sourceElementsLoading ? "加载中…" : `继续加载（已显示 ${sourceElements.length}/${sourceElementsTotal}）`}</button>
                )}
          </div>
        </SourceDetailWindow>
      )}

      {/* 命令目录审阅弹窗:必须挂在 page 根层、不能挂进上面 SourceDetailWindow 的
          子树——那张卡片自己就是 FloatingModalCard,桌面态恒带 translate3d 因而
          是 fixed 后代的包含块,920px 宽的审阅弹窗塞进 740px 的来源详情卡片会被
          overflow:hidden 裁掉(P0,两次评审独立确认)。与 infoModal/MemorySaveDialog
          同一种「调用方持有开关状态、根层渲染」形状。 */}
      {rootModals.view("catalog-review").open && catalogReview && (
        <CommandCatalogReview
          notebookId={catalogReview.notebookId}
          sourceId={catalogReview.sourceId}
          sourceTitle={catalogReview.sourceTitle}
          jobId={catalogReview.jobId}
          canEdit={!readOnlyWorkspace}
          onClose={() => rootModals.requestClose("catalog-review", "button")}
          onOpenTable={openKnowhowTable}
          onToast={setToast}
          isCurrent={() => rootModals.owns(catalogReviewLease)}
          onReviewed={() => setCatalogReviewSeq((seq) => seq + 1)}
          interactive={rootModals.view("catalog-review").topmost}
          zIndex={rootModals.view("catalog-review").zIndex}
        />
      )}

      {rootModals.view("conversation-share").open && sharingSession && currentNotebookId && (
        <ConversationShareModal
          // key 含边界:同一条会话里换一条回答再点分享,必须整块重挂,否则弹窗会带着
          // 上一次的 notice/error 与已加载态,把「已生成分享链接」按到新的边界上。
          key={`${sharingSession.id}:${sharingSession.throughAnswerId}`}
          notebookId={currentNotebookId}
          conversationId={sharingSession.id}
          title={sharingSession.title || ""}
          throughAnswerId={sharingSession.throughAnswerId}
          onClose={() => rootModals.requestClose("conversation-share", "button")}
          interactive={rootModals.view("conversation-share").topmost}
          zIndex={rootModals.view("conversation-share").zIndex}
        />
      )}

      {rootModals.view("analytics").open && analytics && (
        <section className="utility-modal" role="dialog" aria-modal={rootModals.view("analytics").topmost} aria-hidden={!rootModals.view("analytics").topmost} inert={rootModals.view("analytics").topmost ? undefined : true} style={{ zIndex: rootModals.view("analytics").zIndex }} onClick={(event) => { if (event.currentTarget === event.target) closeAnalytics("backdrop"); }}>
          <FloatingModalCard storageKey="analytics.window" className="utility-modal-card">
            {(floating) => (<>
            <div className="source-modal-header" {...floating.dragHandleProps}>
              <div>
                <h2>知识分析看板</h2>
                <p>回答质量、审核进度、知识覆盖、来源状态与索引构建状态的本机统计。</p>
              </div>
              <button className="icon-button" onClick={() => closeAnalytics("button")} title="Close">×</button>
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
                      // extract_kg 是例外:它**只**看 kgGraph.buildingKg,不进 repairingFix
                      // (见下方 runFix 里的理由)。isRepairing 对它恒为 false(从没写过那个键),
                      // 忙碌态完全由 kgGraph.buildingKg 表达——那个位失败时会被 startKgBuild 清掉。
                      const repairing = isRepairing(repairingFix[g.key], g.count)
                        || (g.fix === "extract_kg" && kgGraph.buildingKg);
                      const runFix = async () => {
                        const nb = currentNotebookId;
                        if (!nb || repairing) return;
                        // extract_kg **只**认 kgGraph.buildingKg,不另记 repairingFix(codex 第 1
                        // 轮 P2)。startKgBuild 是 fire-and-forget:同步置 kgGraph.buildingKg,
                        // POST 失败时自己在异步回调里清掉,但**不回传受理结果**。若这里也记一份
                        // repairingFix,那份记录在建图请求被拒时没人清得掉(count 没变、
                        // kgGraph.buildingKg 已归位),
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
                          {!readOnlyWorkspace && (
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
                readOnly={readOnlyWorkspace}
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
                      || kgGraph.buildingKg;
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
                        {!busy && !readOnlyWorkspace && (
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
                                {/* 补连是**后台任务**:POST 立刻返回,忙碌位由 relink/status 的
                                    轮询在终态解除(relinkFromKgView 与它下面那条 effect)。上面那个
                                    busy 只看 KG 构建、不含 kgGraph.relinking——所以这里必须自己带忙碌位,
                                    否则「点完还能接着点」会一路点到服务端 409。
                                    与知识图谱视图侧栏的同一动作(disabled + 「补连中…」)保持一致。 */}
                                <button
                                  type="button"
                                  className="index-cta"
                                  disabled={kgGraph.relinking || kgGraph.rebuilding}
                                  onClick={relinkFromKgView}
                                >
                                  {kgGraph.relinking ? "补连中…" : "补上关联"}
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
                    const busy = uk.building || kgGraph.rebuilding;
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
                        {!busy && !readOnlyWorkspace && (
                          <div className="index-ctas">
                            {/* codex R4 P2(B):这一格自己的 busy 只看 uk.building/kgGraph.rebuilding
                                （概念合并自身的状态标签/色调不该被「补上关联」污染），但共用同一把
                                服务端单飞锁的「补上关联」在跑时，这颗按钮仍需同口径禁用，否则点了
                                只会撞 409。 */}
                            <button
                              type="button"
                              className={`index-cta${uk.dirty ? " primary" : ""}`}
                              disabled={kgGraph.relinking}
                              onClick={confirmRefreshUnifiedKg}
                            >
                              重新合并
                            </button>
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
                      {!readOnlyWorkspace && (
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
                      : v.state === "queued" ? queuedScheduleHint(s, new Date())
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
                        {!readOnlyWorkspace && (
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

      {rootModals.view("kg-schema").open && kgSchema.open && (
        <section className="utility-modal" role="dialog" aria-modal={rootModals.view("kg-schema").topmost} aria-hidden={!rootModals.view("kg-schema").topmost} inert={rootModals.view("kg-schema").topmost ? undefined : true} style={{ zIndex: rootModals.view("kg-schema").zIndex }} onClick={(event) => { if (event.currentTarget === event.target) closeKgSchemas("backdrop"); }}>
          <FloatingModalCard storageKey="schema.window" className="utility-modal-card schema-modal-card">
            {(floating) => (<>
            <div className="source-modal-header" {...floating.dragHandleProps}>
              <div>
                <h2>图谱 Schema</h2>
                <p>左边是这个范围里现有的类型，右边是选中类型的定义；改动要按下保存才生效。</p>
              </div>
              <button className="icon-button" onClick={() => closeKgSchemas("button")} title="Close">×</button>
            </div>
            <div className="schema-modal-body">
              <SchemaManager
                schemas={kgSchema.schemas}
                busy={kgSchema.busy}
                view={kgSchema.view}
                canEdit={kgSchema.view === "global" ? capabilities.canManageGlobalSchemas : capabilities.canManageNotebookSchemas}
                canManageGlobal={capabilities.canManageGlobalSchemas}
                onView={kgWorkspace.selectSchemaView}
                onPatch={kgWorkspace.patchSchema}
                onCreate={kgWorkspace.createSchema}
                onDelete={kgWorkspace.deleteSchema}
                onInduce={kgWorkspace.induceSchemas}
              />
            </div>
            </>)}
          </FloatingModalCard>
        </section>
      )}

      {/* Agent Profile remains an independent presentation modal. */}
      {rootModals.view("understanding").open && currentNotebookId && (
        <section className="utility-modal" role="dialog" aria-modal={rootModals.view("understanding").topmost} aria-hidden={!rootModals.view("understanding").topmost} inert={rootModals.view("understanding").topmost ? undefined : true} style={{ zIndex: rootModals.view("understanding").zIndex }} onClick={(event) => { if (event.currentTarget === event.target) rootModals.requestClose("understanding", "backdrop"); }}>
          <FloatingModalCard storageKey="understanding.window" className="utility-modal-card">
            {(floating) => (<>
            <div className="source-modal-header" {...floating.dragHandleProps}>
              <div>
                <h2>AI 对这个库的理解</h2>
                {/* 窗口标题栏只说「这扇窗里是什么」。原来这句话把下面两档的说明
                    又复述了一遍(「AI 读过这个库之后形成的印象，以及你自己的检索
                    心得」逐字重复第一档的说明),同一屏里同一件事说了两遍。
                    ⚠ 措辞必须把两档「理解」与「Agent 记录」分开(codex #616 R4 P2):
                    只有前两档会进提问、可以改;记录是只读留痕,绝不进回答。一句
                    笼统的「下面几档都会带上、都能改」会给出错误的隐私预期。 */}
                <p>前两档会在提问时一并带上，可以随时改；最后的 Agent 记录只留痕，不会进入回答。</p>
              </div>
              <button className="icon-button" onClick={() => rootModals.requestClose("understanding", "button")} title="Close">×</button>
            </div>
            <div className="source-detail-body">
              {/* key=当前笔记本:切库那一刻整个面板重挂而不是靠 prop 变化自己刷新——
                  草稿/忙碌位/确认态必须随库清零,不能等下面那条关弹窗 effect 追上来
                  (那条 effect 是第二层保险,这里是结构性保证)。 */}
              {/* 依据来源 chip 复用**引用卡点击走的同一条**打开路径,不另造弹窗;
                  标题从已加载的来源列表解析(`display_title` 与引用卡、清单卡同一
                  份服务端命名),查不到就由面板退回 id。 */}
              <AgentProfilePanel
                key={currentNotebookId}
                notebookId={currentNotebookId}
                onOpenSource={(sourceId) => onOpenSourceElement(sourceId)}
                resolveSourceTitle={(sourceId) =>
                  sources.find((item) => item.id === sourceId)?.display_title || ""
                }
              />
            </div>
            </>)}
          </FloatingModalCard>
        </section>
      )}


      {kgGraph.open && (
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
                onClick={openKgAnalysis}
                title="查看这个知识库的构成、合并收敛与主题板块分布"
              >
                <BarChart3 size={16} /> 图谱分析
              </button>
              <button
                type="button"
                className="sort-button kg-schema-button"
                onClick={openKgSchemas}
                title="查看当前笔记本采用的知识对象类型与字段"
              >
                <Database size={16} /> 图谱 Schema
              </button>
              <button className="icon-button" onClick={() => closeKgView()} title="Close">×</button>
            </div>
          </div>
          <div className="kg-view-body">
            <aside className="kg-rail">
              <input className="kg-search" placeholder="搜索节点名称或类型…" value={kgGraph.search} onChange={(e) => handleKgSearchChange(e.target.value)} />
              {!readOnlyWorkspace && (
              <div className="kg-rail-section">
                <h3>图谱处理</h3>
                <div className="kg-action-stack">
                  {/* codex R4 P2(B):「重新合并」与「补上关联」共用服务端同一把按笔记本
                      单飞锁，disabled 必须认「任一忙碌位为真即忙」——否则占槽的那一件事
                      在跑时，另一颗按钮仍可点，点了也只会撞 409。各自的进行态文案不变。 */}
                  <button
                    type="button"
                    className="sort-button"
                    disabled={kgGraph.relinking || kgGraph.rebuilding || kgGraph.buildingKg}
                    title="为没建立关联的内容补上关联（快速、确定性，不覆盖现有图）"
                    onClick={relinkFromKgView}
                  >
                    {kgGraph.relinking ? "补连中…" : "补上关联"}
                  </button>
                  <button
                    type="button"
                    className="sort-button"
                    disabled={kgGraph.rebuilding || kgGraph.relinking || kgGraph.buildingKg}
                    title="对现有概念重新聚类 / 跨文档合并并刷新（不重新分析来源，会先确认）"
                    onClick={confirmRefreshUnifiedKg}
                  >
                    {kgGraph.rebuilding ? "合并中…" : "重新合并"}
                  </button>
                  <button
                    type="button"
                    className="sort-button kg-action-danger"
                    disabled={kgGraph.buildingKg}
                    title="清空现有知识图谱并重新分析全部来源（后台任务，可能数分钟）"
                    onClick={() => { if (currentNotebookId) startKgRebuild(currentNotebookId); }}
                  >
                    {kgGraph.buildingKg ? "分析中…" : "全部重新分析"}
                  </button>
                </div>
              </div>
              )}
              <div className="kg-rail-section">
                <h3>当前视图</h3>
                <div className="tag-row">
                  <span className="tag">节点 {fgData.nodes.length}{!kgSearching && kgGraph.merged ? ` / ${kgGraph.merged.nodes.length}` : ""}</span>
                  <span className="tag">边 {fgData.links.length}{!kgSearching && kgGraph.merged ? ` / ${kgGraph.merged.edges.length}` : ""}</span>
                </div>
                <label className="kg-range">
                  <span>范围</span>
                  <select value={kgGraph.rangeLimit} disabled={kgGraph.rangeBusy || kgSearching} onChange={(e) => changeKgRange(Number(e.target.value))}>
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
                    {kgGraph.searchBusy
                      ? "搜索中…"
                      : `命中 ${fgData.searchHitCount} 个节点`}
                  </p>
                ) : kgGraph.graph && (
                  <p className="tool-hint" style={{ margin: "4px 2px 0" }}>
                    {kgGraph.rangeBusy
                      ? "加载中…"
                      : kgGraph.graph.truncated
                        ? `已载 ${kgGraph.graph.nodes.length} / 共 ${kgGraph.graph.total_nodes ?? kgGraph.graph.nodes.length} 节点 · 按连接度，可扩大范围`
                        : `共 ${kgGraph.graph.total_nodes ?? kgGraph.graph.nodes.length} 节点（已全部显示）`}
                  </p>
                )}
                {kgGraph.status && (
                  <div className="tag-row" style={{ marginTop: 4 }}>
                    {/* 纯状态展示,非交互——唯一动作入口是上方「重新合并」按钮(去重复,见其 title)。 */}
                    <span
                      className="tag"
                      title="概念合并状态；点击上方「重新合并」按钮可手动刷新"
                      style={{ color: kgGraph.status.dirty ? "var(--color-warn, #b97a00)" : undefined }}
                    >
                      {kgGraph.rebuilding ? "重建中…" : kgGraph.status.dirty ? "待重建" : "最新"}
                    </span>
                    {kgGraph.status.last_rebuild_at && (
                      <span className="tag">上次重建 · {formatRelativeTime(kgGraph.status.last_rebuild_at)}</span>
                    )}
                    {scaleIndexStatus && (() => {
                      const s = scaleIndexStatus;
                      const v = describeScaleIndex(s);
                      const clickable = v.primaryOp !== null && !readOnlyWorkspace;
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
                            : v.state === "queued" ? queuedScheduleHint(s, new Date())
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
                    aria-pressed={kgGraph.selectedTypes.length === 0}
                    className={kgGraph.selectedTypes.length === 0 ? "active" : ""}
                    onClick={kgWorkspace.clearTypes}
                  >
                    <span className="kg-shape-stack">
                      {kgTypeCounts.slice(0, 4).map((item) => <KgTypeMark key={item.type} type={item.type} />)}
                    </span>
                    <strong>全部</strong>
                    <em>{kgGraph.graph?.nodes.length ?? 0}</em>
                  </button>
                  {kgTypeCounts.map((item) => (
                    <button
                      aria-pressed={kgGraph.selectedTypes.includes(item.type)}
                      className={kgGraph.selectedTypes.includes(item.type) ? "active" : ""}
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
                <h3>待确认合并 ({kgGraph.pendingMerges.length})</h3>
                {!readOnlyWorkspace && (
                  <>
                    <button className="ghost-button" onClick={reviewPendingMerges} disabled={!kgGraph.pendingMerges.length || kgGraph.reviewBusy}>
                      {kgGraph.reviewBusy ? "判重中…" : "自动判重"}
                    </button>
                    <button
                      className="ghost-button"
                      onClick={reviewAllMerges}
                      disabled={!kgGraph.pendingMerges.length || kgGraph.reviewAllStarting || kgGraph.reviewAllJob?.status === "running"}
                    >
                      {kgGraph.reviewAllJob?.status === "running"
                        ? `全部判重中… ${kgGraph.reviewAllJob.done}/${kgGraph.reviewAllJob.total}`
                        : kgGraph.reviewAllStarting
                          ? "全部判重中…"
                          : "全部自动判重"}
                    </button>
                  </>
                )}
                {kgGraph.pendingMerges.length === 0 ? <p className="tool-hint">无</p> : kgGraph.pendingMerges.map((m) => (
                  <div className="kg-merge-row" key={m.id}>
                    <span>{m.canonical_a.replace(/^K-/, "")} ↔ {m.canonical_b.replace(/^K-/, "")} <em>({m.score.toFixed(2)})</em></span>
                    {!readOnlyWorkspace && <span className="kg-merge-actions">
                      {/* 确认会连带跑一次全量概念合并重建；重建完成前锁住整列，避免新决定
                          与正在发布的旧候选代次竞态。拒绝不重建，但提交期间同样防重复点。 */}
                      <button disabled={kgGraph.decidingMerge !== null || kgGraph.rebuilding} onClick={() => decideMerge(m, true)}>
                        {kgGraph.decidingMerge?.id === m.id && kgGraph.decidingMerge.confirm ? "合并中…" : "合并"}
                      </button>
                      <button disabled={kgGraph.decidingMerge !== null || kgGraph.rebuilding} onClick={() => decideMerge(m, false)}>
                        {kgGraph.decidingMerge?.id === m.id && !kgGraph.decidingMerge.confirm ? "分开中…" : "拒绝"}
                      </button>
                    </span>}
                  </div>
                ))}
              </div>
            </aside>
            <div className="kg-canvas" ref={kgCanvasRef}>
              {kgGraph.graph === null ? <p className="tool-hint kg-canvas-empty">加载中…</p> : kgGraph.vizBuilding ? (
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
                  nodeCanvasObject={(node: any, ctx: CanvasRenderingContext2D, globalScale: number) => drawKgNode(node, ctx, globalScale, kgGraph.selectedNodeId, kgDenseView)}
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
                          className={`kg-node-button ${kgGraph.selectedNodeId === node.id ? "active" : ""}`}
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
                    {kgGraph.nodeContext?.definition && (<><h4>定义</h4><p className="kg-text-card">{kgGraph.nodeContext.definition}</p></>)}
                    {kgGraph.nodeContext?.object_type === "procedure" && kgGraph.nodeContext.steps && kgGraph.nodeContext.steps.length > 0 && (
                      <><h4>流程步骤</h4>{kgGraph.nodeContext.steps.map((s, i) => (
                        <KgProcedureStepCard step={s} index={i} key={`${s.name}-${i}`} />
                      ))}</>
                    )}
                    {kgGraph.conceptDetail && (
                      <>
                        <h4>出处</h4>
                        {kgGraph.conceptDetail.evidence.length === 0 ? <p className="tool-hint">无</p> : kgGraph.conceptDetail.evidence.slice(0, 20).map((ev, i) => (
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
                    {!kgGraph.conceptDetail && kgGraph.nodeContext && (kgGraph.nodeContext.occurrences ?? []).length > 0 && (
                      <><h4>出处</h4>{(kgGraph.nodeContext.occurrences ?? []).slice(0, 10).map((o, i) => (
                        <KgOccurrenceCard occurrence={o} index={i} key={`${o.source_title || o.source_id}-${i}`} />
                      ))}</>
                    )}
                  </div>
                )}
              </div>
            </aside>
          </div>
          {rootModals.view("kg-analysis").open && kgGraph.analysisOpen && currentNotebookId && (
            <KgAnalysisView
              notebookId={currentNotebookId}
              canAnalyze={!readOnlyWorkspace}
              analysisRunning={kgGraph.rebuilding}
              analysisBlocked={kgGraph.relinking || kgGraph.buildingKg}
              interactive={rootModals.view("kg-analysis").topmost}
              zIndex={rootModals.view("kg-analysis").zIndex}
              onAnalyze={confirmGenerateKgAnalysis}
              onClose={closeKgAnalysis}
            />
          )}
        </section>
      )}

      {knowhowNavigation.isOpen && currentNotebookId && (
        <KnowhowPanel
          notebookId={currentNotebookId}
          apiBase={API_BASE}
          canEdit={!readOnlyWorkspace}
          onClose={closeKnowhow}
          initialTableId={knowhowNavigation.jumpTarget?.tableId}
          initialRowId={knowhowNavigation.jumpTarget?.rowId}
          initialHealthFilter={knowhowNavigation.healthFilter}
        />
      )}

      {rootModals.view("promotion-queue").open && (
        <section
          className="utility-modal"
          role="dialog"
          aria-modal={rootModals.view("promotion-queue").topmost}
          aria-hidden={!rootModals.view("promotion-queue").topmost}
          inert={rootModals.view("promotion-queue").topmost ? undefined : true}
          style={{ zIndex: rootModals.view("promotion-queue").zIndex }}
          onClick={(event) => { if (event.currentTarget === event.target) rootModals.requestClose("promotion-queue", "backdrop"); }}
        >
          <FloatingModalCard storageKey="promotion.window" className="utility-modal-card">
            {(floating) => (<>
            <div className="source-modal-header" {...floating.dragHandleProps}>
              <div>
                <h2>内容审核</h2>
                <p>个人知识库中的内容与记忆候选申请收录到公共知识库。批准后会合并重复并加入所选的目标公共知识库。</p>
              </div>
              <button className="icon-button" onClick={() => rootModals.requestClose("promotion-queue", "button")} title="Close">×</button>
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
                              目标库 owner,notebookCollection.rows 只覆盖自有∪只读加入,猜不出别人
                              创建的公共库真名)；查不到再回退旧写法(notebookCollection.rows.find),
                              最后兜底截断 id。 */}
                          目标公共知识库: {cand.target_base_name || notebookCollection.rows.find((n) => n.id === cand.target_base_id)?.name || cand.target_base_id.slice(0, 10)}
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

      {rootModals.view("promotion-target").open && pendingPromotionObjectId && (
        <section
          className="utility-modal"
          role="dialog"
          aria-modal={rootModals.view("promotion-target").topmost}
          aria-hidden={!rootModals.view("promotion-target").topmost}
          inert={rootModals.view("promotion-target").topmost ? undefined : true}
          style={{ zIndex: rootModals.view("promotion-target").zIndex }}
          onClick={(event) => { if (event.currentTarget === event.target) rootModals.requestClose("promotion-target", "backdrop"); }}
        >
          <FloatingModalCard storageKey="promotionTarget.window" className="utility-modal-card narrow">
            {(floating) => (<>
            <div className="source-modal-header" {...floating.dragHandleProps}>
              <div>
                <h2>选择贡献目标</h2>
                <p>本笔记本挂载了多个公共知识库，请选择这条知识要进入哪一个。</p>
              </div>
              <button className="icon-button" onClick={() => rootModals.requestClose("promotion-target", "button")} title="Close">×</button>
            </div>
            <div className="promotion-target-list">
              {(promotionTarget.kind === "choose" ? promotionTarget.options : []).map((base) => (
                <button
                  key={base.id}
                  type="button"
                  className="sort-button promotion-target-option"
                  onClick={() => {
                    const objectId = pendingPromotionObjectId;
                    if (objectId && rootModals.requestClose("promotion-target", "button")) {
                      submitPromotion(objectId, base.id).catch(reportError);
                    }
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

      {rootModals.view("edge-review").open && (
        <section
          className="utility-modal"
          role="dialog"
          aria-modal={rootModals.view("edge-review").topmost}
          aria-hidden={!rootModals.view("edge-review").topmost}
          inert={rootModals.view("edge-review").topmost ? undefined : true}
          style={{ zIndex: rootModals.view("edge-review").zIndex }}
          onClick={(event) => { if (event.currentTarget === event.target) rootModals.requestClose("edge-review", "backdrop"); }}
        >
          <FloatingModalCard storageKey="edgeReview.window" className="utility-modal-card">
            {(floating) => (<>
            <div className="source-modal-header" {...floating.dragHandleProps}>
              <div>
                <h2>关系审核队列</h2>
                <p>按「高中心性 × 低可信」排序的关系。确认可信的关联，或拒绝错误的关联（被拒的关联将从所有图推理遍历中排除）。</p>
              </div>
              <button className="icon-button" onClick={() => rootModals.requestClose("edge-review", "button")} title="Close">×</button>
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

      {rootModals.view("model-service").open && (
        <ModelServicePanel
          status={modelStatus}
          highlightedServiceId={highlightedModelServiceId}
          isAdmin={currentUser.role === "admin"}
          onTestOne={async (serviceId) => { await runSystemModelTest(serviceId); }}
          onTestAll={runAllSystemModelTests}
          onClose={closeModelPanel}
          testingServiceIds={modelTestActivity.services}
          allTesting={modelTestActivity.all}
          interactive={rootModals.view("model-service").topmost}
          zIndex={rootModals.view("model-service").zIndex}
        />
      )}
    </div>
  );
}
function NotebookList({
  entries,
  roleText,
  openingNotebookId,
  openNotebook,
  openMemory,
  openMenu
}: {
  entries: Array<{ notebook: NotebookSummary; index: number; hits: SearchHit[] }>;
  /** 覆盖「角色」列的文案。「群组」分区传「群组成员」;省略时按每行的 access 判。 */
  roleText?: string;
  /**
   * 正在打开的笔记本 id。列表视图与网格卡片是同一个动作的两种外观,忙碌反馈必须
   * 同权:命中行的四个「打开」单元格立刻禁用并显示旋转指示,否则用户在列表里连点
   * 依旧会叠出多组并行请求(网格那边已经挡住了,这边没挡就是同一个缺陷的另一半)。
   */
  openingNotebookId: string | null;
  openNotebook: (id: string) => void;
  openMemory: (id: string) => void;
  openMenu: (id: string, event: MouseEvent<HTMLButtonElement>) => void;
}) {
  // 全部库都落在「群组」分区时,主区一行都没有——只剩一排孤零零的表头,读起来像
  // 「这里本该有东西但没加载出来」。没有行就整段不渲染。
  if (entries.length === 0) return null;
  return (
    <section className="notebook-list">
      <div className="notebook-list-header">
        <span>标题</span><span>来源</span><span>记忆</span><span>创建日期</span><span>角色</span><span />
      </div>
      {entries.map(({ notebook, index, hits }) => {
        const opening = openingNotebookId === notebook.id;
        return (
        <article className="notebook-list-row" key={notebook.id}>
          <button
            className={`notebook-list-title${opening ? " is-opening" : ""}`}
            aria-busy={opening || undefined}
            disabled={opening}
            onClick={() => openNotebook(notebook.id)}
          >
            <span className="list-icon">{cardIcon(index, notebook)}</span>
            <span>
              <strong>{notebook.name}</strong>
              {opening && (
                <small className="notebook-list-open-status">
                  <span className="notebook-card-open-spinner" aria-hidden="true" />
                  打开中…
                </small>
              )}
              {isGroupGranted(notebook) && <small>{grantedViaLabel(notebook)}</small>}
              <SearchHits hits={hits} compact />
            </span>
          </button>
          {/* 三个数据格与标题同一个动作(打开这本库),所以同禁用。⋮ 菜单与「N 条记忆」
              是另外的动作,不禁用。 */}
          <button className="notebook-list-cell" disabled={opening} onClick={() => openNotebook(notebook.id)}>{notebook.counts.sources ?? 0} 个来源</button>
          <button className="notebook-list-cell notebook-memory-link" onClick={() => openMemory(notebook.id)}>{notebook.counts.memories ?? 0} 条</button>
          <button className="notebook-list-cell" disabled={opening} onClick={() => openNotebook(notebook.id)}>{notebook.created_label}</button>
          <button className="notebook-list-cell role-cell" disabled={opening} onClick={() => openNotebook(notebook.id)}>{notebookRoleText(notebook, roleText)}</button>
          <button className="list-row-menu" onClick={(event) => openMenu(notebook.id, event)} title="笔记本操作">⋮</button>
        </article>
        );
      })}
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
  // Strip every tag that is not part of the allow-list above. Stripped tags become
  // a single space, not "": mammoth wraps each cell paragraph in <p>, so dropping
  // them outright glued a two-paragraph cell's "a" and "b" into "ab". HTML collapses
  // the extra whitespace on render, so a plain space is enough.
  return withoutHandlers.replace(/<\/?[a-z][^>]*>/gi, (tag) => (allowed.test(tag) ? tag : " "));
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
    // markdown 的 `> **图片描述**` 引用块。与图注并列渲染而不是二选一：图注是
    // 一行标签，描述是对图的展开说明，两者都写了就都显示。
    const description = typeof element.metadata?.description === "string"
      ? element.metadata.description
      : "";
    const descriptionLines = description.split("\n").filter((line) => line.trim() !== "");
    const descriptionNode = descriptionLines.length > 0 ? (
      <div className="element-image-description">
        {descriptionLines.map((line, index) => <p key={index}>{line}</p>)}
      </div>
    ) : null;
    const url = sourceImageAssetUrl(API_BASE, notebookId, assetId);
    if (url) {
      return (
        <figure className="element-image-figure">
          <AuthedImage url={url} alt={caption || description || "figure"} />
          {caption ? <figcaption>{caption}</figcaption> : null}
          {descriptionNode}
        </figure>
      );
    }
    if (caption || descriptionNode) {
      return (
        <figure className="element-image-figure">
          {caption ? <figcaption>{caption}</figcaption> : null}
          {descriptionNode}
        </figure>
      );
    }
    // 无可渲染图片资源(如 MINERU_RETURN_IMAGES=0 关闭图片)且既无 caption 也无
    // 图片描述时，回退到与其余元素类型一致的纯文本展示，避免空的占位边框。
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

function KnowledgeBrowser({
  kind,
  items,
  types,
  statusFilter,
  duplicates,
  contexts,
  onLoadContext,
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
  contexts: Record<string, NodeContext>;
  onLoadContext: (id: string) => void;
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
  const [dupBusy, setDupBusy] = useState(false);
  // 正在合并的重复条目 id。onMerge 会连着重拉知识列表、类型统计并**重跑一次查重**,
  // 是这个面板里最慢的一步;不锁住的话在几个重复组上连点会并发排出若干次全量重扫。
  const [mergingId, setMergingId] = useState<string | null>(null);
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
              {!contexts[item.id] && (
                <button className="sort-button" onClick={() => onLoadContext(item.id)}>展开原文</button>
              )}
              {contexts[item.id] && (
                <>
                  {contexts[item.id].object_type === "procedure" && contexts[item.id].steps && (contexts[item.id].steps ?? []).length > 0 && (
                    <><p className="section-title">流程步骤</p>{(contexts[item.id].steps ?? []).map((s, i) => (
                      <KgProcedureStepCard step={s} index={i} key={`${s.name}-${i}`} />
                    ))}</>
                  )}
                  {(contexts[item.id].occurrences ?? []).length > 0 && (
                    <><p className="section-title">原文出处</p>{(contexts[item.id].occurrences ?? []).slice(0, 5).map((o, i) => (
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
