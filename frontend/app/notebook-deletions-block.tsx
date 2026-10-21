"use client";

// 笔记本列表顶部的「即将永久删除的笔记本」区块,以及同一套逐本控件(说明框也用)。
//
// 数据只来自 `GET /me/notebook-deletions`(与我有关的、正在删除窗口里的笔记本:我是主人,
// 或我在那里有记忆);读不到就如实说「暂时读不到」,不当作「没有」(fail-closed)。每行:
//
//   * 主人:「撤销删除」;
//   * 自己在这里有记忆(主人也算):「导出我的记忆(N 条)」与「转移到我的笔记本」——转移
//     是**移动**,点之前先看到「转移后不会回来」的提示,再挑一本自己的笔记本确认;
//   * 截止已过(本地时钟):只说「删除时间已到，正在永久删除」,没有控件,稍后重新拉取。
//
// 每个控件的结果都落在它自己旁边(仓库 Interactive feedback 规则):撤销中… / 已撤销 /
// 删除时间已到 / 不存在;导出中… / 已导出 / 导出失败 + 原因;转移中… / 结果条数。
// 在途时控件禁用,防止重复提交;导出在途时撤销仍可点(撤销不影响已经开始的导出)。
//
// 一行一旦有了结果(已撤销、删除时间已到、转移完成……),服务端下一次就可能不再列出它;
// 区块把这样的行按原样再留 `SETTLED_ROW_HOLD_MS`,控件全部停用,好让结果看得见,
// 而不是随着重新拉取一闪就没。

import { useCallback, useEffect, useRef, useState } from "react";

import { COPY_RESULT_HOLD_MS, useCopyResult } from "./copy-result.ts";
import { saveBlobAsFile } from "./download-file.ts";
import {
  cancelNotebookDeletion,
  exportDeletedNotebookMemories,
  listNotebookDeletions,
  transferOutOfDeletedNotebook,
  type NotebookDeletionNotice,
} from "./notebook-deletion-api.ts";
import {
  CANCEL_DONE_TEXT,
  CANCEL_DUE_TEXT,
  CANCEL_FAILED_TEXT,
  CANCEL_GONE_TEXT,
  DELETION_DUE_TEXT,
  DELETIONS_BLOCK_TITLE,
  DELETIONS_UNAVAILABLE_TEXT,
  EXPORT_CLOSED_TEXT,
  EXPORT_FAILED_TEXT,
  EXPORT_NOTHING_TEXT,
  TRANSFER_CLOSED_TEXT,
  TRANSFER_FAILED_TEXT,
  TRANSFER_GONE_TEXT,
  TRANSFER_LABEL,
  TRANSFER_NO_TARGET_TEXT,
  TRANSFER_WARNING_TEXT,
  deletionDue,
  deletionNoticeText,
  exportLabel,
  transferDoneText,
  untransferableText,
} from "./notebook-deletion-copy.ts";
import { LEAVE_EXPORT_TOO_LARGE_TEXT } from "./group-leave-dialog.tsx";
import type { NotebookSummary } from "./workspace-model.ts";

export type DeletionTarget = Pick<NotebookSummary, "id" | "name">;

/** 铃铛点进来、要定位某一本:`seq` 每次点击都变,同一本再点一次也会再定位。 */
export type DeletionFocusRequest = { id: string; seq: number };

type DeletionsState =
  | { status: "loading"; items: NotebookDeletionNotice[] }
  | { status: "ready"; items: NotebookDeletionNotice[] }
  | { status: "unavailable"; items: NotebookDeletionNotice[] };

// setTimeout 的上限(约 24.8 天);更远的截止不需要现在就排定时器。
const MAX_TIMER_MS = 2_147_000_000;

/** 有了结果、服务端已不再列出的一行,还要留在原处多久(只读,控件全部停用)。 */
export const SETTLED_ROW_HOLD_MS = 6000;

/** 铃铛定位的那一行高亮多久。 */
const FOCUS_HIGHLIGHT_MS = 4000;

/** 区块的数据:`refreshKey` 变化(铃铛里删除通知的集合变了、列表刷新了)就重新拉取;
 *  最近的截止一到,本地先改成「正在永久删除」,再拉一次服务端的真相。 */
export function useNotebookDeletions(enabled: boolean, refreshKey: string) {
  const [state, setState] = useState<DeletionsState>({ status: "loading", items: [] });
  const [nowMs, setNowMs] = useState(() => Date.now());
  const [attempt, setAttempt] = useState(0);
  const reload = useCallback(() => setAttempt((n) => n + 1), []);

  useEffect(() => {
    if (!enabled) {
      setState({ status: "ready", items: [] });
      return undefined;
    }
    const controller = new AbortController();
    // 重新拉取期间照旧显示上一次的结果(包括「读不到」),不让区块闪没再闪回。
    setState((current) => (
      current.status === "unavailable" ? current : { status: "loading", items: current.items }
    ));
    listNotebookDeletions(controller.signal)
      .then((items) => {
        if (controller.signal.aborted) return;
        setNowMs(Date.now());
        setState({ status: "ready", items });
      })
      .catch(() => {
        if (!controller.signal.aborted) setState((current) => ({ status: "unavailable", items: current.items }));
      });
    return () => controller.abort();
  }, [enabled, refreshKey, attempt]);

  // 最近一本的截止:到点先切显示,一秒后向服务端要真相(扫尾有自己的节拍,迟一点也只会
  // 多显示一会儿「正在永久删除」)。
  const next = state.items
    .map((item) => Date.parse(item.delete_after))
    .filter((at) => Number.isFinite(at) && at > nowMs)
    .sort((a, b) => a - b)[0];
  useEffect(() => {
    if (next === undefined) return undefined;
    const wait = Math.min(MAX_TIMER_MS, Math.max(0, next - Date.now()) + 1000);
    const timer = setTimeout(() => {
      setNowMs(Date.now());
      reload();
    }, wait);
    return () => clearTimeout(timer);
  }, [next, reload]);

  return { state, nowMs, reload };
}

export type NotebookDeletions = ReturnType<typeof useNotebookDeletions>;

type CancelState =
  | { kind: "idle" } | { kind: "busy" } | { kind: "done" }
  | { kind: "due" } | { kind: "gone" } | { kind: "failed" };

type TransferState =
  | { kind: "closed" }
  | { kind: "choosing"; targetId: string }
  | { kind: "busy"; targetId: string }
  | { kind: "result"; text: string; isFailure: boolean };

/** 一本笔记本的全部控件(区块的一行、说明框的正文都用它)。 */
export function DeletionNoticeControls({
  notice,
  nowMs,
  targets,
  onCancelled,
  onChanged,
  onSettled,
  lingering = false,
}: {
  notice: NotebookDeletionNotice;
  nowMs: number;
  /** 能接收转移的笔记本:我自己的、正常可用的(调用方已去掉这一本与镜像)。 */
  targets: readonly DeletionTarget[];
  /** 撤销成功:列表把这本笔记本重新放回来。 */
  onCancelled: (notebook: NotebookSummary) => void;
  /** 这一本的状态变了(撤销 / 截止 / 转移),区块重新拉取。 */
  onChanged: () => void;
  /** 这一行有了结果:即使服务端不再列出它,也先留着,让结果看得见。 */
  onSettled?: () => void;
  /** 服务端已不再列出、只是为了让结果看得见而留着的行:只读。 */
  lingering?: boolean;
}) {
  const id = notice.notebook_id;
  const name = notice.name || "未命名笔记本";
  const [cancel, setCancel] = useState<CancelState>({ kind: "idle" });
  const [exporting, setExporting] = useState(false);
  const [exportReason, setExportReason] = useState("");
  const exportingRef = useRef(false);
  const exportResult = useCopyResult();
  const [transfer, setTransfer] = useState<TransferState>({ kind: "closed" });
  const mounted = useRef(true);
  const timers = useRef<ReturnType<typeof setTimeout>[]>([]);
  useEffect(() => {
    mounted.current = true;
    const pending = timers.current;
    return () => {
      mounted.current = false;
      pending.forEach(clearTimeout);
    };
  }, []);
  const later = (fn: () => void, ms: number) => {
    timers.current.push(setTimeout(() => { if (mounted.current) fn(); }, ms));
  };

  const due = deletionDue(notice.delete_after, nowMs) || cancel.kind === "due";
  const untransferable = Math.max(0, notice.memory_count - notice.transferable_count);
  const busy = cancel.kind === "busy" || transfer.kind === "busy" || lingering;

  async function runCancel() {
    if (cancel.kind === "busy" || cancel.kind === "done") return;
    setCancel({ kind: "busy" });
    try {
      const outcome = await cancelNotebookDeletion(id);
      if (!mounted.current) return;
      onSettled?.();
      if (outcome.kind === "cancelled") {
        setCancel({ kind: "done" });
        onCancelled(outcome.notebook);
        // 结果在按钮旁停一会儿再收起这一行(否则行一消失,「已撤销」根本来不及看见)。
        later(onChanged, COPY_RESULT_HOLD_MS);
        return;
      }
      setCancel({ kind: outcome.kind });
      later(onChanged, COPY_RESULT_HOLD_MS);
    } catch {
      if (mounted.current) setCancel({ kind: "failed" });
    }
  }

  async function runExport() {
    if (exportingRef.current) return;
    exportingRef.current = true;
    setExporting(true);
    setExportReason("");
    const key = `export:${id}`;
    try {
      const outcome = await exportDeletedNotebookMemories(id);
      if (!mounted.current) return;
      if (outcome.kind === "exported") {
        saveBlobAsFile(outcome.blob, outcome.filename);
        exportResult.report(key, true);
        return;
      }
      exportResult.report(key, false);
      setExportReason(
        outcome.kind === "closed" ? EXPORT_CLOSED_TEXT
          : outcome.kind === "too_large" ? LEAVE_EXPORT_TOO_LARGE_TEXT
            : outcome.kind === "nothing" ? EXPORT_NOTHING_TEXT
              : EXPORT_FAILED_TEXT,
      );
      if (outcome.kind === "closed" || outcome.kind === "nothing") {
        onSettled?.();
        onChanged();
      }
    } finally {
      exportingRef.current = false;
      if (mounted.current) setExporting(false);
    }
  }

  async function runTransfer(targetId: string) {
    if (transfer.kind === "busy") return;
    const target = targets.find((t) => t.id === targetId);
    if (!target) return;
    setTransfer({ kind: "busy", targetId });
    const outcome = await transferOutOfDeletedNotebook(id, targetId);
    if (!mounted.current) return;
    if (outcome.kind === "transferred") {
      const moved = outcome.results.filter((r) => r.ok).length;
      const failed = outcome.results.length - moved;
      setTransfer({ kind: "result", text: transferDoneText(moved, failed, target.name), isFailure: failed > 0 });
      onSettled?.();
      onChanged();
      return;
    }
    setTransfer({
      kind: "result",
      isFailure: true,
      text: outcome.kind === "closed" ? TRANSFER_CLOSED_TEXT
        : outcome.kind === "gone" ? TRANSFER_GONE_TEXT : TRANSFER_FAILED_TEXT,
    });
    if (outcome.kind !== "failed") {
      onSettled?.();
      onChanged();
    }
  }

  if (due && cancel.kind !== "done") {
    return (
      <>
        <p className="notebook-deletion-text" role="status">{DELETION_DUE_TEXT}</p>
        {/* 撤销撞上截止(409):原因落在原来那颗按钮的位置。 */}
        {cancel.kind === "due" && <p className="notebook-deletion-error" role="alert">{CANCEL_DUE_TEXT}</p>}
      </>
    );
  }

  const exportState = exportResult.resultFor(`export:${id}`);
  const canTransfer = notice.transferable_count > 0;

  return (
    <>
      <p className="notebook-deletion-text">{deletionNoticeText(notice)}</p>
      <div className="notebook-deletion-actions">
        {notice.is_owner && (
          <button
            type="button"
            className={cancel.kind === "done" ? "new-pill copy-result-copied" : "new-pill"}
            disabled={busy || cancel.kind === "done"}
            onClick={() => { void runCancel(); }}
          >
            {cancel.kind === "busy" ? "撤销中…" : cancel.kind === "done" ? "已撤销" : "撤销删除"}
          </button>
        )}
        {notice.memory_count > 0 && (
          <button
            type="button"
            className={exportState === "copied"
              ? "sort-button copy-result-copied"
              : exportState === "failed" ? "sort-button copy-result-failed" : "sort-button"}
            disabled={exporting || busy}
            aria-label={`导出「${name}」里你的 ${notice.memory_count} 条记忆`}
            onClick={() => { void runExport(); }}
          >
            {exporting ? "导出中…" : exportState === "copied" ? "已导出" : exportState === "failed" ? "导出失败" : exportLabel(notice.memory_count)}
          </button>
        )}
        {canTransfer && transfer.kind !== "choosing" && transfer.kind !== "busy" && (
          <button
            type="button"
            className="sort-button"
            disabled={busy || cancel.kind === "done"}
            onClick={() => setTransfer({ kind: "choosing", targetId: targets[0]?.id ?? "" })}
          >
            {TRANSFER_LABEL}
          </button>
        )}
      </div>
      {cancel.kind === "done" && <p className="notebook-deletion-note" role="status">{CANCEL_DONE_TEXT}</p>}
      {cancel.kind === "gone" && <p className="notebook-deletion-error" role="alert">{CANCEL_GONE_TEXT}</p>}
      {cancel.kind === "failed" && <p className="notebook-deletion-error" role="alert">{CANCEL_FAILED_TEXT}</p>}
      {exportReason && <p className="notebook-deletion-error" role="alert">{exportReason}</p>}
      {untransferable > 0 && notice.memory_count > 0 && (
        <p className="notebook-deletion-note">{untransferableText(untransferable)}</p>
      )}
      {(transfer.kind === "choosing" || transfer.kind === "busy") && (
        <div className="notebook-deletion-transfer" role="group" aria-label={TRANSFER_LABEL}>
          <p className="notebook-deletion-warning">{TRANSFER_WARNING_TEXT}</p>
          {targets.length === 0 ? (
            <p className="notebook-deletion-note">{TRANSFER_NO_TARGET_TEXT}</p>
          ) : (
            <label className="notebook-deletion-target">
              <span>转移到</span>
              <select
                value={transfer.targetId}
                disabled={transfer.kind === "busy" || lingering}
                onChange={(event) => setTransfer({ kind: "choosing", targetId: event.target.value })}
              >
                {targets.map((target) => (
                  <option key={target.id} value={target.id}>{target.name || "未命名笔记本"}</option>
                ))}
              </select>
            </label>
          )}
          <div className="notebook-deletion-actions">
            {/* 取消永不禁用:在途的转移收不回来,但面板总能关上。 */}
            <button type="button" className="sort-button" onClick={() => setTransfer({ kind: "closed" })}>
              取消
            </button>
            {targets.length > 0 && (
              <button
                type="button"
                className="new-pill danger-pill"
                disabled={transfer.kind === "busy" || lingering || !transfer.targetId}
                onClick={() => { void runTransfer(transfer.targetId); }}
              >
                {transfer.kind === "busy" ? "转移中…" : "确认转移"}
              </button>
            )}
          </div>
        </div>
      )}
      {transfer.kind === "result" && (
        <p
          className={transfer.isFailure ? "notebook-deletion-error" : "notebook-deletion-note"}
          role={transfer.isFailure ? "alert" : "status"}
        >
          {transfer.text}
        </p>
      )}
    </>
  );
}

/** 笔记本列表顶部的区块。没有与我有关的删除、且读得到时不渲染任何东西。 */
export function NotebookDeletionsBlock({
  deletions,
  targets,
  focus,
  onCancelled,
}: {
  deletions: NotebookDeletions;
  /** 我自己的、正常可用的笔记本(转移的目的地候选)。 */
  targets: readonly DeletionTarget[];
  /** 铃铛点进来时要定位的那一本。 */
  focus: DeletionFocusRequest | null;
  onCancelled: (notebook: NotebookSummary) => void;
}) {
  const { state, nowMs, reload } = deletions;

  // 有了结果的行:notebook_id → 当时的通知。服务端不再列出时照它再显示一会儿。
  const [settled, setSettled] = useState<Record<string, NotebookDeletionNotice>>({});
  const settleTimers = useRef<ReturnType<typeof setTimeout>[]>([]);
  useEffect(() => {
    const pending = settleTimers.current;
    return () => pending.forEach(clearTimeout);
  }, []);
  const settle = useCallback((notice: NotebookDeletionNotice) => {
    const id = notice.notebook_id;
    setSettled((current) => ({ ...current, [id]: notice }));
    settleTimers.current.push(setTimeout(() => {
      setSettled((current) => {
        if (!(id in current)) return current;
        const rest = { ...current };
        delete rest[id];
        return rest;
      });
    }, SETTLED_ROW_HOLD_MS));
  }, []);
  const listed = new Set(state.items.map((notice) => notice.notebook_id));
  const rows = [
    ...state.items.map((notice) => ({ notice, lingering: false })),
    ...Object.values(settled)
      .filter((notice) => !listed.has(notice.notebook_id))
      .map((notice) => ({ notice, lingering: true })),
  ];

  // 铃铛定位:每次点击(seq)只滚动、聚焦一次,之后的重新拉取不再抢焦点;高亮一会儿就收。
  const focusId = focus?.id ?? null;
  const focusSeq = focus?.seq ?? null;
  const focusRef = useRef<HTMLLIElement | null>(null);
  const handledSeq = useRef<number | null>(null);
  const [highlightId, setHighlightId] = useState<string | null>(null);
  const focusReady = focusId !== null && listed.has(focusId);
  useEffect(() => {
    if (focusSeq === null || !focusReady || handledSeq.current === focusSeq || !focusRef.current) return;
    handledSeq.current = focusSeq;
    focusRef.current.scrollIntoView?.({ block: "nearest" });
    focusRef.current.focus?.();
    setHighlightId(focusId);
  }, [focusId, focusSeq, focusReady]);
  useEffect(() => {
    if (highlightId === null) return undefined;
    const timer = setTimeout(() => setHighlightId(null), FOCUS_HIGHLIGHT_MS);
    return () => clearTimeout(timer);
  }, [highlightId, focusSeq]);

  if (state.status !== "unavailable" && rows.length === 0) return null;
  return (
    <section className="notebook-deletions" aria-label={DELETIONS_BLOCK_TITLE}>
      <h2>{DELETIONS_BLOCK_TITLE}</h2>
      {state.status === "unavailable" && (
        <div className="notebook-deletion-unavailable">
          <p className="notebook-deletion-error" role="alert">{DELETIONS_UNAVAILABLE_TEXT}</p>
          <button type="button" className="sort-button" onClick={reload}>重试</button>
        </div>
      )}
      {rows.length > 0 && (
        <ul className="notebook-deletion-list">
          {rows.map(({ notice, lingering }) => (
            <li
              key={notice.notebook_id}
              ref={notice.notebook_id === focusId ? focusRef : undefined}
              tabIndex={notice.notebook_id === focusId ? -1 : undefined}
              className={notice.notebook_id === highlightId ? "notebook-deletion-row is-focused" : "notebook-deletion-row"}
            >
              <DeletionNoticeControls
                notice={notice}
                nowMs={nowMs}
                targets={targets.filter((target) => target.id !== notice.notebook_id)}
                onCancelled={onCancelled}
                onChanged={reload}
                onSettled={() => settle(notice)}
                lingering={lingering}
              />
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
