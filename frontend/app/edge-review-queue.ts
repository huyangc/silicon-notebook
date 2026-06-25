// Track E — edge curation / review-queue API client (pure logic, unit-tested
// in edge-review-queue.test.mjs). Self-contained fetch wrapper so it runs under
// `node --test` without importing the React page module. Mirrors promotion-queue.ts.

import { authHeaders } from "./auth.ts";

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

export const fetchEdgeReviewQueue = (
  notebookId: string,
  limit?: number
): Promise<EdgeReviewItem[]> =>
  apiFetch(
    `/notebooks/${notebookId}/edge-review-queue${limit != null ? `?limit=${limit}` : ""}`
  );

export const reviewRelation = (
  notebookId: string,
  relId: string,
  status: EdgeReviewStatus
): Promise<ReviewRelationResult> =>
  apiFetch(
    `/notebooks/${notebookId}/relations/${encodeURIComponent(relId)}/review`,
    { method: "POST", body: JSON.stringify({ status }) }
  );
