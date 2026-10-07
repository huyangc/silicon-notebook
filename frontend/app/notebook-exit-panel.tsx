"use client";

// 「退出共享」的确认面板。渲染 `use-notebook-exit.ts` 里那一份流程,顶栏按钮与笔记本卡片
// 菜单两个入口共用同一个实例(page.tsx 只渲染一次)。
//
// 落点:入口控件的正下方(下方放不下就翻到上方),四边夹在视口内,页面滚动/窗口缩放时
// 重新量入口再摆(算术见 notebook-exit-placement.ts)。面板是浮层,不推动任何内容;挂到
// body 是因为顶栏有 backdrop-filter(它会成为 fixed 后代的包含块并另起层叠上下文),而目标
// 笔记本选择器也是 fixed 全屏浮层,同样不能被困在顶栏里。
//
// 键盘与读屏:role=dialog + aria-modal,名称「退出共享」,带条数的那句话作描述;打开时焦点
// 进「取消」(不是破坏性按钮),Tab 只在面板内循环,Escape 关闭(有一次结果必须给用户看的
// 请求在途时不关),关闭后焦点还给打开它的控件(见 use-dialog-focus.ts、use-notebook-exit.ts)。
//
// 遵循「无退路弹窗」契约:「取消」永不因忙碌而禁用,挂死的请求也有出口。结果都落在
// 按钮自身或紧邻的文字上,不只发页面顶部的横幅。

import { useCallback, useEffect, useId, useLayoutEffect, useRef, type RefObject } from "react";
import { createPortal } from "react-dom";

import {
  EXIT_CHANGED_TEXT,
  EXIT_CHANGED_TO_EMPTY_TEXT,
  EXIT_UNAVAILABLE_TEXT,
  EXIT_WAIT_FOR_EXPORT_TEXT,
} from "./notebook-exit-copy.ts";
import { placePanel } from "./notebook-exit-placement.ts";
import { DestinationPicker } from "./transfer-picker.tsx";
import { useDialogFocus } from "./use-dialog-focus.ts";
import type { ExitAnchor, NotebookExit } from "./use-notebook-exit.ts";

function usePanelPlacement(ref: RefObject<HTMLDivElement | null>, anchor: ExitAnchor) {
  const place = useCallback(() => {
    const panel = ref.current;
    if (!panel) return;
    const element = anchor.element;
    const box = element && element.isConnected
      ? element.getBoundingClientRect()
      : anchor.box;
    const { left, top } = placePanel(
      box,
      { width: panel.offsetWidth, height: panel.offsetHeight },
      {
        width: document.documentElement.clientWidth || window.innerWidth,
        height: window.innerHeight,
      },
    );
    panel.style.left = `${left}px`;
    panel.style.top = `${top}px`;
  }, [ref, anchor]);

  // 每次渲染后重摆:面板高度会随内容(说明、失败提示)变化。
  useLayoutEffect(place);
  useEffect(() => {
    window.addEventListener("scroll", place, true);
    window.addEventListener("resize", place);
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(place);
    if (observer && ref.current) observer.observe(ref.current);
    return () => {
      window.removeEventListener("scroll", place, true);
      window.removeEventListener("resize", place);
      observer?.disconnect();
    };
  }, [place, ref]);
}

function ExitDialog({ exit }: { exit: NotebookExit }) {
  const flow = exit.flow!;
  const panelRef = useRef<HTMLDivElement | null>(null);
  const cancelRef = useRef<HTMLButtonElement | null>(null);
  const pickerHostRef = useRef<HTMLDivElement | null>(null);
  const leadId = useId();
  const hintId = useId();
  const picking = flow.transfer === "picking";
  // 有一次结果必须给用户看的请求在途:退出请求(含事后核对)、已关掉选择器的转移。
  const resultPending = flow.leaving || flow.verifying || flow.transfer === "working";

  usePanelPlacement(panelRef, flow.anchor);
  useDialogFocus({
    containerRef: panelRef,
    active: !picking,
    initialFocusRef: cancelRef,
    onEscape: () => { if (!resultPending) exit.cancel(); },
  });
  useDialogFocus({
    containerRef: pickerHostRef,
    active: picking,
    onEscape: exit.closeTransfer,
  });

  const empty = flow.count === 0;
  // 本笔记本的导出还在进行:退出和转移都要等它完成(导出按页读取,先删/先移走会让文件
  // 缺掉后面的记忆)。取消不受影响。
  const waitingForExport = exit.exporting && !flow.leaving;
  const locked = flow.leaving || flow.recounting || flow.transfer !== "idle" || exit.exporting;
  const exportClass = exit.exportResult === "copied"
    ? "sort-button copy-result-copied"
    : exit.exportResult === "failed"
      ? "sort-button copy-result-failed"
      : "sort-button";
  const exportLabel = exit.exporting
    ? "正在导出…"
    : exit.exportResult === "copied"
      ? "已导出"
      : exit.exportResult === "failed"
        ? "导出失败"
        : "导出为文件";
  const transferLabel = flow.transfer === "loading"
    ? "正在读取…"
    : flow.transfer === "working"
      ? "正在转移…"
      : "转移到其他笔记本";
  const showHint = flow.phase === "confirm" && !empty;

  return (
    <>
      <div
        ref={panelRef}
        className="notebook-exit-panel"
        role="dialog"
        aria-modal="true"
        aria-label="退出共享"
        aria-describedby={showHint ? `${leadId} ${hintId}` : leadId}
        tabIndex={-1}
      >
        {flow.phase === "checking" && <p id={leadId} className="notebook-exit-lead">正在确认…</p>}
        {flow.phase === "leaving" && (
          <p id={leadId} className="notebook-exit-lead">
            {flow.verifying ? "正在确认是否已退出…" : "正在退出…"}
          </p>
        )}
        {flow.phase === "unavailable" && (
          <p id={leadId} className="notebook-exit-lead" role="alert">
            {flow.failure || EXIT_UNAVAILABLE_TEXT}
          </p>
        )}
        {flow.phase === "confirm" && (
          <>
            <p id={leadId} className="notebook-exit-lead">
              {empty
                ? "你在这个笔记本里已经没有记忆了。"
                : `退出后，你在这个笔记本里的 ${flow.count} 条记忆会被永久删除，无法恢复。`}
            </p>
            {showHint && (
              <p id={hintId} className="notebook-exit-hint">可以先把它们转移到你的其他笔记本，或导出为文件保存。</p>
            )}
            {flow.changed && (
              <p className="notebook-exit-hint" role="status">
                {empty ? EXIT_CHANGED_TO_EMPTY_TEXT : EXIT_CHANGED_TEXT}
              </p>
            )}
            {flow.notice && <p className="notebook-exit-hint" role="status">{flow.notice}</p>}
            {flow.failure && <p className="notebook-exit-error" role="alert">{flow.failure}</p>}
            {!empty && (
              <div className="notebook-exit-actions">
                <button
                  type="button"
                  className={exportClass}
                  disabled={exit.exporting || flow.leaving}
                  onClick={() => { void exit.exportMemories(); }}
                >
                  {exportLabel}
                </button>
                <button
                  type="button"
                  className="sort-button"
                  disabled={locked}
                  onClick={() => { void exit.openTransfer(); }}
                >
                  {transferLabel}
                </button>
              </div>
            )}
            {!empty && exit.exportFailureReason && (
              <p className="notebook-exit-error" role="alert">{exit.exportFailureReason}</p>
            )}
          </>
        )}
        {flow.phase === "confirm" && waitingForExport && (
          <p className="notebook-exit-hint" role="status">{EXIT_WAIT_FOR_EXPORT_TEXT}</p>
        )}
        <div className="notebook-exit-actions">
          {flow.phase === "unavailable" && (
            <button type="button" className="sort-button" onClick={exit.retry}>重试</button>
          )}
          {/* 取消永不禁用:不因忙碌而失效,挂死的请求才有出口。 */}
          <button ref={cancelRef} type="button" className="sort-button" onClick={() => exit.cancel()}>
            取消
          </button>
          {flow.phase === "confirm" && (
            <button
              type="button"
              className="new-pill danger-pill"
              disabled={locked}
              onClick={exit.confirm}
            >
              {flow.leaving ? (flow.verifying ? "正在确认…" : "正在退出…") : empty ? "退出共享" : "确认退出并删除"}
            </button>
          )}
        </div>
      </div>
      {picking && (
        // 既有的目标笔记本选择器(记忆页同一个),范围限定为这本笔记本里我自己的记忆。
        // 它是 z 80 的全屏浮层,盖住面板的「取消」,所以它自己的「取消」在提交中也必须可用
        // (`cancelWhileBusy`):关掉它只是收起选择器,已发出的转移落地后照常汇报。
        <div ref={pickerHostRef}>
          <DestinationPicker
            sourceNotebookId={flow.notebookId}
            allowMove
            allowManagedTargets={false}
            title={`复制/移动 ${flow.transferIds.length} 条记忆`}
            showExtractKg
            extractKg={exit.transferExtractKg}
            onExtractKgChange={exit.setTransferExtractKg}
            cancelWhileBusy
            onCancel={exit.closeTransfer}
            onSubmit={exit.submitTransfer}
          />
        </div>
      )}
    </>
  );
}

export function NotebookExitPanel({ exit }: { exit: NotebookExit }) {
  const { flow } = exit;
  if (!flow || typeof document === "undefined") return null;
  const revealed = flow.phase === "confirm" || flow.phase === "unavailable" || exit.slow;
  if (!revealed) return null;
  return createPortal(<ExitDialog exit={exit} />, document.body);
}
