import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";

const mocks = vi.hoisted(() => ({ fetchMe: vi.fn(), fetchReleaseNotesHistory: vi.fn() }));

vi.mock("../../app/auth.ts", () => ({ fetchMe: mocks.fetchMe }));
vi.mock("../../app/release-notes-api.ts", () => ({ fetchReleaseNotesHistory: mocks.fetchReleaseNotesHistory }));

import UpdatesPage from "../../app/updates/page";

const note = (id: string, ordinal: number, level: string, title: string, body = "") =>
  ({ id, ordinal, level, audience: "all", title, body });

const history = {
  available: true,
  build: { version: "v9", ordinal: 9 },
  notes: [
    note("a", 9, "feature", "导出报告", "支持 **Word**"),
    note("b", 8, "internal", "调整索引"),
    note("c", 7, "fix", "修复显示"),
  ],
};

beforeEach(() => {
  mocks.fetchMe.mockReset().mockResolvedValue({ id: "u1" });
  mocks.fetchReleaseNotesHistory.mockReset();
  try { window.localStorage.removeItem("updates.showInternal"); } catch { /* 忽略 */ }
});

test("按接口顺序渲染，正文直接展开，后台改进默认隐藏，勾选后显示", async () => {
  mocks.fetchReleaseNotesHistory.mockResolvedValue(history);
  const user = userEvent.setup();
  render(<UpdatesPage />);
  expect(await screen.findByText("导出报告")).toBeInTheDocument();
  expect(screen.getByText("Word", { selector: "strong" })).toBeInTheDocument();
  expect(screen.getByText("修复显示")).toBeInTheDocument();
  expect(screen.queryByText("调整索引")).not.toBeInTheDocument();
  await user.click(screen.getByRole("checkbox", { name: "显示后台改进" }));
  expect(screen.getByText("调整索引")).toBeInTheDocument();
  expect(screen.getAllByRole("listitem").map((li) => li.textContent)).toEqual([
    expect.stringContaining("导出报告"), expect.stringContaining("调整索引"), expect.stringContaining("修复显示"),
  ]);
});

test("清单不可用时显示暂无更新记录", async () => {
  mocks.fetchReleaseNotesHistory.mockResolvedValue({ available: false, build: null, notes: [] });
  render(<UpdatesPage />);
  expect(await screen.findByText("暂无更新记录")).toBeInTheDocument();
});

test("加载失败显示错误", async () => {
  mocks.fetchReleaseNotesHistory.mockRejectedValue(new Error("down"));
  render(<UpdatesPage />);
  await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());
});

test("只剩后台改进且未勾选时，空态提示去勾选，而不是暂无更新记录", async () => {
  mocks.fetchReleaseNotesHistory.mockResolvedValue({
    ...history, notes: [note("b", 8, "internal", "调整索引")],
  });
  const user = userEvent.setup();
  render(<UpdatesPage />);
  expect(await screen.findByText("没有可显示的更新，勾选「显示后台改进」可以查看后台改进。")).toBeInTheDocument();
  expect(screen.queryByText("暂无更新记录")).not.toBeInTheDocument();
  await user.click(screen.getByRole("checkbox", { name: "显示后台改进" }));
  expect(screen.getByText("调整索引")).toBeInTheDocument();
});

test("「显示后台改进」写入 localStorage，重新挂载后恢复勾选", async () => {
  mocks.fetchReleaseNotesHistory.mockResolvedValue(history);
  const user = userEvent.setup();
  const first = render(<UpdatesPage />);
  await user.click(await screen.findByRole("checkbox", { name: "显示后台改进" }));
  first.unmount();
  render(<UpdatesPage />);
  expect(await screen.findByText("调整索引")).toBeInTheDocument();
  expect(screen.getByRole("checkbox", { name: "显示后台改进" })).toBeChecked();
});

test("每页 20 条，可翻页；切换「显示后台改进」回到第一页", async () => {
  const many = Array.from({ length: 45 }, (_, i) =>
    note(`n${i}`, 100 - i, i % 3 === 0 ? "internal" : "feature", `说明${i}`));
  mocks.fetchReleaseNotesHistory.mockResolvedValue({ ...history, notes: many });
  const user = userEvent.setup();
  render(<UpdatesPage />);
  await screen.findByText("说明1");
  // 45 条里 15 条后台改进，默认隐藏 → 30 条可见，共 2 页。
  expect(screen.getAllByRole("listitem")).toHaveLength(20);
  const nav = screen.getByRole("navigation", { name: "更新记录分页" });
  await user.click(within(nav).getByRole("button", { name: "下一页" }));
  expect(screen.getAllByRole("listitem")).toHaveLength(10);
  expect(screen.queryByText("说明1")).not.toBeInTheDocument();
  await user.click(screen.getByRole("checkbox", { name: "显示后台改进" }));
  expect(screen.getAllByRole("listitem")).toHaveLength(20);
  expect(screen.getByText("说明0")).toBeInTheDocument();
  expect(within(screen.getByRole("navigation", { name: "更新记录分页" })).getByText("第 1 / 3 页")).toBeInTheDocument();
});
