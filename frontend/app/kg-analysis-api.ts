// 图谱分析报告的 API 契约(与 backend/app/models/kg_analysis.py 逐字对齐)+ 取数函数
// (总览 / 来源画像 / 问题清单)与「标为正常」的写入口。
//
// 后端刻意**不产中文文案**:basis / absence / ledger_state / order / units 里全是内部
// 代号,界面词映射在 kg-analysis-model.ts。这份文件只负责「线上是什么形状」。
//
// 读这份契约前必须知道的三件事(它们是这个视图存在的理由,不是可选的细节):
//
// 1. **单位不统一,且不可互比。** 每块数据自带 `units`(字段名 → 单位代号):
//    canonical(合并后的知识对象)/ objects(知识对象行)/ cluster_member_rows /
//    community_pairs(板块对)/ relation_rows / sources / ratio / seq / level。
//    把「来源数」和「对象数」并列成一个数是错的,所以渲染时的单位一律**从这里读**,
//    不在组件里硬写。
// 2. **口径来源必须一起渲染。** `basis` 三值:usable_live(预计算那一刻的实时口径)/
//    community_snapshot(上次主题板块划分)/ unified_rebuild_snapshot(上次整理时的规模)。
//    同屏并列而不标来源,读者没有任何线索分辨为什么数字对不上。
// 3. **缺失 ≠ 为空 ≠ 截断。** `present` 说账本行在不在;`absence` 说为什么不在;
//    产物在场而内容为 0 是另一回事;截断还分落库级(stored_truncated / edge_limit)
//    与请求级(returned / limit)。三者的呈现必须不同。
import { requestJson, requestVoid } from "./api-client.ts";

const options = { tag: "api", unauthorized: "clear-and-reload" as const };

/**
 * 一份数据的新鲜度 + 口径来源 —— **两条相互独立的世代线**。产物缺席时全是 null
 * (不是 0)。
 *
 * `*_seq` 那条是知识对象与关系的写入;`*_cluster_seq` 那条是合并结果。合并的写路径
 * **刻意不动**前一条,所以只看它的话,一次纯合并之后收敛率与最大概念榜单已经陈旧、
 * 报告却会说「与当前一致」。与合并无关的数据(边的出处构成只读关系表)后端给 null,
 * 那半句就不出。
 *
 * `stale` 是两条线的**三值**合取:true = 至少一条明确落后;false = 两条都对齐;
 * **null = 在场却报不出合并世代**(依赖主题板块的两份数据沿用了库里现成的板块划分,
 * 而那批划分建在哪一次合并上没有地方记),或者这份数据压根缺席(靠 `present` 分辨)。
 * null 不等于 false —— 把「无从判断」显示成「与当前一致」正是这个视图存在理由的反面。
 */
export type KgFreshness = {
  basis: string;
  built_at_seq: number | null;
  /** 当前 seq 减去它。**不 clamp**:负值 = 账本比库还新,是异常信号而不是 0。 */
  seq_behind: number | null;
  stale: boolean | null;
  built_at_cluster_seq: number | null;
  cluster_seq_behind: number | null;
};

/** 账本里的一份预计算产物(或它的缺席)。payload 形状随 kind 变,故是自由对象。 */
export type KgArtifactView = {
  kind: string;
  present: boolean;
  optional: boolean;
  absence: string | null;
  freshness: KgFreshness;
  created_at: string;
  units: Record<string, string>;
  payload: Record<string, unknown> | null;
};

export type KgLastRebuild = {
  basis: string;
  at: string;
  object_count: number;
  relation_count: number;
  cluster_count: number;
  units: Record<string, string>;
};

export type KgAnalysisState = {
  present: boolean;
  kg_mutation_seq: number;
  community_seq: number;
  cluster_mutation_seq: number;
  canonical_rel_seq: number;
  dirty: boolean;
  last_rebuild: KgLastRebuild;
  units: Record<string, string>;
};

/** 主题板块列表。payload:{level,total,returned,truncated,limit,top_members_limit,communities[]} */
export type KgBoardOverview = {
  freshness: KgFreshness;
  units: Record<string, string>;
  payload: Record<string, unknown>;
};

export type KgBoardEdge = { src: string; dst: string; weight: number };

/** 跨板块关联的 top-N + **两级截断**的显式披露(落库级 stored_* / 请求级 returned)。 */
export type KgBoardEdgeView = {
  present: boolean;
  freshness: KgFreshness;
  limit: number;
  returned: number;
  returned_weight: number;
  stored: number;
  stored_total: number;
  stored_truncated: boolean;
  edge_limit: number;
  cross_weight: number;
  /** 返回的关联强度 ÷ 全部跨板块关联强度。一条都没有时是 null,不是 0。 */
  weight_coverage: number | null;
  units: Record<string, string>;
  edges: KgBoardEdge[];
};

export type KgAnalysisReport = {
  notebook_id: string;
  generated_at: string;
  level: number;
  ledger_state: string;
  ledger_consistent: boolean;
  state: KgAnalysisState;
  /**
   * **恒定条数、恒定顺序**,缺席的那几份也在里面(带 absence)。问题清单(`findings`)上线后
   * 多一份;它在升级后、第一次整理之前的缺席代号是 `upgrade_pending`(灰色,不是红色)。
   */
  artifacts: KgArtifactView[];
  boards: KgBoardOverview;
  board_edges: KgBoardEdgeView;
  /** 问题清单的账本摘要(只读账本 + 标记,不读明细)。 */
  findings_summary: KgFindingsSummary;
  /** 合并结果建于哪一版合并规则,与当前代码比是否一致(「更新分析」按钮的第一档判据)。 */
  merge_code: KgMergeCodeState;
};

// ------------------------------------------------------------------ 问题清单
//
// 契约与 backend/app/models/kg_analysis.py(FindingsSummary / MergeCodeState / FindingRow /
// FindingsPageResponse)逐字对齐。与上面一样,后端只给内部代号(rule_id / severity /
// subject.kind / actions[].kind / status / handled_reason / reason),界面词全在
// kg-analysis-model.ts。证据 `evidence` 的形状随规则变,是自由对象——逐字段读取,
// 认不出的字段一律不渲染,绝不把代号或英文字段名上屏。

/** 一条问题指向什么:来源 / 来源对 / 概念组 / 全库。label 是发布时的标题或名字快照。 */
export type KgFindingSubject = {
  kind: string;
  key: string;
  label: string;
};

/**
 * 建议动作(从明细派生,不落库)。`source_id` 是删除 / 重新解析对准的那一篇。出不出按钮由
 * 前端按既有入口的同一判据决定(只读成员、镜像库、收录来源、来源此刻在不在),后端仍是权限
 * 的唯一判据。
 */
export type KgFindingAction = {
  kind: string;
  capability: string;
  source_id?: string | null;
};

/** 两条世代线的一对戳(KG 写入 / 合并结果)。 */
export type KgFindingSeq = {
  kg_seq: number | null;
  cluster_seq: number | null;
};

/** 读这一页时,问题涉及的来源此刻的样子(主键点查)。 */
export type KgFindingLiveSource = {
  source_id: string;
  exists: boolean;
  title: string;
  source_type: string;
  /** 正在排队 / 解析 / 抽取。 */
  processing: boolean;
};

export type KgFindingRow = {
  rule_id: string;
  severity: string;
  subject: KgFindingSubject;
  subject_refs: string[];
  evidence: Record<string, unknown>;
  impact: { objects: number };
  actions: KgFindingAction[];
  first_seen: KgFindingSeq;
  /**
   * 读侧派生:open / handled(已处理,更新后核验,仍在待处理)/ dismissed(标为正常)/
   * decided_not_same(F4,已判定不是同一个)/ relink_spent(F5,补上关联已跑过、之后没有
   * 新内容,再跑也补不上)。
   */
  status: string;
  /** handled 的原因:source_deleted / source_processing / source_reprocessed / merge_confirmed / relinked。 */
  handled_reason: string | null;
  dismissed_at: string | null;
  live_sources: KgFindingLiveSource[];
};

/**
 * 三个区各一页:
 *   · `open` —— 待处理:status 为 open 或 handled;
 *   · `passed` —— decided_not_same 与 relink_spent;
 *   · `dismissed` —— 已标为正常。
 */
export type KgFindingZone = "open" | "passed" | "dismissed";

export type KgFindingsPage = {
  notebook_id: string;
  generated_at: string;
  present: boolean;
  absence: string | null;
  freshness: KgFreshness;
  /** 整份清单建于哪一代(不逐行记)。「基于变更 #N」读这里。 */
  built_at: KgFindingSeq | null;
  rules_version: number | null;
  zone: string;
  limit: number;
  offset: number;
  total: number;
  returned: number;
  has_more: boolean;
  units: Record<string, string>;
  /** 每条规则证据里各个数的单位。 */
  evidence_units: Record<string, Record<string, string>>;
  rows: KgFindingRow[];
};

/**
 * 一条规则这一次跑得怎么样(引擎的原字段)。`status` 是 ran / skipped,skipped 时 `reason`
 * 说为什么;各规则另带自己的计数:F1 `sources_checked` / `sources_too_few_concepts`、F2 / F3
 * `sources_checked`、F4 `canonicals_checked` / `groups_decided_apart`、F5 `objects_checked`、
 * S1 `narrow_groups` / `narrow_group_objects`。
 */
export type KgFindingRuleStatus = {
  status: string;
  reason?: string | null;
  total?: number;
  persisted?: number;
  truncated?: boolean;
  [field: string]: unknown;
};

/** 「需要开发处理」区的一项(目前只有 S1)。没有任何按钮。 */
export type KgFindingsDevItem = {
  item_id: string;
  /**
   * 读时判定(按 `unified_kg_state.concept_seed_version`):`fixed_pending_update` = 运行中的代码
   * 已修复、这个库已发布的合并还建在旧规则上,下一次「更新分析」生效;`open` = 仍待开发修复。
   */
  status: string;
  groups: number;
  objects: number;
  /** 例子:每一项是一组只差空格 / 连字符的写法。 */
  samples: Array<{ names: string[] }>;
};

/** S3:只依赖连通分量的结构数(主体内容 / 孤岛),与主题板块划分无关。 */
export type KgFindingsStructure = {
  graph_nodes: number;
  components: number;
  giant_nodes: number;
  usable_objects: number;
  giant_objects: number;
  giant_object_share: number;
  island_components: number;
  island_sources: number;
};

export type KgFindingsResolvedSample = {
  rule_id: string;
  subject_kind: string;
  subject_key: string;
  subject_label: string;
  impact_objects: number;
};

/** 与上一次问题清单的对比。规则版本不同、或任一侧截断/没跑时不可比,`reason` 说为什么。 */
export type KgFindingsComparison = {
  previous_built_at_seq: number | null;
  previous_built_at_cluster_seq: number | null;
  comparable: boolean;
  /** no_previous / rules_version_changed / truncated_or_not_run */
  reason: string | null;
  resolved: number | null;
  persisting: number | null;
  new: number | null;
  resolved_samples: KgFindingsResolvedSample[];
  by_rule: Record<string, unknown>;
};

/** 「补上关联」的持久标记:跑过没有、再跑一次能不能改变结果。 */
export type KgFindingsRelink = {
  attempted: boolean;
  at_kg_seq: number | null;
  at: string | null;
  may_change: boolean | null;
};

export type KgFindingsSummary = {
  present: boolean;
  /** never_computed / unexpected / upgrade_pending(灰色:升级后还没生成)。 */
  absence: string | null;
  freshness: KgFreshness;
  rules_version: number | null;
  built_at: KgFindingSeq | null;
  created_at: string;
  counts: { by_rule: Record<string, number>; dismissed: number } | null;
  /** 按规则代号为键(F1…F5、S1)。 */
  rules: Record<string, KgFindingRuleStatus>;
  dev_items: KgFindingsDevItem[];
  structure: KgFindingsStructure | null;
  comparison: KgFindingsComparison | null;
  relink: KgFindingsRelink | null;
  units: Record<string, string>;
};

/**
 * 合并代码分量:上次合并结果建于哪一版合并规则(`stored`)与当前代码(`current`)。
 * `matches` 为 **null** = 还没合并过(`stored` 为空串,如删除知识图谱之后)—— 那不是
 * 「合并规则已更新」。
 */
export type KgMergeCodeState = {
  stored: string;
  current: string;
  matches: boolean | null;
};

export type KgSourceProfileRow = {
  source_id: string;
  title: string;
  /** 这个来源在来源表里已经不在了(历史清理留下的孤儿引用)。 */
  source_missing: boolean;
  n_objects: number;
  /** 落进主题板块的对象数 —— top_share / mainstream_share 的分母。 */
  n_graph_objects: number;
  top_community_id: string;
  top_share: number;
  community_spread: number;
  mainstream_share: number;
};

export type KgSourceProfilePage = {
  notebook_id: string;
  generated_at: string;
  present: boolean;
  absence: string | null;
  freshness: KgFreshness;
  kg_mutation_seq: number;
  order: string;
  limit: number;
  offset: number;
  total: number;
  returned: number;
  has_more: boolean;
  summary: Record<string, unknown> | null;
  units: Record<string, string>;
  rows: KgSourceProfileRow[];
};

export type KgSourceOrder = "sparse" | "connected";

/**
 * 总览。三个上限(boards ≤200 / top_members ≤20 / edges ≤2000)在**端点层是
 * `Query(..., le=…)`:越界直接 422,不是悄悄 clamp 成上限**。clamp 只发生在
 * service 与 store 那两层(纵深防御,给绕过端点的调用方兜底)。所以这里传的值
 * 必须自己保证合法 —— 传的是**看板实际画得下**的量,不是上限:生产库板块数是万级,
 * 一次拉满既画不出也读不完。
 */
export const fetchKgAnalysis = (
  nb: string,
  { boards = 50, topMembers = 5, edges = 200 } = {},
) =>
  requestJson<KgAnalysisReport>(
    `/notebooks/${nb}/kg-analysis?boards=${boards}&top_members=${topMembers}&edges=${edges}`,
    options,
  );

/**
 * 来源画像的一页。**必须分页**:生产库有 48 836 个来源,一次全返回既是几 MB
 * 也没人读得完;后端硬上限 200/页,分页判据用 has_more 而不是 returned < limit。
 */
export const fetchKgAnalysisSources = (
  nb: string,
  { limit = 20, offset = 0, order = "sparse" as KgSourceOrder } = {},
) =>
  requestJson<KgSourceProfilePage>(
    `/notebooks/${nb}/kg-analysis/sources?limit=${limit}&offset=${offset}&order=${order}`,
    options,
  );

/**
 * 问题清单的一页。端点硬上限 200/页(越界 422);分页判据用 has_more。每一页都带读侧派生的
 * `status`(已处理 / 标为正常 / 已判定不是同一个 / 补上关联已经跑过),所以处置之后要重取这一页,
 * 而不是在前端记住「我刚点过」。
 */
export const fetchKgFindings = (
  nb: string,
  { zone = "open" as KgFindingZone, limit = 20, offset = 0 } = {},
) =>
  requestJson<KgFindingsPage>(
    `/notebooks/${nb}/kg-analysis/findings?zone=${zone}&limit=${limit}&offset=${offset}`,
    options,
  );

// 主键两段都要转义:F2 的主键用「|」拼两个来源 id,F4 的主键是概念名归一后的串(可能含「/」「+」)。
const dismissalPath = (nb: string, ruleId: string, subjectKey: string) =>
  `/notebooks/${nb}/kg-analysis/findings/${encodeURIComponent(ruleId)}/${encodeURIComponent(subjectKey)}/dismissal`;
// 日志里只写路由形状:主键是来源派生的内容(概念名 / 来源 id),不进浏览器日志(URL 编码不算脱敏)。
const dismissalLogPath = (nb: string, ruleId: string) =>
  `/notebooks/${nb}/kg-analysis/findings/${encodeURIComponent(ruleId)}/{subject_key}/dismissal`;

/** 标为正常(需要图谱写权限;镜像库 409;主键不在当前问题清单里 404)。 */
export const dismissKgFinding = (nb: string, ruleId: string, subjectKey: string) =>
  requestVoid(dismissalPath(nb, ruleId, subjectKey), {
    ...options, method: "PUT", logPath: dismissalLogPath(nb, ruleId),
  });

/** 撤销「标为正常」。幂等。 */
export const undismissKgFinding = (nb: string, ruleId: string, subjectKey: string) =>
  requestVoid(dismissalPath(nb, ruleId, subjectKey), {
    ...options, method: "DELETE", logPath: dismissalLogPath(nb, ruleId),
  });
