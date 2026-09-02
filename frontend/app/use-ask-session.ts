"use client";

import { useEffect, useRef, useState } from "react";
import {
  askQuestionLimitHint,
  bulkDeleteConversations,
  cancelAskJob,
  conversationTitleLimitHint,
  deleteConversation,
  fetchAskModes,
  getAskJob,
  getConversation,
  listConversations,
  previewAskIntent,
  renameConversation,
  runAskStream,
  submitFeedback as submitAnswerFeedback,
} from "./ask-api.ts";
import {
  ASK_MODES,
  AUTO_ASK_MODE,
  DEFAULT_ASK_MODE,
  groupOf,
  groupLabel,
  normalizeAskModeProjection,
  requiresKg,
  type AskModeDef,
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

// Hidden-state (owner not visible) fallback values must be **stable references**.
// The returned view is read by page.tsx effects/useMemo that depend on these
// fields; handing back a brand-new `[]`/`{}` on every render makes those
// dependencies "change" every render. One such effect calls setState inside
// its body, which turns into an infinite render loop (Next.js reports
// "Maximum update depth exceeded"). Freezing them also makes any accidental
// in-place write from a consumer throw immediately in development.
// Declared with the same (mutable-looking) type as the state they stand in
// for, so the ternary branches unify cleanly instead of widening the return
// shape to a `T[] | readonly T[]` union for every consumer. `Object.freeze`
// still makes the cast a lie only at the type level, not at runtime; the
// inner `as` on the literal avoids TS inferring `never[]`/`{}` first (which
// `Object.freeze` would then widen to a type with no overlap with `T[]`).
const NO_TURNS: ChatTurn[] = Object.freeze([] as ChatTurn[]) as ChatTurn[];
const NO_SESSIONS: ConversationSummary[] = Object.freeze([] as ConversationSummary[]) as ConversationSummary[];
const NO_PENDING_TRACE: ReasoningTraceStep[] = Object.freeze([] as ReasoningTraceStep[]) as ReasoningTraceStep[];
const EMPTY_FEEDBACK_SENT: Record<string, string> =
  Object.freeze({} as Record<string, string>) as Record<string, string>;

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

type AskModeCache = {
  ownerKey: string;
  modes: readonly AskModeDef[];
};

type AskModeRequest = {
  ownerKey: string;
  promise: Promise<unknown>;
};

/**
 * One in-flight durable Ask stream, tracked for the whole life of its transport.
 *
 * Navigation only *detaches* a run from the visible view (`detachVisibleRun`);
 * the stream keeps reading. Before the server's `started` event nothing about
 * the question is durable — in auto mode the engine is still being selected —
 * so a notebook restore cannot find it in history. This record is what lets
 * `restoreNotebook` re-attach that run to the returning view instead of
 * silently dropping the question. `owner` is rebound on re-attach; everything
 * else is written once (`conversationId`/`jobId` on `started`, `trace` as
 * progress arrives) so the returning view can repaint the pending turn.
 */
type AskRunRecord = {
  owner: AskSessionOwner;
  notebookId: string;
  question: string;
  askedAt: string;
  mode: string;
  trace: ReasoningTraceStep[];
  conversationIdAtStart: string | null;
  conversationId: string | null;
  jobId: string | null;
  controller: AbortController;
  cancelRequested: boolean;
};

/**
 * One reasoning-mode intent preview (and, once the model asks for it, the
 * clarification review) that precedes the durable Ask. Nothing about it exists
 * server-side, so navigation cannot rely on history to bring it back: the run
 * keeps reading its preview stream while detached, records the outcome here,
 * and a notebook restore re-attaches it — resuming the "理解中" turn, re-opening
 * the review, or (if it completed meanwhile) finding the durable run it started.
 */
type AskIntentRunRecord = {
  owner: AskSessionOwner;
  notebookId: string;
  question: string;
  askedAt: string;
  conversationIdAtStart: string | null;
  retrievalEffort: AskRetrievalEffortId;
  scopeSnapshot: { sourceScope: SourceScopePayload; baseScope: BaseScopePayload };
  controller: AbortController;
  draftToken: object;
  flowGeneration: number;
  trace: ReasoningTraceStep[];
  phase: "preview" | "review";
  contract: QueryIntentContract | null;
  understandingMs: number;
  cancelRequested: boolean;
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

async function requestAskCancellation(notebookId: string, jobId: string): Promise<void> {
  // The synchronous endpoint has no enforceable end-to-end database deadline:
  // it can cross more than one transaction and may wait on a process-local lock.
  // Keep this one request authoritative until the server answers; a client timer
  // would release retry authority while the original handler can still be alive.
  await cancelAskJob(notebookId, jobId);
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
  const [pendingMode, setPendingMode] = useState<string>(DEFAULT_ASK_MODE);
  const [pendingTrace, setPendingTrace] = useState<ReasoningTraceStep[]>([]);
  const [mode, setMode] = useState<string>(DEFAULT_ASK_MODE);
  const [askModes, setAskModes] = useState<readonly AskModeDef[]>(ASK_MODES);
  const [retrievalEffort, setRetrievalEffort] = useState<AskRetrievalEffortId>(
    DEFAULT_ASK_RETRIEVAL_EFFORT,
  );
  const [sessionPanelOpen, setSessionPanelOpen] = useState(false);
  const [renamingSessionId, setRenamingSessionId] = useState<string | null>(null);
  const [sessionTitleDraft, setSessionTitleDraft] = useState("");
  const [feedbackSent, setFeedbackSent] = useState<Record<string, string>>({});
  const [reconnectJob, setReconnectJob] = useState<{ jobId: string; seen: number } | null>(null);
  const [ownerSerial, setOwnerSerial] = useState(0);
  const [askModeProjectionSerial, setAskModeProjectionSerial] = useState(0);

  const policyRef = useRef(policy);
  policyRef.current = policy;
  const advancedRef = useRef(policy.advanced);
  const effectsRef = useRef(effects);
  effectsRef.current = effects;
  const actorIdRef = useRef(actorId);
  const notebookIdRef = useRef(notebookId);
  notebookIdRef.current = notebookId;
  const propActorIdRef = useRef(actorId);
  const pendingActorIdRef = useRef<string | null>(null);
  const actorGenerationRef = useRef(0);
  const ownerRef = useRef<AskSessionOwner | null>(null);
  const committedOwnerRef = useRef<AskSessionOwner | null>(null);
  const notebookGenerationRef = useRef(0);
  const viewGenerationRef = useRef(0);
  const conversationIdRef = useRef(conversationId);
  const turnsRef = useRef<ChatTurn[]>(turns);
  const modeRef = useRef(mode);
  const askModesRef = useRef<readonly AskModeDef[]>(askModes);
  const modeChoiceVersionRef = useRef(0);
  const pendingModeSourceRef = useRef<string | null>(null);
  conversationIdRef.current = conversationId;
  turnsRef.current = turns;
  modeRef.current = mode;
  askModesRef.current = askModes;

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
  const askModeCacheRef = useRef<AskModeCache | null>(null);
  const askModeRequestRef = useRef<AskModeRequest | null>(null);
  // Keyed by actor/notebook identity: at most one durable run can be in flight
  // per notebook view (`asking` blocks a second submit), and a detached run is
  // only ever re-attached to a view of the same identity.
  const inFlightRunsRef = useRef<Map<string, AskRunRecord>>(new Map());
  // Same keying for the client-driven intent preview/review that precedes a
  // reasoning Ask; `submit` refuses a second one while either phase is visible.
  const intentRunsRef = useRef<Map<string, AskIntentRunRecord>>(new Map());

  if (pendingActorIdRef.current === actorId) pendingActorIdRef.current = null;
  if (propActorIdRef.current !== actorId) {
    if (!(pendingActorIdRef.current && actorId === null)) {
      pendingActorIdRef.current = null;
      propActorIdRef.current = actorId;
      actorIdRef.current = actorId;
      if (ownerRef.current && ownerRef.current.actorId !== actorId) {
        ownerRef.current = null;
        committedOwnerRef.current = null;
        notebookGenerationRef.current += 1;
        viewGenerationRef.current += 1;
      }
      actorGenerationRef.current += 1;
      askModeCacheRef.current = null;
      askModeRequestRef.current = null;
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

  useEffect(() => {
    // Match the workspace-extension projection cadence: an Ask-mode request is
    // permitted only after the notebook transition has settled successfully.
    // Failed/refused/superseded transitions therefore perform no projection I/O
    // and the conversation restore is never blocked on this deployment surface.
    const owner = committedOwnerRef.current;
    if (
      !owner
      || !sameViewOwner(ownerRef.current, owner)
      || owner.actorId !== actorId
      || owner.notebookId !== notebookId
    ) return;

    const projectionOwnerKey = `${owner.actorId}\u0000${actorGenerationRef.current}`;
    const choiceVersion = modeChoiceVersionRef.current;
    let alive = true;

    const applyProjection = (modes: readonly AskModeDef[]) => {
      if (
        !alive
        || `${actorIdRef.current ?? ""}\u0000${actorGenerationRef.current}` !== projectionOwnerKey
        || !sameViewOwner(ownerRef.current, owner)
        || !sameViewOwner(committedOwnerRef.current, owner)
        || notebookIdRef.current !== owner.notebookId
      ) return;
      askModesRef.current = modes;
      setAskModes(modes);
      setMode((current) => {
        if (modeChoiceVersionRef.current !== choiceVersion) {
          return modeFromTurn({ response: { mode: current } }, modes);
        }
        // A re-attached intent preview/review is reasoning-mode context in its
        // own right; projecting the last turn's mode over it would cancel it.
        return modeFromTurn(
          visibleIntentRun()
            ? { response: { mode: "reasoning" } }
            : turnsRef.current[turnsRef.current.length - 1],
          modes,
        );
      });
      if (pendingModeSourceRef.current) {
        setPendingMode(modeFromTurn(
          { response: { mode: pendingModeSourceRef.current } },
          modes,
        ));
      }
    };

    const cached = askModeCacheRef.current;
    if (cached?.ownerKey === projectionOwnerKey) {
      applyProjection(cached.modes);
      return () => { alive = false; };
    }

    let request = askModeRequestRef.current;
    if (!request || request.ownerKey !== projectionOwnerKey) {
      request = { ownerKey: projectionOwnerKey, promise: fetchAskModes() };
      askModeRequestRef.current = request;
    }
    request.promise.then(
      (raw) => {
        const modes = normalizeAskModeProjection(raw);
        if (`${actorIdRef.current ?? ""}\u0000${actorGenerationRef.current}` === projectionOwnerKey) {
          askModeCacheRef.current = { ownerKey: projectionOwnerKey, modes };
        }
        applyProjection(modes);
      },
      () => {
        // Do not cache failures. This committed workspace degrades to built-ins;
        // the next successful workspace commit for the actor re-issues one call.
        if (askModeRequestRef.current === request) askModeRequestRef.current = null;
        applyProjection(ASK_MODES);
      },
    );
    return () => { alive = false; };
  }, [actorId, askModeProjectionSerial, notebookId]);

  function clearPendingTurn() {
    pendingModeSourceRef.current = null;
    setPendingQuestion("");
    setPendingAskedAt("");
    setPendingMode(DEFAULT_ASK_MODE);
    setPendingTrace([]);
  }

  function abortIntentPreview() {
    const wasIntentPhase = askIntentFlowRef.current === "preview"
      || askIntentFlowRef.current === "review";
    const run = visibleIntentRun();
    if (run) {
      run.cancelRequested = true;
      run.controller.abort();
      if (intentRunsRef.current.get(ownerKey(run.owner)) === run) {
        intentRunsRef.current.delete(ownerKey(run.owner));
      }
    }
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

  function visibleIntentRun(): AskIntentRunRecord | null {
    const owner = ownerRef.current;
    if (!owner) return null;
    const run = intentRunsRef.current.get(ownerKey(owner));
    return run && !run.cancelRequested && sameViewOwner(owner, run.owner) ? run : null;
  }

  function detachedIntentRunFor(owner: AskSessionOwner): AskIntentRunRecord | null {
    const run = intentRunsRef.current.get(ownerKey(owner));
    if (!run || run.cancelRequested || sameViewOwner(ownerRef.current, run.owner)) return null;
    return run;
  }

  function detachIntentPreview() {
    // Navigation keeps a pending preview/review alive for a later restore and
    // clears only its visible projection; every other phase keeps the abort path
    // (a confirmed hand-off is already owned by the durable run).
    const flow = askIntentFlowRef.current;
    if (flow !== "preview" && flow !== "review") {
      abortIntentPreview();
      return;
    }
    askIntentAbortRef.current = null;
    askIntentFlowGenerationRef.current += 1;
    askIntentFlowRef.current = "idle";
    askIntentTraceRef.current = [];
    askIntentDraftRef.current = "";
    askIntentDraftOwnerRef.current = null;
    setIntentChecking(false);
    setIntentReview(null);
    clearPendingTurn();
  }

  function presentIntentReview(run: AskIntentRunRecord, contract: QueryIntentContract) {
    askIntentTraceRef.current = run.trace;
    setPendingTrace(run.trace);
    askIntentFlowRef.current = "review";
    setIntentReview({
      flowGeneration: run.flowGeneration,
      notebookId: run.notebookId,
      conversationId: run.conversationIdAtStart,
      question: run.question,
      contract,
      understandingMs: run.understandingMs,
      askedAt: run.askedAt,
      sourceScope: run.scopeSnapshot.sourceScope,
      baseScope: run.scopeSnapshot.baseScope,
    });
    effectsRef.current.notify("问题存在会改变检索方向的歧义，请先补充确认");
  }

  function attachIntentRun(run: AskIntentRunRecord, owner: AskSessionOwner) {
    run.owner = owner;
    run.flowGeneration = ++askIntentFlowGenerationRef.current;
    askIntentFlowRef.current = run.phase;
    askIntentAbortRef.current = run.phase === "preview" ? run.controller : null;
    askIntentTraceRef.current = run.trace;
    askIntentDraftRef.current = run.question;
    askIntentDraftOwnerRef.current = run.draftToken;
    // The reasoning engine is part of this run's context: the mode projection
    // and the context-change effect both honour a visible intent run.
    modeRef.current = "reasoning";
    setMode("reasoning");
    setQuestion("");
    setConversationId(run.conversationIdAtStart);
    setPendingQuestion(run.question);
    setPendingAskedAt(run.askedAt);
    pendingModeSourceRef.current = "reasoning";
    setPendingMode("reasoning");
    setPendingTrace(run.trace);
    if (run.phase === "review" && run.contract) presentIntentReview(run, run.contract);
    else setIntentChecking(true);
    effectsRef.current.ensureAskVisible();
  }

  function detachedRunFor(owner: AskSessionOwner): AskRunRecord | null {
    const run = inFlightRunsRef.current.get(ownerKey(owner));
    if (!run || run.cancelRequested || sameViewOwner(ownerRef.current, run.owner)) return null;
    return run;
  }

  function attachDetachedRun(run: AskRunRecord, owner: AskSessionOwner) {
    // Rebind the still-reading stream to the returning view: from here on
    // `ownsRun()` inside executeAsk is true again, so `started` publishes the
    // durable id into this view and the final answer lands as a turn.
    run.owner = owner;
    askAbortRef.current = run.controller;
    askJobIdRef.current = run.jobId;
    askNotebookIdRef.current = run.notebookId;
    setReconnectJob(null);
    pendingModeSourceRef.current = run.mode;
    setPendingQuestion(run.question);
    setPendingAskedAt(run.askedAt);
    setPendingMode(run.mode);
    setPendingTrace([...run.trace]);
    setAsking(true);
    setConversationId(run.conversationId ?? run.conversationIdAtStart);
    effectsRef.current.ensureAskVisible();
  }

  function detachVisibleRun() {
    viewGenerationRef.current += 1;
    detachIntentPreview();
    askAbortRef.current = null;
    askJobIdRef.current = null;
    askNotebookIdRef.current = null;
    setAsking(false);
    setReconnectJob(null);
    clearPendingTurn();
  }

  function resetConversationView() {
    modeChoiceVersionRef.current += 1;
    turnsRef.current = [];
    modeRef.current = DEFAULT_ASK_MODE;
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
    actorGenerationRef.current += 1;
    ownerRef.current = null;
    committedOwnerRef.current = null;
    notebookGenerationRef.current += 1;
    viewGenerationRef.current += 1;
    askModeCacheRef.current = null;
    askModeRequestRef.current = null;
    askModesRef.current = ASK_MODES;
    setAskModes(ASK_MODES);
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
    committedOwnerRef.current = null;
    const owner: AskSessionOwner = {
      ...input,
      notebookGeneration: notebookGenerationRef.current,
      viewGeneration: viewGenerationRef.current,
    };
    ownerRef.current = owner;
    const projectionOwnerKey = `${input.actorId}\u0000${actorGenerationRef.current}`;
    const cachedModes = askModeCacheRef.current?.ownerKey === projectionOwnerKey
      ? askModeCacheRef.current.modes
      : ASK_MODES;
    askModesRef.current = cachedModes;
    setAskModes(cachedModes);
    setSessionLoading(true);
    setSessions([]);
    resetConversationView();
    setOwnerSerial((value) => value + 1);
    return owner;
  }

  function finishNotebookTransition(owner: AskNotebookTransition, succeeded = true) {
    if (!sameViewOwner(ownerRef.current, owner)) return;
    setSessionLoading(false);
    committedOwnerRef.current = succeeded ? owner : null;
    setAskModeProjectionSerial((value) => value + 1);
  }

  function leaveWorkspace() {
    detachVisibleRun();
    ownerRef.current = null;
    committedOwnerRef.current = null;
    notebookGenerationRef.current += 1;
    setSessionLoading(false);
    setSessions([]);
    resetConversationView();
    setOwnerSerial((value) => value + 1);
  }

  function abortForLogout() {
    askIntentAbortRef.current?.abort();
    askAbortRef.current?.abort();
    // Detached runs belong to the actor who is leaving; drop their transports
    // too (the durable jobs themselves keep running server-side).
    for (const run of inFlightRunsRef.current.values()) run.controller.abort();
    inFlightRunsRef.current.clear();
    for (const run of intentRunsRef.current.values()) {
      run.cancelRequested = true;
      run.controller.abort();
    }
    intentRunsRef.current.clear();
    leaveWorkspace();
    actorGenerationRef.current += 1;
    askModeCacheRef.current = null;
    askModeRequestRef.current = null;
    askModesRef.current = ASK_MODES;
    setAskModes(ASK_MODES);
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
    const nextTurns = detail.turns.map((turn) => ({
      question: turn.question,
      response: turn.response,
      askedAt: turn.asked_at,
    }));
    turnsRef.current = nextTurns;
    setTurns(nextTurns);
    const restoredMode = modeFromTurn(
      detail.turns[detail.turns.length - 1],
      askModesRef.current,
    );
    modeRef.current = restoredMode;
    setMode(restoredMode);
    setRetrievalEffort(retrievalEffortFromTurn(detail.turns[detail.turns.length - 1]));
    setConversationId(id);
    clearPendingTurn();
    effectsRef.current.ensureAskVisible();
    setSessionPanelOpen(false);
    setRenamingSessionId(null);
    askAbortRef.current = null;
    const active = detail.active_job;
    if (active) {
      pendingModeSourceRef.current = active.mode;
      setPendingQuestion(active.question);
      setPendingAskedAt(active.asked_at);
      setPendingMode(modeFromTurn(
        { response: { mode: active.mode } },
        askModesRef.current,
      ));
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
      // Work detached by navigation outranks "latest in history": an intent
      // preview leaves no server-side trace at all, and a durable run may not
      // have reached `started` yet, so opening the previous latest session over
      // either would hide the question.
      const intentRun = detachedIntentRunFor(owner);
      const run = detachedRunFor(owner);
      const latestId = intentRun
        ? intentRun.conversationIdAtStart
        : run ? run.conversationId ?? run.conversationIdAtStart : list?.[0]?.id;
      await restoreLatestConversation(
        latestId ? [{ id: latestId }] : [],
        (id) => applySessionDetail(id, owner),
      );
      if (!sameViewOwner(ownerRef.current, owner)) return false;
      // Re-read both: a detached preview may have completed and handed off to
      // a durable run while the detail was loading. A job the detail restore
      // projected as active (reconnect polling) is left alone unless it is this
      // very run, whose live transport then outranks polling.
      const intentNow = detachedIntentRunFor(owner);
      const runNow = detachedRunFor(owner);
      if (intentNow && askJobIdRef.current === null) {
        attachIntentRun(intentNow, owner);
      } else if (
        runNow
        && (askJobIdRef.current === null || askJobIdRef.current === runNow.jobId)
      ) {
        attachDetachedRun(runNow, owner);
      }
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

  function selectMode(next: string) {
    if (!currentNotebookOwner()) return;
    if (!askModesRef.current.some((candidate) => candidate.id === next)) return;
    modeChoiceVersionRef.current += 1;
    modeRef.current = next;
    setMode(next);
  }

  function selectRetrievalEffort(next: AskRetrievalEffortId) {
    if (!currentNotebookOwner()) return;
    setRetrievalEffort(next);
  }

  useEffect(() => {
    // Changing conversation or engine mid-preview abandons the preview — unless
    // the view is being set up to match a re-attached intent run.
    const run = visibleIntentRun();
    if (run && conversationId === run.conversationIdAtStart && mode === "reasoning") return;
    abortIntentPreview();
  }, [conversationId, mode]);

  useEffect(() => {
    const wasAdvanced = advancedRef.current;
    advancedRef.current = policy.advanced;
    if (!wasAdvanced || policy.advanced) return;
    const draft = askIntentDraftRef.current;
    abortIntentPreview();
    if (draft) setQuestion(draft);
  }, [policy.advanced]);

  async function executeAsk(
    nextQuestion: string,
    selectedMode: string,
    intent: AskIntentConfirmation | undefined,
    traceSeed: ReasoningTraceStep[] = [],
    askedAt = new Date().toISOString(),
    scopeSnapshot = {
      sourceScope: copySourceScope(policyRef.current.sourceScope),
      baseScope: copyBaseScope(policyRef.current.baseScope),
    },
    effort?: AskRetrievalEffortId,
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
    if (requiresKg(selectedMode, askModesRef.current) && !currentPolicy.kgAvailable) {
      effectsRef.current.notify(`${groupLabel(groupOf(selectedMode, askModesRef.current))}需要知识图谱 — 可在「设置 → 编辑当前笔记本」里挂一个参考库，或先整理该笔记本的知识图谱`);
      return false;
    }
    return startAskRun(
      owner,
      q,
      selectedMode,
      intent,
      traceSeed,
      askedAt,
      scopeSnapshot,
      effort ?? (currentPolicy.advanced ? retrievalEffort : DEFAULT_ASK_RETRIEVAL_EFFORT),
      conversationIdRef.current,
    );
  }

  /**
   * Start the durable Ask stream for `runOwner`. Visible-state writes happen
   * only while that owner is the current view (`ownsRun()`), so a detached
   * intent preview that completes off-screen can start its job here without
   * touching whatever the user is looking at; the run record then lets the
   * next notebook restore re-attach the still-reading stream.
   */
  async function startAskRun(
    runOwner: AskSessionOwner,
    q: string,
    selectedMode: string,
    intent: AskIntentConfirmation | undefined,
    traceSeed: ReasoningTraceStep[],
    askedAt: string,
    scopeSnapshot: { sourceScope: SourceScopePayload; baseScope: BaseScopePayload },
    effort: AskRetrievalEffortId,
    conversationIdAtStart: string | null,
  ): Promise<boolean> {
    let startedConversationId = conversationIdAtStart;
    const controller = new AbortController();
    const run: AskRunRecord = {
      owner: runOwner,
      notebookId: runOwner.notebookId,
      question: q,
      askedAt,
      mode: selectedMode,
      trace: [...traceSeed],
      conversationIdAtStart,
      conversationId: null,
      jobId: null,
      controller,
      cancelRequested: false,
    };
    const runKey = ownerKey(runOwner);
    inFlightRunsRef.current.set(runKey, run);
    // `run.owner` is rebound when a notebook restore re-attaches this run.
    const ownsRun = () => sameViewOwner(ownerRef.current, run.owner);
    if (ownsRun()) {
      effectsRef.current.ensureAskVisible();
      setQuestion("");
      setPendingQuestion(q);
      setPendingAskedAt(askedAt);
      pendingModeSourceRef.current = selectedMode;
      setPendingMode(selectedMode);
      setPendingTrace(traceSeed);
      setAsking(true);
      askJobIdRef.current = null;
      askAbortRef.current = controller;
      askNotebookIdRef.current = runOwner.notebookId;
    }
    try {
      const payload = {
        question: q,
        asked_at: askedAt,
        conversation_id: conversationIdAtStart ?? undefined,
        mode: selectedMode,
        retrieval_effort: effort,
        source_scope: scopeSnapshot.sourceScope,
        base_scope: scopeSnapshot.baseScope,
        ...(intent ? { intent } : {}),
      };
      const response = await runAskStream<AskResponse>(
        runOwner.notebookId,
        payload,
        (step) => {
          run.trace.push(step);
          if (ownsRun()) setPendingTrace((previous) => [...previous, step]);
        },
        controller.signal,
        async (jobId, durableConversationId) => {
          startedConversationId = durableConversationId;
          run.jobId = jobId;
          run.conversationId = durableConversationId;
          if (cancelRequestedControllersRef.current.delete(controller)) {
            const cancelKey = `${runOwner.notebookId}\u0000${jobId}`;
            // A detached pre-start Stop and a restored active-job Stop share the
            // same authority key. Whichever observes `started` first owns the one
            // cancellation request; the other keeps consuming the durable stream.
            if (cancelRequestsInFlightRef.current.has(cancelKey)) return;
            cancelRequestsInFlightRef.current.add(cancelKey);
            try {
              await requestAskCancellation(runOwner.notebookId, jobId);
            } catch {
              if (sameNotebookIdentity(ownerRef.current, runOwner)) {
                effectsRef.current.notify("未能中断后台任务；任务将继续完成，可稍后重开查看");
              }
              return;
            } finally {
              cancelRequestsInFlightRef.current.delete(cancelKey);
            }
            // A history restore may have projected this same durable job while
            // the pre-start cancellation was waiting. Retire only the exact
            // matching refs; a replacement job remains authoritative.
            if (
              askJobIdRef.current === jobId
              && askNotebookIdRef.current === runOwner.notebookId
            ) {
              askJobIdRef.current = null;
              askNotebookIdRef.current = null;
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
      setTurns((previous) => {
        const next = [...previous, { question: q, response, askedAt }];
        turnsRef.current = next;
        return next;
      });
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
      if (inFlightRunsRef.current.get(runKey) === run) inFlightRunsRef.current.delete(runKey);
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
    if (ownsRun()) await loadSessionsFor(run.owner).catch(() => {});
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
    const submitMode = currentPolicy.advanced ? mode : AUTO_ASK_MODE;
    if (requiresKg(submitMode, askModesRef.current) && !currentPolicy.kgAvailable) {
      effectsRef.current.notify(`${groupLabel(groupOf(submitMode, askModesRef.current))}需要知识图谱 — 可在「设置 → 编辑当前笔记本」里挂一个参考库，或先整理该笔记本的知识图谱`);
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
    const run: AskIntentRunRecord = {
      owner,
      notebookId: owner.notebookId,
      question: q,
      askedAt,
      conversationIdAtStart: conversationIdRef.current,
      retrievalEffort: currentPolicy.advanced ? retrievalEffort : DEFAULT_ASK_RETRIEVAL_EFFORT,
      scopeSnapshot,
      controller: new AbortController(),
      draftToken: {},
      flowGeneration: ++askIntentFlowGenerationRef.current,
      trace: [intentUnderstandingStep()],
      phase: "preview",
      contract: null,
      understandingMs: 0,
      cancelRequested: false,
    };
    intentRunsRef.current.set(ownerKey(owner), run);
    askIntentFlowRef.current = "preview";
    askIntentAbortRef.current = run.controller;
    askIntentDraftRef.current = q;
    askIntentDraftOwnerRef.current = run.draftToken;
    askIntentTraceRef.current = run.trace;
    setQuestion("");
    setPendingQuestion(q);
    setPendingAskedAt(askedAt);
    pendingModeSourceRef.current = "reasoning";
    setPendingMode("reasoning");
    setPendingTrace(run.trace);
    setIntentChecking(true);
    await runIntentPreview(run);
  }

  /**
   * Drive one intent preview to its outcome. Navigation may detach the run from
   * the visible view while this awaits, and a notebook restore may re-attach it,
   * so every visible write is gated on `attached()` and the record carries the
   * trace/contract the returning view repaints. A clear intent starts the
   * durable Ask either way: on-screen through `executeAsk`, off-screen through
   * `startAskRun`, so the question is never dropped for having been left.
   */
  async function runIntentPreview(run: AskIntentRunRecord) {
    const runKey = ownerKey(run.owner);
    const attached = () => !run.cancelRequested && sameViewOwner(ownerRef.current, run.owner);
    const understandingStartedAt = Date.now();
    try {
      const contract = await previewAskIntent(
        run.notebookId,
        run.question,
        run.conversationIdAtStart,
        run.controller.signal,
        run.scopeSnapshot.sourceScope,
        run.scopeSnapshot.baseScope,
        (elapsed) => {
          if (run.cancelRequested) return;
          run.trace = replaceLastIntentStep(run.trace, intentUnderstandingStep(elapsed));
          if (attached()) {
            askIntentTraceRef.current = run.trace;
            setPendingTrace(run.trace);
          }
        },
      );
      if (run.cancelRequested) return;
      const understandingMs = elapsedMs(understandingStartedAt, Date.now());
      run.understandingMs = understandingMs;
      if (contract.needs_clarification) {
        run.trace = replaceLastIntentStep(run.trace, intentClarifyStep(contract, understandingMs));
        run.phase = "review";
        run.contract = contract;
        if (attached()) presentIntentReview(run, contract);
        return;
      }
      run.trace = replaceLastIntentStep(run.trace, intentUnderstoodStep(contract, understandingMs));
      if (intentRunsRef.current.get(runKey) === run) intentRunsRef.current.delete(runKey);
      const confirmation = buildAskIntentConfirmation(
        contract,
        contract.resolved_question,
        {},
        understandingMs,
      );
      if (!attached()) {
        await startAskRun(
          run.owner,
          run.question,
          "reasoning",
          confirmation,
          handOffIntentTrace(run.trace),
          run.askedAt,
          run.scopeSnapshot,
          run.retrievalEffort,
          run.conversationIdAtStart,
        );
        return;
      }
      askIntentTraceRef.current = run.trace;
      askIntentAbortRef.current = null;
      setIntentChecking(false);
      askIntentFlowRef.current = "submitting";
      const started = await executeAsk(
        run.question,
        "reasoning",
        confirmation,
        handOffIntentTrace(run.trace),
        run.askedAt,
        run.scopeSnapshot,
        run.retrievalEffort,
      );
      if (releaseIntentDraft(run.draftToken) && !started) {
        setQuestion(run.question);
        askIntentTraceRef.current = [];
        clearPendingTurn();
      }
    } catch (error) {
      if (intentRunsRef.current.get(runKey) === run) intentRunsRef.current.delete(runKey);
      if (!isAbortError(error) && attached()) effectsRef.current.reportError(error);
      const draft = askIntentDraftRef.current;
      if (askIntentAbortRef.current === run.controller && releaseIntentDraft(run.draftToken)) {
        setQuestion(draft || run.question);
        askIntentTraceRef.current = [];
        clearPendingTurn();
      }
    } finally {
      if (askIntentAbortRef.current === run.controller) {
        askIntentAbortRef.current = null;
        setIntentChecking(false);
      }
      if (
        askIntentFlowGenerationRef.current === run.flowGeneration
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
    const run = visibleIntentRun();
    const retireRun = () => {
      if (run && intentRunsRef.current.get(ownerKey(run.owner)) === run) {
        intentRunsRef.current.delete(ownerKey(run.owner));
      }
    };
    if (
      review.notebookId !== owner.notebookId
      || review.conversationId !== conversationIdRef.current
      || modeRef.current !== "reasoning"
    ) {
      if (run) run.cancelRequested = true;
      retireRun();
      setIntentReview(null);
      askIntentTraceRef.current = [];
      releaseIntentDraft(draftToken);
      clearPendingTurn();
      effectsRef.current.notify("问题上下文已经变化，请重新提交");
      return;
    }
    // Confirmation hands the question to the durable run; the preview record
    // has nothing left to re-attach.
    retireRun();
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
        run?.retrievalEffort,
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
    const run = visibleIntentRun();
    if (run) {
      run.cancelRequested = true;
      run.controller.abort();
      if (intentRunsRef.current.get(ownerKey(run.owner)) === run) {
        intentRunsRef.current.delete(ownerKey(run.owner));
      }
    }
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
    // A run the user asked to stop must never be re-attached by a later restore,
    // whichever way the cancellation below settles.
    for (const run of inFlightRunsRef.current.values()) {
      if (run.controller === controller) run.cancelRequested = true;
    }
    if (jobId && activeNotebook) {
      const owner = currentNotebookOwner();
      const cancelKey = `${activeNotebook}\u0000${jobId}`;
      if (cancelRequestsInFlightRef.current.has(cancelKey)) return;
      cancelRequestsInFlightRef.current.add(cancelKey);
      requestAskCancellation(activeNotebook, jobId)
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
    turns: ownerIsVisible ? turns : NO_TURNS,
    conversationId: ownerIsVisible ? conversationId : null,
    sessions: ownerIsVisible ? sessions : NO_SESSIONS,
    asking: ownerIsVisible ? asking : false,
    intentChecking: ownerIsVisible ? intentChecking : false,
    intentReview: ownerIsVisible ? intentReview : null,
    sessionLoading: ownerBelongsToActor ? sessionLoading : false,
    pendingQuestion: ownerIsVisible ? pendingQuestion : "",
    pendingAskedAt: ownerIsVisible ? pendingAskedAt : "",
    pendingMode: ownerIsVisible ? pendingMode : DEFAULT_ASK_MODE,
    pendingTrace: ownerIsVisible ? pendingTrace : NO_PENDING_TRACE,
    askModes: ownerIsVisible ? askModes : ASK_MODES,
    mode: ownerIsVisible ? mode : DEFAULT_ASK_MODE,
    retrievalEffort: ownerIsVisible ? retrievalEffort : DEFAULT_ASK_RETRIEVAL_EFFORT,
    sessionPanelOpen: ownerIsVisible ? sessionPanelOpen : false,
    renamingSessionId: ownerIsVisible ? renamingSessionId : null,
    sessionTitleDraft: ownerIsVisible ? sessionTitleDraft : "",
    sessionTitleOverLimit: conversationTitleLimitHint(sessionTitleDraft.trim()),
    feedbackSent: ownerIsVisible ? feedbackSent : EMPTY_FEEDBACK_SENT,
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
