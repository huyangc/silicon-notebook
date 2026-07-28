"use client";

import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle,
  BookmarkPlus,
  Check,
  ChevronDown,
  ChevronRight,
  Copy,
  ExternalLink,
  Sparkles,
  Table2,
  ThumbsDown,
  ThumbsUp,
} from "lucide-react";
import katex from "katex";

import {
  buildAnswerReferences,
  computeSourceTierCounts,
  renderTextWithReferenceNumbers,
  splitInlineLatex,
  type AnswerReference,
} from "./answer-formatting";
import { AnswerMarkdown } from "./answer-markdown";
import { type ReasoningTraceStep } from "./ask-stream";
import { placeCitationPopover } from "./citation-popover";
import { copyTextSafely } from "./copy-text";
import { mapCitationKnowhowRef } from "./knowhow-model.ts";
import { unwrapStandaloneLatex } from "./math-markdown";
import { KgTypeMark, kgTypeLabel } from "./kg-type-mark";
import {
  modelFailureText,
  sanitizeModelServiceId,
  sanitizeModelSupportId,
  type ModelServiceStatusItem,
} from "./model-services.ts";
import {
  formatDuration,
  getReasoningTraceSummary,
  getTraceStepDetail,
  TRACE_STEP_LABELS,
} from "./reasoning-trace";
import {
  STRUCTURED_ENUMERATION_LIMITS,
} from "./ask-retrieval-effort";
import { shouldShowIndexRequiredBanner, type ScaleIndexStatus } from "./scale-index";
import type { AskResponse, KnowhowBatchCoverage, KnowhowResultSet } from "./workspace-model";
import { SupportIdCopy } from "./support-id-copy";
import { label, MODEL_SERVICE_STATUS_ERROR, TIER } from "./vocabulary";


function InlineFormula({ latex }: { latex: string }) {
  let html = "";
  const normalized = unwrapStandaloneLatex(latex);
  try {
    html = katex.renderToString(normalized, {
      throwOnError: true,
      strict: "ignore",
      displayMode: false,
    });
  } catch {
    html = "";
  }
  if (!html) return <code className="answer-inline-code math-render-fallback">{latex}</code>;
  return <span className="answer-inline-formula" dangerouslySetInnerHTML={{ __html: html }} />;
}


function KnowhowResultSetCard({
  resultSet,
  onOpenKnowhowRow,
}: {
  resultSet: KnowhowResultSet;
  onOpenKnowhowRow: (tableId: string, rowId: string) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const visibleLimit = STRUCTURED_ENUMERATION_LIMITS.initialVisibleRows;
  const rows = expanded ? resultSet.rows : resultSet.rows.slice(0, visibleLimit);
  const coverage = resultSet.coverage;
  const coverageCount = Math.min(coverage.returned_rows, coverage.total_rows);
  const status = coverage.complete ? "完整" : "部分";
  const hasMoreLoadedRows = resultSet.rows.length > visibleLimit;

  return (
    <section className="answer-knowhow-result" aria-label={`表格结果：${resultSet.title}`}>
      <div className="answer-knowhow-result-heading">
        <span className={`tag ${coverage.complete ? "answer-grounded" : "answer-overview"}`}>
          {status} {coverageCount}/{coverage.total_rows}
        </span>
        <strong><Table2 size={14} aria-hidden="true" /> {resultSet.title}</strong>
        {!coverage.complete && coverage.truncated_reason && (
          <span className="answer-knowhow-partial-reason" title={coverage.overflow_semantics || "explicit_partial"}>
            已明确标注为部分结果（{coverage.truncated_reason}）
            {coverage.scanned_rows !== coverage.returned_rows
              ? `；已扫描 ${coverage.scanned_rows} 行`
              : ""}
          </span>
        )}
      </div>
      <div className="answer-table-wrap">
        <table className="answer-table answer-knowhow-table">
          <thead>
            <tr>
              <th scope="col">行</th>
              {resultSet.columns.map((column) => <th key={column.id} scope="col">{column.name}</th>)}
              <th scope="col" aria-label="操作" />
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.row_id}>
                <th scope="row">{row.position + 1}</th>
                {resultSet.columns.map((column) => <td key={column.id}>{row.cells[column.id] || "—"}</td>)}
                <td>
                  <button
                    className="answer-knowhow-open"
                    type="button"
                    onClick={() => onOpenKnowhowRow(resultSet.table_id, row.row_id)}
                  >
                    在表格中查看
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {hasMoreLoadedRows && (
        <button
          type="button"
          className="answer-knowhow-expand"
          onClick={() => setExpanded((value) => !value)}
        >
          {expanded ? "收起已加载行" : `展开全部已加载的 ${resultSet.rows.length} 行`}
        </button>
      )}
      {!coverage.complete && (
        <p className="answer-knowhow-coverage-note">
          已扫描 {coverage.scanned_rows} 行、返回 {coverage.returned_rows} 行；未扫描部分不会被表述为“全部”。
        </p>
      )}
    </section>
  );
}


function KnowhowResultSets({
  resultSets,
  batchCoverage,
  onOpenKnowhowRow,
}: {
  resultSets: KnowhowResultSet[] | undefined;
  batchCoverage: KnowhowBatchCoverage | undefined;
  onOpenKnowhowRow: (tableId: string, rowId: string) => void;
}) {
  if (!resultSets?.length) return null;
  return (
    <div className="answer-knowhow-results">
      {batchCoverage && (
        <section className="answer-knowhow-batch-coverage" aria-label="Knowhow 总体覆盖">
          <span className={`tag ${batchCoverage.complete ? "answer-grounded" : "answer-overview"}`}>
            总体{batchCoverage.complete ? "完整" : "部分"}
          </span>
          <span>
            表 {batchCoverage.selected_tables}/{batchCoverage.known_tables}；行 {batchCoverage.returned_rows}/{batchCoverage.known_total_rows}
          </span>
          {batchCoverage.synthesis_complete != null && (
            <span>
              分析覆盖 {batchCoverage.synthesis_rows}/{batchCoverage.known_total_rows}
              （{batchCoverage.synthesis_complete ? "完整" : "部分"}）
            </span>
          )}
          {!batchCoverage.complete && batchCoverage.truncated_reason && (
            <span className="answer-knowhow-partial-reason">
              截断原因：{batchCoverage.truncated_reason}
            </span>
          )}
        </section>
      )}
      {resultSets.map((resultSet) => (
        <KnowhowResultSetCard
          key={`${resultSet.table_id}-${resultSet.title}`}
          resultSet={resultSet}
          onOpenKnowhowRow={onOpenKnowhowRow}
        />
      ))}
    </div>
  );
}


/** Render a formula headline or inline LaTeX segments inside prose. */
export function LatexText({ text, isFormula = false }: { text: string; isFormula?: boolean }) {
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


function SelectedReferenceDetail({
  reference,
  notebookNames,
  onOpenKnowledgeGraph,
  onOpenKnowhowRow,
}: {
  reference: AnswerReference;
  /** 多领域基准库(Task 14)：id→name 映射，来自 notebooks 列表 + 当前笔记本挂载的
   * 参考库(base_notebooks)合并，供引用徽章把 notebook_id 解成人类可读的库名。 */
  notebookNames: Record<string, string>;
  onOpenKnowledgeGraph: (objectId?: string, sourceNotebookId?: string) => void;
  /** Task 12（引用跳转）：命中 knowhow 格子的引用才出现「在表格中查看」按钮。 */
  onOpenKnowhowRow: (tableId: string, rowId: string) => void;
}) {
  const objectType = reference.anchor?.object_type || "";
  const title = referenceTitle(reference);
  const snippet = referenceSnippet(reference);
  const source = referenceSource(reference);
  const location = referenceLocation(reference);
  // citation/anchor 二选一(buildAnswerReferences 全有全无),但既有的 anchor-only
  // 写法会让「无 [k] 标记、走 citation 回退列表」的答案永远显示不出 tier 徽章——
  // 补齐 citation 分支，和上面 referenceTitle/referenceSource 等 helper 的
  // "anchor 优先、citation 兜底"惯例保持一致。
  const tier = reference.anchor?.tier ?? reference.citation?.tier ?? "";
  const sourceNotebookId = reference.citation?.notebook_id || reference.anchor?.notebook_id || "";
  const sourceName = sourceNotebookId ? notebookNames[sourceNotebookId] : undefined;
  const isRelationReference = objectType === "relation";
  const canLocateInGraph = Boolean(reference.anchor?.object_id) && !isRelationReference;
  // Task 12b（引用跳转扩面）：citation 优先，anchor 兜底——两者理论上不会同时
  // 出现在同一条 reference 上（buildAnswerReferences 二选一），但顺序仍按
  // "更具体的赢"的既有惯例书写，与 knowhow-citation.test.mjs 的显式断言一致。
  const knowhowRef = mapCitationKnowhowRef(reference.citation?.knowhow ?? reference.anchor?.knowhow);
  return (
    <aside className="cite-detail-card" aria-live="polite">
      <div className="cite-detail-head">
        <strong>{reference.displayLabel}</strong>
        {objectType && <span><KgTypeMark type={objectType} />{kgTypeLabel(objectType)}</span>}
        {tier && (
          <span
            className={`tier-badge tier-${tier}`}
            title={
              sourceName
                ? `来自「${sourceName}」（${tier === "base" ? "公共知识库" : "个人知识库"}）`
                : (tier === "base" ? "来自公共知识库" : "来自个人知识库")
            }
          >
            {sourceName ? (
              // 可达性修复(codex 评审 PR#304 第 3 轮 P2 #2):库名此前只进了上面的
              // title(hover 提示),触屏/键盘用户完全看不到是哪个库。这里把库名
              // 并入可见文字——长名走 .tier-badge-source-name 的省略号截断,不撑
              // 爆卡片;title 与可见文字内容一致,悬浮仍能看到被截断的完整库名。
              <>来自「<span className="tier-badge-source-name">{sourceName}</span>」（{label(TIER, tier, "未知来源")}）</>
            ) : (
              // 查不到库名(如跨二级挂载):优雅退回原有的泛化 tier 文案,不吐 id/空白。
              label(TIER, tier, "未知来源")
            )}
          </span>
        )}
        <button
          type="button"
          onClick={() => onOpenKnowledgeGraph(
            reference.anchor?.object_id,
            sourceNotebookId || undefined,
          )}
          disabled={!canLocateInGraph}
          title={
            // 用「知识对象」而非「概念」:引用锚定的是 object_id,其 object_type 可以是
            // Concept / Claim / Formula / Procedure 或 knowhow 表带来的自定义类型。
            // 说「概念」会把后四类说成第一类,用户按图索骥时对不上。
            isRelationReference
              ? "关系引用绑定的是一条关联，不是具体的知识对象，无法在知识图谱中定位"
              : reference.anchor?.object_id
                ? "在知识图谱中定位"
                : "该引用没有绑定到具体的知识对象"
          }
        >
          <ExternalLink size={14} />
          {isRelationReference ? "关系证据不可定位" : "知识图谱"}
        </button>
        {knowhowRef && (
          <button
            type="button"
            onClick={() => onOpenKnowhowRow(knowhowRef.tableId, knowhowRef.rowId)}
            title="在 Knowhow 表格中查看这一行"
          >
            <Table2 size={14} />
            在表格中查看
          </button>
        )}
      </div>
      <h4><LatexText text={title} isFormula={objectType === "formula"} /></h4>
      {snippet && <p><LatexText text={snippet} /></p>}
      {(source || location) && <small>{[source, location].filter(Boolean).join(" · ")}</small>}
    </aside>
  );
}


function CitationPopover({
  reference,
  notebookNames,
  anchorRect,
  onClose,
  onOpenKnowledgeGraph,
  onOpenKnowhowRow,
}: {
  reference: AnswerReference;
  notebookNames: Record<string, string>;
  anchorRect: DOMRect;
  onClose: () => void;
  onOpenKnowledgeGraph: (objectId?: string, sourceNotebookId?: string) => void;
  onOpenKnowhowRow: (tableId: string, rowId: string) => void;
}) {
  const ref = useRef<HTMLDivElement | null>(null);
  const [pos, setPos] = useState<{ top: number; left: number }>(
    () => ({ top: anchorRect.bottom + 6, left: anchorRect.left })
  );
  useLayoutEffect(() => {
    const element = ref.current;
    if (!element) return;
    const rect = element.getBoundingClientRect();
    setPos(placeCitationPopover(
      { top: anchorRect.top, bottom: anchorRect.bottom, left: anchorRect.left },
      { width: rect.width, height: rect.height },
      { width: window.innerWidth, height: window.innerHeight },
    ));
  }, [anchorRect]);
  useEffect(() => {
    const onDown = (event: PointerEvent) => {
      if (ref.current && !ref.current.contains(event.target as Node)) onClose();
    };
    const onKey = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    const onScroll = (event: Event) => {
      // 浮层内部滚动(查看长内容)不应关闭;只有外部页面/祖先滚动导致脱锚时才关。
      if (ref.current && ref.current.contains(event.target as Node)) return;
      onClose();
    };
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
    <div
      ref={ref}
      className="cite-popover"
      role="dialog"
      style={{ position: "fixed", top: pos.top, left: pos.left }}
    >
      <SelectedReferenceDetail
        reference={reference}
        notebookNames={notebookNames}
        onOpenKnowledgeGraph={onOpenKnowledgeGraph}
        onOpenKnowhowRow={onOpenKnowhowRow}
      />
    </div>
  );
}


export function ReasoningTracePanel({
  steps,
  live = false,
}: {
  steps: ReasoningTraceStep[];
  live?: boolean;
}) {
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
        <span className={`reasoning-trace-chip ${summary.latestLabel ? "" : "empty"}`}>
          {summary.latestLabel || "空"}
        </span>
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
                <span>{label(TRACE_STEP_LABELS, step.step_type, "处理中")}</span>
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


type AnswerModelError = NonNullable<AskResponse["model_errors"]>[number];


function modelTestResultText(result: ModelServiceStatusItem): string {
  if (result.status === "ok") return `正常 ${result.latency_ms}ms`;
  if (result.status === "busy") return `繁忙 ${result.active} / ${result.maximum}`;
  return `失败：${label(MODEL_SERVICE_STATUS_ERROR, result.code, "连接未通过")}`;
}


function ModelErrorPanel({
  errors,
  onTestModel,
  onOpenModelStatus,
  testingModelServices = {},
  testingAllModels = false,
}: {
  errors: AnswerModelError[];
  onTestModel?: (serviceId: string) => Promise<ModelServiceStatusItem | null>;
  onOpenModelStatus?: (serviceId: string) => void;
  testingModelServices?: Record<string, boolean>;
  testingAllModels?: boolean;
}) {
  const uniqueErrors = useMemo(() => {
    const seen = new Set<string>();
    return errors.map((error) => ({
      error,
      serviceId: sanitizeModelServiceId(error.service_id),
      supportId: sanitizeModelSupportId(error.support_id),
    })).filter(({ error, serviceId, supportId }) => {
      const key = `${serviceId}\0${error.model}\0${supportId}`;
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  }, [errors]);
  const testingServices = useRef(new Set<string>());
  const [testing, setTesting] = useState<Record<string, boolean>>({});
  const [results, setResults] = useState<Record<string, string>>({});

  async function testService(serviceId: string) {
    if (!onTestModel || !serviceId || testingServices.current.has(serviceId)) return;
    testingServices.current.add(serviceId);
    setTesting((current) => ({ ...current, [serviceId]: true }));
    setResults((current) => ({ ...current, [serviceId]: "" }));
    try {
      const result = await onTestModel(serviceId);
      if (!result) return;
      setResults((current) => ({ ...current, [serviceId]: modelTestResultText(result) }));
    } catch {
      setResults((current) => ({ ...current, [serviceId]: "失败：连接未通过" }));
    } finally {
      testingServices.current.delete(serviceId);
      setTesting((current) => ({ ...current, [serviceId]: false }));
    }
  }

  return (
    <section className="answer-model-error" aria-label="模型服务异常">
      <div className="answer-model-error-heading">⚠️ 本次回答可能不完整</div>
      <ul className="answer-model-error-list">
        {uniqueErrors.map(({ error, serviceId, supportId }) => {
          const isTesting = testingAllModels
            || Boolean(testingModelServices[serviceId])
            || Boolean(testing[serviceId]);
          return (
            <li key={`${serviceId}\0${error.model}\0${supportId}`}>
              <span>{modelFailureText(error)}</span>
              <div className="answer-model-error-actions">
                {onTestModel && serviceId && (
                  <button
                    type="button"
                    disabled={isTesting}
                    onClick={() => testService(serviceId)}
                  >
                    {isTesting ? "测试中…" : "测试此模型"}
                  </button>
                )}
                {onOpenModelStatus && (
                  <button type="button" onClick={() => onOpenModelStatus(serviceId)}>
                    查看模型状态
                  </button>
                )}
                <SupportIdCopy supportId={supportId} className="answer-model-support-id" />
                {results[serviceId] && (
                  <span className="answer-model-test-result" role="status">
                    {results[serviceId]}
                  </span>
                )}
              </div>
            </li>
          );
        })}
      </ul>
    </section>
  );
}


export function AnswerView({
  answer,
  feedbackSent,
  onFeedback,
  onOpenKnowledgeGraph,
  onOpenKnowhowRow,
  notebookId,
  notebookNames,
  onBuildScaleIndex,
  buildingScaleIndex,
  scaleIndexStatus,
  onSaveMemory,
  memorySaved,
  onTestModel,
  onOpenModelStatus,
  testingModelServices,
  testingAllModels,
}: {
  answer: AskResponse;
  feedbackSent: string;
  onFeedback: (rating: "useful" | "not_useful") => void;
  onOpenKnowledgeGraph: (objectId?: string, sourceNotebookId?: string) => void;
  /** Task 12（引用跳转）：命中 knowhow 格子的引用点「在表格中查看」时调用，
   * page.tsx 据此打开 Knowhow 面板并定位到该表该行的抽屉。 */
  onOpenKnowhowRow: (tableId: string, rowId: string) => void;
  notebookId: string | null;
  /** 多领域基准库(Task 14)：id→name 映射，来自 notebooks 列表 + 当前笔记本挂载的
   * 参考库(base_notebooks)合并，逐 turn 复用同一份，供引用徽章标来源库名。 */
  notebookNames: Record<string, string>;
  onBuildScaleIndex: (notebookId: string) => void;
  buildingScaleIndex: boolean;
  scaleIndexStatus?: Pick<ScaleIndexStatus, "exists" | "building" | "state"> | null;
  onSaveMemory: (answerId: string) => void;
  memorySaved: boolean;
  onTestModel?: (serviceId: string) => Promise<ModelServiceStatusItem | null>;
  onOpenModelStatus?: (serviceId: string) => void;
  testingModelServices?: Record<string, boolean>;
  testingAllModels?: boolean;
}) {
  const [copied, setCopied] = useState(false);
  const [citePopover, setCitePopover] = useState<{
    reference: AnswerReference;
    rect: DOMRect;
  } | null>(null);
  const answerText = answer.answer || answer.conclusion || "";
  const scaleIndexQueued = scaleIndexStatus?.state === "queued"
    && !scaleIndexStatus.building;
  const references = useMemo(
    () => buildAnswerReferences(answerText, answer.anchors, answer.citations),
    [answerText, answer.anchors, answer.citations]
  );
  useEffect(() => setCitePopover(null), [answer.answer_id]);
  useEffect(() => {
    if (!copied) return;
    const timer = window.setTimeout(() => setCopied(false), 1400);
    return () => window.clearTimeout(timer);
  }, [copied]);

  async function copyAnswer() {
    if (await copyTextSafely(renderTextWithReferenceNumbers(answerText, references))) {
      setCopied(true);
    }
  }

  return (
    <div className="chat-answer">
      {answer.model_errors && answer.model_errors.length > 0 && (
        <ModelErrorPanel
          errors={answer.model_errors}
          onTestModel={onTestModel}
          onOpenModelStatus={onOpenModelStatus}
          testingModelServices={testingModelServices}
          testingAllModels={testingAllModels}
        />
      )}
      {shouldShowIndexRequiredBanner(answer.index_required, scaleIndexStatus) && (
        <div className="answer-index-degraded" title="内容较多时检索会走索引；尚未建立索引时结果会受限">
          <AlertTriangle size={14} aria-hidden="true" />
          <span>此笔记本内容较多，尚未建立索引，当前检索能力受限。</span>
          <button
            type="button"
            className="mode-engine"
            style={{ marginLeft: 6 }}
            disabled={buildingScaleIndex && !scaleIndexQueued}
            onClick={() => { if (notebookId) onBuildScaleIndex(notebookId); }}
          >
            {scaleIndexQueued ? "立即构建" : buildingScaleIndex ? "构建中…" : "构建索引"}
          </button>
        </div>
      )}
      {(() => {
        const level = answer.evidence_level ?? (answer.grounded ? "grounded" : "inferred");
        const meta = level === "grounded"
          ? { cls: "answer-grounded", label: "有据" }
          : level === "overview"
            ? { cls: "answer-overview", label: "概述（仅薄证据，余为推断）" }
            : { cls: "answer-ungrounded", label: "推断（未命中笔记本依据）" };
        return <span className={`tag ${meta.cls}`}>{meta.label}</span>;
      })()}
      {(() => {
        const { personal, base } = computeSourceTierCounts(references);
        if (personal + base === 0) return null;
        return (
          <span className="tag source-dist" title="本次引用的来源分布（个人知识库 / 公共知识库）">
            来源 · 个人 {personal}
            {base > 0 && <> · <strong className="source-dist-base">公共 {base}</strong></>}
          </span>
        );
      })()}
      <AnswerMarkdown
        answer={answerText}
        anchors={answer.anchors}
        citations={answer.citations}
        selectedReferenceId={citePopover?.reference.id ?? null}
        onReferenceClick={(reference, event) => setCitePopover({
          reference,
          rect: event.currentTarget.getBoundingClientRect(),
        })}
      />
      <KnowhowResultSets
        resultSets={answer.result_sets}
        batchCoverage={answer.result_coverage}
        onOpenKnowhowRow={onOpenKnowhowRow}
      />
      {answer.reasoning_trace && answer.reasoning_trace.length > 0 && (
        <ReasoningTracePanel steps={answer.reasoning_trace} />
      )}
      {citePopover && (
        <CitationPopover
          reference={citePopover.reference}
          notebookNames={notebookNames}
          anchorRect={citePopover.rect}
          onClose={() => setCitePopover(null)}
          // 「知识图谱」「在表格中查看」都会在本卡片之上打开一个新的全屏视图
          // （.kg-view / .knowhow-view，z-index 均为 50，低于本卡片的 60）——
          // 点击跳转后若不关闭这张卡片，它会一直浮在新打开的视图上方挡住内容
          // （真机 QA 反馈）。复用与 onClose 完全相同的收起路径
          // （setCitePopover(null)），不为此新开一套状态。
          onOpenKnowledgeGraph={(objectId, sourceNotebookId) => {
            setCitePopover(null);
            onOpenKnowledgeGraph(objectId, sourceNotebookId);
          }}
          onOpenKnowhowRow={(tableId, rowId) => {
            setCitePopover(null);
            onOpenKnowhowRow(tableId, rowId);
          }}
        />
      )}
      <div className="answer-feedback">
        <button
          aria-label={memorySaved ? "已保存到记忆" : "保存到记忆"}
          className={`answer-memory-save ${memorySaved ? "is-saved" : ""}`}
          disabled={memorySaved}
          onClick={() => onSaveMemory(answer.answer_id)}
          title={memorySaved ? "已保存到记忆" : "保存到记忆"}
          type="button"
        >{memorySaved ? <Check size={15} /> : <BookmarkPlus size={15} />}<span>{memorySaved ? "已保存到记忆" : "保存到记忆"}</span></button>
        <div className="answer-feedback-actions">
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
    </div>
  );
}
