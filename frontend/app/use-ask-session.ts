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
import {
  claimIntentRun,
  clearPersistedIntentRuns,
  findPersistedIntentRuns,
  newIntentRunId,
  releaseIntentRun,
  removePersistedIntentRun,
  savePersistedIntentRun,
  type PersistedIntentRun,
} from "./ask-intent-persist.ts";
import { jobPollDone, newTraceSteps } from "./ask-reconnect.ts";
import { humanizedError, toUserMessage } from "./errors.ts";
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
type AskRunRecord = DetachableRecord & {
  notebookId: string;
  question: string;
  askedAt: string;
  mode: string;
  retrievalEffort: AskRetrievalEffortId;
  trace: ReasoningTraceStep[];
  conversationIdAtStart: string | null;
  conversationId: string | null;
  jobId: string | null;
  controller: AbortController;
  // The final answer when the run settled while nobody was looking: a restore
  // that raced with it projects this turn locally instead of re-reading history.
  result: AskResponse | null;
  // A failure that happened while nobody was looking. The record is kept so
  // the returning view can report it and hand the question back as a draft.
  failure: unknown | null;
  // The preview mirror this run took over (ask-intent-persist.ts), retired on
  // `started`; `keepMirror` marks an in-app unmount that aborted the stream
  // before `started` and left the mirror for the next hook instance.
  mirrorId: string | null;
  keepMirror: boolean;
};

/**
 * Shared shape of every record navigation can leave behind. `key` is the
 * actor/notebook identity a restore matches on; `serial` orders records of the
 * same identity (a user may start another session and ask again while an
 * earlier run is still detached — both must survive, and the newest one is the
 * one a restore brings back first). `owner` is the view the record is bound to;
 * a record is *detached* while no current view owns it.
 */
type DetachableRecord = {
  key: string;
  serial: number;
  owner: AskSessionOwner;
  cancelRequested: boolean;
};

function latestDetachedRecord<T extends DetachableRecord>(
  records: readonly T[],
  owner: AskSessionOwner,
  current: AskSessionOwner | null,
  accept: (record: T) => boolean = () => true,
): T | null {
  const key = ownerKey(owner);
  let latest: T | null = null;
  for (const record of records) {
    if (record.key !== key || record.cancelRequested || sameViewOwner(current, record.owner)) continue;
    if (!accept(record)) continue;
    if (!latest || record.serial > latest.serial) latest = record;
  }
  return latest;
}

function dropRecord<T>(records: T[], record: T): void {
  const index = records.indexOf(record);
  if (index >= 0) records.splice(index, 1);
}

/**
 * One reasoning-mode intent preview (and, once the model asks for it, the
 * clarification review) that precedes the durable Ask. Nothing about it exists
 * server-side, so navigation cannot rely on history to bring it back: the run
 * keeps reading its preview stream while detached, records the outcome here,
 * and a notebook restore re-attaches it — resuming the "理解中" turn, re-opening
 * the review, or (if it completed meanwhile) finding the durable run it started.
 */
type AskIntentRunRecord = DetachableRecord & {
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
  // "failed" = the preview request failed while detached; the record waits for
  // the next restore to report it and return the question as a draft.
  phase: "preview" | "review" | "failed";
  contract: QueryIntentContract | null;
  understandingMs: number;
  failure: unknown | null;
  // Identity of this submission in the per-tab persistence mirror and the
  // cross-tab ownership lock (ask-intent-persist.ts).
  persistId: string;
  // Set when this hook instance retires the run without the user having
  // decided anything (in-app unmount): the continuation is aborted, but the
  // storage mirror is kept for the next instance in this tab to resume.
  keepMirror: boolean;
  // True once this tab owns the record's cross-tab lock; the storage mirror is
  // written only then, so a copied tab can never race the originating one.
  mirrored: boolean;
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
  // Every durable run and every intent preview/review that is in flight or
  // failed while detached. Several records may share one actor/notebook key
  // (another session can be opened and asked in while an earlier run is still
  // detached); a restore re-attaches the newest detached one of that identity.
  const inFlightRunsRef = useRef<AskRunRecord[]>([]);
  const intentRunsRef = useRef<AskIntentRunRecord[]>([]);
  // Runs that settled successfully while detached, kept so a restore that raced
  // with them can project the answer by serial — including the durable successor
  // of an intent preview that finished during the restore. Entries of an identity
  // are released once that identity's next restore/session open has applied
  // (history is authoritative from then on) and on logout; no numeric cap, so a
  // result an in-progress restore selected can never be evicted underneath it.
  const settledRunsRef = useRef<AskRunRecord[]>([]);
  // Submission order across durable runs, intent previews AND records restored
  // from the per-tab mirror: serials are monotonic wall-clock values, the same
  // scheme as the mirror's `savedAt`, so a run materialized after a reload keeps
  // its original place instead of the order it happened to be restored in.
  const runSerialRef = useRef(0);
  function nextRunSerial(atLeast = 0): number {
    runSerialRef.current = Math.max(Date.now(), runSerialRef.current + 1, atLeast);
    return runSerialRef.current;
  }

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
        // own right (projecting the last turn's mode over it would cancel it),
        // a re-attached durable run keeps the engine it was submitted with, and
        // a question handed back as a draft keeps the engine it was asked with.
        const pendingMode = visibleIntentRun() ? "reasoning" : visibleRun()?.mode ?? draftModeRef.current;
        return modeFromTurn(
          pendingMode
            ? { response: { mode: pendingMode } }
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
      dropRecord(intentRunsRef.current, run);
      forgetPersistedIntent(run);
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
    return intentRunsRef.current.find((run) => (
      !run.cancelRequested && run.phase !== "failed" && sameViewOwner(owner, run.owner)
    )) ?? null;
  }

  // The preview/review phase has no server-side trace, so the in-memory record
  // is mirrored to per-tab session storage: a reload of this tab resumes it
  // (see ask-intent-persist.ts). Mirrored on submit and on "needs
  // clarification"; forgotten on hand-off, cancel, mode switch and tombstone.
  function persistIntentRun(run: AskIntentRunRecord) {
    if (run.phase === "failed" || !run.mirrored) return;
    savePersistedIntentRun({
      version: 1,
      id: run.persistId,
      savedAt: Date.now(),
      actorId: run.owner.actorId,
      notebookId: run.notebookId,
      conversationIdAtStart: run.conversationIdAtStart,
      question: run.question,
      askedAt: run.askedAt,
      retrievalEffort: run.retrievalEffort,
      sourceScope: run.scopeSnapshot.sourceScope,
      baseScope: run.scopeSnapshot.baseScope,
      phase: run.phase,
      contract: run.contract,
      understandingMs: run.understandingMs,
      confirmation: null,
    });
  }

  // Hand-off: the intent is settled and the durable POST is about to go out,
  // but until the server acknowledges `started` this record is still the only
  // copy of the question. Keep it (with the confirmed intent, so a reload can
  // re-submit without another understanding pass) until `started`.
  function persistHandoff(run: AskIntentRunRecord, confirmation: AskIntentConfirmation) {
    if (!run.mirrored) return;
    savePersistedIntentRun({
      version: 1,
      id: run.persistId,
      savedAt: Date.now(),
      actorId: run.owner.actorId,
      notebookId: run.notebookId,
      conversationIdAtStart: run.conversationIdAtStart,
      question: run.question,
      askedAt: run.askedAt,
      retrievalEffort: run.retrievalEffort,
      sourceScope: run.scopeSnapshot.sourceScope,
      baseScope: run.scopeSnapshot.baseScope,
      phase: "handoff",
      contract: run.contract,
      understandingMs: run.understandingMs,
      confirmation,
    });
  }

  // The active job the last applied detail reported (if any): a handed-off
  // question that reloads before `started` is reconciled against it and the
  // loaded turns, so a job the server did create is never submitted twice.
  const lastAppliedActiveJobRef = useRef<{ asked_at: string; question: string } | null>(null);
  // The engine a handed-back draft was asked with, honoured by every mode
  // projection (which would otherwise reset it to the last turn's) until the
  // next notebook transition, session switch, explicit engine choice or submit.
  const draftModeRef = useRef<string | null>(null);

  function durableAlreadyHolds(record: PersistedIntentRun): boolean {
    const active = lastAppliedActiveJobRef.current;
    if (active && active.asked_at === record.askedAt && active.question === record.question) return true;
    return turnsRef.current.some((turn) => (
      turn.askedAt === record.askedAt && turn.question === record.question
    ));
  }

  /**
   * A reload landed between hand-off and `started`. If history (the detail
   * just loaded) already shows the job or its answer, the server owns the
   * question and the mirror is simply retired. Otherwise the outcome of the
   * original POST is unknowable from here — it may still commit — and the
   * backend has no idempotency key that would make a client retry safe, so the
   * question is NOT re-submitted automatically: it goes back to the input as a
   * draft, in reasoning mode, with a notice, and the user decides.
   */
  function resumeHandoff(record: PersistedIntentRun, owner: AskSessionOwner) {
    removePersistedIntentRun(record.id);
    releaseIntentRun(record.id);
    if (durableAlreadyHolds(record) || !sameViewOwner(ownerRef.current, owner)) return;
    if (record.conversationIdAtStart === null) {
      // A first question: the view currently shows whatever the restore opened
      // to check history; the draft belongs to a fresh conversation.
      turnsRef.current = [];
      setTurns([]);
      setConversationId(null);
    }
    modeRef.current = "reasoning";
    setMode("reasoning");
    draftModeRef.current = "reasoning";
    setRetrievalEffort(retrievalEffortFromTurn({
      response: { retrieval_effort: record.retrievalEffort },
    }));
    setQuestion(record.question);
    effectsRef.current.notify("上次提交尚未收到服务端确认，问题已退回输入框，请确认后重新发送");
  }

  function forgetPersistedIntent(run: Pick<AskIntentRunRecord, "persistId">) {
    removePersistedIntentRun(run.persistId);
    releaseIntentRun(run.persistId);
  }

  /**
   * Rebuild the intent run a reload interrupted, if this tab has one for the
   * notebook (optionally: for one conversation) and its conversation still
   * exists. Newest first; a record whose conversation is gone is discarded, and
   * one another tab already owns (duplicated tab — session storage is copied)
   * is dropped from this tab instead of being resumed twice. The record is
   * pushed as the view's own (not detached): the caller attaches it and, for the
   * preview phase, re-issues the understanding request the reload aborted.
   */
  // A claimed record, not yet an in-memory run: the caller materializes it only
  // once the conversation detail it belongs to has actually loaded.
  type ResumedIntent =
    | { kind: "intent"; record: PersistedIntentRun }
    | { kind: "handoff"; record: PersistedIntentRun };

  async function resumePersistedIntent(
    owner: AskSessionOwner,
    list: readonly { id: string }[] | null,
    conversationId?: string,
  ): Promise<ResumedIntent | null> {
    if (!policyRef.current.advanced) {
      // The UI mode is per actor and may have been switched to automatic in
      // another tab since these were stored: automatic mode must never resume
      // a reasoning preview or re-open a review. Same outcome as the switch
      // effect, one step later.
      clearPersistedIntentRuns(owner.actorId);
      return null;
    }
    let persisted = null;
    for (const candidate of findPersistedIntentRuns(owner.actorId, owner.notebookId)) {
      if (conversationId !== undefined && candidate.conversationIdAtStart !== conversationId) continue;
      if (
        candidate.conversationIdAtStart !== null
        && !(list ?? []).some((session) => session.id === candidate.conversationIdAtStart)
      ) {
        removePersistedIntentRun(candidate.id);
        continue;
      }
      if (!(await claimIntentRun(candidate.id))) {
        removePersistedIntentRun(candidate.id);
        continue;
      }
      persisted = candidate;
      break;
    }
    if (!persisted || !sameViewOwner(ownerRef.current, owner)) return null;
    if (persisted.phase === "handoff") return { kind: "handoff", record: persisted };
    return { kind: "intent", record: persisted };
  }

  /**
   * Turn a claimed preview/review record into this view's in-memory run. Only
   * called after the conversation detail it belongs to has loaded: a run that
   * existed before a failed detail read would be detached by the retry's
   * transition and its mirror deleted with it.
   */
  function materializeIntentRun(persisted: PersistedIntentRun, owner: AskSessionOwner): AskIntentRunRecord {
    const contract = persisted.phase === "review" ? persisted.contract : null;
    // Its submission time is its place in line; later submissions must still
    // sort after it, so the serial base moves past it.
    nextRunSerial(persisted.savedAt);
    const run: AskIntentRunRecord = {
      key: ownerKey(owner),
      serial: persisted.savedAt,
      owner,
      cancelRequested: false,
      notebookId: owner.notebookId,
      question: persisted.question,
      askedAt: persisted.askedAt,
      conversationIdAtStart: persisted.conversationIdAtStart,
      retrievalEffort: retrievalEffortFromTurn({
        response: { retrieval_effort: persisted.retrievalEffort },
      }),
      scopeSnapshot: {
        sourceScope: copySourceScope(persisted.sourceScope),
        baseScope: copyBaseScope(persisted.baseScope),
      },
      controller: new AbortController(),
      draftToken: {},
      flowGeneration: askIntentFlowGenerationRef.current,
      trace: contract
        ? [intentClarifyStep(contract, persisted.understandingMs)]
        : [intentUnderstandingStep()],
      phase: contract ? "review" : "preview",
      contract,
      understandingMs: persisted.understandingMs,
      failure: null,
      persistId: persisted.id,
      keepMirror: false,
      mirrored: true,
    };
    intentRunsRef.current.push(run);
    return run;
  }

  /**
   * Bring a claimed preview/review back into `owner`'s view once its detail
   * step is over. `detailLoaded` is null when there was no detail to load (a
   * first question), true when it loaded, false when it failed or was refused:
   * then the record is left in storage (lock released) for the next attempt.
   */
  function attachResumedIntent(
    persisted: PersistedIntentRun,
    owner: AskSessionOwner,
    detailLoaded: boolean | null,
  ) {
    if (detailLoaded === false) {
      releaseIntentRun(persisted.id);
      return;
    }
    if (askJobIdRef.current !== null) {
      // A job is already active in that conversation: the stored preview is
      // stale relative to what the server knows. Drop it rather than stack.
      removePersistedIntentRun(persisted.id);
      releaseIntentRun(persisted.id);
      return;
    }
    const run = materializeIntentRun(persisted, owner);
    attachIntentRun(run, owner);
    // The reload aborted the understanding request; issue it again.
    if (run.phase === "preview") void runIntentPreview(run);
  }

  function detachedIntentRunFor(
    owner: AskSessionOwner,
    conversationId?: string,
  ): AskIntentRunRecord | null {
    return latestDetachedRecord(
      intentRunsRef.current,
      owner,
      ownerRef.current,
      (run) => conversationId === undefined || run.conversationIdAtStart === conversationId,
    );
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
    if (run.phase === "failed") {
      // The preview failed while the user was away: say so now and give the
      // question back instead of pretending nothing was asked. Back in the input
      // it is the user's draft again, so this tab has nothing left to resume.
      dropRecord(intentRunsRef.current, run);
      forgetPersistedIntent(run);
      setQuestion(run.question);
      effectsRef.current.reportError(run.failure);
      return;
    }
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
    setRetrievalEffort(run.retrievalEffort);
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

  function detachedRunFor(owner: AskSessionOwner, conversationId?: string): AskRunRecord | null {
    return latestDetachedRecord(
      inFlightRunsRef.current,
      owner,
      ownerRef.current,
      (run) => conversationId === undefined || runConversation(run) === conversationId,
    );
  }

  function visibleRun(): AskRunRecord | null {
    const owner = ownerRef.current;
    if (!owner) return null;
    return inFlightRunsRef.current.find((run) => (
      !run.cancelRequested && !run.failure && sameViewOwner(owner, run.owner)
    )) ?? null;
  }

  /**
   * Whether a detached durable run may be re-attached to a view whose detail
   * restore has just settled. Before `started` there is nothing server-side to
   * compare against, so an idle view takes it. After `started` the restored
   * detail is authoritative: it must advertise this very job as active —
   * a terminal detail already shows the answer, and re-attaching the transport
   * would append the same turn a second time when its final event lands.
   */
  function canAttachRun(run: AskRunRecord, startedBeforeDetail: boolean): boolean {
    if (run.failure) return true;
    // A job that only started while the detail was loading cannot be known to
    // that snapshot; an idle view takes the live stream. Only a job that was
    // already running when the detail was requested must be advertised by it.
    if (!startedBeforeDetail) {
      return askJobIdRef.current === null || askJobIdRef.current === run.jobId;
    }
    return askJobIdRef.current === run.jobId;
  }

  function projectSettledRun(run: AskRunRecord, response: AskResponse) {
    const turn: ChatTurn = { question: run.question, response, askedAt: run.askedAt };
    // A follow-up lands behind the turns the restore just loaded for its
    // conversation (turnsRef is written synchronously by applySessionDetail);
    // a first question is the whole conversation.
    const sameConversation = run.conversationIdAtStart !== null
      && run.conversationIdAtStart === response.conversation_id;
    const alreadyShown = sameConversation && turnsRef.current.some((item) => (
      item.response.answer_id === response.answer_id
    ));
    const next = !sameConversation
      ? [turn]
      : alreadyShown ? turnsRef.current : [...turnsRef.current, turn];
    turnsRef.current = next;
    setTurns(next);
    setConversationId(response.conversation_id);
    clearPendingTurn();
    setAsking(false);
    effectsRef.current.ensureAskVisible();
  }

  function attachDetachedRun(run: AskRunRecord, owner: AskSessionOwner) {
    if (run.failure) {
      dropRecord(inFlightRunsRef.current, run);
      setQuestion(run.question);
      effectsRef.current.reportError(run.failure);
      return;
    }
    // Rebind the still-reading stream to the returning view: from here on
    // `ownsRun()` inside executeAsk is true again, so `started` publishes the
    // durable id into this view and the final answer lands as a turn.
    run.owner = owner;
    askAbortRef.current = run.controller;
    askJobIdRef.current = run.jobId;
    askNotebookIdRef.current = run.notebookId;
    setReconnectJob(null);
    // The notebook transition reset the engine/effort controls; the run carries
    // the selection it was submitted with, so follow-ups keep using it.
    modeChoiceVersionRef.current += 1;
    const restoredMode = modeFromTurn({ response: { mode: run.mode } }, askModesRef.current);
    modeRef.current = restoredMode;
    setMode(restoredMode);
    setRetrievalEffort(run.retrievalEffort);
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
    draftModeRef.current = null;
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
    for (const run of inFlightRunsRef.current) run.controller.abort();
    inFlightRunsRef.current = [];
    for (const run of intentRunsRef.current) {
      run.cancelRequested = true;
      run.controller.abort();
    }
    intentRunsRef.current = [];
    settledRunsRef.current = [];
    if (actorIdRef.current) clearPersistedIntentRuns(actorIdRef.current);
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
    lastAppliedActiveJobRef.current = active
      ? { asked_at: active.asked_at, question: active.question }
      : null;
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

  type DetachedSelection = {
    intentRun: AskIntentRunRecord | null;
    run: AskRunRecord | null;
    selected: DetachableRecord | null;
    runStartedBeforeDetail: boolean;
  };

  // Across both kinds the most recently submitted question comes back first.
  function newerIntent(
    intent: AskIntentRunRecord | null,
    durable: AskRunRecord | null,
  ): intent is AskIntentRunRecord {
    return Boolean(intent && (!durable || intent.serial > durable.serial));
  }

  function runConversation(run: AskRunRecord): string | null {
    return run.conversationId ?? run.conversationIdAtStart;
  }

  /**
   * Drop detached records whose conversation this actor has since deleted in
   * this notebook: a tombstoned conversation must never be resurrected as the
   * target of a re-attached run or review.
   */
  function pruneTombstonedRecords(owner: AskSessionOwner) {
    const deleted = deletedConversationIdsRef.current.get(ownerKey(owner));
    if (!deleted?.size) return;
    const isDetached = (record: DetachableRecord) => (
      record.key === ownerKey(owner) && !sameViewOwner(ownerRef.current, record.owner)
    );
    for (const run of [...inFlightRunsRef.current]) {
      const target = runConversation(run);
      if (isDetached(run) && target !== null && deleted.has(target)) {
        run.cancelRequested = true;
        run.controller.abort();
        dropRecord(inFlightRunsRef.current, run);
      }
    }
    for (const run of [...intentRunsRef.current]) {
      const target = run.conversationIdAtStart;
      if (isDetached(run) && target !== null && deleted.has(target)) {
        run.cancelRequested = true;
        run.controller.abort();
        dropRecord(intentRunsRef.current, run);
        forgetPersistedIntent(run);
      }
    }
  }

  /**
   * Pick the detached work a view restore should bring back — the newest record
   * of this identity, optionally only records bound to one conversation (a
   * same-notebook session open). Captured BEFORE the detail request so the
   * post-request step can tell "settled meanwhile" from "still detached".
   */
  function selectDetachedWork(owner: AskSessionOwner, conversationId?: string): DetachedSelection {
    pruneTombstonedRecords(owner);
    const intentRun = detachedIntentRunFor(owner, conversationId);
    const run = detachedRunFor(owner, conversationId);
    return {
      intentRun,
      run,
      selected: newerIntent(intentRun, run) ? intentRun : run,
      runStartedBeforeDetail: run !== null && run.jobId !== null,
    };
  }

  /**
   * After the detail request settled, re-attach (or locally project) the work
   * chosen by `selectDetachedWork`. Re-reads both kinds: a detached preview may
   * have completed and handed off to a durable run while the detail was
   * loading, or the tracked run may have finished outright. A job the detail
   * restore projected as active (reconnect polling) is left alone unless it is
   * this very run, whose live transport then outranks polling.
   */
  function applyDetachedWork(
    owner: AskSessionOwner,
    selection: DetachedSelection,
    detailLoaded: boolean,
    conversationId?: string,
  ) {
    const { run, selected, runStartedBeforeDetail } = selection;
    const intentNow = detachedIntentRunFor(owner, conversationId);
    const runNow = detachedRunFor(owner, conversationId);
    // The record this restore was built around must still be the one that
    // comes back. A preview that completed meanwhile lives on as the durable
    // run carrying its serial; anything else that settled is durable history
    // now, and an older detached record must not be attached over it.
    const successor = selected && runNow && runNow !== selected && runNow.serial === selected.serial
      ? runNow
      : null;
    const selectedStillDetached = selected !== null
      && (intentNow === selected || runNow === selected);
    if (selected && !selectedStillDetached && !successor) {
      // It settled while the detail was loading — either the run itself or the
      // durable successor of a preview, both carrying the selected serial. Its
      // own final response is the authoritative turn: project it locally (no
      // extra list/detail read — restore stays within one list read and at most
      // one detail read).
      const settled = settledRunsRef.current.find((item) => item.serial === selected.serial);
      if (settled?.result) {
        dropRecord(settledRunsRef.current, settled);
        projectSettledRun(settled, settled.result);
      }
    } else if (successor) {
      if (canAttachRun(successor, false)) attachDetachedRun(successor, owner);
    } else if (
      newerIntent(intentNow, runNow)
      && (intentNow.phase === "failed" || askJobIdRef.current === null)
    ) {
      attachIntentRun(intentNow, owner);
    } else if (runNow) {
      // Only a detail that actually loaded can vouch that an already-running
      // job is terminal; a failed detail read says nothing, and the still-live
      // local transport is then the best state this view has.
      const detailVouches = runNow === run && runStartedBeforeDetail && detailLoaded;
      if (canAttachRun(runNow, detailVouches)) attachDetachedRun(runNow, owner);
    }
    // Whatever settled for this identity is now either projected or part of the
    // history this view just loaded; release it.
    settledRunsRef.current = settledRunsRef.current.filter((item) => item.key !== ownerKey(owner));
  }

  async function restoreNotebook(owner: AskNotebookTransition): Promise<boolean> {
    if (!sameViewOwner(ownerRef.current, owner)) return false;
    try {
      // Work detached by navigation outranks "latest in history": an intent
      // preview leaves no server-side trace at all, and a durable run may not
      // have reached `started` yet, so opening the previous latest session over
      // either would hide the question. Selected BEFORE the list read: a run
      // that settles during that read must still be recognised (and its result
      // projected) rather than vanish between the two reads.
      const selection = selectDetachedWork(owner);
      const list = await loadSessionsFor(owner);
      if (!sameViewOwner(ownerRef.current, owner)) return false;
      const { intentRun, run } = selection;
      // Nothing in memory (a fresh tab after a reload): this tab's persisted
      // preview/review, if any, is the work to bring back.
      const resumed = intentRun === null && run === null
        ? await resumePersistedIntent(owner, list)
        : null;
      if (!sameViewOwner(ownerRef.current, owner)) return false;
      // A handed-off question opens its conversation — or, for a first question,
      // the latest session — so history can say whether the job already exists.
      const latestId = resumed?.kind === "handoff"
        ? resumed.record.conversationIdAtStart ?? list?.[0]?.id
        : resumed
          ? resumed.record.conversationIdAtStart
          : newerIntent(intentRun, run)
            ? intentRun.conversationIdAtStart
            : run ? run.conversationId ?? run.conversationIdAtStart : list?.[0]?.id;
      const loaded = await restoreLatestConversation(
        latestId ? [{ id: latestId }] : [],
        (id) => applySessionDetail(id, owner),
      );
      if (!sameViewOwner(ownerRef.current, owner)) return false;
      if (resumed?.kind === "handoff") {
        // Reconciliation needs history that actually loaded: a failed list or
        // detail read proves nothing about whether the original POST committed,
        // and offering the question for re-submission on that basis could
        // create a duplicate job. Keep the mirror for the next attempt instead.
        const reconciled = latestId ? loaded === true : list !== null;
        if (reconciled) resumeHandoff(resumed.record, owner);
        else releaseIntentRun(resumed.record.id);
      } else if (resumed) {
        attachResumedIntent(resumed.record, owner, latestId ? loaded === true : null);
      } else {
        applyDetachedWork(owner, selection, loaded === true);
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
    draftModeRef.current = null;
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
      // Work detached in THIS conversation (a follow-up not yet `started`, a
      // pending clarification, even the run this very click just detached)
      // comes back with the session — the detail alone cannot show it.
      const selection = selectDetachedWork(owner, id);
      let applied = false;
      let failure: unknown = null;
      try {
        applied = await applySessionDetail(id, owner);
      } catch (error) {
        failure = error;
      }
      if (!sameViewOwner(ownerRef.current, owner)) return false;
      if (applied && !selection.selected) {
        // Fresh tab (nothing in memory): this conversation may still have a
        // persisted preview/review from before a reload.
        const resumed = await resumePersistedIntent(owner, [{ id }], id);
        if (!sameViewOwner(ownerRef.current, owner)) return false;
        if (resumed?.kind === "handoff") {
          resumeHandoff(resumed.record, owner);
        } else if (resumed) {
          attachResumedIntent(resumed.record, owner, true);
        }
      } else if (applied) {
        applyDetachedWork(owner, selection, true, id);
      } else if (failure !== null && selection.selected) {
        // The detail read failed but this view already owns the session: the
        // live local run is the best state it has. Its transcript is unknown,
        // so the pending turn stands alone rather than over another session's.
        turnsRef.current = [];
        setTurns([]);
        applyDetachedWork(owner, selection, false, id);
      }
      if (failure !== null) throw failure;
      return applied;
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
    draftModeRef.current = null;
    modeChoiceVersionRef.current += 1;
    modeRef.current = next;
    setMode(next);
  }

  function selectRetrievalEffort(next: AskRetrievalEffortId) {
    if (!currentNotebookOwner()) return;
    setRetrievalEffort(next);
  }

  useEffect(() => () => {
    // This hook instance is going away (a reload releases the locks with the
    // page; an in-app unmount must not keep owning records it can no longer
    // resume — a later mount in this tab re-claims them from storage). Retire
    // the continuation FIRST: a pending preview must not keep running and hand
    // off a durable job next to the one the replacement instance will start
    // from the same stored record. The mirror itself is kept for that instance.
    for (const run of intentRunsRef.current) {
      run.keepMirror = true;
      run.cancelRequested = true;
      run.controller.abort();
      releaseIntentRun(run.persistId);
    }
    intentRunsRef.current = [];
    // Same for a hand-off whose durable POST has not been acknowledged yet:
    // once `started` arrived the server owns it (history restores it); before
    // that the old stream must not keep running next to whatever the next
    // instance does with the stored hand-off.
    for (const run of inFlightRunsRef.current) {
      if (run.mirrorId === null || run.jobId !== null) continue;
      if (run.cancelRequested) {
        // Stop was already pressed: a stopped question must never come back as
        // a resendable draft (its job may still start after the disconnect).
        removePersistedIntentRun(run.mirrorId);
        releaseIntentRun(run.mirrorId);
        continue;
      }
      run.keepMirror = true;
      run.cancelRequested = true;
      run.controller.abort();
      releaseIntentRun(run.mirrorId);
    }
  }, []);

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
    // The UI mode is per actor: a reasoning preview/review detached in any
    // notebook must not start a reasoning job or re-open its review after the
    // switch. Cancel it and let the next restore of that notebook hand the
    // question back with a reason, exactly like the visible one above.
    for (const run of intentRunsRef.current) {
      if (run.cancelRequested || run.phase === "failed") continue;
      run.controller.abort();
      run.phase = "failed";
      run.failure = humanizedError("已切换到自动模式，未完成的问题理解已取消，问题已退回输入框");
      forgetPersistedIntent(run);
    }
    // Records that were never materialized in this instance (older sessions'
    // previews/reviews after a reload) live only in storage: they belong to
    // the same actor and must not resume once the user returns to advanced.
    if (actorIdRef.current) clearPersistedIntentRuns(actorIdRef.current);
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
    serial?: number,
    mirrorId?: string,
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
      serial,
      mirrorId,
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
    // A run handed over from an intent preview keeps that preview's submission
    // order, so an older question finishing late never outranks a newer one.
    serial: number = nextRunSerial(),
    // The preview's storage mirror (ask-intent-persist.ts). It outlives the
    // hand-off until `started` proves the server owns the question, and is
    // retired if the stream ends without ever starting.
    mirrorId?: string,
  ): Promise<boolean> {
    let startedConversationId = conversationIdAtStart;
    const retireMirror = () => {
      if (!mirrorId) return;
      removePersistedIntentRun(mirrorId);
      releaseIntentRun(mirrorId);
    };
    const controller = new AbortController();
    const run: AskRunRecord = {
      key: ownerKey(runOwner),
      serial,
      owner: runOwner,
      cancelRequested: false,
      notebookId: runOwner.notebookId,
      question: q,
      askedAt,
      mode: selectedMode,
      retrievalEffort: effort,
      trace: [...traceSeed],
      conversationIdAtStart,
      conversationId: null,
      jobId: null,
      controller,
      result: null,
      failure: null,
      mirrorId: mirrorId ?? null,
      keepMirror: false,
    };
    inFlightRunsRef.current.push(run);
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
          // The server now owns the question durably — whatever happens next
          // (including the pre-start Stop below), the tab's mirror is done.
          retireMirror();
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
        // Retire the in-flight record BEFORE the history refresh below awaits:
        // a restore that resolves its detail in that window must see this run
        // as settled (and project its result), not as still running.
        run.result = response;
        dropRecord(inFlightRunsRef.current, run);
        settledRunsRef.current.push(run);
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
        // Failed before `started` while nobody was looking (not a Stop): the
        // question exists nowhere else, so keep the record for the next restore
        // to report it and hand the question back. After `started` a transport
        // failure says nothing about the durable job — history/reconnect own it.
        if (!isAbortError(error) && !run.cancelRequested && run.jobId === null) {
          run.failure = error;
        } else {
          // Nothing left to re-attach: retire before the refresh below awaits so
          // a racing restore does not bind a dead transport to its view.
          dropRecord(inFlightRunsRef.current, run);
        }
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
      // Ended without `started` (failed, stopped, or a transport that never
      // reached the server): the question is back with the user, not pending —
      // unless an in-app unmount retired this stream and left the mirror for
      // the next hook instance to reconcile.
      if (run.jobId === null && !run.keepMirror) retireMirror();
      if (!run.failure) dropRecord(inFlightRunsRef.current, run);
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
    draftModeRef.current = null;
    if (submitMode !== "reasoning") {
      await executeAsk(q, submitMode, undefined, [], askedAt, scopeSnapshot);
      return;
    }
    const run: AskIntentRunRecord = {
      key: ownerKey(owner),
      serial: nextRunSerial(),
      owner,
      cancelRequested: false,
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
      failure: null,
      persistId: newIntentRunId(),
      keepMirror: false,
      mirrored: false,
    };
    intentRunsRef.current.push(run);
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
    // Own this submission across tabs BEFORE exposing its mirror: a tab copied
    // from this one must find the lock already taken, never a record it can
    // claim first. The id is fresh, so the claim only fails without Web Locks
    // support (then it is granted) — but the order is what makes it airtight.
    const owned = await claimIntentRun(run.persistId);
    if (run.keepMirror) {
      // Unmounted while the claim was pending: the continuation is retired,
      // but the question must still reach the mirror for the next instance —
      // and the lock just granted must not stay held by a dead instance.
      run.mirrored = owned;
      persistIntentRun(run);
      releaseIntentRun(run.persistId);
      return;
    }
    if (run.cancelRequested || run.phase === "failed") {
      releaseIntentRun(run.persistId);
      return;
    }
    run.mirrored = owned;
    persistIntentRun(run);
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
      if (run.cancelRequested || run.phase === "failed") return;
      const understandingMs = elapsedMs(understandingStartedAt, Date.now());
      run.understandingMs = understandingMs;
      if (contract.needs_clarification) {
        run.trace = replaceLastIntentStep(run.trace, intentClarifyStep(contract, understandingMs));
        run.phase = "review";
        run.contract = contract;
        persistIntentRun(run);
        if (attached()) presentIntentReview(run, contract);
        return;
      }
      run.trace = replaceLastIntentStep(run.trace, intentUnderstoodStep(contract, understandingMs));
      dropRecord(intentRunsRef.current, run);
      const confirmation = buildAskIntentConfirmation(
        contract,
        contract.resolved_question,
        {},
        understandingMs,
      );
      // The durable job takes over from here, but only `started` proves the
      // server has it: keep the mirror (now carrying the confirmed intent) so a
      // reload in that window can re-submit instead of losing the question.
      persistHandoff(run, confirmation);
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
          run.serial,
          run.persistId,
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
        run.serial,
        run.persistId,
      );
      if (!started) forgetPersistedIntent(run);
      if (releaseIntentDraft(run.draftToken) && !started) {
        setQuestion(run.question);
        askIntentTraceRef.current = [];
        clearPendingTurn();
      }
    } catch (error) {
      // Already retired as failed (e.g. the UI mode switched while detached):
      // the record is waiting for a restore to hand the question back.
      if (run.phase === "failed") return;
      if (!isAbortError(error) && !run.cancelRequested && !attached()) {
        // Failed while detached: nothing visible to report to, and the record
        // is the only place the question still exists. Keep it for the restore.
        run.phase = "failed";
        run.failure = error;
        return;
      }
      dropRecord(intentRunsRef.current, run);
      // Aborted or failed on-screen: the question goes back to the input (or was
      // stopped on purpose) — nothing for a reload to resume. The one exception
      // is an in-app unmount, which retires this continuation but leaves the
      // stored record for the next hook instance in this tab.
      if (run.keepMirror) return;
      forgetPersistedIntent(run);
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
    if (
      review.notebookId !== owner.notebookId
      || review.conversationId !== conversationIdRef.current
      || modeRef.current !== "reasoning"
    ) {
      if (run) {
        run.cancelRequested = true;
        dropRecord(intentRunsRef.current, run);
        forgetPersistedIntent(run);
      }
      setIntentReview(null);
      askIntentTraceRef.current = [];
      releaseIntentDraft(draftToken);
      clearPendingTurn();
      effectsRef.current.notify("问题上下文已经变化，请重新提交");
      return;
    }
    // Confirmation hands the question to the durable run; the in-memory preview
    // record has nothing left to re-attach, but its mirror (now carrying the
    // confirmation) stays until `started` proves the server owns the question.
    if (run) {
      dropRecord(intentRunsRef.current, run);
      persistHandoff(run, confirmation);
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
        run?.retrievalEffort,
        run?.serial,
        run?.persistId,
      );
      if (!started && run) forgetPersistedIntent(run);
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
      dropRecord(intentRunsRef.current, run);
      forgetPersistedIntent(run);
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
    pruneTombstonedRecords(owner);
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
    pruneTombstonedRecords(owner);
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
