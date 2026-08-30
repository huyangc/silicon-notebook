// Track E — edge curation / review-queue API client (pure logic, unit-tested
// in edge-review-queue.test.mjs). Uses the shared transport without importing
// the React page module. Mirrors promotion-queue.ts.

import { requestJson } from "./api-client.ts";

export type EdgeReviewStatus = "pending" | "verified" | "rejected";

export type EdgeReviewItem = {
  rel_id: string;
  notebook_id: string;
  edge_type: string;
  source_object_id: string;
  target_object_id: string;
  source_name: string;
  target_name: string;
  source_type: string;
  target_type: string;
  trust_score: number;
  edge_centrality: number;
  review_priority: number;
  review_status: EdgeReviewStatus;
};

export type ReviewRelationResult = {
  rel_id: string;
  review_status: EdgeReviewStatus;
};

// R3 T-A3: the endpoint returns the limit-bounded, priority-ranked page plus
// the queue's true total (a seq-gated COUNT, independent of `limit`).
export type EdgeReviewQueueResponse = {
  items: EdgeReviewItem[];
  total: number;
};

export const fetchEdgeReviewQueue = (
  notebookId: string,
  limit?: number
): Promise<EdgeReviewQueueResponse> =>
  requestJson(
    `/notebooks/${notebookId}/edge-review-queue${limit != null ? `?limit=${limit}` : ""}`,
    { tag: "edge-review-queue" },
  );

// R3 T-A3 review (P1-1): the modal header must not claim a total without also
// disclosing that the visible list is a truncated page of it. `total` is the
// true seq-gated queue size; `itemsCount` is how many rows the page actually
// rendered (the `limit`-bounded ranking). `total` unknown yet (still loading)
// renders no suffix at all — the bare "关系审核队列" heading.
export const formatEdgeReviewQueueTitle = (
  total: number | null,
  itemsCount: number
): string => {
  if (total == null) return "";
  return total > itemsCount
    ? `（共 ${total} 条 · 显示前 ${itemsCount} 条）`
    : `（共 ${total} 条）`;
};

export const reviewRelation = (
  notebookId: string,
  relId: string,
  status: EdgeReviewStatus
): Promise<ReviewRelationResult> =>
  requestJson(
    `/notebooks/${notebookId}/relations/${encodeURIComponent(relId)}/review`,
    { method: "POST", body: JSON.stringify({ status }), tag: "edge-review-queue" }
  );
