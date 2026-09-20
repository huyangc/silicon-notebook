"use client";

import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { ArrowUp, BookOpen, Check, ChevronRight, Copy, FileText, Layers3, LoaderCircle, MessageSquare, PanelLeft, Plus, Square } from "lucide-react";
import { PageHeader } from "../components/PageHeader";
import { AnswerMarkdown, type AnswerReference } from "../answer-markdown";
// 标准答案视图与推理轨迹面板与笔记本内问答**同一份实现**：全局问答直接调单库
// 引擎之后，两个面拿到的就是同一个 `AskResponse`，没有理由再画第二套。
import { AnswerView, ReasoningTracePanel } from "../answer-panel";
import { AskModePicker } from "../ask-mode-picker";
import { AskIntentReview } from "../ask-intent-review";
import { ChatAnswer } from "../chat-answer";
import { ChatQuestion } from "../chat-question";
import { askQuestionLimitHint } from "../ask-api";
import { CitationPopover } from "../citation-card";
import { type GlobalJob } from "../global-ask-api";
import type { UiMode } from "../ui-mode";
import { GlobalConversationList } from "./conversation-list";
import { NotebookScopePicker } from "./notebook-scope-picker";
import { GlobalCoverageReceipt } from "./global-coverage-receipt";
import { useGlobalAsk } from "./use-global-ask";
import { useCopyResult } from "../copy-result";
import { notebookHash } from "../memory-model";
import "./global-ask.css";

/** 当前打开的引用小卡片：哪一轮问答的哪一条引用，以及它那枚行内标记的视口 rect
 *  （`CitationPopover` 按它定位）。jobId 一起记，同一条会话里多轮问答的行内标记
 *  选中高亮才不会串到别的轮次上。 */
type CiteSelection = { jobId: string; reference: AnswerReference; rect: DOMRect };
/** 本轮该由**本组件**渲染的推理轨迹。
 *
 *  完成后的轨迹以 `answer.reasoning_trace` 为准，而那一份 `AnswerView` 自己就会
 *  渲染——这里只补它不渲染的两种情形：还没有答案（运行中逐步追加的 `job.trace`），
 *  以及答案里的轨迹被清空、只剩作业上那一份。两处都挂同一个面板，所以不会出现
 *  「运行时一种样子、完成后另一种样子」。chunk 引擎没有轨迹，返回空数组。 */
function pendingTraceOf(job: GlobalJob) {
  if (job.answer?.reasoning_trace?.length) return [];
  return job.trace ?? [];
}

const suggestions = [
  { title: "汇总一个主题", question: "围绕同一个主题，各笔记本有哪些关键结论？", icon: Layers3 },
  { title: "比较不同观点", question: "资料中有哪些相互补充或存在分歧的观点？请标明来源。", icon: BookOpen },
  { title: "寻找相关证据", question: "这个问题有哪些原文证据支持，还有哪些信息需要补充？", icon: FileText },
];

export default function GlobalAskWorkspace({ compact = false, embedded = false, active = true, controls, onOpenNotebook, uiMode }: { compact?: boolean; embedded?: boolean; active?: boolean; controls?: ReactNode; onOpenNotebook?: () => void; uiMode?: UiMode }) {
  const ask = useGlobalAsk({ syncUrl: !embedded, active, uiMode });
  const [historyOpen, setHistoryOpen] = useState(false);
  const [cite, setCite] = useState<CiteSelection | null>(null);
  const copyResult = useCopyResult();
  const [copying, setCopying] = useState("");
  const copyFlight = useRef<number | null>(null);
  const copyOwner = useRef(0);
  const composer = useRef<HTMLTextAreaElement>(null);
  const transcript = useRef<HTMLDivElement>(null);
  const names = new Map(ask.notebooks.map((notebook) => [notebook.id, notebook.name]));
  // 引用卡按 id→name 查所属库名（跨库徽章）。`names` 那份 Map 是覆盖回执的既有
  // 形状，这里只换成卡片要的 Record，不另起一份真源。
  const notebookNames = Object.fromEntries(names);
  const busy = ask.loading || ask.opening || ask.submitting || ask.stopping;
  const composerDisabled = busy || ask.openFailed;
  // 有东西在途：作业在跑、问题理解在跑、或者审阅卡正等着确认。三者期间草稿框、
  // 范围与引擎都锁住——否则确认的会是另一份范围/引擎下理解出来的问题。
  const inFlight = Boolean(ask.running) || ask.intentChecking || Boolean(ask.intentReview);
  const hint = askQuestionLimitHint(ask.draft.trim());
  const selectedNotebookIds = ask.scope.mode === "include" ? new Set(ask.scope.notebook_ids) : null;
  const excludesPreviousContext = selectedNotebookIds !== null
    && ask.turns.some((turn) => turn.resolved_notebook_ids.some((id) => !selectedNotebookIds.has(id)));

  useEffect(() => { setCite(null); }, [ask.conversationId]);
  useLayoutEffect(() => {
    ++copyOwner.current;
    copyFlight.current = null;
    copyResult.reset();
    setCopying("");
    return () => { ++copyOwner.current; };
  }, [ask.conversationId, copyResult.reset]);
  // 「Esc 只收一层」的拦截已经下沉进 `CitationPopover` 自己（window 捕获期
  // preventDefault + stopPropagation + 收起）：两条渲染路径的卡片——历史轮次这条
  // 就地渲染的，以及新作业那条由 `AnswerView` 内部渲染的——因此共用同一份保护，
  // 这里不再另写一份 bespoke effect。原生 <dialog> close request 那道缺口由
  // launcher 的 onCancel 读 `citationPopoverHoldsEscape()` 兜底。
  //
  // ⚠ 浮窗收起后本组件**仍然挂载**（launcher 的 `started` 不回 false），所以两条
  // 路径的卡片都必须随 `active` 一起收掉：否则那个捕获期监听会留在 window 上，把
  // 宿主页面的下一次 Esc 整个吞掉；重开浮窗时卡片还会按几分钟前的旧视口坐标悬着。
  // 本地这份直接清 state，`AnswerView` 内部那份走 `dismissSignal`。
  useEffect(() => { if (!active) setCite(null); }, [active]);
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
    <div className="global-layout">
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
              {ask.turns.map((job) => { const trace = pendingTraceOf(job); return <section key={job.job_id} className="global-turn">
                <ChatQuestion question={job.question} askedAt={job.created_at} />
                <div className="global-turn-scope"><BookOpen size={12} />{job.notebook_scope.mode === "all" ? "全部笔记本" : "指定笔记本"} · {job.resolved_notebook_ids.length} 个</div>
                {job.answer ? <ChatAnswer answeredAt={job.answer.answered_at ?? job.created_at}>
                  <div className="global-answer-label"><span className="global-answer-mark">SN</span><strong>综合回答</strong><span>引用来自 {job.cited_notebook_ids.length} 个笔记本</span></div>
                  {/* 单库动作（打开来源 / 知识图谱 / Knowhow 行 / 保存记忆 / 分享 /
                      构建索引 / 导入站外建议）一概不传：全局问答没有「当前笔记本」，
                      这些入口在这里没有承接方，缺席时 AnswerView 按既有惯例连按钮都
                      不渲染。notebookId 传 null 同理——没有可用的资产代理端点，附图区
                      整块不渲染，绝不拿引用自己的 notebook_id 去直连另一个库。
                      只额外给它一个跨库专属的出口：每条引用「打开笔记本」。 */}
                  <AnswerView
                    answer={job.answer}
                    feedbackSent={job.feedback ?? ""}
                    onFeedback={(rating) => void ask.sendFeedback(job.job_id, rating)}
                    notebookId={null}
                    notebookNames={notebookNames}
                    notebookHref={(notebookId, sourceId) => `/${notebookHash(notebookId, sourceId)}`}
                    onOpenNotebook={onOpenNotebook}
                    dismissSignal={active}
                    buildingScaleIndex={false}
                    memorySaved={false}
                  />
                  {/* 轨迹恒在回答**下方**：完成态由 AnswerView 自己渲染在这个位置
                      （answer.reasoning_trace），这里补的是它不渲染的那两种情形，
                      位置必须一致——同一轮里跳一次位置比没有轨迹更难读。 */}
                  {trace.length > 0 && <ReasoningTracePanel steps={trace} />}
                </ChatAnswer> : job.response ? <ChatAnswer answeredAt={job.response.created_at}>
                  <div className="global-answer-label"><span className="global-answer-mark">SN</span><strong>综合回答</strong><span>引用来自 {job.cited_notebook_ids.length} 个笔记本</span></div>
                  {job.response.grounded !== true && <p className="global-answer-grounding" role="note">以下回答未得到原文充分支持，请结合引用核对。</p>}
                  <AnswerMarkdown answer={job.response.answer} anchors={job.response.anchors} citations={job.response.citations}
                    selectedReferenceId={cite?.jobId === job.job_id ? cite.reference.id : null}
                    onReferenceClick={(reference, event) => setCite({ jobId: job.job_id, reference, rect: event.currentTarget.getBoundingClientRect() })} />
                  <footer className="global-answer-footer"><small>{job.response.completeness_notice}</small><button aria-label="复制回答" className={copyResult.resultFor(job.job_id) === "copied" ? "global-text-button copy-result-copied" : copyResult.resultFor(job.job_id) === "failed" ? "global-text-button copy-result-failed" : "global-text-button"} disabled={Boolean(copying)} onClick={() => void copyAnswer(job)}>{copyResult.resultFor(job.job_id) === "copied" ? <Check size={13} /> : <Copy size={13} />}<span role="status">{copying === job.job_id ? "复制中…" : copyResult.resultFor(job.job_id) === "copied" ? "已复制" : copyResult.resultFor(job.job_id) === "failed" ? "复制失败" : "复制"}</span></button></footer>
                </ChatAnswer> : <>
                <div className={`global-job-status${job.status === "failed" ? " failed" : ""}`} role="status">
                  {job.status === "running" && <LoaderCircle size={18} className="global-spin" />}
                  <span>{job.status === "running" ? `正在查阅资料 · 已检索 ${job.searched_notebook_ids.length} / ${job.resolved_notebook_ids.length} 个笔记本` : job.status === "cancelled" ? "已停止回答，可以修改问题后继续。" : job.status === "interrupted" ? "服务已重启，请重新提交问题。" : job.error || "回答未完成，请检查模型服务后重试。"}</span>
                  {job.status !== "running" && <button className="global-text-button" onClick={() => { ask.setDraft(job.question); composer.current?.focus(); }}>重新提问</button>}
                </div>
                {/* 与完成态同一个位置（回答/状态之下），同一轮里不跳位。
                    刻意在 role="status" 之外：它自带一颗可展开的按钮、内容每一步都
                    在变，塞进 live region 会让读屏把整块反复念一遍。 */}
                {trace.length > 0 && <div className="chat-assistant chat-thinking"><ReasoningTracePanel steps={trace} live={job.status === "running"} /></div>}
                </>}
                <GlobalCoverageReceipt job={job} notebookNames={names} />
              </section>; })}
            </div>}
        </div>
        <div className="global-composer-area">
          {ask.error && <div className="global-inline-error" role="alert">{ask.error}<button className="global-text-button" disabled={busy} onClick={() => void ask.load()}>重新加载</button></div>}
          {ask.pollError && <div className="global-inline-error" role="alert">{ask.pollError}<button className="global-text-button" onClick={ask.retryPoll}>重新连接</button></div>}
          {/* 「逐步推理」的问题理解审阅卡，与笔记本内问答同一个组件、同一条交互：
              预检 → 审阅 → 带 intent 提交。确认前草稿原地保留，返回修改即可继续改。 */}
          {ask.intentReview && <AskIntentReview
            contract={ask.intentReview.contract}
            understandingMs={ask.intentReview.understandingMs}
            busy={ask.submitting}
            onConfirm={(confirmation) => void ask.confirmIntent(confirmation)}
            onCancel={ask.cancelIntent}
          />}
          <form className="global-composer" onSubmit={(event) => { event.preventDefault(); void ask.submit(); }}>
            <textarea ref={composer} aria-label="输入问题" placeholder={ask.turns.length ? "继续追问，或选择新的笔记本范围…" : "向你的笔记本提问…"} rows={3} value={ask.draft} disabled={composerDisabled || inFlight} onChange={(event) => ask.setDraft(event.target.value)} onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); void ask.submit(); }
            }} />
            <div className="global-composer-toolbar"><NotebookScopePicker notebooks={ask.notebooks} scope={ask.scope} onChange={ask.setScope} disabled={composerDisabled || inFlight} />
              {/* 引擎选择器与笔记本内问答共用同一份控件。
                  · 扩展引擎不进 `ask.modes`：后端对全局问答一律 422，扩展组因此整组不出现。
                  · `kgAvailable={false}`：全局模式跨多个笔记本，没有「这一个笔记本的
                    知识图谱」可言，更没有可以整理的那一个。笔记本级的两句提示
                    （尚无知识图谱 / 借用参考库推理）在这里都说不成立的话，所以一句
                    不挂——控件本身按 T5 的既有契约照常可选，不拒答。 */}
              <AskModePicker
                modes={ask.modes}
                value={ask.mode}
                onChange={ask.selectMode}
                disabled={composerDisabled || inFlight}
                kgAvailable={false}
                uiMode={ask.uiMode}
              />
              {ask.running || ask.intentChecking
                ? <button className="global-send new-pill" type="button" disabled={ask.stopping} onClick={() => { if (ask.intentChecking) ask.abortIntent(); else void ask.stop(); }}><Square size={14} />{ask.intentChecking ? "取消问题理解" : ask.stopping ? "停止中…" : "停止"}</button>
                : <button className="global-send new-pill" type="submit" disabled={composerDisabled || inFlight || !ask.draft.trim() || Boolean(hint) || !ask.notebooks.length} aria-label="发送问题">{ask.submitting ? <LoaderCircle className="global-spin" size={18} /> : <ArrowUp size={19} />}</button>}
            </div>
            {excludesPreviousContext && <p className="global-scope-context-notice" role="status">范围已收窄：先前涉及其他笔记本的提问不会用于本次追问，请重新说明要讨论的对象。</p>}
          </form>
          <p className={`global-composer-hint${hint ? " invalid" : ""}`}>{hint || (ask.notebooks.length ? "答案来自所选范围的原文 · Enter 发送，Shift + Enter 换行" : "还没有可访问的笔记本，请先返回主页添加资料。")}</p>
        </div>
      </main>
    </div>
    {/* 引用小卡片。与笔记本内问答**同一个组件**，所以两个面的界面与逻辑逐字一致；
        全局问答额外给它一个「打开笔记本」出口。
        ⚠ 就地渲染、不走 portal：全屏形态下浮窗是 top layer 里的模态 <dialog>，
        渲染在它子树之外的 fixed 元素会被整个盖住且不可交互。挂在这里（仍在
        `.global-ask-page` 之内）两种形态都成立；`.global-ask-window` 没有
        transform/contain 一类会改变 fixed 定位包含块的样式，卡片用的视口坐标
        （placeCitationPopover）因此是准的，也不会被 dialog 的 overflow:hidden 裁掉。
        ⚠ notebookId 传 null：全局问答没有「当前笔记本」，也就没有可用的资产代理
        端点。附图区因此整块不渲染（与「无附图」等价）——绝不拿引用自己的
        notebook_id 去直连另一个库的资产，那是前端替用户猜权限。
        onOpenSource / onOpenKnowledgeGraph / onOpenKnowhowRow / importController
        同理一概不传：这些入口都只在某个笔记本的工作区里才有承接方，缺席时卡片
        优雅降级成「不渲染那颗按钮」。 */}
    {cite && <CitationPopover
      reference={cite.reference}
      notebookId={null}
      notebookNames={notebookNames}
      notebookHref={(notebookId, sourceId) => `/${notebookHash(notebookId, sourceId)}`}
      onOpenNotebook={onOpenNotebook}
      anchorRect={cite.rect}
      onClose={() => setCite(null)}
    />}
  </div>;
}
