// 公开分享问答会话的客户端与纯逻辑（无 React、无登录态）。
//
// 它与 `public-report.ts` 是**同一形态的姊妹**：公开页只认一个不可猜的链接口令，
// 不带 session，也不复用任何需要 notebook 上下文的调用。后端返回的就是白名单投影
// （`PublicConversation`），这里不做二次裁剪，只负责取数与呈现所需的最小整形。
//
// 引用编号 helper（`publicReferenceNumber` / `publicCitationRefs`）刻意**不复制**
// 一份实现：它们只吃 `{key,...}` 结构，会话的 `PublicReferenceT` 与报告的
// `PublicReportReferenceT` 逐字段同形，所以这里直接**复用报告那一份**（薄封装），
// 两处编号因此不可能分叉。

import { requestJson } from "./api-client.ts";
import { API_BASE } from "./api-config.ts";
import { httpErrorStatus } from "./errors.ts";
import {
  publicCitationRefs as reportCitationRefs,
  publicReferenceNumber as reportReferenceNumber,
  type PublicCitationRefT,
  type PublicReportReferenceT,
} from "./public-report.ts";

export type { PublicCitationRefT } from "./public-report.ts";

/** 一条引用出处，匿名读者看到的样子。与 `PublicReference`(后端)逐字段同形。 */
export type PublicReferenceT = {
  key: string;
  title: string;
  file_name: string;
  location: string;
  snippet: string;
  /** 标题/摘录超上限被切成前缀时置真——公开页据此显示「已截断」而不静默丢尾。 */
  title_truncated?: boolean;
  snippet_truncated?: boolean;
  file_name_truncated?: boolean;
  /** The reference itself is an image element; its snippet is parser-generated
   * caption/description and must not be repeated as visible prose. */
  is_image_reference?: boolean;
};

/** 一张「本段附图」的公开投影。只带**按链接口令派生的不透明别名**，没有 asset_id。 */
export type PublicImageT = {
  alias: string;
  caption: string;
  reference_keys?: string[];
};

/** 一轮 Q&A 的公开投影。真源：`backend/app/models/ask.py PublicTurn`。 */
export type PublicTurnT = {
  question: string;
  answer_md: string;
  asked_at: string;
  answered_at: string;
  evidence_level: string;
  references: PublicReferenceT[];
  reference_count: number;
  truncated_references: boolean;
  /** C-1：清单卡不进 v1，但绝不静默丢弃——>0 时公开页在该位置留一句可见说明。 */
  omitted_result_sets: number;
  images: PublicImageT[];
};

/** 一条公开分享的问答会话。真源：`backend/app/models/ask.py PublicConversation`。 */
export type PublicConversationT = {
  title: string;
  created_at: string;
  /** 水位时刻——"内容截至何时"，公开页据此说明其后新增的轮次未包含。 */
  shared_at: string;
  turns: PublicTurnT[];
  truncated_turns: boolean;
};

/** 分享链接。链接口令是全部授权，所以 URL 里不带 notebook/conversation id。 */
export function buildPublicConversationLink(token: string, origin: string): string {
  const clean = String(token || "").trim();
  if (!clean) return "";
  return `${String(origin || "").replace(/\/+$/, "")}/c/${encodeURIComponent(clean)}`;
}

/**
 * 一张公开附图的字节地址。
 *
 * 它是 `<img src>`（不走 requestJson），但 URL 前缀必须与 requestJson 同一个
 * API_BASE，否则部署到子路径时会拼错。别名是唯一句柄，链接口令一撤即失去意义。
 */
export function publicConversationImageUrl(token: string, alias: string): string {
  const cleanToken = String(token || "").trim();
  const cleanAlias = String(alias || "").trim();
  if (!cleanToken || !cleanAlias) return "";
  return `${API_BASE}/public/conversations/${encodeURIComponent(cleanToken)}/assets/${encodeURIComponent(cleanAlias)}`;
}

/**
 * 取一份公开会话；未知/已撤销链接口令与不存在无法区分，都返回 null。
 *
 * 走共享 transport 而不是裸 fetch，但用 `auth: "none"`：这是全站唯一不需要 session
 * 的读取，带上凭据反而会让未登录访客在 401 处理上走进已认证应用的分支。
 */
export async function fetchPublicConversation(
  token: string,
): Promise<PublicConversationT | null> {
  const clean = String(token || "").trim();
  if (!clean) return null;
  try {
    const body = await requestJson<PublicConversationT>(
      `/public/conversations/${encodeURIComponent(clean)}`,
      { tag: "public-conversation", auth: "none", cache: "no-store" },
    );
    return {
      ...body,
      turns: (Array.isArray(body.turns) ? body.turns : []).map((turn) => ({
        ...turn,
        references: Array.isArray(turn.references) ? turn.references : [],
        images: Array.isArray(turn.images) ? turn.images : [],
        omitted_result_sets: Number(turn.omitted_result_sets) || 0,
      })),
    };
  } catch (error) {
    if (httpErrorStatus(error) === 404) return null;
    throw error;
  }
}

/**
 * 一条引用对外显示的编号——权威是 key 里的序号（后端已把正文标记重编号成 `kN`）。
 * 复用报告那一份实现：`PublicReferenceT` 与 `PublicReportReferenceT` 同形，所以两条
 * 分享链路的编号口径逐字一致，不可能分叉。
 */
export function publicConversationRefNumber(
  reference: PublicReferenceT | undefined,
  index: number,
): number {
  return reportReferenceNumber(reference as PublicReportReferenceT | undefined, index);
}

/** 正文 `[k]` 标记链接化所需的 key→显示编号映射（复用报告那一份实现）。 */
export function publicConversationCitationRefs(
  references: PublicReferenceT[] | undefined,
): Record<string, PublicCitationRefT> {
  return reportCitationRefs(references as PublicReportReferenceT[] | undefined);
}
