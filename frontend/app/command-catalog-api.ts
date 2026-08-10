// 命令目录(方案 C·C1c)的 API 契约 + 七个取数函数,与 backend/app/models/command_catalog.py
// 逐字对齐。这份文件只负责「线上是什么形状」;界面词与文案装配在 command-catalog-model.ts。
//
// 读这份契约前必须知道的五件事(它们决定了界面必须长成什么样,不是可选细节):
//
// 1. **preview 的数字是下界,不是普查。** 它只读来源的一段有界前缀(`element_limit`
//    条元素,每条再截断),`sampled=true` 表示到顶了、真实成本更高。所以确认弹窗里
//    的「约 N 个」「预计 M 次」必须写成约数,并在 sampled 时明说只看了前一段。
// 2. **重复发起是 409,且响应只带用户可读文案、不带在跑的 job。** 前端必须自己
//    `GET .../job` 把在跑的那个捡回来,否则界面会停在「没有任务」而后台正在花钱。
// 3. **候选是「未确认」的,apply 才写库。** state 四档:candidate(待审阅)/
//    rejected(接地校验整条拦下)/applied(已确认)/dismissed(未写库,原因经
//    dismiss_reason 带一个稳定原因码:apply 冲突时自动写 conflict_existing_row,
//    经 dismiss 端点显式写 user_dismissed,或来源被重新解析后整批作废写
//    source_reparsed)。一次零产出的识别,用户唯一能自己判断「是模型错了还是
//    这份文档不是手册」的依据就是 rejected。
// 3.5 **来源被重新解析后,这一轮的结果就作废了。** 候选里的命令名/摘录/出处都
//    指向抽取时读到的那一代元素;apply/dismiss 因此会 409(「来源已重新解析…」),
//    并顺手把这个任务剩下的候选整批标成 source_reparsed —— 那一次 409 同时解除
//    「重新识别」的拦截守卫,所以文案里那句「请重新识别」是当场可做的。
//    `preview`/`start` 在来源尚未解析完(或解析失败)时同样 409。
// 4. **apply 一次最多确认一页(100 条),`pending_remaining` 不是装饰。** 300 条候选
//    点一次「确认全部」拿到 rows_added:100 会以为做完了 —— 界面必须据它继续提示。
// 5. **重新识别被待审阅候选拦住时,dismiss 是唯一的放弃出口。** apply 只在候选
//    与目标表冲突时才会自动 dismiss;一条审阅者刻意不想要、又不冲突的候选此前
//    无路可走,会把整个来源永久锁在重新识别之外(见 command-catalog-model.ts 的
//    catalogBlocksRestart)。
import { requestJson } from "./api-client.ts";

const options = { tag: "api", unauthorized: "clear-and-reload" as const };

/** 后端 `apply` 单次确认的硬上限(CATALOG_MAX_CANDIDATE_PAGE)。 */
export const CATALOG_APPLY_MAX = 100;

/** 审阅列表每次取一页的条数。上限同为 100,取小值是为了首屏快。 */
export const CATALOG_PAGE_SIZE = 25;

/**
 * 成本预告(v2:全文档窗口抽取)。
 *
 * `estimated_windows` 是「整篇按字符切成多少个窗口」——窗口按字符打包,所以它是
 * 对来源全文字符数的算术,不是抽样估计。`estimated_calls` 才需要读正文(零成本
 * 跳过闸与每窗分片数都要看文本),所以它只在有界前缀里精确测量、前缀之外每窗
 * 按 1 次计;`sampled=true` 表示前缀到顶了、真实成本更高。`skipped_windows_in_
 * prefix` 是前缀内被零模型调用闸(没有可认领的命令名、也没有 flag 形状参数)挡下
 * 的窗口数——它是「为什么调用数远小于窗口数」唯一的解释项。
 *
 * v1 的 `signal`(形状检测)与 `estimated_sections` 已随规则分节一起退役,与后端
 * 同步不留兼容别名。
 */
export type CommandCatalogPreview = {
  source_id: string;
  source_title: string;
  estimated_windows: number;
  estimated_calls: number;
  skipped_windows_in_prefix: number;
  sampled: boolean;
  element_limit: number;
};

export type CommandCatalogProgress = {
  sections_total: number;
  sections_done: number;
  entries: number;
  rejected: number;
  uncovered: number;
  /**
   * 这个任务的候选里还有多少条是 `state='candidate'`(待审阅、还没确认或跳过)。
   * `entries` 是抽取期写一次就不再变的计数,答不了这个问题——确认/跳过之后
   * `entries` 原样不动。R5 P2:靠它在入口卡片上拦住「上一轮还有候选没审阅就
   * 又发起一轮新识别」——`.../job` 只返回最近一次任务,旧候选会永远够不着。
   */
  pending_candidates: number;
};

/**
 * 一次识别任务。`status`:queued | running | succeeded | failed | cancelled。
 *
 * `failure_reason` 是按 `user_error()` 口径写的中文用户文案,可以原样上屏;内部诊断
 * (拦截样本、异常码)在后端另一列,**根本不进传输层** —— 「能不能给用户看」按出处
 * 判定,不靠前端自觉不渲染。
 */
export type CommandCatalogJob = {
  id: string;
  notebook_id: string;
  source_id: string;
  status: string;
  progress: CommandCatalogProgress;
  failure_reason: string;
  created_at: string;
  updated_at: string;
  finished_at: string;
};

export type CommandCatalogArg = {
  name: string;
  required: boolean;
  desc: string;
  default: string;
};

/** 一处被接地校验丢掉的值:哪个字段、什么值、为什么、以及原文哪一段。 */
export type CommandCatalogRejection = {
  field: string;
  value: string;
  reason: string;
  window: string;
};

export type CommandCatalogCandidate = {
  id: string;
  position: number;
  /**
   * 窗口出处(v2)。有继承的面包屑时是原文标题路径;窗口没有可继承的面包屑时是
   * 后端内部占位标签 `window N`(1 起序号)——那不是 UI 文案,展示层必须经
   * `command-catalog-model.ts` 的 `catalogSectionLabel()` 转成中性说法,不得
   * 原样吐给用户。
   */
  section_path: string;
  command_name: string;
  state: string;
  syntax: string;
  description: string;
  args: CommandCatalogArg[];
  examples: string[];
  anchors: string[];
  excerpt: string;
  suspect_related: boolean;
  rejections: CommandCatalogRejection[];
  /**
   * `dismissed` 候选的跳过原因码(`conflict_existing_row` 或 `user_dismissed`),
   * 来自后端 `reject_info.reason`。与 `rejections`(接地校验逐字段拦截,来自
   * `reject_info.fields`)是两套互不相关的载荷 —— 一条候选只会落进其中一种。
   * 界面词由 command-catalog-model.ts 的 `dismissReasonText()` 负责。
   */
  dismiss_reason: string;
  /**
   * `rejections` 在 `MAX_SECTION_REJECTIONS` 处封顶后,还有多少条被拦截记录
   * 没能进这份列表——有界记账,不是静默丢弃。
   */
  rejections_overflow: number;
  /** 因 `MODEL_ARG_DESC_TOTAL_CHARS` 聚合预算被截短的参数说明(`args[].desc`)段数。 */
  desc_overflow: number;
};

/**
 * 一页候选 + 这个任务各档的**精确**总数。
 *
 * `next_cursor` 是 keyset 游标(上一页最后一条的 position),不是页码:确认候选会改
 * state,页码分页会在筛选后的集合上漏行/重行。`has_more` 由后端多读一行确定,不是
 * 「这页读满了」的推断。
 */
export type CommandCatalogCandidatePage = {
  items: CommandCatalogCandidate[];
  next_cursor: number;
  has_more: boolean;
  counts: Record<string, number>;
};

export type CommandCatalogConflict = {
  candidate_id: string;
  command_name: string;
};

/**
 * 确认落库的结果。
 *
 * `conflicts` 是**没有写入**的那些:目标表里已经有同名命令的行。v1 刻意保守 ——
 * 绝不覆盖用户手工编辑过的内容。`pending_remaining` 是这次确认之后仍待审阅的数量。
 *
 * `table_title` 是这次 apply 实际解析/创建的目标表标题(后端权威——论文来源
 * 优先用接地论文标题而非上传文件名)。前端必须直接显示这个值,不得再用
 * `sourceDetail.title` 自己拼「命令目录：<来源标题>」预测——两者在论文来源
 * 上可以不同,预测会让「已写入《命令目录：<预测名>》」这句话撒谎(R15,codex
 * PR #412 评审第 15 轮)。
 */
export type CommandCatalogApplyResult = {
  table_id: string;
  table_title: string;
  created: boolean;
  applied: string[];
  rows_added: number;
  conflicts: CommandCatalogConflict[];
  pending_remaining: number;
};

/**
 * 跳过的结果。不写 knowhow 表,所以没有 `apply` 结果里的
 * `table_id`/`created`/`conflicts` —— 复用那个形状会强迫这里永远填空/假值。
 * `dismissed` 是这次真正被标记为 `dismissed` 的候选 id(已经不是 `candidate`
 * 状态的静默跳过,不重复报告)。`pending_remaining` 与 apply 同名字段同一
 * 语义 —— 跳过后前端要能立即知道「重新识别」的拦截守卫是否已经解除。
 */
export type CommandCatalogDismissResult = {
  dismissed: string[];
  pending_remaining: number;
};

const base = (notebookId: string, sourceId: string) =>
  `/notebooks/${encodeURIComponent(notebookId)}/sources/${encodeURIComponent(sourceId)}/command-catalog`;

/** 零模型调用的形状检测 + 成本预告。入口第一步,用来弹确认。 */
export const fetchCommandCatalogPreview = (notebookId: string, sourceId: string) =>
  requestJson<CommandCatalogPreview>(`${base(notebookId, sourceId)}/preview`, options);

/**
 * 发起识别。重复发起 → 409(只带用户可读文案,**不带在跑的 job**),
 * 调用方接住后必须自己 `fetchCommandCatalogJob` 把在跑的那个捡回来。
 */
export const startCommandCatalog = (notebookId: string, sourceId: string) =>
  requestJson<{ status: string; job: CommandCatalogJob }>(base(notebookId, sourceId), {
    ...options,
    method: "POST",
  });

/** 该来源最近一次任务(没有过任务时 job 为 null)。轮询与 409 兜底都走它。 */
export const fetchCommandCatalogJob = (notebookId: string, sourceId: string) =>
  requestJson<{ job: CommandCatalogJob | null }>(`${base(notebookId, sourceId)}/job`, options);

/** 取消。status:cancelling(下一个分片边界停)/ cancelled(已落终态)/ not_running。 */
export const cancelCommandCatalog = (notebookId: string, sourceId: string) =>
  requestJson<{ status: string; job: CommandCatalogJob | null }>(
    `${base(notebookId, sourceId)}/cancel`,
    { ...options, method: "POST" },
  );

/** 一页候选(keyset)。`state` 取 candidate | rejected。 */
export const fetchCommandCatalogCandidates = (
  notebookId: string,
  sourceId: string,
  { jobId = "", state = "candidate", cursor = 0, limit = CATALOG_PAGE_SIZE } = {},
) =>
  requestJson<CommandCatalogCandidatePage>(
    `${base(notebookId, sourceId)}/candidates`
      + `?job_id=${encodeURIComponent(jobId)}&state=${encodeURIComponent(state)}`
      + `&cursor=${cursor}&limit=${limit}`,
    options,
  );

/**
 * 把选中的候选落进「命令目录：<来源标题>」表。
 *
 * 二选一:`candidateIds`(显式勾选)或 `allPending`(确认全部待审阅)。两者都会被
 * 后端截到 `CATALOG_APPLY_MAX` 条,所以调用方必须读回 `pending_remaining` 继续。
 */
export const applyCommandCatalog = (
  notebookId: string,
  sourceId: string,
  body: { candidate_ids?: string[]; all_pending?: boolean },
  jobId = "",
) =>
  requestJson<CommandCatalogApplyResult>(
    `${base(notebookId, sourceId)}/apply?job_id=${encodeURIComponent(jobId)}`,
    { ...options, method: "POST", body: JSON.stringify(body) },
  );

/**
 * 把选中的候选标记为「已跳过」,不写任何表。
 *
 * 二选一:`candidateIds`/`allPending`,同 `applyCommandCatalog` 的选择契约与
 * `CATALOG_APPLY_MAX` 上限。「重新识别」被待审阅候选拦住时(见
 * command-catalog-model.ts 的 catalogBlocksRestart),这是唯一的放弃出口 ——
 * apply 只在候选与目标表冲突时才会自动 dismiss。
 */
export const dismissCommandCatalog = (
  notebookId: string,
  sourceId: string,
  body: { candidate_ids?: string[]; all_pending?: boolean },
  jobId = "",
) =>
  requestJson<CommandCatalogDismissResult>(
    `${base(notebookId, sourceId)}/dismiss?job_id=${encodeURIComponent(jobId)}`,
    { ...options, method: "POST", body: JSON.stringify(body) },
  );
