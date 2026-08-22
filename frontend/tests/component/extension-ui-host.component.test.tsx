import { StrictMode } from "react";
import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";

import { useWorkspaceExtensions } from "../../app/use-workspace-extensions";
import { WorkspaceExtensionOutlet } from "../../features/extension-sdk/host";
import { defineWorkspaceUiRegistry } from "../../features/extension-sdk/registry";
import type { SystemExtensionProjection } from "../../features/extension-sdk/contracts";


function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

function Sample({ context }: { context: { actor: { id: string }; source: { id: string } | null } }) {
  return <div data-testid="extension">{context.actor.id}:{context.source?.id ?? "workspace"}</div>;
}

const registry = defineWorkspaceUiRegistry([{
  id: "sample-panel",
  pluginId: "sample-plugin",
  pluginVersion: "1.0.0",
  capability: "sample.ui.available",
  slot: "workspace.side_panel",
  permission: "notebook:read",
  mode: "all",
  Component: Sample,
}]);

const projection: SystemExtensionProjection = {
  apiVersion: "1",
  extensions: [{
    pluginId: "sample-plugin",
    displayName: "Sample",
    version: "1.0.0",
    contributionId: "sample-panel",
    available: true,
    unavailableReason: null,
  }],
};

const permissions = {
  notebookRead: true,
  notebookWrite: true,
  notebookConfigure: true,
  sourceRead: false,
  sourceWrite: false,
  systemAdmin: false,
};

function Harness({
  actorId,
  entries = registry,
  load,
}: {
  actorId: string | null;
  entries?: typeof registry;
  load: () => Promise<SystemExtensionProjection>;
}) {
  const live = useWorkspaceExtensions(actorId, entries, load);
  if (!actorId) return null;
  return <WorkspaceExtensionOutlet
    slot="workspace.side_panel"
    registry={entries}
    projection={live}
    context={{
      slot: "workspace.side_panel",
      actor: { id: actorId, username: actorId, displayName: actorId },
      notebook: { id: "notebook-a", name: "Notebook A" },
      source: null,
      uiMode: "advanced",
      permissions,
    }}
  />;
}

afterEach(() => cleanup());

test("empty build registry returns exact null and performs no capability request in StrictMode", () => {
  const load = vi.fn<() => Promise<SystemExtensionProjection>>();
  const { container } = render(<StrictMode><Harness
    actorId="user-a"
    entries={defineWorkspaceUiRegistry([])}
    load={load}
  /></StrictMode>);
  expect(load).not.toHaveBeenCalled();
  expect(container.innerHTML).toBe("");
});

test("two outlets share one actor request and denied rows never mount plugin effects", async () => {
  const load = vi.fn(async () => projection);
  function TwoOutlets() {
    const live = useWorkspaceExtensions("user-a", registry, load);
    const context = {
      slot: "workspace.side_panel" as const,
      actor: { id: "user-a", username: "a", displayName: "A" },
      notebook: { id: "notebook-a", name: "A" },
      source: null,
      uiMode: "advanced" as const,
      permissions,
    };
    return <>
      <WorkspaceExtensionOutlet slot="workspace.side_panel" registry={registry} projection={live} context={context} />
      <WorkspaceExtensionOutlet slot="source.detail_section" registry={registry} projection={live} context={{ ...context, slot: "source.detail_section" }} />
    </>;
  }
  render(<StrictMode><TwoOutlets /></StrictMode>);
  expect(await screen.findByTestId("extension")).toHaveTextContent("user-a:workspace");
  expect(load).toHaveBeenCalledTimes(1);
  expect(screen.getAllByTestId("extension")).toHaveLength(1);
});

test("permission and UI mode changes recompute live without refetching availability", async () => {
  const advancedRegistry = defineWorkspaceUiRegistry([{
    ...registry[0],
    permission: "notebook:write",
    mode: "advanced",
  }]);
  const load = vi.fn(async () => projection);
  function LiveGate({ uiMode, notebookWrite }: {
    uiMode: "auto" | "advanced";
    notebookWrite: boolean;
  }) {
    const live = useWorkspaceExtensions("user-a", advancedRegistry, load);
    return <WorkspaceExtensionOutlet
      slot="workspace.side_panel"
      registry={advancedRegistry}
      projection={live}
      context={{
        slot: "workspace.side_panel",
        actor: { id: "user-a", username: "a", displayName: "A" },
        notebook: { id: "notebook-a", name: "A" },
        source: null,
        uiMode,
        permissions: { ...permissions, notebookWrite },
      }}
    />;
  }
  const view = render(<LiveGate uiMode="auto" notebookWrite />);
  await act(async () => { await Promise.resolve(); });
  expect(screen.queryByTestId("extension")).toBeNull();
  view.rerender(<LiveGate uiMode="advanced" notebookWrite={false} />);
  expect(screen.queryByTestId("extension")).toBeNull();
  view.rerender(<LiveGate uiMode="advanced" notebookWrite />);
  expect(screen.getByTestId("extension")).toBeInTheDocument();
  expect(load).toHaveBeenCalledTimes(1);
});

test("actor A to B to A rejects the first generation response", async () => {
  const a1 = deferred<SystemExtensionProjection>();
  const b = deferred<SystemExtensionProjection>();
  const a2 = deferred<SystemExtensionProjection>();
  const load = vi.fn()
    .mockReturnValueOnce(a1.promise)
    .mockReturnValueOnce(b.promise)
    .mockReturnValueOnce(a2.promise);
  const view = render(<Harness actorId="user-a" load={load} />);
  view.rerender(<Harness actorId="user-b" load={load} />);
  view.rerender(<Harness actorId="user-a" load={load} />);

  await act(async () => { a1.resolve(projection); await Promise.resolve(); });
  expect(screen.queryByTestId("extension")).toBeNull();
  await act(async () => { a2.resolve(projection); await Promise.resolve(); });
  expect(screen.getByTestId("extension")).toHaveTextContent("user-a:workspace");
  expect(load).toHaveBeenCalledTimes(3);
  b.reject(new Error("stale"));
});
