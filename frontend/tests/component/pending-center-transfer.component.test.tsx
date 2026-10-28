import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";

const api = vi.hoisted(() => ({ downloadNotebookExport: vi.fn() }));
vi.mock("../../app/notebook-transfer-api.ts", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../app/notebook-transfer-api.ts")>()),
  ...api,
}));

const { PendingBell } = await import("../../app/pending-center.tsx");
import type { PendingItem } from "../../app/pending-center.tsx";

// 网页导出/导入作业在铃铛里单独一组:每条一句状态,行本身不跳转;行内动作只有完成的
// 导出「下载」、已经发布的导入「打开笔记本」(加上铃铛自己的「×」)。进行中的不计未读,
// 完成待下载与失败计。

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

test("导出与导入单独成组,每条一句状态,点行本身不跳转", async () => {
  const user = userEvent.setup();
  const onOpenItem = renderBell([RUNNING_IMPORT, READY_EXPORT, FAILED_IMPORT]);
  // 徽标只数完成待下载与失败的两条。
  expect(screen.getByText("2")).toBeInTheDocument();

  await user.click(screen.getByLabelText("待确认中心"));
  expect(screen.getByText("导出与导入")).toBeInTheDocument();
  expect(screen.getAllByTestId("pending-transfer-row")).toHaveLength(3);
  expect(screen.getByText("导入进行中")).toBeInTheDocument();
  expect(screen.getByText("导出完成，可以下载")).toBeInTheDocument();
  expect(screen.getByText("导入失败")).toBeInTheDocument();
  expect(screen.getByText("封装工艺库")).toBeInTheDocument();
  await user.click(screen.getByText("导出完成，可以下载"));
  expect(onOpenItem).not.toHaveBeenCalled();
});

test("完成的导出:行内「下载」取票据开始下载,结果写在它身上,面板不关", async () => {
  const user = userEvent.setup();
  api.downloadNotebookExport.mockResolvedValueOnce(undefined);
  const onOpenItem = renderBell([READY_EXPORT]);
  await user.click(screen.getByLabelText("待确认中心"));
  await user.click(screen.getByRole("button", { name: "下载" }));
  expect(api.downloadNotebookExport).toHaveBeenCalledWith("xfer-2");
  expect(await screen.findByRole("button", { name: "已开始下载" })).toBeInTheDocument();
  expect(screen.getByText("导出与导入")).toBeInTheDocument();
  expect(onOpenItem).not.toHaveBeenCalled();
});

test("已经发布的导入:行内「打开笔记本」交给 onOpenItem(打开新笔记本)并收起面板", async () => {
  const user = userEvent.setup();
  const published: PendingItem = {
    type: "notebook_transfer", kind: "import", state: "running", phase: "reembed", job_id: "xfer-5",
    notebook_id: "nb-new", notebook_name: "导入的库",
  };
  const onOpenItem = renderBell([published, RUNNING_IMPORT]);
  await user.click(screen.getByLabelText("待确认中心"));
  expect(screen.getByText("已导入，正在补全检索数据")).toBeInTheDocument();
  // 发布前的那一条没有动作。
  expect(screen.getAllByRole("button", { name: "打开笔记本" })).toHaveLength(1);
  await user.click(screen.getByRole("button", { name: "打开笔记本" }));
  expect(onOpenItem).toHaveBeenCalledWith(published);
  expect(screen.queryByText("导出与导入")).toBeNull();
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

test("状态行本身不像可点的样子:带静态修饰类,可点的是行内动作与「×」", async () => {
  const user = userEvent.setup();
  renderBell([READY_EXPORT]);
  await user.click(screen.getByLabelText("待确认中心"));
  const row = screen.getByTestId("pending-transfer-row");
  expect(row.tagName).toBe("DIV");
  expect(row.classList.contains("pending-row-static")).toBe(true);
});


test("行按作业身份做 key:前一条消失时,它在途的「下载」状态不会错交给下一条", async () => {
  const user = userEvent.setup();
  api.downloadNotebookExport.mockImplementationOnce(() => new Promise<void>(() => undefined));
  const second: PendingItem = { ...READY_EXPORT, job_id: "xfer-6", notebook_name: "第二本" };
  const props = {
    doneItems: [], userId: "u-transfer-key", onOpenItem: vi.fn(), onOpenDone: vi.fn(), onDismissDone: vi.fn(),
  };
  const { rerender } = render(<PendingBell snapshot={{ count: 0, items: [READY_EXPORT, second] }} {...props} />);
  await user.click(screen.getByLabelText("待确认中心"));
  await user.click(screen.getAllByRole("button", { name: "下载" })[0]);
  expect(screen.getByRole("button", { name: "准备下载…" })).toBeDisabled();
  // 第一条(下载完成后后端不再下发它)从快照里消失。
  rerender(<PendingBell snapshot={{ count: 0, items: [second] }} {...props} />);
  expect(screen.queryByRole("button", { name: "准备下载…" })).toBeNull();
  expect(screen.getByRole("button", { name: "下载" })).toBeEnabled();
});

test("关掉的「导入进行中」在新笔记本发布后以「已导入 · 打开笔记本」回来", async () => {
  const user = userEvent.setup();
  const before: PendingItem = { ...RUNNING_IMPORT, job_id: "xfer-11", phase: "rows", notebook_id: "nb-x" };
  const props = {
    doneItems: [], userId: "u-transfer-revive", onOpenItem: vi.fn(), onOpenDone: vi.fn(), onDismissDone: vi.fn(),
  };
  const { rerender } = render(<PendingBell snapshot={{ count: 0, items: [before] }} {...props} />);
  await user.click(screen.getByLabelText("待确认中心"));
  await user.click(screen.getByTitle("关掉"));
  expect(screen.queryByText("导入进行中")).toBeNull();
  rerender(<PendingBell snapshot={{ count: 0, items: [{ ...before, phase: "reembed" }] }} {...props} />);
  expect(screen.getByText("已导入，正在补全检索数据")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "打开笔记本" })).toBeInTheDocument();
});
