const WEEKDAYS = ["星期日", "星期一", "星期二", "星期三", "星期四", "星期五", "星期六"] as const;

function sameLocalDay(left: Date, right: Date): boolean {
  return left.getFullYear() === right.getFullYear()
    && left.getMonth() === right.getMonth()
    && left.getDate() === right.getDate();
}

function startOfLocalWeek(value: Date): Date {
  const start = new Date(value);
  start.setHours(0, 0, 0, 0);
  start.setDate(start.getDate() - ((start.getDay() + 6) % 7));
  return start;
}

/** Format an Ask timestamp in the browser's local timezone. */
export function formatQuestionTime(value: string | Date, now = new Date()): string {
  const askedAt = value instanceof Date ? value : new Date(value);
  if (!Number.isFinite(askedAt.getTime()) || !Number.isFinite(now.getTime())) return "";

  const time = new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).format(askedAt);
  if (sameLocalDay(askedAt, now)) return time;

  const weekStart = startOfLocalWeek(now);
  const nextWeekStart = new Date(weekStart);
  nextWeekStart.setDate(nextWeekStart.getDate() + 7);
  if (askedAt.getTime() >= weekStart.getTime() && askedAt.getTime() < nextWeekStart.getTime()) {
    return `${WEEKDAYS[askedAt.getDay()]} ${time}`;
  }

  const date = `${askedAt.getMonth() + 1}月${askedAt.getDate()}日`;
  const year = askedAt.getFullYear() === now.getFullYear()
    ? ""
    : `${askedAt.getFullYear()}年`;
  return `${year}${date} ${time}`;
}
