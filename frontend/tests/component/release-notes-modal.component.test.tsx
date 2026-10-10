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
  { id: "newer", ordinal: 3190, level: "feature" as const, audience: "all" as const, title: "支持批量导出", body: "新增 **批量导出** 功能" },
  { id: "older", ordinal: 3185, level: "change" as const, audience: "all" as const, title: "上传入口挪到顶部", body: "" },
];

beforeEach(() => {
  mocks.markReleaseNotesSeen.mockReset();
  mocks.markReleaseNotesSeen.mockResolvedValue(undefined);
});

test("显示标题、版本；每条只显示级别与标题，默认收起正文", () => {
  render(<ReleaseNotesModal build={build} notes={notes} onClose={() => undefined} />);
  expect(screen.getByRole("heading", { name: "系统已更新" })).toBeInTheDocument();
  expect(screen.getByText("当前版本 20260929-9bacb8d")).toBeInTheDocument();
  const items = screen.getAllByRole("listitem");
  expect(items).toHaveLength(2);
  expect(items[0]).toHaveTextContent("新功能");
  expect(items[0]).toHaveTextContent("支持批量导出");
  expect(items[1]).toHaveTextContent("变化");
  expect(screen.queryByText("批量导出", { selector: "strong" })).not.toBeInTheDocument();
});

test("有正文的标题可展开/收起并渲染 markdown；无正文的标题不可点", async () => {
  const user = userEvent.setup();
  render(<ReleaseNotesModal build={build} notes={notes} onClose={() => undefined} />);
  const toggle = screen.getByRole("button", { name: /支持批量导出/ });
  expect(toggle).toHaveAttribute("aria-expanded", "false");
  await user.click(toggle);
  expect(toggle).toHaveAttribute("aria-expanded", "true");
  expect(screen.getByText("批量导出", { selector: "strong" })).toBeInTheDocument();
  await user.click(toggle);
  expect(toggle).toHaveAttribute("aria-expanded", "false");
  expect(screen.queryByText("批量导出", { selector: "strong" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /上传入口挪到顶部/ })).not.toBeInTheDocument();
});

test("moreCount > 0 时提示另有 N 项并链到更新记录；为 0 时只给查看全部的链接", () => {
  const { rerender } = render(<ReleaseNotesModal build={build} notes={notes} moreCount={7} onClose={() => undefined} />);
  expect(screen.getByText(/另有 7 项修复与改进，/)).toBeInTheDocument();
  let link = screen.getByRole("link", { name: "查看更新记录" });
  expect(link).toHaveAttribute("href", "/updates");
  expect(link).toHaveAttribute("target", "_blank");
  expect(link).toHaveAttribute("rel", "noreferrer");
  rerender(<ReleaseNotesModal build={build} notes={notes} moreCount={0} onClose={() => undefined} />);
  expect(screen.queryByText(/另有/)).not.toBeInTheDocument();
  link = screen.getByRole("link", { name: "查看全部更新记录" });
  expect(link).toHaveAttribute("href", "/updates");
  expect(link).toHaveAttribute("target", "_blank");
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

test("说明里的链接在新标签页打开，不带走当前页", async () => {
  const note = { id: "l", ordinal: 1, level: "feature" as const, audience: "all" as const, title: "有链接", body: "见 [详情](https://example.com/a)" };
  render(<ReleaseNotesModal build={build} notes={[note]} onClose={() => undefined} />);
  await userEvent.setup().click(screen.getByRole("button", { name: /有链接/ }));
  const link = screen.getByRole("link", { name: "详情" });
  expect(link).toHaveAttribute("target", "_blank");
  expect(link).toHaveAttribute("rel", "noreferrer");
});

test("挂载即把焦点交给「知道了」", () => {
  render(<ReleaseNotesModal build={build} notes={notes} onClose={() => undefined} />);
  expect(screen.getByRole("button", { name: "知道了" })).toHaveFocus();
});
