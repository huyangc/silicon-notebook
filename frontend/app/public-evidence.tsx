"use client";

import { useId, useState, type ReactNode } from "react";
import { ChevronDown, ChevronUp, FileText } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import "katex/dist/katex.min.css";
import { remarkGfmPlugin } from "./markdown-gfm";
import { normalizeMathMarkdown } from "./math-markdown";
import type { PublicEvidenceT } from "./public-report";
import type { Element, Root, Text } from "hast";

function rehypeQuotedPassage({ source, quote }: { source: string; quote: string }) {
  const spans: Array<{ start: number; end: number }> = [];
  if (quote) {
    let start = source.indexOf(quote);
    while (start >= 0) {
      spans.push({ start, end: start + quote.length });
      start = source.indexOf(quote, start + quote.length);
    }
  }
  const overlaps = (start: number, end: number) => spans.filter((span) => span.start < end && span.end > start);
  const isMath = (node: Element) => Array.isArray(node.properties.className)
    && node.properties.className.some((name) => name === "math-inline" || name === "math-display" || name === "language-math");

  function highlight(parent: Root | Element) {
    for (let index = 0; index < parent.children.length; index++) {
      const node = parent.children[index];
      const start = node.position?.start.offset;
      const end = node.position?.end.offset;
      if (node.type === "element") {
        const math = isMath(node) || (node.tagName === "pre" && node.children.some((child) => child.type === "element" && isMath(child)));
        const literalCode = node.tagName === "pre" || node.tagName === "code";
        if ((math || literalCode) && start !== undefined && end !== undefined && overlaps(start, end).length) {
          // KaTeX replaces its math/code scope; highlight outside it so the
          // marker survives without splitting a formula or flattening blocks.
          // Literal code leaves can lack positions too: keep their code scope.
          const block = node.tagName === "pre" || node.tagName === "div";
          parent.children.splice(index, 1, {
            type: "element", tagName: block ? "div" : "mark",
            properties: block ? { role: "mark", className: ["public-evidence-quoted-block"] } : {},
            children: [node],
          });
        } else highlight(node);
        continue;
      }
      if (node.type !== "text" || start === undefined || end === undefined) continue;
      const matching = overlaps(start, end);
      if (!matching.length) continue;
      // Escapes and entities may decode to a different length. In that case
      // retain the leaf intact and highlight it instead of slicing wrong text.
      const ranges = source.slice(start, end) === node.value
        ? matching.map((span) => ({ start: Math.max(start, span.start) - start, end: Math.min(end, span.end) - start }))
        : [{ start: 0, end: node.value.length }];
      const parts: Array<Element | Text> = [];
      let cursor = 0;
      for (const range of ranges) {
        if (range.start > cursor) parts.push({ type: "text", value: node.value.slice(cursor, range.start) });
        parts.push({ type: "element", tagName: "mark", properties: {}, children: [{ type: "text", value: node.value.slice(range.start, range.end) }] });
        cursor = range.end;
      }
      if (cursor < node.value.length) parts.push({ type: "text", value: node.value.slice(cursor) });
      parent.children.splice(index, 1, ...parts);
      index += parts.length - 1;
    }
  }
  return (tree: Root) => {
    if (spans.length) highlight(tree);
  };
}

/** A frozen public excerpt, never a request into the author's private source. */
export function PublicEvidence({ evidence, isMemory = false, isImageReference = false }: {
  evidence?: PublicEvidenceT; isMemory?: boolean; isImageReference?: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  const contextId = useId();
  if (!evidence?.text) return <p className="public-evidence-note">{isMemory ? "此分享未保留个人记忆上下文。"
    : isImageReference ? "此分享未保留图片说明。" : "原文已不可用"}</p>;
  // Old frozen snapshots can predate the image-description kind. Their public
  // image marker corrects presentation without widening or rewriting evidence.
  const imageDescription = evidence.kind === "image_description" || (isImageReference && evidence.kind === "original");
  const label = evidence.kind === "memory" ? "作者的个人记忆"
    : evidence.kind === "knowledge" ? "知识解释" : imageDescription ? "图片说明" : "原文";
  const markdown = normalizeMathMarkdown(evidence.text);
  return <section className="public-evidence">
    <button type="button" className="cite-detail-action" aria-expanded={expanded} aria-controls={expanded ? contextId : undefined}
      onClick={() => setExpanded((value) => !value)}>
      <FileText size={14} aria-hidden="true" />{expanded ? `收起${label}` : `查看${label}`}
      {expanded ? <ChevronUp size={14} aria-hidden="true" /> : <ChevronDown size={14} aria-hidden="true" />}
    </button>
    {expanded && <>
      {evidence.location && <p className="public-evidence-note">{evidence.location}</p>}
      <div id={contextId} role="region" aria-label={label} className="public-evidence-context cite-detail-context">
        <ReactMarkdown remarkPlugins={[remarkGfmPlugin, remarkMath]} rehypePlugins={[[rehypeQuotedPassage, { source: markdown, quote: normalizeMathMarkdown(evidence.quote) }], rehypeKatex]}
          components={EVIDENCE_COMPONENTS}>
          {markdown}
        </ReactMarkdown>
      </div>
      {evidence.truncated && <p className="public-evidence-note">上下文过长，此处仅展示部分内容。</p>}
    </>}
  </section>;
}

const EVIDENCE_COMPONENTS = {
  img: ({ alt }: { alt?: string }) => <span>{alt || "图片"}</span>,
  a: ({ children }: { children?: ReactNode }) => <span>{children}</span>,
  pre: ({ children }: { children?: ReactNode }) => <pre className="answer-code">{children}</pre>,
  table: ({ children }: { children?: ReactNode }) => <div className="answer-table-wrap"><table className="answer-table">{children}</table></div>,
} satisfies NonNullable<Parameters<typeof ReactMarkdown>[0]["components"]>;
