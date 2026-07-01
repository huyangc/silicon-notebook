// Notebook 分享与拷贝 — 分享客户端(纯逻辑,纯 helper 在 notebook-share.test.mjs
// 单测)。自带 fetch 封装,可在 `node --test` 下运行而不 import React 页面模块。
// 镜像 notebook-tier.ts 的样板。

import { authHeaders } from "./auth.ts";

// 复用 tier.ts 里的宽松形状:后端 copy 返回一个完整 NotebookSummary,这里只声明
// 我们会读到的字段,其余用索引签名兜底。
export type NotebookSummaryLike = {
  id: string;
  name: string;
  tier?: string;
  [k: string]: unknown;
};

// 分享库的规模统计(bytes/sources/chunks/nodes/edges),后端以 Dict[str,int] 下发。
export type ShareSize = {
  bytes: number;
  sources: number;
  chunks: number;
  nodes: number;
  edges: number;
};

// POST /notebooks/{id}/share 的响应。
export type ShareResponse = {
  share_token: string;
  copyable: boolean;
  size: ShareSize;
};

// GET /shared/{token} 的响应。mode: "copy"(可拷贝) | "too_large"(库太大只读共享待支持)。
export type SharedPreview = {
  name: string;
  owner_display: string;
  source_count: number;
  node_count: number;
  edge_count: number;
  source_titles: string[];
  mode: "copy" | "too_large";
  size: ShareSize;
};

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
  // 204 No Content(取消分享)没有 body。
  if (res.status === 204) return null as T;
  return res.json() as Promise<T>;
}

// 开启分享,拿到分享 token / 是否可拷贝 / 规模(仅 owner)。
export const shareNotebook = (notebookId: string): Promise<ShareResponse> =>
  apiFetch(`/notebooks/${notebookId}/share`, { method: "POST" });

// 取消分享(仅 owner,期望 204)。
export const unshareNotebook = (notebookId: string): Promise<void> =>
  apiFetch<void>(`/notebooks/${notebookId}/share`, { method: "DELETE" });

// 预览分享内容(任意登录用户;错码/已撤销 → 404 → throw)。
export const previewShared = (token: string): Promise<SharedPreview> =>
  apiFetch(`/shared/${token}`);

// 拷贝分享库到当前用户空间(库太大 → 409 → throw)。
export const copyShared = (token: string): Promise<NotebookSummaryLike> =>
  apiFetch(`/shared/${token}/copy`, { method: "POST" });

// --- 纯 helper(单测) --------------------------------------------------------

// 从 `?share=shr-xxx` 取分享 token;无则 null。容错前导 `?`、多参数。
export const parseShareToken = (search: string): string | null => {
  const qs = (search ?? "").replace(/^\?/, "");
  if (!qs) return null;
  for (const pair of qs.split("&")) {
    const eq = pair.indexOf("=");
    if (eq === -1) continue;
    const key = pair.slice(0, eq);
    if (key === "share") {
      const raw = pair.slice(eq + 1);
      const value = decodeURIComponent(raw);
      return value || null;
    }
  }
  return null;
};

// 拼可复制的分享链接:${origin}/?share=${token}。
export const buildShareLink = (token: string, origin: string): string =>
  `${origin}/?share=${token}`;
