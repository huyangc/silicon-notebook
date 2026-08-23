import { expect, test, vi } from "vitest";

import { createOwnedWorkspaceExtensionActions } from "../../features/extension-sdk/actions";

// X5 T4 — `actions.refreshSources()` 是双闸窄 command 中的 extension-owner 那一半
// （另一半是 `use-source-library.ts:277-279` 自己的 notebook 闸，那一半不在本文件
// 覆盖范围内：`createOwnedWorkspaceExtensionActions` 收到的 `refreshSources` 参数
// 在这里就是一个纯 spy，真正的 `loadSourcesPage` 闸由 use-source-library 自己的
// 单测钉住）。这里只钉「exact-owner freeze-then-revalidate」这道闸本身：与
// `extension-ui-host.component.test.tsx` 里 A-G1/A-G3 那条 `openUnderstanding`
// 用例同构，换成 `refreshSources` 并额外覆盖 promise 语义与换用户的形态。
//
// ⚠ 「静默 resolve」只覆盖 **owner 闸拒绝**这一支：那不是错误，是这次刷新已经没有
// 意义了，不该让插件的 `await actions.refreshSources()` 挂起或抛错。闸放行之后，
// 底下的核心加载失败会**原样 reject**（`use-source-library.ts` 的 `loadSourcesPage`
// 在请求仍是当前请求时 `throw error`），插件必须自己 catch 并用
// `api.userMessage(error, fallback)` 出文案——最后一条用例钉的就是这个方向。

type Owner = { actorId: string; notebookId: string; generation: number };


test("refreshSources delegates to the injected command exactly once under the current owner", async () => {
  const openUnderstanding = vi.fn();
  const refreshSources = vi.fn(async () => {});
  const owner: Owner = { actorId: "user-a", notebookId: "notebook-a", generation: 1 };
  const actions = createOwnedWorkspaceExtensionActions(
    owner,
    (candidate) => (
      candidate.actorId === owner.actorId
      && candidate.notebookId === owner.notebookId
      && candidate.generation === owner.generation
    ),
    openUnderstanding,
    refreshSources,
  );
  await actions.refreshSources();
  expect(refreshSources).toHaveBeenCalledOnce();
  expect(openUnderstanding).not.toHaveBeenCalled();
});


test("a stale refreshSources call after a workspace switch (generation 1 -> 3) resolves without invoking the command", async () => {
  const openUnderstanding = vi.fn();
  const refreshSources = vi.fn(async () => {});
  const generation = { current: 1 };
  const staleOwner: Owner = { actorId: "user-a", notebookId: "notebook-a", generation: 1 };
  const staleAction = createOwnedWorkspaceExtensionActions(
    staleOwner,
    (candidate) => (
      candidate.actorId === staleOwner.actorId
      && candidate.notebookId === staleOwner.notebookId
      && candidate.generation === generation.current
    ),
    openUnderstanding,
    refreshSources,
  );
  // 两次切库：notebook-a(G1) -> notebook-b(G2) -> notebook-a(G3)。staleAction 是
  // A-G1 那次冻结出来的引用，即使浏览器或组件仍握着它的旧回调，也不能作用于
  // A-G3 之后的工作区。
  generation.current = 2;
  generation.current = 3;
  await expect(staleAction.refreshSources()).resolves.toBeUndefined();
  expect(refreshSources).not.toHaveBeenCalled();
});


test("a core load failure reaches the plugin as a rejection, not a silent resolve", async () => {
  // 与 owner 闸拒绝那两条**方向相反**：闸放行了，失败的是底下的核心加载。
  // `loadSourcesPage` 在「请求仍是当前请求」时把异常原样 throw 出来，端口这一层不得
  // 把它吞成 resolve——那会让插件以为列表已经刷新、界面停在旧数据上而不给任何提示。
  const openUnderstanding = vi.fn();
  const failure = new TypeError("network drop");
  const refreshSources = vi.fn(async () => { throw failure; });
  const owner: Owner = { actorId: "user-a", notebookId: "notebook-a", generation: 1 };
  const actions = createOwnedWorkspaceExtensionActions(
    owner,
    () => true,
    openUnderstanding,
    refreshSources,
  );
  await expect(actions.refreshSources()).rejects.toBe(failure);
  expect(refreshSources).toHaveBeenCalledOnce();
});


test("a refreshSources call from a different actor is dropped by the owner gate", async () => {
  const openUnderstanding = vi.fn();
  const refreshSources = vi.fn(async () => {});
  // live 代表当前真正登录的 actor（user-b）；staleActorActions 是按 user-a 冻结出的
  // actions——`owns` 比对的是「候选是不是当前 live owner」，与实际接线中
  // `workspaceExtensions.owns` 的语义一致（见 use-workspace-extensions.ts）。
  const live: Owner = { actorId: "user-b", notebookId: "notebook-a", generation: 1 };
  const staleActorActions = createOwnedWorkspaceExtensionActions(
    { actorId: "user-a", notebookId: "notebook-a", generation: 1 },
    (candidate) => (
      candidate.actorId === live.actorId
      && candidate.notebookId === live.notebookId
      && candidate.generation === live.generation
    ),
    openUnderstanding,
    refreshSources,
  );
  await expect(staleActorActions.refreshSources()).resolves.toBeUndefined();
  expect(refreshSources).not.toHaveBeenCalled();
});
