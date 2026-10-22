// 图谱分析「问题清单」的渲染与交互守卫(2026-10-19 设计 §三/§四/§五)。
//
// 钉的是真实渲染出来的四个区与它们的按钮——纯逻辑那一半(排序、分区、S4 状态表、证据措辞、
// 出不出按钮)在 kg-findings-model.test.mjs:
//   · 四区:待处理(按严重度 → 影响面 → 主键)/ 需要开发处理(无按钮)/ 诊断数据(默认折叠)
//     / 已通过(✓ 与 – 分开,已标为正常可撤销,F4 已判定不是同一个);
//   · 只读成员看得到同样的问题与证据,但一个按钮都没有;
//   · 「更新分析」按 S4 置灰并在旁边写原因(含「合并规则已更新」与空版本不误判);
//   · 「已处理，更新后核验（原因）」「基于变更 #N」(页级 built_at)、对比行可比 / 不可比;
//   · F5 补上关联:open 出按钮、结果就地显示;relink_spent 随 passed 那一页回来,放进诊断数据、
//     不在「已通过」列出也不计数;F4 待确认合并有 / 无(按全组 canonical_ids);
//   · 按钮按下的在飞态与结果写在按钮旁(删除、重新解析、标为正常、撤销);
//   · 打开来源核对时把焦点放在按钮上(来源详情关掉后焦点回到这里);
//   · 重新解析的占位(真的来源库跟踪接上真的面板):从提交到这一次跑完,引用这篇来源的每一条
//     问题上的「重新解析…」都置灰并就地写「已提交重新解析，等待完成」,轮询读到上一次的终态
//     不放开,跑完才放开。
//
// ⚠ 不跨 await 攥 DOM 节点:状态变化之后一律在 waitFor 里重新查询。
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

vi.mock("../../app/kg-analysis-api", () => ({
  fetchKgAnalysis: vi.fn(),
  fetchKgAnalysisSources: vi.fn(),
  fetchKgFindings: vi.fn(),
  dismissKgFinding: vi.fn(),
  undismissKgFinding: vi.fn(),
}));

// 来源库只在「重新解析的占位」那组用例里接上;它读来源走 getSource。
vi.mock("../../app/source-api.ts", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../app/source-api.ts")>()),
  getSource: vi.fn(),
}));

import {
  dismissKgFinding,
  fetchKgAnalysis,
  fetchKgAnalysisSources,
  fetchKgFindings,
  undismissKgFinding,
  type KgAnalysisReport,
  type KgArtifactView,
  type KgFindingRow,
  type KgFindingsPage,
  type KgFindingsSummary,
  type KgFreshness,
  type KgSourceProfilePage,
} from "../../app/kg-analysis-api";
import type { KgFindingHandlers } from "../../app/kg-analysis-findings";
import { KgAnalysisView } from "../../app/kg-analysis-view";
import { getSource } from "../../app/source-api.ts";
import { useSourceLibrary, type SourceReparseOutcome } from "../../app/use-source-library";
import type { PendingMerge, SourceSummary } from "../../app/workspace-model";

const CURRENT: KgFreshness = {
  basis: "usable_live", built_at_seq: 128, seq_behind: 0, stale: false, built_at_cluster_seq: 7, cluster_seq_behind: 0,
};

function artifact(kind: string, overrides: Partial<KgArtifactView> = {}): KgArtifactView {
  return {
    kind,
    present: true,
    optional: kind === "source_profiles",
    absence: null,
    freshness: CURRENT,
    created_at: "2026-10-19T09:00:00Z",
    units: {},
    payload: {},
    ...overrides,
  };
}

function summary(overrides: Partial<KgFindingsSummary> = {}): KgFindingsSummary {
  return {
    present: true,
    absence: null,
    freshness: CURRENT,
    rules_version: 1,
    built_at: { kg_seq: 128, cluster_seq: 7 },
    created_at: "2026-10-19T09:00:00Z",
    counts: { by_rule: { F1: 1, F2: 1, F3: 1, F4: 1, F5: 1 }, dismissed: 1 },
    // 引擎的原字段,按规则代号为键。
    rules: {
      F1: { status: "ran", sources_checked: 84, sources_too_few_concepts: 3, sources_hit: 1, total: 1, truncated: false },
      F2: { status: "ran", sources_checked: 84, candidate_pairs: 40, pairs_hit: 1, total: 1, truncated: false },
      F3: { status: "ran", sources_checked: 84, median_relations_per_object: 1.2, median_isolated_share: 0.02, total: 1 },
      F4: { status: "ran", canonicals_checked: 9400, unnamed_concept_canonicals: 0, groups_decided_apart: 1, total: 1 },
      S1: { status: "ran", narrow_groups: 14, narrow_group_objects: 220, groups_decided_apart: 0 },
      F5: { status: "ran", objects_checked: 41713, total: 1 },
    },
    dev_items: [{
      item_id: "S1", groups: 14, objects: 220,
      samples: [{ names: ["RMS Norm", "RMSNorm"] }, { names: ["Q-Former", "Qformer"] }],
    }],
    structure: {
      graph_nodes: 37340, components: 1138, giant_nodes: 27000, usable_objects: 41713,
      giant_objects: 30366, giant_object_share: 0.728, island_components: 1137, island_sources: 14,
    },
    comparison: {
      previous_built_at_seq: 120, previous_built_at_cluster_seq: 6, comparable: true, reason: null,
      resolved: 2, persisting: 4, new: 1,
      resolved_samples: [{ rule_id: "F1", subject_kind: "source", subject_key: "src-z", subject_label: "Titans.pdf", impact_objects: 90 }],
      by_rule: {},
    },
    relink: { attempted: false, at_kg_seq: null, at: null, may_change: true },
    units: {},
    ...overrides,
  };
}

function report(overrides: Partial<KgAnalysisReport> = {}): KgAnalysisReport {
  return {
    notebook_id: "nb-1",
    generated_at: "2026-10-19T10:00:00Z",
    level: 0,
    ledger_state: "complete",
    ledger_consistent: true,
    state: {
      present: true,
      kg_mutation_seq: 128,
      community_seq: 128,
      cluster_mutation_seq: 7,
      canonical_rel_seq: 128,
      dirty: false,
      last_rebuild: {
        basis: "unified_rebuild_snapshot",
        at: "2026-10-19T08:00:00Z",
        object_count: 41713,
        relation_count: 51617,
        cluster_count: 37340,
        units: { object_count: "objects", relation_count: "relation_rows", cluster_count: "clusters" },
      },
      units: {},
    },
    artifacts: [
      artifact("cluster_size_histogram"),
      artifact("largest_clusters"),
      artifact("relation_provenance", { freshness: { ...CURRENT, built_at_cluster_seq: null, cluster_seq_behind: null } }),
      artifact("community_edges", { payload: { boards_in_giant: 80 } }),
      artifact("source_profiles"),
      artifact("findings"),
    ],
    boards: {
      freshness: CURRENT,
      units: {},
      payload: { level: 0, total: 0, returned: 0, truncated: false, limit: 50, top_members_limit: 5, communities: [] },
    },
    board_edges: {
      present: true, freshness: CURRENT, limit: 200, returned: 0, returned_weight: 0, stored: 0, stored_total: 0,
      stored_truncated: false, edge_limit: 200000, cross_weight: 0, weight_coverage: null, units: {}, edges: [],
    },
    findings_summary: summary(),
    merge_code: { stored: "v4|1024|1024", current: "v4|1024|1024", matches: true },
    ...overrides,
  };
}

function live(sourceId: string, title: string, extra: Partial<{ exists: boolean; source_type: string; processing: boolean }> = {}) {
  return { source_id: sourceId, exists: true, title, source_type: "pdf", processing: false, ...extra };
}

/** 规范形状的一行:没写的字段取「新发现、还没处理」的默认值。 */
function row(fields: Pick<KgFindingRow, "rule_id" | "severity" | "subject" | "evidence" | "impact" | "actions"> & Partial<KgFindingRow>): KgFindingRow {
  return {
    subject_refs: [],
    first_seen: { kg_seq: 128, cluster_seq: 7 },
    status: "open",
    handled_reason: null,
    dismissed_at: null,
    live_sources: [],
    ...fields,
  };
}

const F1 = row({
  rule_id: "F1",
  severity: "high",
  subject: { kind: "source", key: "src-a", label: "06_LMCache_mineru.md" },
  subject_refs: ["src-a"],
  evidence: { shared_share: 0, giant_share: 0, concepts: 84, samples: ["量子光学", "SEIR 模型"] },
  impact: { objects: 410 },
  actions: [{ kind: "delete_source", capability: "sources:write", source_id: "src-a" }],
  live_sources: [live("src-a", "06_LMCache_mineru.md")],
});

const F2 = row({
  rule_id: "F2",
  severity: "medium",
  subject: { kind: "source_pair", key: "src-b|src-c", label: "Mamba2_mineru ⇄ Mamba2_full" },
  subject_refs: ["src-b", "src-c"],
  evidence: {
    jaccard: 0.65,
    shared_concepts: 120,
    sources: [
      { source_id: "src-b", label: "Mamba2_mineru", objects: 300, keep: true },
      { source_id: "src-c", label: "Mamba2_full", objects: 280, keep: false },
    ],
    samples: ["状态空间模型"],
  },
  impact: { objects: 280 },
  // 后端只给建议删除的那一份的删除动作。
  actions: [{ kind: "delete_source", capability: "sources:write", source_id: "src-c" }],
  live_sources: [live("src-b", "Mamba2_mineru"), live("src-c", "Mamba2_full")],
});

const F3 = row({
  rule_id: "F3",
  severity: "medium",
  subject: { kind: "source", key: "src-d", label: "Minitron.pdf" },
  subject_refs: ["src-d"],
  evidence: {
    signals: ["fragmented_names", "low_relations"],
    fragmented_names: 8, fragmented_share: 0.12, fragmented_objects: 30,
    relations_per_object: 0.31, median_relations_per_object: 1.2,
    also_off_topic: true, samples: ["L l a m a 3 . 1"],
  },
  impact: { objects: 200 },
  actions: [{ kind: "reparse_source", capability: "sources:write", source_id: "src-d" }],
  live_sources: [live("src-d", "Minitron.pdf")],
});

const F4 = row({
  rule_id: "F4",
  severity: "low",
  subject: { kind: "concept_group", key: "rmsnorm", label: "RMS Norm" },
  evidence: {
    canonicals: 2, spellings: 2, member_sources: 3,
    members: [
      { canonical_id: "K-rms norm", name: "RMS Norm", objects: 12 },
      { canonical_id: "K-rmsnorm", name: "RMSNorm", objects: 3 },
    ],
    canonical_ids: ["K-rms norm", "K-rmsnorm"],
    classes: [["K-rms norm", "K-rmsnorm"]],
  },
  impact: { objects: 15 },
  actions: [{ kind: "review_merge", capability: "kg:write" }],
});

const F5 = row({
  rule_id: "F5",
  severity: "low",
  subject: { kind: "notebook", key: "notebook", label: "" },
  evidence: {
    isolated_objects: 1830, isolated_share: 0.044, usable_objects: 41713,
    top_sources: [{ source_id: "src-e", label: "Agentic RAG.pdf", isolated_objects: 120 }],
  },
  impact: { objects: 1830 },
  actions: [{ kind: "relink", capability: "kg:write" }],
});

/** F5 补上关联已经跑过、之后没有新内容:后端放在 passed 那一页里返回。 */
const SPENT: KgFindingRow = { ...F5, status: "relink_spent" };

const DECIDED: KgFindingRow = {
  ...F4,
  subject: { kind: "concept_group", key: "llama31", label: "Llama 3.1" },
  status: "decided_not_same",
};

const DISMISSED: KgFindingRow = {
  ...F1,
  subject: { kind: "source", key: "src-t", label: "Titans.pdf" },
  subject_refs: ["src-t"],
  actions: [{ kind: "delete_source", capability: "sources:write", source_id: "src-t" }],
  status: "dismissed",
  dismissed_at: "2026-10-19T09:30:00Z",
  live_sources: [live("src-t", "Titans.pdf")],
};

function page(zone: string, rows: KgFindingRow[], overrides: Partial<KgFindingsPage> = {}): KgFindingsPage {
  return {
    notebook_id: "nb-1",
    generated_at: "2026-10-19T10:00:00Z",
    present: true,
    absence: null,
    freshness: CURRENT,
    built_at: { kg_seq: 128, cluster_seq: 7 },
    rules_version: 1,
    zone,
    limit: 20,
    offset: 0,
    total: rows.length,
    returned: rows.length,
    has_more: false,
    units: { impact_objects: "objects" },
    evidence_units: {},
    rows,
    ...overrides,
  };
}

function sourcePage(): KgSourceProfilePage {
  return {
    notebook_id: "nb-1", generated_at: "2026-10-19T10:00:00Z", present: true, absence: null, freshness: CURRENT,
    kg_mutation_seq: 128, order: "sparse", limit: 20, offset: 0, total: 0, returned: 0, has_more: false,
    summary: null, units: {}, rows: [],
  };
}

let openRows: KgFindingRow[] = [];

beforeEach(() => {
  // 后端已排好;这里故意打乱,钉住「同一页内顺序确定」。
  openRows = [F4, F3, F5, F1, F2];
  vi.mocked(fetchKgAnalysis).mockResolvedValue(report());
  vi.mocked(fetchKgAnalysisSources).mockResolvedValue(sourcePage());
  vi.mocked(fetchKgFindings).mockImplementation(async (_nb, options) => {
    const zone = options?.zone ?? "open";
    if (zone === "passed") return page("passed", [DECIDED]);
    if (zone === "dismissed") return page("dismissed", [DISMISSED]);
    return page("open", openRows);
  });
  vi.mocked(dismissKgFinding).mockResolvedValue(undefined);
  vi.mocked(undismissKgFinding).mockResolvedValue(undefined);
  vi.mocked(getSource).mockReset();
});

afterEach(() => {
  vi.useRealTimers();
});

function renderPanel(props: Partial<Parameters<typeof KgAnalysisView>[0]> = {}) {
  return render(
    <KgAnalysisView
      notebookId="nb-1"
      onClose={() => undefined}
      canAnalyze
      onAnalyze={() => undefined}
      {...props}
    />,
  );
}

function zone(name: RegExp | string): HTMLElement {
  const heading = screen.getByRole("heading", { name, level: 3 });
  const block = heading.closest(".kg-analysis-block");
  if (!block) throw new Error(`没有找到「${String(name)}」所在的区`);
  return block as HTMLElement;
}

function findingItems(): HTMLElement[] {
  return Array.from(zone(/^待处理/).querySelectorAll<HTMLElement>("li.kg-finding"));
}

function findingByRule(ruleLabel: string): HTMLElement {
  const item = findingItems().find((element) => element.querySelector(".kg-finding-rule")?.textContent === ruleLabel);
  if (!item) throw new Error(`待处理里没有「${ruleLabel}」`);
  return item;
}

async function ready() {
  await waitFor(() => expect(findingItems().length).toBe(5));
}

// ------------------------------------------------------------------ 四个区

test("四个区依次排开：待处理（按严重度 → 影响面）、需要开发处理、诊断数据（折叠）、已通过", async () => {
  const { container } = renderPanel();
  await ready();

  expect(screen.getByRole("heading", { name: "待处理（5）", level: 3 })).toBeInTheDocument();
  // 高 → 中(影响面 280 先于 200)→ 低(1,830 先于 15)。
  expect(findingItems().map((item) => item.querySelector(".kg-finding-rule")?.textContent)).toEqual([
    "疑似内容错配", "疑似重复导入", "疑似解析残缺", "没建立关联的内容", "疑似没合并的同名概念",
  ]);

  const dev = zone(/^需要开发处理/);
  expect(within(dev).getByText(/14 组只差空格\/连字符的概念没有合并（例：RMS Norm \/ RMSNorm；Q-Former \/ Qformer）/)).toBeInTheDocument();
  expect(within(dev).queryAllByRole("button")).toHaveLength(0);

  const sections = Array.from(container.querySelectorAll(".kg-analysis-body > *"));
  const order = sections.map((element) => (
    element.matches("details") ? "诊断数据" : element.querySelector("h3")?.textContent ?? ""
  ));
  expect(order).toEqual(["报告状态", "待处理（5）", "需要开发处理（1）", "诊断数据", "已通过"]);
  expect((container.querySelector("details.kg-findings-diagnostics") as HTMLDetailsElement).open).toBe(false);
});

test("每条问题：严重度、规则名、定位标签、证据、影响面（带单位），且不上屏内部代号", async () => {
  renderPanel();
  await ready();

  const f1 = findingByRule("疑似内容错配");
  expect(within(f1).getByText("高")).toBeInTheDocument();
  expect(within(f1).getByText("来源")).toBeInTheDocument();
  expect(within(f1).getByText("06_LMCache_mineru.md")).toBeInTheDocument();
  expect(within(f1).getByText("影响 410 知识对象")).toBeInTheDocument();
  expect(within(f1).getByText("84 个概念里，只有 0% 也出现在其它来源里")).toBeInTheDocument();
  expect(within(f1).getByText("不在主体内容里")).toBeInTheDocument();
  expect(f1.querySelector(".kg-finding-samples")?.textContent).toBe("代表概念：量子光学、SEIR 模型");

  const f2 = findingByRule("疑似重复导入");
  expect(within(f2).getByText("两份共有 120 个概念，概念重合度 65.0%")).toBeInTheDocument();
  expect(within(f2).getByText("Mamba2_mineru：300 个知识对象（建议保留）")).toBeInTheDocument();
  expect(within(f2).getByText("Mamba2_full：280 个知识对象（建议删除）")).toBeInTheDocument();

  const f3 = findingByRule("疑似解析残缺");
  expect(within(f3).getByText("8 个概念的名字被拆成了单个字符（占这篇来源的 12.0%）")).toBeInTheDocument();
  expect(within(f3).getByText("平均每个知识对象只有 0.31 条关联，本库中位数是 1.20")).toBeInTheDocument();
  expect(within(f3).getByText("重新解析后若仍与本库无关，会转为疑似内容错配")).toBeInTheDocument();

  const f4 = findingByRule("疑似没合并的同名概念");
  expect(within(f4).getByText("同一个概念有 2 种写法没有合并，涉及 3 个来源")).toBeInTheDocument();
  expect(f4.querySelector(".kg-finding-samples")?.textContent).toBe("组内的名字（知识对象数）：RMS Norm（12）、RMSNorm（3）");

  const f5 = findingByRule("没建立关联的内容");
  expect(within(f5).getByText("全库")).toBeInTheDocument();
  expect(within(f5).getByText("1,830 个知识对象没有任何关联，占全部 41,713 个的 4.4%")).toBeInTheDocument();

  const pending = zone(/^待处理/);
  for (const code of ["F1", "F2", "src-a", "delete_source", "jaccard", "Jaccard", "canonical", "high"]) {
    expect(pending.textContent ?? "").not.toContain(code);
  }
});

test("认不出的规则与字段不报错、不上屏代号：只少几句话", async () => {
  openRows = [{
    rule_id: "F9",
    severity: "critical",
    subject: { kind: "galaxy", key: "x-1", label: "某个对象" },
    subject_refs: [],
    evidence: { weird_field: 3, samples: "not-a-list" },
    impact: { objects: 0 },
    actions: [{ kind: "teleport", capability: "x" }],
    first_seen: { kg_seq: 128, cluster_seq: 7 },
    status: "mystery",
    handled_reason: "teleported",
    dismissed_at: null,
    live_sources: [],
  }];
  renderPanel();
  await waitFor(() => expect(findingItems().length).toBe(1));
  const item = findingItems()[0];
  expect(within(item).getByText("其它问题")).toBeInTheDocument();
  expect(within(item).getByText("未分级")).toBeInTheDocument();
  expect(within(item).getByText("影响 0 知识对象")).toBeInTheDocument();
  for (const code of ["F9", "critical", "galaxy", "weird_field", "teleport", "mystery", "teleported"]) {
    expect(item.textContent ?? "").not.toContain(code);
  }
});

// ------------------------------------------------------------------ 只读

test("只读成员：同样的问题与证据，一个按钮都没有（含标为正常与撤销）", async () => {
  renderPanel({ canAnalyze: false, findingHandlers: {
    onOpenSource: vi.fn(), onDeleteSource: vi.fn(), onReparseSource: vi.fn(), onReviewMerge: vi.fn(), onRelink: vi.fn(),
  }, pendingMerges: [{ id: "m-1", canonical_a: "K-rms norm", canonical_b: "K-rmsnorm", score: 0.9, status: "pending" }] });
  await ready();
  expect(within(zone(/^待处理/)).queryAllByRole("button")).toHaveLength(0);
  expect(within(findingByRule("疑似内容错配")).getByText("84 个概念里，只有 0% 也出现在其它来源里")).toBeInTheDocument();
  await waitFor(() => expect(within(zone("已通过")).getByText(/已标为正常：疑似内容错配 · Titans\.pdf/)).toBeInTheDocument());
  expect(within(zone("已通过")).queryAllByRole("button")).toHaveLength(0);
  // 头部也没有「更新分析」,只说清谁可以处理。
  expect(screen.queryByRole("button", { name: "更新分析" })).toBeNull();
  expect(within(zone("报告状态")).getByText(/只读成员可以查看问题与证据/)).toBeInTheDocument();
});

// ------------------------------------------------------------------ 头部 / S4

test("报告与当前一致：「更新分析」置灰并写「无需更新」；对比行与已消失的名字", async () => {
  renderPanel();
  await ready();
  const head = zone("报告状态");
  expect(within(head).getByText("报告建于变更 #128、合并 #7 · 与当前一致")).toBeInTheDocument();
  expect(within(head).getByText("上次更新后：2 条已消失 · 4 条仍在 · 1 条新增")).toBeInTheDocument();
  expect(within(head).getByText("已消失：Titans.pdf")).toBeInTheDocument();
  expect(within(head).getByRole("button", { name: "更新分析" })).toBeDisabled();
  expect(within(head).getByText("已与当前图谱一致，无需更新")).toBeInTheDocument();
});

test("合并规则已更新：其余都一致时按钮仍可点，并写明为什么", async () => {
  vi.mocked(fetchKgAnalysis).mockResolvedValue(report({
    merge_code: { stored: "v3|1024|1024", current: "v4|1024|1024", matches: false },
  }));
  const onAnalyze = vi.fn();
  renderPanel({ onAnalyze });
  await ready();
  const head = zone("报告状态");
  const button = within(head).getByRole("button", { name: "更新分析" });
  expect(button).toBeEnabled();
  expect(within(head).getByText("合并规则已更新，需要更新一次")).toBeInTheDocument();
  fireEvent.click(button);
  expect(onAnalyze).toHaveBeenCalledTimes(1);
});

test("还没合并过（删除知识图谱之后 matches 为 null）：不读成「合并规则已更新」", async () => {
  vi.mocked(fetchKgAnalysis).mockResolvedValue(report({
    merge_code: { stored: "", current: "v4|1024|1024", matches: null },
  }));
  renderPanel();
  await ready();
  const head = zone("报告状态");
  expect(within(head).queryByText(/合并规则已更新/)).toBeNull();
  expect(within(head).getByText("已与当前图谱一致，无需更新")).toBeInTheDocument();
});

test("只剩合并代次无从判断：置灰，写普通更新为什么改变不了它", async () => {
  const base = report();
  vi.mocked(fetchKgAnalysis).mockResolvedValue(report({
    artifacts: base.artifacts.map((item) => (
      item.kind === "community_edges"
        ? { ...item, freshness: { ...CURRENT, stale: null, built_at_cluster_seq: null, cluster_seq_behind: null } }
        : item
    )),
  }));
  renderPanel();
  await ready();
  const head = zone("报告状态");
  expect(within(head).getByRole("button", { name: "更新分析" })).toBeDisabled();
  expect(within(head).getByText("这一项只有在重新划分主题板块时才能核对，普通更新不会改变它")).toBeInTheDocument();
});

test("有改动待整理：可点、不写原因；每条问题标「基于变更 #N」（N 取问题页的 built_at）", async () => {
  vi.mocked(fetchKgAnalysis).mockResolvedValue(report({
    state: { ...report().state, kg_mutation_seq: 140, dirty: true },
    findings_summary: summary({ freshness: { ...CURRENT, seq_behind: 12, stale: true } }),
  }));
  vi.mocked(fetchKgFindings).mockImplementation(async (_nb, options) => {
    const zoneName = options?.zone ?? "open";
    if (zoneName !== "open") return page(zoneName, []);
    return page("open", openRows, { built_at: { kg_seq: 126, cluster_seq: 7 } });
  });
  renderPanel();
  await ready();
  const head = zone("报告状态");
  expect(within(head).getByRole("button", { name: "更新分析" })).toBeEnabled();
  expect(within(head).queryByText(/无需更新/)).toBeNull();
  for (const item of findingItems()) {
    expect(within(item).getByText("基于变更 #126")).toBeInTheDocument();
  }
});

test("后台任务在跑：按钮禁用并就地说明；生成中换成「正在生成…」", async () => {
  vi.mocked(fetchKgAnalysis).mockResolvedValue(report({ state: { ...report().state, dirty: true } }));
  const view = renderPanel({ analysisBlocked: true });
  await ready();
  expect(within(zone("报告状态")).getByRole("button", { name: "更新分析" })).toBeDisabled();
  expect(within(zone("报告状态")).getByText("其它图谱任务进行中，完成后即可操作")).toBeInTheDocument();
  view.rerender(
    <KgAnalysisView notebookId="nb-1" onClose={() => undefined} canAnalyze onAnalyze={() => undefined} analysisRunning />,
  );
  expect(within(zone("报告状态")).getByRole("button", { name: "正在生成…" })).toBeDisabled();
});

test("升级后还没生成问题清单：灰色，不是红色；待处理说清这不代表没有问题", async () => {
  const base = report();
  vi.mocked(fetchKgAnalysis).mockResolvedValue(report({
    ledger_state: "partial",
    artifacts: base.artifacts.map((item) => (
      item.kind === "findings"
        ? artifact("findings", {
            present: false, absence: "upgrade_pending", payload: null,
            freshness: { basis: "usable_live", built_at_seq: null, seq_behind: null, stale: null, built_at_cluster_seq: null, cluster_seq_behind: null },
          })
        : item
    )),
    findings_summary: summary({ present: false, absence: "upgrade_pending" }),
  }));
  vi.mocked(fetchKgFindings).mockImplementation(async (_nb, options) => page(options?.zone ?? "open", [], {
    present: false, absence: "upgrade_pending", total: 0, built_at: null,
  }));
  const { container } = renderPanel();
  await waitFor(() => expect(within(zone(/^待处理/)).getByText(/这是升级后新增的问题清单/)).toBeInTheDocument());
  const head = zone("报告状态");
  expect(within(head).getByText("升级后还没生成问题清单")).toBeInTheDocument();
  expect(head.querySelector(".kg-findings-readiness.is-neutral")).not.toBeNull();
  expect(within(head).getByRole("button", { name: "更新分析" })).toBeEnabled();
  expect(within(zone(/^待处理/)).getByText("这里没有列出问题，不代表没有问题。")).toBeInTheDocument();
  // 数据清单里那一行是灰色「升级后还没生成」,不是红色「本该有却缺失」。
  const ledger = zone("本报告用到的数据");
  expect(within(ledger).getByText("升级后还没生成")).toBeInTheDocument();
  expect(within(ledger).queryByText("本该有却缺失")).toBeNull();
  expect(container.querySelectorAll(".anomaly-badge--integrity")).toHaveLength(0);
});

test("对比行不可比时写原因，不硬算", async () => {
  vi.mocked(fetchKgAnalysis).mockResolvedValue(report({
    findings_summary: summary({
      comparison: {
        previous_built_at_seq: 120, previous_built_at_cluster_seq: 6, comparable: false, reason: "rules_version_changed",
        resolved: null, persisting: null, new: null, resolved_samples: [], by_rule: {},
      },
    }),
  }));
  renderPanel();
  await ready();
  const head = zone("报告状态");
  expect(within(head).getByText("规则已更新，本次不与上次比较")).toBeInTheDocument();
  expect(within(head).queryByText(/上次更新后/)).toBeNull();
});

// ------------------------------------------------------------------ 已通过

test("已通过：✓ 检查过没发现、– 没法检查、– 部分没判断、已判定不是同一个、已标为正常分开写", async () => {
  vi.mocked(fetchKgAnalysis).mockResolvedValue(report({
    findings_summary: summary({
      rules: {
        F1: { status: "ran", sources_checked: 84, sources_too_few_concepts: 3, total: 1 },
        F2: { status: "skipped", reason: "fewer_than_two_sources", total: 0 },
        F3: { status: "ran", sources_checked: 84, total: 0 },
        F4: { status: "ran", canonicals_checked: 9400, groups_decided_apart: 1, total: 0 },
        S1: { status: "ran", narrow_groups: 14, narrow_group_objects: 220 },
        F5: { status: "ran", objects_checked: 41713, total: 1 },
      },
    }),
  }));
  renderPanel();
  await ready();
  const passed = zone("已通过");
  await waitFor(() => expect(within(passed).getByText(/已标为正常：疑似内容错配 · Titans\.pdf/)).toBeInTheDocument());
  const lines = Array.from(passed.querySelectorAll(".kg-findings-rule-line")).map((line) => line.textContent);
  expect(lines).toEqual([
    "–疑似内容错配：有 3 个来源概念太少，没有判断",
    "–疑似重复导入：没法检查，能互相比较的来源不足两个",
    "✓没发现解析残缺的来源（检查了 84 个来源）",
    "✓没发现没合并的同名概念（检查了 9,400 个合并后的知识对象）",
    // 重建之后用户驳回过的组不再成行,只剩计数;不写出来,用户的判断就看不见了。
    "≠疑似没合并的同名概念：已判定不是同一个的有 1 组，不再列为问题",
  ]);
  expect(within(passed).getByText(/已判定不是同一个：疑似没合并的同名概念 · Llama 3\.1/)).toBeInTheDocument();
  expect(within(passed).getByRole("button", { name: "撤销" })).toBeEnabled();
});

// ------------------------------------------------------------------ 处置按钮

test("已处理的问题留在待处理、写「已处理，更新后核验」和读侧认出的原因，不再出处置按钮", async () => {
  openRows = [
    { ...F1, status: "handled", handled_reason: "source_deleted", live_sources: [live("src-a", "06_LMCache_mineru.md", { exists: false })] },
    { ...F5, status: "handled", handled_reason: "relinked" },
  ];
  renderPanel({ findingHandlers: { onOpenSource: vi.fn(), onDeleteSource: vi.fn(), onRelink: vi.fn() } });
  await waitFor(() => expect(findingItems().length).toBe(2));
  const f1 = findingByRule("疑似内容错配");
  expect(within(f1).getByText("已处理，更新后核验（来源已删除）")).toBeInTheDocument();
  expect(within(f1).queryByRole("button", { name: /删除来源|打开来源核对|标为正常/ })).toBeNull();
  const f5 = findingByRule("没建立关联的内容");
  expect(within(f5).getByText("已处理，更新后核验（已跑过补上关联）")).toBeInTheDocument();
  expect(within(f5).queryAllByRole("button")).toHaveLength(0);
  expect(screen.getByRole("heading", { name: "待处理（2）", level: 3 })).toBeInTheDocument();
});

test("疑似内容错配：打开来源核对（按钮成为焦点）与删除来源（不可撤销的说明、在飞禁用、结果写在旁边）", async () => {
  let reportDelete: ((outcome: "deleted" | "failed") => void) | null = null;
  const focusedAtOpen: Array<Element | null> = [];
  const handlers: KgFindingHandlers = {
    onOpenSource: vi.fn(async () => { focusedAtOpen.push(document.activeElement); }),
    onDeleteSource: vi.fn((_target, report) => { reportDelete = report; }),
  };
  const view = renderPanel({ findingHandlers: handlers });
  await ready();
  const f1 = findingByRule("疑似内容错配");
  expect(within(f1).getByText("不可撤销，先打开来源核对")).toBeInTheDocument();

  const open = within(f1).getByRole("button", { name: "打开来源核对" });
  fireEvent.click(open);
  expect(handlers.onOpenSource).toHaveBeenCalledWith("src-a");
  // 关掉来源详情后焦点回到这颗按钮:协调器记下的是打开那一刻的焦点。
  expect(focusedAtOpen[0]).toBe(open);

  fireEvent.click(within(findingByRule("疑似内容错配")).getByRole("button", { name: "删除来源…" }));
  expect(handlers.onDeleteSource).toHaveBeenCalledWith(
    { sourceId: "src-a", title: "06_LMCache_mineru.md", sourceType: "pdf" },
    expect.any(Function),
  );
  // 确认之后删除在跑:按钮换成「删除中…」并禁用。
  view.rerender(
    <KgAnalysisView notebookId="nb-1" onClose={() => undefined} canAnalyze onAnalyze={() => undefined}
      findingHandlers={handlers} deletingSourceIds={new Set(["src-a"])} />,
  );
  expect(within(findingByRule("疑似内容错配")).getByRole("button", { name: "删除中…" })).toBeDisabled();

  const callsBefore = vi.mocked(fetchKgFindings).mock.calls.length;
  act(() => reportDelete?.("deleted"));
  await waitFor(() => expect(within(findingByRule("疑似内容错配")).getByText(/已删除「06_LMCache_mineru\.md」/)).toBeInTheDocument());
  // 删完重取问题页(这一条下一次会变成「已处理，更新后核验」)。
  expect(vi.mocked(fetchKgFindings).mock.calls.length).toBeGreaterThan(callsBefore);
});

test("来源详情叠在上面时面板只退出交互树、不卸载：滚动位置与问题原样保留", async () => {
  const view = renderPanel({ findingHandlers: { onOpenSource: vi.fn() } });
  await ready();
  const body = view.container.querySelector(".kg-analysis-body") as HTMLElement;
  body.scrollTop = 340;
  view.rerender(
    <KgAnalysisView notebookId="nb-1" onClose={() => undefined} canAnalyze onAnalyze={() => undefined}
      findingHandlers={{ onOpenSource: vi.fn() }} interactive={false} />,
  );
  expect(view.container.querySelector('[role="dialog"]')).toHaveAttribute("inert");
  view.rerender(
    <KgAnalysisView notebookId="nb-1" onClose={() => undefined} canAnalyze onAnalyze={() => undefined}
      findingHandlers={{ onOpenSource: vi.fn() }} />,
  );
  const after = view.container.querySelector(".kg-analysis-body") as HTMLElement;
  expect(after).toBe(body);
  expect(after.scrollTop).toBe(340);
  expect(findingItems()).toHaveLength(5);
});

test("面板里删掉一篇来源之后总览也重取：「更新分析」按新报告从「无需更新」变成可点", async () => {
  const handlers: KgFindingHandlers = { onDeleteSource: vi.fn((_target, report) => report("deleted")) };
  renderPanel({ findingHandlers: handlers });
  await ready();
  expect(within(zone("报告状态")).getByRole("button", { name: "更新分析" })).toBeDisabled();
  const overviewCalls = vi.mocked(fetchKgAnalysis).mock.calls.length;
  vi.mocked(fetchKgAnalysis).mockResolvedValue(report({ state: { ...report().state, kg_mutation_seq: 129, dirty: true } }));

  fireEvent.click(within(findingByRule("疑似内容错配")).getByRole("button", { name: "删除来源…" }));
  await waitFor(() => expect(within(zone("报告状态")).getByRole("button", { name: "更新分析" })).toBeEnabled());
  expect(vi.mocked(fetchKgAnalysis).mock.calls.length).toBe(overviewCalls + 1);
  expect(within(zone("报告状态")).queryByText(/无需更新/)).toBeNull();
  // 报告落后了:每条问题标出它基于哪一次变更。
  await waitFor(() => expect(within(findingByRule("疑似重复导入")).getByText("基于变更 #128")).toBeInTheDocument());
});

test("失效令牌变化（例如补上关联跑完）时总览静默重取；一次失败不把面板换成一句错误", async () => {
  const view = renderPanel({ findingsRevision: "1|0|0" });
  await ready();
  const overviewCalls = vi.mocked(fetchKgAnalysis).mock.calls.length;
  vi.mocked(fetchKgAnalysis).mockRejectedValueOnce(new Error("flaky"));
  view.rerender(
    <KgAnalysisView notebookId="nb-1" onClose={() => undefined} canAnalyze onAnalyze={() => undefined} findingsRevision="1|1|0" />,
  );
  await waitFor(() => expect(vi.mocked(fetchKgAnalysis).mock.calls.length).toBe(overviewCalls + 1));
  await waitFor(() => expect(findingItems()).toHaveLength(5));
  expect(screen.queryByRole("alert")).toBeNull();
  expect(within(zone("报告状态")).getByRole("button", { name: "更新分析" })).toBeDisabled();

  vi.mocked(fetchKgAnalysis).mockResolvedValue(report({ state: { ...report().state, dirty: true } }));
  view.rerender(
    <KgAnalysisView notebookId="nb-1" onClose={() => undefined} canAnalyze onAnalyze={() => undefined} findingsRevision="1|0|0" />,
  );
  await waitFor(() => expect(within(zone("报告状态")).getByRole("button", { name: "更新分析" })).toBeEnabled());
});

test("「标为正常」带对主键：两段原样交给接口（F2 的主键含「|」，编码在接口层做一次）", async () => {
  openRows = [F2];
  renderPanel();
  await waitFor(() => expect(findingItems()).toHaveLength(1));
  fireEvent.click(within(findingItems()[0]).getByRole("button", { name: "标为正常" }));
  expect(dismissKgFinding).toHaveBeenCalledWith("nb-1", "F2", "src-b|src-c");
});

test("来源已经不在了：打开来源核对失败时如实说，而不是叫人稍后重试", async () => {
  const gone = Object.assign(new Error("not found"), {});
  Object.defineProperty(gone, Symbol.for("silicon-notebook.errors.httpStatus"), { value: 404 });
  renderPanel({ findingHandlers: { onOpenSource: vi.fn(async () => { throw gone; }) } });
  await ready();
  fireEvent.click(within(findingByRule("疑似内容错配")).getByRole("button", { name: "打开来源核对" }));
  await waitFor(() => expect(within(findingByRule("疑似内容错配")).getByText("这篇来源已经不在了")).toBeInTheDocument());
  expect(within(findingByRule("疑似内容错配")).getByRole("button", { name: "打开来源核对" })).toBeEnabled();
});

test("只读成员看到的「还没生成」不叫他去点他没有的按钮", async () => {
  vi.mocked(fetchKgFindings).mockImplementation(async (_nb, options) => page(options?.zone ?? "open", [], {
    present: false, absence: "upgrade_pending", total: 0,
  }));
  renderPanel({ canAnalyze: false });
  await waitFor(() => expect(within(zone(/^待处理/)).getByText(/这是升级后新增的问题清单/)).toBeInTheDocument());
  const pending = zone(/^待处理/);
  expect(pending.textContent).toContain("拥有编辑权限的成员更新一次分析后就会生成");
  expect(pending.textContent).not.toContain("点上方");
});

test("删除没成功：失败写在按钮旁", async () => {
  const handlers: KgFindingHandlers = {
    onDeleteSource: vi.fn((_target, report) => report("failed")),
  };
  renderPanel({ findingHandlers: handlers });
  await ready();
  fireEvent.click(within(findingByRule("疑似内容错配")).getByRole("button", { name: "删除来源…" }));
  expect(within(findingByRule("疑似内容错配")).getByText("没有删掉，请稍后重试")).toBeInTheDocument();
});

test("疑似重复导入：两份各一个「打开来源」，删除只在建议删除的那份旁边，并写明保留理由", async () => {
  const handlers: KgFindingHandlers = { onOpenSource: vi.fn(), onDeleteSource: vi.fn() };
  renderPanel({ findingHandlers: handlers });
  await ready();
  const f2 = findingByRule("疑似重复导入");
  expect(within(f2).getByRole("button", { name: "打开来源核对：Mamba2_mineru" })).toBeInTheDocument();
  expect(within(f2).getByRole("button", { name: "打开来源核对：Mamba2_full" })).toBeInTheDocument();
  const deletes = within(f2).getAllByRole("button", { name: /删除来源/ });
  expect(deletes).toHaveLength(1);
  expect(deletes[0]).toHaveAccessibleName("删除来源：Mamba2_full");
  expect(within(f2).getByText("两份内容几乎相同，保留对象更多的一份")).toBeInTheDocument();
  // 按来源分组、写上是哪一份:两颗同名的「打开来源核对」不会读混。
  const groups = Array.from(f2.querySelectorAll<HTMLElement>(".kg-finding-source-group"));
  expect(groups.map((group) => group.querySelector(".kg-finding-source-name")?.textContent)).toEqual(["Mamba2_mineru", "Mamba2_full"]);
  expect(within(groups[0]).queryByRole("button", { name: /删除来源/ })).toBeNull();
  expect(within(groups[1]).getByRole("button", { name: "删除来源：Mamba2_full" })).toBeInTheDocument();
});

test("疑似解析残缺：重新解析先经确认；确认后在飞禁用，排上后结果写在按钮旁并重取问题页", async () => {
  let progress: ((phase: "running" | "started" | "skipped" | "failed") => void) | null = null;
  const handlers: KgFindingHandlers = {
    onReparseSource: vi.fn((_target, report) => { progress = report; }),
  };
  renderPanel({ findingHandlers: handlers });
  await ready();
  fireEvent.click(within(findingByRule("疑似解析残缺")).getByRole("button", { name: "重新解析…" }));
  expect(handlers.onReparseSource).toHaveBeenCalledWith(
    { sourceId: "src-d", title: "Minitron.pdf", sourceType: "pdf" },
    expect.any(Function),
  );
  // 确认框还开着(没回报 running)时按钮不进在飞态:取消不该把它锁住。
  expect(within(findingByRule("疑似解析残缺")).getByRole("button", { name: "重新解析…" })).toBeEnabled();
  act(() => progress?.("running"));
  expect(within(findingByRule("疑似解析残缺")).getByRole("button", { name: "重新解析中…" })).toBeDisabled();
  const callsBefore = vi.mocked(fetchKgFindings).mock.calls.length;
  act(() => progress?.("started"));
  await waitFor(() => expect(
    within(findingByRule("疑似解析残缺")).getByText("解析完成后点「更新分析」核验"),
  ).toBeInTheDocument());
  expect(vi.mocked(fetchKgFindings).mock.calls.length).toBeGreaterThan(callsBefore);
  // 这一行自己的在飞态到「排上」为止;排上之后锁不锁、锁到什么时候只看全页共用的重新解析占位
  // (这里没有传,等于来源库没在跟)——面板不另起一份。占位的全程见下方「重新解析的占位」那组用例。
  expect(within(findingByRule("疑似解析残缺")).getByRole("button", { name: "重新解析…" })).toBeEnabled();
});

test("收录来源不出「重新解析」；正在解析的来源写按钮置灰并说明", async () => {
  openRows = [
    { ...F3, live_sources: [live("src-d", "Minitron.pdf", { source_type: "promotion" })] },
    { ...F1, live_sources: [live("src-a", "06_LMCache_mineru.md", { processing: true })] },
  ];
  renderPanel({ findingHandlers: { onReparseSource: vi.fn(), onDeleteSource: vi.fn(), onOpenSource: vi.fn() } });
  await waitFor(() => expect(findingItems().length).toBe(2));
  expect(within(findingByRule("疑似解析残缺")).queryByRole("button", { name: /重新解析/ })).toBeNull();
  const f1 = findingByRule("疑似内容错配");
  expect(within(f1).getByRole("button", { name: "删除来源…" })).toBeDisabled();
  expect(within(f1).getByText("这篇来源正在解析，完成后再处理")).toBeInTheDocument();
});

test("没建立关联的内容：可能改变结果时出「补上关联」，在飞禁用；跑完的结果写在待处理区的区级提示里", async () => {
  const handlers: KgFindingHandlers = { onRelink: vi.fn() };
  const view = renderPanel({ findingHandlers: handlers });
  await ready();
  fireEvent.click(within(findingByRule("没建立关联的内容")).getByRole("button", { name: "补上关联" }));
  expect(handlers.onRelink).toHaveBeenCalledTimes(1);

  view.rerender(
    <KgAnalysisView notebookId="nb-1" onClose={() => undefined} canAnalyze onAnalyze={() => undefined}
      findingHandlers={handlers} relinking analysisBlocked />,
  );
  expect(within(findingByRule("没建立关联的内容")).getByRole("button", { name: "补连中…" })).toBeDisabled();

  view.rerender(
    <KgAnalysisView notebookId="nb-1" onClose={() => undefined} canAnalyze onAnalyze={() => undefined}
      findingHandlers={handlers} relinkResult={{ edgesAdded: 12, isolatedAfter: 1818 }} />,
  );
  expect(within(zone(/^待处理/)).getByText("补上关联已跑完：新增 12 条关联，还剩 1,818 个没有关联的知识条目")).toBeInTheDocument();
});

test("补上关联一条都没补上、报告是最新的：那一条移进折叠的诊断数据，结果仍写在看得见的待处理区", async () => {
  // 场景 A:报告与当前一致,补连 0 条 → 读侧判成 relink_spent、随 passed 那一页回来。
  const view = renderPanel({ findingHandlers: { onRelink: vi.fn() } });
  await ready();
  openRows = [F4, F3, F1, F2];
  vi.mocked(fetchKgAnalysis).mockResolvedValue(report({
    findings_summary: summary({ relink: { attempted: true, at_kg_seq: 128, at: "2026-10-20T00:00:00Z", may_change: false } }),
  }));
  vi.mocked(fetchKgFindings).mockImplementation(async (_nb, options) => {
    const zoneName = options?.zone ?? "open";
    if (zoneName === "passed") return page("passed", [DECIDED, SPENT]);
    if (zoneName === "dismissed") return page("dismissed", [DISMISSED]);
    return page("open", openRows);
  });
  view.rerender(
    <KgAnalysisView notebookId="nb-1" onClose={() => undefined} canAnalyze onAnalyze={() => undefined}
      findingHandlers={{ onRelink: vi.fn() }} findingsRevision="1|0" relinkResult={{ edgesAdded: 0, isolatedAfter: 1830 }} />,
  );
  await waitFor(() => expect(findingItems()).toHaveLength(4));
  const details = document.querySelector("details.kg-findings-diagnostics") as HTMLDetailsElement;
  await waitFor(() => expect(details.contains(zone("没建立关联的内容"))).toBe(true));
  expect(details.open).toBe(false);
  const notice = within(zone(/^待处理/)).getByText("补上关联已跑完：新增 0 条关联，还剩 1,830 个没有关联的知识条目");
  expect(details.contains(notice)).toBe(false);
});

test("补上关联一条都没补上、报告已落后：那一条写「已跑过补上关联」，不说「已补上」", async () => {
  // 场景 B:报告落后,补连 0 条 → 读侧判成 handled / relinked,留在待处理。
  openRows = [F1, { ...F5, status: "handled", handled_reason: "relinked" }];
  vi.mocked(fetchKgAnalysis).mockResolvedValue(report({ state: { ...report().state, dirty: true } }));
  renderPanel({ findingHandlers: { onRelink: vi.fn() }, relinkResult: { edgesAdded: 0, isolatedAfter: 1830 } });
  await waitFor(() => expect(findingItems()).toHaveLength(2));
  const f5 = findingByRule("没建立关联的内容");
  expect(within(f5).getByText("已处理，更新后核验（已跑过补上关联）")).toBeInTheDocument();
  expect(within(zone(/^待处理/)).queryByText(/已补上关联/)).toBeNull();
  expect(within(zone(/^待处理/)).getByText("补上关联已跑完：新增 0 条关联，还剩 1,830 个没有关联的知识条目")).toBeInTheDocument();
});

test("别的图谱任务占着维护槽：补上关联禁用并就地说明", async () => {
  renderPanel({ findingHandlers: { onRelink: vi.fn() }, analysisBlocked: true });
  await ready();
  const f5 = findingByRule("没建立关联的内容");
  expect(within(f5).getByRole("button", { name: "补上关联" })).toBeDisabled();
  expect(within(f5).getByText("其它图谱任务进行中")).toBeInTheDocument();
});

test("补上关联已经跑过（relink_spent）：随 passed 那一页回来，放进诊断数据、不出按钮，不在「已通过」列出（只读成员也看得到）", async () => {
  openRows = [F4, F3, F1, F2];
  vi.mocked(fetchKgAnalysis).mockResolvedValue(report({
    findings_summary: summary({ relink: { attempted: true, at_kg_seq: 128, at: "2026-10-18T00:00:00Z", may_change: false } }),
  }));
  vi.mocked(fetchKgFindings).mockImplementation(async (_nb, options) => {
    const zoneName = options?.zone ?? "open";
    if (zoneName === "passed") return page("passed", [DECIDED, SPENT]);
    if (zoneName === "dismissed") return page("dismissed", [DISMISSED]);
    return page("open", openRows);
  });
  const { container } = renderPanel({ findingHandlers: { onRelink: vi.fn() }, canAnalyze: false });
  await waitFor(() => expect(findingItems().length).toBe(4));
  expect(screen.getByRole("heading", { name: "待处理（4）", level: 3 })).toBeInTheDocument();
  const details = container.querySelector("details.kg-findings-diagnostics") as HTMLElement;
  await waitFor(() => expect(within(details).getByRole("heading", { name: "没建立关联的内容", level: 3 })).toBeInTheDocument());
  const spent = zone("没建立关联的内容");
  expect(details.contains(spent)).toBe(true);
  expect(within(spent).getByText("补上关联已经跑过，剩下 1,830 个没有可自动补上的关联；集中在某几个来源的，见疑似解析残缺")).toBeInTheDocument();
  expect(within(spent).getByText("1,830 个知识对象没有任何关联，占全部 41,713 个的 4.4%")).toBeInTheDocument();
  expect(within(spent).queryAllByRole("button")).toHaveLength(0);
  // 已通过区只列 F4 已判定不是同一个与已标为正常,不列这一行。
  const passed = zone("已通过");
  expect(within(passed).getByText(/已判定不是同一个：疑似没合并的同名概念 · Llama 3\.1/)).toBeInTheDocument();
  expect(Array.from(passed.querySelectorAll(".kg-findings-passed-row")).map((item) => item.textContent ?? ""))
    .not.toContainEqual(expect.stringContaining("没建立关联的内容"));
});

test("relink_spent 不计入「已通过」的条数；翻到不含它的页时诊断数据那块仍在，令牌一变按新一页重新认", async () => {
  const decidedRows = (count: number, from: number) => Array.from({ length: count }, (_, index) => ({
    ...DECIDED,
    subject: { kind: "concept_group", key: `group-${from + index}`, label: `组 ${from + index}` },
  }));
  let spentStill = true;
  vi.mocked(fetchKgFindings).mockImplementation(async (_nb, options) => {
    const zoneName = options?.zone ?? "open";
    const offset = options?.offset ?? 0;
    if (zoneName === "dismissed") return page("dismissed", []);
    if (zoneName === "open") return page("open", openRows);
    // passed 共 25 行:第一页 = relink_spent + 19 组,第二页 = 5 组。
    const total = spentStill ? 25 : 24;
    if (offset === 0) {
      const rows = spentStill ? [SPENT, ...decidedRows(19, 1)] : decidedRows(20, 1);
      return page("passed", rows, { total, has_more: true, returned: rows.length });
    }
    const rows = decidedRows(total - 20, 21);
    return page("passed", rows, { total, offset, has_more: false, returned: rows.length });
  });
  const view = renderPanel({ findingsRevision: "1|0|0" });
  await ready();
  const pager = () => zone("已通过").querySelector('[aria-label="已判定不是同一个的分页"]') as HTMLElement;
  await waitFor(() => expect(pager().textContent).toContain("第 1–19 条，共 24 条"));
  expect(within(zone("已通过")).queryByText(/没建立关联的内容/)).toBeNull();

  fireEvent.click(within(pager()).getByRole("button", { name: /下一页/ }));
  await waitFor(() => expect(pager().textContent).toContain("第 20–24 条，共 24 条"));
  // 第二页不含它:诊断数据那块沿用同一代里看到过的那一行。
  expect(screen.getByRole("heading", { name: "没建立关联的内容", level: 3 })).toBeInTheDocument();

  // 来了新内容(令牌变了),它回到了待处理:新一页不含它,诊断数据那块不再沿用。
  spentStill = false;
  view.rerender(
    <KgAnalysisView notebookId="nb-1" onClose={() => undefined} canAnalyze onAnalyze={() => undefined} findingsRevision="2|0|0" />,
  );
  await waitFor(() => expect(screen.queryByRole("heading", { name: "没建立关联的内容", level: 3 })).toBeNull());
  await waitFor(() => expect(pager().textContent).toContain("共 24 条"));
});

test("passed 那一页里没有 relink_spent 时，诊断数据里没有那块「已经跑过」的说明", async () => {
  const { container } = renderPanel();
  await ready();
  const details = container.querySelector("details.kg-findings-diagnostics") as HTMLElement;
  expect(within(details).queryByRole("heading", { name: "没建立关联的内容", level: 3 })).toBeNull();
});

test("疑似没合并的同名概念：待确认合并里有这一组就跳过去，没有就如实说", async () => {
  const merges: PendingMerge[] = [
    { id: "m-other", canonical_a: "K-rms norm", canonical_b: "K-layer norm", score: 0.7, status: "pending" },
    { id: "m-1", canonical_a: "K-rmsnorm", canonical_b: "K-rms norm", score: 0.92, status: "pending" },
  ];
  const handlers: KgFindingHandlers = { onReviewMerge: vi.fn() };
  const view = renderPanel({ findingHandlers: handlers, pendingMerges: merges });
  await ready();
  fireEvent.click(within(findingByRule("疑似没合并的同名概念")).getByRole("button", { name: "去待确认合并" }));
  // 一侧在组内、另一侧是别的概念的候选不是这一组。
  expect(handlers.onReviewMerge).toHaveBeenCalledWith("m-1");

  view.rerender(
    <KgAnalysisView notebookId="nb-1" onClose={() => undefined} canAnalyze onAnalyze={() => undefined}
      findingHandlers={handlers} pendingMerges={[merges[0]]} />,
  );
  const f4 = findingByRule("疑似没合并的同名概念");
  expect(within(f4).queryByRole("button", { name: "去待确认合并" })).toBeNull();
  expect(within(f4).getByText("待确认合并里还没有这一组")).toBeInTheDocument();
});

test("标为正常：在飞禁用；成功后从待处理移走、区里说清去了哪里", async () => {
  let release: () => void = () => {};
  vi.mocked(dismissKgFinding).mockImplementationOnce(() => new Promise<void>((resolve) => { release = resolve; }));
  renderPanel();
  await ready();
  fireEvent.click(within(findingByRule("疑似没合并的同名概念")).getByRole("button", { name: "标为正常" }));
  expect(dismissKgFinding).toHaveBeenCalledWith("nb-1", "F4", "rmsnorm");
  expect(within(findingByRule("疑似没合并的同名概念")).getByRole("button", { name: "处理中…" })).toBeDisabled();

  openRows = [F1, F2, F3, F5];
  await act(async () => { release(); });
  await waitFor(() => expect(findingItems().length).toBe(4));
  expect(within(zone(/^待处理/)).getByText(/已标为正常：疑似没合并的同名概念 · RMS Norm。可以在下方「已通过」里撤销。/)).toBeInTheDocument();
});

test("标为正常在飞时别的原因触发的重取（来源解析推进）换掉了行对象：按钮仍锁着，等这次写自己的重取结束才放开", async () => {
  let release: () => void = () => {};
  vi.mocked(dismissKgFinding).mockImplementationOnce(() => new Promise<void>((resolve) => { release = resolve; }));
  const view = renderPanel({ findingsRevision: "r1" });
  await ready();
  fireEvent.click(within(findingByRule("疑似没合并的同名概念")).getByRole("button", { name: "标为正常" }));
  expect(within(findingByRule("疑似没合并的同名概念")).getByRole("button", { name: "处理中…" })).toBeDisabled();

  // 来源库的跟踪推进了 contentRevision:问题页重取,回来的是新的行对象(同一条问题)。
  const callsBefore = vi.mocked(fetchKgFindings).mock.calls.length;
  openRows = openRows.map((item) => ({ ...item }));
  view.rerender(
    <KgAnalysisView notebookId="nb-1" onClose={() => undefined} canAnalyze onAnalyze={() => undefined} findingsRevision="r2" />,
  );
  await waitFor(() => expect(vi.mocked(fetchKgFindings).mock.calls.length).toBeGreaterThan(callsBefore));
  await settle();
  expect(within(findingByRule("疑似没合并的同名概念")).getByRole("button", { name: "处理中…" })).toBeDisabled();

  // 这次写回来、它自己触发的那次重取结束:这一条移走。
  openRows = openRows.filter((item) => item.rule_id !== "F4");
  await act(async () => { release(); });
  await waitFor(() => expect(findingItems()).toHaveLength(4));
});

test("标为正常写成功、随后的问题页重取失败：按钮不停在「处理中…」，按钮旁说清标记已写上", async () => {
  renderPanel();
  await ready();
  vi.mocked(fetchKgFindings).mockRejectedValue(new Error("flaky"));
  fireEvent.click(within(findingByRule("疑似没合并的同名概念")).getByRole("button", { name: "标为正常" }));
  expect(within(findingByRule("疑似没合并的同名概念")).getByRole("button", { name: "处理中…" })).toBeDisabled();
  expect(dismissKgFinding).toHaveBeenCalledWith("nb-1", "F4", "rmsnorm");
  await waitFor(() => expect(within(findingByRule("疑似没合并的同名概念")).getByRole("button", { name: "标为正常" })).toBeEnabled());
  expect(within(findingByRule("疑似没合并的同名概念")).getByText("已标为正常，但问题清单没能刷新；点上方「刷新」重新读取")).toBeInTheDocument();
  expect(within(zone(/^待处理/)).getByText("问题清单没能刷新，下面是上一次读到的内容")).toBeInTheDocument();
});

test("撤销写成功、随后的重取失败：按钮不停在「撤销中…」，按钮旁说清已撤销", async () => {
  renderPanel();
  await ready();
  await waitFor(() => expect(within(zone("已通过")).getByRole("button", { name: "撤销" })).toBeInTheDocument());
  vi.mocked(fetchKgFindings).mockRejectedValue(new Error("flaky"));
  fireEvent.click(within(zone("已通过")).getByRole("button", { name: "撤销" }));
  expect(within(zone("已通过")).getByRole("button", { name: "撤销中…" })).toBeDisabled();
  await waitFor(() => expect(within(zone("已通过")).getByRole("button", { name: "撤销" })).toBeEnabled());
  expect(within(zone("已通过")).getByText("已撤销，但问题清单没能刷新；点上方「刷新」重新读取")).toBeInTheDocument();
});

test("标为正常失败：原因写在按钮旁，按钮恢复可点", async () => {
  vi.mocked(dismissKgFinding).mockRejectedValueOnce(new Error("boom"));
  renderPanel();
  await ready();
  fireEvent.click(within(findingByRule("疑似没合并的同名概念")).getByRole("button", { name: "标为正常" }));
  await waitFor(() => expect(within(findingByRule("疑似没合并的同名概念")).getByText("没能标为正常，请稍后重试")).toBeInTheDocument());
  expect(within(findingByRule("疑似没合并的同名概念")).getByRole("button", { name: "标为正常" })).toBeEnabled();
});

test("撤销「标为正常」：调用撤销接口并重取，已通过区说清它回到了待处理", async () => {
  renderPanel();
  await ready();
  await waitFor(() => expect(within(zone("已通过")).getByRole("button", { name: "撤销" })).toBeInTheDocument());
  const callsBefore = vi.mocked(fetchKgFindings).mock.calls.length;
  fireEvent.click(within(zone("已通过")).getByRole("button", { name: "撤销" }));
  expect(undismissKgFinding).toHaveBeenCalledWith("nb-1", "F1", "src-t");
  await waitFor(() => expect(within(zone("已通过")).getByText(/已撤销：疑似内容错配 · Titans\.pdf 回到了待处理/)).toBeInTheDocument());
  // 撤销接口是在 act 之外回来的:提示与「重取」的 effect 同一次提交排上,但 waitFor 期间 effect
  // 异步冲刷——提示看得见时重取可能还没发出去(本机全量并发下见过 3 > 3 失败),所以也在 waitFor 里等。
  await waitFor(() => expect(vi.mocked(fetchKgFindings).mock.calls.length).toBeGreaterThan(callsBefore));
});

/** 从第 `from` 次起问过的问题页,写成「区@偏移」并排序(请求集合,不是总次数)。 */
function findingsRequestsSince(from: number): string[] {
  return vi.mocked(fetchKgFindings).mock.calls.slice(from)
    .map(([, options]) => `${options?.zone ?? "open"}@${options?.offset ?? 0}`)
    .sort();
}

/** 让已经排上的 effect 与请求都跑完(waitFor 期间 React 的 effect 是异步冲刷的)。 */
async function settle() {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 30));
  });
}

test("失效令牌变化（来源被删、补上关联跑完……）时三页按当前偏移各重取一次", async () => {
  // 已通过区的两页要等总览回来、看过计数才取(findingsZonesToFetch),比待处理晚一拍;而
  // waitFor 等待期间 React 的 effect 是异步冲刷的——「待处理列出来了」那一刻它们未必已经
  // 发出。所以起点要等三页都取过一次再记,断言的是请求集合而不是总次数(CI 上曾在起点只
  // 记到 1 次请求,于是数成 6 ≠ 4)。
  const view = renderPanel({ findingsRevision: "1|0|0" });
  await ready();
  await waitFor(() => expect(findingsRequestsSince(0)).toEqual(["dismissed@0", "open@0", "passed@0"]));
  await settle();
  const callsBefore = vi.mocked(fetchKgFindings).mock.calls.length;
  view.rerender(
    <KgAnalysisView notebookId="nb-1" onClose={() => undefined} canAnalyze onAnalyze={() => undefined} findingsRevision="2|0|0" />,
  );
  await waitFor(() => expect(findingsRequestsSince(callsBefore)).toEqual(["dismissed@0", "open@0", "passed@0"]));
  // 每页恰好一次:没有重复失效、没有依赖抖动带来的第二轮。
  await settle();
  expect(findingsRequestsSince(callsBefore)).toEqual(["dismissed@0", "open@0", "passed@0"]);
});

test("换库时问题清单当场清空：新库的报告先回来、问题清单还在路上时，也不摆上一个库的问题", async () => {
  const view = renderPanel();
  await ready();
  vi.mocked(fetchKgAnalysis).mockResolvedValue(report({ notebook_id: "nb-2" }));
  vi.mocked(fetchKgFindings).mockReturnValue(new Promise(() => {}));
  view.rerender(<KgAnalysisView notebookId="nb-2" onClose={() => undefined} canAnalyze onAnalyze={() => undefined} />);
  expect(screen.queryByText("06_LMCache_mineru.md")).toBeNull();
  await waitFor(() => expect(screen.getByRole("heading", { name: "报告状态", level: 3 })).toBeInTheDocument());
  expect(screen.queryByText("06_LMCache_mineru.md")).toBeNull();
  expect(within(zone(/^待处理/)).getByText("正在读取问题清单…")).toBeInTheDocument();
});

test("待处理分页：按 offset 取下一页", async () => {
  vi.mocked(fetchKgFindings).mockImplementation(async (_nb, options) => {
    const zoneName = options?.zone ?? "open";
    if (zoneName !== "open") return page(zoneName, []);
    return page("open", openRows, { total: 45, has_more: (options?.offset ?? 0) + 20 < 45, offset: options?.offset ?? 0 });
  });
  renderPanel();
  await ready();
  expect(screen.getByRole("heading", { name: "待处理（45）", level: 3 })).toBeInTheDocument();
  fireEvent.click(within(zone(/^待处理/)).getByRole("button", { name: /下一页/ }));
  await waitFor(() => expect(vi.mocked(fetchKgFindings)).toHaveBeenLastCalledWith("nb-1", { zone: "open", limit: 20, offset: 20 }));
});

// ------------------------------------------------------------------ 评审跟进

test("截断了却一条都没有：已通过区写「–」并说明没能检查完整，不画「✓」", async () => {
  vi.mocked(fetchKgAnalysis).mockResolvedValue(report({
    findings_summary: summary({
      rules: {
        F1: { status: "ran", sources_checked: 49000, sources_too_few_concepts: 0, total: 0, truncated: true },
        F2: { status: "ran", sources_checked: 49000, total: 0, persisted: 0, truncated: true },
        F3: { status: "ran", sources_checked: 49000, total: 1 },
        F4: { status: "ran", canonicals_checked: 9400, groups_decided_apart: 0, total: 1 },
        F5: { status: "ran", objects_checked: 41713, total: 1 },
      },
    }),
  }));
  openRows = [F3, F4, F5];
  renderPanel();
  await waitFor(() => expect(findingItems()).toHaveLength(3));
  const passed = zone("已通过");
  const lines = Array.from(passed.querySelectorAll(".kg-findings-rule-line")).map((line) => line.textContent);
  expect(lines).toContain("–疑似内容错配：没能检查完整，这次没发现不代表没有");
  expect(lines).toContain("–疑似重复导入：没能检查完整，这次没发现不代表没有");
  expect(lines.some((line) => line?.startsWith("✓没发现内容错配") || line?.startsWith("✓没发现重复导入"))).toBe(false);
  // 待处理区同时说没检查完整——两处说的是同一件事,不再一处打勾一处说没查完。
  expect(within(zone(/^待处理/)).getByText("疑似重复导入没能检查完整，列出的不是全部")).toBeInTheDocument();
});

test("一定为空的区不发请求：没有标为正常的问题、账本里没有 F4 且补上关联没跑过时，只取待处理那一页", async () => {
  vi.mocked(fetchKgAnalysis).mockResolvedValue(report({
    findings_summary: summary({ counts: { by_rule: { F1: 1, F2: 1, F3: 1, F5: 1 }, dismissed: 0 } }),
  }));
  openRows = [F1, F2, F3, F5];
  renderPanel();
  await waitFor(() => expect(findingItems()).toHaveLength(4));
  // 「没发请求」要等总览驱动的 effect 跑完再断言,否则可能只是还没轮到。
  await settle();
  const zones = vi.mocked(fetchKgFindings).mock.calls.map(([, options]) => options?.zone);
  expect(zones).not.toContain("passed");
  expect(zones).not.toContain("dismissed");
  expect(zones).toContain("open");
});

test("标为正常之后总览也重取：豁免数从 0 变成 1，「已标为正常」那一页这才去取", async () => {
  vi.mocked(fetchKgAnalysis).mockResolvedValue(report({
    findings_summary: summary({ counts: { by_rule: { F1: 1, F2: 1, F3: 1, F5: 1 }, dismissed: 0 } }),
  }));
  openRows = [F1, F2, F3, F5];
  renderPanel();
  await waitFor(() => expect(findingItems()).toHaveLength(4));
  expect(vi.mocked(fetchKgFindings).mock.calls.some(([, options]) => options?.zone === "dismissed")).toBe(false);
  vi.mocked(fetchKgAnalysis).mockResolvedValue(report({
    findings_summary: summary({ counts: { by_rule: { F1: 1, F2: 1, F3: 1, F5: 1 }, dismissed: 1 } }),
  }));
  openRows = [F2, F3, F5];
  fireEvent.click(within(findingByRule("疑似内容错配")).getByRole("button", { name: "标为正常" }));
  await waitFor(() => expect(vi.mocked(fetchKgFindings).mock.calls.some(([, options]) => options?.zone === "dismissed")).toBe(true));
  await waitFor(() => expect(within(zone("已通过")).getByText(/已标为正常：疑似内容错配 · Titans\.pdf/)).toBeInTheDocument());
});

test("问题页重取失败：上一次读到的列表原样留着，就地说一句没能刷新", async () => {
  const view = renderPanel({ findingsRevision: "1|0" });
  await ready();
  vi.mocked(fetchKgFindings).mockRejectedValue(new Error("flaky"));
  view.rerender(
    <KgAnalysisView notebookId="nb-1" onClose={() => undefined} canAnalyze onAnalyze={() => undefined} findingsRevision="2|0" />,
  );
  await waitFor(() => expect(within(zone(/^待处理/)).getByText("问题清单没能刷新，下面是上一次读到的内容")).toBeInTheDocument());
  expect(findingItems()).toHaveLength(5);
  expect(within(zone(/^待处理/)).queryByRole("alert")).toBeNull();
  // 下一次取到了:提示消失。
  vi.mocked(fetchKgFindings).mockImplementation(async (_nb, options) => {
    const zoneName = options?.zone ?? "open";
    if (zoneName === "passed") return page("passed", [DECIDED]);
    if (zoneName === "dismissed") return page("dismissed", [DISMISSED]);
    return page("open", openRows);
  });
  view.rerender(
    <KgAnalysisView notebookId="nb-1" onClose={() => undefined} canAnalyze onAnalyze={() => undefined} findingsRevision="3|0" />,
  );
  await waitFor(() => expect(within(zone(/^待处理/)).queryByText("问题清单没能刷新，下面是上一次读到的内容")).toBeNull());
});

test("对比整体不可比时，仍可比的规则各写各的计数", async () => {
  vi.mocked(fetchKgAnalysis).mockResolvedValue(report({
    findings_summary: summary({
      comparison: {
        previous_built_at_seq: 120, previous_built_at_cluster_seq: 6, comparable: false, reason: "truncated_or_not_run",
        resolved: null, persisting: null, new: null,
        resolved_samples: [{ rule_id: "F3", subject_kind: "source", subject_key: "s", subject_label: "Minitron.pdf", impact_objects: 9 }],
        by_rule: {
          F1: { comparable: false }, F2: { comparable: false },
          F3: { comparable: true, resolved: 1, persisting: 2, new: 0 },
        },
      },
    }),
  }));
  renderPanel();
  await ready();
  const head = zone("报告状态");
  expect(within(head).getByText("上次或这次有规则没能完整检查，本次不与上次比较")).toBeInTheDocument();
  expect(within(head).getByText("疑似解析残缺：1 条已消失 · 2 条仍在 · 0 条新增")).toBeInTheDocument();
  expect(within(head).getByText("已消失：Minitron.pdf")).toBeInTheDocument();
});

test("「已标为正常」停在已经不存在的那一页时退回上一页（撤销第 2 页的最后一条之后）", async () => {
  let total = 21;
  const marked = (count: number, from: number) => Array.from({ length: count }, (_, index) => ({
    ...DISMISSED,
    subject: { kind: "source", key: `src-m${from + index}`, label: `标过 ${from + index}.pdf` },
  }));
  vi.mocked(fetchKgFindings).mockImplementation(async (_nb, options) => {
    const zoneName = options?.zone ?? "open";
    const offset = options?.offset ?? 0;
    if (zoneName === "open") return page("open", openRows);
    if (zoneName === "passed") return page("passed", [DECIDED]);
    const rows = offset === 0 ? marked(Math.min(20, total), 1) : marked(Math.max(0, total - 20), 21);
    return page("dismissed", rows, { offset, total, returned: rows.length, has_more: offset + 20 < total });
  });
  renderPanel();
  await ready();
  const pager = () => zone("已通过").querySelector('[aria-label="已标为正常的分页"]') as HTMLElement | null;
  await waitFor(() => expect(pager()).not.toBeNull());
  fireEvent.click(within(pager()!).getByRole("button", { name: /下一页/ }));
  await waitFor(() => expect(within(zone("已通过")).getByText(/标过 21\.pdf/)).toBeInTheDocument());
  total = 20;
  fireEvent.click(within(zone("已通过")).getByRole("button", { name: "撤销" }));
  // 第 2 页空了:退回第 1 页,而不是留一页空白。
  await waitFor(() => expect(within(zone("已通过")).getByText(/标过 20\.pdf/)).toBeInTheDocument());
  const rows = Array.from(zone("已通过").querySelectorAll(".kg-findings-passed-row")).map((row) => row.textContent ?? "");
  expect(rows.some((row) => row.includes("标过 21.pdf"))).toBe(false);
});

test("疑似重复导入的两份同名来源：按钮组名与读屏名带上对象数，分得开", async () => {
  openRows = [{
    ...F2,
    subject: { kind: "source_pair", key: "src-b|src-c", label: "Mamba2.pdf ⇄ Mamba2.pdf" },
    evidence: {
      ...F2.evidence,
      sources: [
        { source_id: "src-b", label: "Mamba2.pdf", objects: 300, keep: true },
        { source_id: "src-c", label: "Mamba2.pdf", objects: 280, keep: false },
      ],
    },
    live_sources: [live("src-b", "Mamba2.pdf"), live("src-c", "Mamba2.pdf")],
  }];
  renderPanel({ findingHandlers: { onOpenSource: vi.fn(), onDeleteSource: vi.fn() } });
  await waitFor(() => expect(findingItems()).toHaveLength(1));
  const f2 = findingItems()[0];
  const groups = Array.from(f2.querySelectorAll<HTMLElement>(".kg-finding-source-group"));
  expect(groups.map((group) => group.querySelector(".kg-finding-source-name")?.textContent)).toEqual([
    "Mamba2.pdf（300 个知识对象）",
    "Mamba2.pdf（280 个知识对象）",
  ]);
  expect(within(f2).getByRole("button", { name: "打开来源核对：Mamba2.pdf（300 个知识对象）" })).toBeInTheDocument();
  expect(within(f2).getByRole("button", { name: "删除来源：Mamba2.pdf（280 个知识对象）" })).toBeInTheDocument();
});

// ------------------------------------------------------------ 重新解析的占位(来源库跟踪,全页一份)
//
// 真的来源库(use-source-library)接上真的面板,按 page.tsx 的接线:面板的占位就是来源库的
// `reparsingSourceIds`;确认框「重新解析」之后那一段交给 `submitFollowedReparse`(读基准 → 提交
// → 排上就按 id 跟到这一次跑完)。重新解析端点只把作业交给作业池、返回前不改来源状态,作业池忙时
// 头几次轮询读到的还是上一次的终态——那段时间按钮必须一直锁着。

const LIBRARY_EFFECTS = {
  setStatusText: vi.fn(),
  reportError: vi.fn(),
  setToast: vi.fn(),
  invalidateKnowledge: vi.fn(),
  refreshCollection: vi.fn(async () => undefined),
  refreshNotebook: vi.fn(async () => undefined),
  refreshCheckup: vi.fn(async () => undefined),
};
const REPARSING_NOTE = "已提交重新解析，等待完成";

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => { resolve = res; });
  return { promise, resolve };
}

let library: ReturnType<typeof useSourceLibrary> | null = null;
let submission: Promise<SourceReparseOutcome> | null = null;

function FollowedPanel({ submit }: { submit: () => Promise<SourceReparseOutcome> }) {
  const sourceLibrary = useSourceLibrary({ actorId: "user-a", canWriteSources: true, effects: LIBRARY_EFFECTS });
  library = sourceLibrary;
  return (
    <KgAnalysisView
      notebookId="nb-1"
      onClose={() => undefined}
      canAnalyze
      onAnalyze={() => undefined}
      findingsRevision={sourceLibrary.contentRevision}
      reparsingSourceIds={sourceLibrary.reparsingSourceIds}
      findingHandlers={{
        onReparseSource: (target, report) => {
          report("running");
          submission = sourceLibrary.submitFollowedReparse(target.sourceId, submit).then((outcome) => {
            report(outcome === "none" ? "skipped" : outcome);
            return outcome;
          });
        },
      }}
    />
  );
}

async function renderFollowedPanel(submit: () => Promise<SourceReparseOutcome>) {
  library = null;
  submission = null;
  render(<FollowedPanel submit={submit} />);
  act(() => {
    library!.commitNotebookSnapshot({
      actorId: "user-a", notebookId: "nb-1", workspaceEpoch: 1,
      page: { items: [], total_count: 0, offset: 0, limit: 50 },
    });
  });
  await waitFor(() => expect(findingItems()).toHaveLength(openRows.length));
  await settle();
}

/** 来源详情的读数:这一篇此刻的解析状态与 updated_at。 */
function detailAt(sourceId: string, parseStatus: string, updatedAt: string): SourceSummary {
  return {
    id: sourceId, notebook_id: "nb-1", title: sourceId, display_title: sourceId, type: "file",
    status: parseStatus, parse_status: parseStatus, summary: "", element_count: 0,
    file_name: `${sourceId}.pdf`, file_size: 1, created_at: "2026-10-19T00:00:00Z", created_label: "10月19日",
    updated_at: updatedAt,
  };
}

function reparseButtonOf(ruleLabel: string): HTMLElement {
  return within(findingByRule(ruleLabel)).getByRole("button", { name: /^重新解析/ });
}

/** 这一条的「重新解析…」被占位锁着:置灰,旁边就地写状态(读屏按状态播报)。 */
function expectHeld(ruleLabel: string) {
  const item = findingByRule(ruleLabel);
  expect(within(item).getByRole("button", { name: "重新解析…" })).toBeDisabled();
  expect(within(item).getByText(REPARSING_NOTE)).toHaveAttribute("role", "status");
}

function expectReleased(ruleLabel: string) {
  const item = findingByRule(ruleLabel);
  expect(within(item).getByRole("button", { name: "重新解析…" })).toBeEnabled();
  expect(within(item).queryByText(REPARSING_NOTE)).toBeNull();
}

/** 同一篇来源(src-d)出现在第二条问题里:疑似内容错配,建议重新解析。 */
const MISMATCH_ON_D = row({
  rule_id: "F1",
  severity: "high",
  subject: { kind: "source", key: "src-d", label: "Minitron.pdf" },
  subject_refs: ["src-d"],
  evidence: { shared_share: 0, giant_share: 0, concepts: 40, samples: ["量子光学"] },
  impact: { objects: 200 },
  actions: [{ kind: "reparse_source", capability: "sources:write", source_id: "src-d" }],
  live_sources: [live("src-d", "Minitron.pdf")],
});

test("重新解析排上之后、这一次跑完之前（轮询读到的还是上一次的终态）：按钮一直置灰并就地写状态；跑完才放开", async () => {
  const request = deferred<SourceReparseOutcome>();
  vi.mocked(getSource)
    .mockResolvedValueOnce(detailAt("src-d", "extracted", "T0")) // 提交前读的基准
    .mockResolvedValueOnce(detailAt("src-d", "extracted", "T0")) // 作业池忙:还是上一次的终态
    .mockResolvedValueOnce(detailAt("src-d", "extracted", "T0"))
    .mockResolvedValueOnce(detailAt("src-d", "parsing", "T1"))
    .mockResolvedValue(detailAt("src-d", "extracted", "T2"));
  await renderFollowedPanel(() => request.promise);
  vi.useFakeTimers();

  fireEvent.click(reparseButtonOf("疑似解析残缺"));
  expect(reparseButtonOf("疑似解析残缺")).toBeDisabled();
  await act(async () => {
    request.resolve("started");
    await submission;
  });
  expectHeld("疑似解析残缺");
  expect(within(findingByRule("疑似解析残缺")).getByText("解析完成后点「更新分析」核验")).toBeInTheDocument();

  // 头两次轮询读到的都是上一次留下的「已就绪」:不是这一次跑完,不放开。
  await act(async () => vi.advanceTimersByTimeAsync(1500));
  await act(async () => vi.advanceTimersByTimeAsync(2250));
  expect(vi.mocked(getSource)).toHaveBeenCalledTimes(3);
  expectHeld("疑似解析残缺");
  // 这一次开始了(解析中):仍锁着。
  await act(async () => vi.advanceTimersByTimeAsync(3375));
  expect(vi.mocked(getSource)).toHaveBeenCalledTimes(4);
  expectHeld("疑似解析残缺");
  // 这一次跑完:放开,状态也撤掉。
  await act(async () => vi.advanceTimersByTimeAsync(5063));
  expect(vi.mocked(getSource)).toHaveBeenCalledTimes(5);
  expectReleased("疑似解析残缺");
});

test("两条问题引用同一篇来源：从一条提交重新解析，另一条从确认那一刻起也置灰并就地写状态；占位一撤两条一起放开", async () => {
  openRows = [F3, MISMATCH_ON_D];
  const request = deferred<SourceReparseOutcome>();
  vi.mocked(getSource).mockResolvedValue(detailAt("src-d", "extracted", "T0"));
  await renderFollowedPanel(() => request.promise);
  vi.useFakeTimers();
  expectReleased("疑似内容错配");

  fireEvent.click(reparseButtonOf("疑似解析残缺"));
  // 请求还在路上:点的那一条是「重新解析中…」,另一条已经点不了。
  expect(within(findingByRule("疑似解析残缺")).getByRole("button", { name: "重新解析中…" })).toBeDisabled();
  expectHeld("疑似内容错配");
  await act(async () => {
    request.resolve("started");
    await submission;
  });
  expectHeld("疑似解析残缺");
  expectHeld("疑似内容错配");
  // 轮询读到上一次的终态:两条都还锁着。
  await act(async () => vi.advanceTimersByTimeAsync(1500 + 2250));
  expectHeld("疑似解析残缺");
  expectHeld("疑似内容错配");
  // 来源库不再跟这一篇(面板关闭时的 untrackSources;跑完、404、超时、切库同样走到这里):一起放开。
  act(() => library!.untrackSources());
  expectReleased("疑似解析残缺");
  expectReleased("疑似内容错配");
});

test.each([
  ["failed", "没能开始重新解析，请稍后重试"],
  ["none", "这篇来源已不在或无需重新解析"],
] as const)("没排上（%s）：占位当场撤掉，按钮恢复可点，结果写在按钮旁", async (outcome, text) => {
  openRows = [F3, MISMATCH_ON_D];
  vi.mocked(getSource).mockResolvedValue(detailAt("src-d", "extracted", "T0"));
  await renderFollowedPanel(async () => outcome);
  fireEvent.click(reparseButtonOf("疑似解析残缺"));
  await act(async () => {
    await submission;
  });
  expectReleased("疑似解析残缺");
  expectReleased("疑似内容错配");
  expect(within(findingByRule("疑似解析残缺")).getByText(text)).toBeInTheDocument();
  expect(library!.followedSourceCount).toBe(0);
});

test("提交前读基准的那段时间里来源库换了工作区：不发重新解析请求，这一行只放开在飞态、不写任何结果", async () => {
  let resolveBaseline: (item: SourceSummary) => void = () => {};
  vi.mocked(getSource).mockReturnValueOnce(new Promise<SourceSummary>((resolve) => { resolveBaseline = resolve; }));
  const submit = vi.fn(async (): Promise<SourceReparseOutcome> => "started");
  await renderFollowedPanel(submit);
  fireEvent.click(reparseButtonOf("疑似解析残缺"));
  expect(within(findingByRule("疑似解析残缺")).getByRole("button", { name: "重新解析中…" })).toBeDisabled();

  // 来源库换到另一个工作区(切库;切账号同理),这张面板还在。
  act(() => {
    library!.commitNotebookSnapshot({
      actorId: "user-a", notebookId: "nb-2", workspaceEpoch: 2,
      page: { items: [], total_count: 0, offset: 0, limit: 50 },
    });
  });
  await act(async () => {
    resolveBaseline(detailAt("src-d", "extracted", "T0"));
    await submission;
  });
  expect(submit).not.toHaveBeenCalled();
  expectReleased("疑似解析残缺");
  const actions = findingByRule("疑似解析残缺").querySelector(".kg-finding-actions");
  expect(actions?.querySelector(".kg-finding-result")).toBeNull();
  expect(library!.followedSourceCount).toBe(0);
});
