import { performApiRequest, requestBlob, requestJson, requestVoid } from "./api-client.ts";
import { throwShareFailure } from "./share-failure.ts";

// 类型化的 409 披露确认异常的定义在中立模块 `share-failure.ts`（会话分享共用）；报告 hook 的
// 依赖边界只许经 report-api，所以这里转出。
export { ShareDisclosureRequired } from "./share-failure.ts";
import type { ReportDetailT, ReportFrameT, ReportSummaryT } from "./report-model.ts";
import type { BaseScopePayload, SourceScopePayload } from "./source-scope.ts";

const options = { tag: "api", unauthorized: "clear-and-reload" as const };

/**
 * 研究问题的长度上限。
 *
 * **与 `backend/app/models/reports.py` 的 `REPORT_QUESTION_MAX_CHARS` 同值**，
 * 改一侧就要改另一侧。
 *
 * 两侧都要有，是「数值上限与截断」红线的要求：用户编辑的数据不得静默截断——前端
 * 显示同一护栏（输入框直接敲不进去），API 超限**明确拒绝**（后端 422，不裁短了存）。
 * 这条对报告尤其承重：公开分享页把 `reports.question` **原样**发给匿名访客，所以
 * 「不截断」只有在创建那一刻就挡住超长问题时才成立。
 */
// 尺子搬到了 `input-limits.ts`（问答那半护栏要用同一把，而让 `ask-api` import
// `report-api` 只为借一个纯函数会造出一条假的模块依赖）。这里继续导出，既有
// 引用方与单测无需改动。
export { countCodePoints } from "./input-limits.ts";
export { REPORT_INPUT_LIMITS, reportQuestionLimitHint } from "./report-model.ts";

/**
 * 超限时的提示文案；没超返回 `null`。
 *
 * **超出的文字一个字都不删**——护栏是「拦住提交」，不是「替用户裁剪」。曾经在
 * `onChange` 里按上限夹过一刀，那等于用户粘进来 10,000 字、当场只剩 4,000 而且不
 * 说一声，正是「用户编辑的数据不得静默截断」要防的（codex #525 R3）。留着原文，
 * 用户自己精简，或者去别处取回被他放弃的那段。
 */
export const createReport = (
  nb: string,
  question: string,
  depth: number,
  sourceScope?: SourceScopePayload,
  baseScope?: BaseScopePayload,
  // 自动模式：问题清晰(无阻断歧义)时服务端自动确认意图 + 自动接受默认大纲直接
  // 生成；有歧义仍停在 intent_ready，前端照常显示补充问题信息卡。高级模式恒 false。
  autoGenerate?: boolean,
) =>
  requestJson<{ report_id: string }>(`/notebooks/${nb}/reports`, {
    ...options,
    method: "POST",
    body: JSON.stringify({
      question,
      depth,
      source_scope: sourceScope,
      base_scope: baseScope,
      auto_generate: Boolean(autoGenerate),
    }),
  });

export const confirmReportIntent = (
  nb: string,
  id: string,
  payload: { resolved_question: string; answers: { id: string; answer: string }[] },
) =>
  requestJson<{ status: string }>(`/notebooks/${nb}/reports/${id}/intent`, {
    ...options,
    method: "POST",
    body: JSON.stringify(payload),
  });

export const listReports = (nb: string) =>
  requestJson<ReportSummaryT[]>(`/notebooks/${nb}/reports`, options);

export const getReport = (nb: string, id: string) =>
  requestJson<ReportDetailT>(`/notebooks/${nb}/reports/${id}`, options);

export const cancelReport = (nb: string, id: string) =>
  requestJson<{ status: string }>(`/notebooks/${nb}/reports/${id}/cancel`, {
    ...options,
    method: "POST",
  });

export const deleteReport = (nb: string, id: string) =>
  requestJson<{ status: string }>(`/notebooks/${nb}/reports/${id}`, {
    ...options,
    method: "DELETE",
  });

/** 分享前的披露(服务端按即将公开的确切范围数):`memory_count` = 公开页可能带上内容的作者本人
 *  个人记忆条数(被引用的,以及生成时用到的);`foreign_memory_count` = 报告引用的其他成员的个人
 *  记忆条数,大于 0 时这份报告不能公开。 */
export const getReportShareDisclosure = (nb: string, id: string) =>
  requestJson<{ memory_count: number; foreign_memory_count?: number }>(
    `/notebooks/${nb}/reports/${id}/share/disclosure`,
    options,
  );

// `acknowledgedMemoryCount` 缺省时不带 body:零个 Memory 引用的报告分享,请求与今天逐字节相同。
export const shareReport = async (
  nb: string,
  id: string,
  acknowledgedMemoryCount?: number,
): Promise<{ share_token: string }> => {
  const res = await performApiRequest(`/notebooks/${nb}/reports/${id}/share`, {
    ...options,
    method: "POST",
    ...(acknowledgedMemoryCount === undefined
      ? {}
      : { body: JSON.stringify({ acknowledged_memory_count: acknowledgedMemoryCount }) }),
  });
  if (!res.ok) await throwShareFailure(res, options.tag);
  return res.json() as Promise<{ share_token: string }>;
};

export const getReportShare = (nb: string, id: string) =>
  requestJson<{ share_token: string }>(`/notebooks/${nb}/reports/${id}/share`, options);

export const unshareReport = (nb: string, id: string) =>
  requestVoid(`/notebooks/${nb}/reports/${id}/share`, {
    ...options,
    method: "DELETE",
  });

export const updateReportOutline = (
  nb: string,
  id: string,
  payload: { sections: unknown[]; frame?: ReportFrameT },
) =>
  requestJson<{ status: string; sections: number }>(
    `/notebooks/${nb}/reports/${id}/outline`,
    { ...options, method: "PATCH", body: JSON.stringify(payload) },
  );

export const generateReport = (nb: string, id: string, depth?: number) =>
  requestJson<{ status: string }>(`/notebooks/${nb}/reports/${id}/generate`, {
    ...options,
    method: "POST",
    body: JSON.stringify(depth != null ? { depth } : {}),
  });

export async function fetchReportsZip(
  nb: string,
  reportIds: string[],
): Promise<Blob> {
  return requestBlob(`/notebooks/${nb}/reports/export`, {
    ...options,
    method: "POST",
    body: JSON.stringify({ report_ids: reportIds }),
  });
}
