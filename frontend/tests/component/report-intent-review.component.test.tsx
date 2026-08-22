import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";

import { IntentReview, type ReportDetailT } from "../../app/report-view";


afterEach(cleanup);


function detail(): ReportDetailT {
  return {
    id: "rep-1",
    question: "分析一下这个问题",
    status: "intent_ready",
    progress: "需要补充关键信息",
    section_count: 0,
    created_at: "2026-07-25T00:00:00Z",
    created_by: "user-1",
    outline: [],
    sections: [],
    section_status: [],
    gaps: [],
    content_md: "",
    references: [],
    error: "",
    understanding: {
      resolved_question: "分析这个问题",
      intent_type: "other",
      confidence: 0.42,
      needs_clarification: true,
      mandatory_topics: [{
        id: "intent-1",
        title: "待明确",
        question: "需要分析哪个对象？",
        retrieval_queries: ["分析"],
      }],
      ambiguities: [{
        id: "ambiguity-input",
        question: "你提到的对象具体是什么？",
        reason: "存在无法解析的指代",
        required: true,
        options: ["电荷泵 PLL", "环形振荡器"],
      }],
      assumptions: ["面向电路设计"],
    },
  };
}


test("blocks retrieval planning until required clarification is answered", async () => {
  const onConfirm = vi.fn().mockResolvedValue(undefined);
  const user = userEvent.setup();

  render(
    <IntentReview
      report={detail()}
      busy={false}
      onConfirm={onConfirm}
      setToast={vi.fn()}
      readOnly={false}
    />,
  );

  expect(screen.getByText("这一步只理解你的问题，不读取语料。确认后才会检索并规划大纲。")).toBeVisible();
  // 置信度移到「系统的理解」摘要行的标签里,前缀「理解」由所在区块承担。
  expect(screen.getByText("置信度 42%")).toBeVisible();
  const submit = screen.getByRole("button", { name: "提交补充并开始规划" });
  expect(submit).toBeDisabled();

  await user.click(screen.getByRole("button", { name: "电荷泵 PLL" }));
  expect(submit).toBeEnabled();
  await user.click(submit);

  expect(onConfirm).toHaveBeenCalledWith({
    resolved_question: "分析这个问题",
    answers: [{ id: "ambiguity-input", answer: "电荷泵 PLL" }],
  });
});


// 只读成员(读共享成员)看到的仍须是完整的问题与澄清项,只是不可编辑。
// 改版时我一度把整个确认区换成一句等待提示,等于让协作者看不到所有者正在被问
// 什么(codex #389 第 1 轮 P2)。
test("read-only members still see the question and clarifications, just disabled", () => {
  render(
    <IntentReview
      report={detail()}
      busy={false}
      onConfirm={vi.fn()}
      setToast={vi.fn()}
      readOnly
    />,
  );

  expect(screen.getByText("该报告正在等待所有者确认问题理解。")).toBeVisible();
  // 研究问题与澄清项都在,且都禁用。
  const question = screen.getByDisplayValue("分析这个问题") as HTMLTextAreaElement;
  expect(question).toBeVisible();
  expect(question).toBeDisabled();
  expect(screen.getByText("你提到的对象具体是什么？")).toBeVisible();
  expect(screen.getByText("存在无法解析的指代")).toBeVisible();
  expect(screen.getByRole("button", { name: "电荷泵 PLL" })).toBeDisabled();
  expect(
    screen.getByLabelText("你提到的对象具体是什么？的补充答案"),
  ).toBeDisabled();
  // 只读时不给确认按钮。
  expect(screen.queryByRole("button", { name: /开始规划/ })).toBeNull();
});
