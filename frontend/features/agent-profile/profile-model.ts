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

/**
 * 一条「这段话凭什么这么说」的记账。形状随写入方而变,两种都由服务端计算:
 *
 * * 共享底座(`corpus_shape`/`key_entities`/`corpus_gaps`)记 `source_ids`——模型
 *   给的 id 必须逐字出现在服务端喂过去的统计里,对不上的在落库前就被丢掉;
 * * `usage_gaps` 记 `zero_hit_queries`——本人这批提问里有多少次检索空手而归,
 *   由服务端从同一份样本数出来,不是模型复述的数字。
 *
 * ⚠ 类型此前是 `unknown[]` 且从不渲染(codex #520 R2 P2-2):设计契约要的是
 * 「结论可点开来源」,而一个界面上永远看不见的字段等于没有这条契约。字段本身
 * 可能缺失(旧行、别的写入方),所以每一项都是可选的,消费方只读它认得的那些。
 */
export type BlockEvidenceEntry = {
  claim_index?: number;
  source_ids?: unknown;
  zero_hit_queries?: unknown;
};

export type UnderstandingBlock = {
  label: string;
  value: string;
  evidence: readonly BlockEvidenceEntry[];
  revision: number;
  updated_at: string;
  updated_origin: string;
};

/**
 * 这一块背后的来源 id,去重、保持服务端给的顺序。
 *
 * 宽松到底:`evidence` 是服务端 JSON 列里存下来的,历史行、别的写入方、以及
 * `usage_gaps` 那种压根没有 `source_ids` 的形状都会经过这里。任何认不出来的东西
 * 一律跳过而不是抛——一个渲染不出「依据来源」的块只是少一行,一个抛异常的
 * 纯函数会把整个面板打成白屏。
 */
export function evidenceSourceIds(block: UnderstandingBlock): string[] {
  const seen = new Set<string>();
  const ordered: string[] = [];
  for (const entry of block.evidence ?? []) {
    if (!entry || typeof entry !== "object") continue;
    const ids = (entry as BlockEvidenceEntry).source_ids;
    if (!Array.isArray(ids)) continue;
    for (const id of ids) {
      if (typeof id !== "string" || !id || seen.has(id)) continue;
      seen.add(id);
      ordered.push(id);
    }
  }
  return ordered;
}

/**
 * 这一块背后的「检索没找到」次数,没有就是 `null`。
 *
 * `null` 与 `0` 刻意分开:前者是「这一块不记这种依据」(整行不渲染),后者是服务端
 * 真的数出来零次。把两者合并会让 `usage_gaps` 在样本里一次空检索都没有时显示成
 * 「没有依据」,而它恰恰是有依据的——依据是「零次」。
 */
export function zeroHitCount(block: UnderstandingBlock): number | null {
  for (const entry of block.evidence ?? []) {
    if (!entry || typeof entry !== "object") continue;
    const count = (entry as BlockEvidenceEntry).zero_hit_queries;
    if (typeof count !== "number" || !Number.isFinite(count)) continue;
    return count;
  }
  return null;
}

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
 * 一份还没保存的草稿——`baseRevision` 记的是它 fork 自哪一版服务端内容,不是当前
 * 服务端版本。轮询/重取会把 `data` 里的块换成更新的版本,但**不会**主动改这个值:
 * 只有丢掉草稿重新开始编辑,才会捕获一个新的 fork 点(见 `draftIsStale`)。
 */
export type UnderstandingDraft = { value: string; baseRevision: number };

/**
 * 这份草稿是不是已经"陈旧"——它 fork 出去之后,服务端那一行又往前走了一版(被
 * 后台整理、或用户自己另一次保存推进)。陈旧不代表要丢草稿,只代表保存会是一次
 * **知情覆盖**,界面必须显式说清楚而不是静默吃掉差异。
 */
export function draftIsStale(
  block: UnderstandingBlock,
  draft: UnderstandingDraft | undefined,
): boolean {
  return draft !== undefined && block.revision !== draft.baseRevision;
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

// ---------------------------------------------------------------------------
// P3(T5)——「Agent 记录」:调用者自己的 observation 日志,只读/清空,不可编辑。
// 与上面五块「理解」完全独立的第二套小节,共用同一个面板、同一个总闸判据。
// ---------------------------------------------------------------------------

/**
 * 「Agent 记录」的两种行,逐字对齐后端 `agent_observations.kind` 的取值。
 * `note` 是 Agent 自己写下的短句,`call` 是它调用这个库的记账。这是一份**封闭**
 * 词表:清空端点按白名单校验,认不出的值一律 400。
 */
export type AgentRecordKind = "note" | "call";

/** 一条 GET `.../agent-observations` 返回的记录,逐字对齐后端 `AgentObservationOut`。 */
export type AgentObservation = {
  id: string;
  agent_profile_id: string;
  agent_name: string;
  text: string;
  created_at: string;
};

/**
 * 一条 Agent 调用这个库的记账,逐字对齐后端 `AgentCallOut`。
 *
 * 与上面的 `AgentObservation` 是**两种**东西,不是一种的两个变体:那一条带的是
 * Agent 自己写下的话(`text`),这一条带的是系统准许这次调用时用的能力档
 * (`capability`)。合成一个 `text` 字段就等于邀请渲染方把系统记的账当成 Agent
 * 说过的话上屏。
 */
export type AgentCall = {
  id: string;
  agent_profile_id: string;
  agent_name: string;
  capability: string;
  created_at: string;
};

export type AgentObservationsResponse = {
  enabled: boolean;
  items: AgentObservation[];
  /**
   * 调用记录**自己**那把部署开关。与 `enabled`(整个特性的总闸)是两回事:
   * 一个部署可以开着「AI 对这个库的理解」却不记调用,这时面板必须说「这里没
   * 开记录」,而不是把空清单渲染成「从来没有 Agent 调用过这个库」。
   */
  calls_enabled: boolean;
  calls: AgentCall[];
};

/**
 * 真源镜像:后端 `GET .../agent-observations` 的 `limit` 查询参数默认值
 * `AGENT_OBSERVATION_SAMPLE_MAX`(`backend/app/repositories/ports.py`）。
 * 前端从不传 `limit`,响应模型也不回传它用了哪个值(见 `AgentObservationsResponse`
 * 的字段集合),所以「这一页是不是恰好取满了」只能靠这份镜像常量判断——两边
 * 必须同值,改后端默认值时要跟着改这里。
 */
export const AGENT_OBSERVATION_SAMPLE_MAX = 20;

/** 一个 Agent 名下的全部记录,保持服务端给的新到旧顺序不变。 */
export type AgentObservationGroup = {
  agentProfileId: string;
  agentName: string;
  items: AgentObservation[];
};

/**
 * 名字缺失时(该 Agent 的资料已被删除)的占位显示名。空字符串会让分组标题
 * 那一行看起来像是漏渲染了,不如显式说清楚。
 */
const UNKNOWN_AGENT_DISPLAY_NAME = "该 Agent";

/**
 * 按 `agent_profile_id` 分组,组的先后顺序 = 该组第一条记录在原列表里出现的
 * 顺序(即服务端给的新到旧顺序),组内顺序不变。
 */
export function groupObservationsByAgent(
  items: readonly AgentObservation[],
): AgentObservationGroup[] {
  const order: string[] = [];
  const groups = new Map<string, AgentObservationGroup>();
  for (const item of items) {
    const key = item.agent_profile_id;
    let group = groups.get(key);
    if (!group) {
      group = {
        agentProfileId: key,
        agentName: item.agent_name || UNKNOWN_AGENT_DISPLAY_NAME,
        items: [],
      };
      groups.set(key, group);
      order.push(key);
    }
    group.items.push(item);
  }
  return order.map((key) => groups.get(key) as AgentObservationGroup);
}

/**
 * 「Agent 记录」列表用的相对时间,刻度与 `page.tsx` 里 Ask 问答用的
 * `formatRelativeTime` 逐字相同(刚刚/N 分钟前/N 小时前/N 天前/回落到日期),
 * 但独立成一份纯函数——那一个是 `page.tsx` 的模块内私有函数、没有导出,而这个
 * 面板要能脱离 `page.tsx` 单独渲染测试。刻度保持一致是刻意的:同一产品里不该
 * 出现两套「多久之前」的措辞。
 */
export function observationRelativeTime(iso: string): string {
  const then = new Date(iso).getTime();
  if (!Number.isFinite(then)) return "";
  const diffSec = Math.round((Date.now() - then) / 1000);
  if (diffSec < 60) return "刚刚";
  if (diffSec < 3600) return `${Math.floor(diffSec / 60)} 分钟前`;
  if (diffSec < 86400) return `${Math.floor(diffSec / 3600)} 小时前`;
  if (diffSec < 86400 * 30) return `${Math.floor(diffSec / 86400)} 天前`;
  return new Date(then).toLocaleDateString();
}

/**
 * 真源镜像:后端 `GET .../agent-observations` 的 `call_limit` 默认值
 * `AGENT_CALL_SAMPLE_MAX`(`backend/app/repositories/ports.py`)。与上面
 * `AGENT_OBSERVATION_SAMPLE_MAX` 分开一份,理由与后端把两个常量分开的理由
 * 逐字相同:两份清单的写入速率差一个数量级,共用一个数字就等于让其中一侧的
 * 取数宽度替另一侧做决定。
 */
export const AGENT_CALL_SAMPLE_MAX = 20;

/** 一个 Agent 名下的调用记账,保持服务端给的新到旧顺序不变。 */
export type AgentCallGroup = {
  agentProfileId: string;
  agentName: string;
  items: AgentCall[];
};

/**
 * 能力档 → 用户看得懂的一句话。
 *
 * 后端记的是**能力档**而不是工具名(见 `append_call` 的契约:能力档是那个唯一
 * 收口本来就收到的参数,新加的工具因此天然被记上,不会因为作者忘了传而漏记)。
 * 代价是同一档下的不同工具在这里读起来一样——这句译名因此按「这次调用**做了
 * 什么**」写,而不是假装能区分它调的是哪个工具。
 */
const CALL_CAPABILITY_LABELS: Readonly<Record<string, string>> = {
  "ask:execute": "提问",
  "knowledge:read": "查资料",
  "memory:read": "读我的记忆",
  "memory:propose": "提交记忆",
  "knowhow:code": "写表格公式",
  "sources:write": "添加资料",
  "sources:delete": "删除资料",
  "maintenance:execute": "整理这个库",
  "agent_profile:read": "读这个库的理解",
  "agent_observation:write": "写下使用线索",
};

/**
 * 认不出的能力档退回一句中性的话,**绝不**把 `ask:execute` 这种协议串直接上屏。
 * 部署侧插件可以带来这份表里没有的能力档,那不是异常,只是这份表不认识它——
 * 与全站 `vocabulary.ts` 的 `label()` 同一条口径(未命中永不泄漏原值)。
 */
export function callCapabilityLabel(capability: string): string {
  return CALL_CAPABILITY_LABELS[capability] ?? "用了这个库";
}

/**
 * 按 `agent_profile_id` 分组,顺序规则与 `groupObservationsByAgent` 逐字相同。
 * 刻意不与它合成一个泛型函数:两个清单的元素类型不同(`text` vs `capability`),
 * 合成之后调用处要么退化成 `any`,要么多出一个类型参数来省这十行。
 */
export function groupCallsByAgent(items: readonly AgentCall[]): AgentCallGroup[] {
  const order: string[] = [];
  const groups = new Map<string, AgentCallGroup>();
  for (const item of items) {
    const key = item.agent_profile_id;
    let group = groups.get(key);
    if (!group) {
      group = {
        agentProfileId: key,
        agentName: item.agent_name || UNKNOWN_AGENT_DISPLAY_NAME,
        items: [],
      };
      groups.set(key, group);
      order.push(key);
    }
    group.items.push(item);
  }
  return order.map((key) => groups.get(key) as AgentCallGroup);
}

/**
 * 连着的同一能力档折成一行并计数。
 *
 * 一次 reasoning 提问里 Agent 可能连打十几次检索,逐条铺开之后「这个 Agent 在
 * 做什么」反而被淹掉了。折叠**只发生在渲染层**:服务端仍然逐条存,清空、计数与
 * 环形上限都按真实条数走——这里折的是看的方式,不是记的方式。
 *
 * 只折**相邻**的同档调用,不做全局聚合:相邻意味着「这一串是连着发生的」,而把
 * 全天所有 `knowledge:read` 加成一个数字会让时间线不再是时间线。
 */
export type AgentCallRun = {
  id: string;
  capability: string;
  count: number;
  /** 这一串里**最新**那一次的时间——列表是新到旧,所以就是第一条。 */
  created_at: string;
};

export function collapseCallRuns(items: readonly AgentCall[]): AgentCallRun[] {
  const runs: AgentCallRun[] = [];
  for (const item of items) {
    const last = runs[runs.length - 1];
    if (last && last.capability === item.capability) {
      last.count += 1;
      continue;
    }
    runs.push({
      id: item.id,
      capability: item.capability,
      count: 1,
      created_at: item.created_at,
    });
  }
  return runs;
}
