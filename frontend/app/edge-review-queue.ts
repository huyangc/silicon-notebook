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

export const reviewRelation = (
  notebookId: string,
  relId: string,
  status: EdgeReviewStatus
): Promise<ReviewRelationResult> =>
  requestJson(
    `/notebooks/${notebookId}/relations/${encodeURIComponent(relId)}/review`,
    { method: "POST", body: JSON.stringify({ status }), tag: "edge-review-queue" }
  );
