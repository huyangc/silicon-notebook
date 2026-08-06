"use client";

// 公开分享的深度报告页 —— 唯一不需要登录的界面。
//
// 它刻意不 import 主应用的任何东西：没有 notebook 上下文、没有 session、
// 没有引用跳转。渲染的就是后端白名单投影里的正文与参考文献。

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";

import { normalizeMathMarkdown } from "../../math-markdown";
import {
  fetchPublicReport,
  publicReferencesByKey,
  type PublicReportT,
} from "../../public-report";

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

  const { report } = state;
  const refsByKey = publicReferencesByKey(report.references);
  return (
    <main className="public-report">
      <header className="public-report-head">
        <h1>{report.question || "深度报告"}</h1>
        {report.updated_at && (
          <p className="public-report-note">
            生成于 <PublicTime value={report.updated_at} />
          </p>
        )}
      </header>

      <article className="report-markdown answer-markdown">
        <ReactMarkdown
          remarkPlugins={[remarkGfm, remarkMath]}
          rehypePlugins={[rehypeKatex]}
        >
          {/* 与已认证的报告视图同一套归一化：单行 $$…$$ 要当块级公式处理，
              网关返回的转义 Markdown 层也要剥掉。少了它，同一份报告在分享页
              和站内页会渲染成两个样子（长公式被当行内并裁掉）。 */}
          {normalizeMathMarkdown(report.content_md)}
        </ReactMarkdown>
      </article>

      {report.references.length > 0 && (
        <section className="public-report-references" aria-label="参考文献">
          <h2>参考文献</h2>
          <ol>
            {report.references.map((reference) => (
              <li key={reference.key} id={`ref-${reference.key}`}>
                <strong>{reference.title || reference.file_name || "(未命名资料)"}</strong>
                {reference.location && <span className="public-report-locus">{reference.location}</span>}
                {reference.file_name && reference.file_name !== reference.title && (
                  <small>原始文件：{reference.file_name}</small>
                )}
                {reference.snippet && <blockquote>{reference.snippet}</blockquote>}
              </li>
            ))}
          </ol>
          {report.truncated_references && (
            <p className="public-report-note">
              参考文献过多，此处只展示前 {report.references.length} 条。
            </p>
          )}
        </section>
      )}

      <footer className="public-report-note">
        本页是只读分享副本，引用可核对但不可打开原始资料。
        {Object.keys(refsByKey).length > 0
          && ` 正文中的 [k] 标记对应上方第 ${Object.keys(refsByKey).length} 条以内的参考文献。`}
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
