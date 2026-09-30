// 提问开始时没能及时读出的挂载参考库（E1-2）：回答下方如实说一句它没有参与；
// 健康的回答（字段缺席）一个节点都不多。
import { render, screen } from "@testing-library/react";
import { expect, test } from "vitest";

import { AnswerView } from "../../app/answer-panel";
import type { AskResponse } from "../../app/workspace-model";

function renderAnswer(extra: Partial<AskResponse>) {
  const response: AskResponse = {
    answer_id: "answer-1",
    conversation_id: "conversation-1",
    conclusion: "结论。",
    answer: "结论。",
    grounded: false,
    anchors: [],
    related_knowledge: [],
    citations: [],
    llm_mode: "reasoning",
    ...extra,
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

test("a skipped mounted library is named under the answer", () => {
  renderAnswer({ skipped_libraries: [{ notebook_id: "lib-1", name: "器件手册" }] });
  const notice = screen.getByRole("note");
  expect(notice).toHaveClass("answer-skipped-libraries-notice");
  expect(notice).toHaveTextContent(
    "参考库《器件手册》这次没能及时读取，本次回答没有用到它的资料；再问一次通常就会包含。",
  );
  expect(notice).not.toHaveTextContent("lib-1");
});

test("a healthy answer renders no notice", () => {
  const { container } = renderAnswer({});
  expect(container.querySelector(".answer-skipped-libraries-notice")).toBeNull();
});
