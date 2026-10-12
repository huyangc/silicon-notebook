import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";

vi.mock("../../app/waiting-wish-carousel", () => ({
  WaitingWishCarousel: () => <div aria-label="测试许愿轮播" />,
}));

import { ReportsPanel, type ReportDetailT } from "../../app/report-view";
import { reportWorkspaceFixture } from "./report-workspace-fixture";

afterEach(cleanup);

const RAW = "OperationalError: connection to 10.9.8.7:5432 refused";

// 作者本人的报告详情:正文里失败章节只写中性说明,原文在 sections[].error。
function doneReport(sections: ReportDetailT["sections"]): ReportDetailT {
  return {
    id: "rep-failed-section",
    question: "为什么?",
    status: "done",
    progress: "",
    section_count: sections.length,
    created_at: "2026-08-01T00:00:00Z",
    created_by: "user-1",
    outline: [],
    sections,
    section_status: [],
    gaps: [],
    content_md: "## A\n\n（本节生成失败:本节处理过程出错）\n\n## B\n\n正文 B",
    references: [],
    error: "",
    understanding: {},
  };
}

function renderReport(report: ReportDetailT) {
  render(
    <ReportsPanel
      notebookId="nb-1"
      workspace={reportWorkspaceFixture({ active: report })}
      setToast={vi.fn()}
    />,
  );
}

test("作者在自己的报告里可以展开看到失败章节的原文", async () => {
  const user = userEvent.setup();
  renderReport(doneReport([
    { title: "A", markdown: "", grounded: false, failed: true, error: RAW },
    { title: "B", markdown: "正文 B", grounded: false },
  ]));

  const block = screen.getByLabelText("章节失败原因");
  // 正文里仍是中性说明;原文默认折叠在说明旁的「失败原因」里。
  expect(screen.getByText(/本节处理过程出错/)).toBeInTheDocument();
  const summary = within(block).getByText("A · 失败原因");
  expect(within(block).getByText(RAW)).not.toBeVisible();
  await user.click(summary);
  expect(within(block).getByText(RAW)).toBeVisible();
  expect(within(block).queryByText(/B · 失败原因/)).toBeNull();
});

test("没有失败章节时不显示失败原因", () => {
  renderReport(doneReport([
    { title: "B", markdown: "正文 B", grounded: false },
    // 没有原文(例如旧数据)也不渲染空的折叠项。
    { title: "C", markdown: "", grounded: false, failed: true },
  ]));
  expect(screen.queryByLabelText("章节失败原因")).toBeNull();
});
