import { API_BASE, authHeaders } from "../../auth.ts";

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
};

export async function fetchAdminUsers(): Promise<AdminUserUsage[]> {
  const res = await fetch(`${API_BASE}/admin/users`, { headers: authHeaders() });
  if (res.status === 403) throw new Error("forbidden");
  if (!res.ok) throw new Error(`${res.status}`);
  return res.json();
}
