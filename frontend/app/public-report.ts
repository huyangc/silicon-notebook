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
 * 引用按 key 建索引，供正文里的 `[k]` 标记查找。
 *
 * 公开页的引用**不可点开原文** —— 后端根本没给 source_id/element_id。所以这里
 * 只提供标题/位置/摘录，正文标记渲染成不可点的角标而不是链接：一个点不动的
 * 链接比没有链接更让人困惑。
 */
export function publicReferencesByKey(
  references: PublicReportReferenceT[] | undefined,
): Record<string, PublicReportReferenceT> {
  const out: Record<string, PublicReportReferenceT> = {};
  for (const reference of references || []) {
    const key = String(reference?.key || "").trim();
    if (key) out[key] = reference;
  }
  return out;
}
