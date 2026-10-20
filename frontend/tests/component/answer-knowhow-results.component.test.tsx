import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";

import { AnswerView } from "../../app/answer-panel";
import type { AskResponse, KnowhowResultSet, SpreadsheetAnalysisResult } from "../../app/workspace-model";


function answerWithRows(totalRows: number, complete = true): AskResponse {
  const loadedRows = complete ? totalRows : 25;
  return {
    answer_id: "answer-result-set",
    conversation_id: "conversation-1",
    conclusion: "已读取表格。",
    answer: "已读取表格。",
    grounded: true,
    anchors: [],
    related_knowledge: [],
    citations: [],
    llm_mode: "structured",
    result_coverage: {
      known_tables: 1,
      selected_tables: 1,
      known_total_rows: totalRows,
      scanned_rows: complete ? totalRows : 25,
      returned_rows: complete ? totalRows : 25,
      complete,
      truncated_reason: complete ? null : "row_limit",
      overflow_semantics: complete ? "" : "explicit_partial",
      synthesis_rows: 0,
      synthesis_complete: null,
    },
    result_sets: [{
      kind: "knowhow",
      table_id: "table-1",
      title: "方法清单",
      columns: [{ id: "method", name: "方法" }],
      rows: Array.from({ length: loadedRows }, (_, index) => ({
        row_id: `row-${index}`,
        position: index,
        cells: { method: `方法 ${index + 1}` },
      })),
      coverage: {
        total_rows: totalRows,
        scanned_rows: complete ? totalRows : 25,
        returned_rows: complete ? totalRows : 25,
        complete,
        truncated_reason: complete ? null : "row_limit",
        overflow_semantics: complete ? "" : "explicit_partial",
      },
    }],
  };
}


function renderAnswer(answer: AskResponse, onOpenKnowhowRow = vi.fn(), onOpenSource = vi.fn()) {
  render(
    <AnswerView
      answer={answer}
      feedbackSent=""
      onFeedback={() => undefined}
      onOpenKnowledgeGraph={() => undefined}
      onOpenKnowhowRow={onOpenKnowhowRow}
      onOpenSource={onOpenSource}
      notebookId="nb-1"
      notebookNames={{}}
      onBuildScaleIndex={() => undefined}
      buildingScaleIndex={false}
      onSaveMemory={() => undefined}
      memorySaved={false}
    />,
  );
  return onOpenKnowhowRow;
}


test("Excel 分析结果显示计算回执、公式告警并可定位原来源", async () => {
  const user = userEvent.setup();
  const answer = answerWithRows(1);
  const spreadsheet: SpreadsheetAnalysisResult = {
    kind: "spreadsheet",
    source_id: "src-sheet",
    source_title: "销售数据",
    source_file_name: "sales.xlsx",
    sheet: "Sales",
    range: "A1:C40",
    operation: "aggregate",
    columns: [{ id: "Region", name: "Region" }, { id: "Amount · sum", name: "Amount · sum" }],
    rows: [{
      position: 1,
      cells: { Region: "East", "Amount · sum": "50" },
      citation: {
        label: "销售数据 · Sales!A1:C40",
        source_id: "src-sheet",
        element_id: "el-sheet-2",
        location_label: "Sales!A1:C40",
        quoted_span: "电子表格确定性分析结果",
      },
    }],
    coverage: {
      total_rows: 1,
      scanned_rows: 39,
      returned_rows: 1,
      complete: true,
      truncated_reason: null,
      overflow_semantics: "",
    },
    formula_cells: 4,
    unresolved_formula_cells: 1,
    warnings: ["部分公式没有缓存结果；相关单元格按空值处理。"],
  };
  answer.result_sets = [spreadsheet];
  answer.result_coverage = undefined;
  const openSource = vi.fn();
  renderAnswer(answer, vi.fn(), openSource);

  expect(screen.getByText(/Excel 分析 · 销售数据/)).toBeInTheDocument();
  expect(screen.getByText(/已扫描 39 行；公式单元格 4 个，其中 1 个没有可用缓存值/)).toBeInTheDocument();
  expect(screen.getByText("50")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "查看来源" }));
  expect(openSource).toHaveBeenCalledWith("src-sheet", "el-sheet-2");
});


test("a workbook receipt from a mounted library carries its library and renders the same", () => {
  // The wire's optional `notebook_id` / `tier` on the result (absent for the
  // current library at the default tier) are part of the typed mirror.
  const receipt: SpreadsheetAnalysisResult = {
    kind: "spreadsheet",
    source_id: "src-sheet",
    source_title: "销售数据",
    sheet: "Sales",
    range: "A1:B3",
    operation: "filter",
    columns: [{ id: "Region", name: "Region" }],
    rows: [],
    coverage: {
      total_rows: 0, scanned_rows: 2, returned_rows: 0, complete: true,
      truncated_reason: null, overflow_semantics: "",
    },
    formula_cells: 0,
    unresolved_formula_cells: 0,
    notebook_id: "nb-mounted",
    tier: "base",
  };
  const answer = answerWithRows(1);
  answer.result_sets = [receipt];
  answer.result_coverage = undefined;
  renderAnswer(answer);
  expect(screen.getByText(/Excel 分析 · 销售数据/)).toBeInTheDocument();
  expect(receipt.notebook_id).toBe("nb-mounted");
  expect(receipt.tier).toBe("base");
  // No name known for that library: no badge, and never the bare id.
  expect(screen.queryByText(/来自「/)).not.toBeInTheDocument();
  expect(screen.queryByText(/nb-mounted/)).not.toBeInTheDocument();
});


function workbookFrom(notebookId: string, tier = "personal"): SpreadsheetAnalysisResult {
  return {
    kind: "spreadsheet",
    source_id: `src-${notebookId}`,
    source_title: "报价单",
    sheet: "Quotes",
    range: "A1:B3",
    operation: "top",
    columns: [{ id: "Price", name: "Price" }],
    rows: [{ position: 1, cells: { Price: "5" } }],
    coverage: {
      total_rows: 1, scanned_rows: 2, returned_rows: 1, complete: true,
      truncated_reason: null, overflow_semantics: "",
    },
    formula_cells: 0,
    unresolved_formula_cells: 0,
    notebook_id: notebookId,
    tier,
  };
}


function renderWorkbook(
  receipt: SpreadsheetAnalysisResult,
  notebookId: string | null,
  notebookNames: Record<string, string>,
) {
  const answer = answerWithRows(1);
  answer.result_sets = [receipt];
  answer.result_coverage = undefined;
  render(
    <AnswerView
      answer={answer}
      feedbackSent=""
      onFeedback={() => undefined}
      notebookId={notebookId}
      notebookNames={notebookNames}
      buildingScaleIndex={false}
      memorySaved={false}
    />,
  );
}


test("a global answer's workbook card names the library the table came from", () => {
  // Global Ask has no active notebook, so every workbook is from "another"
  // library; the title line says which, in the citation card's own wording.
  renderWorkbook(workbookFrom("nb-b", "base"), null, { "nb-a": "A 库", "nb-b": "B 库" });
  const heading = screen.getByText(/Excel 分析 · 报价单/).closest("div");
  expect(heading).toHaveTextContent("来自「B 库」（公共知识库）");
  expect(heading).not.toHaveTextContent("A 库");
});


test("a mounted library's workbook card names it; the current library's does not", () => {
  renderWorkbook(workbookFrom("nb-mounted"), "nb-1", { "nb-mounted": "参考库甲", "nb-1": "本库" });
  expect(screen.getByText(/Excel 分析 · 报价单/).closest("div"))
    .toHaveTextContent("来自「参考库甲」（个人知识库）");
});


function renderWorkbooks(
  receipts: SpreadsheetAnalysisResult[],
  notebookId: string | null,
  notebookNames: Record<string, string>,
) {
  const answer = answerWithRows(1);
  answer.result_sets = receipts;
  answer.result_coverage = undefined;
  render(
    <AnswerView
      answer={answer}
      feedbackSent=""
      onFeedback={() => undefined}
      notebookId={notebookId}
      notebookNames={notebookNames}
      buildingScaleIndex={false}
      memorySaved={false}
    />,
  );
}


test("a multi-table plan renders one card per table, in plan order, each naming its own library", () => {
  // Global Ask comparing a quote table in library A with one in library B:
  // the same workbook title in both, told apart only by each card's badge.
  renderWorkbooks(
    [workbookFrom("nb-b", "base"), workbookFrom("nb-a")],
    null,
    { "nb-a": "A 库", "nb-b": "B 库" },
  );
  const cards = screen.getAllByRole("region", { name: "Excel 分析：报价单" });
  expect(cards).toHaveLength(2);
  expect(cards[0]).toHaveTextContent("来自「B 库」（公共知识库）");
  expect(cards[0]).not.toHaveTextContent("A 库");
  expect(cards[1]).toHaveTextContent("来自「A 库」（个人知识库）");
  expect(cards[1]).not.toHaveTextContent("B 库");
});


test("two tables of the current library render as two badge-free cards, the cap disclosure on the last", () => {
  const disclosure = "本次规划了 4 项表格分析，超过单次最多 3 项的上限，按规划顺序只做了前 3 项；未做：丁 · Quotes（数据概况）。";
  const first = { ...workbookFrom("nb-1"), source_id: "src-1", source_title: "甲报价表" };
  const second = {
    ...workbookFrom("nb-1"), source_id: "src-2", source_title: "乙报价表", warnings: [disclosure],
  };
  renderWorkbooks([first, second], "nb-1", { "nb-1": "本库" });
  const cards = screen.getAllByRole("region", { name: /^Excel 分析：/ });
  expect(cards.map((card) => card.getAttribute("aria-label"))).toEqual([
    "Excel 分析：甲报价表", "Excel 分析：乙报价表",
  ]);
  expect(screen.queryByText(/来自「/)).not.toBeInTheDocument();
  expect(cards[0]).not.toHaveTextContent(disclosure);
  expect(cards[1]).toHaveTextContent(disclosure);
});


test("two analyses of one sheet render as two cards, each naming its parameters", () => {
  // Two rankings of the same sheet: source_id, sheet and operation are all
  // equal, so only the parameter reading tells the cards apart.
  const byPrice = { ...workbookFrom("nb-1"), analysis_label: "按 Price 降序" };
  const byQty = {
    ...workbookFrom("nb-1"), analysis_label: "按 Qty 降序",
    rows: [{ position: 1, cells: { Price: "1" } }],
  };
  // Same source_id/sheet/operation: a key built from those alone would be a
  // duplicate, which React reports through console.error.
  const consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);
  renderWorkbooks([byPrice, byQty], "nb-1", { "nb-1": "本库" });
  const keyWarnings = consoleError.mock.calls.filter((call) =>
    call.some((part) => typeof part === "string" && part.includes("same key")),
  );
  consoleError.mockRestore();
  expect(keyWarnings).toEqual([]);
  const cards = screen.getAllByRole("region", { name: "Excel 分析：报价单" });
  expect(cards).toHaveLength(2);
  expect(cards[0]).toHaveTextContent("Quotes!A1:B3 · 排序分析（按 Price 降序）");
  expect(cards[1]).toHaveTextContent("Quotes!A1:B3 · 排序分析（按 Qty 降序）");
  expect(cards[1]).toHaveTextContent("1");
});


test("a sheet analysed once shows no parameter reading", () => {
  renderWorkbooks([workbookFrom("nb-1")], "nb-1", { "nb-1": "本库" });
  expect(screen.getByText("Quotes!A1:B3 · 排序分析")).toBeInTheDocument();
});


test("a workbook card from the current library carries no library badge", () => {
  renderWorkbook(workbookFrom("nb-1"), "nb-1", { "nb-1": "本库" });
  expect(screen.getByText(/Excel 分析 · 报价单/)).toBeInTheDocument();
  expect(screen.queryByText(/来自「/)).not.toBeInTheDocument();
});


test("complete Knowhow result shows coverage, initially caps at 20 rows, then expands loaded rows", async () => {
  const user = userEvent.setup();
  const openRow = renderAnswer(answerWithRows(25));

  expect(screen.getByText("完整 25/25")).toBeInTheDocument();
  expect(screen.getByText("方法 20")).toBeInTheDocument();
  expect(screen.queryByText("方法 21")).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "展开全部已加载的 25 行" }));
  expect(screen.getByText("方法 25")).toBeInTheDocument();
  await user.click(screen.getAllByRole("button", { name: "在表格中查看" })[24]);
  expect(openRow).toHaveBeenCalledWith("table-1", "row-24");
});


test("partial Knowhow result never claims completeness and exposes the bound reason", () => {
  renderAnswer(answerWithRows(100, false));
  expect(screen.getByText("部分 25/100")).toBeInTheDocument();
  // truncated_reason 走界面词映射(P2 修复),不再原样吐 row_limit 这个内部代号。
  expect(screen.getByText("已明确标注为部分结果（已达本轮可读取的行数上限）")).toBeInTheDocument();
  expect(screen.getByText(/未扫描部分不会被表述为“全部”/)).toBeInTheDocument();
});


test("payload-limited badge reports returned rows separately from scanned rows", () => {
  const answer = answerWithRows(100, false);
  // result_sets 是 kind 判别 union(PR-2 T5/T6);answerWithRows 只构造 kind="knowhow"
  // 行,这里窄化类型以继续直接改 .rows/.coverage(与本文件其余用例一致的写法)。
  const result = answer.result_sets![0] as KnowhowResultSet;
  result.rows = result.rows.slice(0, 5);
  result.coverage.returned_rows = 5;
  result.coverage.scanned_rows = 25;
  result.coverage.truncated_reason = "payload_limit";

  renderAnswer(answer);
  expect(screen.getByText("部分 5/100")).toBeInTheDocument();
  expect(screen.getAllByText(/已扫描 25 行/)).toHaveLength(2);
});


test("未知 truncated_reason 兜底显示「部分结果」,不吐英文内部代号", () => {
  const answer = answerWithRows(100, false);
  const result = answer.result_sets![0] as KnowhowResultSet;
  result.coverage.truncated_reason = "some_future_reason_code";

  renderAnswer(answer);
  expect(screen.getByText("已明确标注为部分结果（部分结果）")).toBeInTheDocument();
  expect(screen.queryByText(/some_future_reason_code/)).not.toBeInTheDocument();
});


// 技术债 C2:「部分结果」标注的悬停提示此前是 overflow_semantics 的原值 explicit_partial。
test("部分结果标注的悬停提示是中文，不吐 overflow_semantics 代号", () => {
  const answer = answerWithRows(100, false);
  const result = answer.result_sets![0] as KnowhowResultSet;
  result.coverage.truncated_reason = "row_limit";

  renderAnswer(answer);
  const reason = screen.getByText("已明确标注为部分结果（已达本轮可读取的行数上限）");
  expect(reason).toHaveAttribute("title", "结果未全部列出");
});


test("batch coverage distinguishes omitted tables from complete selected tables", () => {
  const answer = answerWithRows(10, true);
  answer.result_coverage = {
    known_tables: 10,
    selected_tables: 8,
    known_total_rows: 10,
    scanned_rows: 8,
    returned_rows: 8,
    complete: false,
    truncated_reason: "table_limit",
    overflow_semantics: "explicit_partial",
    synthesis_rows: 8,
    synthesis_complete: false,
  };

  renderAnswer(answer);
  expect(screen.getByText("总体部分")).toBeInTheDocument();
  expect(screen.getByText("表 8/10；行 8/10")).toBeInTheDocument();
  expect(screen.getByText("分析覆盖 8/10（部分）")).toBeInTheDocument();
  // truncated_reason 走界面词映射(P2 修复),不再原样吐 table_limit 这个内部代号。
  expect(screen.getByText("截断原因：已达本轮可读取的表数上限")).toBeInTheDocument();
  expect(screen.getByText("完整 10/10")).toBeInTheDocument();
});
