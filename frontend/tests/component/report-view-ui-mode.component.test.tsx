import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";

import { ReportsPanel } from "../../app/report-view";


afterEach(cleanup);


function renderPanel(uiMode?: "auto" | "advanced") {
  render(
    <ReportsPanel
      notebookId="nb-1"
      listReports={vi.fn().mockResolvedValue([])}
      getReport={vi.fn()}
      createReport={vi.fn().mockResolvedValue({ report_id: "rep-1" })}
      confirmReportIntent={vi.fn()}
      updateReportOutline={vi.fn()}
      generateReport={vi.fn()}
      cancelReport={vi.fn()}
      deleteReport={vi.fn()}
      shareReport={vi.fn()}
      getReportShare={vi.fn()}
      unshareReport={vi.fn()}
      downloadReportsZip={vi.fn()}
      setToast={vi.fn()}
      uiMode={uiMode}
    />,
  );
}


test("高级模式(默认)渲染「研究深度」档位控件", async () => {
  renderPanel("advanced");
  expect(await screen.findByRole("button", { name: /研究深度/ })).toBeVisible();
});


test("省略 uiMode 时按既有高级模式行为渲染档位控件(调用方兼容)", async () => {
  renderPanel();
  expect(await screen.findByRole("button", { name: /研究深度/ })).toBeVisible();
});


test("自动模式下不渲染「研究深度」档位控件", async () => {
  renderPanel("auto");
  // 等列表加载完成(空列表提示先出现),确认控件区域已经渲染完毕再断言缺失。
  expect(await screen.findByText("还没有深度报告。输入研究问题，生成第一份带出处的长文报告。")).toBeVisible();
  expect(screen.queryByRole("button", { name: /研究深度/ })).toBeNull();
});


test("自动模式创建报告固定用默认档「标准」(depth=2)，不读 depthIdx", async () => {
  const createReport = vi.fn().mockResolvedValue({ report_id: "rep-1" });
  render(
    <ReportsPanel
      notebookId="nb-1"
      listReports={vi.fn().mockResolvedValue([])}
      getReport={vi.fn()}
      createReport={createReport}
      confirmReportIntent={vi.fn()}
      updateReportOutline={vi.fn()}
      generateReport={vi.fn()}
      cancelReport={vi.fn()}
      deleteReport={vi.fn()}
      shareReport={vi.fn()}
      getReportShare={vi.fn()}
      unshareReport={vi.fn()}
      downloadReportsZip={vi.fn()}
      setToast={vi.fn()}
      uiMode="auto"
    />,
  );
  await screen.findByText("还没有深度报告。输入研究问题，生成第一份带出处的长文报告。");
  const { default: userEvent } = await import("@testing-library/user-event");
  const user = userEvent.setup();
  await user.type(screen.getByPlaceholderText(/想深入研究什么/), "对比两类方法");
  await user.click(screen.getByRole("button", { name: "生成深度报告" }));

  expect(createReport).toHaveBeenCalledWith("nb-1", "对比两类方法", 2);
});
