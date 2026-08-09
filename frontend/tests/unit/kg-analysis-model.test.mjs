// 图谱分析报告的纯装配逻辑守卫。
//
// 这份测试钉的是这个视图**唯一的价值**——如实呈现。四组断言各对应一条要求:
//   · 单位:计数的单位从响应的 units 里读,改 units 上屏文案必须跟着变(硬写就漏红);
//   · 新鲜度:建于哪次变更 / 落后多少 / 从没算过,三种表达必须分得开,负值不 clamp;
//   · 缺失 ≠ 为空:没有分母时是 null(渲染成「—」),不是 0;
//   · 规模自适应:俯瞰图 top-N 截断、长尾汇总、以及「根本没取回」的那一截,三者分开
//     声明,绝不混成一个数。
import test from "node:test";
import assert from "node:assert/strict";

import {
  BOARD_MAP_MAX_NODES,
  basisLabel,
  boardList,
  boardMapModel,
  convergenceTable,
  countText,
  formatCount,
  formatPercent,
  freshnessNote,
  unitFor,
  unitLabel,
} from "../../app/kg-analysis-model.ts";

// 本机 base 库实测(设计 §3.5 的那张表):concept 的真实收敛率 31%,四类混算只有 10%。
const HISTOGRAM = {
  member_rows: 41713,
  clusters: 37340,
  excluded_member_rows: 128,
  empty_clusters: 7,
  empty_cluster_member_rows: 19,
  by_object_type: [
    { object_type: "concept", object_types: 1, member_rows: 9400, clusters: 6487 },
    { object_type: "claim", object_types: 1, member_rows: 29126, clusters: 27779 },
    { object_type: "formula", object_types: 1, member_rows: 2038, clusters: 1929 },
    { object_type: "procedure", object_types: 1, member_rows: 1149, clusters: 1145 },
    { object_type: "other", object_types: 3, member_rows: 0, clusters: 0 },
  ],
};

const HISTOGRAM_UNITS = {
  member_rows: "cluster_member_rows",
  clusters: "clusters",
  excluded_member_rows: "cluster_member_rows",
  object_types: "object_types",
};

function boardsPayload({ total, sizes, limit = 50, topK = 5 }) {
  return {
    freshness: { basis: "community_snapshot", built_at_seq: 100, seq_behind: 0, stale: false },
    units: { total: "communities", returned: "communities", limit: "communities", size: "canonical" },
    payload: {
      level: 0,
      total,
      returned: sizes.length,
      truncated: total > sizes.length,
      limit,
      top_members_limit: topK,
      communities: sizes.map((size, index) => ({
        id: `c-${String(index).padStart(3, "0")}`,
        size,
        top_members: [`代表-${index}`],
        top_members_truncated: size > 1,
      })),
    },
  };
}

// ------------------------------------------------------------------ 数字与单位

test("计数千分位不依赖运行环境的区域设置", () => {
  assert.equal(formatCount(8783591), "8,783,591");
  assert.equal(formatCount(0), "0");
  assert.equal(formatCount(-1234), "-1,234");
});

test("比例:null 渲染成「—」而不是 0%(「没有分母」与「算出来是 0」不是一回事)", () => {
  assert.equal(formatPercent(null), "—");
  assert.equal(formatPercent(undefined), "—");
  assert.equal(formatPercent(0), "0%");
  assert.equal(formatPercent(0.3098936), "31.0%");
  // 非零但极小的比例不能被四舍五入成 0%——那会被读成「一个都没覆盖」。
  assert.equal(formatPercent(0.0000004), "<0.1%");
});

test("单位从响应的 units 表里读,不在渲染处硬写", () => {
  assert.equal(unitFor(HISTOGRAM_UNITS, "member_rows"), "合并前的成员");
  assert.equal(unitFor(HISTOGRAM_UNITS, "clusters"), "合并后的知识对象");
  assert.equal(countText(9400, HISTOGRAM_UNITS, "member_rows"), "9,400 合并前的成员");
  // 同一个数字换一张单位表,上屏文案必须跟着变——硬写单位的实现过不了这一条。
  assert.equal(countText(9400, { member_rows: "sources" }, "member_rows"), "9,400 来源");
});

test("units 里没有登记的字段退到中性词,不把英文代号上屏", () => {
  assert.equal(unitFor(HISTOGRAM_UNITS, "no_such_field"), "项");
  assert.equal(unitFor(null, "member_rows"), "项");
  assert.equal(unitLabel("brand_new_unit_code"), "项");
});

test("四种互不可比的单位各有各的界面词", () => {
  assert.equal(unitLabel("objects"), "知识对象");
  assert.equal(unitLabel("canonical"), "合并后的知识对象");
  assert.equal(unitLabel("cluster_member_rows"), "合并前的成员");
  assert.equal(unitLabel("community_pairs"), "板块对");
  assert.equal(unitLabel("relation_rows"), "关联");
  assert.equal(unitLabel("sources"), "来源");
});

// -------------------------------------------------------------------- 新鲜度

test("新鲜度:建于哪次变更 + 落后多少,三种情形三种说法", () => {
  const fresh = freshnessNote({ basis: "usable_live", built_at_seq: 128, seq_behind: 0, stale: false });
  assert.equal(fresh.built, "建于变更 #128");
  assert.equal(fresh.behind, "与当前一致");
  assert.equal(fresh.basis, "整理当时的实时口径");

  const stale = freshnessNote({ basis: "community_snapshot", built_at_seq: 100, seq_behind: 28, stale: true });
  assert.equal(stale.built, "建于变更 #100");
  assert.equal(stale.behind, "落后 28 次变更");
  assert.equal(stale.basis, "上次主题板块划分");
});

test("新鲜度:从没算过时说「尚未生成」,不能表现成「刚建好」", () => {
  const note = freshnessNote({ basis: "community_snapshot", built_at_seq: null, seq_behind: null, stale: null });
  assert.equal(note.built, "尚未生成");
  assert.equal(note.behind, "");
  assert.equal(freshnessNote(null).built, "尚未生成");
});

test("新鲜度:落后量为负(数据比库还新)单独成一档,不 clamp 成 0", () => {
  const ahead = freshnessNote({ basis: "usable_live", built_at_seq: 130, seq_behind: -3, stale: true });
  assert.equal(ahead.behind, "比当前新 3 次变更");
});

test("新鲜度:合并单独动过时必须说出来,不能因为变更没动就报「与当前一致」", () => {
  // 这正是后端那条闸漏掉的形态:变更计数一动没动,合并结果换了一代,而收敛率与
  // 最大概念榜单都是从合并结果算的。少说这半句,整块数据就是在骗人。
  const merged = freshnessNote({
    basis: "usable_live",
    built_at_seq: 128,
    seq_behind: 0,
    stale: true,
    built_at_cluster_seq: 6,
    cluster_seq_behind: 3,
  });
  assert.equal(merged.built, "建于变更 #128、合并 #6");
  assert.equal(merged.behind, "落后 3 次合并");

  const both = freshnessNote({
    basis: "community_snapshot",
    built_at_seq: 100,
    seq_behind: 28,
    stale: true,
    built_at_cluster_seq: 6,
    cluster_seq_behind: 3,
  });
  assert.equal(both.behind, "落后 28 次变更 · 落后 3 次合并");

  const aheadOnMerge = freshnessNote({
    basis: "usable_live",
    built_at_seq: 128,
    seq_behind: 0,
    stale: true,
    built_at_cluster_seq: 9,
    cluster_seq_behind: -2,
  });
  assert.equal(aheadOnMerge.behind, "比当前新 2 次合并");
});

test("新鲜度:与合并无关的数据不编造合并世代(边的出处只读关系表)", () => {
  const provenance = freshnessNote({
    basis: "usable_live",
    built_at_seq: 128,
    seq_behind: 0,
    stale: false,
    built_at_cluster_seq: null,
    cluster_seq_behind: null,
  });
  assert.equal(provenance.built, "建于变更 #128");
  assert.equal(provenance.behind, "与当前一致");
});

test("新鲜度:板块数据说不出建在哪次合并上时,绝不显示成「与当前一致」", () => {
  // 「只补账本」那一轮的形状(codex 第 7 轮 P2):主题板块沿用库里现成的划分,而那批
  // 划分建在哪一次合并上没有地方记 —— 后端因此给 stale=null。它与上一条(本来就与
  // 合并无关)的两个 *_cluster_* 字段长得一模一样,只有 stale 分得开;混为一谈,
  // 一份拼了两个时候的数据就会理直气壮地说「与当前一致」。
  const unknown = freshnessNote({
    basis: "community_snapshot",
    built_at_seq: 128,
    seq_behind: 0,
    stale: null,
    built_at_cluster_seq: null,
    cluster_seq_behind: null,
  });
  assert.equal(unknown.built, "建于变更 #128、合并次数未知");
  assert.equal(unknown.behind, "无法判断落后多少次合并");

  // 变更那条线同时落后时两句话都要说 —— 未知不吞掉已知。
  const alsoBehind = freshnessNote({
    basis: "community_snapshot",
    built_at_seq: 100,
    seq_behind: 28,
    stale: null,
    built_at_cluster_seq: null,
    cluster_seq_behind: null,
  });
  assert.equal(alsoBehind.behind, "落后 28 次变更 · 无法判断落后多少次合并");
});

test("口径来源未声明时说「口径未标注」,不假装知道", () => {
  assert.equal(basisLabel("unified_rebuild_snapshot"), "上次整理时的规模");
  assert.equal(basisLabel(""), "口径未标注");
  assert.equal(basisLabel(null), "口径未标注");
});

// ------------------------------------------------------------------ A1 / A2

test("收敛率按类型分列:concept 的 31% 不能被 claim 稀释成 10%", () => {
  const table = convergenceTable(HISTOGRAM);
  const byKey = Object.fromEntries(table.rows.map((row) => [row.key, row]));

  assert.equal(formatPercent(byKey.concept.rate), "31.0%");
  assert.equal(formatPercent(byKey.claim.rate), "4.6%");
  assert.equal(formatPercent(byKey.formula.rate), "5.3%");
  assert.equal(formatPercent(byKey.procedure.rate), "0.3%");
  // 合计仍然要出,但它是**另一个数**,与分列的任何一行都不相等。
  assert.equal(formatPercent(table.total.rate), "10.5%");
  assert.notEqual(formatPercent(table.total.rate), formatPercent(byKey.concept.rate));
});

test("收敛率:五个分组恒在(零值的「其他」也保留——「没有自定义类型」本身是信息)", () => {
  const table = convergenceTable(HISTOGRAM);
  assert.deepEqual(
    table.rows.map((row) => row.key),
    ["concept", "claim", "formula", "procedure", "other"],
  );
  assert.deepEqual(
    table.rows.map((row) => row.label),
    ["概念 Concept", "论断 Claim", "公式 Formula", "过程 Procedure", "其他类型"],
  );
  assert.equal(byKeyOther(table).objectTypes, 3);
});

function byKeyOther(table) {
  return table.rows.find((row) => row.key === "other");
}

test("收敛率:没有分母时是 null(渲染成「—」),不是 0%", () => {
  const table = convergenceTable({ member_rows: 0, clusters: 0, by_object_type: [] });
  assert.equal(table.total.rate, null);
  assert.equal(table.total.share, null);
  assert.equal(formatPercent(table.total.rate), "—");
});

test("收敛率:载荷缺席(产物没算过)不抛错,读出来是一张全零表", () => {
  const table = convergenceTable(null);
  assert.equal(table.rows.length, 0);
  assert.equal(table.total.memberRows, 0);
  assert.equal(table.total.rate, null);
});

test("对象构成:各类型占比之和为 1,排除量单独报出不并进分母", () => {
  const table = convergenceTable(HISTOGRAM);
  const sum = table.rows.reduce((acc, row) => acc + (row.share ?? 0), 0);
  assert.ok(Math.abs(sum - 1) < 1e-9, `占比之和应为 1，实际 ${sum}`);
  assert.equal(table.excludedMemberRows, 128);
  assert.equal(table.emptyClusters, 7);
  assert.equal(table.emptyClusterMemberRows, 19);
});

// -------------------------------------------------------------------- C1 / E1

test("板块列表:返回了多少 / 一共多少 / 还有多少没返回,三个数分开", () => {
  const list = boardList(boardsPayload({ total: 1217, sizes: [10, 8, 6] }));
  assert.equal(list.total, 1217);
  assert.equal(list.returned, 3);
  assert.equal(list.unreturned, 1214);
  assert.equal(list.truncated, true);
  // 返回的这几个的成员合计**不是**全库成员数——未返回板块的规模未知。
  assert.equal(list.returnedMembers, 24);
});

test("俯瞰图:超出上限的板块并成一个长尾汇总节点,而不是画上万个点", () => {
  const sizes = Array.from({ length: 30 }, (_, index) => 100 - index);
  const list = boardList(boardsPayload({ total: 1217, sizes }));
  const map = boardMapModel(list, null);

  assert.equal(map.drawn, BOARD_MAP_MAX_NODES);
  assert.equal(map.nodes.filter((node) => node.kind === "board").length, BOARD_MAP_MAX_NODES);

  const tail = map.nodes.filter((node) => node.kind === "tail");
  assert.equal(tail.length, 1);
  assert.equal(tail[0].boards, 30 - BOARD_MAP_MAX_NODES);
  assert.equal(tail[0].label, "其余 6 个板块");
  assert.equal(tail[0].size, sizes.slice(BOARD_MAP_MAX_NODES).reduce((a, b) => a + b, 0));
});

test("俯瞰图:「没画出来」与「根本没取回」是两截,不能并进同一个汇总节点", () => {
  const sizes = Array.from({ length: 30 }, (_, index) => 100 - index);
  const map = boardMapModel(boardList(boardsPayload({ total: 1217, sizes })), null);
  // 长尾节点只汇总**取回了规模**的那 6 个;另外 1187 个连规模都不知道。
  assert.equal(map.tailBoards, 6);
  assert.equal(map.unreturned, 1217 - 30);
  assert.notEqual(map.tailBoards, map.unreturned);
  assert.equal(map.boardCoverage, BOARD_MAP_MAX_NODES / 1217);
});

test("俯瞰图:板块数小于上限时不出长尾节点,覆盖率是 100%", () => {
  const map = boardMapModel(boardList(boardsPayload({ total: 3, sizes: [5, 4, 3] })), null);
  assert.equal(map.drawn, 3);
  assert.equal(map.tailBoards, 0);
  assert.equal(map.unreturned, 0);
  assert.equal(map.nodes.every((node) => node.kind === "board"), true);
  assert.equal(formatPercent(map.boardCoverage), "100.0%");
});

test("俯瞰图:一个板块都没有时覆盖率是 null 而不是 0(免得被读成「画漏了」)", () => {
  const map = boardMapModel(boardList(boardsPayload({ total: 0, sizes: [] })), null);
  assert.equal(map.nodes.length, 0);
  assert.equal(map.boardCoverage, null);
});

test("俯瞰图:只画两端都在图上的连线,长尾汇总节点不连线", () => {
  const sizes = Array.from({ length: 30 }, (_, index) => 100 - index);
  const list = boardList(boardsPayload({ total: 30, sizes }));
  const edgeView = {
    present: true,
    freshness: { basis: "community_snapshot", built_at_seq: 100, seq_behind: 0, stale: false },
    limit: 200,
    returned: 3,
    returned_weight: 60,
    stored: 3,
    stored_total: 9,
    stored_truncated: true,
    edge_limit: 200000,
    cross_weight: 120,
    weight_coverage: 0.5,
    units: {},
    // 第三条指向 c-029(排在第 30 位,没被单独画出)——它必须落在图外。
    edges: [
      { src: "c-000", dst: "c-001", weight: 30 },
      { src: "c-001", dst: "c-002", weight: 20 },
      { src: "c-000", dst: "c-029", weight: 10 },
    ],
  };
  const map = boardMapModel(list, edgeView);

  assert.equal(map.drawnEdges, 2);
  assert.equal(map.returnedEdges, 3);
  assert.equal(map.storedEdges, 3);
  assert.equal(map.storedTotalEdges, 9);
  assert.equal(map.storedTruncated, true);
  assert.equal(map.edgeLimit, 200000);
  assert.equal(map.requestLimit, 200);
  assert.equal(map.weightCoverage, 0.5);
  // 三个截断层级各是各的数,不能互相顶替。
  assert.notEqual(map.drawnEdges, map.returnedEdges);
  assert.notEqual(map.returnedEdges, map.storedTotalEdges);
});

test("俯瞰图:一条板块间连线都没有时覆盖率是 null,不是 0", () => {
  const list = boardList(boardsPayload({ total: 2, sizes: [3, 2] }));
  const map = boardMapModel(list, {
    present: true,
    freshness: { basis: "community_snapshot", built_at_seq: 1, seq_behind: 0, stale: false },
    limit: 200, returned: 0, returned_weight: 0, stored: 0, stored_total: 0,
    stored_truncated: false, edge_limit: 200000, cross_weight: 0,
    weight_coverage: null, units: {}, edges: [],
  });
  assert.equal(map.weightCoverage, null);
  assert.equal(formatPercent(map.weightCoverage), "—");
});

test("俯瞰图布局是确定性的:同一份数据两次装配坐标逐字相同,且按规模降序", () => {
  const list = boardList(boardsPayload({ total: 5, sizes: [3, 9, 5, 9, 1] }));
  const first = boardMapModel(list, null);
  const second = boardMapModel(list, null);
  assert.deepEqual(first.nodes, second.nodes);
  assert.deepEqual(
    first.nodes.map((node) => node.size),
    [9, 9, 5, 3, 1],
  );
  // 规模并列时按板块 id 升序,免得两次渲染换位置。
  assert.deepEqual(
    first.nodes.slice(0, 2).map((node) => node.id),
    ["c-001", "c-003"],
  );
});
