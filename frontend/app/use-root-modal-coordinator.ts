"use client";

import { useEffect, useLayoutEffect, useRef, useState } from "react";

export type RootModalSlot =
  | "global-ask"
  | "password-change"
  | "release-notes"
  | "search-profile"
  | "notebook-editor"
  | "notebook-delete"
  | "notebook-export"
  | "notebook-import"
  | "source-add"
  | "source-detail"
  | "info"
  | "model-service"
  | "notebook-share"
  | "shared-preview"
  | "shared-by-me"
  | "memory-save"
  | "catalog-review"
  | "conversation-share"
  | "analytics"
  | "kg-schema"
  | "kg-analysis"
  | "understanding"
  | "promotion-queue"
  | "promotion-target"
  | "edge-review"
  | "answer-image-preview"
  // 部署插件的弹窗。**一个通用格子，不是"每个插件一个 slot"**：这条联合类型里不许
  // 出现任何插件名——插件是部署时装进来的（`SILICON_NOTEBOOK_UI_PLUGINS`），公网仓库
  // 的核心类型认不得它们，写进来就等于要求每装一个插件改一次基座（"零补丁"红线）。
  // 代价是一次只允许一个插件弹窗：持有者由 `page.tsx` 的 holder state 记着，host 按
  // `contribution.pluginId` 过滤，非持有者看到的是一份 closed view。
  | "extension";

export type RootModalCloseReason =
  | "button"
  | "backdrop"
  | "escape"
  | "owner-invalidated"
  | "conflict";

export type ActorModalOwner = Readonly<{
  kind: "actor";
  actorId: string;
  actorGeneration: number;
}>;

export type WorkspaceModalOwner = Readonly<{
  kind: "workspace";
  actorId: string;
  actorGeneration: number;
  notebookId: string;
  workspaceEpoch: number;
  workspaceGeneration: number;
}>;

export type SourceModalOwner = Readonly<{
  kind: "source";
  actorId: string;
  actorGeneration: number;
  notebookId: string;
  workspaceEpoch: number;
  workspaceGeneration: number;
  sourceId: string;
  sourceGeneration: number;
}>;

export type RootModalOwner = ActorModalOwner | WorkspaceModalOwner | SourceModalOwner;

/** 子层租约叠在哪一次打开的父层之上：格子 + 那一次的 issue。父层换了一次打开
 *  （同格子新 issue）就不再是它的父层。 */
export type RootModalParent = Readonly<{
  slot: RootModalSlot;
  issue: number;
}>;

export type RootModalLease<S extends RootModalSlot = RootModalSlot> = Readonly<{
  slot: S;
  issue: number;
  conflictIssue: number | null;
  workspaceIssue: number | null;
  owner: RootModalOwner;
  returnFocus: HTMLElement | null;
  parent: RootModalParent | null;
}>;

export type RootModalView = Readonly<{
  open: boolean;
  topmost: boolean;
  zIndex: number;
}>;

export type ModalPolicy = Readonly<{
  ownerKinds: readonly RootModalOwner["kind"][];
  conflictGroup: "primary" | null;
  layer: 20 | 60 | 80 | 84;
  backdrop: boolean;
  escape: boolean;
  workspaceSensitive?: boolean;
  /**
   * 本格还可以作为**子层**叠在哪些格子之上（`openAbove`）。子层：
   * · 继承父层那一次打开的 owner（不受本格 `ownerKinds` 约束——它的寿命被父层框住）；
   * · 与父层（及父层的祖先）不按冲突组互斥，其余规则照旧：同组的别的 primary 一样
   *   被它顶掉，它也一样被后来的 primary 顶掉；
   * · 恒在父层之上（同层按开启顺序，父层被 `bringToFront` 时子层跟着上移），因此
   *   Escape / 遮罩先归它——「最上层先吃」是 `requestClose` 既有的那条规则，不是
   *   父层里的特判；
   * · 父层不论因何离场（关闭、被顶掉、owner 失效），子层同一刻一起撤下。
   * 不列在这里的父层一概拒绝：叠放关系与其余策略一样只在这张表里声明。
   */
  stacksOver?: readonly RootModalSlot[];
}>;

// This table is the presentation policy's only source of truth.  It deliberately
// contains no domain payload, permission vocabulary, API callback, busy flag, or
// timer.  A single primary root dialog may be visible, while the typed info layer
// may sit above it; this preserves the existing analytics -> confirmation and
// source-detail -> confirmation shapes without introducing one global activeModal.
export const ROOT_MODAL_POLICIES: Readonly<Record<RootModalSlot, ModalPolicy>> = {
  "global-ask": { ownerKinds: ["actor"], conflictGroup: "primary", layer: 60, backdrop: false, escape: true },
  "password-change": { ownerKinds: ["actor"], conflictGroup: "primary", layer: 60, backdrop: false, escape: false },
  // 登录后异步弹出的公告，不是用户主动打开的工作流：conflictGroup 为 null，这样它
  // 晚到时不会把用户此刻已开的 primary 弹窗（如分享链接预览）挤掉；同层按开启顺序
  // 叠放，晚开的在上，被盖住时由 view().topmost 退出交互树。actor 拥有，不随笔记本切换失效。
  "release-notes": { ownerKinds: ["actor"], conflictGroup: null, layer: 60, backdrop: false, escape: false },
  "search-profile": { ownerKinds: ["actor"], conflictGroup: "primary", layer: 60, backdrop: false, escape: false },
  "notebook-editor": { ownerKinds: ["actor"], conflictGroup: "primary", layer: 60, backdrop: false, escape: false, workspaceSensitive: true },
  "notebook-delete": { ownerKinds: ["actor"], conflictGroup: "primary", layer: 60, backdrop: false, escape: false, workspaceSensitive: true },
  // 网页导出(镜像库卡片菜单的入口)与导入:同笔记本设置一档——集合页上 actor 拥有、
  // 打开笔记本即撤下。两条退路(Escape、遮罩)都关着:导入弹窗里有选好、还没上传的
  // 文件,误触遮罩不该把它丢掉;所以「×」任何时候都不禁用(作业在服务端接着跑)。
  "notebook-export": { ownerKinds: ["actor"], conflictGroup: "primary", layer: 60, backdrop: false, escape: false, workspaceSensitive: true },
  "notebook-import": { ownerKinds: ["actor"], conflictGroup: "primary", layer: 60, backdrop: false, escape: false, workspaceSensitive: true },
  "source-add": { ownerKinds: ["workspace"], conflictGroup: "primary", layer: 20, backdrop: true, escape: false },
  "source-detail": { ownerKinds: ["workspace"], conflictGroup: "primary", layer: 60, backdrop: false, escape: false },
  info: { ownerKinds: ["actor", "workspace", "source"], conflictGroup: null, layer: 80, backdrop: false, escape: false },
  "model-service": { ownerKinds: ["actor"], conflictGroup: "primary", layer: 60, backdrop: true, escape: true },
  "notebook-share": { ownerKinds: ["workspace"], conflictGroup: "primary", layer: 60, backdrop: true, escape: false },
  "shared-preview": { ownerKinds: ["actor"], conflictGroup: "primary", layer: 60, backdrop: true, escape: false },
  "shared-by-me": { ownerKinds: ["actor"], conflictGroup: "primary", layer: 60, backdrop: true, escape: false },
  "memory-save": { ownerKinds: ["workspace"], conflictGroup: "primary", layer: 84, backdrop: false, escape: false },
  "catalog-review": { ownerKinds: ["source"], conflictGroup: "primary", layer: 80, backdrop: true, escape: false },
  "conversation-share": { ownerKinds: ["workspace"], conflictGroup: "primary", layer: 60, backdrop: false, escape: false },
  analytics: { ownerKinds: ["workspace"], conflictGroup: "primary", layer: 60, backdrop: true, escape: false },
  "kg-schema": { ownerKinds: ["workspace"], conflictGroup: "primary", layer: 60, backdrop: true, escape: false },
  "kg-analysis": { ownerKinds: ["workspace"], conflictGroup: "primary", layer: 60, backdrop: true, escape: false },
  understanding: { ownerKinds: ["workspace"], conflictGroup: "primary", layer: 60, backdrop: true, escape: false },
  "promotion-queue": { ownerKinds: ["actor"], conflictGroup: "primary", layer: 60, backdrop: true, escape: false },
  "promotion-target": { ownerKinds: ["workspace"], conflictGroup: "primary", layer: 60, backdrop: true, escape: false },
  "edge-review": { ownerKinds: ["workspace"], conflictGroup: "primary", layer: 60, backdrop: true, escape: false },
  // 全局问答窗口里的引用配图点开后叠在窗口之上（子层，见 `stacksOver`）：第一次 Esc
  // 只收图、焦点回到点开它的那张图，窗口原样留着；窗口离场时预览随之撤下。笔记本内
  // 那条入口不经子层，仍是普通的 workspace primary，口径一字未变。
  "answer-image-preview": { ownerKinds: ["workspace"], conflictGroup: "primary", layer: 60, backdrop: true, escape: true, stacksOver: ["global-ask"] },
  // 与最接近的核心弹窗（`understanding`——内建插件打开的就是它）同一档：workspace 拥有、
  // primary 冲突组、layer 60、可点遮罩关闭、不接 Escape。`escape: false` 不是遗漏：
  // 除 `model-service` 外每个 primary 都是 false，给插件弹窗单独开 Escape 会让它比任何
  // 核心弹窗都更容易被误关。与 `info`（conflictGroup 为 null、layer 80）的共存因此是
  // 默认规则给的，不需要往 `primarySlotsMayCoexist` 里加例外。
  extension: { ownerKinds: ["workspace"], conflictGroup: "primary", layer: 60, backdrop: true, escape: false },
};

/**
 * 纯判据：`slot` 能否作为子层叠在 `parentSlot` 这一次打开之上。`parentChain` 是父层连同
 * 它的祖先（父、祖父……）。策略表没声明这一对，或本格已经在父链上（互相叠放：叠上去
 * 之后父链里那一格被这次发布替换掉，留下一个父层已不在场的孤儿子层），一概拒绝。
 */
export function mayStackAbove(
  policies: Readonly<Partial<Record<RootModalSlot, Readonly<{ stacksOver?: readonly RootModalSlot[] }>>>>,
  slot: RootModalSlot,
  parentSlot: RootModalSlot,
  parentChain: ReadonlySet<RootModalSlot>,
): boolean {
  return Boolean(policies[slot]?.stacksOver?.includes(parentSlot)) && !parentChain.has(slot);
}

// --- 一次 Escape 按键的账（模块级，全页一份）--------------------------------------
//
// 「一次 Escape 只收一层」。同一次按键可能以两种形式先后到来：keydown，以及浏览器在它
// 之后派发的 close request（`<dialog>` 的 cancel）。各浏览器对「keydown 被
// preventDefault 就不再派发 close request」并不一致，而那一刻被盖住的一层往往已经恢复
// 成最上层——没有这本账，它会被同一次按键一起关掉。
//
// 账记在**按键**上：每次 Escape 按下记一个序号，这一次按键至多被「认领」一次。认领方
// 不止协调器——在 window 捕获期自己吃掉 Esc 的层（引用卡、全局问答的分享弹窗）也认领，
// 于是它们吃掉的那一次按键，随后的 close request 同样拿不到名额。
//
// 「这一次按键」何时结束：任意键的 keyup、别的键按下、pointerdown、失焦、页面隐藏，
// 都算结束。不只认 Escape 自己的 keyup：macOS 按住 Cmd 时其他键的 keyup 会被吞掉，只认
// 它的话账会一直停在「按住」，之后不经键盘的 close request（返回手势）就一直被拒。上面
// 这几种事件在按键与它的 close request 之间都不会出现（两者在同一个任务里先后派发），
// 所以提前结束不会放走那一次 close request。不经键盘的 close request 不在任何一次按键
// 之内，照旧每次关一层。
//
// **按住也只关一层**：按住 Escape 时浏览器连发 `repeat: true` 的 keydown，它们都属于
// 同一次按键，不另起序号——否则第一次 keydown 关掉预览，下一次自动重复就把刚露出来的
// 引用卡或窗口也关掉。按键在上面那几个结束条件出现之后若还有 Escape 的自动重复（例如
// 按住 Escape 时点了一下鼠标），它重新挂回同一个序号，那个序号已被认领，照样拿不到名额。
// 别的键的自动重复（例如一直按着 Shift）不算「别的键按下」，不结束这次按键。
//
// 模块级而不是挂在某个协调器实例上：认领方里有独立页（`/ask`、`/c/{token}`）上没有协调器
// 的组件；监听在模块加载时就位，早于任何认领方的捕获期监听，序号因此总在同一次派发里
// 先加一。只碰 window 上的键鼠事件，不做 I/O、不起计时器。
const escapePress = { serial: 0, held: false, claimed: -1 };

function endEscapePress() {
  escapePress.held = false;
}

if (typeof window !== "undefined") {
  window.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") {
      if (!event.repeat) endEscapePress();
      return;
    }
    if (!event.repeat) escapePress.serial += 1;
    escapePress.held = true;
  }, true);
  window.addEventListener("keyup", endEscapePress, true);
  window.addEventListener("pointerdown", endEscapePress, true);
  window.addEventListener("blur", endEscapePress);
  document.addEventListener("visibilitychange", endEscapePress);
}

/**
 * 认领这一次 Escape 按键：返回 true 表示由调用方处理它（一层就此关闭或吃掉这次按键），
 * false 表示这次按键已经被别的层认领过，调用方不得再为它关闭任何东西。不在任何一次按键
 * 之内（不经键盘的 close request）恒为 true。
 */
export function claimEscapePress(): boolean {
  if (!escapePress.held) return true;
  if (escapePress.claimed === escapePress.serial) return false;
  escapePress.claimed = escapePress.serial;
  return true;
}

// The only primary pairs that may stay open together, as [already open, being
// opened]. Each one is a drill-down from the slot already open:
//   · source-detail ⇄ catalog-review — the command-catalog review opens from a
//     source's own detail window (both directions, as before);
//   · kg-analysis → source-detail — the KG analysis findings open a source for
//     checking ("打开来源核对") on top of the panel, which stays open underneath
//     (inert while covered) so closing the source returns the reader to the same
//     finding, scroll position and focused button;
//   · kg-analysis → catalog-review — that source detail can open its catalog
//     review while the panel is still underneath; without this pair the second
//     step would close the panel the first step deliberately kept.
// Directed on purpose: the analysis panel renders inside the KG view's stacking
// context (below every root-level dialog), so opening it while a source detail is
// already up must still evict the detail — kept, the detail would sit on top
// visually while the coordinator made the panel topmost and the detail inert.
const COEXISTING_PRIMARY_PAIRS: ReadonlyArray<readonly [RootModalSlot, RootModalSlot]> = [
  ["source-detail", "catalog-review"],
  ["catalog-review", "source-detail"],
  ["kg-analysis", "source-detail"],
  ["kg-analysis", "catalog-review"],
];

function primarySlotsMayCoexist(open: RootModalSlot, opening: RootModalSlot): boolean {
  return COEXISTING_PRIMARY_PAIRS.some(([kept, incoming]) => open === kept && opening === incoming);
}

type ActiveLease = RootModalLease & Readonly<{ order: number }>;

type PendingFocusReturn = Readonly<{
  element: HTMLElement;
  expectedSlot: RootModalSlot | null;
  expectedIssue: number | null;
}>;

export type RootModalTransition = Readonly<{
  serial: number;
}>;

type RootModalOptions = {
  actorId: string | null;
  sourceId: string | null;
  onClosed(slot: RootModalSlot, reason: RootModalCloseReason): void;
  /**
   * 换一张策略表。**只给用例用**：产品代码一律用 `ROOT_MODAL_POLICIES`（它是策略的唯一
   * 真源），这个入口只为验证策略表组合不出的情形（例如互相叠放）下协调器自身的判据。
   */
  policies?: Readonly<Record<RootModalSlot, ModalPolicy>>;
};

function connected(element: HTMLElement | null): element is HTMLElement {
  return Boolean(element?.isConnected);
}

function sameOwner(left: RootModalOwner, right: RootModalOwner): boolean {
  if (
    left.kind !== right.kind
    || left.actorId !== right.actorId
    || left.actorGeneration !== right.actorGeneration
  ) return false;
  if (left.kind === "actor" || right.kind === "actor") return left.kind === right.kind;
  if (
    left.notebookId !== right.notebookId
    || left.workspaceEpoch !== right.workspaceEpoch
    || left.workspaceGeneration !== right.workspaceGeneration
  ) return false;
  if (left.kind === "workspace" || right.kind === "workspace") return left.kind === right.kind;
  return left.sourceId === right.sourceId && left.sourceGeneration === right.sourceGeneration;
}

export function useRootModalCoordinator({ actorId, sourceId, onClosed, policies = ROOT_MODAL_POLICIES }: RootModalOptions) {
  const onClosedRef = useRef(onClosed);
  onClosedRef.current = onClosed;
  const actorIdRef = useRef(actorId);
  const previousActorIdRef = useRef(actorId);
  const pendingActorIdRef = useRef<string | null>(null);
  const actorDetachedRef = useRef(false);
  const actorGenerationRef = useRef(0);
  const workspaceOwnerRef = useRef<WorkspaceModalOwner | null>(null);
  const workspaceGenerationRef = useRef(0);
  const workspaceTransitionSerialRef = useRef(0);
  const sourceIdRef = useRef(sourceId);
  const sourceGenerationRef = useRef(0);
  const previousSourceIdRef = useRef(sourceId);
  const issueRef = useRef(new Map<RootModalSlot, number>());
  const primaryIssueRef = useRef(0);
  const activeRef = useRef(new Map<RootModalSlot, ActiveLease>());
  const orderRef = useRef(0);
  const pendingInvalidationsRef = useRef<Array<[RootModalSlot, RootModalCloseReason]>>([]);
  const pendingFocusReturnRef = useRef<PendingFocusReturn | null>(null);
  const [version, setVersion] = useState(0);

  const ownerIsCurrent = (owner: RootModalOwner): boolean => {
    if (
      actorIdRef.current !== owner.actorId
      || actorGenerationRef.current !== owner.actorGeneration
    ) return false;
    if (owner.kind === "actor") return true;
    const workspace = workspaceOwnerRef.current;
    if (
      !workspace
      || workspace.notebookId !== owner.notebookId
      || workspace.workspaceEpoch !== owner.workspaceEpoch
      || workspace.workspaceGeneration !== owner.workspaceGeneration
    ) return false;
    if (owner.kind === "workspace") return true;
    return sourceIdRef.current === owner.sourceId
      && sourceGenerationRef.current === owner.sourceGeneration;
  };

  const childrenOf = (parent: ActiveLease): ActiveLease[] => (
    [...activeRef.current.values()].filter((active) => (
      active.parent?.slot === parent.slot && active.parent.issue === parent.issue
    ))
  );

  const removeWithoutRender = (
    slot: RootModalSlot,
    reason: RootModalCloseReason,
    notify: boolean,
  ): ActiveLease | null => {
    const active = activeRef.current.get(slot) ?? null;
    if (!active) return null;
    // 子层先撤：父层没了，叠在它上面的那一层同一刻离场（它的 owner 就是父层这一次打开）。
    for (const child of childrenOf(active)) removeWithoutRender(child.slot, "owner-invalidated", notify);
    activeRef.current.delete(slot);
    if (notify) onClosedRef.current(slot, reason);
    else pendingInvalidationsRef.current.push([slot, reason]);
    return active;
  };

  const invalidateAllWithoutRender = () => {
    for (const slot of [...activeRef.current.keys()]) {
      removeWithoutRender(slot, "owner-invalidated", false);
    }
  };

  const invalidateSourceWithoutRender = () => {
    for (const [slot, active] of [...activeRef.current.entries()]) {
      if (active.owner.kind === "source") {
        removeWithoutRender(slot, "owner-invalidated", false);
      }
    }
  };

  // Authentication is a render-time authority boundary.  The pending bridge
  // covers fetchMe/ AuthGate calling activateActor just before currentUser is
  // published; the explicit detached latch prevents an old prop from rebinding
  // the coordinator while logout is still awaiting the server.
  if (!actorDetachedRef.current) {
    if (pendingActorIdRef.current === actorId) pendingActorIdRef.current = null;
    if (previousActorIdRef.current !== actorId) {
      if (!(pendingActorIdRef.current && actorId === null)) {
        pendingActorIdRef.current = null;
        previousActorIdRef.current = actorId;
        actorIdRef.current = actorId;
        actorGenerationRef.current += 1;
        workspaceGenerationRef.current += 1;
        workspaceOwnerRef.current = null;
        issueRef.current.clear();
        invalidateAllWithoutRender();
      }
    } else if (!pendingActorIdRef.current) {
      actorIdRef.current = actorId;
    }
  }

  if (previousSourceIdRef.current !== sourceId) {
    previousSourceIdRef.current = sourceId;
    sourceIdRef.current = sourceId;
    sourceGenerationRef.current += 1;
    invalidateSourceWithoutRender();
  } else {
    sourceIdRef.current = sourceId;
  }

  useEffect(() => {
    if (pendingInvalidationsRef.current.length === 0) return;
    const pending = pendingInvalidationsRef.current.splice(0);
    for (const [slot, reason] of pending) onClosedRef.current(slot, reason);
    setVersion((value) => value + 1);
  }, [actorId, sourceId]);

  const currentEntries = (): ActiveLease[] => (
    [...activeRef.current.values()].filter((active) => ownerIsCurrent(active.owner))
  );

  const topmost = (): ActiveLease | null => {
    let winner: ActiveLease | null = null;
    for (const active of currentEntries()) {
      if (!winner) {
        winner = active;
        continue;
      }
      const layer = policies[active.slot].layer;
      const winnerLayer = policies[winner.slot].layer;
      if (layer > winnerLayer || (layer === winnerLayer && active.order > winner.order)) {
        winner = active;
      }
    }
    return winner;
  };

  // A covered primary is still inert in the DOM during requestClose itself.
  // Restore focus only after React has committed the version that removes that
  // inert attribute, and only if the same underlying layer is still topmost.
  useLayoutEffect(() => {
    const pending = pendingFocusReturnRef.current;
    if (!pending) return;
    pendingFocusReturnRef.current = null;
    const current = topmost();
    if (
      (pending.expectedSlot === null && current !== null)
      || (
        pending.expectedSlot !== null
        && (current?.slot !== pending.expectedSlot || current.issue !== pending.expectedIssue)
      )
      || !connected(pending.element)
      || pending.element.closest("[inert]")
    ) return;
    pending.element.focus();
  }, [version]); // eslint-disable-line react-hooks/exhaustive-deps

  function activateActor(nextActorId: string) {
    if (!nextActorId || actorIdRef.current === nextActorId || actorIdRef.current !== null) return;
    actorDetachedRef.current = false;
    pendingActorIdRef.current = nextActorId;
    previousActorIdRef.current = nextActorId;
    actorIdRef.current = nextActorId;
    actorGenerationRef.current += 1;
    workspaceGenerationRef.current += 1;
    workspaceOwnerRef.current = null;
    issueRef.current.clear();
    invalidateAllWithoutRender();
    setVersion((value) => value + 1);
  }

  function leaveActor() {
    actorDetachedRef.current = true;
    pendingActorIdRef.current = null;
    previousActorIdRef.current = null;
    actorIdRef.current = null;
    actorGenerationRef.current += 1;
    workspaceGenerationRef.current += 1;
    workspaceOwnerRef.current = null;
    issueRef.current.clear();
    for (const slot of [...activeRef.current.keys()]) {
      removeWithoutRender(slot, "owner-invalidated", true);
    }
    setVersion((value) => value + 1);
  }

  function captureActorOwner(): ActorModalOwner | null {
    const currentActorId = actorIdRef.current;
    return currentActorId
      ? Object.freeze({
        kind: "actor" as const,
        actorId: currentActorId,
        actorGeneration: actorGenerationRef.current,
      })
      : null;
  }

  function captureWorkspaceOwner(): WorkspaceModalOwner | null {
    const owner = workspaceOwnerRef.current;
    return owner && ownerIsCurrent(owner) ? owner : null;
  }

  function captureSourceOwner(): SourceModalOwner | null {
    const workspace = captureWorkspaceOwner();
    const currentSourceId = sourceIdRef.current;
    return workspace && currentSourceId
      ? Object.freeze({
        ...workspace,
        kind: "source" as const,
        sourceId: currentSourceId,
        sourceGeneration: sourceGenerationRef.current,
      })
      : null;
  }

  function beginWorkspaceTransition(): RootModalTransition {
    const transition = Object.freeze({
      serial: ++workspaceTransitionSerialRef.current,
    });
    workspaceGenerationRef.current += 1;
    sourceGenerationRef.current += 1;
    workspaceOwnerRef.current = null;
    for (const [slot, active] of [...activeRef.current.entries()]) {
      if (active.owner.kind !== "actor" || policies[slot].workspaceSensitive) {
        removeWithoutRender(slot, "owner-invalidated", true);
      }
    }
    setVersion((value) => value + 1);
    return transition;
  }

  function finishWorkspaceTransition(
    transition: RootModalTransition,
    result: Readonly<{ actorId: string; notebookId: string; workspaceEpoch: number }> | null,
  ) {
    if (workspaceTransitionSerialRef.current !== transition.serial) return;
    const currentActorId = actorIdRef.current;
    const target = result && result.actorId === currentActorId
      ? result
      : null;
    workspaceOwnerRef.current = target && currentActorId
      ? Object.freeze({
        kind: "workspace" as const,
        actorId: currentActorId,
        actorGeneration: actorGenerationRef.current,
        notebookId: target.notebookId,
        workspaceEpoch: target.workspaceEpoch,
        workspaceGeneration: workspaceGenerationRef.current,
      })
      : null;
    setVersion((value) => value + 1);
  }

  function leaveWorkspace() {
    workspaceTransitionSerialRef.current += 1;
    workspaceGenerationRef.current += 1;
    sourceGenerationRef.current += 1;
    workspaceOwnerRef.current = null;
    for (const [slot, active] of [...activeRef.current.entries()]) {
      if (active.owner.kind !== "actor" || policies[slot].workspaceSensitive) {
        removeWithoutRender(slot, "owner-invalidated", true);
      }
    }
    setVersion((value) => value + 1);
  }

  function issue<S extends RootModalSlot>(
    slot: S,
    owner: RootModalOwner | null,
  ): RootModalLease<S> | null {
    return issueLease(slot, owner, null);
  }

  function issueLease<S extends RootModalSlot>(
    slot: S,
    owner: RootModalOwner | null,
    parent: RootModalParent | null,
  ): RootModalLease<S> | null {
    const policy = policies[slot];
    if (!owner || !ownerIsCurrent(owner)) return null;
    // 子层的 owner 是父层那一次打开的 owner，归属由 `stacksOver` 授权，不再看 ownerKinds。
    if (!parent && !policy.ownerKinds.includes(owner.kind)) return null;
    const nextIssue = (issueRef.current.get(slot) ?? 0) + 1;
    issueRef.current.set(slot, nextIssue);
    const conflictIssue = policy.conflictGroup === "primary"
      ? ++primaryIssueRef.current
      : null;
    const workspaceIssue = policy.workspaceSensitive
      ? workspaceGenerationRef.current
      : null;
    const returnFocus = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
    return Object.freeze({ slot, issue: nextIssue, conflictIssue, workspaceIssue, owner, returnFocus, parent });
  }

  /** 父层这一次打开仍在场、且仍属当前 owner。 */
  const parentIsCurrent = (parent: RootModalParent | null): boolean => {
    if (!parent) return true;
    const active = activeRef.current.get(parent.slot);
    return Boolean(active && active.issue === parent.issue && ownerIsCurrent(active.owner));
  };

  /** 一次打开的父层链（父、祖父……），发布子层时它们都不算冲突。 */
  const ancestorsOf = (parent: RootModalParent | null): Set<RootModalSlot> => {
    const slots = new Set<RootModalSlot>();
    let cursor = parent;
    while (cursor && !slots.has(cursor.slot)) {
      slots.add(cursor.slot);
      cursor = activeRef.current.get(cursor.slot)?.parent ?? null;
    }
    return slots;
  };

  // Async openers need an authority check before the presentation lease is
  // published. `owns` deliberately requires an already-open slot; this checks
  // only the frozen issue and owner and performs no browser effect.
  function leaseIsCurrent<S extends RootModalSlot>(
    lease: RootModalLease<S> | null,
  ): lease is RootModalLease<S> {
    return Boolean(
      lease
      && issueRef.current.get(lease.slot) === lease.issue
      && (
        lease.conflictIssue === null
        || primaryIssueRef.current === lease.conflictIssue
      )
      && (
        lease.workspaceIssue === null
        || workspaceGenerationRef.current === lease.workspaceIssue
      )
      && ownerIsCurrent(lease.owner)
      && parentIsCurrent(lease.parent),
    );
  }

  function publish<S extends RootModalSlot>(lease: RootModalLease<S> | null): lease is RootModalLease<S> {
    if (!leaseIsCurrent(lease)) return false;
    const policy = policies[lease.slot];
    const ancestors = ancestorsOf(lease.parent);
    for (const [slot] of [...activeRef.current.entries()]) {
      if (ancestors.has(slot)) continue;
      if (
        slot === lease.slot
        || (
          policy.conflictGroup
          && policies[slot].conflictGroup === policy.conflictGroup
          && !primarySlotsMayCoexist(slot, lease.slot)
        )
      ) {
        removeWithoutRender(slot, "conflict", true);
      }
    }
    activeRef.current.set(lease.slot, Object.freeze({
      ...lease,
      order: ++orderRef.current,
    }));
    setVersion((value) => value + 1);
    return true;
  }

  function open<S extends RootModalSlot>(slot: S, owner: RootModalOwner | null): RootModalLease<S> | null {
    const current = activeLease(slot);
    // 同一 owner 重开是幂等的——只对顶层打开成立。开着的若是子层，它的 owner 是继承来的
    // （不受本格 ownerKinds 约束），不能借这条短路让顶层 open 绕过 ownerKinds。
    if (owner && current && current.parent === null && sameOwner(current.owner, owner)) return current;
    const lease = issue(slot, owner);
    return publish(lease) ? lease : null;
  }

  /**
   * 把 `slot` 作为子层叠在**此刻最上层**的 `parentSlot` 之上打开（见策略表的
   * `stacksOver`）。父层不在场、不是最上层（被别的层盖住时用户本来点不到它）、或
   * 策略表没有声明这一对叠放关系，一概返回 null，什么都不动。
   */
  function openAbove<S extends RootModalSlot>(slot: S, parentSlot: RootModalSlot): RootModalLease<S> | null {
    const parent = activeRef.current.get(parentSlot);
    if (!parent || !ownerIsCurrent(parent.owner) || topmost()?.slot !== parentSlot) return null;
    const link = Object.freeze({ slot: parentSlot, issue: parent.issue });
    if (!mayStackAbove(policies, slot, parentSlot, ancestorsOf(link))) return null;
    const lease = issueLease(slot, parent.owner, link);
    return publish(lease) ? lease : null;
  }

  function owns(lease: RootModalLease | null): boolean {
    if (!leaseIsCurrent(lease)) return false;
    const active = activeRef.current.get(lease.slot);
    return Boolean(active && active.issue === lease.issue);
  }

  function activeLease<S extends RootModalSlot>(slot: S): RootModalLease<S> | null {
    const active = activeRef.current.get(slot);
    return active && active.slot === slot && ownerIsCurrent(active.owner)
      ? Object.freeze({
        slot,
        issue: active.issue,
        conflictIssue: active.conflictIssue,
        workspaceIssue: active.workspaceIssue,
        owner: active.owner,
        returnFocus: active.returnFocus,
        parent: active.parent,
      })
      : null;
  }

  function requestClose(slot: RootModalSlot, reason: "button" | "backdrop" | "escape"): boolean {
    const active = activeRef.current.get(slot);
    if (!active || !ownerIsCurrent(active.owner)) return false;
    const policy = policies[slot];
    if (reason === "backdrop" && !policy.backdrop) return false;
    if (reason === "escape" && !policy.escape) return false;
    if ((reason === "backdrop" || reason === "escape") && topmost()?.slot !== slot) return false;
    if (reason === "escape" && !claimEscapePress()) return false;
    const wasTopmost = topmost()?.slot === slot;
    removeWithoutRender(slot, reason, true);
    const revealed = topmost();
    pendingFocusReturnRef.current = wasTopmost && connected(active.returnFocus)
      ? Object.freeze({
        element: active.returnFocus,
        expectedSlot: revealed?.slot ?? null,
        expectedIssue: revealed?.issue ?? null,
      })
      : null;
    setVersion((value) => value + 1);
    return true;
  }

  function bringToFront(slot: RootModalSlot) {
    const active = activeRef.current.get(slot);
    if (!active || !ownerIsCurrent(active.owner)) return;
    // 子层恒在父层之上：父层上移时，叠在它上面的各层按原有先后跟着上移。
    const raise = (lease: ActiveLease) => {
      activeRef.current.set(lease.slot, Object.freeze({ ...lease, order: ++orderRef.current }));
      for (const child of childrenOf(lease).sort((left, right) => left.order - right.order)) raise(child);
    };
    raise(active);
    setVersion((value) => value + 1);
  }

  function view(slot: RootModalSlot): RootModalView {
    const active = activeRef.current.get(slot);
    if (!active || !ownerIsCurrent(active.owner)) {
      return Object.freeze({ open: false, topmost: false, zIndex: policies[slot].layer });
    }
    const sameLayer = currentEntries()
      .filter((entry) => policies[entry.slot].layer === policies[slot].layer)
      .sort((left, right) => left.order - right.order);
    const rank = Math.max(0, sameLayer.findIndex((entry) => entry.slot === slot));
    return Object.freeze({
      open: true,
      topmost: topmost()?.slot === slot,
      zIndex: policies[slot].layer + rank,
    });
  }

  return {
    activateActor,
    leaveActor,
    captureActorOwner,
    captureWorkspaceOwner,
    captureSourceOwner,
    beginWorkspaceTransition,
    finishWorkspaceTransition,
    leaveWorkspace,
    issue,
    publish,
    open,
    openAbove,
    leaseIsCurrent,
    owns,
    activeLease,
    requestClose,
    bringToFront,
    view,
  };
}

// 协调器的完整命令面。给**别的模块**用来 `Pick<>` 出自己真正需要的那几个命令
// （见 use-promotion-queue.ts / use-edge-review-queue.ts）——它们因此拿到的是结构
// 类型的窄面，而不是"整个 rootModals"；page.tsx 直接把 rootModals 传过去即可。
// 纯类型别名，无运行时代码，不改变本模块"只依赖 react、不持有任何领域负载"的边界。
export type RootModalCoordinator = ReturnType<typeof useRootModalCoordinator>;
