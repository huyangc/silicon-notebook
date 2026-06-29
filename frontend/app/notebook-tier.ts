// Two-tier federation — notebook tier client (pure logic, unit-tested in
// notebook-tier.test.mjs). Self-contained fetch wrapper so it runs under
// `node --test` without importing the React page module.

import { authHeaders } from "./auth.ts";

export type NotebookTier = "base" | "personal";

export type NotebookSummaryLike = {
  id: string;
  name: string;
  tier?: string;
  [k: string]: unknown;
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
  return res.json() as Promise<T>;
}

export type TierAction = "set" | "replace" | "unset";

export type TierActionState = {
  action: TierAction;
  label: string;
  otherBaseName?: string;
};

// 三态基准库按钮 —— 基准库全局唯一:
// - 当前 notebook 已是 base → unset(可取消)
// - 别处已有 base → replace(带当前基准库名,点击需确认替换)
// - 全局无 base → set(直接设)
export const tierActionState = (
  current: NotebookSummaryLike | undefined,
  all: readonly NotebookSummaryLike[]
): TierActionState => {
  if (current?.tier === "base") {
    return { action: "unset", label: "取消基准库" };
  }
  const otherBase = all.find((n) => n.tier === "base" && n.id !== current?.id);
  if (otherBase) {
    return { action: "replace", label: "替换为基准库", otherBaseName: otherBase.name };
  }
  return { action: "set", label: "设为基准库" };
};

export const setNotebookTier = (
  notebookId: string,
  tier: NotebookTier
): Promise<NotebookSummaryLike> =>
  apiFetch(`/notebooks/${notebookId}/tier`, {
    method: "POST",
    body: JSON.stringify({ tier }),
  });
