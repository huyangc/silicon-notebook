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
  await act(async () => value!.searchSources());
  expect(value!.sources).toEqual([all[0]]);

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
