// 回答上方的关联报告回执（「就此追问」）：读入时可展开逐节列出读了什么，没读时一行原因，
// 没挂报告的回答一个节点都不多。
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test } from "vitest";

import { AnswerView } from "../../app/answer-panel";
import type { AskResponse, LinkedReportReceipt } from "../../app/workspace-model";


function answer(receipt?: LinkedReportReceipt): AskResponse {
  return {
    answer_id: "answer-1",
    conversation_id: "conversation-1",
    conclusion: "对应结论见正文。",
    answer: "对应结论见正文。",
    grounded: true,
    anchors: [],
    related_knowledge: [],
    citations: [],
    llm_mode: "grounded",
    ...(receipt ? { linked_report: receipt } : {}),
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


test("读入了部分章节：摘要一句说清，展开逐节列出读入与否和引用证据计数", async () => {
  const user = userEvent.setup();
  const { container } = render(view(answer({
    report_id: "report-1",
    title: "电池安全综述",
    status: "read",
    sections: [
      { index: 1, title: "热失控机理", read: false },
      { index: 2, title: "防护材料", read: true },
      { index: 3, title: "标准与测试", read: true },
    ],
    evidence_restored: 18,
    evidence_gone: 2,
    evidence_over_budget: 1,
  })));

  const summary = screen.getByText(
    "关联报告《电池安全综述》 · 已读取执行摘要与第 2、3 节（共 3 节）；复原引用证据 18 条，2 条已失效，1 条超出篇幅未读入",
  );
  const details = summary.closest("details");
  expect(details).toHaveClass("answer-linked-report");
  expect(details).not.toHaveAttribute("open");
  // 回执排在回答正文之前（与检索范围回执同一处）。
  const markdown = container.querySelector(".answer-markdown")!;
  expect(details!.compareDocumentPosition(markdown) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();

  await user.click(summary);
  expect(screen.getByText("执行摘要")).toBeVisible();
  expect(screen.getByText("已读取")).toBeVisible();
  expect(screen.getByText("第 1 节《热失控机理》")).toHaveClass("linked-report-section-skipped");
  expect(screen.getByText("正文未读入")).toBeVisible();
  expect(screen.getByText("第 2 节《防护材料》")).not.toHaveClass("linked-report-section-skipped");
  expect(screen.getAllByText("已读取正文")).toHaveLength(2);
  expect(screen.getByText("复原 18 条 · 2 条已失效 · 1 条超出篇幅未读入")).toBeVisible();
});

test("读入了全文：摘要写「已读取报告全文」，执行摘要截断时如实标出", async () => {
  const user = userEvent.setup();
  const { rerender } = render(view(answer({
    report_id: "report-1",
    title: "电池安全综述",
    status: "read",
    sections: [{ index: 1, title: "一", read: true }],
    evidence_restored: 3,
  })));
  expect(screen.getByText("关联报告《电池安全综述》 · 已读取报告全文；复原引用证据 3 条")).toBeInTheDocument();

  rerender(view(answer({
    report_id: "report-1",
    title: "电池安全综述",
    status: "read",
    sections: [{ index: 1, title: "一", read: true }],
    summary_truncated: true,
  })));
  const summary = screen.getByText("关联报告《电池安全综述》 · 已读取执行摘要（已截断）与全部 1 节");
  await user.click(summary);
  expect(screen.getByText("已读取（篇幅所限，已截断）")).toBeVisible();
  expect(screen.getByText("复原 0 条")).toBeVisible();
});

test("这一轮没读报告：一行原样展示后端写好的原因，没有可展开的折叠块", () => {
  const reason = "本轮不读取这份报告：它用到了你的个人记忆，而收窄检索范围后本轮不读取个人记忆";
  const { container } = render(view(answer({
    report_id: "report-1",
    title: "电池安全综述",
    status: "skipped",
    reason_code: "memory_closed",
    reason,
  })));
  const note = screen.getByText(`关联报告《电池安全综述》 · ${reason}`);
  expect(note).toHaveAttribute("role", "note");
  expect(note).toHaveClass("answer-linked-report", "skipped");
  expect(container.querySelector("details.answer-linked-report")).toBeNull();
});

test("读取出错的回执题目为空：用不含题目的兜底文案", () => {
  render(view(answer({
    report_id: "report-1",
    title: "",
    status: "skipped",
    reason_code: "read_failed",
    reason: "本轮不读取这份报告：读取报告时出错，请重试",
  })));
  expect(screen.getByText("关联报告 · 本轮不读取这份报告：读取报告时出错，请重试")).toBeInTheDocument();
  expect(screen.queryByText(/《》/)).toBeNull();
});

test("没挂报告的回答一个节点都不多", () => {
  const { container } = render(view(answer()));
  expect(container.querySelector(".answer-linked-report")).toBeNull();
  expect(screen.queryByText(/关联报告/)).toBeNull();
});
