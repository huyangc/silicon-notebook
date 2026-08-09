// 按节合成答案的标题:必须真的落在 `.chat-answer .answer-markdown` 这条链上。
//
// 这是 answer-heading-scope-guard.test.mjs 的另一半。那个守卫钉住「字阶不许写成裸
// `.answer-markdown h*`」(否则泄漏进深度报告);这里钉住收窄之后**问答答案自己还吃得
// 到**——两条断言缺一，收窄就可能变成"顺手把新字阶整个关掉"。
//
// jsdom 不实现特异性级联，所以这里量的是 DOM 祖先链（那是选择器能否命中的前提），
// 不是 getComputedStyle。
import { render, screen } from "@testing-library/react";
import { expect, test } from "vitest";

import { AnswerView } from "../../app/answer-panel";
import type { AskResponse } from "../../app/workspace-model";


const sectionedAnswer: AskResponse = {
  answer_id: "answer-sectioned",
  conversation_id: "conversation-1",
  conclusion: "两节答案。",
  // 后端 outline_synthesis.outline_answer_text 的产出形状:顶层节 ## / 子节 ###。
  answer: "## 甲部分\n\n甲部分正文。\n\n### 子节\n\n子节正文。\n\n## 乙部分\n\n乙部分正文。",
  grounded: true,
  anchors: [],
  related_knowledge: [],
  citations: [],
  llm_mode: "grounded",
};


function view() {
  return (
    <AnswerView
      answer={sectionedAnswer}
      feedbackSent=""
      onFeedback={() => undefined}
      onOpenKnowledgeGraph={() => undefined}
      onOpenKnowhowRow={() => undefined}
      notebookId="nb-1"
      notebookNames={{}}
      onBuildScaleIndex={() => undefined}
      buildingScaleIndex={false}
      scaleIndexStatus={null}
      onSaveMemory={() => undefined}
      memorySaved={false}
    />
  );
}


test("按节合成的 ## / ### 标题渲染成 h2/h3，并落在问答自己的容器链下", () => {
  render(view());

  const h2s = screen.getAllByRole("heading", { level: 2 });
  const h3s = screen.getAllByRole("heading", { level: 3 });
  expect(h2s.map((node) => node.textContent)).toEqual(["甲部分", "乙部分"]);
  expect(h3s.map((node) => node.textContent)).toEqual(["子节"]);

  for (const heading of [...h2s, ...h3s]) {
    // 共用渲染管线容器（globals.css 的字阶挂在它上面）。
    expect(heading.closest(".answer-markdown")).not.toBeNull();
    // 问答自己的容器：收窄后的选择器靠它把深度报告排除在外。
    expect(heading.closest(".chat-answer")).not.toBeNull();
  }
});
