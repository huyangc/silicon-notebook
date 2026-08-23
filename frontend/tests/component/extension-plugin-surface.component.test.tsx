import { useRef, useState } from "react";
import { act, cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";

import { useRootModalCoordinator } from "../../app/use-root-modal-coordinator";
import { createOwnedWorkspaceExtensionActions } from "../../features/extension-sdk/actions";
import { ExtensionModal } from "../../features/extension-sdk/ui";
import { WorkspaceExtensionOutlet } from "../../features/extension-sdk/host";
import { defineWorkspaceUiRegistry } from "../../features/extension-sdk/registry";
import type {
  SystemExtensionProjection,
  WorkspaceExtensionContext,
  WorkspaceExtensionPluginActions,
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
    () => undefined,
    () => undefined,
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
    () => undefined,
    () => undefined,
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
    () => undefined,
    () => undefined,
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
    () => undefined,
    () => undefined,
  );
  await expect(staleActorActions.refreshSources()).resolves.toBeUndefined();
  expect(refreshSources).not.toHaveBeenCalled();
});


// X5 T5 — SDK 共享弹窗 `ExtensionModal`。它是插件唯一被允许使用的弹窗外壳，所以钉的是
// 四件事：可及性与关闭语义、拖动手柄真的接上了共享浮窗实现（而不是自己搓了一个）、
// **接入核心 root-dialog 裁决**（codex #578 R1 P2：被盖住时退出交互树、一次只有一个
// 插件弹窗、认领与释放都只经协调器），以及生命周期的第二道兜底——outlet 的 ownerKey
// 门（切库/离开工作区时整棵子树卸载）。

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
  coordinator = null;
  lastPluginActions = new Map();
});

const permissions = {
  notebookRead: true, notebookWrite: false, notebookConfigure: false,
  sourceRead: false, sourceWrite: false, systemAdmin: false,
};
// 壳层构造的那一半 context:**不含** `pluginId` 与 `dialog`，两者都由 host 按
// contribution 逐条注入（`host.tsx` 的 outlet props 就是这个 `Omit`）。
const context: Omit<WorkspaceExtensionContext, "pluginId" | "dialog"> = {
  slot: "workspace.side_panel",
  actor: { id: "user-a", username: "user-a", displayName: "user-a" },
  notebook: { id: "notebook-a", name: "notebook-a" },
  source: null,
  uiMode: "auto",
  permissions,
};

/** 直接渲染 `ExtensionModal` 的用例用的那份 host 注入结果：开着且在最上层。 */
const TOPMOST_DIALOG = { open: true, topmost: true, zIndex: 60 } as const;

function modalContext(
  pluginId: string,
  dialog: WorkspaceExtensionContext["dialog"] = TOPMOST_DIALOG,
): WorkspaceExtensionContext {
  return { ...context, pluginId, dialog };
}

/**
 * 一条会开弹窗的合成 contribution：按钮**请求**打开，弹窗自己的 × 请求关闭。
 *
 * 它刻意**没有** `useState`——弹窗的可见性归核心的 root-dialog 裁决（`context.dialog`），
 * 插件自己那份状态既关不掉一次被顶掉的弹窗、也会与协调器分叉。`context.pluginId` 与
 * `context.dialog` 都由 host 注入，插件只是把整份 context 原样转交。
 *
 * 用工厂而不是三份复制：三条 contribution 只在标题/按钮文案/存储键上不同，复制一份
 * JSX 必然分叉。`contributionId` 只用来把 actions 记进 `lastPluginActions`（用例要拿
 * 迟到的旧回调）——**插件自己拿不到它**，`openDialog()` 是 host 绑好的零参数命令。
 */
function modalPanel(options: {
  contributionId: string;
  openLabel: string;
  title: string;
  storageKey: string;
  bodyText: string;
}) {
  return function Panel({ context: pluginContext, actions: pluginActions }: WorkspaceExtensionProps) {
    lastPluginActions.set(options.contributionId, pluginActions);
    return (
      <>
        <button type="button" onClick={() => pluginActions.openDialog()}>{options.openLabel}</button>
        <ExtensionModal
          context={pluginContext}
          actions={pluginActions}
          storageKey={options.storageKey}
          title={options.title}
          description="一句说明"
        >
          <p>{options.bodyText}</p>
        </ExtensionModal>
      </>
    );
  };
}

const modalRegistry = defineWorkspaceUiRegistry([{
  id: "sample-panel", pluginId: "sample-plugin", pluginVersion: "1.0.0",
  capability: "sample.ui.available", slot: "workspace.side_panel",
  permission: "notebook:read", mode: "all",
  Component: modalPanel({
    contributionId: "sample-panel", openLabel: "打开面板",
    title: "示例面板", storageKey: "sample.panel", bodyText: "面板内容",
  }),
}, {
  // **同一个插件的第二条 contribution**：持有权按 contribution id 而不是 plugin id，
  // 否则这两条会同时看到 `dialog.open === true`、一起挂出弹窗（codex #578 R1 P2）。
  id: "sample-second-panel", pluginId: "sample-plugin", pluginVersion: "1.0.0",
  capability: "sample.ui.available", slot: "workspace.side_panel",
  permission: "notebook:read", mode: "all",
  Component: modalPanel({
    contributionId: "sample-second-panel", openLabel: "打开同插件第二面板",
    title: "同插件第二面板", storageKey: "sample.second", bodyText: "第二面板内容",
  }),
}, {
  id: "other-panel", pluginId: "other-plugin", pluginVersion: "1.0.0",
  capability: "other.ui.available", slot: "workspace.side_panel",
  permission: "notebook:read", mode: "all",
  Component: modalPanel({
    contributionId: "other-panel", openLabel: "打开另一个面板",
    title: "另一个面板", storageKey: "other.panel", bodyText: "另一个面板内容",
  }),
}]);
const modalProjection: SystemExtensionProjection = { apiVersion: "1", extensions: [{
  pluginId: "sample-plugin", displayName: "Sample", version: "1.0.0",
  contributionId: "sample-panel", available: true, unavailableReason: null,
}, {
  pluginId: "sample-plugin", displayName: "Sample", version: "1.0.0",
  contributionId: "sample-second-panel", available: true, unavailableReason: null,
}, {
  pluginId: "other-plugin", displayName: "Other", version: "1.0.0",
  contributionId: "other-panel", available: true, unavailableReason: null,
}] };

/**
 * 壳层的最小复刻：`page.tsx` 的三样接线——协调器、`extension` 那格的持有者 state、
 * 以及"认领只经 `rootModals.open`、释放只经 `onClosed`"这两条。用例通过 `coordinator`
 * 直接驱动核心侧（进工作区、开一个盖在上面的 `info` 层、开一个冲突的 primary）。
 */
let coordinator: ReturnType<typeof useRootModalCoordinator> | null = null;
/**
 * 用例可以抓下来的一份「上一次渲染时插件手上的 actions」，用于模拟迟到的旧回调。
 * 键是 **contribution id**——那正是弹窗持有权的粒度。
 */
let lastPluginActions = new Map<string, WorkspaceExtensionPluginActions>();

function Outlet({ ownerKey }: { ownerKey: string | null }) {
  const [holder, setHolder] = useState<string | null>(null);
  // 与 page.tsx 逐字同构：关闭请求按**当时**的持有者判，所以另留一份 ref。
  const holderRef = useRef<string | null>(null);
  const rootModals = useRootModalCoordinator({
    actorId: "user-a",
    sourceId: null,
    onClosed: (slot) => {
      if (slot !== "extension") return;
      holderRef.current = null;
      setHolder(null);
    },
  });
  coordinator = rootModals;
  const outletActions = createOwnedWorkspaceExtensionActions(
    { actorId: "user-a" },
    () => true,
    () => undefined,
    async () => {},
    (contributionId) => {
      if (rootModals.open("extension", rootModals.captureWorkspaceOwner())) {
        holderRef.current = contributionId;
        setHolder(contributionId);
      }
    },
    (contributionId, reason) => {
      if (holderRef.current !== contributionId) return;
      rootModals.requestClose("extension", reason);
    },
  );
  // 镜像 page.tsx 的门：没有 owner 就整棵子树不渲染。
  if (!ownerKey) return null;
  return (
    <WorkspaceExtensionOutlet
      slot="workspace.side_panel"
      registry={modalRegistry}
      projection={modalProjection}
      context={context}
      actions={outletActions}
      ownerKey={ownerKey}
      dialog={rootModals.view("extension")}
      dialogHolder={holder}
    />
  );
}

function enterWorkspace(notebookId = "notebook-a", workspaceEpoch = 1) {
  act(() => {
    const transition = coordinator!.beginWorkspaceTransition();
    coordinator!.finishWorkspaceTransition(transition, {
      actorId: "user-a", notebookId, workspaceEpoch,
    });
  });
}


test("the shared modal is a labelled dialog whose close button asks the coordinator exactly once", async () => {
  const user = userEvent.setup();
  const closeDialog = vi.fn();
  render(
    <ExtensionModal context={modalContext("sample-plugin")} actions={{ closeDialog }} storageKey="sample.panel" title="示例面板" description="一句说明">
      <p>面板内容</p>
    </ExtensionModal>,
  );
  const dialog = screen.getByRole("dialog", { name: "示例面板" });
  // 可及名取 title：弹窗里可能一个 <h2> 都没有被辅助技术关联上，aria-label 是唯一
  // 保证读得到名字的途径。
  expect(dialog).toHaveAttribute("aria-modal", "true");
  expect(dialog).toHaveClass("utility-modal");
  // 层级由协调器给，不由样式表的 `.utility-modal { z-index: 60 }` 兜底——插件弹窗与
  // 核心弹窗同处一个 primary 冲突组，落不到 DOM 上就没法参与排序。
  expect(dialog.style.zIndex).toBe("60");
  expect(screen.getByText("一句说明")).toBeInTheDocument();
  expect(screen.getByText("面板内容")).toBeInTheDocument();

  await user.click(screen.getByRole("button", { name: "关闭" }));
  expect(closeDialog).toHaveBeenCalledOnce();
});


test("a closed dialog view renders nothing at all", () => {
  // 可见性归核心：插件没有第二个开关。这条同时是下面那些"开着"的用例的反向保护。
  const { container } = render(
    <ExtensionModal
      context={modalContext("sample-plugin", { open: false, topmost: false, zIndex: 0 })}
      actions={{ closeDialog: () => undefined }}
      storageKey="sample.panel"
      title="示例面板"
    >
      <p>面板内容</p>
    </ExtensionModal>,
  );
  expect(container).toBeEmptyDOMElement();
});


test("a covered dialog leaves the interaction tree instead of staying focusable behind the top layer", () => {
  render(
    <ExtensionModal
      context={modalContext("sample-plugin", { open: true, topmost: false, zIndex: 60 })}
      actions={{ closeDialog: () => undefined }}
      storageKey="sample.panel"
      title="示例面板"
    >
      <button type="button">面板里的按钮</button>
    </ExtensionModal>,
  );
  // `getByRole` 会跳过 aria-hidden 的子树，所以这里按 class 取。
  const dialog = document.querySelector(".utility-modal") as HTMLElement;
  expect(dialog).not.toBeNull();
  expect(dialog).toHaveAttribute("aria-hidden", "true");
  expect(dialog).toHaveAttribute("inert");
  expect(dialog).toHaveAttribute("aria-modal", "false");
});


test("the modal refuses a host-injected dialog view that is not a coordinator view", () => {
  // `context.dialog` 缺席不会报错、只会让弹窗永远不出现（或永远不退出交互树）——
  // 与畸形存储键同类的静默失败，所以这里同样响亮失败。
  const broken = { ...context, pluginId: "sample-plugin" } as unknown as WorkspaceExtensionContext;
  expect(() => render(
    <ExtensionModal context={broken} actions={{ closeDialog: () => undefined }} storageKey="sample.panel" title="t">
      <p>x</p>
    </ExtensionModal>,
  )).toThrow(TypeError);
});


test("the header is the shared floating-window drag handle, not a hand-rolled one", () => {
  const { container } = render(
    <ExtensionModal context={modalContext("sample-plugin")} actions={{ closeDialog: () => undefined }} storageKey="sample.panel" title="示例面板">
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
    <ExtensionModal context={modalContext("sample-plugin")} actions={{ closeDialog: () => undefined }} storageKey="sample.panel" title="示例面板">
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
    <ExtensionModal context={modalContext(pluginId)} actions={{ closeDialog: () => undefined }} storageKey="sample.panel" title="示例面板">
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
      <ExtensionModal context={modalContext("sample-plugin")} actions={{ closeDialog: () => undefined }} storageKey={storageKey} title="t">
        <p>x</p>
      </ExtensionModal>,
    )).toThrow(TypeError);
    cleanup();
  }
});


// ---------------------------------------------------------------------------
// codex #578 R1 P2 — 插件弹窗接入 root-dialog 裁决。下面这组用例用的都是那份最小
// 壳层复刻（`Outlet`），驱动的是真的 `useRootModalCoordinator`。

test("openDialog claims the coordinator slot and the dialog becomes topmost", async () => {
  const user = userEvent.setup();
  render(<Outlet ownerKey="user-a:notebook-a:1" />);
  enterWorkspace();
  expect(screen.queryByRole("dialog", { name: "示例面板" })).toBeNull();

  await user.click(screen.getByRole("button", { name: "打开面板" }));
  const dialog = screen.getByRole("dialog", { name: "示例面板" });
  expect(dialog).toBeInTheDocument();
  expect(dialog).toHaveAttribute("aria-modal", "true");
  expect(dialog).not.toHaveAttribute("inert");
  // 认领的是核心那格，不是插件自己的一份影子状态。
  expect(coordinator!.view("extension").open).toBe(true);
});


test("a core dialog above the extension dialog pushes it out of the interaction tree", async () => {
  const user = userEvent.setup();
  render(<Outlet ownerKey="user-a:notebook-a:1" />);
  enterWorkspace();
  await user.click(screen.getByRole("button", { name: "打开面板" }));
  expect(screen.getByRole("dialog", { name: "示例面板" })).not.toHaveAttribute("inert");

  // `info` 是唯一合法压在 primary 之上的层（conflictGroup 为 null、layer 80）——它才是
  // 「盖住」的真实形态。另一个 **primary** 会走冲突分支把插件弹窗整个关掉，见下一条。
  act(() => { coordinator!.open("info", coordinator!.captureWorkspaceOwner()); });
  const covered = document.querySelector(".utility-modal") as HTMLElement;
  expect(covered).not.toBeNull();
  expect(covered).toHaveAttribute("inert");
  expect(covered).toHaveAttribute("aria-hidden", "true");
  expect(covered).toHaveAttribute("aria-modal", "false");
  // 还在 DOM 里（只是退出了交互树），否则这条断言与「被关掉」分不开。
  expect(screen.getByText("面板内容")).toBeInTheDocument();
});


test("a conflicting core primary closes the extension dialog and releases the holder", async () => {
  const user = userEvent.setup();
  render(<Outlet ownerKey="user-a:notebook-a:1" />);
  enterWorkspace();
  await user.click(screen.getByRole("button", { name: "打开面板" }));

  act(() => { coordinator!.open("notebook-editor", coordinator!.captureActorOwner()); });
  expect(screen.queryByRole("dialog", { name: "示例面板" })).toBeNull();
  expect(coordinator!.view("extension").open).toBe(false);
  // 持有者随 lease 一起释放：同一个插件立刻还能重新认领（holder 卡住的话，
  // `handleRootModalClosed` 那条 close sink 就没有把两者绑在一起）。
  act(() => { coordinator!.requestClose("notebook-editor", "button"); });
  await user.click(screen.getByRole("button", { name: "打开面板" }));
  expect(screen.getByRole("dialog", { name: "示例面板" })).toBeInTheDocument();
});


test("only the holder sees an open dialog: a second plugin gets a closed view", async () => {
  const user = userEvent.setup();
  render(<Outlet ownerKey="user-a:notebook-a:1" />);
  enterWorkspace();
  await user.click(screen.getByRole("button", { name: "打开面板" }));
  expect(screen.getByRole("dialog", { name: "示例面板" })).toBeInTheDocument();
  // 另一个插件同时在场、同一格 lease 开着——它拿到的必须是一份 closed view。
  expect(screen.queryByRole("dialog", { name: "另一个面板" })).toBeNull();
  expect(lastPluginActions.get("other-panel")).toBeDefined();

  // 换它认领：那一格易主，上一个插件的弹窗随之消失。
  await user.click(screen.getByRole("button", { name: "打开另一个面板" }));
  expect(screen.getByRole("dialog", { name: "另一个面板" })).toBeInTheDocument();
  expect(screen.queryByRole("dialog", { name: "示例面板" })).toBeNull();
});


test("two contributions of the same plugin do not both open: the holder is a contribution, not a plugin", async () => {
  // codex #578 R1 P2：按 plugin id 判持有权时这两条 contribution 会同时看到
  // `dialog.open === true`，屏幕上一次挂出两个弹窗。
  const user = userEvent.setup();
  render(<Outlet ownerKey="user-a:notebook-a:1" />);
  enterWorkspace();
  await user.click(screen.getByRole("button", { name: "打开面板" }));
  expect(screen.getByRole("dialog", { name: "示例面板" })).toBeInTheDocument();
  expect(screen.queryByRole("dialog", { name: "同插件第二面板" })).toBeNull();
  expect(document.querySelectorAll(".utility-modal")).toHaveLength(1);

  // 同插件的另一条来认领：易主，前一条随之关闭——仍然只有一个弹窗。
  await user.click(screen.getByRole("button", { name: "打开同插件第二面板" }));
  expect(screen.getByRole("dialog", { name: "同插件第二面板" })).toBeInTheDocument();
  expect(screen.queryByRole("dialog", { name: "示例面板" })).toBeNull();
  expect(document.querySelectorAll(".utility-modal")).toHaveLength(1);

  // 同插件的旧回调同样关不掉现任持有者的弹窗——闸判的是 contribution，不是 plugin。
  act(() => { lastPluginActions.get("sample-panel")!.closeDialog(); });
  expect(screen.getByRole("dialog", { name: "同插件第二面板" })).toBeInTheDocument();
});


test("a stale closeDialog from a plugin that no longer holds the slot cannot close someone else's dialog", async () => {
  // codex #578 R1 P2：owner 闸只挡得住「换代之后的旧回调」，挡不住「同一代里那一格已经
  // 易主」。插件 A 留着的那份 `closeDialog()` 必须对 B 的弹窗无效。
  const user = userEvent.setup();
  render(<Outlet ownerKey="user-a:notebook-a:1" />);
  enterWorkspace();
  await user.click(screen.getByRole("button", { name: "打开面板" }));
  const staleClose = lastPluginActions.get("sample-panel")!.closeDialog;

  await user.click(screen.getByRole("button", { name: "打开另一个面板" }));
  expect(screen.getByRole("dialog", { name: "另一个面板" })).toBeInTheDocument();

  act(() => { staleClose(); });
  expect(screen.getByRole("dialog", { name: "另一个面板" })).toBeInTheDocument();
  expect(coordinator!.view("extension").open).toBe(true);

  // 反向保护：当前持有者自己关，必须真的关得掉（否则上面那条在"谁都关不掉"时也绿）。
  act(() => { lastPluginActions.get("other-panel")!.closeDialog(); });
  expect(screen.queryByRole("dialog", { name: "另一个面板" })).toBeNull();
  expect(coordinator!.view("extension").open).toBe(false);
});


test("the close button releases both the dialog and the coordinator slot", async () => {
  const user = userEvent.setup();
  render(<Outlet ownerKey="user-a:notebook-a:1" />);
  enterWorkspace();
  await user.click(screen.getByRole("button", { name: "打开面板" }));
  await user.click(screen.getByRole("button", { name: "关闭" }));
  expect(screen.queryByRole("dialog", { name: "示例面板" })).toBeNull();
  expect(coordinator!.view("extension").open).toBe(false);
});


test("the modal's lifetime is owned by the coordinator and by the outlet's ownerKey gate", async () => {
  const user = userEvent.setup();
  // ① owner 消失（离开工作区/登出）——整棵子树卸载，弹窗跟着走。
  const view = render(<Outlet ownerKey="user-a:notebook-a:1" />);
  enterWorkspace();
  await user.click(screen.getByRole("button", { name: "打开面板" }));
  expect(screen.getByRole("dialog", { name: "示例面板" })).toBeInTheDocument();

  view.rerender(<Outlet ownerKey={null} />);
  expect(screen.queryByRole("dialog", { name: "示例面板" })).toBeNull();
  expect(view.container).toBeEmptyDOMElement();

  // ② 切库——协调器同步撤销 workspace 拥有的那格 lease。这是切库的真实路径：不靠它，
  // 用户会在新笔记本里看到一个仍开着的、装着上一本库数据的插件弹窗。outlet 的
  // ownerKey 门是同向的第二道兜底（contribution 的 React key 含 ownerKey）。
  view.rerender(<Outlet ownerKey="user-a:notebook-a:1" />);
  await user.click(screen.getByRole("button", { name: "打开面板" }));
  expect(screen.getByRole("dialog", { name: "示例面板" })).toBeInTheDocument();

  act(() => { coordinator!.beginWorkspaceTransition(); });
  expect(screen.queryByRole("dialog", { name: "示例面板" })).toBeNull();
  view.rerender(<Outlet ownerKey="user-a:notebook-b:2" />);
  expect(screen.queryByRole("dialog", { name: "示例面板" })).toBeNull();
  expect(screen.getByRole("button", { name: "打开面板" })).toBeInTheDocument();
});
