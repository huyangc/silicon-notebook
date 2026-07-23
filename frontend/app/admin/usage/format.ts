export function canSeeAdminUsage(role: string | undefined): boolean {
  return role === "admin";
}

export function formatLastActive(iso: string | null | undefined): string {
  if (!iso) return "—";
  return iso.replace("T", " ").slice(0, 16);
}

export function logsDrillHref(userId: string): string {
  return `/dev/logs?owner=${encodeURIComponent(userId)}`;
}

// 管理端文档上限输入的合法区间,与后端 admin_routes 的范围校验保持一致。前端先
// 挡一道给即时反馈,后端仍是真源(越界会返回带 X-User-Message 的 400)。
export const UPLOAD_LIMIT_MIN = 1;
export const UPLOAD_LIMIT_MAX = 100000;

// 把用户输入解析成合法上限,非法(空、非整数、越界、带小数/符号/科学计数)返回 null。
export function parseUploadLimit(raw: string): number | null {
  const trimmed = (raw ?? "").trim();
  if (!/^\d+$/.test(trimmed)) return null;
  const value = Number(trimmed);
  if (!Number.isInteger(value) || value < UPLOAD_LIMIT_MIN || value > UPLOAD_LIMIT_MAX) return null;
  return value;
}
