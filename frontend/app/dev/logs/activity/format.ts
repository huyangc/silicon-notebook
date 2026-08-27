// Pure presentation helpers for the "活动" view. Imported by both the React
// components (compiled by Next) and format.test.mjs (run by Node) — keep this
// file free of React/JSX imports so the plain `node --test` run below works.

import { ASK_STATUS, PARSE_STATUS, REPORT_STATUS, label } from "../../../vocabulary.ts";
import type { ActivityAsk, ActivityItem, ActivityReport, ActivitySource } from "./types";

export type ActivityKind = ActivityItem["type"];
export type ActivityTone = "ok" | "error" | "warn" | "muted";

// 界面词:三类活动条目的中文名。不得出现 chunk/KG/schema/projection 等内部黑话
// (scripts/check_ui_vocabulary.py 是硬门)。
const KIND_LABELS: Record<ActivityKind, string> = {
  ask: "提问",
  source: "来源",
  report: "报告",
};

export function activityKindLabel(type: ActivityKind): string {
  return KIND_LABELS[type];
}

const NO_TITLE = "（无标题）";

// 行主标题:ask/report 取提问原文,source 只读 display_title。
//
// ⚠ source 分支**不得**回落 file_name。真源 source_display_title()
// (backend/app/services/source_display.py)刻意让空白 title 遮蔽 file_name、
// 返回空串(见其测试 test_whitespace_only_source_title_still_beats_the_file_name)
// ——它代表"这份来源的显示名就是没有",不是"没有就换个名字凑合"。如果这里
// 再加一层 `|| file_name`,同一篇 title="   " 的来源在引用卡上是"无名"、
// 在活动流里却叫 q3-report.pdf，正是 `docs/product-and-api.md` 契约要防的"同一批来源两个
// 名字"。前端把空标题渲染成占位符是合法的展示决策,但那个决策(NO_TITLE)
// 必须发生在 display_title 已经确定为空**之后**,不能在它之前塞进一个
// 不同的回退来源。
export function activityTitle(item: ActivityItem): string {
  const raw = item.type === "source" ? item.display_title : item.question;
  const trimmed = (raw ?? "").trim();
  return trimmed || NO_TITLE;
}

// 来源行的 parse_status 缺失时回退 status——与
// frontend/app/anomaly-severity.ts::sourceAnomalies 的既有回退规则保持一致,
// 避免同一行在两处的"失败/未失败"判定互相矛盾。
function effectiveSourceStatus(item: ActivitySource): string {
  return item.parse_status || item.status;
}

// 行上状态徽标的中文短语。三类各自复用既有中文状态映射(单一真源全部在
// vocabulary.ts:PARSE_STATUS/ASK_STATUS/REPORT_STATUS),不在本文件另造一套
// 说法。
export function activityStatusLabel(item: ActivityItem): string {
  switch (item.type) {
    case "ask":
      return label(ASK_STATUS, item.status, "未知状态");
    case "source":
      return label(PARSE_STATUS, effectiveSourceStatus(item), "未知状态");
    case "report":
      return label(REPORT_STATUS, item.status, "未知状态");
    default: {
      const exhaustive: never = item;
      return exhaustive;
    }
  }
}

function askTone(item: ActivityAsk): ActivityTone {
  if (item.status === "done") return "ok";
  if (item.status === "failed") return "error";
  // cancelled 是用户主动取消,不是失败——与 reportTone 保持同一判断(见下),
  // 不显示成红,否则混合流里两行都写"已取消"却一红一灰,会让人以为红的那次
  // 还出了别的问题。
  return "muted"; // running / cancelled,以及任何未来新增的非终态值
}

// 来源状态色调。真源是 globals.css 的 `.status-*` 选择器组
// (globals.css:5265-5286 附近,`.status-extracted`/`.status-failed`/
// `.status-queued,.status-parsing,.status-parsed,.status-extracting` 三组,
// 该处注释原文:"中间态（已解析但 KG 抽取未完成等）显示处理中橙，绿色只留给
// 真正 extracted"),与来源页签保持同一套判据,不能反过来把"解析完、KG 还没抽"
// 的中间态显示成绿色——那会让管理员在活动流里读成"这份没问题",而来源页签
// 同一行却是橙色"处理中"。
//
// ⚠ frontend/app/dev/logs/logs.css 目前只有 .badge.ok/.retry/.error/.muted,
// 没有 .warn 类——这里先把判据接上,渲染样式(橙色 .badge.warn)由消费本模块
// 的 React 组件任务在 logs.css 里补齐。
function sourceTone(item: ActivitySource): ActivityTone {
  const status = effectiveSourceStatus(item);
  if (status === "extracted") return "ok";
  if (status === "failed") return "error";
  if (status === "queued" || status === "parsing" || status === "parsed" || status === "extracting") {
    return "warn";
  }
  return "muted"; // uploaded / metadata-only,以及任何未来新增的未知值
}

function reportTone(item: ActivityReport): ActivityTone {
  if (item.status === "done") return "ok";
  if (item.status === "failed") return "error";
  return "muted"; // pending/planning/intent_ready/outline_ready/running/generating/cancelled
}

// 行上的状态样式色调。判据按类型分别定,详见各 *Tone 辅助函数上的注释。
export function activityTone(item: ActivityItem): ActivityTone {
  switch (item.type) {
    case "ask":
      return askTone(item);
    case "source":
      return sourceTone(item);
    case "report":
      return reportTone(item);
    default: {
      const exhaustive: never = item;
      return exhaustive;
    }
  }
}

function activityKey(item: ActivityItem): string {
  // 复合键含 (created_at, id, type):ask_jobs/sources/reports 是三张各自独立
  // 自增/生成主键的表,不同类型的条目完全可能撞出相同的 (created_at, id)。
  // type 作为第三维是零成本的额外保险。
  return `${item.created_at}::${item.id}::${item.type}`;
}

// 追加下一页活动流时按 (created_at, id, type) 去重后拼接。服务端三张表
// (ask_jobs / sources / reports)各自 keyset 分页、前端归并,这里只做去重拼接
// ——**不**重新排序,排序权威在服务端。
//
// ⚠ 本函数不做任何 scope 校验:它看不见 notebook_id/请求 generation,调用方
// 必须自带既有页面惯用的 generation ref + scope key 守卫(见 dev/logs/page.tsx
// 的 requestScopeKey 模式),否则跨 notebook 或过期请求的迟到响应会被这里
// 静默拼进当前列表。
export function mergeActivityPages(prev: ActivityItem[], next: ActivityItem[]): ActivityItem[] {
  const seen = new Set(prev.map(activityKey));
  const merged = prev.slice();
  for (const item of next) {
    const key = activityKey(item);
    if (seen.has(key)) continue;
    seen.add(key);
    merged.push(item);
  }
  return merged;
}
