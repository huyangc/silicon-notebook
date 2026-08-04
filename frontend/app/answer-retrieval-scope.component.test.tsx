import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test } from "vitest";

import { AnswerView } from "./answer-panel";
import type { AskResponse, RetrievalScopeReceipt } from "./workspace-model";


function answer(scope?: RetrievalScopeReceipt): AskResponse {
  return {
    answer_id: scope ? "scoped" : "legacy",
    conversation_id: "conversation-1",
    conclusion: "对应结论见正文。",
    answer: "对应结论见正文。",
    grounded: true,
    anchors: [],
    related_knowledge: [],
    citations: [],
    llm_mode: "grounded",
    ...(scope ? { retrieval_scope: scope } : {}),
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


// 真机事故的那一屏:勾定单篇文章提问,回答却全部来自 84 篇论文的参考库。回执要让
// 这件事在答案上一眼可见,而不是等用户去数引用。
test("范围回执双段披露本库与参考库,并逐个列出参考库的参与情况", async () => {
  const user = userEvent.setup();
  const { container } = render(view(answer({
    local: { selected: 1, total: 5 },
    bases: [
      { notebook_id: "base-1", name: "LLM Structure & Infra", included: false },
      { notebook_id: "base-2", name: "Analog IC", included: true },
    ],
  })));

  const summary = screen.getByText("检索范围：本库 1/5 · 参考库 1/2");
  expect(summary.closest("details")).not.toHaveAttribute("open");

  const receipt = container.querySelector(".answer-retrieval-scope");
  const answerMarkdown = container.querySelector(".answer-markdown");
  expect(receipt).not.toBeNull();
  expect(
    receipt!.compareDocumentPosition(answerMarkdown!)
      & Node.DOCUMENT_POSITION_FOLLOWING,
  ).toBeTruthy();

  await user.click(summary);
  expect(screen.getByText("本笔记本来源 1/5")).toBeVisible();
  expect(screen.getByText("参考库《LLM Structure & Infra》")).toBeVisible();
  expect(screen.getByText("本次未参与检索")).toBeVisible();
  expect(screen.getByText("参考库《Analog IC》")).toBeVisible();
  expect(screen.getByText("已参与检索")).toBeVisible();
  // 未参与的那一行要看得出来是被排除的,不能和已参与的行长得一样。
  expect(
    screen.getByText("参考库《LLM Structure & Infra》").className,
  ).toContain("scope-excluded");
  expect(screen.getByText("参考库《Analog IC》").className).not.toContain("scope-excluded");
});


test("没挂参考库时回执只报本库一段", () => {
  render(view(answer({ local: { selected: 2, total: 9 }, bases: [] })));
  expect(screen.getByText("检索范围：本库 2/9")).toBeVisible();
});


// 回执缺席 = 后端判定这轮两维都没收窄(浏览器每次都发显式范围,全选也不例外,所以
// 「提交了范围」不是信号),或那条回答早于本特性。凭空渲染一行「本库 0/0」比不渲染更糟。
test("没有 retrieval_scope 时什么都不渲染", () => {
  const { container } = render(view(answer()));
  expect(container.querySelector(".answer-retrieval-scope")).toBeNull();
  expect(screen.queryByText(/检索范围：/)).toBeNull();
});


// 只收窄一维也要出回执 —— 事故那一屏正是「本库勾了 1 篇、参考库全留着」。「另一维
// 是全量」这件事本身就是最该被读到的那一行,不能因为它没被收窄就藏起来。
test("只收窄本库一维时仍出回执,并如实报出全量的参考库", () => {
  render(view(answer({
    local: { selected: 1, total: 12 },
    bases: [{ notebook_id: "base-1", name: "Analog IC", included: true }],
  })));
  expect(screen.getByText("检索范围：本库 1/12 · 参考库 1/1")).toBeVisible();
});


test("只收窄参考库一维时仍出回执", () => {
  render(view(answer({
    local: { selected: 12, total: 12 },
    bases: [
      { notebook_id: "base-1", name: "Analog IC", included: false },
      { notebook_id: "base-2", name: "LLM Structure & Infra", included: true },
    ],
  })));
  expect(screen.getByText("检索范围：本库 12/12 · 参考库 1/2")).toBeVisible();
});


// ⚠「算不算收窄」只有后端一份判据(冻结那一刻的实时全集),回执里的 total 却刻意用
// 可能滞后的缓存计数 —— 对展示是可接受的陈旧,对闸不是。所以浏览器绝不能按回执数字
// 重算一遍:并发上传一篇就会让一次真收窄在这里被算成 12/12 而整行消失。
test("后端下发的回执一律渲染,不按数字自行重算是否收窄", () => {
  render(view(answer({
    local: { selected: 12, total: 12 },
    bases: [{ notebook_id: "base-1", name: "Analog IC", included: true }],
  })));
  expect(screen.getByText("检索范围：本库 12/12 · 参考库 1/1")).toBeVisible();
});


// 库名是授权时刻的持久化快照。空名字(旧回执/异常数据)不能渲染成「参考库《》」。
test("库名缺失时退回可读占位,不留空书名号", () => {
  render(view(answer({
    local: { selected: 1, total: 4 },
    bases: [{ notebook_id: "base-1", name: "", included: true }],
  })));
  expect(screen.getByText("参考库《未命名》")).toBeInTheDocument();
});
