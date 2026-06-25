// Track F — governance / promotion queue API client (pure logic, unit-tested
// in promotion-queue.test.mjs). Kept self-contained (own fetch wrapper) so it
// can be exercised in isolation under `node --test` without importing the
// React page module.

import { authHeaders } from "./auth.ts";

export type PromotionCandidate = {
  id: string;
  notebook_id: string;
  object_id: string;
  object_type: string;
  status: string;
  reason: string;
  reviewed_by: string;
  base_match_id: string;
  created_at: string;
  payload: Record<string, unknown>;
  evidence: Array<{
    source_title?: string;
    quoted_span?: string;
    confidence?: number;
    [k: string]: unknown;
  }>;
};

export type PromotionApproveResult = {
  candidate_id: string;
  base_object_id: string;
  merged_into: string;
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

export const fetchPromotionQueue = (
  status?: string
): Promise<PromotionCandidate[]> =>
  apiFetch(`/promotion-queue${status ? `?status=${status}` : ""}`);

export const proposePromotion = (
  notebookId: string,
  objectId: string
): Promise<PromotionCandidate> =>
  apiFetch(`/notebooks/${notebookId}/knowledge/${objectId}/promote`, {
    method: "POST",
  });

export const approvePromotion = (
  candidateId: string
): Promise<PromotionApproveResult> =>
  apiFetch(`/promotion-queue/${encodeURIComponent(candidateId)}/approve`, {
    method: "POST",
  });

export const rejectPromotion = (
  candidateId: string,
  reason = ""
): Promise<PromotionCandidate> =>
  apiFetch(`/promotion-queue/${encodeURIComponent(candidateId)}/reject`, {
    method: "POST",
    body: JSON.stringify({ reason }),
  });
