import { render, screen, waitFor } from "@testing-library/react";
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
