/**
 * answer-markdown.tsx
 *
 * 基于 react-markdown 的答案渲染组件，替代原先的自研 parseMarkdownBlocks 方案。
 * 保留 [k\d+] 引用徽章（可点击，点开对应 anchor/citation 详情）和 KaTeX 数学公式渲染。
 */
"use client";

import { createContext, useContext, useMemo, type MouseEvent, type ReactNode } from "react";
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
import { remarkAnswerInference } from "./answer-inference";
import { normalizeInferenceListMarkers } from "./inference-list-markers";
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

type AnswerMarkdownRenderState = Readonly<{
  refsByCitationKey: Record<string, AnswerReference>;
  selectedReferenceId: string | null;
  onReferenceClick: AnswerMarkdownProps["onReferenceClick"];
  renderCitationImages?: AnswerMarkdownProps["renderCitationImages"];
}>;

// The renderer functions are React component *types*, so they must not be recreated on each
// AnswerMarkdown render: replacing them remounts authenticated images and makes them flash.
// Their changing data travels through Context instead of a render-mutated ref. Context publishes
// the new value only with the committed tree, so an interrupted concurrent render cannot make the
// still-visible citation buttons observe callbacks or references that never committed.
const AnswerMarkdownRenderContext = createContext<AnswerMarkdownRenderState | null>(null);

function AnswerMarkdownLink({ href, children }: { href?: string; children?: React.ReactNode }) {
  const state = useContext(AnswerMarkdownRenderContext);
  if (href?.startsWith("cite:")) {
    const reference = state?.refsByCitationKey[href.slice(5)];
    if (reference && state) {
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
}

function AnswerMarkdownPre({ children }: { children?: React.ReactNode }) {
  return <pre className="answer-code">{children}</pre>;
}

function AnswerMarkdownTable({ children }: { children?: React.ReactNode }) {
  return (
    <div className="answer-table-wrap">
      <table className="answer-table">{children}</table>
    </div>
  );
}

function AnswerMarkdownAside({
  node,
  children,
}: {
  node?: { properties?: Record<string, unknown> };
  children?: React.ReactNode;
}) {
  const state = useContext(AnswerMarkdownRenderContext);
  const raw = node?.properties?.[CITATION_IMAGE_SLOT_ATTRIBUTE];
  const items = citationImageSlotItems(raw);
  if (items.length > 0) return state?.renderCitationImages?.(items) ?? null;
  return <aside>{children}</aside>;
}

const ANSWER_MARKDOWN_COMPONENTS = {
  a: AnswerMarkdownLink,
  pre: AnswerMarkdownPre,
  table: AnswerMarkdownTable,
  aside: AnswerMarkdownAside,
} satisfies NonNullable<Parameters<typeof ReactMarkdown>[0]["components"]>;

// 默认参数必须是模块级常量：写成 `anchors = []` 会让省略这两个 prop 的调用方
// （Memory 等纯 Markdown 消费面）每次渲染拿到一个新数组，下面按引用比较的
// useMemo 永远失效，等于整层白做。
const NO_ANCHORS: AnswerAnchorLike[] = [];
const NO_CITATIONS: CitationLike[] = [];

export function AnswerMarkdown({
  answer,
  anchors = NO_ANCHORS,
  citations = NO_CITATIONS,
  selectedReferenceId = null,
  onReferenceClick,
  renderCitationImages,
  citationImageOrder,
}: AnswerMarkdownProps) {
  // 构建 key→reference 映射，供 remarkCitations 插件和 <a> 组件使用
  const references = useMemo(
    () => buildAnswerReferences(answer, anchors, citations),
    [answer, anchors, citations],
  );
  const refsByCitationKey = useMemo(() => referenceByCitationKey(references), [references]);
  const imageIdsByCitationKey: CitationImageIdsByKey = useMemo(() => Object.fromEntries(
    Object.entries(refsByCitationKey).map(([key, reference]) => [
      key,
      (reference.anchor?.images ?? reference.citation?.images ?? []).map((image) => image.asset_id),
    ]),
  ), [refsByCitationKey]);

  // Ask 输入框每敲一个键都会让 page.tsx 整页重渲染一次，波及每一轮历史回答。
  // 模块级 components（上文）已保证重渲染不换型、不重挂载；这里更进一步：answer/
  // anchors/citations 没变时直接复用上一次的 ReactMarkdown 元素，让 React 整棵
  // 子树 bail out——remark+KaTeX+rehype 解析是 O(全会话内容) 的开销，没理由跟着
  // 每个按键重跑。选中态、点击回调、图片渲染端口刻意不进依赖：它们经由上面的
  // Context 送达，Provider value 的变化会穿透被跳过的子树到达徽章/图片槽消费者，
  // 所以引用高亮与点击仍然即时。citationImageOrder 的记账随解析走：解析被跳过时
  // 账本保持原样（内容没变，账也不该变），依赖变化重跑时由插件清空重记。
  const markdownTree = useMemo(() => (
    <ReactMarkdown
      remarkPlugins={[
        remarkGfmPlugin,
        remarkMath,
        [remarkCitations, refsByCitationKey] as [typeof remarkCitations, Record<string, AnswerReference>],
        remarkAnswerInference,
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
      components={ANSWER_MARKDOWN_COMPONENTS}
    >
      {normalizeInferenceListMarkers(normalizeMathMarkdown(answer))}
    </ReactMarkdown>
  ), [answer, refsByCitationKey, imageIdsByCitationKey, citationImageOrder]);

  return (
    <div className="answer-markdown">
      <AnswerMarkdownRenderContext.Provider value={{
        refsByCitationKey,
        selectedReferenceId,
        onReferenceClick,
        renderCitationImages,
      }}>
        {markdownTree}
      </AnswerMarkdownRenderContext.Provider>
    </div>
  );
}
