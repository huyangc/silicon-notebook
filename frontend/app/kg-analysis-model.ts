// 图谱分析报告:后端内部代号 → 界面词 + 纯装配逻辑。单测于 kg-analysis-model.test.mjs。
//
// 值导入须带 .ts 后缀:本模块被 node --test 直接加载(同 checkup-view.ts / scale-index.ts)。
//
// 这个模块承担四件后端刻意没做的事(后端不产中文文案,见 kg-analysis-api.ts 头注):
//
//   1. **单位**:每块数据自带 `units`(字段名 → 单位代号),渲染时的单位一律从响应里
//      读、经 UNIT_LABEL 映射,**不在组件里硬写**。单位真的不一样——把「来源数」和
//      「对象数」并列成一个数是错的,把 canonical 计数当对象计数去除更错。
//   2. **口径来源**:`basis` 三值同屏并列时必须可分辨,否则「数字对不上」会被读成
//      「其中一个算错了」。
//   3. **缺失 / 为空 / 截断**三者分开:产物没算过、合法缺席、算过但内容为 0、以及
//      两级截断(落库级 + 请求级),呈现必须不同,绝不能都渲染成一个空白或一个 0。
//   4. **规模自适应**:板块数在生产上是万级,俯瞰图只画 top-N + 一个长尾汇总节点,
//      并显式声明覆盖了多少。这里刻意**不**假设板块数量落在某个区间(设计 §二·五
//      约束 3)——1 千个和 10 万个走同一条路径,区别只体现在声明出来的数字上。
import { label } from "./vocabulary.ts";
import { KG_TYPE_LABELS } from "./kg-type-model.ts";
import type {
  KgAnalysisReport,
  KgBoardEdgeView,
  KgBoardOverview,
  KgFindingRow,
  KgFindingRuleStatus,
  KgFindingSeq,
  KgFindingsComparison,
  KgFindingsSummary,
  KgFreshness,
  KgMergeCodeState,
} from "./kg-analysis-api.ts";
import type { PendingMerge } from "./workspace-model.ts";

// ---------------------------------------------------------------- 界面词映射

/** 五份预计算数据的界面名。顺序由后端固定,这里只管叫法。 */
export const ARTIFACT_LABEL: Record<string, string> = {
  cluster_size_histogram: "合并规模分布",
  largest_clusters: "最大的合并对象",
  relation_provenance: "关联出处构成",
  community_edges: "跨板块关联",
  source_profiles: "来源板块画像",
  findings: "问题清单",
};

/** 每份预计算数据在报告里回答什么问题。 */
export const ARTIFACT_PURPOSE: Record<string, string> = {
  cluster_size_histogram: "回答各类知识被合并了多少，用于对象构成与收敛率。",
  largest_clusters: "列出成员最多的概念合并组，用于排查过度合并。",
  relation_provenance: "说明关联由原始内容还是自动补连产生，用于判断关联覆盖方式。",
  community_edges: "统计主题板块之间的关联，用于判断主题边界与孤立程度。",
  source_profiles: "比较每个来源与主体板块的连接，用于发现疑似偏题或关联不足的来源。",
  findings: "按规则找出需要处理的来源与概念，附上证据与建议的处理方式。",
};

/**
 * 单位代号 → 界面词。
 *
 * ⚠ canonical 与 clusters 都指「合并后的知识对象」(前者按节点数、后者按簇数,在这个
 * 报告里数的是同一种东西),故映射到同一个词;cluster_member_rows 是**合并前**的成员
 * 条目,与 objects(全部可用知识对象)也不是一回事——三者混着相除得不到任何有意义的
 * 比例,所以它们各有各的词。
 */
export const UNIT_LABEL: Record<string, string> = {
  objects: "知识对象",
  canonical: "合并后的知识对象",
  clusters: "合并后的知识对象",
  cluster_member_rows: "合并前的成员",
  communities: "主题板块",
  community_pairs: "板块对",
  relation_rows: "关联",
  sources: "来源",
  object_types: "对象类型",
  ratio: "比例",
  seq: "变更序号",
  level: "层级",
};

/** 口径来源代号 → 界面词。同屏并列的数字靠它分辨为什么对不上。 */
export const BASIS_LABEL: Record<string, string> = {
  usable_live: "整理当时的实时口径",
  community_snapshot: "上次主题板块划分",
  unified_rebuild_snapshot: "上次整理时的规模",
};

/** 整份报告的数据齐不齐。 */
export const LEDGER_STATE_LABEL: Record<string, string> = {
  empty: "尚未生成",
  partial: "不完整",
  complete: "齐全",
};

export const SOURCE_ORDER_LABEL: Record<string, string> = {
  sparse: "关联最稀疏的在前",
  connected: "关联最紧密的在前",
};

// ---------------------------------------------------------------- 数字与单位

/** 千分位。刻意不用 toLocaleString:它随运行环境的区域设置变,测试与真机会不一致。 */
export function formatCount(value: number): string {
  const safe = Number.isFinite(value) ? Math.round(value) : 0;
  const sign = safe < 0 ? "-" : "";
  return sign + Math.abs(safe).toString().replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

/**
 * 比例 → 百分数。null 渲染成「—」而不是 0% ——「没有可算的分母」与「算出来是 0」
 * 是两件事,后者会被读成「一个都没覆盖」。
 */
export function formatPercent(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  if (value === 0) return "0%";
  const pct = value * 100;
  if (pct > 0 && pct < 0.1) return "<0.1%";
  if (pct < 0 && pct > -0.1) return ">-0.1%";
  return `${pct.toFixed(1)}%`;
}

/** 单位代号 → 界面词;未知代号退到中性的「项」而不是把英文代号上屏。 */
export function unitLabel(code: string): string {
  return label(UNIT_LABEL, code, "项");
}

/** 口径来源代号 → 界面词;未声明口径的一律说「口径未标注」,不假装知道。 */
export function basisLabel(code: string | null | undefined): string {
  return label(BASIS_LABEL, code ?? "", "口径未标注");
}

/**
 * 某个字段的单位界面词,**从响应的 units 表里查**。
 *
 * 这是「单位必须渲染出来」这条要求的落点:组件不许自己知道 `head_members` 是
 * canonical 计数,只能问响应。units 里没有这个字段(后端加了新字段却没登记单位)时
 * 退到中性词,而不是猜一个。
 */
export function unitFor(units: Record<string, string> | null | undefined, field: string): string {
  if (!units || !Object.hasOwn(units, field)) return "项";
  return unitLabel(units[field]);
}

/** 「9,400 合并前的成员」——计数与它的单位永远一起上屏,不给拆开的机会。 */
export function countText(
  value: number,
  units: Record<string, string> | null | undefined,
  field: string,
): string {
  return `${formatCount(value)} ${unitFor(units, field)}`;
}

// -------------------------------------------------------------------- 新鲜度

export type FreshnessNote = {
  /** 口径来源(「上次主题板块划分」…)。 */
  basis: string;
  /** 建于哪次变更(「建于变更 #128」/「建于变更 #128、合并 #7」/「尚未生成」)。 */
  built: string;
  /**
   * 落后多少 —— **两条世代线合起来说**:「与当前一致」/「落后 12 次变更」/
   * 「落后 3 次合并」/「落后 12 次变更 · 落后 3 次合并」/「比当前新 3 次变更」。
   */
  behind: string;
};

/** 一条世代线的落后量 → 界面词;0 或 null 不出词(合起来怎么说由调用方决定)。 */
function driftClause(behind: number | null | undefined, noun: string): string {
  if (behind === null || behind === undefined || behind === 0) return "";
  if (behind > 0) return `落后 ${formatCount(behind)} 次${noun}`;
  return `比当前新 ${formatCount(-behind)} 次${noun}`;
}

/**
 * 逐指标新鲜度(设计 §3.3,**有真实教训**:有人拿一份陈旧数据推出了关于图结构的
 * 重大结论,随后才得知该库尚未整理,整个推断作废)。
 *
 * 所以每一格数据都要能自证「建于何时、落后多少、什么口径」,而不是靠报告顶部一条
 * 「可能过期」的横幅。三个字段都是 null 时说的是「从没算过」——不是「刚建好」。
 *
 * ⚠ **两条世代线,少说一条就会谎报「与当前一致」。** 变更(知识对象与关系的写入)
 * 与合并(把同一个概念散落的多条记录并成一条)是各自独立会动的两件事:合并单独动过
 * 之后,收敛率与最大概念榜单已经不是屏幕上这一份了,而变更计数一动没动。只说变更那
 * 条线的话,这一格会理直气壮地说「与当前一致」—— 那正是这个视图存在的理由的反面。
 * 与合并无关的数据(边的出处构成只读关系表)后端给 null,这里就不出那半句,而不是
 * 替它编一个恒为 0 的落后量。
 *
 * ⚠ **第三档:在场却报不出合并世代**(`stale === null`)。依赖主题板块的两份数据
 * 沿用了库里现成的板块划分,而那批划分建在哪一次合并上没有地方记 —— 这一格既不是
 * 「与当前一致」也说不出「落后几次合并」,只能如实说不知道。
 */
export function freshnessNote(freshness: KgFreshness | null | undefined): FreshnessNote {
  const basis = basisLabel(freshness?.basis);
  if (!freshness || freshness.built_at_seq === null || freshness.built_at_seq === undefined) {
    return { basis, built: "尚未生成", behind: "" };
  }
  const builtCluster = freshness.built_at_cluster_seq ?? null;
  // ⚠ 在场(上面已排除缺席)而 `stale === null` = **合并世代无从判断**:依赖主题板块
  // 的两份数据由「只补账本」那条路径产出时,板块划分沿用库里现成的,而它建在哪一次
  // 合并上没有地方记。这一档与「这份数据本来就与合并无关」(边的出处构成)长得很像
  // ——两者的 `built_at_cluster_seq` 都是 null——但说的是两件事:后者没有这条线,
  // 前者有这条线却读不出来。不分开说,前者就会显示成「与当前一致」,而那正是后端第 7
  // 轮刚修掉的那句谎话在前端原地复发。
  const mergeUnknown = freshness.stale === null;
  const builtChange = `建于变更 #${formatCount(freshness.built_at_seq)}`;
  const built =
    builtCluster !== null
      ? `${builtChange}、合并 #${formatCount(builtCluster)}`
      : mergeUnknown
        ? `${builtChange}、合并次数未知`
        : builtChange;
  const seqBehind = freshness.seq_behind ?? null;
  const clusterBehind = freshness.cluster_seq_behind ?? null;
  // 两条线都无从判断时不编一句「与当前一致」——那是替读者下一个我们没有依据的结论。
  if (seqBehind === null && clusterBehind === null && !mergeUnknown) {
    return { basis, built, behind: "" };
  }
  const drift = [
    driftClause(seqBehind, "变更"),
    mergeUnknown ? "无法判断落后多少次合并" : driftClause(clusterBehind, "合并"),
  ].filter((clause) => clause !== "");
  return { basis, built, behind: drift.length > 0 ? drift.join(" · ") : "与当前一致" };
}

// ------------------------------------------------------ 载荷读取(防御式)
// payload 在契约上是自由对象(形状随 kind 变),所以这里逐字段取值并做类型收窄,
// 不用 `as` 假装知道形状。缺字段退 0 / 空数组,而不是抛错让整页白屏。

function numberAt(source: unknown, key: string): number {
  if (!source || typeof source !== "object") return 0;
  const value = (source as Record<string, unknown>)[key];
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

function boolAt(source: unknown, key: string): boolean {
  if (!source || typeof source !== "object") return false;
  return (source as Record<string, unknown>)[key] === true;
}

function stringAt(source: unknown, key: string): string {
  if (!source || typeof source !== "object") return "";
  const value = (source as Record<string, unknown>)[key];
  return typeof value === "string" ? value : "";
}

function listAt(source: unknown, key: string): unknown[] {
  if (!source || typeof source !== "object") return [];
  const value = (source as Record<string, unknown>)[key];
  return Array.isArray(value) ? value : [];
}

function stringListAt(source: unknown, key: string): string[] {
  return listAt(source, key).filter((item): item is string => typeof item === "string");
}

// -------------------------------------------------- A1 对象构成 / A2 收敛率

export type ConvergenceRow = {
  key: string;
  /** 类型界面词。四类走 kgTypeLabel(与后端 OBJECT_TYPE_LABELS 同源),其余归「其他类型」。 */
  label: string;
  /** 合并前的成员条目数(单位 cluster_member_rows)。 */
  memberRows: number;
  /** 合并后的知识对象数(单位 clusters)。 */
  clusters: number;
  /** 收敛率 =(合并前 − 合并后)/ 合并前;没有分母时是 null(不是 0)。 */
  rate: number | null;
  /** 占全部成员条目的比例;没有分母时 null。 */
  share: number | null;
  /** 这一组里有几种不同的对象类型(只有「其他」可能 >1)。 */
  objectTypes: number;
};

export type ConvergenceTable = {
  /** 定长定序的分组行(后端保证五组都在,零值组也保留——「其他 = 0」本身是信息)。 */
  rows: ConvergenceRow[];
  /** 全类型合计。 */
  total: ConvergenceRow;
  /** 成员所属对象不可用而被排除的条目数。 */
  excludedMemberRows: number;
  /** 成员全被排除、整个合并对象落空的个数。 */
  emptyClusters: number;
  emptyClusterMemberRows: number;
};

export type LargestClusterRow = {
  id: string;
  name: string;
  members: number;
};

export type LargestClusters = {
  rows: LargestClusterRow[];
  limit: number;
  truncated: boolean;
};

/** 最大概念合并组榜单。名称是用户内容，允许原样展示。 */
export function largestClusters(payload: Record<string, unknown> | null): LargestClusters {
  return {
    rows: listAt(payload, "clusters").map((item) => ({
      id: stringAt(item, "canonical_id"),
      name: stringAt(item, "canonical_name"),
      members: numberAt(item, "members"),
    })),
    limit: numberAt(payload, "limit"),
    truncated: boolAt(payload, "truncated"),
  };
}

const RELATION_BASIS_LABEL: Record<string, string> = {
  "relink:shared-element": "共享出处自动补连",
  "relink:name-match": "同名对象自动补连",
  "relink:other": "其它规则自动补连",
  "tagged:other": "带出处标记的关联",
  untagged: "未标注生成方式的关联",
};

export type RelationProvenanceRow = {
  key: string;
  label: string;
  count: number;
  share: number | null;
};

export type RelationProvenance = {
  rows: RelationProvenanceRow[];
  counted: number;
  relink: number;
  relinkShare: number | null;
  totalRows: number;
  rejected: number;
  endpointUnusable: number;
};

/** 关联生成方式。它描述覆盖结构，不把某一种来源武断地判为更高质量。 */
export function relationProvenance(payload: Record<string, unknown> | null): RelationProvenance {
  const counted = numberAt(payload, "counted");
  const relink = numberAt(payload, "relink");
  const buckets = payload && typeof payload.buckets === "object" && payload.buckets
    ? payload.buckets as Record<string, unknown>
    : {};
  const excluded = payload && typeof payload.excluded === "object" && payload.excluded
    ? payload.excluded as Record<string, unknown>
    : {};
  const rows = Object.keys(RELATION_BASIS_LABEL).map((key) => {
    const raw = buckets[key];
    const count = typeof raw === "number" && Number.isFinite(raw) ? raw : 0;
    return {
      key,
      label: label(RELATION_BASIS_LABEL, key, "其它关联"),
      count,
      share: counted > 0 ? count / counted : null,
    };
  });
  const rejected = typeof excluded.rejected === "number" ? excluded.rejected : 0;
  const endpointUnusable = typeof excluded.endpoint_unusable === "number"
    ? excluded.endpoint_unusable
    : 0;
  return {
    rows,
    counted,
    relink,
    relinkShare: counted > 0 ? relink / counted : null,
    totalRows: numberAt(payload, "total_rows"),
    rejected,
    endpointUnusable,
  };
}

const OTHER_GROUP_LABEL = "其他类型";

function convergenceRow(source: unknown, key: string, text: string, totalRows: number): ConvergenceRow {
  const memberRows = numberAt(source, "member_rows");
  const clusters = numberAt(source, "clusters");
  return {
    key,
    label: text,
    memberRows,
    clusters,
    rate: memberRows > 0 ? (memberRows - clusters) / memberRows : null,
    share: totalRows > 0 ? memberRows / totalRows : null,
    objectTypes: numberAt(source, "object_types"),
  };
}

/**
 * 簇大小分布载荷 → 按类型分列的收敛率表 + 全类型合计。
 *
 * **分列是刻意的,别合并成一个数**:本机库上 concept 的收敛率是 31%,四类混算只有
 * 10%——差 3 倍。claim 占七成行数且几乎全是单元素,混在一起会把 concept 的真实收敛
 * 稀释成噪音(设计 §3.5)。
 */
export function convergenceTable(payload: Record<string, unknown> | null): ConvergenceTable {
  const totalRows = numberAt(payload, "member_rows");
  const groups = listAt(payload, "by_object_type");
  const rows = groups.map((group) => {
    const objectType = stringAt(group, "object_type");
    // 已知四类走 KG_TYPE_LABELS(与后端 OBJECT_TYPE_LABELS 逐字一致的那份);"other"
    // 是后端的显式兜底组名,不是类型名;再有别的取值一律也归「其他类型」——绝不把
    // 英文代号直接上屏(raw-enum-fallback 那条红线)。
    const text = objectType === "other"
      ? OTHER_GROUP_LABEL
      : label(KG_TYPE_LABELS, objectType, OTHER_GROUP_LABEL);
    return convergenceRow(group, objectType || "unknown", text, totalRows);
  });
  return {
    rows,
    total: convergenceRow(payload, "__total__", "合计", totalRows),
    excludedMemberRows: numberAt(payload, "excluded_member_rows"),
    emptyClusters: numberAt(payload, "empty_clusters"),
    emptyClusterMemberRows: numberAt(payload, "empty_cluster_member_rows"),
  };
}

// ------------------------------------------------------------ C1 主题板块列表

export type BoardRow = {
  id: string;
  /** 按规模降序的名次(1 起)。板块 id 是内部代号,不上屏。 */
  rank: number;
  size: number;
  /** 代表对象(top-K);为空说明这个板块一个代表都取不到。 */
  members: string[];
  /** 成员数 > K,列出的只是其中几个。 */
  membersTruncated: boolean;
};

export type BoardList = {
  level: number;
  rows: BoardRow[];
  /** 库里一共有多少个板块。 */
  total: number;
  /** 本次返回了多少个。 */
  returned: number;
  /** 还有多少个没返回(生产库是万级,这个数通常不是 0)。 */
  unreturned: number;
  truncated: boolean;
  limit: number;
  topMembersLimit: number;
  /** 返回的这些板块的成员合计——**不是**全库成员数,未返回板块的规模未知。 */
  returnedMembers: number;
};

export function boardList(boards: KgBoardOverview | null | undefined): BoardList {
  const payload = boards?.payload ?? null;
  const rows = listAt(payload, "communities").map((item, index) => ({
    id: stringAt(item, "id"),
    rank: index + 1,
    size: numberAt(item, "size"),
    members: stringListAt(item, "top_members"),
    membersTruncated: boolAt(item, "top_members_truncated"),
  }));
  const total = numberAt(payload, "total");
  const returned = numberAt(payload, "returned");
  return {
    level: numberAt(payload, "level"),
    rows,
    total,
    returned,
    unreturned: Math.max(0, total - returned),
    truncated: boolAt(payload, "truncated"),
    limit: numberAt(payload, "limit"),
    topMembersLimit: numberAt(payload, "top_members_limit"),
    returnedMembers: rows.reduce((sum, row) => sum + row.size, 0),
  };
}

// ------------------------------------------------------------ E1 板块俯瞰图

/**
 * 一图最多单独画几个板块。板块数在生产上是万级,一次渲染上万个点既画不出也读不懂;
 * 超出的部分并成一个长尾汇总节点,并把「画了几个 / 一共几个 / 还有几个没返回」全部
 * 声明出来。这个常量是**呈现上限**,不是对数据形态的假设——1 千个和 10 万个走同一条
 * 路径(设计 §二·五 约束 3)。
 */
export const BOARD_MAP_MAX_NODES = 24;
export const BOARD_MAP_VIEW = { width: 640, height: 400 };

const MAP_CENTER_X = BOARD_MAP_VIEW.width / 2;
const MAP_CENTER_Y = BOARD_MAP_VIEW.height / 2;
const MAP_RING_RADIUS = 142;
const MAP_MIN_NODE_RADIUS = 7;
const MAP_MAX_NODE_RADIUS = 30;
const MAP_MIN_EDGE_WIDTH = 1;
const MAP_MAX_EDGE_WIDTH = 5;

export type BoardMapNode = {
  key: string;
  kind: "board" | "tail";
  /** 板块 id;长尾节点为空串。 */
  id: string;
  label: string;
  /** 成员数(单位 canonical);长尾节点是它汇总的那些板块的成员合计。 */
  size: number;
  /** 这个节点代表几个板块(单独画出的恒为 1)。 */
  boards: number;
  x: number;
  y: number;
  r: number;
};

export type BoardMapEdge = {
  key: string;
  src: string;
  dst: string;
  weight: number;
  x1: number;
  y1: number;
  x2: number;
  y2: number;
  width: number;
};

export type BoardMapModel = {
  nodes: BoardMapNode[];
  edges: BoardMapEdge[];
  /** 单独画出的板块数。 */
  drawn: number;
  /** 本次返回的板块数。 */
  returned: number;
  /** 库里的板块总数。 */
  total: number;
  /** 返回了、但没单独画出(并进长尾节点)的板块数与它们的成员合计。 */
  tailBoards: number;
  tailMembers: number;
  /** 后端列表上限之外、根本没返回的板块数——它们的规模**未知**,不在长尾里。 */
  unreturned: number;
  /** 单独画出的那些板块的成员合计。 */
  drawnMembers: number;
  /** 画出的板块数 ÷ 板块总数。没有板块时 null(不是 0)。 */
  boardCoverage: number | null;
  /** 本图实际画出的关联条数(两端都在图上的那些)。 */
  drawnEdges: number;
  /** 本次返回的关联条数(请求级上限 limit)。 */
  returnedEdges: number;
  /** 落库的关联条数与截断前的板块对总数(落库级上限 edgeLimit)。 */
  storedEdges: number;
  storedTotalEdges: number;
  storedTruncated: boolean;
  edgeLimit: number;
  requestLimit: number;
  /** 返回的关联强度 ÷ 全部跨板块关联强度;一条都没有时 null(不是 0)。 */
  weightCoverage: number | null;
};

/**
 * 板块列表 + 跨板块关联 → 俯瞰图(一板块一节点、大小按成员数、连线按跨板块关联)。
 *
 * 布局是**确定性**的:按规模降序排在一个圆环上(并列按 id 升序),长尾汇总节点放在
 * 圆心。不跑力导向——同一份数据每次画出来必须一样,否则截图对不上、测试也钉不住。
 *
 * 三级截断分别声明,绝不合并成一个数:
 *   · 本图 top-N(drawn < returned)→ 长尾汇总节点 + tailBoards/tailMembers;
 *   · 后端列表上限(returned < total)→ unreturned,**规模未知**,不并进长尾;
 *   · 关联的落库级与请求级上限 → storedTruncated / edgeLimit / requestLimit,
 *     外加 weightCoverage(返回的关联强度占全部跨板块关联强度的比例)。
 */
export function boardMapModel(
  boards: BoardList,
  edgeView: KgBoardEdgeView | null | undefined,
  maxNodes: number = BOARD_MAP_MAX_NODES,
): BoardMapModel {
  const cap = Math.max(1, Math.floor(maxNodes));
  const sorted = [...boards.rows].sort((a, b) => b.size - a.size || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
  const drawn = sorted.slice(0, cap);
  const tail = sorted.slice(cap);
  const tailMembers = tail.reduce((sum, row) => sum + row.size, 0);
  const drawnMembers = drawn.reduce((sum, row) => sum + row.size, 0);
  const maxSize = drawn.reduce((max, row) => Math.max(max, row.size), 0);

  const radiusOf = (size: number) => {
    if (maxSize <= 0) return MAP_MIN_NODE_RADIUS;
    const scaled = Math.sqrt(Math.max(0, size) / maxSize);
    return MAP_MIN_NODE_RADIUS + (MAP_MAX_NODE_RADIUS - MAP_MIN_NODE_RADIUS) * Math.min(1, scaled);
  };

  const nodes: BoardMapNode[] = drawn.map((row, index) => {
    const angle = (index / Math.max(1, drawn.length)) * Math.PI * 2 - Math.PI / 2;
    return {
      key: row.id || `board-${index}`,
      kind: "board" as const,
      id: row.id,
      label: row.members[0] ?? `板块 ${row.rank}`,
      size: row.size,
      boards: 1,
      x: MAP_CENTER_X + MAP_RING_RADIUS * Math.cos(angle),
      y: MAP_CENTER_Y + MAP_RING_RADIUS * Math.sin(angle),
      r: radiusOf(row.size),
    };
  });
  if (tail.length > 0) {
    nodes.push({
      key: "__tail__",
      kind: "tail",
      id: "",
      label: `其余 ${formatCount(tail.length)} 个板块`,
      size: tailMembers,
      boards: tail.length,
      x: MAP_CENTER_X,
      y: MAP_CENTER_Y,
      r: radiusOf(tailMembers),
    });
  }

  const position = new Map(nodes.filter((node) => node.kind === "board").map((node) => [node.id, node]));
  const rawEdges = edgeView?.edges ?? [];
  const maxWeight = rawEdges.reduce((max, edge) => Math.max(max, edge.weight), 0);
  const edges: BoardMapEdge[] = [];
  rawEdges.forEach((edge, index) => {
    // 长尾节点**不连线**:它是若干板块的汇总,把指向其中某一个的关联画到汇总节点上
    // 会凭空造出一条库里不存在的关联。画不出的那些由 drawnEdges/returnedEdges 的
    // 差额如实交代。
    const from = position.get(edge.src);
    const to = position.get(edge.dst);
    if (!from || !to) return;
    edges.push({
      key: `${edge.src}--${edge.dst}--${index}`,
      src: edge.src,
      dst: edge.dst,
      weight: edge.weight,
      x1: from.x,
      y1: from.y,
      x2: to.x,
      y2: to.y,
      width: maxWeight > 0
        ? MAP_MIN_EDGE_WIDTH + (MAP_MAX_EDGE_WIDTH - MAP_MIN_EDGE_WIDTH) * (edge.weight / maxWeight)
        : MAP_MIN_EDGE_WIDTH,
    });
  });

  return {
    nodes,
    edges,
    drawn: drawn.length,
    returned: boards.returned,
    total: boards.total,
    tailBoards: tail.length,
    tailMembers,
    unreturned: boards.unreturned,
    drawnMembers,
    boardCoverage: boards.total > 0 ? drawn.length / boards.total : null,
    drawnEdges: edges.length,
    returnedEdges: edgeView?.returned ?? 0,
    storedEdges: edgeView?.stored ?? 0,
    storedTotalEdges: edgeView?.stored_total ?? 0,
    storedTruncated: edgeView?.stored_truncated ?? false,
    edgeLimit: edgeView?.edge_limit ?? 0,
    requestLimit: edgeView?.limit ?? 0,
    weightCoverage: edgeView?.weight_coverage ?? null,
  };
}

// ------------------------------------------------------------------ 报告总览

export type ReportHeadline = {
  ledgerState: string;
  /** 当前变更序号。 */
  currentSeq: number;
  /** 板块划分建于哪次变更 + 落后多少。 */
  boards: FreshnessNote;
  dirty: boolean;
};

export function reportHeadline(report: KgAnalysisReport): ReportHeadline {
  return {
    ledgerState: label(LEDGER_STATE_LABEL, report.ledger_state, "状态未知"),
    currentSeq: report.state.kg_mutation_seq,
    boards: freshnessNote(report.boards.freshness),
    dirty: report.state.dirty,
  };
}

/** 一份产物的界面名;后端新增了 kind 而这里没跟上时退到中性词,不把代号上屏。 */
export function artifactLabel(kind: string): string {
  return label(ARTIFACT_LABEL, kind, "报告数据");
}

export function artifactPurpose(kind: string): string {
  return label(ARTIFACT_PURPOSE, kind, "提供这份报告所需的一项统计口径。");
}

// ======================================================= 问题清单(2026-10-19 设计)
//
// 面板主体从「统计 + 模板句」改成**问题清单**:每条问题指向具体来源或概念组,附判据用到的
// 数字、影响面和建议动作(设计 §三/§四)。这里是它的全部纯逻辑——界面词、排序与分区、
// 「更新分析」按钮的状态表(§五 S4)、证据格式化、建议动作的呈现计划。组件只负责画。
//
// 两条贯穿全段的红线:
//   · 后端只给内部代号,这里映射成界面词;认不出的代号/字段**一律不渲染**,不报错,
//     也不把代号或英文字段名原样上屏(raw-enum-fallback 那条)。证据 `evidence` 的字段
//     随规则变,逐字段读取:缺一个字段只少一句话,不会让整条问题消失。
//   · 「检查过没发现」「没法检查」「已标为正常」三件事必须分开说(§3.4),「没法检查」
//     不算通过。

/** 规则编号 → 界面词(§十)。S1 不在这里:它只出现在「需要开发处理」区,不是一条问题。 */
export const FINDING_RULE_LABEL: Record<string, string> = {
  F1: "疑似内容错配",
  F2: "疑似重复导入",
  F3: "疑似解析残缺",
  F4: "疑似没合并的同名概念",
  F5: "没建立关联的内容",
};

/** 严重度只用来排序和配色,不累加成分数(§3.3)。 */
export const FINDING_SEVERITY_LABEL: Record<string, string> = {
  high: "高",
  medium: "中",
  low: "低",
};

const FINDING_SEVERITY_RANK: Record<string, number> = { high: 0, medium: 1, low: 2 };

/** 定位标签:这条问题指向的是什么。 */
export const FINDING_SUBJECT_KIND_LABEL: Record<string, string> = {
  source: "来源",
  source_pair: "两个来源",
  concept_group: "概念组",
  notebook: "全库",
};

/** 已通过区「检查过、没发现」那一行说的是没发现什么。 */
const FINDING_CLEAN_NOUN: Record<string, string> = {
  F1: "内容错配的来源",
  F2: "重复导入的来源",
  F3: "解析残缺的来源",
  F4: "没合并的同名概念",
  F5: "没建立关联的内容",
};

/** 一条规则「没法检查」的原因(这一档不算通过)。 */
export const FINDING_SKIP_REASON_LABEL: Record<string, string> = {
  no_objects: "库里还没有可用的知识对象",
  fewer_than_two_sources: "能互相比较的来源不足两个",
  no_named_concepts: "还没有带名字的概念",
};

/** 「已处理，更新后核验」后面那半句:读侧是凭什么认出来的。 */
export const FINDING_HANDLED_REASON_LABEL: Record<string, string> = {
  source_deleted: "来源已删除",
  source_processing: "来源正在解析",
  source_reprocessed: "来源已重新解析",
  merge_confirmed: "已确认合并",
  relinked: "已补上关联",
};

/** 与上一次问题清单不可比的原因。 */
const FINDING_COMPARISON_REASON_LABEL: Record<string, string> = {
  no_previous: "这是第一份问题清单，还没有可以比较的上一次",
  rules_version_changed: "规则已更新，本次不与上次比较",
  truncated_or_not_run: "上次或这次有规则没能完整检查，本次不与上次比较",
};

/** 引擎状态表里每条规则「检查了多少」的那个计数,以及它的单位代号。 */
const FINDING_CHECKED_FIELD: Record<string, readonly [string, string]> = {
  F1: ["sources_checked", "sources"],
  F2: ["sources_checked", "sources"],
  F3: ["sources_checked", "sources"],
  F4: ["canonicals_checked", "canonical"],
  F5: ["objects_checked", "objects"],
};

const FINDING_RULE_ORDER = ["F1", "F2", "F3", "F4", "F5"];

export function findingRuleLabel(ruleId: string): string {
  return label(FINDING_RULE_LABEL, ruleId, "其它问题");
}

export function findingSeverityLabel(severity: string): string {
  return label(FINDING_SEVERITY_LABEL, severity, "未分级");
}

/** 配色用的严重度档;认不出的落到中性的 unknown,不拿代号拼 class。 */
export function findingSeverityTone(severity: string): "high" | "medium" | "low" | "unknown" {
  return severity === "high" || severity === "medium" || severity === "low" ? severity : "unknown";
}

export function findingSubjectKindLabel(kind: string): string {
  return label(FINDING_SUBJECT_KIND_LABEL, kind, "对象");
}

function finiteNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/** 影响面(单位:知识对象)。 */
export function findingImpact(row: KgFindingRow): number {
  return finiteNumber(row.impact.objects) ?? 0;
}

/** 「影响 1,234 知识对象」—— 单位从这一页的 units 读,没登记时说「知识对象」(契约写明的单位)。 */
export function findingImpactText(row: KgFindingRow, units: Record<string, string> | null | undefined): string {
  const unit = units && Object.hasOwn(units, "impact_objects")
    ? unitLabel(units.impact_objects)
    : UNIT_LABEL.objects;
  return `影响 ${formatCount(findingImpact(row))} ${unit}`;
}

/** 按代码单元比较,与后端 Python 的 str 排序一致(不随运行环境的区域设置变)。 */
function compareText(a: string, b: string): number {
  return a < b ? -1 : a > b ? 1 : 0;
}

/**
 * 显示顺序(§3.3):严重度 → 影响面降序 → 主键 `(rule_id, subject_key)` 升序。后端已经按
 * 这个顺序给了,这里再排一次只是为了**同一页内**顺序确定(稳定排序,后端排好的保持原样)。
 */
export function compareFindings(a: KgFindingRow, b: KgFindingRow): number {
  const rank = (row: KgFindingRow) => (
    Object.hasOwn(FINDING_SEVERITY_RANK, row.severity) ? FINDING_SEVERITY_RANK[row.severity] : 3
  );
  return rank(a) - rank(b)
    || findingImpact(b) - findingImpact(a)
    || compareText(a.rule_id, b.rule_id)
    || compareText(a.subject.key, b.subject.key);
}

export function sortFindings(rows: readonly KgFindingRow[]): KgFindingRow[] {
  return [...rows].sort(compareFindings);
}

export type FindingPartition = {
  /** 待处理:open 与 handled(已处理,更新后核验——不从列表里消失)。 */
  pending: KgFindingRow[];
  /** 已标为正常(可撤销)。 */
  dismissed: KgFindingRow[];
  /** 已通过区列出的行:F4 已判定不是同一个。 */
  passed: KgFindingRow[];
  /**
   * F5 补上关联已经跑过、之后没有新内容(§4.5)。后端把它放在 `passed` 那一页里返回,界面却
   * 放进「诊断数据」的那一块:不在「已通过」列出,也不计入它的条数。
   */
  spent: KgFindingRow[];
};

/**
 * 按读侧派生的 `status` 分进各区(与后端的区划分同一张表,只有 relink_spent 由界面另放)。
 * 认不出的状态按「待处理」放——宁可多给用户看一条,也不把一条问题悄悄藏掉。
 */
export function partitionFindings(rows: readonly KgFindingRow[]): FindingPartition {
  const partition: FindingPartition = { pending: [], dismissed: [], passed: [], spent: [] };
  for (const row of sortFindings(rows)) {
    if (row.status === "dismissed") partition.dismissed.push(row);
    else if (row.status === "decided_not_same") partition.passed.push(row);
    else if (row.status === "relink_spent") partition.spent.push(row);
    else partition.pending.push(row);
  }
  return partition;
}

/** 「已处理，更新后核验」,带上读侧认出来的原因(认不出的原因不说)。 */
export function findingHandledText(row: KgFindingRow): string {
  if (row.status !== "handled") return "";
  const reason = row.handled_reason && Object.hasOwn(FINDING_HANDLED_REASON_LABEL, row.handled_reason)
    ? FINDING_HANDLED_REASON_LABEL[row.handled_reason]
    : "";
  return reason ? `已处理，更新后核验（${reason}）` : "已处理，更新后核验";
}

// --------------------------------------------- 报告可信度(原「先看结论」第一张卡)

export type AnalysisReadiness = {
  tone: "danger" | "warn" | "neutral" | "ok";
  title: string;
  text: string;
};

/**
 * 报告能不能据以下结论。红 = 数字互相对不上;灰 = 没生成 / 升级后还没生成 / 代次无法验证;
 * 黄 = 旧版本;绿 = 可用。
 *
 * ⚠ 「升级后还没生成」(`upgrade_pending`)是灰色,**不是**红色:新版本多了一份数据,已部署的
 * 库在下一次整理之前当然没有它。把它判成「本该有却缺失」会在每一个库上亮红灯。
 */
export function analysisReadiness(report: KgAnalysisReport): AnalysisReadiness {
  const integrity = !report.ledger_consistent || report.artifacts.some((item) => (
    item.absence === "unexpected"
    || (item.freshness.seq_behind ?? 0) < 0
    || (item.freshness.cluster_seq_behind ?? 0) < 0
  ));
  if (integrity) {
    return {
      tone: "danger",
      title: "暂不可据此下结论",
      text: "报告数据有缺失或版本互相对不上。先更新分析；更新后仍出现红色状态时再排查数据完整性。",
    };
  }
  if (report.ledger_state === "empty") {
    return {
      tone: "neutral",
      title: "尚未生成分析",
      text: "当前只有图谱内容，没有质量统计。生成后才会出现问题清单与合并、主题、来源诊断。",
    };
  }
  if (report.findings_summary.absence === "upgrade_pending") {
    return {
      tone: "neutral",
      title: "升级后还没生成问题清单",
      text: "这是升级后新增的数据，这个知识库还没生成过；更新一次分析后，会列出需要处理的问题。",
    };
  }
  const needsUpdate = report.state.dirty || report.artifacts.some((item) => (
    item.present && item.freshness.stale === true
  ));
  if (needsUpdate) {
    return {
      tone: "warn",
      title: "先更新，再判断",
      text: "图谱内容或合并结果在这份报告之后发生过变化；黄色状态描述的是旧版本。",
    };
  }
  if (report.artifacts.some((item) => item.present && item.freshness.stale === null)) {
    return {
      tone: "neutral",
      title: "可用，但有一项无法验证",
      text: "报告与当前图谱内容一致，但旧主题划分没有保存其合并代次，部分合并新鲜度无法核对。这不代表数据已经陈旧，重复更新也不一定消除该提示。",
    };
  }
  return {
    tone: "ok",
    title: "可用于当前判断",
    text: "报告数据齐全，且与当前图谱版本一致。下面的问题与指标可以作为处理依据。",
  };
}

// --------------------------------------------- 「更新分析」按钮的状态表(§五 S4)

export type AnalysisUpdateKind = "rules_updated" | "needs_update" | "unverifiable" | "current";

export type AnalysisUpdateState = {
  kind: AnalysisUpdateKind;
  /** 按下去会不会有任何变化。false 时按钮置灰,旁边写 `reason`。 */
  enabled: boolean;
  label: string;
  /** 写在按钮旁的一句话;可点且没什么要特别说明时为空串。 */
  reason: string;
};

/**
 * 合并结果是不是建于旧的合并规则:只认 `matches === false`。`matches` 为 null 说的是
 * 「这个库此刻没有合并结果」(删除知识图谱、索引管线发布之后存的版本是空串),**不是**
 * 「合并规则已更新」——那一档交给下面的账本不齐 / dirty 去判。
 */
export function mergeRulesChanged(mergeCode: KgMergeCodeState): boolean {
  return mergeCode.matches === false;
}

/**
 * 「更新分析」按钮该不该可点、旁边写什么(§五 S4 那张表,按优先级):
 *
 *   1. 合并结果建于旧的合并规则 → 可点,写「合并规则已更新，需要更新一次」;
 *   2. `dirty`;任一在场产物或板块 `stale === true`(问题清单的规则版本不同也算在它的
 *      新鲜度里);账本不齐(含「升级后还没生成」);数字互相对不上 → 可点;
 *   3. 只剩 `stale === null`(合并代次无从判断)→ 置灰,写为什么普通更新改变不了它;
 *   4. 全部一致 → 置灰,写「已与当前图谱一致，无需更新」。
 *
 * 判据只读报告里已有的新鲜度字段——刻意**不**为它在请求路径上重算聚类输入版本(那要对
 * 对象表和向量表各做一次全表计数)。
 */
export function analysisUpdateState(report: KgAnalysisReport): AnalysisUpdateState {
  const empty = report.ledger_state === "empty";
  const buttonLabel = empty ? "生成分析" : "更新分析";
  if (mergeRulesChanged(report.merge_code)) {
    return { kind: "rules_updated", enabled: true, label: buttonLabel, reason: "合并规则已更新，需要更新一次" };
  }
  const boards = report.boards.freshness;
  const incomplete = empty
    || report.ledger_state !== "complete"
    || !report.findings_summary.present
    || report.artifacts.some((item) => !item.present && item.absence !== "expected");
  const integrity = !report.ledger_consistent || report.artifacts.some((item) => (
    (item.freshness.seq_behind ?? 0) < 0 || (item.freshness.cluster_seq_behind ?? 0) < 0
  ));
  const stale = report.state.dirty
    || report.artifacts.some((item) => item.present && item.freshness.stale === true)
    || boards.stale === true;
  if (incomplete || integrity || stale) {
    return { kind: "needs_update", enabled: true, label: buttonLabel, reason: "" };
  }
  // 从没建过板块的库,板块那一格三个字段全是 null —— 那不是「代次无法验证」,是没有板块。
  const boardsUnknown = boards.stale === null && boards.built_at_seq !== null;
  if (boardsUnknown || report.artifacts.some((item) => item.present && item.freshness.stale === null)) {
    return {
      kind: "unverifiable",
      enabled: false,
      label: buttonLabel,
      reason: "这一项只有在重新划分主题板块时才能核对，普通更新不会改变它",
    };
  }
  return { kind: "current", enabled: false, label: buttonLabel, reason: "已与当前图谱一致，无需更新" };
}

// ------------------------------------------------------------ 报告头部两行

/**
 * 「报告建于变更 #26、合并 #8 · 与当前一致」。问题清单还没生成(或升级后还没生成)时返回空串
 * ——那时头部的可信度一行与待处理区的缺席说明已经说清楚了,不拿空的新鲜度编一句
 * 「与当前一致」,也不重复同一句话。
 */
export function findingsReportLine(summary: KgFindingsSummary): string {
  if (!summary.present) return "";
  const note = freshnessNote(summary.freshness);
  if (note.built === "尚未生成") return "";
  return note.behind ? `报告${note.built} · ${note.behind}` : `报告${note.built}`;
}

export type FindingsComparisonView = {
  comparable: boolean;
  text: string;
  /** 已消失的那几条,用发布时存下的名字快照(来源删了也说得出是谁)。 */
  resolvedNames: string[];
};

function comparisonCount(value: number | null): string {
  return value === null ? "—" : formatCount(value);
}

/** 「上次更新后：N 条已消失 · M 条仍在 · K 条新增」;不可比时写原因,不硬算(§3.2 / §六)。 */
export function findingsComparisonView(
  comparison: KgFindingsComparison | null,
): FindingsComparisonView | null {
  if (!comparison) return null;
  if (!comparison.comparable) {
    return {
      comparable: false,
      text: label(FINDING_COMPARISON_REASON_LABEL, comparison.reason ?? "", "本次不与上次比较"),
      resolvedNames: [],
    };
  }
  const names: string[] = [];
  for (const sample of comparison.resolved_samples) {
    const name = sample.subject_label.trim();
    if (name && !names.includes(name)) names.push(name);
  }
  return {
    comparable: true,
    text: `上次更新后：${comparisonCount(comparison.resolved)} 条已消失 · ${comparisonCount(comparison.persisting)} 条仍在 · ${comparisonCount(comparison.new)} 条新增`,
    resolvedNames: names,
  };
}

/**
 * 报告落后于数据时,每条问题都标「基于变更 #N」,免得读者以为那是此刻的状态(§3.5 末尾)。
 * N 是整份清单建于哪一代(页级 `built_at`,不逐行记)。报告与当前一致时返回空串。
 */
export function findingBasedOnChange(
  builtAt: KgFindingSeq | null,
  summary: KgFindingsSummary,
  dirty: boolean,
): string {
  const freshness = summary.freshness;
  const behind = dirty || freshness.stale === true || (freshness.seq_behind ?? 0) > 0;
  if (!behind) return "";
  const seq = finiteNumber(builtAt?.kg_seq);
  return seq === null ? "基于旧的报告" : `基于变更 #${formatCount(seq)}`;
}

// ------------------------------------------------------------ 证据格式化

export type FindingEvidenceView = {
  /** 判据用到的数字,一句一行。 */
  lines: string[];
  /** 代表名字(代表概念 / 破碎名样例 / 组内名字 / 最多的来源)。 */
  samples: string[];
  sampleLabel: string;
  /** 附注(另一份重复导入、重新解析后可能转为内容错配……)。 */
  notes: string[];
};

function evidenceNumber(evidence: Record<string, unknown>, key: string): number | null {
  return finiteNumber(evidence[key]);
}

function evidenceList(evidence: Record<string, unknown>, key: string): unknown[] {
  const value = evidence[key];
  return Array.isArray(value) ? value : [];
}

function evidenceNames(evidence: Record<string, unknown>, key: string, max = 5): string[] {
  const names: string[] = [];
  for (const item of evidenceList(evidence, key)) {
    const name = typeof item === "string" ? item.trim() : "";
    if (name && !names.includes(name)) names.push(name);
    if (names.length >= max) break;
  }
  return names;
}

function textField(source: unknown, key: string): string {
  if (!source || typeof source !== "object") return "";
  const value = (source as Record<string, unknown>)[key];
  return typeof value === "string" ? value.trim() : "";
}

function numberField(source: unknown, key: string): number | null {
  if (!source || typeof source !== "object") return null;
  return finiteNumber((source as Record<string, unknown>)[key]);
}

/** 关联/对象比这类小数:两位足够看出差距,不装精确。 */
function formatDecimal(value: number): string {
  return value.toFixed(2);
}

const OFF_TOPIC_AFTER_REPARSE = "重新解析后若仍与本库无关，会转为疑似内容错配";

function offTopicEvidence(evidence: Record<string, unknown>, view: FindingEvidenceView) {
  const concepts = evidenceNumber(evidence, "concepts");
  const shared = evidenceNumber(evidence, "shared_share");
  if (concepts !== null && shared !== null) {
    view.lines.push(`${formatCount(concepts)} 个概念里，只有 ${formatPercent(shared)} 也出现在其它来源里`);
  } else if (shared !== null) {
    view.lines.push(`与其它来源共有的概念只占 ${formatPercent(shared)}`);
  }
  // 「在不在主体内容里」只作证据、不作门槛;图里一条关联都没有时后端不给这个数,那就不说。
  const giant = evidenceNumber(evidence, "giant_share");
  if (giant !== null) {
    view.lines.push(giant === 0 ? "不在主体内容里" : `在主体内容里的部分占 ${formatPercent(giant)}`);
  }
  view.samples = evidenceNames(evidence, "samples");
  view.sampleLabel = "代表概念";
  // 两份重复的错文件合成一行(§4.1):另一份挂在这里,两份都建议删除。
  for (const partner of evidenceList(evidence, "partners")) {
    const name = textField(partner, "label");
    view.notes.push(name ? `另有一份重复导入：${name}，两份都建议删除` : "另有一份重复导入，两份都建议删除");
  }
}

function duplicateEvidence(evidence: Record<string, unknown>, view: FindingEvidenceView) {
  const jaccard = evidenceNumber(evidence, "jaccard");
  const shared = evidenceNumber(evidence, "shared_concepts");
  const clauses: string[] = [];
  if (shared !== null) clauses.push(`两份共有 ${formatCount(shared)} 个概念`);
  if (jaccard !== null) clauses.push(`概念重合度 ${formatPercent(jaccard)}`);
  if (clauses.length > 0) view.lines.push(clauses.join("，"));
  for (const side of evidenceList(evidence, "sources")) {
    const name = textField(side, "label") || "（没有标题）";
    const objects = numberField(side, "objects");
    const keep = side && typeof side === "object" ? (side as Record<string, unknown>).keep : undefined;
    const verdict = keep === true ? "（建议保留）" : keep === false ? "（建议删除）" : "";
    view.lines.push(objects === null
      ? `${name}${verdict}`
      : `${name}：${formatCount(objects)} 个知识对象${verdict}`);
  }
  view.samples = evidenceNames(evidence, "samples");
  view.sampleLabel = "共有的代表概念";
}

function parseDamageEvidence(evidence: Record<string, unknown>, view: FindingEvidenceView) {
  // 证据里只放判据真正用到的数(§3.1),所以「字段在」就意味着那条子信号命中。
  const fragmented = evidenceNumber(evidence, "fragmented_names");
  if (fragmented !== null) {
    const share = evidenceNumber(evidence, "fragmented_share");
    view.lines.push(share === null
      ? `${formatCount(fragmented)} 个概念的名字被拆成了单个字符`
      : `${formatCount(fragmented)} 个概念的名字被拆成了单个字符（占这篇来源的 ${formatPercent(share)}）`);
  }
  const relationsPerObject = evidenceNumber(evidence, "relations_per_object");
  if (relationsPerObject !== null) {
    const median = evidenceNumber(evidence, "median_relations_per_object");
    view.lines.push(median === null
      ? `平均每个知识对象只有 ${formatDecimal(relationsPerObject)} 条关联`
      : `平均每个知识对象只有 ${formatDecimal(relationsPerObject)} 条关联，本库中位数是 ${formatDecimal(median)}`);
  }
  const isolated = evidenceNumber(evidence, "isolated_objects");
  if (isolated !== null) {
    const share = evidenceNumber(evidence, "isolated_share");
    // 判据是「高于门槛」(全库中位数的 3 倍与 10% 取大),门槛本身就在证据里:说门槛,
    // 不只说中位数——中位数接近 0 时只给中位数会读成「中位数 0% 也算命中」。
    const threshold = evidenceNumber(evidence, "isolated_share_threshold");
    const tail = [
      share === null ? "" : `占 ${formatPercent(share)}`,
      threshold === null ? "" : `超过本库判为偏多的门槛 ${formatPercent(threshold)}`,
    ].filter(Boolean).join("，");
    view.lines.push(tail
      ? `${formatCount(isolated)} 个知识对象没有任何关联（${tail}）`
      : `${formatCount(isolated)} 个知识对象没有任何关联`);
  }
  view.samples = evidenceNames(evidence, "samples");
  view.sampleLabel = "破碎的名字";
  if (evidence.also_off_topic === true) view.notes.push(OFF_TOPIC_AFTER_REPARSE);
}

function underMergeEvidence(evidence: Record<string, unknown>, view: FindingEvidenceView) {
  const shown: string[] = [];
  for (const member of evidenceList(evidence, "members")) {
    const name = textField(member, "name");
    if (!name) continue;
    const objects = numberField(member, "objects");
    shown.push(objects === null ? name : `${name}（${formatCount(objects)}）`);
    if (shown.length >= 5) break;
  }
  const spellings = evidenceNumber(evidence, "spellings");
  const sources = evidenceNumber(evidence, "member_sources");
  const clauses: string[] = [];
  if (spellings !== null) clauses.push(`同一个概念有 ${formatCount(spellings)} 种写法没有合并`);
  if (sources !== null) clauses.push(`涉及 ${formatCount(sources)} 个来源`);
  if (clauses.length > 0) view.lines.push(clauses.join("，"));
  view.samples = shown;
  view.sampleLabel = "组内的名字（知识对象数）";
}

function isolatedEvidence(evidence: Record<string, unknown>, view: FindingEvidenceView) {
  const isolated = evidenceNumber(evidence, "isolated_objects");
  const usable = evidenceNumber(evidence, "usable_objects");
  const share = evidenceNumber(evidence, "isolated_share");
  if (isolated !== null) {
    view.lines.push(usable !== null && share !== null
      ? `${formatCount(isolated)} 个知识对象没有任何关联，占全部 ${formatCount(usable)} 个的 ${formatPercent(share)}`
      : `${formatCount(isolated)} 个知识对象没有任何关联`);
  }
  const top: string[] = [];
  for (const item of evidenceList(evidence, "top_sources")) {
    const name = textField(item, "label") || "（没有标题）";
    const count = numberField(item, "isolated_objects");
    top.push(count === null ? name : `${name}（${formatCount(count)}）`);
    if (top.length >= 5) break;
  }
  view.samples = top;
  view.sampleLabel = "最多的来源（没有关联的知识对象数）";
}

/**
 * 一条问题的证据 → 几句人话。只渲染判据真正用到的数(§3.1);认不出的规则不编证据。
 */
export function findingEvidenceView(row: KgFindingRow): FindingEvidenceView {
  const evidence = row.evidence;
  const view: FindingEvidenceView = { lines: [], samples: [], sampleLabel: "", notes: [] };
  if (row.rule_id === "F1") offTopicEvidence(evidence, view);
  else if (row.rule_id === "F2") duplicateEvidence(evidence, view);
  else if (row.rule_id === "F3") parseDamageEvidence(evidence, view);
  else if (row.rule_id === "F4") underMergeEvidence(evidence, view);
  else if (row.rule_id === "F5") isolatedEvidence(evidence, view);
  return view;
}

/** 定位标签后面那段名字。F5 是全库一条;没有名字时如实说没有,不拿主键充数。 */
export function findingSubjectText(row: KgFindingRow): string {
  if (row.subject.kind === "notebook") return "全库";
  const text = row.subject.label.trim();
  if (text) return text;
  const source = row.live_sources.find((item) => item.exists && item.title.trim());
  return source ? source.title.trim() : "（没有标题）";
}

// ------------------------------------------------------------ 建议动作的呈现计划

export type FindingButtonSpec =
  | { kind: "open_source"; sourceId: string; title: string }
  | { kind: "delete_source"; sourceId: string; title: string; sourceType: string }
  | { kind: "reparse_source"; sourceId: string; title: string; sourceType: string }
  | { kind: "review_merge"; mergeId: string }
  | { kind: "relink" };

export type FindingActionPlan = {
  buttons: FindingButtonSpec[];
  /** 写在按钮旁的说明(「不可撤销，先打开来源核对」……)。只在有对应按钮时出现。 */
  notes: string[];
  /** 不出按钮的原因(待确认合并里没有这一组……)。 */
  blocked: string[];
  /** 正在解析的来源:对准它们的写按钮置灰,旁边写原因。 */
  processingSourceIds: string[];
};

export type FindingActionContext = {
  /** 可编辑成员(只读成员、镜像库都是 false):false 时一个按钮都不出(§3.4)。 */
  canWrite: boolean;
  pendingMerges: readonly PendingMerge[];
};

type SourceFacts = {
  title: string;
  sourceType: string;
  exists: boolean;
  processing: boolean;
  /** 读侧给了这一篇此刻的样子(类型、在不在、是否在解析)。 */
  known: boolean;
};

function sourceFacts(row: KgFindingRow, sourceId: string): SourceFacts {
  const live = row.live_sources.find((item) => item.source_id === sourceId);
  const side = evidenceList(row.evidence, "sources").find((item) => textField(item, "source_id") === sourceId);
  const partner = evidenceList(row.evidence, "partners").find((item) => textField(item, "source_id") === sourceId);
  const subjectTitle = row.subject.kind === "source" && row.subject.key === sourceId ? row.subject.label.trim() : "";
  return {
    title: live?.title.trim()
      || textField(side, "label")
      || textField(partner, "label")
      || subjectTitle
      || "（没有标题）",
    sourceType: live?.source_type ?? "",
    // 读侧每一页都会给涉及的来源此刻的样子;万一缺了这一篇,「打开来源核对」照出(只读、
    // 后端裁决),删除与重新解析不出——不知道类型就既拦不住收录来源的重新解析,也会让
    // 删除确认框用错文案。
    exists: live ? live.exists : true,
    processing: Boolean(live?.processing),
    known: Boolean(live),
  };
}

/** F2 证据里标了「建议保留」的那一侧:删除按钮绝不放在它旁边。 */
function keptSide(row: KgFindingRow, sourceId: string): boolean {
  return evidenceList(row.evidence, "sources").some((item) => (
    textField(item, "source_id") === sourceId
    && Boolean(item && typeof item === "object" && (item as Record<string, unknown>).keep === true)
  ));
}

/**
 * F4 这一组在待确认合并里的那一对:两侧都必须是这一组的成员(`canonical_ids` 是全组的
 * 合并后的知识对象 id,不是只截了 5 个的样例)。一侧在组内、另一侧是别的概念的候选不是
 * 这一组。
 */
export function pendingMergeForFinding(
  row: KgFindingRow,
  pendingMerges: readonly PendingMerge[],
): PendingMerge | null {
  const ids = new Set(
    evidenceList(row.evidence, "canonical_ids").filter((id): id is string => typeof id === "string" && id !== ""),
  );
  if (ids.size < 2) return null;
  return pendingMerges.find((merge) => ids.has(merge.canonical_a) && ids.has(merge.canonical_b)) ?? null;
}

const DELETE_NOTE = "不可撤销，先打开来源核对";
const KEEP_NOTE = "两份内容几乎相同，保留对象更多的一份";
const PROCESSING_NOTE = "这篇来源正在解析，完成后再处理";
const NOT_IN_QUEUE_NOTE = "待确认合并里还没有这一组";

/**
 * 一条问题该出哪些按钮、旁边写什么(§四各条的「建议动作」)。
 *
 * 判据与既有入口同一套:只读成员与镜像库没有任何按钮(「标为正常」由调用方按同一位判断);
 * 收录来源不出「重新解析」;来源已不在的不出;只有 `open` 的行出按钮(已处理、已标为正常、
 * 已判定不是同一个、补上关联已经跑过的都不出)。后端仍是权限的唯一判据——这里只决定
 * 「出不出入口」。
 */
export function findingActionPlan(row: KgFindingRow, context: FindingActionContext): FindingActionPlan {
  const plan: FindingActionPlan = { buttons: [], notes: [], blocked: [], processingSourceIds: [] };
  if (!context.canWrite || row.status !== "open") return plan;
  const opened = new Set<string>();
  const addOpen = (sourceId: string, facts: SourceFacts) => {
    if (opened.has(sourceId) || !facts.exists) return;
    opened.add(sourceId);
    plan.buttons.push({ kind: "open_source", sourceId, title: facts.title });
  };
  // F2:两份各有一个「打开来源」,删除只放在建议删除的那份旁边。
  if (row.rule_id === "F2") {
    for (const sourceId of row.subject_refs) addOpen(sourceId, sourceFacts(row, sourceId));
  }
  for (const action of row.actions) {
    if (action.kind === "delete_source" || action.kind === "reparse_source") {
      const sourceId = action.source_id ?? "";
      if (!sourceId) continue;
      const facts = sourceFacts(row, sourceId);
      if (!facts.exists) continue;
      addOpen(sourceId, facts);
      if (!facts.known) continue;
      if (action.kind === "reparse_source" && facts.sourceType === "promotion") continue;
      // 疑似重复导入:建议保留的那份旁边不出删除(前端自己的防线)。
      if (action.kind === "delete_source" && row.rule_id === "F2" && keptSide(row, sourceId)) continue;
      if (facts.processing && !plan.processingSourceIds.includes(sourceId)) {
        plan.processingSourceIds.push(sourceId);
      }
      plan.buttons.push({ kind: action.kind, sourceId, title: facts.title, sourceType: facts.sourceType });
      if (action.kind === "delete_source") {
        if (!plan.notes.includes(DELETE_NOTE)) plan.notes.push(DELETE_NOTE);
        if (row.rule_id === "F2" && !plan.notes.includes(KEEP_NOTE)) plan.notes.push(KEEP_NOTE);
      }
    } else if (action.kind === "review_merge") {
      const merge = pendingMergeForFinding(row, context.pendingMerges);
      if (merge) plan.buttons.push({ kind: "review_merge", mergeId: merge.id });
      else if (!plan.blocked.includes(NOT_IN_QUEUE_NOTE)) plan.blocked.push(NOT_IN_QUEUE_NOTE);
    } else if (action.kind === "relink") {
      plan.buttons.push({ kind: "relink" });
    }
  }
  if (plan.processingSourceIds.length > 0) plan.notes.push(PROCESSING_NOTE);
  return plan;
}

/** 补上关联跑完之后写在按钮旁的那一句(数字是它自己返回的,口径与问题清单不同、不互相核对)。 */
export function relinkResultText(result: { edgesAdded: number; isolatedAfter: number }): string {
  return `新增 ${formatCount(result.edgesAdded)} 条关联，还剩 ${formatCount(result.isolatedAfter)} 个没有关联的知识条目`;
}

// ------------------------------------------------------------ 主体内容 / 孤岛(§五 S3)

/**
 * 「主题结构信号」改报 S3 的口径:主体内容(主连通块)含多少比例的知识对象、内部有几个
 * 主题;另有几个孤岛、来自几个来源。不再报「当前分为 N 个主题板块」——那个数里九成是
 * 一整个孤立连通块,数的其实是碎片个数。
 *
 * 只有「主体内容里有几个主题」依赖板块划分,来自依赖板块的那份产物;它缺席时只少那半句。
 */
export function structureSignal(
  summary: KgFindingsSummary,
  communityEdgesPayload: Record<string, unknown> | null | undefined,
): { headline: string; text: string } | null {
  const structure = summary.present ? summary.structure : null;
  if (!structure) return null;
  const share = finiteNumber(structure.giant_object_share);
  const islands = finiteNumber(structure.island_components);
  const islandSources = finiteNumber(structure.island_sources);
  if (share === null && islands === null) return null;
  const boardsInGiant = finiteNumber(communityEdgesPayload?.boards_in_giant);
  const main = share === null
    ? ""
    : boardsInGiant === null
      ? `主体内容含 ${formatPercent(share)} 的知识对象`
      : `主体内容含 ${formatPercent(share)} 的知识对象，内部有 ${formatCount(boardsInGiant)} 个主题`;
  const island = islands === null
    ? ""
    : islands === 0
      ? "没有孤岛"
      : islandSources === null
        ? `另有 ${formatCount(islands)} 个孤岛`
        : `另有 ${formatCount(islands)} 个孤岛，来自 ${formatCount(islandSources)} 个来源`;
  return {
    headline: share === null ? "主体内容占比未知" : `主体内容占 ${formatPercent(share)}`,
    text: `${[main, island].filter(Boolean).join("；")}。`,
  };
}

// ------------------------------------------------------------ 需要开发处理(§五 B 档)

/**
 * 「需要开发处理」区的条目。只写现象、规模和「需要开发修复」,**不放按钮**:看起来能修、
 * 按下去不会有任何变化的按钮一律不许出现(§3.4)。认不出的条目不上屏。
 */
export function devItemViews(summary: KgFindingsSummary): Array<{ key: string; text: string }> {
  if (!summary.present) return [];
  const views: Array<{ key: string; text: string }> = [];
  for (const item of summary.dev_items) {
    if (item.item_id !== "S1" || !(item.groups > 0)) continue;
    const examples = item.samples
      .map((sample) => sample.names
        .filter((name) => name.trim() !== "")
        .slice(0, 5)
        .join(" / "))
      .filter(Boolean)
      .slice(0, 3);
    const example = examples.length > 0 ? `（例：${examples.join("；")}）` : "";
    views.push({
      key: "S1",
      text: `${formatCount(item.groups)} 组只差空格/连字符的概念没有合并${example}，这是合并规则的缺陷，修好后会自动合并，不需要逐条处理。`,
    });
  }
  return views;
}

// ------------------------------------------------------------ 已通过区的规则行

export type PassedRuleLine = {
  ruleId: string;
  /**
   * clean = 检查过、没发现(✓);skipped = 没法检查(–,不算通过);partial = 有一部分没判断(–);
   * decided = F4 用户已判定不是同一个的组数(≠)——重建之后这些组不再成行,只剩这一个计数,
   * 不写出来用户的判断就看不见了。
   */
  state: "clean" | "skipped" | "partial" | "decided";
  text: string;
};

function checkedText(ruleId: string, status: KgFindingRuleStatus): string {
  const field = FINDING_CHECKED_FIELD[ruleId];
  const checked = field ? finiteNumber(status[field[0]]) : null;
  if (!field || checked === null) return "";
  return `（检查了 ${formatCount(checked)} 个${label(UNIT_LABEL, field[1], "项")}）`;
}

/**
 * 已通过区每条规则一行(§3.4)。三种状态分开写,绝不都画成绿勾:
 *   ✓ 检查过、没发现 —— 写明检查了多少;
 *   – 没法检查 —— 写原因,这一档不算通过;
 *   – 检查了但有一部分没判断(F1 概念太少的来源)—— 同样单列。
 * 有问题的规则不在这里出「✓」:它的问题在待处理区。
 */
export function passedRuleLines(summary: KgFindingsSummary): PassedRuleLine[] {
  if (!summary.present) return [];
  const lines: PassedRuleLine[] = [];
  for (const ruleId of FINDING_RULE_ORDER) {
    const rule = summary.rules[ruleId];
    if (!rule) continue;
    const name = FINDING_RULE_LABEL[ruleId];
    if (rule.status === "skipped") {
      lines.push({
        ruleId,
        state: "skipped",
        text: `${name}：没法检查，${label(FINDING_SKIP_REASON_LABEL, rule.reason ?? "", "这一次没有可检查的内容")}`,
      });
      continue;
    }
    if (rule.status !== "ran") continue;
    if (finiteNumber(rule.total) === 0) {
      lines.push({ ruleId, state: "clean", text: `没发现${FINDING_CLEAN_NOUN[ruleId]}${checkedText(ruleId, rule)}` });
    }
    const notJudged = ruleId === "F1" ? finiteNumber(rule.sources_too_few_concepts) : null;
    if (notJudged !== null && notJudged > 0) {
      lines.push({ ruleId, state: "partial", text: `${name}：有 ${formatCount(notJudged)} 个来源概念太少，没有判断` });
    }
    const decided = ruleId === "F4" ? finiteNumber(rule.groups_decided_apart) : null;
    if (decided !== null && decided > 0) {
      lines.push({
        ruleId,
        state: "decided",
        text: `${name}：已判定不是同一个的有 ${formatCount(decided)} 组，不再列为问题`,
      });
    }
  }
  return lines;
}

/** 已通过区里一行的说明(F4 已判定不是同一个 / 已标为正常)。 */
export function passedRowText(row: KgFindingRow): string {
  const what = `${findingRuleLabel(row.rule_id)} · ${findingSubjectText(row)}`;
  if (row.status === "decided_not_same") return `已判定不是同一个：${what}`;
  if (row.status === "dismissed") return `已标为正常：${what}`;
  return what;
}

/**
 * 诊断数据里「没建立关联的内容」那一块的现状句(§4.5)。「补上关联」确定且幂等:跑过之后
 * 没有新内容,再跑也补不上——不放按钮,只说现状。M 取这条问题自己的孤立对象数。
 */
export function relinkSpentText(row: KgFindingRow): string {
  const isolated = evidenceNumber(row.evidence, "isolated_objects");
  return isolated === null
    ? "补上关联已经跑过，剩下的没有可自动补上的关联；集中在某几个来源的，见疑似解析残缺"
    : `补上关联已经跑过，剩下 ${formatCount(isolated)} 个没有可自动补上的关联；集中在某几个来源的，见疑似解析残缺`;
}

/** 某条规则的问题太多、只保存了前若干条:写在待处理区,截断绝不静默。 */
export function truncatedRuleNotes(summary: KgFindingsSummary): string[] {
  if (!summary.present) return [];
  const notes: string[] = [];
  for (const ruleId of FINDING_RULE_ORDER) {
    const rule = summary.rules[ruleId];
    if (!rule || rule.truncated !== true) continue;
    const total = finiteNumber(rule.total);
    const persisted = finiteNumber(rule.persisted);
    notes.push(total !== null && persisted !== null && persisted < total
      ? `${FINDING_RULE_LABEL[ruleId]}共 ${formatCount(total)} 条，只保存了前 ${formatCount(persisted)} 条`
      : `${FINDING_RULE_LABEL[ruleId]}没能检查完整，列出的不是全部`);
  }
  return notes;
}
