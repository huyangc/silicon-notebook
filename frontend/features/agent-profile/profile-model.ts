/**
 * 「AI 对这个库的理解」面板的纯逻辑与线上形状(P1-T7)。
 *
 * 这个模块刻意只放**没有 React 的东西**:块顺序、字符护栏、忙碌位判据、轮询预算。
 * 组件(`app/agent-profile-panel.tsx`)只负责把它们接到 DOM 上——判据留在这里才有
 * 单测钉得住,搬进组件就只剩渲染测试能碰到它们了(命令目录那条守卫的教训:忙碌
 * 判据一旦内联,「终态才解除」被改成「POST 返回即解除」不会有任何东西报红)。
 *
 * 三条与后端直接对齐的事实,改动前先看后端:
 *
 * 1. **块字符上限 400 的真源是后端** `backend/app/services/agent_profile_block.py`
 *    的 `AGENT_PROFILE_VALUE_MAX_CHARS`,超限那边是明确 422 而不是静默截断。这里
 *    这份是**同口径的前置护栏**(超限直接禁用保存,不让用户白发一次必然失败的请求),
 *    不是权威判定。
 * 2. **GET 不返回没写过的块**:服务端只回真实存在的行,面板自己按顺序补空块,创建
 *    新块时用 `expected_revision: 0`(与 `write_block` 的「行还不存在」约定同一个值)。
 * 3. **`job.status` 永不出现排队态**:认领(claim)直接写 `running`,所以「在忙」的
 *    判据只认 `running` 一个值。
 */
import {
  busyForNotebook,
  claimNotebookSlot,
  releaseNotebookClaim,
} from "../kg-maintenance/notebook-busy-set.ts";

/**
 * 忙碌位的三个纯函数**原样再导出**,不是复制一份实现。
 *
 * 「重新整理」与「补上关联」「重新合并」是同一类后台任务:按笔记本单飞、点完
 * 立刻不可点、解除按证据。那套语义的全部边角(只加自己那一格、只清自己那一格)
 * 已经有单测钉住,复制第二份等于把那些边角重新变成没人看的代码。`kg-relink-status.ts`
 * 也是这样按历史命名再导出同一份实现的,这里沿用同一形态。
 */
export { busyForNotebook, claimNotebookSlot, releaseNotebookClaim };

/** 写端点的两条作用域:共享底座 / 本人的那一份。服务端永远从登录身份解析归属。 */
export type UnderstandingScope = "shared" | "mine";

export type UnderstandingBlock = {
  label: string;
  value: string;
  /** 形状随 label 与写入方而变,面板不消费,原样透传。 */
  evidence: unknown[];
  revision: number;
  updated_at: string;
  updated_origin: string;
};

export type UnderstandingJobStatus = {
  status: string;
  pending: number;
  updated_at: string;
  failure_reason: string;
};

/** 两条链各自的状态。任一侧为 `null` = 这条链从没被认领过(冷启动,不是错误)。 */
export type UnderstandingJobs = {
  base: UnderstandingJobStatus | null;
  mine: UnderstandingJobStatus | null;
};

export type UnderstandingResponse = {
  enabled: boolean;
  base: UnderstandingBlock[];
  mine: UnderstandingBlock[];
  job: UnderstandingJobs;
  can_edit_base: boolean;
};

/**
 * 单块字符上限。真源是后端 `AGENT_PROFILE_VALUE_MAX_CHARS`(见模块开头第 1 条),
 * 两边必须同值:小于后端会拦住合法输入,大于后端会让用户写完才吃 422。
 */
export const AGENT_PROFILE_VALUE_MAX_CHARS = 400;

/** 共享那一份的三块(服务端 `BASE_LABELS`,写它需要笔记本的编辑权)。 */
export const BASE_LABELS = ["corpus_shape", "key_entities", "corpus_gaps"] as const;

/** 本人那一份的两块(服务端 `OVERLAY_LABELS`,有读权就能改自己的)。 */
export const OVERLAY_LABELS = ["retrieval_notes", "usage_gaps"] as const;

/** 五块的固定顺序,与服务端 `PROFILE_LABEL_ORDER` 逐字一致。 */
export const PROFILE_LABEL_ORDER: readonly string[] = [
  ...BASE_LABELS,
  ...OVERLAY_LABELS,
];

/**
 * 每块的界面标题。内部 label 一个字都不上屏——它们是英文枚举名,对用户没有意义,
 * 而且「拿不到中文就直出枚举名」正是 raw-enum-fallback 守卫要挡的形态,所以下面
 * 的取值一律走「查不到就整块不渲染」而不是回落到 label 本身。
 */
export const PROFILE_BLOCK_TITLES: Record<string, string> = {
  corpus_shape: "这个库大致是什么",
  key_entities: "反复出现的关键内容",
  corpus_gaps: "这个库还缺什么",
  retrieval_notes: "怎么查更容易找到",
  usage_gaps: "常问却没找到的",
};

/** 每块的一句说明,写清「这段话是干什么用的」。 */
export const PROFILE_BLOCK_HINTS: Record<string, string> = {
  corpus_shape: "这个库整体上收了哪一类资料。",
  key_entities: "在这个库里反复出现、值得优先关注的名字与主题。",
  corpus_gaps: "已经看出来的空缺，提问时可以避开。",
  retrieval_notes: "你自己的问法经验，只有你能看到，也只对你的提问生效。",
  usage_gaps: "你问过但这个库没给出答案的方向，只有你能看到。",
};

/** 轮询间隔:整理是一次有界的后台任务,4 秒足够跟上。 */
export const UNDERSTANDING_POLL_MS = 4000;

/**
 * 轮询尝试上限。服务端只在进程内记状态,任务真卡死时它会一直如实回报「在跑」,
 * 没有上限的轮询就会一直转下去。超限之后按中性文案收——不猜「大概是成功了」,
 * 也不猜「失败了」。
 */
export const UNDERSTANDING_POLL_MAX_ATTEMPTS = 60;

/** 超过轮询上限时的中性文案:不宣布结局,只说去哪儿看。 */
export const UNDERSTANDING_POLL_GAVE_UP_MESSAGE = "整理可能仍在进行，稍后刷新查看";

/** 一个从没写过的块。`revision: 0` 与服务端「行还不存在」的约定同值(见开头第 2 条)。 */
export function emptyUnderstandingBlock(label: string): UnderstandingBlock {
  return { label, value: "", evidence: [], revision: 0, updated_at: "", updated_origin: "" };
}

/**
 * 把服务端返回的块按固定顺序排好,缺的补成空块。
 *
 * 两件事一起做而不是分两步:服务端只回写过的行(可能一行都没有),而界面必须恒定
 * 显示这一档的每一块——否则「还没整理出来的那块」在界面上会直接消失,用户既看不到
 * 它存在,也就没法手写一条进去。
 */
export function orderedUnderstandingBlocks(
  rows: readonly UnderstandingBlock[],
  labels: readonly string[],
): UnderstandingBlock[] {
  const byLabel = new Map(rows.map((row) => [row.label, row]));
  return labels.map((label) => byLabel.get(label) ?? emptyUnderstandingBlock(label));
}

/**
 * 字符数按**码点**数,不按 UTF-16 码元数。
 *
 * 后端数的是 Python 的 `len()`(码点),`"👍".length` 在 JS 里却是 2。用 `.length`
 * 会让前端在还没到上限时就禁用保存,而用户看不出为什么。
 */
export function understandingValueLength(value: string): number {
  return Array.from(value).length;
}

/** 超限判据,与后端 422 同口径。 */
export function understandingValueTooLong(value: string): boolean {
  return understandingValueLength(value) > AGENT_PROFILE_VALUE_MAX_CHARS;
}

/**
 * 这条链在不在忙。判据只认 `running` 一个值——服务端认领时直接写 `running`,
 * 没有排队态可看(见模块开头第 3 条)。
 */
export function isUnderstandingChainBusy(
  job: UnderstandingJobStatus | null | undefined,
): boolean {
  return job?.status === "running";
}
