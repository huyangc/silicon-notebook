import { performApiRequest, requestBlob, requestJson, requestVoid } from "./api-client.ts";
import { throwHumanizedHttpError } from "./errors.ts";
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

/** 分享前的披露:公开页会带上作者本人多少条个人记忆摘录(服务端按即将公开的确切范围数)。 */
export const getReportShareDisclosure = (nb: string, id: string) =>
  requestJson<{ memory_count: number }>(`/notebooks/${nb}/reports/${id}/share/disclosure`, options);

// 409 `share_disclosure_required`:作者确认的条数与服务端此刻数出来的不相等(或根本没带确认
// 而库里有 Memory 引用)。数字由服务端给,前端只负责把确认条就地更新成它——所以这里不走通用
// 人话层(那只会把它压成「操作有冲突」),而是转成类型化异常,`memoryCount` 就是新的确数。
export class ShareDisclosureRequired extends Error {
  readonly memoryCount: number;
  readonly newMemoryCount: number;

  constructor(memoryCount: number, newMemoryCount: number) {
    super("share_disclosure_required");
    this.name = "ShareDisclosureRequired";
    this.memoryCount = memoryCount;
    this.newMemoryCount = newMemoryCount;
  }
}

/** 从 409 正文里认出 `share_disclosure_required`;认不出返回 null(落回通用错误路径)。
 *  code 与状态码成对匹配,且 `memory_count` 必须是非负整数。结构在 `detail` 下(FastAPI 形状),
 *  也容忍根层同形状。 */
export const parseShareDisclosureRequired = (
  status: number,
  body: unknown,
): { memoryCount: number; newMemoryCount: number } | null => {
  if (status !== 409 || typeof body !== "object" || body === null) return null;
  const root = body as Record<string, unknown>;
  const detail = typeof root.detail === "object" && root.detail !== null
    ? root.detail as Record<string, unknown>
    : root;
  if (detail.code !== "share_disclosure_required") return null;
  const count = detail.memory_count;
  if (typeof count !== "number" || !Number.isInteger(count) || count < 0) return null;
  const added = detail.new_memory_count;
  return {
    memoryCount: count,
    newMemoryCount: typeof added === "number" && Number.isInteger(added) && added >= 0 ? added : 0,
  };
};

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
  if (!res.ok) {
    if (res.status === 409) {
      // body 只能消费一次:探测读克隆,真正的报错仍交给原始 res 走人话层。
      let parsed: unknown;
      try {
        parsed = JSON.parse(await res.clone().text());
      } catch {
        parsed = undefined;
      }
      const required = parseShareDisclosureRequired(res.status, parsed);
      if (required) throw new ShareDisclosureRequired(required.memoryCount, required.newMemoryCount);
    }
    await throwHumanizedHttpError(res, options.tag);
  }
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
