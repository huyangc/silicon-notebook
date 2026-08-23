"use client";

import { useEffect, useRef, useState } from "react";
import {
  sameKgIdentity,
  sameKgOwner,
  type KgWorkspaceOwner,
  type KgWorkspaceTransition,
} from "./kg-workspace-model";
import type { NotebookSummary } from "./workspace-model";

// The shared actor/notebook/generation gate behind the three KG domain owners
// (`use-kg-knowledge` / `use-kg-schema` / `use-kg-graph`). It owns **only**
// identity: which actor, which notebook, which owner generation, whether a
// notebook transition is in flight, and the per-kind operation tokens that
// make a write command exclusive. It holds no domain state, issues no domain
// I/O, and imports no API module — the boundary guard pins that.
//
// Domains never see each other. Everything that has to happen across all
// three (clear on owner change, invalidate, adopt a freshly established
// owner) is announced here as a fan-out callback and routed by the
// composition layer (`use-kg-workspace.ts`) into each domain's own **named
// command**. No domain ever receives another domain's setter.
export type KgOwnerAuthority = {
  ownerVersion: number;
  currentOwner: () => KgWorkspaceOwner | null;
  owns: (owner: KgWorkspaceOwner) => boolean;
  ownsIdentity: (owner: Pick<KgWorkspaceOwner, "actorId" | "notebookId">) => boolean;
  beginOperation: (kind: string) => object;
  ownsOperation: (kind: string, token: object) => boolean;
};

export type KgOwnerFanOut = {
  // A new owner replaces the previous one: drop every domain's visible state
  // without touching in-flight request counters (the old owner's responses
  // are already rejected by the generation comparison in `owns`).
  clearVisibleState: () => void;
  // No owner at all until the next establishment: bump every domain's
  // in-flight request counters *and* drop its visible state.
  invalidate: () => void;
  // A fresh owner is established. `notebookSnapshot` is the notebook frozen
  // by `finishNotebookTransition`; the domain that tracks durable KG build
  // work interprets it (this hook never reads its fields).
  adoptOwner: (owner: KgWorkspaceOwner, notebookSnapshot: NotebookSummary | null) => void;
};

export type UseKgOwnerOptions = {
  actorId: string | null;
  notebookId: string | null;
  fanOut: KgOwnerFanOut;
};

export function useKgOwner({ actorId, notebookId, fanOut }: UseKgOwnerOptions) {
  const fanOutRef = useRef(fanOut);
  fanOutRef.current = fanOut;
  const actorIdRef = useRef(actorId);
  actorIdRef.current = actorId;
  const notebookIdRef = useRef(notebookId);
  notebookIdRef.current = notebookId;

  const generationRef = useRef(0);
  const viewGenerationRef = useRef(0);
  const transitionGenerationRef = useRef(0);
  const transitionSuspendedRef = useRef(false);
  const pendingNotebookSnapshotRef = useRef<NotebookSummary | null>(null);
  const ownerRef = useRef<KgWorkspaceOwner | null>(null);
  const [ownerVersion, forceOwnerRender] = useState(0);
  const operationTokensRef = useRef(new Map<string, object>());

  const currentOwner = (): KgWorkspaceOwner | null => {
    const owner = ownerRef.current;
    if (!owner || !actorIdRef.current || !notebookIdRef.current) return null;
    if (owner.actorId !== actorIdRef.current || owner.notebookId !== notebookIdRef.current) return null;
    return owner;
  };

  const owns = (owner: KgWorkspaceOwner): boolean => Boolean(
    actorIdRef.current === owner.actorId
    && notebookIdRef.current === owner.notebookId
    && sameKgOwner(ownerRef.current, owner),
  );

  const ownsIdentity = (owner: Pick<KgWorkspaceOwner, "actorId" | "notebookId">): boolean => Boolean(
    actorIdRef.current === owner.actorId
    && notebookIdRef.current === owner.notebookId
    && sameKgIdentity(ownerRef.current, owner),
  );

  const beginOperation = (kind: string): object => {
    const token = {};
    operationTokensRef.current.set(kind, token);
    return token;
  };
  const ownsOperation = (kind: string, token: object): boolean =>
    operationTokensRef.current.get(kind) === token;

  const invalidate = () => {
    generationRef.current += 1;
    viewGenerationRef.current += 1;
    ownerRef.current = null;
    pendingNotebookSnapshotRef.current = null;
    fanOutRef.current.invalidate();
    forceOwnerRender((value) => value + 1);
  };

  useEffect(() => {
    if (!actorId || !notebookId) {
      if (ownerRef.current) invalidate();
      return;
    }
    if (transitionSuspendedRef.current) return;
    const current = ownerRef.current;
    if (current && current.actorId === actorId && current.notebookId === notebookId) return;
    const owner: KgWorkspaceOwner = {
      actorId,
      notebookId,
      generation: ++generationRef.current,
      viewGeneration: ++viewGenerationRef.current,
    };
    ownerRef.current = owner;
    fanOutRef.current.clearVisibleState();
    const notebookSnapshot = pendingNotebookSnapshotRef.current;
    pendingNotebookSnapshotRef.current = null;
    fanOutRef.current.adoptOwner(owner, notebookSnapshot);
    forceOwnerRender((value) => value + 1);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [actorId, notebookId, ownerVersion]);

  const beginNotebookTransition = (): KgWorkspaceTransition => {
    transitionSuspendedRef.current = true;
    pendingNotebookSnapshotRef.current = null;
    const transition = { generation: ++transitionGenerationRef.current };
    invalidate();
    return transition;
  };

  const finishNotebookTransition = (
    transition: KgWorkspaceTransition,
    notebook?: NotebookSummary | null,
  ) => {
    if (transition.generation !== transitionGenerationRef.current) return;
    pendingNotebookSnapshotRef.current = notebook ?? null;
    transitionSuspendedRef.current = false;
    forceOwnerRender((value) => value + 1);
  };

  const activateActor = (nextActorId: string) => {
    if (!nextActorId || (actorIdRef.current && actorIdRef.current !== nextActorId)) invalidate();
    actorIdRef.current = nextActorId;
  };

  const leaveWorkspace = () => {
    transitionSuspendedRef.current = false;
    transitionGenerationRef.current += 1;
    invalidate();
  };

  const authority: KgOwnerAuthority = {
    ownerVersion,
    currentOwner,
    owns,
    ownsIdentity,
    beginOperation,
    ownsOperation,
  };

  return {
    authority,
    activateActor,
    beginNotebookTransition,
    finishNotebookTransition,
    leaveWorkspace,
  };
}
