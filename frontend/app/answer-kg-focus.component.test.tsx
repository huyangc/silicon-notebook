import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";

import { AnswerView } from "./answer-panel";
import type { AskResponse } from "./workspace-model";


test("Ask 图谱引用把对象 id 与真实来源 notebook 一起交给跳转处理器", async () => {
  const user = userEvent.setup();
  const onOpenKnowledgeGraph = vi.fn();
  const answer: AskResponse = {
    answer_id: "answer-base-citation",
    conversation_id: "conversation-1",
    conclusion: "来自公共库的结论 [k1]。",
    answer: "来自公共库的结论 [k1]。",
    grounded: true,
    anchors: [{
      key: "k1",
      object_id: "base-object-1",
      object_type: "claim",
      label: "公共库结论",
      name: "公共库结论",
      source_title: "公共来源",
      location_label: "第 1 节",
      notebook_id: "base-notebook-1",
      tier: "base",
    }],
    related_knowledge: [],
    citations: [],
    llm_mode: "deterministic",
  };

  render(
    <AnswerView
      answer={answer}
      feedbackSent=""
      onFeedback={() => undefined}
      onOpenKnowledgeGraph={onOpenKnowledgeGraph}
      onOpenKnowhowRow={() => undefined}
      notebookId="personal-notebook-1"
      notebookNames={{ "base-notebook-1": "公共基础库" }}
      onBuildScaleIndex={() => undefined}
      buildingScaleIndex={false}
      onSaveMemory={() => undefined}
      memorySaved={false}
    />,
  );

  await user.click(screen.getByRole("button", { name: "[1]" }));
  await user.click(screen.getByRole("button", { name: "知识图谱" }));

  expect(onOpenKnowledgeGraph).toHaveBeenCalledWith(
    "base-object-1",
    "base-notebook-1",
  );
});
