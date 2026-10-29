"use client";

// 公开分享的深度报告页 —— 唯一不需要登录的界面。
//
// 它刻意不 import 主应用的**产品层**：没有 notebook 上下文、没有 session、
// 没有引用跳转到原文。渲染的就是后端白名单投影里的正文与引用出处。
//
// 但**渲染管线**必须与站内共用，不能各写一份：
//   * `remarkCitations` 是 `[k]` / 【k】标记链接化的唯一实现（引用标记兼容红线：
//     Ask、报告、前端链接化采用同一套语法口径）；
//   * `katex/dist/katex.min.css` 少了它，rehype-katex 产出的 `.katex-mathml`
//     不会被裁掉 —— MathML 与 HTML 两份同时上屏，公式变成逐字符乱码；
//   * 表格/代码块沿用 `.answer-table-wrap` / `.answer-code`，宽内容才会在自己的
//     内容块里横向滚动，而不是把整页顶宽。
// 这三样都是无 notebook 上下文的纯渲染件，复用它们不破坏这一页的隔离。
//
// 点正文引用标记与站内问答同一个交互：标记旁弹出同一张引用卡
// （`PublicCitationPopover` → `CitationPopover`），不再把读者拽到页底。正文里的
// 「参考文献」一节与站内同一个插件折叠（`rehypeReportReferencesFold`），页底的
// 「引用出处」摘录清单同样默认收起、可展开。

import { createContext, useContext, useEffect, useMemo, useState } from "react";
import { useParams } from "next/navigation";
import ReactMarkdown, { defaultUrlTransform } from "react-markdown";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import "katex/dist/katex.min.css";

import { remarkCitations } from "../../answer-citations";
import { remarkAnswerInference } from "../../answer-inference";
import { normalizeInferenceListMarkers } from "../../inference-list-markers";
import { remarkGfmPlugin } from "../../markdown-gfm";
import { normalizeMathMarkdown } from "../../math-markdown";
import { PublicCitationPopover, type PublicCitePopoverState } from "../../public-citation-popover";
import { rehypeReportReferencesFold } from "../../rehype-report-references-fold";
import {
  fetchPublicReport,
  publicCitationRefs,
  publicReferenceLocation,
  publicReferenceNumber,
  type PublicCitationRefT,
  type PublicReportT,
} from "../../public-report";

type PublicReportRenderState = Readonly<{
  citationRefs: Record<string, PublicCitationRefT>;
  selectedKey: string | null;
  openReference: (key: string, rect: DOMRect) => void;
}>;

// 渲染器是 React 组件*类型*，绝不能每次渲染重建：换型即整棵 markdown 子树卸载重挂，
// 点一次引用（改选中态）就会让正文闪一下、被点的那颗标记也被换掉。会变的状态走
// Context（与 `c/[token]/page.tsx`、`report-view.tsx` 同一做法）。
const PublicReportRenderContext = createContext<PublicReportRenderState | null>(null);

function PublicReportLink({ href, children }: { href?: string; children?: React.ReactNode }) {
  const state = useContext(PublicReportRenderContext);
  if (href?.startsWith("cite:")) {
    const key = href.slice(5);
    if (state?.citationRefs[key]) {
      return (
        <button
          type="button"
          className={`cite-chip${state.selectedKey === key ? " active" : ""}`}
          onClick={(event) => state.openReference(key, event.currentTarget.getBoundingClientRect())}
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
}

function PublicReportPre({ children }: { children?: React.ReactNode }) {
  return <pre className="answer-code">{children}</pre>;
}

function PublicReportTable({ children }: { children?: React.ReactNode }) {
  return (
    <div className="answer-table-wrap">
      <table className="answer-table">{children}</table>
    </div>
  );
}

const PUBLIC_REPORT_COMPONENTS = {
  a: PublicReportLink,
  pre: PublicReportPre,
  table: PublicReportTable,
} satisfies NonNullable<Parameters<typeof ReactMarkdown>[0]["components"]>;

type LoadState =
  | { kind: "loading" }
  | { kind: "missing" }
  | { kind: "error" }
  | { kind: "ready"; report: PublicReportT };

export default function PublicReportPage() {
  const params = useParams<{ token: string }>();
  const token = String(params?.token || "");
  const [state, setState] = useState<LoadState>({ kind: "loading" });

  useEffect(() => {
    let cancelled = false;
    setState({ kind: "loading" });
    fetchPublicReport(token)
      .then((report) => {
        if (cancelled) return;
        setState(report ? { kind: "ready", report } : { kind: "missing" });
      })
      .catch(() => {
        if (!cancelled) setState({ kind: "error" });
      });
    return () => {
      cancelled = true;
    };
  }, [token]);

  if (state.kind === "loading") {
    return <main className="public-report"><p className="public-report-note">正在加载…</p></main>;
  }
  if (state.kind === "missing") {
    return (
      <main className="public-report">
        <h1>链接不可用</h1>
        <p className="public-report-note">
          这个分享链接不存在，或者已被创建者撤销。
        </p>
      </main>
    );
  }
  if (state.kind === "error") {
    return (
      <main className="public-report">
        <h1>暂时打不开</h1>
        <p className="public-report-note">请稍后重试。</p>
      </main>
    );
  }

  return <PublicReportView report={state.report} />;
}

/** 一份已取到的报告。拆成独立组件是为了能用 hooks 记住解析结果（见 markdownTree）。 */
function PublicReportView({ report }: { report: PublicReportT }) {
  // 正文标记点开的那条引用：标记旁弹出引用卡，标记本身高亮。换报告（换 token）时
  // 本组件随加载态卸载，选中态自然清零。
  const [citePopover, setCitePopover] = useState<PublicCitePopoverState | null>(null);
  const selectedKey = citePopover?.key ?? null;
  const citationRefs = useMemo(() => publicCitationRefs(report.references), [report.references]);
  const popoverReference = citePopover
    ? report.references.find((reference) => reference.key === citePopover.key)
    : undefined;
  const renderState = useMemo<PublicReportRenderState>(() => ({
    citationRefs,
    selectedKey,
    openReference: (key, rect) => setCitePopover({ key, rect }),
  }), [citationRefs, selectedKey]);

  // 选中引用只换高亮与浮层；正文没变时复用上一次的 ReactMarkdown 元素，remark+KaTeX
  // 解析不跟着重跑。选中态与点击回调经 Context 穿透被跳过的子树送达标记。
  const markdownTree = useMemo(() => (
    <ReactMarkdown
      remarkPlugins={[
        remarkGfmPlugin,
        remarkMath,
        [remarkCitations, citationRefs] as [typeof remarkCitations, typeof citationRefs],
        remarkAnswerInference,
      ]}
      rehypePlugins={[rehypeKatex, rehypeReportReferencesFold]}
      // 默认 urlTransform 会清掉 cite: 协议 → 引用编号丢失;放行 cite:,
      // 其余仍走默认清洗(防 javascript: 等不安全协议)。
      urlTransform={(url) => (url.startsWith("cite:") ? url : defaultUrlTransform(url))}
      components={PUBLIC_REPORT_COMPONENTS}
    >
      {/* 与已认证的报告视图同一套归一化：单行 $$…$$ 要当块级公式处理，
          网关返回的转义 Markdown 层也要剥掉。少了它，同一份报告在分享页
          和站内页会渲染成两个样子（长公式被当行内并裁掉）。 */}
      {normalizeInferenceListMarkers(normalizeMathMarkdown(report.content_md))}
    </ReactMarkdown>
  ), [report.content_md, citationRefs]);

  return (
    <main className="public-report">
      <header className="public-report-head">
        <p className="public-report-eyebrow">深度报告 · 只读分享</p>
        <h1>{report.question || "深度报告"}</h1>
        {/* 只可能出现在创建期护栏上线之前建的报告上；截断绝不静默。 */}
        {report.question_truncated && (
          <p className="public-report-note public-report-truncated">（研究问题过长，已截断）</p>
        )}
        <p className="public-report-meta">
          {report.updated_at && (
            <span>
              生成于 <PublicTime value={report.updated_at} />
            </span>
          )}
          {report.references.length > 0 && <span>{report.references.length} 条引用出处</span>}
        </p>
      </header>

      <article className="report-markdown answer-markdown">
        <PublicReportRenderContext.Provider value={renderState}>
          {markdownTree}
        </PublicReportRenderContext.Provider>
      </article>

      {citePopover && popoverReference && (
        <PublicCitationPopover
          reference={popoverReference}
          displayLabel={citationRefs[citePopover.key]?.displayLabel || ""}
          anchorRect={citePopover.rect}
          onClose={() => setCitePopover(null)}
        />
      )}

      {report.references.length > 0 && (
        <details className="public-report-references public-references-fold">
          <summary>引用出处 · {report.references.length} 条</summary>
          <p className="public-report-note">
            正文里的编号对应下面的条目。分享副本只保留摘录，不能打开原始资料。
          </p>
          <ol>
            {report.references.map((reference, index) => (
              <li key={reference.key} id={`ref-${reference.key}`}>
                <span className="public-report-refnum" aria-hidden="true">
                  {publicReferenceNumber(reference, index)}
                </span>
                <div className="public-report-refbody">
                  <strong>{reference.title || reference.file_name || "(未命名资料)"}</strong>
                  {reference.title_truncated && (
                    <small className="public-report-truncated">（标题过长，已截断）</small>
                  )}
                  {publicReferenceLocation(reference) && <span className="public-report-locus">{publicReferenceLocation(reference)}</span>}
                  {reference.file_name && reference.file_name !== reference.title && (
                    <small>原始文件：{reference.file_name}</small>
                  )}
                  {reference.file_name_truncated && (
                    <small className="public-report-truncated">（原始文件名过长，已截断）</small>
                  )}
                  {reference.snippet && <blockquote>{reference.snippet}</blockquote>}
                  {reference.snippet_truncated && (
                    <small className="public-report-truncated">（摘录过长，已截断）</small>
                  )}
                </div>
              </li>
            ))}
          </ol>
          {report.truncated_references && (
            <p className="public-report-note">
              引用出处过多，此处只展示前 {report.references.length} 条。
            </p>
          )}
        </details>
      )}

      <footer className="public-report-foot">
        <p className="public-report-note">本页是只读分享副本，引用可核对但不可打开原始资料。</p>
      </footer>
    </main>
  );
}

/** 浏览器本地时区渲染；服务端渲染时先留空，避免 hydration 前后不一致。 */
function PublicTime({ value }: { value: string }) {
  const [text, setText] = useState("");
  useEffect(() => {
    const parsed = new Date(value);
    setText(Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString());
  }, [value]);
  return <>{text}</>;
}
