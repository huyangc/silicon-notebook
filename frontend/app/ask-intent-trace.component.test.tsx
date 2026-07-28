import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test } from "vitest";

import { ReasoningTracePanel } from "./answer-panel";
import {
  intentClarifyStep,
  intentConfirmedStep,
  intentUnderstandingStep,
  intentUnderstoodStep,
} from "./ask-intent-trace";
import type { QueryIntentContract } from "./ask-intent-model";


afterEach(cleanup);


const contract: QueryIntentContract = {
  objective: "分析电荷泵 PLL 失锁",
  resolved_question: "电荷泵 PLL 在 PVT 角落下为什么会失锁",
  intent_type: "diagnose",
  result_scope: "ranked",
  completeness_required: false,
  entities: ["电荷泵 PLL"],
  mandatory_topics: [{
    id: "intent-1", title: "失锁", question: "为什么会失锁？", retrieval_queries: [],
  }],
  comparison_axes: [],
  constraints: [],
  excluded_topics: [],
  expected_output: "",
  assumptions: [],
  ambiguities: [],
  confidence: 0.9,
  needs_clarification: false,
  confirmed: false,
};


// 理解阶段此前只有输入框上方一行灰字,与轨迹完全分离。这里验证它真的渲染进
// 同一个轨迹面板:折叠行显示当前这一步,展开后与后端步骤同列。
test("问题理解阶段在轨迹面板里渲染,并与后端步骤同列", async () => {
  const user = userEvent.setup();
  const { container } = render(
    <ReasoningTracePanel
      steps={[
        intentUnderstoodStep(contract, 1500),
        { step_type: "start", summary: "启动检索", detail: { mode: "reasoning" } },
        { step_type: "memory", summary: "参考了 2 条你的记忆", detail: { count: 2 }, duration_ms: 120 },
        { step_type: "synthesis", summary: "已生成答案，引用 4 处证据",
          detail: { citations: 4, anchors: 2, evidence_level: "grounded" }, duration_ms: 8000 },
      ]}
    />,
  );

  // 折叠行:总步数与总耗时把理解与作答两段都算进去(1500 + 120 + 8000 = 9.6s)。
  expect(container.querySelector(".reasoning-trace-count")?.textContent)
    .toBe("4 步 · 9.6s");

  await user.click(screen.getByRole("button", { expanded: false }));
  const labels = screen.getAllByRole("listitem").map((row) => row.textContent ?? "");
  expect(labels[0]).toContain("理解");
  expect(labels[0]).toContain("已理解问题");
  expect(labels[0]).toContain("电荷泵 PLL 在 PVT 角落下为什么会失锁");
  expect(labels[1]).toContain("启动");
  expect(labels[2]).toContain("记忆");
  expect(labels[2]).toContain("2 条记忆");     // 不是通用的「2 个候选」
  expect(labels[3]).toContain("作答");
  expect(labels[3]).toContain("4 处引用");
});


test("理解在途与待澄清都占轨迹的当前一步(不再是轨迹之外的提示条)", () => {
  const { unmount } = render(
    <ReasoningTracePanel steps={[intentUnderstandingStep()]} live />,
  );
  expect(screen.getByText("理解")).toBeTruthy();
  expect(screen.getByText("正在理解问题，尚未读取资料或开始检索")).toBeTruthy();
  unmount();

  render(
    <ReasoningTracePanel
      steps={[intentClarifyStep(
        { ...contract, needs_clarification: true,
          ambiguities: [{ id: "a", question: "关注哪个指标？" }] },
        800,
      )]}
      live
    />,
  );
  expect(screen.getByText(/1 处会改变检索方向的歧义/)).toBeTruthy();
});


test("用户定稿那一步不计入总耗时(填表是人的时间)", () => {
  const { container } = render(
    <ReasoningTracePanel
      steps={[
        intentClarifyStep({ ...contract, ambiguities: [{ id: "a", question: "?" }] }, 800),
        intentConfirmedStep("最终问题", 1),
      ]}
    />,
  );
  // 两步,但总耗时只有理解那一步的 800ms —— 用户在确认卡片上思考多久都不该
  // 计进「这次问答花了多久」。
  expect(container.querySelector(".reasoning-trace-count")?.textContent)
    .toBe("2 步 · 800ms");
});
