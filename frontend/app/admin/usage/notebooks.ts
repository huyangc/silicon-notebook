import { API_BASE, authHeaders } from "../../auth.ts";
import { throwHumanizedHttpError } from "../../errors.ts";
import { label } from "../../vocabulary.ts";

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
  const res = await fetch(
    `${API_BASE}/admin/users/${encodeURIComponent(userId)}/notebooks`,
    { headers: authHeaders() }
  );
  if (res.status === 403) throw new Error("forbidden");
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
