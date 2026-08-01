import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test } from "vitest";

import { AnswerView } from "./answer-panel";
import type { AskResponse } from "./workspace-model";


function answer(sourceScoped: boolean): AskResponse {
  return {
    answer_id: sourceScoped ? "scoped" : "legacy",
    conversation_id: "conversation-1",
    conclusion: "对应命令见正文。",
    answer: "对应命令见正文。",
    grounded: true,
    anchors: [],
    related_knowledge: [],
    citations: [],
    llm_mode: "grounded",
    ...(sourceScoped ? {
      intent: {
        objective: "比较命令",
        resolved_question: "比较两个 manual 中的命令",
        intent_type: "compare",
        result_scope: "ranked",
        completeness_required: false,
        entities: [],
        source_refs: ["Manual A", "Manual B"],
        source_scope: {
          mode: "selected",
          sources: [
            {
              source_id: "source-a",
              notebook_id: "notebook-1",
              title: "Manual A",
              source_file_name: "manual-a.pdf",
            },
            {
              source_id: "source-b",
              notebook_id: "notebook-1",
              title: "Manual B.pdf",
              source_file_name: "Manual B.pdf",
            },
          ],
        },
        mandatory_topics: [],
        comparison_axes: [],
        constraints: [],
        excluded_topics: [],
        expected_output: "",
        assumptions: [],
        ambiguities: [],
        confidence: 1,
        needs_clarification: false,
        confirmed: true,
      },
    } : {}),
  };
}


function view(response: AskResponse) {
  return (
    <AnswerView
      answer={response}
      feedbackSent=""
      onFeedback={() => undefined}
      onOpenKnowledgeGraph={() => undefined}
      onOpenKnowhowRow={() => undefined}
      notebookId="notebook-1"
      notebookNames={{}}
      onBuildScaleIndex={() => undefined}
      buildingScaleIndex={false}
      scaleIndexStatus={null}
      onSaveMemory={() => undefined}
      memorySaved={false}
    />
  );
}


test("reopened scoped answer shows its persisted source scope before the body", async () => {
  const user = userEvent.setup();
  const { container } = render(view(answer(true)));
  const summary = screen.getByText("本次依据：2 个指定来源");
  const sourceScope = container.querySelector(".answer-source-scope");
  const answerMarkdown = container.querySelector(".answer-markdown");
  expect(summary.closest("details")).not.toHaveAttribute("open");
  expect(sourceScope).not.toBeNull();
  expect(answerMarkdown).not.toBeNull();
  expect(
    sourceScope!.compareDocumentPosition(answerMarkdown!) &
      Node.DOCUMENT_POSITION_FOLLOWING,
  ).toBeTruthy();

  await user.click(summary);
  expect(screen.getByText("Manual A")).toBeVisible();
  expect(screen.getByText("原始文件：manual-a.pdf")).toBeVisible();
  expect(screen.queryByText("原始文件：Manual B.pdf")).toBeNull();
});


test("legacy answers without source_scope keep the historical rendering", () => {
  render(view(answer(false)));
  expect(screen.queryByText(/本次依据：/)).toBeNull();
});
