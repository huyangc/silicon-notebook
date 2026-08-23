import { useState } from "react";
import { act, cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";

import { createOwnedWorkspaceExtensionActions } from "../../features/extension-sdk/actions";
import { ExtensionModal } from "../../features/extension-sdk/ui";
import { WorkspaceExtensionOutlet } from "../../features/extension-sdk/host";
import { defineWorkspaceUiRegistry } from "../../features/extension-sdk/registry";
import type {
  SystemExtensionProjection,
  WorkspaceExtensionContext,
  WorkspaceExtensionProps,
} from "../../features/extension-sdk/contracts";

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


// X5 T5 — SDK 共享弹窗 `ExtensionModal`。它是插件唯一被允许使用的弹窗外壳，所以钉的是
// 三件事：可及性与关闭语义、拖动手柄真的接上了共享浮窗实现（而不是自己搓了一个）、
// 以及**生命周期归 outlet 的 ownerKey 门**——它刻意不接
// `use-root-modal-coordinator`（实现计划 R4 登记接受），所以「切库/离开工作区时弹窗
// 跟着消失」这一条只能由那道门兑现，必须有用例证明它真的兑现了。

// jsdom 的 `window.innerWidth` 是整个 worker 共享的:下面那条窄视口用例把它按到 700
// 之后不还原,同文件里**之后**渲染浮窗的用例就都跑在「浮窗几何已停用」的世界里,而
// 它们断言的恰好是几何生效时才有的东西。还原写在 afterEach 里而不是那条用例末尾——
// 用例中途失败时 `afterEach` 仍会跑,写在末尾则不会。
const ORIGINAL_INNER_WIDTH = window.innerWidth;

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  Object.defineProperty(window, "innerWidth", {
    configurable: true, writable: true, value: ORIGINAL_INNER_WIDTH,
  });
  window.sessionStorage.clear();
});

const permissions = {
  notebookRead: true, notebookWrite: false, notebookConfigure: false,
  sourceRead: false, sourceWrite: false, systemAdmin: false,
};
// 壳层构造的那一半 context:**不含** `pluginId`，它由 host 按 contribution 逐条注入
// （`host.tsx` 的 outlet props 就是这个 `Omit`）。
const context: Omit<WorkspaceExtensionContext, "pluginId"> = {
  slot: "workspace.side_panel",
  actor: { id: "user-a", username: "user-a", displayName: "user-a" },
  notebook: { id: "notebook-a", name: "notebook-a" },
  source: null,
  uiMode: "auto",
  permissions,
};
const actions = {
  openUnderstanding: () => undefined,
  refreshSources: async () => {},
  api: {
    requestJson: async <T,>() => ({} as T),
    requestVoid: async () => {},
    requestBlob: async () => new Blob(),
    userMessage: (_error: unknown, fallback: string) => fallback,
  },
};

/**
 * 一条会开弹窗的合成 contribution：按钮打开，弹窗自己的 × 关闭。
 *
 * 插件把 `context.pluginId` 原样转交给弹窗——那是它拿到自己身份的唯一途径（host
 * 注入，插件说了不算），也是窗口位置存储键里隔离插件与插件的那一段。
 */
function ModalPlugin({ context: pluginContext }: WorkspaceExtensionProps) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>打开面板</button>
      {open && (
        <ExtensionModal
          pluginId={pluginContext.pluginId}
          storageKey="sample.panel"
          title="示例面板"
          description="一句说明"
          onClose={() => setOpen(false)}
        >
          <p>面板内容</p>
        </ExtensionModal>
      )}
    </>
  );
}

const modalRegistry = defineWorkspaceUiRegistry([{
  id: "sample-panel", pluginId: "sample-plugin", pluginVersion: "1.0.0",
  capability: "sample.ui.available", slot: "workspace.side_panel",
  permission: "notebook:read", mode: "all", Component: ModalPlugin,
}]);
const modalProjection: SystemExtensionProjection = { apiVersion: "1", extensions: [{
  pluginId: "sample-plugin", displayName: "Sample", version: "1.0.0",
  contributionId: "sample-panel", available: true, unavailableReason: null,
}] };

function Outlet({ ownerKey }: { ownerKey: string | null }) {
  // 镜像 page.tsx 的门：没有 owner 就整棵子树不渲染。
  if (!ownerKey) return null;
  return (
    <WorkspaceExtensionOutlet
      slot="workspace.side_panel"
      registry={modalRegistry}
      projection={modalProjection}
      context={context}
      actions={actions}
      ownerKey={ownerKey}
    />
  );
}


test("the shared modal is a labelled dialog whose close button fires exactly once", async () => {
  const user = userEvent.setup();
  const onClose = vi.fn();
  render(
    <ExtensionModal pluginId="sample-plugin" storageKey="sample.panel" title="示例面板" description="一句说明" onClose={onClose}>
      <p>面板内容</p>
    </ExtensionModal>,
  );
  const dialog = screen.getByRole("dialog", { name: "示例面板" });
  // 可及名取 title：弹窗里可能一个 <h2> 都没有被辅助技术关联上，aria-label 是唯一
  // 保证读得到名字的途径。
  expect(dialog).toHaveAttribute("aria-modal", "true");
  expect(dialog).toHaveClass("utility-modal");
  expect(screen.getByText("一句说明")).toBeInTheDocument();
  expect(screen.getByText("面板内容")).toBeInTheDocument();

  await user.click(screen.getByRole("button", { name: "关闭" }));
  expect(onClose).toHaveBeenCalledOnce();
});


test("the header is the shared floating-window drag handle, not a hand-rolled one", () => {
  const { container } = render(
    <ExtensionModal pluginId="sample-plugin" storageKey="sample.panel" title="示例面板" onClose={() => undefined}>
      <p>面板内容</p>
    </ExtensionModal>,
  );
  const header = container.querySelector(".source-modal-header") as HTMLElement;
  expect(header).not.toBeNull();
  // `touchAction: none` 与 grab 光标是 `useFloatingWindow` 的 dragHandleProps 独有的
  // 组合（`onPointerDown` 在 React 上不是 DOM 属性，量不到；能量到的是它施加的样式）。
  // 少了这两条就说明 dragHandleProps 根本没展开到标题栏上——弹窗仍然渲染、只是拖不动，
  // 是一次静默失败。
  expect(header.style.touchAction).toBe("none");
  expect(header.style.cursor).toBe("grab");
  // 没有 description 时不留一个空的 <p>。
  expect(container.querySelectorAll(".source-modal-header p")).toHaveLength(0);
});


test("narrow viewports hand the geometry back to CSS instead of pinning an inline transform", () => {
  Object.defineProperty(window, "innerWidth", { configurable: true, writable: true, value: 1024 });
  const { container } = render(
    <ExtensionModal pluginId="sample-plugin" storageKey="sample.panel" title="示例面板" onClose={() => undefined}>
      <p>面板内容</p>
    </ExtensionModal>,
  );
  const card = container.querySelector(".utility-modal-card") as HTMLElement;
  expect(card).not.toBeNull();
  // 桌面宽度下卡片带内联 transform（浮窗几何生效）。
  expect(card.style.transform).not.toBe("");

  act(() => {
    window.innerWidth = 700;
    window.dispatchEvent(new Event("resize"));
  });
  // ≤720px 一律停用浮窗几何：内联样式优先级高于媒体查询，留着它会把桌面记忆的
  // 位置/尺寸贴到整屏卡片上造成横向溢出。
  expect(card.style.transform).toBe("");
});


// 存储键的隔离：`extension.` 前缀只把插件与**核心**弹窗分开;插件与插件之间靠
// `pluginId` 段。少了那一段,两个插件各写一个 `storageKey="search"` 就共用同一格
// sessionStorage、互相顶掉窗口位置——而这种失败事后完全看不出来(它只表现为位置
// 偶尔"自己变了"),所以必须有用例钉住。
//
// 判据是**行为**不是字符串:往期望的键里预置一份位置记忆,看弹窗认不认。断言那个
// 键名本身的写法会在实现改成等价拼接时误报,而这样写只会在真的读错键时报红。
const SEEDED_RECT = JSON.stringify({ x: 120, y: 40, width: null, height: null });

function renderModalFor(pluginId: string) {
  const { container } = render(
    <ExtensionModal pluginId={pluginId} storageKey="sample.panel" title="示例面板" onClose={() => undefined}>
      <p>面板内容</p>
    </ExtensionModal>,
  );
  return (container.querySelector(".utility-modal-card") as HTMLElement).style.transform;
}


test("the window position memory is keyed per plugin, not just per storageKey", () => {
  window.sessionStorage.setItem("extension.sample-plugin.sample.panel.window", SEEDED_RECT);

  const owned = renderModalFor("sample-plugin");
  // 非空转保护:先证明预置的那份记忆真的被读到了(否则下面那条"另一个插件读不到"
  // 在实现根本不读 sessionStorage 时也会绿)。
  expect(owned).toContain("120px");
  expect(owned).toContain("40px");
  cleanup();

  // 同一个 storageKey、另一个 pluginId:必须读不到上面那份记忆。
  const other = renderModalFor("other-plugin");
  expect(other).not.toContain("120px");
  expect(other).not.toBe(owned);
  cleanup();

  // 同 pluginId + 同 storageKey 仍然共用同一格:分段不是把每次渲染都隔离掉。
  expect(renderModalFor("sample-plugin")).toBe(owned);
});


test("the modal refuses a malformed identity instead of silently building a stray key", () => {
  // 两段都校验:`pluginId` 走 host 注入(形状已由 registry 校验过),`storageKey` 完全
  // 由插件给。畸形键不会报错、只会安静地落进另一格记忆,所以这里要响亮失败。
  for (const pluginId of ["Sample", "../other", "", "sample plugin"]) {
    expect(() => renderModalFor(pluginId)).toThrow(TypeError);
    cleanup();
  }
  for (const storageKey of ["", "a/b", "a b", "..", "x".repeat(65)]) {
    expect(() => render(
      <ExtensionModal pluginId="sample-plugin" storageKey={storageKey} title="t" onClose={() => undefined}>
        <p>x</p>
      </ExtensionModal>,
    )).toThrow(TypeError);
    cleanup();
  }
});


test("the modal's lifetime is owned by the outlet's ownerKey gate", async () => {
  const user = userEvent.setup();
  // ① owner 消失（离开工作区/登出）——整棵子树卸载，弹窗跟着走。
  const view = render(<Outlet ownerKey="user-a:notebook-a:1" />);
  await user.click(screen.getByRole("button", { name: "打开面板" }));
  expect(screen.getByRole("dialog", { name: "示例面板" })).toBeInTheDocument();

  view.rerender(<Outlet ownerKey={null} />);
  expect(screen.queryByRole("dialog", { name: "示例面板" })).toBeNull();
  expect(view.container).toBeEmptyDOMElement();

  // ② owner 换代（切库）——outlet 仍在，但 contribution 的 React key 含 ownerKey，
  // 组件重挂载、`open` 状态被丢弃。这是切库的真实路径：不靠它，用户会在新笔记本里
  // 看到一个仍开着的、装着上一本库数据的插件弹窗。
  view.rerender(<Outlet ownerKey="user-a:notebook-a:1" />);
  await user.click(screen.getByRole("button", { name: "打开面板" }));
  expect(screen.getByRole("dialog", { name: "示例面板" })).toBeInTheDocument();

  view.rerender(<Outlet ownerKey="user-a:notebook-b:2" />);
  expect(screen.queryByRole("dialog", { name: "示例面板" })).toBeNull();
  expect(screen.getByRole("button", { name: "打开面板" })).toBeInTheDocument();
});
