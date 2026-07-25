import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";

import { AnswerView } from "./answer-panel";
import type { AskResponse } from "./workspace-model";


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


function renderAnswer(answer: AskResponse, onOpenKnowhowRow = vi.fn()) {
  render(
    <AnswerView
      answer={answer}
      feedbackSent=""
      onFeedback={() => undefined}
      onOpenKnowledgeGraph={() => undefined}
      onOpenKnowhowRow={onOpenKnowhowRow}
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
  expect(screen.getByText("已明确标注为部分结果（row_limit）")).toBeInTheDocument();
  expect(screen.getByText(/未扫描部分不会被表述为“全部”/)).toBeInTheDocument();
});


test("payload-limited badge reports returned rows separately from scanned rows", () => {
  const answer = answerWithRows(100, false);
  const result = answer.result_sets![0];
  result.rows = result.rows.slice(0, 5);
  result.coverage.returned_rows = 5;
  result.coverage.scanned_rows = 25;
  result.coverage.truncated_reason = "payload_limit";

  renderAnswer(answer);
  expect(screen.getByText("部分 5/100")).toBeInTheDocument();
  expect(screen.getAllByText(/已扫描 25 行/)).toHaveLength(2);
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
  expect(screen.getByText("截断原因：table_limit")).toBeInTheDocument();
  expect(screen.getByText("完整 10/10")).toBeInTheDocument();
});
