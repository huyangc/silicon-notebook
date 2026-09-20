import { useCallback, useEffect, useRef, useState } from "react";
import { fetchMe } from "../auth.ts";
import { listNotebooks } from "../notebook-api.ts";
import { askQuestionLimitHint } from "../ask-api.ts";
import { httpErrorStatus, toUserMessage } from "../errors.ts";
import {
  askGlobal, cancelGlobalJob, getGlobalConversation, getGlobalJob, listGlobalConversations,
  previewGlobalAskIntent, submitGlobalFeedback,
  GLOBAL_ASK_MAX_NOTEBOOKS, GLOBAL_ASK_PAGE_SIZE, submittableGlobalScope,
  type GlobalConversation, type GlobalConversationDetail, type GlobalJob, type GlobalScope,
} from "../global-ask-api.ts";
import { ASK_MODES, DEFAULT_ASK_MODE, askModeIds, submissionAskMode } from "../ask-modes.ts";
import { isAdvanced, normalizeUiMode, type UiMode } from "../ui-mode.ts";
import { DEFAULT_ASK_RETRIEVAL_EFFORT } from "../ask-retrieval-effort.ts";
import {
  buildAskIntentConfirmation,
  type AskIntentConfirmation,
  type QueryIntentContract,
} from "../ask-intent-model.ts";
import {
  handOffIntentTrace, intentClarifyStep, intentConfirmedStep, intentUnderstandingStep,
  intentUnderstoodStep, replaceLastIntentStep,
} from "../ask-intent-trace.ts";
import type { ReasoningTraceStep } from "../ask-stream.ts";
import { hasProcessOutput } from "../stopped-turn.tsx";
import type { NotebookSummary } from "../workspace-model.ts";

const POLL_MIN_INTERVAL_MS = 1200;
const POLL_MAX_INTERVAL_MS = 15000;
const POLL_BACKOFF_FACTOR = 2;

/** 全局问答可选的引擎:**只有内置那两个**。部署扩展引擎不参与——后端对全局
 *  问答一律 422,所以界面压根不该把它们摆出来(扩展组因此自然为空)。 */
export const GLOBAL_ASK_MODES = ASK_MODES;
const GLOBAL_ASK_MODE_IDS = new Set(askModeIds(GLOBAL_ASK_MODES));

/** 全局问答 v1 不提供档位控件:恒用默认档。这里是一个具名常量而不是一格没有
 *  消费方的状态——界面上没有改它的入口,做成 state 只会是死代码。 */
const GLOBAL_ASK_RETRIEVAL_EFFORT = DEFAULT_ASK_RETRIEVAL_EFFORT;

/** 引擎选择是 per-viewer 的便利记忆(同「记住上次用的那个页签」),不是要可靠
 *  持久、也不需要跨设备的状态,所以放浏览器存储。读写一律 try/catch:隐私窗口、
 *  站点数据被清、预览环境里访问器本身就可能抛,读不到也必须能正常渲染。 */
const MODE_STORAGE_KEY = "sn.globalAskMode";

/** 问题理解的审阅态:待用户补齐澄清项的那一刻。 */
type GlobalIntentReview = {
  question: string;
  contract: QueryIntentContract;
  understandingMs: number;
};

/** 已提交、服务端还没交回作业的那一轮：问题立即上屏（与笔记本内问答的 pending 轮
 *  同一个做法），问题理解的步骤实时画在它下面。它只活在这个 hook 里。 */
export type GlobalPendingTurn = {
  question: string;
  askedAt: string;
  scope: GlobalScope;
  trace: ReasoningTraceStep[];
};

// 轨迹步数也进这把键：逐步推理的一轮可以连出好几步而覆盖回执一动不动，键里没有它，
// 轮询会把「只长轨迹」判成没进展、一路退避到 15 秒，实时轨迹就成了 15 秒一跳。
function progressKey(job: GlobalJob): string {
  return JSON.stringify([job.status, job.searched_notebook_ids, job.skipped_notebooks ?? [], job.degraded_notebook_ids ?? [], job.trace?.length ?? 0]);
}

/** 这条作业是否已经有过程输出上屏：轨迹里有真正的检索 / 推理步，或任何一个库已经
 *  交回了检索回执。停止时据此分两种情形（规则见 `stopped-turn.tsx`）。 */
export function globalJobHasProcessOutput(job: GlobalJob): boolean {
  return hasProcessOutput(job.trace)
    || job.searched_notebook_ids.length > 0
    || (job.skipped_notebooks?.length ?? 0) > 0;
}

/** 下一次提问会替换的那条记录：会话里**最新**的一轮，且它是被停止的。 */
export function replaceableTurn(turns: readonly GlobalJob[]): GlobalJob | null {
  const last = turns[turns.length - 1];
  return last && last.status === "cancelled" ? last : null;
}

// A cancelled/done turn is terminal even if a previously issued poll arrives later.
function mergeJob(current: GlobalJob, incoming: GlobalJob): GlobalJob {
  if (current.status !== "running" && incoming.status === "running") return current;
  if (current.status === "running" && incoming.status === "running") {
    return { ...incoming, searched_notebook_ids: [...new Set([...current.searched_notebook_ids, ...incoming.searched_notebook_ids])] };
  }
  return incoming;
}

export function useGlobalAsk({ syncUrl = true, active = true, uiMode: hostUiMode }: {
  syncUrl?: boolean; active?: boolean;
  /** 宿主（笔记本页里的浮窗）手里那份**实时**的界面模式。独立的 /ask 页面不传，
   *  改用本 hook 载入时读到的用户档案。 */
  uiMode?: UiMode;
} = {}) {
  const [notebooks, setNotebooks] = useState<NotebookSummary[]>([]);
  const [conversations, setConversations] = useState<GlobalConversation[]>([]);
  const [conversationId, setConversationId] = useState("");
  // 当前会话的标题，**以会话详情为准**。`conversations` 只有最新一页，深链
  // 打开较旧的会话时在列表里查不到；凡是要显示会话名的地方（分享弹窗抬头）
  // 照着列表查就会写成「未命名会话」。
  const [conversationTitle, setConversationTitle] = useState("");
  const [turns, setTurns] = useState<GlobalJob[]>([]);
  const [scope, setScope] = useState<GlobalScope>({ mode: "all" });
  const [draft, setDraft] = useState("");
  // 服务端渲染出的首屏读不到 localStorage,所以初值恒是默认引擎、挂载后再改写;
  // 直接在 useState 初始化里读会让首屏与 hydration 后的选中页签对不上。
  const [mode, setMode] = useState<string>(DEFAULT_ASK_MODE);
  // 界面模式决定有没有引擎控件，也决定**提交走哪个引擎**——与笔记本内问答同一条
  // 规则（`submissionAskMode`）：简化界面没有控件，提交固定走 SIMPLIFIED_ASK_MODE；
  // `mode` 在简化界面下只是一份不可见的记忆。档案读到之前按简化界面处理：宁可晚一拍
  // 露出控件，也不先露出一个这位用户本不该看到的控件。
  const [loadedUiMode, setLoadedUiMode] = useState<UiMode>(normalizeUiMode(undefined));
  const uiMode = hostUiMode ?? loadedUiMode;
  const submissionMode = submissionAskMode(isAdvanced(uiMode), mode);
  const [intentReview, setIntentReview] = useState<GlobalIntentReview | null>(null);
  const [intentChecking, setIntentChecking] = useState(false);
  const intentAbort = useRef<AbortController | null>(null);
  const [pending, setPendingState] = useState<GlobalPendingTurn | null>(null);
  const pendingRef = useRef<GlobalPendingTurn | null>(null);
  // 问题理解阶段合成的那几步，交接给作业之后接在它的实时轨迹前面（笔记本内问答的
  // traceSeed）。只在本次实时展示里有用：完成后以 `answer.reasoning_trace` 为准。
  const [traceSeeds, setTraceSeeds] = useState<Record<string, ReasoningTraceStep[]>>({});
  // 「提交请求还没回来就按了停止」：作业 id 此刻还不知道，等它一回来立刻停掉并丢弃。
  const stopRequested = useRef(false);
  // 轮次快照的代数：对账换掉第一页与游标时递增，在途的「加载更早的问答」据此作废，
  // 迟到的旧页才不会把已经改正的游标写回去。
  const turnsGeneration = useRef(0);
  // 每次提交递增。替换失败后的那次自动重拉不 await，期间用户可能已经重试成功：
  // 重拉回来时序号对不上，就说明它手里是旧快照，不许覆盖刚接受的新一轮。
  const submitSerial = useRef(0);
  const [loading, setLoading] = useState(true);
  const [opening, setOpening] = useState(false);
  const [openFailed, setOpenFailed] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [error, setError] = useState("");
  const [pollError, setPollError] = useState("");
  const [historyError, setHistoryError] = useState("");
  const [pollRevision, setPollRevision] = useState(0);
  const [moreHistory, setMoreHistory] = useState(false);
  const [historyOffset, setHistoryOffset] = useState(0);
  const [loadingHistory, setLoadingHistory] = useState(false);
  const [turnOffset, setTurnOffset] = useState<number | null>(null);
  const [loadingTurns, setLoadingTurns] = useState(false);
  const owner = useRef(0);
  const mounted = useRef(true);
  const flight = useRef(false);
  const historyFlight = useRef(false);
  const turnsFlight = useRef(false);
  const historyVersion = useRef(0);
  const historyNeedsRefresh = useRef(false);
  const retryRequest = useRef<{ key: string; id: string } | null>(null);
  const currentId = useRef(conversationId);
  currentId.current = conversationId;
  const currentScope = useRef(scope);
  currentScope.current = scope;
  const currentTurns = useRef(turns);
  currentTurns.current = turns;
  const initialized = useRef(false);
  const notebookVersion = useRef(0);
  const wasActive = useRef(active);
  const running = turns.find((turn) => turn.status === "running");

  /**
   * 退休在途的问题理解与它的审阅卡。
   *
   * **凡是推进 `owner.current` 的地方都必须调它**：`previewIntent` 的 finally 只在
   * 「自己仍是当前那次预检」时才收 `intentChecking`，owner 换了而这里没清，界面就会
   * 永久停在「取消问题理解」——草稿框、范围、引擎全禁用，而 `abortIntent()` 已经是
   * 空动作，只能刷新页面。
   */
  const retireIntent = useCallback((options: { returnQuestion?: boolean } = {}) => {
    intentAbort.current?.abort();
    intentAbort.current = null;
    setIntentChecking(false);
    setIntentReview(null);
    // 上屏的 pending 轮与在途预检同生共死。`returnQuestion`：这一轮是被「重新加载」
    // 打断的，对话没换，问题交还输入框；切换 / 新建对话则随旧对话一起作废。
    const retired = pendingRef.current;
    pendingRef.current = null;
    setPendingState(null);
    stopRequested.current = false;
    // 轨迹种子只对本次实时展示有意义：视图一换（重载 / 切换 / 新建对话）就清掉，
    // 不让它跟着标签页无限累积。
    setTraceSeeds({});
    if (retired && options.returnQuestion) setDraft(retired.question);
  }, []);

  function showPending(next: GlobalPendingTurn | null) {
    pendingRef.current = next;
    setPendingState(next);
  }
  function updatePendingTrace(update: (steps: ReasoningTraceStep[]) => ReasoningTraceStep[]) {
    const current = pendingRef.current;
    if (current) showPending({ ...current, trace: update(current.trace) });
  }
  /** 情形二的收尾：这一轮还没有任何过程输出就结束了（取消、失败、返回修改）——
   *  问题回到输入框，对话里不留它。 */
  function returnQuestion() {
    const retired = pendingRef.current;
    showPending(null);
    if (retired) setDraft((draft) => draft.trim() ? draft : retired.question);
  }

  const load = useCallback(async () => {
    const ticket = ++owner.current;
    const libraryTicket = ++notebookVersion.current;
    const resumeId = currentId.current || (syncUrl ? new URLSearchParams(window.location.search).get("conversation_id") : "");
    const preserveScope = initialized.current;
    const draftScope = currentScope.current;
    ++historyVersion.current;
    // owner 刚被推进，在途预检随之失去归属：必须在同一处退休它（见 retireIntent）。
    retireIntent({ returnQuestion: true });
    setLoading(true);
    setOpenFailed(Boolean(resumeId));
    setError("");
    setPollError("");
    setPollRevision((value) => value + 1);
    setTurns([]);
    setTurnOffset(null);
    setConversationId(resumeId || "");
    try {
      const me = await fetchMe();
      if (mounted.current && owner.current === ticket) setLoadedUiMode(normalizeUiMode(me.ui_mode));
      const [libraries, history] = await Promise.all([listNotebooks(), listGlobalConversations()]);
      if (!mounted.current || owner.current !== ticket) return;
      if (libraryTicket === notebookVersion.current) setNotebooks(libraries);
      setConversations(history);
      setMoreHistory(history.length === GLOBAL_ASK_PAGE_SIZE);
      setHistoryOffset(history.length);
      historyNeedsRefresh.current = false;
      if (resumeId) {
        const detail = await getGlobalConversation(resumeId);
        if (!mounted.current || owner.current !== ticket) return;
        setTurns(detail.turns);
        setConversationTitle(detail.title || "");
        setTurnOffset(detail.has_more ? detail.next_offset : null);
        setConversationId(detail.id);
        setScope(preserveScope ? draftScope : detail.notebook_scope);
        setOpenFailed(false);
      }
      initialized.current = true;
    } catch (cause) {
      if (mounted.current && owner.current === ticket) setError(toUserMessage(cause, "全局问答加载失败，请重试"));
    } finally {
      if (mounted.current && owner.current === ticket) setLoading(false);
    }
  }, [syncUrl, retireIntent]);

  useEffect(() => {
    mounted.current = true;
    void load();
    return () => { mounted.current = false; ++owner.current; ++historyVersion.current; };
  }, [load]);

  useEffect(() => {
    try {
      const stored = window.localStorage.getItem(MODE_STORAGE_KEY);
      if (stored && GLOBAL_ASK_MODE_IDS.has(stored)) setMode(stored);
    } catch { /* 读不到就用默认引擎，渲染照常。 */ }
  }, []);

  function selectMode(id: string) {
    if (!GLOBAL_ASK_MODE_IDS.has(id)) return;
    setMode(id);
    try { window.localStorage.setItem(MODE_STORAGE_KEY, id); } catch { /* 记不住不影响本次提问。 */ }
  }

  useEffect(() => {
    const reopening = active && !wasActive.current;
    wasActive.current = active;
    if (!reopening) return;
    const ticket = ++notebookVersion.current;
    void listNotebooks().then((libraries) => {
      if (mounted.current && notebookVersion.current === ticket) setNotebooks(libraries);
    }).catch((cause) => {
      if (mounted.current && notebookVersion.current === ticket) setError(toUserMessage(cause, "笔记本列表刷新失败，请重新加载"));
    });
  }, [active]);

  // 「全部」在可读笔记本超过上限时提交必然 422：把它换成一份看得见、改得动的预选。
  // 放在 effect 里而不是只在提交时换，是因为范围选择器要把这 8 个显示成已勾选。
  useEffect(() => {
    if (loading) return;
    const next = submittableGlobalScope(scope, notebooks);
    if (next !== scope) setScope(next);
  }, [loading, scope, notebooks]);

  useEffect(() => {
    if (!running || loading) return;
    const ticket = owner.current;
    let disposed = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let inFlight = false;
    let resumeRequested = false;
    let interval = POLL_MIN_INTERVAL_MS;
    let previousProgress = progressKey(running);
    let terminal = false;
    const isCurrent = () => !disposed && mounted.current && ticket === owner.current;
    const visible = () => document.visibilityState !== "hidden";
    function schedule() {
      if (isCurrent() && visible() && !terminal) timer = setTimeout(poll, interval);
    }
    async function poll() {
      if (!isCurrent() || !visible() || terminal || inFlight) return;
      inFlight = true;
      try {
        const update = await getGlobalJob(running!.job_id);
        if (!isCurrent()) return;
        setTurns((items) => items.map((item) => item.job_id === update.job_id ? mergeJob(item, update) : item));
        setPollError("");
        const nextProgress = progressKey(update);
        interval = nextProgress === previousProgress ? Math.min(POLL_MAX_INTERVAL_MS, interval * POLL_BACKOFF_FACTOR) : POLL_MIN_INTERVAL_MS;
        previousProgress = nextProgress;
        terminal = update.status !== "running";
      } catch (cause) {
        // 作业不存在了（别的标签页把它停掉并丢弃了）：这不是「暂时读不到」。重试只会
        // 永远 404、输入区永远锁着——以服务端为准对一次账，这一轮到此为止。
        if (isCurrent() && httpErrorStatus(cause) === 404) {
          const synced = await readConversation(running!.conversation_id);
          if (isCurrent() && synced !== null) {
            terminal = true;
            setPollError("");
            if (synced === "gone") dropConversationIdentity(running!.conversation_id);
            else applyConversation(synced);
            if (synced === "gone" || !synced.turns.some((item) => item.job_id === running!.job_id)) handBack(running!.question);
            return;
          }
        }
        if (isCurrent()) {
          interval = Math.min(POLL_MAX_INTERVAL_MS, interval * POLL_BACKOFF_FACTOR);
          setPollError(toUserMessage(cause, "暂时无法获取进度，正在自动重试；任务仍可能在后台运行"));
        }
      } finally {
        inFlight = false;
        if (resumeRequested && isCurrent() && visible() && !terminal) {
          resumeRequested = false;
          interval = POLL_MIN_INTERVAL_MS;
          void poll();
        } else schedule();
      }
    }
    function onVisibilityChange() {
      clearTimeout(timer);
      if (!visible()) { resumeRequested = false; return; }
      interval = POLL_MIN_INTERVAL_MS;
      if (inFlight) resumeRequested = true;
      else void poll();
    }
    // active only controls the chat presentation: minimizing never detaches this owner.
    document.addEventListener("visibilitychange", onVisibilityChange);
    schedule();
    return () => {
      disposed = true;
      clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, [running?.job_id, running?.status, pollRevision, loading]);

  function updateUrl(id: string) {
    if (!syncUrl) return;
    const url = new URL(window.location.href);
    if (id) url.searchParams.set("conversation_id", id); else url.searchParams.delete("conversation_id");
    window.history.replaceState(null, "", url);
  }

  async function openConversation(id: string) {
    if (flight.current) return;
    const ticket = ++owner.current;
    setOpening(true);
    setOpenFailed(false);
    setError("");
    setPollError("");
    setTurns([]);
    setTurnOffset(null);
    setLoadingTurns(false);
    setConversationId(id);
    setConversationTitle("");
    setDraft("");
    setScope({ mode: "all" });
    // 切换对话同样推进了 owner：在途的问题理解与审阅卡一并退休。
    retireIntent();
    try {
      const detail = await getGlobalConversation(id);
      if (!mounted.current || ticket !== owner.current) return;
      setTurns(detail.turns);
      setTurnOffset(detail.has_more ? detail.next_offset : null);
      setConversationTitle(detail.title || "");
      setScope(detail.notebook_scope);
      updateUrl(id);
    } catch (cause) {
      if (mounted.current && ticket === owner.current) {
        setOpenFailed(true);
        setError(toUserMessage(cause, "对话加载失败，请重新打开"));
      }
    } finally {
      if (mounted.current && ticket === owner.current) setOpening(false);
    }
  }

  function resetConversation() {
    ++owner.current;
    flight.current = false;
    currentId.current = "";
    setSubmitting(false);
    setStopping(false);
    // 在途的问题理解属于旧对话：连同它的审阅卡一起退休，否则确认时会把上一段
    // 对话的问题提交进新对话。
    retireIntent();
    // Explicitly starting over ends the old draft's idempotent retry identity.
    retryRequest.current = null;
    setConversationId("");
    setConversationTitle("");
    setTurns([]);
    setTurnOffset(null);
    setLoadingTurns(false);
    setDraft("");
    setScope({ mode: "all" });
    setError("");
    setPollError("");
    setOpening(false);
    setOpenFailed(false);
    updateUrl("");
  }

  function newConversation() {
    if (flight.current) return;
    resetConversation();
  }

  async function loadMoreHistory() {
    if (historyFlight.current) return;
    historyFlight.current = true;
    setLoadingHistory(true);
    setHistoryError("");
    const version = historyVersion.current;
    const refresh = historyNeedsRefresh.current;
    const offset = refresh ? 0 : historyOffset;
    try {
      const page = await listGlobalConversations(offset);
      if (!mounted.current || version !== historyVersion.current) return;
      setConversations((items) => refresh ? page : [...items, ...page.filter((item) => !items.some((old) => old.id === item.id))]);
      setHistoryOffset(offset + page.length);
      setMoreHistory(page.length === GLOBAL_ASK_PAGE_SIZE);
      historyNeedsRefresh.current = false;
    } catch (cause) {
      if (mounted.current && version === historyVersion.current) setHistoryError(toUserMessage(cause, "历史对话加载失败，请重试"));
    } finally {
      historyFlight.current = false;
      if (mounted.current) setLoadingHistory(false);
    }
  }

  async function loadMoreTurns() {
    if (turnsFlight.current || turnOffset === null) return;
    turnsFlight.current = true;
    setLoadingTurns(true);
    const ticket = owner.current;
    const generation = turnsGeneration.current;
    try {
      const detail = await getGlobalConversation(conversationId, turnOffset);
      if (!mounted.current || owner.current !== ticket || generation !== turnsGeneration.current) return;
      setTurns((items) => [...detail.turns.filter((item) => !items.some((old) => old.job_id === item.job_id)), ...items]);
      setTurnOffset(detail.has_more ? detail.next_offset : null);
    } catch (cause) {
      if (mounted.current && ticket === owner.current) setError(toUserMessage(cause, "更早的问答加载失败，请重试"));
    } finally {
      turnsFlight.current = false;
      if (mounted.current && ticket === owner.current) setLoadingTurns(false);
    }
  }

  async function submit() {
    if (flight.current || running || opening || openFailed || loading || intentChecking || intentReview) return;
    const question = draft.trim();
    const hint = askQuestionLimitHint(question);
    if (!question || hint) { setError(hint || "请先输入问题"); return; }
    if (scope.mode === "include" && !scope.notebook_ids.length) { setError("请至少选择一个笔记本"); return; }
    if (scope.mode === "include" && scope.notebook_ids.length > GLOBAL_ASK_MAX_NOTEBOOKS) {
      setError(`一次最多检索 ${GLOBAL_ASK_MAX_NOTEBOOKS} 个笔记本，请减少选择`); return;
    }
    if (scope.mode === "include" && scope.notebook_ids.some((id) => !notebooks.some((item) => item.id === id))) {
      setError("部分已选笔记本不可访问，请重新选择范围"); return;
    }
    // 问题**立即**上屏、输入框清空——与笔记本内问答同一个时刻（点击当下，先于任何
    // await）。此前要等问题理解与建作业两次往返都回来，界面上像卡住了一样。
    stopRequested.current = false;
    showPending({ question, askedAt: new Date().toISOString(), scope, trace: [] });
    setDraft("");
    // 「逐步推理」与笔记本内问答走同一套交互：先预检问题理解，需要澄清时弹审阅卡，
    // 确认后才带着 intent 提交。通用问答没有这一步，直接建作业。
    if (submissionMode === "reasoning") { await previewIntent(question); return; }
    await submitJob(question);
  }

  /**
   * 跑一次问题理解预检。理解清楚就直接把系统的理解当确认提交；需要澄清就把合同
   * 交给审阅卡。理解步骤实时画在 pending 轮下面（与笔记本内问答同一组合成步，
   * `ask-intent-trace.ts`）；取消或失败时问题回到输入框，用户原地就能改。
   */
  async function previewIntent(question: string) {
    const ticket = owner.current;
    const controller = new AbortController();
    intentAbort.current = controller;
    setIntentChecking(true);
    setError("");
    const startedAt = Date.now();
    updatePendingTrace(() => [intentUnderstandingStep()]);
    try {
      const contract = await previewGlobalAskIntent(
        question, conversationId || undefined, scope, controller.signal,
        (elapsed) => {
          if (mounted.current && ticket === owner.current && intentAbort.current === controller) {
            updatePendingTrace((steps) => replaceLastIntentStep(steps, intentUnderstandingStep(elapsed)));
          }
        },
      );
      if (!mounted.current || ticket !== owner.current) return;
      // `signal.aborted` 也要查：流可能已经返回、而用户恰在这一瞬点了「取消问题
      // 理解」。只 abort 请求不查这一下，作业照样会被建出来——取消变成了假的。
      if (controller.signal.aborted) { returnQuestion(); return; }
      const understandingMs = Math.max(0, Date.now() - startedAt);
      if (contract.needs_clarification) {
        updatePendingTrace((steps) => replaceLastIntentStep(steps, intentClarifyStep(contract, understandingMs)));
        setIntentReview({ question, contract, understandingMs });
        return;
      }
      updatePendingTrace((steps) => replaceLastIntentStep(steps, intentUnderstoodStep(contract, understandingMs)));
      // 理解阶段到此结束，**先**收掉它再提交（与笔记本内问答同一处接缝）：建作业的
      // POST 在服务端还要跑一段，这期间按下的停止必须落到「提交在途」那条路径上，
      // 而不是对一个早已返回的理解请求做一次空的 abort。
      intentAbort.current = null;
      setIntentChecking(false);
      await submitJob(question, buildAskIntentConfirmation(
        contract, contract.resolved_question, {}, understandingMs,
      ));
    } catch (cause) {
      if (!mounted.current || ticket !== owner.current) return;
      // 取消也好、失败也好，此刻都还没有任何过程输出：问题回到输入框。
      returnQuestion();
      // 用户自己按下的「取消问题理解」不是失败，不上错误条。
      if (!controller.signal.aborted) setError(toUserMessage(cause, "问题理解没能完成，请重试"));
    } finally {
      // 判据是「自己这次预检是否仍是当前那一次」，**不看 ticket**：owner 换过
      // （load / 切换对话 / 新建对话）时那条路径已经 retireIntent 收过状态了，而
      // 按 ticket 判会让本次的收尾整个跳过，界面永久停在「取消问题理解」。
      // 判据是「自己这次预检是否仍是当前那一次」，**不看 ticket**：owner 换过
      // （load / 切换对话 / 新建对话）时那条路径已经 retireIntent 收过状态了，
      // 按 ticket 判则会把本次的收尾整个跳过。两道一起才让「界面永久停在『取消
      // 问题理解』」不可能发生——退休不再依赖每一个未来的 owner 推进点都记得调。
      if (intentAbort.current === controller) {
        intentAbort.current = null;
        if (mounted.current) setIntentChecking(false);
      }
    }
  }

  function abortIntent() {
    intentAbort.current?.abort();
  }

  async function confirmIntent(confirmation: AskIntentConfirmation) {
    const review = intentReview;
    if (!review || flight.current) return;
    setIntentReview(null);
    updatePendingTrace((steps) => [
      ...steps, intentConfirmedStep(confirmation.resolved_question, confirmation.answers.length),
    ]);
    await submitJob(review.question, confirmation);
  }

  /** 审阅卡的「返回修改」：问题回到输入框（情形二——还没有任何过程输出）。 */
  function cancelIntent() {
    setIntentReview(null);
    returnQuestion();
  }

  async function submitJob(question: string, intent?: AskIntentConfirmation) {
    flight.current = true;
    setSubmitting(true);
    setError("");
    const ticket = owner.current;
    const serial = ++submitSerial.current;
    // 引擎进幂等键：换引擎重问同一个问题是**另一次**提问，不该复用上一次的
    // request id 被后端当成重复提交挡掉。
    // 确认内容里**用户亲手改过的部分**也进幂等键：后端按「问题 / 范围 / 会话 / 引擎」
    // 认重试、刻意不比 intent（每次预检模型给出的理解与耗时都不同，比了重试就永远
    // 409）。所以「响应丢了 → 重新预检 → 这次改了确认的问题或补充回答」必须在这里换一个
    // request id，否则后端会把上一次确认的那份作业原样交回来。没动过的确认不进键，
    // 重试才仍然命中同一份作业。
    const edited = intent && (intent.answers.length > 0
      || intent.resolved_question !== intent.contract.resolved_question)
      ? { resolved: intent.resolved_question, answers: intent.answers }
      : null;
    // 会话里最新的一轮若是被停止的，这次提问就替换它（规则见 `stopped-turn.tsx`）。
    // 进幂等键：同一个问题「替换 A」与「不替换」是两次不同的提交。
    const replaces = replaceableTurn(currentTurns.current)?.job_id;
    const key = JSON.stringify({ question, scope, conversationId, mode: submissionMode, edited, replaces });
    if (retryRequest.current?.key !== key) retryRequest.current = { key, id: crypto.randomUUID() };
    try {
      const payload = {
        question,
        notebook_scope: scope,
        conversation_id: conversationId || undefined,
        client_request_id: retryRequest.current.id,
        mode: submissionMode,
        intent,
        retrieval_effort: GLOBAL_ASK_RETRIEVAL_EFFORT,
        replaces_job_id: replaces,
      };
      // 用户在提交途中按过停止、而这次请求失败了：响应可能只是丢了，作业照样建了。
      // 新会话的第一问此刻连会话 id 都没有、无从对账——用**同一个幂等 id** 再发一次：
      // 已建的作业会被原样回放，那次停止才够得着它（下面的 stopRequested 分支）。
      const job = await askGlobal(payload).catch((cause) => {
        if (!stopRequested.current || !mounted.current || ticket !== owner.current) throw cause;
        return askGlobal(payload);
      });
      if (!mounted.current || ticket !== owner.current) return;
      retryRequest.current = null;
      let adopted = job;
      if (stopRequested.current && job.status === "running") {
        // 提交还没回来用户就按了停止：作业刚建好、什么都还没上屏——停掉并丢弃，
        // 问题回到输入框（情形二）。停不掉就照常接管它，停止键仍在。
        stopRequested.current = false;
        const outcome = await discardJob(job, () => mounted.current && ticket === owner.current);
        if (!mounted.current || ticket !== owner.current) return;
        if (outcome === null) return;
        if (outcome === "failed") setError("停止失败，请重试");
        else adopted = outcome;
      }
      const seed = handOffIntentTrace(pendingRef.current?.trace ?? []);
      if (seed.length) setTraceSeeds((seeds) => ({ ...seeds, [job.job_id]: seed }));
      showPending(null);
      setConversationId(job.conversation_id);
      setTurns((items) => [...items.filter((item) => item.job_id !== job.job_id && item.job_id !== replaces), adopted]);
      setPollError("");
      updateUrl(job.conversation_id);
      const version = ++historyVersion.current;
      void listGlobalConversations().then((history) => {
        if (mounted.current && ticket === owner.current && version === historyVersion.current) {
          setConversations(history);
          // 替换掉会话里唯一的一轮时，服务端会把自动标题改成新问题：抬头跟着列表走。
          const current = history.find((item) => item.id === job.conversation_id);
          if (current) setConversationTitle(current.title || "");
          setHistoryOffset(history.length);
          setMoreHistory(history.length === GLOBAL_ASK_PAGE_SIZE);
          setHistoryError("");
          historyNeedsRefresh.current = false;
        }
      }).catch(() => {
        if (mounted.current && ticket === owner.current && version === historyVersion.current) {
          historyNeedsRefresh.current = true;
          setHistoryError("问题已提交，对话列表暂未更新，请重试加载。");
        }
      });
    } catch (cause) {
      if (mounted.current && ticket === owner.current) {
        const stopWanted = stopRequested.current;
        returnQuestion();
        setError(toUserMessage(cause, "提交失败，请重试；重复提交不会创建重复任务"));
        // 失败不等于没提交：响应可能只是丢了。凡是这次提交带着「替换」或一次还没
        // 兑现的「停止」，就以服务端为准重新同步一次会话（`readConversation`）：
        //   · 会话里多出一条我们没见过的、问题相同的作业——那就是这次提交：认领它
        //     （清掉弹回的问题与错误），不给用户一个会建出重复回答的「重试」；用户
        //     按过停止的话，对它兑现那次停止；
        //   · 本地那条「已停止」记录在服务端已经不在了——换上真实的轮次，并作废这次
        //     的重试身份：下一次提交按新状态重新算要不要替换；
        //   · 会话本身不在了——退回「还没有会话」。
        if ((replaces || stopWanted) && conversationId) {
          const known = new Set(currentTurns.current.map((item) => item.job_id));
          const alive = () => mounted.current && ticket === owner.current && serial === submitSerial.current;
          void readConversation(conversationId).then(async (synced) => {
            if (!alive() || synced === null) return;
            retryRequest.current = null;
            if (synced === "gone") { dropConversationIdentity(conversationId); return; }
            applyConversation(synced);
            const accepted = synced.turns.find((item) => !known.has(item.job_id) && item.question === question);
            if (!accepted) return;
            setDraft((draft) => draft.trim() === question ? "" : draft);
            setError("");
            if (stopWanted && accepted.status === "running") {
              const outcome = await discardJob(accepted, alive);
              if (alive() && outcome === "failed") setError("停止失败，请重试");
            }
          });
        }
      }
    } finally {
      if (ticket === owner.current) {
        flight.current = false;
        stopRequested.current = false;
        if (mounted.current) { setSubmitting(false); setStopping(false); }
      }
    }
  }

  // ------------------------------------------------------------------
  // 与服务端对账的**唯一**接缝。
  //
  // 替换与丢弃都会让服务端的作业、乃至整条会话消失，而响应可能丢、别的标签页也可能
  // 先动手。凡是本地与服务端可能已经不一致的地方——停止并丢弃之后、取消失败之后、
  // 带替换 / 带停止的提交失败之后、轮询读到 404 之后——一律**重读会话、以它为准**，
  // 不在各个调用点各猜各的（那样每个点都要重新发明「游标退一格」「会话还在不在」）。
  // ------------------------------------------------------------------

  /** 重读一条会话。`"gone"` = 服务端已经没有它；`null` = 这次没读到（网络 / 5xx），
   *  什么都不能断言。 */
  async function readConversation(id: string) {
    try {
      return await getGlobalConversation(id);
    } catch (cause) {
      return httpErrorStatus(cause) === 404 ? "gone" as const : null;
    }
  }

  /** 把服务端的第一页装进视图：轮次与「更早的问答」游标一起换，两者因此不会错位。 */
  function applyConversation(detail: GlobalConversationDetail) {
    ++turnsGeneration.current;
    setTurns(detail.turns);
    setTurnOffset(detail.has_more ? detail.next_offset : null);
    setConversationTitle(detail.title || "");
  }

  /** 会话在服务端已经不存在：退回「还没有会话」，历史列表与它的 OFFSET 游标同步。 */
  function dropConversationIdentity(id: string) {
    ++turnsGeneration.current;
    setTurns([]);
    setTurnOffset(null);
    retryRequest.current = null;
    if (!currentId.current || currentId.current === id) {
      currentId.current = "";
      setConversationId("");
      setConversationTitle("");
      updateUrl("");
    }
    ++historyVersion.current;
    if (conversations.some((item) => item.id === id)) setHistoryOffset((offset) => Math.max(0, offset - 1));
    setConversations((items) => items.filter((item) => item.id !== id));
  }

  /** 情形二的问题交还：pending 轮撤掉，输入框空着就把问题放回去。 */
  function handBack(question: string) {
    showPending(null);
    setDraft((draft) => draft.trim() ? draft : question);
  }

  /**
   * 停掉一条还没有任何过程输出的作业，并以服务端为准收尾。
   *   · `null`——它已经不在了（连同它开出来的空会话）：问题已交还输入框；
   *   · 一条作业——它还在：别处先一步停了它、它抢先答完了、或这次没能确认删除。
   *     交回的是服务端那一份**完整**的作业；
   *   · `"failed"`——取消没成、也读不到会话：什么都没变，停止键仍可重试。
   */
  async function discardJob(target: GlobalJob, alive: () => boolean): Promise<GlobalJob | null | "failed"> {
    let answered: GlobalJob | null = null;
    try {
      answered = await cancelGlobalJob(target.job_id, true);
    } catch { /* 响应丢了不等于没停掉：下面照样对账。 */ }
    // 抢在取消到达之前已经答完 / 失败：取消接口交回的就是终态，无需对账。
    if (answered && answered.status !== "cancelled") return answered;
    const synced = await readConversation(target.conversation_id);
    if (!alive()) return answered ?? "failed";
    if (synced === null) return answered ?? "failed";
    if (synced === "gone") {
      dropConversationIdentity(target.conversation_id);
      handBack(target.question);
      return null;
    }
    applyConversation(synced);
    const still = synced.turns.find((item) => item.job_id === target.job_id);
    // 还在跑、而取消请求又没成：这就是一次失败的停止，停止键留着重试。
    if (still) return !answered && still.status === "running" ? "failed" : still;
    handBack(target.question);
    return null;
  }

  /**
   * 停止。全站同一套取消风格（`stopped-turn.tsx`）：
   *   · 还没有过程输出——问题理解中、提交还没回来、或作业建好了但什么都还没上屏——
   *     问题回到输入框，不留记录（服务端一并丢弃）；
   *   · 已有检索 / 推理过程上屏——这一轮留在对话里，可「编辑问题」，下一次提问替换它。
   */
  async function stop() {
    if (intentChecking) { abortIntent(); return; }
    if (!running) {
      // 提交请求在途：作业 id 还不知道。记下来，`submitJob` 拿到作业就停掉并丢弃。
      if (submitting && pendingRef.current) { stopRequested.current = true; setStopping(true); }
      return;
    }
    if (flight.current) return;
    const ticket = owner.current;
    const target = running;
    const alive = () => mounted.current && ticket === owner.current;
    flight.current = true;
    setStopping(true);
    try {
      if (globalJobHasProcessOutput(target)) {
        // 情形一：停下来，记录留在对话里。
        const job = await cancelGlobalJob(target.job_id, false);
        if (alive()) {
          setPollError("");
          setTurns((items) => items.map((item) => item.job_id === job.job_id ? mergeJob(item, job) : item));
        }
      } else {
        const outcome = await discardJob(target, alive);
        if (alive()) {
          setPollError("");
          if (outcome === "failed") setError("停止失败，请重试");
          else if (outcome) setTurns((items) => items.map((item) => item.job_id === outcome.job_id ? mergeJob(item, outcome) : item));
        }
      }
    } catch (cause) {
      if (alive()) setError(toUserMessage(cause, "停止失败，请重试"));
    } finally {
      if (ticket === owner.current) {
        flight.current = false;
        if (mounted.current) setStopping(false);
      }
    }
  }

  /**
   * 👍/👎 反馈:乐观地把这一轮的 `feedback` 置为 `rating`——按钮立刻高亮并禁用
   * （`AnswerView` 按 `feedbackSent` truthy 禁用），满足「按下必须有可见变化」。
   * 失败则把这一轮的 `feedback` 改回提交前的值，并按其它动作同样的方式给出
   * 中文提示（`error` 状态位，走 `toUserMessage` 的既有人话层）。
   *
   * 命中当前渲染里已有 feedback 的这一轮就直接跳过——按钮本来就已禁用，这里
   * 只是防止一次意外的重复派发覆盖已经发生的反馈。
   *
   * owner/ticket 纪律：切换会话（`load` / `openConversation` / `newConversation`
   * 都会推进 `owner.current`）之后迟到的响应不得写进新会话，同 `submitJob`/
   * `stop` 的既有写法。
   */
  async function sendFeedback(jobId: string, rating: "useful" | "not_useful") {
    const target = turns.find((item) => item.job_id === jobId);
    if (!target || target.feedback) return;
    const ticket = owner.current;
    const previous = target.feedback ?? "";
    setTurns((items) => items.map((item) => item.job_id === jobId ? { ...item, feedback: rating } : item));
    try {
      const updated = await submitGlobalFeedback(jobId, rating);
      if (mounted.current && ticket === owner.current) {
        setTurns((items) => items.map((item) => item.job_id === updated.job_id ? mergeJob(item, updated) : item));
      }
    } catch (cause) {
      if (mounted.current && ticket === owner.current) {
        setTurns((items) => items.map((item) => item.job_id === jobId ? { ...item, feedback: previous } : item));
        setError(toUserMessage(cause, "反馈提交失败，请重试"));
      }
    }
  }

  function updateConversation(updated: GlobalConversation) {
    if (updated.id === currentId.current) setConversationTitle(updated.title || "");
    ++historyVersion.current;
    setConversations((items) => items.map((item) => item.id === updated.id ? updated : item));
  }
  function removeConversation(id: string) {
    ++historyVersion.current;
    setConversations((items) => items.filter((item) => item.id !== id));
    setHistoryOffset((offset) => Math.max(0, offset - 1));
    // Deletion can finish while a submit or cancel is pending. Retire that owner
    // unconditionally so its late response cannot restore the deleted conversation.
    if (currentId.current === id) resetConversation();
  }

  return {
    notebooks, conversations, conversationId, conversationTitle, turns, scope, setScope, draft, setDraft,
    loading, opening, openFailed, submitting, stopping, error, pollError, historyError, running,
    pending, traceSeeds,
    moreHistory, loadingHistory, turnOffset, loadingTurns, loadMoreHistory, loadMoreTurns,
    load, openConversation, newConversation, submit, stop, sendFeedback, updateConversation, removeConversation,
    mode, selectMode, modes: GLOBAL_ASK_MODES, uiMode,
    intentReview, intentChecking, confirmIntent, cancelIntent, abortIntent,
    retryPoll: () => { setPollError(""); setPollRevision((value) => value + 1); },
  };
}
