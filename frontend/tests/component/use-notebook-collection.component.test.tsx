import { act, cleanup, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import type { NotebookSummary } from "../../app/workspace-model";

const notebookApi = vi.hoisted(() => ({
  createNotebook: vi.fn(),
  deleteNotebook: vi.fn(),
  fetchNotebookIndexingPipeline: vi.fn(),
  getNotebook: vi.fn(),
  listNotebooks: vi.fn(),
  setNotebookIndexingPipeline: vi.fn(),
  updateNotebook: vi.fn(),
}));
const basesApi = vi.hoisted(() => ({
  listBases: vi.fn(),
  listMountable: vi.fn(),
  mountedByCount: vi.fn(),
  setBases: vi.fn(),
}));
const searchApi = vi.hoisted(() => ({ searchNotebooksBounded: vi.fn() }));

vi.mock("../../app/notebook-api.ts", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../app/notebook-api.ts")>()),
  ...notebookApi,
}));
vi.mock("../../app/notebook-bases.ts", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../app/notebook-bases.ts")>()),
  ...basesApi,
}));
vi.mock("../../app/collection-search.ts", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../app/collection-search.ts")>()),
  ...searchApi,
}));

import { useNotebookCollection } from "../../app/use-notebook-collection";

type HookValue = ReturnType<typeof useNotebookCollection>;
type HookOptions = Parameters<typeof useNotebookCollection>[0];

function notebook(
  id: string,
  access: "owner" | "reader" = "owner",
  canManageContent = false,
): NotebookSummary {
  return {
    id,
    name: id,
    purpose: "",
    primary_domain: "",
    status: "ready",
    counts: { sources: 0 },
    created_label: "8月22日",
    access,
    can_manage_content: canManageContent,
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function indexingProjection(overrides: Record<string, unknown> = {}) {
  return {
    pipeline_id: null,
    version: "builtin-v1",
    available: true,
    missing: false,
    pending: false,
    options: [
      {
        pipeline_id: null,
        label: "内建管线",
        description: "builtin",
        version: "builtin-v1",
        available: true,
        selected: true,
      },
      {
        pipeline_id: "plugin.arxiv",
        label: "arXiv 管线",
        description: "plugin",
        version: "2026.08",
        available: true,
        selected: false,
      },
    ],
    changed: false,
    warning_count: 0,
    rebuild_status: "idle",
    job_id: null,
    ...overrides,
  };
}

const effects: HookOptions["effects"] = {
  reportError: vi.fn(),
  notify: vi.fn(),
  refreshComposite: vi.fn(async () => undefined),
  onNotebookCreated: vi.fn(async () => undefined),
  onNotebookUpdated: vi.fn(),
  onNotebookDeleted: vi.fn(),
  captureNavigationEpoch: vi.fn(() => 1),
  reconcileAccess: vi.fn(async () => undefined),
};

let value: HookValue | null = null;

function Harness({ actorId = "user-a" }: { actorId?: string | null }) {
  value = useNotebookCollection({ actorId, effects });
  return <div>{value.rows.map((row) => row.id).join(",")}</div>;
}

function publish(rows: NotebookSummary[]) {
  const read = value!.beginListRead();
  expect(read).not.toBeNull();
  act(() => { value!.commitListSnapshot(read, rows); });
}

// refreshCompositeAfterCommit 刻意吞掉刷新失败——写入已经落库，不能被重分类成失败——
// 但那条 catch 同时盖住了刷新自己的后半段状态写入，所以它必须留下可观测记录。用这个
// 助手包住「故意让刷新失败」的用例：既钉住那条记录，也让测试输出不被真实 stderr 淹没。
const installedSpies: Array<{ mockRestore: () => void }> = [];

function captureRefreshFailureLog() {
  const spy = vi.spyOn(console, "error").mockImplementation(() => {});
  installedSpies.push(spy);
  return spy;
}

// 记录必须过 logDiagnostic —— 它是「未翻译诊断」的唯一截断/压平出口（AGENTS.md：
// 异常原文与私有路径不许进日志）。断言钉的是那条出口的形态：带 tag 的前缀 + 被压平
// 成一行字符串的诊断，而不是原始 Error 对象。
function expectRefreshFailureLogged(spy: ReturnType<typeof captureRefreshFailureLog>) {
  expect(spy).toHaveBeenCalledWith(
    expect.stringContaining("[collection-refresh]"),
    "Error: refresh failed",
  );
}

const editorPatch = {
  name: "renamed",
  purpose: "",
  primary_domain: "",
  target_users: "",
  access_scope: "",
  expected_questions: [],
  source_types: [],
  taxonomy: [],
};

beforeEach(() => {
  value = null;
  vi.clearAllMocks();
  notebookApi.listNotebooks.mockResolvedValue([]);
  notebookApi.createNotebook.mockResolvedValue(notebook("created"));
  notebookApi.updateNotebook.mockResolvedValue(notebook("a"));
  notebookApi.getNotebook.mockResolvedValue(notebook("a"));
  notebookApi.fetchNotebookIndexingPipeline.mockResolvedValue(indexingProjection());
  notebookApi.setNotebookIndexingPipeline.mockResolvedValue(indexingProjection({
    pipeline_id: "plugin.arxiv",
    version: "2026.08",
    pending: true,
    changed: true,
    rebuild_status: "pending",
    job_id: "job-1",
  }));
  notebookApi.deleteNotebook.mockResolvedValue(undefined);
  basesApi.listMountable.mockResolvedValue([]);
  basesApi.listBases.mockResolvedValue([]);
  basesApi.mountedByCount.mockResolvedValue({ count: 0 });
  basesApi.setBases.mockResolvedValue([]);
  searchApi.searchNotebooksBounded.mockResolvedValue({});
});

afterEach(() => {
  while (installedSpies.length) installedSpies.pop()!.mockRestore();
  vi.useRealTimers();
  cleanup();
});

test("published watermark admits an older success when a newer read has not published", () => {
  render(<Harness />);
  const older = value!.beginListRead();
  const newer = value!.beginListRead();
  expect(older && newer).toBeTruthy();

  act(() => { expect(value!.commitListSnapshot(older, [notebook("older")])).toBe(true); });
  expect(value!.rows.map((row) => row.id)).toEqual(["older"]);
  act(() => { expect(value!.commitListSnapshot(newer, [notebook("newer")])).toBe(true); });
  expect(value!.rows.map((row) => row.id)).toEqual(["newer"]);
  act(() => { expect(value!.commitListSnapshot(older, [notebook("stale")])).toBe(false); });
});

test("actor replacement synchronously hides rows and rejects the old list ticket", () => {
  const view = render(<Harness />);
  const publishedRead = value!.beginListRead();
  act(() => { value!.commitListSnapshot(publishedRead, [notebook("private-a")]); });
  expect(value!.rows.map((row) => row.id)).toEqual(["private-a"]);
  const unpublishedOldActorRead = value!.beginListRead();

  view.rerender(<Harness actorId="user-b" />);
  expect(value!.rows).toEqual([]);
  act(() => {
    expect(value!.commitListSnapshot(unpublishedOldActorRead, [notebook("late-a")])).toBe(false);
  });
  expect(value!.rows).toEqual([]);
});

test("explicit logout detach cannot be rebound by the still-published actor prop", async () => {
  render(<Harness />);
  publish([notebook("private-a")]);
  expect(value!.rows.map((row) => row.id)).toEqual(["private-a"]);

  act(() => value!.leaveActor());

  expect(value!.rows).toEqual([]);
  expect(value!.beginListRead()).toBeNull();
  await expect(value!.renameNotebook("private-a", "late")).resolves.toBeNull();
  expect(notebookApi.updateNotebook).not.toHaveBeenCalled();
});

test("search waits 250ms and a replaced actor cannot publish deferred private hits", async () => {
  vi.useFakeTimers();
  const pending = deferred<Record<string, Array<{ scope: string; notebook_id: string; label: string; text: string }>>>();
  searchApi.searchNotebooksBounded.mockReturnValueOnce(pending.promise);
  const view = render(<Harness />);
  publish([notebook("a")]);
  act(() => value!.updateSearchQuery("needle"));

  act(() => { vi.advanceTimersByTime(249); });
  expect(searchApi.searchNotebooksBounded).not.toHaveBeenCalled();
  await act(async () => { vi.advanceTimersByTime(1); });
  expect(searchApi.searchNotebooksBounded).toHaveBeenCalledTimes(1);
  const oldSearchSignal = searchApi.searchNotebooksBounded.mock.calls[0]?.[2] as AbortSignal;
  expect(oldSearchSignal.aborted).toBe(false);

  view.rerender(<Harness actorId="user-b" />);
  expect(oldSearchSignal.aborted).toBe(true);
  await act(async () => pending.resolve({
    a: [{ scope: "Source", notebook_id: "a", label: "secret", text: "secret" }],
  }));
  expect(value!.searchHits).toEqual({});
});

test("a newer rows generation wins when the same search query resolves out of order", async () => {
  vi.useFakeTimers();
  const older = deferred<Record<string, Array<{ scope: string; notebook_id: string; label: string; text: string }>>>();
  const newer = deferred<Record<string, Array<{ scope: string; notebook_id: string; label: string; text: string }>>>();
  searchApi.searchNotebooksBounded
    .mockReturnValueOnce(older.promise)
    .mockReturnValueOnce(newer.promise);
  render(<Harness />);
  publish([notebook("a")]);
  act(() => value!.updateSearchQuery("same-query"));
  await act(async () => { vi.advanceTimersByTime(250); });
  expect(searchApi.searchNotebooksBounded).toHaveBeenCalledTimes(1);

  publish([notebook("a"), notebook("b")]);
  await act(async () => { vi.advanceTimersByTime(250); });
  expect(searchApi.searchNotebooksBounded).toHaveBeenCalledTimes(2);
  await act(async () => newer.resolve({
    b: [{ scope: "Source", notebook_id: "b", label: "new", text: "new" }],
  }));
  expect(Object.keys(value!.searchHits)).toEqual(["b"]);

  await act(async () => older.resolve({
    a: [{ scope: "Source", notebook_id: "a", label: "old", text: "old" }],
  }));
  expect(Object.keys(value!.searchHits)).toEqual(["b"]);
});

test("default creation is single-flight and preserves create then refresh then open order", async () => {
  const pending = deferred<NotebookSummary>();
  const order: string[] = [];
  notebookApi.createNotebook.mockImplementationOnce(async () => {
    order.push("create");
    return pending.promise;
  });
  vi.mocked(effects.refreshComposite).mockImplementationOnce(async () => { order.push("refresh"); });
  vi.mocked(effects.onNotebookCreated).mockImplementationOnce(async () => { order.push("open"); });
  render(<Harness />);

  let first!: Promise<void>;
  act(() => {
    first = value!.createDefaultNotebook();
    void value!.createDefaultNotebook();
  });
  expect(notebookApi.createNotebook).toHaveBeenCalledTimes(1);
  await act(async () => pending.resolve(notebook("created")));
  await first;
  expect(order).toEqual(["create", "refresh", "open"]);

  await act(async () => value!.createDefaultNotebook());
  expect(notebookApi.createNotebook).toHaveBeenCalledTimes(2);
});

test("default creation releases its single-flight authority after failure", async () => {
  notebookApi.createNotebook
    .mockRejectedValueOnce(new Error("create failed"))
    .mockResolvedValueOnce(notebook("retry-created"));
  render(<Harness />);

  await act(async () => value!.createDefaultNotebook());
  expect(effects.reportError).toHaveBeenCalledTimes(1);
  await act(async () => value!.createDefaultNotebook());

  expect(notebookApi.createNotebook).toHaveBeenCalledTimes(2);
  expect(effects.onNotebookCreated).toHaveBeenCalledTimes(1);
});

test("a committed creation still opens and reports success when collection refresh fails", async () => {
  const logged = captureRefreshFailureLog();
  vi.mocked(effects.refreshComposite).mockRejectedValueOnce(new Error("refresh failed"));
  render(<Harness />);

  await act(async () => value!.createDefaultNotebook());

  expect(notebookApi.createNotebook).toHaveBeenCalledTimes(1);
  expect(effects.onNotebookCreated).toHaveBeenCalledWith(expect.objectContaining({ id: "created" }));
  expect(effects.reportError).not.toHaveBeenCalled();
  expect(effects.notify).toHaveBeenCalledWith(
    "笔记本已创建，但列表暂未刷新；请稍后刷新页面。",
  );
  expectRefreshFailureLogged(logged);
});

test("a creation that neither refreshed nor opened does not send the user to a list without it", async () => {
  captureRefreshFailureLog();
  const openFailure = new Error("open failed");
  vi.mocked(effects.refreshComposite).mockRejectedValueOnce(new Error("refresh failed"));
  vi.mocked(effects.onNotebookCreated).mockRejectedValueOnce(openFailure);
  render(<Harness />);

  await act(async () => value!.createDefaultNotebook());

  expect(effects.reportError).toHaveBeenCalledWith(openFailure);
  expect(effects.notify).toHaveBeenCalledWith(
    "笔记本已创建，但暂时没能打开、列表也暂未刷新；请稍后刷新页面。",
  );
});

test("a creation that refreshed but failed to open still points at the list", async () => {
  const openFailure = new Error("open failed");
  vi.mocked(effects.onNotebookCreated).mockRejectedValueOnce(openFailure);
  render(<Harness />);

  await act(async () => value!.createDefaultNotebook());

  expect(effects.reportError).toHaveBeenCalledWith(openFailure);
  expect(effects.notify).toHaveBeenCalledWith(
    "笔记本已创建，但暂时没能打开；请从列表重新打开。",
  );
});

test("group administrators retain PATCH-only rename and can open settings without owner-only reference I/O", async () => {
  const renamed = notebook("shared", "reader", true);
  renamed.name = "renamed";
  notebookApi.updateNotebook.mockResolvedValueOnce(renamed);
  render(<Harness />);
  publish([notebook("shared", "reader", true)]);

  let result: NotebookSummary | null = null;
  await act(async () => {
    result = await value!.renameNotebook("shared", "renamed");
  });

  expect(notebookApi.updateNotebook).toHaveBeenCalledTimes(1);
  expect(notebookApi.updateNotebook).toHaveBeenCalledWith("shared", { name: "renamed" });
  expect((result as NotebookSummary | null)?.name).toBe("renamed");
  await act(async () => value!.openEditor("shared"));
  expect(value!.editor?.target.id).toBe("shared");
  expect(notebookApi.fetchNotebookIndexingPipeline).toHaveBeenCalledWith("shared");
  expect(basesApi.listMountable).not.toHaveBeenCalled();
  expect(basesApi.listBases).not.toHaveBeenCalled();
  await act(async () => value!.openDelete("shared"));
  expect(value!.deletion).toBeNull();
  expect(basesApi.mountedByCount).not.toHaveBeenCalled();
});

test("one-click revert to builtin uses only indexing pipeline APIs", async () => {
  notebookApi.fetchNotebookIndexingPipeline.mockResolvedValueOnce(indexingProjection({
    pipeline_id: "plugin.missing",
    version: "2026.08",
    available: false,
    missing: true,
    options: [
      {
        pipeline_id: null,
        label: "内建管线",
        description: "builtin",
        version: "builtin-v1",
        available: true,
        selected: false,
      },
      {
        pipeline_id: "plugin.missing",
        label: "已停用的索引管线",
        description: "missing",
        version: "2026.08",
        available: false,
        selected: true,
      },
    ],
  }));
  notebookApi.setNotebookIndexingPipeline.mockResolvedValueOnce(indexingProjection({
    pipeline_id: null,
    version: "builtin-v1",
    available: true,
    missing: false,
    pending: true,
    changed: true,
    rebuild_status: "pending",
    job_id: "job-built-in",
  }));
  render(<Harness />);
  publish([notebook("shared", "reader", true)]);

  await act(async () => value!.openEditor("shared"));
  await act(async () => value!.revertIndexingPipelineToBuiltin());

  expect(notebookApi.setNotebookIndexingPipeline).toHaveBeenCalledWith("shared", null);
  expect(basesApi.listMountable).not.toHaveBeenCalled();
  expect(basesApi.listBases).not.toHaveBeenCalled();
});

test("failed indexing rebuild can retry the same selected pipeline", async () => {
  notebookApi.fetchNotebookIndexingPipeline.mockResolvedValueOnce(indexingProjection({
    pipeline_id: "plugin.arxiv",
    version: "2026.08",
    available: true,
    missing: false,
    pending: true,
    rebuild_status: "failed",
  }));
  notebookApi.setNotebookIndexingPipeline.mockResolvedValueOnce(indexingProjection({
    pipeline_id: "plugin.arxiv",
    version: "2026.08",
    available: true,
    missing: false,
    pending: true,
    changed: true,
    rebuild_status: "pending",
    job_id: "job-retry",
  }));
  render(<Harness />);
  publish([notebook("shared", "reader", true)]);

  await act(async () => value!.openEditor("shared"));
  await act(async () => value!.retryIndexingPipelineRebuild());

  expect(notebookApi.setNotebookIndexingPipeline).toHaveBeenCalledWith(
    "shared",
    "plugin.arxiv",
  );
});

test("an explicit permission downgrade stops indexing restart before detail refresh", async () => {
  const restart = deferred<ReturnType<typeof indexingProjection>>();
  notebookApi.setNotebookIndexingPipeline.mockReturnValueOnce(restart.promise);
  render(<Harness />);
  publish([notebook("shared", "reader", true)]);
  await act(async () => value!.openEditor("shared"));

  let restarting!: Promise<void>;
  act(() => { restarting = value!.revertIndexingPipelineToBuiltin(); });
  publish([notebook("shared", "reader", false)]);
  await act(async () => restart.resolve(indexingProjection({ changed: true })));
  await restarting;

  expect(notebookApi.getNotebook).not.toHaveBeenCalled();
  expect(effects.notify).toHaveBeenCalledWith(
    "权限已变更，已停止继续操作；此前已提交的修改不会撤销。",
  );
  expect(value!.editor).toBeNull();
});

test("rename reports a committed write even when the refreshed list loses manage access", async () => {
  const renamed = notebook("shared");
  renamed.name = "renamed";
  renamed.shared_from = "旧授权";
  renamed.is_shared = false;
  renamed.granted_via = [{ group_id: "old", group_name: "旧群组", kind: "team" }];
  renamed.document_limit = 321;
  notebookApi.updateNotebook.mockResolvedValueOnce(renamed);
  render(<Harness />);
  publish([notebook("shared", "reader", true)]);
  vi.mocked(effects.refreshComposite).mockImplementationOnce(async () => {
    const refreshed = notebook("shared", "reader", false);
    refreshed.shared_from = "新授权";
    refreshed.is_shared = true;
    refreshed.granted_via = [{ group_id: "new", group_name: "新群组", kind: "team" }];
    refreshed.document_limit = 0;
    publish([refreshed]);
  });

  let result: NotebookSummary | null = renamed;
  await act(async () => {
    result = await value!.renameNotebook("shared", "renamed");
  });

  expect(notebookApi.updateNotebook).toHaveBeenCalledTimes(1);
  expect(result).toMatchObject({
    name: "renamed",
    access: "reader",
    shared_from: "新授权",
    is_shared: true,
    granted_via: [{ group_id: "new", group_name: "新群组", kind: "team" }],
    can_manage_content: false,
    document_limit: 321,
  });
  expect(effects.notify).toHaveBeenCalledWith("笔记本名称已更新");
});

test("rename does not return stale detail after a successful refresh removes the notebook", async () => {
  const renamed = notebook("shared", "reader", true);
  renamed.name = "renamed";
  notebookApi.updateNotebook.mockResolvedValueOnce(renamed);
  render(<Harness />);
  publish([notebook("shared", "reader", true)]);
  vi.mocked(effects.refreshComposite).mockImplementationOnce(async () => { publish([]); });

  let result: NotebookSummary | null = renamed;
  await act(async () => {
    result = await value!.renameNotebook("shared", "renamed");
  });

  expect(result).toBeNull();
  expect(effects.notify).toHaveBeenCalledWith("笔记本名称已更新");
});

test("rename returns its committed detail when collection refresh fails", async () => {
  const logged = captureRefreshFailureLog();
  const renamed = notebook("a");
  renamed.name = "renamed";
  notebookApi.updateNotebook.mockResolvedValueOnce(renamed);
  vi.mocked(effects.refreshComposite).mockRejectedValueOnce(new Error("refresh failed"));
  render(<Harness />);
  publish([notebook("a")]);

  let result: NotebookSummary | null = null;
  await act(async () => { result = await value!.renameNotebook("a", "renamed"); });

  expect((result as NotebookSummary | null)?.name).toBe("renamed");
  expect(effects.notify).toHaveBeenCalledWith(
    "笔记本名称已更新，但列表暂未刷新；请稍后刷新页面。",
  );
  expectRefreshFailureLogged(logged);
});

test("rename is single-flight and releases its key after both success and failure", async () => {
  const first = deferred<NotebookSummary>();
  notebookApi.updateNotebook
    .mockReturnValueOnce(first.promise)
    .mockRejectedValueOnce(new Error("rename failed"))
    .mockResolvedValueOnce(notebook("a"));
  render(<Harness />);
  publish([notebook("a")]);

  let firstRename!: Promise<NotebookSummary | null>;
  act(() => {
    firstRename = value!.renameNotebook("a", "one");
    void value!.renameNotebook("a", "duplicate");
  });
  expect(notebookApi.updateNotebook).toHaveBeenCalledTimes(1);
  await act(async () => first.resolve(notebook("a")));
  await firstRename;

  await expect(value!.renameNotebook("a", "two")).rejects.toThrow("rename failed");
  await act(async () => value!.renameNotebook("a", "three"));
  expect(notebookApi.updateNotebook).toHaveBeenCalledTimes(3);
});

test("editor open is latest-wins and an explicit permission downgrade survives later omission", async () => {
  const mountableA = deferred<never[]>();
  const basesA = deferred<never[]>();
  const pipelineA = deferred<ReturnType<typeof indexingProjection>>();
  basesApi.listMountable
    .mockReturnValueOnce(mountableA.promise)
    .mockResolvedValueOnce([]);
  basesApi.listBases
    .mockReturnValueOnce(basesA.promise)
    .mockResolvedValueOnce([]);
  notebookApi.fetchNotebookIndexingPipeline
    .mockReturnValueOnce(pipelineA.promise)
    .mockResolvedValueOnce(indexingProjection());
  render(<Harness />);
  publish([notebook("a"), notebook("b")]);

  let openingA!: Promise<boolean>;
  act(() => { openingA = value!.openEditor("a"); });
  await act(async () => value!.openEditor("b"));
  expect(value!.editor?.target.id).toBe("b");
  await act(async () => {
    pipelineA.resolve(indexingProjection());
    mountableA.resolve([]);
    basesA.resolve([]);
  });
  await openingA;
  expect(value!.editor?.target.id).toBe("b");

  const update = deferred<NotebookSummary>();
  notebookApi.updateNotebook.mockReturnValueOnce(update.promise);
  let saving!: Promise<void>;
  act(() => { saving = value!.saveEditor({ ...editorPatch, name: "b" }); });
  publish([notebook("b", "reader")]);
  expect(value!.editor?.busy).toBe(true);
  publish([]);
  expect(value!.editor?.busy).toBe(true);
  await act(async () => update.resolve(notebook("b")));
  await saving;
  expect(basesApi.setBases).not.toHaveBeenCalled();
  expect(notebookApi.getNotebook).not.toHaveBeenCalled();
  expect(effects.onNotebookUpdated).not.toHaveBeenCalled();
  expect(effects.notify).toHaveBeenCalledWith(
    "权限已变更，已停止继续操作；此前已提交的修改不会撤销。",
  );
  expect(value!.editor).toBeNull();
});

test("a reauthorized editor can retry after the denied attempt itself fails", async () => {
  const firstUpdate = deferred<NotebookSummary>();
  notebookApi.updateNotebook.mockReturnValueOnce(firstUpdate.promise);
  render(<Harness />);
  publish([notebook("a")]);
  await act(async () => value!.openEditor("a"));

  let firstSave!: Promise<void>;
  act(() => { firstSave = value!.saveEditor(editorPatch); });
  publish([notebook("a", "reader")]);
  publish([notebook("a")]);
  await act(async () => firstUpdate.reject(new Error("first PATCH failed")));
  await firstSave;
  expect(value!.editor?.busy).toBe(false);

  await act(async () => value!.saveEditor(editorPatch));

  expect(notebookApi.updateNotebook).toHaveBeenCalledTimes(2);
  expect(basesApi.setBases).toHaveBeenCalledTimes(1);
  expect(notebookApi.getNotebook).toHaveBeenCalledTimes(1);
  expect(effects.onNotebookUpdated).toHaveBeenCalledTimes(1);
});

test("a transient list omission while final detail is pending does not cancel save", async () => {
  const detail = deferred<NotebookSummary>();
  notebookApi.getNotebook.mockReturnValueOnce(detail.promise);
  render(<Harness />);
  publish([notebook("a")]);
  await act(async () => value!.openEditor("a"));
  expect(value!.editor?.target.id).toBe("a");

  let saving!: Promise<void>;
  act(() => {
    saving = value!.saveEditor(editorPatch);
  });
  await waitFor(() => expect(notebookApi.getNotebook).toHaveBeenCalledTimes(1));
  publish([]);
  expect(value!.editor?.busy).toBe(true);
  publish([notebook("a")]);
  expect(value!.editor?.busy).toBe(true);

  await act(async () => detail.resolve(notebook("a")));
  await saving;
  expect(effects.onNotebookUpdated).toHaveBeenCalledTimes(1);
  expect(effects.notify).toHaveBeenCalledWith("笔记本信息已更新");
});

test("a backend rejection after a transient list omission is reported instead of swallowed", async () => {
  const update = deferred<NotebookSummary>();
  const rejection = new Error("permission changed");
  notebookApi.updateNotebook.mockReturnValueOnce(update.promise);
  basesApi.setBases.mockRejectedValueOnce(rejection);
  render(<Harness />);
  publish([notebook("a")]);
  await act(async () => value!.openEditor("a"));

  let saving!: Promise<void>;
  act(() => { saving = value!.saveEditor(editorPatch); });
  publish([]);
  expect(value!.editor?.busy).toBe(true);
  await act(async () => update.resolve(notebook("a")));
  await saving;

  expect(effects.reportError).toHaveBeenCalledWith(rejection);
  expect(notebookApi.getNotebook).not.toHaveBeenCalled();
  expect(effects.notify).not.toHaveBeenCalled();
});

test("a committed editor save is not reclassified as failed when collection refresh fails", async () => {
  const logged = captureRefreshFailureLog();
  vi.mocked(effects.refreshComposite).mockRejectedValueOnce(new Error("refresh failed"));
  render(<Harness />);
  publish([notebook("a")]);
  await act(async () => value!.openEditor("a"));

  await act(async () => value!.saveEditor(editorPatch));

  expect(effects.onNotebookUpdated).toHaveBeenCalledTimes(1);
  expect(effects.reportError).not.toHaveBeenCalled();
  expect(effects.notify).toHaveBeenCalledWith(
    "笔记本信息已更新，但列表暂未刷新；请稍后刷新页面。",
  );
  expectRefreshFailureLogged(logged);
});

test("dismissing settings during a save stops waiting and says so", async () => {
  const stuck = deferred<NotebookSummary>();
  notebookApi.updateNotebook.mockReturnValueOnce(stuck.promise);
  render(<Harness />);
  publish([notebook("a")]);
  await act(async () => value!.openEditor("a"));

  let saving!: Promise<void>;
  act(() => { saving = value!.saveEditor(editorPatch); });
  expect(value!.editor?.busy).toBe(true);

  // 弹窗不接 Escape 也不接遮罩点击，关闭入口是保存期间唯一的出口。
  act(() => { value!.closeEditor(); });
  expect(value!.editor).toBeNull();
  expect(effects.notify).toHaveBeenCalledWith(
    "已停止等待保存结果；此前已提交的修改不会撤销，未提交的部分不会继续。",
  );

  // 剩下的步骤停在下一个检查点：不再打 PUT bases / GET，也不再改界面。
  await act(async () => stuck.resolve(notebook("a")));
  await saving;
  expect(basesApi.setBases).not.toHaveBeenCalled();
  expect(effects.onNotebookUpdated).not.toHaveBeenCalled();
});

test("a dismissed save keeps its notebook single-flight until the server answers", async () => {
  // 掐掉浏览器 fetch 只是让客户端不再等响应，FastAPI 那侧的 handler 照跑。所以放弃的
  // 那次写入必须一直占着这本笔记本，直到客户端真看到它落定——否则重试可能先提交，
  // 再被那个较老的 handler 覆盖回去（codex #629 R2 P1）。
  const stuck = deferred<NotebookSummary>();
  notebookApi.updateNotebook.mockReturnValueOnce(stuck.promise);
  render(<Harness />);
  publish([notebook("a")]);
  await act(async () => value!.openEditor("a"));

  let saving!: Promise<void>;
  act(() => { saving = value!.saveEditor(editorPatch); });
  act(() => { value!.closeEditor(); });

  // 重开时如实显示这本库还有一笔写在飞：不给一个 saveEditor 会静默拒绝的「保存」。
  await act(async () => value!.openEditor("a"));
  expect(value!.editor?.busy).toBe(true);
  await act(async () => value!.saveEditor(editorPatch));
  expect(notebookApi.updateNotebook).toHaveBeenCalledTimes(1);

  // 那次写入落定时已经提交过 PATCH，而这个重开的弹窗是在它之前建的表单快照——
  // 原样放行「保存」会把刚落库的值写回旧值，所以它被关掉并重拉一次投影
  //（codex #629 R4 P1）。
  await act(async () => stuck.resolve(notebook("a")));
  await saving;
  expect(value!.editor).toBeNull();
  expect(effects.notify).toHaveBeenCalledWith(
    "刚才停止等待的那次保存，已提交的部分已生效；列表已刷新，设置请重新打开确认。",
  );

  // 重新打开（这次拿到的是刷新后的行）之后才允许第二次写入。
  await act(async () => value!.openEditor("a"));
  expect(value!.editor?.busy).toBe(false);
  await act(async () => value!.saveEditor(editorPatch));
  expect(notebookApi.updateNotebook).toHaveBeenCalledTimes(2);
});

test("a second save keeps the notebook while the first one unwinds its refresh", async () => {
  // 完成写序列的那次保存会在等 collection 刷新**之前**就释放这本库（#628 的刻意设计），
  // 所以第二次保存可能在第一次还没收尾时合法地占住同一个键。收尾必须认 token，否则它会
  // 把第二次的占位删掉、把第二次的弹窗标成不忙（codex #629 R3 P1）。
  const firstRefresh = deferred<void>();
  const secondUpdate = deferred<NotebookSummary>();
  vi.mocked(effects.refreshComposite)
    .mockReturnValueOnce(firstRefresh.promise)
    .mockResolvedValueOnce(undefined);
  notebookApi.updateNotebook
    .mockResolvedValueOnce(notebook("a"))
    .mockReturnValueOnce(secondUpdate.promise);
  render(<Harness />);
  publish([notebook("a")]);
  await act(async () => value!.openEditor("a"));

  let firstSave!: Promise<void>;
  act(() => { firstSave = value!.saveEditor(editorPatch); });
  await waitFor(() => expect(value!.editor).toBeNull());

  // 写序列已结束、刷新还在飞：这时重开并再存一次是允许的。
  await act(async () => value!.openEditor("a"));
  expect(value!.editor?.busy).toBe(false);
  let secondSave!: Promise<void>;
  act(() => { secondSave = value!.saveEditor(editorPatch); });
  expect(value!.editor?.busy).toBe(true);

  // 第一次的收尾跑完，不许动第二次的占位与忙碌位。
  await act(async () => firstRefresh.resolve(undefined));
  await firstSave;
  expect(value!.editor?.busy).toBe(true);
  await act(async () => value!.saveEditor(editorPatch));
  expect(notebookApi.updateNotebook).toHaveBeenCalledTimes(2);

  await act(async () => secondUpdate.resolve(notebook("a")));
  await secondSave;
});

test("a failed save keeps its own dialog open instead of treating it as abandoned", async () => {
  // 与「提交后被放弃」区分开：请求**失败**时表单快照并没有落后于服务端，#628 刻意
  // 让这个弹窗留在原地供重试，不该被当成陈旧快照关掉，也不该发重新载入提示。
  const rejection = new Error("bases rejected");
  basesApi.setBases.mockRejectedValueOnce(rejection);
  render(<Harness />);
  publish([notebook("a")]);
  await act(async () => value!.openEditor("a"));

  await act(async () => value!.saveEditor(editorPatch));

  expect(effects.reportError).toHaveBeenCalledWith(rejection);
  expect(value!.editor?.busy).toBe(false);
  expect(effects.notify).not.toHaveBeenCalled();
});

test("recovery keeps the notebook held until the projection is actually reloaded", async () => {
  // 提前释放会让「重开设置」拿到这次刷新即将替换掉的那一行，再存一次就把已经落库的值
  // 写回旧值（codex #629 R5 P1）。
  const stuck = deferred<NotebookSummary>();
  const slowRefresh = deferred<void>();
  notebookApi.updateNotebook.mockReturnValueOnce(stuck.promise);
  vi.mocked(effects.refreshComposite).mockReturnValueOnce(slowRefresh.promise);
  render(<Harness />);
  publish([notebook("a")]);
  await act(async () => value!.openEditor("a"));

  let saving!: Promise<void>;
  act(() => { saving = value!.saveEditor(editorPatch); });
  act(() => { value!.closeEditor(); });
  await act(async () => stuck.resolve(notebook("a")));

  // 刷新还在飞：这本库仍然占着，重开只能拿到在途态，存不出去。
  await act(async () => value!.openEditor("a"));
  expect(value!.editor?.busy).toBe(true);
  await act(async () => value!.saveEditor(editorPatch));
  expect(notebookApi.updateNotebook).toHaveBeenCalledTimes(1);

  // 恢复跑完后，这个「恢复期间开出来的」弹窗同样建立在旧行上，而且已经没有任何在飞
  // 写入能再解除它的忙碌——必须一并撤下，不能留一个永久禁用的框（codex #629 R6 P2）。
  await act(async () => slowRefresh.resolve(undefined));
  await saving;
  expect(value!.editor).toBeNull();

  await act(async () => value!.openEditor("a"));
  expect(value!.editor?.busy).toBe(false);
});

test("an open in flight during recovery publishes the refreshed row, not its snapshot", async () => {
  // openEditor 的表单快照原本取在它自己那几个请求**之前**。恢复的刷新恰好在这段窗口里
  // 落地时，发布出来的就是刷新前的旧行——一次未经编辑的「保存」又能把已经落库的值写回
  // 旧值（codex #629 R7 P1）。
  const stuck = deferred<NotebookSummary>();
  const pipeline = deferred<ReturnType<typeof indexingProjection>>();
  const refreshedRow = notebook("a");
  refreshedRow.name = "服务端最新";
  notebookApi.updateNotebook.mockReturnValueOnce(stuck.promise);
  vi.mocked(effects.refreshComposite).mockImplementationOnce(async () => {
    publish([refreshedRow]);
  });
  render(<Harness />);
  publish([notebook("a")]);
  await act(async () => value!.openEditor("a"));

  let saving!: Promise<void>;
  act(() => { saving = value!.saveEditor(editorPatch); });
  act(() => { value!.closeEditor(); });

  // 重开的请求先发出去，快照此刻还是旧行。
  notebookApi.fetchNotebookIndexingPipeline.mockReturnValueOnce(pipeline.promise);
  let opening!: Promise<boolean>;
  act(() => { opening = value!.openEditor("a"); });

  // 恢复在这段窗口里跑完并换掉了行。
  await act(async () => stuck.resolve(notebook("a")));
  await saving;

  await act(async () => pipeline.resolve(indexingProjection()));
  await opening;
  expect(value!.editor?.target.name).toBe("服务端最新");
});

test("recovery does not claim a refresh that failed", async () => {
  captureRefreshFailureLog();
  const stuck = deferred<NotebookSummary>();
  notebookApi.updateNotebook.mockReturnValueOnce(stuck.promise);
  vi.mocked(effects.refreshComposite).mockRejectedValueOnce(new Error("refresh failed"));
  render(<Harness />);
  publish([notebook("a")]);
  await act(async () => value!.openEditor("a"));

  let saving!: Promise<void>;
  act(() => { saving = value!.saveEditor(editorPatch); });
  act(() => { value!.closeEditor(); });
  await act(async () => stuck.resolve(notebook("a")));
  await saving;

  expect(effects.notify).toHaveBeenCalledWith(
    "刚才停止等待的那次保存，已提交的部分已生效；列表暂未刷新，请先刷新页面再打开设置。",
  );
});

test("an abandoned save that committed nothing leaves the reopened dialog usable", async () => {
  // 放弃 + 落过写 才构成陈旧快照。第一步就失败时什么都没落库，重开的表单并不落后于
  // 服务端——不该被撤下，也不该发「已提交的部分已生效」这种事实错误的提示。
  const stuck = deferred<NotebookSummary>();
  notebookApi.updateNotebook.mockReturnValueOnce(stuck.promise);
  render(<Harness />);
  publish([notebook("a")]);
  await act(async () => value!.openEditor("a"));

  let saving!: Promise<void>;
  act(() => { saving = value!.saveEditor(editorPatch); });
  act(() => { value!.closeEditor(); });
  await act(async () => value!.openEditor("a"));

  await act(async () => stuck.reject(new Error("PATCH failed")));
  await saving;

  expect(value!.editor?.busy).toBe(false);
  expect(effects.refreshComposite).not.toHaveBeenCalled();
  expect(effects.notify).not.toHaveBeenCalledWith(
    "刚才停止等待的那次保存，已提交的部分已生效；列表已刷新，设置请重新打开确认。",
  );
});

test("a reopened stale form cannot write the abandoned save's values back", async () => {
  // openEditor 的表单快照取自当时的 collection 行。放弃的那次写入落库之后，这份快照
  // 就早于服务端；若原样放行「保存」，一次未经编辑的提交就会把刚落库的值写回旧值。
  const stuck = deferred<NotebookSummary>();
  notebookApi.updateNotebook.mockReturnValueOnce(stuck.promise);
  render(<Harness />);
  publish([notebook("a")]);
  await act(async () => value!.openEditor("a"));

  let saving!: Promise<void>;
  act(() => { saving = value!.saveEditor(editorPatch); });
  act(() => { value!.closeEditor(); });
  await act(async () => value!.openEditor("a"));

  await act(async () => stuck.resolve(notebook("a")));
  await saving;

  // 弹窗已被撤下，陈旧表单再也提交不出去；投影也重新拉过一次。
  expect(value!.editor).toBeNull();
  expect(effects.refreshComposite).toHaveBeenCalledTimes(1);
  await act(async () => value!.saveEditor(editorPatch));
  expect(notebookApi.updateNotebook).toHaveBeenCalledTimes(1);
});

test("an outstanding write on one notebook does not block another notebook's settings", async () => {
  const stuck = deferred<NotebookSummary>();
  notebookApi.updateNotebook.mockReturnValueOnce(stuck.promise);
  render(<Harness />);
  publish([notebook("a"), notebook("b")]);
  await act(async () => value!.openEditor("a"));

  let saving!: Promise<void>;
  act(() => { saving = value!.saveEditor(editorPatch); });
  await act(async () => value!.openEditor("b"));
  expect(value!.editor?.busy).toBe(false);

  await act(async () => value!.saveEditor(editorPatch));
  expect(notebookApi.updateNotebook).toHaveBeenNthCalledWith(2, "b", expect.anything());

  await act(async () => stuck.resolve(notebook("a")));
  await saving;
});

test("a settings dialog reopened after re-login still recovers from its old write", async () => {
  // deletingRef / editorSavingIdsRef 都按 actor id 记账，登出再登录回同一个账号时
  // 键还在；释放若绑在发起时的 generation 上，重开的框会永远卡在忙碌（codex #629 R2 P2）。
  const stuck = deferred<NotebookSummary>();
  notebookApi.updateNotebook.mockReturnValueOnce(stuck.promise);
  const view = render(<Harness />);
  publish([notebook("a")]);
  await act(async () => value!.openEditor("a"));

  let saving!: Promise<void>;
  act(() => { saving = value!.saveEditor(editorPatch); });
  act(() => { value!.closeEditor(); });

  view.rerender(<Harness actorId={null} />);
  view.rerender(<Harness actorId="user-a" />);
  publish([notebook("a")]);
  await act(async () => value!.openEditor("a"));
  expect(value!.editor?.busy).toBe(true);

  // 落定之后必须回到可用状态（而不是永远卡在忙碌）——这里是「提交过就重开」那一路，
  // 所以它被关掉并重拉投影；关键是它没有停在忙碌上。
  await act(async () => stuck.resolve(notebook("a")));
  await saving;
  expect(value!.editor).toBeNull();
  await act(async () => value!.openEditor("a"));
  expect(value!.editor?.busy).toBe(false);
});

test("closing settings without a save in flight says nothing", async () => {
  render(<Harness />);
  publish([notebook("a")]);
  await act(async () => value!.openEditor("a"));

  act(() => { value!.closeEditor(); });

  expect(value!.editor).toBeNull();
  expect(effects.notify).not.toHaveBeenCalled();
});

test("a save refuses to run while the open editor and the pending operation name different notebooks", async () => {
  const pipeline = deferred<ReturnType<typeof indexingProjection>>();
  render(<Harness />);
  publish([notebook("a"), notebook("b")]);
  await act(async () => value!.openEditor("a"));

  // openEditor("b") 已经把 operation 换成 b，但它的投影还没回来，弹窗仍显示 a。
  notebookApi.fetchNotebookIndexingPipeline.mockReturnValueOnce(pipeline.promise);
  let opening!: Promise<boolean>;
  act(() => { opening = value!.openEditor("b"); });
  expect(value!.editor?.target.id).toBe("a");

  await act(async () => value!.saveEditor(editorPatch));
  expect(notebookApi.updateNotebook).not.toHaveBeenCalled();

  await act(async () => pipeline.resolve(indexingProjection()));
  await opening;
});

test("an immediate reopen after a committed save can unmount a base before refresh finishes", async () => {
  const firstRefresh = deferred<void>();
  const mountedBase = {
    id: "base-a",
    name: "公共知识库 A",
    tier: "base",
    active: true,
    inactive_reason: "",
  };
  vi.mocked(effects.refreshComposite)
    .mockReturnValueOnce(firstRefresh.promise)
    .mockResolvedValueOnce(undefined);
  basesApi.listMountable.mockResolvedValue([{ id: "base-a", name: "公共知识库 A", tier: "base" }]);
  basesApi.listBases
    .mockResolvedValueOnce([])
    .mockResolvedValueOnce([mountedBase]);
  basesApi.setBases
    .mockResolvedValueOnce([mountedBase])
    .mockResolvedValueOnce([]);

  render(<Harness />);
  publish([notebook("a")]);
  await act(async () => value!.openEditor("a"));
  act(() => value!.toggleMountedBase("base-a", true));

  let firstSave!: Promise<void>;
  act(() => { firstSave = value!.saveEditor(editorPatch); });
  await waitFor(() => expect(value!.editor).toBeNull());
  expect(basesApi.setBases).toHaveBeenNthCalledWith(1, "a", ["base-a"]);

  await act(async () => value!.openEditor("a"));
  expect(value!.editor?.mountedIds).toEqual(["base-a"]);
  act(() => value!.toggleMountedBase("base-a", false));
  await act(async () => value!.saveEditor(editorPatch));

  expect(notebookApi.updateNotebook).toHaveBeenCalledTimes(2);
  expect(basesApi.setBases).toHaveBeenNthCalledWith(2, "a", []);
  await act(async () => firstRefresh.resolve(undefined));
  await firstSave;
});

test("editor save is single-flight and releases its authority after success and failure", async () => {
  const first = deferred<NotebookSummary>();
  notebookApi.updateNotebook
    .mockReturnValueOnce(first.promise)
    .mockRejectedValueOnce(new Error("save failed"))
    .mockResolvedValueOnce(notebook("a"));
  render(<Harness />);
  publish([notebook("a")]);
  await act(async () => value!.openEditor("a"));

  let firstSave!: Promise<void>;
  act(() => {
    firstSave = value!.saveEditor(editorPatch);
    void value!.saveEditor(editorPatch);
  });
  expect(notebookApi.updateNotebook).toHaveBeenCalledTimes(1);
  await act(async () => first.resolve(notebook("a")));
  await firstSave;

  await act(async () => value!.openEditor("a"));
  await act(async () => value!.saveEditor(editorPatch));
  expect(effects.reportError).toHaveBeenCalledTimes(1);
  await act(async () => value!.saveEditor(editorPatch));
  expect(notebookApi.updateNotebook).toHaveBeenCalledTimes(3);
});

test("a published permission downgrade closes an owner-only delete confirmation", async () => {
  render(<Harness />);
  publish([notebook("a")]);
  await act(async () => value!.openDelete("a"));
  expect(value!.deletion?.target.id).toBe("a");

  publish([notebook("a", "reader")]);

  expect(value!.deletion).toBeNull();
  publish([notebook("a")]);
  expect(value!.deletion).toBeNull();
  await act(async () => value!.confirmDelete());
  expect(notebookApi.deleteNotebook).not.toHaveBeenCalled();
});

test("an in-flight delete reports backend rejection after a transient list omission", async () => {
  const deletion = deferred<undefined>();
  const rejection = new Error("delete rejected");
  notebookApi.deleteNotebook.mockReturnValueOnce(deletion.promise);
  render(<Harness />);
  publish([notebook("a")]);
  await act(async () => value!.openDelete("a"));

  let deleting!: Promise<void>;
  act(() => { deleting = value!.confirmDelete(); });
  publish([]);
  expect(value!.deletion?.busy).toBe(true);
  await act(async () => deletion.reject(rejection));
  await deleting;

  expect(effects.reportError).toHaveBeenCalledWith(rejection);
  expect(value!.deletion).toBeNull();
});

test("dismissing the confirmation does not withdraw a DELETE already on the wire", async () => {
  const deletion = deferred<undefined>();
  notebookApi.deleteNotebook.mockReturnValueOnce(deletion.promise);
  render(<Harness />);
  publish([notebook("a")]);
  await act(async () => value!.openDelete("a"));

  let deleting!: Promise<void>;
  act(() => { deleting = value!.confirmDelete(); });
  act(() => { value!.closeDelete(); });
  expect(value!.deletion).toBeNull();
  expect(effects.notify).toHaveBeenCalledWith("删除请求仍在进行；结果稍后会反映在列表里。");

  await act(async () => deletion.resolve(undefined));
  await deleting;
  expect(effects.onNotebookDeleted).toHaveBeenCalledWith("a");
  expect(effects.notify).toHaveBeenCalledWith("笔记本已删除");
  expect(value!.rows).toEqual([]);
});

test("a delete that fails after its confirmation was dismissed still reports", async () => {
  const deletion = deferred<undefined>();
  const rejection = new Error("delete rejected");
  notebookApi.deleteNotebook.mockReturnValueOnce(deletion.promise);
  render(<Harness />);
  publish([notebook("a")]);
  await act(async () => value!.openDelete("a"));

  let deleting!: Promise<void>;
  act(() => { deleting = value!.confirmDelete(); });
  act(() => { value!.closeDelete(); });
  await act(async () => deletion.reject(rejection));
  await deleting;

  expect(effects.reportError).toHaveBeenCalledWith(rejection);
  expect(value!.rows.map((row) => row.id)).toEqual(["a"]);
});

test("a confirmation reopened over a pending DELETE shows it pending and recovers", async () => {
  // 关掉再打开时行还在（DELETE 没落定），一个崭新的 busy:false 确认框会给出一个
  // confirmDelete 永远静默拒绝的「确认」——挂死的请求下它永远按不动（codex #629 R1 P2）。
  const first = deferred<undefined>();
  const rejection = new Error("delete rejected");
  notebookApi.deleteNotebook
    .mockReturnValueOnce(first.promise)
    .mockResolvedValueOnce(undefined);
  render(<Harness />);
  publish([notebook("a")]);
  await act(async () => value!.openDelete("a"));

  let deleting!: Promise<void>;
  act(() => { deleting = value!.confirmDelete(); });
  act(() => { value!.closeDelete(); });

  await act(async () => value!.openDelete("a"));
  expect(value!.deletion?.busy).toBe(true);

  await act(async () => first.reject(rejection));
  await deleting;
  expect(effects.reportError).toHaveBeenCalledWith(rejection);
  // 原请求落定后，重开的这个框回到可按状态——而不是永远卡在「删除中…」。
  expect(value!.deletion?.busy).toBe(false);

  await act(async () => value!.confirmDelete());
  expect(notebookApi.deleteNotebook).toHaveBeenCalledTimes(2);
  expect(value!.rows).toEqual([]);
});

test("a confirmation reopened after re-login still recovers from its old DELETE", async () => {
  // 与设置弹窗同一条：deletingRef 按 actor id 记账，登出再登录回同一账号时键还在，
  // 释放若绑在发起时的 generation 上，重开的框会永远卡在「删除中」（codex #629 R2 P2）。
  const stuck = deferred<undefined>();
  notebookApi.deleteNotebook.mockReturnValueOnce(stuck.promise);
  const view = render(<Harness />);
  publish([notebook("a")]);
  await act(async () => value!.openDelete("a"));

  let deleting!: Promise<void>;
  act(() => { deleting = value!.confirmDelete(); });
  act(() => { value!.closeDelete(); });

  view.rerender(<Harness actorId={null} />);
  view.rerender(<Harness actorId="user-a" />);
  publish([notebook("a")]);
  await act(async () => value!.openDelete("a"));
  expect(value!.deletion?.busy).toBe(true);

  await act(async () => stuck.reject(new Error("delete rejected")));
  await deleting;
  expect(value!.deletion?.busy).toBe(false);
});

test("a completed delete does not close a confirmation opened for another notebook", async () => {
  const stuck = deferred<undefined>();
  notebookApi.deleteNotebook.mockReturnValueOnce(stuck.promise);
  render(<Harness />);
  publish([notebook("a"), notebook("b")]);
  await act(async () => value!.openDelete("a"));

  let deleting!: Promise<void>;
  act(() => { deleting = value!.confirmDelete(); });
  act(() => { value!.closeDelete(); });
  await act(async () => value!.openDelete("b"));
  expect(value!.deletion?.target.id).toBe("b");

  await act(async () => stuck.resolve(undefined));
  await deleting;

  // A 删成功不该顺手把 B 的确认框关掉（codex #629 R3 P2）。
  expect(value!.deletion?.target.id).toBe("b");
  expect(value!.rows.map((row) => row.id)).toEqual(["b"]);
});

test("a delete that fails after re-login reports into the dialog it recovered", async () => {
  // 确认框刻意跨 actor 世代显示为在途，那么它的失败也必须按同一条身份权威汇报——
  // 否则忙碌位悄悄清掉，用户得不到任何解释（codex #629 R4 P2）。
  const stuck = deferred<undefined>();
  const rejection = new Error("delete rejected");
  notebookApi.deleteNotebook.mockReturnValueOnce(stuck.promise);
  const view = render(<Harness />);
  publish([notebook("a")]);
  await act(async () => value!.openDelete("a"));

  let deleting!: Promise<void>;
  act(() => { deleting = value!.confirmDelete(); });
  act(() => { value!.closeDelete(); });

  view.rerender(<Harness actorId={null} />);
  view.rerender(<Harness actorId="user-a" />);
  publish([notebook("a")]);
  await act(async () => value!.openDelete("a"));

  await act(async () => stuck.reject(rejection));
  await deleting;

  expect(effects.reportError).toHaveBeenCalledWith(rejection);
  expect(value!.deletion?.busy).toBe(false);
});

test("closing the confirmation without a delete in flight says nothing", async () => {
  render(<Harness />);
  publish([notebook("a")]);
  await act(async () => value!.openDelete("a"));

  act(() => { value!.closeDelete(); });

  expect(value!.deletion).toBeNull();
  expect(effects.notify).not.toHaveBeenCalled();
});

test("delete is single-flight and a pre-delete list cannot resurrect its tombstone", async () => {
  const deletion = deferred<undefined>();
  notebookApi.deleteNotebook.mockReturnValueOnce(deletion.promise);
  render(<Harness />);
  publish([notebook("a")]);
  const staleRead = value!.beginListRead();
  await act(async () => value!.openDelete("a"));

  let deleting!: Promise<void>;
  act(() => {
    deleting = value!.confirmDelete();
    void value!.confirmDelete();
  });
  expect(notebookApi.deleteNotebook).toHaveBeenCalledTimes(1);
  await act(async () => deletion.resolve(undefined));
  await deleting;
  expect(value!.rows).toEqual([]);
  act(() => { value!.commitListSnapshot(staleRead, [notebook("a")]); });
  expect(value!.rows).toEqual([]);
});

test("delete releases its single-flight key after failure and permits one retry", async () => {
  const first = deferred<undefined>();
  notebookApi.deleteNotebook
    .mockReturnValueOnce(first.promise)
    .mockResolvedValueOnce(undefined);
  render(<Harness />);
  publish([notebook("a")]);
  await act(async () => value!.openDelete("a"));

  let deleting!: Promise<void>;
  act(() => {
    deleting = value!.confirmDelete();
    void value!.confirmDelete();
  });
  expect(notebookApi.deleteNotebook).toHaveBeenCalledTimes(1);
  await act(async () => first.reject(new Error("delete failed")));
  await deleting;
  expect(effects.reportError).toHaveBeenCalledTimes(1);

  await act(async () => value!.confirmDelete());
  expect(notebookApi.deleteNotebook).toHaveBeenCalledTimes(2);
});

test("only a post-delete snapshot may garbage-collect a tombstone", async () => {
  render(<Harness />);
  publish([notebook("a")]);
  const staleWithoutTarget = value!.beginListRead();
  const laterStaleWithTarget = value!.beginListRead();
  await act(async () => value!.openDelete("a"));
  await act(async () => value!.confirmDelete());
  expect(value!.rows).toEqual([]);

  act(() => { value!.commitListSnapshot(staleWithoutTarget, []); });
  expect(value!.rows).toEqual([]);
  act(() => { value!.commitListSnapshot(laterStaleWithTarget, [notebook("a")]); });
  expect(value!.rows).toEqual([]);
});

test("a delete completed across actor A to B to A removes the restored A card", async () => {
  const deletion = deferred<undefined>();
  notebookApi.deleteNotebook.mockReturnValueOnce(deletion.promise);
  const view = render(<Harness />);
  publish([notebook("a")]);
  await act(async () => value!.openDelete("a"));
  let deleting!: Promise<void>;
  act(() => { deleting = value!.confirmDelete(); });

  view.rerender(<Harness actorId="user-b" />);
  await act(async () => Promise.resolve());
  publish([notebook("b")]);
  view.rerender(<Harness actorId="user-a" />);
  await act(async () => Promise.resolve());
  publish([notebook("a")]);
  expect(value!.rows.map((row) => row.id)).toEqual(["a"]);

  await act(async () => deletion.resolve(undefined));
  await deleting;
  expect(value!.rows).toEqual([]);
});

test("access refresh retries once when a later list read supersedes it", async () => {
  const first = deferred<NotebookSummary[]>();
  notebookApi.listNotebooks
    .mockReturnValueOnce(first.promise)
    .mockResolvedValueOnce([notebook("fresh")]);
  render(<Harness />);

  let refreshing!: Promise<void>;
  act(() => { refreshing = value!.refreshAfterAccessChange(9); });
  value!.beginListRead();
  await act(async () => first.resolve([notebook("old")]));
  await refreshing;
  expect(notebookApi.listNotebooks).toHaveBeenCalledTimes(2);
  expect(effects.reconcileAccess).toHaveBeenCalledTimes(1);
  expect(effects.reconcileAccess).toHaveBeenCalledWith([notebook("fresh")], 9);
});

test("access refresh remains one-shot after its sole retry is superseded again", async () => {
  const first = deferred<NotebookSummary[]>();
  const retry = deferred<NotebookSummary[]>();
  notebookApi.listNotebooks
    .mockReturnValueOnce(first.promise)
    .mockReturnValueOnce(retry.promise);
  render(<Harness />);

  let refreshing!: Promise<void>;
  act(() => { refreshing = value!.refreshAfterAccessChange(9); });
  value!.beginListRead();
  await act(async () => first.resolve([notebook("old")]));
  expect(notebookApi.listNotebooks).toHaveBeenCalledTimes(2);
  value!.beginListRead();
  await act(async () => retry.resolve([notebook("also-old")]));
  await refreshing;

  expect(notebookApi.listNotebooks).toHaveBeenCalledTimes(2);
  expect(effects.reconcileAccess).not.toHaveBeenCalled();
});

// PR #557 regression: `rows`/`visibleRows`/`searchHits` used to fall back to
// a bare `[]`/`{}` literal whenever rows are not yet published for this
// owner (no beginListRead/commitListSnapshot has landed — e.g. actorId is
// null, or the collection page hasn't loaded a list yet). A bare literal is
// a brand-new reference on every render, which makes a consuming effect's
// dependency array "change" every render (see use-ask-session.ts for the
// traced infinite-loop incident). The fix hoists frozen, stable module-level
// fallback constants; re-rendering with rows still unpublished must hand
// back the *same* reference every time.
test("rows-unpublished view fields stay referentially stable across re-renders", () => {
  const view = render(<Harness actorId={null} />);
  const first = value!;
  expect(first.rows).toEqual([]);
  expect(first.visibleRows).toEqual([]);
  expect(first.searchHits).toEqual({});
  // A plain `useState` initial value is never frozen; only the hidden-state
  // fallback branch (the module-level `NO_*` constant) is. Asserting frozen
  // here pins down *which* branch actually produced this value, not merely
  // that it happens to equal an empty literal.
  expect(Object.isFrozen(first.rows)).toBe(true);
  expect(Object.isFrozen(first.visibleRows)).toBe(true);
  expect(Object.isFrozen(first.searchHits)).toBe(true);

  act(() => {
    view.rerender(<Harness actorId={null} />);
  });
  const second = value!;
  act(() => {
    view.rerender(<Harness actorId={null} />);
  });
  const third = value!;

  for (const later of [second, third]) {
    expect(later.rows).toBe(first.rows);
    expect(later.visibleRows).toBe(first.visibleRows);
    expect(later.searchHits).toBe(first.searchHits);
  }
});
