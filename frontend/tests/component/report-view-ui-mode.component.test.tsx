import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";

import { ReportsPanel } from "../../app/report-view";
import { reportWorkspaceFixture } from "./report-workspace-fixture";

afterEach(cleanup);

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
