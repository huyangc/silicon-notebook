"use client";

import { useEffect, useRef, useState } from "react";

export type RootModalSlot =
  | "password-change"
  | "search-profile"
  | "source-add"
  | "info"
  | "model-service"
  | "notebook-share"
  | "shared-preview"
  | "shared-by-me"
  | "memory-save"
  | "catalog-review"
  | "conversation-share"
  | "analytics"
  | "understanding"
  | "promotion-queue"
  | "promotion-target"
  | "edge-review";

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

export type RootModalLease<S extends RootModalSlot = RootModalSlot> = Readonly<{
  slot: S;
  issue: number;
  owner: RootModalOwner;
  returnFocus: HTMLElement | null;
}>;

export type RootModalView = Readonly<{
  open: boolean;
  topmost: boolean;
  zIndex: number;
}>;

type ModalPolicy = Readonly<{
  ownerKinds: readonly RootModalOwner["kind"][];
  conflictGroup: "primary" | null;
  layer: 20 | 60 | 80 | 84;
  backdrop: boolean;
  escape: boolean;
}>;

// This table is the presentation policy's only source of truth.  It deliberately
// contains no domain payload, permission vocabulary, API callback, busy flag, or
// timer.  A single primary root dialog may be visible, while the typed info layer
// may sit above it; this preserves the existing analytics -> confirmation and
// source-detail -> confirmation shapes without introducing one global activeModal.
export const ROOT_MODAL_POLICIES: Readonly<Record<RootModalSlot, ModalPolicy>> = {
  "password-change": { ownerKinds: ["actor"], conflictGroup: "primary", layer: 60, backdrop: false, escape: false },
  "search-profile": { ownerKinds: ["actor"], conflictGroup: "primary", layer: 60, backdrop: false, escape: false },
  "source-add": { ownerKinds: ["workspace"], conflictGroup: "primary", layer: 20, backdrop: true, escape: false },
  info: { ownerKinds: ["actor", "workspace", "source"], conflictGroup: null, layer: 80, backdrop: false, escape: false },
  "model-service": { ownerKinds: ["actor"], conflictGroup: "primary", layer: 60, backdrop: true, escape: true },
  "notebook-share": { ownerKinds: ["workspace"], conflictGroup: "primary", layer: 60, backdrop: true, escape: false },
  "shared-preview": { ownerKinds: ["actor"], conflictGroup: "primary", layer: 60, backdrop: true, escape: false },
  "shared-by-me": { ownerKinds: ["actor"], conflictGroup: "primary", layer: 60, backdrop: true, escape: false },
  "memory-save": { ownerKinds: ["workspace"], conflictGroup: "primary", layer: 84, backdrop: false, escape: false },
  "catalog-review": { ownerKinds: ["source"], conflictGroup: "primary", layer: 80, backdrop: true, escape: false },
  "conversation-share": { ownerKinds: ["workspace"], conflictGroup: "primary", layer: 60, backdrop: false, escape: false },
  analytics: { ownerKinds: ["workspace"], conflictGroup: "primary", layer: 60, backdrop: true, escape: false },
  understanding: { ownerKinds: ["workspace"], conflictGroup: "primary", layer: 60, backdrop: true, escape: false },
  "promotion-queue": { ownerKinds: ["actor"], conflictGroup: "primary", layer: 60, backdrop: true, escape: false },
  "promotion-target": { ownerKinds: ["workspace"], conflictGroup: "primary", layer: 60, backdrop: true, escape: false },
  "edge-review": { ownerKinds: ["workspace"], conflictGroup: "primary", layer: 60, backdrop: true, escape: false },
};

type ActiveLease = RootModalLease & Readonly<{ order: number }>;

export type RootModalTransition = Readonly<{
  serial: number;
}>;

type RootModalOptions = {
  actorId: string | null;
  sourceId: string | null;
  onClosed(slot: RootModalSlot, reason: RootModalCloseReason): void;
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

export function useRootModalCoordinator({ actorId, sourceId, onClosed }: RootModalOptions) {
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
  const activeRef = useRef(new Map<RootModalSlot, ActiveLease>());
  const orderRef = useRef(0);
  const pendingInvalidationsRef = useRef<Array<[RootModalSlot, RootModalCloseReason]>>([]);
  const [, setVersion] = useState(0);

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

  const removeWithoutRender = (
    slot: RootModalSlot,
    reason: RootModalCloseReason,
    notify: boolean,
  ): ActiveLease | null => {
    const active = activeRef.current.get(slot) ?? null;
    if (!active) return null;
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
      const layer = ROOT_MODAL_POLICIES[active.slot].layer;
      const winnerLayer = ROOT_MODAL_POLICIES[winner.slot].layer;
      if (layer > winnerLayer || (layer === winnerLayer && active.order > winner.order)) {
        winner = active;
      }
    }
    return winner;
  };

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
      if (active.owner.kind !== "actor") {
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
      if (active.owner.kind !== "actor") {
        removeWithoutRender(slot, "owner-invalidated", true);
      }
    }
    setVersion((value) => value + 1);
  }

  function issue<S extends RootModalSlot>(
    slot: S,
    owner: RootModalOwner | null,
  ): RootModalLease<S> | null {
    const policy = ROOT_MODAL_POLICIES[slot];
    if (!owner || !policy.ownerKinds.includes(owner.kind) || !ownerIsCurrent(owner)) return null;
    const nextIssue = (issueRef.current.get(slot) ?? 0) + 1;
    issueRef.current.set(slot, nextIssue);
    const returnFocus = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
    return Object.freeze({ slot, issue: nextIssue, owner, returnFocus });
  }

  // Async openers need an authority check before the presentation lease is
  // published. `owns` deliberately requires an already-open slot; this checks
  // only the frozen issue and owner and performs no browser effect.
  function leaseIsCurrent<S extends RootModalSlot>(
    lease: RootModalLease<S> | null,
  ): lease is RootModalLease<S> {
    return Boolean(
      lease
      && issueRef.current.get(lease.slot) === lease.issue
      && ownerIsCurrent(lease.owner),
    );
  }

  function publish<S extends RootModalSlot>(lease: RootModalLease<S> | null): lease is RootModalLease<S> {
    if (!leaseIsCurrent(lease)) return false;
    const policy = ROOT_MODAL_POLICIES[lease.slot];
    for (const [slot, active] of [...activeRef.current.entries()]) {
      if (
        slot === lease.slot
        || (policy.conflictGroup && ROOT_MODAL_POLICIES[slot].conflictGroup === policy.conflictGroup)
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
    if (owner && current && sameOwner(current.owner, owner)) return current;
    const lease = issue(slot, owner);
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
        owner: active.owner,
        returnFocus: active.returnFocus,
      })
      : null;
  }

  function requestClose(slot: RootModalSlot, reason: "button" | "backdrop" | "escape"): boolean {
    const active = activeRef.current.get(slot);
    if (!active || !ownerIsCurrent(active.owner)) return false;
    const policy = ROOT_MODAL_POLICIES[slot];
    if (reason === "backdrop" && !policy.backdrop) return false;
    if (reason === "escape" && !policy.escape) return false;
    if ((reason === "backdrop" || reason === "escape") && topmost()?.slot !== slot) return false;
    const wasTopmost = topmost()?.slot === slot;
    removeWithoutRender(slot, reason, true);
    setVersion((value) => value + 1);
    if (wasTopmost && connected(active.returnFocus)) active.returnFocus.focus();
    return true;
  }

  function bringToFront(slot: RootModalSlot) {
    const active = activeRef.current.get(slot);
    if (!active || !ownerIsCurrent(active.owner)) return;
    activeRef.current.set(slot, Object.freeze({ ...active, order: ++orderRef.current }));
    setVersion((value) => value + 1);
  }

  function view(slot: RootModalSlot): RootModalView {
    const active = activeRef.current.get(slot);
    if (!active || !ownerIsCurrent(active.owner)) {
      return Object.freeze({ open: false, topmost: false, zIndex: ROOT_MODAL_POLICIES[slot].layer });
    }
    const sameLayer = currentEntries()
      .filter((entry) => ROOT_MODAL_POLICIES[entry.slot].layer === ROOT_MODAL_POLICIES[slot].layer)
      .sort((left, right) => left.order - right.order);
    const rank = Math.max(0, sameLayer.findIndex((entry) => entry.slot === slot));
    return Object.freeze({
      open: true,
      topmost: topmost()?.slot === slot,
      zIndex: ROOT_MODAL_POLICIES[slot].layer + rank,
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
    leaseIsCurrent,
    owns,
    activeLease,
    requestClose,
    bringToFront,
    view,
  };
}
