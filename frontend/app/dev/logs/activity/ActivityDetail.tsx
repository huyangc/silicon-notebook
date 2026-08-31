"use client";
// 「活动」视图右栏 · 详情：随中栏/左栏选中项切换。
//
// 三类详情共用既有渲染件，不另造一套：
//   · 推理轨迹 → answer-panel.tsx 的 ReasoningTracePanel（与 knowhow-panel 同一用法）
//   · 答案正文 → 同文件的 AnswerView
//   · 来源异常小字 → AnomalyBadge + sourceAnomalies()（经 source-view.tsx）
//   · 提问时间 → chat-question-time.ts；报告耗时 → report-time.ts

import { AnswerView, ReasoningTracePanel } from "../../../answer-panel.tsx";
import type { ReasoningTraceStep } from "../../../ask-stream.ts";
import { formatQuestionTime } from "../../../chat-question-time.ts";
import { formatReportTiming } from "../../../report-time.ts";
import { ASK_MODES, modeLabel, type AskModeId } from "../../../ask-modes.ts";
import { REPORT_DEPTH, label } from "../../../vocabulary.ts";
import type { AskResponse } from "../../../workspace-model.ts";
import { ActivityDetailBoundary } from "./DetailBoundary.tsx";
import { activityStatusLabel, activityTitle, activityTone } from "./format.ts";
import { SourceAnomalies } from "./source-view.tsx";
import type { ActivityItem, ActivityReport, ActivitySource, AskDetail } from "./types";

// 这是一个只读的排障视图：AnswerView 的交互回调在这里都没有承接方，所以**一个都不传**
// ——它们全是可选 prop，缺省即那颗按钮不渲染（onSaveMemory / onFeedback /
// onOpenKnowledgeGraph / onOpenKnowhowRow / onBuildScaleIndex / onOpenSource）。
//
// ⚠ 曾经的做法是传空实现、再由 logs.css 隐藏，那条路是错的：CSS 只盖住了
// `.answer-feedback` 与索引横幅的按钮，引用浮层里的「知识图谱」/「在表格中查看」
// 与清单卡每行的「在表格中查看」照常以**启用态**渲染，点下去什么都不发生。
// 判据因此收敛成一条：没有承接方就不给控件，不靠样式表补救。
// 回归门：frontend/app/answer-panel-readonly.component.test.tsx。

function userFacingModeLabel(mode: string): string {
  const known = ASK_MODES.some((definition) => definition.id === mode);
  return known ? modeLabel(mode as AskModeId) : "";
}

// AskDetail.answer / .trace 在契约里是 unknown（形状的真源是 answer-panel 那侧，
// 不在活动视图这里重新声明）。这两个收口函数负责在渲染前做最小的形状确认。
function asAnswer(value: unknown): AskResponse | null {
  return value && typeof value === "object" ? (value as AskResponse) : null;
}

function asTrace(value: unknown[]): ReasoningTraceStep[] {
  return Array.isArray(value) ? (value as ReasoningTraceStep[]) : [];
}

function RetainedActivityNotice({
  item,
  now,
}: {
  item: ActivityItem;
  now?: Date;
}) {
  if (!item.notebook_deleted_at) return null;
  const name = (item.notebook_name ?? "").trim();
  const until = formatQuestionTime(item.retained_until ?? "", now);
  return (
    <div className="activity-retained-notice" role="note">
      原笔记本{name ? `《${name}》` : ""}已删除。这里只保留分析所需的活动摘要
      {until ? `，留存至 ${until}` : ""}；正文、答案、引用和推理过程已随笔记本删除。
    </div>
  );
}

function AskDetailPane({
  item,
  detail,
  loading,
  error,
  notebookNames,
  now,
}: {
  item: Extract<ActivityItem, { type: "ask" }>;
  detail: AskDetail | null;
  loading: boolean;
  error: string;
  notebookNames: Record<string, string>;
  now?: Date;
}) {
  const answer = asAnswer(detail?.answer);
  const persistedTrace = asTrace(detail?.trace ?? []);
  // AnswerView 自己会渲染答案负载里带的轨迹。两份轨迹说的是同一次运行，同屏挂两个
  // 面板只会让人以为跑了两轮，所以负载里有轨迹时这里不再重复渲染持久化的那份。
  const showPersistedTrace = persistedTrace.length > 0
    && (answer?.reasoning_trace ?? []).length === 0;
  const failure = detail?.error ?? "";
  // The stream item may still describe a live notebook when deletion races
  // with the detail request. In that case the detail endpoint is authoritative:
  // it falls back to the retained row after the live ask has cascaded away.
  const retentionItem = detail?.notebook_deleted_at
    ? {
        ...item,
        notebook_name: detail.notebook_name || item.notebook_name,
        notebook_deleted_at: detail.notebook_deleted_at,
        retained_until: detail.retained_until || item.retained_until,
      }
    : item;
  return (
    <div className="activity-detail-body">
      <div className="activity-detail-head">
        <span className={`badge ${activityTone(item)}`}>{activityStatusLabel(item)}</span>
        {userFacingModeLabel(item.mode) ? (
          <span className="activity-chip">{userFacingModeLabel(item.mode)}</span>
        ) : null}
        <span className="activity-detail-time">
          {formatQuestionTime(item.asked_at || item.created_at, now)}
        </span>
      </div>
      <h2 className="activity-detail-title">{activityTitle(item)}</h2>
      <RetainedActivityNotice item={retentionItem} now={now} />
      {error ? <div className="errorbar">{error}</div> : null}
      {loading ? <div className="empty">加载中…</div> : null}
      {!loading && !error && failure ? (
        <div className="detail-error">
          <strong>失败原因：</strong>
          {failure}
        </div>
      ) : null}
      {showPersistedTrace ? <ReasoningTracePanel steps={persistedTrace} /> : null}
      {answer ? (
        <div className="activity-answer">
          <AnswerView
            answer={answer}
            buildingScaleIndex={false}
            feedbackSent=""
            memorySaved={false}
            // F3(评审修复,checkpoint b6541f26):检索结果带图(T1/T2)的附图资产 URL
            // 恒用**当前 active notebook**过权限；这里的 item.notebook_id 是被查看者
            // 的笔记本，不是管理员自己的 active notebook，用它拼资产 URL 会打向一个
            // 管理员未必在其 participant 集内的库、必 404。而 SelectedReferenceDetail
            // 对 notebookId 为空已有既有行为——整个「本段附图」区不渲染，与「无图」
            // 等价（见其 JSDoc）。管理员查看他人活动本就是只读排障场景，宁可不显示
            // 附图，也不给一颗必然 404 的图片请求或误导性的直连。
            notebookId={null}
            notebookNames={notebookNames}
            scaleIndexStatus={null}
          />
        </div>
      ) : null}
      {!loading && !error && !answer && !failure && !retentionItem.notebook_deleted_at ? (
        <div className="empty">这次提问没有留下答案</div>
      ) : null}
    </div>
  );
}

function SourceDetailPane({
  item,
  now,
}: {
  item: ActivitySource;
  now?: Date;
}) {
  const title = activityTitle(item);
  // 引用卡的同一条规则：显示名与原始文件名不同时，把原始文件也说出来（论文标题
  // 接地后，来源名与上传时的文件名往往对不上，只给标题会让人找不到那份文件）。
  const originalFile = item.file_name && item.file_name !== title ? item.file_name : "";
  // 左栏点开与中栏点开是同一个入口的两条路，时间必须逐字相同：两边都只走
  // formatQuestionTime（浏览器本地时区）。曾经的 `createdLabel` 兜底是服务端按
  // **服务端**日历日算好的字符串，留着它就是留着第二种格式、乃至第二个日期。
  // ⚠ formatQuestionTime 是全函数：空串/非法值一律回空串，所以不需要在这里再判一次
  // 空——下面那句 `createdText ?` 同时兜住「没给时间戳」与「给了但解析不了」。
  const createdText = formatQuestionTime(item.created_at, now);
  // ⚠ 只有布尔，没有诊断原文。后端的 `error_message` 是原始异常串，可能带服务端
  // 绝对路径（`FileNotFoundError: /…/storage/notebooks/…`）——而管理员看**别人**
  // 的活动流正是 ScopedSourceDetail 那条红线要防的场景，所以契约给的是
  // `parse_failed`，这里只按它显示一句固定文案。
  return (
    <div className="activity-detail-body">
      <div className="activity-detail-head">
        <span className={`badge ${activityTone(item)}`}>{activityStatusLabel(item)}</span>
        {item.source_type ? <span className="activity-chip">{item.source_type}</span> : null}
        {createdText ? <span className="activity-detail-time">{createdText}</span> : null}
      </div>
      <h2 className="activity-detail-title">{title}</h2>
      <RetainedActivityNotice item={item} now={now} />
      {originalFile ? (
        <dl className="activity-detail-facts">
          <dt>原始文件</dt>
          <dd title={originalFile}>{originalFile}</dd>
        </dl>
      ) : null}
      <SourceAnomalies block source={item} />
      {item.parse_failed ? (
        <div className="detail-error">解析没有成功完成，这个来源没有可检索的内容。</div>
      ) : null}
    </div>
  );
}

function ReportDetailPane({
  item,
  now,
}: {
  item: ActivityReport;
  now?: Date;
}) {
  return (
    <div className="activity-detail-body">
      <div className="activity-detail-head">
        <span className={`badge ${activityTone(item)}`}>{activityStatusLabel(item)}</span>
        <span className="activity-chip">
          {label(REPORT_DEPTH, String(item.depth), "自定义深度")}
        </span>
      </div>
      <h2 className="activity-detail-title">{activityTitle(item)}</h2>
      <RetainedActivityNotice item={item} now={now} />
      {/* 与活动流行上同一条规则：耗时只能来自 generation_started_at → updated_at。 */}
      <p className="activity-detail-time">
        {formatReportTiming(
          item.status,
          item.created_at,
          item.updated_at,
          item.generation_started_at,
          now,
        )}
      </p>
    </div>
  );
}

export function ActivityDetail({
  item,
  askDetail,
  askDetailLoading,
  askDetailError,
  notebookNames,
  now,
}: {
  item: ActivityItem | null;
  askDetail: AskDetail | null;
  askDetailLoading: boolean;
  askDetailError: string;
  notebookNames: Record<string, string>;
  now?: Date;
}) {
  return (
    <div className="activity-detail">
      <div className="activity-col-head">详情</div>
      {/* 一条坏 payload 不该白屏（见 DetailBoundary 顶部说明）。resetKey 跟着选中项
          走，换一条就重新挂载。 */}
      <ActivityDetailBoundary resetKey={item ? `${item.type}:${item.id}` : ""}>
        {!item ? (
          <div className="empty">选择左侧一条活动查看详情</div>
        ) : item.type === "ask" ? (
          <AskDetailPane
            detail={askDetail}
            error={askDetailError}
            item={item}
            loading={askDetailLoading}
            notebookNames={notebookNames}
            now={now}
          />
        ) : item.type === "source" ? (
          <SourceDetailPane item={item} now={now} />
        ) : (
          <ReportDetailPane item={item} now={now} />
        )}
      </ActivityDetailBoundary>
    </div>
  );
}
