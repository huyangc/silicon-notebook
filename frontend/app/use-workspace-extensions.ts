"use client";

import { useEffect, useRef, useState } from "react";

import { fetchSystemExtensions } from "./system-api.ts";
import type {
  SystemExtensionProjection,
  WorkspaceUiContribution,
} from "../features/extension-sdk/contracts.ts";
import { WORKSPACE_UI_CONTRIBUTIONS } from "../features/extension-sdk/registry.ts";


type AvailabilityState = Readonly<{
  ownerKey: string;
  projection: SystemExtensionProjection;
}>;

type AvailabilityRequest = Readonly<{
  ownerKey: string;
  promise: Promise<SystemExtensionProjection>;
}>;


export function useWorkspaceExtensions(
  actorId: string | null,
  registry: readonly WorkspaceUiContribution[] = WORKSPACE_UI_CONTRIBUTIONS,
  load: () => Promise<SystemExtensionProjection> = fetchSystemExtensions,
): SystemExtensionProjection | null {
  const actorRef = useRef(actorId);
  const generationRef = useRef(0);
  const requestRef = useRef<AvailabilityRequest | null>(null);
  if (actorRef.current !== actorId) {
    actorRef.current = actorId;
    generationRef.current += 1;
    requestRef.current = null;
  }
  const ownerKey = `${actorId ?? ""}\0${generationRef.current}`;
  const [state, setState] = useState<AvailabilityState | null>(null);

  useEffect(() => {
    // This is intentionally before AbortController, timers, or any other side
    // effect.  PR-14's empty build registry is byte-equivalent at runtime.
    if (registry.length === 0 || !actorId) return;
    let alive = true;
    let request = requestRef.current;
    if (!request || request.ownerKey !== ownerKey) {
      request = { ownerKey, promise: load() };
      requestRef.current = request;
    }
    request.promise.then(
      (projection) => {
        if (
          alive
          && actorRef.current === actorId
          && `${actorRef.current}\0${generationRef.current}` === ownerKey
        ) setState({ ownerKey, projection });
      },
      () => {
        if (
          alive
          && actorRef.current === actorId
          && `${actorRef.current}\0${generationRef.current}` === ownerKey
        ) setState({ ownerKey, projection: { apiVersion: "1", extensions: [] } });
      },
    );
    return () => { alive = false; };
  }, [actorId, load, ownerKey, registry]);

  return state?.ownerKey === ownerKey ? state.projection : null;
}
