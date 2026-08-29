/**
 * answer-markdown.tsx
 *
 * 基于 react-markdown 的答案渲染组件，替代原先的自研 parseMarkdownBlocks 方案。
 * 保留 [k\d+] 引用徽章（可点击，点开对应 anchor/citation 详情）和 KaTeX 数学公式渲染。
 */
"use client";

import { useRef, type MouseEvent, type ReactNode } from "react";
import ReactMarkdown, { defaultUrlTransform } from "react-markdown";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import "katex/dist/katex.min.css";
import {
  buildAnswerReferences,
  referenceByCitationKey,
  type AnswerAnchorLike,
  type CitationLike,
  type AnswerReference,
} from "./answer-formatting";
import { remarkCitations } from "./answer-citations";
import { remarkGfmPlugin } from "./markdown-gfm";
import { normalizeMathMarkdown } from "./math-markdown";
import {
  CITATION_IMAGE_SLOT_ATTRIBUTE,
  citationImageSlotItems,
  rehypeCitationImages,
  type CitationImageIdsByKey,
  type CitationImageOrder,
  type CitationImageSlotItem,
} from "./rehype-citation-images";

// Re-export types for external callers (page.tsx uses these).
export type { AnswerReference };

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
  /** 引用图片的块级渲染端口。缺省时不插入图片区（Memory 等纯 Markdown 消费面）。 */
  renderCitationImages?: (items: CitationImageSlotItem[]) => ReactNode;
  /** 记账本：本次渲染实际插进正文的图片条目按正文顺序写进它，供放大预览的左右切换
   *  取用（见 rehype-citation-images.ts 里 CitationImageOrder 的完整理由）。 */
  citationImageOrder?: CitationImageOrder;
}

export function AnswerMarkdown({
  answer,
  anchors = [],
  citations = [],
  selectedReferenceId = null,
  onReferenceClick,
  renderCitationImages,
  citationImageOrder,
}: AnswerMarkdownProps) {
  // 构建 key→reference 映射，供 remarkCitations 插件和 <a> 组件使用
  const references = buildAnswerReferences(answer, anchors, citations);
  const refsByCitationKey = referenceByCitationKey(references);
  const imageIdsByCitationKey: CitationImageIdsByKey = Object.fromEntries(
    Object.entries(refsByCitationKey).map(([key, reference]) => [
      key,
      (reference.anchor?.images ?? reference.citation?.images ?? []).map((image) => image.asset_id),
    ]),
  );

  // React treats the functions in `components` as component *types*. Rebuilding those
  // functions on every AnswerMarkdown render therefore unmounts and remounts every
  // customized subtree, even when the answer itself did not change. In particular, an
  // unrelated parent update (such as typing in the next Ask input) used to replace the
  // citation-image <aside>, which made AuthedImage revoke its object URL, show loading,
  // and fetch the same asset again: the visible image flashed on every keystroke.
  //
  // Keep the component types stable for this AnswerMarkdown instance, while reading the
  // latest render data through a ref. Citation selection/callbacks can still change
  // immediately without turning those ordinary updates into subtree replacement.
  const renderStateRef = useRef({
    refsByCitationKey,
    selectedReferenceId,
    onReferenceClick,
    renderCitationImages,
  });
  renderStateRef.current = {
    refsByCitationKey,
    selectedReferenceId,
    onReferenceClick,
    renderCitationImages,
  };

  // -----------------------------------------------------------------------
  // 自定义 <a> 渲染：cite: 开头 → 引用徽章；其余 → 普通新标签链接
  // -----------------------------------------------------------------------
  const componentsRef = useRef<Parameters<typeof ReactMarkdown>[0]["components"] | null>(null);
  if (componentsRef.current === null) {
    componentsRef.current = {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      a({ href, children }: { href?: string; children?: React.ReactNode }) {
        if (href?.startsWith("cite:")) {
          const key = href.slice(5);
          const state = renderStateRef.current;
          const reference = state.refsByCitationKey[key];
          if (reference) {
            const isSelected = state.selectedReferenceId === reference.id;
            return (
              <span className="cite-chip-wrap">
                <button
                  type="button"
                  aria-expanded={isSelected}
                  className={`cite-chip${isSelected ? " active" : ""}`}
                  onClick={(event) => state.onReferenceClick(reference, event)}
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
      aside({ node, children }: { node?: { properties?: Record<string, unknown> }; children?: React.ReactNode }) {
        const raw = node?.properties?.[CITATION_IMAGE_SLOT_ATTRIBUTE];
        const items = citationImageSlotItems(raw);
        if (items.length > 0) return renderStateRef.current.renderCitationImages?.(items) ?? null;
        return <aside>{children}</aside>;
      },
    };
  }
  const components = componentsRef.current;

  return (
    <div className="answer-markdown">
      <ReactMarkdown
        remarkPlugins={[
          remarkGfmPlugin,
          remarkMath,
          [remarkCitations, refsByCitationKey] as [typeof remarkCitations, Record<string, AnswerReference>],
        ]}
        rehypePlugins={[
          rehypeKatex,
          [rehypeCitationImages, imageIdsByCitationKey, citationImageOrder] as [
            typeof rehypeCitationImages, CitationImageIdsByKey, CitationImageOrder | undefined,
          ],
        ]}
        // 默认 urlTransform 会清掉非常规协议（含我们的 cite:），导致引用徽章 href 丢失。
        // 放行 cite:，其余 URL 仍走默认清洗（防 javascript: 等不安全协议）。
        urlTransform={(url) => (url.startsWith("cite:") ? url : defaultUrlTransform(url))}
        components={components}
      >
        {normalizeMathMarkdown(answer)}
      </ReactMarkdown>
    </div>
  );
}
