import { StrictMode, forwardRef, useImperativeHandle } from "react";
import { act, cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";

import { useWorkspaceExtensions, type WorkspaceExtensions } from "../../app/use-workspace-extensions";
import { WorkspaceExtensionOutlet } from "../../features/extension-sdk/host";
import { createOwnedWorkspaceExtensionActions } from "../../features/extension-sdk/actions";
import {
  BUILTIN_WORKSPACE_UI_CONTRIBUTIONS,
  defineWorkspaceUiRegistry,
} from "../../features/extension-sdk/registry";
import { WORKSPACE_UI_CONTRIBUTIONS } from "../../features/extension-sdk/workspace-registry";
import type {
  SystemExtensionProjection,
  WorkspaceExtensionProps,
} from "../../features/extension-sdk/contracts";

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
// 两条 contribution、两个不同的 pluginId，各自渲染一个只会调 `actions.api` 的按钮。
// 两条同时在场是承重的：一条证明不了端口是**按 contribution** 绑定的——那种形态下
// "outlet 传了某个 api" 与 "outlet 传了正确的 api" 长得一模一样。
function apiProbe(testId: string) {
  return function ApiProbe({ actions }: WorkspaceExtensionProps) {
    return (
      <button
        data-testid={testId}
        onClick={() => {
          void actions.api.requestJson("/x", {
            // 伪造的鉴权凭据必须在端口里就被剥掉。断言那一端刻意不写成
            // `Authorization === null`——不发这两个头时它本来就是 null，那种断言
            // 在剥离逻辑被整个删掉之后照样绿。
            headers: { Authorization: "Bearer forged", Cookie: "session=forged" },
          });
        }}
      >
        probe
      </button>
    );
  };
}
const apiRegistry = defineWorkspaceUiRegistry([{
  id: "sample-panel", pluginId: "sample-plugin", pluginVersion: "1.0.0",
  capability: "sample.ui.available", slot: "workspace.side_panel",
  permission: "notebook:read", mode: "all", Component: apiProbe("probe-sample"),
}, {
  id: "other-panel", pluginId: "other-plugin", pluginVersion: "1.0.0",
  capability: "other.ui.available", slot: "workspace.side_panel",
  permission: "notebook:read", mode: "all", Component: apiProbe("probe-other"),
}]);
const apiProjection: SystemExtensionProjection = { apiVersion: "1", extensions: [{
  pluginId: "sample-plugin", displayName: "Sample", version: "1.0.0",
  contributionId: "sample-panel", available: true, unavailableReason: null,
}, {
  pluginId: "other-plugin", displayName: "Other", version: "1.0.0",
  contributionId: "other-panel", available: true, unavailableReason: null,
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
      actions={{ openUnderstanding: onOpen, refreshSources: async () => {} }} context={context} />
    <WorkspaceExtensionOutlet slot="source.detail_section" registry={entries}
      projection={live.projection} ownerKey={live.ownerKey}
      actions={{ openUnderstanding: onOpen, refreshSources: async () => {} }} context={{ ...context, slot: "source.detail_section" }} />
  </>;
});

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

function commit(ref: { current: WorkspaceExtensions | null }, actorId: string, notebookId: string, workspaceEpoch: number) {
  const transition = ref.current!.beginNotebookTransition({ actorId, notebookId, workspaceEpoch });
  ref.current!.finishNotebookTransition(transition!, true);
}

test("with zero local plugins the merged registry is the builtin one, entry for entry", () => {
  // 本仓库不带任何仓库外插件，所以生成的 registry.local.ts 是空数组存根，合并
  // registry 必须与内建目录逐字相同——拆成两个模块不得改变默认部署看到的任何东西。
  //
  // 这两条长度断言是**登记接受的取舍**：装了私有插件的那棵树上，合并 registry 会长
  // 出更多条目，这条用例必然红。基座的 `npm run test` 因此只是**公网仓库**（零插件）
  // 的验收；私有部署的验收是 `.local/ui-extension-contract.json` 与后端投影对账
  // 加 `npm run build`（见实现计划 T7 的文档条目）。把它放宽成 `>= 1` 就等于不再钉
  // 「零插件时合并结果逐字等于内建目录」——而那正是拆模块这件事唯一要证明的性质。
  expect(BUILTIN_WORKSPACE_UI_CONTRIBUTIONS).toHaveLength(1);
  expect(WORKSPACE_UI_CONTRIBUTIONS).toHaveLength(1);
  expect(WORKSPACE_UI_CONTRIBUTIONS).toEqual(BUILTIN_WORKSPACE_UI_CONTRIBUTIONS);
  // 合并结果仍是 defineWorkspaceUiRegistry 的产物：直接拼两个数组会在零插件部署上
  // 看不出差别，却丢掉冻结——以及本地条目本该受到的那一整套元数据校验。
  expect(Object.isFrozen(WORKSPACE_UI_CONTRIBUTIONS)).toBe(true);
  expect(Object.isFrozen(WORKSPACE_UI_CONTRIBUTIONS[0])).toBe(true);
  const [merged] = WORKSPACE_UI_CONTRIBUTIONS;
  const [builtin] = BUILTIN_WORKSPACE_UI_CONTRIBUTIONS;
  for (const field of [
    "id", "pluginId", "pluginVersion", "capability", "slot", "permission", "mode", "Component",
  ] as const) {
    expect(merged[field]).toBe(builtin[field]);
  }
});

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
  expect(await screen.findByRole("button", { name: "AI 对这个库的理解" })).toBeInTheDocument();
  expect(load).toHaveBeenCalledTimes(1);
  expect(onOpen).not.toHaveBeenCalled();
  expect(screen.getAllByLabelText("AI 对这个库的理解")).toHaveLength(1);
  const sidePanel = view.container.querySelector(".workspace-extension-outlet-workspace-side_panel");
  expect(screen.getByLabelText("AI 对这个库的理解").closest("aside")).toBe(sidePanel);
  expect(view.container.querySelectorAll(".workspace-extension-outlet-workspace-side_panel")).toHaveLength(1);
  expect(sidePanel?.parentElement).toBe(view.container);
  await user.click(screen.getByRole("button", { name: "AI 对这个库的理解" }));
  expect(onOpen).toHaveBeenCalledOnce();
});

test("the outlet injects an api port bound to each contribution's own plugin id", async () => {
  // 走的是**真的**默认 transport（api-client → 核心请求管线），只把最外层的网络出口
  // 换成假的：这样 `/extensions/<id>/` 前缀、API_BASE 约束与 options 口径是一起被
  // 证明的，而不是靠一个替身 transport 自说自话。
  const user = userEvent.setup();
  const calls: { url: string; init: RequestInit }[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: unknown, init: RequestInit) => {
    calls.push({ url: String(url), init });
    return {
      ok: true, status: 200,
      headers: { get: () => null },
      json: async () => ({ ok: true }),
    };
  }));
  const load = vi.fn(async () => apiProjection);
  const ref = { current: null as WorkspaceExtensions | null };
  render(<Harness ref={ref} actorId="user-a" notebookId="notebook-a" entries={apiRegistry} load={load} />);
  act(() => commit(ref, "user-a", "notebook-a", 1));

  await user.click(await screen.findByTestId("probe-sample"));
  await user.click(await screen.findByTestId("probe-other"));
  expect(calls).toHaveLength(2);
  expect(calls.map((entry) => new URL(entry.url).pathname)).toEqual([
    "/api/extensions/sample-plugin/x",
    "/api/extensions/other-plugin/x",
  ]);
  // 核心身份由端口固定：插件设不了鉴权凭据，也改不了 401 的处置。probe 真的塞了一份
  // 伪造的 Authorization/Cookie（见 apiProbe），所以这里量的是"剥掉了没有"而不是
  // "本来就没有"。
  const sent = new Headers(calls[0].init.headers);
  expect(sent.get("Authorization")).not.toBe("Bearer forged");
  expect(sent.get("Cookie")).not.toBe("session=forged");
  // 本夹具没有登录 token（localStorage 是空的），所以核心那一步不会补 Authorization，
  // 剥离之后这个头一个都不剩。有 token 的部署上它会是核心自己那份 `Bearer <token>`。
  expect(sent.get("Authorization")).toBeNull();
  expect(sent.get("Cookie")).toBeNull();
  expect(calls[0].init.method).toBeUndefined();
  expect(load).toHaveBeenCalledTimes(1);
});


test("owned actions reject an A-G1 callback after A-B-A and admit the current A-G3 callback", () => {
  const openUnderstanding = vi.fn();
  const refreshSources = vi.fn(async () => {});
  const generation = { current: 1 };
  const oldAction = createOwnedWorkspaceExtensionActions(
    { actorId: "user-a", notebookId: "notebook-a", generation: 1 },
    (owner) => owner.generation === generation.current,
    openUnderstanding,
    refreshSources,
  );
  generation.current = 2;
  generation.current = 3;
  oldAction.openUnderstanding();
  expect(openUnderstanding).not.toHaveBeenCalled();

  const currentAction = createOwnedWorkspaceExtensionActions(
    { actorId: "user-a", notebookId: "notebook-a", generation: 3 },
    (owner) => owner.generation === generation.current,
    openUnderstanding,
    refreshSources,
  );
  currentAction.openUnderstanding();
  expect(openUnderstanding).toHaveBeenCalledOnce();
});

test("permission denial unmounts the real plugin without refetching", async () => {
  const load = vi.fn(async () => realProjection);
  const ref = { current: null as WorkspaceExtensions | null };
  const view = render(<Harness ref={ref} actorId="user-a" notebookId="notebook-a" load={load} />);
  act(() => commit(ref, "user-a", "notebook-a", 1));
  await screen.findByRole("button", { name: "AI 对这个库的理解" });
  view.rerender(<Harness ref={ref} actorId="user-a" notebookId="notebook-a" load={load} notebookRead={false} />);
  expect(screen.queryByRole("button", { name: "AI 对这个库的理解" })).toBeNull();
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
  expect(screen.queryByRole("button", { name: "AI 对这个库的理解" })).toBeNull();
  expect(view.container.querySelector(".workspace-extension-outlet-workspace-side_panel")).toBeNull();
  expect(view.container).toBeEmptyDOMElement();
  expect(load).toHaveBeenCalledTimes(1);
});

test("same-actor A-B-A transitions hide synchronously, reuse projection and reject old owner", async () => {
  const load = vi.fn(async () => realProjection);
  const ref = { current: null as WorkspaceExtensions | null };
  const view = render(<Harness ref={ref} actorId="user-a" notebookId="notebook-a" load={load} />);
  act(() => commit(ref, "user-a", "notebook-a", 1));
  await screen.findByRole("button", { name: "AI 对这个库的理解" });
  const oldOwner = ref.current!.owner!;
  let next: ReturnType<WorkspaceExtensions["beginNotebookTransition"]>;
  act(() => { next = ref.current!.beginNotebookTransition({ actorId: "user-a", notebookId: "notebook-b", workspaceEpoch: 2 }); });
  expect(screen.queryByRole("button", { name: "AI 对这个库的理解" })).toBeNull();
  expect(ref.current!.owns(oldOwner)).toBe(false);
  view.rerender(<Harness ref={ref} actorId="user-a" notebookId="notebook-b" load={load} />);
  act(() => ref.current!.finishNotebookTransition(next!, true));
  expect(await screen.findByRole("button", { name: "AI 对这个库的理解" })).toBeInTheDocument();
  let returned!: ReturnType<WorkspaceExtensions["beginNotebookTransition"]>;
  act(() => { returned = ref.current!.beginNotebookTransition({ actorId: "user-a", notebookId: "notebook-a", workspaceEpoch: 3 }); });
  view.rerender(<Harness ref={ref} actorId="user-a" notebookId="notebook-a" load={load} />);
  act(() => ref.current!.finishNotebookTransition(returned!, true));
  expect(await screen.findByRole("button", { name: "AI 对这个库的理解" })).toBeInTheDocument();
  expect(ref.current!.owns(oldOwner)).toBe(false);
  expect(load).toHaveBeenCalledTimes(1);
});

test("a late older workspace finish cannot replace the latest successful transition", async () => {
  const load = vi.fn(async () => realProjection);
  const ref = { current: null as WorkspaceExtensions | null };
  const view = render(<Harness ref={ref} actorId="user-a" notebookId="notebook-a" load={load} />);
  act(() => commit(ref, "user-a", "notebook-a", 1));
  await screen.findByRole("button", { name: "AI 对这个库的理解" });

  let older!: ReturnType<WorkspaceExtensions["beginNotebookTransition"]>;
  act(() => { older = ref.current!.beginNotebookTransition({ actorId: "user-a", notebookId: "notebook-b", workspaceEpoch: 2 }); });
  view.rerender(<Harness ref={ref} actorId="user-a" notebookId="notebook-b" load={load} />);
  let latest!: ReturnType<WorkspaceExtensions["beginNotebookTransition"]>;
  act(() => { latest = ref.current!.beginNotebookTransition({ actorId: "user-a", notebookId: "notebook-a", workspaceEpoch: 3 }); });
  view.rerender(<Harness ref={ref} actorId="user-a" notebookId="notebook-a" load={load} />);
  act(() => ref.current!.finishNotebookTransition(latest!, true));
  expect(await screen.findByRole("button", { name: "AI 对这个库的理解" })).toBeInTheDocument();
  const currentOwner = ref.current!.owner!;

  act(() => ref.current!.finishNotebookTransition(older!, true));
  expect(screen.getByRole("button", { name: "AI 对这个库的理解" })).toBeInTheDocument();
  expect(ref.current!.owner).toBe(currentOwner);
  expect(ref.current!.owns(currentOwner)).toBe(true);
  expect(ref.current!.owns(older!)).toBe(false);
  expect(load).toHaveBeenCalledTimes(1);
});

test("failed transition remains suspended", async () => {
  const load = vi.fn(async () => realProjection);
  const ref = { current: null as WorkspaceExtensions | null };
  render(<Harness ref={ref} actorId="user-a" notebookId="notebook-a" load={load} />);
  act(() => commit(ref, "user-a", "notebook-a", 1));
  await screen.findByRole("button", { name: "AI 对这个库的理解" });
  act(() => {
    const transition = ref.current!.beginNotebookTransition({ actorId: "user-a", notebookId: "notebook-b", workspaceEpoch: 2 });
    ref.current!.finishNotebookTransition(transition!, false);
  });
  expect(screen.queryByRole("button", { name: "AI 对这个库的理解" })).toBeNull();
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

test("a failed projection request is re-issued on the next committed workspace", async () => {
  const first = deferred<SystemExtensionProjection>();
  const second = deferred<SystemExtensionProjection>();
  const load = vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
  const ref = { current: null as WorkspaceExtensions | null };
  const view = render(<Harness ref={ref} actorId="user-a" notebookId="notebook-a" entries={syntheticRegistry} load={load} />);
  act(() => commit(ref, "user-a", "notebook-a", 1));
  expect(load).toHaveBeenCalledTimes(1);
  await act(async () => { first.reject(new Error("network drop")); await Promise.resolve(); });
  expect(screen.queryByTestId("extension")).toBeNull();
  expect(load).toHaveBeenCalledTimes(1);

  // Same actor, next committed workspace (a notebook switch) must retry rather
  // than reuse the request the network drop already spent.
  view.rerender(<Harness ref={ref} actorId="user-a" notebookId="notebook-b" entries={syntheticRegistry} load={load} />);
  act(() => commit(ref, "user-a", "notebook-b", 2));
  expect(load).toHaveBeenCalledTimes(2);
  await act(async () => { second.resolve(syntheticProjection); await Promise.resolve(); });
  expect(screen.getByTestId("extension")).toBeInTheDocument();
  expect(load).toHaveBeenCalledTimes(2);
});
