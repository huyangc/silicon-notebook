// 目标笔记本选择器 modal —— knowhow 表传输(C3,单张)与 Memory 传输(C4,单条/批量)
// 共用同一层 UI:选目标笔记本 + 复制/移动切换 + 待定/错误态。本组件不关心传输的
// 是一张表还是一批 Memory ——那部分逻辑(调用哪个 transfer API、结果怎么汇总)
// 全部下放给调用方通过 onSubmit 决定,这里只负责"选目标 + 确认"这层交互。
"use client";

import { useEffect, useState } from "react";
import { requestJson } from "./api-client.ts";
import { toUserMessage } from "./errors.ts";
import { useFloatingWindow } from "./use-floating-window";
import type { NotebookSummary } from "./workspace-model.ts";
import { destinationNotebooks, type TransferMode } from "./transfer-model.ts";

export function DestinationPicker({
  sourceNotebookId,
  allowMove,
  title,
  showExtractKg = false,
  extractKg = true,
  onExtractKgChange,
  onCancel,
  onSubmit,
}: {
  sourceNotebookId: string;
  allowMove: boolean;
  title: string;
  // Important 4（复审）：spec §5.2.6/§9/§12 要求的「同时抽取到知识图谱」
  // opt-out——批量 move/copy 会对每条 confirmed memory 触发一次 LLM 抽取
  // （效率优先约束要求新增 LLM 调用必须可关）。knowhow 调用方（C3）不传这
  // 三个新 prop，三者全部可选 + showExtractKg 默认 false，渲染上什么都不多
  // 出来——保持 DestinationPicker 对 knowhow-panel.tsx 的既有契约不变。
  showExtractKg?: boolean;
  extractKg?: boolean;
  onExtractKgChange?: (value: boolean) => void;
  onCancel: () => void;
  // round 10 P2：第三个参数是目标笔记本的 name——本组件自己刚从 /notebooks
  // 拉到的完整候选列表（下面的 notebooks state）里现成就有，调用方（尤其是
  // memory-panel.tsx 的全局视图）不必再自己维护一份"全部笔记本 id→name"的
  // 映射去查（它现成的 notebookOptions 只覆盖"当前已经有 memory 的笔记本"，
  // 搬到一个全新的空笔记本时查不到）。knowhow 调用方（C3）的 onSubmit 目前
  // 只声明了两个参数——这在 TS 里合法（回调可以忽略多传的实参），不强制
  // 所有调用方都用上第三个。
  onSubmit: (targetNotebookId: string, mode: TransferMode, targetNotebookName: string) => Promise<void>;
}) {
  const [notebooks, setNotebooks] = useState<NotebookSummary[]>([]);
  const [target, setTarget] = useState("");
  const [mode, setMode] = useState<TransferMode>("copy");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const floating = useFloatingWindow({ storageKey: "transfer.picker.window", resizable: false });

  // allowMove 从 true 翻成 false 时把 mode 拽回 copy。当前调用方都是「每次传输重新
  // 挂载一个新实例」,所以 mode 不可能在 allowMove=false 的实例上变成 move;但这是
  // 权限边界的 UI 半边(只读源永远不能提交 move),兜住它成本为零,别依赖调用方的
  // 挂载习惯来维持这个不变量。
  useEffect(() => {
    if (!allowMove) setMode("copy");
  }, [allowMove]);

  useEffect(() => {
    const controller = new AbortController();
    requestJson<NotebookSummary[]>("/notebooks", {
      signal: controller.signal,
      tag: "transfer-picker",
    })
      .then((all: NotebookSummary[]) => setNotebooks(destinationNotebooks(all, sourceNotebookId)))
      .catch((err) => {
        if (err?.name !== "AbortError") setError("加载笔记本列表失败");
      });
    return () => controller.abort();
  }, [sourceNotebookId]);

  const submit = async () => {
    if (!target) {
      setError("请选择目标笔记本");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      // round 10 P2：target 恒是从下面 <select> 渲染的 notebooks 列表里选出
      // 来的 id，此刻在这份列表里查得到——find 失败（列表在选中后又变了这种
      // 理论上不该发生的情况）时兜底成 target 本身，好过让调用方拿到空串
      // 拼出"已移动到「」"这种看着像 bug 的文案。
      const targetName = notebooks.find((n) => n.id === target)?.name ?? target;
      await onSubmit(target, mode, targetName);
      // 成功后不在这里复位 busy——modal 是否关闭由调用方决定(onSubmit resolve
      // 即代表调用方已经/即将卸载本组件);只有失败分支需要复位以便用户重试。
    } catch (err) {
      setError(toUserMessage(err, "操作失败"));
      setBusy(false);
    }
  };

  return (
    <div className="utility-modal utility-modal-top" role="dialog" aria-modal="true" aria-label={title}>
      <div ref={floating.cardRef} className="utility-modal-card narrow transfer-picker-card" style={floating.style}>
        <h3 {...floating.dragHandleProps}>{title}</h3>
        <label className="transfer-target-field">
          目标笔记本
          <select value={target} disabled={busy} onChange={(e) => setTarget(e.target.value)}>
            <option value="">选择…</option>
            {notebooks.map((n) => (
              <option key={n.id} value={n.id}>
                {n.name}
              </option>
            ))}
          </select>
        </label>
        {allowMove && (
          <div className="transfer-mode-toggle">
            <label>
              <input
                type="radio"
                name="transfer-mode"
                checked={mode === "copy"}
                disabled={busy}
                onChange={() => setMode("copy")}
              />
              复制
            </label>
            <label>
              <input
                type="radio"
                name="transfer-mode"
                checked={mode === "move"}
                disabled={busy}
                onChange={() => setMode("move")}
              />
              移动(会从源删除)
            </label>
          </div>
        )}
        {showExtractKg && (
          // 复用 memory-panel.tsx MemoryEditor 里同一个「同时抽取到知识图谱」
          // 勾选样式（.agent-check，memory-panel.css）——只有 showExtractKg 时
          // 才渲染，knowhow 调用方永远不传这个 prop，不会触发那条跨文件 CSS
          // 依赖警告（见上方 .memory-dialog-actions 头注释）：knowhow 用的那个
          // bundle 压根不会渲染这个节点。
          <label className="agent-check">
            <input
              type="checkbox"
              checked={extractKg}
              disabled={busy}
              onChange={(e) => onExtractKgChange?.(e.target.checked)}
            />
            同时整理进知识图谱
          </label>
        )}
        {error && (
          <p className="transfer-error" role="alert">
            {error}
          </p>
        )}
        {/* ⚠ 跨文件 CSS 依赖:.memory-dialog-actions(布局/按钮尺寸/.primary/:disabled)
            全部定义在 memory-panel.css 里,而那个文件只由 memory-panel.tsx 的
            `import "./memory-panel.css"` 副作用加载。今天成立是因为 page.tsx 把
            MemoryPanel 和 KnowhowPanel 静态 import 进同一个 bundle;哪天有人用
            next/dynamic 把其中之一切出去,另一个消费方的这排按钮会静默丢掉全部样式
            (不报错、只是变丑)。真要拆分时,把 memory-panel.css 里
            .memory-dialog-actions 那几条(第 20/103/486-491/712 行附近的分组选择器)
            提到 globals.css,别在这里另造一套。 */}
        <div className="memory-dialog-actions">
          <button type="button" disabled={busy} onClick={onCancel}>
            取消
          </button>
          <button type="button" className="primary" disabled={busy || !target} onClick={submit}>
            {busy ? "处理中…" : "确认"}
          </button>
        </div>
      </div>
    </div>
  );
}
