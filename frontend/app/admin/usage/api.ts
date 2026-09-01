import { performApiRequest } from "../../api-client.ts";
import { readHttpError, throwHumanizedHttpError } from "../../errors.ts";

// 「非管理员」哨兵:admin 总览把 403 分流到专门的无权限视图,而不是显示一条
// 错误文案。这条控制流不能被人话层吃掉,所以这里抛的是标识而不是文案——但
// 诊断不能跟着丢:抛之前一律先走 readHttpError() 把 状态码 + 后端 detail +
// X-Request-Id 记进 console,否则「明明是管理员却看到无权限」无从查起。
export const FORBIDDEN_SENTINEL = "forbidden";

async function throwForbiddenSentinel(res: Response): Promise<never> {
  await readHttpError(res, "admin");
  throw new Error(FORBIDDEN_SENTINEL);
}

export type AdminUserUsage = {
  id: string;
  username: string;
  role: string;
  created_at: string;
  notebooks: number;
  sources: number;
  /** @deprecated 兼容旧管理端；界面使用 questions。 */
  conversations: number;
  questions: number;
  reports: number;
  last_active: string | null;
  is_online: boolean;
  role_mutable: boolean;
  // 有效文档上限(有覆盖用覆盖、否则全局默认)与「是否为该用户单独设置过」标记。
  upload_limit: number;
  upload_limit_overridden: boolean;
};

export type AnalysisIssue = {
  id: string;
  category: "source_parse" | "spreadsheet_analysis" | "model_output";
  status: "open" | "resolved";
  code: string;
  summary: string;
  owner_id: string;
  notebook_id: string;
  notebook_name: string;
  source_id: string;
  source_title: string;
  file_name: string;
  source_type: string;
  workload_id: string;
  workload_label: string;
  model_area: ModelAnalysisArea | "";
  failure_kind: string;
  support_id: string;
  parent_id: string;
  created_at: string;
  updated_at: string;
  resolved_at: string;
  expires_at: string;
  artifact_available: boolean;
  source_deleted: boolean;
  notebook_deleted: boolean;
};

export type ModelAnalysisArea =
  | "ask" | "report" | "source" | "knowledge" | "memory" | "knowhow" | "retrieval";

export type AnalysisIssueModelArtifact = {
  issue_id: string;
  question: string;
  messages: { role: string; content: string }[];
  schema_hint: string;
  response: string;
  workload_id: string;
  workload_label: string;
  model_area: ModelAnalysisArea | "";
  failure_kind: string;
  support_id: string;
  parent_id: string;
  reason: string;
  occurred_at: string;
};

export type AdminUserRole = "admin" | "user";

export type AdminUserRoleResult = {
  id: string;
  username: string;
  role: AdminUserRole;
};

export type AdminUserUploadLimit = {
  id: string;
  username: string;
  upload_limit: number;
  upload_limit_overridden: boolean;
};

export async function fetchAdminUsers(): Promise<AdminUserUsage[]> {
  const res = await performApiRequest("/admin/users", { tag: "admin" });
  if (res.status === 403) await throwForbiddenSentinel(res);
  if (!res.ok) await throwHumanizedHttpError(res, "admin");
  return res.json();
}

export async function fetchAnalysisIssues(filters: {
  ownerId?: string;
  status?: "open" | "resolved" | "";
  category?: "source_parse" | "spreadsheet_analysis" | "model_output" | "";
  modelArea?: ModelAnalysisArea | "";
} = {}): Promise<AnalysisIssue[]> {
  const params = new URLSearchParams();
  if (filters.ownerId) params.set("owner_id", filters.ownerId);
  if (filters.status) params.set("status", filters.status);
  if (filters.category) params.set("category", filters.category);
  if (filters.modelArea) params.set("model_area", filters.modelArea);
  const query = params.toString();
  const res = await performApiRequest(
    `/admin/analysis-issues${query ? `?${query}` : ""}`,
    { tag: "admin" },
  );
  if (res.status === 403) await throwForbiddenSentinel(res);
  if (!res.ok) await throwHumanizedHttpError(res, "admin");
  const data = (await res.json()) as { items: AnalysisIssue[] };
  return data.items;
}

export async function fetchAnalysisIssueModelArtifact(
  issueId: string,
): Promise<AnalysisIssueModelArtifact> {
  const res = await performApiRequest(
    `/admin/analysis-issues/${encodeURIComponent(issueId)}/artifact`,
    { tag: "admin" },
  );
  if (res.status === 403) await throwForbiddenSentinel(res);
  if (!res.ok) await throwHumanizedHttpError(res, "admin");
  return res.json();
}

export async function fetchOnlineIds(): Promise<string[]> {
  const res = await performApiRequest("/admin/online", { tag: "admin" });
  if (res.status === 403) await throwForbiddenSentinel(res);
  if (!res.ok) await throwHumanizedHttpError(res, "admin");
  const data = (await res.json()) as { online_ids: string[] };
  return data.online_ids;
}

export async function updateAdminUserRole(
  userId: string,
  role: AdminUserRole,
): Promise<AdminUserRoleResult> {
  const res = await performApiRequest(
    `/admin/users/${encodeURIComponent(userId)}/role`,
    { tag: "admin", method: "PATCH", body: JSON.stringify({ role }) },
  );
  if (res.status === 403) await throwForbiddenSentinel(res);
  if (!res.ok) await throwHumanizedHttpError(res, "admin");
  return res.json();
}

// 设/清某用户的文档上限覆盖。limit=null 清除覆盖(回落全局默认)。镜像
// updateAdminUserRole 的 403 哨兵分流 + 人话层错误处理。
export async function updateAdminUserUploadLimit(
  userId: string,
  limit: number | null,
): Promise<AdminUserUploadLimit> {
  const res = await performApiRequest(
    `/admin/users/${encodeURIComponent(userId)}/upload-limit`,
    { tag: "admin", method: "PATCH", body: JSON.stringify({ limit }) },
  );
  if (res.status === 403) await throwForbiddenSentinel(res);
  if (!res.ok) await throwHumanizedHttpError(res, "admin");
  return res.json();
}

export type AdminPasswordReset = { id: string; username: string };

// 管理员重置目标用户的密码。镜像 updateAdminUserRole 的 403 哨兵分流 + 人话层
// 错误处理;成功后目标用户全部会话被吊销,需用新密码重新登录(仅后端行为,
// 这里不做额外处理)。
export async function resetAdminUserPassword(userId: string, newPassword: string): Promise<AdminPasswordReset> {
  const res = await performApiRequest(
    `/admin/users/${encodeURIComponent(userId)}/reset-password`,
    { tag: "admin", method: "POST", body: JSON.stringify({ new_password: newPassword }) },
  );
  if (res.status === 403) await throwForbiddenSentinel(res);
  if (!res.ok) await throwHumanizedHttpError(res, "admin");
  return res.json();
}

export async function fetchUploadLimitDefault(): Promise<number> {
  const res = await performApiRequest("/admin/settings/upload-limit-default", { tag: "admin" });
  if (res.status === 403) await throwForbiddenSentinel(res);
  if (!res.ok) await throwHumanizedHttpError(res, "admin");
  const data = (await res.json()) as { limit: number };
  return data.limit;
}

export async function updateUploadLimitDefault(limit: number): Promise<number> {
  const res = await performApiRequest(
    "/admin/settings/upload-limit-default",
    { tag: "admin", method: "PATCH", body: JSON.stringify({ limit }) },
  );
  if (res.status === 403) await throwForbiddenSentinel(res);
  if (!res.ok) await throwHumanizedHttpError(res, "admin");
  const data = (await res.json()) as { limit: number };
  return data.limit;
}
