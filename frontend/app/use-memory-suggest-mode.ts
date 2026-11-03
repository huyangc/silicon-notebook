"use client";

import { useEffect, useRef, useState } from "react";
import { setMemoryMode, type AuthUser, type MemoryMode } from "./auth";
import { humanizedError, toUserMessage } from "./errors";
import type { RootModalCoordinator, RootModalLease } from "./use-root-modal-coordinator";

export const MEMORY_SUGGEST_NOTICE_MS = 5000;

export type MemorySuggestModeView = Readonly<{
  actorId: string | null;
  actorGeneration: number;
  mode: MemoryMode;
  busy: boolean;
  notice: string;
  failureMessage: string;
  deletedRevision: number;
  confirmationLease: RootModalLease<"memory-suggest-close"> | null;
}>;

type ResultState = Omit<MemorySuggestModeView, "actorId" | "mode" | "confirmationLease">;
type Owner = { actorId: string | null; signal: AbortSignal; generation: number };

function emptyResult(actorGeneration: number): ResultState {
  return { actorGeneration, busy: false, notice: "", failureMessage: "", deletedRevision: 0 };
}

/** Actor-wide writes survive notebook navigation, but never a session/actor change. */
export function useMemorySuggestMode({
  user,
  sessionSignal,
  onUserChanged,
  modals,
}: {
  user: AuthUser | null;
  sessionSignal: AbortSignal;
  onUserChanged: (user: AuthUser) => void;
  modals: Pick<RootModalCoordinator, "captureActorOwner" | "issue" | "publish" | "owns" | "requestClose">;
}) {
  const ownerRef = useRef<Owner>({ actorId: user?.id ?? null, signal: sessionSignal, generation: 0 });
  if (ownerRef.current.actorId !== (user?.id ?? null) || ownerRef.current.signal !== sessionSignal) {
    ownerRef.current = { actorId: user?.id ?? null, signal: sessionSignal, generation: ownerRef.current.generation + 1 };
  }
  const owner = ownerRef.current;
  const [result, setResult] = useState<ResultState>(() => emptyResult(owner.generation));
  const visibleResult = result.actorGeneration === owner.generation ? result : emptyResult(owner.generation);
  const operationRef = useRef<object | null>(null);
  const operationOwnerRef = useRef<Owner | null>(null);
  const [confirmationLease, setConfirmationLease] = useState<RootModalLease<"memory-suggest-close"> | null>(null);
  const confirmationLeaseRef = useRef(confirmationLease);
  const confirmationOwnerRef = useRef<Owner | null>(null);
  const modalsRef = useRef(modals);
  modalsRef.current = modals;
  const onUserChangedRef = useRef(onUserChanged);
  onUserChangedRef.current = onUserChanged;
  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);
  useEffect(() => {
    if (confirmationOwnerRef.current && confirmationOwnerRef.current !== owner && confirmationLeaseRef.current) {
      modalsRef.current.requestClose("memory-suggest-close", "button");
    }
  }, [owner]);
  useEffect(() => {
    if (!result.notice && !result.failureMessage) return;
    const timer = window.setTimeout(() => {
      setResult((current) => current === result ? { ...current, notice: "", failureMessage: "" } : current);
    }, MEMORY_SUGGEST_NOTICE_MS);
    return () => window.clearTimeout(timer);
  }, [result]);

  function openConfirmation() {
    if (!owner.actorId || ownerRef.current !== owner || owner.signal.aborted || (operationRef.current && operationOwnerRef.current === owner)) return;
    if (confirmationLeaseRef.current && modals.owns(confirmationLeaseRef.current)) return;
    const lease = modals.issue("memory-suggest-close", modals.captureActorOwner());
    if (!lease || lease.owner.actorId !== owner.actorId || !modals.publish(lease)) return;
    confirmationLeaseRef.current = lease;
    confirmationOwnerRef.current = owner;
    setConfirmationLease(lease);
  }

  function closeConfirmation(lease: RootModalLease<"memory-suggest-close">) {
    if (confirmationLeaseRef.current?.issue !== lease.issue || !modals.owns(lease)) return;
    modals.requestClose("memory-suggest-close", "button");
  }

  // Presentation close clears only its payload, never the actor-wide write guard.
  function clearConfirmation() {
    confirmationLeaseRef.current = null;
    confirmationOwnerRef.current = null;
    setConfirmationLease(null);
  }

  async function changeMode(mode: MemoryMode, deleteUnadopted = false): Promise<boolean> {
    if (!owner.actorId || ownerRef.current !== owner || owner.signal.aborted || (operationRef.current && operationOwnerRef.current === owner)) return false;
    const operation = {};
    operationRef.current = operation;
    operationOwnerRef.current = owner;
    const isCurrent = () => mountedRef.current && ownerRef.current === owner && !owner.signal.aborted && operationRef.current === operation;
    setResult((current) => ({ ...(current.actorGeneration === owner.generation ? current : emptyResult(owner.generation)), busy: true, notice: "", failureMessage: "" }));
    try {
      const response = await setMemoryMode(mode, deleteUnadopted);
      if (!isCurrent()) return false;
      if (response.user.id !== owner.actorId) throw humanizedError("记忆建议设置返回的用户不一致，请刷新后重试");
      onUserChangedRef.current(response.user);
      const deletedNotice = response.deleted_suggestion_count ? `，已删除 ${response.deleted_suggestion_count} 条未处理的建议` : "";
      const notice = response.user.memory_mode === "suggest" ? "已开启记忆建议" : `已关闭记忆建议${deletedNotice}`;
      setResult((current) => ({
        ...current,
        busy: false,
        notice,
        deletedRevision: current.deletedRevision + (response.deleted_suggestion_count > 0 ? 1 : 0),
      }));
      return true;
    } catch (cause) {
      if (isCurrent()) setResult((current) => ({ ...current, busy: false, failureMessage: toUserMessage(cause, "设置失败，请稍后重试") }));
      return false;
    } finally {
      if (operationRef.current === operation) {
        operationRef.current = null;
        operationOwnerRef.current = null;
      }
    }
  }

  return {
    view: { ...visibleResult, actorId: owner.actorId, mode: user?.memory_mode ?? "manual", confirmationLease: confirmationOwnerRef.current === owner && modals.owns(confirmationLease) ? confirmationLease : null } as MemorySuggestModeView,
    commands: { setMode: changeMode, openConfirmation, closeConfirmation, clearConfirmation },
  };
}
