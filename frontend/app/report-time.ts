/** Format a deep-report timestamp in the browser's local timezone. */
export function formatReportTime(value: string | Date, now = new Date()): string {
  const reportTime = value instanceof Date ? value : new Date(value);
  const reportMs = reportTime.getTime();
  const nowMs = now.getTime();
  if (!Number.isFinite(reportMs) || !Number.isFinite(nowMs)) return "";

  const exact = `${reportTime.getFullYear()}年${reportTime.getMonth() + 1}月${reportTime.getDate()}日 `
    + `${String(reportTime.getHours()).padStart(2, "0")}:${String(reportTime.getMinutes()).padStart(2, "0")}`;
  const diffSec = Math.max(0, Math.round((nowMs - reportMs) / 1000));

  let relative: string;
  if (diffSec < 60) relative = "刚刚";
  else if (diffSec < 3600) relative = `${Math.floor(diffSec / 60)} 分钟前`;
  else if (diffSec < 86400) relative = `${Math.floor(diffSec / 3600)} 小时前`;
  else if (diffSec < 86400 * 30) relative = `${Math.floor(diffSec / 86400)} 天前`;
  else if (diffSec < 86400 * 365) relative = `${Math.floor(diffSec / (86400 * 30))} 个月前`;
  else relative = `${Math.floor(diffSec / (86400 * 365))} 年前`;

  return `${exact} · ${relative}`;
}

/** Format wall-clock time from full-report generation start through completion. */
export function formatReportDuration(
  createdAt: string | Date,
  completedAt: string | Date,
): string {
  const start = createdAt instanceof Date ? createdAt : new Date(createdAt);
  const end = completedAt instanceof Date ? completedAt : new Date(completedAt);
  const elapsedMs = end.getTime() - start.getTime();
  if (!Number.isFinite(elapsedMs) || elapsedMs < 0) return "";

  const totalMinutes = Math.floor(elapsedMs / 60_000);
  if (totalMinutes === 0) return "少于 1 分钟";
  const days = Math.floor(totalMinutes / (24 * 60));
  const hours = Math.floor((totalMinutes % (24 * 60)) / 60);
  const minutes = totalMinutes % 60;
  if (days > 0) {
    return `${days} 天${hours > 0 ? ` ${hours} 小时` : ""}${minutes > 0 ? ` ${minutes} 分钟` : ""}`;
  }
  if (hours > 0) return `${hours} 小时${minutes > 0 ? ` ${minutes} 分钟` : ""}`;
  return `${totalMinutes} 分钟`;
}

/**
 * Completed reports use their terminal write as generation time. A final
 * duration is shown only when the backend persisted the full-report generation
 * start, so confirmation waits and legacy reports are never misrepresented.
 */
export function formatReportTiming(
  status: string,
  createdAt: string,
  updatedAt?: string,
  generationStartedAt?: string,
  now = new Date(),
): string {
  const completedAt = status === "done"
    && updatedAt
    && Number.isFinite(new Date(updatedAt).getTime())
    ? updatedAt
    : "";
  const shownAt = completedAt || createdAt;
  const timestamp = formatReportTime(shownAt, now);
  if (!timestamp) return "";
  if (!completedAt) return `创建于 ${timestamp}`;

  const duration = generationStartedAt
    ? formatReportDuration(generationStartedAt, shownAt)
    : "";
  return `生成于 ${timestamp}${duration ? ` · 总耗时 ${duration}` : ""}`;
}
