"use client";

import { useEffect, useRef, useState } from "react";

type Owner = { actorId: string | null; signal: AbortSignal };
type OpenedSession = { opened: boolean; epoch: number };
type Request = { owner: Owner; notebookId: string; conversationId: string; epoch: number; settle: (value: boolean, epoch?: number) => void };

/** Opening a notebook resolves before React must commit its new hook props.
 * Wait for that commit, then call the current render's existing session owner. */
export function useMemorySourceSession({ actorId, notebookId, sessionSignal, workspaceEpoch, currentNotebookId, currentEpoch, openSession, reportError }: {
  actorId: string | null;
  notebookId: string | null;
  sessionSignal: AbortSignal;
  workspaceEpoch: number;
  currentNotebookId: () => string | null;
  currentEpoch: () => number;
  openSession: (id: string) => Promise<boolean>;
  reportError: (error: unknown) => void;
}) {
  const ownerRef = useRef<Owner>({ actorId, signal: sessionSignal });
  if (ownerRef.current.actorId !== actorId || ownerRef.current.signal !== sessionSignal) ownerRef.current = { actorId, signal: sessionSignal };
  const owner = ownerRef.current;
  const latest = useRef({ currentNotebookId, currentEpoch, openSession, reportError });
  latest.current = { currentNotebookId, currentEpoch, openSession, reportError };
  const [request, setRequest] = useState<Request | null>(null);
  const pending = useRef<Request | null>(null);
  const active = useRef<Request | null>(null);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; pending.current?.settle(false); active.current?.settle(false); pending.current = null; active.current = null; };
  }, []);
  useEffect(() => {
    if (active.current && active.current.owner !== owner) { active.current.settle(false); active.current = null; }
    if (!request || pending.current !== request) return;
    if (request.owner !== owner || owner.signal.aborted || latest.current.currentEpoch() !== request.epoch || latest.current.currentNotebookId() !== request.notebookId) {
      pending.current = null;
      request.settle(false);
      return;
    }
    if (notebookId !== request.notebookId) return;
    pending.current = null;
    active.current = request;
    const opening = latest.current.openSession(request.conversationId);
    const epoch = latest.current.currentEpoch();
    const owns = () => mounted.current && ownerRef.current === owner && !owner.signal.aborted && latest.current.currentEpoch() === epoch && latest.current.currentNotebookId() === request.notebookId;
    void opening.then((opened) => request.settle(Boolean(opened && owns()), epoch), (error) => { if (owns()) latest.current.reportError(error); request.settle(false, epoch); }).finally(() => {
      if (active.current === request) active.current = null;
    });
  }, [request, notebookId, owner, workspaceEpoch]);

  function open(targetNotebookId: string, conversationId: string): Promise<OpenedSession> {
    if (!mounted.current || !owner.actorId || ownerRef.current !== owner || owner.signal.aborted) return Promise.resolve({ opened: false, epoch: latest.current.currentEpoch() });
    pending.current?.settle(false);
    active.current?.settle(false);
    return new Promise((resolve) => {
      const epoch = latest.current.currentEpoch();
      const settle = (opened: boolean, committedEpoch = epoch) => resolve({ opened, epoch: committedEpoch });
      const next = { owner, notebookId: targetNotebookId, conversationId, epoch, settle };
      pending.current = next;
      setRequest(next);
    });
  }
  return { open };
}
