import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";
import { SourceReader } from "../../app/source-reader";
import * as api from "../../app/source-api";
import { setToken } from "../../app/auth-session";

vi.mock("../../app/page-reload", () => ({ reloadPage: vi.fn() }));

vi.mock("../../app/source-api", () => ({
  getNotebookSource: vi.fn(),
  getNotebookSourceElementsPage: vi.fn(),
  fetchInternalAssetBlob: vi.fn(),
}));
const row = (id: string, text: string) => ({ id, text, element_type: "paragraph", metadata: { preview_markdown: text }, source_id: "s", location_label: "" });
beforeEach(() => {
  localStorage.clear();
  setToken("test-token");
  vi.mocked(api.getNotebookSource).mockReset().mockResolvedValue({ id: "s", title: "技术手册" } as never);
  vi.mocked(api.getNotebookSourceElementsPage).mockReset();
});

test("reader uses bounded participant-proxy pages and retries continuation without losing text", async () => {
  const user = userEvent.setup();
  vi.mocked(api.getNotebookSourceElementsPage)
    .mockResolvedValueOnce({ items: [row("a", "**第一页**")], offset: 0, limit: 40, total_count: 2 })
    .mockRejectedValueOnce(new Error("unavailable"))
    .mockResolvedValueOnce({ items: [row("b", "第二页")], offset: 1, limit: 40, total_count: 2 });
  render(<SourceReader notebookId="active" sourceId="s" />);
  expect(screen.getByRole("status")).toHaveTextContent("正在加载内容");
  await screen.findByRole("heading", { name: "技术手册" });
  expect(api.getNotebookSource).toHaveBeenCalledWith("active", "s");
  expect(api.getNotebookSourceElementsPage).toHaveBeenCalledWith("active", "s", 0, 40);
  expect(api.getNotebookSourceElementsPage).toHaveBeenCalledTimes(1);
  expect(screen.getByText("第一页").tagName).toBe("STRONG");
  await user.click(screen.getByRole("button", { name: "继续加载" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("后续内容加载失败");
  expect(screen.getByText("第一页")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "重试" }));
  await screen.findByText("第二页");
  expect(api.getNotebookSourceElementsPage).toHaveBeenLastCalledWith("active", "s", 1, 40);
  expect(screen.getByText("已显示全部内容")).toBeInTheDocument();
});

test("signed-out reader offers login without issuing source requests", async () => {
  localStorage.clear();
  render(<SourceReader notebookId="n" sourceId="s" />);
  expect(await screen.findByRole("link", { name: "登录并返回笔记本" })).toHaveAttribute("href", "/#notebook=n&source=s");
  expect(api.getNotebookSource).not.toHaveBeenCalled();
});

test("unavailable and empty sources expose retry and empty states without diagnostic leakage", async () => {
  vi.mocked(api.getNotebookSourceElementsPage).mockRejectedValueOnce(new Error("private diagnostic"))
    .mockResolvedValueOnce({ items: [], total_count: 0, offset: 0, limit: 40 });
  render(<SourceReader notebookId="n" sourceId="s" />);
  expect(await screen.findByRole("alert")).not.toHaveTextContent("private diagnostic");
  await userEvent.click(screen.getByRole("button", { name: "重试" }));
  expect(await screen.findByText(/^当前来源还没有解析出内容/)).toBeInTheDocument();
});
