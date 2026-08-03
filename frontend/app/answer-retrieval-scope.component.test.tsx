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


// 回执缺席 = 这轮一个范围字段都没提交(所有历史回答都是这样)。凭空渲染一行
// 「本库 0/0」比不渲染更糟。
test("历史回答没有 retrieval_scope 时什么都不渲染", () => {
  const { container } = render(view(answer()));
  expect(container.querySelector(".answer-retrieval-scope")).toBeNull();
  expect(screen.queryByText(/检索范围：/)).toBeNull();
});


// ⚠ 这是**默认状态**:提问框全选时照样发一份显式载荷,后端据此照常回执,所以
// 「回执在场」是常态而不是信号。照单渲染就等于给每一条回答挂一行零信息量的
// 「本库 12/12 · 参考库 2/2」,还把正文下推近 40px。判据必须落在回执内容上。
test("两维都是全量时不渲染回执", () => {
  const { container } = render(view(answer({
    local: { selected: 12, total: 12 },
    bases: [
      { notebook_id: "base-1", name: "LLM Structure & Infra", included: true },
      { notebook_id: "base-2", name: "Analog IC", included: true },
    ],
  })));
  expect(container.querySelector(".answer-retrieval-scope")).toBeNull();
  expect(screen.queryByText(/检索范围：/)).toBeNull();
});


// 只收窄一维也要出回执 —— 事故那一屏正是「本库勾了 1 篇、参考库全留着」。
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


// reasoning 路径下两张卡上下相邻。过去都以「本次依据：」开头 —— 第一张的
// 「本库 12/12」会被读成「12 篇都进了答案」,与第二张的「2 个指定来源」正面矛盾。
test("范围回执与模型点名来源两张卡的措辞分得开", async () => {
  const user = userEvent.setup();
  render(view({
    ...answer({
      local: { selected: 3, total: 12 },
      bases: [{ notebook_id: "base-1", name: "Analog IC", included: true }],
    }),
    intent: {
      objective: "比较命令",
      resolved_question: "比较两个 manual 中的命令",
      intent_type: "compare",
      result_scope: "ranked",
      completeness_required: false,
      entities: [],
      source_refs: ["Manual A"],
      source_scope: {
        mode: "selected",
        sources: [
          {
            source_id: "source-a",
            notebook_id: "notebook-1",
            title: "Manual A",
            source_file_name: "manual-a.pdf",
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
  }));

  const scope = screen.getByText("检索范围：本库 3/12 · 参考库 1/1");
  const basis = screen.getByText("本次依据：1 个指定来源");
  expect(scope).toBeVisible();
  expect(basis).toBeVisible();
  // 两句开头不同 —— 这正是上一版把两张卡读成互相矛盾的地方。
  expect(scope.textContent).not.toContain("本次依据");
  expect(basis.textContent).not.toContain("检索范围");

  await user.click(scope);
  expect(screen.getByText("本笔记本来源 3/12")).toBeVisible();
});
