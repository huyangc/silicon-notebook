// Track F — governance / promotion queue API client (pure logic, unit-tested
// in promotion-queue.test.mjs). Uses the shared transport while remaining
// independently testable without importing the React page module.

import { requestJson } from "./api-client.ts";

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
  source_kind: "knowledge" | "memory";
  memory_id: string;
  source_revision: number;
  // 多领域基准库(Task 7/8):这条候选要晋升进哪个公共知识库。挂 >1 个公共库时由
  // 提交方显式指定;队列里暴露出来是策展人审核"该进哪个库"的唯一依据。
  target_base_id: string;
  // Task 13 审查 #4:target_base_id 对应的库名,后端 join notebooks 给出——
  // 策展人不一定是目标库的 owner,前端自己的 notebooks 列表(自有∪只读加入)
  // 覆盖不到"别人创建的公共知识库",猜不出真名。目标为空或库已不存在时是空串。
  target_base_name: string;
};

export type PromotionApproveResult = {
  candidate_id: string;
  base_object_id: string;
  base_object_ids: string[];
  merged_into: string;
};

export const fetchPromotionQueue = (
  status?: string
): Promise<PromotionCandidate[]> =>
  requestJson(`/promotion-queue${status ? `?status=${status}` : ""}`, { tag: "promotion-queue" });

// targetBaseId 只在笔记本挂了 >1 个公共知识库时才需要显式传(挂 0/1 个由服务端
// 自行解析/拒绝,见 notebook-bases.ts::resolvePromotionTarget)。不传时不带 body——
// 与后端 PromoteRequest 的可选请求体默认值对称,零请求体是合法输入,不是"忘传"。
export const proposePromotion = (
  notebookId: string,
  objectId: string,
  targetBaseId?: string
): Promise<PromotionCandidate> =>
  requestJson(`/notebooks/${notebookId}/knowledge/${objectId}/promote`, {
    method: "POST",
    ...(targetBaseId ? { body: JSON.stringify({ target_base_id: targetBaseId }) } : {}),
    tag: "promotion-queue",
  });

export const approvePromotion = (
  candidateId: string
): Promise<PromotionApproveResult> =>
  requestJson(`/promotion-queue/${encodeURIComponent(candidateId)}/approve`, {
    method: "POST",
    tag: "promotion-queue",
  });

export const rejectPromotion = (
  candidateId: string,
  reason = ""
): Promise<PromotionCandidate> =>
  requestJson(`/promotion-queue/${encodeURIComponent(candidateId)}/reject`, {
    method: "POST",
    body: JSON.stringify({ reason }),
    tag: "promotion-queue",
  });
