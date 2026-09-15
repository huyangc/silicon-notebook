import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";

vi.mock("../../app/waiting-wish-carousel", () => ({
  WaitingWishCarousel: () => <div aria-label="测试许愿轮播" />,
}));

import { downloadReportArchive, ReportsPanel, type ReportDetailT } from "../../app/report-view";
import type { ReportSummaryT } from "../../app/report-model";
import { reportWorkspaceFixture } from "./report-workspace-fixture";

afterEach(cleanup);

test("归档下载保留固定文件名并回收临时 URL", () => {
  const createObjectURL = vi.fn(() => "blob:reports");
  const revokeObjectURL = vi.fn();
  Object.defineProperty(URL, "createObjectURL", { configurable: true, value: createObjectURL });
  Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: revokeObjectURL });
  const anchor = document.createElement("a");
  const click = vi.spyOn(anchor, "click").mockImplementation(() => undefined);
  const createElement = vi.spyOn(document, "createElement").mockReturnValue(anchor);

  const blob = new Blob(["zip"]);
  downloadReportArchive(blob);

  expect(createObjectURL).toHaveBeenCalledWith(blob);
  expect(anchor.download).toBe("reports.zip");
  expect(anchor.href).toBe("blob:reports");
  expect(click).toHaveBeenCalledOnce();
  expect(revokeObjectURL).toHaveBeenCalledWith("blob:reports");
  createElement.mockRestore();
  Reflect.deleteProperty(URL, "createObjectURL");
  Reflect.deleteProperty(URL, "revokeObjectURL");
});

function renderPanel(uiMode?: "auto" | "advanced") {
  render(
    <ReportsPanel
      notebookId="nb-1"
      workspace={reportWorkspaceFixture()}
      setToast={vi.fn()}
      uiMode={uiMode}
    />,
  );
}

test("高级模式(默认)渲染「研究深度」档位控件", () => {
  renderPanel("advanced");
  expect(screen.getByRole("button", { name: /研究深度/ })).toBeVisible();
});

test("省略 uiMode 时按既有高级模式行为渲染档位控件(调用方兼容)", () => {
  renderPanel();
  expect(screen.getByRole("button", { name: /研究深度/ })).toBeVisible();
});

test("自动模式下不渲染「研究深度」档位控件", () => {
  renderPanel("auto");
  expect(screen.getByText("还没有深度报告。输入研究问题，生成第一份带出处的长文报告。")).toBeVisible();
  expect(screen.queryByRole("button", { name: /研究深度/ })).toBeNull();
});

test("创建动作委托给 report workspace owner", async () => {
  const submitCreate = vi.fn();
  render(
    <ReportsPanel
      notebookId="nb-1"
      workspace={reportWorkspaceFixture({ question: "对比两类方法", submitCreate })}
      setToast={vi.fn()}
      uiMode="auto"
    />,
  );
  screen.getByRole("button", { name: "生成深度报告" }).click();
  expect(submitCreate).toHaveBeenCalledOnce();
});

test("深度报告生成等待态挂载许愿轮播，终态不挂载", () => {
  const active = {
    id: "report-running",
    question: "分析未来趋势",
    status: "generating",
    progress: "正在撰写",
    section_count: 1,
    created_at: "2026-08-31T10:00:00Z",
    created_by: "user-1",
    outline: [{ title: "趋势", scope: "", sub_queries: [] }],
    sections: [],
    section_status: [],
    gaps: [],
    content_md: "",
    references: [],
    error: "",
    understanding: {},
  } satisfies ReportDetailT;
  const view = render(
    <ReportsPanel
      notebookId="nb-1"
      workspace={reportWorkspaceFixture({ active })}
      setToast={vi.fn()}
    />,
  );

  expect(screen.getByLabelText("测试许愿轮播")).toBeInTheDocument();

  view.rerender(
    <ReportsPanel
      notebookId="nb-1"
      workspace={reportWorkspaceFixture({ active: { ...active, status: "done" } })}
      setToast={vi.fn()}
    />,
  );
  expect(screen.queryByLabelText("测试许愿轮播")).not.toBeInTheDocument();
});

// 深度报告清单分页：接口一次性整份返回（GET /notebooks/{id}/reports 不分页），
// 界面每页 20 条。
function reportSummaries(count: number): ReportSummaryT[] {
  return Array.from({ length: count }, (_, index) => ({
    id: `report-${String(index + 1).padStart(2, "0")}`,
    question: `问题${String(index + 1).padStart(2, "0")}`,
    status: "done",
    progress: "",
    section_count: 2,
    created_at: "2026-08-01T00:00:00Z",
    created_by: "user-1",
  }));
}

function reportQuestions(): string[] {
  return Array.from(document.querySelectorAll(".report-list .chat-session-card-main > span"))
    .map((node) => node.textContent ?? "");
}

function reportDetail(summary: ReportSummaryT): ReportDetailT {
  return {
    ...summary,
    outline: [],
    sections: [],
    section_status: [],
    gaps: [],
    content_md: "",
    references: [],
    error: "",
    understanding: {},
  };
}

test("深度报告超过一页时分页展示，翻页后显示下一批报告", async () => {
  const user = userEvent.setup();
  render(
    <ReportsPanel
      notebookId="nb-1"
      workspace={reportWorkspaceFixture({ reports: reportSummaries(25) })}
      setToast={vi.fn()}
    />,
  );

  expect(reportQuestions()).toHaveLength(20);
  expect(reportQuestions()[0]).toBe("问题01");
  const pager = screen.getByRole("navigation", { name: "深度报告分页" });
  expect(within(pager).getByText("1–20 / 25")).toBeInTheDocument();

  await user.click(within(pager).getByRole("button", { name: "下一页" }));
  expect(reportQuestions()).toEqual(["问题21", "问题22", "问题23", "问题24", "问题25"]);
  expect(within(pager).getByRole("button", { name: "下一页" })).toBeDisabled();
});

test("一页放得下时不显示深度报告分页控件", () => {
  render(
    <ReportsPanel
      notebookId="nb-1"
      workspace={reportWorkspaceFixture({ reports: reportSummaries(20) })}
      setToast={vi.fn()}
    />,
  );

  expect(reportQuestions()).toHaveLength(20);
  expect(screen.queryByRole("navigation", { name: "深度报告分页" })).not.toBeInTheDocument();
});

test("打开落在后面一页的报告、再返回列表时，列表自动翻到它所在的页", async () => {
  const summaries = reportSummaries(25);
  const target = summaries[22]; // report-23，落在第二页（21–25）
  const { rerender } = render(
    <ReportsPanel
      notebookId="nb-1"
      workspace={reportWorkspaceFixture({ reports: summaries })}
      setToast={vi.fn()}
    />,
  );
  expect(reportQuestions()[0]).toBe("问题01");

  // 打开该报告：active 变为这份报告（详情视图取代列表，列表分页状态在背后跟随）。
  rerender(
    <ReportsPanel
      notebookId="nb-1"
      workspace={reportWorkspaceFixture({ reports: summaries, active: reportDetail(target) })}
      setToast={vi.fn()}
    />,
  );
  expect(screen.getByRole("button", { name: "返回列表" })).toBeInTheDocument();

  // 返回列表：active 清空，列表应当已经翻到 report-23 所在的那一页，而不是回到第一页。
  rerender(
    <ReportsPanel
      notebookId="nb-1"
      workspace={reportWorkspaceFixture({ reports: summaries, active: null })}
      setToast={vi.fn()}
    />,
  );
  await waitFor(() => expect(reportQuestions()).toContain("问题23"));
  const pager = screen.getByRole("navigation", { name: "深度报告分页" });
  expect(within(pager).getByText("21–25 / 25")).toBeInTheDocument();
});
