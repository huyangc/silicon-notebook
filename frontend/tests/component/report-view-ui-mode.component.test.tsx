import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";

vi.mock("../../app/waiting-wish-carousel", () => ({
  WaitingWishCarousel: () => <div aria-label="测试许愿轮播" />,
}));

import { downloadReportArchive, ReportsPanel, type ReportDetailT } from "../../app/report-view";
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
