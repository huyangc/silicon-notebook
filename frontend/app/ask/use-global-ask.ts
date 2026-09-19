import { useCallback, useEffect, useRef, useState } from "react";
import { fetchMe } from "../auth.ts";
import { listNotebooks } from "../notebook-api.ts";
import { askQuestionLimitHint } from "../ask-api.ts";
import { toUserMessage } from "../errors.ts";
import {
  askGlobal, cancelGlobalJob, getGlobalConversation, getGlobalJob, listGlobalConversations,
  GLOBAL_ASK_MAX_NOTEBOOKS, GLOBAL_ASK_PAGE_SIZE, submittableGlobalScope,
  type GlobalConversation, type GlobalJob, type GlobalScope,
} from "../global-ask-api.ts";
import type { NotebookSummary } from "../workspace-model.ts";

const POLL_MIN_INTERVAL_MS = 1200;
const POLL_MAX_INTERVAL_MS = 15000;
const POLL_BACKOFF_FACTOR = 2;

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

export function useGlobalAsk({ syncUrl = true, active = true }: { syncUrl?: boolean; active?: boolean } = {}) {
  const [notebooks, setNotebooks] = useState<NotebookSummary[]>([]);
  const [conversations, setConversations] = useState<GlobalConversation[]>([]);
  const [conversationId, setConversationId] = useState("");
  const [turns, setTurns] = useState<GlobalJob[]>([]);
  const [scope, setScope] = useState<GlobalScope>({ mode: "all" });
  const [draft, setDraft] = useState("");
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

  const load = useCallback(async () => {
    const ticket = ++owner.current;
    const libraryTicket = ++notebookVersion.current;
    const resumeId = currentId.current || (syncUrl ? new URLSearchParams(window.location.search).get("conversation_id") : "");
    const preserveScope = initialized.current;
    const draftScope = currentScope.current;
    ++historyVersion.current;
    setLoading(true);
    setOpenFailed(Boolean(resumeId));
    setError("");
    setPollError("");
    setPollRevision((value) => value + 1);
    setTurns([]);
    setTurnOffset(null);
    setConversationId(resumeId || "");
    try {
      await fetchMe();
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
  }, [syncUrl]);

  useEffect(() => {
    mounted.current = true;
    void load();
    return () => { mounted.current = false; ++owner.current; ++historyVersion.current; };
  }, [load]);

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
    if (flight.current || running || opening || openFailed || loading) return;
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
    flight.current = true;
    setSubmitting(true);
    setError("");
    const ticket = owner.current;
    const key = JSON.stringify({ question, scope, conversationId });
    if (retryRequest.current?.key !== key) retryRequest.current = { key, id: crypto.randomUUID() };
    try {
      const job = await askGlobal({ question, notebook_scope: scope, conversation_id: conversationId || undefined, client_request_id: retryRequest.current.id });
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
    load, openConversation, newConversation, submit, stop, updateConversation, removeConversation,
    retryPoll: () => { setPollError(""); setPollRevision((value) => value + 1); },
  };
}
