import type { CitationImageLike } from "./answer-formatting.ts";
import { countCodePoints } from "./input-limits.ts";
import type { ReportCoverage } from "./report-outline-model.ts";
import type { BaseScopePayload, SourceScopePayload } from "./source-scope.ts";

/** Stable report-workspace protocol values shared by the owner and view. */
export const REPORT_DEPTHS = [1, 2, 4, 8, 16] as const;
export const REPORT_DEFAULT_DEPTH_INDEX = 1;
export const REPORT_POLL_INTERVAL_MS = 6000;

export const isReportActive = (status: string): boolean =>
  status === "pending"
  || status === "running"
  || status === "planning"
  || status === "generating";

export const REPORT_INPUT_LIMITS = {
  questionMaxChars: 4000,
} as const;

/** Reject over-limit user text without silently truncating it. */
export const reportQuestionLimitHint = (question: string): string | null => {
  const used = countCodePoints(question);
  const max = REPORT_INPUT_LIMITS.questionMaxChars;
  return used > max
    ? `研究问题超出 ${max} 字上限（当前 ${used} 字），请精简后再开始`
    : null;
};

/** API-facing report contracts shared by the data owner and presentation layer. */
export type ReportSummaryT = {
  id: string;
  question: string;
  status: string;
  progress: string;
  section_count: number;
  created_at: string;
  generation_started_at?: string;
  updated_at?: string;
  created_by: string;
  depth?: number;
};

export type ReportSufficiency = "充足" | "薄弱" | "缺失";

export type ReportOutlineSectionT = {
  title: string;
  scope: string;
  sub_queries: string[];
  perspectives?: string[];
  tensions?: string[];
  sufficiency?: ReportSufficiency;
  gap_note?: string;
  intent_ids?: string[];
  intent_questions?: string[];
  intent_catalog?: { id: string; title: string; question: string; retrieval_queries: string[] }[];
  intent_contract?: {
    objective: string;
    mandatory_topics: { id: string; title: string; question: string; retrieval_queries: string[] }[];
    comparison_axes?: string[];
    constraints?: string[];
    excluded_topics?: string[];
    expected_output?: string;
  };
  coverage?: ReportCoverage;
};

export type ReportDistributionT = {
  label?: string;
  name?: string;
  value?: string | number;
  type?: string;
  year?: string | number;
  count?: number;
};

export type ReportCorpusProfileT = {
  total_sources?: number;
  displayed_sources?: number;
  representative_count?: number;
  independent_documents?: number;
  independent_families?: number;
  duplicate_inflation?: number;
  identified_duplicate_lower_bound?: number;
  identity_uncertain_sources?: number;
  type_distribution?: ReportDistributionT[] | Record<string, number>;
  year_distribution?: ReportDistributionT[] | Record<string, number>;
  representatives?: { title?: string; label?: string; source_title?: string }[];
  metadata_coverage?: number;
  metadata_sources?: number;
  unknown_year?: number;
  completeness_disclosure?: string;
  unavailable_reason?: string;
};

export type ReportFrameFacetT = {
  id: string;
  name: string;
  values: string[];
  exclusive?: boolean;
};

export type ReportFrameAxisT = {
  id: string;
  name: string;
  condition_fields: string[];
};

export type ReportFrameT = {
  subject_kind?: string;
  facets?: ReportFrameFacetT[];
  axes?: ReportFrameAxisT[];
  instance_policy?: string;
};

export type ReportCredibilityT = {
  independent_documents?: number;
  independent_source_families?: number;
  independent_sources?: number;
  anchor_count?: number;
  top1_share?: number;
  top1_concentration?: number;
  synthesis_status?: "not_requested" | "available" | "skipped_no_evidence" | "failed_model" | "failed_validation";
  claim_ledgers_available?: number;
  claim_ledgers_partial?: number;
  claim_ledgers_total?: number;
};

export type ReportCitationAuditT = {
  support_rate?: number;
  supported_claims?: number;
  total_claims?: number;
  high_risk_uncited_count?: number;
  unsupported?: number;
  high_risk_assertions?: number;
};

export type ReportContextStageT = {
  failed?: boolean;
  compressed?: boolean;
  input_items?: number;
  output_items?: number;
  omitted_items?: number;
  partial_items?: number;
  invalid_items?: number;
};

export type ReportContextProcessingT = {
  extra_calls?: number;
  extra_calls_max?: number;
  max_level?: number;
  compression_max_levels?: number;
  compressed?: boolean;
  estimated_counting?: boolean;
  stages?: Partial<Record<"planning_intent" | "planning_outline" | "planning_sufficiency" | "retrieve" | "draft" | "synthesis" | "final_editor", ReportContextStageT>>;
};

export type ReportUnderstandingT = {
  objective?: string;
  resolved_question?: string;
  intent_type?: string;
  entities?: string[];
  mandatory_topics?: { id: string; title: string; question: string; retrieval_queries: string[] }[];
  comparison_axes?: string[];
  constraints?: string[];
  excluded_topics?: string[];
  expected_output?: string;
  assumptions?: string[];
  ambiguities?: {
    id: string;
    question: string;
    reason?: string;
    required?: boolean;
    options?: string[];
  }[];
  confidence?: number;
  needs_clarification?: boolean;
  confirmed?: boolean;
  result_scope?: "ranked" | "complete" | "aggregate" | "hybrid";
  completeness_required?: boolean;
  corpus_profile?: ReportCorpusProfileT;
  report_frame?: ReportFrameT;
  credibility?: ReportCredibilityT;
  context_processing?: ReportContextProcessingT;
  /** 报告创建时冻结的两维检索范围（服务端快照，`narrowed` 说明是否真的收窄过）。
   *  「就此追问」不直接读它，而是问 `GET …/followup` 要一份按现状过滤过的范围。 */
  source_scope?: SourceScopePayload & { narrowed?: boolean | null };
  base_scope?: BaseScopePayload & { narrowed?: boolean | null };
};

/**
 * `GET /notebooks/{nb}/reports/{rid}/followup`：「就此追问」起步用的检索范围——报告
 * 生成时冻结的范围，减去如今已无法勾选的部分。真源：`backend/app/models/reports.py
 * ReportFollowupScope`。没收窄过的一维是「什么都不排除」（`exclude` + 空表）。
 * `dropped_*` 是冻结时勾选、现在已无法勾选的条数；`fell_back_to_all` 为真时两维都
 * 已退回全选（冻结的选择已无可用部分，或无法按原样恢复）。
 */
export type ReportFollowupScopeT = {
  report_id: string;
  title: string;
  source_scope: SourceScopePayload;
  base_scope: BaseScopePayload;
  dropped_sources?: number;
  dropped_libraries?: number;
  fell_back_to_all?: boolean;
};

export type ReportDetailT = ReportSummaryT & {
  outline: ReportOutlineSectionT[];
  sections: {
    title: string;
    markdown: string;
    grounded: boolean;
    evidence_level?: string;
    failed?: boolean;
    // 失败章节的原文,只有报告作者的详情里有;正文里失败章节只写中性说明。
    error?: string;
    citation_audit?: ReportCitationAuditT;
    context_processing?: {
      max_level?: number;
      coverage_limited?: boolean;
    };
  }[];
  section_status?: { title: string; phase: string; step: number }[];
  gaps: string[];
  content_md: string;
  shared?: boolean;
  references: {
    key: string;
    label: string;
    name?: string;
    source_title?: string;
    source_file_name?: string;
    location_label?: string;
    object_id?: string;
    object_type?: string;
    source_id?: string;
    element_id?: string;
    snippet?: string;
    tier?: string;
    from_reference_library?: boolean;
    family_key?: string;
    images?: CitationImageLike[];
  }[];
  understanding: ReportUnderstandingT;
  error: string;
};
