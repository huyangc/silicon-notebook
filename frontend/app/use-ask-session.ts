"use client";

import { useEffect, useRef, useState } from "react";
import {
  askQuestionLimitHint,
  bulkDeleteConversations,
  cancelAskJob,
  conversationTitleLimitHint,
  deleteConversation,
  getAskJob,
  getConversation,
  listConversations,
  previewAskIntent,
  renameConversation,
  runAskStream,
  submitFeedback as submitAnswerFeedback,
} from "./ask-api.ts";
import {
  DEFAULT_ASK_MODE,
  groupLabel,
  requiresKg,
  type AskModeId,
  modeFromTurn,
} from "./ask-modes.ts";
import {
  DEFAULT_ASK_RETRIEVAL_EFFORT,
  retrievalEffortFromTurn,
  type AskRetrievalEffortId,
} from "./ask-retrieval-effort.ts";
import {
  buildAskIntentConfirmation,
  type AskIntentConfirmation,
  type QueryIntentContract,
} from "./ask-intent-model.ts";
import {
  elapsedMs,
  handOffIntentTrace,
  intentClarifyStep,
  intentConfirmedStep,
  intentUnderstandingStep,
  intentUnderstoodStep,
  replaceLastIntentStep,
} from "./ask-intent-trace.ts";
import { jobPollDone, newTraceSteps } from "./ask-reconnect.ts";
import { toUserMessage } from "./errors.ts";
import { mergeSessionListFallback, recordStartedConversation } from "./ask-session-state.ts";
import {
  conversationCleanupToast,
  reconcileConversationCleanup,
  type ConversationCleanupResult,
} from "./conversation-cleanup.ts";
import type { BaseScopePayload, SourceScopePayload } from "./source-scope.ts";
import type { ReasoningTraceStep } from "./ask-stream.ts";
import type { AskResponse, ChatTurn, ConversationSummary } from "./workspace-model.ts";
import {
  followLatestNotebookRequest,
  restoreLatestConversation,
  sessionListRequestIsCurrent,
} from "./workspace-transitions.ts";

const RECONNECT_POLL_MS = 1500;
const RECONNECT_CAP_MS = 20 * 60 * 1000;

type AskPolicy = Readonly<{
  advanced: boolean;
  askUnavailable: boolean;
  scopeBlocked: boolean;
  kgAvailable: boolean;
  sourceScope: SourceScopePayload;
  baseScope: BaseScopePayload;
}>;

type AskEffects = Readonly<{
  notify(message: string): void;
  reportError(error: unknown): void;
  ensureAskVisible(): void;
}>;

type UseAskSessionOptions = Readonly<{
  actorId: string | null;
  notebookId: string | null;
  policy: AskPolicy;
  effects: AskEffects;
}>;

export type AskSessionOwner = Readonly<{
  actorId: string;
  notebookId: string;
  workspaceEpoch: number;
  notebookGeneration: number;
  viewGeneration: number;
}>;

export type AskNotebookTransition = AskSessionOwner;

type IntentReview = Readonly<{
  flowGeneration: number;
  notebookId: string;
  conversationId: string | null;
  question: string;
  contract: QueryIntentContract;
  understandingMs: number;
  askedAt: string;
  sourceScope: SourceScopePayload;
  baseScope: BaseScopePayload;
}>;

type SessionRequest = {
  notebookId: string;
  notebookGeneration: number;
  requestId: number;
  promise: Promise<ConversationSummary[]>;
};

function sameNotebookOwner(
  current: AskSessionOwner | null,
  expected: Pick<AskSessionOwner, "actorId" | "notebookId" | "notebookGeneration">,
): boolean {
  return Boolean(
    current
    && current.actorId === expected.actorId
    && current.notebookId === expected.notebookId
    && current.notebookGeneration === expected.notebookGeneration,
  );
}

function sameViewOwner(current: AskSessionOwner | null, expected: AskSessionOwner): boolean {
  return Boolean(
    sameNotebookOwner(current, expected)
    && current?.workspaceEpoch === expected.workspaceEpoch
    && current.viewGeneration === expected.viewGeneration,
  );
}

function copySourceScope(scope: SourceScopePayload): SourceScopePayload {
  return { mode: scope.mode, source_ids: [...scope.source_ids] };
}

function copyBaseScope(scope: BaseScopePayload): BaseScopePayload {
  return { mode: scope.mode, notebook_ids: [...scope.notebook_ids] };
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

function ownerKey(owner: Pick<AskSessionOwner, "actorId" | "notebookId">): string {
  return `${owner.actorId}\u0000${owner.notebookId}`;
}

function sameNotebookIdentity(
  current: AskSessionOwner | null,
  expected: Pick<AskSessionOwner, "actorId" | "notebookId">,
): boolean {
  return Boolean(
    current
    && current.actorId === expected.actorId
    && current.notebookId === expected.notebookId,
  );
}

export function useAskSession({ actorId, notebookId, policy, effects }: UseAskSessionOptions) {
  const [question, setQuestion] = useState("");
  const [turns, setTurns] = useState<ChatTurn[]>([]);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [sessions, setSessions] = useState<ConversationSummary[]>([]);
  const [asking, setAsking] = useState(false);
  const [intentChecking, setIntentChecking] = useState(false);
  const [intentReview, setIntentReview] = useState<IntentReview | null>(null);
  const [sessionLoading, setSessionLoading] = useState(false);
  const [pendingQuestion, setPendingQuestion] = useState("");
  const [pendingAskedAt, setPendingAskedAt] = useState("");
  const [pendingMode, setPendingMode] = useState<AskModeId>(DEFAULT_ASK_MODE);
  const [pendingTrace, setPendingTrace] = useState<ReasoningTraceStep[]>([]);
  const [mode, setMode] = useState<AskModeId>(DEFAULT_ASK_MODE);
  const [retrievalEffort, setRetrievalEffort] = useState<AskRetrievalEffortId>(
    DEFAULT_ASK_RETRIEVAL_EFFORT,
  );
  const [sessionPanelOpen, setSessionPanelOpen] = useState(false);
  const [renamingSessionId, setRenamingSessionId] = useState<string | null>(null);
  const [sessionTitleDraft, setSessionTitleDraft] = useState("");
  const [feedbackSent, setFeedbackSent] = useState<Record<string, string>>({});
  const [reconnectJob, setReconnectJob] = useState<{ jobId: string; seen: number } | null>(null);
  const [ownerSerial, setOwnerSerial] = useState(0);

  const policyRef = useRef(policy);
  policyRef.current = policy;
  const effectsRef = useRef(effects);
  effectsRef.current = effects;
  const actorIdRef = useRef(actorId);
  const notebookIdRef = useRef(notebookId);
  notebookIdRef.current = notebookId;
  const propActorIdRef = useRef(actorId);
  const pendingActorIdRef = useRef<string | null>(null);
  const ownerRef = useRef<AskSessionOwner | null>(null);
  const notebookGenerationRef = useRef(0);
  const viewGenerationRef = useRef(0);
  const conversationIdRef = useRef(conversationId);
  const modeRef = useRef(mode);
  conversationIdRef.current = conversationId;
  modeRef.current = mode;

  const askAbortRef = useRef<AbortController | null>(null);
  const askIntentAbortRef = useRef<AbortController | null>(null);
  const askIntentFlowRef = useRef<"idle" | "preview" | "review" | "submitting">("idle");
  const askIntentFlowGenerationRef = useRef(0);
  const askIntentTraceRef = useRef<ReasoningTraceStep[]>([]);
  const askIntentDraftRef = useRef("");
  const askIntentDraftOwnerRef = useRef<object | null>(null);
  const askJobIdRef = useRef<string | null>(null);
  const askNotebookIdRef = useRef<string | null>(null);
  const sessionListRequestRef = useRef(0);
  const latestSessionListRef = useRef<SessionRequest | null>(null);
  const optimisticConversationIdsRef = useRef<Set<string>>(new Set());
  const cancelRequestedControllersRef = useRef<Set<AbortController>>(new Set());
  const cancelRequestsInFlightRef = useRef<Set<string>>(new Set());
  const reconnectConversationIdRef = useRef<string | null>(null);
  const renameRequestRef = useRef(0);
  const renameViewGenerationRef = useRef(0);
  const feedbackRequestRef = useRef<Map<string, number>>(new Map());
  const deletedConversationIdsRef = useRef<Map<string, Set<string>>>(new Map());

  if (pendingActorIdRef.current === actorId) pendingActorIdRef.current = null;
  if (propActorIdRef.current !== actorId) {
    if (!(pendingActorIdRef.current && actorId === null)) {
      pendingActorIdRef.current = null;
      propActorIdRef.current = actorId;
      actorIdRef.current = actorId;
      if (ownerRef.current && ownerRef.current.actorId !== actorId) {
        ownerRef.current = null;
        notebookGenerationRef.current += 1;
        viewGenerationRef.current += 1;
      }
    }
  } else if (!pendingActorIdRef.current) {
    actorIdRef.current = actorId;
  }

  const ownerIsVisible = Boolean(
    ownerRef.current
    && ownerRef.current.actorId === actorIdRef.current
    && ownerRef.current.notebookId === notebookId,
  );
  const ownerBelongsToActor = Boolean(
    ownerRef.current && ownerRef.current.actorId === actorIdRef.current,
  );

  function clearPendingTurn() {
    setPendingQuestion("");
    setPendingAskedAt("");
    setPendingMode(DEFAULT_ASK_MODE);
    setPendingTrace([]);
  }

  function abortIntentPreview() {
    const wasIntentPhase = askIntentFlowRef.current === "preview"
      || askIntentFlowRef.current === "review";
    askIntentAbortRef.current?.abort();
    askIntentAbortRef.current = null;
    askIntentFlowGenerationRef.current += 1;
    askIntentFlowRef.current = "idle";
    askIntentTraceRef.current = [];
    askIntentDraftRef.current = "";
    askIntentDraftOwnerRef.current = null;
    setIntentChecking(false);
    setIntentReview(null);
    if (wasIntentPhase) clearPendingTurn();
  }

  function releaseIntentDraft(token: object): boolean {
    if (askIntentDraftOwnerRef.current !== token) return false;
    askIntentDraftRef.current = "";
    askIntentDraftOwnerRef.current = null;
    return true;
  }

  function detachVisibleRun() {
    viewGenerationRef.current += 1;
    abortIntentPreview();
    askAbortRef.current = null;
    askJobIdRef.current = null;
    askNotebookIdRef.current = null;
    setAsking(false);
    setReconnectJob(null);
    clearPendingTurn();
  }

  function resetConversationView() {
    setQuestion("");
    setTurns([]);
    setConversationId(null);
    setMode(DEFAULT_ASK_MODE);
    setRetrievalEffort(DEFAULT_ASK_RETRIEVAL_EFFORT);
    setFeedbackSent({});
    setSessionPanelOpen(false);
    setRenamingSessionId(null);
    setSessionTitleDraft("");
    clearPendingTurn();
  }

  function activateActor(nextActorId: string) {
    if (!nextActorId || actorIdRef.current === nextActorId || actorIdRef.current !== null) return;
    actorIdRef.current = nextActorId;
    propActorIdRef.current = nextActorId;
    pendingActorIdRef.current = nextActorId;
    ownerRef.current = null;
    notebookGenerationRef.current += 1;
    viewGenerationRef.current += 1;
  }

  function beginNotebookTransition(input: {
    actorId: string;
    notebookId: string;
    workspaceEpoch: number;
  }): AskNotebookTransition | null {
    if (!input.actorId || actorIdRef.current !== input.actorId) return null;
    detachVisibleRun();
    notebookGenerationRef.current += 1;
    optimisticConversationIdsRef.current.clear();
    latestSessionListRef.current = null;
    const owner: AskSessionOwner = {
      ...input,
      notebookGeneration: notebookGenerationRef.current,
      viewGeneration: viewGenerationRef.current,
    };
    ownerRef.current = owner;
    setSessionLoading(true);
    setSessions([]);
    resetConversationView();
    setOwnerSerial((value) => value + 1);
    return owner;
  }

  function finishNotebookTransition(owner: AskNotebookTransition) {
    if (sameViewOwner(ownerRef.current, owner)) setSessionLoading(false);
  }

  function leaveWorkspace() {
    detachVisibleRun();
    ownerRef.current = null;
    notebookGenerationRef.current += 1;
    setSessionLoading(false);
    setSessions([]);
    resetConversationView();
    setOwnerSerial((value) => value + 1);
  }

  function abortForLogout() {
    askIntentAbortRef.current?.abort();
    askAbortRef.current?.abort();
    leaveWorkspace();
  }

  function currentNotebookOwner(): AskSessionOwner | null {
    const owner = ownerRef.current;
    return owner
      && owner.actorId === actorIdRef.current
      && owner.notebookId === notebookIdRef.current
      ? owner
      : null;
  }

  async function loadSessionsFor(
    expected: Pick<AskSessionOwner, "actorId" | "notebookId" | "notebookGeneration">,
  ): Promise<ConversationSummary[] | null> {
    if (!sameNotebookOwner(ownerRef.current, expected)) return null;
    const requestId = ++sessionListRequestRef.current;
    const request: SessionRequest = {
      notebookId: expected.notebookId,
      notebookGeneration: expected.notebookGeneration,
      requestId,
      promise: listConversations(expected.notebookId),
    };
    latestSessionListRef.current = request;
    const resolved = await followLatestNotebookRequest(
      request,
      () => latestSessionListRef.current,
      () => sameNotebookOwner(ownerRef.current, expected),
    );
    if (!resolved || !sameNotebookOwner(ownerRef.current, expected)) return null;
    const deletedIds = deletedConversationIdsRef.current.get(ownerKey(expected));
    const visible = deletedIds?.size
      ? resolved.value.filter((session) => !deletedIds.has(session.id))
      : resolved.value;
    if (sessionListRequestIsCurrent(
      resolved.generationId,
      sessionListRequestRef.current,
      expected.notebookId,
      ownerRef.current?.notebookId ?? null,
    )) {
      if (resolved.requestId === resolved.generationId) {
        optimisticConversationIdsRef.current.clear();
        setSessions(visible);
      } else {
        setSessions((current) => mergeSessionListFallback(
          current,
          visible,
          optimisticConversationIdsRef.current,
        ).filter((session) => !deletedIds?.has(session.id)));
      }
    }
    return visible;
  }

  async function refreshSessions(): Promise<ConversationSummary[] | null> {
    const owner = currentNotebookOwner();
    return owner ? loadSessionsFor(owner) : null;
  }

  async function applySessionDetail(id: string, expected: AskSessionOwner): Promise<boolean> {
    const detail = await getConversation(id);
    if (
      !sameViewOwner(ownerRef.current, expected)
      || detail.notebook_id !== expected.notebookId
      || deletedConversationIdsRef.current.get(ownerKey(expected))?.has(id)
    ) {
      return false;
    }
    const summary: ConversationSummary = {
      id: detail.id,
      title: detail.title,
      updated_at: detail.updated_at,
      turn_count: detail.turn_count,
      used_reasoning: detail.used_reasoning ?? Boolean(
        detail.turns[detail.turns.length - 1]?.response.reasoning_trace?.length,
      ),
    };
    setSessions((current) => current.some((session) => session.id === detail.id)
      ? current.map((session) => session.id === detail.id ? summary : session)
      : [summary, ...current]);
    setTurns(detail.turns.map((turn) => ({
      question: turn.question,
      response: turn.response,
      askedAt: turn.asked_at,
    })));
    setMode(modeFromTurn(detail.turns[detail.turns.length - 1]));
    setRetrievalEffort(retrievalEffortFromTurn(detail.turns[detail.turns.length - 1]));
    setConversationId(id);
    clearPendingTurn();
    effectsRef.current.ensureAskVisible();
    setSessionPanelOpen(false);
    setRenamingSessionId(null);
    askAbortRef.current = null;
    const active = detail.active_job;
    if (active) {
      setPendingQuestion(active.question);
      setPendingAskedAt(active.asked_at);
      setPendingMode(modeFromTurn({ response: { mode: active.mode } }));
      setPendingTrace(active.trace ?? []);
      setAsking(true);
      askJobIdRef.current = active.job_id;
      askNotebookIdRef.current = expected.notebookId;
      reconnectConversationIdRef.current = id;
      setReconnectJob({ jobId: active.job_id, seen: (active.trace ?? []).length });
    } else {
      setReconnectJob(null);
      setAsking(false);
      askJobIdRef.current = null;
      askNotebookIdRef.current = null;
    }
    return true;
  }

  async function restoreNotebook(owner: AskNotebookTransition): Promise<boolean> {
    if (!sameViewOwner(ownerRef.current, owner)) return false;
    try {
      const list = await loadSessionsFor(owner);
      if (!sameViewOwner(ownerRef.current, owner)) return false;
      await restoreLatestConversation(
        list ?? [],
        (id) => applySessionDetail(id, owner),
      );
    } catch (error) {
      if (sameViewOwner(ownerRef.current, owner)) throw error;
      return false;
    }
    return sameViewOwner(ownerRef.current, owner);
  }

  function nextSessionOwner(workspaceEpoch: number): AskSessionOwner | null {
    const current = currentNotebookOwner();
    if (!current) return null;
    detachVisibleRun();
    const owner = {
      ...current,
      workspaceEpoch,
      viewGeneration: viewGenerationRef.current,
    };
    ownerRef.current = owner;
    setOwnerSerial((value) => value + 1);
    return owner;
  }

  async function openSession(id: string, workspaceEpoch: number) {
    const owner = nextSessionOwner(workspaceEpoch);
    if (!owner) return false;
    setSessionLoading(true);
    try {
      return await applySessionDetail(id, owner);
    } catch (error) {
      if (sameViewOwner(ownerRef.current, owner)) throw error;
      return false;
    } finally {
      if (sameViewOwner(ownerRef.current, owner)) setSessionLoading(false);
    }
  }

  function startNewSession(workspaceEpoch: number) {
    const owner = nextSessionOwner(workspaceEpoch);
    if (!owner) return;
    setSessionLoading(false);
    resetConversationView();
    effectsRef.current.ensureAskVisible();
  }

  function releaseQuestion(value: string) {
    if (!currentNotebookOwner()) return;
    setQuestion(value);
  }

  function selectMode(next: AskModeId) {
    if (!currentNotebookOwner()) return;
    setMode(next);
  }

  function selectRetrievalEffort(next: AskRetrievalEffortId) {
    if (!currentNotebookOwner()) return;
    setRetrievalEffort(next);
  }

  useEffect(() => {
    if (!policy.advanced && mode === "graph") setMode("reasoning");
  }, [policy.advanced, mode]);

  useEffect(() => {
    abortIntentPreview();
  }, [conversationId, mode]);

  async function executeAsk(
    nextQuestion: string,
    selectedMode: AskModeId,
    intent: AskIntentConfirmation | undefined,
    traceSeed: ReasoningTraceStep[] = [],
    askedAt = new Date().toISOString(),
    scopeSnapshot = {
      sourceScope: copySourceScope(policyRef.current.sourceScope),
      baseScope: copyBaseScope(policyRef.current.baseScope),
    },
  ): Promise<boolean> {
    const owner = currentNotebookOwner();
    if (!owner || asking || sessionLoading) return false;
    const q = nextQuestion.trim();
    if (!q) return false;
    const currentPolicy = policyRef.current;
    if (currentPolicy.askUnavailable || currentPolicy.scopeBlocked) {
      effectsRef.current.notify(currentPolicy.scopeBlocked && currentPolicy.advanced
        ? "当前检索范围为空，请至少选择一个来源或参考库。"
        : "请先添加来源，或在「设置 → 编辑当前笔记本」里挂载一个参考库，再开始对话。");
      return false;
    }
    if (requiresKg(selectedMode) && !currentPolicy.kgAvailable) {
      effectsRef.current.notify(`${groupLabel("strict")}需要知识图谱 — 可在「设置 → 编辑当前笔记本」里挂一个参考库，或先整理该笔记本的知识图谱`);
      return false;
    }
    const runOwner = owner;
    const conversationIdAtStart = conversationIdRef.current;
    let startedConversationId = conversationIdAtStart;
    const ownsRun = () => sameViewOwner(ownerRef.current, runOwner);
    effectsRef.current.ensureAskVisible();
    setQuestion("");
    setPendingQuestion(q);
    setPendingAskedAt(askedAt);
    setPendingMode(selectedMode);
    setPendingTrace(traceSeed);
    setAsking(true);
    const controller = new AbortController();
    askJobIdRef.current = null;
    askAbortRef.current = controller;
    askNotebookIdRef.current = runOwner.notebookId;
    try {
      const payload = {
        question: q,
        asked_at: askedAt,
        conversation_id: conversationIdAtStart ?? undefined,
        mode: selectedMode,
        retrieval_effort: currentPolicy.advanced
          ? retrievalEffort
          : DEFAULT_ASK_RETRIEVAL_EFFORT,
        source_scope: scopeSnapshot.sourceScope,
        base_scope: scopeSnapshot.baseScope,
        ...(intent ? { intent } : {}),
      };
      const response = await runAskStream<AskResponse>(
        runOwner.notebookId,
        payload,
        (step) => {
          if (ownsRun()) setPendingTrace((previous) => [...previous, step]);
        },
        controller.signal,
        async (jobId, durableConversationId) => {
          startedConversationId = durableConversationId;
          if (cancelRequestedControllersRef.current.delete(controller)) {
            try {
              await cancelAskJob(runOwner.notebookId, jobId);
            } catch {
              if (sameNotebookIdentity(ownerRef.current, runOwner)) {
                effectsRef.current.notify("未能中断后台任务；任务将继续完成，可稍后重开查看");
              }
              return;
            }
            controller.abort();
            return;
          }
          const ownsVisibleRun = ownsRun();
          if (ownsVisibleRun) {
            askJobIdRef.current = jobId;
            setConversationId(durableConversationId);
          }
          const currentOwner = ownerRef.current;
          if (sameNotebookIdentity(currentOwner, runOwner) && currentOwner) {
            optimisticConversationIdsRef.current.add(durableConversationId);
            setSessions((current) => recordStartedConversation(current, {
              conversationId: durableConversationId,
              question: q,
              startedAt: new Date().toISOString(),
            }));
            loadSessionsFor(currentOwner).catch(() => {});
          }
        },
      );
      if (!ownsRun()) {
        const currentOwner = ownerRef.current;
        if (sameNotebookIdentity(currentOwner, runOwner) && currentOwner) {
          await loadSessionsFor(currentOwner).catch(() => {});
        }
        return true;
      }
      setTurns((previous) => [...previous, { question: q, response, askedAt }]);
      setConversationId(response.conversation_id);
    } catch (error) {
      if (!ownsRun()) {
        const currentOwner = ownerRef.current;
        if (sameNotebookIdentity(currentOwner, runOwner) && currentOwner) {
          await loadSessionsFor(currentOwner).catch(() => {});
        }
        return true;
      }
      setQuestion(q);
      if (startedConversationId !== conversationIdAtStart) setConversationId(conversationIdAtStart);
      if (isAbortError(error)) {
        effectsRef.current.notify("已中断回答");
        return true;
      }
      effectsRef.current.reportError(error);
    } finally {
      if (ownsRun()) {
        if (askAbortRef.current === controller) askAbortRef.current = null;
        askJobIdRef.current = null;
        askNotebookIdRef.current = null;
        askIntentTraceRef.current = [];
        clearPendingTurn();
        setAsking(false);
      }
      cancelRequestedControllersRef.current.delete(controller);
    }
    if (ownsRun()) await loadSessionsFor(runOwner).catch(() => {});
    return true;
  }

  async function submit(nextQuestion = question) {
    const owner = currentNotebookOwner();
    if (
      !owner || asking || intentChecking || sessionLoading || intentReview
      || askIntentFlowRef.current !== "idle"
    ) return;
    const q = nextQuestion.trim();
    if (!q || askQuestionLimitHint(q)) return;
    const currentPolicy = policyRef.current;
    if (currentPolicy.askUnavailable || currentPolicy.scopeBlocked) {
      effectsRef.current.notify(currentPolicy.scopeBlocked && currentPolicy.advanced
        ? "当前检索范围为空，请至少选择一个来源或参考库。"
        : "请先添加来源，或在「设置 → 编辑当前笔记本」里挂载一个参考库，再开始对话。");
      return;
    }
    const submitMode = !currentPolicy.advanced && mode === "graph" ? "reasoning" : mode;
    if (requiresKg(submitMode) && !currentPolicy.kgAvailable) {
      effectsRef.current.notify(`${groupLabel("strict")}需要知识图谱 — 可在「设置 → 编辑当前笔记本」里挂一个参考库，或先整理该笔记本的知识图谱`);
      return;
    }
    const askedAt = new Date().toISOString();
    const scopeSnapshot = {
      sourceScope: copySourceScope(currentPolicy.sourceScope),
      baseScope: copyBaseScope(currentPolicy.baseScope),
    };
    if (submitMode !== "reasoning") {
      await executeAsk(q, submitMode, undefined, [], askedAt, scopeSnapshot);
      return;
    }
    const conversationIdAtStart = conversationIdRef.current;
    const previewOwner = owner;
    const controller = new AbortController();
    const flowGeneration = ++askIntentFlowGenerationRef.current;
    askIntentFlowRef.current = "preview";
    askIntentAbortRef.current = controller;
    const draftToken = {};
    askIntentDraftRef.current = q;
    askIntentDraftOwnerRef.current = draftToken;
    askIntentTraceRef.current = [intentUnderstandingStep()];
    setQuestion("");
    setPendingQuestion(q);
    setPendingAskedAt(askedAt);
    setPendingMode("reasoning");
    setPendingTrace(askIntentTraceRef.current);
    setIntentChecking(true);
    const understandingStartedAt = Date.now();
    try {
      const contract = await previewAskIntent(
        previewOwner.notebookId,
        q,
        conversationIdAtStart,
        controller.signal,
        scopeSnapshot.sourceScope,
        scopeSnapshot.baseScope,
      );
      if (
        controller.signal.aborted
        || askIntentFlowGenerationRef.current !== flowGeneration
        || !sameViewOwner(ownerRef.current, previewOwner)
        || conversationIdRef.current !== conversationIdAtStart
        || modeRef.current !== "reasoning"
      ) return;
      const understandingMs = elapsedMs(understandingStartedAt, Date.now());
      if (contract.needs_clarification) {
        askIntentTraceRef.current = replaceLastIntentStep(
          askIntentTraceRef.current,
          intentClarifyStep(contract, understandingMs),
        );
        setPendingTrace(askIntentTraceRef.current);
        askIntentFlowRef.current = "review";
        setIntentReview({
          flowGeneration,
          notebookId: previewOwner.notebookId,
          conversationId: conversationIdAtStart,
          question: q,
          contract,
          understandingMs,
          askedAt,
          sourceScope: scopeSnapshot.sourceScope,
          baseScope: scopeSnapshot.baseScope,
        });
        effectsRef.current.notify("问题存在会改变检索方向的歧义，请先补充确认");
        return;
      }
      askIntentTraceRef.current = replaceLastIntentStep(
        askIntentTraceRef.current,
        intentUnderstoodStep(contract, understandingMs),
      );
      askIntentAbortRef.current = null;
      setIntentChecking(false);
      askIntentFlowRef.current = "submitting";
      const started = await executeAsk(
        q,
        "reasoning",
        buildAskIntentConfirmation(contract, contract.resolved_question, {}, understandingMs),
        handOffIntentTrace(askIntentTraceRef.current),
        askedAt,
        scopeSnapshot,
      );
      if (releaseIntentDraft(draftToken) && !started) {
        setQuestion(q);
        askIntentTraceRef.current = [];
        clearPendingTurn();
      }
    } catch (error) {
      if (
        !isAbortError(error)
        && askIntentFlowGenerationRef.current === flowGeneration
        && sameViewOwner(ownerRef.current, previewOwner)
      ) effectsRef.current.reportError(error);
      const draft = askIntentDraftRef.current;
      if (askIntentAbortRef.current === controller && releaseIntentDraft(draftToken)) {
        setQuestion(draft || q);
        askIntentTraceRef.current = [];
        clearPendingTurn();
      }
    } finally {
      if (askIntentAbortRef.current === controller) {
        askIntentAbortRef.current = null;
        setIntentChecking(false);
      }
      if (
        askIntentFlowGenerationRef.current === flowGeneration
        && askIntentFlowRef.current !== "review"
      ) askIntentFlowRef.current = "idle";
    }
  }

  async function confirmIntent(confirmation: AskIntentConfirmation) {
    const review = intentReview;
    const owner = currentNotebookOwner();
    if (
      !review || !owner || askIntentFlowRef.current !== "review"
      || askIntentFlowGenerationRef.current !== review.flowGeneration
    ) return;
    const flowGeneration = review.flowGeneration;
    const draftToken = askIntentDraftOwnerRef.current ?? {};
    askIntentDraftOwnerRef.current = draftToken;
    if (
      review.notebookId !== owner.notebookId
      || review.conversationId !== conversationIdRef.current
      || modeRef.current !== "reasoning"
    ) {
      setIntentReview(null);
      askIntentTraceRef.current = [];
      releaseIntentDraft(draftToken);
      clearPendingTurn();
      effectsRef.current.notify("问题上下文已经变化，请重新提交");
      return;
    }
    askIntentFlowRef.current = "submitting";
    setIntentReview(null);
    const traceSeed = [
      ...askIntentTraceRef.current,
      intentConfirmedStep(confirmation.resolved_question, confirmation.answers.length),
    ];
    askIntentTraceRef.current = traceSeed;
    const scopeSnapshot = {
      sourceScope: copySourceScope(review.sourceScope),
      baseScope: copyBaseScope(review.baseScope),
    };
    try {
      const started = await executeAsk(
        review.question,
        "reasoning",
        confirmation,
        handOffIntentTrace(traceSeed),
        review.askedAt,
        scopeSnapshot,
      );
      if (releaseIntentDraft(draftToken) && !started) {
        setQuestion(review.question);
        askIntentTraceRef.current = [];
        clearPendingTurn();
      }
    } finally {
      if (askIntentFlowGenerationRef.current === flowGeneration) {
        askIntentFlowRef.current = "idle";
      }
    }
  }

  function cancelIntent() {
    askIntentFlowGenerationRef.current += 1;
    askIntentFlowRef.current = "idle";
    setIntentReview(null);
    setQuestion(askIntentDraftRef.current || intentReview?.question || "");
    askIntentDraftRef.current = "";
    askIntentDraftOwnerRef.current = null;
    askIntentTraceRef.current = [];
    clearPendingTurn();
    effectsRef.current.notify("已返回修改问题");
  }

  function abort() {
    if (!currentNotebookOwner()) return;
    if (intentChecking) {
      const draft = askIntentDraftRef.current;
      abortIntentPreview();
      if (draft) setQuestion(draft);
      effectsRef.current.notify("已取消问题理解");
      return;
    }
    const jobId = askJobIdRef.current;
    const activeNotebook = askNotebookIdRef.current;
    const controller = askAbortRef.current;
    if (jobId && activeNotebook) {
      const owner = currentNotebookOwner();
      const cancelKey = `${activeNotebook}\u0000${jobId}`;
      if (cancelRequestsInFlightRef.current.has(cancelKey)) return;
      cancelRequestsInFlightRef.current.add(cancelKey);
      cancelAskJob(activeNotebook, jobId)
        .then(() => {
          // A durable Ask job is not cancelled merely because its transport is
          // disconnected. Keep reading until the authoritative cancel request
          // succeeds; otherwise a transient cancel failure would strand a
          // still-running backend job while also throwing away the only local
          // controller that can retry Stop.
          controller?.abort();
          if (askJobIdRef.current === jobId) askJobIdRef.current = null;
          if (askNotebookIdRef.current === activeNotebook) askNotebookIdRef.current = null;
          if (askAbortRef.current === controller) askAbortRef.current = null;
          const currentOwner = ownerRef.current;
          if (owner && sameNotebookIdentity(currentOwner, owner) && currentOwner) {
            return loadSessionsFor(currentOwner);
          }
          return null;
        })
        .catch(() => {
          const stillActive = askJobIdRef.current === jobId || askAbortRef.current === controller;
          if (stillActive && owner && sameViewOwner(ownerRef.current, owner)) {
            effectsRef.current.notify("取消失败，请重试");
          }
        })
        .finally(() => { cancelRequestsInFlightRef.current.delete(cancelKey); });
    } else if (controller) {
      cancelRequestedControllersRef.current.add(controller);
      viewGenerationRef.current += 1;
      const currentOwner = ownerRef.current;
      if (currentOwner) {
        ownerRef.current = {
          ...currentOwner,
          viewGeneration: viewGenerationRef.current,
        };
        setOwnerSerial((value) => value + 1);
      }
      if (askAbortRef.current === controller) askAbortRef.current = null;
      askJobIdRef.current = null;
      askNotebookIdRef.current = null;
      setQuestion(pendingQuestion);
      clearPendingTurn();
      setAsking(false);
      effectsRef.current.notify("正在中断回答");
    }
  }

  useEffect(() => {
    if (!reconnectJob) return;
    const owner = ownerRef.current;
    if (!owner) return;
    const jobId = reconnectJob.jobId;
    let stopped = false;
    let terminalHandled = false;
    let seen = reconnectJob.seen;
    let timer: number | undefined;
    let capTimer: number | undefined;
    const startedAt = Date.now();

    const stopAtCap = () => {
      if (stopped || !sameViewOwner(ownerRef.current, owner)) return;
      stopped = true;
      terminalHandled = true;
      if (timer !== undefined) window.clearTimeout(timer);
      setReconnectJob(null);
      setAsking(false);
      clearPendingTurn();
      askJobIdRef.current = null;
      askNotebookIdRef.current = null;
      effectsRef.current.notify("该问答仍在后台进行，请稍后重开查看");
    };
    capTimer = window.setTimeout(stopAtCap, RECONNECT_CAP_MS);

    const schedule = () => {
      if (stopped || !sameViewOwner(ownerRef.current, owner)) return;
      if (Date.now() - startedAt >= RECONNECT_CAP_MS) return stopAtCap();
      timer = window.setTimeout(tick, RECONNECT_POLL_MS);
    };

    const tick = async () => {
      if (stopped || !sameViewOwner(ownerRef.current, owner)) return;
      try {
        const detail = await getAskJob(owner.notebookId, jobId);
        if (stopped || !sameViewOwner(ownerRef.current, owner)) return;
        const fresh = newTraceSteps(detail.trace ?? [], seen);
        if (fresh.length) {
          seen += fresh.length;
          setPendingTrace((previous) => [...previous, ...fresh]);
        }
        if (jobPollDone(detail.status)) {
          if (terminalHandled) return;
          terminalHandled = true;
          if (detail.status === "done") {
            try {
              const applied = await applySessionDetail(
                reconnectConversationIdRef.current ?? "",
                owner,
              );
              if (!applied) {
                if (!sameViewOwner(ownerRef.current, owner)) return;
              }
            } catch {
              terminalHandled = false;
              schedule();
              return;
            }
            await loadSessionsFor(owner).catch(() => {});
          } else if (detail.status === "cancelled") {
            effectsRef.current.notify("该问答已被取消");
            await loadSessionsFor(owner).catch(() => {});
          } else {
            effectsRef.current.notify(detail.status === "interrupted"
              ? "该问答因服务重启中断"
              : toUserMessage(
                detail.error ? new Error(detail.error) : null,
                "该问答失败，请稍后重试",
              ));
            await loadSessionsFor(owner).catch(() => {});
          }
          stopped = true;
          if (timer !== undefined) window.clearTimeout(timer);
          if (capTimer !== undefined) window.clearTimeout(capTimer);
          const replacementJobId = askJobIdRef.current;
          if (!replacementJobId || replacementJobId === jobId) {
            setReconnectJob(null);
            setAsking(false);
            clearPendingTurn();
            askJobIdRef.current = null;
            askNotebookIdRef.current = null;
          }
          return;
        }
      } catch {
        // Transient reconnect failures retry on the same bounded cadence.
      }
      schedule();
    };

    schedule();
    return () => {
      stopped = true;
      if (timer !== undefined) window.clearTimeout(timer);
      if (capTimer !== undefined) window.clearTimeout(capTimer);
    };
  }, [reconnectJob?.jobId, ownerSerial]);

  function toggleSessionPanel() {
    if (!currentNotebookOwner()) return;
    setSessionPanelOpen((open) => !open);
  }

  function closeSessionPanel() {
    setSessionPanelOpen(false);
  }

  function beginRenameSession(session: ConversationSummary) {
    if (!currentNotebookOwner()) return;
    renameRequestRef.current += 1;
    renameViewGenerationRef.current += 1;
    setRenamingSessionId(session.id);
    setSessionTitleDraft(session.title || "未命名会话");
    setSessionPanelOpen(true);
  }

  function updateSessionTitleDraft(value: string) {
    renameViewGenerationRef.current += 1;
    setSessionTitleDraft(value);
  }

  function cancelRenameSession() {
    renameRequestRef.current += 1;
    renameViewGenerationRef.current += 1;
    setRenamingSessionId(null);
  }

  async function commitRenameSession(sessionId: string) {
    const owner = currentNotebookOwner();
    if (!owner) return;
    const next = sessionTitleDraft.trim();
    const current = sessions.find((session) => session.id === sessionId);
    if (!next || next === current?.title) {
      if (sameViewOwner(ownerRef.current, owner)) setRenamingSessionId(null);
      return;
    }
    if (conversationTitleLimitHint(next)) return;
    const requestId = ++renameRequestRef.current;
    const viewGeneration = renameViewGenerationRef.current;
    try {
      await renameConversation(sessionId, next);
    } catch (error) {
      if (
        sameViewOwner(ownerRef.current, owner)
        && renameRequestRef.current === requestId
        && renameViewGenerationRef.current === viewGeneration
      ) throw error;
      return;
    }
    const currentOwner = ownerRef.current;
    if (!sameNotebookIdentity(currentOwner, owner) || !currentOwner) return;
    setSessions((currentSessions) => currentSessions.map((session) => (
      session.id === sessionId ? { ...session, title: next } : session
    )));
    await loadSessionsFor(currentOwner).catch(() => {});
    if (
      !sameViewOwner(ownerRef.current, owner)
      || renameRequestRef.current !== requestId
      || renameViewGenerationRef.current !== viewGeneration
    ) return;
    setRenamingSessionId(null);
    effectsRef.current.notify("会话已重命名");
  }

  async function deleteSession(id: string) {
    const owner = currentNotebookOwner();
    if (!owner) return;
    try {
      await deleteConversation(id);
    } catch (error) {
      if (sameViewOwner(ownerRef.current, owner)) throw error;
      return;
    }
    const key = ownerKey(owner);
    const deleted = deletedConversationIdsRef.current.get(key) ?? new Set<string>();
    deleted.add(id);
    deletedConversationIdsRef.current.set(key, deleted);
    const currentOwner = ownerRef.current;
    if (!sameNotebookIdentity(currentOwner, owner)) return;
    setSessions((current) => current.filter((session) => session.id !== id));
    if (conversationIdRef.current === id) {
      setTurns([]);
      setConversationId(null);
      clearPendingTurn();
    }
    if (currentOwner) await loadSessionsFor(currentOwner).catch(() => {});
    if (sameViewOwner(ownerRef.current, owner)) effectsRef.current.notify("会话已删除");
  }

  async function bulkCleanup(days: number) {
    const owner = currentNotebookOwner();
    if (!owner) return;
    let result: ConversationCleanupResult;
    try {
      result = await bulkDeleteConversations(owner.notebookId, days);
    } catch (error) {
      if (sameViewOwner(ownerRef.current, owner)) throw error;
      return;
    }
    const key = ownerKey(owner);
    const deleted = deletedConversationIdsRef.current.get(key) ?? new Set<string>();
    for (const id of result.deleted_ids) deleted.add(id);
    deletedConversationIdsRef.current.set(key, deleted);
    const currentOwner = ownerRef.current;
    if (!sameNotebookIdentity(currentOwner, owner)) return;
    setSessions((currentSessions) => reconcileConversationCleanup(
      currentSessions,
      conversationIdRef.current,
      result.deleted_ids,
    ).sessions);
    if (conversationIdRef.current && deleted.has(conversationIdRef.current)) {
      setTurns([]);
      setConversationId(null);
      clearPendingTurn();
    }
    if (currentOwner) await loadSessionsFor(currentOwner).catch(() => {});
    if (sameViewOwner(ownerRef.current, owner)) {
      effectsRef.current.notify(conversationCleanupToast(result.deleted));
    }
  }

  async function submitFeedback(answerId: string, rating: "useful" | "not_useful", comment = "") {
    const owner = currentNotebookOwner();
    if (!answerId || !owner) return;
    const requestId = (feedbackRequestRef.current.get(answerId) ?? 0) + 1;
    feedbackRequestRef.current.set(answerId, requestId);
    try {
      await submitAnswerFeedback(answerId, rating, comment);
    } catch (error) {
      if (
        sameViewOwner(ownerRef.current, owner)
        && feedbackRequestRef.current.get(answerId) === requestId
      ) throw error;
      return;
    }
    if (
      !sameViewOwner(ownerRef.current, owner)
      || feedbackRequestRef.current.get(answerId) !== requestId
    ) return;
    setFeedbackSent((previous) => ({ ...previous, [answerId]: rating }));
    effectsRef.current.notify("感谢反馈");
  }

  return {
    question: ownerIsVisible ? question : "",
    turns: ownerIsVisible ? turns : [],
    conversationId: ownerIsVisible ? conversationId : null,
    sessions: ownerIsVisible ? sessions : [],
    asking: ownerIsVisible ? asking : false,
    intentChecking: ownerIsVisible ? intentChecking : false,
    intentReview: ownerIsVisible ? intentReview : null,
    sessionLoading: ownerBelongsToActor ? sessionLoading : false,
    pendingQuestion: ownerIsVisible ? pendingQuestion : "",
    pendingAskedAt: ownerIsVisible ? pendingAskedAt : "",
    pendingMode: ownerIsVisible ? pendingMode : DEFAULT_ASK_MODE,
    pendingTrace: ownerIsVisible ? pendingTrace : [],
    mode: ownerIsVisible ? mode : DEFAULT_ASK_MODE,
    retrievalEffort: ownerIsVisible ? retrievalEffort : DEFAULT_ASK_RETRIEVAL_EFFORT,
    sessionPanelOpen: ownerIsVisible ? sessionPanelOpen : false,
    renamingSessionId: ownerIsVisible ? renamingSessionId : null,
    sessionTitleDraft: ownerIsVisible ? sessionTitleDraft : "",
    sessionTitleOverLimit: conversationTitleLimitHint(sessionTitleDraft.trim()),
    feedbackSent: ownerIsVisible ? feedbackSent : {},
    inFlight: ownerIsVisible && (asking || intentChecking || Boolean(intentReview)),
    activateActor,
    beginNotebookTransition,
    finishNotebookTransition,
    restoreNotebook,
    leaveWorkspace,
    abortForLogout,
    openSession,
    startNewSession,
    releaseQuestion,
    selectMode,
    selectRetrievalEffort,
    submit,
    confirmIntent,
    cancelIntent,
    abort,
    refreshSessions,
    toggleSessionPanel,
    closeSessionPanel,
    beginRenameSession,
    updateSessionTitleDraft,
    cancelRenameSession,
    commitRenameSession,
    deleteSession,
    bulkCleanup,
    submitFeedback,
  } as const;
}
