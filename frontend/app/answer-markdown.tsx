/**
 * answer-markdown.tsx
 *
 * 基于 react-markdown 的答案渲染组件，替代原先的自研 parseMarkdownBlocks 方案。
 * 保留 [k\d+] 引用徽章（可点击，点开对应 anchor/citation 详情）和 KaTeX 数学公式渲染。
 */
"use client";

import { MouseEvent } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import "katex/dist/katex.min.css";
import { visit } from "unist-util-visit";
import type { Root, Text, Link, PhrasingContent } from "mdast";
import {
  buildAnswerReferences,
  referenceByAnchorKey,
  type AnswerAnchorLike,
  type CitationLike,
  type AnswerReference,
} from "./answer-formatting";

// Re-export types for external callers (page.tsx uses these).
export type { AnswerReference };

// ---------------------------------------------------------------------------
// remarkCitations — remark 插件：把 [k\d+] 替换为 cite: 链接节点
// ---------------------------------------------------------------------------

/**
 * 创建 remark 插件，将 text 节点中的 [k\d+] 标记替换为 `link` 节点，
 * `url` 设置为 `cite:<key>`，供后续 <a> 组件识别为引用徽章。
 *
 * @param refsByKey key→AnswerReference 的映射，用于取显示编号；
 *                  未命中的 key 原样保留为文本节点。
 */
function remarkCitations(refsByKey: Record<string, AnswerReference>) {
  return () => (tree: Root) => {
    visit(tree, "text", (node: Text, index, parent) => {
      if (!parent || index === undefined) return;
      const pattern = /\[(k\d+)\]/g;
      const text = node.value;
      if (!pattern.test(text)) return;

      // 重置 lastIndex（test() 会改变它）
      pattern.lastIndex = 0;

      const newChildren: PhrasingContent[] = [];
      let last = 0;
      let match: RegExpExecArray | null;

      while ((match = pattern.exec(text)) !== null) {
        const key = match[1];
        const ref = refsByKey[key];

        // 前面的纯文本
        if (match.index > last) {
          newChildren.push({ type: "text", value: text.slice(last, match.index) });
        }

        if (ref) {
          // 替换为 link 节点，url = "cite:<key>"
          const linkNode: Link = {
            type: "link",
            url: `cite:${key}`,
            children: [{ type: "text", value: ref.displayLabel }],
          };
          newChildren.push(linkNode);
        } else {
          // key 不在引用映射里 → 原样保留文本
          newChildren.push({ type: "text", value: match[0] });
        }

        last = match.index + match[0].length;
      }

      // 尾部剩余文本
      if (last < text.length) {
        newChildren.push({ type: "text", value: text.slice(last) });
      }

      if (newChildren.length > 0) {
        parent.children.splice(index, 1, ...newChildren);
        // 返回跳过偏移，避免重复访问新插入的节点
        return [undefined, index + newChildren.length] as const;
      }
    });
  };
}

// ---------------------------------------------------------------------------
// AnswerMarkdown component
// ---------------------------------------------------------------------------

export interface AnswerMarkdownProps {
  /** 原始答案文本（含 markdown 和 [k\d+] 引用标记） */
  answer: string;
  /** 答案中引用的 anchor 列表 */
  anchors?: AnswerAnchorLike[];
  /** 答案中引用的 citation 列表 */
  citations?: CitationLike[];
  /** 当前已选中的引用 ID（高亮徽章用） */
  selectedReferenceId?: string | null;
  /** 点击引用徽章时的回调，传入 AnswerReference */
  onReferenceClick: (reference: AnswerReference, event: MouseEvent<HTMLButtonElement>) => void;
}

export function AnswerMarkdown({
  answer,
  anchors = [],
  citations = [],
  selectedReferenceId = null,
  onReferenceClick,
}: AnswerMarkdownProps) {
  // 构建 key→reference 映射，供 remarkCitations 插件和 <a> 组件使用
  const references = buildAnswerReferences(answer, anchors, citations);
  const refsByKey = referenceByAnchorKey(references);

  // -----------------------------------------------------------------------
  // 自定义 <a> 渲染：cite: 开头 → 引用徽章；其余 → 普通新标签链接
  // -----------------------------------------------------------------------
  const components = {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    a({ href, children }: { href?: string; children?: React.ReactNode }) {
      if (href?.startsWith("cite:")) {
        const key = href.slice(5);
        const reference = refsByKey[key];
        if (reference) {
          const isSelected = selectedReferenceId === reference.id;
          return (
            <span className="cite-chip-wrap">
              <button
                type="button"
                aria-expanded={isSelected}
                className={`cite-chip${isSelected ? " active" : ""}`}
                onClick={(event) => onReferenceClick(reference, event)}
              >
                {children}
              </button>
            </span>
          );
        }
        // key 不在映射里，fallback 为文本
        return <span>{children}</span>;
      }
      // 普通外链
      return (
        <a href={href} target="_blank" rel="noreferrer">
          {children}
        </a>
      );
    },
    // 给代码块加现有样式 class，保持外观一致
    pre({ children }: { children?: React.ReactNode }) {
      return <pre className="answer-code">{children}</pre>;
    },
    // 表格外层包装
    table({ children }: { children?: React.ReactNode }) {
      return (
        <div className="answer-table-wrap">
          <table className="answer-table">{children}</table>
        </div>
      );
    },
  } as Parameters<typeof ReactMarkdown>[0]["components"];

  return (
    <div className="answer-markdown">
      <ReactMarkdown
        remarkPlugins={[
          remarkGfm,
          remarkMath,
          [remarkCitations, refsByKey] as [typeof remarkCitations, Record<string, AnswerReference>],
        ]}
        rehypePlugins={[rehypeKatex]}
        components={components}
      >
        {answer}
      </ReactMarkdown>
    </div>
  );
}
