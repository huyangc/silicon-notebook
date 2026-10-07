// 会话公开分享的两个请求形状，笔记本内与全局问答共用（只此一份）。
//
// 「分享」POST 的请求体：不带确认值时与接入披露之前**逐字节相同**
// （`{"expected_through_id": ...}`），只有作者确认过一个确数时才多一个
// `acknowledged_memory_count`——零条个人记忆的会话，网络上看不出任何新增步骤。
// 409 `share_disclosure_required` 转成带确数的 `ShareDisclosureRequired`（与报告分享
// 同一个类型、同一个解析器，见 report-api.ts），其余失败照常走人话层。

import { performApiRequest, type ApiRequestOptions } from "./api-client.ts";
import { throwShareFailure } from "./report-api.ts";
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
      ? { expected_through_id: expectedThroughId }
      : { expected_through_id: expectedThroughId, acknowledged_memory_count: acknowledgedMemoryCount },
  );
}

export function conversationShareDisclosurePath(sharePath: string, throughId: string): string {
  return `${sharePath}/disclosure?through_id=${encodeURIComponent(throughId)}`;
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
