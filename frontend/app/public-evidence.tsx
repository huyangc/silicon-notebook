"use client";

import { useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import "katex/dist/katex.min.css";
import { remarkGfmPlugin } from "./markdown-gfm";
import { normalizeMathMarkdown } from "./math-markdown";
import type { PublicEvidenceT } from "./public-report";
import { visit, SKIP } from "unist-util-visit";
import type { Root, Text, PhrasingContent } from "mdast";

function remarkQuotedPassage(quote: string) {
  return (tree: Root) => {
    if (!quote) return;
    visit(tree, "text", (node: Text, index, parent) => {
      if (!parent || index === undefined) return;
      const start = node.value.indexOf(quote);
      if (start < 0) return;
      const parts: PhrasingContent[] = [];
      if (start) parts.push({ type: "text", value: node.value.slice(0, start) });
      parts.push({ type: "strong", data: { hName: "mark" }, children: [{ type: "text", value: quote }] });
      if (start + quote.length < node.value.length) parts.push({ type: "text", value: node.value.slice(start + quote.length) });
      parent.children.splice(index, 1, ...parts);
      return [SKIP, index + parts.length];
    });
  };
}

/** A frozen public excerpt, never a request into the author's private source. */
export function PublicEvidence({ evidence, isMemory = false }: { evidence?: PublicEvidenceT; isMemory?: boolean }) {
  const [expanded, setExpanded] = useState(false);
  if (!evidence?.text) return <p className="public-evidence-note">{isMemory ? "此分享未保留个人记忆上下文。" : "原文已不可用"}</p>;
  const label = evidence.kind === "memory" ? "作者的个人记忆"
    : evidence.kind === "knowledge" ? "知识解释" : "原文";
  return <section className="public-evidence">
    <button type="button" className="link-button inline-action" aria-expanded={expanded}
      onClick={() => setExpanded((value) => !value)}>{expanded ? `收起${label}` : `查看${label}`}</button>
    {expanded && <>
      {evidence.location && <p className="public-evidence-note">{evidence.location}</p>}
      <div className="public-evidence-context">
        <ReactMarkdown remarkPlugins={[remarkGfmPlugin, remarkMath, [remarkQuotedPassage, evidence.quote]]} rehypePlugins={[rehypeKatex]}
          components={{ img: ({ alt }) => <span>{alt || "图片"}</span>,
            a: ({ children }) => <span>{children}</span> }}>
          {normalizeMathMarkdown(evidence.text)}
        </ReactMarkdown>
      </div>
      {evidence.truncated && <p className="public-evidence-note">上下文过长，此处仅展示部分内容。</p>}
    </>}
  </section>;
}
