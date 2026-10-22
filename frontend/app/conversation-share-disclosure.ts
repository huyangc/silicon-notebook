// 会话分享披露计数的纯逻辑（T5,设计 §五/§八）。
//
// 从 `conversation-share-modal.tsx` 抽出来单独成文件,是为了能用 node --test 单测
// (那条 harness 只跑 .test.mjs、能 import .ts 但不能 import 带 JSX 的 .tsx),
// 与 `effort-picker-logic.ts` / `effort-picker.tsx` 的拆分同一手法。**Memory 披露
// 绝不省略**是用户 consent 红线,承重逻辑落在这里,由
// `frontend/tests/unit/conversation-share-disclosure.test.mjs` 钉住(codex T5 评审 P2-1)。
//
// M4:个人记忆的条数**只有服务端一处定义**(`services/share_disclosure.py`,披露端点
// 返回)。它比前端能看到的多——Memory 投影命中的元素/图谱引用没有 `memory_id`,前端数
// 不到——所以这里**不再自己数** Memory:附图仍按轮次统计,记忆条数由调用方传入服务端
// 的数字。前端数出来的数字一旦与服务端并存,就会出现「披露少于实际公开」的那一种错。
//
// 「已分享 vs 新增」的分类判据是 `shared_through_id` 在权威 turn 顺序里的位置,而不是
// created_at 时间戳(codex #522 R3):后端公开快照的 keyset 用 `(created_at, rowid/
// ordinal)` 排序,同刻并列的两条答案里排在水位之后(rowid 更大)的那条算「新增」;纯
// 时间戳分类会把它算成「已分享」→ newCount 少算 → 弹窗隐藏「更新到最新」→ 那条答案
// 永远发布不出去。分类必须与后端 keyset 同源。

/** 披露与边界逻辑吃的那一份「轮次」——**结构性**的最小形状，不是某一个面的 wire 类型。
 *
 *  笔记本内会话原样传 `ConversationDetail["turns"]`（逐字兼容，下面每个函数的行为一个
 *  字没动）；全局问答把 `GlobalJob[]` 适配成同一形状，`answer_id` 位放的是 **job_id**
 *  ——全局侧的分享水位边界是**作业**，不是答案行。
 *
 *  ⚠ 这里刻意不 import 任何一个面的会话类型：披露逻辑是两个面共用的**同一份**实现，
 *  一旦它认得某一面的具体类型，第二个面就只能复制一份近似实现出来，而「Memory 披露
 *  绝不省略」那条红线就会立刻分家成两份、各修一半。 */
/** 公开页最多显示多少轮——后端 `app/domain/conversation_public_view.MAX_TURNS` 的
 *  镜像。公开范围（到水位或边界为止的那段前缀）比它长时，公开页只显示其中**最近**的这么多
 *  轮（被分享的那条回答一定在页上），更早的轮次不在页上、并如实说出轮数（后端
 *  `public_window`）。披露的每个数字都按同一个窗口算，见 `publicWindow`。 */
export const SHARE_SNAPSHOT_MAX_TURNS = 500;

/** 一段公开前缀里公开页实际显示的那几轮的下标：最近的 `SHARE_SNAPSHOT_MAX_TURNS` 个。
 *  与后端 `public_window` 同一个定义——两边谁换了口径，`share_counts_golden.json` 会红。 */
function publicWindow(indices: number[]): number[] {
  return indices.length > SHARE_SNAPSHOT_MAX_TURNS
    ? indices.slice(indices.length - SHARE_SNAPSHOT_MAX_TURNS)
    : indices;
}

export type ShareTurnImage = { asset_id?: string };
export type ShareTurn = {
  answer_id: string;
  created_at: string;
  response?: {
    anchors?: { images?: ShareTurnImage[] }[] | null;
    citations?: { images?: ShareTurnImage[] }[] | null;
  } | null;
};

/** 披露端点（以及会话 409 `share_disclosure_required`）回来的个人记忆条数（服务端口径）。
 *  `memoryCount` = 公开页将包含的、你引用到的不同个人记忆条数；`newMemoryCount` = 其中相对
 *  **当前已公开水位**新增的条数（不是相对作者确认值多出几条）。前端据此推出「当前链接里已有
 *  `memoryCount - newMemoryCount` 条」，所以新增恒 ≤ 总数。 */
export type ServerMemoryDisclosure = { memoryCount: number; newMemoryCount: number };

const isCount = (value: unknown): value is number =>
  typeof value === "number" && Number.isInteger(value) && value >= 0;

/** 读披露端点的回执；形状不对（缺字段、负数、非整数）一律当作取数失败（返回 null），
 *  绝不猜一个数字——取不到就走兜底文案、POST 不带确认值，由服务端 409 带回确数。 */
export function readServerMemoryDisclosure(raw: unknown): ServerMemoryDisclosure | null {
  if (typeof raw !== "object" || raw === null) return null;
  const body = raw as Record<string, unknown>;
  if (!isCount(body.memory_count)) return null;
  const added = body.new_memory_count;
  if (added !== undefined && !isCount(added)) return null;
  // 新增是总数的子集：新增 > 总数是自相矛盾的回执，当作取数失败，绝不把当前条数压成 0 去猜。
  if (added !== undefined && added > body.memory_count) return null;
  return { memoryCount: body.memory_count, newMemoryCount: added ?? 0 };
}

/** 两句披露文案（产品裁决 M4 的字面）。 */
export const shareMemorySentence = (count: number): string =>
  `公开页会包含 ${count} 条你引用到的个人记忆摘录。`;
export const shareUpdateMemorySentence = (count: number, added: number): string =>
  `更新后公开页共 ${count} 条你引用到的个人记忆摘录${added > 0 ? `（新增 ${added} 条）` : ""}。`;

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
  /** 公开范围比公开页长时，页上没有显示的更早轮数（公开页只显示最近
   *  `SHARE_SNAPSHOT_MAX_TURNS` 轮）。普通长度的会话恒为 0。 */
  earlierCount: number;
};

/** 一轮的附图数：anchors ∪ citations 的图按 asset_id 去重（读者每轮看到几张就是几张）。 */
function turnImageCount(turn: ShareTurn): number {
  const response: NonNullable<ShareTurn["response"]> = turn.response || {};
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
  }
  return assetIds.size;
}

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
  turns: ShareTurn[],
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
 * 看到几张就是几张）。K（记忆）：**不在这里数**，由调用方传入服务端披露端点给出的条数
 * `memoryCount`（见文件头 M4）；本函数只把它原样放进结果。
 *
 * 附图计数方向刻意**保守**(宁多勿少):前端统计 anchors ∪ citations,而公开页只投影
 * 被选中的那一支(anchors 有 marker 命中时 else citations),是子集——所以披露数
 * ≥ 公开页实际,绝不出现「公开页有内容而披露没数」(codex T5 评审已核这条不变量)。
 *
 * 附图只数公开页**实际显示**的那些轮次（`publicWindow`：公开范围里最近的
 * `SHARE_SNAPSHOT_MAX_TURNS` 轮）；更早的轮数记在 `earlierCount`。普通长度的会话窗口就是
 * 全部，结果与引入窗口之前逐项相同。
 */
export function summarizeShareDisclosure(
  turns: ShareTurn[],
  sharedThroughId: string,
  watermark: string,
  memoryCount: number,
): ShareDisclosure {
  const shown = sharedWindow(turns, sharedThroughId, watermark);
  let imageCount = 0;
  for (const index of shown.window) imageCount += turnImageCount(turns[index]);
  return {
    sharedCount: shown.shared.length,
    newCount: turns.length - shown.shared.length,
    imageCount,
    memoryCount,
    earlierCount: shown.shared.length - shown.window.length,
  };
}

/** 水位之内的轮次下标（升序），以及其中公开页实际显示的那几轮（`publicWindow`）。 */
function sharedWindow(
  turns: ShareTurn[],
  sharedThroughId: string,
  watermark: string,
): { shared: number[]; window: number[] } {
  const isShared = watermarkClassifier(turns, sharedThroughId, watermark);
  const shared: number[] = [];
  for (let index = 0; index < turns.length; index += 1) {
    if (isShared(index, turns[index].created_at)) shared.push(index);
  }
  return { shared, window: publicWindow(shared) };
}

export type ShareUpdatePreview = {
  /** 「更新到最新」将公开的范围——**全部**轮次(id="")的披露。 */
  afterUpdate: ShareDisclosure;
  /** 相对**当前已公开**(在当前水位之内)的增量:更新会新暴露的去重记忆数。 */
  newMemoryCount: number;
  /** 同上,更新会新暴露的附图数:更新后公开页上、当前公开页还没显示的那些轮次的附图
   *  (按轮求和,不去重)。普通长度的会话里就是「更新后 - 当前」。 */
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
 * `afterUpdate` 是全部轮次(id="")的披露即更新后公开的范围;`newImageCount` 是相对当前
 * 已公开(在当前水位之内)的附图增量,恒 ≥ 0。个人记忆两个数都取自服务端披露端点:
 * `memoryCount` 是更新后的总条数,`newMemoryCount` 是相对已公开范围新增的条数(服务端
 * 按 Memory id 去重后相减,早前轮次公开过、新轮又引用一次的不计入新增)。
 */
export function summarizeShareUpdate(
  turns: ShareTurn[],
  sharedThroughId: string,
  watermark: string,
  memory: ServerMemoryDisclosure,
): ShareUpdatePreview {
  // 更新后公开页显示的轮次里、当前公开页还没显示的那些——公开页只显示最近
  // `SHARE_SNAPSHOT_MAX_TURNS` 轮，所以长会话更新时会有轮次移出窗口，新增附图不能用
  // 「更新后 - 当前」相减（会把移出的那些也减掉，甚至为负）。
  const current = new Set(sharedWindow(turns, sharedThroughId, watermark).window);
  const afterUpdate = summarizeShareDisclosure(turns, "", "", memory.memoryCount);
  let newImageCount = 0;
  for (const index of sharedWindow(turns, "", "").window) {
    if (!current.has(index)) newImageCount += turnImageCount(turns[index]);
  }
  return {
    afterUpdate,
    newMemoryCount: memory.newMemoryCount,
    newImageCount,
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
 *  水位 id 在 turns 里解析不到时**不**判 ahead，但也**不能**判「未越界」——那是
 *  `watermarkUnknown`，第三种状态（codex #530 R2 P1）。两种成因：水位答案被删（那时后端
 *  的回归检查同样会跳过并照常推进，见该方法的 "a current boundary that no longer
 *  resolves ... skips the check and advances"，所以发布必须保持可用），或者**它比我们
 *  读到的 turns 更新**——另一个标签页在我们读完会话详情之后把分享推进到了新一轮。后者
 *  下断言「没越界」就是在承诺一个更窄的范围，而复制按钮当场可用。发布照旧允许（服务端
 *  要么钉住、要么 409 让用户刷新），但范围**不可**声称已知。 */
export type ShareBoundary = {
  /** 边界答案在权威 turn 顺序里的下标；-1 = 未指定边界，或指定了但解析不出。 */
  index: number;
  /** 本次将要发布的那批轮次——截到边界答案（含）为止；未指定边界时是全部轮次。 */
  turns: ShareTurn[];
  /** 指定了边界但 turns 里找不到它 → 披露算不出，退化成不带数字的兜底文案。 */
  unresolved: boolean;
  /** 当前水位已越过边界：公开页已包含这条回答，而且还包含更多（后端不允许收回）。 */
  watermarkAhead: boolean;
  /** `watermarkAhead` 时，公开页比这条边界多包含的轮数。 */
  aheadCount: number;
  /** 水位停在边界**之前**：这条回答目前**还不在**链接里，要按「更新到这一条」才进去。
   *  界面必须点明这一点——链接与复制按钮就在文案下面，照着「已包含」去复制会发出一份
   *  缺了这条回答的快照（codex #530 R2 P2）。 */
  watermarkBehind: boolean;
  /** 水位 id 非空但不在 turns 里：链接的真实范围**未知**（见上面的 doc）。 */
  watermarkUnknown: boolean;
};

/** 边界模式下「当前链接相对这条回答处在什么位置」——文案分支的唯一判据。抽成纯函数是
 *  因为它决定的是一句会被照着复制链接的话，而不是样式（codex #530 R1/R2 三条都出在这里）。 */
export type ShareScopeState = "unshared" | "at" | "behind" | "ahead" | "unknown";

export function shareScopeState(boundary: Omit<ShareBoundary, "turns">, shared: boolean): ShareScopeState {
  if (!shared) return "unshared";
  // 边界本身解析不出时，「这条回答在不在链接里」同样答不了。
  if (boundary.unresolved || boundary.watermarkUnknown) return "unknown";
  if (boundary.watermarkAhead) return "ahead";
  if (boundary.watermarkBehind) return "behind";
  return "at";
}

export function resolveShareBoundary(
  turns: ShareTurn[],
  throughAnswerId: string,
  sharedThroughId: string,
): ShareBoundary {
  const id = String(throughAnswerId || "").trim();
  const markId = String(sharedThroughId || "").trim();
  if (!id) {
    return {
      index: -1,
      turns,
      unresolved: false,
      watermarkAhead: false,
      aheadCount: 0,
      watermarkBehind: false,
      watermarkUnknown: false,
    };
  }
  const index = turns.findIndex((turn) => turn.answer_id === id);
  if (index < 0) {
    // 详情没加载出来，或这条答案已不在会话里。披露一个数都算不出，所以给空批次让
    // 组件走 countsError 兜底文案；**仍可发布**——expected_through_id 就是用户点的
    // 那条 id，不依赖 turns，服务端要么钉在它上面、要么 409。
    return {
      index: -1,
      turns: [],
      unresolved: true,
      watermarkAhead: false,
      aheadCount: 0,
      watermarkBehind: false,
      watermarkUnknown: false,
    };
  }
  const markIndex = markId ? turns.findIndex((turn) => turn.answer_id === markId) : -1;
  // 水位 id 非空却不在 turns 里 → 范围未知，**既不是** ahead 也不是 behind（三值，
  // 不是「不是 ahead 就等于没越界」的二值，codex #530 R2 P1）。
  const unknown = Boolean(markId) && markIndex < 0;
  const ahead = markIndex > index;
  return {
    index,
    turns: turns.slice(0, index + 1),
    unresolved: false,
    watermarkAhead: ahead,
    aheadCount: ahead ? markIndex - index : 0,
    watermarkBehind: markIndex >= 0 && markIndex < index,
    watermarkUnknown: unknown,
  };
}

/** 弹窗据以说话的全部数字（个人记忆条数除外，那个只来自披露端点）。
 *
 *  两个来源、同一个形状：笔记本内会话从完整轮次就地算（`shareCountsFromTurns`，
 *  `GET /conversations/{id}` 不分页）；全局会话由服务端按公开页的同一个窗口在 SQL 里数
 *  （`GET /global-ask/conversations/{id}/share/counts`，`readServerShareCounts`）——再长的
 *  会话也给确数，浏览器不再翻页。后端 `app/domain/share_counts.py` 与这里逐项同一算法，
 *  `backend/tests/fixtures/share_counts_golden.json` 把两边钉在同一组期望值上。 */
export type ShareCounts = {
  /** 本会话共几轮（已完成的轮次）。 */
  totalTurns: number;
  /** 边界那一轮：用户点的那条，或（会话列表入口）最新一条；解析不出时为空串。 */
  throughId: string;
  boundary: Omit<ShareBoundary, "turns">;
  /** 链接此刻的内容（未分享时：按下去会发布的内容）。`memoryCount` 恒为 0，由调用方补。 */
  current: ShareDisclosure;
  /** 分享 / 更新会发布的内容。 */
  update: { imageCount: number; newImageCount: number; earlierCount: number };
};

/** 从完整轮次算出 `ShareCounts`——弹窗接入确数端点之前就地做的那几步，一步没改。 */
export function shareCountsFromTurns(
  turns: ShareTurn[],
  throughAnswerId: string,
  sharedThroughId: string,
  watermark: string,
): ShareCounts {
  const resolved = resolveShareBoundary(turns, throughAnswerId, sharedThroughId);
  const { turns: scopedTurns, ...boundary } = resolved;
  // 水位已越过边界（或水位在本地看不到）时，链接实际公开的是「整条到水位为止」，
  // 所以披露按完整 turns 统计（见弹窗里 `disclosureTurns` 的老注释）。
  const disclosureTurns = (boundary.watermarkAhead || boundary.watermarkUnknown) ? turns : scopedTurns;
  const current = summarizeShareDisclosure(disclosureTurns, sharedThroughId, watermark, 0);
  const update = summarizeShareUpdate(scopedTurns, sharedThroughId, watermark, { memoryCount: 0, newMemoryCount: 0 });
  return {
    totalTurns: turns.length,
    throughId: scopedTurns.length ? scopedTurns[scopedTurns.length - 1].answer_id : "",
    boundary,
    current,
    update: {
      imageCount: update.afterUpdate.imageCount,
      newImageCount: update.newImageCount,
      earlierCount: update.afterUpdate.earlierCount,
    },
  };
}

const COUNT_FIELDS = [
  "total_turns", "boundary_index", "ahead_count", "new_turns", "published_turns", "earlier_turns",
  "image_count", "update_image_count", "update_new_image_count", "update_earlier_turns",
] as const;
const FLAG_FIELDS = ["boundary_unresolved", "watermark_ahead", "watermark_behind", "watermark_unknown"] as const;

/** 读确数端点的回执；形状不对（缺字段、负数、非整数、自相矛盾）一律当作取数失败（null），
 *  弹窗随即说「没能取到确数」——绝不凭半个回执给一个数字。 */
export function readServerShareCounts(raw: unknown): ShareCounts | null {
  if (typeof raw !== "object" || raw === null) return null;
  const body = raw as Record<string, unknown>;
  if (typeof body.through_id !== "string") return null;
  for (const field of COUNT_FIELDS) {
    const value = body[field];
    if (field === "boundary_index" ? !(Number.isInteger(value) && (value as number) >= -1) : !isCount(value)) return null;
  }
  for (const field of FLAG_FIELDS) if (typeof body[field] !== "boolean") return null;
  const count = (field: (typeof COUNT_FIELDS)[number]) => body[field] as number;
  if (count("update_new_image_count") > count("update_image_count")) return null;
  return {
    totalTurns: count("total_turns"),
    throughId: body.through_id,
    boundary: {
      index: count("boundary_index"),
      unresolved: body.boundary_unresolved as boolean,
      watermarkAhead: body.watermark_ahead as boolean,
      aheadCount: count("ahead_count"),
      watermarkBehind: body.watermark_behind as boolean,
      watermarkUnknown: body.watermark_unknown as boolean,
    },
    current: {
      sharedCount: count("published_turns") + count("earlier_turns"),
      newCount: count("new_turns"),
      imageCount: count("image_count"),
      memoryCount: 0,
      earlierCount: count("earlier_turns"),
    },
    update: {
      imageCount: count("update_image_count"),
      newImageCount: count("update_new_image_count"),
      earlierCount: count("update_earlier_turns"),
    },
  };
}

/** 刚把水位钉到 `counts.throughId`（或刚撤销分享）之后的数字，不必再问一次服务端：
 *  两种情形下链接的内容（未分享时：按下去会发布的内容）都正好是边界之前的那段前缀，
 *  也就是 `update` 描述的那一份——新增 0 轮，范围五态回到「正好在边界上」。
 *  由 `conversation-share-disclosure.test.mjs` 对照 `shareCountsFromTurns` 钉住。 */
export function countsAfterPublishing(counts: ShareCounts): ShareCounts {
  // 边界之前的那段前缀有几轮：会话列表入口是整条会话，回答下的入口是到边界为止。
  const scoped = counts.boundary.unresolved ? 0 : counts.boundary.index >= 0 ? counts.boundary.index + 1 : counts.totalTurns;
  return {
    ...counts,
    boundary: { ...counts.boundary, watermarkAhead: false, aheadCount: 0, watermarkBehind: false, watermarkUnknown: false },
    current: {
      sharedCount: scoped,
      newCount: 0,
      imageCount: counts.update.imageCount,
      memoryCount: 0,
      earlierCount: counts.update.earlierCount,
    },
    update: { ...counts.update, newImageCount: 0 },
  };
}

/** 公开范围比公开页长时的那一句（产品裁决 2026-10-22：如实说出更早的轮次没有包含）。 */
export const shareEarlierSentence = (count: number): string =>
  `公开页只显示最近 ${SHARE_SNAPSHOT_MAX_TURNS} 轮，更早的 ${count} 轮未包含。`;
export const shareUpdateEarlierSentence = (count: number): string =>
  `更新后公开页只显示最近 ${SHARE_SNAPSHOT_MAX_TURNS} 轮，更早的 ${count} 轮未包含。`;
