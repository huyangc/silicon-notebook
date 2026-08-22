import { StrictMode, forwardRef, useImperativeHandle } from "react";
import { act, cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";

import { useWorkspaceExtensions, type WorkspaceExtensions } from "../../app/use-workspace-extensions";
import { WorkspaceExtensionOutlet } from "../../features/extension-sdk/host";
import { WORKSPACE_UI_CONTRIBUTIONS, defineWorkspaceUiRegistry } from "../../features/extension-sdk/registry";
import type { SystemExtensionProjection } from "../../features/extension-sdk/contracts";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

function Sample() { return <div data-testid="extension">sample</div>; }
const syntheticRegistry = defineWorkspaceUiRegistry([{
  id: "sample-panel", pluginId: "sample-plugin", pluginVersion: "1.0.0",
  capability: "sample.ui.available", slot: "workspace.side_panel",
  permission: "notebook:read", mode: "all", Component: Sample,
}]);
const realProjection: SystemExtensionProjection = { apiVersion: "1", extensions: [{
  pluginId: "builtin.ask_agent_profile", displayName: "Ask agent profile", version: "1.0.0",
  contributionId: "builtin.ask_agent_profile.workspace_panel", available: true, unavailableReason: null,
}] };
const syntheticProjection: SystemExtensionProjection = { apiVersion: "1", extensions: [{
  pluginId: "sample-plugin", displayName: "Sample", version: "1.0.0",
  contributionId: "sample-panel", available: true, unavailableReason: null,
}] };
const permissions = {
  notebookRead: true, notebookWrite: false, notebookConfigure: false,
  sourceRead: false, sourceWrite: false, systemAdmin: false,
};

type HarnessProps = {
  actorId: string | null;
  notebookId: string | null;
  entries?: typeof WORKSPACE_UI_CONTRIBUTIONS;
  load: () => Promise<SystemExtensionProjection>;
  onOpen?: () => void;
  notebookRead?: boolean;
};
const Harness = forwardRef<WorkspaceExtensions, HarnessProps>(function Harness({
  actorId, notebookId, entries = WORKSPACE_UI_CONTRIBUTIONS, load,
  onOpen = () => undefined, notebookRead = true,
}, ref) {
  const live = useWorkspaceExtensions(actorId, notebookId, entries, load);
  useImperativeHandle(ref, () => live, [live]);
  if (!actorId || !notebookId || !live.ownerKey) return null;
  const context = {
    slot: "workspace.side_panel" as const,
    actor: { id: actorId, username: actorId, displayName: actorId },
    notebook: { id: notebookId, name: notebookId }, source: null,
    uiMode: "auto" as const, permissions: { ...permissions, notebookRead },
  };
  return <>
    <WorkspaceExtensionOutlet slot="workspace.side_panel" registry={entries}
      projection={live.projection} ownerKey={live.ownerKey}
      actions={{ openUnderstanding: onOpen }} context={context} />
    <WorkspaceExtensionOutlet slot="source.detail_section" registry={entries}
      projection={live.projection} ownerKey={live.ownerKey}
      actions={{ openUnderstanding: onOpen }} context={{ ...context, slot: "source.detail_section" }} />
  </>;
});

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

function commit(ref: { current: WorkspaceExtensions | null }, actorId: string, notebookId: string, workspaceEpoch: number) {
  const transition = ref.current!.beginNotebookTransition({ actorId, notebookId, workspaceEpoch });
  ref.current!.finishNotebookTransition(transition!, true);
}

test("empty registry and collection path perform no request, controller or DOM work", () => {
  const load = vi.fn<() => Promise<SystemExtensionProjection>>();
  const abortController = vi.fn();
  vi.stubGlobal("AbortController", abortController);
  const ref = { current: null as WorkspaceExtensions | null };
  const { container } = render(<StrictMode><Harness ref={ref} actorId="user-a" notebookId={null}
    entries={defineWorkspaceUiRegistry([])} load={load} /></StrictMode>);
  expect(load).not.toHaveBeenCalled();
  expect(abortController).not.toHaveBeenCalled();
  expect(container).toBeEmptyDOMElement();
});

test("real production contribution is lazy, mode-all and reader-visible, with one actor request", async () => {
  const user = userEvent.setup();
  const load = vi.fn(async () => realProjection);
  const onOpen = vi.fn();
  const ref = { current: null as WorkspaceExtensions | null };
  const view = render(<StrictMode><Harness ref={ref} actorId="user-a" notebookId="notebook-a" load={load} onOpen={onOpen} /></StrictMode>);
  expect(load).not.toHaveBeenCalled();
  act(() => commit(ref, "user-a", "notebook-a", 1));
  expect(await screen.findByRole("button", { name: "打开理解面板" })).toBeInTheDocument();
  expect(load).toHaveBeenCalledTimes(1);
  expect(onOpen).not.toHaveBeenCalled();
  expect(screen.getAllByLabelText("AI 对这个库的理解")).toHaveLength(1);
  expect(screen.getByLabelText("AI 对这个库的理解").closest("aside")).toBe(
    view.container.querySelector(".workspace-extension-outlet-workspace-side_panel"),
  );
  expect(view.container.querySelectorAll(".workspace-extension-outlet-workspace-side_panel")).toHaveLength(1);
  await user.click(screen.getByRole("button", { name: "打开理解面板" }));
  expect(onOpen).toHaveBeenCalledOnce();
});

test("permission denial unmounts the real plugin without refetching", async () => {
  const load = vi.fn(async () => realProjection);
  const ref = { current: null as WorkspaceExtensions | null };
  const view = render(<Harness ref={ref} actorId="user-a" notebookId="notebook-a" load={load} />);
  act(() => commit(ref, "user-a", "notebook-a", 1));
  await screen.findByRole("button", { name: "打开理解面板" });
  view.rerender(<Harness ref={ref} actorId="user-a" notebookId="notebook-a" load={load} notebookRead={false} />);
  expect(screen.queryByRole("button", { name: "打开理解面板" })).toBeNull();
  expect(view.container.querySelector(".workspace-extension-outlet-workspace-side_panel")).toBeNull();
  expect(load).toHaveBeenCalledTimes(1);
});

test.each([
  ["missing", { apiVersion: "1", extensions: [] } satisfies SystemExtensionProjection],
  ["disabled", {
    apiVersion: "1", extensions: [{
      ...realProjection.extensions[0], available: false, unavailableReason: "disabled" as const,
    }],
  } satisfies SystemExtensionProjection],
  ["unavailable", {
    apiVersion: "1", extensions: [{
      ...realProjection.extensions[0], available: false, unavailableReason: "unavailable" as const,
    }],
  } satisfies SystemExtensionProjection],
])("a %s real server contribution leaves no side-panel wrapper", async (_case, projection) => {
  const load = vi.fn(async () => projection);
  const ref = { current: null as WorkspaceExtensions | null };
  const view = render(<Harness ref={ref} actorId="user-a" notebookId="notebook-a" load={load} />);
  act(() => commit(ref, "user-a", "notebook-a", 1));
  await act(async () => { await Promise.resolve(); });
  expect(screen.queryByRole("button", { name: "打开理解面板" })).toBeNull();
  expect(view.container.querySelector(".workspace-extension-outlet-workspace-side_panel")).toBeNull();
  expect(view.container).toBeEmptyDOMElement();
  expect(load).toHaveBeenCalledTimes(1);
});

test("same-actor A-B-A transitions hide synchronously, reuse projection and reject old owner", async () => {
  const load = vi.fn(async () => realProjection);
  const ref = { current: null as WorkspaceExtensions | null };
  const view = render(<Harness ref={ref} actorId="user-a" notebookId="notebook-a" load={load} />);
  act(() => commit(ref, "user-a", "notebook-a", 1));
  await screen.findByRole("button", { name: "打开理解面板" });
  const oldOwner = ref.current!.owner!;
  let next: ReturnType<WorkspaceExtensions["beginNotebookTransition"]>;
  act(() => { next = ref.current!.beginNotebookTransition({ actorId: "user-a", notebookId: "notebook-b", workspaceEpoch: 2 }); });
  expect(screen.queryByRole("button", { name: "打开理解面板" })).toBeNull();
  expect(ref.current!.owns(oldOwner)).toBe(false);
  view.rerender(<Harness ref={ref} actorId="user-a" notebookId="notebook-b" load={load} />);
  act(() => ref.current!.finishNotebookTransition(next!, true));
  expect(await screen.findByRole("button", { name: "打开理解面板" })).toBeInTheDocument();
  let returned!: ReturnType<WorkspaceExtensions["beginNotebookTransition"]>;
  act(() => { returned = ref.current!.beginNotebookTransition({ actorId: "user-a", notebookId: "notebook-a", workspaceEpoch: 3 }); });
  view.rerender(<Harness ref={ref} actorId="user-a" notebookId="notebook-a" load={load} />);
  act(() => ref.current!.finishNotebookTransition(returned!, true));
  expect(await screen.findByRole("button", { name: "打开理解面板" })).toBeInTheDocument();
  expect(ref.current!.owns(oldOwner)).toBe(false);
  expect(load).toHaveBeenCalledTimes(1);
});

test("failed transition remains suspended", async () => {
  const load = vi.fn(async () => realProjection);
  const ref = { current: null as WorkspaceExtensions | null };
  render(<Harness ref={ref} actorId="user-a" notebookId="notebook-a" load={load} />);
  act(() => commit(ref, "user-a", "notebook-a", 1));
  await screen.findByRole("button", { name: "打开理解面板" });
  act(() => {
    const transition = ref.current!.beginNotebookTransition({ actorId: "user-a", notebookId: "notebook-b", workspaceEpoch: 2 });
    ref.current!.finishNotebookTransition(transition!, false);
  });
  expect(screen.queryByRole("button", { name: "打开理解面板" })).toBeNull();
});

test("actor A-B-A rejects the first actor response and issues one request per generation", async () => {
  const a1 = deferred<SystemExtensionProjection>();
  const b = deferred<SystemExtensionProjection>();
  const a2 = deferred<SystemExtensionProjection>();
  const load = vi.fn().mockReturnValueOnce(a1.promise).mockReturnValueOnce(b.promise).mockReturnValueOnce(a2.promise);
  const ref = { current: null as WorkspaceExtensions | null };
  const view = render(<Harness ref={ref} actorId="user-a" notebookId="notebook-a" entries={syntheticRegistry} load={load} />);
  act(() => commit(ref, "user-a", "notebook-a", 1));
  view.rerender(<Harness ref={ref} actorId="user-b" notebookId="notebook-b" entries={syntheticRegistry} load={load} />);
  act(() => commit(ref, "user-b", "notebook-b", 2));
  view.rerender(<Harness ref={ref} actorId="user-a" notebookId="notebook-a" entries={syntheticRegistry} load={load} />);
  act(() => commit(ref, "user-a", "notebook-a", 3));
  await act(async () => { a1.resolve(syntheticProjection); await Promise.resolve(); });
  expect(screen.queryByTestId("extension")).toBeNull();
  await act(async () => { a2.resolve(syntheticProjection); await Promise.resolve(); });
  expect(screen.getByTestId("extension")).toBeInTheDocument();
  expect(load).toHaveBeenCalledTimes(3);
});
