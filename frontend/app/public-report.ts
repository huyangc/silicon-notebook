// 公开分享报告的客户端与纯逻辑（无 React、无登录态）。
//
// 这一侧刻意与已认证的应用完全分离：公开页只认一个 token，不带 session，
// 也不复用任何需要 notebook 上下文的调用。后端返回的就是白名单投影，
// 这里不做二次裁剪，只负责取数与呈现所需的最小整形。

import { requestJson } from "./api-client.ts";
import { httpErrorStatus } from "./errors.ts";

export type PublicReportReferenceT = {
  key: string;
  title: string;
  file_name: string;
  location: string;
  snippet: string;
  /**
   * 标题/摘录/原始文件名超上限被切成前缀时置真——公开页据此显示「已截断」而不
   * 静默丢尾（AGENTS.md「用户编辑的数据不得静默截断」）。可选是为了兼容还没带
   * 这三个字段的旧后端：缺字段按「没截断」处理，不凭空弹出提示。
   */
  title_truncated?: boolean;
  snippet_truncated?: boolean;
  file_name_truncated?: boolean;
};

export type PublicReportT = {
  question: string;
  content_md: string;
  created_at: string;
  updated_at: string;
  references: PublicReportReferenceT[];
  reference_count: number;
  truncated_references: boolean;
};

/** 分享链接。token 是全部授权，所以 URL 里不带 notebook/report id。 */
export function buildPublicReportLink(token: string, origin: string): string {
  const clean = String(token || "").trim();
  if (!clean) return "";
  return `${String(origin || "").replace(/\/+$/, "")}/r/${encodeURIComponent(clean)}`;
}

/**
 * 取一份公开报告；未知/已撤销 token 与不存在无法区分，都返回 null。
 *
 * 走共享 transport 而不是裸 fetch（生产 HTTP 调用归 api-client 所有），但用
 * `auth: "none"`：这是全站唯一不需要 session 的读取，带上 token 反而会让未登录
 * 访客在 401 处理上走进已认证应用的分支。
 */
export async function fetchPublicReport(token: string): Promise<PublicReportT | null> {
  const clean = String(token || "").trim();
  if (!clean) return null;
  try {
    const body = await requestJson<PublicReportT>(
      `/public/reports/${encodeURIComponent(clean)}`,
      { tag: "public-report", auth: "none", cache: "no-store" },
    );
    return {
      ...body,
      references: Array.isArray(body.references) ? body.references : [],
    };
  } catch (error) {
    if (httpErrorStatus(error) === 404) return null;
    throw error;
  }
}

/**
 * 一条引用对外显示的编号。
 *
 * 权威是 key 里的序号：后端在写 `content_md` 之前已经把正文标记全局重编号成
 * `kN`，所以正文里写的就是 N。**不能**照搬站内视图的「位置序号」——公开投影会
 * 丢掉既无标题又无摘录的条目（`report_public_view.public_report_payload`），
 * 位置序号一旦被这道过滤挤位，正文的 [12] 就会指到清单里的第 11 条。
 * 只有 key 不是 `kN` 形状时才退回位置序号。
 */
export function publicReferenceNumber(
  reference: PublicReportReferenceT | undefined,
  index: number,
): number {
  const matched = String(reference?.key || "").trim().match(/^k(\d+)$/);
  return matched ? Number(matched[1]) : index + 1;
}

/** remarkCitations 需要的最小引用形状（结构上兼容 AnswerReference）。 */
export type PublicCitationRefT = { id: string; displayLabel: string };

/**
 * 正文 `[k]` 标记链接化所需的 key→显示编号映射。
 *
 * 与「引用出处」清单共用 `publicReferenceNumber`，两处编号因此不可能分叉；
 * 显示形态 `[N]` 与站内答案/报告逐字一致（`answer-formatting` 的 displayLabel）。
 */
export function publicCitationRefs(
  references: PublicReportReferenceT[] | undefined,
): Record<string, PublicCitationRefT> {
  const out: Record<string, PublicCitationRefT> = {};
  (references || []).forEach((reference, index) => {
    const key = String(reference?.key || "").trim();
    if (!key) return;
    out[key] = {
      id: `public:${key}`,
      displayLabel: `[${publicReferenceNumber(reference, index)}]`,
    };
  });
  return out;
}
