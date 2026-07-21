import { performApiRequest } from "../../api-client.ts";
import { readHttpError, throwHumanizedHttpError } from "../../errors.ts";
import { label } from "../../vocabulary.ts";
import { FORBIDDEN_SENTINEL } from "./api.ts";

export type AdminUserNotebook = {
  id: string;
  name: string;
  status: string;
  sources: number;
  conversations: number;
  reports: number;
  created_at: string;
  updated_at: string;
};

export async function fetchUserNotebooks(userId: string): Promise<AdminUserNotebook[]> {
  const res = await performApiRequest(
    `/admin/users/${encodeURIComponent(userId)}/notebooks`,
    { tag: "admin" },
  );
  // 同 api.ts:哨兵不带文案,但诊断先落 console(见 FORBIDDEN_SENTINEL 注释)。
  if (res.status === 403) {
    await readHttpError(res, "admin");
    throw new Error(FORBIDDEN_SENTINEL);
  }
  if (!res.ok) await throwHumanizedHttpError(res, "admin");
  return res.json();
}

const STATUS_CN: Record<string, string> = {
  ready: "就绪",
  draft: "草稿",
  processing: "处理中",
  error: "失败",
  copying: "复制中",
};

export function notebookStatusLabel(s: string): string {
  return label(STATUS_CN, s, "未知状态");
}
