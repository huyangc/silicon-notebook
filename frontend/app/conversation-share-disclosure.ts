// 会话分享披露计数的纯逻辑（T5,设计 §五/§八）。
//
// 从 `conversation-share-modal.tsx` 抽出来单独成文件,是为了能用 node --test 单测
// (那条 harness 只跑 .test.mjs、能 import .ts 但不能 import 带 JSX 的 .tsx),
// 与 `effort-picker-logic.ts` / `effort-picker.tsx` 的拆分同一手法。**Memory 披露
// 绝不省略**是用户 consent 红线,承重逻辑落在这里,由
// `frontend/tests/unit/conversation-share-disclosure.test.mjs` 钉住(codex T5 评审 P2-1)。
//
// 「已分享 vs 新增」的分类判据是 `shared_through_id` 在权威 turn 顺序里的位置,而不是
// created_at 时间戳(codex #522 R3):后端公开快照的 keyset 用 `(created_at, rowid/
// ordinal)` 排序,同刻并列的两条答案里排在水位之后(rowid 更大)的那条算「新增」;纯
// 时间戳分类会把它算成「已分享」→ newCount 少算 → 弹窗隐藏「更新到最新」→ 那条答案
// 永远发布不出去。分类必须与后端 keyset 同源。

import type { ConversationDetail } from "./workspace-model.ts";

// countsError 兜底文案（设计 §五 consent 红线）。会话详情没加载出来时算不出精确数字,
// 但披露的**两个面——附图与个人记忆——缺一不可**:公开页两者都会包含,只提其一等于对
// 另一半静默省略(codex #522 R2 P2)。承重在这里定成常量、由组件 import,并由
// `conversation-share-disclosure.test.mjs` 钉住"两者都提"(node --test 进不了 .tsx,
// 与本文件其余纯逻辑同一拆分理由)。
export const SHARE_DISCLOSURE_COUNTS_ERROR =
  "公开页可能包含引用到的附图与个人记忆摘录（本次未能统计数量）。";
export const SHARE_UPDATE_COUNTS_ERROR =
  "「更新到最新」会公开新增轮次，其中可能包含新引用的附图与个人记忆摘录（本次未能统计数量）。";

export type ShareDisclosure = {
  sharedCount: number;
  newCount: number;
  imageCount: number;
  memoryCount: number;
};

/** 一轮的 created_at 是否落在水位（含）之前。**仅用于**「水位答案已被删、`shared_through_id`
 *  在 turns 里找不到」的兜底分类(镜像后端 `public_conversation_by_token` 的删除兜底:
 *  也回退纯 `created_at` 时刻区间)。正常路径按 id/顺序分类,见 `watermarkClassifier`。
 *  水位为空（未分享）时全部计入——那是按下「分享」将要发布的预览。任一时刻解析失败按
 *  "计入"处理（宁可多披露）。
 *
 *  ⚠ 用 `new Date`（浏览器本地时区）解析,而后端水位谓词用 `julianday`(UTC)。二者
 *  口径不同源,但这条路径只在水位答案被删的边缘情形触发,且只影响**预览计数**:公开页
 *  实际内容 100% 由服务端决定。 */
export function withinWatermark(createdAt: string, watermark: string): boolean {
  if (!watermark) return true;
  const created = new Date(createdAt).getTime();
  const mark = new Date(watermark).getTime();
  if (Number.isNaN(created) || Number.isNaN(mark)) return true;
  return created <= mark;
}

/** 按 `shared_through_id` 在权威 turn 顺序里的位置,返回一个「这一轮是否已在水位之内」
 *  的判据（codex #522 R3）。三种情形:
 *   * id 为空（未分享 / `afterUpdate` 的全部轮次）→ 每一轮都「在内」;
 *   * id 命中 turns 里第 `idx` 条 → `turns[0..idx]` 在内,`turns[idx+1..]` 是新增
 *     (与后端 keyset 逐位一致——顺序即权威,不看时间戳,故同刻并列也不会错分);
 *   * id 非空但 turns 里找不到（水位答案被删/漂移）→ 回退 `withinWatermark` 时间戳
 *     区间,与后端删除兜底同口径,绝不 crash。 */
function watermarkClassifier(
  turns: ConversationDetail["turns"],
  sharedThroughId: string,
  watermark: string,
): (index: number, createdAt: string) => boolean {
  const id = String(sharedThroughId || "").trim();
  if (!id) return () => true;
  const idx = turns.findIndex((turn) => turn.answer_id === id);
  if (idx >= 0) return (index) => index <= idx;
  return (_index, createdAt) => withinWatermark(createdAt, watermark);
}

/**
 * 按水位统计快照的披露计数——纯函数,方便对"Memory 披露不省略"这条红线单测。
 *
 * 分类判据是 `sharedThroughId`（见 `watermarkClassifier`）,`watermark` 时间戳只在
 * 水位答案被删、id 找不到时作兜底,并供组件显示"内容截至何时"。
 *
 * M（附图）：各轮 anchors ∪ citations 里图片按 asset_id 去重后逐轮求和（读者每轮
 * 看到几张就是几张）。K（记忆）：各轮 citations 里 memory_id 非空的按 memory_id
 * 去重（K 条不同的个人记忆,而不是被引用几次）。
 *
 * 计数方向刻意**保守**(宁多勿少):前端统计 anchors ∪ citations,而公开页只投影
 * 被选中的那一支(anchors 有 marker 命中时 else citations),是子集——所以披露数
 * ≥ 公开页实际,绝不出现「公开页有内容而披露没数」(codex T5 评审已核这条不变量)。
 */
export function summarizeShareDisclosure(
  turns: ConversationDetail["turns"],
  sharedThroughId: string,
  watermark: string,
): ShareDisclosure {
  const isShared = watermarkClassifier(turns, sharedThroughId, watermark);
  let sharedCount = 0;
  let newCount = 0;
  let imageCount = 0;
  const memoryIds = new Set<string>();
  for (let index = 0; index < turns.length; index += 1) {
    const turn = turns[index];
    if (!isShared(index, turn.created_at)) {
      newCount += 1;
      continue;
    }
    sharedCount += 1;
    const response = turn.response || ({} as ConversationDetail["turns"][number]["response"]);
    const assetIds = new Set<string>();
    for (const anchor of response.anchors || []) {
      for (const image of anchor.images || []) {
        if (image.asset_id) assetIds.add(image.asset_id);
      }
    }
    for (const citation of response.citations || []) {
      for (const image of citation.images || []) {
        if (image.asset_id) assetIds.add(image.asset_id);
      }
      if (citation.memory_id) memoryIds.add(citation.memory_id);
    }
    imageCount += assetIds.size;
  }
  return { sharedCount, newCount, imageCount, memoryCount: memoryIds.size };
}

export type ShareUpdatePreview = {
  /** 「更新到最新」将公开的范围——**全部**轮次(id="")的披露。 */
  afterUpdate: ShareDisclosure;
  /** 相对**当前已公开**(在当前水位之内)的增量:更新会新暴露的去重记忆数。 */
  newMemoryCount: number;
  /** 同上,更新会新暴露的附图数(按轮求和,不去重)。 */
  newImageCount: number;
};

/**
 * 「更新到最新」的**前瞻**披露——consent 判据是「这个按钮将要公开什么」(设计 §五
 * consent 红线;codex #522 R1 P1)。
 *
 * 已分享的会话点「更新到最新」会把水位推到**全部**轮次,所以按钮在被点击**之前**
 * 就必须显示更新后会公开多少条记忆摘录;否则水位之后新轮引用的私有 Memory 会先被
 * 公开、条数事后才涨——即在**未披露**的情况下公开了新的私有 Memory。
 *
 * `afterUpdate` 是全部轮次(id="")的披露即更新后公开的范围;`newMemoryCount`/
 * `newImageCount` 是相对当前已公开(在当前水位之内)的增量。当前披露恒为 `afterUpdate`
 * 的**子集**(afterUpdate 多算的只是水位之后的新轮),故两个增量恒 ≥ 0——记忆按 id
 * 全局去重,current 的去重集 ⊆ afterUpdate 的去重集,相减即「更新才会新暴露的记忆数」
 * (已在早前轮次公开过、新轮又引用一次的记忆不计入新增)。
 */
export function summarizeShareUpdate(
  turns: ConversationDetail["turns"],
  sharedThroughId: string,
  watermark: string,
): ShareUpdatePreview {
  const current = summarizeShareDisclosure(turns, sharedThroughId, watermark);
  const afterUpdate = summarizeShareDisclosure(turns, "", "");
  return {
    afterUpdate,
    newMemoryCount: afterUpdate.memoryCount - current.memoryCount,
    newImageCount: afterUpdate.imageCount - current.imageCount,
  };
}
