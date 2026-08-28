/**
 * report-view.tsx
 *
 * 「深度报告」tab 的纯展示层。报告状态、类型化 API 调用、权限复核与轮询都由
 * use-report-workspace.ts 单一拥有；page.tsx 只组合 owner 与跨域 presentation effects。
 */
"use client";

import { useEffect, useRef, useState } from "react";
import { ArrowDown, ArrowLeft, ArrowUp, Check, CheckSquare, ChevronRight, Copy, Download, Plus, Share2, Sparkles, Square, Trash2, X } from "lucide-react";
import ReactMarkdown, { defaultUrlTransform } from "react-markdown";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import "katex/dist/katex.min.css";
import { remarkGfmPlugin } from "./markdown-gfm";
import { normalizeMathMarkdown } from "./math-markdown";
import { remarkCitations } from "./answer-citations";
import {
  computeSourceTierCounts, referenceByAnchorKey, type AnswerReference,
} from "./answer-formatting";
import { API_BASE } from "./api-config";
import { AuthedImage } from "./authed-image";
import { useCopyResult } from "./copy-result";
import { EffortPicker, type EffortOption } from "./effort-picker";
import { quotedPhraseHint } from "./query-syntax";
import { sourceImageAssetUrl } from "./source-image";
import { isAdvanced, type UiMode } from "./ui-mode.ts";
import {
  DEFAULT_REPORT_MAX_SECTIONS,
  DEFAULT_REPORT_MAX_SUBQUERIES_PER_SECTION,
  formatReportCoverage,
  parseReportSubQueries,
} from "./report-outline-model";
import type {
  ReportCitationAuditT,
  ReportCorpusProfileT,
  ReportCredibilityT,
  ReportDetailT,
  ReportDistributionT,
  ReportFrameAxisT,
  ReportFrameFacetT,
  ReportFrameT,
  ReportOutlineSectionT,
  ReportSufficiency,
  ReportSummaryT,
  ReportUnderstandingT,
} from "./report-model";
import { REPORT_DEPTHS, isReportActive, reportQuestionLimitHint } from "./report-model";
import { formatReportTiming } from "./report-time";
import type { ReportWorkspace } from "./use-report-workspace";
import { label, REPORT_DEPTH, REPORT_STATUS } from "./vocabulary";

export type {
  ReportCitationAuditT,
  ReportCorpusProfileT,
  ReportCredibilityT,
  ReportDetailT,
  ReportDistributionT,
  ReportFrameAxisT,
  ReportFrameFacetT,
  ReportFrameT,
  ReportOutlineSectionT,
  ReportSufficiency,
  ReportSummaryT,
  ReportUnderstandingT,
} from "./report-model";

// 非终态判定:轮询用。两阶段里 planning/generating 是活跃阶段,与 pending/running 同样需要轮询;
// outline_ready 是稳定的「等用户确认」态,不轮询(用户编辑大纲期间不该被刷新覆盖)。
// 研究深度:五档命名,index 0→4 一一对应 DEPTHS(每节 reflect 步上限)。
// 各档都算深入,区别在充分程度;后端 create_report 会 clamp 到 [1,16]。
const DEPTHS = REPORT_DEPTHS;
// 档名从 vocabulary.ts::REPORT_DEPTH 派生:那张按 depth 取值索引的表还要服务
// 只拿得到 reports.depth 数值的消费方(dev/logs 活动流)。两处各写一份档名必然漂移,
// 所以这里只保留「顺序」这一份本地信息,文字本身来自共享表。
const DEPTH_LABELS = DEPTHS.map((value) => label(REPORT_DEPTH, String(value), "标准"));
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

// 状态中文表已挪到 vocabulary.ts::REPORT_STATUS(跨模块单一真源,
// dev/logs/activity/format.ts 同样消费它)。

// 计划指定的导出方式:Blob → 临时 URL → 触发下载。
export function downloadReportMarkdown(r: ReportDetailT) {
  const blob = new Blob([r.content_md], { type: "text/markdown" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `report-${r.id}.md`;
  a.click();
  URL.revokeObjectURL(url);
}

export function downloadReportArchive(blob: Blob) {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = "reports.zip";
  anchor.click();
  URL.revokeObjectURL(url);
}

// 复制正文到剪贴板:优先 navigator.clipboard,回退到隐藏 textarea + execCommand。
export async function copyReportContent(text: string): Promise<void> {
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
  // execCommand 在被拒时**返回 false 而不抛异常**（非安全上下文、权限受限的
  // 浏览器里很常见）。忽略它，调用方就会理直气壮地说「已复制」。
  const copied = document.execCommand("copy");
  document.body.removeChild(textarea);
  if (!copied) throw new Error("clipboard copy was rejected");
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
      // 本段附图（T6）：空数组同 exclude_if 惯例整体缺席，undefined 时
      // AnswerAnchorLike.images 保持可选。
      images: reference.images,
    },
  }));
}

export function ReportMarkdown({
  markdown,
  references = [],
  notebookId = "",
}: {
  markdown: string;
  references?: ReportDetailT["references"];
  /**
   * 本段附图（T6）资产代理端点用的 active notebook id。可选——镜像
   * answer-panel.tsx SelectedReferenceDetail 的 `notebookId: string | null`
   * 防御式惯例：没有可用 notebook 上下文的调用点（如纯 markdown 渲染测试）
   * 不必传，此时附图区整体不渲染，与"无附图"等价，不是渲染失败。
   */
  notebookId?: string;
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
          remarkGfmPlugin,
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
          {/* 本段附图（T6）：与上方引证内容（snippet/来源/原始文件）用独立区块 +
              顶部分隔线区分——不是模型引用过的证据，只是证据片段附近的图，
              与 answer-panel.tsx SelectedReferenceDetail 同一视觉语义、同一套
              .cite-detail-images CSS（该 class 不 scope 在 .cite-popover
              下）。缺字段（旧报告/无图引用）或没有 notebookId（无资产代理端点
              可用，同 T2 的既有防御式惯例）时整体不渲染，AuthedImage 保持
              懒加载。这里刻意**不**接 onOpenSource 跳转（对比 answer-panel.tsx
              第 922 行 Ask 侧图片可点击跳转来源）：本组件没有这个 prop——报告
              侧引用详情区本就没有"打开来源"的交互面，与 Ask 侧的不对称是 v1
              范围内的刻意决定，不是遗漏。 */}
          {(selectedReference.images?.length ?? 0) > 0 && notebookId && (
            <div className="cite-detail-images">
              <span className="cite-detail-images-label">本段附图</span>
              <ul className="cite-detail-image-list">
                {selectedReference.images!.map((image) => {
                  const imageUrl = sourceImageAssetUrl(API_BASE, notebookId, image.asset_id);
                  return (
                    <li key={image.element_id} className="cite-detail-image-item">
                      {imageUrl
                        ? <AuthedImage url={imageUrl} alt={image.caption || "附图"} />
                        : <p className="tool-hint">图片不可用</p>}
                      {image.caption && (
                        <small className="cite-detail-image-caption">{image.caption}</small>
                      )}
                    </li>
                  );
                })}
              </ul>
            </div>
          )}
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
      <span className="report-status-label">{label(REPORT_STATUS, status, "处理中")}</span>
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
  busy,
  onConfirm,
  setToast,
  readOnly,
}: {
  report: ReportDetailT;
  busy: boolean;
  onConfirm: (payload: {
    resolved_question: string;
    answers: { id: string; answer: string }[];
  }) => Promise<void>;
  setToast: (message: string) => void;
  readOnly: boolean;
}) {
  const understanding = report.understanding || {};
  const ambiguities = understanding.ambiguities || [];
  const [resolvedQuestion, setResolvedQuestion] = useState(
    understanding.resolved_question || report.question,
  );
  const [answers, setAnswers] = useState<Record<string, string>>({});
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
    await onConfirm({
      resolved_question: resolvedQuestion.trim(),
      answers: ambiguities
        .map((item) => ({ id: item.id, answer: (answers[item.id] || "").trim() }))
        .filter((item) => item.answer),
    });
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
  // Legacy references do not say whether family_key/source_id was identity
  // verified.  Deriving these metrics from them would count every unresolved
  // source as independent and understate the conservative Top-1 bound.  Show
  // only the backend-persisted credibility values; the source-tier badge below
  // remains available for old reports.
  const independent = typeof declared === "number" && Number.isFinite(declared)
    ? declared
    : null;
  const top1 = percent(credibility.top1_share ?? credibility.top1_concentration);
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
  const ledgersPartial = credibility.claim_ledgers_partial;
  const showLedgers = typeof ledgersAvailable === "number" || typeof ledgersTotal === "number";
  if (!synthesisStatus && !showLedgers) return null;
  // Only silence receipts that are pure expected negatives.  `ledgersTotal` is
  // the section count, and a single-section report has no cross-section
  // consistency to synthesise, so its "not requested" carries no information.
  // The depth arm is legacy-only: reports generated while synthesis was gated
  // at depth >= 8 are expected no-ops too, and nothing in the payload dates a
  // report.  New low-depth reports do request synthesis, so they surface a real
  // status instead of reaching this branch.
  const isExpectedNoop = synthesisStatus === "not_requested"
    && ledgersAvailable === 0
    && typeof ledgersTotal === "number"
    && (ledgersTotal < 2 || (report.depth != null && report.depth < 8));
  if (isExpectedNoop) return null;
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
          {typeof ledgersPartial === "number" && ledgersPartial > 0
            ? `（其中 ${ledgersPartial} 节为部分账本）`
            : ""}
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
    <div className="report-source-dist" title="报告引证的资料数量与个人/公共来源分布">
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

/**
 * 缺统计时的说明。后端把「有意跳过」与「统计出错」分开标记后，界面不能再对两者
 * 说同一句话——限定资料范围的报告没有任何东西出错。历史报告缺标记，保持沉默而
 * 不猜测原因（此时卡片仍会显示完整性披露）。
 */
function corpusUnavailableCopy(reason: string | undefined): string {
  if (reason === "scope_restricted") {
    return "本次报告限定了检索的资料范围，因此没有统计整个知识库的资料基础。";
  }
  return reason ? "资料基础统计未能完成，本次没有可用的资料统计。" : "";
}

/**
 * 本次引用到的参考库资料**份数**（按来源去重，不是锚点数）。画像只统计当前笔记本，
 * 而检索是跨挂载参考库的；不点明这一半，"基于 N 份资料"会被读成证据的全部。
 * 与后端 `base_reference_source_count` 同口径。
 */
function baseReferenceSourceCount(
  references: ReportDetailT["references"] | undefined,
): number {
  const seen = new Set<string>();
  for (const reference of references || []) {
    // 是否来自挂载库由**归属 notebook** 判定：用户挂载自己的另一个 notebook 时
    // tier 仍是 "personal"，只看 tier 会把它的引用全漏掉。旧报告没有该字段，
    // 回退到 tier 信号。
    const mounted = reference?.from_reference_library !== undefined
      ? Boolean(reference.from_reference_library)
      : reference?.tier === "base";
    if (!mounted) continue;
    // 参考库的知识证据可以合法地没有 source_id（后端装配时用 family_key 兜底）。
    // 但只有能标识**来源**的 key 才算数：装配的最后兜底 `evidence:<锚点>` 每条
    // 引用都不同，计入它会把一份身份未知的资料数成好几份。
    let key = String(reference.source_id || "").trim();
    if (!key) {
      const family = String(reference.family_key || "").trim();
      key = family && !family.startsWith("evidence:") ? family : "";
    }
    if (key) seen.add(key);
  }
  return seen.size;
}

export function ReportCorpusBasis({ report }: { report: ReportDetailT }) {
  const profile = report.understanding?.corpus_profile;
  const scope = report.understanding?.result_scope;
  // An unavailable marker is a non-empty object, so presence alone no longer
  // means "there are statistics to show".
  const unavailableCopy = corpusUnavailableCopy(profile?.unavailable_reason);
  const measured = profile && !profile.unavailable_reason ? profile : undefined;
  const baseSources = baseReferenceSourceCount(report.references);
  const disclosure = profile?.completeness_disclosure
    || ((scope === "complete" || scope === "aggregate" || scope === "hybrid")
      ? "本报告按相关性检索生成，未做完整枚举。"
      : "");
  if (!profile && !disclosure && baseSources === 0) return null;
  const typeText = formatDistribution(measured?.type_distribution);
  const yearText = formatDistribution(measured?.year_distribution);
  const representativeTitles = (measured?.representatives || [])
    .map((item) => String(item.title || item.label || item.source_title || "").trim())
    .filter(Boolean);
  const duplicateLowerBound = measured?.identified_duplicate_lower_bound
    ?? measured?.duplicate_inflation;
  return (
    <section className="report-corpus-basis" aria-label="资料基础">
      <div className="report-corpus-basis-head">
        <h3>资料基础</h3>
        {typeof measured?.total_sources === "number" && (
          <span>{measured.total_sources} 份资料</span>
        )}
      </div>
      {unavailableCopy && (
        <p className="report-corpus-basis-unavailable">{unavailableCopy}</p>
      )}
      {baseSources > 0 && (
        <p className="report-corpus-basis-unavailable">
          {measured
            ? `另引用了 ${baseSources} 份参考库资料，未计入上述统计。`
            /* 没有统计时不能说「未计入上述统计」——旧报告的卡片上方是空的，
               那句话会指向一段并不存在的内容。 */
            : `本报告引用了 ${baseSources} 份参考库资料。`}
        </p>
      )}
      {measured && (
        <div className="report-corpus-basis-facts">
          {typeof (measured.independent_documents ?? measured.independent_families) === "number" && (
            <span>可区分资料 {measured.independent_documents ?? measured.independent_families}</span>
          )}
          {typeof (measured.displayed_sources ?? measured.representative_count) === "number" && typeof measured.total_sources === "number" && (
            <span>代表资料 {measured.displayed_sources ?? measured.representative_count}/{measured.total_sources}</span>
          )}
          {typeof measured.identity_uncertain_sources === "number" && measured.identity_uncertain_sources > 0 && (
            <span>另有 {measured.identity_uncertain_sources} 份资料无法可靠合并</span>
          )}
          {typeof duplicateLowerBound === "number" && duplicateLowerBound > 0 && (
            <span>保守识别重复至少 {duplicateLowerBound} 份</span>
          )}
          {percent(measured.metadata_coverage) && (
            <span>资料识别信息完整度 {percent(measured.metadata_coverage)}</span>
          )}
        </div>
      )}
      {(typeText || yearText) && (
        <dl className="report-corpus-basis-distributions">
          {typeText && <><dt>资料类型</dt><dd>{typeText}</dd></>}
          {yearText && <><dt>时间分布</dt><dd>{yearText}{typeof measured?.unknown_year === "number" ? ` · 年份未知 ${measured.unknown_year}` : ""}</dd></>}
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
  busy,
  onGenerate,
  setToast,
  maxSections = DEFAULT_REPORT_MAX_SECTIONS,
  maxSubqueriesPerSection = DEFAULT_REPORT_MAX_SUBQUERIES_PER_SECTION,
}: {
  report: ReportDetailT;
  busy: boolean;
  onGenerate: (payload: {
    sections: ReportOutlineSectionT[];
    frame?: ReportFrameT;
  }) => Promise<void>;
  setToast: (message: string) => void;
  maxSections?: number;
  maxSubqueriesPerSection?: number;
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
  const hasTooManySections = validCount > maxSections;
  const hasTooManySubqueries = sections.some(
    (section) => (section.sub_queries || []).filter((query) => query.trim()).length
      > maxSubqueriesPerSection,
  );
  const intentBindingCounts = new Map<string, number>();
  for (const section of sections) {
    for (const intentId of section.intent_ids || []) {
      intentBindingCounts.set(intentId, (intentBindingCounts.get(intentId) || 0) + 1);
    }
  }

  async function confirmGenerate() {
    if (busy) return;
    if (hasTooManySections) {
      setToast(`报告大纲最多可保留 ${maxSections} 个章节`);
      return;
    }
    if (hasTooManySubqueries) {
      setToast(`每个章节最多可保留 ${maxSubqueriesPerSection} 条检索方向`);
      return;
    }
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
    await onGenerate({ sections: cleaned, frame });
  }

  return (
    <div className="report-outline-editor">
      <div className="report-outline-editor-head">
        <div>
          <h3>确认研究大纲</h3>
          <p>已先锁定用户问题，再按语料覆盖规划出 {sections.length} 个章节（最多 {maxSections} 个）。可核对每节必答问题，修改标题、范围与检索方向后生成。</p>
        </div>
        <button
          className="report-action"
          type="button"
          onClick={addSection}
          disabled={busy || sections.length >= maxSections}
        >
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
                  <span>
                    检索方向（每行一条）
                    <small>最多 {maxSubqueriesPerSection} 条</small>
                  </span>
                  <textarea
                    value={(s.sub_queries || []).join("\n")}
                    rows={Math.max(
                      2,
                      Math.min(
                        maxSubqueriesPerSection,
                        (s.sub_queries || []).length || 2,
                      ),
                    )}
                    placeholder="输入本节需要执行的检索方向"
                    disabled={busy}
                    onChange={(e) => patchSection(s._key, { sub_queries: parseReportSubQueries(e.target.value) })}
                  />
                  {(s.sub_queries || []).filter((query) => query.trim()).length > maxSubqueriesPerSection && (
                    <small className="report-outline-field-error">
                      已超出上限，请删减后再生成。
                    </small>
                  )}
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
          {hasTooManySections
            ? `已超出 ${maxSections} 个章节上限`
            : validCount > 0
              ? `${validCount} 个有效章节`
              : "至少保留一个有标题的章节"}
        </span>
        <button
          className="button"
          type="button"
          disabled={busy || validCount === 0 || hasTooManySections || hasTooManySubqueries}
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
  workspace: ReportWorkspace;
  setToast: (message: string) => void;
  readOnly?: boolean;
  creationDisabled?: boolean;
  creationDisabledReason?: string;
  maxSections?: number;
  maxSubqueriesPerSection?: number;
  uiMode?: UiMode;
}

export function ReportsPanel({
  notebookId,
  workspace,
  setToast,
  readOnly = false,
  creationDisabled = false,
  creationDisabledReason = "",
  maxSections = DEFAULT_REPORT_MAX_SECTIONS,
  maxSubqueriesPerSection = DEFAULT_REPORT_MAX_SUBQUERIES_PER_SECTION,
  uiMode = "advanced",
}: ReportsPanelProps) {
  const {
    reports,
    active,
    question,
    depthIndex: depthIdx,
    creating,
    actionBusy,
    intentBusy,
    outlineBusy,
    shareBusy,
    shared,
    confirmDelete,
    confirmDeleteId,
    deletingId,
    downloadingId,
    selectMode,
    selectedIds,
    zipBusy,
    updateQuestion: setQuestion,
    selectDepth: setDepthIdx,
    submitCreate,
    openReport,
    backToList,
    requestCancel,
    requestRetry,
    confirmIntent,
    confirmOutline,
    toggleShare,
    copyShareLink,
    requestDelete,
    deleteById: deleteFromList,
    chooseDeleteConfirmation: setConfirmDeleteId,
    downloadOne,
    toggleSelectMode,
    toggleSelected,
    downloadSelected: downloadSelectedZip,
  } = workspace;
  // 工具栏里两颗复制按钮共用一格结果态:同一排里同时亮起两个结果没有意义,而 key 保证
  // 结果落在按下的那一颗上。复制正文此前只有成功态(失败被 .catch 吞掉,按钮纹丝不动)。
  // key 带上报告 id:结果要挂 1.6s,期间在报告之间切换会让同一颗按钮指向**另一份**报告,
  // 固定串会让新报告顶着上一份的「已复制」出现(codex #612 R2 P2)。
  const copyResult = useCopyResult();
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
                className={copyResult.resultFor(`content:${active.id}`) === "copied" ? "report-action copy-result-copied" : copyResult.resultFor(`content:${active.id}`) === "failed" ? "report-action copy-result-failed" : "report-action"}
                type="button"
                onClick={() => {
                  copyReportContent(active.content_md)
                    .then(() => copyResult.report(`content:${active.id}`, true))
                    .catch(() => copyResult.report(`content:${active.id}`, false));
                }}
              >
                {copyResult.resultFor(`content:${active.id}`) === "copied" ? <Check size={14} /> : copyResult.resultFor(`content:${active.id}`) === "failed" ? <X size={14} /> : <Copy size={14} />}
                {" "}
                {copyResult.resultFor(`content:${active.id}`) === "copied" ? "已复制" : copyResult.resultFor(`content:${active.id}`) === "failed" ? "复制失败" : "复制"}
              </button>
            )}
            {active.content_md && (
              <button className="report-action" type="button" onClick={() => downloadReportMarkdown(active)}>
                <Download size={14} /> 下载 .md
              </button>
            )}
            {!readOnly && active.status === "done" && (
              <button
                className="report-action"
                type="button"
                disabled={shareBusy}
                onClick={() => void toggleShare()}
              >
                <Share2 size={14} />
                {shareBusy
                  ? (shared ? "撤销中…" : "生成链接中…")
                  : (shared ? "取消分享" : "分享")}
              </button>
            )}
            {!readOnly && shared && (
              <button
                className={copyResult.resultFor(`share-link:${active.id}`) === "copied" ? "report-action copy-result-copied" : copyResult.resultFor(`share-link:${active.id}`) === "failed" ? "report-action copy-result-failed" : "report-action"}
                type="button"
                disabled={shareBusy}
                onClick={() => {
                  // null = 这一次没走到复制(切库/换报告/取回链接失败),那时按钮不该
                  // 闪一下「复制失败」——错误本身已由 hook 经 notify 报出。
                  void copyShareLink().then((copied) => {
                    if (copied !== null) copyResult.report(`share-link:${active.id}`, copied);
                  });
                }}
              >
                {copyResult.resultFor(`share-link:${active.id}`) === "copied" ? <Check size={14} /> : copyResult.resultFor(`share-link:${active.id}`) === "failed" ? <X size={14} /> : <Copy size={14} />}
                {" "}
                {copyResult.resultFor(`share-link:${active.id}`) === "copied" ? "已复制" : copyResult.resultFor(`share-link:${active.id}`) === "failed" ? "复制失败" : "复制链接"}
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
        {active.status === "failed" && (
          <div className="report-error">
            <span>{active.outline.length > 0
              ? "报告没能生成完，可复用已确认的问题和大纲重新生成。"
              : "报告在形成可复用大纲前失败，请重新创建报告。"}</span>
            {!readOnly && active.outline.length > 0 && (
              <button className="report-action" type="button" disabled={actionBusy}
                onClick={() => void requestRetry()}>
                重新生成
              </button>
            )}
          </div>
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
            busy={intentBusy}
            onConfirm={confirmIntent}
            readOnly={readOnly}
            setToast={setToast}
          />
        )}
        {active.status === "outline_ready" && !readOnly && (
          <OutlineEditor
            report={active}
            busy={outlineBusy}
            onGenerate={confirmOutline}
            setToast={setToast}
            maxSections={maxSections}
            maxSubqueriesPerSection={maxSubqueriesPerSection}
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
          <ReportMarkdown markdown={active.content_md} references={active.references} notebookId={notebookId} />
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
  // 超限只拦提交,不动用户输入(codex #525 R3):按码点数,与后端同一把尺。
  const questionLimitHint = reportQuestionLimitHint(question);
  const questionTooLong = questionLimitHint !== null;

  return (
    <div className="report-panel">
      {!readOnly && <div className="report-compose">
        <textarea
          className="report-compose-input"
          rows={2}
          placeholder={creationDisabled
            ? (creationDisabledReason || "请先选择检索来源")
            : "想深入研究什么？例如：对比库内各时序收敛方法的适用场景、代价与已知坑"}
          value={question}
          disabled={creating || creationDisabled}
          // 刻意**不**加 `maxLength`、也不在这里夹：超限的处理是「拦住提交并说清楚」
          // （见下方 hint 与按钮的 disabled），不是替用户把粘进来的尾巴删掉。
          onChange={(event) => setQuestion(event.target.value)}
        />
        <div className="report-compose-actions">
          {/* 逐节检索走的就是问答那套逐步推理,英文双引号在这里同样生效——
              回执与提问框共用同一份规则,不另写一套措辞。 */}
          {/* 超限提示优先于引号回执：它是此刻唯一挡着提交的东西。 */}
          <span className={`report-compose-hint${questionTooLong ? " over-limit" : ""}`}>
            {questionLimitHint
              ?? quotedPhraseHint(question)
              ?? "后台多轮检索并逐节撰写，约 5–15 分钟，期间可离开此页"}
          </span>
          <div className="report-compose-controls">
            {isAdvanced(uiMode) && (
              <EffortPicker
                chipLabel="深度"
                title="研究深度"
                options={DEPTH_OPTIONS}
                value={String(depthIdx)}
                onChange={(id) => setDepthIdx(Number(id))}
                disabled={creating || creationDisabled}
              />
            )}
            <button
              className="button"
              type="button"
              disabled={creating || creationDisabled || !question.trim() || questionTooLong}
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
