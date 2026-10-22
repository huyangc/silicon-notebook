import { act, cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import {
  ROOT_MODAL_POLICIES,
  useRootModalCoordinator,
  type RootModalCloseReason,
  type RootModalSlot,
} from "../../app/use-root-modal-coordinator";

type HookValue = ReturnType<typeof useRootModalCoordinator>;

const closed = vi.fn<(slot: RootModalSlot, reason: RootModalCloseReason) => void>();
let value: HookValue | null = null;

function Harness({
  actorId = "user-a",
  sourceId = null,
}: {
  actorId?: string | null;
  sourceId?: string | null;
}) {
  value = useRootModalCoordinator({ actorId, sourceId, onClosed: closed });
  const analytics = value.view("analytics");
  const info = value.view("info");
  return <div>
    {analytics.open ? (
      <section
        data-testid="analytics-surface"
        inert={analytics.topmost ? undefined : true}
        aria-hidden={!analytics.topmost}
      >
        <span data-testid="analytics-action-owner">
          <button data-testid="analytics-action">analytics</button>
        </span>
      </section>
    ) : null}
    {info.open ? <section data-testid="info-surface">info</section> : null}
  </div>;
}

function enterWorkspace(notebookId = "notebook-a", workspaceEpoch = 1) {
  const transition = value!.beginWorkspaceTransition();
  act(() => {
    value!.finishWorkspaceTransition(transition, {
      actorId: "user-a",
      notebookId,
      workspaceEpoch,
    });
  });
}

beforeEach(() => {
  value = null;
  vi.clearAllMocks();
});

afterEach(() => {
  vi.useRealTimers();
  cleanup();
});

test("workspace transition synchronously hides slots and an A/B/A lease never revives", () => {
  render(<Harness />);
  enterWorkspace("notebook-a", 1);
  const ownerA1 = value!.captureWorkspaceOwner();
  const leaseA1 = value!.open("notebook-share", ownerA1);
  expect(leaseA1).not.toBeNull();
  expect(value!.view("notebook-share").open).toBe(true);

  const toB = value!.beginWorkspaceTransition();
  expect(value!.view("notebook-share").open).toBe(false);
  expect(closed).toHaveBeenCalledWith("notebook-share", "owner-invalidated");
  act(() => value!.finishWorkspaceTransition(toB, {
    actorId: "user-a",
    notebookId: "notebook-b",
    workspaceEpoch: 2,
  }));
  const toAAgain = value!.beginWorkspaceTransition();
  act(() => value!.finishWorkspaceTransition(toAAgain, {
    actorId: "user-a",
    notebookId: "notebook-a",
    workspaceEpoch: 3,
  }));
  expect(value!.publish(leaseA1)).toBe(false);
  expect(value!.view("notebook-share").open).toBe(false);
});

test("a failed workspace transition leaves authority suspended and never revives the old slot", () => {
  render(<Harness />);
  enterWorkspace("notebook-a", 1);
  const old = value!.open("source-add", value!.captureWorkspaceOwner());
  const transition = value!.beginWorkspaceTransition();
  act(() => value!.finishWorkspaceTransition(transition, null));
  expect(value!.captureWorkspaceOwner()).toBeNull();
  expect(value!.publish(old)).toBe(false);
  expect(value!.open("source-add", value!.captureWorkspaceOwner())).toBeNull();
});

test("actor replacement synchronously hides actor and workspace slots", () => {
  const screen = render(<Harness />);
  enterWorkspace();
  const actor = value!.captureActorOwner();
  const workspace = value!.captureWorkspaceOwner();
  act(() => {
    value!.open("model-service", actor);
    value!.open("info", workspace);
  });
  expect(value!.view("model-service").open).toBe(true);
  expect(value!.view("info").open).toBe(true);

  screen.rerender(<Harness actorId="user-b" />);
  expect(value!.view("model-service").open).toBe(false);
  expect(value!.view("info").open).toBe(false);
});

test("a deferred opener publishes only for its frozen owner and latest issue", () => {
  render(<Harness />);
  enterWorkspace();
  const owner = value!.captureWorkspaceOwner();
  const older = value!.issue("notebook-share", owner);
  const newer = value!.issue("notebook-share", owner);
  expect(value!.publish(older)).toBe(false);
  expect(value!.publish(newer)).toBe(true);

  const transition = value!.beginWorkspaceTransition();
  act(() => value!.finishWorkspaceTransition(transition, {
    actorId: "user-a",
    notebookId: "notebook-b",
    workspaceEpoch: 2,
  }));
  expect(value!.publish(newer)).toBe(false);
});

test("a deferred primary opener cannot replace a newer primary slot", () => {
  render(<Harness />);
  enterWorkspace();
  const actor = value!.captureActorOwner();
  const deferredSearch = value!.issue("search-profile", actor);
  const password = value!.open("password-change", actor);
  expect(password).not.toBeNull();
  expect(value!.view("password-change").open).toBe(true);

  expect(value!.publish(deferredSearch)).toBe(false);
  expect(value!.view("search-profile").open).toBe(false);
  expect(value!.view("password-change").open).toBe(true);
  expect(closed).not.toHaveBeenCalledWith("password-change", "conflict");
});

test("collection and KG presentation slots participate in the same primary watermark", () => {
  render(<Harness />);
  enterWorkspace();
  const actor = value!.captureActorOwner();
  const workspace = value!.captureWorkspaceOwner();
  const deferredAnalytics = value!.issue("analytics", workspace);
  expect(value!.open("notebook-editor", actor)).not.toBeNull();
  expect(value!.publish(deferredAnalytics)).toBe(false);
  expect(value!.view("notebook-editor").open).toBe(true);

  expect(value!.open("kg-schema", workspace)).not.toBeNull();
  expect(value!.view("notebook-editor").open).toBe(false);
  expect(value!.view("kg-schema").open).toBe(true);
  expect(value!.open("kg-analysis", workspace)).not.toBeNull();
  expect(value!.view("kg-schema").open).toBe(false);
  expect(value!.view("kg-analysis").open).toBe(true);
});

test("answer image preview is an Escape-closable workspace primary", () => {
  render(<Harness />);
  enterWorkspace();
  const actor = value!.captureActorOwner();
  const workspace = value!.captureWorkspaceOwner();

  expect(value!.open("answer-image-preview", actor)).toBeNull();
  expect(value!.open("answer-image-preview", workspace)).not.toBeNull();
  expect(value!.view("answer-image-preview").topmost).toBe(true);
  expect(value!.requestClose("answer-image-preview", "escape")).toBe(true);
  expect(closed).toHaveBeenCalledWith("answer-image-preview", "escape");

  expect(value!.open("answer-image-preview", workspace)).not.toBeNull();
  value!.beginWorkspaceTransition();
  expect(value!.view("answer-image-preview").open).toBe(false);
  expect(closed).toHaveBeenCalledWith("answer-image-preview", "owner-invalidated");
});

// codex #578 R1 P2 — 部署插件的弹窗接进来的那**一个通用格子**。它没有自己的 policy
// 分档：与其它 primary 互斥、与 `info` 共存、workspace 拥有、切库同步撤销。这里钉的是
// 它真的落在既有那套规则里，而不是被悄悄放宽成一个不参与冲突/层级的旁路。
test("the generic extension slot is an ordinary workspace primary that coexists only with info", () => {
  render(<Harness />);
  enterWorkspace();
  const workspace = value!.captureWorkspaceOwner();
  const actor = value!.captureActorOwner();

  // actor 拥有的 slot 不能拿它开：插件弹窗只在有 workspace 时存在。
  expect(value!.open("extension", actor)).toBeNull();
  expect(value!.open("extension", workspace)).not.toBeNull();
  expect(value!.view("extension").open).toBe(true);
  expect(value!.view("extension").topmost).toBe(true);

  // info 合法压在它之上：仍开着，但退出 topmost（消费方据此加 inert/aria-hidden）。
  expect(value!.open("info", workspace)).not.toBeNull();
  expect(value!.view("extension").open).toBe(true);
  expect(value!.view("extension").topmost).toBe(false);
  expect(value!.view("extension").zIndex).toBeLessThan(value!.view("info").zIndex);

  // 另一个 primary 则把它冲突关掉——反过来也一样。
  expect(value!.open("understanding", workspace)).not.toBeNull();
  expect(value!.view("extension").open).toBe(false);
  expect(closed).toHaveBeenCalledWith("extension", "conflict");
  expect(value!.open("extension", workspace)).not.toBeNull();
  expect(value!.view("understanding").open).toBe(false);

  // 切库同步撤销。
  value!.beginWorkspaceTransition();
  expect(value!.view("extension").open).toBe(false);
  expect(closed).toHaveBeenCalledWith("extension", "owner-invalidated");
});

test("source detail invalidates older primary opens while its catalog review remains a legal overlay", () => {
  const screen = render(<Harness />);
  enterWorkspace();
  const workspace = value!.captureWorkspaceOwner();
  const deferredAnalytics = value!.issue("analytics", workspace);
  expect(value!.open("source-detail", workspace)).not.toBeNull();
  expect(value!.publish(deferredAnalytics)).toBe(false);
  expect(value!.view("source-detail").open).toBe(true);

  screen.rerender(<Harness sourceId="source-a" />);
  expect(value!.open("catalog-review", value!.captureSourceOwner())).not.toBeNull();
  expect(value!.view("source-detail").open).toBe(true);
  expect(value!.view("source-detail").topmost).toBe(false);
  expect(value!.view("catalog-review").topmost).toBe(true);
});

// 图谱分析的问题清单「打开来源核对」:来源详情叠在分析面板上层打开,面板不关;来源详情里
// 再开命令目录审阅,面板仍在;逐层关掉之后焦点回到面板里那颗按钮。面板全程不卸载,所以
// 滚动位置原样保留。其它 primary 照旧把分析面板冲突关掉——共存只给这几对。
function KgAnalysisHarness({ sourceId = null }: { sourceId?: string | null }) {
  value = useRootModalCoordinator({ actorId: "user-a", sourceId, onClosed: closed });
  const analysis = value.view("kg-analysis");
  const detail = value.view("source-detail");
  return <div>
    {analysis.open ? (
      <section data-testid="kg-analysis-surface" inert={analysis.topmost ? undefined : true} aria-hidden={!analysis.topmost}>
        <button data-testid="open-source-for-checking">打开来源核对</button>
      </section>
    ) : null}
    {detail.open ? <section data-testid="source-detail-surface">来源</section> : null}
  </div>;
}

test("source detail and its catalog review coexist with the KG analysis panel; closing them returns focus to the opener", () => {
  const screen = render(<KgAnalysisHarness />);
  enterWorkspace();
  const workspace = value!.captureWorkspaceOwner();
  act(() => { value!.open("kg-analysis", workspace); });
  const opener = screen.getByTestId("open-source-for-checking");
  opener.focus();

  // openSourceDetailById 的形状:先 issue(此刻记下焦点),异步取到详情后再 publish。
  const lease = value!.issue("source-detail", value!.captureWorkspaceOwner());
  act(() => { expect(value!.publish(lease)).toBe(true); });
  screen.rerender(<KgAnalysisHarness sourceId="source-a" />);
  expect(value!.view("kg-analysis").open).toBe(true);
  expect(value!.view("kg-analysis").topmost).toBe(false);
  expect(value!.view("source-detail").topmost).toBe(true);
  expect(value!.view("source-detail").zIndex).toBeGreaterThan(value!.view("kg-analysis").zIndex);
  expect(screen.getByTestId("kg-analysis-surface")).toHaveAttribute("inert");

  act(() => { expect(value!.open("catalog-review", value!.captureSourceOwner())).not.toBeNull(); });
  expect(value!.view("kg-analysis").open).toBe(true);
  expect(value!.view("source-detail").open).toBe(true);
  expect(value!.view("catalog-review").topmost).toBe(true);
  expect(closed).not.toHaveBeenCalledWith("kg-analysis", "conflict");

  act(() => { expect(value!.requestClose("catalog-review", "button")).toBe(true); });
  act(() => { expect(value!.requestClose("source-detail", "button")).toBe(true); });
  expect(value!.view("kg-analysis").topmost).toBe(true);
  expect(screen.getByTestId("kg-analysis-surface")).not.toHaveAttribute("inert");
  expect(document.activeElement).toBe(screen.getByTestId("open-source-for-checking"));
});

test("the coexistence pairs are narrow and directed: other primaries still evict the panel, and opening the panel over a source detail evicts the detail", () => {
  const screen = render(<KgAnalysisHarness />);
  enterWorkspace();
  const workspace = value!.captureWorkspaceOwner();
  // 共存只给「面板已开、再往下钻」这一个方向;其余每个 primary 照旧把面板关掉。
  const others = (Object.keys(ROOT_MODAL_POLICIES) as RootModalSlot[]).filter((slot) => (
    ROOT_MODAL_POLICIES[slot].conflictGroup === "primary"
    && !["kg-analysis", "source-detail", "catalog-review"].includes(slot)
  ));
  expect(others.length).toBeGreaterThan(5);
  for (const slot of others) {
    act(() => { value!.open("kg-analysis", workspace); });
    const owner = ROOT_MODAL_POLICIES[slot].ownerKinds.includes("workspace")
      ? workspace
      : value!.captureActorOwner();
    act(() => { value!.open(slot, owner); });
    expect(value!.view("kg-analysis").open, slot).toBe(false);
    act(() => { value!.requestClose(slot, "button"); });
  }

  // 反方向:来源详情已开(例如从知识图谱的出处列表打开),再开分析面板——面板画在知识图谱
  // 视图的层叠上下文里,低于任何根层弹窗;两者若共存,视觉上来源详情压着面板,协调器却
  // 让面板成为最上层、把来源详情设成 inert。所以这个方向照旧冲突关闭。
  act(() => { value!.open("source-detail", workspace); });
  screen.rerender(<KgAnalysisHarness sourceId="source-a" />);
  act(() => { value!.open("kg-analysis", workspace); });
  expect(value!.view("kg-analysis").open).toBe(true);
  expect(value!.view("source-detail").open).toBe(false);
  expect(closed).toHaveBeenCalledWith("source-detail", "conflict");
  act(() => { value!.open("catalog-review", value!.captureSourceOwner()); });
  act(() => { value!.requestClose("kg-analysis", "button"); });
  act(() => { value!.open("kg-analysis", workspace); });
  expect(value!.view("catalog-review").open).toBe(false);
});

test("workspace navigation closes collection dialogs but keeps actor-global settings", () => {
  render(<Harness />);
  enterWorkspace();
  const actor = value!.captureActorOwner();
  expect(value!.open("notebook-editor", actor)).not.toBeNull();
  value!.beginWorkspaceTransition();
  expect(value!.view("notebook-editor").open).toBe(false);
  expect(closed).toHaveBeenCalledWith("notebook-editor", "owner-invalidated");

  expect(value!.open("password-change", actor)).not.toBeNull();
  value!.beginWorkspaceTransition();
  expect(value!.view("password-change").open).toBe(true);
});

test("a deferred collection ticket cannot publish after a workspace transition", () => {
  render(<Harness />);
  enterWorkspace("notebook-a", 1);
  const actor = value!.captureActorOwner();
  const editor = value!.issue("notebook-editor", actor);
  const deletion = value!.issue("notebook-delete", actor);
  const transition = value!.beginWorkspaceTransition();
  act(() => value!.finishWorkspaceTransition(transition, {
    actorId: "user-a",
    notebookId: "notebook-b",
    workspaceEpoch: 2,
  }));
  expect(value!.publish(editor)).toBe(false);
  expect(value!.publish(deletion)).toBe(false);
});

test("primary slots conflict while the info layer remains a legal child overlay", () => {
  render(<Harness />);
  enterWorkspace();
  const actor = value!.captureActorOwner();
  const workspace = value!.captureWorkspaceOwner();
  act(() => value!.open("model-service", actor));
  expect(value!.view("model-service").topmost).toBe(true);

  act(() => value!.open("info", workspace));
  expect(value!.view("model-service").open).toBe(true);
  expect(value!.view("model-service").topmost).toBe(false);
  expect(value!.view("info").topmost).toBe(true);

  act(() => value!.open("analytics", workspace));
  expect(value!.view("model-service").open).toBe(false);
  expect(value!.view("analytics").open).toBe(true);
  expect(value!.view("info").open).toBe(true);
  expect(closed).toHaveBeenCalledWith("model-service", "conflict");
});

test("opening the same slot for the same owner is idempotent and never runs domain cleanup", () => {
  render(<Harness />);
  enterWorkspace();
  const owner = value!.captureWorkspaceOwner();
  const first = value!.open("source-add", owner);
  const second = value!.open("source-add", owner);
  expect(second).toEqual(first);
  expect(value!.view("source-add").open).toBe(true);
  expect(closed).not.toHaveBeenCalled();
});

test("only the topmost slot accepts Escape and backdrop follows the frozen policy", () => {
  render(<Harness />);
  enterWorkspace();
  const actor = value!.captureActorOwner();
  const workspace = value!.captureWorkspaceOwner();
  act(() => value!.open("model-service", actor));
  act(() => value!.open("info", workspace));
  expect(value!.requestClose("model-service", "escape")).toBe(false);
  expect(value!.requestClose("info", "escape")).toBe(false);
  expect(value!.requestClose("info", "button")).toBe(true);
  expect(value!.requestClose("model-service", "escape")).toBe(true);

  act(() => value!.open("source-add", workspace));
  expect(value!.requestClose("source-add", "backdrop")).toBe(true);
  act(() => value!.open("password-change", actor));
  expect(value!.requestClose("password-change", "backdrop")).toBe(false);
});

test("source generation rejects a catalog review after source A/B/A", () => {
  const screen = render(<Harness sourceId="source-a" />);
  enterWorkspace();
  const old = value!.issue("catalog-review", value!.captureSourceOwner());
  screen.rerender(<Harness sourceId="source-b" />);
  screen.rerender(<Harness sourceId="source-a" />);
  expect(value!.publish(old)).toBe(false);
});

test("focus returns only for a user close of the topmost connected opener", () => {
  render(<Harness />);
  enterWorkspace();
  const opener = document.createElement("button");
  document.body.appendChild(opener);
  opener.focus();
  const workspace = value!.captureWorkspaceOwner();
  act(() => value!.open("analytics", workspace));
  const inside = document.createElement("button");
  document.body.appendChild(inside);
  inside.focus();
  act(() => value!.open("info", workspace));
  act(() => {
    expect(value!.requestClose("info", "button")).toBe(true);
  });
  expect(document.activeElement).toBe(inside);
  inside.remove();
  act(() => {
    expect(value!.requestClose("analytics", "button")).toBe(true);
  });
  expect(document.activeElement).toBe(opener);
  opener.remove();
});

test("overlay focus return waits until the underlying primary is no longer inert", () => {
  const screen = render(<Harness />);
  enterWorkspace();
  const workspace = value!.captureWorkspaceOwner();
  act(() => value!.open("analytics", workspace));
  const analyticsAction = screen.getByTestId("analytics-action");
  analyticsAction.focus();
  act(() => value!.open("info", workspace));
  expect(screen.getByTestId("analytics-surface")).toHaveAttribute("inert");

  const observedInertAncestors: Array<Element | null> = [];
  const nativeFocus = analyticsAction.focus.bind(analyticsAction);
  const focus = vi.spyOn(analyticsAction, "focus").mockImplementation(() => {
    observedInertAncestors.push(analyticsAction.closest("[inert]"));
    nativeFocus();
  });
  act(() => {
    expect(value!.requestClose("info", "button")).toBe(true);
  });
  expect(focus).toHaveBeenCalledTimes(1);
  expect(observedInertAncestors).toEqual([null]);
  expect(document.activeElement).toBe(analyticsAction);
});

test("focus return is dropped when a newer overlay becomes topmost in the same commit", () => {
  render(<Harness />);
  enterWorkspace();
  const workspace = value!.captureWorkspaceOwner();
  const opener = document.createElement("button");
  document.body.appendChild(opener);
  opener.focus();
  act(() => value!.open("analytics", workspace));
  act(() => value!.open("info", workspace));
  const focus = vi.spyOn(opener, "focus");

  act(() => {
    expect(value!.requestClose("info", "button")).toBe(true);
    expect(value!.open("info", workspace)).not.toBeNull();
  });
  expect(value!.view("info").topmost).toBe(true);
  expect(focus).not.toHaveBeenCalled();
  opener.remove();
});

test("focus return is dropped while the opener remains inside any inert ancestor", () => {
  const screen = render(<Harness />);
  enterWorkspace();
  const workspace = value!.captureWorkspaceOwner();
  act(() => value!.open("analytics", workspace));
  const analyticsAction = screen.getByTestId("analytics-action");
  analyticsAction.focus();
  act(() => value!.open("info", workspace));
  const owner = screen.getByTestId("analytics-action-owner");
  owner.setAttribute("inert", "");
  const focus = vi.spyOn(analyticsAction, "focus");

  act(() => {
    expect(value!.requestClose("info", "button")).toBe(true);
  });
  expect(value!.view("analytics").topmost).toBe(true);
  expect(owner).toHaveAttribute("inert");
  expect(focus).not.toHaveBeenCalled();
});

test("the coordinator creates no timer or I/O work", () => {
  vi.useFakeTimers();
  render(<Harness />);
  enterWorkspace();
  act(() => {
    value!.open("model-service", value!.captureActorOwner());
    value!.open("info", value!.captureWorkspaceOwner());
    value!.requestClose("info", "button");
    value!.leaveWorkspace();
  });
  expect(vi.getTimerCount()).toBe(0);
});

test("release-notes is an actor-owned announcement that never evicts a primary modal and cannot be closed by backdrop/escape", () => {
  render(<Harness />);
  const actor = value!.captureActorOwner();
  expect(value!.open("password-change", actor)).not.toBeNull();
  expect(value!.open("release-notes", actor)).not.toBeNull();
  expect(value!.view("password-change").open).toBe(true);
  expect(value!.view("release-notes").open).toBe(true);
  expect(value!.view("release-notes").topmost).toBe(true);
  expect(value!.view("password-change").topmost).toBe(false);
  expect(closed).not.toHaveBeenCalledWith("password-change", "conflict");
  expect(value!.requestClose("release-notes", "backdrop")).toBe(false);
  expect(value!.requestClose("release-notes", "escape")).toBe(false);
  expect(value!.requestClose("release-notes", "button")).toBe(true);
  expect(closed).toHaveBeenCalledWith("release-notes", "button");
  expect(value!.view("password-change").topmost).toBe(true);
});

// --- 子层（stacksOver / openAbove）------------------------------------------------
//
// 全局问答窗口里的引用配图要叠在窗口**之上**：同一格 answer-image-preview，但不按
// primary 冲突组把窗口顶掉，Escape / 遮罩先归它（requestClose 既有的「只有最上层接
// Escape」那条规则），窗口离场时它一起撤下。笔记本内那条入口不经子层，口径不变。

test("a stacked image preview sits above the global window and takes the first Escape", () => {
  render(<Harness />);
  const actor = value!.captureActorOwner();
  act(() => { value!.open("global-ask", actor); });
  let lease: ReturnType<HookValue["openAbove"]> = null;
  act(() => { lease = value!.openAbove("answer-image-preview", "global-ask"); });
  expect(lease).not.toBeNull();
  // 子层继承父层那一次打开的 owner（actor），不经本格 ownerKinds。
  expect(lease!.owner).toBe(actor);
  expect(lease!.parent).toEqual({ slot: "global-ask", issue: value!.activeLease("global-ask")!.issue });
  expect(closed).not.toHaveBeenCalled();
  expect(value!.view("global-ask").open).toBe(true);
  expect(value!.view("global-ask").topmost).toBe(false);
  expect(value!.view("answer-image-preview").topmost).toBe(true);
  expect(value!.view("answer-image-preview").zIndex).toBeGreaterThan(value!.view("global-ask").zIndex);

  // 第一次 Escape：窗口不是最上层，它的请求被拒；预览收下。
  expect(value!.requestClose("global-ask", "escape")).toBe(false);
  act(() => { expect(value!.requestClose("answer-image-preview", "escape")).toBe(true); });
  expect(closed).toHaveBeenCalledWith("answer-image-preview", "escape");
  expect(value!.view("global-ask").open).toBe(true);
  expect(value!.view("global-ask").topmost).toBe(true);
  // 第二次 Escape 才轮到窗口。
  act(() => { expect(value!.requestClose("global-ask", "escape")).toBe(true); });
  expect(closed).toHaveBeenLastCalledWith("global-ask", "escape");
});

test("backdrop and the close button close only the stacked preview", () => {
  render(<Harness />);
  act(() => { value!.open("global-ask", value!.captureActorOwner()); });
  act(() => { value!.openAbove("answer-image-preview", "global-ask"); });
  act(() => { expect(value!.requestClose("answer-image-preview", "backdrop")).toBe(true); });
  expect(value!.view("global-ask").topmost).toBe(true);
  act(() => { value!.openAbove("answer-image-preview", "global-ask"); });
  act(() => { expect(value!.requestClose("answer-image-preview", "button")).toBe(true); });
  expect(value!.view("global-ask").open).toBe(true);
  expect(closed.mock.calls.filter(([slot]) => slot === "global-ask")).toEqual([]);
});

test("openAbove refuses undeclared pairs, an absent parent and a covered parent", () => {
  render(<Harness />);
  enterWorkspace();
  const actor = value!.captureActorOwner();
  // 父层不在场。
  expect(value!.openAbove("answer-image-preview", "global-ask")).toBeNull();
  // 策略表没有声明的叠放关系。
  act(() => { value!.open("model-service", actor); });
  expect(value!.openAbove("answer-image-preview", "model-service")).toBeNull();
  act(() => { value!.open("global-ask", actor); });
  expect(value!.openAbove("info", "global-ask")).toBeNull();
  // 父层被更高的层盖住：用户本来点不到它，不许从它下面再叠一层。
  act(() => { value!.open("info", actor); });
  expect(value!.openAbove("answer-image-preview", "global-ask")).toBeNull();
  expect(value!.view("answer-image-preview").open).toBe(false);
  // 顶层打开的口径不变：actor owner 仍被 ownerKinds 拒绝。
  expect(value!.open("answer-image-preview", actor)).toBeNull();
});

test("the stacked preview leaves with its parent, whatever removes the parent", () => {
  const screen = render(<Harness />);
  const actor = () => value!.captureActorOwner();
  // ① 父层被按钮关掉。
  act(() => { value!.open("global-ask", actor()); });
  act(() => { value!.openAbove("answer-image-preview", "global-ask"); });
  act(() => { value!.requestClose("global-ask", "button"); });
  expect(value!.view("answer-image-preview").open).toBe(false);
  expect(closed).toHaveBeenCalledWith("answer-image-preview", "owner-invalidated");
  // ② 父层被另一个 primary 顶掉。
  closed.mockClear();
  act(() => { value!.open("global-ask", actor()); });
  act(() => { value!.openAbove("answer-image-preview", "global-ask"); });
  act(() => { value!.open("model-service", actor()); });
  expect(value!.view("global-ask").open).toBe(false);
  expect(value!.view("answer-image-preview").open).toBe(false);
  expect(closed).toHaveBeenCalledWith("global-ask", "conflict");
  expect(closed.mock.calls.some(([slot]) => slot === "answer-image-preview")).toBe(true);
  act(() => { value!.requestClose("model-service", "button"); });
  // ③ 换了用户：两层一起失效，旧子层租约不能复活。
  act(() => { value!.open("global-ask", actor()); });
  let stale: ReturnType<HookValue["openAbove"]> = null;
  act(() => { stale = value!.openAbove("answer-image-preview", "global-ask"); });
  screen.rerender(<Harness actorId="user-b" />);
  expect(value!.view("answer-image-preview").open).toBe(false);
  expect(value!.leaseIsCurrent(stale)).toBe(false);
  expect(value!.publish(stale)).toBe(false);
});

test("a stacked lease is bound to that one opening of its parent", () => {
  render(<Harness />);
  act(() => { value!.open("global-ask", value!.captureActorOwner()); });
  expect(value!.issue("answer-image-preview", value!.captureActorOwner())).toBeNull();
  let stacked: ReturnType<HookValue["openAbove"]> = null;
  act(() => { stacked = value!.openAbove("answer-image-preview", "global-ask"); });
  act(() => { value!.requestClose("answer-image-preview", "button"); });
  act(() => { value!.requestClose("global-ask", "button"); });
  act(() => { value!.open("global-ask", value!.captureActorOwner()); });
  // 窗口重开是另一次打开：上一次的子层租约不能发布到新窗口之上。
  expect(value!.publish(stacked)).toBe(false);
  expect(value!.view("answer-image-preview").open).toBe(false);
});

test("bringing the parent to front keeps its stacked child on top", () => {
  render(<Harness />);
  const actor = value!.captureActorOwner();
  act(() => { value!.open("global-ask", actor); });
  act(() => { value!.openAbove("answer-image-preview", "global-ask"); });
  act(() => { value!.open("release-notes", actor); });
  expect(value!.view("release-notes").topmost).toBe(true);
  act(() => { value!.bringToFront("global-ask"); });
  expect(value!.view("answer-image-preview").topmost).toBe(true);
  expect(value!.view("answer-image-preview").zIndex).toBeGreaterThan(value!.view("global-ask").zIndex);
  expect(value!.view("global-ask").zIndex).toBeGreaterThan(value!.view("release-notes").zIndex);
});

test("an in-notebook image preview is still an ordinary primary that replaces the global window", () => {
  render(<Harness />);
  enterWorkspace();
  act(() => { value!.open("global-ask", value!.captureActorOwner()); });
  act(() => { value!.open("answer-image-preview", value!.captureWorkspaceOwner()); });
  expect(value!.view("global-ask").open).toBe(false);
  expect(closed).toHaveBeenCalledWith("global-ask", "conflict");
  expect(value!.activeLease("answer-image-preview")!.parent).toBeNull();
});

test("closing the stacked preview returns focus to its opener once the window is interactive again", () => {
  function StackHarness() {
    value = useRootModalCoordinator({ actorId: "user-a", sourceId: null, onClosed: closed });
    const chat = value.view("global-ask");
    return chat.open ? <section data-testid="window" inert={chat.topmost ? undefined : true}>
      <button data-testid="opener">图</button>
    </section> : null;
  }
  const screen = render(<StackHarness />);
  act(() => { value!.open("global-ask", value!.captureActorOwner()); });
  const opener = screen.getByTestId("opener");
  opener.focus();
  act(() => { value!.openAbove("answer-image-preview", "global-ask"); });
  expect(screen.getByTestId("window")).toHaveAttribute("inert");
  const focus = vi.spyOn(opener, "focus");
  act(() => { value!.requestClose("answer-image-preview", "escape"); });
  expect(screen.getByTestId("window")).not.toHaveAttribute("inert");
  expect(focus).toHaveBeenCalledTimes(1);
  expect(document.activeElement).toBe(opener);
});
