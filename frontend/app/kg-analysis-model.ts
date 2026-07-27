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
  KgFreshness,
} from "./kg-analysis-api.ts";

// ---------------------------------------------------------------- 界面词映射

/** 五份预计算数据的界面名。顺序由后端固定,这里只管叫法。 */
export const ARTIFACT_LABEL: Record<string, string> = {
  cluster_size_histogram: "合并规模分布",
  largest_clusters: "最大的合并对象",
  relation_provenance: "关联出处构成",
  community_edges: "跨板块关联",
  source_profiles: "来源板块画像",
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
  /** 建于哪次变更(「建于变更 #128」/「尚未生成」)。 */
  built: string;
  /** 落后多少(「与当前一致」/「落后 12 次变更」/「比当前新 3 次变更」)。 */
  behind: string;
};

/**
 * 逐指标新鲜度(设计 §3.3,**有真实教训**:有人拿一份陈旧数据推出了关于图结构的
 * 重大结论,随后才得知该库尚未整理,整个推断作废)。
 *
 * 所以每一格数据都要能自证「建于何时、落后多少、什么口径」,而不是靠报告顶部一条
 * 「可能过期」的横幅。三个字段都是 null 时说的是「从没算过」——不是「刚建好」。
 */
export function freshnessNote(freshness: KgFreshness | null | undefined): FreshnessNote {
  const basis = basisLabel(freshness?.basis);
  if (!freshness || freshness.built_at_seq === null || freshness.built_at_seq === undefined) {
    return { basis, built: "尚未生成", behind: "" };
  }
  const built = `建于变更 #${formatCount(freshness.built_at_seq)}`;
  const behind = freshness.seq_behind ?? null;
  if (behind === null) return { basis, built, behind: "" };
  if (behind === 0) return { basis, built, behind: "与当前一致" };
  if (behind > 0) return { basis, built, behind: `落后 ${formatCount(behind)} 次变更` };
  return { basis, built, behind: `比当前新 ${formatCount(-behind)} 次变更` };
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
