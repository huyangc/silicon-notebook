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
 * 把输入夹到 `max` 个 **Unicode 码点**，与后端 Pydantic 的 `max_length` 同一把尺。
 *
 * 刻意不用 `<textarea maxLength>`：HTML 那个属性数的是 **UTF-16 code unit**，而
 * Pydantic 数的是码点。含 emoji 等非 BMP 字符时一个字符占两个 code unit，于是
 * `maxLength={4000}` 会在 2,000 个 emoji 处就停手，而 API 其实收 4,000 个——两边
 * 号称「同一护栏」却对不上（codex #525 R2 P2）。方向上它是保守的（前端更严，不会
 * 放过 API 会拒的输入），但红线要的是**同一条**护栏，不是一条更紧的。
 *
 * 中文全在 BMP 内（1 码点 = 1 code unit），所以对绝大多数输入两者逐字相同；这里
 * 只是把「绝大多数」变成「全部」。仓库里 `GROUP_INPUT_LIMITS` / `MEMORY_INPUT_LIMITS`
 * 仍用 `maxLength`，它们不喂任何匿名投影，这处更严格是刻意的、不是不一致。
 */
export const clampToCodePoints = (value: string, max: number): string => {
  const points = Array.from(value);
  return points.length <= max ? value : points.slice(0, max).join("");
};

// 报告的检索范围在**创建那一刻定格**（`generateReport` 因此不带范围）：意图确认与
// 生成前由后端按持久化的那一份重验，用户在这中间改勾选不会追溯改写已建报告。
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
