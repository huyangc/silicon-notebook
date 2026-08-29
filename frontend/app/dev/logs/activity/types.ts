// Types for the "活动" view's data layer (dev/logs/activity).
//
// Field names mirror the backend contract crystallized in
// docs/superpowers/specs/2026-08-04-user-activity-log-view-design_zh.md §3-4
// and implemented concurrently by another task in
// backend/app/api/admin_routes.py + backend/app/repositories/{sqlite,postgres}/query_store.py.
// Do not rename fields here without updating that contract.

// 来源清单响应沿用既有 `GET /notebooks/{id}/sources` 的分页形状——不重新声明
// 一份,`SourceSummary`/`PaginatedSources` 的真源是 workspace-model.ts。
export type { SourceSummary, PaginatedSources } from "../../../workspace-model.ts";

export type ActivityCursor = { ts: string; id: string };

/** 空串表示混合活动；其余值与后端 activity_type 查询参数逐字对应。 */
export type ActivityTypeFilter = "" | "ask" | "source" | "report";

export type ActivityAsk = {
  type: "ask";
  id: string;
  notebook_id: string;
  created_at: string;
  asked_at: string;
  conversation_id: string;
  question: string;
  mode: string;
  status: string;
  answer_id: string;
  error: string;
};

// extraction_warning/parse_quality_warning/paper_meta_status 是
// anomaly-severity.ts::sourceAnomalies() 的入参字段——缺了它们,MinerU 降级、
// 部分内容未分析等异常小字在活动流的来源行上就出不来。
export type ActivitySource = {
  type: "source";
  id: string;
  notebook_id: string;
  created_at: string;
  display_title: string;
  file_name: string;
  source_type: string;
  parse_status: string;
  status: string;
  // ⚠ 刻意是布尔而不是诊断原文。`sources.error_message` 是原始异常串，可能带服务端
  // 绝对路径；管理员看别人的活动流与跨库代理读取是同一条披露边界，所以契约只给
  // 「解析失败了没有」这一位信息（与 ScopedSourceDetail 的 parse_failed 同形）。
  parse_failed: boolean;
  extraction_warning: string;
  parse_quality_warning: boolean;
  paper_meta_status: string;
};

// generation_started_at 为空串表示旧报告缺这个开始戳(红线:不得编造耗时)。
// 配 report-time.ts::formatReportTiming(status, created_at, updated_at,
// generation_started_at) 使用,不能用 updated_at - created_at 顶替——那会把
// 大纲确认的等待算进耗时。
export type ActivityReport = {
  type: "report";
  id: string;
  notebook_id: string;
  created_at: string;
  updated_at: string;
  question: string;
  depth: number;
  status: string;
  generation_started_at: string;
};

// 按 `type` 判别的活动流条目联合类型。三类各自数据来源不同(ask_jobs+answers /
// sources / reports),中栏活动流按时间归并展示。
export type ActivityItem = ActivityAsk | ActivitySource | ActivityReport;

export type ActivityResponse = {
  items: ActivityItem[];
  has_more: boolean;
  next_cursor: ActivityCursor | null;
};

// 右栏「选中提问」详情。trace/answer 的渲染交给既有 ask-intent-trace /
// 答案组件复用,这里刻意留 unknown,不重新声明它们的形状。
export type AskDetail = {
  job_id: string;
  notebook_id: string;
  conversation_id: string;
  question: string;
  mode: string;
  status: string;
  asked_at: string;
  answered_at: string;
  error: string;
  trace: unknown[];
  // `unknown` 已经包含 null,`unknown | null` 是多余的联合。
  answer: unknown;
};
