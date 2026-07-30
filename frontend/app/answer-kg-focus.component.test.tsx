import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";

import { AnswerView } from "./answer-panel";
import type { AskResponse } from "./workspace-model";


test("Ask 图谱引用把对象 id 与真实来源 notebook 一起交给跳转处理器", async () => {
  const user = userEvent.setup();
  const onOpenKnowledgeGraph = vi.fn();
  const onOpenSource = vi.fn();
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
      source_id: "base-source-1",
      element_id: "base-element-1",
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
      onOpenSource={onOpenSource}
      notebookId="personal-notebook-1"
      notebookNames={{ "base-notebook-1": "公共基础库" }}
      onBuildScaleIndex={() => undefined}
      buildingScaleIndex={false}
      onSaveMemory={() => undefined}
      memorySaved={false}
    />,
  );

  await user.click(screen.getByRole("button", { name: "[1]" }));
  await user.click(screen.getByRole("button", { name: "查看原文" }));
  expect(onOpenSource).toHaveBeenCalledWith("base-source-1", "base-element-1");

  // 点击引用详情里的动作会按既有交互关闭详情；重新打开后再独立验证图谱跳转。
  await user.click(screen.getByRole("button", { name: "[1]" }));
  await user.click(screen.getByRole("button", { name: "知识图谱" }));

  expect(onOpenKnowledgeGraph).toHaveBeenCalledWith(
    "base-object-1",
    "base-notebook-1",
  );
});


test("本库答案锚点携带 source locator 时可跳到精确原文元素", async () => {
  const user = userEvent.setup();
  const onOpenSource = vi.fn();
  const answer: AskResponse = {
    answer_id: "answer-source-citation",
    conversation_id: "conversation-1",
    conclusion: "清单结论 [k5001]。",
    answer: "清单结论 [k5001]。",
    grounded: true,
    anchors: [{
      key: "k5001", object_id: "element-8", object_type: "element",
      label: "清单结论", name: "清单结论", source_title: "来源论文",
      location_label: "p. 8", source_id: "source-1", element_id: "element-8",
      tier: "personal",
    }],
    related_knowledge: [], citations: [], llm_mode: "reasoning",
  };

  render(
    <AnswerView
      answer={answer}
      feedbackSent=""
      onFeedback={() => undefined}
      onOpenKnowledgeGraph={() => undefined}
      onOpenKnowhowRow={() => undefined}
      onOpenSource={onOpenSource}
      notebookId="personal-notebook-1"
      notebookNames={{}}
      onBuildScaleIndex={() => undefined}
      buildingScaleIndex={false}
      onSaveMemory={() => undefined}
      memorySaved={false}
    />,
  );

  await user.click(screen.getByRole("button", { name: "[1]" }));
  expect(screen.queryByRole("button", { name: "知识图谱" })).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "查看原文" }));
  expect(onOpenSource).toHaveBeenCalledWith("source-1", "element-8");
});


test("文档引用(来源清单行)同样不摆图谱按钮,并跳到该文档本身", async () => {
  // 与上一条同构:文档锚点连 object_id 都没有(文档不是图谱节点),摆一个永远禁用
  // 的「知识图谱」按钮只是噪声。它需要的定位是 source_id,element_id 为空——
  // onOpenSource 因此拿到 undefined 作为元素参数,来源详情打开整份文档。
  const user = userEvent.setup();
  const onOpenSource = vi.fn();
  const answer: AskResponse = {
    answer_id: "answer-document-citation",
    conversation_id: "conversation-1",
    conclusion: "本库只有一篇 [k5001]。",
    answer: "本库只有一篇 [k5001]。",
    grounded: false,
    anchors: [{
      key: "k5001", object_id: "", object_type: "source",
      label: "论文一", name: "论文一", source_title: "论文一",
      location_label: "学术论文", source_id: "src-1", element_id: "",
      tier: "personal",
    }],
    related_knowledge: [], citations: [], llm_mode: "reasoning",
  };

  render(
    <AnswerView
      answer={answer}
      feedbackSent=""
      onFeedback={() => undefined}
      onOpenKnowledgeGraph={() => undefined}
      onOpenKnowhowRow={() => undefined}
      onOpenSource={onOpenSource}
      notebookId="personal-notebook-1"
      notebookNames={{}}
      onBuildScaleIndex={() => undefined}
      buildingScaleIndex={false}
      onSaveMemory={() => undefined}
      memorySaved={false}
    />,
  );

  await user.click(screen.getByRole("button", { name: "[1]" }));
  expect(screen.queryByRole("button", { name: "知识图谱" })).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "查看原文" }));
  expect(onOpenSource).toHaveBeenCalledWith("src-1", undefined);
});
