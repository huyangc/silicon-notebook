import { act, cleanup, fireEvent, render, renderHook, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { useCollabPulse } from "../../app/use-collab-pulse";
import { CollabBar } from "../../app/collab-bar";
import { useCollabPanelRefresh } from "../../app/use-collab-panel-refresh";
import { CollabRefreshNotice } from "../../app/collab-refresh-notice";
import type { CollabKind, CollabPulseResult } from "../../app/collab-api";
import type { NotebookSummary } from "../../app/workspace-model";

const notebook = (id = "n1", active: boolean | null = true, cursor = "e1:4"): NotebookSummary => ({ id, name: id, purpose: "", primary_domain: "", status: "ready", counts: {}, created_label: "", collab_pulse: active, pulse_cursor: cursor });
const response = (overrides: Partial<CollabPulseResult> = {}): CollabPulseResult => ({ cursor: "e1:5", reset: false, changes: [], viewers: [], other_reader_count: 0, next_interval_s: 15, ...overrides });
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>((done) => { resolve = done; }); return { promise, resolve }; }
async function flush() { await act(async () => { await Promise.resolve(); await Promise.resolve(); }); }
async function advance(ms: number) { await act(async () => { await vi.advanceTimersByTimeAsync(ms); }); }
beforeEach(() => { vi.useFakeTimers(); Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" }); });
afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

function setup(snapshot = notebook(), reply: () => Promise<Response> = async () => json(response())) {
  const fetcher = vi.fn(reply);
  vi.stubGlobal("fetch", fetcher);
  const abort = new AbortController();
  const effects = { isMounted: vi.fn((_kind: CollabKind) => true), canRefresh: vi.fn((_kind: CollabKind) => true), refresh: vi.fn(async (_kind: CollabKind, _current: () => boolean): Promise<void> => undefined), accessLost: vi.fn() };
  const hook = renderHook(({ actorId, current }: { actorId: string; current: NotebookSummary }) => useCollabPulse({ actorId, notebookId: current.id, notebook: current, sessionSignal: abort.signal, effects }), { initialProps: { actorId: "a", current: snapshot } });
  const open = (current = snapshot, actorId = "a", workspaceEpoch = 1) => act(() => {
    hook.result.current.activateActor(actorId);
    const ticket = hook.result.current.beginNotebookTransition({ actorId, notebookId: current.id, workspaceEpoch });
    if (!ticket) throw new Error("fixture owner refused");
    hook.result.current.finishNotebookTransition(ticket, current);
  });
  open();
  return { ...hook, fetcher, abort, effects, open };
}

test("single-user and public-reader detail gates send zero pulse requests", async () => {
  const fixture = setup(notebook("n1", false));
  await advance(90_000);
  expect(fixture.fetcher).not.toHaveBeenCalled();
  fixture.rerender({ actorId: "a", current: notebook("n1", null) });
  await advance(90_000);
  expect(fixture.fetcher).not.toHaveBeenCalled();
});

test("detail cursor starts polling; null detail updates preserve the gate and never overwrite its running cursor", async () => {
  const fixture = setup(notebook(), async () => json(response({ next_interval_s: 30 })));
  await flush();
  expect(JSON.parse(String((fixture.fetcher.mock.calls[0] as unknown as [unknown, RequestInit])[1].body))).toEqual({ since: "e1:4", visible: true });
  fixture.rerender({ actorId: "a", current: notebook("n1", null, "wrong:999") });
  await advance(29_999);
  expect(fixture.fetcher).toHaveBeenCalledTimes(1);
  await advance(1);
  expect(fixture.fetcher).toHaveBeenCalledTimes(2);
  expect(JSON.parse(String((fixture.fetcher.mock.calls[1] as unknown as [unknown, RequestInit])[1].body)).since).toBe("e1:5");
});

test("hidden tabs pause; becoming visible sends one immediate single-flight pulse", async () => {
  Object.defineProperty(document, "visibilityState", { configurable: true, value: "hidden" });
  const held = deferred<Response>();
  const fixture = setup(notebook(), () => held.promise);
  await advance(60_000);
  expect(fixture.fetcher).not.toHaveBeenCalled();
  Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
  act(() => document.dispatchEvent(new Event("visibilitychange")));
  act(() => document.dispatchEvent(new Event("visibilitychange")));
  expect(fixture.fetcher).toHaveBeenCalledTimes(1);
  held.resolve(json(response()));
  await flush();
  Object.defineProperty(document, "visibilityState", { configurable: true, value: "hidden" });
  act(() => document.dispatchEvent(new Event("visibilitychange")));
  await advance(60_000);
  expect(fixture.fetcher).toHaveBeenCalledTimes(1);
});

test("kind refreshes debounce and serialize without losing a second simultaneous kind", async () => {
  const held = deferred<void>();
  const fixture = setup(notebook(), async () => json(response({ changes: [{ kind: "sources", actor_name: "乙", at: "now" }, { kind: "notebook", actor_name: "乙", at: "now" }] })));
  fixture.effects.refresh.mockImplementation(async (kind) => { if (kind === "sources") await held.promise; });
  await flush();
  await advance(1_999);
  expect(fixture.effects.refresh).not.toHaveBeenCalled();
  await advance(1);
  expect(fixture.effects.refresh.mock.calls.map(([kind]) => kind)).toEqual(["sources"]);
  held.resolve();
  await flush();
  expect(fixture.effects.refresh.mock.calls.map(([kind]) => kind)).toEqual(["sources", "notebook"]);
});

test("a reply finishing after the tab hides does not advance its cursor or refresh hidden panels", async () => {
  const held = deferred<Response>();
  const fixture = setup(notebook(), () => held.promise);
  Object.defineProperty(document, "visibilityState", { configurable: true, value: "hidden" });
  act(() => document.dispatchEvent(new Event("visibilitychange")));
  held.resolve(json(response({ changes: [{ kind: "sources", actor_name: "乙", at: "now" }] })));
  await flush(); await advance(10_000);
  expect(fixture.effects.refresh).not.toHaveBeenCalled();
  Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
  act(() => document.dispatchEvent(new Event("visibilitychange")));
  await flush();
  expect(JSON.parse(String((fixture.fetcher.mock.calls[1] as unknown as [unknown, RequestInit])[1].body)).since).toBe("e1:4");
});

test("hiding after cursor adoption defers debounced panel reads and drains the accepted change on foreground", async () => {
  const fixture = setup(notebook(), async () => json(response({ changes: [{ kind: "sources", actor_name: "乙", at: "now" }] })));
  await flush();
  Object.defineProperty(document, "visibilityState", { configurable: true, value: "hidden" });
  act(() => document.dispatchEvent(new Event("visibilitychange")));
  await advance(5_000);
  expect(fixture.effects.refresh).not.toHaveBeenCalled();
  Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
  act(() => document.dispatchEvent(new Event("visibilitychange")));
  await flush();
  expect(fixture.effects.refresh.mock.calls.map(([kind]) => kind)).toEqual(["sources"]);
  expect(JSON.parse(String((fixture.fetcher.mock.calls[1] as unknown as [unknown, RequestInit])[1].body)).since).toBe("e1:5");
});

test("editing panels defer to an inline refresh; explicit retry is single-flight and clears its receipt", async () => {
  const fixture = setup(notebook(), async () => json(response({ changes: [{ kind: "sources", actor_name: "乙", at: "now" }] })));
  fixture.effects.canRefresh.mockReturnValue(false);
  await flush(); await advance(2_000);
  expect(fixture.effects.refresh).not.toHaveBeenCalled();
  expect(fixture.result.current.view.pending).toEqual(["sources"]);
  fixture.effects.canRefresh.mockReturnValue(true);
  const held = deferred<void>();
  fixture.effects.refresh.mockImplementation(() => held.promise);
  act(() => { void fixture.result.current.refreshPending(); void fixture.result.current.refreshPending(); });
  expect(fixture.result.current.view.busy).toBe(true);
  expect(fixture.effects.refresh).toHaveBeenCalledTimes(1);
  held.resolve(); await flush();
  expect(fixture.result.current.view.pending).toEqual([]);
  expect(fixture.result.current.view.notice).toBe("已刷新");
  await advance(3_000);
  expect(fixture.result.current.view.notice).toBe("");
});

test("unmounted kinds cause neither panel I/O nor a reminder that can never be refreshed", async () => {
  const fixture = setup(notebook(), async () => json(response({ changes: [{ kind: "knowledge", actor_name: "乙", at: "now" }] })));
  fixture.effects.isMounted.mockReturnValue(false);
  await flush(); await advance(2_000);
  expect(fixture.effects.refresh).not.toHaveBeenCalled();
  expect(fixture.result.current.view.pending).toEqual([]);
});

test("first reset refreshes mounted kinds once, consecutive resets wait for the user", async () => {
  let rev = 5;
  const fixture = setup(notebook(), async () => json(response({ cursor: `e1:${rev++}`, reset: true })));
  await flush(); await advance(2_000);
  expect(fixture.effects.refresh).toHaveBeenCalledTimes(8);
  await advance(15_000);
  expect(fixture.effects.refresh).toHaveBeenCalledTimes(8);
  expect(fixture.result.current.view.pending).toHaveLength(8);
});

test("a consecutive reset cancels the first reset's debounce before it can read", async () => {
  let revision = 5;
  let pulses = 0;
  const fixture = setup(notebook(), async () => json(response({ cursor: `e1:${revision++}`, reset: ++pulses <= 2, next_interval_s: 1 })));
  await flush(); await advance(1_000);
  expect(fixture.fetcher).toHaveBeenCalledTimes(2);
  expect(fixture.result.current.view.pending).toHaveLength(8);
  await advance(1_000);
  expect(fixture.effects.refresh).not.toHaveBeenCalled();
  expect(fixture.result.current.view.pending).toHaveLength(8);
  await act(async () => { await fixture.result.current.refreshPending(); });
  expect(fixture.effects.refresh).toHaveBeenCalledTimes(8);
  expect(fixture.result.current.view.pending).toEqual([]);
  expect(fixture.result.current.view.notice).toBe("已刷新");
});

test.each(["debounce", "foreground drain"] as const)("a consecutive reset cancels queued %s reads without cancelling the in-flight read", async (origin) => {
  let revision = 5;
  const held = deferred<void>();
  const fixture = setup(notebook(), async () => json(response({ cursor: `e1:${revision++}`, reset: true })));
  fixture.effects.isMounted.mockImplementation((kind) => kind === "sources" || kind === "notebook");
  fixture.effects.refresh.mockImplementationOnce(() => held.promise);
  await flush();
  if (origin === "debounce") {
    await advance(2_000);
    expect(fixture.effects.refresh.mock.calls.map(([kind]) => kind)).toEqual(["sources"]);
    await advance(13_000);
  } else {
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "hidden" });
    act(() => document.dispatchEvent(new Event("visibilitychange")));
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
    act(() => document.dispatchEvent(new Event("visibilitychange")));
    await flush();
  }
  expect(fixture.fetcher).toHaveBeenCalledTimes(2);
  expect(fixture.result.current.view.pending).toEqual(["sources", "notebook"]);
  held.resolve(); await flush();
  expect(fixture.effects.refresh.mock.calls.map(([kind]) => kind)).toEqual(["sources"]);
  expect(fixture.result.current.view.pending).toEqual(["sources", "notebook"]);
  expect(fixture.result.current.view.busy).toBe(false);
  await act(async () => { await fixture.result.current.refreshPending(); });
  expect(fixture.effects.refresh.mock.calls.map(([kind]) => kind)).toEqual(["sources", "sources", "notebook"]);
  expect(fixture.result.current.view.pending).toEqual([]);
});

test("a consecutive reset drops an unmounted panel's old timer without creating a reminder", async () => {
  let revision = 5;
  const fixture = setup(notebook(), async () => json(response({ cursor: `e1:${revision++}`, reset: true, next_interval_s: 1 })));
  fixture.effects.isMounted.mockImplementation((kind) => kind === "sources");
  await flush();
  fixture.effects.isMounted.mockReturnValue(false);
  await advance(1_000);
  expect(fixture.result.current.view.pending).toEqual([]);
  // Reopening performs its ordinary authoritative read; an old first-reset
  // timer must not survive the consecutive reset and add another read.
  fixture.effects.isMounted.mockImplementation((kind) => kind === "sources");
  await advance(1_000);
  expect(fixture.effects.refresh).not.toHaveBeenCalled();
  expect(fixture.result.current.view.pending).toEqual(["sources"]);
});

test.each(["automatic", "manual", "failed"] as const)("a newer reset survives an older %s refresh and can be explicitly refreshed", async (mode) => {
  let revision = 5;
  const held = deferred<void>();
  const fixture = setup(notebook(), async () => json(response({ cursor: `e1:${revision++}`, reset: true })));
  fixture.effects.isMounted.mockImplementation((kind) => kind === "sources");
  fixture.effects.canRefresh.mockReturnValue(mode !== "manual");
  fixture.effects.refresh.mockImplementation(async () => {
    await held.promise;
    if (mode === "failed") throw new Error("unavailable");
  });
  await flush(); await advance(2_000);
  if (mode === "manual") {
    expect(fixture.result.current.view.pending).toEqual(["sources"]);
    fixture.effects.canRefresh.mockReturnValue(true);
    act(() => { void fixture.result.current.refreshPending(); });
  }
  expect(fixture.effects.refresh).toHaveBeenCalledTimes(1);
  await advance(13_000);
  expect(fixture.fetcher).toHaveBeenCalledTimes(2);
  expect(fixture.result.current.view.pending).toEqual(["sources"]);
  expect(fixture.effects.refresh).toHaveBeenCalledTimes(1);

  held.resolve(); await flush();
  expect(fixture.result.current.view.busy).toBe(false);
  expect(fixture.result.current.view.pending).toEqual(["sources"]);
  if (mode === "failed") expect(fixture.result.current.view.notice).toBe("刷新没成功，请重试");
  fixture.effects.refresh.mockResolvedValue(undefined);
  await act(async () => { await fixture.result.current.refreshPending(); });
  expect(fixture.effects.refresh).toHaveBeenCalledTimes(2);
  expect(fixture.result.current.view.pending).toEqual([]);
  expect(fixture.result.current.view.notice).toBe("已刷新");
});

test("a new ordinary change queues a fresh read after an older held refresh", async () => {
  let revision = 5;
  const held = deferred<void>();
  const fixture = setup(notebook(), async () => json(response({ cursor: `e1:${revision++}`, changes: [{ kind: "sources", actor_name: "乙", at: "now" }] })));
  fixture.effects.canRefresh.mockReturnValue(false);
  await flush(); await advance(2_000);
  fixture.effects.canRefresh.mockReturnValue(true);
  fixture.effects.refresh.mockImplementationOnce(() => held.promise);
  act(() => { void fixture.result.current.refreshPending(); });
  await advance(15_000);
  expect(fixture.effects.refresh).toHaveBeenCalledTimes(1);
  held.resolve(); await flush();
  expect(fixture.effects.refresh).toHaveBeenCalledTimes(2);
  expect(fixture.result.current.view.pending).toEqual([]);
});

test("an old workspace refresh cannot clear or release the new workspace's refresh", async () => {
  const old = deferred<void>();
  const next = deferred<void>();
  const fixture = setup(notebook(), async () => json(response({ reset: true })));
  fixture.effects.isMounted.mockImplementation((kind) => kind === "sources");
  fixture.effects.refresh.mockImplementationOnce(() => old.promise).mockImplementationOnce(() => next.promise);
  await flush(); await advance(2_000);
  fixture.rerender({ actorId: "a", current: notebook("n2") });
  fixture.open(notebook("n2"), "a", 2);
  await flush(); await advance(2_000);
  expect(fixture.effects.refresh).toHaveBeenCalledTimes(2);
  old.resolve(); await flush();
  expect(fixture.result.current.view.busy).toBe(true);
  next.resolve(); await flush();
  expect(fixture.result.current.view.busy).toBe(false);
  expect(fixture.result.current.view.pending).toEqual([]);
});

test("epoch A-B-A disables polling with a content-free event; 404 instead reconciles authority once", async () => {
  const log = vi.spyOn(console, "info").mockImplementation(() => undefined);
  const replies = ["e2:1", "e1:6"];
  const fixture = setup(notebook(), async () => json(response({ cursor: replies.shift()!, reset: true })));
  await flush(); await advance(15_000);
  expect(fixture.result.current.view.unavailable).toBe(true);
  expect(log).toHaveBeenCalledWith("[collab-pulse] disabled: epoch_returned");
  await advance(60_000);
  expect(fixture.fetcher).toHaveBeenCalledTimes(2);
  fixture.unmount();
  const denied = setup(notebook(), async () => json({ detail: "denied" }, 404));
  await flush(); await advance(60_000);
  expect(denied.effects.accessLost).toHaveBeenCalledTimes(1);
  expect(denied.fetcher).toHaveBeenCalledTimes(1);
});

test("A-B-A navigation and actor/session changes reject old replies and abort the old transport", async () => {
  const old = deferred<Response>();
  const fixture = setup(notebook(), () => old.promise);
  const signal = (fixture.fetcher.mock.calls[0] as unknown as [unknown, RequestInit])[1].signal;
  fixture.rerender({ actorId: "a", current: notebook("n2") });
  fixture.open(notebook("n2"), "a", 2);
  fixture.rerender({ actorId: "a", current: notebook("n1") });
  fixture.open(notebook("n1"), "a", 3);
  expect(signal?.aborted).toBe(true);
  fixture.abort.abort();
  old.resolve(json(response({ changes: [{ kind: "sources", actor_name: "old actor", at: "now" }] })));
  await flush(); await advance(60_000);
  expect(fixture.effects.refresh).not.toHaveBeenCalled();
  expect(fixture.result.current.view.notice).not.toContain("old actor");
});

test("presence renders writer names and a reader count, with no read-only identity or activity request", async () => {
  const fetcher = vi.fn(); vi.stubGlobal("fetch", fetcher);
  render(<CollabBar notebookId="n1" canReadActivity={false} sessionSignal={new AbortController().signal} onRefresh={async () => undefined}
    view={{ enabled: true, unavailable: false, viewers: [{ user_id: "writer", display_name: "乙", role: "editor" }, { user_id: "secret-reader-id", display_name: "secret-reader-name", role: "read" }], otherReaderCount: 3, pending: [], busy: false, notice: "" }} />);
  expect(screen.getByText(/乙 · 另有 3 位读者/)).toBeVisible();
  expect(screen.queryByText(/secret-reader/)).toBeNull();
  expect(screen.queryByRole("button", { name: "最近改动" })).toBeNull();
  expect(fetcher).not.toHaveBeenCalled();
});

test("recent activity reads only when opened and keeps a refresh single-flight with a local timed receipt", async () => {
  const held = deferred<Response>();
  const fetcher = vi.fn(async () => held.promise); vi.stubGlobal("fetch", fetcher);
  render(<CollabBar notebookId="n1" canReadActivity sessionSignal={new AbortController().signal} onRefresh={async () => undefined}
    view={{ enabled: true, unavailable: false, viewers: [], otherReaderCount: 0, pending: [], busy: false, notice: "" }} />);
  expect(fetcher).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "最近改动" }));
  expect(fetcher).toHaveBeenCalledTimes(1);
  expect(screen.getByRole("button", { name: "正在读取…" })).toBeDisabled();
  held.resolve(json({ items: [{ id: "x", kind: "sources", actor_id: "e", actor_name: "乙", capability: "sources:write", target: "POST /notebooks/{notebook_id}/sources", created_at: "2026-11-01T00:00:00Z" }] }));
  await flush();
  expect(screen.getByText("乙更新了来源")).toBeVisible();
  expect(screen.queryByText(/sources:write|POST \/notebooks/)).toBeNull();
  const second = deferred<Response>(); fetcher.mockImplementation(() => second.promise);
  fireEvent.click(screen.getByRole("button", { name: "刷新改动" }));
  expect(screen.getByRole("button", { name: "正在读取…" })).toBeDisabled();
  second.resolve(json({ items: [] })); await flush();
  expect(screen.getByText("已刷新")).toBeVisible();
  expect(screen.getByText("暂无最近改动")).toBeVisible();
  await advance(2_000);
  fetcher.mockImplementation(async () => json({ items: [] }));
  fireEvent.click(screen.getByRole("button", { name: "刷新改动" })); await flush();
  await advance(1_001);
  expect(screen.getByText("已刷新")).toBeVisible();
  await advance(3_000);
  expect(screen.queryByText("已刷新")).toBeNull();
});

test("a mounted panel keeps drafts on an external revision and refreshes only after editing ends", async () => {
  const read = vi.fn(async () => true);
  function Panel({ revision, safe }: { revision: number; safe: boolean }) {
    const refresh = useCollabPanelRefresh(revision, safe, read);
    return <><input aria-label="草稿" defaultValue="unfinished" /><CollabRefreshNotice refresh={refresh} /></>;
  }
  const { rerender } = render(<Panel revision={0} safe={false} />);
  rerender(<Panel revision={1} safe={false} />);
  expect(read).not.toHaveBeenCalled();
  expect(screen.getByLabelText("草稿")).toHaveValue("unfinished");
  rerender(<Panel revision={1} safe />);
  expect(read).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "刷新" }));
  await flush();
  expect(read).toHaveBeenCalledTimes(1);
  expect(screen.getByText("已刷新")).toBeVisible();
  expect(screen.getByLabelText("草稿")).toHaveValue("unfinished");
});

test("an unavailable activity read remains retryable and never turns into a false empty history", async () => {
  const fetcher = vi.fn(async () => json({ detail: "unavailable" }, 503)); vi.stubGlobal("fetch", fetcher);
  render(<CollabBar notebookId="n1" canReadActivity sessionSignal={new AbortController().signal} onRefresh={async () => undefined}
    view={{ enabled: true, unavailable: false, viewers: [], otherReaderCount: 0, pending: [], busy: false, notice: "" }} />);
  fireEvent.click(screen.getByRole("button", { name: "最近改动" })); await flush();
  await advance(3_000);
  expect(screen.getByText("最近改动暂不可用")).toBeVisible();
  expect(screen.queryByText("暂无最近改动")).toBeNull();
  fetcher.mockImplementation(async () => json({ items: [] }));
  fireEvent.click(screen.getByRole("button", { name: "刷新改动" })); await flush();
  expect(screen.getByText("暂无最近改动")).toBeVisible();
  expect(screen.queryByText("最近改动暂不可用")).toBeNull();
});

test("a newer safe panel revision arriving in flight drains after the current read", async () => {
  const first = deferred<boolean>();
  const read = vi.fn(async () => true).mockReturnValueOnce(first.promise);
  const hook = renderHook(({ revision, safe }) => useCollabPanelRefresh(revision, safe, read), { initialProps: { revision: 0, safe: true } });
  hook.rerender({ revision: 1, safe: true });
  hook.rerender({ revision: 2, safe: true });
  expect(read).toHaveBeenCalledTimes(1);
  first.resolve(true); await flush();
  expect(read).toHaveBeenCalledTimes(2);
  expect(hook.result.current.pending).toBe(false);
  expect(hook.result.current.busy).toBe(false);
});

test.each(["failed", "editing", "hidden"] as const)("panel drain retains newer work instead of retrying through %s", async (stop) => {
  const first = deferred<boolean>();
  const read = vi.fn(async () => true).mockReturnValueOnce(first.promise);
  const hook = renderHook(({ revision, safe }) => useCollabPanelRefresh(revision, safe, read), { initialProps: { revision: 0, safe: true } });
  hook.rerender({ revision: 1, safe: true });
  hook.rerender({ revision: 2, safe: stop !== "editing" });
  if (stop === "hidden") {
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "hidden" });
    act(() => document.dispatchEvent(new Event("visibilitychange")));
  }
  first.resolve(stop !== "failed"); await flush(); await advance(60_000);
  expect(read).toHaveBeenCalledTimes(1);
  expect(hook.result.current.pending).toBe(true);
  if (stop === "editing") hook.rerender({ revision: 2, safe: true });
  Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
  act(() => document.dispatchEvent(new Event("visibilitychange")));
  await flush();
  if (stop === "hidden") expect(read).toHaveBeenCalledTimes(2);
  else {
    expect(read).toHaveBeenCalledTimes(1);
    act(() => { void hook.result.current.refresh(); }); await flush();
    expect(read).toHaveBeenCalledTimes(2);
  }
  expect(hook.result.current.pending).toBe(false);
});
