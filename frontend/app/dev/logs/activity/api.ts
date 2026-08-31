// Data-fetching for the "活动" view. Endpoints implemented concurrently in
// backend/app/api/admin_routes.py (see types.ts header for the contract note).
import { performApiRequest } from "../../../api-client.ts";
import { readHttpError, throwHumanizedHttpError } from "../../../errors.ts";
import type {
  ActivityCursor,
  ActivityResponse,
  ActivitySource,
  ActivityTypeFilter,
  AskDetail,
  PaginatedSources,
} from "./types";

const TAG = "admin-activity";

// 「非管理员/非本人」哨兵——镜像 frontend/app/admin/usage/api.ts 的
// FORBIDDEN_SENTINEL 分流模式:诊断(状态码 + 后端 detail + X-Request-Id)先经
// readHttpError() 落 console,再抛一个不带文案的标识,调用方按标识分流到专门的
// 无权限视图而不是走人话层错误文案。这里独立定义(而非跨模块 import usage/api.ts
// 的同名常量)是因为这是另一族端点(/admin/users/{id}/activity 等),自身的
// 403 语境与 usage 总览的 403 语境并非同一件事。
export const FORBIDDEN_SENTINEL = "forbidden";

async function get<T>(path: string): Promise<T> {
  const res = await performApiRequest(path, { tag: TAG });
  if (res.status === 403) {
    await readHttpError(res, TAG);
    throw new Error(FORBIDDEN_SENTINEL);
  }
  if (!res.ok) await throwHumanizedHttpError(res, TAG);
  return res.json() as Promise<T>;
}

export type FetchUserActivityParams = {
  activityType?: Exclude<ActivityTypeFilter, "">;
  notebookId?: string;
  // 半开区间:created_at >= since、created_at < until。原样传字符串,不在前端
  // 做时区/格式转换——两个视图共享顶部同一条范围条,值从范围条控件原样带过来。
  // 必须随请求下推给服务端(不能先取页再客户端过滤):三张表各自 keyset 分页,
  // 分页游标(before_ts/before_id)与 has_more/next_cursor 都是按未过滤口径算的,
  // 客户端过滤会拿到空页或残页。
  since?: string;
  until?: string;
  before?: ActivityCursor | null;
  limit?: number;
};

// GET /admin/users/{user_id}/activity?activity_type=&notebook_id=&since=&until=&before_ts=&before_id=&limit=
export function fetchUserActivity(
  userId: string,
  params: FetchUserActivityParams = {},
): Promise<ActivityResponse> {
  const qs = new URLSearchParams();
  if (params.activityType) qs.set("activity_type", params.activityType);
  if (params.notebookId) qs.set("notebook_id", params.notebookId);
  if (params.since) qs.set("since", params.since);
  if (params.until) qs.set("until", params.until);
  if (params.before) {
    qs.set("before_ts", params.before.ts);
    qs.set("before_id", params.before.id);
  }
  if (params.limit !== undefined) qs.set("limit", String(params.limit));
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  return get<ActivityResponse>(`/admin/users/${encodeURIComponent(userId)}/activity${suffix}`);
}

export type FetchUserNotebookSourcesParams = {
  offset?: number;
  limit?: number;
};

// GET /admin/users/{user_id}/notebooks/{notebook_id}/sources?offset=&limit=
export function fetchUserNotebookSources(
  userId: string,
  notebookId: string,
  params: FetchUserNotebookSourcesParams = {},
): Promise<PaginatedSources> {
  const qs = new URLSearchParams();
  if (params.offset !== undefined) qs.set("offset", String(params.offset));
  if (params.limit !== undefined) qs.set("limit", String(params.limit));
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  return get<PaginatedSources>(
    `/admin/users/${encodeURIComponent(userId)}/notebooks/${encodeURIComponent(notebookId)}/sources${suffix}`,
  );
}

// GET /admin/users/{user_id}/notebooks/{notebook_id}/sources/{source_id}
export function fetchUserNotebookSource(
  userId: string,
  notebookId: string,
  sourceId: string,
): Promise<ActivitySource> {
  return get<ActivitySource>(
    `/admin/users/${encodeURIComponent(userId)}/notebooks/${encodeURIComponent(notebookId)}/sources/${encodeURIComponent(sourceId)}`,
  );
}

// GET /admin/users/{user_id}/asks/{job_id}
export function fetchUserAskDetail(userId: string, jobId: string): Promise<AskDetail> {
  return get<AskDetail>(
    `/admin/users/${encodeURIComponent(userId)}/asks/${encodeURIComponent(jobId)}`,
  );
}
