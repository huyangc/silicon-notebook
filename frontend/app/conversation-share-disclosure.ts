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
// 同上，边界模式（每条回答下的分享按钮）的措辞。**必须另起一条常量而不是复用上面那句**：
// 那句写着「更新到最新」，而这个按钮推进到的是用户点的那条回答、不是最新——用户据以决定
// 要不要按下去的那句话，说错了公开范围就等于没披露。
export const SHARE_UPDATE_BOUNDED_COUNTS_ERROR =
  "「更新到这一条」会公开新增轮次，其中可能包含新引用的附图与个人记忆摘录（本次未能统计数量）。";

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

/** 一次「分享到某条回答为止」请求解析出的边界（每条回答下的分享按钮，T6）。
 *
 *  `throughAnswerId` 为空＝会话列表里那个分享按钮的既有语义（整条会话 / 更新到最新），
 *  此时本函数逐字返回原样的 turns，下游一切判定与接入前一致。
 *
 *  `watermarkAhead` 是本类型存在的理由。后端水位是 **advance-only**
 *  （`ask_state_store.share_conversation`，codex #522 R3）：边界排在已发布水位**之前**
 *  的请求一律 `ConversationShareWatermarkStale` → 409，绝不允许把已经公开出去的轮次
 *  再收回来。所以「已分享到第 7 轮、又点第 3 轮下面的分享」在服务端是**做不到**的一件事，
 *  界面必须直接不给发布动作、并说明当前链接已经包含了这条回答——给了按钮只会换回一句
 *  「这条会话已有变化，请刷新后重新分享」，而实际上什么都没变过，那句话是误导。
 *
 *  水位 id 在 turns 里解析不到（水位答案被删）时**不**判 ahead：后端那条回归检查同样
 *  会跳过并照常推进（见该方法的 "a current boundary that no longer resolves ... skips
 *  the check and advances"），此时禁用发布会拦掉一次本来能成的分享。 */
export type ShareBoundary = {
  /** 边界答案在权威 turn 顺序里的下标；-1 = 未指定边界，或指定了但解析不出。 */
  index: number;
  /** 本次将要发布的那批轮次——截到边界答案（含）为止；未指定边界时是全部轮次。 */
  turns: ConversationDetail["turns"];
  /** 指定了边界但 turns 里找不到它 → 披露算不出，退化成不带数字的兜底文案。 */
  unresolved: boolean;
  /** 当前水位已越过边界：公开页已包含这条回答，而且还包含更多（后端不允许收回）。 */
  watermarkAhead: boolean;
  /** `watermarkAhead` 时，公开页比这条边界多包含的轮数。 */
  aheadCount: number;
};

export function resolveShareBoundary(
  turns: ConversationDetail["turns"],
  throughAnswerId: string,
  sharedThroughId: string,
): ShareBoundary {
  const id = String(throughAnswerId || "").trim();
  if (!id) {
    return { index: -1, turns, unresolved: false, watermarkAhead: false, aheadCount: 0 };
  }
  const index = turns.findIndex((turn) => turn.answer_id === id);
  if (index < 0) {
    // 详情没加载出来，或这条答案已不在会话里。披露一个数都算不出，所以给空批次让
    // 组件走 countsError 兜底文案；**仍可发布**——expected_through_id 就是用户点的
    // 那条 id，不依赖 turns，服务端要么钉在它上面、要么 409。
    return { index: -1, turns: [], unresolved: true, watermarkAhead: false, aheadCount: 0 };
  }
  const markId = String(sharedThroughId || "").trim();
  const markIndex = markId ? turns.findIndex((turn) => turn.answer_id === markId) : -1;
  const ahead = markIndex > index;
  return {
    index,
    turns: turns.slice(0, index + 1),
    unresolved: false,
    watermarkAhead: ahead,
    aheadCount: ahead ? markIndex - index : 0,
  };
}
