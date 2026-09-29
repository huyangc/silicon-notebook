import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  markReleaseNotesSeen: vi.fn(),
}));

vi.mock("../../app/release-notes-api.ts", () => ({
  markReleaseNotesSeen: mocks.markReleaseNotesSeen,
}));

import { ReleaseNotesModal } from "../../app/release-notes-modal";

const build = { version: "20260929-9bacb8d", ordinal: 3190 };
const notes = [
  { id: "newer", ordinal: 3190, body: "新增 **批量导出** 功能" },
  { id: "older", ordinal: 3185, body: "修复了一个显示问题" },
];

beforeEach(() => {
  mocks.markReleaseNotesSeen.mockReset();
  mocks.markReleaseNotesSeen.mockResolvedValue(undefined);
});

test("显示标题、版本，并按接口顺序渲染 markdown 说明", () => {
  render(<ReleaseNotesModal build={build} notes={notes} onClose={() => undefined} />);
  expect(screen.getByRole("heading", { name: "系统已更新" })).toBeInTheDocument();
  expect(screen.getByText("当前版本 20260929-9bacb8d")).toBeInTheDocument();
  const strong = screen.getByText("批量导出");
  expect(strong.tagName).toBe("STRONG");
  const items = screen.getAllByRole("listitem");
  expect(items).toHaveLength(2);
  expect(items[0]).toHaveTextContent("新增 批量导出 功能");
  expect(items[1]).toHaveTextContent("修复了一个显示问题");
});

test.each([
  ["知道了", () => screen.getByRole("button", { name: "知道了" })],
  ["×", () => screen.getByTitle("关闭")],
])("点「%s」立即关闭并只上报一次 build.ordinal", async (_label, target) => {
  const user = userEvent.setup();
  const onClose = vi.fn();
  render(<ReleaseNotesModal build={build} notes={notes} onClose={onClose} />);
  await user.click(target());
  await user.click(target());
  expect(onClose).toHaveBeenCalled();
  expect(mocks.markReleaseNotesSeen).toHaveBeenCalledTimes(1);
  expect(mocks.markReleaseNotesSeen).toHaveBeenCalledWith(3190);
});

test("上报失败静默：不抛错、不显示错误，仍然关闭", async () => {
  mocks.markReleaseNotesSeen.mockRejectedValue(new Error("boom"));
  const user = userEvent.setup();
  const onClose = vi.fn();
  render(<ReleaseNotesModal build={build} notes={notes} onClose={onClose} />);
  await user.click(screen.getByRole("button", { name: "知道了" }));
  expect(onClose).toHaveBeenCalledTimes(1);
  expect(screen.queryByText(/boom|失败/)).not.toBeInTheDocument();
});

test("上报同步抛错也不挡住关闭", async () => {
  mocks.markReleaseNotesSeen.mockImplementation(() => { throw new Error("sync"); });
  const user = userEvent.setup();
  const onClose = vi.fn();
  render(<ReleaseNotesModal build={build} notes={notes} onClose={onClose} />);
  await user.click(screen.getByTitle("关闭"));
  expect(onClose).toHaveBeenCalledTimes(1);
});

test("说明里的链接在新标签页打开，不带走当前页", () => {
  render(<ReleaseNotesModal build={build} notes={[{ id: "l", ordinal: 1, body: "见 [详情](https://example.com/a)" }]} onClose={() => undefined} />);
  const link = screen.getByRole("link", { name: "详情" });
  expect(link).toHaveAttribute("target", "_blank");
  expect(link).toHaveAttribute("rel", "noreferrer");
});

test("挂载即把焦点交给「知道了」", () => {
  render(<ReleaseNotesModal build={build} notes={notes} onClose={() => undefined} />);
  expect(screen.getByRole("button", { name: "知道了" })).toHaveFocus();
});
