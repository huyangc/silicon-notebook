// 目标笔记本选择器 modal —— knowhow 表传输(C3,单张)与 Memory 传输(C4,单条/批量)
// 共用同一层 UI:选目标笔记本 + 复制/移动切换 + 待定/错误态。本组件不关心传输的
// 是一张表还是一批 Memory ——那部分逻辑(调用哪个 transfer API、结果怎么汇总)
// 全部下放给调用方通过 onSubmit 决定,这里只负责"选目标 + 确认"这层交互。
"use client";

import { useEffect, useState } from "react";
import { authHeaders } from "./auth.ts";
import type { NotebookSummary } from "./workspace-model.ts";
import { destinationNotebooks, type TransferMode } from "./transfer-model.ts";

const API_BASE =
  (typeof process !== "undefined"
    ? process.env?.NEXT_PUBLIC_API_BASE_URL
    : undefined) ?? "http://127.0.0.1:8000/api";

export function DestinationPicker({
  sourceNotebookId,
  allowMove,
  title,
  onCancel,
  onSubmit,
}: {
  sourceNotebookId: string;
  allowMove: boolean;
  title: string;
  onCancel: () => void;
  onSubmit: (targetNotebookId: string, mode: TransferMode) => Promise<void>;
}) {
  const [notebooks, setNotebooks] = useState<NotebookSummary[]>([]);
  const [target, setTarget] = useState("");
  const [mode, setMode] = useState<TransferMode>("copy");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    fetch(API_BASE + "/notebooks", {
      headers: { ...authHeaders() },
      signal: controller.signal,
    })
      .then((res) => (res.ok ? res.json() : Promise.reject(new Error(`${res.status}`))))
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
      await onSubmit(target, mode);
      // 成功后不在这里复位 busy——modal 是否关闭由调用方决定(onSubmit resolve
      // 即代表调用方已经/即将卸载本组件);只有失败分支需要复位以便用户重试。
    } catch (err) {
      setError(err instanceof Error ? err.message : "操作失败");
      setBusy(false);
    }
  };

  return (
    <div className="utility-modal utility-modal-top" role="dialog" aria-modal="true" aria-label={title}>
      <div className="utility-modal-card narrow transfer-picker-card">
        <h3>{title}</h3>
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
        {error && (
          <p className="transfer-error" role="alert">
            {error}
          </p>
        )}
        <div className="memory-dialog-actions">
          <button type="button" disabled={busy} onClick={onCancel}>
            取消
          </button>
          <button type="button" disabled={busy || !target} onClick={submit}>
            {busy ? "处理中…" : "确认"}
          </button>
        </div>
      </div>
    </div>
  );
}
