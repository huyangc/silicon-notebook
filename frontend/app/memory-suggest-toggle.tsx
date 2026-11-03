"use client";

import { useEffect, useRef, useState } from "react";
import type { MemoryMode } from "./auth";
import { requestJson } from "./api-client";
import { humanizedError, toUserMessage } from "./errors";
import { FloatingModalCard } from "./floating-modal-card";
import { MEMORY_PAGE_SIZE, memoryListPath } from "./memory-model";
import type { MemorySuggestModeView } from "./use-memory-suggest-mode";
import { useDialogFocus } from "./use-dialog-focus";
import type { RootModalLease, RootModalView } from "./use-root-modal-coordinator";
import type { PaginatedMemories } from "./workspace-model";

export type MemorySuggestToggleProps = {
  enabled: boolean;
  modelReady: boolean;
  view: MemorySuggestModeView;
  sessionSignal: AbortSignal;
  onSetMode: (mode: MemoryMode, deleteUnadopted?: boolean) => Promise<boolean>;
  modalView: RootModalView;
  onOpenConfirmation: () => void;
  onCloseConfirmation: (lease: RootModalLease<"memory-suggest-close">) => void;
};

export function MemorySuggestToggle({ enabled, modelReady, view, sessionSignal, onSetMode, modalView, onOpenConfirmation, onCloseConfirmation }: MemorySuggestToggleProps) {
  const confirming = modalView.open && Boolean(view.confirmationLease);
  const [deleteUnadopted, setDeleteUnadopted] = useState(false);
  const [count, setCount] = useState<number | null>(null);
  const [countError, setCountError] = useState("");
  const [countRetry, setCountRetry] = useState(0);
  const dialogRef = useRef<HTMLDivElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  const confirmationRef = useRef({ lease: view.confirmationLease, close: onCloseConfirmation });
  confirmationRef.current = { lease: view.confirmationLease, close: onCloseConfirmation };
  useDialogFocus({ containerRef: dialogRef, active: confirming && modalView.topmost, initialFocusRef: cancelRef });
  useEffect(() => {
    return () => {
      const { lease, close } = confirmationRef.current;
      if (lease) close(lease);
    };
  }, []);
  useEffect(() => {
    if (enabled && !sessionSignal.aborted) return;
    const { lease, close } = confirmationRef.current;
    if (lease) close(lease);
  }, [enabled, sessionSignal]);
  useEffect(() => {
    setDeleteUnadopted(false);
    setCount(null);
    setCountError("");
  }, [view.confirmationLease?.issue]);
  useEffect(() => {
    if (!enabled || !confirming || !view.actorId || sessionSignal.aborted) return;
    const controller = new AbortController();
    const abort = () => controller.abort();
    sessionSignal.addEventListener("abort", abort, { once: true });
    setCount(null);
    setCountError("");
    // The mutation deletes across every readable notebook, regardless of the
    // current panel's notebook/search/status filters. Only candidate suggestions count.
    requestJson<PaginatedMemories>(memoryListPath({
      scope: "global", notebookId: null, status: "candidate", origin: "suggested", query: "", offset: 0, limit: MEMORY_PAGE_SIZE,
    }), { tag: "memory", unauthorized: "clear-and-reload", signal: controller.signal })
      .then((page) => {
        if (controller.signal.aborted) return;
        if (!Number.isSafeInteger(page.total_count) || page.total_count < 0) {
          throw humanizedError("建议数量暂不可用，请重试");
        }
        setCount(page.total_count);
      })
      .catch((cause) => { if (!controller.signal.aborted) setCountError(toUserMessage(cause, "读取建议数量失败，请重试")); });
    return () => {
      controller.abort();
      sessionSignal.removeEventListener("abort", abort);
    };
  }, [confirming, countRetry, enabled, sessionSignal, view.actorGeneration, view.actorId, view.confirmationLease?.issue]);

  if (!enabled || !view.actorId) return null;
  const unavailable = !modelReady;
  const disabled = view.busy || sessionSignal.aborted;
  async function confirmClose() {
    const lease = view.confirmationLease;
    if (!lease) return;
    const changed = await onSetMode("manual", deleteUnadopted);
    if (changed) onCloseConfirmation(lease);
  }
  return (
    <div className="memory-suggest-settings">
      <p>
        记忆建议：{view.mode === "suggest" ? "已开启" : "已关闭"} · {" "}
        <button type="button" className="link-button inline-action" disabled={disabled || (view.mode === "manual" && unavailable)} onClick={() => {
          if (view.mode === "manual") void onSetMode("suggest");
          else onOpenConfirmation();
        }}>{view.busy ? "正在设置…" : view.mode === "suggest" ? "关闭" : "开启"}</button>
        {view.notice && <span className="memory-suggest-result" role="status">{view.notice}</span>}
        {view.failureMessage && !confirming && <span className="memory-suggest-result memory-error" role="alert">{view.failureMessage}</span>}
      </p>
      <p>开启后，系统会根据你之后在各笔记本的提问建议可保存的记忆，你采纳后才会生效。</p>
      {unavailable && <p>记忆建议暂不可用：管理员尚未配置模型</p>}
      {confirming && (
        <div ref={dialogRef} className="utility-modal utility-modal-top" role="dialog" aria-modal={modalView.topmost} aria-hidden={!modalView.topmost} inert={modalView.topmost ? undefined : true} aria-label="关闭记忆建议" style={{ zIndex: modalView.zIndex }}>
          <FloatingModalCard storageKey="memory.suggest-mode.window" className="utility-modal-card memory-confirm-card">
            {(floating) => (<>
              <h3 {...floating.dragHandleProps}>关闭记忆建议？</h3>
              <p>关闭后不再生成新建议，已采纳的记忆仍会保留。</p>
              <label>
                <input type="checkbox" checked={deleteUnadopted} disabled={disabled || count === null} onChange={(event) => setDeleteUnadopted(event.target.checked)} />
                {count === null ? countError ? "未能读取未处理的建议数量" : "正在读取未处理的建议数量…" : `同时删除 ${count} 条未处理的建议`}
              </label>
              {countError && <p role="alert">{countError} <button type="button" className="link-button inline-action" disabled={disabled} onClick={() => setCountRetry((current) => current + 1)}>重试</button></p>}
              {view.failureMessage && <p role="alert">{view.failureMessage}</p>}
              <div className="memory-dialog-actions">
                <button ref={cancelRef} type="button" onClick={() => { if (view.confirmationLease) onCloseConfirmation(view.confirmationLease); }}>取消</button>
                <button type="button" disabled={disabled || (deleteUnadopted && count === null)} onClick={() => { void confirmClose(); }}>{view.busy ? "正在关闭…" : "确认关闭"}</button>
              </div>
            </>)}
          </FloatingModalCard>
        </div>
      )}
    </div>
  );
}
