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
  conversations: number;
  reports: number;
  last_active: string | null;
  is_online: boolean;
};

export async function fetchAdminUsers(): Promise<AdminUserUsage[]> {
  const res = await performApiRequest("/admin/users", { tag: "admin" });
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
