// 图谱分析报告的 API 契约(与 backend/app/models/kg_analysis.py 逐字对齐)+ 两个只读取数函数。
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
import { requestJson } from "./api-client.ts";

const options = { tag: "api", unauthorized: "clear-and-reload" as const };

/**
 * 一份数据的新鲜度 + 口径来源 —— **两条相互独立的世代线**。产物缺席时全是 null
 * (不是 0)。
 *
 * `*_seq` 那条是知识对象与关系的写入;`*_cluster_seq` 那条是合并结果。合并的写路径
 * **刻意不动**前一条,所以只看它的话,一次纯合并之后收敛率与最大概念榜单已经陈旧、
 * 报告却会说「与当前一致」。与合并无关的数据(边的出处构成只读关系表)后端给 null,
 * 那半句就不出。`stale` 是两条线的合取。
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
  /** **恒五条、恒定顺序**,缺席的那几份也在里面(带 absence)。 */
  artifacts: KgArtifactView[];
  boards: KgBoardOverview;
  board_edges: KgBoardEdgeView;
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
