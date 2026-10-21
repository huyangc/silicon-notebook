"use client";

// 「退出群组」的确认面板(群组页设置页签里的「退出群组」按钮打开)。
//
// 退群不删除任何记忆,但经本群组才读得到的笔记本退出后就看不到了,里面本人的记忆也一起
// 看不到。所以确认之前先问服务端(`GET /groups/{id}/membership/leave-preview`):会有几个
// 笔记本、共几条本人的记忆看不到,逐本列出,每本旁边一个「导出」——复用退出共享同一个
// 导出端点(`/notebooks/{id}/memories/export`,只导出本人在这本笔记本里的记忆),导出范围
// 就是这几本。没有会看不到的记忆时如实说一句,不出现导出入口。
//
// 落点与键盘:与「退出共享」面板同一套(`usePanelPlacement` / `useDialogFocus`)——浮层挂到
// body,摆在入口按钮下方(放不下翻到上方);role=dialog + aria-modal;打开时焦点落在「取消」
// (不是破坏性按钮);Tab 只在面板内循环;Escape 关闭(退出请求在途时不关:结果要给人看)。
//
// 遵循「无退路弹窗」契约:「取消」永不禁用,挂死的请求也有出口;在途的退出请求收不回来,
// 由群组页按群组 id 占住那个组的入口按钮(「正在退出…」)直到它落定,失败原因落在那个组的
// 按钮旁。导出同理:在途的导出按笔记本 id 记在群组页那一层(`useLeaveExports`),面板取消
// 再打开也认得它。导出端点在发出第一个字节之前就在一个快照里读完全部条目(读权在同一个
// 快照里复核),所以退出不会让文件缺页;但退出若抢在导出的读权复核之前落定,导出会失败——
// 因此任何一本还在导出时都不能确认退出。导出的结果落在导出按钮自身(已导出 / 导出失败),
// 超过上限的原因写在那一行下面。
//
// 告知读不到就不放行确认(与退出共享「不盲退」一致);告知回 404 说明此刻已经不是这个组的
// 成员(比如在别的标签页退过了),如实说出来、请群组页刷新清单,不让人「稍后重试」。
//
// 所有者已删除、正在一天删除窗口里的笔记本也会列出(`pending_delete`):名字后标「即将删除」,
// 行下说明退群后就无法再导出——它的「导出」与区块里那个是同一个端点,窗口期内照样可用。

import { useCallback, useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";

import { useCopyResult } from "./copy-result.ts";
import { saveBlobAsFile } from "./download-file.ts";
import { httpErrorStatus } from "./errors.ts";
import { getGroupLeavePreview, type GroupLeavePreview } from "./group-api.ts";
import { LEAVE_PENDING_TAG, leavePendingDeleteText } from "./notebook-deletion-copy.ts";
import { exportOwnMemories } from "./notebook-exit-api.ts";
import { usePanelPlacement } from "./notebook-exit-panel.tsx";
import { useDialogFocus } from "./use-dialog-focus.ts";
import type { ExitAnchor } from "./use-notebook-exit.ts";

export const LEAVE_PREVIEW_UNAVAILABLE_TEXT = "暂时无法确认退出后会看不到哪些记忆，请稍后重试。";
export const LEAVE_ALREADY_GONE_TEXT = "你已经不在这个群组里了。";
export const LEAVE_WAIT_FOR_EXPORT_TEXT = "正在导出，导出完成后才能退出。";
export const LEAVE_EXPORT_TOO_LARGE_TEXT =
  "这个笔记本里你的记忆内容太多，超过了一次导出的上限，没能导出。";

/** 有会看不到的记忆时的那句话。数字都取自服务端的告知。 */
export const leaveHiddenText = (notebookCount: number, memoryCount: number): string =>
  `退出后，你将看不到 ${notebookCount} 个笔记本里你的 ${memoryCount} 条记忆。`;

/** 重新加入后能否再看到取决于那时的身份(邀请链接以普通成员加入,只共享给组管理员的笔记本
 *  就读不到),所以只说带条件的那一句。 */
export const LEAVE_KEPT_TEXT =
  "记忆不会删除。重新加入本群组后，以你那时的身份仍能读到的笔记本里，记忆会再次显示。退出前可以先导出为文件保存。";
export const LEAVE_NOTHING_HIDDEN_TEXT = "你的记忆不会因为退出而看不到。";

type PreviewState =
  | { status: "loading" }
  | { status: "unavailable" }
  | { status: "gone" }
  | { status: "ready"; preview: GroupLeavePreview };

/** 退群面板的导出状态。由群组页持有(面板取消再打开、换组再回来都还在),按笔记本 id 记。 */
export function useLeaveExports() {
  const [exporting, setExporting] = useState<ReadonlySet<string>>(new Set());
  const [reasons, setReasons] = useState<ReadonlyMap<string, string>>(new Map());
  const exportingRef = useRef<Set<string>>(new Set());
  const result = useCopyResult();
  const { report } = result;

  const exportNotebook = useCallback(async (notebookId: string) => {
    // 同步闸:state 更新是异步的,快速双击会在它生效前发出第二个下载。
    if (exportingRef.current.has(notebookId)) return;
    exportingRef.current.add(notebookId);
    setExporting(new Set(exportingRef.current));
    setReasons((current) => {
      const next = new Map(current);
      next.delete(notebookId);
      return next;
    });
    const key = `export:${notebookId}`;
    try {
      const { blob, filename } = await exportOwnMemories(notebookId);
      saveBlobAsFile(blob, filename);
      report(key, true);
    } catch (error) {
      if (httpErrorStatus(error) === 413) {
        setReasons((current) => new Map(current).set(notebookId, LEAVE_EXPORT_TOO_LARGE_TEXT));
      }
      report(key, false);
    } finally {
      exportingRef.current.delete(notebookId);
      setExporting(new Set(exportingRef.current));
    }
  }, [report]);

  return {
    /** 正在导出的笔记本 id。 */
    exporting,
    /** 上一次导出失败、且有可以说给用户听的原因时,按笔记本 id 记的原因。 */
    reasons,
    resultFor: (notebookId: string) => result.resultFor(`export:${notebookId}`),
    exportNotebook,
  };
}

export type LeaveExports = ReturnType<typeof useLeaveExports>;

export type GroupLeaveDialogProps = {
  groupId: string;
  anchor: ExitAnchor;
  /** 这个组的退出请求在途(含取消面板之后仍在途的那一次)。 */
  leaving: boolean;
  /** 这个组上一次退出没有成功的原因(已翻成人话),落在确认按钮旁。 */
  failure: string;
  /** 群组页持有的导出状态(`useLeaveExports`)。 */
  exports: LeaveExports;
  onCancel: () => void;
  onConfirm: () => void;
  /** 告知回 404:此刻已经不是这个组的成员,群组页据此刷新清单。 */
  onGone: () => void;
};

function GroupLeavePanel({
  groupId, anchor, leaving, failure, exports, onCancel, onConfirm, onGone,
}: GroupLeaveDialogProps) {
  const panelRef = useRef<HTMLDivElement | null>(null);
  const cancelRef = useRef<HTMLButtonElement | null>(null);
  const leadId = useId();
  const hintId = useId();
  const [state, setState] = useState<PreviewState>({ status: "loading" });
  const [attempt, setAttempt] = useState(0);
  const onGoneRef = useRef(onGone);
  onGoneRef.current = onGone;

  usePanelPlacement(panelRef, anchor);
  useDialogFocus({
    containerRef: panelRef,
    active: true,
    initialFocusRef: cancelRef,
    restoreFocus: true,
    onEscape: () => { if (!leaving) onCancel(); },
  });

  useEffect(() => {
    const controller = new AbortController();
    setState({ status: "loading" });
    getGroupLeavePreview(groupId, controller.signal)
      .then((preview) => {
        if (!controller.signal.aborted) setState({ status: "ready", preview });
      })
      .catch((error) => {
        if (controller.signal.aborted) return;
        if (httpErrorStatus(error) === 404) {
          setState({ status: "gone" });
          onGoneRef.current();
          return;
        }
        setState({ status: "unavailable" });
      });
    return () => controller.abort();
  }, [groupId, attempt]);

  const ready = state.status === "ready" ? state.preview : null;
  const hidden = ready !== null && ready.memory_count > 0;
  const exportBusy = exports.exporting.size > 0;
  const confirmDisabled = ready === null || leaving || exportBusy;

  return (
    <div
      ref={panelRef}
      className="notebook-exit-panel group-leave-panel"
      role="dialog"
      aria-modal="true"
      aria-label="退出群组"
      aria-describedby={ready ? `${leadId} ${hintId}` : leadId}
      tabIndex={-1}
    >
      {state.status === "loading" && (
        <p id={leadId} className="notebook-exit-lead">正在确认退出后会看不到哪些记忆…</p>
      )}
      {state.status === "unavailable" && (
        <p id={leadId} className="notebook-exit-lead" role="alert">{LEAVE_PREVIEW_UNAVAILABLE_TEXT}</p>
      )}
      {state.status === "gone" && (
        <p id={leadId} className="notebook-exit-lead" role="alert">{LEAVE_ALREADY_GONE_TEXT}</p>
      )}
      {ready && !hidden && (
        <>
          <p id={leadId} className="notebook-exit-lead">退出后，你将失去经本群组获得的知识库访问权。</p>
          <p id={hintId} className="notebook-exit-hint">{LEAVE_NOTHING_HIDDEN_TEXT}</p>
        </>
      )}
      {ready && hidden && (
        <>
          <p id={leadId} className="notebook-exit-lead">
            {leaveHiddenText(ready.notebook_count, ready.memory_count)}
          </p>
          <p id={hintId} className="notebook-exit-hint">{LEAVE_KEPT_TEXT}</p>
          <ul className="group-leave-list" aria-label="退出后会看不到的笔记本">
            {ready.notebooks.map((notebook) => {
              const name = notebook.name || "未命名笔记本";
              const busy = exports.exporting.has(notebook.notebook_id);
              const result = exports.resultFor(notebook.notebook_id);
              const reason = exports.reasons.get(notebook.notebook_id) ?? "";
              return (
                <li key={notebook.notebook_id} className="group-leave-row">
                  <span className="group-leave-name">
                    {name}
                    {notebook.pending_delete && <span className="group-leave-pending-tag">{LEAVE_PENDING_TAG}</span>}
                  </span>
                  <span className="group-leave-count">{notebook.memory_count} 条</span>
                  <button
                    type="button"
                    className={result === "copied"
                      ? "sort-button copy-result-copied"
                      : result === "failed" ? "sort-button copy-result-failed" : "sort-button"}
                    disabled={busy || leaving}
                    aria-label={`导出「${name}」里你的 ${notebook.memory_count} 条记忆`}
                    onClick={() => { void exports.exportNotebook(notebook.notebook_id); }}
                  >
                    {busy ? "正在导出…" : result === "copied" ? "已导出" : result === "failed" ? "导出失败" : "导出"}
                  </button>
                  {notebook.pending_delete && notebook.delete_after && (
                    <p className="notebook-exit-hint group-leave-row-note">{leavePendingDeleteText(notebook.delete_after)}</p>
                  )}
                  {reason && <p className="notebook-exit-error group-leave-row-note" role="alert">{reason}</p>}
                </li>
              );
            })}
          </ul>
        </>
      )}
      {ready && exportBusy && !leaving && (
        <p className="notebook-exit-hint" role="status">{LEAVE_WAIT_FOR_EXPORT_TEXT}</p>
      )}
      {failure && <p className="notebook-exit-error" role="alert">{failure}</p>}
      <div className="notebook-exit-actions">
        {state.status === "unavailable" && (
          <button type="button" className="sort-button" onClick={() => setAttempt((n) => n + 1)}>重试</button>
        )}
        {/* 取消永不禁用:不因忙碌而失效,挂死的请求才有出口。 */}
        <button ref={cancelRef} type="button" className="sort-button" onClick={onCancel}>取消</button>
        {(state.status === "loading" || state.status === "ready") && (
          <button
            type="button"
            className="new-pill danger-pill"
            disabled={confirmDisabled}
            onClick={onConfirm}
          >
            {leaving ? "正在退出…" : "确认退出"}
          </button>
        )}
      </div>
    </div>
  );
}

export function GroupLeaveDialog(props: GroupLeaveDialogProps) {
  if (typeof document === "undefined") return null;
  return createPortal(<GroupLeavePanel {...props} />, document.body);
}
