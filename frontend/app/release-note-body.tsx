"use client";

import type { ReactNode } from "react";
import ReactMarkdown from "react-markdown";

import { remarkGfmPlugin } from "./markdown-gfm";

// 说明里的链接一律新标签页打开:在当前页跳走会丢掉工作区状态(弹窗里 close() 也没机会跑)。
const markdownComponents = {
  a({ href, children }: { href?: string; children?: ReactNode }) {
    return <a href={href} target="_blank" rel="noreferrer">{children}</a>;
  },
} as Parameters<typeof ReactMarkdown>[0]["components"];

/** 更新说明正文的 markdown 渲染,弹窗与更新记录页共用。 */
export function ReleaseNoteBody({ body }: { body: string }) {
  return <ReactMarkdown remarkPlugins={[remarkGfmPlugin]} components={markdownComponents}>{body}</ReactMarkdown>;
}
