"use client";

import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { ArrowUp, BookOpen, Check, ChevronRight, Copy, FileText, Layers3, LoaderCircle, MessageSquare, PanelLeft, Plus, Square, X } from "lucide-react";
import { PageHeader } from "../components/PageHeader";
import { AnswerMarkdown, type AnswerReference } from "../answer-markdown";
import { buildAnswerReferences } from "../answer-formatting";
import { ChatAnswer } from "../chat-answer";
import { ChatQuestion } from "../chat-question";
import { askQuestionLimitHint } from "../ask-api";
import { toUserMessage } from "../errors";
import { getGlobalCitation, type GlobalJob } from "../global-ask-api";
import type { SourceElement } from "../workspace-model";
import { GlobalConversationList } from "./conversation-list";
import { NotebookScopePicker } from "./notebook-scope-picker";
import { GlobalCoverageReceipt } from "./global-coverage-receipt";
import { useGlobalAsk } from "./use-global-ask";
import { useCopyResult } from "../copy-result";
import { notebookHash } from "../memory-model";
import "./global-ask.css";

type EvidenceSelection = { jobId: string; reference: AnswerReference };
const suggestions = [
  { title: "汇总一个主题", question: "围绕同一个主题，各笔记本有哪些关键结论？", icon: Layers3 },
  { title: "比较不同观点", question: "资料中有哪些相互补充或存在分歧的观点？请标明来源。", icon: BookOpen },
  { title: "寻找相关证据", question: "这个问题有哪些原文证据支持，还有哪些信息需要补充？", icon: FileText },
];

export default function GlobalAskWorkspace({ compact = false, embedded = false, active = true, controls, onOpenNotebook }: { compact?: boolean; embedded?: boolean; active?: boolean; controls?: ReactNode; onOpenNotebook?: () => void }) {
  const ask = useGlobalAsk({ syncUrl: !embedded, active });
  const [historyOpen, setHistoryOpen] = useState(false);
  const [selected, setSelected] = useState<EvidenceSelection | null>(null);
  const [element, setElement] = useState<SourceElement | null>(null);
  const [evidenceError, setEvidenceError] = useState("");
  const [evidenceRevision, setEvidenceRevision] = useState(0);
  const copyResult = useCopyResult();
  const [copying, setCopying] = useState("");
  const copyFlight = useRef<number | null>(null);
  const copyOwner = useRef(0);
  const composer = useRef<HTMLTextAreaElement>(null);
  const reader = useRef<HTMLElement>(null);
  const transcript = useRef<HTMLDivElement>(null);
  const names = new Map(ask.notebooks.map((notebook) => [notebook.id, notebook.name]));
  const busy = ask.loading || ask.opening || ask.submitting || ask.stopping;
  const composerDisabled = busy || ask.openFailed;
  const hint = askQuestionLimitHint(ask.draft.trim());
  const selectedValue = selected?.reference.anchor ?? selected?.reference.citation;
  const selectedNotebookIds = ask.scope.mode === "include" ? new Set(ask.scope.notebook_ids) : null;
  const excludesPreviousContext = selectedNotebookIds !== null
    && ask.turns.some((turn) => turn.resolved_notebook_ids.some((id) => !selectedNotebookIds.has(id)));

  useEffect(() => { setSelected(null); }, [ask.conversationId]);
  useLayoutEffect(() => {
    ++copyOwner.current;
    copyFlight.current = null;
    copyResult.reset();
    setCopying("");
    return () => { ++copyOwner.current; };
  }, [ask.conversationId, copyResult.reset]);
  useEffect(() => {
    if (!selected) return;
    let disposed = false;
    setElement(null);
    setEvidenceError("");
    reader.current?.focus();
    const id = selected.reference.anchor?.element_id ?? selected.reference.citation?.element_id;
    if (!id) { setEvidenceError("这条引用暂时无法定位原文，请重新提问。"); return; }
    void getGlobalCitation(selected.jobId, id).then((value) => {
      if (!disposed) setElement(value);
    }).catch((cause) => {
      if (!disposed) setEvidenceError(toUserMessage(cause, "原文加载失败，请重试"));
    });
    return () => { disposed = true; };
  }, [selected, evidenceRevision]);
  useEffect(() => {
    if (ask.running) transcript.current?.scrollTo({ top: transcript.current.scrollHeight, behavior: "smooth" });
  }, [ask.running?.job_id]);

  async function copyAnswer(job: GlobalJob) {
    if (copyFlight.current !== null) return;
    const ticket = copyOwner.current;
    copyFlight.current = ticket;
    setCopying(job.job_id);
    try {
      await navigator.clipboard.writeText(job.response?.answer ?? "");
      if (ticket === copyOwner.current) copyResult.report(job.job_id, true);
    } catch {
      if (ticket === copyOwner.current) copyResult.report(job.job_id, false);
    } finally {
      if (copyFlight.current === ticket) copyFlight.current = null;
      if (ticket === copyOwner.current) setCopying("");
    }
  }

  return <div className={`global-ask-page${compact ? " compact" : ""}`}>
    {embedded ? <header className="global-window-heading"><div><span className="global-window-mark"><MessageSquare size={19} /></span><div><strong>全局问答</strong><small>连接你的所有笔记本</small></div></div><div className="global-window-controls">{controls}</div></header> : <PageHeader title="全局问答" />}
    <div className="global-mobile-toolbar">
      <button className="sort-button" onClick={() => setHistoryOpen(!historyOpen)} aria-expanded={historyOpen}><PanelLeft size={16} />历史对话</button>
      <button className="sort-button" disabled={busy} onClick={() => { ask.newConversation(); setHistoryOpen(false); }}><Plus size={16} />新建对话</button>
    </div>
    <div className={`global-layout${selected ? " has-evidence" : ""}`}>
      <div className={`global-history-wrap${historyOpen ? " mobile-open" : ""}`}>
        <GlobalConversationList items={ask.conversations} activeId={ask.conversationId} disabled={busy}
          more={ask.moreHistory} loadingMore={ask.loadingHistory} error={ask.historyError}
          onLoadMore={() => void ask.loadMoreHistory()} onOpen={(id) => { void ask.openConversation(id); setHistoryOpen(false); }}
          onNew={() => { ask.newConversation(); setHistoryOpen(false); }} onUpdated={ask.updateConversation} onDeleted={ask.removeConversation} />
      </div>
      <main className="global-main">
        <header className="global-main-heading"><div><MessageSquare size={18} /><h1>全局问答</h1></div><span>连接资料，找到答案</span></header>
        <div className="global-transcript" ref={transcript} aria-label="问答内容" aria-busy={ask.loading || ask.opening}>
          {ask.loading || ask.opening ? <div className="global-loading" role="status"><LoaderCircle className="global-spin" size={22} />正在加载对话…</div> : !ask.turns.length ?
            <div className="global-welcome">
              <div className="global-welcome-mark"><Layers3 size={30} strokeWidth={1.5} /></div>
              <p className="global-eyebrow">让知识彼此连接</p>
              <h2>{compact ? "让笔记本一起回答。" : <>一个问题，<br />连接你的所有笔记本。</>}</h2>
              <p className="global-welcome-description">从不同资料中汇总结论、比较观点、寻找证据。<br />默认检索全部笔记本，也可以选择本次提问的范围。</p>
              <div className="global-suggestions">{suggestions.map(({ title, question, icon: Icon }) => <button key={title} disabled={busy} onClick={() => { ask.setDraft(question); composer.current?.focus(); }}><Icon size={19} /><strong>{title}</strong><ChevronRight size={14} /></button>)}</div>
            </div> : <div className="global-turns">
              {ask.turnOffset !== null && <button className="sort-button global-older" disabled={ask.loadingTurns} onClick={() => void ask.loadMoreTurns()}>{ask.loadingTurns ? "正在加载…" : "加载更早的问答"}</button>}
              {ask.turns.map((job) => <section key={job.job_id} className="global-turn">
                <ChatQuestion question={job.question} askedAt={job.created_at} />
                <div className="global-turn-scope"><BookOpen size={12} />{job.notebook_scope.mode === "all" ? "全部笔记本" : "指定笔记本"} · {job.resolved_notebook_ids.length} 个</div>
                {job.response ? <ChatAnswer answeredAt={job.response.created_at}>
                  <div className="global-answer-label"><span className="global-answer-mark">SN</span><strong>综合回答</strong><span>引用来自 {job.cited_notebook_ids.length} 个笔记本</span></div>
                  {job.response.grounded !== true && <p className="global-answer-grounding" role="note">以下回答未得到原文充分支持，请结合引用核对。</p>}
                  <AnswerMarkdown answer={job.response.answer} anchors={job.response.anchors} citations={job.response.citations}
                    selectedReferenceId={selected?.jobId === job.job_id ? selected.reference.id : null}
                    onReferenceClick={(reference) => setSelected({ jobId: job.job_id, reference })} />
                  <div className="global-citation-list">{buildAnswerReferences(job.response.answer, job.response.anchors, job.response.citations).map((reference) => {
                    const value = reference.anchor ?? reference.citation;
                    return <button key={reference.id} className="global-citation" onClick={() => setSelected({ jobId: job.job_id, reference })}>
                      <FileText size={14} /><span>{names.get(value?.notebook_id ?? "") ?? "笔记本"}<small>{reference.anchor?.source_title ?? reference.citation?.label ?? reference.displayLabel}</small></span><ChevronRight size={13} />
                    </button>;
                  })}</div>
                  <footer className="global-answer-footer"><small>{job.response.completeness_notice}</small><button aria-label="复制回答" className={copyResult.resultFor(job.job_id) === "copied" ? "global-text-button copy-result-copied" : copyResult.resultFor(job.job_id) === "failed" ? "global-text-button copy-result-failed" : "global-text-button"} disabled={Boolean(copying)} onClick={() => void copyAnswer(job)}>{copyResult.resultFor(job.job_id) === "copied" ? <Check size={13} /> : <Copy size={13} />}<span role="status">{copying === job.job_id ? "复制中…" : copyResult.resultFor(job.job_id) === "copied" ? "已复制" : copyResult.resultFor(job.job_id) === "failed" ? "复制失败" : "复制"}</span></button></footer>
                </ChatAnswer> : <div className={`global-job-status${job.status === "failed" ? " failed" : ""}`} role="status">
                  {job.status === "running" && <LoaderCircle size={18} className="global-spin" />}
                  <span>{job.status === "running" ? `正在查阅资料 · 已检索 ${job.searched_notebook_ids.length} / ${job.resolved_notebook_ids.length} 个笔记本` : job.status === "cancelled" ? "已停止回答，可以修改问题后继续。" : job.status === "interrupted" ? "服务已重启，请重新提交问题。" : job.error || "回答未完成，请检查模型服务后重试。"}</span>
                  {job.status !== "running" && <button className="global-text-button" onClick={() => { ask.setDraft(job.question); composer.current?.focus(); }}>重新提问</button>}
                </div>}
                <GlobalCoverageReceipt job={job} notebookNames={names} />
              </section>)}
            </div>}
        </div>
        <div className="global-composer-area">
          {ask.error && <div className="global-inline-error" role="alert">{ask.error}<button className="global-text-button" disabled={busy} onClick={() => void ask.load()}>重新加载</button></div>}
          {ask.pollError && <div className="global-inline-error" role="alert">{ask.pollError}<button className="global-text-button" onClick={ask.retryPoll}>重新连接</button></div>}
          <form className="global-composer" onSubmit={(event) => { event.preventDefault(); void ask.submit(); }}>
            <textarea ref={composer} aria-label="输入问题" placeholder={ask.turns.length ? "继续追问，或选择新的笔记本范围…" : "向你的笔记本提问…"} rows={3} value={ask.draft} disabled={composerDisabled || Boolean(ask.running)} onChange={(event) => ask.setDraft(event.target.value)} onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); void ask.submit(); }
            }} />
            <div className="global-composer-toolbar"><NotebookScopePicker notebooks={ask.notebooks} scope={ask.scope} onChange={ask.setScope} disabled={composerDisabled || Boolean(ask.running)} />
              {ask.running ? <button className="global-send new-pill" type="button" disabled={ask.stopping} onClick={() => void ask.stop()}><Square size={14} />{ask.stopping ? "停止中…" : "停止"}</button> : <button className="global-send new-pill" type="submit" disabled={composerDisabled || !ask.draft.trim() || Boolean(hint) || !ask.notebooks.length} aria-label="发送问题">{ask.submitting ? <LoaderCircle className="global-spin" size={18} /> : <ArrowUp size={19} />}</button>}
            </div>
            {excludesPreviousContext && <p className="global-scope-context-notice" role="status">范围已收窄：先前涉及其他笔记本的提问不会用于本次追问，请重新说明要讨论的对象。</p>}
          </form>
          <p className={`global-composer-hint${hint ? " invalid" : ""}`}>{hint || (ask.notebooks.length ? "答案来自所选范围的原文 · Enter 发送，Shift + Enter 换行" : "还没有可访问的笔记本，请先返回主页添加资料。")}</p>
        </div>
      </main>
      {selected && <aside className="global-evidence" ref={reader} tabIndex={-1} aria-label="引用原文" onKeyDown={(event) => { if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); setSelected(null); } }}>
        <div className="global-evidence-heading"><span><FileText size={17} />引用原文</span><button className="icon-button" aria-label="关闭引用原文" onClick={() => setSelected(null)}><X size={18} /></button></div>
        <div className="global-evidence-body"><p className="global-evidence-notebook"><BookOpen size={14} />{names.get(selectedValue?.notebook_id ?? "") ?? "来源笔记本"}</p>
          <h2>{selected.reference.anchor?.source_title ?? selected.reference.citation?.label ?? "引用来源"}</h2>
          <small>{element?.location_label ?? selectedValue?.location_label}</small>
          {element && selectedValue?.notebook_id && <a className="global-original-link" href={`/${notebookHash(selectedValue.notebook_id, element.source_id)}`} onClick={onOpenNotebook}>在笔记本中打开 <ChevronRight size={13} /></a>}
          {evidenceError ? <div className="global-inline-error" role="alert">{evidenceError}<button className="global-text-button" onClick={() => setEvidenceRevision((value) => value + 1)}>重试</button></div> : element ? <div className="global-original-text">{element.text}</div> : <div className="global-loading" role="status"><LoaderCircle size={18} className="global-spin" />正在读取原文…</div>}
        </div>
      </aside>}
    </div>
  </div>;
}
