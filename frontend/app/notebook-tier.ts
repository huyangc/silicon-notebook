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

export const nextTier = (current?: string): NotebookTier =>
  current === "base" ? "personal" : "base";

export const tierLabel = (current?: string): string =>
  current === "base" ? "取消基准库" : "设为基准库";

export const setNotebookTier = (
  notebookId: string,
  tier: NotebookTier
): Promise<NotebookSummaryLike> =>
  apiFetch(`/notebooks/${notebookId}/tier`, {
    method: "POST",
    body: JSON.stringify({ tier }),
  });
