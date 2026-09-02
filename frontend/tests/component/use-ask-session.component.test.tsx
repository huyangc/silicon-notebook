import { act, cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import type { AskIntentConfirmation, QueryIntentContract } from "../../app/ask-intent-model";
import type { AskJobDetail } from "../../app/ask-reconnect";
import type { ReasoningTraceStep } from "../../app/ask-stream";
import type {
  AskResponse,
  ConversationDetail,
  ConversationSummary,
} from "../../app/workspace-model";

const api = vi.hoisted(() => ({
  bulkDeleteConversations: vi.fn(),
  cancelAskJob: vi.fn(),
  deleteConversation: vi.fn(),
  fetchAskModes: vi.fn(),
  getAskJob: vi.fn(),
  getConversation: vi.fn(),
  listConversations: vi.fn(),
  previewAskIntent: vi.fn(),
  renameConversation: vi.fn(),
  runAskStream: vi.fn(),
  submitFeedback: vi.fn(),
}));

vi.mock("../../app/ask-api.ts", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../app/ask-api.ts")>()),
  ...api,
}));

import { useAskSession } from "../../app/use-ask-session";

type HookValue = ReturnType<typeof useAskSession>;
type HookOptions = Parameters<typeof useAskSession>[0];
type AskPolicy = HookOptions["policy"];

const DEFAULT_POLICY: AskPolicy = {
  advanced: true,
  askUnavailable: false,
  scopeBlocked: false,
  kgAvailable: true,
  sourceScope: { mode: "exclude", source_ids: [] },
  baseScope: { mode: "exclude", notebook_ids: [] },
};

const effects: HookOptions["effects"] = {
  notify: vi.fn(),
  reportError: vi.fn(),
  ensureAskVisible: vi.fn(),
};

let value: HookValue | null = null;

function Harness({
  actorId = "user-a",
  notebookId = "notebook-a",
  policy = DEFAULT_POLICY,
}: {
  actorId?: string | null;
  notebookId?: string | null;
  policy?: AskPolicy;
}) {
  value = useAskSession({ actorId, notebookId, policy, effects });
  return (
    <div data-testid="ask-state">
      {value.conversationId ?? "none"}:{value.sessions.map((item) => item.id).join(",")}
    </div>
  );
}

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function answer(conversationId: string, answerId = `answer-${conversationId}`): AskResponse {
  return {
    answer_id: answerId,
    conversation_id: conversationId,
    conclusion: "done",
    answer: "done",
    grounded: true,
    anchors: [],
    related_knowledge: [],
    citations: [],
    llm_mode: "test",
    mode: "chunk",
  };
}

function summary(id: string, title = id): ConversationSummary {
  return {
    id,
    title,
    updated_at: "2026-08-22T00:00:00Z",
    turn_count: 1,
    used_reasoning: false,
  };
}

function detail(
  id: string,
  notebookId = "notebook-a",
  options: { activeJob?: ConversationDetail["active_job"]; question?: string } = {},
): ConversationDetail {
  const response = answer(id);
  return {
    id,
    notebook_id: notebookId,
    title: id,
    updated_at: "2026-08-22T00:00:00Z",
    turn_count: 1,
    turns: [{
      answer_id: response.answer_id,
      question: options.question ?? `question-${id}`,
      response,
      asked_at: "2026-08-22T00:00:00Z",
      created_at: "2026-08-22T00:00:00Z",
    }],
    ...(options.activeJob ? { active_job: options.activeJob } : {}),
  };
}

function runningJob(jobId = "job-a"): AskJobDetail {
  return {
    job_id: jobId,
    status: "running",
    mode: "reasoning",
    question: "question",
    trace: [],
    answer_id: "",
    error: "",
  };
}

function doneJob(jobId = "job-a"): AskJobDetail {
  return {
    ...runningJob(jobId),
    status: "done",
    answer_id: `answer-${jobId}`,
  };
}

function contractFor(question: string, needsClarification: boolean): QueryIntentContract {
  return {
    objective: question,
    resolved_question: question,
    intent_type: "other",
    result_scope: "ranked",
    completeness_required: false,
    entities: [],
    mandatory_topics: [],
    comparison_axes: [],
    constraints: [],
    excluded_topics: [],
    expected_output: "answer",
    assumptions: [],
    ambiguities: needsClarification ? [{ id: "which", question: "Which one?", required: true }] : [],
    confidence: needsClarification ? 0.5 : 0.9,
    needs_clarification: needsClarification,
    confirmed: false,
  };
}

function beginOwnedNotebook(workspaceEpoch = 1) {
  let owner: ReturnType<HookValue["beginNotebookTransition"]> = null;
  act(() => {
    owner = value!.beginNotebookTransition({
      actorId: "user-a",
      notebookId: "notebook-a",
      workspaceEpoch,
    });
  });
  expect(owner).not.toBeNull();
  act(() => value!.finishNotebookTransition(owner!));
  return owner!;
}

beforeEach(() => {
  value = null;
  for (const mock of Object.values(api)) mock.mockReset();
  vi.clearAllMocks();
  api.listConversations.mockResolvedValue([]);
  api.fetchAskModes.mockResolvedValue([]);
  api.cancelAskJob.mockResolvedValue(undefined);
  api.deleteConversation.mockResolvedValue(undefined);
  api.renameConversation.mockResolvedValue(undefined);
  api.submitFeedback.mockResolvedValue(undefined);
  api.bulkDeleteConversations.mockResolvedValue({ deleted: 0, deleted_ids: [] });
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

test("notebook restore performs one list read and one latest-detail read", async () => {
  api.listConversations.mockResolvedValue([summary("conversation-latest")]);
  api.getConversation.mockResolvedValue(detail("conversation-latest"));
  render(<Harness />);

  let owner: ReturnType<HookValue["beginNotebookTransition"]> = null;
  act(() => {
    owner = value!.beginNotebookTransition({
      actorId: "user-a",
      notebookId: "notebook-a",
      workspaceEpoch: 1,
    });
  });
  await act(async () => {
    await value!.restoreNotebook(owner!);
  });

  expect(api.listConversations).toHaveBeenCalledTimes(1);
  expect(api.listConversations).toHaveBeenCalledWith("notebook-a");
  expect(api.getConversation).toHaveBeenCalledTimes(1);
  expect(api.getConversation).toHaveBeenCalledWith("conversation-latest");
  expect(value!.conversationId).toBe("conversation-latest");
  expect(value!.turns).toHaveLength(1);
  expect(api.fetchAskModes).not.toHaveBeenCalled();

  await act(async () => {
    value!.finishNotebookTransition(owner!, true);
  });
  expect(api.fetchAskModes).toHaveBeenCalledTimes(1);
});

test("runtime Ask modes load once per actor generation and restore the exact plugin mode", async () => {
  api.fetchAskModes.mockResolvedValue([{
    id: "corp.search",
    group: "extension",
    label: "企业检索",
    desc: "使用部署内检索策略回答",
    requires_kg: false,
    streaming: true,
    streams_trace: true,
  }]);
  api.listConversations.mockResolvedValue([summary("conversation-plugin")]);
  const pluginDetail = detail("conversation-plugin");
  pluginDetail.turns[0].response.mode = "corp.search";
  api.getConversation.mockResolvedValue(pluginDetail);
  render(<Harness />);

  let owner: ReturnType<HookValue["beginNotebookTransition"]> = null;
  act(() => {
    owner = value!.beginNotebookTransition({
      actorId: "user-a",
      notebookId: "notebook-a",
      workspaceEpoch: 1,
    });
  });
  await act(async () => {
    await value!.restoreNotebook(owner!);
  });
  expect(api.fetchAskModes).not.toHaveBeenCalled();
  await act(async () => {
    value!.finishNotebookTransition(owner!, true);
  });
  expect(api.fetchAskModes).toHaveBeenCalledTimes(1);
  expect(value!.askModes.map((candidate) => candidate.id)).toEqual([
    "chunk", "reasoning", "corp.search",
  ]);
  expect(value!.mode).toBe("corp.search");

  act(() => {
    owner = value!.beginNotebookTransition({
      actorId: "user-a",
      notebookId: "notebook-a",
      workspaceEpoch: 2,
    })!;
  });
  await act(async () => {
    await value!.restoreNotebook(owner!);
    value!.finishNotebookTransition(owner!, true);
  });
  expect(api.fetchAskModes).toHaveBeenCalledTimes(1);
  expect(value!.mode).toBe("corp.search");
});

test("a deployment Ask engine uses the durable Ask stream and receives live plugin trace", async () => {
  const stream = deferred<AskResponse>();
  api.fetchAskModes.mockResolvedValue([{
    id: "corp.search",
    group: "extension",
    label: "企业检索",
    desc: "使用部署内检索策略回答",
    requires_kg: false,
    streaming: true,
    streams_trace: true,
  }]);
  api.runAskStream.mockImplementation((
    _notebookId: string,
    _payload: unknown,
    onProgress: (step: {
      step_type: string;
      summary: string;
      detail: Record<string, unknown>;
      duration_ms?: number;
    }) => void,
  ) => {
    onProgress({
      step_type: "plugin",
      summary: "检索",
      detail: { detail: "命中一条" },
      duration_ms: 125,
    });
    return stream.promise;
  });
  render(<Harness />);
  let owner: ReturnType<HookValue["beginNotebookTransition"]> = null;
  act(() => {
    owner = value!.beginNotebookTransition({
      actorId: "user-a",
      notebookId: "notebook-a",
      workspaceEpoch: 1,
    });
  });
  await act(async () => {
    value!.finishNotebookTransition(owner!, true);
  });
  expect(value!.askModes.some((candidate) => candidate.id === "corp.search")).toBe(true);

  act(() => value!.selectMode("corp.search"));
  let submitting!: Promise<void>;
  act(() => {
    submitting = value!.submit("插件问题");
  });
  expect(value!.pendingTrace).toEqual([{
    step_type: "plugin",
    summary: "检索",
    detail: { detail: "命中一条" },
    duration_ms: 125,
  }]);
  expect(value!.asking).toBe(true);
  stream.resolve({
    ...answer("conversation-plugin"),
    mode: "corp.search",
    reasoning_trace: [{
      step_type: "plugin",
      summary: "检索",
      detail: { detail: "命中一条" },
      duration_ms: 125,
    }],
  });
  await act(async () => {
    await submitting;
  });

  expect(api.previewAskIntent).not.toHaveBeenCalled();
  expect(api.runAskStream).toHaveBeenCalledTimes(1);
  expect(api.runAskStream.mock.calls[0]?.[0]).toBe("notebook-a");
  expect(api.runAskStream.mock.calls[0]?.[1]).toMatchObject({
    question: "插件问题",
    mode: "corp.search",
  });
  expect(value!.turns[0]?.response.mode).toBe("corp.search");
  expect(value!.turns[0]?.response.reasoning_trace?.[0]?.duration_ms).toBe(125);
});

test("simplified mode submits the backend auto selector without intent preview", async () => {
  const simplifiedPolicy: AskPolicy = { ...DEFAULT_POLICY, advanced: false };
  api.runAskStream.mockResolvedValue({
    ...answer("conversation-auto"),
    mode: "chunk",
  });
  render(<Harness policy={simplifiedPolicy} />);
  beginOwnedNotebook();

  // A stale advanced-mode choice must neither leak into the request nor be
  // destroyed; the hidden surface delegates this turn to the backend. The
  // mocked response resolves to a DIFFERENT mode ("chunk") than the one
  // selected ("reasoning") specifically so this proves value.mode still
  // reflects the user's own selection instead of being silently overwritten
  // by response.mode.
  act(() => value!.selectMode("reasoning"));
  await act(async () => {
    await value!.submit("请判断这个问题需要怎样分析");
  });

  expect(api.previewAskIntent).not.toHaveBeenCalled();
  expect(api.runAskStream).toHaveBeenCalledTimes(1);
  expect(api.runAskStream.mock.calls[0]?.[1]).toMatchObject({
    question: "请判断这个问题需要怎样分析",
    mode: "auto",
  });
  expect(value!.mode).toBe("reasoning");
  expect(value!.turns[0]?.response.mode).toBe("chunk");
});

test("a failed Ask-mode projection retries on the next committed workspace", async () => {
  api.fetchAskModes
    .mockRejectedValueOnce(new Error("projection unavailable"))
    .mockResolvedValueOnce([{
      id: "corp.search",
      group: "extension",
      label: "企业检索",
      desc: "使用部署内检索策略回答",
      requires_kg: false,
      streaming: true,
      streams_trace: true,
    }]);
  render(<Harness />);

  let owner: ReturnType<HookValue["beginNotebookTransition"]> = null;
  act(() => {
    owner = value!.beginNotebookTransition({
      actorId: "user-a",
      notebookId: "notebook-a",
      workspaceEpoch: 1,
    });
  });
  await act(async () => {
    await value!.restoreNotebook(owner!);
    value!.finishNotebookTransition(owner!, true);
  });
  expect(api.fetchAskModes).toHaveBeenCalledTimes(1);
  expect(value!.askModes.map((candidate) => candidate.id)).toEqual([
    "chunk", "reasoning",
  ]);

  act(() => {
    owner = value!.beginNotebookTransition({
      actorId: "user-a",
      notebookId: "notebook-a",
      workspaceEpoch: 2,
    })!;
  });
  await act(async () => {
    await value!.restoreNotebook(owner!);
    value!.finishNotebookTransition(owner!, true);
  });
  expect(api.fetchAskModes).toHaveBeenCalledTimes(2);
  expect(value!.askModes.map((candidate) => candidate.id)).toEqual([
    "chunk", "reasoning", "corp.search",
  ]);
});

test("a rolled-back notebook transition performs no Ask-mode projection I/O", async () => {
  render(<Harness />);

  let owner: ReturnType<HookValue["beginNotebookTransition"]> = null;
  act(() => {
    owner = value!.beginNotebookTransition({
      actorId: "user-a",
      notebookId: "notebook-a",
      workspaceEpoch: 1,
    });
  });
  await act(async () => {
    await value!.restoreNotebook(owner!);
  });
  expect(api.fetchAskModes).not.toHaveBeenCalled();

  await act(async () => {
    value!.finishNotebookTransition(owner!, false);
  });
  expect(api.fetchAskModes).not.toHaveBeenCalled();
});

test("a historical plugin mode falls back when that engine is not projected", async () => {
  api.listConversations.mockResolvedValue([summary("conversation-disabled-plugin")]);
  const disabled = detail("conversation-disabled-plugin");
  disabled.turns[0].response.mode = "corp.disabled";
  api.getConversation.mockResolvedValue(disabled);
  render(<Harness />);

  const owner = beginOwnedNotebook();
  await act(async () => {
    await value!.restoreNotebook(owner);
  });
  expect(value!.mode).toBe("chunk");
});

test("empty restore skips detail and a foreign-notebook detail never commits", async () => {
  render(<Harness />);
  let owner = beginOwnedNotebook();
  await act(async () => {
    await value!.restoreNotebook(owner);
  });
  expect(api.listConversations).toHaveBeenCalledTimes(1);
  expect(api.getConversation).not.toHaveBeenCalled();
  expect(value!.conversationId).toBeNull();

  api.listConversations.mockResolvedValueOnce([summary("foreign")]);
  api.getConversation.mockResolvedValueOnce(detail("foreign", "notebook-b"));
  act(() => {
    owner = value!.beginNotebookTransition({
      actorId: "user-a",
      notebookId: "notebook-a",
      workspaceEpoch: 2,
    })!;
  });
  await act(async () => {
    await value!.restoreNotebook(owner);
    value!.finishNotebookTransition(owner);
  });

  expect(api.getConversation).toHaveBeenCalledTimes(1);
  expect(value!.conversationId).toBeNull();
  expect(value!.turns).toEqual([]);
});

test("a late delete of A cannot clear newly opened B in the same notebook", async () => {
  const deletion = deferred<undefined>();
  api.deleteConversation.mockReturnValueOnce(deletion.promise);
  api.listConversations.mockResolvedValue([summary("conversation-a"), summary("conversation-b")]);
  api.getConversation.mockImplementation(async (id: string) => detail(id));
  render(<Harness />);

  const owner = beginOwnedNotebook();
  await act(async () => {
    await value!.restoreNotebook(owner);
  });
  expect(value!.conversationId).toBe("conversation-a");

  let deleting!: Promise<void>;
  act(() => {
    deleting = value!.deleteSession("conversation-a");
  });
  await act(async () => {
    await value!.openSession("conversation-b", 2);
  });
  expect(value!.conversationId).toBe("conversation-b");

  deletion.resolve(undefined);
  await act(async () => deleting);

  expect(value!.conversationId).toBe("conversation-b");
  expect(value!.turns[0]?.response.conversation_id).toBe("conversation-b");
  expect(value!.sessions.map((item) => item.id)).toEqual(["conversation-b"]);
});

test("same-notebook view detach preserves the durable stream and publishes its started session", async () => {
  const stream = deferred<AskResponse>();
  let signal: AbortSignal | undefined;
  let onStart: ((jobId: string, conversationId: string) => void | Promise<void>) | undefined;
  api.runAskStream.mockImplementation((
    _notebookId: string,
    _payload: unknown,
    _onProgress: unknown,
    nextSignal?: AbortSignal,
    nextOnStart?: (jobId: string, conversationId: string) => void | Promise<void>,
  ) => {
    signal = nextSignal;
    onStart = nextOnStart;
    return stream.promise;
  });
  api.listConversations.mockResolvedValue([summary("conversation-durable")]);
  render(<Harness />);
  beginOwnedNotebook();

  let submitting!: Promise<void>;
  act(() => {
    submitting = value!.submit("durable question");
  });
  act(() => value!.startNewSession(2));

  expect(signal?.aborted).toBe(false);
  expect(api.cancelAskJob).not.toHaveBeenCalled();
  await act(async () => {
    await onStart!("job-durable", "conversation-durable");
  });
  expect(value!.sessions.map((item) => item.id)).toEqual(["conversation-durable"]);
  expect(value!.conversationId).toBeNull();
  expect(signal?.aborted).toBe(false);

  stream.resolve(answer("conversation-durable"));
  await act(async () => submitting);
  expect(api.cancelAskJob).not.toHaveBeenCalled();
  expect(value!.conversationId).toBeNull();
  expect(value!.turns).toEqual([]);
});

test("abort before started waits for the durable job id, cancels once, then aborts transport", async () => {
  const cancellation = deferred<undefined>();
  api.cancelAskJob.mockReturnValueOnce(cancellation.promise);
  let signal: AbortSignal | undefined;
  let onStart: ((jobId: string, conversationId: string) => void | Promise<void>) | undefined;
  api.runAskStream.mockImplementation((
    _notebookId: string,
    _payload: unknown,
    _onProgress: unknown,
    nextSignal?: AbortSignal,
    nextOnStart?: (jobId: string, conversationId: string) => void | Promise<void>,
  ) => {
    signal = nextSignal;
    onStart = nextOnStart;
    return new Promise<AskResponse>((_resolve, reject) => {
      nextSignal?.addEventListener("abort", () => {
        reject(new DOMException("cancelled", "AbortError"));
      }, { once: true });
    });
  });
  render(<Harness />);
  beginOwnedNotebook();

  let submitting!: Promise<void>;
  act(() => {
    submitting = value!.submit("keep my draft");
  });
  act(() => value!.abort());
  expect(value!.question).toBe("keep my draft");
  expect(api.cancelAskJob).not.toHaveBeenCalled();
  expect(signal?.aborted).toBe(false);

  let starting!: void | Promise<void>;
  act(() => {
    starting = onStart!("job-late", "conversation-late");
  });
  expect(api.cancelAskJob).toHaveBeenCalledTimes(1);
  expect(api.cancelAskJob).toHaveBeenCalledWith(
    "notebook-a",
    "job-late",
  );
  expect(signal?.aborted).toBe(false);
  act(() => value!.abort());
  expect(api.cancelAskJob).toHaveBeenCalledTimes(1);

  cancellation.resolve(undefined);
  await act(async () => {
    await starting;
    await submitting;
  });
  expect(signal?.aborted).toBe(true);
  expect(api.cancelAskJob).toHaveBeenCalledTimes(1);
  expect(value!.question).toBe("keep my draft");
});

test("pre-start cancellation remains single-flight after history restores the active job", async () => {
  const cancellation = deferred<undefined>();
  api.cancelAskJob.mockReturnValue(cancellation.promise);
  let signal: AbortSignal | undefined;
  let onStart: ((jobId: string, conversationId: string) => void | Promise<void>) | undefined;
  api.runAskStream.mockImplementation((
    _notebookId: string,
    _payload: unknown,
    _onProgress: unknown,
    nextSignal?: AbortSignal,
    nextOnStart?: (jobId: string, conversationId: string) => void | Promise<void>,
  ) => {
    signal = nextSignal;
    onStart = nextOnStart;
    return new Promise<AskResponse>((_resolve, reject) => {
      nextSignal?.addEventListener("abort", () => {
        reject(new DOMException("cancelled", "AbortError"));
      }, { once: true });
    });
  });
  api.getConversation.mockResolvedValue(detail(
    "conversation-pre-start",
    "notebook-a",
    {
      activeJob: {
        job_id: "job-pre-start",
        question: "pre-start question",
        asked_at: "2026-08-22T00:00:00Z",
        mode: "reasoning",
        trace: [],
      },
    },
  ));
  render(<Harness />);
  beginOwnedNotebook();

  let submitting!: Promise<void>;
  act(() => {
    submitting = value!.submit("pre-start question");
  });
  act(() => value!.abort());
  let starting!: void | Promise<void>;
  act(() => {
    starting = onStart!("job-pre-start", "conversation-pre-start");
  });
  expect(api.cancelAskJob).toHaveBeenCalledTimes(1);
  expect(signal?.aborted).toBe(false);

  act(() => value!.startNewSession(2));
  await act(async () => {
    await value!.openSession("conversation-pre-start", 3);
  });
  expect(value!.asking).toBe(true);
  act(() => value!.abort());
  expect(api.cancelAskJob).toHaveBeenCalledTimes(1);
  expect(signal?.aborted).toBe(false);

  cancellation.resolve(undefined);
  await act(async () => {
    await starting;
    await submitting;
  });
  expect(signal?.aborted).toBe(true);
  expect(api.cancelAskJob).toHaveBeenCalledTimes(1);
  act(() => value!.abort());
  expect(api.cancelAskJob).toHaveBeenCalledTimes(1);
});

test("restored cancellation authority wins a late onStart and releases after failure", async () => {
  const stream = deferred<AskResponse>();
  const firstCancellation = deferred<undefined>();
  const secondCancellation = deferred<undefined>();
  api.cancelAskJob
    .mockReturnValueOnce(firstCancellation.promise)
    .mockReturnValueOnce(secondCancellation.promise);
  let onStart: ((jobId: string, conversationId: string) => void | Promise<void>) | undefined;
  api.runAskStream.mockImplementation((
    _notebookId: string,
    _payload: unknown,
    _onProgress: unknown,
    _signal?: AbortSignal,
    nextOnStart?: (jobId: string, conversationId: string) => void | Promise<void>,
  ) => {
    onStart = nextOnStart;
    return stream.promise;
  });
  api.getConversation.mockResolvedValue(detail(
    "conversation-restored-first",
    "notebook-a",
    {
      activeJob: {
        job_id: "job-restored-first",
        question: "restored-first question",
        asked_at: "2026-08-22T00:00:00Z",
        mode: "reasoning",
        trace: [],
      },
    },
  ));
  render(<Harness />);
  beginOwnedNotebook();

  let submitting!: Promise<void>;
  act(() => {
    submitting = value!.submit("restored-first question");
  });
  act(() => value!.abort());
  act(() => value!.startNewSession(2));
  await act(async () => {
    await value!.openSession("conversation-restored-first", 3);
  });

  act(() => value!.abort());
  expect(api.cancelAskJob).toHaveBeenCalledTimes(1);
  await act(async () => {
    await onStart!("job-restored-first", "conversation-restored-first");
  });
  expect(api.cancelAskJob).toHaveBeenCalledTimes(1);

  firstCancellation.reject(new Error("cancel unavailable"));
  await act(async () => {
    await firstCancellation.promise.catch(() => undefined);
    await Promise.resolve();
    await Promise.resolve();
  });
  act(() => value!.abort());
  expect(api.cancelAskJob).toHaveBeenCalledTimes(2);

  secondCancellation.resolve(undefined);
  await act(async () => {
    await secondCancellation.promise;
    await Promise.resolve();
    await Promise.resolve();
  });
  stream.resolve(answer("conversation-restored-first"));
  await act(async () => submitting);
  act(() => value!.abort());
  expect(api.cancelAskJob).toHaveBeenCalledTimes(2);
});

test("failed pre-start cancellation releases restored authority for one retry", async () => {
  const stream = deferred<AskResponse>();
  const firstCancellation = deferred<undefined>();
  const secondCancellation = deferred<undefined>();
  api.cancelAskJob
    .mockReturnValueOnce(firstCancellation.promise)
    .mockReturnValueOnce(secondCancellation.promise);
  let onStart: ((jobId: string, conversationId: string) => void | Promise<void>) | undefined;
  api.runAskStream.mockImplementation((
    _notebookId: string,
    _payload: unknown,
    _onProgress: unknown,
    _signal?: AbortSignal,
    nextOnStart?: (jobId: string, conversationId: string) => void | Promise<void>,
  ) => {
    onStart = nextOnStart;
    return stream.promise;
  });
  api.getConversation.mockResolvedValue(detail(
    "conversation-pre-start-retry",
    "notebook-a",
    {
      activeJob: {
        job_id: "job-pre-start-retry",
        question: "pre-start retry question",
        asked_at: "2026-08-22T00:00:00Z",
        mode: "reasoning",
        trace: [],
      },
    },
  ));
  render(<Harness />);
  beginOwnedNotebook();

  let submitting!: Promise<void>;
  act(() => {
    submitting = value!.submit("pre-start retry question");
  });
  act(() => value!.abort());
  let starting!: void | Promise<void>;
  act(() => {
    starting = onStart!("job-pre-start-retry", "conversation-pre-start-retry");
  });
  expect(api.cancelAskJob).toHaveBeenCalledTimes(1);

  act(() => value!.startNewSession(2));
  await act(async () => {
    await value!.openSession("conversation-pre-start-retry", 3);
  });
  act(() => value!.abort());
  expect(api.cancelAskJob).toHaveBeenCalledTimes(1);

  firstCancellation.reject(new Error("cancel unavailable"));
  await act(async () => {
    await starting;
    await Promise.resolve();
  });
  act(() => value!.abort());
  expect(api.cancelAskJob).toHaveBeenCalledTimes(2);
  act(() => value!.abort());
  expect(api.cancelAskJob).toHaveBeenCalledTimes(2);

  secondCancellation.resolve(undefined);
  await act(async () => {
    await secondCancellation.promise;
    await Promise.resolve();
    await Promise.resolve();
  });
  stream.reject(new DOMException("cancelled", "AbortError"));
  await act(async () => submitting);
  expect(api.cancelAskJob).toHaveBeenCalledTimes(2);
});

test("failed pre-start cancellation keeps transport alive and only refreshes same-identity history", async () => {
  const stream = deferred<AskResponse>();
  let signal: AbortSignal | undefined;
  let onStart: ((jobId: string, conversationId: string) => void | Promise<void>) | undefined;
  api.cancelAskJob.mockRejectedValueOnce(new Error("cancel unavailable"));
  api.runAskStream.mockImplementation((
    _notebookId: string,
    _payload: unknown,
    _onProgress: unknown,
    nextSignal?: AbortSignal,
    nextOnStart?: (jobId: string, conversationId: string) => void | Promise<void>,
  ) => {
    signal = nextSignal;
    onStart = nextOnStart;
    return stream.promise;
  });
  api.listConversations
    .mockResolvedValueOnce([summary("conversation-visible")])
    .mockResolvedValueOnce([
      summary("conversation-background"),
      summary("conversation-visible"),
    ]);
  api.getConversation.mockResolvedValueOnce(detail("conversation-visible"));
  render(<Harness />);
  const owner = beginOwnedNotebook();
  await act(async () => {
    await value!.restoreNotebook(owner);
  });
  expect(value!.conversationId).toBe("conversation-visible");
  expect(value!.turns[0]?.response.answer_id).toBe("answer-conversation-visible");

  let submitting!: Promise<void>;
  act(() => {
    submitting = value!.submit("background question");
  });
  act(() => value!.abort());
  expect(signal?.aborted).toBe(false);
  expect(api.cancelAskJob).not.toHaveBeenCalled();

  await act(async () => {
    await onStart!("job-background", "conversation-background");
  });
  expect(api.cancelAskJob).toHaveBeenCalledTimes(1);
  expect(api.cancelAskJob).toHaveBeenCalledWith(
    "notebook-a",
    "job-background",
  );
  expect(signal?.aborted).toBe(false);
  expect(effects.notify).toHaveBeenCalledWith(
    "未能中断后台任务；任务将继续完成，可稍后重开查看",
  );
  act(() => value!.abort());
  expect(api.cancelAskJob).toHaveBeenCalledTimes(1);
  expect(value!.conversationId).toBe("conversation-visible");
  expect(value!.turns[0]?.response.answer_id).toBe("answer-conversation-visible");

  stream.resolve(answer("conversation-background"));
  await act(async () => submitting);
  expect(signal?.aborted).toBe(false);
  expect(api.cancelAskJob).toHaveBeenCalledTimes(1);
  expect(api.listConversations).toHaveBeenCalledTimes(2);
  expect(api.listConversations.mock.calls).toEqual([
    ["notebook-a"],
    ["notebook-a"],
  ]);
  expect(value!.sessions.map((item) => item.id)).toEqual([
    "conversation-background",
    "conversation-visible",
  ]);
  expect(value!.conversationId).toBe("conversation-visible");
  expect(value!.turns).toHaveLength(1);
  expect(value!.turns[0]?.response.answer_id).toBe("answer-conversation-visible");
});

test("abort after started cancels exactly once and aborts the local stream", async () => {
  const started = deferred<undefined>();
  const cancellation = deferred<undefined>();
  api.cancelAskJob.mockReturnValueOnce(cancellation.promise);
  let signal: AbortSignal | undefined;
  api.runAskStream.mockImplementation(async (
    _notebookId: string,
    _payload: unknown,
    _onProgress: unknown,
    nextSignal?: AbortSignal,
    onStart?: (jobId: string, conversationId: string) => void | Promise<void>,
  ) => {
    signal = nextSignal;
    await onStart!("job-started", "conversation-started");
    started.resolve(undefined);
    return await new Promise<AskResponse>((_resolve, reject) => {
      nextSignal?.addEventListener("abort", () => {
        reject(new DOMException("cancelled", "AbortError"));
      }, { once: true });
    });
  });
  api.listConversations.mockResolvedValue([summary("conversation-started")]);
  render(<Harness />);
  beginOwnedNotebook();

  let submitting!: Promise<void>;
  act(() => {
    submitting = value!.submit("started question");
  });
  await act(async () => started.promise);
  expect(signal?.aborted).toBe(false);

  act(() => value!.abort());
  expect(api.cancelAskJob).toHaveBeenCalledTimes(1);
  expect(api.cancelAskJob).toHaveBeenCalledWith(
    "notebook-a",
    "job-started",
  );
  expect(signal?.aborted).toBe(false);
  act(() => value!.abort());
  expect(api.cancelAskJob).toHaveBeenCalledTimes(1);
  cancellation.resolve(undefined);
  await act(async () => submitting);
  expect(signal?.aborted).toBe(true);
});

test("a failed started cancellation keeps the stream and Stop retry alive", async () => {
  const started = deferred<undefined>();
  const firstCancel = deferred<undefined>();
  const secondCancel = deferred<undefined>();
  api.cancelAskJob
    .mockReturnValueOnce(firstCancel.promise)
    .mockReturnValueOnce(secondCancel.promise);
  let signal: AbortSignal | undefined;
  api.runAskStream.mockImplementation(async (
    _notebookId: string,
    _payload: unknown,
    _onProgress: unknown,
    nextSignal?: AbortSignal,
    onStart?: (jobId: string, conversationId: string) => void | Promise<void>,
  ) => {
    signal = nextSignal;
    await onStart!("job-started-retry", "conversation-started-retry");
    started.resolve(undefined);
    return await new Promise<AskResponse>((_resolve, reject) => {
      nextSignal?.addEventListener("abort", () => {
        reject(new DOMException("cancelled", "AbortError"));
      }, { once: true });
    });
  });
  render(<Harness />);
  beginOwnedNotebook();

  let submitting!: Promise<void>;
  act(() => {
    submitting = value!.submit("retry Stop question");
  });
  await act(async () => started.promise);

  act(() => value!.abort());
  expect(api.cancelAskJob).toHaveBeenCalledTimes(1);
  expect(signal?.aborted).toBe(false);
  firstCancel.reject(new Error("cancel unavailable"));
  await act(async () => {
    await firstCancel.promise.catch(() => undefined);
    await Promise.resolve();
  });
  expect(signal?.aborted).toBe(false);
  expect(effects.notify).toHaveBeenCalledWith("取消失败，请重试");

  act(() => value!.abort());
  act(() => value!.abort());
  expect(api.cancelAskJob).toHaveBeenCalledTimes(2);
  expect(api.cancelAskJob).toHaveBeenNthCalledWith(
    2,
    "notebook-a",
    "job-started-retry",
  );
  expect(signal?.aborted).toBe(false);
  secondCancel.resolve(undefined);
  await act(async () => submitting);
  expect(signal?.aborted).toBe(true);
});

test("a stalled started cancellation stays single-flight until authority answers", async () => {
  vi.useFakeTimers();
  const cancellation = deferred<undefined>();
  api.cancelAskJob.mockReturnValueOnce(cancellation.promise);
  let signal: AbortSignal | undefined;
  let onStart: ((jobId: string, conversationId: string) => void | Promise<void>) | undefined;
  api.runAskStream.mockImplementation((
    _notebookId: string,
    _payload: unknown,
    _onProgress: unknown,
    nextSignal?: AbortSignal,
    nextOnStart?: (jobId: string, conversationId: string) => void | Promise<void>,
  ) => {
    signal = nextSignal;
    onStart = nextOnStart;
    return new Promise<AskResponse>((_resolve, reject) => {
      nextSignal?.addEventListener("abort", () => {
        reject(new DOMException("cancelled", "AbortError"));
      }, { once: true });
    });
  });
  render(<Harness />);
  beginOwnedNotebook();
  let submitting!: Promise<void>;
  act(() => {
    submitting = value!.submit("authoritative Stop question");
  });
  await act(async () => {
    await onStart!("job-authoritative-cancel", "conversation-authoritative-cancel");
  });

  act(() => value!.abort());
  expect(api.cancelAskJob).toHaveBeenCalledTimes(1);
  expect(signal?.aborted).toBe(false);
  await act(async () => {
    vi.advanceTimersByTime(60 * 60 * 1000);
    await Promise.resolve();
    await Promise.resolve();
  });
  expect(api.cancelAskJob).toHaveBeenCalledTimes(1);
  expect(signal?.aborted).toBe(false);
  act(() => value!.abort());
  expect(api.cancelAskJob).toHaveBeenCalledTimes(1);
  expect(signal?.aborted).toBe(false);

  cancellation.resolve(undefined);
  await act(async () => submitting);
  expect(signal?.aborted).toBe(true);
});

test("reconnect cancellation deduplicates while pending and retries after controller-less failure", async () => {
  vi.useFakeTimers();
  const firstCancel = deferred<undefined>();
  const secondCancel = deferred<undefined>();
  api.cancelAskJob
    .mockReturnValueOnce(firstCancel.promise)
    .mockReturnValueOnce(secondCancel.promise);
  api.listConversations.mockResolvedValue([summary("conversation-reconnect-cancel")]);
  api.getConversation.mockResolvedValue(detail("conversation-reconnect-cancel", "notebook-a", {
    activeJob: {
      job_id: "job-reconnect-cancel",
      question: "reconnect cancel question",
      asked_at: "2026-08-22T00:00:00Z",
      mode: "reasoning",
      trace: [],
    },
  }));
  render(<Harness />);
  const owner = beginOwnedNotebook();
  await act(async () => {
    await value!.restoreNotebook(owner);
    value!.finishNotebookTransition(owner);
  });
  expect(value!.asking).toBe(true);

  act(() => value!.abort());
  act(() => value!.abort());
  expect(api.cancelAskJob).toHaveBeenCalledTimes(1);
  expect(api.cancelAskJob).toHaveBeenCalledWith(
    "notebook-a",
    "job-reconnect-cancel",
  );

  firstCancel.reject(new Error("cancel failed"));
  await act(async () => {
    await firstCancel.promise.catch(() => undefined);
    await Promise.resolve();
    await Promise.resolve();
  });
  expect(effects.notify).toHaveBeenCalledWith("取消失败，请重试");

  act(() => value!.abort());
  act(() => value!.abort());
  expect(api.cancelAskJob).toHaveBeenCalledTimes(2);
  expect(api.cancelAskJob).toHaveBeenNthCalledWith(
    2,
    "notebook-a",
    "job-reconnect-cancel",
  );

  secondCancel.resolve(undefined);
  await act(async () => {
    await secondCancel.promise;
    await Promise.resolve();
    await Promise.resolve();
  });
  expect(api.listConversations).toHaveBeenCalledTimes(2);
  act(() => value!.abort());
  expect(api.cancelAskJob).toHaveBeenCalledTimes(2);
  expect(api.getAskJob).not.toHaveBeenCalled();
});

test("reconnect polling is single-flight and self-schedules only after settlement", async () => {
  vi.useFakeTimers();
  const firstPoll = deferred<AskJobDetail>();
  const secondPoll = deferred<AskJobDetail>();
  api.getAskJob
    .mockReturnValueOnce(firstPoll.promise)
    .mockReturnValueOnce(secondPoll.promise);
  api.listConversations.mockResolvedValue([summary("conversation-running")]);
  api.getConversation.mockResolvedValue(detail("conversation-running", "notebook-a", {
    activeJob: {
      job_id: "job-running",
      question: "running question",
      asked_at: "2026-08-22T00:00:00Z",
      mode: "reasoning",
      trace: [],
    },
  }));
  const view = render(<Harness />);
  const owner = beginOwnedNotebook();
  await act(async () => {
    await value!.restoreNotebook(owner);
    value!.finishNotebookTransition(owner);
  });

  act(() => vi.advanceTimersByTime(1500));
  expect(api.getAskJob).toHaveBeenCalledTimes(1);
  act(() => vi.advanceTimersByTime(15_000));
  expect(api.getAskJob).toHaveBeenCalledTimes(1);

  firstPoll.resolve(runningJob("job-running"));
  await act(async () => {
    await Promise.resolve();
  });
  act(() => vi.advanceTimersByTime(1499));
  expect(api.getAskJob).toHaveBeenCalledTimes(1);
  act(() => vi.advanceTimersByTime(1));
  expect(api.getAskJob).toHaveBeenCalledTimes(2);

  view.unmount();
  secondPoll.resolve(runningJob("job-running"));
  await act(async () => {
    await Promise.resolve();
  });
});

test("an old reconnect terminal cannot clear a replacement active job from refreshed detail", async () => {
  vi.useFakeTimers();
  const replacementPoll = deferred<AskJobDetail>();
  api.getAskJob
    .mockResolvedValueOnce(doneJob("job-old"))
    .mockReturnValueOnce(replacementPoll.promise);
  api.listConversations.mockResolvedValue([summary("conversation-running")]);
  api.getConversation
    .mockResolvedValueOnce(detail("conversation-running", "notebook-a", {
      activeJob: {
        job_id: "job-old",
        question: "old question",
        asked_at: "2026-08-22T00:00:00Z",
        mode: "reasoning",
        trace: [],
      },
    }))
    .mockResolvedValueOnce(detail("conversation-running", "notebook-a", {
      activeJob: {
        job_id: "job-replacement",
        question: "replacement question",
        asked_at: "2026-08-22T00:01:00Z",
        mode: "reasoning",
        trace: [{ step_type: "intent", summary: "replacement trace", detail: {} }],
      },
    }));
  const view = render(<Harness />);
  const owner = beginOwnedNotebook();
  await act(async () => {
    await value!.restoreNotebook(owner);
    value!.finishNotebookTransition(owner);
  });

  await act(async () => {
    await vi.advanceTimersByTimeAsync(1500);
  });
  expect(api.getAskJob).toHaveBeenNthCalledWith(1, "notebook-a", "job-old");
  expect(api.getConversation).toHaveBeenCalledTimes(2);
  expect(value!.asking).toBe(true);
  expect(value!.pendingQuestion).toBe("replacement question");
  expect(value!.pendingTrace.map((step) => step.summary)).toEqual(["replacement trace"]);

  act(() => vi.advanceTimersByTime(1500));
  expect(api.getAskJob).toHaveBeenCalledTimes(2);
  expect(api.getAskJob).toHaveBeenNthCalledWith(2, "notebook-a", "job-replacement");
  expect(value!.asking).toBe(true);

  view.unmount();
  replacementPoll.resolve(runningJob("job-replacement"));
  await act(async () => {
    await Promise.resolve();
  });
});

test("a reconnect poll hanging past the cap releases UI and ignores its late terminal result", async () => {
  vi.useFakeTimers();
  const hangingPoll = deferred<AskJobDetail>();
  api.getAskJob.mockReturnValueOnce(hangingPoll.promise);
  api.listConversations.mockResolvedValue([summary("conversation-hanging")]);
  api.getConversation
    .mockResolvedValueOnce(detail("conversation-hanging", "notebook-a", {
      activeJob: {
        job_id: "job-hanging",
        question: "hanging question",
        asked_at: "2026-08-22T00:00:00Z",
        mode: "reasoning",
        trace: [],
      },
    }))
    .mockResolvedValueOnce(detail("conversation-hanging", "notebook-a"));
  render(<Harness />);
  const owner = beginOwnedNotebook();
  await act(async () => {
    await value!.restoreNotebook(owner);
    value!.finishNotebookTransition(owner);
  });

  act(() => vi.advanceTimersByTime(1500));
  expect(api.getAskJob).toHaveBeenCalledTimes(1);
  expect(value!.asking).toBe(true);
  act(() => vi.advanceTimersByTime(20 * 60 * 1000 - 1500));
  expect(value!.asking).toBe(false);
  expect(value!.pendingQuestion).toBe("");
  expect(value!.pendingTrace).toEqual([]);
  expect(effects.notify).toHaveBeenCalledWith("该问答仍在后台进行，请稍后重开查看");

  hangingPoll.resolve(doneJob("job-hanging"));
  await act(async () => {
    await Promise.resolve();
  });
  expect(api.getConversation).toHaveBeenCalledTimes(1);
  expect(api.listConversations).toHaveBeenCalledTimes(1);
  expect(api.getAskJob).toHaveBeenCalledTimes(1);
  expect(value!.asking).toBe(false);
  expect(value!.pendingQuestion).toBe("");
});

test("intent confirmation reuses the exact preview scope snapshot", async () => {
  const contract: QueryIntentContract = {
    objective: "compare",
    resolved_question: "resolved question",
    intent_type: "comparison",
    result_scope: "ranked",
    completeness_required: false,
    entities: [],
    mandatory_topics: [],
    comparison_axes: [],
    constraints: [],
    excluded_topics: [],
    expected_output: "answer",
    assumptions: [],
    ambiguities: [{ id: "which", question: "Which one?", required: true }],
    confidence: 0.5,
    needs_clarification: true,
    confirmed: false,
  };
  const originalPolicy: AskPolicy = {
    ...DEFAULT_POLICY,
    sourceScope: { mode: "include", source_ids: ["source-old"] },
    baseScope: { mode: "include", notebook_ids: ["base-old"] },
  };
  const changedPolicy: AskPolicy = {
    ...DEFAULT_POLICY,
    sourceScope: { mode: "include", source_ids: ["source-new"] },
    baseScope: { mode: "include", notebook_ids: ["base-new"] },
  };
  api.previewAskIntent.mockResolvedValue(contract);
  api.runAskStream.mockResolvedValue(answer("conversation-intent"));
  const view = render(<Harness policy={originalPolicy} />);
  beginOwnedNotebook();
  act(() => value!.selectMode("reasoning"));

  await act(async () => {
    await value!.submit("ambiguous question");
  });
  expect(value!.intentReview?.contract).toBe(contract);
  expect(api.previewAskIntent).toHaveBeenCalledTimes(1);
  expect(api.previewAskIntent).toHaveBeenCalledWith(
    "notebook-a",
    "ambiguous question",
    null,
    expect.any(AbortSignal),
    originalPolicy.sourceScope,
    originalPolicy.baseScope,
    expect.any(Function),
  );

  view.rerender(<Harness policy={changedPolicy} />);
  const confirmation: AskIntentConfirmation = {
    contract,
    resolved_question: "resolved question",
    answers: [{ id: "which", answer: "the old scope" }],
  };
  await act(async () => {
    await value!.confirmIntent(confirmation);
  });

  expect(api.previewAskIntent).toHaveBeenCalledTimes(1);
  expect(api.runAskStream).toHaveBeenCalledTimes(1);
  const payload = api.runAskStream.mock.calls[0]?.[1] as {
    source_scope: AskPolicy["sourceScope"];
    base_scope: AskPolicy["baseScope"];
  };
  expect(payload.source_scope).toEqual(originalPolicy.sourceScope);
  expect(payload.base_scope).toEqual(originalPolicy.baseScope);
});

test.each(["preview", "review"] as const)(
  "switching from advanced to automatic mode clears an intent %s and restores the draft",
  async (phase) => {
    const contract: QueryIntentContract = {
      objective: "ambiguous",
      resolved_question: "ambiguous",
      intent_type: "other",
      result_scope: "ranked",
      completeness_required: false,
      entities: [],
      mandatory_topics: [],
      comparison_axes: [],
      constraints: [],
      excluded_topics: [],
      expected_output: "answer",
      assumptions: [],
      ambiguities: [{ id: "which", question: "Which one?", required: true }],
      confidence: 0.5,
      needs_clarification: true,
      confirmed: false,
    };
    const preview = deferred<QueryIntentContract>();
    api.previewAskIntent.mockReturnValue(preview.promise);
    const view = render(<Harness />);
    beginOwnedNotebook();
    act(() => value!.selectMode("reasoning"));

    let submission!: Promise<void>;
    act(() => {
      submission = value!.submit("draft question");
    });
    if (phase === "review") {
      preview.resolve(contract);
      await act(async () => submission);
      expect(value!.intentReview).not.toBeNull();
    } else {
      expect(value!.intentChecking).toBe(true);
    }

    view.rerender(<Harness policy={{ ...DEFAULT_POLICY, advanced: false }} />);
    expect(value!.intentChecking).toBe(false);
    expect(value!.intentReview).toBeNull();
    expect(value!.pendingQuestion).toBe("");
    expect(value!.question).toBe("draft question");

    if (phase === "preview") {
      preview.resolve(contract);
      await act(async () => submission);
    }
    expect(api.runAskStream).not.toHaveBeenCalled();
  },
);

test("an old confirmed stream cannot idle a newer same-notebook clarification flow", async () => {
  const ambiguous = (label: string): QueryIntentContract => ({
    objective: label,
    resolved_question: `${label} resolved`,
    intent_type: "comparison",
    result_scope: "ranked",
    completeness_required: false,
    entities: [],
    mandatory_topics: [],
    comparison_axes: [],
    constraints: [],
    excluded_topics: [],
    expected_output: "answer",
    assumptions: [],
    ambiguities: [{ id: `${label}-choice`, question: "Which one?", required: true }],
    confidence: 0.5,
    needs_clarification: true,
    confirmed: false,
  });
  const contractA = ambiguous("A");
  const contractB = ambiguous("B");
  const streamA = deferred<AskResponse>();
  api.previewAskIntent
    .mockResolvedValueOnce(contractA)
    .mockResolvedValueOnce(contractB);
  api.runAskStream
    .mockReturnValueOnce(streamA.promise)
    .mockResolvedValueOnce(answer("conversation-b", "answer-b-new"));
  api.listConversations.mockResolvedValue([
    summary("conversation-a"),
    summary("conversation-b"),
  ]);
  api.getConversation.mockImplementation(async (id: string) => {
    const conversation = detail(id);
    conversation.turns[0].response.mode = "reasoning";
    return conversation;
  });
  render(<Harness />);
  const owner = beginOwnedNotebook();
  await act(async () => {
    await value!.restoreNotebook(owner);
  });
  expect(value!.conversationId).toBe("conversation-a");
  expect(value!.mode).toBe("reasoning");

  await act(async () => {
    await value!.submit("question A");
  });
  const reviewA = value!.intentReview!;
  expect(reviewA.contract).toBe(contractA);
  let confirmingA!: Promise<void>;
  act(() => {
    confirmingA = value!.confirmIntent({
      contract: contractA,
      resolved_question: contractA.resolved_question,
      answers: [{ id: "A-choice", answer: "A answer" }],
    });
  });
  expect(api.runAskStream).toHaveBeenCalledTimes(1);

  await act(async () => {
    await value!.openSession("conversation-b", 2);
  });
  await act(async () => {
    await value!.submit("question B");
  });
  expect(value!.intentReview?.contract).toBe(contractB);
  expect(value!.intentReview?.question).toBe("question B");

  streamA.resolve(answer("conversation-a", "answer-a-old"));
  await act(async () => confirmingA);
  expect(value!.intentReview?.contract).toBe(contractB);
  expect(value!.intentReview?.question).toBe("question B");

  await act(async () => {
    await value!.confirmIntent({
      contract: contractB,
      resolved_question: contractB.resolved_question,
      answers: [{ id: "B-choice", answer: "B answer" }],
    });
  });
  expect(api.previewAskIntent).toHaveBeenCalledTimes(2);
  expect(api.runAskStream).toHaveBeenCalledTimes(2);
  expect(api.runAskStream.mock.calls[1]?.[1]).toMatchObject({
    question: "question B",
    conversation_id: "conversation-b",
  });
  expect(reviewA.question).toBe("question A");
});

test.each(["delete", "bulk"] as const)(
  "%s response survives notebook A/G1 -> B -> A/G3 and tombstones a stale current row once",
  async (operation) => {
    const deletion = deferred<undefined>();
    const cleanupResult = deferred<{ deleted: number; deleted_ids: string[] }>();
    api.deleteConversation.mockReturnValueOnce(deletion.promise);
    api.bulkDeleteConversations.mockReturnValueOnce(cleanupResult.promise);
    api.listConversations.mockImplementation(async (notebookId: string) => (
      notebookId === "notebook-a"
        ? [summary("conversation-a"), summary("conversation-a-keep")]
        : []
    ));
    api.getConversation.mockImplementation(async (id: string) => detail(id, "notebook-a"));
    const view = render(<Harness />);

    let ownerA1: ReturnType<HookValue["beginNotebookTransition"]> = null;
    act(() => {
      ownerA1 = value!.beginNotebookTransition({
        actorId: "user-a",
        notebookId: "notebook-a",
        workspaceEpoch: 1,
      });
    });
    await act(async () => {
      await value!.restoreNotebook(ownerA1!);
      value!.finishNotebookTransition(ownerA1!);
    });
    expect(value!.conversationId).toBe("conversation-a");

    let mutating!: Promise<void>;
    act(() => {
      mutating = operation === "delete"
        ? value!.deleteSession("conversation-a")
        : value!.bulkCleanup(7);
    });

    let ownerB: ReturnType<HookValue["beginNotebookTransition"]> = null;
    act(() => {
      ownerB = value!.beginNotebookTransition({
        actorId: "user-a",
        notebookId: "notebook-b",
        workspaceEpoch: 2,
      });
    });
    view.rerender(<Harness notebookId="notebook-b" />);
    act(() => value!.finishNotebookTransition(ownerB!));

    let ownerA3: ReturnType<HookValue["beginNotebookTransition"]> = null;
    act(() => {
      ownerA3 = value!.beginNotebookTransition({
        actorId: "user-a",
        notebookId: "notebook-a",
        workspaceEpoch: 3,
      });
    });
    view.rerender(<Harness notebookId="notebook-a" />);
    await act(async () => {
      await value!.restoreNotebook(ownerA3!);
      value!.finishNotebookTransition(ownerA3!);
    });
    expect(value!.conversationId).toBe("conversation-a");
    expect(value!.sessions.map((item) => item.id)).toContain("conversation-a");

    if (operation === "delete") deletion.resolve(undefined);
    else cleanupResult.resolve({ deleted: 1, deleted_ids: ["conversation-a"] });
    await act(async () => mutating);

    expect(value!.conversationId).toBeNull();
    expect(value!.turns).toEqual([]);
    expect(value!.sessions.map((item) => item.id)).toEqual(["conversation-a-keep"]);
    expect(api.listConversations).toHaveBeenCalledTimes(3);
    if (operation === "delete") {
      expect(api.deleteConversation).toHaveBeenCalledTimes(1);
      expect(api.deleteConversation).toHaveBeenCalledWith("conversation-a");
      expect(api.bulkDeleteConversations).not.toHaveBeenCalled();
    } else {
      expect(api.bulkDeleteConversations).toHaveBeenCalledTimes(1);
      expect(api.bulkDeleteConversations).toHaveBeenCalledWith("notebook-a", 7);
      expect(api.deleteConversation).not.toHaveBeenCalled();
    }
  },
);

test.each(["delete", "bulk"] as const)(
  "%s failure from notebook A/G1 is silent after B -> A/G3 replaces its view owner",
  async (operation) => {
    const deletion = deferred<undefined>();
    const cleanupResult = deferred<{ deleted: number; deleted_ids: string[] }>();
    api.deleteConversation.mockReturnValueOnce(deletion.promise);
    api.bulkDeleteConversations.mockReturnValueOnce(cleanupResult.promise);
    const view = render(<Harness />);
    const ownerA1 = beginOwnedNotebook(1);
    let mutating!: Promise<void>;
    act(() => {
      mutating = operation === "delete"
        ? value!.deleteSession("conversation-old")
        : value!.bulkCleanup(7);
    });

    let ownerB: ReturnType<HookValue["beginNotebookTransition"]> = null;
    act(() => {
      ownerB = value!.beginNotebookTransition({
        actorId: "user-a",
        notebookId: "notebook-b",
        workspaceEpoch: 2,
      });
    });
    view.rerender(<Harness notebookId="notebook-b" />);
    act(() => value!.finishNotebookTransition(ownerB!));

    let ownerA3: ReturnType<HookValue["beginNotebookTransition"]> = null;
    act(() => {
      ownerA3 = value!.beginNotebookTransition({
        actorId: "user-a",
        notebookId: "notebook-a",
        workspaceEpoch: 3,
      });
    });
    view.rerender(<Harness notebookId="notebook-a" />);
    act(() => value!.finishNotebookTransition(ownerA3!));
    expect(ownerA3!.notebookGeneration).not.toBe(ownerA1.notebookGeneration);

    const staleError = new Error("stale mutation failed");
    if (operation === "delete") deletion.reject(staleError);
    else cleanupResult.reject(staleError);
    await act(async () => {
      await expect(mutating).resolves.toBeUndefined();
    });
    expect(effects.reportError).not.toHaveBeenCalled();
  },
);

test("actor replacement drops delayed restore work and disables commands from the old owner", async () => {
  const lateDetail = deferred<ConversationDetail>();
  const lateList = deferred<ConversationSummary[]>();
  api.listConversations
    .mockResolvedValueOnce([summary("conversation-a-late-detail")])
    .mockReturnValueOnce(lateList.promise)
    .mockResolvedValueOnce([summary("conversation-b-current")]);
  api.getConversation
    .mockReturnValueOnce(lateDetail.promise)
    .mockResolvedValueOnce(detail("conversation-b-current"));
  const view = render(<Harness />);
  let ownerA: ReturnType<HookValue["beginNotebookTransition"]> = null;
  act(() => {
    ownerA = value!.beginNotebookTransition({
      actorId: "user-a",
      notebookId: "notebook-a",
      workspaceEpoch: 1,
    });
  });
  const oldCommands = value!;
  let restoringA!: Promise<boolean>;
  act(() => {
    restoringA = oldCommands.restoreNotebook(ownerA!);
  });
  await act(async () => {
    await Promise.resolve();
  });
  expect(api.getConversation).toHaveBeenCalledTimes(1);
  let refreshingA!: Promise<ConversationSummary[] | null>;
  act(() => {
    refreshingA = oldCommands.refreshSessions();
  });
  expect(api.listConversations).toHaveBeenCalledTimes(2);

  view.rerender(<Harness actorId="user-b" />);
  const businessCounts = {
    lists: api.listConversations.mock.calls.length,
    details: api.getConversation.mock.calls.length,
  };
  await act(async () => {
    expect(await oldCommands.restoreNotebook(ownerA!)).toBe(false);
    expect(await oldCommands.openSession("conversation-a-old", 99)).toBe(false);
    await oldCommands.deleteSession("conversation-a-old");
    await oldCommands.bulkCleanup(7);
    await oldCommands.submitFeedback("answer-a-old", "useful");
    await oldCommands.submit("old question");
  });
  expect(api.listConversations).toHaveBeenCalledTimes(businessCounts.lists);
  expect(api.getConversation).toHaveBeenCalledTimes(businessCounts.details);
  expect(api.deleteConversation).not.toHaveBeenCalled();
  expect(api.bulkDeleteConversations).not.toHaveBeenCalled();
  expect(api.submitFeedback).not.toHaveBeenCalled();
  expect(api.runAskStream).not.toHaveBeenCalled();
  expect(api.previewAskIntent).not.toHaveBeenCalled();

  let ownerB: ReturnType<HookValue["beginNotebookTransition"]> = null;
  act(() => {
    ownerB = value!.beginNotebookTransition({
      actorId: "user-b",
      notebookId: "notebook-a",
      workspaceEpoch: 2,
    });
  });
  await act(async () => {
    await value!.restoreNotebook(ownerB!);
    value!.finishNotebookTransition(ownerB!);
  });
  expect(value!.conversationId).toBe("conversation-b-current");
  expect(value!.sessions.map((item) => item.id)).toEqual(["conversation-b-current"]);
  const visibleEffects = vi.mocked(effects.ensureAskVisible).mock.calls.length;

  lateDetail.resolve(detail("conversation-a-late-detail"));
  lateList.resolve([summary("conversation-a-late-list")]);
  await act(async () => {
    await restoringA;
    await refreshingA;
  });
  expect(value!.conversationId).toBe("conversation-b-current");
  expect(value!.turns[0]?.response.conversation_id).toBe("conversation-b-current");
  expect(value!.sessions.map((item) => item.id)).toEqual(["conversation-b-current"]);
  expect(effects.ensureAskVisible).toHaveBeenCalledTimes(visibleEffects);
});

test("durable A/G1 stream re-attaches to the returning A/G3 view after notebook A -> B -> A", async () => {
  const stream = deferred<AskResponse>();
  let signal: AbortSignal | undefined;
  let onStart: ((jobId: string, conversationId: string) => void | Promise<void>) | undefined;
  api.runAskStream.mockImplementation((
    _notebookId: string,
    _payload: unknown,
    _onProgress: unknown,
    nextSignal?: AbortSignal,
    nextOnStart?: (jobId: string, conversationId: string) => void | Promise<void>,
  ) => {
    signal = nextSignal;
    onStart = nextOnStart;
    return stream.promise;
  });
  api.listConversations
    .mockResolvedValueOnce([summary("conversation-g3")])
    .mockResolvedValueOnce([
      summary("conversation-durable"),
      summary("conversation-g3"),
    ])
    .mockResolvedValueOnce([
      summary("conversation-durable"),
      summary("conversation-g3"),
    ]);
  api.getConversation.mockResolvedValueOnce(detail("conversation-g3"));
  const view = render(<Harness />);
  let ownerA1: ReturnType<HookValue["beginNotebookTransition"]> = null;
  act(() => {
    ownerA1 = value!.beginNotebookTransition({
      actorId: "user-a",
      notebookId: "notebook-a",
      workspaceEpoch: 1,
    });
    value!.finishNotebookTransition(ownerA1!);
  });
  let submitting!: Promise<void>;
  act(() => {
    submitting = value!.submit("durable G1 question");
  });

  let ownerB2: ReturnType<HookValue["beginNotebookTransition"]> = null;
  act(() => {
    ownerB2 = value!.beginNotebookTransition({
      actorId: "user-a",
      notebookId: "notebook-b",
      workspaceEpoch: 2,
    });
  });
  view.rerender(<Harness notebookId="notebook-b" />);
  act(() => value!.finishNotebookTransition(ownerB2!));

  let ownerA3: ReturnType<HookValue["beginNotebookTransition"]> = null;
  act(() => {
    ownerA3 = value!.beginNotebookTransition({
      actorId: "user-a",
      notebookId: "notebook-a",
      workspaceEpoch: 3,
    });
  });
  view.rerender(<Harness notebookId="notebook-a" />);
  await act(async () => {
    await value!.restoreNotebook(ownerA3!);
    value!.finishNotebookTransition(ownerA3!);
  });
  // The G1 run has no durable conversation yet, so the G3 restore neither opens
  // the previous latest session over it nor drops it: the pending turn comes back.
  expect(api.getConversation).not.toHaveBeenCalled();
  expect(value!.conversationId).toBeNull();
  expect(value!.turns).toEqual([]);
  expect(value!.asking).toBe(true);
  expect(value!.pendingQuestion).toBe("durable G1 question");

  await act(async () => {
    await onStart!("job-durable", "conversation-durable");
  });
  expect(signal?.aborted).toBe(false);
  expect(api.cancelAskJob).not.toHaveBeenCalled();
  expect(value!.sessions.map((item) => item.id)).toEqual([
    "conversation-durable",
    "conversation-g3",
  ]);
  expect(value!.conversationId).toBe("conversation-durable");

  stream.resolve(answer("conversation-durable"));
  await act(async () => submitting);
  expect(signal?.aborted).toBe(false);
  expect(api.cancelAskJob).not.toHaveBeenCalled();
  expect(api.listConversations).toHaveBeenCalledTimes(3);
  expect(api.listConversations.mock.calls).toEqual([
    ["notebook-a"],
    ["notebook-a"],
    ["notebook-a"],
  ]);
  expect(value!.sessions.map((item) => item.id)).toEqual([
    "conversation-durable",
    "conversation-g3",
  ]);
  expect(value!.asking).toBe(false);
  expect(value!.conversationId).toBe("conversation-durable");
  expect(value!.turns.map((turn) => turn.question)).toEqual(["durable G1 question"]);
  expect(value!.turns[0]?.response.answer_id).toBe("answer-conversation-durable");
});

test("a new-session run detached before started is re-attached on notebook return", async () => {
  const stream = deferred<AskResponse>();
  let signal: AbortSignal | undefined;
  let onProgress: ((step: ReasoningTraceStep) => void | Promise<void>) | undefined;
  let onStart: ((jobId: string, conversationId: string) => void | Promise<void>) | undefined;
  api.runAskStream.mockImplementation((
    _notebookId: string,
    _payload: unknown,
    nextOnProgress: (step: ReasoningTraceStep) => void | Promise<void>,
    nextSignal?: AbortSignal,
    nextOnStart?: (jobId: string, conversationId: string) => void | Promise<void>,
  ) => {
    onProgress = nextOnProgress;
    signal = nextSignal;
    onStart = nextOnStart;
    return stream.promise;
  });
  api.listConversations.mockResolvedValue([summary("conversation-older")]);
  api.getConversation.mockResolvedValue(detail("conversation-older"));
  render(<Harness />);
  beginOwnedNotebook();

  let submitting!: Promise<void>;
  act(() => {
    submitting = value!.submit("detached question");
  });
  await act(async () => {
    await onProgress!({ step_type: "intent", summary: "selecting engine", detail: {} });
  });
  act(() => value!.leaveWorkspace());
  expect(signal?.aborted).toBe(false);

  const owner = beginOwnedNotebook(2);
  await act(async () => {
    await value!.restoreNotebook(owner);
    value!.finishNotebookTransition(owner);
  });
  // The previous latest session is not opened over the unfinished question.
  expect(api.getConversation).not.toHaveBeenCalled();
  expect(value!.conversationId).toBeNull();
  expect(value!.turns).toEqual([]);
  expect(value!.asking).toBe(true);
  expect(value!.pendingQuestion).toBe("detached question");
  expect(value!.pendingTrace.map((step) => step.summary)).toEqual(["selecting engine"]);

  api.listConversations.mockResolvedValue([
    summary("conversation-detached"),
    summary("conversation-older"),
  ]);
  await act(async () => {
    await onStart!("job-detached", "conversation-detached");
  });
  expect(value!.conversationId).toBe("conversation-detached");
  expect(value!.sessions[0]?.id).toBe("conversation-detached");

  stream.resolve(answer("conversation-detached"));
  await act(async () => submitting);
  expect(signal?.aborted).toBe(false);
  expect(api.cancelAskJob).not.toHaveBeenCalled();
  expect(value!.asking).toBe(false);
  expect(value!.pendingQuestion).toBe("");
  expect(value!.conversationId).toBe("conversation-detached");
  expect(value!.turns.map((turn) => turn.question)).toEqual(["detached question"]);
  expect(value!.turns[0]?.response.answer_id).toBe("answer-conversation-detached");
});

test("a follow-up detached before started reopens its own conversation, not the newest one", async () => {
  const stream = deferred<AskResponse>();
  let onStart: ((jobId: string, conversationId: string) => void | Promise<void>) | undefined;
  api.runAskStream.mockImplementation((
    _notebookId: string,
    _payload: unknown,
    _onProgress: unknown,
    _signal?: AbortSignal,
    nextOnStart?: (jobId: string, conversationId: string) => void | Promise<void>,
  ) => {
    onStart = nextOnStart;
    return stream.promise;
  });
  api.listConversations.mockResolvedValue([summary("conversation-x"), summary("conversation-y")]);
  api.getConversation.mockResolvedValue(detail("conversation-x"));
  render(<Harness />);
  const first = beginOwnedNotebook();
  await act(async () => {
    await value!.restoreNotebook(first);
  });
  expect(value!.conversationId).toBe("conversation-x");

  let submitting!: Promise<void>;
  act(() => {
    submitting = value!.submit("follow-up question");
  });
  act(() => value!.leaveWorkspace());

  // Another session became the newest while the follow-up was still routing.
  api.listConversations.mockResolvedValue([summary("conversation-y"), summary("conversation-x")]);
  const owner = beginOwnedNotebook(2);
  await act(async () => {
    await value!.restoreNotebook(owner);
    value!.finishNotebookTransition(owner);
  });
  expect(api.getConversation).toHaveBeenCalledTimes(2);
  expect(api.getConversation).toHaveBeenLastCalledWith("conversation-x");
  expect(value!.conversationId).toBe("conversation-x");
  expect(value!.turns).toHaveLength(1);
  expect(value!.asking).toBe(true);
  expect(value!.pendingQuestion).toBe("follow-up question");

  await act(async () => {
    await onStart!("job-follow-up", "conversation-x");
  });
  stream.resolve(answer("conversation-x", "answer-follow-up"));
  await act(async () => submitting);
  expect(value!.asking).toBe(false);
  expect(value!.conversationId).toBe("conversation-x");
  expect(value!.turns.map((turn) => turn.question)).toEqual([
    "question-conversation-x",
    "follow-up question",
  ]);
  expect(value!.turns[1]?.response.answer_id).toBe("answer-follow-up");
});

test("a run detached after started re-attaches its live stream instead of polling the job", async () => {
  vi.useFakeTimers();
  const stream = deferred<AskResponse>();
  let onStart: ((jobId: string, conversationId: string) => void | Promise<void>) | undefined;
  api.runAskStream.mockImplementation((
    _notebookId: string,
    _payload: unknown,
    _onProgress: unknown,
    _signal?: AbortSignal,
    nextOnStart?: (jobId: string, conversationId: string) => void | Promise<void>,
  ) => {
    onStart = nextOnStart;
    return stream.promise;
  });
  api.listConversations.mockResolvedValue([summary("conversation-durable")]);
  api.getConversation.mockResolvedValue({
    ...detail("conversation-durable"),
    turn_count: 0,
    turns: [],
    active_job: {
      job_id: "job-durable",
      question: "durable question",
      asked_at: "2026-08-22T00:00:00Z",
      mode: "chunk",
      trace: [],
    },
  });
  render(<Harness />);
  beginOwnedNotebook();

  let submitting!: Promise<void>;
  act(() => {
    submitting = value!.submit("durable question");
  });
  await act(async () => {
    await onStart!("job-durable", "conversation-durable");
  });
  act(() => value!.leaveWorkspace());

  const owner = beginOwnedNotebook(2);
  await act(async () => {
    await value!.restoreNotebook(owner);
    value!.finishNotebookTransition(owner);
  });
  expect(api.getConversation).toHaveBeenCalledWith("conversation-durable");
  expect(value!.conversationId).toBe("conversation-durable");
  expect(value!.asking).toBe(true);
  expect(value!.pendingQuestion).toBe("durable question");

  act(() => vi.advanceTimersByTime(1500));
  expect(api.getAskJob).not.toHaveBeenCalled();

  stream.resolve(answer("conversation-durable"));
  await act(async () => submitting);
  expect(api.getAskJob).not.toHaveBeenCalled();
  expect(value!.asking).toBe(false);
  expect(value!.turns.map((turn) => turn.question)).toEqual(["durable question"]);
});

test("a run stopped before started is never re-attached by a later restore", async () => {
  const stream = deferred<AskResponse>();
  let signal: AbortSignal | undefined;
  let onStart: ((jobId: string, conversationId: string) => void | Promise<void>) | undefined;
  api.runAskStream.mockImplementation((
    _notebookId: string,
    _payload: unknown,
    _onProgress: unknown,
    nextSignal?: AbortSignal,
    nextOnStart?: (jobId: string, conversationId: string) => void | Promise<void>,
  ) => {
    signal = nextSignal;
    onStart = nextOnStart;
    return stream.promise;
  });
  api.listConversations.mockResolvedValue([summary("conversation-older")]);
  api.getConversation.mockResolvedValue(detail("conversation-older"));
  render(<Harness />);
  beginOwnedNotebook();

  let submitting!: Promise<void>;
  act(() => {
    submitting = value!.submit("stopped question");
  });
  act(() => value!.abort());
  expect(value!.asking).toBe(false);
  act(() => value!.leaveWorkspace());

  const owner = beginOwnedNotebook(2);
  await act(async () => {
    await value!.restoreNotebook(owner);
    value!.finishNotebookTransition(owner);
  });
  expect(value!.asking).toBe(false);
  expect(value!.pendingQuestion).toBe("");
  expect(value!.conversationId).toBe("conversation-older");

  await act(async () => {
    await onStart!("job-stopped", "conversation-stopped");
  });
  expect(api.cancelAskJob).toHaveBeenCalledTimes(1);
  expect(signal?.aborted).toBe(true);
  stream.reject(new DOMException("aborted", "AbortError"));
  await act(async () => submitting);
  expect(value!.asking).toBe(false);
  expect(value!.conversationId).toBe("conversation-older");
  expect(value!.turns.map((turn) => turn.question)).toEqual(["question-conversation-older"]);
});

test("leaving during a reasoning intent preview keeps it running and re-attaches it on return", async () => {
  const preview = deferred<QueryIntentContract>();
  api.previewAskIntent.mockReturnValue(preview.promise);
  api.runAskStream.mockResolvedValue(answer("conversation-intent"));
  api.listConversations.mockResolvedValue([summary("conversation-older")]);
  api.getConversation.mockResolvedValue(detail("conversation-older"));
  render(<Harness />);
  beginOwnedNotebook();
  act(() => value!.selectMode("reasoning"));

  let submitting!: Promise<void>;
  act(() => {
    submitting = value!.submit("clear question");
  });
  expect(value!.intentChecking).toBe(true);
  act(() => value!.leaveWorkspace());
  const signal = api.previewAskIntent.mock.calls[0]?.[3] as AbortSignal;
  expect(signal.aborted).toBe(false);

  const owner = beginOwnedNotebook(2);
  await act(async () => {
    await value!.restoreNotebook(owner);
    value!.finishNotebookTransition(owner);
  });
  // The preview is reasoning context in its own right: no older session is
  // opened over it, and the mode projection does not cancel it.
  expect(api.getConversation).not.toHaveBeenCalled();
  expect(value!.intentChecking).toBe(true);
  expect(value!.pendingQuestion).toBe("clear question");
  expect(value!.mode).toBe("reasoning");
  expect(value!.conversationId).toBeNull();
  expect(signal.aborted).toBe(false);

  preview.resolve(contractFor("clear question", false));
  await act(async () => submitting);
  expect(api.runAskStream).toHaveBeenCalledTimes(1);
  expect(api.runAskStream.mock.calls[0]?.[1]).toMatchObject({
    question: "clear question",
    mode: "reasoning",
  });
  expect(value!.intentChecking).toBe(false);
  expect(value!.asking).toBe(false);
  expect(value!.conversationId).toBe("conversation-intent");
  expect(value!.turns.map((turn) => turn.question)).toEqual(["clear question"]);
});

test("an intent preview that completes while away starts the durable Ask and re-attaches it on return", async () => {
  const preview = deferred<QueryIntentContract>();
  const stream = deferred<AskResponse>();
  let onStart: ((jobId: string, conversationId: string) => void | Promise<void>) | undefined;
  api.previewAskIntent.mockReturnValue(preview.promise);
  api.runAskStream.mockImplementation((
    _notebookId: string,
    _payload: unknown,
    _onProgress: unknown,
    _signal?: AbortSignal,
    nextOnStart?: (jobId: string, conversationId: string) => void | Promise<void>,
  ) => {
    onStart = nextOnStart;
    return stream.promise;
  });
  api.listConversations.mockResolvedValue([summary("conversation-older")]);
  api.getConversation.mockResolvedValue(detail("conversation-older"));
  render(<Harness />);
  beginOwnedNotebook();
  act(() => value!.selectMode("reasoning"));

  let submitting!: Promise<void>;
  act(() => {
    submitting = value!.submit("away question");
  });
  act(() => value!.leaveWorkspace());

  preview.resolve(contractFor("away question", false));
  await act(async () => {
    await preview.promise;
    await Promise.resolve();
    await Promise.resolve();
  });
  // Off-screen completion still starts the durable job with the frozen intent.
  expect(api.runAskStream).toHaveBeenCalledTimes(1);
  expect(api.runAskStream.mock.calls[0]?.[1]).toMatchObject({
    question: "away question",
    mode: "reasoning",
    intent: expect.objectContaining({ resolved_question: "away question" }),
  });

  const owner = beginOwnedNotebook(2);
  await act(async () => {
    await value!.restoreNotebook(owner);
    value!.finishNotebookTransition(owner);
  });
  expect(api.getConversation).not.toHaveBeenCalled();
  expect(value!.intentChecking).toBe(false);
  expect(value!.asking).toBe(true);
  expect(value!.pendingQuestion).toBe("away question");

  await act(async () => {
    await onStart!("job-away", "conversation-away");
  });
  expect(value!.conversationId).toBe("conversation-away");
  stream.resolve(answer("conversation-away"));
  await act(async () => submitting);
  expect(value!.asking).toBe(false);
  expect(value!.turns.map((turn) => turn.question)).toEqual(["away question"]);
});

test("a clarification requested while away re-opens on return and confirms with the frozen scope", async () => {
  const preview = deferred<QueryIntentContract>();
  api.previewAskIntent.mockReturnValue(preview.promise);
  api.runAskStream.mockResolvedValue(answer("conversation-review"));
  const scopedPolicy: AskPolicy = {
    ...DEFAULT_POLICY,
    sourceScope: { mode: "include", source_ids: ["source-old"] },
    baseScope: { mode: "include", notebook_ids: ["base-old"] },
  };
  const view = render(<Harness policy={scopedPolicy} />);
  beginOwnedNotebook();
  act(() => value!.selectMode("reasoning"));

  let submitting!: Promise<void>;
  act(() => {
    submitting = value!.submit("ambiguous away");
  });
  act(() => value!.leaveWorkspace());
  const contract = contractFor("ambiguous away", true);
  preview.resolve(contract);
  await act(async () => submitting);
  expect(api.runAskStream).not.toHaveBeenCalled();
  expect(effects.notify).not.toHaveBeenCalledWith("问题存在会改变检索方向的歧义，请先补充确认");

  view.rerender(<Harness policy={DEFAULT_POLICY} />);
  const owner = beginOwnedNotebook(2);
  await act(async () => {
    await value!.restoreNotebook(owner);
    value!.finishNotebookTransition(owner);
  });
  expect(value!.intentReview?.contract).toBe(contract);
  expect(value!.intentReview?.question).toBe("ambiguous away");
  expect(value!.pendingQuestion).toBe("ambiguous away");
  expect(value!.intentChecking).toBe(false);
  expect(value!.mode).toBe("reasoning");
  expect(effects.notify).toHaveBeenCalledWith("问题存在会改变检索方向的歧义，请先补充确认");

  await act(async () => {
    await value!.confirmIntent({
      contract,
      resolved_question: "ambiguous away resolved",
      answers: [{ id: "which", answer: "that one" }],
    });
  });
  expect(api.runAskStream).toHaveBeenCalledTimes(1);
  expect(api.runAskStream.mock.calls[0]?.[1]).toMatchObject({
    question: "ambiguous away",
    mode: "reasoning",
    source_scope: scopedPolicy.sourceScope,
    base_scope: scopedPolicy.baseScope,
  });
  expect(value!.intentReview).toBeNull();
  expect(value!.turns.map((turn) => turn.question)).toEqual(["ambiguous away"]);
});

test("leaving with a clarification open re-opens it on return and cancel still restores the draft", async () => {
  const contract = contractFor("ambiguous open", true);
  api.previewAskIntent.mockResolvedValue(contract);
  render(<Harness />);
  beginOwnedNotebook();
  act(() => value!.selectMode("reasoning"));
  await act(async () => {
    await value!.submit("ambiguous open");
  });
  expect(value!.intentReview?.contract).toBe(contract);

  act(() => value!.leaveWorkspace());
  const owner = beginOwnedNotebook(2);
  await act(async () => {
    await value!.restoreNotebook(owner);
    value!.finishNotebookTransition(owner);
  });
  expect(value!.intentReview?.contract).toBe(contract);
  expect(value!.pendingQuestion).toBe("ambiguous open");

  act(() => value!.cancelIntent());
  expect(value!.intentReview).toBeNull();
  expect(value!.pendingQuestion).toBe("");
  expect(value!.question).toBe("ambiguous open");
  expect(api.runAskStream).not.toHaveBeenCalled();

  // The cancelled review is gone for good: a further restore has nothing to re-attach.
  act(() => value!.leaveWorkspace());
  const again = beginOwnedNotebook(3);
  await act(async () => {
    await value!.restoreNotebook(again);
    value!.finishNotebookTransition(again);
  });
  expect(value!.intentReview).toBeNull();
  expect(value!.pendingQuestion).toBe("");
});

// codex #661 r1 P1: records were keyed by actor/notebook only, so asking again
// in a new session overwrote the earlier detached record.
test("two detached runs of one notebook both survive and restore newest-first", async () => {
  const streamA = deferred<AskResponse>();
  const streamB = deferred<AskResponse>();
  const onStarts: Array<(jobId: string, conversationId: string) => void | Promise<void>> = [];
  api.runAskStream.mockImplementation((
    _notebookId: string,
    _payload: unknown,
    _onProgress: unknown,
    _signal?: AbortSignal,
    nextOnStart?: (jobId: string, conversationId: string) => void | Promise<void>,
  ) => {
    onStarts.push(nextOnStart!);
    return onStarts.length === 1 ? streamA.promise : streamB.promise;
  });
  api.listConversations.mockResolvedValue([]);
  render(<Harness />);
  beginOwnedNotebook();

  let submittingA!: Promise<void>;
  act(() => {
    submittingA = value!.submit("question A");
  });
  act(() => value!.startNewSession(2));
  let submittingB!: Promise<void>;
  act(() => {
    submittingB = value!.submit("question B");
  });
  expect(api.runAskStream).toHaveBeenCalledTimes(2);
  act(() => value!.leaveWorkspace());

  const owner = beginOwnedNotebook(3);
  await act(async () => {
    await value!.restoreNotebook(owner);
    value!.finishNotebookTransition(owner);
  });
  expect(value!.asking).toBe(true);
  expect(value!.pendingQuestion).toBe("question B");

  // The older run still lands in history without disturbing the view.
  api.listConversations.mockResolvedValue([summary("conversation-a")]);
  await act(async () => {
    await onStarts[0]!("job-a", "conversation-a");
  });
  streamA.resolve(answer("conversation-a"));
  await act(async () => submittingA);
  expect(value!.pendingQuestion).toBe("question B");
  expect(value!.turns).toEqual([]);
  expect(value!.sessions.map((item) => item.id)).toContain("conversation-a");

  await act(async () => {
    await onStarts[1]!("job-b", "conversation-b");
  });
  streamB.resolve(answer("conversation-b"));
  await act(async () => submittingB);
  expect(value!.conversationId).toBe("conversation-b");
  expect(value!.turns.map((turn) => turn.question)).toEqual(["question B"]);
});

test("a detached clarification outlives a newer run in the same notebook and re-opens after it settles", async () => {
  const preview = deferred<QueryIntentContract>();
  const stream = deferred<AskResponse>();
  let onStart: ((jobId: string, conversationId: string) => void | Promise<void>) | undefined;
  api.previewAskIntent.mockReturnValue(preview.promise);
  api.runAskStream.mockImplementation((
    _notebookId: string,
    _payload: unknown,
    _onProgress: unknown,
    _signal?: AbortSignal,
    nextOnStart?: (jobId: string, conversationId: string) => void | Promise<void>,
  ) => {
    onStart = nextOnStart;
    return stream.promise;
  });
  api.listConversations.mockResolvedValue([]);
  render(<Harness />);
  beginOwnedNotebook();
  act(() => value!.selectMode("reasoning"));

  let submittingA!: Promise<void>;
  act(() => {
    submittingA = value!.submit("ambiguous A");
  });
  act(() => value!.startNewSession(2));
  let submittingB!: Promise<void>;
  act(() => {
    submittingB = value!.submit("question B");
  });
  expect(api.runAskStream).toHaveBeenCalledTimes(1);
  act(() => value!.leaveWorkspace());

  const contract = contractFor("ambiguous A", true);
  preview.resolve(contract);
  await act(async () => submittingA);

  const owner = beginOwnedNotebook(3);
  await act(async () => {
    await value!.restoreNotebook(owner);
    value!.finishNotebookTransition(owner);
  });
  expect(value!.pendingQuestion).toBe("question B");
  expect(value!.intentReview).toBeNull();

  api.listConversations.mockResolvedValue([summary("conversation-b")]);
  await act(async () => {
    await onStart!("job-b", "conversation-b");
  });
  stream.resolve(answer("conversation-b"));
  await act(async () => submittingB);
  expect(value!.turns.map((turn) => turn.question)).toEqual(["question B"]);

  act(() => value!.leaveWorkspace());
  const again = beginOwnedNotebook(4);
  await act(async () => {
    await value!.restoreNotebook(again);
    value!.finishNotebookTransition(again);
  });
  expect(api.getConversation).not.toHaveBeenCalled();
  expect(value!.intentReview?.contract).toBe(contract);
  expect(value!.pendingQuestion).toBe("ambiguous A");
  expect(value!.conversationId).toBeNull();
});

// codex #661 r1 P1: a preview that failed while detached used to delete its only
// record silently — the question was gone and nothing reported the failure.
test("an intent preview that fails while away reports on return and hands the question back", async () => {
  const preview = deferred<QueryIntentContract>();
  api.previewAskIntent.mockReturnValue(preview.promise);
  api.listConversations.mockResolvedValue([summary("conversation-older")]);
  api.getConversation.mockResolvedValue(detail("conversation-older"));
  render(<Harness />);
  beginOwnedNotebook();
  act(() => value!.selectMode("reasoning"));

  let submitting!: Promise<void>;
  act(() => {
    submitting = value!.submit("failing question");
  });
  act(() => value!.leaveWorkspace());
  const failure = new Error("intent service down");
  preview.reject(failure);
  await act(async () => submitting);
  expect(effects.reportError).not.toHaveBeenCalled();

  const owner = beginOwnedNotebook(2);
  await act(async () => {
    await value!.restoreNotebook(owner);
    value!.finishNotebookTransition(owner);
  });
  expect(effects.reportError).toHaveBeenCalledWith(failure);
  expect(value!.question).toBe("failing question");
  expect(value!.intentChecking).toBe(false);
  expect(value!.pendingQuestion).toBe("");
  expect(api.runAskStream).not.toHaveBeenCalled();

  // Consumed once: a further restore falls back to plain history.
  act(() => value!.leaveWorkspace());
  const again = beginOwnedNotebook(3);
  await act(async () => {
    await value!.restoreNotebook(again);
    value!.finishNotebookTransition(again);
  });
  expect(effects.reportError).toHaveBeenCalledTimes(1);
  expect(value!.conversationId).toBe("conversation-older");
});

test("a durable run that fails while away reports on return and hands the question back", async () => {
  const stream = deferred<AskResponse>();
  api.runAskStream.mockReturnValue(stream.promise);
  api.listConversations.mockResolvedValue([summary("conversation-older")]);
  api.getConversation.mockResolvedValue(detail("conversation-older"));
  render(<Harness />);
  beginOwnedNotebook();

  let submitting!: Promise<void>;
  act(() => {
    submitting = value!.submit("failing durable question");
  });
  act(() => value!.leaveWorkspace());
  const failure = new Error("ask service down");
  stream.reject(failure);
  await act(async () => submitting);
  expect(effects.reportError).not.toHaveBeenCalled();

  const owner = beginOwnedNotebook(2);
  await act(async () => {
    await value!.restoreNotebook(owner);
    value!.finishNotebookTransition(owner);
  });
  expect(effects.reportError).toHaveBeenCalledWith(failure);
  expect(value!.question).toBe("failing durable question");
  expect(value!.asking).toBe(false);
  expect(value!.pendingQuestion).toBe("");
});

// codex #661 r1 P2: a post-`started` run whose job finished server-side while the
// returning view loaded its (now terminal) detail must not be re-attached — its
// final event would append the already restored turn a second time.
test("a run whose restored detail is already terminal is not re-attached and does not duplicate its turn", async () => {
  const stream = deferred<AskResponse>();
  let onStart: ((jobId: string, conversationId: string) => void | Promise<void>) | undefined;
  api.runAskStream.mockImplementation((
    _notebookId: string,
    _payload: unknown,
    _onProgress: unknown,
    _signal?: AbortSignal,
    nextOnStart?: (jobId: string, conversationId: string) => void | Promise<void>,
  ) => {
    onStart = nextOnStart;
    return stream.promise;
  });
  api.listConversations.mockResolvedValue([summary("conversation-durable")]);
  api.getConversation.mockResolvedValue(
    detail("conversation-durable", "notebook-a", { question: "durable question" }),
  );
  render(<Harness />);
  beginOwnedNotebook();

  let submitting!: Promise<void>;
  act(() => {
    submitting = value!.submit("durable question");
  });
  await act(async () => {
    await onStart!("job-durable", "conversation-durable");
  });
  act(() => value!.leaveWorkspace());

  const owner = beginOwnedNotebook(2);
  await act(async () => {
    await value!.restoreNotebook(owner);
    value!.finishNotebookTransition(owner);
  });
  expect(value!.conversationId).toBe("conversation-durable");
  expect(value!.asking).toBe(false);
  expect(value!.turns.map((turn) => turn.question)).toEqual(["durable question"]);

  stream.resolve(answer("conversation-durable"));
  await act(async () => submitting);
  expect(value!.turns.map((turn) => turn.question)).toEqual(["durable question"]);
  expect(value!.asking).toBe(false);
});

// codex #661 r1 P2: a pre-`started` run that started and finished while the
// restore was still loading detail vanished from the records, so the restore
// neither attached it nor showed its answer.
test("a run that completes during restoration is projected from its final response instead of leaving the view blank", async () => {
  const stream = deferred<AskResponse>();
  let onStart: ((jobId: string, conversationId: string) => void | Promise<void>) | undefined;
  api.runAskStream.mockImplementation((
    _notebookId: string,
    _payload: unknown,
    _onProgress: unknown,
    _signal?: AbortSignal,
    nextOnStart?: (jobId: string, conversationId: string) => void | Promise<void>,
  ) => {
    onStart = nextOnStart;
    return stream.promise;
  });
  api.listConversations.mockResolvedValue([summary("conversation-x")]);
  const staleDetail = detail("conversation-x");
  const settledDetail: ConversationDetail = {
    ...detail("conversation-x"),
    turn_count: 2,
    turns: [
      ...staleDetail.turns,
      {
        answer_id: "answer-follow-up",
        question: "follow-up",
        response: answer("conversation-x", "answer-follow-up"),
        asked_at: "2026-08-22T00:01:00Z",
        created_at: "2026-08-22T00:01:00Z",
      },
    ],
  };
  const lateDetail = deferred<ConversationDetail>();
  api.getConversation
    .mockResolvedValueOnce(staleDetail)
    .mockReturnValueOnce(lateDetail.promise)
    .mockResolvedValueOnce(settledDetail);
  render(<Harness />);
  const first = beginOwnedNotebook();
  await act(async () => {
    await value!.restoreNotebook(first);
  });
  expect(value!.conversationId).toBe("conversation-x");

  let submitting!: Promise<void>;
  act(() => {
    submitting = value!.submit("follow-up");
  });
  act(() => value!.leaveWorkspace());

  const owner = beginOwnedNotebook(2);
  let restoring!: Promise<boolean>;
  act(() => {
    restoring = value!.restoreNotebook(owner);
  });
  await act(async () => {
    for (let i = 0; i < 20 && api.getConversation.mock.calls.length < 2; i += 1) {
      await Promise.resolve();
    }
  });
  expect(api.getConversation).toHaveBeenCalledTimes(2);

  // The tracked run starts and finishes while that detail is still loading.
  await act(async () => {
    await onStart!("job-follow-up", "conversation-x");
  });
  stream.resolve(answer("conversation-x", "answer-follow-up"));
  await act(async () => submitting);

  lateDetail.resolve(staleDetail);
  await act(async () => {
    await restoring;
    value!.finishNotebookTransition(owner);
  });
  // The settled run's own final response is projected — the restore itself adds
  // no further detail read (the two extra list reads are the run's own
  // started/final history refreshes, not the restore's).
  expect(api.getConversation).toHaveBeenCalledTimes(2);
  expect(api.listConversations).toHaveBeenCalledTimes(4);
  expect(value!.asking).toBe(false);
  expect(value!.conversationId).toBe("conversation-x");
  expect(value!.turns.map((turn) => turn.question)).toEqual(["question-conversation-x", "follow-up"]);
  expect(value!.turns[1]?.response.answer_id).toBe("answer-follow-up");
});

// codex #661 r2 P2: a durable run handed over from an older intent preview must
// keep that preview's submission order, or it would outrank a newer question.
test("an older preview finishing late hands its serial to the durable run and stays behind the newer question", async () => {
  const preview = deferred<QueryIntentContract>();
  const streamA = deferred<AskResponse>();
  const streamB = deferred<AskResponse>();
  const streams: Array<{ question: string; onStart: (jobId: string, conversationId: string) => void | Promise<void> }> = [];
  api.previewAskIntent.mockReturnValue(preview.promise);
  api.runAskStream.mockImplementation((
    _notebookId: string,
    payload: unknown,
    _onProgress: unknown,
    _signal?: AbortSignal,
    nextOnStart?: (jobId: string, conversationId: string) => void | Promise<void>,
  ) => {
    const question = (payload as { question: string }).question;
    streams.push({ question, onStart: nextOnStart! });
    return question === "question B" ? streamB.promise : streamA.promise;
  });
  api.listConversations.mockResolvedValue([]);
  render(<Harness />);
  beginOwnedNotebook();
  act(() => value!.selectMode("reasoning"));

  let submittingA!: Promise<void>;
  act(() => {
    submittingA = value!.submit("clear A");
  });
  act(() => value!.startNewSession(2));
  let submittingB!: Promise<void>;
  act(() => {
    submittingB = value!.submit("question B");
  });
  act(() => value!.leaveWorkspace());

  // The older preview completes while away and starts its durable run late.
  preview.resolve(contractFor("clear A", false));
  await act(async () => {
    await preview.promise;
    await Promise.resolve();
    await Promise.resolve();
  });
  expect(streams.map((item) => item.question)).toEqual(["question B", "clear A"]);

  const owner = beginOwnedNotebook(3);
  await act(async () => {
    await value!.restoreNotebook(owner);
    value!.finishNotebookTransition(owner);
  });
  expect(value!.pendingQuestion).toBe("question B");

  streamA.resolve(answer("conversation-a"));
  streamB.resolve(answer("conversation-b"));
  await act(async () => {
    await streams[1]!.onStart("job-a", "conversation-a");
    await streams[0]!.onStart("job-b", "conversation-b");
    await submittingA;
    await submittingB;
  });
  expect(value!.turns.map((turn) => turn.question)).toEqual(["question B"]);
});

// codex #661 r2 P2: after `started` the durable job outlives the transport, so a
// detached transport failure must defer to history/reconnect instead of
// re-offering the question as a failed draft.
test("a post-started transport failure while away defers to the restored durable state", async () => {
  const stream = deferred<AskResponse>();
  let onStart: ((jobId: string, conversationId: string) => void | Promise<void>) | undefined;
  api.runAskStream.mockImplementation((
    _notebookId: string,
    _payload: unknown,
    _onProgress: unknown,
    _signal?: AbortSignal,
    nextOnStart?: (jobId: string, conversationId: string) => void | Promise<void>,
  ) => {
    onStart = nextOnStart;
    return stream.promise;
  });
  api.listConversations.mockResolvedValue([summary("conversation-durable")]);
  api.getConversation.mockResolvedValue({
    ...detail("conversation-durable"),
    turn_count: 0,
    turns: [],
    active_job: {
      job_id: "job-durable",
      question: "durable question",
      asked_at: "2026-08-22T00:00:00Z",
      mode: "chunk",
      trace: [],
    },
  });
  render(<Harness />);
  beginOwnedNotebook();

  let submitting!: Promise<void>;
  act(() => {
    submitting = value!.submit("durable question");
  });
  await act(async () => {
    await onStart!("job-durable", "conversation-durable");
  });
  act(() => value!.leaveWorkspace());
  stream.reject(new Error("transport reset"));
  await act(async () => submitting);

  const owner = beginOwnedNotebook(2);
  await act(async () => {
    await value!.restoreNotebook(owner);
    value!.finishNotebookTransition(owner);
  });
  expect(effects.reportError).not.toHaveBeenCalled();
  expect(value!.question).toBe("");
  // The server still owns the job: reconnect polling projects it as pending.
  expect(value!.asking).toBe(true);
  expect(value!.pendingQuestion).toBe("durable question");
  expect(value!.conversationId).toBe("conversation-durable");
});

// codex #661 r3 P2: the UI mode is per actor, so switching to automatic mode must
// also cancel a reasoning preview detached in a notebook the user has left.
test("switching to automatic mode cancels a detached intent preview and returns it as a draft on restore", async () => {
  const preview = deferred<QueryIntentContract>();
  api.previewAskIntent.mockReturnValue(preview.promise);
  api.listConversations.mockResolvedValue([]);
  const view = render(<Harness />);
  beginOwnedNotebook();
  act(() => value!.selectMode("reasoning"));

  let submitting!: Promise<void>;
  act(() => {
    submitting = value!.submit("detached reasoning question");
  });
  act(() => value!.leaveWorkspace());
  const signal = api.previewAskIntent.mock.calls[0]?.[3] as AbortSignal;
  expect(signal.aborted).toBe(false);

  view.rerender(<Harness policy={{ ...DEFAULT_POLICY, advanced: false }} />);
  expect(signal.aborted).toBe(true);
  preview.resolve(contractFor("detached reasoning question", false));
  await act(async () => submitting);
  expect(api.runAskStream).not.toHaveBeenCalled();

  view.rerender(<Harness />);
  const owner = beginOwnedNotebook(2);
  await act(async () => {
    await value!.restoreNotebook(owner);
    value!.finishNotebookTransition(owner);
  });
  expect(value!.question).toBe("detached reasoning question");
  expect(value!.intentChecking).toBe(false);
  expect(value!.intentReview).toBeNull();
  expect(effects.reportError).toHaveBeenCalledTimes(1);
  expect(api.runAskStream).not.toHaveBeenCalled();
});

// codex #661 r4 P2: when the newest detached run settles while the restore's
// detail is loading, an older detached run must not be attached over the
// restored conversation — history is reloaded for the newest question instead.
test("an older detached run is not attached when the newest one settles during restoration", async () => {
  const streamA = deferred<AskResponse>();
  const streamB = deferred<AskResponse>();
  const streams: Array<{ question: string; onStart: (jobId: string, conversationId: string) => void | Promise<void> }> = [];
  api.runAskStream.mockImplementation((
    _notebookId: string,
    payload: unknown,
    _onProgress: unknown,
    _signal?: AbortSignal,
    nextOnStart?: (jobId: string, conversationId: string) => void | Promise<void>,
  ) => {
    const question = (payload as { question: string }).question;
    streams.push({ question, onStart: nextOnStart! });
    return question === "question B" ? streamB.promise : streamA.promise;
  });
  api.listConversations.mockResolvedValue([summary("conversation-x")]);
  const staleDetail = detail("conversation-x");
  const settledDetail: ConversationDetail = {
    ...detail("conversation-x"),
    turn_count: 2,
    turns: [
      ...staleDetail.turns,
      {
        answer_id: "answer-b",
        question: "question B",
        response: answer("conversation-x", "answer-b"),
        asked_at: "2026-08-22T00:01:00Z",
        created_at: "2026-08-22T00:01:00Z",
      },
    ],
  };
  const lateDetail = deferred<ConversationDetail>();
  api.getConversation
    .mockResolvedValueOnce(staleDetail)
    .mockReturnValueOnce(lateDetail.promise)
    .mockResolvedValueOnce(settledDetail);
  render(<Harness />);
  beginOwnedNotebook();

  // Older run A in a fresh session, then B as a follow-up in conversation X.
  let submittingA!: Promise<void>;
  act(() => {
    submittingA = value!.submit("question A");
  });
  await act(async () => {
    await value!.openSession("conversation-x", 2);
  });
  let submittingB!: Promise<void>;
  act(() => {
    submittingB = value!.submit("question B");
  });
  act(() => value!.leaveWorkspace());

  const owner = beginOwnedNotebook(3);
  let restoring!: Promise<boolean>;
  act(() => {
    restoring = value!.restoreNotebook(owner);
  });
  await act(async () => {
    for (let i = 0; i < 20 && api.getConversation.mock.calls.length < 2; i += 1) {
      await Promise.resolve();
    }
  });
  expect(api.getConversation).toHaveBeenCalledTimes(2);

  // B settles while its detail is still loading; A stays detached.
  const b = streams.find((item) => item.question === "question B")!;
  await act(async () => {
    await b.onStart("job-b", "conversation-x");
  });
  streamB.resolve(answer("conversation-x", "answer-b"));
  await act(async () => submittingB);

  lateDetail.resolve(staleDetail);
  await act(async () => {
    await restoring;
    value!.finishNotebookTransition(owner);
  });
  expect(api.getConversation).toHaveBeenCalledTimes(2);
  expect(value!.conversationId).toBe("conversation-x");
  expect(value!.turns.map((turn) => turn.question)).toEqual(["question-conversation-x", "question B"]);
  expect(value!.asking).toBe(false);
  expect(value!.pendingQuestion).toBe("");

  // A remains a detached record and only refreshes history when it lands.
  const a = streams.find((item) => item.question === "question A")!;
  await act(async () => {
    await a.onStart("job-a", "conversation-a");
  });
  streamA.resolve(answer("conversation-a"));
  await act(async () => submittingA);
  expect(value!.conversationId).toBe("conversation-x");
  expect(value!.turns).toHaveLength(2);
});

// codex #661 r5 P2: the notebook transition resets the engine/effort controls;
// re-attaching a run must restore the selection it was submitted with.
test("re-attaching a run restores its engine and retrieval effort for follow-ups", async () => {
  const stream = deferred<AskResponse>();
  api.runAskStream.mockReturnValue(stream.promise);
  api.listConversations.mockResolvedValue([]);
  render(<Harness />);
  const first = beginOwnedNotebook();
  await act(async () => {
    await value!.restoreNotebook(first);
  });
  act(() => value!.selectRetrievalEffort("exhaustive"));

  let submitting!: Promise<void>;
  act(() => {
    submitting = value!.submit("budgeted question");
  });
  expect((api.runAskStream.mock.calls[0]?.[1] as { retrieval_effort: string }).retrieval_effort).toBe("exhaustive");
  act(() => value!.leaveWorkspace());

  const owner = beginOwnedNotebook(2);
  await act(async () => {
    await value!.restoreNotebook(owner);
    value!.finishNotebookTransition(owner);
  });
  expect(value!.asking).toBe(true);
  expect(value!.mode).toBe("chunk");
  expect(value!.retrievalEffort).toBe("exhaustive");

  stream.resolve(answer("conversation-budgeted"));
  await act(async () => submitting);
  expect(value!.retrievalEffort).toBe("exhaustive");
  expect(value!.turns.map((turn) => turn.question)).toEqual(["budgeted question"]);
});

// PR #557 regression: `turns`/`sessions`/`pendingTrace`/`feedbackSent` used to
// fall back to a bare `[]`/`{}` literal whenever the owner is not visible
// (actorId is null, e.g. logged out / collection page). A bare literal is a
// brand-new reference on every render, which makes a consuming effect's
// dependency array "change" every render — page.tsx has one such effect that
// calls setState in its body, turning into an infinite render loop ("Maximum
// update depth exceeded"). The fix hoists frozen, stable module-level
// fallback constants; re-rendering with the owner still hidden must hand
// back the *same* reference every time.
test("owner-hidden view fields stay referentially stable across re-renders", () => {
  const view = render(<Harness actorId={null} notebookId={null} />);
  const first = value!;
  expect(first.turns).toEqual([]);
  expect(first.sessions).toEqual([]);
  expect(first.pendingTrace).toEqual([]);
  expect(first.feedbackSent).toEqual({});
  // A plain `useState` initial value is never frozen; only the hidden-state
  // fallback branch (the module-level `NO_*` constant) is. Asserting frozen
  // here pins down *which* branch actually produced this value, not merely
  // that it happens to equal an empty literal.
  expect(Object.isFrozen(first.turns)).toBe(true);
  expect(Object.isFrozen(first.sessions)).toBe(true);
  expect(Object.isFrozen(first.pendingTrace)).toBe(true);
  expect(Object.isFrozen(first.feedbackSent)).toBe(true);

  act(() => {
    view.rerender(<Harness actorId={null} notebookId={null} />);
  });
  const second = value!;
  act(() => {
    view.rerender(<Harness actorId={null} notebookId={null} />);
  });
  const third = value!;

  for (const later of [second, third]) {
    expect(later.turns).toBe(first.turns);
    expect(later.sessions).toBe(first.sessions);
    expect(later.pendingTrace).toBe(first.pendingTrace);
    expect(later.feedbackSent).toBe(first.feedbackSent);
  }
});
