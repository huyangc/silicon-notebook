// 会话公开分享的两个请求形状，笔记本内与全局问答共用（只此一份）。
//
// 网页明确告知原文公开范围后，POST 带 include_original_context 确认重新发布原文。
// 只有作者确认过记忆条数时才附加 acknowledged_memory_count。
// 409 `share_disclosure_required` 转成带确数的 `ShareDisclosureRequired`（与报告分享
// 同一个类型、同一个解析器，见 share-failure.ts），其余失败照常走人话层。

import { performApiRequest, type ApiRequestOptions } from "./api-client.ts";
import { throwShareFailure } from "./share-failure.ts";
import type { ConversationShareResponse } from "./workspace-model.ts";

/** 披露端点的回执：服务端按「即将公开的确切范围」数出来的个人记忆条数。 */
export type ConversationShareDisclosureResponse = {
  memory_count: number;
  new_memory_count: number;
};

export function conversationShareRequestBody(
  expectedThroughId: string,
  acknowledgedMemoryCount?: number,
): string {
  return JSON.stringify(
    acknowledgedMemoryCount === undefined
      ? { expected_through_id: expectedThroughId, include_original_context: true }
      : { expected_through_id: expectedThroughId, acknowledged_memory_count: acknowledgedMemoryCount, include_original_context: true },
  );
}

export function conversationShareDisclosurePath(sharePath: string, throughId: string): string {
  return `${sharePath}/disclosure?through_id=${encodeURIComponent(throughId)}`;
}

/** 确数端点（全局会话）：边界与弹窗此刻持有的链接水位，原样交给服务端；水位为 `null`
 *  时不带水位参数，服务端按它此刻存着的水位数。 */
export function conversationShareCountsPath(
  sharePath: string,
  throughId: string,
  watermark: { id: string; at: string } | null,
): string {
  const query = new URLSearchParams({ through_id: throughId });
  if (watermark) {
    query.set("watermark_id", watermark.id);
    query.set("watermark_at", watermark.at);
  }
  return `${sharePath}/counts?${query.toString()}`;
}

export async function postConversationShare(
  sharePath: string,
  options: ApiRequestOptions,
  expectedThroughId: string,
  acknowledgedMemoryCount?: number,
): Promise<ConversationShareResponse> {
  const res = await performApiRequest(sharePath, {
    ...options,
    method: "POST",
    body: conversationShareRequestBody(expectedThroughId, acknowledgedMemoryCount),
  });
  if (!res.ok) await throwShareFailure(res, options.tag);
  return res.json() as Promise<ConversationShareResponse>;
}
