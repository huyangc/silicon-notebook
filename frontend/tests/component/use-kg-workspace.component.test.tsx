import { act, cleanup, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import type { SchemaWriteOutcome } from "../../app/schema-manager.tsx";

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
  deleteKg: vi.fn(),
  fetchConceptDetail: vi.fn(),
  fetchKgDeleteStatus: vi.fn(),
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
  KG_DELETE_BUSY_MESSAGE,
  KG_DELETE_FAILED_MESSAGE,
  KG_DELETE_POLL_MAX_ATTEMPTS,
  KG_DELETE_RESULT_HOLD_MS,
  KG_DELETE_TIMEOUT_MESSAGE,
  KG_DELETE_UNKNOWN_MESSAGE,
  type KgDeleteRunStatus,
  type KgDeleteStatus,
} from "../../features/kg-maintenance/kg-delete-status";
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
const refreshAfterKgDelete = vi.fn<(notebookId: string, guard: () => boolean) => Promise<void>>();
const notify = vi.fn<(message: string) => void>();

const effects: HookOptions["effects"] = {
  notify,
  reportError: vi.fn(),
  refreshCollection: vi.fn().mockResolvedValue(undefined),
  refreshNotebook,
  refreshAfterKgDelete,
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

function deleteStatus(
  status: KgDeleteRunStatus,
  overrides: Partial<KgDeleteStatus> = {},
): KgDeleteStatus {
  return {
    job_id: "",
    notebook_id: "notebook-a",
    status,
    running: status === "running",
    objects_deleted: 0,
    relations_deleted: 0,
    ...overrides,
  };
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
  kgApi.fetchKgDeleteStatus.mockResolvedValue(deleteStatus("idle"));
  kgApi.deleteKg.mockResolvedValue({ status: "deleting", notebook_id: "notebook-a", job_id: "delete-a" });
  refreshAfterKgDelete.mockResolvedValue(undefined);
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

  let mutating!: Promise<SchemaWriteOutcome>;
  act(() => { mutating = value!.patchSchema("concept", { status: "active" }); });
  await waitFor(() => expect(knowledgeApi.updateNotebookObjectSchema).toHaveBeenCalledOnce());
  rerender(<Harness policy={{ ...writablePolicy, canManageNotebookSchemas: false }} />);
  await act(async () => pending.resolve());
  // 权限在写入在飞期间被撤走：既不重新拉清单、也不报成功。回执是 `unconfirmed` 而不是
  // `failed`——请求已经发出去了，这一格只是无从确认；说成失败会引着面板劝用户重试。
  expect(await mutating).toBe("unconfirmed");
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
    value!.startKgDelete("notebook-a");
    value!.startKgBuild();
    value!.updateKnowledge("knowledge-a", { status: "approved" });
  });
  expect(kgApi.reviewMerges).not.toHaveBeenCalled();
  expect(kgApi.reviewAllMerges).not.toHaveBeenCalled();
  expect(kgApi.relinkKg).not.toHaveBeenCalled();
  expect(kgApi.deleteKg).not.toHaveBeenCalled();
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

test("KG delete claims before POST, blocks sibling entries, then settles with a refreshed, notebook-scoped timed result", async () => {
  const { rerender } = render(<Harness />);
  await waitFor(() => expect(kgApi.fetchKgDeleteStatus).toHaveBeenCalledOnce());
  vi.useFakeTimers();
  const deletePending = deferred<Awaited<ReturnType<typeof kgApi.deleteKg>>>();
  kgApi.deleteKg.mockReturnValueOnce(deletePending.promise);

  // 确认弹窗打开时是另一本：点「确定」时绝不能删到当前这本上。
  await act(async () => value!.startKgDelete("notebook-b"));
  expect(kgApi.deleteKg).not.toHaveBeenCalled();
  expect(value!.graph.deleting).toBe(false);

  let deleting!: Promise<void>;
  act(() => { deleting = value!.startKgDelete("notebook-a"); });
  expect(kgApi.deleteKg).toHaveBeenCalledOnce();
  expect(value!.graph.deleting).toBe(true);

  // 任一忙碌位为真即忙：同一维护槽与整理入口在删除期间都不发请求。
  await act(async () => {
    await value!.startKgDelete("notebook-a");
    await value!.startRelink();
    await value!.startRebuild();
    await value!.startKgBuild();
  });
  expect(kgApi.deleteKg).toHaveBeenCalledOnce();
  expect(kgApi.relinkKg).not.toHaveBeenCalled();
  expect(kgApi.rebuildUnifiedKg).not.toHaveBeenCalled();
  expect(kgApi.buildKg).not.toHaveBeenCalled();

  await act(async () => deletePending.resolve({
    status: "deleting", notebook_id: "notebook-a", job_id: "delete-a",
  }));
  await deleting;
  expect(notify).toHaveBeenCalledWith("已开始删除知识图谱；完成后会自动更新");

  kgApi.fetchKgDeleteStatus
    .mockResolvedValueOnce(deleteStatus("running", { job_id: "delete-a" }))
    .mockResolvedValue(deleteStatus("succeeded", {
      job_id: "delete-a", objects_deleted: 12, relations_deleted: 30,
    }));
  await act(async () => { await vi.advanceTimersByTimeAsync(3_000); });
  expect(value!.graph.deleting).toBe(true);
  expect(value!.graph.deleteResult).toBeNull();
  expect(refreshAfterKgDelete).not.toHaveBeenCalled();

  await act(async () => { await vi.advanceTimersByTimeAsync(3_000); });
  expect(value!.graph.deleting).toBe(false);
  expect(value!.graph.deleteResult).toEqual({ tone: "success", text: "已删除 12 个知识对象" });
  expect(kgApi.fetchUnifiedGraph).toHaveBeenCalledWith("notebook-a", 80);
  expect(kgApi.fetchPendingMerges).toHaveBeenCalledWith("notebook-a");
  expect(refreshNotebook).toHaveBeenCalledWith("notebook-a", expect.any(Function));
  expect(refreshAfterKgDelete).toHaveBeenCalledWith("notebook-a", expect.any(Function));

  // 结果按笔记本分格：B 既看不到 A 的结果，也不被 A 的删除置灰。
  let transition!: ReturnType<HookValue["beginNotebookTransition"]>;
  act(() => { transition = value!.beginNotebookTransition(); });
  rerender(<Harness notebookId="notebook-b" />);
  act(() => value!.finishNotebookTransition(transition, notebook("notebook-b")));
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  expect(value!.graph.deleteResult).toBeNull();
  expect(value!.graph.deleting).toBe(false);

  act(() => { transition = value!.beginNotebookTransition(); });
  rerender(<Harness notebookId="notebook-a" />);
  act(() => value!.finishNotebookTransition(transition, notebook("notebook-a")));
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  expect(value!.graph.deleteResult?.text).toBe("已删除 12 个知识对象");

  // 结果按自己的计时器消失。
  await act(async () => { await vi.advanceTimersByTimeAsync(KG_DELETE_RESULT_HOLD_MS); });
  expect(value!.graph.deleteResult).toBeNull();
});

test("opening a notebook re-claims a running KG delete and reports that observed job's own result", async () => {
  vi.useFakeTimers();
  kgApi.fetchKgDeleteStatus
    .mockResolvedValueOnce(deleteStatus("running", { job_id: "delete-other-tab" }))
    .mockResolvedValue(deleteStatus("succeeded", { job_id: "delete-other-tab", objects_deleted: 7 }));
  render(<Harness />);
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  expect(value!.graph.deleting).toBe(true);
  await act(async () => value!.startRelink());
  expect(kgApi.relinkKg).not.toHaveBeenCalled();

  await act(async () => { await vi.advanceTimersByTimeAsync(3_000); });
  expect(value!.graph.deleting).toBe(false);
  expect(value!.graph.deleteResult).toEqual({ tone: "success", text: "已删除 7 个知识对象" });
});

test("a 409 delete whose adoption finds every kind idle re-claims before the retry POST and polls the retried job", async () => {
  render(<Harness />);
  await waitFor(() => expect(kgApi.fetchKgDeleteStatus).toHaveBeenCalledOnce());
  vi.useFakeTimers();
  kgApi.deleteKg
    .mockRejectedValueOnce(httpConflict())
    .mockResolvedValueOnce({ status: "deleting", notebook_id: "notebook-a", job_id: "delete-retry" });

  await act(async () => value!.startKgDelete("notebook-a"));
  expect(kgApi.deleteKg).toHaveBeenCalledTimes(2);
  expect(value!.graph.deleting).toBe(true);

  kgApi.fetchKgDeleteStatus.mockResolvedValue(deleteStatus("succeeded", {
    job_id: "delete-retry", objects_deleted: 4,
  }));
  await act(async () => { await vi.advanceTimersByTimeAsync(3_000); });
  expect(value!.graph.deleting).toBe(false);
  expect(value!.graph.deleteResult).toEqual({ tone: "success", text: "已删除 4 个知识对象" });
});

test("relink also re-claims its slot before a retry POST that follows an all-idle adoption", async () => {
  render(<Harness />);
  await waitFor(() => expect(kgApi.fetchRelinkStatus).toHaveBeenCalledOnce());
  kgApi.relinkKg
    .mockRejectedValueOnce(httpConflict())
    .mockResolvedValueOnce({ status: "running", notebook_id: "notebook-a", job_id: "relink-retry" });
  await act(async () => value!.startRelink());
  expect(kgApi.relinkKg).toHaveBeenCalledTimes(2);
  expect(value!.graph.relinking).toBe(true);
});

test("rebuild also re-claims its slot before a retry POST that follows an all-idle adoption", async () => {
  render(<Harness />);
  await waitFor(() => expect(kgApi.fetchUnifiedKgRebuildStatus).toHaveBeenCalledOnce());
  kgApi.rebuildUnifiedKg
    .mockRejectedValueOnce(httpConflict())
    .mockResolvedValueOnce({ status: "running", notebook_id: "notebook-a", job_id: "rebuild-retry" });
  await act(async () => value!.startRebuild());
  expect(kgApi.rebuildUnifiedKg).toHaveBeenCalledTimes(2);
  expect(value!.graph.rebuilding).toBe(true);
});

async function refusedDeleteWithUnknownAdoption() {
  render(<Harness />);
  await waitFor(() => expect(kgApi.fetchKgDeleteStatus).toHaveBeenCalledOnce());
  vi.useFakeTimers();
  kgApi.deleteKg.mockRejectedValueOnce(httpConflict());
  // 领养时删除状态探测偶发失败 → verdict unknown，本标签页什么都没开始、也没见过在跑的删除。
  kgApi.fetchKgDeleteStatus.mockRejectedValueOnce(new Error("probe down"));

  await act(async () => value!.startKgDelete("notebook-a"));
  expect(kgApi.deleteKg).toHaveBeenCalledOnce();
  expect(value!.graph.deleting).toBe(true);
  expect(value!.graph.deleteResult).toEqual({ tone: "neutral", text: KG_DELETE_BUSY_MESSAGE });
}

test("an unknown adoption verdict never reports a stale succeeded's counts or overwrites the 409 text, but refreshes because a delete did run", async () => {
  const consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
  await refusedDeleteWithUnknownAdoption();

  kgApi.fetchKgDeleteStatus.mockResolvedValue(deleteStatus("succeeded", {
    job_id: "delete-earlier-click", objects_deleted: 120,
  }));
  await act(async () => { await vi.advanceTimersByTimeAsync(3_000); });
  expect(value!.graph.deleting).toBe(false);
  expect(value!.graph.deleteResult).toEqual({ tone: "neutral", text: KG_DELETE_BUSY_MESSAGE });
  expect(notify.mock.calls.flat().some((message) => String(message).includes("已删除"))).toBe(false);
  // 非空 job_id 的终态证明确有一次删除跑完（例如另一个标签页）：图谱变过，照删除后的样子刷新。
  expect(kgApi.fetchUnifiedGraph).toHaveBeenCalledWith("notebook-a", 80);
  expect(refreshAfterKgDelete).toHaveBeenCalledWith("notebook-a", expect.any(Function));
  consoleError.mockRestore();
});

test("an unknown adoption verdict followed by idle is a pure release: no refresh, no result change", async () => {
  const consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
  await refusedDeleteWithUnknownAdoption();

  kgApi.fetchKgDeleteStatus.mockResolvedValue(deleteStatus("idle"));
  await act(async () => { await vi.advanceTimersByTimeAsync(3_000); });
  expect(value!.graph.deleting).toBe(false);
  expect(value!.graph.deleteResult).toEqual({ tone: "neutral", text: KG_DELETE_BUSY_MESSAGE });
  expect(refreshAfterKgDelete).not.toHaveBeenCalled();
  expect(kgApi.fetchUnifiedGraph).not.toHaveBeenCalled();
  expect(refreshNotebook).not.toHaveBeenCalled();
  consoleError.mockRestore();
});

test("the delete terminal refresh invalidates a slower in-flight range request so it cannot repaint the old graph", async () => {
  render(<Harness />);
  await waitFor(() => expect(kgApi.fetchKgDeleteStatus).toHaveBeenCalledOnce());
  await act(async () => value!.openGraph());
  vi.useFakeTimers();
  const staleGraph: UnifiedGraphResp = {
    nodes: [{ id: "deleted-node", object_type: "concept", payload: { name: "已删除" } }],
    edges: [],
  } as unknown as UnifiedGraphResp;
  const rangePending = deferred<UnifiedGraphResp>();
  kgApi.fetchUnifiedGraph.mockReturnValueOnce(rangePending.promise);
  let changing!: Promise<void>;
  act(() => { changing = value!.changeRange(0); });
  expect(value!.graph.rangeBusy).toBe(true);

  const freshGraph = graph();
  kgApi.fetchUnifiedGraph.mockResolvedValueOnce(freshGraph);
  kgApi.fetchKgDeleteStatus.mockResolvedValue(deleteStatus("succeeded", {
    job_id: "delete-a", objects_deleted: 1,
  }));
  await act(async () => value!.startKgDelete("notebook-a"));
  await act(async () => { await vi.advanceTimersByTimeAsync(3_000); });
  expect(value!.graph.deleting).toBe(false);
  expect(value!.graph.graph).toBe(freshGraph);
  expect(value!.graph.rangeBusy).toBe(false);

  await act(async () => rangePending.resolve(staleGraph));
  await changing;
  expect(value!.graph.graph).toBe(freshGraph);
  expect(value!.graph.rangeBusy).toBe(false);
});

test("re-entering Knowledge right after an invalidation reloads with the status filter the dropdown still shows", async () => {
  render(<Harness />);
  await act(async () => value!.enterKnowledge());
  await act(async () => { value!.selectKnowledgeStatus("draft"); });
  await waitFor(() => expect(knowledgeApi.listKnowledge).toHaveBeenLastCalledWith(
    "notebook-a", "concept", "draft", 0, 50,
  ));
  const listCalls = knowledgeApi.listKnowledge.mock.calls.length;

  // 与删除知识图谱后的依赖刷新同一顺序：同一拍里先作废、再重新进入。
  await act(async () => {
    value!.invalidateKnowledge();
    await value!.enterKnowledge();
  });
  expect(knowledgeApi.listKnowledge).toHaveBeenCalledTimes(listCalls + 1);
  expect(knowledgeApi.listKnowledge).toHaveBeenLastCalledWith("notebook-a", "concept", "draft", 0, 50);
  expect(value!.knowledge.statusFilter).toBe("draft");
});

test("a terminal status for a different job than the one submitted settles as unknown, never with its counts", async () => {
  render(<Harness />);
  await waitFor(() => expect(kgApi.fetchKgDeleteStatus).toHaveBeenCalledOnce());
  vi.useFakeTimers();
  await act(async () => value!.startKgDelete("notebook-a"));
  kgApi.fetchKgDeleteStatus.mockResolvedValue(deleteStatus("succeeded", {
    job_id: "delete-someone-else", objects_deleted: 50,
  }));

  await act(async () => { await vi.advanceTimersByTimeAsync(3_000); });
  expect(value!.graph.deleting).toBe(true);
  await act(async () => { await vi.advanceTimersByTimeAsync(3_000); });
  expect(value!.graph.deleting).toBe(false);
  expect(value!.graph.deleteResult).toEqual({ tone: "neutral", text: KG_DELETE_UNKNOWN_MESSAGE });
  expect(notify.mock.calls.flat().some((message) => String(message).includes("50"))).toBe(false);
  // 我们确实开始过一次删除，图谱可能已经变了：仍然刷新。
  expect(refreshAfterKgDelete).toHaveBeenCalledWith("notebook-a", expect.any(Function));
});

test("an idle status after our submitted delete (process restarted) settles as unknown and refreshes", async () => {
  render(<Harness />);
  await waitFor(() => expect(kgApi.fetchKgDeleteStatus).toHaveBeenCalledOnce());
  vi.useFakeTimers();
  await act(async () => value!.startKgDelete("notebook-a"));
  kgApi.fetchKgDeleteStatus.mockResolvedValue(deleteStatus("idle"));

  await act(async () => { await vi.advanceTimersByTimeAsync(6_000); });
  expect(value!.graph.deleting).toBe(false);
  expect(value!.graph.deleteResult).toEqual({ tone: "neutral", text: KG_DELETE_UNKNOWN_MESSAGE });
  expect(refreshAfterKgDelete).toHaveBeenCalledOnce();
});

test("a delete that never reaches a terminal status unlocks at the poll cap and says it may still be running", async () => {
  render(<Harness />);
  await waitFor(() => expect(kgApi.fetchKgDeleteStatus).toHaveBeenCalledOnce());
  vi.useFakeTimers();
  await act(async () => value!.startKgDelete("notebook-a"));
  kgApi.fetchKgDeleteStatus.mockResolvedValue(deleteStatus("running", { job_id: "delete-a" }));

  await act(async () => {
    await vi.advanceTimersByTimeAsync((KG_DELETE_POLL_MAX_ATTEMPTS + 1) * 3_000);
  });
  expect(value!.graph.deleting).toBe(false);
  expect(value!.graph.deleteResult).toEqual({ tone: "neutral", text: KG_DELETE_TIMEOUT_MESSAGE });
});

test("the delete result lands only after the refresh completes, and its timer is cleared on unmount", async () => {
  const { unmount } = render(<Harness />);
  await waitFor(() => expect(kgApi.fetchKgDeleteStatus).toHaveBeenCalledOnce());
  vi.useFakeTimers();
  await act(async () => value!.startKgDelete("notebook-a"));
  const graphPending = deferred<UnifiedGraphResp>();
  kgApi.fetchUnifiedGraph.mockReturnValueOnce(graphPending.promise);
  kgApi.fetchKgDeleteStatus.mockResolvedValue(deleteStatus("succeeded", {
    job_id: "delete-a", objects_deleted: 2,
  }));

  await act(async () => { await vi.advanceTimersByTimeAsync(3_000); });
  expect(kgApi.fetchUnifiedGraph).toHaveBeenCalledWith("notebook-a", 80);
  expect(value!.graph.deleting).toBe(true);
  expect(value!.graph.deleteResult).toBeNull();
  expect(notify).not.toHaveBeenCalledWith("已删除 2 个知识对象");

  await act(async () => graphPending.resolve(graph()));
  expect(notify).toHaveBeenCalledWith("已删除 2 个知识对象");
  expect(value!.graph.deleteResult).toEqual({ tone: "success", text: "已删除 2 个知识对象" });
  expect(value!.graph.deleting).toBe(false);

  // 此刻挂着的计时器里有结果那一格的保留计时器；卸载必须把它撤掉，不能留一个到点还去
  // 改已卸载组件状态的回调。
  const pendingTimers = vi.getTimerCount();
  expect(pendingTimers).toBeGreaterThan(0);
  unmount();
  expect(vi.getTimerCount()).toBeLessThan(pendingTimers);
});

test("the delete terminal refresh clears search hits, an in-flight search, and the selected node", async () => {
  render(<Harness />);
  await waitFor(() => expect(kgApi.fetchKgDeleteStatus).toHaveBeenCalledOnce());
  await act(async () => value!.openGraph());
  vi.useFakeTimers();
  await act(async () => value!.selectNode("node-a"));
  expect(value!.graph.selectedNodeId).toBe("node-a");

  const hit = { object_id: "node-a", name: "A", object_type: "concept", score: 1, match: "A" };
  kgApi.fetchKgSearch.mockResolvedValueOnce({ query: "A", hits: [hit] });
  act(() => value!.updateGraphSearch("A"));
  await act(async () => { await vi.advanceTimersByTimeAsync(300); });
  expect(value!.graph.searchHits).toHaveLength(1);

  const lateSearch = deferred<{ query: string; hits: typeof hit[] }>();
  kgApi.fetchKgSearch.mockReturnValueOnce(lateSearch.promise);
  act(() => value!.updateGraphSearch("AB"));
  await act(async () => { await vi.advanceTimersByTimeAsync(300); });
  expect(value!.graph.searchBusy).toBe(true);

  kgApi.fetchKgDeleteStatus.mockResolvedValue(deleteStatus("succeeded", {
    job_id: "delete-a", objects_deleted: 3,
  }));
  await act(async () => value!.startKgDelete("notebook-a"));
  await act(async () => { await vi.advanceTimersByTimeAsync(3_000); });
  expect(value!.graph.deleting).toBe(false);
  expect(value!.graph.search).toBe("");
  expect(value!.graph.searchHits).toEqual([]);
  expect(value!.graph.searchBusy).toBe(false);
  expect(value!.graph.selectedNodeId).toBeNull();
  expect(value!.graph.nodeContext).toBeNull();

  await act(async () => lateSearch.resolve({ query: "AB", hits: [hit] }));
  expect(value!.graph.searchHits).toEqual([]);
});

test("review commands refuse while a delete runs, and a delete refuses while a review is in flight", async () => {
  render(<Harness />);
  await waitFor(() => expect(kgApi.fetchKgDeleteStatus).toHaveBeenCalledOnce());
  const consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
  const deletePending = deferred<Awaited<ReturnType<typeof kgApi.deleteKg>>>();
  kgApi.deleteKg.mockReturnValueOnce(deletePending.promise);
  let deleting!: Promise<void>;
  act(() => { deleting = value!.startKgDelete("notebook-a"); });
  await act(async () => {
    await value!.reviewPendingMerges();
    await value!.reviewAllMerges();
  });
  expect(kgApi.reviewMerges).not.toHaveBeenCalled();
  expect(kgApi.reviewAllMerges).not.toHaveBeenCalled();
  await act(async () => deletePending.reject(new Error("network down")));
  await deleting;
  expect(value!.graph.deleting).toBe(false);

  const reviewPending = deferred<Awaited<ReturnType<typeof kgApi.reviewMerges>>>();
  kgApi.reviewMerges.mockReturnValueOnce(reviewPending.promise);
  let reviewing!: Promise<void>;
  act(() => { reviewing = value!.reviewPendingMerges(); });
  expect(value!.graph.reviewBusy).toBe(true);
  await act(async () => value!.startKgDelete("notebook-a"));
  expect(kgApi.deleteKg).toHaveBeenCalledTimes(1);
  await act(async () => reviewPending.resolve({ reviewed: 0, confirmed: 0, rejected: 0, unsure: 0 }));
  await reviewing;
  consoleError.mockRestore();
});

test("a 409 delete adopts the running relink and explains the refusal beside the button", async () => {
  render(<Harness />);
  await waitFor(() => expect(kgApi.fetchKgDeleteStatus).toHaveBeenCalledOnce());
  // 夹具的 409 不是后端 user_error 的可展示文案，兜底路径会打一条诊断——静音它。
  const consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
  kgApi.deleteKg.mockRejectedValueOnce(httpConflict());
  kgApi.fetchRelinkStatus.mockResolvedValueOnce({
    job_id: "relink-other-tab",
    notebook_id: "notebook-a",
    status: "running",
    running: true,
    isolated_before: 0,
    edges_added: 0,
    isolated_after: 0,
  });

  await act(async () => value!.startKgDelete("notebook-a"));
  expect(kgApi.deleteKg).toHaveBeenCalledOnce();
  expect(value!.graph.deleting).toBe(false);
  expect(value!.graph.relinking).toBe(true);
  expect(value!.graph.deleteResult).toEqual({ tone: "neutral", text: KG_DELETE_BUSY_MESSAGE });
  expect(effects.reportError).not.toHaveBeenCalled();
  consoleError.mockRestore();
});

test("a failed delete POST reports, lands a failure beside the button, and releases authority", async () => {
  render(<Harness />);
  await waitFor(() => expect(kgApi.fetchKgDeleteStatus).toHaveBeenCalledOnce());
  const consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
  kgApi.deleteKg.mockRejectedValueOnce(new Error("network down"));

  await act(async () => value!.startKgDelete("notebook-a"));
  expect(effects.reportError).toHaveBeenCalledOnce();
  expect(value!.graph.deleting).toBe(false);
  expect(value!.graph.deleteResult).toEqual({ tone: "failed", text: KG_DELETE_FAILED_MESSAGE });

  await act(async () => value!.startKgDelete("notebook-a"));
  expect(kgApi.deleteKg).toHaveBeenCalledTimes(2);
  // 新的一次认领先让出同一格的旧结果。
  expect(value!.graph.deleteResult).toBeNull();
  consoleError.mockRestore();
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
