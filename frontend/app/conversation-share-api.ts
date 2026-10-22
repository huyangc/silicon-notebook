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
  getConversationShareDisclosure,
  shareConversation,
  unshareConversation,
} from "./ask-api.ts";
import type { ConversationShareDisclosureResponse } from "./conversation-share-request.ts";
import type { ShareTurn } from "./conversation-share-disclosure.ts";
import type { ConversationShareResponse } from "./workspace-model.ts";

/** 弹窗的数字从哪来（`ShareCounts`，见 conversation-share-disclosure.ts）。
 *
 *  · `turns`：取回这条会话的**全部**轮次，弹窗就地算。只适用于不分页的读取——笔记本内
 *    会话（`GET /conversations/{id}` 一次取回全部 answers 行）。
 *  · `server`：服务端按公开页的同一个窗口在 SQL 里数（全局会话的
 *    `GET /global-ask/conversations/{id}/share/counts`）。全局会话的轮次是**分页**读的，
 *    浏览器翻页只能翻到一个上界、翻不全就只能说「拿不全」；确数端点让再长的会话也有确数。
 *    参数是弹窗此刻持有的范围：边界（空串 = 最新）与链接水位（未分享时两个都是空串）。
 *    回执原样交给 `readServerShareCounts` 校验，形状不对按取数失败处理。 */
export type ShareCountsSource =
  | { kind: "turns"; load: () => Promise<ShareTurn[]> }
  | { kind: "server"; load: (throughId: string, watermarkId: string, watermarkAt: string) => Promise<unknown> };

export type ConversationShareApi = {
  /** 这条会话的稳定身份（`<面>:<id>`）。弹窗据它决定何时重新加载分享态与轮次。 */
  key: string;
  /** 读回当前分享态。**未分享是 404**（正常态，弹窗据此显示「生成分享链接」）。 */
  load: () => Promise<ConversationShareResponse>;
  /** 弹窗的数字从哪来（见 `ShareCountsSource`）。 */
  counts: ShareCountsSource;
  /** 「分享」与「更新到最新/这一条」是**同一个**调用：幂等复用链接口令，同时把水位
   *  钉在 `expectedThroughId` 上。空串回退「当前最新」（会话列表那个入口的旧语义）。 */
  share: (expectedThroughId: string, acknowledgedMemoryCount?: number) => Promise<ConversationShareResponse>;
  /** 服务端按「即将公开的确切范围」（`throughId`：水位将被钉到的那条回答，空串 = 当前
   *  最新）数出的个人记忆条数。**Memory 的计数只在服务端**——前端不自己数。 */
  loadDisclosure: (throughId: string) => Promise<ConversationShareDisclosureResponse>;
  unshare: () => Promise<void>;
};

/** 笔记本内会话的分享接线。行为与抽取前**逐字相同**——同样的四个调用、同样的顺序、
 *  同样的参数，只是从弹窗内部挪到了这里。
 *
 *  数字就地从轮次算（`counts.kind === "turns"`）：`GET /conversations/{id}` **不分页**，
 *  后端 `ask_state_store.get_conversation` 一次取回这条会话的全部 answers 行（无 LIMIT，
 *  `CONVERSATION_ANSWERS_ORDER_ASC` 升序），所以笔记本侧不存在「只拿到一页」的情形。
 *  这条注释就是那个判据——端点哪天改成分页，这里必须改成 `server`，否则数字会静默变小。 */
export function notebookConversationShareApi(
  notebookId: string,
  conversationId: string,
): ConversationShareApi {
  return {
    key: `notebook:${notebookId}:${conversationId}`,
    load: () => getConversationShare(notebookId, conversationId),
    counts: {
      kind: "turns",
      load: () => getConversation(conversationId).then((detail) => detail.turns || []),
    },
    // 没有确认值时第四个参数是 undefined，`JSON.stringify` 会丢掉它——网络上的请求体与接入
    // 披露之前逐字节相同（由 `conversation-share-requests.component.test.tsx` 钉请求体字节）。
    share: (expectedThroughId, acknowledgedMemoryCount) =>
      shareConversation(notebookId, conversationId, expectedThroughId, acknowledgedMemoryCount),
    loadDisclosure: (throughId) => getConversationShareDisclosure(notebookId, conversationId, throughId),
    unshare: () => unshareConversation(notebookId, conversationId),
  };
}
