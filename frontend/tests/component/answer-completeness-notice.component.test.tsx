import { render, screen } from "@testing-library/react";
import { expect, test } from "vitest";

import { AnswerView } from "../../app/answer-panel";
import type { AskResponse } from "../../app/workspace-model";

function renderAnswer(body: string) {
  const notice = "本次回答未验证完整性，不能视为全部结果。";
  const response: AskResponse = {
    answer_id: "answer-1",
    conversation_id: "conversation-1",
    conclusion: `${body}\n\n${notice}`,
    answer: body ? `${body}\n\n> ${notice}` : notice,
    completeness_notice: notice,
    grounded: false,
    anchors: [],
    related_knowledge: [],
    citations: [],
    llm_mode: "ungrounded",
  };
  return render(
    <AnswerView
      answer={response}
      feedbackSent=""
      notebookId="notebook-1"
      notebookNames={{}}
      buildingScaleIndex={false}
      scaleIndexStatus={null}
      memorySaved={false}
    />,
  );
}

test("完整性提示在模型留下未闭合代码围栏时仍显示在正文外", () => {
  const { container } = renderAnswer("示例\n\n```text\n未闭合");
  const code = container.querySelector(".answer-markdown pre code");
  const notice = screen.getByText("本次回答未验证完整性，不能视为全部结果。");

  expect(code).not.toBeNull();
  expect(code).toHaveTextContent("未闭合");
  expect(code).not.toHaveTextContent("本次回答未验证完整性");
  expect(notice).toHaveClass("answer-completeness-notice");
  expect(code!.compareDocumentPosition(notice) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
});

test("模型没有生成正文时完整性提示只显示一次", () => {
  const { container } = renderAnswer("");
  expect(screen.getAllByText("本次回答未验证完整性，不能视为全部结果。")).toHaveLength(1);
  expect(container.querySelector(".answer-completeness-notice")).toBeVisible();
});
