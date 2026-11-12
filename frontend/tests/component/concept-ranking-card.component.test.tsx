import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";
import { ConceptRankingCard } from "../../app/concept-ranking-card";
import type { ConceptRankingCoverage, ConceptRankingResult, ConceptRankingRow } from "../../app/workspace-model";

function coverage(overrides: Partial<ConceptRankingCoverage> = {}): ConceptRankingCoverage {
  return {
    notebook_id: "nb", notebook_name: "活动库", status: "ok", reason: "", path: "scope_full",
    scoped: false, drifted: false, scope_sources: 12, candidates_lexical_only: false,
    stale_counts: false, seq_behind: 0, recount_capped: false, recount_skipped: 0,
    unnamed_dropped: 0, duplicate_import_pairs: 0, exact: true, skipped: "", next_cursor: "",
    ...overrides,
  };
}
function row(name = "注意力"): ConceptRankingRow {
  return { name, source_count: 3, object_count: 8, same_name_group: "g2", notebook_id: "nb", tier: "personal",
    citations: [{ label: "论文", source_id: "source", element_id: "element", location_label: "第 2 页", quoted_span: "原文摘录" }] };
}
function result(overrides: Partial<ConceptRankingResult> = {}): ConceptRankingResult {
  return { kind: "concept_ranking", query: "模型", scope: "all", sections: [{ coverage: coverage(), rows: [row()], notice: "" }], ...overrides };
}

test("renders the ranking headings, backend counts, same-name group and source action", async () => {
  const onOpenSource = vi.fn();
  render(<ConceptRankingCard resultSet={result()} notebookNames={{}} onOpenSource={onOpenSource} />);
  expect(screen.getByRole("region", { name: "概念排行" })).toBeInTheDocument();
  expect(screen.getByText("主题：模型")).toBeInTheDocument();
  expect(screen.getAllByRole("columnheader").map((cell) => cell.textContent)).toEqual(["概念", "来源数", "提及条数"]);
  expect(screen.getByText("注意力")).toBeInTheDocument();
  expect(screen.getByRole("cell", { name: "3" })).toBeInTheDocument();
  expect(screen.getByRole("cell", { name: "8" })).toBeInTheDocument();
  expect(screen.getByText("可能与第 2 组同名概念重复")).toBeInTheDocument();
  expect(screen.getByText("按提及它的来源篇数统计，范围：12 篇来源")).toBeInTheDocument();
  await userEvent.setup().click(screen.getByRole("button", { name: "查看原文" }));
  expect(onOpenSource).toHaveBeenCalledWith("source", "element");
  expect(screen.getByRole("table").parentElement).toHaveClass("answer-table-wrap");
});

test("multi-notebook sections prefer current names and failures have only their backend notice", () => {
  const sections = [
    { coverage: coverage(), rows: [row()], notice: "" },
    { coverage: coverage({ notebook_id: "ref", notebook_name: "参考资料", status: "unavailable", exact: false }), rows: [], notice: "没能按提及次数排行：统计时出错。" },
  ];
  render(<ConceptRankingCard resultSet={result({ sections })} notebookNames={{ nb: "当前库名" }} onOpenSource={vi.fn()} />);
  expect(screen.getByRole("heading", { name: "当前库名" })).toBeInTheDocument();
  const failedSection = screen.getByRole("heading", { name: "参考资料" }).closest("section")!;
  expect(within(failedSection).getByText(sections[1].notice)).toBeInTheDocument();
  expect(within(failedSection).queryByRole("table")).not.toBeInTheDocument();
  expect(within(failedSection).queryByRole("button")).not.toBeInTheDocument();
});

test("non-exact coverage lists each applicable disclosure", () => {
  render(<ConceptRankingCard resultSet={result({ sections: [{ coverage: coverage({
    exact: false, drifted: true, candidates_lexical_only: true, stale_counts: true,
    recount_skipped: 2, unnamed_dropped: 1,
  }), rows: [row()], notice: "" }] })} notebookNames={{}} />);
  for (const note of ["统计期间来源有变化", "只按字面匹配找候选", "统计建于较早的图谱状态", "部分候选未计入", "部分概念取不到名称"]) {
    expect(screen.getByText(note)).toBeInTheDocument();
  }
  expect(screen.queryByRole("button")).not.toBeInTheDocument();
});

test("already delivered rows are never repaginated or discarded after a failed page", () => {
  const rows = Array.from({ length: 40 }, (_, index) => ({ ...row(`概念 ${index + 1}`), citations: [] }));
  render(<ConceptRankingCard resultSet={result({ sections: [{ coverage: coverage({ status: "unavailable" }), rows,
    notice: "最新一页读取失败" }] })} notebookNames={{}} />);
  expect(screen.getAllByRole("row")).toHaveLength(41);
  expect(screen.getByText("概念 40")).toBeInTheDocument();
  expect(screen.getByText("按提及它的来源篇数统计，范围：12 篇来源")).toBeInTheDocument();
  expect(screen.queryByText("最新一页读取失败")).not.toBeInTheDocument();
});

test("25 successful notebooks retain every delivered section and row", () => {
  const sections = Array.from({ length: 25 }, (_, index) => ({
    coverage: coverage({ notebook_id: `nb-${index}`, notebook_name: `库 ${index}` }), rows: [row(`概念 ${index}`)], notice: "",
  }));
  render(<ConceptRankingCard resultSet={result({ sections })} notebookNames={{}} />);
  expect(screen.getAllByRole("table")).toHaveLength(25);
  expect(screen.getAllByRole("row")).toHaveLength(50);
});

test("an empty successful section is an empty result and missing query has no topic label", () => {
  render(<ConceptRankingCard resultSet={result({ query: "", sections: [{ coverage: coverage(), rows: [], notice: "" }] })}
    notebookNames={{}} />);
  expect(screen.getByText("没有符合条件的概念")).toBeInTheDocument();
  expect(screen.queryByText(/主题：/)).not.toBeInTheDocument();
  expect(screen.queryByRole("button")).not.toBeInTheDocument();
});

test("unverified citations retain their excerpts but cannot open missing sources", () => {
  const rankedRow = row();
  rankedRow.citations[0].verification = "source_gone";
  render(<ConceptRankingCard resultSet={result({ sections: [{ coverage: coverage(), rows: [rankedRow], notice: "" }] })}
    notebookNames={{}} onOpenSource={vi.fn()} />);
  expect(screen.getByText("资料已删除")).toBeInTheDocument();
  expect(screen.getByText("原文摘录")).toBeInTheDocument();
  expect(screen.queryByRole("button")).not.toBeInTheDocument();
});

test("query coverage discloses its candidate scope even without another warning", () => {
  render(<ConceptRankingCard resultSet={result({ sections: [{ coverage: coverage({
    exact: false, path: "query",
  }), rows: [row()], notice: "" }] })} notebookNames={{}} />);
  expect(screen.getByText("只在与主题最相关的概念里排行")).toBeInTheDocument();
  expect(screen.queryByText(/范围：12 篇来源/)).not.toBeInTheDocument();
});

test.each([true, false])("duplicate-import disclosure survives exact=%s", (exact) => {
  render(<ConceptRankingCard resultSet={result({ sections: [{ coverage: coverage({
    exact, duplicate_import_pairs: 2,
  }), rows: [row()], notice: "" }] })} notebookNames={{}} />);
  expect(screen.getByText("有疑似重复导入的来源，计数可能偏高")).toBeInTheDocument();
});
