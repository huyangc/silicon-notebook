/**
 * report-view.tsx
 *
 * 「深度报告」tab:生成 / 列表 / 进度轮询 / 查看 / 取消 / 下载 .md。
 * page.tsx 已过大,面板逻辑集中在这里;page.tsx 只负责接线
 * (类型化 api 函数 + chat-body 里的 <ReportsPanel …/> 分支)。
 *
 * 轮询约定(镜像 page.tsx 的索引构建轮询写法):
 * - 列表视图:存在非终态(pending/running)报告时每 6s 刷一次列表,终态即停;
 * - 详情视图:打开的报告非终态时每 6s 刷一次详情,到终态后再同步一次列表;
 * - 组件卸载/依赖变化时清理 interval。
 */
"use client";

import { useEffect, useRef, useState } from "react";
import { ArrowDown, ArrowLeft, ArrowUp, Check, CheckSquare, ChevronRight, Copy, Download, Plus, Sparkles, Square, Trash2, X } from "lucide-react";
import ReactMarkdown, { defaultUrlTransform } from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import "katex/dist/katex.min.css";
import { normalizeMathMarkdown } from "./math-markdown";
import { remarkCitations } from "./answer-citations";
import { computeSourceTierCounts, referenceByAnchorKey, type AnswerReference } from "./answer-formatting";
import { logDiagnostic, toUserMessage } from "./errors";
import { EffortPicker, type EffortOption } from "./effort-picker";
import { quotedPhraseHint } from "./query-syntax";
import { formatReportCoverage, parseReportSubQueries, type ReportCoverage } from "./report-outline-model";
import { formatReportTiming } from "./report-time";
import { label } from "./vocabulary";

// ---------------------------------------------------------------------------
// 类型(与 backend/app/models/schemas.py 的 ReportSummary/ReportDetail 对齐)
// ---------------------------------------------------------------------------

export type ReportSummaryT = {
  id: string;
  question: string;
  // planning | outline_ready | generating(两阶段新增)| pending | running | done | failed | cancelled
  status: string;
  progress: string;
  section_count: number;
  created_at: string;
  generation_started_at?: string;
  updated_at?: string;
  created_by: string;
  depth?: number;
};

// 大纲富对象:STORM 预写作产物 + 充分性 Judge 判定;title/scope 用户可在编辑器改。
export type ReportSufficiency = "充足" | "薄弱" | "缺失";
export type ReportOutlineSectionT = {
  title: string;
  scope: string;
  sub_queries: string[];
  perspectives?: string[]; // 哪些视角挖出该节
  tensions?: string[]; // 与其他节/视角的张力(v1 纯文字)
  sufficiency?: ReportSufficiency; // 语料充分性:充足/薄弱/缺失
  gap_note?: string; // 缺口一句话说明
  action?: string; // keep | supplement | external
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
  /** 检索口径由共享问题理解合同给出；旧报告没有这两个字段。 */
  result_scope?: "ranked" | "complete" | "aggregate" | "hybrid";
  completeness_required?: boolean;
  corpus_profile?: ReportCorpusProfileT;
  report_frame?: ReportFrameT;
  credibility?: ReportCredibilityT;
};

export type ReportDistributionT = {
  label?: string;
  name?: string;
  value?: string | number;
  type?: string;
  year?: string | number;
  count?: number;
};

/** 报告所依据资料的可见摘要。所有字段可选，以兼容历史报告和 fail-open 后端。 */
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

/** 仅在比较/分类报告中出现；缺失时按历史大纲流程退化。 */
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
  /** 全篇综合的执行结果；旧报告缺失时不渲染状态。 */
  synthesis_status?: "not_requested" | "available" | "skipped_no_evidence" | "failed_model" | "failed_validation";
  /** 有可用主张账本的章节数及本次报告总章节数。 */
  claim_ledgers_available?: number;
  claim_ledgers_total?: number;
};

export type ReportCitationAuditT = {
  support_rate?: number;
  supported_claims?: number;
  total_claims?: number;
  high_risk_uncited_count?: number;
  /** 当前确定性审计的命名；保留上面的 UI 别名以兼容后续演进。 */
  unsupported?: number;
  high_risk_assertions?: number;
};

export type ReportDetailT = ReportSummaryT & {
  outline: ReportOutlineSectionT[];
  sections: {
    title: string;
    markdown: string;
    grounded: boolean;
    evidence_level?: string;
    failed?: boolean;
    citation_audit?: ReportCitationAuditT;
  }[];
  section_status?: { title: string; phase: string; step: number }[];
  gaps: string[];
  content_md: string;
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
    /** 同一可区分资料可能有多个锚点；仅用于统计，不改引用跳转。 */
    family_key?: string;
  }[];
  understanding: ReportUnderstandingT;
  error: string;
};

// 非终态判定:轮询用。两阶段里 planning/generating 是活跃阶段,与 pending/running 同样需要轮询;
// outline_ready 是稳定的「等用户确认」态,不轮询(用户编辑大纲期间不该被刷新覆盖)。
const isReportActive = (status: string) =>
  status === "pending" || status === "running" || status === "planning" || status === "generating";

// 研究深度:五档命名,index 0→4 一一对应 DEPTHS(每节 reflect 步上限)。
// 各档都算深入,区别在充分程度;后端 create_report 会 clamp 到 [1,16]。
const DEPTHS = [1, 2, 4, 8, 16];
const DEPTH_LABELS = ["概览", "标准", "深入", "详尽", "穷尽"];
// 每档一句中性说明(不用快/聪明措辞),popover 里给选中档显示。
const DEPTH_HINTS = [
  "最快出稿，覆盖主干要点",
  "常用档，深度与篇幅平衡",
  "逐节多轮检索，论证更完整",
  "更广取证，细节与边角更全",
  "最充分深挖，覆盖尽可能全面",
];
// 喂给共享档位控件(EffortPicker)的档位表。id 用档位下标:DEPTHS 的取值本身可能重排,
// 下标才是这里与 depthIdx 之间稳定的那一环。
const DEPTH_OPTIONS: readonly EffortOption[] = DEPTH_LABELS.map((label, index) => ({
  id: String(index),
  label,
  hint: DEPTH_HINTS[index],
}));

// 节内进度:phase → 图标类型。完成=对勾,失败=叹号,其余进行中=点动画。
type SectionPhaseIcon = "done" | "failed" | "active";
const sectionPhaseIcon = (phase: string): SectionPhaseIcon =>
  phase === "完成" ? "done" : phase === "失败" ? "failed" : "active";

const STATUS_LABELS: Record<string, string> = {
  pending: "排队中",
  planning: "规划中",
  intent_ready: "待确认问题",
  outline_ready: "待确认",
  running: "生成中",
  generating: "生成中",
  done: "完成",
  failed: "失败",
  cancelled: "已取消",
};

// 计划指定的导出方式:Blob → 临时 URL → 触发下载。
function downloadMd(r: ReportDetailT) {
  const blob = new Blob([r.content_md], { type: "text/markdown" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `report-${r.id}.md`;
  a.click();
  URL.revokeObjectURL(url);
}

// 复制正文到剪贴板:优先 navigator.clipboard,回退到隐藏 textarea + execCommand。
async function copyReportContent(text: string): Promise<void> {
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

// ---------------------------------------------------------------------------
// ReportMarkdown:复用 answer-markdown.tsx 的引用基建。
// 正文里的 [k\d+] 现为全局按来源去重编号,后端保证每个内联 marker 都有对应
// reference;由 references 构造 refsByKey → remarkCitations 把 [k] 转成可点击
// cite-chip,点击高亮并滚动到「参考文献」段(h2 覆盖挂 id=report-references)。
// ---------------------------------------------------------------------------

/**
 * 报告和 Ask 都必须按「用户实际可见的引用」统计来源层级。这里仅做 wire
 * shape 适配，计数始终委托给 answer-formatting 的唯一实现。
 */
function reportReferencesAsAnswerReferences(
  references: ReportDetailT["references"],
): AnswerReference[] {
  return references.map((reference, index) => ({
    id: `report:${reference.key}`,
    displayLabel: `[${index + 1}]`,
    anchor: {
      key: reference.key,
      object_id: reference.object_id || "",
      object_type: reference.object_type || "",
      label: reference.label,
      name: reference.name,
      source_title: reference.source_title,
      source_file_name: reference.source_file_name,
      location_label: reference.location_label,
      snippet: reference.snippet,
      tier: reference.tier,
    },
  }));
}

export function ReportMarkdown({
  markdown,
  references = [],
}: {
  markdown: string;
  references?: ReportDetailT["references"];
}) {
  const [selectedRefKey, setSelectedRefKey] = useState<string | null>(null);
  const refObjs = reportReferencesAsAnswerReferences(references);
  const refsByKey = referenceByAnchorKey(refObjs);
  const selectedReference = selectedRefKey ? refsByKey[selectedRefKey]?.anchor : undefined;
  const components = {
    a({ href, children }: { href?: string; children?: React.ReactNode }) {
      if (href?.startsWith("cite:")) {
        const key = href.slice(5);
        if (refsByKey[key]) {
          return (
            <button
              type="button"
              className={`cite-chip${selectedRefKey === key ? " active" : ""}`}
              onClick={() => {
                setSelectedRefKey(key);
                document
                  .getElementById("report-references")
                  ?.scrollIntoView({ behavior: "smooth", block: "start" });
              }}
            >
              {children}
            </button>
          );
        }
        return <span>{children}</span>;
      }
      return (
        <a href={href} target="_blank" rel="noreferrer">
          {children}
        </a>
      );
    },
    h2({ children }: { children?: React.ReactNode }) {
      const text = Array.isArray(children) ? children.join("") : String(children ?? "");
      return <h2 id={text.includes("参考文献") ? "report-references" : undefined}>{children}</h2>;
    },
    // 代码块/表格沿用问答区现有样式 class,保持全站观感一致。
    pre({ children }: { children?: React.ReactNode }) {
      return <pre className="answer-code">{children}</pre>;
    },
    table({ children }: { children?: React.ReactNode }) {
      return (
        <div className="answer-table-wrap">
          <table className="answer-table">{children}</table>
        </div>
      );
    },
  } as Parameters<typeof ReactMarkdown>[0]["components"];
  return (
    <div className="report-markdown answer-markdown">
      <ReactMarkdown
        remarkPlugins={[
          remarkGfm,
          remarkMath,
          [remarkCitations, refsByKey] as [typeof remarkCitations, Record<string, AnswerReference>],
        ]}
        rehypePlugins={[rehypeKatex]}
        // 默认 urlTransform 会清掉 cite: 协议 → 徽章 href 丢失;放行 cite:,
        // 其余仍走默认清洗(防 javascript: 等不安全协议)。
        urlTransform={(url) => (url.startsWith("cite:") ? url : defaultUrlTransform(url))}
        components={components}
      >
        {normalizeMathMarkdown(markdown)}
      </ReactMarkdown>
      {selectedReference && (
        <aside className="report-reference-detail" aria-label="引用原文">
          <strong>{selectedReference.source_title || selectedReference.label}</strong>
          {selectedReference.location_label && <span>{selectedReference.location_label}</span>}
          {selectedReference.source_file_name
            && selectedReference.source_file_name !== selectedReference.source_title && (
              <small title={selectedReference.source_file_name}>
                原始文件：{selectedReference.source_file_name}
              </small>
            )}
          {selectedReference.snippet && <blockquote>{selectedReference.snippet}</blockquote>}
        </aside>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// 状态徽章:pending/running 亮色(running 附 progress 文字),终态沉色。
// ---------------------------------------------------------------------------

function ReportStatusBadge({ status, progress }: { status: string; progress: string }) {
  const live = isReportActive(status);
  return (
    <span className={`report-status ${status}`} title={progress || undefined}>
      {live && <span className="report-status-dot" aria-hidden />}
      <span className="report-status-label">{label(STATUS_LABELS, status, "处理中")}</span>
      {live && progress && <span className="report-status-progress">{progress}</span>}
    </span>
  );
}

// ---------------------------------------------------------------------------
// 问题理解确认(status==='intent_ready'):未确认前不接触语料或规划大纲。
// ---------------------------------------------------------------------------

const INTENT_TYPE_LABELS: Record<string, string> = {
  explain: "解释机理",
  compare: "比较分析",
  diagnose: "诊断问题",
  design: "设计方案",
  review: "综述评估",
  other: "综合研究",
};

export function IntentReview({
  report,
  notebookId,
  confirmReportIntent,
  onPlanning,
  setToast,
  readOnly,
}: {
  report: ReportDetailT;
  notebookId: string;
  confirmReportIntent: (
    nb: string,
    rid: string,
    payload: { resolved_question: string; answers: { id: string; answer: string }[] },
  ) => Promise<{ status: string }>;
  onPlanning: (detail: ReportDetailT) => void;
  setToast: (message: string) => void;
  readOnly: boolean;
}) {
  const understanding = report.understanding || {};
  const ambiguities = understanding.ambiguities || [];
  const [resolvedQuestion, setResolvedQuestion] = useState(
    understanding.resolved_question || report.question,
  );
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    setResolvedQuestion(understanding.resolved_question || report.question);
    setAnswers({});
  }, [report.id, report.question, understanding.resolved_question]);

  const missingRequired = ambiguities.some(
    (item) => item.required !== false && !(answers[item.id] || "").trim(),
  );
  const confidence = typeof understanding.confidence === "number"
    ? Math.round(Math.max(0, Math.min(1, understanding.confidence)) * 100)
    : null;

  async function confirm() {
    if (busy || readOnly) return;
    if (!resolvedQuestion.trim()) {
      setToast("请先填写确认后的研究问题");
      return;
    }
    if (missingRequired) {
      setToast("请先回答所有必填澄清问题");
      return;
    }
    setBusy(true);
    try {
      await confirmReportIntent(notebookId, report.id, {
        resolved_question: resolvedQuestion.trim(),
        answers: ambiguities
          .map((item) => ({ id: item.id, answer: (answers[item.id] || "").trim() }))
          .filter((item) => item.answer),
      });
      setToast("问题理解已确认，开始检查语料并规划大纲");
      onPlanning({ ...report, status: "planning", progress: "按已确认问题规划中" });
    } catch (error) {
      setToast(toUserMessage(error, "问题确认没能提交，请稍后重试"));
    } finally {
      setBusy(false);
    }
  }

  const answered = (id: string) => Boolean((answers[id] || "").trim());
  const pending = ambiguities.filter(
    (item) => item.required !== false && !answered(item.id),
  ).length;
  // 与逐步推理那张卡共用 .intent-card 外观:同一个「签核系统的理解」交互,
  // 不该在两个页面长成两个样子。内容口径各自保留(这边是研究问题/规划)。
  const detailGroups = [
    ["必须回答", (understanding.mandatory_topics || []).map((item) => item.question)],
    ["研究对象", understanding.entities || []],
    ["比较维度", understanding.comparison_axes || []],
    ["约束条件", understanding.constraints || []],
    ["不纳入范围", understanding.excluded_topics || []],
    ["成立前提", understanding.assumptions || []],
    ["期望输出", understanding.expected_output ? [understanding.expected_output] : []],
  ] as const;
  const asChips = new Set(["研究对象", "比较维度", "不纳入范围"]);

  return (
    <div className="intent-card">
      <div className="intent-card-head">
        <div>
          <h3>{understanding.needs_clarification ? "补充问题信息" : "确认问题理解"}</h3>
          <p>这一步只理解你的问题，不读取语料。确认后才会检索并规划大纲。</p>
        </div>
        {!readOnly && (
          <div className="intent-card-actions">
            <button className="button" type="button" disabled={busy || missingRequired} onClick={() => void confirm()}>
              <Check size={15} />
              {busy ? (
                <span>提交中…</span>
              ) : ambiguities.length > 0 ? (
                <span>提交补充并开始规划</span>
              ) : (
                <span>确认理解并开始规划</span>
              )}
            </button>
          </div>
        )}
      </div>

      {/* 只读成员照样看得到问题与澄清项(禁用即可) —— 把整个区换成一句等待提示,
          等于让协作者看不到所有者正在被问什么。 */}
      <div className="intent-card-zone">
        <p className="intent-card-eyebrow">
          {readOnly ? "等待所有者确认" : "待你确认"}
          {!readOnly && pending > 0 && <em className="todo">还差 {pending} 项</em>}
          {!readOnly && pending === 0 && ambiguities.length > 0 && (
            <em className="done">已补齐</em>
          )}
        </p>
        {readOnly && (
          <p className="intent-card-readonly">该报告正在等待所有者确认问题理解。</p>
        )}

          <label className="intent-card-question">
            <span>确认后的研究问题</span>
            <textarea
              rows={2}
              value={resolvedQuestion}
              disabled={busy || readOnly}
              onChange={(event) => setResolvedQuestion(event.target.value)}
            />
          </label>

          <div className="intent-card-asks">
            {ambiguities.map((item, index) => (
              <div
                className={`intent-card-ask${answered(item.id) ? " answered" : ""}`}
                key={item.id}
              >
                <span className="intent-card-mark" aria-hidden="true">
                  {answered(item.id) ? <Check size={12} /> : index + 1}
                </span>
                <span className="intent-card-ask-question">
                  {item.question}
                  {item.required !== false && <em>必填</em>}
                </span>
                {item.reason && <small>{item.reason}</small>}
                {item.options && item.options.length > 0 && (
                  <span className="intent-card-options">
                    {item.options.map((option) => (
                      <button
                        type="button"
                        key={option}
                        disabled={busy || readOnly}
                        aria-pressed={(answers[item.id] || "") === option}
                        className={(answers[item.id] || "") === option ? "selected" : ""}
                        onClick={() => setAnswers((current) => ({ ...current, [item.id]: option }))}
                      >
                        {option}
                      </button>
                    ))}
                  </span>
                )}
                <textarea
                  aria-label={`${item.question}的补充答案`}
                  rows={2}
                  value={answers[item.id] || ""}
                  disabled={busy || readOnly}
                  placeholder="补充你的答案"
                  onChange={(event) => setAnswers((current) => ({
                    ...current,
                    [item.id]: event.target.value,
                  }))}
                />
              </div>
            ))}
        </div>
      </div>

      <details className="intent-card-readout" open>
        <summary>
          <ChevronRight size={14} />
          系统的理解
          <span className="intent-card-tags">
            <span>{INTENT_TYPE_LABELS[understanding.intent_type || ""] || "综合研究"}</span>
            {confidence !== null && <span>置信度 {confidence}%</span>}
          </span>
        </summary>
        <dl className="intent-card-rows">
          {detailGroups.map(([title, values]) => values.length > 0 && (
            <div className="intent-card-row" key={title}>
              <dt>{title}</dt>
              <dd>
                {asChips.has(title) ? (
                  <span className="chips">
                    {values.map((value) => <span key={value}>{value}</span>)}
                  </span>
                ) : values.length === 1 ? values[0] : (
                  <ul>{values.map((value) => <li key={value}>{value}</li>)}</ul>
                )}
              </dd>
            </div>
          ))}
        </dl>
        {(understanding.assumptions || []).length > 0 && !readOnly && (
          <p className="intent-card-note">
            假设不合适的话，直接改上面的研究问题，或在澄清答案里说明。
          </p>
        )}
      </details>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 大纲编辑器(status==='outline_ready'):STORM 富大纲 → 用户可编辑 → 确认后生成。
// 每节一张卡片:title/scope 受控输入、上移/下移/删节、顶部新增;徽章行展示
// 用户必答问题 / 检索方向 / coverage / perspectives / sufficiency / tensions。
// ---------------------------------------------------------------------------

const SUFFICIENCY_META: Record<ReportSufficiency, { label: string; cls: string }> = {
  充足: { label: "证据充足", cls: "ok" },
  薄弱: { label: "证据薄弱", cls: "weak" },
  缺失: { label: "证据缺失", cls: "missing" },
};

const trimmedLines = (value: string) => value.split("\n").map((item) => item.trim()).filter(Boolean);

function asDistributionRows(
  value: ReportDistributionT[] | Record<string, number> | undefined,
): { label: string; count: number }[] {
  if (Array.isArray(value)) {
    return value.flatMap((row) => {
      const label = String(row.label ?? row.name ?? row.value ?? row.type ?? row.year ?? "").trim();
      const count = Number(row.count ?? 0);
      return label && Number.isFinite(count) ? [{ label, count }] : [];
    });
  }
  if (value && typeof value === "object") {
    return Object.entries(value).flatMap(([label, count]) =>
      Number.isFinite(Number(count)) ? [{ label, count: Number(count) }] : [],
    );
  }
  return [];
}

function formatDistribution(
  value: ReportDistributionT[] | Record<string, number> | undefined,
  max = 4,
): string {
  const rows = asDistributionRows(value);
  const shown = rows.slice(0, max).map((row) => `${row.label} ${row.count}`);
  return rows.length > max ? `${shown.join(" · ")} · 其余 ${rows.length - max} 类` : shown.join(" · ");
}

function percent(value: number | undefined): string | null {
  if (typeof value !== "number" || !Number.isFinite(value)) return null;
  const normalized = value <= 1 ? value * 100 : value;
  return `${Math.round(Math.max(0, Math.min(100, normalized)))}%`;
}

function citationSummary(report: ReportDetailT): {
  independent: number | null;
  top1: string | null;
} {
  const credibility = report.understanding?.credibility || {};
  const declared = credibility.independent_documents
    ?? credibility.independent_source_families
    ?? credibility.independent_sources;
  const familyCounts = new Map<string, number>();
  for (const reference of report.references || []) {
    // Never use the number of citation anchors as a source count. A row without
    // a stable source/family identity is deliberately excluded from this metric.
    const key = String(reference.family_key || reference.source_id || "").trim();
    if (key) familyCounts.set(key, (familyCounts.get(key) || 0) + 1);
  }
  const independent = typeof declared === "number" && Number.isFinite(declared)
    ? declared
    : familyCounts.size > 0 ? familyCounts.size : null;
  const top1 = percent(credibility.top1_share ?? credibility.top1_concentration)
    ?? (familyCounts.size > 0
      ? percent(Math.max(...familyCounts.values()) / Math.max(1, [...familyCounts.values()].reduce((a, b) => a + b, 0)))
      : null);
  return { independent, top1 };
}

const SYNTHESIS_STATUS_COPY: Record<NonNullable<ReportCredibilityT["synthesis_status"]>, string> = {
  not_requested: "本次报告未请求全篇综合。",
  available: "全篇综合已完成，已用于检查跨章节的一致性。",
  skipped_no_evidence: "已跳过全篇综合：可用资料不足，正文保持逐节结果。",
  failed_model: "全篇综合未完成：模型调用失败，正文未被改写。",
  failed_validation: "全篇综合未完成：返回结果未通过校验，正文未被改写。",
};

/** 报告生成的可见可信度回执；缺失字段的历史报告保持静默。 */
export function ReportCredibilitySummary({ report }: { report: ReportDetailT }) {
  const credibility = report.understanding?.credibility;
  if (!credibility) return null;
  const synthesisStatus = credibility.synthesis_status;
  const ledgersAvailable = credibility.claim_ledgers_available;
  const ledgersTotal = credibility.claim_ledgers_total;
  const showLedgers = typeof ledgersAvailable === "number" || typeof ledgersTotal === "number";
  if (!synthesisStatus && !showLedgers) return null;
  const statusClass = synthesisStatus === "failed_model" || synthesisStatus === "failed_validation"
    ? "failed"
    : synthesisStatus === "skipped_no_evidence"
      ? "skipped"
      : "ok";
  return (
    <section className="report-credibility-summary" aria-label="报告可信度回执">
      {synthesisStatus && (
        <p className={`report-synthesis-status ${statusClass}`}>
          <strong>全篇综合：</strong>{SYNTHESIS_STATUS_COPY[synthesisStatus]}
        </p>
      )}
      {showLedgers && (
        <p className="report-claim-ledgers">
          主张账本：{typeof ledgersAvailable === "number" ? ledgersAvailable : 0}
          {typeof ledgersTotal === "number" ? `/${ledgersTotal}` : ""} 节可用
        </p>
      )}
    </section>
  );
}

/** 资料集中度与个人/公共来源口径共用 Ask 的显示引用统计。 */
export function ReportCitationDistribution({ report }: { report: ReportDetailT }) {
  const summary = citationSummary(report);
  const { personal, base } = computeSourceTierCounts(
    reportReferencesAsAnswerReferences(report.references || []),
  );
  if (summary.independent == null && !summary.top1 && personal + base === 0) return null;
  return (
    <div className="report-source-dist" title="按可区分资料统计，不以引用标记数量代替资料数量">
      {summary.independent != null && <>可区分资料 {summary.independent}</>}
      {summary.top1 && <><span aria-hidden> · </span>最集中资料占 {summary.top1}</>}
      {personal + base > 0 && (
        <span className="tag source-dist" title="本次引用的来源分布（个人知识库 / 公共知识库）">
          来源 · 个人 {personal}
          {base > 0 && <> · <strong className="source-dist-base">公共 {base}</strong></>}
        </span>
      )}
    </div>
  );
}

function citationAuditLabel(audit: ReportCitationAuditT | undefined): string | null {
  if (!audit) return null;
  const fromRate = percent(audit.support_rate);
  if (fromRate) return `引证覆盖率 ${fromRate}`;
  if (typeof audit.supported_claims === "number" && typeof audit.total_claims === "number" && audit.total_claims > 0) {
    return `引证覆盖率 ${Math.round(audit.supported_claims / audit.total_claims * 100)}%`;
  }
  return null;
}

export function ReportCorpusBasis({ report }: { report: ReportDetailT }) {
  const profile = report.understanding?.corpus_profile;
  const scope = report.understanding?.result_scope;
  const disclosure = profile?.completeness_disclosure
    || ((scope === "complete" || scope === "aggregate" || scope === "hybrid")
      ? "本报告按相关性检索生成，未做完整枚举。"
      : "");
  if (!profile && !disclosure) return null;
  const typeText = formatDistribution(profile?.type_distribution);
  const yearText = formatDistribution(profile?.year_distribution);
  const representativeTitles = (profile?.representatives || [])
    .map((item) => String(item.title || item.label || item.source_title || "").trim())
    .filter(Boolean);
  const duplicateLowerBound = profile?.identified_duplicate_lower_bound
    ?? profile?.duplicate_inflation;
  return (
    <section className="report-corpus-basis" aria-label="资料基础">
      <div className="report-corpus-basis-head">
        <h3>资料基础</h3>
        {typeof profile?.total_sources === "number" && (
          <span>{profile.total_sources} 份资料</span>
        )}
      </div>
      {profile && (
        <div className="report-corpus-basis-facts">
          {typeof (profile.independent_documents ?? profile.independent_families) === "number" && (
            <span>可区分资料 {profile.independent_documents ?? profile.independent_families}</span>
          )}
          {typeof (profile.displayed_sources ?? profile.representative_count) === "number" && typeof profile.total_sources === "number" && (
            <span>代表资料 {profile.displayed_sources ?? profile.representative_count}/{profile.total_sources}</span>
          )}
          {typeof profile.identity_uncertain_sources === "number" && profile.identity_uncertain_sources > 0 && (
            <span>另有 {profile.identity_uncertain_sources} 份资料无法可靠合并</span>
          )}
          {typeof duplicateLowerBound === "number" && duplicateLowerBound > 0 && (
            <span>保守识别重复至少 {duplicateLowerBound} 份</span>
          )}
          {percent(profile.metadata_coverage) && (
            <span>资料识别信息完整度 {percent(profile.metadata_coverage)}</span>
          )}
        </div>
      )}
      {(typeText || yearText) && (
        <dl className="report-corpus-basis-distributions">
          {typeText && <><dt>资料类型</dt><dd>{typeText}</dd></>}
          {yearText && <><dt>时间分布</dt><dd>{yearText}{typeof profile?.unknown_year === "number" ? ` · 年份未知 ${profile.unknown_year}` : ""}</dd></>}
        </dl>
      )}
      {representativeTitles.length > 0 && (
        <p className="report-corpus-basis-representatives">代表资料：{representativeTitles.join("；")}</p>
      )}
      {disclosure && <p className="report-corpus-basis-disclosure">{disclosure}</p>}
    </section>
  );
}

export function ReportFrameEditor({
  frame,
  disabled,
  onChange,
}: {
  frame: ReportFrameT | undefined;
  disabled: boolean;
  onChange: (next: ReportFrameT | undefined) => void;
}) {
  if (!frame) return null;
  const facets = frame.facets || [];
  const axes = frame.axes || [];
  const patch = (part: Partial<ReportFrameT>) => onChange({ ...frame, ...part });
  return (
    <section className="report-frame-editor" aria-label="分析框架">
      <div>
        <h4>分析框架</h4>
        <p>用于统一分类和比较口径；确认后会随大纲一同保存。</p>
      </div>
      <label>
        <span>对象类型</span>
        <input value={frame.subject_kind || ""} disabled={disabled}
          onChange={(event) => patch({ subject_kind: event.target.value })} />
      </label>
      <label>
        <span>实例使用方式</span>
        <input value={frame.instance_policy || ""} disabled={disabled}
          onChange={(event) => patch({ instance_policy: event.target.value })} />
      </label>
      {facets.map((facet, index) => (
        <fieldset key={facet.id || `facet-${index}`}>
          <legend>分类维度</legend>
          <button
            type="button"
            className="report-frame-remove"
            title="删除此分类维度"
            aria-label={`删除分类维度：${facet.name || index + 1}`}
            disabled={disabled}
            onClick={() => patch({ facets: facets.filter((_, itemIndex) => itemIndex !== index) })}
          >
            <X size={14} aria-hidden="true" /> 删除
          </button>
          <label>
            <span>名称</span>
            <input value={facet.name || ""} disabled={disabled} onChange={(event) => {
              const next = facets.slice();
              next[index] = { ...facet, name: event.target.value };
              patch({ facets: next });
            }} />
          </label>
          <label>
            <span>可选值（每行一项）</span>
            <textarea value={(facet.values || []).join("\n")} disabled={disabled} rows={Math.max(2, Math.min(5, facet.values?.length || 2))}
              onChange={(event) => {
                const next = facets.slice();
                next[index] = { ...facet, values: trimmedLines(event.target.value) };
                patch({ facets: next });
              }} />
          </label>
          <label className="report-frame-check">
            <input type="checkbox" checked={facet.exclusive === true} disabled={disabled} onChange={(event) => {
              const next = facets.slice();
              next[index] = { ...facet, exclusive: event.target.checked };
              patch({ facets: next });
            }} />
            <span>同一实例只能归入一个值</span>
          </label>
        </fieldset>
      ))}
      {axes.map((axis, index) => (
        <fieldset key={axis.id || `axis-${index}`}>
          <legend>比较条件</legend>
          <button
            type="button"
            className="report-frame-remove"
            title="删除此比较条件"
            aria-label={`删除比较条件：${axis.name || index + 1}`}
            disabled={disabled}
            onClick={() => patch({ axes: axes.filter((_, itemIndex) => itemIndex !== index) })}
          >
            <X size={14} aria-hidden="true" /> 删除
          </button>
          <label>
            <span>名称</span>
            <input value={axis.name || ""} disabled={disabled} onChange={(event) => {
              const next = axes.slice();
              next[index] = { ...axis, name: event.target.value };
              patch({ axes: next });
            }} />
          </label>
          <label>
            <span>适用条件（每行一项）</span>
            <textarea value={(axis.condition_fields || []).join("\n")} disabled={disabled} rows={Math.max(2, Math.min(5, axis.condition_fields?.length || 2))}
              onChange={(event) => {
                const next = axes.slice();
                next[index] = { ...axis, condition_fields: trimmedLines(event.target.value) };
                patch({ axes: next });
              }} />
          </label>
        </fieldset>
      ))}
    </section>
  );
}

// 后端富字段编辑期原样透传;title/scope/sub_queries 可改,也可增删排序。
type EditSection = ReportOutlineSectionT & { _key: string };
let _outlineKeySeq = 0;
const freshOutlineKey = () => `sec-${Date.now().toString(36)}-${(_outlineKeySeq++).toString(36)}`;
const toEditSections = (outline: ReportOutlineSectionT[]): EditSection[] =>
  outline.map((s) => ({ ...s, _key: freshOutlineKey() }));

export function OutlineEditor({
  report,
  notebookId,
  updateReportOutline,
  generateReport,
  onGenerating,
  setToast,
}: {
  report: ReportDetailT;
  notebookId: string;
  updateReportOutline: (
    nb: string,
    rid: string,
    payload: { sections: unknown[]; frame?: ReportFrameT },
  ) => Promise<{ status: string; sections: number }>;
  generateReport: (nb: string, rid: string, depth?: number) => Promise<{ status: string }>;
  onGenerating: (detail: ReportDetailT) => void;
  setToast: (message: string) => void;
}) {
  // 本地可编辑副本;仅当报告 id 变化时重新播种(避免打字被父层 state 覆盖)。
  const [sections, setSections] = useState<EditSection[]>(() => toEditSections(report.outline));
  const [frame, setFrame] = useState<ReportFrameT | undefined>(() => report.understanding?.report_frame);
  const seededId = useRef(report.id);
  useEffect(() => {
    if (seededId.current !== report.id) {
      seededId.current = report.id;
      setSections(toEditSections(report.outline));
      setFrame(report.understanding?.report_frame);
    }
  }, [report.id, report.outline, report.understanding?.report_frame]);

  const [busy, setBusy] = useState(false);

  const patchSection = (key: string, patch: Partial<EditSection>) =>
    setSections((prev) => prev.map((s) => (s._key === key ? { ...s, ...patch } : s)));
  const removeSection = (key: string) =>
    setSections((prev) => prev.filter((s) => s._key !== key));
  const moveSection = (index: number, dir: -1 | 1) =>
    setSections((prev) => {
      const next = index + dir;
      if (next < 0 || next >= prev.length) return prev;
      const copy = prev.slice();
      [copy[index], copy[next]] = [copy[next], copy[index]];
      return copy;
    });
  const addSection = () =>
    setSections((prev) => [
      ...prev,
      { _key: freshOutlineKey(), title: "", scope: "", sub_queries: [] },
    ]);

  // 有效节 = 标题非空;后端要求 ≥1 有效节且每节带 sub_queries。新增的空节会带上占位
  // sub_query(用标题),保证 PATCH 校验通过并让生成阶段有检索种子。
  const validCount = sections.filter((s) => s.title.trim()).length;
  const intentBindingCounts = new Map<string, number>();
  for (const section of sections) {
    for (const intentId of section.intent_ids || []) {
      intentBindingCounts.set(intentId, (intentBindingCounts.get(intentId) || 0) + 1);
    }
  }

  async function confirmGenerate() {
    if (busy) return;
    const cleaned = sections
      .filter((s) => s.title.trim())
      .map(({ _key, ...s }) => {
        void _key;
        const subs = (s.sub_queries || []).map((q) => q.trim()).filter(Boolean);
        return { ...s, title: s.title.trim(), scope: (s.scope || "").trim(),
                 sub_queries: subs.length > 0 ? subs : [s.title.trim()] };
      });
    if (cleaned.length === 0) {
      setToast("请至少保留一个有标题的章节");
      return;
    }
    setBusy(true);
    try {
      await updateReportOutline(notebookId, report.id, { sections: cleaned, frame });
      await generateReport(notebookId, report.id);
      setToast("已确认大纲，开始生成完整报告");
      // 乐观切到生成态,让父层立刻进 section_status 进度视图并恢复轮询。
      onGenerating({ ...report, status: "generating", progress: "章节 0/" + cleaned.length + " 完成" });
    } catch (error) {
      // 同 surfaceError:fetch 层的译文(没权限 / 已删除 / 冲突)比「可以重试」
      // 有用,别压平;非 HTTP 异常才退兜底。原始值的诊断由 toUserMessage 统一
      // 记(见下方 surfaceError 的注释:这里不再补裸 console.error)。
      setToast(toUserMessage(error, "报告没能生成完，可以重试"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="report-outline-editor">
      <div className="report-outline-editor-head">
        <div>
          <h3>确认研究大纲</h3>
          <p>已先锁定用户问题，再按语料覆盖规划出 {sections.length} 个章节。可核对每节必答问题，修改标题、范围与检索方向后生成。</p>
        </div>
        <button className="report-action" type="button" onClick={addSection} disabled={busy}>
          <Plus size={14} /> 新增章节
        </button>
      </div>

      <ReportFrameEditor frame={frame} disabled={busy} onChange={setFrame} />

      {sections.length === 0 ? (
        <div className="report-outline-empty">大纲为空,点「新增章节」添加,或返回列表重新规划。</div>
      ) : (
        <ol className="report-outline-cards">
          {sections.map((s, index) => {
            const suf = s.sufficiency ? SUFFICIENCY_META[s.sufficiency] : null;
            const protectsOnlyIntentBinding = (s.intent_ids || []).some(
              (intentId) => intentBindingCounts.get(intentId) === 1,
            );
            return (
              <li className="report-outline-card" key={s._key}>
                <div className="report-outline-card-top">
                  <span className="report-outline-card-index">{index + 1}</span>
                  <input
                    className="report-outline-card-title"
                    type="text"
                    value={s.title}
                    placeholder="章节标题"
                    disabled={busy}
                    onChange={(e) => patchSection(s._key, { title: e.target.value })}
                  />
                  <div className="report-outline-card-ops">
                    <button
                      type="button"
                      className="report-outline-op"
                      title="上移"
                      aria-label="上移"
                      disabled={busy || index === 0}
                      onClick={() => moveSection(index, -1)}
                    >
                      <ArrowUp size={14} />
                    </button>
                    <button
                      type="button"
                      className="report-outline-op"
                      title="下移"
                      aria-label="下移"
                      disabled={busy || index === sections.length - 1}
                      onClick={() => moveSection(index, 1)}
                    >
                      <ArrowDown size={14} />
                    </button>
                    <button
                      type="button"
                      className="report-outline-op danger"
                      title={protectsOnlyIntentBinding ? "必答主题至少需要保留一节" : "删除本节"}
                      aria-label={protectsOnlyIntentBinding ? "本节含唯一必答主题，不能删除" : "删除本节"}
                      disabled={busy || protectsOnlyIntentBinding}
                      onClick={() => removeSection(s._key)}
                    >
                      <X size={14} />
                    </button>
                  </div>
                </div>
                <input
                  className="report-outline-card-scope"
                  type="text"
                  value={s.scope || ""}
                  placeholder="本节范围(一句话)"
                  disabled={busy}
                  onChange={(e) => patchSection(s._key, { scope: e.target.value })}
                />
                {s.intent_questions && s.intent_questions.length > 0 && (
                  <div className="report-intent-binding">
                    <span>本节必须回答</span>
                    <ul>
                      {s.intent_questions.map((item, i) => (
                        <li key={`${s._key}-intent-${i}`}>{item}</li>
                      ))}
                    </ul>
                  </div>
                )}
                <label className="report-outline-query-field">
                  <span>检索方向（每行一条）</span>
                  <textarea
                    value={(s.sub_queries || []).join("\n")}
                    rows={Math.max(2, Math.min(4, (s.sub_queries || []).length || 2))}
                    placeholder="输入本节需要执行的检索方向"
                    disabled={busy}
                    onChange={(e) => patchSection(s._key, { sub_queries: parseReportSubQueries(e.target.value) })}
                  />
                </label>
                {(suf || s.coverage || (s.perspectives && s.perspectives.length > 0)) && (
                  <div className="report-outline-badges">
                    {suf && (
                      <span className={`report-suf ${suf.cls}`}>
                        {suf.label}
                        {s.gap_note ? ` · ${s.gap_note}` : ""}
                      </span>
                    )}
                    {s.coverage && (
                      <span className="report-coverage" title="当前检索方向的客观命中数">
                        {formatReportCoverage(s.coverage)}
                      </span>
                    )}
                    {(s.perspectives || []).map((p, i) => (
                      <span className="report-perspective" key={`${s._key}-p-${i}`}>{p}</span>
                    ))}
                  </div>
                )}
                {s.tensions && s.tensions.length > 0 && (
                  <ul className="report-tensions">
                    {s.tensions.map((t, i) => (
                      <li key={`${s._key}-t-${i}`}>⚡ {t}</li>
                    ))}
                  </ul>
                )}
              </li>
            );
          })}
        </ol>
      )}

      <div className="report-outline-editor-foot">
        <span className="report-outline-editor-count">
          {validCount > 0 ? `${validCount} 个有效章节` : "至少保留一个有标题的章节"}
        </span>
        <button
          className="button"
          type="button"
          disabled={busy || validCount === 0}
          onClick={() => void confirmGenerate()}
        >
          <Sparkles size={15} /> {busy ? "提交中…" : "生成完整报告"}
        </button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// ReportsPanel
// ---------------------------------------------------------------------------

export interface ReportsPanelProps {
  notebookId: string;
  listReports: (nb: string) => Promise<ReportSummaryT[]>;
  getReport: (nb: string, rid: string) => Promise<ReportDetailT>;
  createReport: (nb: string, question: string, depth: number) => Promise<{ report_id: string }>;
  confirmReportIntent: (
    nb: string,
    rid: string,
    payload: { resolved_question: string; answers: { id: string; answer: string }[] },
  ) => Promise<{ status: string }>;
  updateReportOutline: (
    nb: string,
    rid: string,
    payload: { sections: unknown[]; frame?: ReportFrameT },
  ) => Promise<{ status: string; sections: number }>;
  generateReport: (nb: string, rid: string, depth?: number) => Promise<{ status: string }>;
  cancelReport: (nb: string, rid: string) => Promise<{ status: string }>;
  deleteReport: (nb: string, rid: string) => Promise<{ status: string }>;
  downloadReportsZip: (nb: string, reportIds: string[]) => Promise<void>;
  setToast: (message: string) => void;
  /** 「待确认中心」深链:指定报告 id 后自动拉详情并打开大纲编辑器,消费后由父组件清空。 */
  focusReportId?: string | null;
  onFocusConsumed?: () => void;
  readOnly?: boolean;
}

export function ReportsPanel({
  notebookId,
  listReports,
  getReport,
  createReport,
  confirmReportIntent,
  updateReportOutline,
  generateReport,
  cancelReport,
  deleteReport,
  downloadReportsZip,
  setToast,
  focusReportId,
  onFocusConsumed,
  readOnly = false,
}: ReportsPanelProps) {
  const [reports, setReports] = useState<ReportSummaryT[] | null>(null);
  const [active, setActive] = useState<ReportDetailT | null>(null);
  const [copied, setCopied] = useState(false);
  const [question, setQuestion] = useState("");
  const [depthIdx, setDepthIdx] = useState(1); // 默认「标准」(depth=2)
  const [creating, setCreating] = useState(false);
  const [actionBusy, setActionBusy] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  // 列表单篇下载:记录正在下载的 rid,禁用该行按钮防重复点击。
  const [downloadingId, setDownloadingId] = useState<string | null>(null);
  // 列表内删除:两步确认——记录待确认删除的 rid + 正在删除的 rid。
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  // 多选批量下载:是否处于多选模式 + 已选 rid 集合 + zip 下载中标志。
  const [selectMode, setSelectMode] = useState(false);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [zipBusy, setZipBusy] = useState(false);

  const surfaceError = (error: unknown) => {
    // ⚠这里**不要**再补 `console.error(error)`。原始值已经由 toUserMessage 送进
    // errors.ts 的受限诊断出口(压单行 + 统一截断到 500 字符)。补一条裸日志既是
    // 重复打印,又绕过那个上限——一个整页 HTML 错误页 / 超长堆栈能刷爆控制台,
    // 正是本轮「HTTP 与非 HTTP 共用同一截断上限」整改要堵的。(第四轮评审阻塞 3)
    //
    // fetch 层已按状态码译过(401/403/404/409 各不相同),直接展示译文——别再压平
    // 成一句通用文案,否则用户分不清「登录失效 / 没权限 / 已删除 / 冲突」,还会
    // 对权限和已删除这类重试也没用的问题反复点。非 HTTP 异常(如断网的
    // "Failed to fetch")才用兜底文案。
    setToast(toUserMessage(error, "报告操作没成功，请稍后重试"));
  };

  // 进 tab / 切换 notebook:重置视图并拉一次列表。
  useEffect(() => {
    let cancelled = false;
    setReports(null);
    setActive(null);
    setQuestion("");
    setConfirmDelete(false);
    setSelectMode(false);
    setSelectedIds(new Set());
    listReports(notebookId)
      .then((rows) => { if (!cancelled) setReports(rows); })
      .catch((error) => { if (!cancelled) { setReports([]); surfaceError(error); } });
    return () => { cancelled = true; };
    // surfaceError 仅包装 setToast,不入依赖。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [notebookId, listReports]);

  // 「待确认中心」深链:focusReportId 就绪且列表已拉取后,拉该报告详情并打开大纲编辑器;
  // 无论成败都要消费掉 focusReportId,避免重复触发。
  useEffect(() => {
    if (!focusReportId || reports === null) return;
    (async () => {
      try {
        const detail = await getReport(notebookId, focusReportId);
        setActive(detail);
      } catch (error) {
        surfaceError(error);
      } finally {
        onFocusConsumed?.();
      }
    })();
    // surfaceError 仅包装 setToast,不入依赖。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focusReportId, reports, notebookId, getReport, onFocusConsumed]);

  // 列表轮询:列表视图下存在非终态报告时每 6s 刷新;终态即停,卸载清理。
  const hasLiveReports = (reports ?? []).some((r) => isReportActive(r.status));
  const detailOpen = active !== null;
  useEffect(() => {
    if (!hasLiveReports || detailOpen) return;
    let cancelled = false;
    const poll = window.setInterval(async () => {
      try {
        const rows = await listReports(notebookId);
        if (!cancelled) setReports(rows);
      } catch { /* 瞬时失败:下一轮继续 */ }
    }, 6000);
    return () => { cancelled = true; window.clearInterval(poll); };
  }, [hasLiveReports, detailOpen, notebookId, listReports]);

  // 详情轮询:打开的报告非终态时每 6s 刷新;到终态再同步一次列表徽章。
  const activeId = active?.id ?? null;
  const activeLive = active ? isReportActive(active.status) : false;
  useEffect(() => {
    if (!activeId || !activeLive) return;
    let cancelled = false;
    const poll = window.setInterval(async () => {
      try {
        const detail = await getReport(notebookId, activeId);
        if (cancelled) return;
        setActive((cur) => (cur && cur.id === activeId ? detail : cur));
        if (!isReportActive(detail.status)) {
          listReports(notebookId)
            .then((rows) => { if (!cancelled) setReports(rows); })
            .catch(() => {});
        }
      } catch { /* 瞬时失败:下一轮继续 */ }
    }, 6000);
    return () => { cancelled = true; window.clearInterval(poll); };
  }, [activeId, activeLive, notebookId, getReport, listReports]);

  // 报告失败的原始异常串(services/report_execution.py 写的
  // `f"{type(exc).__name__}: {exc}"`)不上屏——界面给稳定文案,原文只进 console。
  // 放 useEffect 而不是渲染期:后者会随每次重渲染刷屏。
  const activeError = active?.status === "failed" ? active.error : null;
  useEffect(() => {
    if (activeError) logDiagnostic("report", activeError);
  }, [activeError]);

  // 删除二次确认 4s 后自动复位,避免按钮长期停在危险态。
  useEffect(() => {
    if (!confirmDelete) return;
    const timer = window.setTimeout(() => setConfirmDelete(false), 4000);
    return () => window.clearTimeout(timer);
  }, [confirmDelete]);

  async function submitCreate() {
    const q = question.trim();
    if (!q || creating) return;
    setCreating(true);
    try {
      await createReport(notebookId, q, DEPTHS[depthIdx]);
      setQuestion("");
      setToast("正在理解研究问题，完成后请先确认或补充关键信息");
      setReports(await listReports(notebookId));
    } catch (error) {
      surfaceError(error);
    } finally {
      setCreating(false);
    }
  }

  async function openReport(rid: string) {
    try {
      setConfirmDelete(false);
      setActive(await getReport(notebookId, rid));
    } catch (error) {
      surfaceError(error);
    }
  }

  function backToList() {
    setActive(null);
    setConfirmDelete(false);
    listReports(notebookId).then(setReports).catch(() => {});
  }

  async function requestCancel() {
    if (!active || actionBusy) return;
    setActionBusy(true);
    try {
      await cancelReport(notebookId, active.id);
      setToast("已请求取消，报告将停在当前进度");
      const detail = await getReport(notebookId, active.id);
      setActive((cur) => (cur && cur.id === detail.id ? detail : cur));
    } catch (error) {
      surfaceError(error);
    } finally {
      setActionBusy(false);
    }
  }

  async function requestDelete() {
    if (!active || actionBusy) return;
    if (!confirmDelete) { setConfirmDelete(true); return; }
    setActionBusy(true);
    try {
      await deleteReport(notebookId, active.id);
      setToast("报告已删除");
      setActive(null);
      setConfirmDelete(false);
      setReports(await listReports(notebookId));
    } catch (error) {
      surfaceError(error);
    } finally {
      setActionBusy(false);
    }
  }

  // 列表内单篇下载:拉详情拿 content_md,复用 downloadMd;瞬时禁用防重复点击。
  async function downloadOne(rid: string) {
    if (downloadingId) return;
    setDownloadingId(rid);
    try {
      const detail = await getReport(notebookId, rid);
      if (!detail.content_md) {
        setToast("该报告没有正文内容，无法下载");
        return;
      }
      downloadMd(detail);
    } catch (error) {
      surfaceError(error);
    } finally {
      setDownloadingId(null);
    }
  }

  // 列表内删除:第一次点亮出确认,确认后才真删;删完刷新列表。
  async function deleteFromList(rid: string) {
    if (deletingId) return;
    setDeletingId(rid);
    try {
      await deleteReport(notebookId, rid);
      setToast("报告已删除");
      setConfirmDeleteId(null);
      if (active && active.id === rid) setActive(null);
      setReports(await listReports(notebookId));
    } catch (error) {
      surfaceError(error);
    } finally {
      setDeletingId(null);
    }
  }

  // 进入/退出多选模式;退出时清空已选。
  function toggleSelectMode() {
    setSelectMode((on) => {
      if (on) setSelectedIds(new Set());
      return !on;
    });
  }

  // 勾选/取消勾选单行(仅 done 行会调用)。
  function toggleSelected(rid: string) {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(rid)) next.delete(rid);
      else next.add(rid);
      return next;
    });
  }

  // 批量下载:导出选中报告为 reports.zip;成功后退出多选并清空。
  async function downloadSelectedZip() {
    if (zipBusy || selectedIds.size === 0) return;
    setZipBusy(true);
    try {
      await downloadReportsZip(notebookId, Array.from(selectedIds));
      setSelectMode(false);
      setSelectedIds(new Set());
    } catch (error) {
      surfaceError(error);
    } finally {
      setZipBusy(false);
    }
  }

  // ---- 详情视图 ----
  if (active) {
    const displayQuestion = active.understanding?.confirmed
      ? active.understanding.resolved_question || active.question
      : active.question;
    return (
      <div className="report-panel report-detail">
        <div className="report-detail-head">
          <button className="report-action" type="button" onClick={backToList}>
            <ArrowLeft size={14} /> 返回列表
          </button>
          <div className="report-detail-actions">
            {!readOnly && isReportActive(active.status) && (
              <button
                className="report-action"
                type="button"
                disabled={actionBusy}
                onClick={() => void requestCancel()}
              >
                <Square size={12} /> {active.status === "planning" ? "取消规划" : "取消生成"}
              </button>
            )}
            {active.content_md && (
              <button
                className="report-action"
                type="button"
                onClick={() => {
                  copyReportContent(active.content_md)
                    .then(() => { setCopied(true); window.setTimeout(() => setCopied(false), 1600); })
                    .catch(() => undefined);
                }}
              >
                {copied ? <Check size={14} /> : <Copy size={14} />} {copied ? "已复制" : "复制"}
              </button>
            )}
            {active.content_md && (
              <button className="report-action" type="button" onClick={() => downloadMd(active)}>
                <Download size={14} /> 下载 .md
              </button>
            )}
            {!readOnly && (
              <button
                className={`report-action ${confirmDelete ? "danger" : ""}`}
                type="button"
                disabled={actionBusy}
                onClick={() => void requestDelete()}
              >
                <Trash2 size={14} /> {confirmDelete ? "确认删除" : "删除"}
              </button>
            )}
          </div>
        </div>
        <div className="report-detail-title">
          <h2 title={displayQuestion}>{displayQuestion}</h2>
          <div className="report-detail-meta">
            <ReportStatusBadge status={active.status} progress={active.progress} />
            <small title={active.status === "done" ? "总耗时按确认大纲、开始生成完整报告到完成计算" : undefined}>
              {formatReportTiming(active.status, active.created_at, active.updated_at, active.generation_started_at)}
              {active.section_count > 0 && ` · ${active.section_count} 节`}
            </small>
          </div>
        </div>
        {active.status === "failed" && active.error && (
          <div className="report-error">报告没能生成完，可以重试。</div>
        )}
        {active.status === "planning" && (
          <div className="report-running-hint report-planning-hint">
            <span className="report-status-dot" aria-hidden />
            <p>
              {active.progress.includes("理解研究问题")
                ? "正在理解研究问题，尚未读取语料"
                : "正在按已确认的问题检查语料并规划大纲"}
              {active.progress ? ` · ${active.progress}` : ""}
            </p>
          </div>
        )}
        {active.status === "intent_ready" && (
          <IntentReview
            report={active}
            notebookId={notebookId}
            confirmReportIntent={confirmReportIntent}
            readOnly={readOnly}
            onPlanning={(detail) => {
              setActive((current) => current?.id === detail.id ? detail : current);
              listReports(notebookId).then(setReports).catch(() => {});
            }}
            setToast={setToast}
          />
        )}
        {active.status === "outline_ready" && !readOnly && (
          <OutlineEditor
            report={active}
            notebookId={notebookId}
            updateReportOutline={updateReportOutline}
            generateReport={generateReport}
            onGenerating={(detail) => {
              setActive((cur) => (cur && cur.id === detail.id ? detail : cur));
              listReports(notebookId).then(setReports).catch(() => {});
            }}
            setToast={setToast}
          />
        )}
        {active.status === "outline_ready" && readOnly && (
          <div className="report-running-hint">
            <p>该报告大纲等待所有者确认。</p>
            <ol className="report-outline">
              {active.outline.map((section, index) => (
                <li key={`${section.title}-${index}`}>{section.title}</li>
              ))}
            </ol>
          </div>
        )}
        {isReportActive(active.status) && active.status !== "planning" && (
          <div className="report-running-hint">
            <p>正在后台生成，此页每 6 秒自动刷新；也可以先去其他 tab，随时回来查看。</p>
            {active.section_status && active.section_status.length > 0 ? (
              <ul className="report-section-status">
                {active.section_status.map((s, index) => {
                  const icon = sectionPhaseIcon(s.phase);
                  return (
                    <li className="report-section-row" key={`${s.title}-${index}`}>
                      <span className={`report-section-icon ${icon}`} aria-hidden>
                        {icon === "done" ? "✓" : icon === "failed" ? "!" : null}
                      </span>
                      <span className="report-section-title" title={s.title}>{s.title}</span>
                      <span
                        className="report-section-phase"
                        title={`${s.phase}${s.phase.startsWith("深挖") && s.step > 0 ? ` 第${s.step}步` : ""}`}
                      >
                        {s.phase}
                        {/* 深挖阶段的 phase 文案可能带大纲进度（「深挖中（已整理大纲 N 节）」），
                            按前缀判定,否则一旦模型开始整理大纲,步数就凭空消失。
                            文案变长后这一列会省略号截断（globals.css），故与标题列一样挂 title
                            让 hover 看得到全文。 */}
                        {s.phase.startsWith("深挖") && s.step > 0 && ` 第${s.step}步`}
                      </span>
                    </li>
                  );
                })}
              </ul>
            ) : active.outline.length > 0 ? (
              <>
                <span className="report-outline-caption">研究大纲（{active.outline.length} 节）</span>
                <ol className="report-outline">
                  {active.outline.map((o, index) => (
                    <li key={`${o.title}-${index}`} title={o.scope || undefined}>{o.title}</li>
                  ))}
                </ol>
              </>
            ) : (
              active.progress && <p className="report-running-progress">{active.progress}</p>
            )}
          </div>
        )}
        <ReportCorpusBasis report={active} />
        <ReportCredibilitySummary report={active} />
        {active.content_md && <ReportCitationDistribution report={active} />}
        {active.content_md && active.sections.some((section) => citationAuditLabel(section.citation_audit)) && (
          <div className="report-section-audits" aria-label="章节引证覆盖率">
            {active.sections.map((section, index) => {
              const coverage = citationAuditLabel(section.citation_audit);
              if (!coverage) return null;
              const uncited = section.citation_audit?.high_risk_uncited_count
                ?? section.citation_audit?.unsupported;
              return (
                <span className="report-citation-audit" key={`${section.title}-${index}`}>
                  {section.title} · {coverage}
                  {typeof uncited === "number" && uncited > 0 && ` · ${uncited} 条高风险断言待补引证`}
                </span>
              );
            })}
          </div>
        )}
        {active.content_md ? (
          <ReportMarkdown markdown={active.content_md} references={active.references} />
        ) : (
          !isReportActive(active.status)
          && !["failed", "intent_ready", "outline_ready"].includes(active.status) && (
            <p className="tool-hint">该报告没有正文内容（可能在完成前被取消）。</p>
          )
        )}
      </div>
    );
  }

  // ---- 列表视图 ----
  return (
    <div className="report-panel">
      {!readOnly && <div className="report-compose">
        <textarea
          className="report-compose-input"
          rows={2}
          placeholder="想深入研究什么？例如：对比库内各时序收敛方法的适用场景、代价与已知坑"
          value={question}
          disabled={creating}
          onChange={(event) => setQuestion(event.target.value)}
        />
        <div className="report-compose-actions">
          {/* 逐节检索走的就是问答那套逐步推理,英文双引号在这里同样生效——
              回执与提问框共用同一份规则,不另写一套措辞。 */}
          <span className="report-compose-hint">
            {quotedPhraseHint(question)
              ?? "后台多轮检索并逐节撰写，约 5–15 分钟，期间可离开此页"}
          </span>
          <div className="report-compose-controls">
            <EffortPicker
              chipLabel="深度"
              title="研究深度"
              options={DEPTH_OPTIONS}
              value={String(depthIdx)}
              onChange={(id) => setDepthIdx(Number(id))}
              disabled={creating}
            />
            <button
              className="button"
              type="button"
              disabled={creating || !question.trim()}
              onClick={() => void submitCreate()}
            >
              {creating ? "提交中…" : "生成深度报告"}
            </button>
          </div>
        </div>
      </div>}
      {reports === null ? (
        <p className="tool-hint">加载中…</p>
      ) : reports.length === 0 ? (
        <div className="chat-session-empty">还没有深度报告。输入研究问题，生成第一份带出处的长文报告。</div>
      ) : (
        <>
          {(() => {
            const doneCount = reports.filter((r) => r.status === "done").length;
            return (
              <div className={`report-list-toolbar${selectMode ? " select" : ""}`}>
                {selectMode ? (
                  <>
                    <span className="report-select-count">已选 {selectedIds.size} 篇</span>
                    <div className="report-select-actions">
                      <button
                        className="report-action"
                        type="button"
                        disabled={zipBusy || selectedIds.size === 0}
                        onClick={() => void downloadSelectedZip()}
                      >
                        <Download size={14} /> {zipBusy ? "打包中…" : "下载 zip"}
                      </button>
                      <button className="report-action" type="button" onClick={toggleSelectMode}>
                        取消
                      </button>
                    </div>
                  </>
                ) : (
                  <button
                    className="report-list-select-toggle"
                    type="button"
                    disabled={doneCount === 0}
                    onClick={toggleSelectMode}
                  >
                    <CheckSquare size={14} /> 批量下载
                  </button>
                )}
              </div>
            );
          })()}
          <div className="report-list">
            {reports.map((r) => {
              const isDone = r.status === "done";
              const checked = selectedIds.has(r.id);
              return (
                <article
                  className={`chat-session-card report-card${selectMode && isDone ? " selectable" : ""}${checked ? " selected" : ""}`}
                  key={r.id}
                >
                  {selectMode && isDone && (
                    <label className="report-card-check" onClick={(e) => e.stopPropagation()}>
                      <input
                        type="checkbox"
                        checked={checked}
                        onChange={() => toggleSelected(r.id)}
                        aria-label={`选择报告：${r.question}`}
                      />
                    </label>
                  )}
                  <button
                    className="chat-session-card-main"
                    type="button"
                    title={r.question}
                    onClick={() => (selectMode && isDone ? toggleSelected(r.id) : void openReport(r.id))}
                  >
                    <span>{r.question}</span>
                    <small title={r.status === "done" ? "总耗时按确认大纲、开始生成完整报告到完成计算" : undefined}>
                      {formatReportTiming(r.status, r.created_at, r.updated_at, r.generation_started_at)}
                      {r.section_count > 0 && ` · ${r.section_count} 节`}
                    </small>
                  </button>
                  <div className="report-card-tail">
                    <ReportStatusBadge status={r.status} progress={r.progress} />
                    {isDone && !selectMode && (
                      <button
                        className="report-card-download"
                        type="button"
                        title="下载 .md"
                        aria-label="下载 .md"
                        disabled={downloadingId === r.id}
                        onClick={(e) => {
                          e.stopPropagation();
                          void downloadOne(r.id);
                        }}
                      >
                        <Download size={16} />
                      </button>
                    )}
                    {!readOnly && !selectMode && (
                      confirmDeleteId === r.id ? (
                        <span className="report-card-confirm" onClick={(e) => e.stopPropagation()}>
                          <span className="report-card-confirm-text">删除?</span>
                          <button
                            type="button"
                            className="report-card-confirm-yes"
                            disabled={deletingId === r.id}
                            onClick={(e) => {
                              e.stopPropagation();
                              void deleteFromList(r.id);
                            }}
                          >
                            {deletingId === r.id ? "删除中…" : "确认"}
                          </button>
                          <button
                            type="button"
                            className="report-card-confirm-no"
                            onClick={(e) => {
                              e.stopPropagation();
                              setConfirmDeleteId(null);
                            }}
                          >
                            取消
                          </button>
                        </span>
                      ) : (
                        <button
                          className="report-card-delete"
                          type="button"
                          title="删除报告"
                          aria-label="删除报告"
                          onClick={(e) => {
                            e.stopPropagation();
                            setConfirmDeleteId(r.id);
                          }}
                        >
                          <Trash2 size={16} />
                        </button>
                      )
                    )}
                  </div>
                </article>
              );
            })}
          </div>
        </>
      )}
    </div>
  );
}
