// Two-tier federation — notebook tier client (pure logic, unit-tested in
// notebook-tier.test.mjs). Uses the shared transport without importing the
// React page module.

import { requestJson } from "./api-client.ts";

export type NotebookTier = "base" | "personal";

export type NotebookSummaryLike = {
  id: string;
  name: string;
  tier?: string;
  [k: string]: unknown;
};

export type TierAction = "set" | "unset";

export type TierActionState = {
  action: TierAction;
  label: string;
};

// 二态发布按钮 —— 公共知识库不再全局唯一(多领域各有自己的),故没有「替换」这一态:
// - 当前 notebook 已是 base → unset(撤回发布)
// - 否则 → set(发布)
export const tierActionState = (
  current: NotebookSummaryLike | undefined
): TierActionState =>
  current?.tier === "base"
    ? { action: "unset", label: "取消公共知识库" }
    : { action: "set", label: "设为公共知识库" };

export const setNotebookTier = (
  notebookId: string,
  tier: NotebookTier
): Promise<NotebookSummaryLike> =>
  requestJson(`/notebooks/${notebookId}/tier`, {
    method: "POST",
    body: JSON.stringify({ tier }),
    tag: "tier",
  });
