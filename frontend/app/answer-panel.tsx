"use client";

import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import {
  BookmarkPlus,
  Check,
  ChevronDown,
  ChevronRight,
  Copy,
  ExternalLink,
  Sparkles,
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
import { KgTypeMark, kgTypeLabel } from "./kg-type-mark";
import {
  formatDuration,
  getReasoningTraceSummary,
  getTraceStepDetail,
  TRACE_STEP_LABELS,
} from "./reasoning-trace";
import type { AskResponse } from "./workspace-model";


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
  onOpenKnowledgeGraph,
}: {
  reference: AnswerReference;
  onOpenKnowledgeGraph: (objectId?: string) => void;
}) {
  const objectType = reference.anchor?.object_type || "";
  const title = referenceTitle(reference);
  const snippet = referenceSnippet(reference);
  const source = referenceSource(reference);
  const location = referenceLocation(reference);
  const tier = reference.anchor?.tier || "";
  const isRelationReference = objectType === "relation";
  const canLocateInGraph = Boolean(reference.anchor?.object_id) && !isRelationReference;
  return (
    <aside className="cite-detail-card" aria-live="polite">
      <div className="cite-detail-head">
        <strong>{reference.displayLabel}</strong>
        {objectType && <span><KgTypeMark type={objectType} />{kgTypeLabel(objectType)}</span>}
        {tier && (
          <span
            className={`tier-badge tier-${tier}`}
            title={tier === "base" ? "来自基准库（权威参考层）" : "来自个人层"}
          >
            {tier === "base" ? "base" : "personal"}
          </span>
        )}
        <button
          type="button"
          onClick={() => onOpenKnowledgeGraph(reference.anchor?.object_id)}
          disabled={!canLocateInGraph}
          title={
            isRelationReference
              ? "关系引用绑定的是边证据，不是知识节点，无法在知识图谱中定位"
              : reference.anchor?.object_id
                ? "在知识图谱中定位"
                : "该引用没有绑定知识节点"
          }
        >
          <ExternalLink size={14} />
          {isRelationReference ? "关系证据不可定位" : "知识图谱"}
        </button>
      </div>
      <h4><LatexText text={title} isFormula={objectType === "formula"} /></h4>
      {snippet && <p><LatexText text={snippet} /></p>}
      {(source || location) && <small>{[source, location].filter(Boolean).join(" · ")}</small>}
    </aside>
  );
}


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
    <div
      ref={ref}
      className="cite-popover"
      role="dialog"
      style={{ position: "fixed", top: pos.top, left: pos.left }}
    >
      <SelectedReferenceDetail reference={reference} onOpenKnowledgeGraph={onOpenKnowledgeGraph} />
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


export function AnswerView({
  answer,
  feedbackSent,
  onFeedback,
  onOpenKnowledgeGraph,
  notebookId,
  onBuildScaleIndex,
  buildingScaleIndex,
  onSaveMemory,
  memorySaved,
}: {
  answer: AskResponse;
  feedbackSent: string;
  onFeedback: (rating: "useful" | "not_useful") => void;
  onOpenKnowledgeGraph: (objectId?: string) => void;
  notebookId: string | null;
  onBuildScaleIndex: (notebookId: string) => void;
  buildingScaleIndex: boolean;
  onSaveMemory: (answerId: string) => void;
  memorySaved: boolean;
}) {
  const [copied, setCopied] = useState(false);
  const [citePopover, setCitePopover] = useState<{
    reference: AnswerReference;
    rect: DOMRect;
  } | null>(null);
  const answerText = answer.answer || answer.conclusion || "";
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
    await copyTextToClipboard(renderTextWithReferenceNumbers(answerText, references));
    setCopied(true);
  }

  return (
    <div className="chat-answer">
      {answer.model_errors && answer.model_errors.length > 0 && (() => {
        const labelOf = (stage: string) => ({
          embed: "向量模型",
          rerank: "重排模型",
          answer: "答案模型",
          rewrite: "改写模型",
        } as Record<string, string>)[stage] ?? stage;
        const names = Array.from(new Set(answer.model_errors.map((error) => labelOf(error.stage)))).join("、");
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
        onReferenceClick={(reference, event) => setCitePopover({
          reference,
          rect: event.currentTarget.getBoundingClientRect(),
        })}
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
          aria-label={memorySaved ? "已保存到 Memory" : "保存到 Memory"}
          className={`answer-memory-save ${memorySaved ? "is-saved" : ""}`}
          disabled={memorySaved}
          onClick={() => onSaveMemory(answer.answer_id)}
          title={memorySaved ? "已保存到 Memory" : "保存到 Memory"}
          type="button"
        >{memorySaved ? <Check size={15} /> : <BookmarkPlus size={15} />}<span>{memorySaved ? "已保存到 Memory" : "保存到 Memory"}</span></button>
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
