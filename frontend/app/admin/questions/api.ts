import { requestJson } from "../../api-client.ts";
import type { SubmittedVia } from "../../submitted-via.ts";

// Mirrors the named protocol rails in backend/app/models/admin.py.
export const ADMIN_QUESTIONS_QUERY_MAX_CHARS = 200;
export const ADMIN_QUESTIONS_DEFAULT_LIMIT = 50;
export const ADMIN_QUESTIONS_MAX_LIMIT = 200;

export type AdminQuestionKind = "ask" | "report";

export type AdminQuestionSubmittedVia = SubmittedVia;

/** 「笔记本内」还是「全局」提问；报告恒为 "notebook"（没有全局报告）。 */
export type AdminQuestionScope = "notebook" | "global";

export type AdminQuestionItem = {
  type: AdminQuestionKind;
  scope: AdminQuestionScope;
  submitted_via: "" | SubmittedVia;
  id: string;
  user_id: string;
  username: string;
  notebook_id: string;
  notebook_name: string;
  question: string;
  status: string;
  created_at: string;
};

export type AdminQuestionStats = {
  total: number;
  asks: number;
  reports: number;
  active_users: number;
  /** 当前筛选下的全局问答数，是 `asks` 的子集。 */
  global_asks: number;
};

export type AdminQuestionsPage = {
  items: AdminQuestionItem[];
  stats: AdminQuestionStats;
  total: number;
  offset: number;
  limit: number;
};

export async function fetchAdminQuestions(filters: {
  kind?: AdminQuestionKind;
  scope?: AdminQuestionScope;
  submittedVia?: AdminQuestionSubmittedVia;
  userId?: string;
  query?: string;
  offset?: number;
  limit?: number;
}): Promise<AdminQuestionsPage> {
  const limit = filters.limit ?? ADMIN_QUESTIONS_DEFAULT_LIMIT;
  if (!Number.isInteger(limit) || limit < 1 || limit > ADMIN_QUESTIONS_MAX_LIMIT) {
    throw new RangeError(`每页数量必须是 1 到 ${ADMIN_QUESTIONS_MAX_LIMIT} 之间的整数`);
  }
  const query = new URLSearchParams({
    offset: String(filters.offset ?? 0),
    limit: String(limit),
  });
  if (filters.kind) query.set("kind", filters.kind);
  if (filters.scope) query.set("scope", filters.scope);
  if (filters.submittedVia) query.set("submitted_via", filters.submittedVia);
  if (filters.userId) query.set("user_id", filters.userId);
  if (filters.query) query.set("q", filters.query);
  return requestJson<AdminQuestionsPage>(`/admin/questions?${query.toString()}`, {
    tag: "admin-questions",
  });
}
