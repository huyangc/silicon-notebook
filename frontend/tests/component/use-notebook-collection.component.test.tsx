import { act, cleanup, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import type { NotebookSummary } from "../../app/workspace-model";

const notebookApi = vi.hoisted(() => ({
  createNotebook: vi.fn(),
  deleteNotebook: vi.fn(),
  getNotebook: vi.fn(),
  listNotebooks: vi.fn(),
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
  notebookApi.deleteNotebook.mockResolvedValue(undefined);
  basesApi.listMountable.mockResolvedValue([]);
  basesApi.listBases.mockResolvedValue([]);
  basesApi.mountedByCount.mockResolvedValue({ count: 0 });
  basesApi.setBases.mockResolvedValue([]);
  searchApi.searchNotebooksBounded.mockResolvedValue({});
});

afterEach(() => {
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

test("group administrators retain PATCH-only inline rename without gaining owner actions", async () => {
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
  expect(basesApi.listMountable).not.toHaveBeenCalled();
  await act(async () => value!.openEditor("shared"));
  expect(value!.editor).toBeNull();
  expect(basesApi.listMountable).not.toHaveBeenCalled();
  await act(async () => value!.openDelete("shared"));
  expect(value!.deletion).toBeNull();
  expect(basesApi.mountedByCount).not.toHaveBeenCalled();
});

test("rename cannot return a stale writable detail after the composite refresh revokes manage access", async () => {
  const renamed = notebook("shared", "reader", true);
  renamed.name = "renamed";
  notebookApi.updateNotebook.mockResolvedValueOnce(renamed);
  render(<Harness />);
  publish([notebook("shared", "reader", true)]);
  vi.mocked(effects.refreshComposite).mockImplementationOnce(async () => {
    publish([notebook("shared", "reader", false)]);
  });

  let result: NotebookSummary | null = renamed;
  await act(async () => {
    result = await value!.renameNotebook("shared", "renamed");
  });

  expect(notebookApi.updateNotebook).toHaveBeenCalledTimes(1);
  expect(result).toBeNull();
  expect(effects.notify).not.toHaveBeenCalled();
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

test("editor open is latest-wins and a permission downgrade blocks the second write", async () => {
  const mountableA = deferred<never[]>();
  const basesA = deferred<never[]>();
  basesApi.listMountable
    .mockReturnValueOnce(mountableA.promise)
    .mockResolvedValueOnce([]);
  basesApi.listBases
    .mockReturnValueOnce(basesA.promise)
    .mockResolvedValueOnce([]);
  render(<Harness />);
  publish([notebook("a"), notebook("b")]);

  let openingA!: Promise<boolean>;
  act(() => { openingA = value!.openEditor("a"); });
  await act(async () => value!.openEditor("b"));
  expect(value!.editor?.target.id).toBe("b");
  await act(async () => {
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
  await act(async () => update.resolve(notebook("b")));
  await saving;
  expect(basesApi.setBases).not.toHaveBeenCalled();
});

test("a downgrade while final editor detail is pending closes owner UI and blocks stale detail commit", async () => {
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
  publish([notebook("a", "reader")]);
  expect(value!.editor).toBeNull();
  publish([notebook("a")]);
  expect(value!.editor).toBeNull();

  await act(async () => detail.resolve(notebook("a")));
  await saving;
  expect(effects.onNotebookUpdated).not.toHaveBeenCalled();
  expect(effects.notify).not.toHaveBeenCalled();
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
