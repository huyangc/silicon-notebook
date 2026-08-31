import { requestJson } from "../../api-client.ts";

// Mirrors the named protocol rails in backend/app/models/admin.py.
export const ADMIN_QUESTIONS_QUERY_MAX_CHARS = 200;
export const ADMIN_QUESTIONS_DEFAULT_LIMIT = 50;
export const ADMIN_QUESTIONS_MAX_LIMIT = 200;

export type AdminQuestionKind = "ask" | "report";

export type AdminQuestionItem = {
  type: AdminQuestionKind;
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
  if (filters.userId) query.set("user_id", filters.userId);
  if (filters.query) query.set("q", filters.query);
  return requestJson<AdminQuestionsPage>(`/admin/questions?${query.toString()}`, {
    tag: "admin-questions",
  });
}
