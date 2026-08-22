// 图谱分析视图的渲染守卫。
//
// 钉的是四条「如实呈现」要求在**真实渲染结果**上的落点(纯逻辑那一半在
// kg-analysis-model.test.mjs):
//   · 逐指标新鲜度——每一块自带口径/建于哪次变更/落后多少,而不是页顶一条横幅;
//   · 单位从响应的 units 读——换一张单位表,上屏文案必须跟着变;
//   · 缺失 / 合法缺席 / 本该有却缺失 / 算过但为 0,四种情形四种呈现;
//   · 俯瞰图的 top-N 截断、长尾汇总、以及「根本没取回」的那一截,分开声明。
import { fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";

vi.mock("../../app/kg-analysis-api", () => ({
  fetchKgAnalysis: vi.fn(),
  fetchKgAnalysisSources: vi.fn(),
}));

import {
  fetchKgAnalysis,
  fetchKgAnalysisSources,
  type KgAnalysisReport,
  type KgArtifactView,
  type KgSourceProfilePage,
} from "../../app/kg-analysis-api";
import { KgAnalysisView } from "../../app/kg-analysis-view";

const HISTOGRAM_UNITS = {
  member_rows: "cluster_member_rows",
  clusters: "clusters",
  excluded_member_rows: "cluster_member_rows",
  empty_clusters: "clusters",
  empty_cluster_member_rows: "cluster_member_rows",
  object_types: "object_types",
};

function artifact(kind: string, overrides: Partial<KgArtifactView> = {}): KgArtifactView {
  return {
    kind,
    present: true,
    optional: kind === "source_profiles",
    absence: null,
    freshness: { basis: "usable_live", built_at_seq: 128, seq_behind: 0, stale: false, built_at_cluster_seq: 7, cluster_seq_behind: 0 },
    created_at: "2026-07-25T09:00:00Z",
    units: {},
    payload: {},
    ...overrides,
  };
}

function histogramArtifact(overrides: Partial<KgArtifactView> = {}): KgArtifactView {
  return artifact("cluster_size_histogram", {
    units: HISTOGRAM_UNITS,
    payload: {
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
        { object_type: "other", object_types: 2, member_rows: 0, clusters: 0 },
      ],
    },
    ...overrides,
  });
}

function boards(count: number, total: number) {
  return {
    freshness: { basis: "community_snapshot", built_at_seq: 100, seq_behind: 28, stale: true, built_at_cluster_seq: 5, cluster_seq_behind: 2 },
    units: {
      level: "level",
      total: "communities",
      returned: "communities",
      limit: "communities",
      top_members_limit: "canonical",
      size: "canonical",
    },
    payload: {
      level: 0,
      total,
      returned: count,
      truncated: total > count,
      limit: 50,
      top_members_limit: 5,
      communities: Array.from({ length: count }, (_, index) => ({
        id: `c-${String(index).padStart(3, "0")}`,
        size: 100 - index,
        top_members: [`板块代表 ${index}`],
        top_members_truncated: true,
      })),
    },
  };
}

function report(overrides: Partial<KgAnalysisReport> = {}): KgAnalysisReport {
  return {
    notebook_id: "nb-1",
    generated_at: "2026-07-25T10:00:00Z",
    level: 0,
    ledger_state: "complete",
    ledger_consistent: true,
    state: {
      present: true,
      kg_mutation_seq: 128,
      community_seq: 100,
      cluster_mutation_seq: 128,
      canonical_rel_seq: 128,
      dirty: false,
      last_rebuild: {
        basis: "unified_rebuild_snapshot",
        at: "2026-07-20T08:00:00Z",
        object_count: 8783591,
        relation_count: 8360000,
        cluster_count: 2000000,
        units: {
          object_count: "objects",
          relation_count: "relation_rows",
          cluster_count: "clusters",
        },
      },
      units: {},
    },
    artifacts: [
      histogramArtifact(),
      artifact("largest_clusters", {
        units: { limit: "clusters", members: "cluster_member_rows" },
        payload: {
          object_type: "concept",
          limit: 20,
          truncated: false,
          clusters: [
            { canonical_id: "cluster-1", canonical_name: "时序收敛", members: 18 },
            { canonical_id: "cluster-2", canonical_name: "保持时间", members: 9 },
          ],
        },
      }),
      artifact("relation_provenance", {
        units: {
          counted: "relation_rows",
          relink: "relation_rows",
          total_rows: "relation_rows",
          rejected: "relation_rows",
          endpoint_unusable: "relation_rows",
        },
        payload: {
          counted: 100,
          relink: 30,
          total_rows: 106,
          buckets: {
            "relink:shared-element": 20,
            "relink:name-match": 10,
            "relink:other": 0,
            "tagged:other": 15,
            untagged: 55,
          },
          excluded: { rejected: 2, endpoint_unusable: 4 },
        },
      }),
      artifact("community_edges", {
        freshness: { basis: "community_snapshot", built_at_seq: 100, seq_behind: 28, stale: true, built_at_cluster_seq: 5, cluster_seq_behind: 2 },
        payload: {
          level: 0,
          edges: 3,
          edges_total: 9,
          truncated: true,
          edge_limit: 200000,
          cross_weight: 120,
          intra_weight: 900,
          communities: 1217,
        },
      }),
      artifact("source_profiles", {
        // 与 community_edges 同批算出,所以同样建于 #100、同样落后 28 次变更。
        freshness: { basis: "community_snapshot", built_at_seq: 100, seq_behind: 28, stale: true, built_at_cluster_seq: 5, cluster_seq_behind: 2 },
        units: {
          head_communities: "communities",
          head_members: "canonical",
          total_members: "canonical",
          mainstream_coverage: "ratio",
        },
        payload: {
          level: 0,
          sources: 48836,
          mainstream_coverage: 0.8,
          head_communities: 12,
          head_members: 900,
          total_members: 1500,
        },
      }),
    ],
    boards: boards(30, 1217),
    board_edges: {
      present: true,
      freshness: { basis: "community_snapshot", built_at_seq: 100, seq_behind: 28, stale: true, built_at_cluster_seq: 5, cluster_seq_behind: 2 },
      limit: 200,
      returned: 3,
      returned_weight: 60,
      stored: 3,
      stored_total: 9,
      stored_truncated: true,
      edge_limit: 200000,
      cross_weight: 120,
      weight_coverage: 0.5,
      units: {
        limit: "community_pairs",
        returned: "community_pairs",
        stored: "community_pairs",
        stored_total: "community_pairs",
        edge_limit: "community_pairs",
        returned_weight: "relation_rows",
        cross_weight: "relation_rows",
        weight_coverage: "ratio",
        weight: "relation_rows",
      },
      edges: [
        { src: "c-000", dst: "c-001", weight: 30 },
        { src: "c-001", dst: "c-002", weight: 20 },
        { src: "c-000", dst: "c-029", weight: 10 },
      ],
    },
    ...overrides,
  };
}

function sourcePage(overrides: Partial<KgSourceProfilePage> = {}): KgSourceProfilePage {
  return {
    notebook_id: "nb-1",
    generated_at: "2026-07-25T10:00:00Z",
    present: true,
    absence: null,
    freshness: { basis: "community_snapshot", built_at_seq: 100, seq_behind: 28, stale: true, built_at_cluster_seq: 5, cluster_seq_behind: 2 },
    kg_mutation_seq: 128,
    order: "sparse",
    limit: 20,
    offset: 0,
    total: 48836,
    returned: 2,
    has_more: true,
    summary: { level: 0, sources: 48836, mainstream_coverage: 0.8, head_communities: 12, head_members: 900, total_members: 1500 },
    units: {
      total: "sources",
      returned: "sources",
      limit: "sources",
      offset: "sources",
      n_objects: "objects",
      n_graph_objects: "objects",
      top_share: "ratio",
      community_spread: "communities",
      mainstream_share: "ratio",
    },
    rows: [
      {
        source_id: "src-1",
        title: "光遗传学质粒手册",
        source_missing: false,
        n_objects: 120,
        n_graph_objects: 90,
        top_community_id: "c-900",
        top_share: 0.9,
        community_spread: 2,
        mainstream_share: 0.01,
      },
      {
        source_id: "src-2",
        title: "",
        source_missing: true,
        n_objects: 5,
        n_graph_objects: 0,
        top_community_id: "",
        top_share: 0,
        community_spread: 0,
        mainstream_share: 0,
      },
    ],
    ...overrides,
  };
}

beforeEach(() => {
  vi.mocked(fetchKgAnalysis).mockResolvedValue(report());
  vi.mocked(fetchKgAnalysisSources).mockResolvedValue(sourcePage());
});

function renderView() {
  return render(<KgAnalysisView notebookId="nb-1" onClose={() => undefined} />);
}

test("上层确认框出现时从交互树退出，回到顶层后恢复模态语义", () => {
  const { container, rerender } = render(
    <KgAnalysisView notebookId="nb-1" onClose={() => undefined} interactive={false} zIndex={61} />,
  );
  const covered = container.querySelector<HTMLElement>('[role="dialog"]');
  expect(covered).not.toBeNull();
  expect(covered).toHaveAttribute("aria-hidden", "true");
  expect(covered).toHaveAttribute("inert");
  expect(covered).toHaveAttribute("aria-modal", "false");
  expect(covered).toHaveStyle({ zIndex: "61" });

  rerender(<KgAnalysisView notebookId="nb-1" onClose={() => undefined} interactive />);
  const active = screen.getByRole("dialog", { name: "图谱分析" });
  expect(active).not.toHaveAttribute("aria-hidden", "true");
  expect(active).not.toHaveAttribute("inert");
  expect(active).toHaveAttribute("aria-modal", "true");
});

function blockByTitle(title: string): HTMLElement {
  const heading = screen.getByRole("heading", { name: title, level: 3 });
  const block = heading.closest(".kg-analysis-block");
  if (!block) throw new Error(`没有找到「${title}」所在的块`);
  return block as HTMLElement;
}

// --------------------------------------------------------------- 逐指标新鲜度

test("每一块数据各自标注口径与落后量，而不是页顶挂一条横幅", async () => {
  const { container } = renderView();
  await screen.findByRole("heading", { name: "对象构成", level: 3 });

  // 同屏并列的两块口径不同、新鲜度也不同——这正是必须逐块标注的理由。
  const composition = blockByTitle("对象构成");
  expect(within(composition).getByText("整理当时的实时口径")).toBeInTheDocument();
  // 两条世代线一起上屏:合并那条会单独动,只说变更就会谎报「与当前一致」。
  expect(within(composition).getByText("建于变更 #128、合并 #7")).toBeInTheDocument();
  expect(within(composition).getByText("与当前一致")).toBeInTheDocument();

  const boardsBlock = blockByTitle("主题板块");
  expect(within(boardsBlock).getByText("上次主题板块划分")).toBeInTheDocument();
  expect(within(boardsBlock).getByText("建于变更 #100、合并 #5")).toBeInTheDocument();
  expect(within(boardsBlock).getByText("落后 28 次变更 · 落后 2 次合并")).toBeInTheDocument();

  // 每一块(含数据清单里的每一行)都挂着自己的那一条,不是全局一条。
  expect(container.querySelectorAll(".kg-analysis-freshness").length).toBeGreaterThanOrEqual(9);
});

test("陈旧的那几份挂黄色异常小字，新鲜的不挂", async () => {
  const { container } = renderView();
  await screen.findByRole("heading", { name: "本报告用到的数据", level: 3 });

  const ledger = blockByTitle("本报告用到的数据");
  const stale = within(ledger).getAllByText("落后于当前内容");
  // community_edges 与 source_profiles 建于 #100,落后 28 次;其余三份是当前的。
  expect(stale.length).toBe(2);
  expect(container.querySelectorAll(".anomaly-badge--retrieval").length).toBeGreaterThanOrEqual(2);
});

test("五份数据恒定列出，缺席的那份也在清单里而不是少一行", async () => {
  const { container } = renderView();
  await screen.findByRole("heading", { name: "本报告用到的数据", level: 3 });
  expect(container.querySelectorAll(".kg-analysis-ledger-row").length).toBe(5);
});

test("先把技术状态翻译成结论，并解释状态颜色和更新方式", async () => {
  renderView();
  await screen.findByRole("heading", { name: "先看结论", level: 3 });

  const readout = blockByTitle("先看结论");
  expect(within(readout).getByText("先更新，再判断")).toBeInTheDocument();
  expect(within(readout).getByText(/概念条目从 9,400 条收敛为 6,487 个/)).toBeInTheDocument();
  expect(within(readout).getByText(/当前分为 1,217 个主题板块/)).toBeInTheDocument();
  expect(within(readout).getByText(/当前最先值得复核的是“光遗传学质粒手册”/)).toBeInTheDocument();
  expect(within(readout).getByText(/红色：数字不可信/)).toBeInTheDocument();
  expect(within(readout).getByText(/黄色：旧版本/)).toBeInTheDocument();
  expect(within(readout).getByText(/不会重新分析来源/)).toBeInTheDocument();
});

test("合并代次无法验证不谎报成旧版本，也不诱导用户反复更新", async () => {
  const current = report();
  vi.mocked(fetchKgAnalysis).mockResolvedValueOnce(report({
    state: {
      ...current.state,
      kg_mutation_seq: 128,
      cluster_mutation_seq: 7,
      community_seq: 128,
      dirty: false,
    },
    artifacts: current.artifacts.map((item) => (
      item.kind === "community_edges"
        ? artifact("community_edges", {
            freshness: {
              basis: "community_snapshot",
              built_at_seq: 128,
              seq_behind: 0,
              stale: null,
              built_at_cluster_seq: null,
              cluster_seq_behind: null,
            },
            payload: item.payload,
          })
        : artifact(item.kind, {
            freshness: {
              ...item.freshness,
              built_at_seq: 128,
              seq_behind: 0,
              stale: false,
              built_at_cluster_seq: item.freshness.built_at_cluster_seq === null ? null : 7,
              cluster_seq_behind: item.freshness.built_at_cluster_seq === null ? null : 0,
            },
            units: item.units,
            payload: item.payload,
          })
    )),
  }));

  renderView();
  const readout = await screen.findByRole("heading", { name: "先看结论", level: 3 });
  const block = readout.closest(".kg-analysis-block") as HTMLElement;
  expect(within(block).getByText("可用，但有一项无法验证")).toBeInTheDocument();
  expect(within(block).getByText(/这不代表数据已经陈旧/)).toBeInTheDocument();
  expect(within(block).queryByText("先更新，再判断")).not.toBeInTheDocument();
});

test("账本不只报生成状态，还解释用途并展示最大合并组与关联形成方式", async () => {
  renderView();
  await screen.findByRole("heading", { name: "本报告用到的数据", level: 3 });

  const ledger = blockByTitle("本报告用到的数据");
  expect(within(ledger).getByText(/用于排查过度合并/)).toBeInTheDocument();
  expect(within(ledger).getByText(/用于判断关联覆盖方式/)).toBeInTheDocument();

  const clusters = blockByTitle("需要复核的大型合并组");
  expect(within(clusters).getByText("时序收敛")).toBeInTheDocument();
  expect(within(clusters).getByText("18 合并前的成员")).toBeInTheDocument();

  const provenance = blockByTitle("关联是怎样形成的");
  expect(within(provenance).getByRole("row", { name: /共享出处自动补连 20 20.0%/ })).toBeInTheDocument();
  expect(within(provenance).getByText(/自动补连占 30.0%/)).toBeInTheDocument();
});

test("可编辑成员能从分析页生成或更新；后台完成后自动重取报告", async () => {
  const onAnalyze = vi.fn();
  const view = render(
    <KgAnalysisView
      notebookId="nb-1"
      canAnalyze
      analysisRunning={false}
      onAnalyze={onAnalyze}
      onClose={() => undefined}
    />,
  );
  await screen.findByRole("heading", { name: "先看结论", level: 3 });

  fireEvent.click(screen.getByRole("button", { name: "更新分析" }));
  expect(onAnalyze).toHaveBeenCalledTimes(1);

  const reportCalls = vi.mocked(fetchKgAnalysis).mock.calls.length;
  view.rerender(
    <KgAnalysisView
      notebookId="nb-1"
      canAnalyze
      analysisRunning
      onAnalyze={onAnalyze}
      onClose={() => undefined}
    />,
  );
  expect(screen.getByRole("button", { name: "正在生成…" })).toBeDisabled();

  view.rerender(
    <KgAnalysisView
      notebookId="nb-1"
      canAnalyze
      analysisRunning={false}
      onAnalyze={onAnalyze}
      onClose={() => undefined}
    />,
  );
  expect(vi.mocked(fetchKgAnalysis).mock.calls.length).toBeGreaterThan(reportCalls);
});

// ------------------------------------------------------------------ 单位渲染

test("计数的单位从响应的 units 读——换一张单位表，上屏文案跟着变", async () => {
  const { unmount } = renderView();
  await screen.findByRole("heading", { name: "对象构成", level: 3 });
  expect(within(blockByTitle("对象构成")).getByText("9,400 合并前的成员")).toBeInTheDocument();
  // 表头也来自 units,不是写死的列名。
  expect(within(blockByTitle("合并收敛率")).getByRole("columnheader", { name: "合并后的知识对象" })).toBeInTheDocument();
  unmount();

  vi.mocked(fetchKgAnalysis).mockResolvedValue(
    report({
      artifacts: report().artifacts.map((item) => (
        item.kind === "cluster_size_histogram"
          ? histogramArtifact({ units: { ...HISTOGRAM_UNITS, member_rows: "objects", clusters: "canonical" } })
          : item
      )),
    }),
  );
  renderView();
  await screen.findByRole("heading", { name: "对象构成", level: 3 });
  expect(within(blockByTitle("对象构成")).getByText("9,400 知识对象")).toBeInTheDocument();
  expect(within(blockByTitle("合并收敛率")).getByRole("columnheader", { name: "合并后的知识对象" })).toBeInTheDocument();
});

test("上次整理的三个规模数各带各的单位，不并成一个可相除的数", async () => {
  renderView();
  await screen.findByRole("heading", { name: "报告口径与新鲜度", level: 3 });
  const state = blockByTitle("报告口径与新鲜度");
  // 精确到「规模那一行」——口径徽标本身也叫「上次整理时的规模」,不能与它撞。
  const note = within(state).getByText(/^上次整理时的规模：/);
  expect(note.textContent).toContain("8,783,591 知识对象");
  expect(note.textContent).toContain("8,360,000 关联");
  expect(note.textContent).toContain("2,000,000 合并后的知识对象");
});

// ------------------------------------------------------- 收敛率按类型分列

test("收敛率按类型分列并另给合计，concept 的 31% 不被稀释成 10%", async () => {
  renderView();
  await screen.findByRole("heading", { name: "合并收敛率", level: 3 });
  const table = within(blockByTitle("合并收敛率")).getByRole("table");

  const conceptRow = within(table).getByRole("row", { name: /概念 Concept/ });
  expect(within(conceptRow).getByText("31.0%")).toBeInTheDocument();
  const claimRow = within(table).getByRole("row", { name: /论断 Claim/ });
  expect(within(claimRow).getByText("4.6%")).toBeInTheDocument();
  const totalRow = within(table).getByRole("row", { name: /合计/ });
  expect(within(totalRow).getByText("10.5%")).toBeInTheDocument();
});

// ---------------------------------------------- 缺失 / 合法缺席 / 为空 三分

test("从没算过：说「还没算过」并明确它不是 0", async () => {
  vi.mocked(fetchKgAnalysis).mockResolvedValue(
    report({
      ledger_state: "empty",
      artifacts: report().artifacts.map((item) => (
        item.kind === "cluster_size_histogram"
          ? histogramArtifact({
              present: false,
              absence: "never_computed",
              payload: null,
              freshness: { basis: "usable_live", built_at_seq: null, seq_behind: null, stale: null, built_at_cluster_seq: null, cluster_seq_behind: null },
            })
          : item
      )),
    }),
  );
  renderView();
  await screen.findByRole("heading", { name: "对象构成", level: 3 });

  const block = blockByTitle("对象构成");
  expect(within(block).getByText(/这个知识库还没算过这份数据/)).toBeInTheDocument();
  expect(within(block).getByText(/与「算出来是 0」不是一回事/)).toBeInTheDocument();
  // 异常小字与新鲜度行都说「尚未生成」:一个是分档徽标,一个是版本行,两处都要有。
  expect(within(block).getAllByText("尚未生成").length).toBe(2);
  expect(within(block).queryByRole("table")).not.toBeInTheDocument();
});

test("本该有却缺失：升级成红色异常小字，措辞与「没算过」不同", async () => {
  vi.mocked(fetchKgAnalysis).mockResolvedValue(
    report({
      ledger_state: "partial",
      artifacts: report().artifacts.map((item) => (
        item.kind === "cluster_size_histogram"
          ? histogramArtifact({
              present: false,
              absence: "unexpected",
              payload: null,
              freshness: { basis: "usable_live", built_at_seq: null, seq_behind: null, stale: null, built_at_cluster_seq: null, cluster_seq_behind: null },
            })
          : item
      )),
    }),
  );
  const view = renderView();
  await screen.findByRole("heading", { name: "对象构成", level: 3 });

  const block = blockByTitle("对象构成");
  expect(within(block).getByText(/唯独这一份没有写下来/)).toBeInTheDocument();
  expect(within(block).getAllByText("本该有却缺失").length).toBeGreaterThanOrEqual(1);
  expect(view.container.querySelectorAll(".anomaly-badge--integrity").length).toBeGreaterThanOrEqual(1);
});

test("合法缺席（零板块的库不出来源画像）：说清是刻意不生成，不是异常", async () => {
  vi.mocked(fetchKgAnalysisSources).mockResolvedValue(
    sourcePage({
      present: false,
      absence: "expected",
      summary: null,
      total: 0,
      returned: 0,
      has_more: false,
      rows: [],
      freshness: { basis: "community_snapshot", built_at_seq: null, seq_behind: null, stale: null, built_at_cluster_seq: null, cluster_seq_behind: null },
    }),
  );
  renderView();
  await screen.findByRole("heading", { name: "关联稀疏的来源", level: 3 });

  const block = blockByTitle("关联稀疏的来源");
  expect(within(block).getByText(/一个主题板块都没有/)).toBeInTheDocument();
  expect(within(block).getByText("本次无需生成")).toBeInTheDocument();
  expect(within(block).queryByRole("table")).not.toBeInTheDocument();
});

test("算过但内容为 0：与「没算过」用两句不同的话", async () => {
  vi.mocked(fetchKgAnalysis).mockResolvedValue(
    report({
      artifacts: report().artifacts.map((item) => (
        item.kind === "cluster_size_histogram"
          ? histogramArtifact({ payload: { member_rows: 0, clusters: 0, by_object_type: [] } })
          : item
      )),
    }),
  );
  renderView();
  await screen.findByRole("heading", { name: "对象构成", level: 3 });

  const block = blockByTitle("对象构成");
  expect(within(block).getByText(/这份数据已经生成，但当前没有任何可统计的条目/)).toBeInTheDocument();
  expect(within(block).queryByText(/还没算过这份数据/)).not.toBeInTheDocument();
});

test("数据不是同一轮算出来的：整份报告挂红色异常小字", async () => {
  vi.mocked(fetchKgAnalysis).mockResolvedValue(report({ ledger_consistent: false }));
  renderView();
  await screen.findByRole("heading", { name: "报告口径与新鲜度", level: 3 });
  expect(within(blockByTitle("报告口径与新鲜度")).getByText("数字口径不一致")).toBeInTheDocument();
});

// -------------------------------------------------------- 俯瞰图的规模自适应

test("俯瞰图只画 top-N，并把「并进汇总节点」与「根本没取回」分开声明", async () => {
  const { container } = renderView();
  await screen.findByRole("heading", { name: "板块俯瞰图", level: 3 });

  const block = blockByTitle("板块俯瞰图");
  // 30 个已取回的板块里单独画 24 个,其余 6 个并成一个汇总节点(共 25 个圆)。
  expect(container.querySelectorAll(".kg-analysis-map-node").length).toBe(25);
  expect(container.querySelectorAll(".kg-analysis-map-tail").length).toBe(1);

  expect(within(block).getByText(/单独画出 24 个/)).toBeInTheDocument();
  expect(within(block).getByText(/另有 6 个已取到规模的板块/)).toBeInTheDocument();
  // 图上那个汇总节点自己也得说实话:它只代表**已取回**的 6 个,不能把没取回的算进来。
  expect(container.querySelector(".kg-analysis-map-tail title")?.textContent)
    .toContain("其余 6 个板块");
  // 未取回的 1187 个规模未知,**不在**汇总节点里——这两个数不能互相顶替。
  expect(within(block).getByText(/还有 1,187 个板块本次没有取回/)).toBeInTheDocument();
});

test("俯瞰图声明三级连线口径：本图画出 / 本次取回 / 库内存有（含落库截断）", async () => {
  renderView();
  await screen.findByRole("heading", { name: "板块俯瞰图", level: 3 });
  const block = blockByTitle("板块俯瞰图");

  // 指向没画出来的板块的那一条不上图,但取回条数照实说。
  expect(within(block).getByText(/本图画出 2 条/)).toBeInTheDocument();
  expect(within(block).getByText(/本次取回 3 板块对/)).toBeInTheDocument();
  expect(within(block).getByText(/是从 9 个板块对里按关联强度截到上限 200,000 的结果/)).toBeInTheDocument();
  expect(within(block).getByText(/占全部板块间关联强度的 50.0%/)).toBeInTheDocument();
});

// --------------------------------------------------------------- 来源分页

test("来源表走后端分页：翻页按 offset 重新取，不是一次拉回 4.8 万行", async () => {
  renderView();
  await screen.findByRole("heading", { name: "关联稀疏的来源", level: 3 });

  expect(vi.mocked(fetchKgAnalysisSources)).toHaveBeenCalledWith("nb-1", {
    limit: 20,
    offset: 0,
    order: "sparse",
  });
  const block = blockByTitle("关联稀疏的来源");
  expect(within(block).getByText(/共 48,836 来源/)).toBeInTheDocument();

  fireEvent.click(within(block).getByRole("button", { name: /下一页/ }));
  expect(vi.mocked(fetchKgAnalysisSources)).toHaveBeenLastCalledWith("nb-1", {
    limit: 20,
    offset: 20,
    order: "sparse",
  });
});

test("切换排序回到第一页，并按新顺序取数", async () => {
  renderView();
  await screen.findByRole("heading", { name: "关联稀疏的来源", level: 3 });
  const block = blockByTitle("关联稀疏的来源");

  fireEvent.click(within(block).getByRole("button", { name: "关联最紧密的在前" }));
  expect(vi.mocked(fetchKgAnalysisSources)).toHaveBeenLastCalledWith("nb-1", {
    limit: 20,
    offset: 0,
    order: "connected",
  });
});

test("切换来源表排序后，结论区仍保留固定的稀疏来源复核候选", async () => {
  renderView();
  await screen.findByText(/当前最先值得复核的是“光遗传学质粒手册”/);

  const sourcesBlock = blockByTitle("关联稀疏的来源");
  fireEvent.click(within(sourcesBlock).getByRole("button", { name: "关联最紧密的在前" }));

  const readout = blockByTitle("先看结论");
  expect(within(readout).getByText(/当前最先值得复核的是“光遗传学质粒手册”/)).toBeInTheDocument();
  expect(within(readout).queryByText("等待来源画像")).not.toBeInTheDocument();
});

// 切排序 / 翻页时请求在飞、屏上还是**上一次**的行。此时任何「跟着控件走」的标注都会把
// 旧数据说成新的：换排序时那两个按钮当场亮起新选的那个（aria-pressed），换页时页码同理。
// 与第 11 轮那条「换库不复位」是同一类缺陷（状态不跟着数据走），只是 scope 从 notebook
// 缩到了一次请求。修法：**渲染一律读 `page` 自带的 order/offset**，控件值只在一页都还
// 没有时兜底；再补一条「正在读取」的反馈，免得点了没反应。
test("切排序时旧的行不会被标上新选的顺序——标注跟着数据走，不跟着控件走", async () => {
  renderView();
  await screen.findByRole("heading", { name: "关联稀疏的来源", level: 3 });
  const block = blockByTitle("关联稀疏的来源");

  // 新的一页挂着不返回：屏上留着「关联最稀疏的在前」那一页的行。
  vi.mocked(fetchKgAnalysisSources).mockReturnValue(new Promise(() => {}));
  fireEvent.click(within(block).getByRole("button", { name: "关联最紧密的在前" }));

  expect(vi.mocked(fetchKgAnalysisSources)).toHaveBeenLastCalledWith("nb-1", {
    limit: 20,
    offset: 0,
    order: "connected",
  });
  expect(within(block).getByText("光遗传学质粒手册")).toBeInTheDocument();
  expect(
    within(block).getByRole("button", { name: "关联最稀疏的在前" }),
  ).toHaveAttribute("aria-pressed", "true");
  expect(
    within(block).getByRole("button", { name: "关联最紧密的在前" }),
  ).toHaveAttribute("aria-pressed", "false");
  // 点了必须有反应 —— 否则「标注跟着数据走」就成了「整块死住、毫无反馈」。
  expect(within(block).getByText(/正在读取/)).toBeInTheDocument();
});

test("翻页时旧的行不会被标上新的页码", async () => {
  renderView();
  await screen.findByRole("heading", { name: "关联稀疏的来源", level: 3 });
  const block = blockByTitle("关联稀疏的来源");
  expect(within(block).getByText(/第 1–2 个/)).toBeInTheDocument();

  vi.mocked(fetchKgAnalysisSources).mockReturnValue(new Promise(() => {}));
  fireEvent.click(within(block).getByRole("button", { name: /下一页/ }));

  expect(vi.mocked(fetchKgAnalysisSources)).toHaveBeenLastCalledWith("nb-1", {
    limit: 20,
    offset: 20,
    order: "sparse",
  });
  // 行还是第一页那两条，页码就必须还是第一页的。
  expect(within(block).getByText("光遗传学质粒手册")).toBeInTheDocument();
  expect(within(block).getByText(/第 1–2 个/)).toBeInTheDocument();
  expect(within(block).queryByText(/第 21–/)).not.toBeInTheDocument();
});

test("指向已删除来源的那一行标出来，而不是留一个空标题让人猜", async () => {
  renderView();
  await screen.findByRole("heading", { name: "关联稀疏的来源", level: 3 });
  const block = blockByTitle("关联稀疏的来源");
  expect(within(block).getByText("来源已不存在")).toBeInTheDocument();
  expect(within(block).getByText("（没有标题）")).toBeInTheDocument();
});

test("主体板块的口径读总览里那份数据的单位表（分页响应不带这几个字段的单位）", async () => {
  renderView();
  await screen.findByRole("heading", { name: "关联稀疏的来源", level: 3 });
  const block = blockByTitle("关联稀疏的来源");
  const note = within(block).getByText(/主体板块 =/);
  expect(note.textContent).toContain("累计覆盖 80.0% 成员");
  expect(note.textContent).toContain("12 主题板块");
  expect(note.textContent).toContain("900 合并后的知识对象");
  // 它读的是**总览**那一轮,与同块表格不是同一次请求 —— 所以必须自带一条新鲜度行,
  // 靠块头那一条戳不住它(设计 §3.3 要求逐指标标注)。
  expect(note.querySelector(".kg-analysis-freshness")).not.toBeNull();
  expect(note.textContent).toContain("建于变更 #100");
});

test("主体板块口径不与来源画像的缺席同屏打脸：这一页没有画像时那段口径不出现", async () => {
  // 真实可复现场景(不需要并发):打开视图时总览建于 #100、head_communities=12;
  // 后台跑了一次整理,库变成零板块、来源画像合法缺席(#140);用户点「下一页」——
  // 只有 /sources 重取,总览不重取。不门控的话,同一块里上下相邻两行会是
  //   「主体板块 = …共 12 主题板块，含 900 / 1,500 合并后的知识对象」(旧总览)
  //   「这个知识库一个主题板块都没有……因此没有生成。」(新的这一页)
  // 而块头的新鲜度行用的是新的那一轮,没有任何东西标出上面那段来自旧的一轮。
  vi.mocked(fetchKgAnalysisSources).mockResolvedValue(
    sourcePage({
      present: false,
      absence: "expected",
      summary: null,
      total: 0,
      returned: 0,
      has_more: false,
      rows: [],
      freshness: { basis: "community_snapshot", built_at_seq: 140, seq_behind: 0, stale: false, built_at_cluster_seq: 7, cluster_seq_behind: 0 },
    }),
  );
  renderView();
  await screen.findByRole("heading", { name: "关联稀疏的来源", level: 3 });

  const block = blockByTitle("关联稀疏的来源");
  expect(within(block).getByText(/一个主题板块都没有/)).toBeInTheDocument();
  expect(within(block).queryByText(/主体板块 =/)).not.toBeInTheDocument();
  expect(within(block).queryByText(/12 主题板块/)).not.toBeInTheDocument();
});

// --------------------------------------------------- 换库时笔记本作用域的复位
//
// 与 kg-analysis-view-toggle.test.mjs 那条是同一类缺陷的另一半:那边钉的是「卸载不等于
// 复位」(开关活在父组件上),这边钉的是「组件不卸载时,状态也得跟着 scope 走」。
// `notebookId` 变了而组件仍挂载,effect 要到**提交之后**才跑,所以复位必须在渲染期发生。

test("换库时上一个库的报告与分页位置当场清空，不会先渲染一帧旧数字", async () => {
  const { rerender } = render(<KgAnalysisView notebookId="nb-1" onClose={() => undefined} />);
  await screen.findByRole("heading", { name: "对象构成", level: 3 });
  // 先把分页推到第二页 —— 越界请求就是这么来的。
  fireEvent.click(within(blockByTitle("关联稀疏的来源")).getByRole("button", { name: /下一页/ }));
  expect(vi.mocked(fetchKgAnalysisSources)).toHaveBeenLastCalledWith("nb-1", {
    limit: 20,
    offset: 20,
    order: "sparse",
  });

  // 新库的两个请求都挂着不返回:这一帧屏上只能有加载态,不能有 nb-1 的任何数字。
  vi.mocked(fetchKgAnalysis).mockReturnValue(new Promise(() => {}));
  vi.mocked(fetchKgAnalysisSources).mockReturnValue(new Promise(() => {}));
  rerender(<KgAnalysisView notebookId="nb-2" onClose={() => undefined} />);

  expect(screen.queryByRole("heading", { name: "对象构成", level: 3 })).not.toBeInTheDocument();
  expect(screen.queryByRole("heading", { name: "主题板块", level: 3 })).not.toBeInTheDocument();
  expect(screen.queryByText(/共 48,836 来源/)).not.toBeInTheDocument();
  expect(screen.getByText("正在读取报告…")).toBeInTheDocument();
  // 分页位置归零 —— 而且是**当场**归零。断言写成「新库总共只被问过这一次、且是第 0 页」
  // 才拦得住移动变异:把复位挪进一个 useEffect 里,新库仍会先被问一次第 20 页(越界),
  // 只是随后又补问了第 0 页;只断「最后一次是第 0 页」的话那种写法照样全绿。
  const asked = vi.mocked(fetchKgAnalysisSources).mock.calls.filter(([id]) => id === "nb-2");
  expect(asked).toEqual([["nb-2", { limit: 20, offset: 0, order: "sparse" }]]);
});

test("同一个库重新渲染不清空报告——复位跟着 notebookId 走，不是每次渲染都来一遍", async () => {
  // 反向守卫:把条件写成恒真(或干脆无条件复位)会让每次父组件重渲都闪一次加载态、
  // 并把用户的分页位置抹掉。这条钉住「只有 scope 真的变了才复位」。
  const { rerender } = render(<KgAnalysisView notebookId="nb-1" onClose={() => undefined} />);
  await screen.findByRole("heading", { name: "对象构成", level: 3 });
  fireEvent.click(within(blockByTitle("关联稀疏的来源")).getByRole("button", { name: /下一页/ }));

  rerender(<KgAnalysisView notebookId="nb-1" onClose={() => undefined} />);

  expect(screen.getByRole("heading", { name: "对象构成", level: 3 })).toBeInTheDocument();
  expect(screen.queryByText("正在读取报告…")).not.toBeInTheDocument();
  expect(vi.mocked(fetchKgAnalysisSources)).toHaveBeenLastCalledWith("nb-1", {
    limit: 20,
    offset: 20,
    order: "sparse",
  });
});

test("主题板块陈旧时挂同一档黄色徽标，不是唯一一块只有灰色小字的", async () => {
  // 设计 §3.3 记的那次真实事故(据 88 580 个板块推出「图散成一地」,随后才得知库未整理)
  // 说的正是这一块数据。同一份 community_seq=100 / 落后 28,「本报告用到的数据」和
  // 「板块俯瞰图」都出黄标,这一块也必须出 —— 否则读者会因为「别的块有标、这块没有」
  // 而认为这块没问题。
  const view = renderView();
  await screen.findByRole("heading", { name: "主题板块", level: 3 });
  const block = blockByTitle("主题板块");
  expect(within(block).getByText("落后于当前内容")).toBeInTheDocument();
  expect(block.querySelectorAll(".anomaly-badge--retrieval").length).toBeGreaterThanOrEqual(1);
  view.unmount();

  // 徽标必须读**这一块自己**的那份新鲜度(report.boards),不能读同屏另一块的。
  // 夹具里 boards 与 board_edges 恰好同为「建于 #100 / 落后 28」,只断「陈旧时有徽标」
  // 的话,把数据源换成 board_edges 照样全绿 —— 所以这里把 boards 单独调成新鲜、
  // board_edges 保持陈旧:接错线就会在这里冒出一个不该有的徽标。
  const base = report();
  vi.mocked(fetchKgAnalysis).mockResolvedValue(
    report({
      boards: {
        ...base.boards,
        freshness: { basis: "community_snapshot", built_at_seq: 128, seq_behind: 0, stale: false, built_at_cluster_seq: 7, cluster_seq_behind: 0 },
      },
    }),
  );
  renderView();
  await screen.findByRole("heading", { name: "主题板块", level: 3 });
  const fresh = blockByTitle("主题板块");
  expect(within(fresh).queryByText("落后于当前内容")).not.toBeInTheDocument();
  expect(fresh.querySelectorAll(".anomaly-badge").length).toBe(0);
  // 对照:同屏的板块俯瞰图仍然陈旧,徽标还在 —— 证明上面那个「没有徽标」不是整页都没渲染。
  expect(within(blockByTitle("板块俯瞰图")).getByText("落后于当前内容")).toBeInTheDocument();
});
