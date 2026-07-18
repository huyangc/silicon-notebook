// Memory 跨 notebook 传输(复制/移动)— 网络客户端(纯逻辑在 transfer-model.ts
// 单测)。自带 fetch 封装,可在 `node --test` 下运行而不 import React 页面模块。
// 镜像 notebook-share.ts 的样板。

import { authHeaders } from "./auth.ts";
import { memoryTransferBody, type TransferMode, type TransferResult } from "./transfer-model.ts";

// 结果单项类型(source_id/new_id/ok/error/status)的唯一真源是 transfer-model.ts
// 的 TransferResult——这里只重新导出,不重复声明,避免两份定义分叉。
export type { TransferResult } from "./transfer-model.ts";

const API_BASE =
  (typeof process !== "undefined"
    ? process.env?.NEXT_PUBLIC_API_BASE_URL
    : undefined) ?? "http://127.0.0.1:8000/api";

async function apiFetch<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(API_BASE + url, {
    headers: { "Content-Type": "application/json", ...authHeaders() },
    ...init,
  });
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  if (res.status === 204) return null as T;
  return res.json() as Promise<T>;
}

// 批量把 memory 复制/移动到另一个 notebook。是 all-or-nothing 之外的语义:
// 请求本身要么整体 200,要么整体因鉴权/校验问题 4xx/5xx(抛错);200 之内每条
// memory 各自独立成败,汇总在 results 里(源不存在/状态不对 → 该条 status=
// "failed";move 复制成功但源清理失败 → 该条 status="copied_source_not_removed",
// ok=false 但 new_id 非空)。调用方用 transfer-model.ts 的
// summarizeTransferResults() 汇总展示,不要在这里再堆一遍判断逻辑。
export const transferMemories = (
  memoryIds: readonly string[],
  targetNotebookId: string,
  mode: TransferMode,
  extractKg: boolean
): Promise<{ results: TransferResult[] }> =>
  apiFetch(`/memories/transfer`, {
    method: "POST",
    body: JSON.stringify(memoryTransferBody(memoryIds, targetNotebookId, mode, extractKg)),
  });
