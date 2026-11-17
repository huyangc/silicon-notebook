"use client";

import { useEffect, useRef, useState } from "react";
import { ArrowLeft, BookOpen, Loader2 } from "lucide-react";
import { getToken, subscribeTokenChanges } from "./auth-session";
import { reloadPage } from "./page-reload";
import { getNotebookSource, getNotebookSourceElementsPage } from "./source-api";
import { notebookHash } from "./memory-model";
import { SourcePreview } from "./source-preview";
import type { SourceElement, SourceSummary } from "./workspace-model";

// Same bounded page size as the notebook source detail; no eager full-document read.
const READER_PAGE_SIZE = 40;

export function SourceReader({ notebookId, sourceId }: { notebookId: string; sourceId: string }) {
  const [source, setSource] = useState<SourceSummary | null>(null);
  const [elements, setElements] = useState<SourceElement[]>([]);
  const [total, setTotal] = useState(0);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState("");
  const [signedOut, setSignedOut] = useState(false);
  const [revision, setRevision] = useState(0);
  const inFlight = useRef(false);
  const generation = useRef(0);
  const backUrl = `/${notebookHash(notebookId, sourceId)}`;

  useEffect(() => {
    const owner = ++generation.current;
    let alive = true;
    inFlight.current = true;
    setBusy(true);
    setError("");
    setSignedOut(false);
    setSource(null);
    setElements([]);
    const unsubscribe = subscribeTokenChanges(() => {
      generation.current = owner + 1;
      setSource(null);
      setElements([]);
      reloadPage();
    });
    if (!getToken()) {
      setSignedOut(true);
      setBusy(false);
      inFlight.current = false;
      return () => { alive = false; unsubscribe(); generation.current = owner + 1; };
    }
    const token = getToken();
    Promise.all([
      getNotebookSource(notebookId, sourceId),
      getNotebookSourceElementsPage(notebookId, sourceId, 0, READER_PAGE_SIZE),
    ]).then(([detail, page]) => {
      if (!alive || owner !== generation.current || token !== getToken()) return;
      setSource(detail);
      setElements(page.items);
      setTotal(page.total_count);
    }).catch(() => {
      if (alive && owner === generation.current) setError("来源暂时无法读取，请重试或返回笔记本确认访问权限。");
    }).finally(() => {
      if (alive && owner === generation.current) { setBusy(false); inFlight.current = false; }
    });
    return () => { alive = false; generation.current = owner + 1; unsubscribe(); };
  }, [notebookId, sourceId, revision]);

  async function loadMore() {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    setError("");
    const owner = generation.current;
    const token = getToken();
    try {
      const page = await getNotebookSourceElementsPage(notebookId, sourceId, elements.length, READER_PAGE_SIZE);
      if (owner !== generation.current || token !== getToken()) return;
      setElements((previous) => [...previous, ...page.items]);
      setTotal(page.total_count);
    } catch {
      if (owner === generation.current) setError("后续内容加载失败，请重试。");
    } finally {
      if (owner === generation.current) { setBusy(false); inFlight.current = false; }
    }
  }

  return (
    <main className="source-reader">
      <nav className="source-reader-bar" aria-label="阅读导航">
        <a className="button secondary" href={backUrl}><ArrowLeft size={16} aria-hidden="true" />返回笔记本</a>
        <span><BookOpen size={16} aria-hidden="true" />全屏阅读</span>
      </nav>
      <header className="source-reader-heading">
        <p className="public-report-eyebrow">来源 · 阅读预览</p>
        <h1>{source?.title || "来源预览"}</h1>
        {source && <p className="source-reader-meta">已显示 {elements.length} / {total} 个元素</p>}
      </header>
      <section className="source-reader-paper" aria-label="来源正文" aria-busy={busy}>
        {signedOut ? <p>请先<a className="link-button inline-action" href={backUrl}>登录并返回笔记本</a>查看此来源。</p> : <>
          <div className="source-element-stack"><SourcePreview elements={elements} notebookId={notebookId} /></div>
          {busy && <p className="source-reader-status" role="status"><Loader2 className="busy-spin" size={18} aria-hidden="true" />正在加载内容…</p>}
          {error && <div className="source-reader-status" role="alert">{error}
            <button type="button" className="button secondary" disabled={busy}
              onClick={() => source ? void loadMore() : setRevision((value) => value + 1)}>重试</button>
          </div>}
          {source && !busy && !error && elements.length === 0 && <p className="source-reader-status">
            {source.parse_failed || source.parse_status === "failed" ? "这个来源没能解析成功，请返回笔记本查看。" : "当前来源还没有解析出内容，请解析完成后再打开。"}
          </p>}
          {source && elements.length < total && !error && <div className="source-reader-footer">
            <button type="button" className="button secondary" disabled={busy} onClick={() => void loadMore()}>
              {busy ? "加载中…" : "继续加载"}
            </button>
            <span>已显示 {elements.length} / {total} 个元素</span>
          </div>}
          {source && total > 0 && elements.length >= total && <p className="source-reader-end">已显示全部内容</p>}
        </>}
      </section>
    </main>
  );
}
