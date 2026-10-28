import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";

import { PendingBell, type PendingItem } from "../../app/pending-center.tsx";

// 网页导出/导入作业在完整面板上线前的最小呈现:单独一组,每条只有一句状态,
// 不跳转、不带操作;进行中的不计未读,完成待下载与失败计。

const RUNNING_IMPORT: PendingItem = {
  type: "notebook_transfer", kind: "import", state: "running", job_id: "xfer-1",
  notebook_id: "", notebook_name: "",
};
const READY_EXPORT: PendingItem = {
  type: "notebook_transfer", kind: "export", state: "succeeded", job_id: "xfer-2",
  notebook_id: "nb1", notebook_name: "封装工艺库",
};
const FAILED_IMPORT: PendingItem = {
  type: "notebook_transfer", kind: "import", state: "failed", job_id: "xfer-3",
  notebook_id: "", notebook_name: "",
};

function renderBell(items: PendingItem[]) {
  const onOpenItem = vi.fn();
  render(
    <PendingBell
      snapshot={{ count: 0, items }}
      doneItems={[]}
      userId="u-transfer"
      onOpenItem={onOpenItem}
      onOpenDone={vi.fn()}
      onDismissDone={vi.fn()}
    />,
  );
  return onOpenItem;
}

test("导出与导入单独成组,每条一句状态,点击不跳转", async () => {
  const user = userEvent.setup();
  const onOpenItem = renderBell([RUNNING_IMPORT, READY_EXPORT, FAILED_IMPORT]);
  // 徽标只数完成待下载与失败的两条。
  expect(screen.getByText("2")).toBeInTheDocument();

  await user.click(screen.getByLabelText("待确认中心"));
  expect(screen.getByText("导出与导入")).toBeInTheDocument();
  expect(screen.getAllByTestId("pending-transfer-row")).toHaveLength(3);
  expect(screen.getByText("导入进行中")).toBeInTheDocument();
  expect(screen.getByText("导出完成")).toBeInTheDocument();
  expect(screen.getByText("导入失败")).toBeInTheDocument();
  expect(screen.getByText("封装工艺库")).toBeInTheDocument();
  await user.click(screen.getByText("导出完成"));
  expect(onOpenItem).not.toHaveBeenCalled();
});

test("两条发布前的导入互不相撞:各自可见、各自可关掉", async () => {
  const user = userEvent.setup();
  const other = { ...RUNNING_IMPORT, job_id: "xfer-9" };
  renderBell([RUNNING_IMPORT, other]);
  await user.click(screen.getByLabelText("待确认中心"));
  expect(screen.getAllByText("导入进行中")).toHaveLength(2);
  await user.click(screen.getAllByTitle("关掉")[0]);
  expect(screen.getAllByText("导入进行中")).toHaveLength(1);
});

test("状态行不像可点的样子:带静态修饰类,只有「×」可点", async () => {
  const user = userEvent.setup();
  renderBell([READY_EXPORT]);
  await user.click(screen.getByLabelText("待确认中心"));
  const row = screen.getByTestId("pending-transfer-row");
  expect(row.tagName).toBe("DIV");
  expect(row.classList.contains("pending-row-static")).toBe(true);
});
