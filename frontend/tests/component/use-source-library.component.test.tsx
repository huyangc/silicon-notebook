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

import { useSourceLibrary } from "../../app/use-source-library";

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

function Harness({ actorId = "user-a" }: { actorId?: string | null }) {
  value = useSourceLibrary({ actorId, canWriteSources: true, effects });
  return <div data-testid="ids">{value.sources.map((item) => item.id).join(",")}</div>;
}

beforeEach(() => {
  value = null;
  vi.clearAllMocks();
});

test("source detail scrolling and rendering share one sanitized element id", () => {
  expect(sourceElementDomId("element/path:中文")).toBe("source-element-element_path___");
});

afterEach(() => {
  vi.useRealTimers();
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
  await act(async () => value!.deleteSource(source("no-owner", "notebook-a")));
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
  await act(async () => value!.deleteSource(source("foreign", "notebook-b")));
  expect(api.deleteSource).not.toHaveBeenCalled();
  await act(async () => value!.deleteSource(source("owned", "notebook-a")));
  expect(api.deleteSource).toHaveBeenCalledTimes(1);
  expect(api.deleteSource).toHaveBeenCalledWith("owned");
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
  let deleting!: Promise<void>;
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

test("terminal polling refresh survives the hasPending cleanup but remains owner-bound", async () => {
  vi.useFakeTimers();
  api.getSource.mockResolvedValue(source("pending", "notebook-a", "extracted"));
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
  expect(effects.refreshCheckup).toHaveBeenCalledWith("notebook-a", expect.any(Function));
  expect(value!.sources[0]?.parse_status).toBe("extracted");
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
  const first = value!.loadSourcesPage({ page: 0, q: "older" });
  const second = value!.loadSourcesPage({ page: 1, q: "newer" });
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
