import { act, cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import {
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
