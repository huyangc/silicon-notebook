import { useCallback, useEffect, useRef, useState } from "react";
import { fetchMe } from "../auth.ts";
import { listNotebooks } from "../notebook-api.ts";
import { askQuestionLimitHint } from "../ask-api.ts";
import { toUserMessage } from "../errors.ts";
import {
  askGlobal, cancelGlobalJob, getGlobalConversation, getGlobalJob, listGlobalConversations,
  previewGlobalAskIntent, submitGlobalFeedback,
  GLOBAL_ASK_MAX_NOTEBOOKS, GLOBAL_ASK_PAGE_SIZE, submittableGlobalScope,
  type GlobalConversation, type GlobalJob, type GlobalScope,
} from "../global-ask-api.ts";
import { ASK_MODES, DEFAULT_ASK_MODE, askModeIds, submissionAskMode } from "../ask-modes.ts";
import { isAdvanced, normalizeUiMode, type UiMode } from "../ui-mode.ts";
import { DEFAULT_ASK_RETRIEVAL_EFFORT } from "../ask-retrieval-effort.ts";
import {
  buildAskIntentConfirmation,
  type AskIntentConfirmation,
  type QueryIntentContract,
} from "../ask-intent-model.ts";
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

function progressKey(job: GlobalJob): string {
  return JSON.stringify([job.status, job.searched_notebook_ids, job.skipped_notebooks ?? [], job.degraded_notebook_ids ?? []]);
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
  const retireIntent = useCallback(() => {
    intentAbort.current?.abort();
    intentAbort.current = null;
    setIntentChecking(false);
    setIntentReview(null);
  }, []);

  const load = useCallback(async () => {
    const ticket = ++owner.current;
    const libraryTicket = ++notebookVersion.current;
    const resumeId = currentId.current || (syncUrl ? new URLSearchParams(window.location.search).get("conversation_id") : "");
    const preserveScope = initialized.current;
    const draftScope = currentScope.current;
    ++historyVersion.current;
    // owner 刚被推进，在途预检随之失去归属：必须在同一处退休它（见 retireIntent）。
    retireIntent();
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
    setDraft("");
    setScope({ mode: "all" });
    // 切换对话同样推进了 owner：在途的问题理解与审阅卡一并退休。
    retireIntent();
    try {
      const detail = await getGlobalConversation(id);
      if (!mounted.current || ticket !== owner.current) return;
      setTurns(detail.turns);
      setTurnOffset(detail.has_more ? detail.next_offset : null);
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
    try {
      const detail = await getGlobalConversation(conversationId, turnOffset);
      if (!mounted.current || owner.current !== ticket) return;
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
    // 「逐步推理」与笔记本内问答走同一套交互：先预检问题理解，需要澄清时弹审阅卡，
    // 确认后才带着 intent 提交。通用问答没有这一步，直接建作业。
    if (submissionMode === "reasoning") { await previewIntent(question); return; }
    await submitJob(question);
  }

  /**
   * 跑一次问题理解预检。理解清楚就直接把系统的理解当确认提交；需要澄清就把合同
   * 交给审阅卡。草稿全程不动——取消或失败后用户原地就能改。
   */
  async function previewIntent(question: string) {
    const ticket = owner.current;
    const controller = new AbortController();
    intentAbort.current = controller;
    setIntentChecking(true);
    setError("");
    const startedAt = Date.now();
    try {
      const contract = await previewGlobalAskIntent(
        question, conversationId || undefined, scope, controller.signal,
      );
      // `signal.aborted` 也要查：流可能已经返回、而用户恰在这一瞬点了「取消问题
      // 理解」。只 abort 请求不查这一下，作业照样会被建出来——取消变成了假的。
      if (!mounted.current || ticket !== owner.current || controller.signal.aborted) return;
      const understandingMs = Math.max(0, Date.now() - startedAt);
      if (contract.needs_clarification) {
        setIntentReview({ question, contract, understandingMs });
        return;
      }
      await submitJob(question, buildAskIntentConfirmation(
        contract, contract.resolved_question, {}, understandingMs,
      ));
    } catch (cause) {
      if (!mounted.current || ticket !== owner.current) return;
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
    await submitJob(review.question, confirmation);
  }

  function cancelIntent() {
    setIntentReview(null);
  }

  async function submitJob(question: string, intent?: AskIntentConfirmation) {
    flight.current = true;
    setSubmitting(true);
    setError("");
    const ticket = owner.current;
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
    const key = JSON.stringify({ question, scope, conversationId, mode: submissionMode, edited });
    if (retryRequest.current?.key !== key) retryRequest.current = { key, id: crypto.randomUUID() };
    try {
      const job = await askGlobal({
        question,
        notebook_scope: scope,
        conversation_id: conversationId || undefined,
        client_request_id: retryRequest.current.id,
        mode: submissionMode,
        intent,
        retrieval_effort: GLOBAL_ASK_RETRIEVAL_EFFORT,
      });
      if (!mounted.current || ticket !== owner.current) return;
      setConversationId(job.conversation_id);
      setTurns((items) => [...items.filter((item) => item.job_id !== job.job_id), job]);
      setDraft("");
      setPollError("");
      retryRequest.current = null;
      updateUrl(job.conversation_id);
      const version = ++historyVersion.current;
      void listGlobalConversations().then((history) => {
        if (mounted.current && ticket === owner.current && version === historyVersion.current) {
          setConversations(history);
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
      if (mounted.current && ticket === owner.current) setError(toUserMessage(cause, "提交失败，请重试；重复提交不会创建重复任务"));
    } finally {
      if (ticket === owner.current) {
        flight.current = false;
        if (mounted.current) setSubmitting(false);
      }
    }
  }

  async function stop() {
    if (!running || flight.current) return;
    const ticket = owner.current;
    flight.current = true;
    setStopping(true);
    try {
      const job = await cancelGlobalJob(running.job_id);
      if (mounted.current && ticket === owner.current) {
        setTurns((items) => items.map((item) => item.job_id === job.job_id ? mergeJob(item, job) : item));
        setPollError("");
      }
    } catch (cause) {
      if (mounted.current && ticket === owner.current) setError(toUserMessage(cause, "停止失败，请重试"));
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
    notebooks, conversations, conversationId, turns, scope, setScope, draft, setDraft,
    loading, opening, openFailed, submitting, stopping, error, pollError, historyError, running,
    moreHistory, loadingHistory, turnOffset, loadingTurns, loadMoreHistory, loadMoreTurns,
    load, openConversation, newConversation, submit, stop, sendFeedback, updateConversation, removeConversation,
    mode, selectMode, modes: GLOBAL_ASK_MODES, uiMode,
    intentReview, intentChecking, confirmIntent, cancelIntent, abortIntent,
    retryPoll: () => { setPollError(""); setPollRevision((value) => value + 1); },
  };
}
