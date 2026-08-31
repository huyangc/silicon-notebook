"use client";
// 「活动」视图中栏 · 活动流：提问 / 来源 / 报告三类条目按服务端给的时间序渲染。
// 纯展示组件——取数、翻页与竞态都在 ActivityView 里。

import { formatQuestionTime } from "../../../chat-question-time.ts";
import { formatReportTiming } from "../../../report-time.ts";
import { ASK_MODES, modeLabel, type AskModeId } from "../../../ask-modes.ts";
import { REPORT_DEPTH, label } from "../../../vocabulary.ts";
import {
  activityKindLabel,
  activityStatusLabel,
  activityTitle,
  activityTone,
} from "./format.ts";
import { SourceAnomalies } from "./source-view.tsx";
import type { ActivityItem, ActivityTypeFilter } from "./types";

const ACTIVITY_TYPE_OPTIONS: Array<{ value: ActivityTypeFilter; label: string }> = [
  { value: "", label: "全部" },
  { value: "ask", label: "提问" },
  { value: "source", label: "来源" },
  { value: "report", label: "报告" },
];

export function activityKey(item: ActivityItem): string {
  return `${item.type}:${item.id}`;
}

// ask_jobs.mode 是库里的原始值,可能是面向用户的模式之外的内部值(或历史值)。
// modeLabel() 对未知 id 会抛,所以这里先按注册表过一道;认不出就不显示,绝不把
// 内部代号直接上屏。
function userFacingModeLabel(mode: string): string {
  const known = ASK_MODES.some((definition) => definition.id === mode);
  return known ? modeLabel(mode as AskModeId) : "";
}

function ActivityRow({
  item,
  selected,
  onSelect,
  now,
}: {
  item: ActivityItem;
  selected: boolean;
  onSelect: (item: ActivityItem) => void;
  now?: Date;
}) {
  const title = activityTitle(item);
  return (
    <button
      className={`logrow activity-row${selected ? " selected" : ""}`}
      onClick={() => onSelect(item)}
      type="button"
    >
      <div className="logrow-head">
        <span className="badge kind">{activityKindLabel(item.type)}</span>
        <span className={`badge ${activityTone(item)}`}>{activityStatusLabel(item)}</span>
        {item.type === "ask" && userFacingModeLabel(item.mode) ? (
          <span className="activity-chip">{userFacingModeLabel(item.mode)}</span>
        ) : null}
        {item.type === "source" && item.source_type ? (
          <span className="activity-chip">{item.source_type}</span>
        ) : null}
        {item.type === "report" ? (
          <span className="activity-chip">
            {label(REPORT_DEPTH, String(item.depth), "自定义深度")}
          </span>
        ) : null}
        {item.notebook_deleted_at ? (
          <span className="activity-chip">原笔记本已删除</span>
        ) : null}
        <span className="logrow-spacer" />
        {item.type !== "report" ? (
          <span className="logrow-num">
            {formatQuestionTime(
              item.type === "ask" ? item.asked_at || item.created_at : item.created_at,
              now,
            )}
          </span>
        ) : null}
      </div>
      <div className="logrow-preview" title={title}>
        {title}
      </div>
      {/* 报告的时间与总耗时只能出自 formatReportTiming:未完成的报告只显示创建时间,
          缺 generation_started_at 的旧报告不显示耗时。绝不能拿
          updated_at - created_at 顶替——那会把用户确认大纲的等待算进去。 */}
      {item.type === "report" ? (
        <div className="logrow-ts">
          {formatReportTiming(
            item.status,
            item.created_at,
            item.updated_at,
            item.generation_started_at,
            now,
          )}
        </div>
      ) : null}
      {item.type === "source" ? <SourceAnomalies source={item} /> : null}
    </button>
  );
}

export function ActivityStream({
  items,
  activityType = "",
  onActivityTypeChange = () => undefined,
  activityFailure = "",
  onRetryActivity = () => undefined,
  selectedKey,
  onSelect,
  hasMore,
  onLoadMore,
  loading,
  identityErrored,
  now,
}: {
  items: ActivityItem[];
  activityType?: ActivityTypeFilter;
  onActivityTypeChange?: (value: ActivityTypeFilter) => void;
  activityFailure?: string;
  onRetryActivity?: () => void;
  selectedKey: string;
  onSelect: (item: ActivityItem) => void;
  hasMore: boolean;
  onLoadMore: () => void;
  loading: boolean;
  /** 用户身份本身取不到（fetchMe 失败）——不是「加载中」也不是「确定了、就是空」,
   *  不能断言这个范围里没有活动记录，错误已经在页面顶部显示。 */
  identityErrored?: boolean;
  now?: Date;
}) {
  return (
    <div className={`activity-stream${activityType === "ask" ? " activity-stream-question-overview" : ""}`}>
      <div className="activity-stream-head">
        <div className="activity-col-head">
          {activityType === "ask" ? "提问概览" : "活动"}
        </div>
        <div className="activity-type-filter" aria-label="按活动类型筛选" role="group">
          {ACTIVITY_TYPE_OPTIONS.map((option) => (
            <button
              aria-pressed={activityType === option.value}
              className={`activity-type-button${activityType === option.value ? " active" : ""}`}
              disabled={loading}
              key={option.value || "all"}
              onClick={() => onActivityTypeChange(option.value)}
              type="button"
            >
              {option.label}
            </button>
          ))}
        </div>
        {activityFailure ? (
          <div className="activity-type-feedback" role="alert">
            <span>{activityFailure}</span>
            <button disabled={loading} onClick={onRetryActivity} type="button">重试</button>
          </div>
        ) : null}
        {activityType === "ask" ? (
          <p className="activity-focus-hint">集中查看用户提出的问题，了解当前关注点。</p>
        ) : null}
      </div>
      <div className="activity-stream-list">
        {items.length === 0 && !loading && !identityErrored ? (
          <div className="empty">
            {activityType === "ask" ? "这个范围里没有提问" : "这个范围里没有活动记录"}
          </div>
        ) : null}
        {items.map((item) => (
          <ActivityRow
            item={item}
            key={activityKey(item)}
            now={now}
            onSelect={onSelect}
            selected={selectedKey === activityKey(item)}
          />
        ))}
        {loading ? <div className="empty">加载中…</div> : null}
        {hasMore ? (
          <button className="loadmore" disabled={loading} onClick={onLoadMore} type="button">
            加载更多
          </button>
        ) : null}
      </div>
    </div>
  );
}
