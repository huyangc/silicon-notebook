import { act, cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import type { AskIntentConfirmation, QueryIntentContract } from "../../app/ask-intent-model";
import type { AskJobDetail } from "../../app/ask-reconnect";
import type {
  AskResponse,
  ConversationDetail,
  ConversationSummary,
} from "../../app/workspace-model";

const api = vi.hoisted(() => ({
  bulkDeleteConversations: vi.fn(),
  cancelAskJob: vi.fn(),
  deleteConversation: vi.fn(),
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

  const owner = beginOwnedNotebook();
  await act(async () => {
    await value!.restoreNotebook(owner);
  });

  expect(api.listConversations).toHaveBeenCalledTimes(1);
  expect(api.listConversations).toHaveBeenCalledWith("notebook-a");
  expect(api.getConversation).toHaveBeenCalledTimes(1);
  expect(api.getConversation).toHaveBeenCalledWith("conversation-latest");
  expect(value!.conversationId).toBe("conversation-latest");
  expect(value!.turns).toHaveLength(1);
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

test("durable A/G1 stream publishes only history after notebook A -> B -> A/G3", async () => {
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
  expect(value!.conversationId).toBe("conversation-g3");
  expect(value!.turns[0]?.response.answer_id).toBe("answer-conversation-g3");

  await act(async () => {
    await onStart!("job-durable", "conversation-durable");
  });
  expect(signal?.aborted).toBe(false);
  expect(api.cancelAskJob).not.toHaveBeenCalled();
  expect(value!.sessions.map((item) => item.id)).toEqual([
    "conversation-durable",
    "conversation-g3",
  ]);
  expect(value!.conversationId).toBe("conversation-g3");
  expect(value!.turns[0]?.response.answer_id).toBe("answer-conversation-g3");

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
  expect(value!.conversationId).toBe("conversation-g3");
  expect(value!.turns).toHaveLength(1);
  expect(value!.turns[0]?.response.answer_id).toBe("answer-conversation-g3");
});
