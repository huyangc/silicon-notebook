"use client";

// 「退出共享」的确认面板。渲染 `use-notebook-exit.ts` 里那一份流程,顶栏按钮与笔记本卡片
// 菜单两个入口共用同一个实例(page.tsx 只渲染一次)。
//
// 落点:按下时的 fixed 坐标(顶栏=按钮正下方;卡片菜单=菜单原来所在的位置,那个菜单一点
// 就关,所以没法「就地」长在菜单里)。面板是浮层,不推动任何内容;挂到 body 是因为顶栏有
// backdrop-filter(它会成为 fixed 后代的包含块并另起层叠上下文),而目标笔记本选择器也是
// fixed 全屏浮层,同样不能被困在顶栏里。
//
// 遵循「无退路弹窗」契约:「取消」永不因忙碌而禁用,挂死的请求也有出口。结果都落在
// 按钮自身或紧邻的文字上,不只发页面顶部的横幅。

import { createPortal } from "react-dom";

import { DestinationPicker } from "./transfer-picker.tsx";
import { EXIT_UNAVAILABLE_TEXT, type NotebookExit } from "./use-notebook-exit.ts";

export function NotebookExitPanel({ exit }: { exit: NotebookExit }) {
  const { flow } = exit;
  if (!flow || typeof document === "undefined") return null;
  const revealed = flow.phase === "confirm" || flow.phase === "unavailable" || exit.slow;
  if (!revealed) return null;

  const empty = flow.count === 0;
  const locked = flow.leaving || flow.recounting || flow.transfer !== "idle";
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

  const panel = (
    <div
      className="notebook-exit-panel"
      role="dialog"
      aria-label="退出共享"
      style={{
        left: `max(8px, min(${flow.anchor.left}px, calc(100vw - 376px)))`,
        top: flow.anchor.top,
      }}
    >
      {flow.phase === "checking" && <p className="notebook-exit-lead">正在确认…</p>}
      {flow.phase === "leaving" && <p className="notebook-exit-lead">正在退出…</p>}
      {flow.phase === "unavailable" && (
        <p className="notebook-exit-lead" role="alert">{EXIT_UNAVAILABLE_TEXT}</p>
      )}
      {flow.phase === "confirm" && (
        <>
          <p className="notebook-exit-lead">
            {empty
              ? "你在这个笔记本里已经没有记忆了。"
              : `退出后，你在这个笔记本里的 ${flow.count} 条记忆会被永久删除，无法恢复。`}
          </p>
          {!empty && (
            <p className="notebook-exit-hint">可以先把它们转移到你的其他笔记本，或导出为文件保存。</p>
          )}
          {flow.changed && (
            <p className="notebook-exit-hint" role="status">记忆数量有变化，请重新确认。</p>
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
                {flow.transfer === "loading" ? "正在读取…" : "转移到其他笔记本"}
              </button>
            </div>
          )}
        </>
      )}
      <div className="notebook-exit-actions">
        {flow.phase === "unavailable" && (
          <button type="button" className="sort-button" onClick={exit.retry}>重试</button>
        )}
        {/* 取消永不禁用:不因忙碌而失效,挂死的请求才有出口。 */}
        <button type="button" className="sort-button" onClick={exit.cancel}>取消</button>
        {flow.phase === "confirm" && (
          <button
            type="button"
            className="new-pill danger-pill"
            disabled={locked}
            onClick={exit.confirm}
          >
            {flow.leaving ? "正在退出…" : empty ? "退出共享" : "确认退出并删除"}
          </button>
        )}
      </div>
    </div>
  );

  return createPortal(
    <>
      {panel}
      {flow.transfer === "picking" && (
        // 既有的目标笔记本选择器(记忆页同一个),范围限定为这本笔记本里我自己的记忆。
        <DestinationPicker
          sourceNotebookId={flow.notebookId}
          allowMove
          allowManagedTargets={false}
          title={`复制/移动 ${flow.transferIds.length} 条记忆`}
          showExtractKg
          extractKg={exit.transferExtractKg}
          onExtractKgChange={exit.setTransferExtractKg}
          onCancel={exit.closeTransfer}
          onSubmit={exit.submitTransfer}
        />
      )}
    </>,
    document.body,
  );
}
