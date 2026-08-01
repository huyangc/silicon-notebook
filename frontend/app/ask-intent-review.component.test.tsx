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
  result_scope: "ranked",
  completeness_required: false,
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
  // 必答主题挪进「系统的理解」读出区,行标签不再带模式名——整张卡就是这一轮的,
  // 再挂一次模式名只是噪声。卡片的 aria-label 仍带模式名,下面那条断言钉住它。
  expect(screen.getByText("必须回答")).toBeVisible();
  expect(
    screen.getByLabelText(`确认${modeLabel("reasoning")}的问题理解`),
  ).toBeVisible();
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


test("selected source scope is read-only and requires one explicit confirmation", async () => {
  const onConfirm = vi.fn();
  const user = userEvent.setup();
  const scopedContract: QueryIntentContract = {
    ...contract,
    resolved_question: "比较 place_opt_design 在两套工具中的对应命令",
    source_refs: ["Innovus User Guide", "ICC2 Command Reference.pdf"],
    source_scope: {
      mode: "selected",
      sources: [
        {
          source_id: "source-innovus",
          notebook_id: "notebook-1",
          title: "Innovus User Guide",
          source_file_name: "innovus-23.1.pdf",
        },
        {
          source_id: "source-icc2",
          notebook_id: "notebook-1",
          title: "ICC2 Command Reference.pdf",
          source_file_name: "ICC2 Command Reference.pdf",
        },
      ],
    },
    ambiguities: [{
      id: "source_scope_confirmation",
      question: "确认本次仅依据这 2 个指定来源吗？",
      reason: "限定后的来源集合将约束本轮全部证据与引用",
      required: true,
      options: ["确认"],
    }],
  };

  render(
    <AskIntentReview
      contract={scopedContract}
      onConfirm={onConfirm}
      onCancel={vi.fn()}
    />,
  );

  expect(screen.getByRole("heading", { name: "本轮只检索下列来源" })).toBeVisible();
  expect(screen.getByText("检索资料：仅限 2 个来源")).toBeVisible();
  expect(screen.getByText("Innovus User Guide")).toBeVisible();
  expect(screen.getByText("原始文件：innovus-23.1.pdf")).toBeVisible();
  expect(screen.queryByText("原始文件：ICC2 Command Reference.pdf")).toBeNull();

  const question = screen.getByDisplayValue(scopedContract.resolved_question);
  expect(question).toBeDisabled();
  expect(question).toHaveAttribute("readonly");
  const submit = screen.getByRole("button", { name: "确认并开始检索" });
  expect(submit).toBeDisabled();

  await user.click(screen.getByRole("radio", { name: "确认，仅检索上述来源" }));
  expect(submit).toBeEnabled();
  await user.click(submit);
  expect(onConfirm).toHaveBeenCalledWith({
    contract: scopedContract,
    resolved_question: scopedContract.resolved_question,
    answers: [{ id: "source_scope_confirmation", answer: "确认" }],
  });
});
