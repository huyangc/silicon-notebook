import type { AdminUserUsage } from "./api.ts";

export function canSeeAdminUsage(role: string | undefined): boolean {
  return role === "admin";
}

export function formatLastActive(iso: string | null | undefined): string {
  if (!iso) return "—";
  return iso.replace("T", " ").slice(0, 16);
}

export function logsDrillHref(userId: string): string {
  return `/dev/logs?owner=${encodeURIComponent(userId)}&view=activity&activity_type=ask`;
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

export type AdminUserSortKey =
  | "username"
  | "role"
  | "created_at"
  | "notebooks"
  | "sources"
  | "questions"
  | "reports"
  | "last_active"
  | "upload_limit";

export type SortDirection = "asc" | "desc";

function compareText(left: string, right: string): number {
  return left.localeCompare(right, "zh-CN", { numeric: true, sensitivity: "base" });
}

function compareTimestamp(left: string, right: string): number {
  const leftEpoch = Date.parse(left);
  const rightEpoch = Date.parse(right);
  const leftValid = Number.isFinite(leftEpoch);
  const rightValid = Number.isFinite(rightEpoch);
  if (leftValid && rightValid) return leftEpoch - rightEpoch;
  if (leftValid) return -1;
  if (rightValid) return 1;
  // 后端正常返回 ISO 时间；异常旧值仍保持确定性且不让比较器返回 NaN。
  return compareText(left, right);
}

function compareAdminUserValue(
  left: AdminUserUsage,
  right: AdminUserUsage,
  key: AdminUserSortKey,
): number {
  if (key === "username") return compareText(left.username, right.username);
  if (key === "role") {
    const roleRank = (role: string) => role === "admin" ? 0 : 1;
    return roleRank(left.role) - roleRank(right.role);
  }
  if (key === "created_at") return compareTimestamp(left.created_at, right.created_at);
  if (key === "last_active") {
    // 未活跃用户无可比较时间，无论升降序都固定放到末尾。
    if (!left.last_active && !right.last_active) return 0;
    if (!left.last_active) return 1;
    if (!right.last_active) return -1;
    return compareTimestamp(left.last_active, right.last_active);
  }
  if (key === "upload_limit") {
    // 管理员展示为「不限」，按正无穷参与排序才与用户看到的语义一致。
    const leftUnlimited = left.role === "admin";
    const rightUnlimited = right.role === "admin";
    if (leftUnlimited && rightUnlimited) return 0;
    if (leftUnlimited) return 1;
    if (rightUnlimited) return -1;
    return left.upload_limit - right.upload_limit;
  }
  return left[key] - right[key];
}

export function sortAdminUsers(
  rows: AdminUserUsage[],
  key: AdminUserSortKey,
  direction: SortDirection,
): AdminUserUsage[] {
  return rows
    .map((row, index) => ({ row, index }))
    .sort((left, right) => {
      let compared = compareAdminUserValue(left.row, right.row, key);
      // last_active 的空值始终置底，不随方向翻转。
      if (key === "last_active" && (!left.row.last_active || !right.row.last_active)) {
        return compared || left.index - right.index;
      }
      if (direction === "desc") compared *= -1;
      return compared || left.index - right.index;
    })
    .map(({ row }) => row);
}
