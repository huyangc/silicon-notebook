// 会话分享弹窗吃的那一份**注入接口**，以及笔记本内会话的实现。
//
// 弹窗（`conversation-share-modal.tsx`）原本自己拼 `/notebooks/{id}/conversations/{id}/share`
// 三个端点、并自己调 `getConversation` 取轮次。全局问答的会话住在另一组端点上
// （`/global-ask/conversations/{id}/share`，水位边界是**作业**而不是答案行），但弹窗
// 正文、五句范围文案、两条披露与全部按钮反馈**一个字都不该有第二份实现**——所以把
// 「怎么读、怎么发、怎么撤、轮次从哪来」整体抽成这一个对象注入进去。
//
// ⚠ `key` 不是装饰：弹窗的加载 effect 以它作依赖。工厂每次渲染都会造一个新对象
// （调用点写起来才不用记得 memo），只有这个字符串是稳定的身份；拿对象本身作依赖会
// 让 effect 每帧重跑，拿空依赖又会让切会话时停在上一条的分享态上。

import {
  getConversation,
  getConversationShare,
  shareConversation,
  unshareConversation,
} from "./ask-api.ts";
import type { ShareTurn } from "./conversation-share-disclosure.ts";
import type { ConversationShareResponse } from "./workspace-model.ts";

export type ConversationShareApi = {
  /** 这条会话的稳定身份（`<面>:<id>`）。弹窗据它决定何时重新加载分享态与轮次。 */
  key: string;
  /** 读回当前分享态。**未分享是 404**（正常态，弹窗据此显示「生成分享链接」）。 */
  load: () => Promise<ConversationShareResponse>;
  /** 披露逻辑要的那批轮次（`ShareTurn`，按时间升序——末条即最新）。 */
  loadTurns: () => Promise<ShareTurn[]>;
  /** 「分享」与「更新到最新/这一条」是**同一个**调用：幂等复用链接口令，同时把水位
   *  钉在 `expectedThroughId` 上。空串回退「当前最新」（会话列表那个入口的旧语义）。 */
  share: (expectedThroughId: string) => Promise<ConversationShareResponse>;
  unshare: () => Promise<void>;
};

/** 笔记本内会话的分享接线。行为与抽取前**逐字相同**——同样的四个调用、同样的顺序、
 *  同样的参数，只是从弹窗内部挪到了这里。 */
export function notebookConversationShareApi(
  notebookId: string,
  conversationId: string,
): ConversationShareApi {
  return {
    key: `notebook:${notebookId}:${conversationId}`,
    load: () => getConversationShare(notebookId, conversationId),
    loadTurns: () => getConversation(conversationId).then((detail) => detail.turns || []),
    share: (expectedThroughId) => shareConversation(notebookId, conversationId, expectedThroughId),
    unshare: () => unshareConversation(notebookId, conversationId),
  };
}
