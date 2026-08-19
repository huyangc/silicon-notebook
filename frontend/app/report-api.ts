import { requestBlob, requestJson, requestVoid } from "./api-client.ts";
import type { ReportDetailT, ReportFrameT, ReportSummaryT } from "./report-view.tsx";
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
export const REPORT_INPUT_LIMITS = {
  questionMaxChars: 4000,
} as const;

/**
 * 一段文本的 **Unicode 码点**数——与后端 Pydantic `max_length` 数的是同一种单位。
 *
 * 刻意不用 `value.length`（也就是 `<textarea maxLength>` 用的那把尺）：那数的是
 * **UTF-16 code unit**，含 emoji 等非 BMP 字符时一个字符占两个，于是 4,000 的护栏
 * 会在 2,000 个 emoji 处就停手，而 API 其实收 4,000 个——两边号称「同一护栏」却对
 * 不上（codex #525 R2）。中文全在 BMP 内（1 码点 = 1 code unit），所以对绝大多数
 * 输入两者逐字相同；这里只是把「绝大多数」变成「全部」。
 *
 * 仓库里 `GROUP_INPUT_LIMITS` / `MEMORY_INPUT_LIMITS` 仍用 `maxLength`，它们不喂
 * 任何匿名投影，这处更严格是刻意的、不是不一致。
 */
export const countCodePoints = (value: string): number => Array.from(value).length;

/**
 * 超限时的提示文案；没超返回 `null`。
 *
 * **超出的文字一个字都不删**——护栏是「拦住提交」，不是「替用户裁剪」。曾经在
 * `onChange` 里按上限夹过一刀，那等于用户粘进来 10,000 字、当场只剩 4,000 而且不
 * 说一声，正是「用户编辑的数据不得静默截断」要防的（codex #525 R3）。留着原文，
 * 用户自己精简，或者去别处取回被他放弃的那段。
 */
export const reportQuestionLimitHint = (question: string): string | null => {
  const used = countCodePoints(question);
  const max = REPORT_INPUT_LIMITS.questionMaxChars;
  if (used > max) return `研究问题超出 ${max} 字上限（当前 ${used} 字），请精简后再开始`;
  return null;
};

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

export const shareReport = (nb: string, id: string) =>
  requestJson<{ share_token: string }>(`/notebooks/${nb}/reports/${id}/share`, {
    ...options,
    method: "POST",
  });

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

export async function downloadReportsZip(
  nb: string,
  reportIds: string[],
): Promise<void> {
  const blob = await requestBlob(`/notebooks/${nb}/reports/export`, {
    ...options,
    method: "POST",
    body: JSON.stringify({ report_ids: reportIds }),
  });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = "reports.zip";
  anchor.click();
  URL.revokeObjectURL(url);
}
