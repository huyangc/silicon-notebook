import { act, render } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import type { SourceSummary } from "../../app/workspace-model";
import { sourceElementDomId } from "../../app/source-detail-state";

const api = vi.hoisted(() => ({
  getSource: vi.fn(),
  listSources: vi.fn(),
  getNotebookSource: vi.fn(),
  getNotebookSourceElementsPage: vi.fn(),
  parseSource: vi.fn(),
  deleteSource: vi.fn(),
}));

vi.mock("../../app/source-api.ts", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../app/source-api.ts")>()),
  ...api,
}));

import { useSourceLibrary, type SourceReparseOutcome } from "../../app/use-source-library";

function source(id: string, notebookId: string, parseStatus = "extracted"): SourceSummary {
  return {
    id,
    notebook_id: notebookId,
    title: id,
    display_title: id,
    type: "file",
    status: parseStatus,
    parse_status: parseStatus,
    summary: "",
    element_count: 0,
    file_name: `${id}.md`,
    file_size: 1,
    created_at: "2026-08-22T00:00:00Z",
    created_label: "8月22日",
  };
}

type HookValue = ReturnType<typeof useSourceLibrary>;
let value: HookValue | null = null;

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

const effects = {
  setStatusText: vi.fn(),
  reportError: vi.fn(),
  setToast: vi.fn(),
  invalidateKnowledge: vi.fn(),
  refreshCollection: vi.fn(async () => undefined),
  refreshNotebook: vi.fn(async () => undefined),
  refreshCheckup: vi.fn(async () => undefined),
};

function Harness({ actorId = "user-a", canWrite = true }: { actorId?: string | null; canWrite?: boolean }) {
  value = useSourceLibrary({ actorId, canWriteSources: canWrite, effects });
  return <div data-testid="ids">{value.sources.map((item) => item.id).join(",")}</div>;
}

beforeEach(() => {
  value = null;
  vi.clearAllMocks();
  // 接口桩每条用例自己设:上一条没用完的 mockReturnValueOnce 不得漏进下一条。
  for (const mock of Object.values(api)) mock.mockReset();
});

test("source detail scrolling and rendering share one sanitized element id", () => {
  expect(sourceElementDomId("element/path:中文")).toBe("source-element-element_path___");
});

afterEach(() => {
  vi.useRealTimers();
});

test.each(["", "   ", "\t　 "])("blank source search %j restores all sources and releases busy", async (blank) => {
  render(<Harness />);
  const all = [source("match", "notebook-a"), source("other", "notebook-a")];
  act(() => {
    value!.commitNotebookSnapshot({
      actorId: "user-a", notebookId: "notebook-a", workspaceEpoch: 1,
      page: { items: all, total_count: 2, offset: 0, limit: 50 },
    });
    value!.toggleSource("other");
    value!.setSourceQuery("match");
  });
  api.listSources.mockResolvedValueOnce({ items: [all[0]], total_count: 1, offset: 0, limit: 50 });
  const contentRevision = value!.contentRevision;
  await act(async () => value!.searchSources());
  expect(value!.sources).toEqual([all[0]]);
  expect(value!.contentRevision).toBe(contentRevision);

  const reset = deferred<{ items: SourceSummary[]; total_count: number; offset: number; limit: number }>();
  api.listSources.mockReturnValueOnce(reset.promise);
  let loading!: Promise<void>;
  act(() => {
    value!.setSourceQuery(blank);
    loading = value!.searchSources();
  });
  expect(value!.sourceQuery).toBe("");
  expect(value!.sourcesPageLoading).toBe(true);
  expect(api.listSources).toHaveBeenLastCalledWith("notebook-a", 0, 50, "", expect.any(AbortSignal));
  reset.resolve({ items: all, total_count: 2, offset: 0, limit: 50 });
  await act(async () => loading);
  expect(value!.sources).toEqual(all);
  expect(value!.sourcesTotal).toBe(2);
  expect(value!.notebookSourceTotal).toBe(2);
  expect(value!.sourcesPage).toBe(0);
  expect(value!.sourcesPageLoading).toBe(false);
  expect(value!.currentPageRequest()).toEqual({ page: 0, q: "" });
  expect(value!.sourceScopeSelection.ids.has("other")).toBe(true);
  expect(value!.contentRevision).toBe(contentRevision);
});

test("content refresh token ignores pagination but changes for updated source content and uploads", async () => {
  render(<Harness />);
  act(() => {
    value!.commitNotebookSnapshot({
      actorId: "user-a", notebookId: "notebook-a", workspaceEpoch: 1,
      page: { items: [source("first", "notebook-a")], total_count: 100, offset: 0, limit: 50 },
    });
  });
  const initial = value!.contentRevision;
  const second = source("second", "notebook-a");
  api.listSources.mockResolvedValueOnce({ items: [second], total_count: 100, offset: 50, limit: 50 });
  await act(async () => value!.loadSourcesPage({ page: 1 }));
  expect(value!.contentRevision).toBe(initial);
  api.listSources.mockResolvedValueOnce({ items: [{ ...second, summary: "更新摘要" }], total_count: 100, offset: 50, limit: 50 });
  await act(async () => value!.loadSourcesPage({ page: 1 }));
  expect(value!.contentRevision).toBeGreaterThan(initial);
  const updated = value!.contentRevision;
  api.listSources.mockResolvedValueOnce({ items: [source("replacement", "notebook-a")], total_count: 100, offset: 50, limit: 50 });
  await act(async () => value!.loadSourcesPage({ page: 1 }));
  expect(value!.contentRevision).toBeGreaterThan(updated);
  const refreshed = value!.contentRevision;
  act(() => { value!.commitUploadedSources(value!.captureOwner(), [source("new", "notebook-a")], 1); });
  expect(value!.contentRevision).toBeGreaterThan(refreshed);
});

test("late upload and URL results cannot commit across notebook or actor transitions", () => {
  const view = render(<Harness />);
  let ownerA = null as ReturnType<HookValue["captureOwner"]>;
  act(() => {
    ownerA = value!.commitNotebookSnapshot({
      actorId: "user-a",
      notebookId: "notebook-a",
      workspaceEpoch: 1,
      page: { items: [], total_count: 0, offset: 0, limit: 50 },
    });
  });
  act(() => {
    value!.beginTransition();
    value!.commitNotebookSnapshot({
      actorId: "user-a",
      notebookId: "notebook-b",
      workspaceEpoch: 2,
      page: { items: [], total_count: 0, offset: 0, limit: 50 },
    });
  });
  expect(value!.commitUploadedSources(ownerA, [source("old-upload", "notebook-a")], 1)).toBe(false);
  expect(value!.commitUrlSources(ownerA, [source("old-url", "notebook-a")])).toBe(false);
  expect(value!.sources).toEqual([]);

  const currentOwner = value!.captureOwner();
  expect(currentOwner?.notebookId).toBe("notebook-b");
  view.rerender(<Harness actorId="user-b" />);
  expect(value!.captureOwner()).toBeNull();
  expect(value!.sources).toEqual([]);
  expect(
    value!.commitUploadedSources(currentOwner, [source("old-user", "notebook-b")], 1),
  ).toBe(false);
  view.rerender(<Harness actorId="user-a" />);
  expect(value!.captureOwner()).toBeNull();
});

test("authenticated bootstrap can activate the actor before the React user state rerenders", () => {
  const view = render(<Harness actorId={null} />);
  act(() => {
    value!.activateActor("user-a");
  });
  let owner = null as ReturnType<HookValue["commitNotebookSnapshot"]>;
  act(() => {
    owner = value!.commitNotebookSnapshot({
      actorId: "user-a",
      notebookId: "notebook-a",
      workspaceEpoch: 1,
      page: {
        items: [source("restored", "notebook-a")],
        total_count: 1,
        offset: 0,
        limit: 50,
      },
    });
  });
  expect(owner?.actorId).toBe("user-a");
  expect(value!.sources.map((item) => item.id)).toEqual(["restored"]);
  view.rerender(<Harness actorId="user-a" />);
  expect(value!.captureOwner()).toBe(owner);
});

test("delete requires the current owner and an exact source notebook", async () => {
  api.deleteSource.mockResolvedValue(undefined);
  render(<Harness />);
  // 返回值说的是「这次真的删掉了没有」:被拒(没有 owner / 别的库)、请求失败都是 false,
  // 图谱分析的问题清单据此把结果写在「删除来源…」按钮旁。
  let refused: boolean | undefined;
  await act(async () => { refused = await value!.deleteSource(source("no-owner", "notebook-a")); });
  expect(refused).toBe(false);
  expect(api.deleteSource).not.toHaveBeenCalled();

  act(() => {
    value!.commitNotebookSnapshot({
      actorId: "user-a",
      notebookId: "notebook-a",
      workspaceEpoch: 1,
      page: {
        items: [source("owned", "notebook-a")],
        total_count: 1,
        offset: 0,
        limit: 50,
      },
    });
  });
  let foreign: boolean | undefined;
  await act(async () => { foreign = await value!.deleteSource(source("foreign", "notebook-b")); });
  expect(foreign).toBe(false);
  expect(api.deleteSource).not.toHaveBeenCalled();
  let deleted: boolean | undefined;
  await act(async () => { deleted = await value!.deleteSource(source("owned", "notebook-a")); });
  expect(deleted).toBe(true);
  expect(api.deleteSource).toHaveBeenCalledTimes(1);
  expect(api.deleteSource).toHaveBeenCalledWith("owned");
});

test("a delete request that fails reports false and still releases the busy bit", async () => {
  api.deleteSource.mockRejectedValueOnce(new Error("boom"));
  render(<Harness />);
  act(() => {
    value!.commitNotebookSnapshot({
      actorId: "user-a",
      notebookId: "notebook-a",
      workspaceEpoch: 1,
      page: {
        items: [source("broken", "notebook-a")],
        total_count: 1,
        offset: 0,
        limit: 50,
      },
    });
  });
  let deleted: boolean | undefined;
  await act(async () => { deleted = await value!.deleteSource(source("broken", "notebook-a")); });
  expect(deleted).toBe(false);
  expect(effects.reportError).toHaveBeenCalled();
  expect(value!.deletingSourceIds.has("broken")).toBe(false);
});

test("an in-flight delete remains visibly busy across A to B to A", async () => {
  const deletion = deferred<undefined>();
  api.deleteSource.mockReturnValueOnce(deletion.promise);
  render(<Harness />);
  const pageA: Parameters<HookValue["commitNotebookSnapshot"]>[0] = {
    actorId: "user-a",
    notebookId: "notebook-a",
    workspaceEpoch: 1,
    page: {
      items: [source("deleting", "notebook-a")],
      total_count: 1,
      offset: 0,
      limit: 50,
    },
  };
  act(() => {
    value!.commitNotebookSnapshot(pageA);
  });
  let deleting!: Promise<boolean>;
  act(() => {
    deleting = value!.deleteSource(source("deleting", "notebook-a"));
  });
  expect(value!.deletingSourceIds.has("deleting")).toBe(true);

  act(() => {
    value!.beginTransition();
    value!.commitNotebookSnapshot({
      ...pageA,
      notebookId: "notebook-b",
      workspaceEpoch: 2,
      page: { ...pageA.page, items: [] },
    });
  });
  expect(value!.deletingSourceIds.has("deleting")).toBe(false);
  act(() => {
    value!.beginTransition();
    value!.commitNotebookSnapshot({ ...pageA, workspaceEpoch: 3 });
  });
  expect(value!.deletingSourceIds.has("deleting")).toBe(true);
  await act(async () => value!.deleteSource(source("deleting", "notebook-a")));
  expect(api.deleteSource).toHaveBeenCalledTimes(1);

  deletion.resolve(undefined);
  await act(async () => deleting);
  expect(value!.deletingSourceIds.has("deleting")).toBe(false);
});

function commitPage(items: SourceSummary[], notebookId = "notebook-a") {
  act(() => {
    value!.commitNotebookSnapshot({
      actorId: "user-a", notebookId, workspaceEpoch: 1,
      page: { items, total_count: items.length, offset: 0, limit: 50 },
    });
  });
}

test("a source followed off the current page is read to a terminal state, moving contentRevision on each change", async () => {
  // 图谱分析里「重新解析…」的那一篇多半不在来源库当前页:只轮询当前页的话,它解析完也不会
  // 让问题清单与总览失效。
  vi.useFakeTimers();
  api.getSource
    .mockResolvedValueOnce(source("elsewhere", "notebook-a", "parsing"))
    .mockResolvedValueOnce(source("elsewhere", "notebook-a", "extracted"));
  render(<Harness />);
  commitPage([source("first", "notebook-a")]);
  const initial = value!.contentRevision;
  act(() => value!.trackSourceProgress(["elsewhere"]));

  await act(async () => vi.advanceTimersByTimeAsync(1500));
  expect(api.getSource).toHaveBeenCalledWith("elsewhere");
  const parsing = value!.contentRevision;
  expect(parsing).toBeGreaterThan(initial);

  await act(async () => vi.advanceTimersByTimeAsync(2250));
  expect(value!.contentRevision).toBeGreaterThan(parsing);
  expect(effects.refreshCheckup).toHaveBeenCalledWith("notebook-a", expect.any(Function));

  // 到了终态就不再读;当前页原样。
  const calls = api.getSource.mock.calls.length;
  await act(async () => vi.advanceTimersByTimeAsync(60_000));
  expect(api.getSource.mock.calls.length).toBe(calls);
  expect(value!.sources.map((item) => item.id)).toEqual(["first"]);
  expect(effects.reportError).not.toHaveBeenCalled();
});

test("a followed source on the current page leaves the follow set once it reaches a terminal state", async () => {
  vi.useFakeTimers();
  api.getSource.mockResolvedValue(source("here", "notebook-a", "extracted"));
  render(<Harness />);
  commitPage([source("here", "notebook-a", "parsing")]);
  act(() => value!.trackSourceProgress(["here"]));
  expect(value!.followedSourceCount).toBe(1);

  await act(async () => vi.advanceTimersByTimeAsync(1500));
  expect(value!.sources[0]?.parse_status).toBe("extracted");
  // 在当前页上走到终态同样移出跟踪集合——否则集合非空、hasPending 恒真,轮询器空转不重启。
  expect(value!.followedSourceCount).toBe(0);
});

test("a followed source on the current page with a stale terminal status is still read, and the page row follows", async () => {
  // 重新解析刚排上时,当前页上那一行可能还是旧的「已就绪」:页内轮询看不见它,得按跟踪读。
  vi.useFakeTimers();
  api.getSource
    .mockResolvedValueOnce(source("here", "notebook-a", "parsing"))
    .mockResolvedValue(source("here", "notebook-a", "extracted"));
  render(<Harness />);
  commitPage([source("here", "notebook-a", "extracted")]);
  const initial = value!.contentRevision;
  act(() => value!.trackSourceProgress(["here"]));

  await act(async () => vi.advanceTimersByTimeAsync(1500));
  expect(api.getSource).toHaveBeenCalledWith("here");
  expect(value!.sources[0]?.parse_status).toBe("parsing");
  expect(value!.contentRevision).toBeGreaterThan(initial);

  await act(async () => vi.advanceTimersByTimeAsync(2250));
  expect(value!.sources[0]?.parse_status).toBe("extracted");
  expect(value!.followedSourceCount).toBe(0);
});

test("new follow work wakes a poller that went idle, even though hasPending never flipped", async () => {
  vi.useFakeTimers();
  api.getSource.mockImplementation(async (id: string) => (
    id === "stuck" ? source("stuck", "notebook-a", "parsing") : { ...source(id, "notebook-a", "extracted"), updated_at: "T9" }
  ));
  render(<Harness />);
  commitPage([source("stuck", "notebook-a", "parsing")]);
  // 用完尝试次数:轮询器不再安排定时器,但当前页仍有处理中的来源,hasPending 一直为真。
  await act(async () => vi.advanceTimersByTimeAsync(60 * 60 * 1000));
  expect(effects.setStatusText).toHaveBeenCalledWith("处理超时：部分来源长时间未完成，请稍后重试");
  const revision = value!.contentRevision;

  // 这一次跑得很快:第一次读到时已经是新的终态(updated_at 相对提交前的基准推进了)。
  act(() => value!.trackSourceProgress(["elsewhere"], { elsewhere: "T0" }));
  await act(async () => vi.advanceTimersByTimeAsync(1500));
  expect(api.getSource).toHaveBeenCalledWith("elsewhere");
  expect(value!.contentRevision).toBeGreaterThan(revision);
  expect(value!.followedSourceCount).toBe(0);
});

function at(item: SourceSummary, updatedAt: string): SourceSummary {
  return { ...item, updated_at: updatedAt };
}

test("a reparse still queued behind a busy pool: stale terminal reads do not end the follow; this run's start and end do", async () => {
  // 重新解析端点只把作业交给作业池、返回前不改来源状态:作业池忙时头一两次轮询还会读到上一次
  // 的 extracted。那不是这一次跑完——跟踪要等到这一次开始(或 updated_at 相对提交前推进)再到终态。
  vi.useFakeTimers();
  api.getSource
    .mockResolvedValueOnce(at(source("elsewhere", "notebook-a", "extracted"), "T0"))
    .mockResolvedValueOnce(at(source("elsewhere", "notebook-a", "extracted"), "T0"))
    .mockResolvedValueOnce(at(source("elsewhere", "notebook-a", "parsing"), "T1"))
    .mockResolvedValue(at(source("elsewhere", "notebook-a", "extracted"), "T2"));
  render(<Harness />);
  commitPage([source("first", "notebook-a")]);
  const initial = value!.contentRevision;
  act(() => value!.trackSourceProgress(["elsewhere"], { elsewhere: "T0" }));

  await act(async () => vi.advanceTimersByTimeAsync(1500));
  await act(async () => vi.advanceTimersByTimeAsync(2250));
  expect(api.getSource).toHaveBeenCalledTimes(2);
  // 两次都是上一次留下的旧终态:还在跟,也不让分析失效。
  expect(value!.followedSourceCount).toBe(1);
  expect(value!.contentRevision).toBe(initial);
  expect(effects.refreshCheckup).not.toHaveBeenCalled();

  await act(async () => vi.advanceTimersByTimeAsync(3375));
  const started = value!.contentRevision;
  expect(started).toBeGreaterThan(initial);
  expect(value!.followedSourceCount).toBe(1);

  await act(async () => vi.advanceTimersByTimeAsync(5063));
  expect(value!.contentRevision).toBeGreaterThan(started);
  expect(value!.followedSourceCount).toBe(0);
  expect(effects.refreshCheckup).toHaveBeenCalledWith("notebook-a", expect.any(Function));
});

test("a whole run that falls between two reads still ends the follow: updated_at moved past the pre-submission baseline", async () => {
  vi.useFakeTimers();
  api.getSource
    .mockResolvedValueOnce(at(source("elsewhere", "notebook-a", "extracted"), "T0"))
    .mockResolvedValue(at(source("elsewhere", "notebook-a", "extracted"), "T2"));
  render(<Harness />);
  commitPage([source("first", "notebook-a")]);
  const initial = value!.contentRevision;
  act(() => value!.trackSourceProgress(["elsewhere"], { elsewhere: "T0" }));

  await act(async () => vi.advanceTimersByTimeAsync(1500));
  expect(value!.followedSourceCount).toBe(1);
  await act(async () => vi.advanceTimersByTimeAsync(2250));
  expect(value!.followedSourceCount).toBe(0);
  expect(value!.contentRevision).toBeGreaterThan(initial);
});

test("the follow baseline is the source detail's updated_at, or null when that read fails", async () => {
  render(<Harness />);
  commitPage([source("first", "notebook-a")]);
  api.getSource.mockResolvedValueOnce(at(source("elsewhere", "notebook-a", "extracted"), "T5"));
  await expect(value!.readFollowBaseline("elsewhere")).resolves.toBe("T5");
  api.getSource.mockRejectedValueOnce(new Error("offline"));
  await expect(value!.readFollowBaseline("elsewhere")).resolves.toBeNull();
  expect(effects.reportError).not.toHaveBeenCalled();
});

test("without a baseline a stale terminal read never ends the follow; only seeing this run start does", async () => {
  vi.useFakeTimers();
  api.getSource.mockResolvedValue(at(source("elsewhere", "notebook-a", "extracted"), "T0"));
  render(<Harness />);
  commitPage([source("first", "notebook-a")]);
  act(() => value!.trackSourceProgress(["elsewhere"]));
  await act(async () => vi.advanceTimersByTimeAsync(20_000));
  expect(api.getSource.mock.calls.length).toBeGreaterThan(2);
  expect(value!.followedSourceCount).toBe(1);
});

test("following stops on untrackSources, on a notebook switch, and once the followed source is gone", async () => {
  vi.useFakeTimers();
  api.getSource.mockResolvedValue(source("elsewhere", "notebook-a", "parsing"));
  render(<Harness />);
  commitPage([source("first", "notebook-a")]);

  act(() => value!.trackSourceProgress(["elsewhere"]));
  expect(value!.reparsingSourceIds.has("elsewhere")).toBe(true);
  act(() => value!.untrackSources());
  // 跟踪停了,重新解析的占位也一起撤掉。
  expect(value!.reparsingSourceIds.size).toBe(0);
  await act(async () => vi.advanceTimersByTimeAsync(10_000));
  expect(api.getSource).not.toHaveBeenCalled();

  act(() => value!.trackSourceProgress(["elsewhere"]));
  commitPage([source("other", "notebook-b")], "notebook-b");
  expect(value!.reparsingSourceIds.size).toBe(0);
  await act(async () => vi.advanceTimersByTimeAsync(10_000));
  expect(api.getSource).not.toHaveBeenCalled();

  // 跟着的那一篇已经被删了(404):不再跟,也不报错。
  const gone = Object.assign(new Error("not found"), {});
  Object.defineProperty(gone, Symbol.for("silicon-notebook.errors.httpStatus"), { value: 404 });
  api.getSource.mockRejectedValue(gone);
  act(() => value!.trackSourceProgress(["deleted-meanwhile"]));
  expect(value!.reparsingSourceIds.has("deleted-meanwhile")).toBe(true);
  await act(async () => vi.advanceTimersByTimeAsync(1500));
  expect(api.getSource).toHaveBeenCalledTimes(1);
  expect(value!.reparsingSourceIds.size).toBe(0);
  await act(async () => vi.advanceTimersByTimeAsync(30_000));
  expect(api.getSource).toHaveBeenCalledTimes(1);
  expect(effects.reportError).not.toHaveBeenCalled();
});

// ------------------------------------------------------------ 重新解析的占位(全页一份)
// 「重新解析」的每个入口(图谱分析问题清单里引用这篇来源的每一行、来源详情)都读同一份
// `reparsingSourceIds`:从提交起,到跟踪判定这一次跑完(或跟踪被撤掉)为止。

test("a followed reparse holds the placeholder from submission, through stale terminal reads, until this run ends", async () => {
  vi.useFakeTimers();
  const baseline = deferred<SourceSummary>();
  const request = deferred<SourceReparseOutcome>();
  api.getSource
    .mockReturnValueOnce(baseline.promise)
    .mockResolvedValueOnce(at(source("elsewhere", "notebook-a", "extracted"), "T0"))
    .mockResolvedValueOnce(at(source("elsewhere", "notebook-a", "extracted"), "T0"))
    .mockResolvedValueOnce(at(source("elsewhere", "notebook-a", "parsing"), "T1"))
    .mockResolvedValue(at(source("elsewhere", "notebook-a", "extracted"), "T2"));
  render(<Harness />);
  commitPage([source("first", "notebook-a")]);

  let submitted!: Promise<SourceReparseOutcome>;
  act(() => { submitted = value!.submitFollowedReparse("elsewhere", () => request.promise); });
  // 读基准、请求还在路上:已经占着。
  expect(value!.reparsingSourceIds.has("elsewhere")).toBe(true);
  await act(async () => { baseline.resolve(at(source("elsewhere", "notebook-a", "extracted"), "T0")); });
  expect(value!.reparsingSourceIds.has("elsewhere")).toBe(true);
  expect(value!.followedSourceCount).toBe(0);
  let outcome: SourceReparseOutcome | undefined;
  await act(async () => {
    request.resolve("started");
    outcome = await submitted;
  });
  expect(outcome).toBe("started");
  // 排上之后由跟踪接着占,中间没有空档。
  expect(value!.followedSourceCount).toBe(1);
  expect(value!.reparsingSourceIds.has("elsewhere")).toBe(true);

  await act(async () => vi.advanceTimersByTimeAsync(1500));
  await act(async () => vi.advanceTimersByTimeAsync(2250));
  expect(value!.reparsingSourceIds.has("elsewhere")).toBe(true);
  await act(async () => vi.advanceTimersByTimeAsync(3375));
  expect(value!.reparsingSourceIds.has("elsewhere")).toBe(true);
  await act(async () => vi.advanceTimersByTimeAsync(5063));
  expect(value!.reparsingSourceIds.size).toBe(0);
  expect(value!.followedSourceCount).toBe(0);
});

test("a followed reparse reads its baseline before submitting, so a whole run between two reads still ends the follow", async () => {
  vi.useFakeTimers();
  api.getSource
    .mockResolvedValueOnce(at(source("elsewhere", "notebook-a", "extracted"), "T0"))
    .mockResolvedValue(at(source("elsewhere", "notebook-a", "extracted"), "T2"));
  render(<Harness />);
  commitPage([source("first", "notebook-a")]);
  let readsAtSubmit = -1;
  await act(async () => {
    await value!.submitFollowedReparse("elsewhere", async () => {
      readsAtSubmit = api.getSource.mock.calls.length;
      return "started";
    });
  });
  expect(readsAtSubmit).toBe(1);
  expect(value!.reparsingSourceIds.has("elsewhere")).toBe(true);
  await act(async () => vi.advanceTimersByTimeAsync(1500));
  expect(value!.reparsingSourceIds.size).toBe(0);
});

test.each(["none", "failed"] as const)("a followed reparse that did not start (%s) releases the placeholder and follows nothing", async (result) => {
  api.getSource.mockResolvedValue(at(source("elsewhere", "notebook-a"), "T0"));
  render(<Harness />);
  commitPage([source("first", "notebook-a")]);
  let outcome: SourceReparseOutcome | undefined;
  await act(async () => { outcome = await value!.submitFollowedReparse("elsewhere", async () => result); });
  expect(outcome).toBe(result);
  expect(value!.reparsingSourceIds.size).toBe(0);
  expect(value!.followedSourceCount).toBe(0);
});

/** 让 submitFollowedReparse 里已经回来的基准读取把续体跑完(几次微任务)。 */
async function drain() {
  await act(async () => {
    for (let i = 0; i < 6; i += 1) await Promise.resolve();
  });
}

test("a followed reparse whose request returns after a notebook switch is abandoned: no follow, no placeholder, and submit's owner check has turned false", async () => {
  api.getSource.mockResolvedValue(at(source("elsewhere", "notebook-a"), "T0"));
  const request = deferred<SourceReparseOutcome>();
  render(<Harness />);
  commitPage([source("first", "notebook-a")]);
  let stillOwned: (() => boolean) | null = null;
  let submitted!: Promise<SourceReparseOutcome>;
  act(() => {
    submitted = value!.submitFollowedReparse("elsewhere", (check) => {
      stillOwned = check;
      return request.promise;
    });
  });
  // 基准读完、请求在路上:提交时的 owner 还在。
  await drain();
  expect(stillOwned).not.toBeNull();
  expect(stillOwned!()).toBe(true);
  expect(value!.reparsingSourceIds.has("elsewhere")).toBe(true);

  commitPage([source("other", "notebook-b")], "notebook-b");
  // 交给 submit 的那把核对随之变假——page 的 reparseOutcome 据此丢掉提示与刷新(source-reparse-request 的用例)。
  expect(stillOwned!()).toBe(false);
  expect(value!.reparsingSourceIds.size).toBe(0);
  let outcome: SourceReparseOutcome | undefined;
  await act(async () => {
    // 即便 submit 没察觉、照旧回 started,也不跟、不算数。
    request.resolve("started");
    outcome = await submitted;
  });
  expect(outcome).toBe("abandoned");
  expect(value!.followedSourceCount).toBe(0);
  expect(value!.reparsingSourceIds.size).toBe(0);
});

test("a notebook switch while the baseline read is pending: nothing is submitted, and the new notebook's placeholder for the same source stays", async () => {
  const baselineA = deferred<SourceSummary>();
  const baselineB = deferred<SourceSummary>();
  api.getSource.mockReturnValueOnce(baselineA.promise).mockReturnValueOnce(baselineB.promise);
  render(<Harness />);
  commitPage([source("first", "notebook-a")]);
  const submitA = vi.fn(async (): Promise<SourceReparseOutcome> => "started");
  let submittedA!: Promise<SourceReparseOutcome>;
  act(() => { submittedA = value!.submitFollowedReparse("shared", submitA); });
  expect(value!.reparsingSourceIds.has("shared")).toBe(true);

  commitPage([source("other", "notebook-b")], "notebook-b");
  expect(value!.reparsingSourceIds.size).toBe(0);
  // 新的 owner 也提交了同一个 id(它的基准读取还挂着),占着自己那一份。
  const requestB = deferred<SourceReparseOutcome>();
  const submitB = vi.fn(() => requestB.promise);
  let submittedB!: Promise<SourceReparseOutcome>;
  act(() => { submittedB = value!.submitFollowedReparse("shared", submitB); });
  expect(value!.reparsingSourceIds.has("shared")).toBe(true);

  // 旧 owner 的基准读取回来:不提交,只撤自己那一份,新 owner 的占位不受影响。
  let outcome: SourceReparseOutcome | undefined;
  await act(async () => {
    baselineA.resolve(at(source("shared", "notebook-a"), "T0"));
    outcome = await submittedA;
  });
  expect(outcome).toBe("abandoned");
  expect(submitA).not.toHaveBeenCalled();
  expect(value!.reparsingSourceIds.has("shared")).toBe(true);
  expect(value!.followedSourceCount).toBe(0);

  // 新 owner 这一次照常提交、排上、跟踪。
  await act(async () => { baselineB.resolve(at(source("shared", "notebook-b"), "T0")); });
  await drain();
  expect(submitB).toHaveBeenCalledTimes(1);
  await act(async () => {
    requestB.resolve("started");
    await submittedB;
  });
  expect(value!.followedSourceCount).toBe(1);
  expect(value!.reparsingSourceIds.has("shared")).toBe(true);
});

test.each([
  ["an account switch", { actorId: "user-b" }],
  ["losing write access", { canWrite: false }],
] as const)("%s while the baseline read is pending: nothing is submitted", async (_label, props) => {
  const baseline = deferred<SourceSummary>();
  api.getSource.mockReturnValueOnce(baseline.promise);
  const view = render(<Harness />);
  commitPage([source("first", "notebook-a")]);
  const submit = vi.fn(async (): Promise<SourceReparseOutcome> => "started");
  let submitted!: Promise<SourceReparseOutcome>;
  act(() => { submitted = value!.submitFollowedReparse("elsewhere", submit); });
  view.rerender(<Harness {...props} />);
  let outcome: SourceReparseOutcome | undefined;
  await act(async () => {
    baseline.resolve(at(source("elsewhere", "notebook-a"), "T0"));
    outcome = await submitted;
  });
  expect(outcome).toBe("abandoned");
  expect(submit).not.toHaveBeenCalled();
  expect(value!.followedSourceCount).toBe(0);
});

test("without an owner or write access a followed reparse is not even started", async () => {
  const submit = vi.fn(async (): Promise<SourceReparseOutcome> => "started");
  const view = render(<Harness />);
  await expect(value!.submitFollowedReparse("elsewhere", submit)).resolves.toBe("abandoned");
  commitPage([source("first", "notebook-a")]);
  view.rerender(<Harness canWrite={false} />);
  await expect(value!.submitFollowedReparse("elsewhere", submit)).resolves.toBe("abandoned");
  expect(submit).not.toHaveBeenCalled();
  expect(api.getSource).not.toHaveBeenCalled();
  expect(value!.reparsingSourceIds.size).toBe(0);
});

test("the placeholder is released when the follow runs out of attempts", async () => {
  vi.useFakeTimers();
  // 一直读到上一次的终态(updated_at 没动):跟踪自己判定不了这一次跑完,只能等尝试次数用尽。
  api.getSource.mockResolvedValue(at(source("elsewhere", "notebook-a", "extracted"), "T0"));
  render(<Harness />);
  commitPage([source("first", "notebook-a")]);
  await act(async () => { await value!.submitFollowedReparse("elsewhere", async () => "started"); });
  expect(value!.reparsingSourceIds.has("elsewhere")).toBe(true);
  await act(async () => vi.advanceTimersByTimeAsync(10 * 60 * 1000));
  expect(value!.reparsingSourceIds.has("elsewhere")).toBe(true);
  await act(async () => vi.advanceTimersByTimeAsync(60 * 60 * 1000));
  expect(effects.setStatusText).toHaveBeenCalledWith("处理超时：部分来源长时间未完成，请稍后重试");
  expect(value!.reparsingSourceIds.size).toBe(0);
  expect(value!.followedSourceCount).toBe(0);
});

test("the source detail's reparse is refused while that source holds the placeholder, and its own reparse holds it while in flight", async () => {
  vi.useFakeTimers();
  api.getSource.mockResolvedValue(at(source("here", "notebook-a"), "T0"));
  api.getNotebookSource.mockResolvedValue(source("here", "notebook-a"));
  api.getNotebookSourceElementsPage.mockResolvedValue({ items: [], total_count: 0, offset: 0, limit: 40 });
  render(<Harness />);
  commitPage([source("here", "notebook-a")]);
  await act(async () => { await value!.openSourceById("here"); });
  expect(value!.sourceDetail?.id).toBe("here");

  // 图谱分析里提交的重新解析还没跑完:来源详情的「重新解析」不再发一次。
  await act(async () => { await value!.submitFollowedReparse("here", async () => "started"); });
  expect(value!.reparsingSourceIds.has("here")).toBe(true);
  await act(async () => value!.reparseSource());
  expect(api.parseSource).not.toHaveBeenCalled();
  expect(value!.reparsingSource).toBe(false);

  // 跟踪撤掉之后才能点;详情自己的重新解析在飞期间同样占着这篇来源(问题清单那边据此置灰)。
  act(() => value!.untrackSources());
  expect(value!.reparsingSourceIds.size).toBe(0);
  const parsed = deferred<SourceSummary>();
  api.parseSource.mockReturnValueOnce(parsed.promise);
  let reparsing!: Promise<void>;
  act(() => { reparsing = value!.reparseSource(); });
  expect(api.parseSource).toHaveBeenCalledWith("here");
  expect(value!.reparsingSource).toBe(true);
  expect(value!.reparsingSourceIds.has("here")).toBe(true);
  parsed.resolve(source("here", "notebook-a"));
  await act(async () => reparsing);
  expect(value!.reparsingSource).toBe(false);
  expect(value!.reparsingSourceIds.size).toBe(0);
});

// ------------------------------------------------------------ 跟踪流程的穷举矩阵(codex 第 7 轮)
// 每个 await 点 × 每个取消/变更事件。统一收口在两把核对上:跟踪代次(followEpochRef:
// untrack / 换 owner / 换账号 / 卸载都推进它,续体在 await 之后核对)与提交时的归属
// (owner + 写权限 + 仍挂载)。代次作废只阻止前端登记跟踪与占位,不取消已发出的作业。

const gone404 = () => {
  const error = new Error("not found");
  Object.defineProperty(error, Symbol.for("silicon-notebook.errors.httpStatus"), { value: 404 });
  return error;
};

type View = ReturnType<typeof render>;
type FollowEvent = readonly [label: string, fire: (view: View) => void];
const PANEL_CLOSE: FollowEvent = ["the analysis panel closing (untrackSources)", () => act(() => value!.untrackSources())];
const NOTEBOOK_SWITCH: FollowEvent = ["a notebook switch", () => commitPage([source("other", "notebook-b")], "notebook-b")];
const ACCOUNT_SWITCH: FollowEvent = ["an account switch", (view) => view.rerender(<Harness actorId="user-b" />)];
const WRITE_LOSS: FollowEvent = ["losing write access", (view) => view.rerender(<Harness canWrite={false} />)];
const UNMOUNT: FollowEvent = ["unmount", (view) => view.unmount()];

test.each(
  (["baseline read", "request"] as const).flatMap((phase) => (
    [PANEL_CLOSE, NOTEBOOK_SWITCH, ACCOUNT_SWITCH, WRITE_LOSS, UNMOUNT].map(([label, fire]) => [phase, label, fire] as const)
  )),
)("submission with its %s pending, then %s", async (phase, label, fire) => {
  const baseline = deferred<SourceSummary>();
  const request = deferred<SourceReparseOutcome>();
  api.getSource.mockReturnValueOnce(baseline.promise);
  const view = render(<Harness />);
  commitPage([source("first", "notebook-a")]);
  let check: (() => boolean) | null = null;
  const submit = vi.fn((stillOwned: () => boolean) => {
    check = stillOwned;
    return request.promise;
  });
  let submitted!: Promise<SourceReparseOutcome>;
  act(() => { submitted = value!.submitFollowedReparse("elsewhere", submit); });
  if (phase === "request") {
    await act(async () => { baseline.resolve(at(source("elsewhere", "notebook-a"), "T0")); });
    await drain();
    expect(submit).toHaveBeenCalledTimes(1);
  }
  fire(view);
  const panelClosed = label === PANEL_CLOSE[0];
  const mounted = label !== UNMOUNT[0];
  // 关面板 / 切库 / 切账号:占位当场撤掉(切账号是整份视图随 owner 隐藏)。
  if (mounted && label !== WRITE_LOSS[0]) expect(value!.reparsingSourceIds.size).toBe(0);
  let outcome: SourceReparseOutcome | undefined;
  await act(async () => {
    baseline.resolve(at(source("elsewhere", "notebook-a"), "T0"));
    request.resolve("started");
    outcome = await submitted;
  });
  if (panelClosed) {
    // 同一工作区里关面板不取消已确认的重新解析:请求照发、提示照出(归属仍在),只是不再登记跟踪。
    expect(submit).toHaveBeenCalledTimes(1);
    expect(check!()).toBe(true);
    expect(outcome).toBe("started");
  } else {
    // 归属变了:请求还没发就不发;已经发了,结果作废(不提示、不刷新、不跟)。
    expect(submit).toHaveBeenCalledTimes(phase === "request" ? 1 : 0);
    if (phase === "request") expect(check!()).toBe(false);
    expect(outcome).toBe("abandoned");
  }
  if (mounted) {
    expect(value!.followedSourceCount).toBe(0);
    expect(value!.reparsingSourceIds.size).toBe(0);
  }
});

test("a 404 on the baseline read still submits (no baseline); a request that schedules nothing follows nothing", async () => {
  api.getSource.mockRejectedValueOnce(gone404());
  render(<Harness />);
  commitPage([source("first", "notebook-a")]);
  const submit = vi.fn(async (): Promise<SourceReparseOutcome> => "none");
  let outcome: SourceReparseOutcome | undefined;
  await act(async () => { outcome = await value!.submitFollowedReparse("gone", submit); });
  expect(submit).toHaveBeenCalledTimes(1);
  expect(outcome).toBe("none");
  expect(value!.followedSourceCount).toBe(0);
  expect(value!.reparsingSourceIds.size).toBe(0);
  expect(effects.reportError).not.toHaveBeenCalled();
});

test("the attempt cap drops the follows it budgeted, not a submission still in flight: that one registers later with a fresh budget", async () => {
  vi.useFakeTimers();
  // 一直读到上一次的终态(updated_at 没动):这些跟踪自己判定不了跑完。
  api.getSource.mockImplementation(async (id: string) => at(source(id, "notebook-a", "extracted"), "T0"));
  render(<Harness />);
  commitPage([source("first", "notebook-a")]);
  act(() => value!.trackSourceProgress(["stuck"], { stuck: "T0" }));
  const request = deferred<SourceReparseOutcome>();
  let submitted!: Promise<SourceReparseOutcome>;
  act(() => { submitted = value!.submitFollowedReparse("fresh", () => request.promise); });
  await act(async () => vi.advanceTimersByTimeAsync(60 * 60 * 1000));
  expect(effects.setStatusText).toHaveBeenCalledWith("处理超时：部分来源长时间未完成，请稍后重试");
  expect(value!.reparsingSourceIds.has("stuck")).toBe(false);
  expect(value!.reparsingSourceIds.has("fresh")).toBe(true);

  await act(async () => {
    request.resolve("started");
    await submitted;
  });
  expect(value!.followedSourceCount).toBe(1);
  expect(value!.reparsingSourceIds.has("fresh")).toBe(true);
  const reads = api.getSource.mock.calls.length;
  await act(async () => vi.advanceTimersByTimeAsync(1500));
  expect(api.getSource.mock.calls.length).toBeGreaterThan(reads);
});

test.each(["its baseline read", "its request", "its follow", "the source detail's own reparse"] as const)(
  "a second submission for a source while %s holds the placeholder is refused: no read, no request",
  async (holder) => {
    const baseline = deferred<SourceSummary>();
    const request = deferred<SourceReparseOutcome>();
    const parsed = deferred<SourceSummary>();
    // 只有先来的那次提交读基准(挂着);其余读取立刻回来——放行了重复提交的话当场断言失败,不挂住。
    api.getSource.mockResolvedValue(at(source("x", "notebook-a"), "T0"));
    if (holder === "its baseline read" || holder === "its request") api.getSource.mockReturnValueOnce(baseline.promise);
    api.getNotebookSource.mockResolvedValue(source("x", "notebook-a"));
    api.getNotebookSourceElementsPage.mockResolvedValue({ items: [], total_count: 0, offset: 0, limit: 40 });
    api.parseSource.mockReturnValueOnce(parsed.promise);
    render(<Harness />);
    commitPage([source("first", "notebook-a")]);
    let first: Promise<unknown> | null = null;
    if (holder === "its baseline read" || holder === "its request") {
      act(() => { first = value!.submitFollowedReparse("x", () => request.promise); });
      if (holder === "its request") {
        await act(async () => { baseline.resolve(at(source("x", "notebook-a"), "T0")); });
        await drain();
      }
    } else if (holder === "its follow") {
      act(() => value!.trackSourceProgress(["x"]));
    } else {
      await act(async () => { await value!.openSourceById("x"); });
      act(() => { first = value!.reparseSource(); });
    }
    expect(value!.reparsingSourceIds.has("x")).toBe(true);
    const reads = api.getSource.mock.calls.length;
    const second = vi.fn(async (): Promise<SourceReparseOutcome> => "started");
    let outcome: SourceReparseOutcome | undefined;
    await act(async () => { outcome = await value!.submitFollowedReparse("x", second); });
    expect(outcome).toBe("abandoned");
    expect(second).not.toHaveBeenCalled();
    expect(api.getSource.mock.calls.length).toBe(reads);
    // 先来的那一份不受影响。
    expect(value!.reparsingSourceIds.has("x")).toBe(true);
    await act(async () => {
      baseline.resolve(at(source("x", "notebook-a"), "T0"));
      request.resolve("none");
      parsed.resolve(source("x", "notebook-a"));
      await first;
    });
  },
);

test("the source detail's reparse is refused while a findings submission for that source is still in flight", async () => {
  const baseline = deferred<SourceSummary>();
  api.getSource.mockReturnValueOnce(baseline.promise);
  api.getNotebookSource.mockResolvedValue(source("x", "notebook-a"));
  api.getNotebookSourceElementsPage.mockResolvedValue({ items: [], total_count: 0, offset: 0, limit: 40 });
  render(<Harness />);
  commitPage([source("x", "notebook-a")]);
  await act(async () => { await value!.openSourceById("x"); });
  let submitted!: Promise<SourceReparseOutcome>;
  act(() => { submitted = value!.submitFollowedReparse("x", async () => "none"); });
  await act(async () => value!.reparseSource());
  expect(api.parseSource).not.toHaveBeenCalled();
  expect(value!.reparsingSource).toBe(false);
  await act(async () => {
    baseline.resolve(at(source("x", "notebook-a"), "T0"));
    await submitted;
  });
  expect(value!.reparsingSourceIds.size).toBe(0);
});

test.each([ACCOUNT_SWITCH, UNMOUNT])("a registered follow between two reads, then %s: no more reads", async (label, fire) => {
  vi.useFakeTimers();
  api.getSource.mockResolvedValue(source("elsewhere", "notebook-a", "parsing"));
  const view = render(<Harness />);
  commitPage([source("first", "notebook-a")]);
  act(() => value!.trackSourceProgress(["elsewhere"]));
  fire(view);
  await act(async () => vi.advanceTimersByTimeAsync(30_000));
  expect(api.getSource).not.toHaveBeenCalled();
  if (label === ACCOUNT_SWITCH[0]) expect(value!.reparsingSourceIds.size).toBe(0);
});

test("losing write access does not stop observing a registered follow: the accepted run is still read to its end", async () => {
  vi.useFakeTimers();
  api.getSource
    .mockResolvedValueOnce(at(source("elsewhere", "notebook-a", "parsing"), "T1"))
    .mockResolvedValue(at(source("elsewhere", "notebook-a", "extracted"), "T2"));
  const view = render(<Harness />);
  commitPage([source("first", "notebook-a")]);
  const initial = value!.contentRevision;
  act(() => value!.trackSourceProgress(["elsewhere"], { elsewhere: "T0" }));
  view.rerender(<Harness canWrite={false} />);
  await act(async () => vi.advanceTimersByTimeAsync(1500));
  expect(value!.followedSourceCount).toBe(1);
  await act(async () => vi.advanceTimersByTimeAsync(2250));
  expect(value!.followedSourceCount).toBe(0);
  expect(value!.contentRevision).toBeGreaterThan(initial);
  expect(effects.refreshCheckup).toHaveBeenCalledWith("notebook-a", expect.any(Function));
});

test.each([PANEL_CLOSE, NOTEBOOK_SWITCH, ACCOUNT_SWITCH, UNMOUNT])(
  "an off-page follow read in flight, then %s: its result is void (no revision bump, no refresh)",
  async (label, fire) => {
    vi.useFakeTimers();
    // 当前页上另有一篇一直在处理中:轮询器不因跟踪集合清空而停,在途那一轮照常回来。
    const read = deferred<SourceSummary>();
    api.getSource.mockImplementation((id: string) => (
      id === "elsewhere" ? read.promise : Promise.resolve(source("busy", "notebook-a", "parsing"))
    ));
    const view = render(<Harness />);
    commitPage([source("busy", "notebook-a", "parsing")]);
    act(() => value!.trackSourceProgress(["elsewhere"], { elsewhere: "T0" }));
    await act(async () => vi.advanceTimersByTimeAsync(1500));
    expect(api.getSource).toHaveBeenCalledWith("elsewhere");
    fire(view);
    const mounted = label !== UNMOUNT[0];
    const revision = mounted ? value!.contentRevision : 0;
    // 读回来的是「这一次跑完了」:作废的那一轮不得据此推进内容版本、刷新体检与列表。
    await act(async () => { read.resolve(at(source("elsewhere", "notebook-a", "extracted"), "T2")); });
    expect(effects.refreshCheckup).not.toHaveBeenCalled();
    expect(effects.refreshCollection).not.toHaveBeenCalled();
    if (mounted) {
      expect(value!.contentRevision).toBe(revision);
      expect(value!.followedSourceCount).toBe(0);
    }
  },
);

test("an off-page follow read in flight, the panel closes and the same source is submitted again: the stale read cannot end the new follow", async () => {
  vi.useFakeTimers();
  const read = deferred<SourceSummary>();
  api.getSource.mockImplementation((id: string) => (
    id === "elsewhere" ? read.promise : Promise.resolve(source("busy", "notebook-a", "parsing"))
  ));
  render(<Harness />);
  commitPage([source("busy", "notebook-a", "parsing")]);
  act(() => value!.trackSourceProgress(["elsewhere"], { elsewhere: "T0" }));
  await act(async () => vi.advanceTimersByTimeAsync(1500));
  act(() => value!.untrackSources());
  // 新的一次:基准已是 T2(上一次早已跑完)。
  act(() => value!.trackSourceProgress(["elsewhere"], { elsewhere: "T2" }));
  // 旧那一轮读回 extracted T2——对旧跟踪是「跑完」,对新跟踪只是上一次的终态。
  await act(async () => { read.resolve(at(source("elsewhere", "notebook-a", "extracted"), "T2")); });
  expect(value!.followedSourceCount).toBe(1);
  expect(value!.reparsingSourceIds.has("elsewhere")).toBe(true);
});

test.each([PANEL_CLOSE, NOTEBOOK_SWITCH, ACCOUNT_SWITCH, UNMOUNT])(
  "a page read of a followed on-page source in flight, then %s",
  async (label, fire) => {
    vi.useFakeTimers();
    const read = deferred<SourceSummary>();
    api.getSource.mockImplementation(() => read.promise);
    const view = render(<Harness />);
    commitPage([source("here", "notebook-a", "parsing")]);
    act(() => value!.trackSourceProgress(["here"]));
    await act(async () => vi.advanceTimersByTimeAsync(1500));
    expect(api.getSource).toHaveBeenCalledWith("here");
    fire(view);
    await act(async () => { read.resolve(source("here", "notebook-a", "extracted")); });
    if (label === PANEL_CLOSE[0]) {
      // 页内的行照常更新(那是来源列表自己的轮询),跟踪不会被这次结果重新建起来。
      expect(value!.sources[0]?.parse_status).toBe("extracted");
      expect(value!.followedSourceCount).toBe(0);
      expect(value!.reparsingSourceIds.size).toBe(0);
    } else {
      // 换了 owner / 卸载:这一轮整个作废。
      expect(effects.refreshCollection).not.toHaveBeenCalled();
      expect(effects.refreshCheckup).not.toHaveBeenCalled();
      if (label === NOTEBOOK_SWITCH[0]) expect(value!.sources.map((item) => item.id)).toEqual(["other"]);
    }
  },
);

test.each([true, false])("a pending source on the current page that reads as gone (followed: %s) leaves the page, which reloads -- no error, no more reads", async (followed) => {
  vi.useFakeTimers();
  api.getSource.mockRejectedValue(gone404());
  api.listSources.mockResolvedValue({ items: [source("other", "notebook-a")], total_count: 1, offset: 0, limit: 50 });
  render(<Harness />);
  commitPage([source("here", "notebook-a", "parsing"), source("other", "notebook-a")]);
  if (followed) act(() => value!.trackSourceProgress(["here"]));
  const initial = value!.contentRevision;
  await act(async () => vi.advanceTimersByTimeAsync(1500));
  expect(value!.followedSourceCount).toBe(0);
  expect(value!.reparsingSourceIds.size).toBe(0);
  expect(value!.sources.map((item) => item.id)).toEqual(["other"]);
  expect(value!.contentRevision).toBeGreaterThan(initial);
  expect(api.listSources).toHaveBeenCalledWith("notebook-a", 0, 50, "", expect.any(AbortSignal));
  expect(effects.reportError).not.toHaveBeenCalled();
  const reads = api.getSource.mock.calls.length;
  await act(async () => vi.advanceTimersByTimeAsync(30_000));
  expect(api.getSource.mock.calls.length).toBe(reads);
});

test.each([
  PANEL_CLOSE,
  NOTEBOOK_SWITCH,
  ACCOUNT_SWITCH,
  WRITE_LOSS,
  ["the request failing (the source was deleted, 404)", () => undefined] as FollowEvent,
])("the source detail's own reparse in flight, then %s", async (label, fire) => {
  api.getNotebookSource.mockResolvedValue(source("here", "notebook-a"));
  api.getNotebookSourceElementsPage.mockResolvedValue({ items: [], total_count: 0, offset: 0, limit: 40 });
  const parsed = deferred<SourceSummary>();
  api.parseSource.mockReturnValueOnce(parsed.promise);
  const view = render(<Harness />);
  commitPage([source("here", "notebook-a")]);
  await act(async () => { await value!.openSourceById("here"); });
  let reparsing!: Promise<void>;
  act(() => { reparsing = value!.reparseSource(); });
  expect(value!.reparsingSourceIds.has("here")).toBe(true);
  fire(view);
  const sameWorkspace = label === PANEL_CLOSE[0] || label === WRITE_LOSS[0];
  const failing = label.startsWith("the request failing");
  // 关面板只作废跟踪;详情自己的请求还在路上,占位照旧。换了 owner:占位随 owner 撤掉。
  expect(value!.reparsingSourceIds.has("here")).toBe(sameWorkspace || failing);
  await act(async () => {
    if (failing) parsed.reject(gone404());
    else parsed.resolve(source("here", "notebook-a"));
    await reparsing;
  });
  expect(value!.reparsingSourceIds.size).toBe(0);
  if (sameWorkspace) expect(effects.setToast).toHaveBeenCalledWith("Source 已重新解析");
  else expect(effects.setToast).not.toHaveBeenCalledWith("Source 已重新解析");
  if (failing) expect(effects.reportError).toHaveBeenCalledTimes(1);
  else expect(effects.reportError).not.toHaveBeenCalled();
});

test("a slow or failing checkup refresh does not delay terminal poll scheduling", async () => {
  vi.useFakeTimers();
  const checkup = deferred<undefined>();
  effects.refreshCheckup.mockReturnValueOnce(checkup.promise);
  api.getSource
    .mockResolvedValueOnce(source("pending", "notebook-a", "extracted"))
    .mockResolvedValueOnce(source("pending-2", "notebook-a", "parsing"))
    .mockResolvedValue(source("pending-2", "notebook-a", "parsing"));
  render(<Harness />);
  act(() => {
    value!.commitNotebookSnapshot({
      actorId: "user-a",
      notebookId: "notebook-a",
      workspaceEpoch: 1,
      page: {
        items: [
          source("pending", "notebook-a", "parsing"),
          source("pending-2", "notebook-a", "parsing"),
        ],
        total_count: 2,
        offset: 0,
        limit: 50,
      },
    });
  });

  await act(async () => vi.advanceTimersByTimeAsync(1500));
  expect(effects.refreshCheckup).toHaveBeenCalledTimes(1);
  await act(async () => vi.advanceTimersByTimeAsync(2250));
  expect(api.getSource).toHaveBeenCalledTimes(3);
  checkup.reject(new Error("transient checkup"));
  await act(async () => Promise.resolve());
  expect(effects.reportError).not.toHaveBeenCalled();
});

test.each(["extracted", "failed"])("terminal polling refresh for %s survives the hasPending cleanup but remains owner-bound", async (terminalStatus) => {
  vi.useFakeTimers();
  api.getSource.mockResolvedValue(source("pending", "notebook-a", terminalStatus));
  render(<Harness />);
  act(() => {
    value!.commitNotebookSnapshot({
      actorId: "user-a",
      notebookId: "notebook-a",
      workspaceEpoch: 1,
      page: {
        items: [source("pending", "notebook-a", "parsing")],
        total_count: 1,
        offset: 0,
        limit: 50,
      },
    });
  });

  await act(async () => {
    await vi.advanceTimersByTimeAsync(1500);
  });

  expect(api.getSource).toHaveBeenCalledTimes(1);
  expect(effects.refreshCollection).toHaveBeenCalledTimes(1);
  expect(effects.refreshNotebook).toHaveBeenCalledWith("notebook-a", expect.any(Function));
  if (terminalStatus === "extracted") {
    expect(effects.refreshCheckup).toHaveBeenCalledWith("notebook-a", expect.any(Function));
  } else {
    expect(effects.refreshCheckup).not.toHaveBeenCalled();
    expect(effects.setStatusText).toHaveBeenCalledWith("来源处理失败：pending.md");
  }
  expect(value!.sources[0]?.parse_status).toBe(terminalStatus);
});

test("a detail response cannot cross a notebook transition", async () => {
  const detail = deferred<SourceSummary>();
  const elements = deferred<{
    items: never[];
    total_count: number;
    offset: number;
    limit: number;
  }>();
  api.getNotebookSource.mockReturnValue(detail.promise);
  api.getNotebookSourceElementsPage.mockReturnValue(elements.promise);
  render(<Harness />);
  act(() => {
    value!.commitNotebookSnapshot({
      actorId: "user-a",
      notebookId: "notebook-a",
      workspaceEpoch: 1,
      page: { items: [], total_count: 0, offset: 0, limit: 50 },
    });
  });
  const opening = value!.openSourceById("source-a", "element-a");
  act(() => {
    value!.beginTransition();
    value!.commitNotebookSnapshot({
      actorId: "user-a",
      notebookId: "notebook-b",
      workspaceEpoch: 2,
      page: { items: [], total_count: 0, offset: 0, limit: 50 },
    });
  });
  detail.resolve(source("source-a", "notebook-a"));
  elements.resolve({ items: [], total_count: 0, offset: 0, limit: 40 });
  await act(async () => opening);

  expect(api.getNotebookSource).toHaveBeenCalledWith("notebook-a", "source-a");
  expect(api.getNotebookSourceElementsPage).toHaveBeenCalledTimes(1);
  expect(value!.sourceDetail).toBeNull();
  expect(value!.highlightedElementId).toBe("");
});

test("latest source page wins without an extra list request", async () => {
  const older = deferred<{ items: SourceSummary[]; total_count: number; offset: number; limit: number }>();
  const newer = deferred<{ items: SourceSummary[]; total_count: number; offset: number; limit: number }>();
  api.listSources.mockReturnValueOnce(older.promise).mockReturnValueOnce(newer.promise);
  render(<Harness />);
  act(() => {
    value!.commitNotebookSnapshot({
      actorId: "user-a",
      notebookId: "notebook-a",
      workspaceEpoch: 1,
      page: { items: [], total_count: 100, offset: 0, limit: 50 },
    });
  });
  let first!: Promise<void>;
  act(() => {
    first = value!.loadSourcesPage({ page: 0, q: "older" });
  });
  let second!: Promise<void>;
  act(() => {
    second = value!.loadSourcesPage({ page: 1, q: "newer" });
  });
  newer.resolve({
    items: [source("newer", "notebook-a")],
    total_count: 100,
    offset: 50,
    limit: 50,
  });
  await act(async () => second);
  older.resolve({
    items: [source("older", "notebook-a")],
    total_count: 100,
    offset: 0,
    limit: 50,
  });
  await act(async () => first);

  expect(api.listSources).toHaveBeenCalledTimes(2);
  expect(value!.sources.map((item) => item.id)).toEqual(["newer"]);
  expect(value!.sourcesPage).toBe(1);
});

test("loadSourcesPage reports busy while the source-list request is in flight", async () => {
  const page = deferred<{ items: SourceSummary[]; total_count: number; offset: number; limit: number }>();
  api.listSources.mockReturnValueOnce(page.promise);
  render(<Harness />);
  act(() => {
    value!.commitNotebookSnapshot({
      actorId: "user-a",
      notebookId: "notebook-a",
      workspaceEpoch: 1,
      page: { items: [], total_count: 0, offset: 0, limit: 50 },
    });
  });
  expect(value!.sourcesPageLoading).toBe(false);

  let loading!: Promise<void>;
  act(() => {
    loading = value!.loadSourcesPage({ page: 0, q: "" });
  });
  expect(value!.sourcesPageLoading).toBe(true);

  page.resolve({ items: [], total_count: 0, offset: 0, limit: 50 });
  await act(async () => loading);
  expect(value!.sourcesPageLoading).toBe(false);
});

// A superseding request must cancel the one it replaces for real (an actual
// AbortController#abort(), not just a discarded response) — see the
// `pageRequestRef`-only design this hook used to use, where a stale response
// still made it all the way back from the server. The stale request's
// eventual AbortError-shaped rejection must also stay invisible to callers:
// `isCurrent()` is always false for it by construction (superseding always
// bumps `pageRequestRef.current` before/while aborting), so `loadSourcesPage`
// takes the silent `return` branch, never the `throw` one, for that request.
test("a superseding source page request aborts the stale one without surfacing its rejection", async () => {
  const older = deferred<{ items: SourceSummary[]; total_count: number; offset: number; limit: number }>();
  const newer = deferred<{ items: SourceSummary[]; total_count: number; offset: number; limit: number }>();
  api.listSources.mockReturnValueOnce(older.promise).mockReturnValueOnce(newer.promise);
  render(<Harness />);
  act(() => {
    value!.commitNotebookSnapshot({
      actorId: "user-a",
      notebookId: "notebook-a",
      workspaceEpoch: 1,
      page: { items: [], total_count: 100, offset: 0, limit: 50 },
    });
  });

  let first!: Promise<void>;
  act(() => {
    first = value!.loadSourcesPage({ page: 0, q: "older" });
  });
  const firstSignal = api.listSources.mock.calls[0]?.[4] as AbortSignal;
  expect(firstSignal).toBeInstanceOf(AbortSignal);
  expect(firstSignal.aborted).toBe(false);

  let second!: Promise<void>;
  act(() => {
    second = value!.loadSourcesPage({ page: 1, q: "newer" });
  });
  // The stale request must be cancelled for real, not merely outvoted.
  expect(firstSignal.aborted).toBe(true);
  // Busy must not flicker off between the superseded and superseding request.
  expect(value!.sourcesPageLoading).toBe(true);

  older.reject(new DOMException("aborted", "AbortError"));
  await act(async () => {
    await expect(first).resolves.toBeUndefined();
  });
  expect(effects.reportError).not.toHaveBeenCalled();
  // The still in-flight superseding request keeps busy asserted.
  expect(value!.sourcesPageLoading).toBe(true);

  newer.resolve({
    items: [source("newer", "notebook-a")],
    total_count: 100,
    offset: 50,
    limit: 50,
  });
  await act(async () => second);
  expect(value!.sourcesPageLoading).toBe(false);
  expect(value!.sources.map((item) => item.id)).toEqual(["newer"]);
});

test("a clamp-triggered second request stays within the same busy window", async () => {
  const requested = deferred<{ items: SourceSummary[]; total_count: number; offset: number; limit: number }>();
  const clamped = deferred<{ items: SourceSummary[]; total_count: number; offset: number; limit: number }>();
  api.listSources.mockReturnValueOnce(requested.promise).mockReturnValueOnce(clamped.promise);
  render(<Harness />);
  act(() => {
    value!.commitNotebookSnapshot({
      actorId: "user-a",
      notebookId: "notebook-a",
      workspaceEpoch: 1,
      page: { items: [], total_count: 300, offset: 0, limit: 50 },
    });
  });

  let loading!: Promise<void>;
  act(() => {
    // Page 5 is requested while the server still reports 300 rows (last page 5).
    loading = value!.loadSourcesPage({ page: 5, q: "" });
  });
  expect(value!.sourcesPageLoading).toBe(true);

  // The total shrank server-side: page 5 no longer exists, so
  // `clampSourcePage` re-fetches page 1 (the new last page) within this same
  // call. Busy must hold steady across that internal re-fetch.
  act(() => {
    requested.resolve({ items: [], total_count: 60, offset: 250, limit: 50 });
  });
  await act(async () => {
    await Promise.resolve();
  });
  expect(api.listSources).toHaveBeenCalledTimes(2);
  expect(value!.sourcesPageLoading).toBe(true);
  // Both `listSources` calls inside one busy window must share the same
  // AbortController — a fresh controller for the clamp-triggered second
  // call would leave the first one's abort unable to cancel it.
  const firstSignal = api.listSources.mock.calls[0]?.[4] as AbortSignal;
  const secondSignal = api.listSources.mock.calls[1]?.[4] as AbortSignal;
  expect(secondSignal).toBeInstanceOf(AbortSignal);
  expect(secondSignal).toBe(firstSignal);
  expect(secondSignal.aborted).toBe(false);

  clamped.resolve({
    items: [source("clamped", "notebook-a")],
    total_count: 60,
    offset: 50,
    limit: 50,
  });
  await act(async () => loading);
  expect(value!.sourcesPageLoading).toBe(false);
  expect(value!.sourcesPage).toBe(1);
  expect(value!.sources.map((item) => item.id)).toEqual(["clamped"]);
});

// PR review P0: `isCurrent()` going false must not always mean "release
// busy" — only when this call is still the busy window's holder
// (`requestId === pageRequestRef.current`, i.e. no superseding request has
// taken over). A caller-supplied `guard()` veto with no owner/actor
// transition (the exact shape of page.tsx's poll-completion call sites,
// whose own effect cleanup can flip `cancelled` before the response lands)
// used to leave `sourcesPageLoading` stuck `true` forever, because the old
// code released busy only when `isCurrent()` was true.
test("a guard veto releases busy and never commits the vetoed response", async () => {
  const page = deferred<{ items: SourceSummary[]; total_count: number; offset: number; limit: number }>();
  api.listSources.mockReturnValueOnce(page.promise);
  render(<Harness />);
  act(() => {
    value!.commitNotebookSnapshot({
      actorId: "user-a",
      notebookId: "notebook-a",
      workspaceEpoch: 1,
      page: {
        items: [source("existing", "notebook-a")],
        total_count: 1,
        offset: 0,
        limit: 50,
      },
    });
  });

  let loading!: Promise<void>;
  act(() => {
    loading = value!.loadSourcesPage({ page: 0, q: "", guard: () => false });
  });
  expect(value!.sourcesPageLoading).toBe(true);

  page.resolve({
    items: [source("vetoed", "notebook-a")],
    total_count: 1,
    offset: 0,
    limit: 50,
  });
  await act(async () => loading);

  expect(value!.sourcesPageLoading).toBe(false);
  expect(effects.reportError).not.toHaveBeenCalled();
  expect(value!.sourcesTotal).toBe(1);
  expect(value!.sources.map((item) => item.id)).toEqual(["existing"]);
});

// Same P0 shape as the guard-veto test above, but for the clamp-triggered
// *second* `listSources` call's success path (~415): the first response
// resolves fine (guard still passes) and shrinks `total_count` enough to
// trigger `clampSourcePage`'s internal re-fetch, then the guard is vetoed
// before the second, clamp-triggered response lands. `requestId` still
// equals `pageRequestRef.current` throughout (nobody superseded this call),
// so nothing else is coming to release busy — this call must still release
// it itself via the same `releaseBusyIfWindowHolder()` path, and must not
// commit the vetoed response.
test("a guard veto after a clamp-triggered second request still releases busy", async () => {
  const requested = deferred<{ items: SourceSummary[]; total_count: number; offset: number; limit: number }>();
  const clamped = deferred<{ items: SourceSummary[]; total_count: number; offset: number; limit: number }>();
  api.listSources.mockReturnValueOnce(requested.promise).mockReturnValueOnce(clamped.promise);
  render(<Harness />);
  act(() => {
    value!.commitNotebookSnapshot({
      actorId: "user-a",
      notebookId: "notebook-a",
      workspaceEpoch: 1,
      page: { items: [], total_count: 300, offset: 0, limit: 50 },
    });
  });

  let guardPass = true;
  const guard = () => guardPass;

  let loading!: Promise<void>;
  act(() => {
    loading = value!.loadSourcesPage({ page: 5, q: "", guard });
  });
  expect(value!.sourcesPageLoading).toBe(true);

  // The total shrank server-side, triggering the clamp-driven re-fetch —
  // guard still passes for this first response.
  act(() => {
    requested.resolve({ items: [], total_count: 60, offset: 250, limit: 50 });
  });
  await act(async () => {
    await Promise.resolve();
  });
  expect(api.listSources).toHaveBeenCalledTimes(2);
  expect(value!.sourcesPageLoading).toBe(true);

  // Guard is vetoed before the clamp-triggered second response lands.
  guardPass = false;
  clamped.resolve({
    items: [source("clamped", "notebook-a")],
    total_count: 60,
    offset: 50,
    limit: 50,
  });
  await act(async () => loading);

  expect(value!.sourcesPageLoading).toBe(false);
  expect(effects.reportError).not.toHaveBeenCalled();
  expect(value!.sources).toEqual([]);
});

// Same P0 shape, but for the *first* `listSources` call's error path (~377):
// a genuine network failure (not an abort) rejects while the guard is
// already vetoed. `isCurrent()` is false because of the guard, not because
// of a superseding request, so `requestId` still equals
// `pageRequestRef.current` — this call must release busy itself and must
// swallow the rejection silently (no throw), matching page.tsx's
// poll-completion call sites where the guard can flip before the response
// lands with no follow-up `loadSourcesPage` call to take over.
test("a guard veto releases busy on a genuine network failure without surfacing it", async () => {
  const page = deferred<{ items: SourceSummary[]; total_count: number; offset: number; limit: number }>();
  api.listSources.mockReturnValueOnce(page.promise);
  render(<Harness />);
  act(() => {
    value!.commitNotebookSnapshot({
      actorId: "user-a",
      notebookId: "notebook-a",
      workspaceEpoch: 1,
      page: { items: [], total_count: 0, offset: 0, limit: 50 },
    });
  });

  let loading!: Promise<void>;
  act(() => {
    loading = value!.loadSourcesPage({ page: 0, q: "", guard: () => false });
  });
  expect(value!.sourcesPageLoading).toBe(true);

  page.reject(new TypeError("network error"));
  await act(async () => {
    await expect(loading).resolves.toBeUndefined();
  });

  expect(value!.sourcesPageLoading).toBe(false);
  expect(effects.reportError).not.toHaveBeenCalled();
});

// A source-page request superseded by any of this hook's abort sites —
// commitNotebookSnapshot, beginTransition, or the render-time actor-change
// branch (~168) — must both cancel the network call for real and release (or
// mask) busy: the site itself is responsible for the release (it is not the
// request's own window to release: a new owner is already active, or none
// is), and the request's own eventual AbortError-shaped rejection must stay
// invisible to callers. Shared setup/assertions so each site is exercised
// through the identical shape; only the abort action itself differs.
//
// `activateActor` bumps `pageRequestRef`/aborts too (mirroring every other
// site here), but it is deliberately NOT covered by this same-shape harness:
// its own guard (`actorIdRef.current !== null` → return) only lets it
// proceed past the guard when no actor is active yet — and `ownerRef`/a
// live `loadSourcesPage` request can only exist once `actorIdRef.current` is
// already non-null (commitNotebookSnapshot requires `actorIdRef.current ===
// input.actorId`, a non-empty string). So there is no reachable state where
// `activateActor` runs its body with a genuinely in-flight page request to
// cancel — its `abortInFlightSourcesPage()` call can only ever fire against
// an already-empty `pageAbortRef` (see the "authenticated bootstrap" test
// above, the only scenario that exercises this function's body at all: it
// always runs before any owner/request exists). A test asserting that shape
// would not go red if that call were deleted, so per the same allowance this
// review gave the render-time branch, no test is added for it here.
async function assertAbortSiteReleasesBusyMidRequest(
  performAbortSite: (view: ReturnType<typeof render>) => void,
) {
  const page = deferred<{ items: SourceSummary[]; total_count: number; offset: number; limit: number }>();
  api.listSources.mockReturnValueOnce(page.promise);
  const view = render(<Harness />);
  act(() => {
    value!.commitNotebookSnapshot({
      actorId: "user-a",
      notebookId: "notebook-a",
      workspaceEpoch: 1,
      page: { items: [], total_count: 0, offset: 0, limit: 50 },
    });
  });

  let loading!: Promise<void>;
  act(() => {
    loading = value!.loadSourcesPage({ page: 0, q: "" });
  });
  const signal = api.listSources.mock.calls[0]?.[4] as AbortSignal;
  expect(signal.aborted).toBe(false);
  expect(value!.sourcesPageLoading).toBe(true);

  act(() => {
    performAbortSite(view);
  });
  expect(signal.aborted).toBe(true);
  expect(value!.sourcesPageLoading).toBe(false);

  page.reject(new DOMException("aborted", "AbortError"));
  await act(async () => {
    await expect(loading).resolves.toBeUndefined();
  });
  expect(effects.reportError).not.toHaveBeenCalled();
  expect(value!.sourcesPageLoading).toBe(false);
}

test("commitNotebookSnapshot mid-request aborts it for real and releases busy", async () => {
  await assertAbortSiteReleasesBusyMidRequest(() => {
    value!.commitNotebookSnapshot({
      actorId: "user-a",
      notebookId: "notebook-b",
      workspaceEpoch: 2,
      page: { items: [], total_count: 0, offset: 0, limit: 50 },
    });
  });
});

test("beginTransition mid-request aborts it for real and releases busy", async () => {
  await assertAbortSiteReleasesBusyMidRequest(() => {
    value!.beginTransition();
  });
});

// The render-time actor-change branch (~168) fires on any actorId prop
// change while an owner is active — unlike `activateActor` above, it is
// reachable with a genuinely in-flight request: rerender with a different
// actorId while `loadSourcesPage` is outstanding. It does not call
// `setSourcesPageLoading(false)` directly (unlike beginTransition /
// commitNotebookSnapshot); busy instead goes false because it invalidates
// `ownerRef`, which makes the hook's returned `sourcesPageLoading` field
// masked to `false` (`ownerIsActive ? sourcesPageLoading : false`) — the
// "busy 被遮蔽" case the shared assertion above also covers correctly, since
// it only reads the hook's returned field.
test("an actor-change rerender mid-request aborts it for real and masks busy", async () => {
  await assertAbortSiteReleasesBusyMidRequest((view) => {
    view.rerender(<Harness actorId="user-b" />);
  });
});

// Unmount is not one of the owner/actor invalidation sites (beginTransition /
// activateActor / commitNotebookSnapshot / the render-time actor-change
// branch) that the "AbortError can only fire when isCurrent() is already
// false" invariant relies on — the unmount cleanup effect aborts
// `pageAbortRef` directly, without bumping `pageRequestRef` or clearing
// `ownerRef`. So after unmount, `isCurrent()` for the in-flight request is
// still (structurally) true when its AbortError-shaped rejection arrives.
// Without the explicit `error.name === "AbortError"` check, the old
// isCurrent()-gated `throw` would fire for real here, rejecting the
// `loadSourcesPage` promise and surfacing the cancellation as an error to
// whichever caller is awaiting or `.catch`-ing it — after the component that
// owned the request no longer exists.
test("unmounting aborts the in-flight source page request without surfacing an AbortError", async () => {
  const page = deferred<{ items: SourceSummary[]; total_count: number; offset: number; limit: number }>();
  api.listSources.mockReturnValueOnce(page.promise);
  const view = render(<Harness />);
  act(() => {
    value!.commitNotebookSnapshot({
      actorId: "user-a",
      notebookId: "notebook-a",
      workspaceEpoch: 1,
      page: { items: [], total_count: 0, offset: 0, limit: 50 },
    });
  });

  let loading!: Promise<void>;
  act(() => {
    loading = value!.loadSourcesPage({ page: 0, q: "" });
  });
  const signal = api.listSources.mock.calls[0]?.[4] as AbortSignal;
  expect(signal.aborted).toBe(false);

  act(() => {
    view.unmount();
  });
  expect(signal.aborted).toBe(true);

  page.reject(new DOMException("aborted", "AbortError"));
  await expect(loading).resolves.toBeUndefined();
  expect(effects.reportError).not.toHaveBeenCalled();
});

// Same unmount scenario as above — `isCurrent()` still (structurally) true
// when the rejection arrives — but with the rejection shaped the way undici
// actually raises it on an aborted body-stream-read: a plain `TypeError:
// terminated`, not a DOMException. Without also checking
// `controller.signal.aborted`, `error instanceof DOMException` is false here,
// so the code would fall through to the `isCurrent()`-gated `throw`, which is
// still true post-unmount — rejecting `loadSourcesPage` and surfacing the
// cancellation as a real error after the owning component is gone.
test("unmounting still swallows the abort when the rejection is a plain TypeError, not a DOMException", async () => {
  const page = deferred<{ items: SourceSummary[]; total_count: number; offset: number; limit: number }>();
  api.listSources.mockReturnValueOnce(page.promise);
  const view = render(<Harness />);
  act(() => {
    value!.commitNotebookSnapshot({
      actorId: "user-a",
      notebookId: "notebook-a",
      workspaceEpoch: 1,
      page: { items: [], total_count: 0, offset: 0, limit: 50 },
    });
  });

  let loading!: Promise<void>;
  act(() => {
    loading = value!.loadSourcesPage({ page: 0, q: "" });
  });
  const signal = api.listSources.mock.calls[0]?.[4] as AbortSignal;
  expect(signal.aborted).toBe(false);

  act(() => {
    view.unmount();
  });
  expect(signal.aborted).toBe(true);

  page.reject(new TypeError("terminated"));
  await expect(loading).resolves.toBeUndefined();
  expect(effects.reportError).not.toHaveBeenCalled();
});

// Same TypeError-shaped abort, but landing on the clamp-triggered *second*
// `listSources` call's catch (~413) instead of the first. Both calls inside
// one busy window share a single AbortController (see the clamp busy-window
// test above), so unmounting mid clamp-refetch aborts the same controller
// the second call is awaiting on.
test("unmounting mid clamp-triggered refetch still swallows a plain TypeError abort", async () => {
  const requested = deferred<{ items: SourceSummary[]; total_count: number; offset: number; limit: number }>();
  const clamped = deferred<{ items: SourceSummary[]; total_count: number; offset: number; limit: number }>();
  api.listSources.mockReturnValueOnce(requested.promise).mockReturnValueOnce(clamped.promise);
  const view = render(<Harness />);
  act(() => {
    value!.commitNotebookSnapshot({
      actorId: "user-a",
      notebookId: "notebook-a",
      workspaceEpoch: 1,
      page: { items: [], total_count: 300, offset: 0, limit: 50 },
    });
  });

  let loading!: Promise<void>;
  act(() => {
    loading = value!.loadSourcesPage({ page: 5, q: "" });
  });

  act(() => {
    requested.resolve({ items: [], total_count: 60, offset: 250, limit: 50 });
  });
  await act(async () => {
    await Promise.resolve();
  });
  expect(api.listSources).toHaveBeenCalledTimes(2);

  act(() => {
    view.unmount();
  });

  clamped.reject(new TypeError("terminated"));
  await expect(loading).resolves.toBeUndefined();
  expect(effects.reportError).not.toHaveBeenCalled();
});

// PR #557 regression: `sources`/`sourceElements` used to fall back to a bare
// `[]` literal whenever the owner is not active (no commitNotebookSnapshot
// has ever landed — e.g. actorId is null). A bare literal is a brand-new
// reference on every render, which makes a consuming effect's dependency
// array "change" every render (see use-ask-session.ts for the traced
// infinite-loop incident). The fix hoists frozen, stable module-level
// fallback constants; re-rendering with the owner still inactive must hand
// back the *same* reference every time.
test("owner-inactive view fields stay referentially stable across re-renders", () => {
  const view = render(<Harness actorId={null} />);
  const first = value!;
  expect(first.sources).toEqual([]);
  expect(first.sourceElements).toEqual([]);
  // A plain `useState` initial value is never frozen; only the hidden-state
  // fallback branch (the module-level `NO_*` constant) is. Asserting frozen
  // here pins down *which* branch actually produced this value, not merely
  // that it happens to equal an empty literal.
  expect(Object.isFrozen(first.sources)).toBe(true);
  expect(Object.isFrozen(first.sourceElements)).toBe(true);

  act(() => {
    view.rerender(<Harness actorId={null} />);
  });
  const second = value!;
  act(() => {
    view.rerender(<Harness actorId={null} />);
  });
  const third = value!;

  for (const later of [second, third]) {
    expect(later.sources).toBe(first.sources);
    expect(later.sourceElements).toBe(first.sourceElements);
  }
});

test("a public library's promotion sources are never polled and never take a document slot", async () => {
  // PR-E8: a promotion source is never parsed or analysed. Even with a
  // non-terminal stored status (data written before the terminal state), the
  // list must not start the 1.5 s source poll, and the document-limit count
  // is the backend's visible_document_count, not the list total.
  vi.useFakeTimers();
  render(<Harness />);
  const promoted = (id: string, parseStatus: string): SourceSummary => ({
    ...source(id, "notebook-a", parseStatus),
    type: "promotion",
    title: `收录自：${id}`,
  });
  const uploads = Array.from({ length: 15 }, (_, index) => source(`doc-${index}`, "notebook-a"));
  const promotions = [
    promoted("promo-terminal", "extracted"),
    ...Array.from({ length: 5 }, (_, index) => promoted(`promo-${index}`, "parsed")),
  ];
  act(() => {
    value!.commitNotebookSnapshot({
      actorId: "user-a", notebookId: "notebook-a", workspaceEpoch: 1,
      page: {
        items: [...uploads, ...promotions], total_count: 21, offset: 0, limit: 50,
        visible_document_count: 15,
      },
    });
  });
  await act(async () => {
    vi.advanceTimersByTime(10_000);
  });
  expect(api.getSource).not.toHaveBeenCalled();
  expect(effects.setStatusText).not.toHaveBeenCalledWith(expect.stringContaining("正在处理来源"));
  expect(value!.notebookSourceTotal).toBe(21);
  expect(value!.notebookDocumentCount).toBe(15);

  // deleting a promotion source does not free a document slot; deleting an
  // uploaded document does
  api.deleteSource.mockResolvedValue(undefined);
  api.listSources.mockResolvedValue({
    items: [...uploads, ...promotions], total_count: 21, offset: 0, limit: 50,
    visible_document_count: 15,
  });
  await act(async () => {
    await value!.deleteSource(promotions[1]);
  });
  expect(value!.notebookDocumentCount).toBe(15);
});
