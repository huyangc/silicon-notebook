export const TODAY_VALUE = ""; // 空 = 让后端按其本地时区兜底为「今天」，避开浏览器/服务器时区错位

// 「活动」视图里同一个空值表示**全部时间**（不下发 since/until）。刻意与
// TODAY_VALUE 共用同一个值而不是另造一个哨兵：两个视图共享顶部同一条范围条，
// 空值在两边都表示「不指定某一天」，于是「切回模型调用视图时回落到今天」不需要
// 任何额外的转换代码——那个视图读到空值本来就是今天。
export const ALL_TIME_VALUE = TODAY_VALUE;

// 日志文件才有的「未分天」分区。活动流读的是数据库里的 created_at，没有这个概念，
// 所以活动视图按「全部时间」处理它（dayRange 返回空区间），下拉里也不提供它。
export const LEGACY_VALUE = "legacy";

export function dayLabel(v: string): string {
  if (v === TODAY_VALUE) return "今天";
  if (v === LEGACY_VALUE) return "历史(未分天)";
  return v;
}

/** 「活动」视图的日期下拉标签：空值在这里是「全部时间」而不是「今天」。 */
export function activityDayLabel(v: string): string {
  if (v === ALL_TIME_VALUE) return "全部时间";
  return dayLabel(v);
}

/** 活动流的时间过滤区间：半开 [since, until)。空对象 = 不过滤。 */
export type DayRange = { since?: string; until?: string };

const DAY_RE = /^(\d{4})-(\d{2})-(\d{2})$/;

/**
 * 该 Date 所处时刻的浏览器 UTC 偏移，写成 ISO 8601 的 `±HH:MM`。
 *
 * `getTimezoneOffset()` 给的是「UTC 减本地」的分钟数，符号与 ISO 8601 **相反**
 * （UTC+8 返回 -480），所以先取反再拼。逐个边界各算一次而不是全局算一次：夏令时
 * 地区里 `since` 与 `until` 的偏移可以不同，共用一个会让切换日多算/少算一小时。
 */
function offsetSuffix(local: Date): string {
  const minutes = -local.getTimezoneOffset();
  const sign = minutes < 0 ? "-" : "+";
  const abs = Math.abs(minutes);
  const hours = String(Math.floor(abs / 60)).padStart(2, "0");
  const rest = String(abs % 60).padStart(2, "0");
  return `${sign}${hours}:${rest}`;
}

/** 本地午夜的 Date → `YYYY-MM-DDT00:00:00±HH:MM`（读的是本地日历字段）。 */
function localMidnightBound(local: Date): string {
  const year = String(local.getFullYear()).padStart(4, "0");
  const month = String(local.getMonth() + 1).padStart(2, "0");
  const day = String(local.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}T00:00:00${offsetSuffix(local)}`;
}

/**
 * 把日期下拉的取值翻成活动流的 since/until。
 *
 * 产出的是**带浏览器 UTC 偏移**的边界（`2026-08-04T00:00:00+08:00`），代表
 * 「浏览器本地日历日的午夜」这个**绝对时刻**。
 *
 * ⚠ 这里曾经拼裸串（`2026-08-04T00:00:00`），依据是「库里 created_at 是无时区的本地
 * ISO」。那个依据**已经过期**：`repository_facade._now()` 现在是
 * `datetime.now().astimezone().isoformat()`，新行一律带 `+08:00` 这样的偏移。而后端
 * 契约（`app/core/activity_time.py`）把裸串**按 UTC 读**，于是两侧各自自洽、拼起来
 * 错开一个本机偏移：UTC+8 主机上 `created_at = 2026-08-04T06:30:00+08:00` 的来源，
 * 选「8月4日」看不到、选「8月3日」才出现——而那一行自己显示的时间（按浏览器本地
 * 渲染）写的就是 8月4日 06:30，同一屏自相矛盾。
 *
 * 修法是把偏移**显式写出来**而不是依赖某种裸串约定：界面显示的一切时间都按浏览器
 * 本地时区（设计稿 §3.1 的红线），那么「一天」的边界就该按浏览器本地算。
 * `parse_activity_instant` 本来就正确解析 aware 串，SQLite 的 `julianday()` 对带偏移
 * 的串同样解析正确。
 *
 * 日历算术走**本地** Date 构造器（它同样天然处理跨月/跨年/闰年进位），这样拿到的
 * 就是各自那一天的本地偏移。先做一次回环校验，挡住 `2026-02-30` 这种能被 Date 悄悄
 * 顺延成 3 月 2 日的非法日期：那会让 since 指向一个不存在的日子，区间整个错位。
 *
 * 空值（全部时间）、legacy、任何非法值一律返回空区间 = 不过滤。
 */
export function dayRange(day: string): DayRange {
  const matched = DAY_RE.exec(day);
  if (!matched) return {};
  const year = Number(matched[1]);
  const month = Number(matched[2]);
  const date = Number(matched[3]);
  const start = new Date(year, month - 1, date);
  if (
    start.getFullYear() !== year
    || start.getMonth() !== month - 1
    || start.getDate() !== date
  ) {
    return {};
  }
  const next = new Date(year, month - 1, date + 1);
  return { since: localMidnightBound(start), until: localMidnightBound(next) };
}
