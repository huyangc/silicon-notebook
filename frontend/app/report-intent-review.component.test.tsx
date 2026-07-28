import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";

import { IntentReview, type ReportDetailT } from "./report-view";


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
  const confirmReportIntent = vi.fn().mockResolvedValue({ status: "planning" });
  const onPlanning = vi.fn();
  const user = userEvent.setup();

  render(
    <IntentReview
      report={detail()}
      notebookId="nb-1"
      confirmReportIntent={confirmReportIntent}
      onPlanning={onPlanning}
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

  expect(confirmReportIntent).toHaveBeenCalledWith("nb-1", "rep-1", {
    resolved_question: "分析这个问题",
    answers: [{ id: "ambiguity-input", answer: "电荷泵 PLL" }],
  });
  expect(onPlanning).toHaveBeenCalledWith(expect.objectContaining({
    status: "planning",
    progress: "按已确认问题规划中",
  }));
});
