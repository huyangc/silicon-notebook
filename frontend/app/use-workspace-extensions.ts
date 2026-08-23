"use client";

import { useEffect, useRef, useState } from "react";

import { fetchSystemExtensions } from "./system-api.ts";
import type { SystemExtensionProjection, WorkspaceUiContribution } from "../features/extension-sdk/contracts.ts";
import { WORKSPACE_UI_CONTRIBUTIONS } from "../features/extension-sdk/registry.ts";

type AvailabilityState = Readonly<{ ownerKey: string; projection: SystemExtensionProjection }>;
type AvailabilityRequest = Readonly<{ ownerKey: string; promise: Promise<SystemExtensionProjection> }>;

export type WorkspaceExtensionOwner = Readonly<{
  actorId: string;
  notebookId: string;
  workspaceEpoch: number;
  generation: number;
}>;
export type WorkspaceExtensionTransition = WorkspaceExtensionOwner;
export type WorkspaceExtensions = Readonly<{
  projection: SystemExtensionProjection | null;
  owner: WorkspaceExtensionOwner | null;
  ownerKey: string | null;
  activateActor(actorId: string): void;
  beginNotebookTransition(input: Omit<WorkspaceExtensionOwner, "generation">): WorkspaceExtensionTransition | null;
  finishNotebookTransition(transition: WorkspaceExtensionTransition, succeeded: boolean): void;
  leaveWorkspace(): void;
  owns(owner: WorkspaceExtensionOwner): boolean;
}>;

function sameOwner(left: WorkspaceExtensionOwner | null, right: WorkspaceExtensionOwner): boolean {
  return Boolean(left
    && left.actorId === right.actorId
    && left.notebookId === right.notebookId
    && left.workspaceEpoch === right.workspaceEpoch
    && left.generation === right.generation);
}

export function useWorkspaceExtensions(
  actorId: string | null,
  notebookId: string | null,
  registry: readonly WorkspaceUiContribution[] = WORKSPACE_UI_CONTRIBUTIONS,
  load: () => Promise<SystemExtensionProjection> = fetchSystemExtensions,
): WorkspaceExtensions {
  const actorRef = useRef(actorId);
  const previousActorRef = useRef(actorId);
  const pendingActorRef = useRef<string | null>(null);
  const actorGenerationRef = useRef(0);
  const workspaceGenerationRef = useRef(0);
  const notebookIdRef = useRef(notebookId);
  const ownerRef = useRef<WorkspaceExtensionOwner | null>(null);
  const requestRef = useRef<AvailabilityRequest | null>(null);
  const [ownerVersion, setOwnerVersion] = useState(0);
  const [state, setState] = useState<AvailabilityState | null>(null);

  notebookIdRef.current = notebookId;
  if (pendingActorRef.current === actorId) pendingActorRef.current = null;
  if (previousActorRef.current !== actorId) {
    if (!(pendingActorRef.current && actorId === null)) {
      pendingActorRef.current = null;
      previousActorRef.current = actorId;
      actorRef.current = actorId;
      actorGenerationRef.current += 1;
      requestRef.current = null;
      ownerRef.current = null;
      workspaceGenerationRef.current += 1;
    }
  } else if (!pendingActorRef.current) {
    actorRef.current = actorId;
  }

  const availabilityOwnerKey = `${actorId ?? ""}\0${actorGenerationRef.current}`;
  const visibleOwner = ownerRef.current;

  useEffect(() => {
    // Keep the zero-registry, signed-out and collection paths free of requests,
    // controllers, timers and plugin effects. A committed workspace is the
    // first point that needs the actor-scoped server projection.
    if (registry.length === 0 || !actorId || !visibleOwner) return;
    let alive = true;
    let request = requestRef.current;
    if (!request || request.ownerKey !== availabilityOwnerKey) {
      request = { ownerKey: availabilityOwnerKey, promise: load() };
      requestRef.current = request;
    }
    request.promise.then(
      (projection) => {
        if (alive && actorRef.current === actorId
          && `${actorRef.current}\0${actorGenerationRef.current}` === availabilityOwnerKey) {
          setState({ ownerKey: availabilityOwnerKey, projection });
        }
      },
      () => {
        // A failed projection request must not be cached forever: clear it so the
        // next committed workspace (notebook switch, actor reactivation, or any
        // other ownerVersion bump) re-issues the request instead of leaving the
        // side panel entry hidden until the user signs out and back in. The
        // identity check is what makes this a no-op once the actor has already
        // rotated generation — actor rotation resets requestRef.current
        // synchronously during render (see above), so a late reject from a
        // superseded generation can never see itself as the still-current request.
        if (requestRef.current === request) requestRef.current = null;
        if (alive && actorRef.current === actorId
          && `${actorRef.current}\0${actorGenerationRef.current}` === availabilityOwnerKey) {
          setState({ ownerKey: availabilityOwnerKey, projection: { apiVersion: "1", extensions: [] } });
        }
      },
    );
    return () => { alive = false; };
  }, [actorId, availabilityOwnerKey, load, ownerVersion, registry, visibleOwner]);

  function activateActor(nextActorId: string): void {
    if (!nextActorId || actorRef.current === nextActorId || actorRef.current !== null) return;
    actorRef.current = nextActorId;
    previousActorRef.current = nextActorId;
    pendingActorRef.current = nextActorId;
    actorGenerationRef.current += 1;
    workspaceGenerationRef.current += 1;
    requestRef.current = null;
    ownerRef.current = null;
    setOwnerVersion((value) => value + 1);
  }

  function beginNotebookTransition(
    input: Omit<WorkspaceExtensionOwner, "generation">,
  ): WorkspaceExtensionTransition | null {
    if (!input.actorId || actorRef.current !== input.actorId) return null;
    const transition = { ...input, generation: ++workspaceGenerationRef.current };
    ownerRef.current = null;
    setOwnerVersion((value) => value + 1);
    return transition;
  }

  function finishNotebookTransition(
    transition: WorkspaceExtensionTransition,
    succeeded: boolean,
  ): void {
    if (transition.generation !== workspaceGenerationRef.current
      || transition.actorId !== actorRef.current) return;
    if (succeeded) ownerRef.current = transition;
    setOwnerVersion((value) => value + 1);
  }

  function leaveWorkspace(): void {
    workspaceGenerationRef.current += 1;
    ownerRef.current = null;
    setOwnerVersion((value) => value + 1);
  }

  function owns(owner: WorkspaceExtensionOwner): boolean {
    return sameOwner(ownerRef.current, owner)
      && actorRef.current === owner.actorId
      && notebookIdRef.current === owner.notebookId;
  }

  const ownerVisible = Boolean(visibleOwner
    && visibleOwner.actorId === actorId
    && visibleOwner.notebookId === notebookId);
  const owner = ownerVisible ? visibleOwner : null;
  return {
    projection: owner && state?.ownerKey === availabilityOwnerKey ? state.projection : null,
    owner,
    ownerKey: owner
      ? `${owner.actorId}\0${owner.notebookId}\0${owner.workspaceEpoch}\0${owner.generation}`
      : null,
    activateActor,
    beginNotebookTransition,
    finishNotebookTransition,
    leaveWorkspace,
    owns,
  };
}
