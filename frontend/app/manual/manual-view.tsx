"use client";

// 站内「使用手册」的渲染件。手册正文由 `page.tsx` 在构建期从 `docs/user-manual_zh.md`
// 读入后整篇交给这里;本组件不发任何请求、不依赖登录态——它和公开分享页一样,是
// 一个不需要登录就能打开的界面。
//
// 渲染管线沿用站内 Markdown 的既有件:GFM 元组走 `remarkGfmPlugin`(单个 `~` 不能
// 被当成删除线,手册里同样会写「2~3 周」这类区间),表格/代码块沿用
// `.answer-table-wrap` / `.answer-code`,宽内容在自己的块里横向滚动。

import type { ReactNode } from "react";
import ReactMarkdown from "react-markdown";

import { remarkGfmPlugin } from "../markdown-gfm";
import { PageHeader } from "../components/PageHeader.tsx";
import { headingSlug, isRepoDocLink } from "./manual-markdown.ts";
import "./manual.css";

function nodeText(node: ReactNode): string {
  if (node == null || typeof node === "boolean") return "";
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(nodeText).join("");
  if (typeof node === "object" && "props" in node) {
    const props = (node as { props?: { children?: ReactNode } }).props;
    return nodeText(props?.children);
  }
  return "";
}

type HeadingProps = { children?: ReactNode };

function heading(level: 1 | 2 | 3 | 4) {
  const Tag = `h${level}` as const;
  return function ManualHeading({ children }: HeadingProps) {
    return <Tag id={headingSlug(nodeText(children))}>{children}</Tag>;
  };
}

const components = {
  h1: heading(1),
  h2: heading(2),
  h3: heading(3),
  h4: heading(4),
  a({ href, children }: { href?: string; children?: ReactNode }) {
    if (isRepoDocLink(href)) return <span className="manual-doc-ref">{children}</span>;
    if (href && /^https?:\/\//i.test(href)) {
      return (
        <a href={href} target="_blank" rel="noreferrer">
          {children}
        </a>
      );
    }
    // react-markdown 会把 href 里的中文百分号编码;站内锚点解码回去,让它与标题上
    // 的 id 逐字相同(浏览器匹配 fragment 时本来也会先解码,这里只是让 DOM 可读)。
    const target = href?.startsWith("#") ? decodeURIComponent(href) : href;
    return <a href={target}>{children}</a>;
  },
  pre({ children }: { children?: ReactNode }) {
    return <pre className="answer-code">{children}</pre>;
  },
  table({ children }: { children?: ReactNode }) {
    return (
      <div className="answer-table-wrap">
        <table className="answer-table">{children}</table>
      </div>
    );
  },
} as Parameters<typeof ReactMarkdown>[0]["components"];

export function ManualView({ markdown }: { markdown: string }) {
  return (
    <main className="manual-page">
      <PageHeader title="使用手册" />
      <article className="manual-body">
        <ReactMarkdown remarkPlugins={[remarkGfmPlugin]} components={components}>
          {markdown}
        </ReactMarkdown>
      </article>
    </main>
  );
}
