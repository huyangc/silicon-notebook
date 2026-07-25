import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";

import { AskIntentReview } from "./ask-intent-review";
import { modeLabel } from "./ask-modes";
import type { QueryIntentContract } from "./ask-intent-model";


afterEach(cleanup);


const contract: QueryIntentContract = {
  objective: "分析一下这个问题",
  resolved_question: "分析电荷泵 PLL",
  intent_type: "diagnose",
  entities: ["电荷泵 PLL"],
  mandatory_topics: [{
    id: "intent-1",
    title: "失锁",
    question: "为什么会失锁？",
    retrieval_queries: ["电荷泵 PLL 失锁"],
  }],
  comparison_axes: [],
  constraints: ["PVT 角落"],
  excluded_topics: [],
  expected_output: "给出按优先级排序的排查步骤",
  assumptions: ["环路已正常上电"],
  ambiguities: [{
    id: "ambiguity-input",
    question: "重点关注哪个指标？",
    reason: "不同指标会改变检索方向",
    required: true,
    options: ["锁定时间", "抖动"],
  }],
  confidence: 0.48,
  needs_clarification: true,
  confirmed: false,
};


test("reasoning intent review blocks retrieval until clarification is confirmed", async () => {
  const onConfirm = vi.fn();
  const user = userEvent.setup();
  render(
    <AskIntentReview
      contract={contract}
      onConfirm={onConfirm}
      onCancel={vi.fn()}
    />,
  );

  expect(screen.getByText("这一步只理解你的问题，不读取资料；确认后才会开始逐步检索。")).toBeVisible();
  expect(screen.getByText(`${modeLabel("reasoning")}必须回答`)).toBeVisible();
  expect(screen.getByText("给出按优先级排序的排查步骤")).toBeVisible();
  expect(screen.getByText("环路已正常上电")).toBeVisible();
  const submit = screen.getByRole("button", { name: "确认并开始检索" });
  expect(submit).toBeDisabled();

  await user.click(screen.getByRole("button", { name: "锁定时间" }));
  expect(submit).toBeEnabled();
  await user.click(submit);

  expect(onConfirm).toHaveBeenCalledWith({
    contract,
    resolved_question: "分析电荷泵 PLL",
    answers: [{ id: "ambiguity-input", answer: "锁定时间" }],
  });
});
