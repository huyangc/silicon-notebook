import { act, cleanup, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import type { NotebookSummary, PendingMerge, UnifiedGraphResp } from "../../app/workspace-model";

const knowledgeApi = vi.hoisted(() => ({
  createNotebookObjectSchema: vi.fn(),
  createObjectSchema: vi.fn(),
  deleteNotebookObjectSchema: vi.fn(),
  deleteObjectSchema: vi.fn(),
  findDuplicates: vi.fn(),
  listKnowledge: vi.fn(),
  listKnowledgeTypes: vi.fn(),
  listNotebookObjectSchemas: vi.fn(),
  listObjectSchemas: vi.fn(),
  mergeKnowledge: vi.fn(),
  proposeObjectSchemas: vi.fn(),
  updateKnowledge: vi.fn(),
  updateNotebookObjectSchema: vi.fn(),
  updateObjectSchema: vi.fn(),
}));

const kgApi = vi.hoisted(() => ({
  buildKg: vi.fn(),
  confirmMerge: vi.fn(),
  fetchConceptDetail: vi.fn(),
  fetchKgNeighbors: vi.fn(),
  fetchKgSearch: vi.fn(),
  fetchMergeReviewJob: vi.fn(),
  fetchNodeContext: vi.fn(),
  fetchPendingMerges: vi.fn(),
  fetchRelinkStatus: vi.fn(),
  fetchUnifiedGraph: vi.fn(),
  fetchUnifiedKgRebuildStatus: vi.fn(),
  fetchUnifiedKgStatus: vi.fn(),
  rebuildKg: vi.fn(),
  rebuildUnifiedKg: vi.fn(),
  rejectMerge: vi.fn(),
  relinkKg: vi.fn(),
  reviewAllMerges: vi.fn(),
  reviewMerges: vi.fn(),
}));

vi.mock("../../app/knowledge-api.ts", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../app/knowledge-api.ts")>()),
  ...knowledgeApi,
}));

vi.mock("../../features/kg-maintenance/kg-api.ts", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../features/kg-maintenance/kg-api.ts")>()),
  ...kgApi,
}));

import { useKgWorkspace } from "../../app/use-kg-workspace";
import {
  REBUILD_POLL_MAX_ATTEMPTS,
  REBUILD_POLL_TIMED_OUT,
} from "../../features/kg-maintenance/kg-rebuild-status";

type HookValue = ReturnType<typeof useKgWorkspace>;
type HookOptions = Parameters<typeof useKgWorkspace>[0];

const writablePolicy: HookOptions["policy"] = {
  canGovernKnowledge: true,
  canManageNotebookSchemas: true,
  canManageGlobalSchemas: true,
  canWriteKg: true,
  externalBuildPolling: false,
};

const refreshNotebook = vi.fn();
const notify = vi.fn<(message: string) => void>();

const effects: HookOptions["effects"] = {
  notify,
  reportError: vi.fn(),
  refreshCollection: vi.fn().mockResolvedValue(undefined),
  refreshNotebook,
  focusGraphNode: vi.fn(),
};

let value: HookValue | null = null;

function Harness({
  actorId = "user-a",
  notebookId = "notebook-a",
  policy = writablePolicy,
}: {
  actorId?: string | null;
  notebookId?: string | null;
  policy?: HookOptions["policy"];
}) {
  value = useKgWorkspace({ actorId, notebookId, policy, effects });
  return (
    <div>
      {value.knowledge.items?.map((row) => row.id).join(",") ?? "knowledge-idle"}:
      {value.graph.open ? "graph-open" : "graph-closed"}:
      {value.graph.buildingKg ? "building" : "idle"}
    </div>
  );
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function httpConflict(): Error {
  const error = new Error("busy");
  Object.defineProperty(error, Symbol.for("silicon-notebook.errors.httpStatus"), {
    value: 409,
  });
  return error;
}

function graph(vizBuilding = false): UnifiedGraphResp {
  return { nodes: [], edges: [], viz_building: vizBuilding };
}

function candidate(id = "merge-a"): PendingMerge {
  return {
    id,
    canonical_a: "K-a",
    canonical_b: "K-b",
    score: 0.9,
    status: "pending",
  };
}

function notebook(id: string, jobId?: string): NotebookSummary {
  return {
    id,
    name: id,
    purpose: "",
    primary_domain: "",
    status: "ready",
    counts: {},
    created_label: "today",
    ...(jobId ? {
      kg_build: {
        job_id: jobId,
        mode: "incremental",
        status: "running",
        stage: "extracting",
        total_sources: 1,
        completed_sources: 0,
        failed_sources: 0,
        error_code: "",
        user_message: "",
        updated_at: "2026-08-22T00:00:00Z",
      },
    } : {}),
  };
}

beforeEach(() => {
  value = null;
  vi.clearAllMocks();
  knowledgeApi.listKnowledgeTypes.mockResolvedValue([
    { object_type: "concept", label: "概念", count: 1 },
  ]);
  knowledgeApi.listKnowledge.mockResolvedValue({
    items: [{
      id: "knowledge-a",
      object_type: "concept",
      headline: "A",
      fields: [],
      status: "reviewed",
      evidence: [],
    }],
    total_count: 1,
    offset: 0,
    limit: 50,
  });
  knowledgeApi.listNotebookObjectSchemas.mockResolvedValue([]);
  knowledgeApi.listObjectSchemas.mockResolvedValue([]);
  knowledgeApi.proposeObjectSchemas.mockResolvedValue([]);
  kgApi.fetchUnifiedGraph.mockResolvedValue(graph());
  kgApi.fetchPendingMerges.mockResolvedValue([]);
  kgApi.fetchUnifiedKgStatus.mockResolvedValue({
    dirty: false,
    last_rebuild_at: "",
    objects: 0,
    relations: 0,
    clusters: 0,
    viz_indexed: true,
    viz_nodes: 0,
    viz_edges: 0,
    viz_stale: false,
  });
  kgApi.fetchMergeReviewJob.mockResolvedValue({ status: "idle", total: 0, done: 0, error: "" });
  kgApi.fetchUnifiedKgRebuildStatus.mockResolvedValue({
    job_id: "",
    notebook_id: "notebook-a",
    status: "idle",
    running: false,
    clusters: 0,
  });
  kgApi.fetchRelinkStatus.mockResolvedValue({
    job_id: "",
    notebook_id: "notebook-a",
    status: "idle",
    running: false,
    linked: 0,
  });
  kgApi.fetchKgNeighbors.mockResolvedValue({ nodes: [], edges: [] });
  kgApi.fetchKgSearch.mockResolvedValue({ query: "", hits: [] });
  kgApi.fetchNodeContext.mockResolvedValue({
    id: "node-a",
    object_type: "concept",
    name: "A",
    section_path: "",
    occurrences: [],
    definition: null,
    steps: null,
  });
  kgApi.buildKg.mockResolvedValue({ status: "running", notebook_id: "notebook-a", job_id: "build-a" });
  kgApi.rebuildKg.mockResolvedValue({ status: "running", notebook_id: "notebook-a", job_id: "build-a" });
  kgApi.rebuildUnifiedKg.mockResolvedValue({ status: "running", notebook_id: "notebook-a", job_id: "rebuild-a" });
  kgApi.relinkKg.mockResolvedValue({ status: "running", notebook_id: "notebook-a", job_id: "relink-a" });
  kgApi.reviewMerges.mockResolvedValue({ reviewed: 0, confirmed: 0, rejected: 0, unsure: 0 });
  kgApi.reviewAllMerges.mockResolvedValue({ status: "running" });
  kgApi.confirmMerge.mockResolvedValue({ ok: true });
  kgApi.rejectMerge.mockResolvedValue({ ok: true });
  refreshNotebook.mockResolvedValue(notebook("notebook-a"));
});

afterEach(() => {
  vi.useRealTimers();
  cleanup();
});

test("content reads stay lazy while owner recovery only probes maintenance status", async () => {
  render(<Harness />);
  await waitFor(() => expect(kgApi.fetchUnifiedKgRebuildStatus).toHaveBeenCalledOnce());
  expect(kgApi.fetchRelinkStatus).toHaveBeenCalledOnce();
  expect(kgApi.fetchMergeReviewJob).toHaveBeenCalledOnce();
  expect(knowledgeApi.listKnowledgeTypes).not.toHaveBeenCalled();
  expect(knowledgeApi.listKnowledge).not.toHaveBeenCalled();
  expect(knowledgeApi.listNotebookObjectSchemas).not.toHaveBeenCalled();
  expect(kgApi.fetchUnifiedGraph).not.toHaveBeenCalled();

  await act(async () => value!.enterKnowledge());
  expect(knowledgeApi.listKnowledgeTypes).toHaveBeenCalledWith("notebook-a");
  expect(knowledgeApi.listKnowledge).toHaveBeenCalledWith(
    "notebook-a", "concept", "", 0, 50,
  );
  expect(value!.knowledge.items?.map((row) => row.id)).toEqual(["knowledge-a"]);
});

test("actor replacement rejects a deferred knowledge list commit", async () => {
  const pending = deferred<Awaited<ReturnType<typeof knowledgeApi.listKnowledge>>>();
  knowledgeApi.listKnowledge.mockReturnValueOnce(pending.promise);
  const { rerender } = render(<Harness />);
  await waitFor(() => expect(kgApi.fetchRelinkStatus).toHaveBeenCalledOnce());

  let entering!: Promise<void>;
  act(() => { entering = value!.enterKnowledge(); });
  await waitFor(() => expect(knowledgeApi.listKnowledge).toHaveBeenCalledOnce());
  rerender(<Harness actorId="user-b" />);
  await act(async () => pending.resolve({
    items: [{
      id: "private-a",
      object_type: "concept",
      headline: "private",
      fields: [],
      status: "reviewed",
      evidence: [],
    }],
    total_count: 1,
    offset: 0,
    limit: 50,
  }));
  await entering;
  expect(value!.knowledge.items).toBeNull();
});

test("schema mutation rechecks live authority before its derived reload", async () => {
  const pending = deferred<void>();
  knowledgeApi.updateNotebookObjectSchema.mockReturnValueOnce(pending.promise);
  const { rerender } = render(<Harness />);
  act(() => value!.openSchemas());
  await waitFor(() => expect(knowledgeApi.listNotebookObjectSchemas).toHaveBeenCalledOnce());

  let mutating!: Promise<void>;
  act(() => { mutating = value!.patchSchema("concept", { status: "active" }); });
  await waitFor(() => expect(knowledgeApi.updateNotebookObjectSchema).toHaveBeenCalledOnce());
  rerender(<Harness policy={{ ...writablePolicy, canManageNotebookSchemas: false }} />);
  await act(async () => pending.resolve());
  await mutating;
  expect(knowledgeApi.listNotebookObjectSchemas).toHaveBeenCalledTimes(1);
  expect(effects.notify).not.toHaveBeenCalledWith("类型已更新");
});

test("opening the graph does not invalidate an in-flight Knowledge write owner", async () => {
  const pending = deferred<void>();
  knowledgeApi.updateKnowledge.mockReturnValueOnce(pending.promise);
  render(<Harness />);

  let updating!: Promise<void>;
  act(() => { updating = value!.updateKnowledge("knowledge-a", { status: "approved" }); });
  await waitFor(() => expect(value!.knowledge.busyId).toBe("knowledge-a"));
  await act(async () => value!.openGraph());
  await act(async () => pending.resolve());
  await updating;

  expect(value!.knowledge.busyId).toBeNull();
});

test("opening the graph does not stop the existing KG build poll", async () => {
  vi.useFakeTimers();
  refreshNotebook.mockResolvedValue(notebook("notebook-a", "build-a"));
  render(<Harness />);
  await act(async () => value!.startKgBuild());
  expect(value!.graph.buildingKg).toBe(true);
  expect(refreshNotebook).toHaveBeenCalledTimes(1);

  await act(async () => value!.openGraph());
  await act(async () => { await vi.advanceTimersByTimeAsync(6_000); });
  expect(refreshNotebook).toHaveBeenCalledTimes(2);
  expect(value!.graph.buildingKg).toBe(true);
});

test("a closed graph suppresses the stale open request error", async () => {
  const pending = deferred<UnifiedGraphResp>();
  kgApi.fetchUnifiedGraph.mockReturnValueOnce(pending.promise);
  render(<Harness />);

  let opening!: Promise<void>;
  act(() => { opening = value!.openGraph(); });
  act(() => value!.closeGraph());
  await act(async () => pending.reject(new Error("stale graph read")));
  await opening;
  expect(effects.reportError).not.toHaveBeenCalled();
});

test("read-only policy neither restores review work nor admits write commands", async () => {
  const readOnly: HookOptions["policy"] = {
    canGovernKnowledge: false,
    canManageNotebookSchemas: false,
    canManageGlobalSchemas: false,
    canWriteKg: false,
    externalBuildPolling: false,
  };
  render(<Harness policy={readOnly} />);
  await waitFor(() => expect(kgApi.fetchRelinkStatus).toHaveBeenCalledOnce());
  expect(kgApi.fetchMergeReviewJob).not.toHaveBeenCalled();

  act(() => {
    value!.reviewPendingMerges();
    value!.reviewAllMerges();
    value!.startRelink();
    value!.startRebuild();
    value!.startKgBuild();
    value!.updateKnowledge("knowledge-a", { status: "approved" });
  });
  expect(kgApi.reviewMerges).not.toHaveBeenCalled();
  expect(kgApi.reviewAllMerges).not.toHaveBeenCalled();
  expect(kgApi.relinkKg).not.toHaveBeenCalled();
  expect(kgApi.rebuildUnifiedKg).not.toHaveBeenCalled();
  expect(kgApi.buildKg).not.toHaveBeenCalled();
  expect(knowledgeApi.updateKnowledge).not.toHaveBeenCalled();
});

test("live permission loss stops review-job polling without another read", async () => {
  kgApi.fetchMergeReviewJob.mockResolvedValue({
    status: "running", total: 4, done: 1, error: "",
  });
  const { rerender } = render(<Harness />);
  await waitFor(() => expect(value!.graph.reviewAllRunning).toBe(true));
  expect(kgApi.fetchMergeReviewJob).toHaveBeenCalledOnce();

  vi.useFakeTimers();
  rerender(<Harness policy={{ ...writablePolicy, canWriteKg: false }} />);
  await act(async () => { await vi.advanceTimersByTimeAsync(12_000); });
  expect(kgApi.fetchMergeReviewJob).toHaveBeenCalledOnce();
});

test("maintenance submission suppresses stale terminal polls and rechecks permission before a 409 retry", async () => {
  const relinkPending = deferred<Awaited<ReturnType<typeof kgApi.relinkKg>>>();
  kgApi.relinkKg.mockReturnValueOnce(relinkPending.promise);
  const { rerender } = render(<Harness />);
  await waitFor(() => expect(kgApi.fetchRelinkStatus).toHaveBeenCalledOnce());
  vi.useFakeTimers();
  kgApi.fetchRelinkStatus.mockResolvedValue({
    job_id: "stale-relink",
    notebook_id: "notebook-a",
    status: "succeeded",
    running: false,
    linked: 1,
  });

  let relinking!: Promise<void>;
  act(() => { relinking = value!.startRelink(); });
  expect(kgApi.relinkKg).toHaveBeenCalledOnce();
  await act(async () => { await vi.advanceTimersByTimeAsync(3_000); });
  expect(value!.graph.relinking).toBe(true);

  const readOnly = { ...writablePolicy, canWriteKg: false };
  rerender(<Harness policy={readOnly} />);
  await act(async () => relinkPending.reject(httpConflict()));
  await relinking;
  expect(kgApi.relinkKg).toHaveBeenCalledTimes(1);

  rerender(<Harness policy={writablePolicy} />);
  const rebuildPending = deferred<Awaited<ReturnType<typeof kgApi.rebuildUnifiedKg>>>();
  kgApi.rebuildUnifiedKg.mockReturnValueOnce(rebuildPending.promise);
  let rebuilding!: Promise<void>;
  act(() => { rebuilding = value!.startRebuild(); });
  expect(kgApi.rebuildUnifiedKg).toHaveBeenCalledOnce();
  rerender(<Harness policy={readOnly} />);
  await act(async () => rebuildPending.reject(httpConflict()));
  await rebuilding;
  expect(kgApi.rebuildUnifiedKg).toHaveBeenCalledTimes(1);
});

test("synchronous relink commands stay single-flight and release authority after failure", async () => {
  const relinkPending = deferred<Awaited<ReturnType<typeof kgApi.relinkKg>>>();
  kgApi.relinkKg.mockReturnValueOnce(relinkPending.promise);
  render(<Harness />);
  await waitFor(() => expect(kgApi.fetchRelinkStatus).toHaveBeenCalledOnce());

  let firstRelink!: Promise<void>;
  act(() => {
    firstRelink = value!.startRelink();
    void value!.startRelink();
  });
  expect(kgApi.relinkKg).toHaveBeenCalledOnce();
  await act(async () => relinkPending.reject(new Error("relink failed")));
  await firstRelink;
  await waitFor(() => expect(value!.graph.relinking).toBe(false));
  await act(async () => value!.startRelink());
  expect(kgApi.relinkKg).toHaveBeenCalledTimes(2);
});

test("synchronous rebuild commands stay single-flight and release authority after failure", async () => {
  const rebuildPending = deferred<Awaited<ReturnType<typeof kgApi.rebuildUnifiedKg>>>();
  kgApi.rebuildUnifiedKg.mockReturnValueOnce(rebuildPending.promise);
  render(<Harness />);
  await waitFor(() => expect(kgApi.fetchUnifiedKgRebuildStatus).toHaveBeenCalledOnce());

  let firstRebuild!: Promise<void>;
  act(() => {
    firstRebuild = value!.startRebuild();
    void value!.startRebuild();
  });
  expect(kgApi.rebuildUnifiedKg).toHaveBeenCalledOnce();
  await act(async () => rebuildPending.reject(new Error("rebuild failed")));
  await firstRebuild;
  await waitFor(() => expect(value!.graph.rebuilding).toBe(false));
  await act(async () => value!.startRebuild());
  expect(kgApi.rebuildUnifiedKg).toHaveBeenCalledTimes(2);
});

test("Knowledge context reads are latest-wins and invalidated by a kind change", async () => {
  const first = deferred<Awaited<ReturnType<typeof kgApi.fetchNodeContext>>>();
  const second = deferred<Awaited<ReturnType<typeof kgApi.fetchNodeContext>>>();
  kgApi.fetchNodeContext
    .mockReturnValueOnce(first.promise)
    .mockReturnValueOnce(second.promise);
  render(<Harness />);
  await act(async () => value!.enterKnowledge());

  let firstRead!: Promise<void>;
  let secondRead!: Promise<void>;
  act(() => {
    firstRead = value!.loadKnowledgeContext("knowledge-a");
    secondRead = value!.loadKnowledgeContext("knowledge-a");
  });
  await act(async () => second.resolve({
    id: "knowledge-a",
    object_type: "concept",
    name: "newer",
    section_path: "",
    occurrences: [],
    definition: null,
    steps: null,
  }));
  await secondRead;
  await act(async () => first.resolve({
    id: "knowledge-a",
    object_type: "concept",
    name: "older",
    section_path: "",
    occurrences: [],
    definition: null,
    steps: null,
  }));
  await firstRead;
  expect(value!.knowledge.contexts["knowledge-a"]?.name).toBe("newer");

  const stale = deferred<Awaited<ReturnType<typeof kgApi.fetchNodeContext>>>();
  kgApi.fetchNodeContext.mockReturnValueOnce(stale.promise);
  let staleRead!: Promise<void>;
  act(() => { staleRead = value!.loadKnowledgeContext("knowledge-b"); });
  act(() => value!.selectKnowledgeKind("claim"));
  act(() => value!.selectKnowledgeKind("concept"));
  await act(async () => stale.resolve({
    id: "knowledge-b",
    object_type: "concept",
    name: "stale after kind round-trip",
    section_path: "",
    occurrences: [],
    definition: null,
    steps: null,
  }));
  await staleRead;
  expect(value!.knowledge.contexts["knowledge-b"]).toBeUndefined();
});

test("pending rebuild retries spend one POST per poll tick without adoption reads or repeated toast", async () => {
  const merge = candidate();
  kgApi.fetchPendingMerges.mockResolvedValue([merge]);
  kgApi.fetchUnifiedKgRebuildStatus
    .mockResolvedValueOnce({
      job_id: "", notebook_id: "notebook-a", status: "idle", running: false, clusters: 0,
    })
    .mockResolvedValueOnce({
      job_id: "occupied", notebook_id: "notebook-a", status: "running", running: true, clusters: 0,
    })
    .mockResolvedValue({
      job_id: "occupied", notebook_id: "notebook-a", status: "succeeded", running: false, clusters: 1,
    });
  kgApi.rebuildUnifiedKg
    .mockRejectedValueOnce(httpConflict())
    .mockRejectedValueOnce(httpConflict())
    .mockResolvedValueOnce({ status: "running", notebook_id: "notebook-a", job_id: "replacement" });

  render(<Harness />);
  await waitFor(() => expect(kgApi.fetchUnifiedKgRebuildStatus).toHaveBeenCalledOnce());
  await act(async () => value!.openGraph());
  vi.useFakeTimers();
  await act(async () => value!.decideMerge(merge, true));

  const pendingNotice = "合并已记录，将在当前任务完成后自动重新合并";
  const terminalNotice = "已重新合并，现有 1 组概念";
  expect(kgApi.rebuildUnifiedKg).toHaveBeenCalledTimes(1);
  expect(kgApi.fetchUnifiedKgRebuildStatus).toHaveBeenCalledTimes(2);
  expect(kgApi.fetchRelinkStatus).toHaveBeenCalledTimes(2);
  expect(notify.mock.calls.filter(([message]) => message === pendingNotice)).toHaveLength(1);
  expect(notify.mock.calls.filter(([message]) => message === terminalNotice)).toHaveLength(0);

  await act(async () => { await vi.advanceTimersByTimeAsync(3_000); });
  expect(kgApi.rebuildUnifiedKg).toHaveBeenCalledTimes(2);
  expect(kgApi.fetchUnifiedKgRebuildStatus).toHaveBeenCalledTimes(3);
  expect(kgApi.fetchRelinkStatus).toHaveBeenCalledTimes(2);
  expect(notify.mock.calls.filter(([message]) => message === pendingNotice)).toHaveLength(1);
  expect(notify.mock.calls.filter(([message]) => message === terminalNotice)).toHaveLength(1);

  await act(async () => { await vi.advanceTimersByTimeAsync(3_000); });
  expect(kgApi.rebuildUnifiedKg).toHaveBeenCalledTimes(3);
  expect(kgApi.fetchUnifiedKgRebuildStatus).toHaveBeenCalledTimes(4);
  expect(kgApi.fetchRelinkStatus).toHaveBeenCalledTimes(2);
  expect(notify.mock.calls.filter(([message]) => message === pendingNotice)).toHaveLength(1);
  expect(notify.mock.calls.filter(([message]) => message === terminalNotice)).toHaveLength(1);
});

test("pending rebuild retries remain bounded when every replacement POST stays occupied", async () => {
  const merge = candidate();
  kgApi.fetchPendingMerges.mockResolvedValue([merge]);
  kgApi.fetchUnifiedKgRebuildStatus
    .mockResolvedValueOnce({
      job_id: "", notebook_id: "notebook-a", status: "idle", running: false, clusters: 0,
    })
    .mockResolvedValueOnce({
      job_id: "occupied", notebook_id: "notebook-a", status: "running", running: true, clusters: 0,
    })
    .mockResolvedValue({
      job_id: "occupied", notebook_id: "notebook-a", status: "succeeded", running: false, clusters: 1,
    });
  kgApi.rebuildUnifiedKg.mockRejectedValue(httpConflict());

  render(<Harness />);
  await waitFor(() => expect(kgApi.fetchUnifiedKgRebuildStatus).toHaveBeenCalledOnce());
  await act(async () => value!.openGraph());
  vi.useFakeTimers();
  await act(async () => value!.decideMerge(merge, true));
  expect(value!.graph.rebuilding).toBe(true);

  await act(async () => {
    await vi.advanceTimersByTimeAsync((REBUILD_POLL_MAX_ATTEMPTS + 1) * 3_000);
  });
  expect(kgApi.rebuildUnifiedKg).toHaveBeenCalledTimes(REBUILD_POLL_MAX_ATTEMPTS + 1);
  expect(value!.graph.rebuilding).toBe(false);
  expect(notify.mock.calls.filter(
    ([message]) => message === REBUILD_POLL_TIMED_OUT.toast,
  )).toHaveLength(1);
});

test("graph opening preserves parallel core reads and rejects stale search results", async () => {
  vi.useFakeTimers();
  const pendingSearch = deferred<{ query: string; hits: Array<{ object_id: string; name: string; object_type: string; score: number; match: string }> }>();
  kgApi.fetchKgSearch.mockReturnValueOnce(pendingSearch.promise);
  const { rerender } = render(<Harness />);
  await act(async () => value!.openGraph());
  expect(kgApi.fetchUnifiedGraph).toHaveBeenCalledWith("notebook-a", 80);
  expect(kgApi.fetchPendingMerges).toHaveBeenCalledWith("notebook-a");
  expect(kgApi.fetchUnifiedKgStatus).toHaveBeenCalledWith("notebook-a");

  act(() => value!.updateGraphSearch("private"));
  await act(async () => { await vi.advanceTimersByTimeAsync(300); });
  expect(kgApi.fetchKgSearch).toHaveBeenCalledWith("notebook-a", "private");
  rerender(<Harness actorId="user-b" notebookId="notebook-b" />);
  await act(async () => pendingSearch.resolve({
    query: "private",
    hits: [{ object_id: "secret-a", name: "secret", object_type: "concept", score: 1, match: "secret" }],
  }));
  expect(value!.graph.searchHits).toEqual([]);
});

test("viz polling is single-flight when a graph read exceeds one cadence", async () => {
  vi.useFakeTimers();
  const slowPoll = deferred<UnifiedGraphResp>();
  kgApi.fetchUnifiedGraph
    .mockResolvedValueOnce(graph(true))
    .mockReturnValueOnce(slowPoll.promise);
  render(<Harness />);
  await act(async () => value!.openGraph());

  await act(async () => { await vi.advanceTimersByTimeAsync(18_000); });
  expect(kgApi.fetchUnifiedGraph).toHaveBeenCalledTimes(2);
  await act(async () => slowPoll.resolve(graph(false)));
  expect(value!.graph.vizBuilding).toBe(false);
});

test("a successful notebook transition restores an in-flight build after the new owner exists", async () => {
  const { rerender } = render(<Harness />);
  await waitFor(() => expect(kgApi.fetchRelinkStatus).toHaveBeenCalledOnce());
  let transition!: ReturnType<HookValue["beginNotebookTransition"]>;
  act(() => { transition = value!.beginNotebookTransition(); });
  rerender(<Harness notebookId="notebook-b" />);
  expect(value!.graph.buildingKg).toBe(false);

  act(() => value!.finishNotebookTransition(transition, notebook("notebook-b", "build-b")));
  await waitFor(() => expect(value!.graph.buildingKg).toBe(true));
  expect(value!.graph.trackedKgJobId).toBe("build-b");
});

test("a merge decision tombstone converges across A to B to A", async () => {
  const pendingDecision = deferred<{ ok: boolean }>();
  const merge = candidate();
  kgApi.fetchPendingMerges.mockResolvedValue([merge]);
  kgApi.rejectMerge.mockReturnValueOnce(pendingDecision.promise);
  const { rerender } = render(<Harness />);
  await act(async () => value!.openGraph());
  expect(value!.graph.pendingMerges).toHaveLength(1);

  let deciding!: Promise<void>;
  act(() => { deciding = value!.decideMerge(merge, false); });
  let transition!: ReturnType<HookValue["beginNotebookTransition"]>;
  act(() => { transition = value!.beginNotebookTransition(); });
  rerender(<Harness notebookId="notebook-b" />);
  act(() => value!.finishNotebookTransition(transition, notebook("notebook-b")));
  await waitFor(() => expect(kgApi.fetchRelinkStatus).toHaveBeenCalledWith("notebook-b"));

  act(() => { transition = value!.beginNotebookTransition(); });
  rerender(<Harness notebookId="notebook-a" />);
  act(() => value!.finishNotebookTransition(transition, notebook("notebook-a")));
  await waitFor(() => expect(kgApi.fetchRelinkStatus).toHaveBeenCalledTimes(3));
  await act(async () => value!.openGraph());
  expect(value!.graph.pendingMerges).toHaveLength(1);

  await act(async () => pendingDecision.resolve({ ok: true }));
  await deciding;
  expect(value!.graph.pendingMerges).toEqual([]);
});

// PR #557 regression: `knowledge.types`/`knowledge.contexts`/`graph.searchHits`/
// `graph.selectedTypes`/`graph.pendingMerges` used to fall back to a bare
// `[]`/`{}` literal whenever the owner is not visible (no
// beginNotebookTransition has ever landed — e.g. actorId/notebookId are
// null). A bare literal is a brand-new reference on every render, which
// makes a consuming effect's dependency array "change" every render (see
// use-ask-session.ts for the traced infinite-loop incident). The fix hoists
// frozen, stable module-level fallback constants; re-rendering with the
// owner still hidden must hand back the *same* reference every time.
test("owner-hidden knowledge/graph view fields stay referentially stable across re-renders", () => {
  const view = render(<Harness actorId={null} notebookId={null} />);
  const first = value!;
  expect(first.knowledge.types).toEqual([]);
  expect(first.knowledge.contexts).toEqual({});
  expect(first.graph.searchHits).toEqual([]);
  expect(first.graph.selectedTypes).toEqual([]);
  expect(first.graph.pendingMerges).toEqual([]);
  // A plain `useState` initial value is never frozen; only the hidden-state
  // fallback branch (the module-level `NO_*` constant) is. Asserting frozen
  // here pins down *which* branch actually produced this value, not merely
  // that it happens to equal an empty literal.
  expect(Object.isFrozen(first.knowledge.types)).toBe(true);
  expect(Object.isFrozen(first.knowledge.contexts)).toBe(true);
  expect(Object.isFrozen(first.graph.searchHits)).toBe(true);
  expect(Object.isFrozen(first.graph.selectedTypes)).toBe(true);
  expect(Object.isFrozen(first.graph.pendingMerges)).toBe(true);

  act(() => {
    view.rerender(<Harness actorId={null} notebookId={null} />);
  });
  const second = value!;
  act(() => {
    view.rerender(<Harness actorId={null} notebookId={null} />);
  });
  const third = value!;

  for (const later of [second, third]) {
    expect(later.knowledge.types).toBe(first.knowledge.types);
    expect(later.knowledge.contexts).toBe(first.knowledge.contexts);
    expect(later.graph.searchHits).toBe(first.graph.searchHits);
    expect(later.graph.selectedTypes).toBe(first.graph.selectedTypes);
    expect(later.graph.pendingMerges).toBe(first.graph.pendingMerges);
  }
});
