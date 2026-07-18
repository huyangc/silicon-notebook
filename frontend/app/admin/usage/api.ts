import { API_BASE, authHeaders } from "../../auth.ts";
import { throwHumanizedHttpError } from "../../errors.ts";

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
  const res = await fetch(`${API_BASE}/admin/users`, { headers: authHeaders() });
  if (res.status === 403) throw new Error("forbidden");
  if (!res.ok) await throwHumanizedHttpError(res, "admin");
  return res.json();
}

export async function fetchOnlineIds(): Promise<string[]> {
  const res = await fetch(`${API_BASE}/admin/online`, { headers: authHeaders() });
  if (res.status === 403) throw new Error("forbidden");
  if (!res.ok) await throwHumanizedHttpError(res, "admin");
  const data = (await res.json()) as { online_ids: string[] };
  return data.online_ids;
}
