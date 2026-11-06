import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";
import { PublicShareSave } from "../../app/public-share-save";

const mocks = vi.hoisted(() => ({ request: vi.fn(), save: vi.fn() }));
vi.mock("../../app/api-client", () => ({ requestJson: mocks.request }));
vi.mock("../../app/public-share-save-api", () => ({ savePublicShare: mocks.save }));
vi.mock("../../app/auth-session", () => ({ getToken: () => "authenticated" }));
afterEach(() => {
  vi.clearAllMocks();
  window.history.replaceState(null, "", "/");
});

test("旧的单轮登录返回链接恢复为整份内容的保存弹窗，并清除恢复参数", async () => {
  window.history.replaceState(null, "", "/c/test?save=0");
  mocks.request.mockResolvedValue([{ id: "mine", name: "我的库", access: "owner" }]);
  render(<PublicShareSave kind="conversations" token="private-link" title="整份" expectedRevision="rev" />);
  await screen.findByRole("dialog", { name: "保存到我的笔记本" });
  await waitFor(() => expect(mocks.request).toHaveBeenCalledTimes(1));
  expect(await screen.findByLabelText("保存标题")).toHaveValue("整份");
  expect(screen.getByText("分享问答 · 全部公开轮次")).toBeInTheDocument();
  expect(window.location.search).toBe("");
});

test("只在弹窗中选择自己的笔记本，整份保存时阻止重复提交和关闭", async () => {
  const user = userEvent.setup();
  mocks.request.mockResolvedValue([{ id: "mine", name: "我的库", access: "owner" }, { id: "other", name: "共享只读库", access: "reader" }]);
  let finish!: (value: unknown) => void;
  mocks.save.mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
  render(<PublicShareSave kind="conversations" token="private-link" title="问题" expectedRevision="rev" />);
  expect(screen.queryByLabelText("目标笔记本")).toBeNull();
  expect(mocks.request).not.toHaveBeenCalled();
  await user.click(screen.getByRole("button", { name: "保存到我的笔记本" }));
  await screen.findByRole("option", { name: "我的库" });
  expect(screen.queryByRole("option", { name: "共享只读库" })).toBeNull();
  const dialog = screen.getByRole("dialog");
  const confirm = screen.getByRole("button", { name: "确认保存" });
  fireEvent.click(confirm);
  fireEvent.click(confirm);
  expect(mocks.save).toHaveBeenCalledTimes(1);
  expect(mocks.save).toHaveBeenCalledWith("conversations", "private-link", { notebook_id: "mine", title: "问题", expected_revision: "rev" });
  expect(screen.getByRole("button", { name: "保存中…" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "关闭" })).toBeDisabled();
  await user.keyboard("{Escape}");
  fireEvent.click(dialog);
  expect(dialog).toBeInTheDocument();
  await act(async () => finish({ notebook_id: "mine", source_id: "source", reused: false }));
  expect(await screen.findByRole("button", { name: "已保存" })).toBeInTheDocument();
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(screen.getByRole("link", { name: "打开笔记本" })).toHaveAttribute("href", "/#notebook=mine&source=source");
});

test("新建目标后保存整份报告，失败保留表单供重试", async () => {
  const user = userEvent.setup();
  mocks.request.mockResolvedValueOnce([]).mockResolvedValueOnce({ id: "new-id", name: "新库" });
  mocks.save.mockRejectedValueOnce(new Error("private diagnostic"));
  render(<PublicShareSave kind="reports" token="private-link" title="报告" expectedRevision="rev" />);
  await user.click(screen.getByRole("button", { name: "保存到我的笔记本" }));
  await user.type(await screen.findByLabelText("笔记本名称"), "新库");
  expect(screen.getByRole("button", { name: "确认保存" })).toBeEnabled();
  await user.click(screen.getByRole("button", { name: "确认保存" }));
  await waitFor(() => expect(mocks.save).toHaveBeenCalledWith("reports", "private-link", { notebook_id: "new-id", title: "报告", expected_revision: "rev" }));
  expect(await screen.findByText("保存失败，请重试。")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "确认保存" })).toBeEnabled();
  expect(screen.getByLabelText("目标笔记本")).toHaveValue("new-id");
  mocks.save.mockResolvedValueOnce({ notebook_id: "new-id", source_id: "source", reused: false });
  await user.click(screen.getByRole("button", { name: "确认保存" }));
  expect(await screen.findByRole("button", { name: "已保存" })).toBeInTheDocument();
  expect(mocks.request).toHaveBeenCalledTimes(2);
  expect(screen.queryByText("private diagnostic")).toBeNull();
});

test("弹窗内循环键盘焦点，取消、Escape 和遮罩关闭后焦点回到保存入口", async () => {
  const user = userEvent.setup();
  mocks.request.mockResolvedValue([{ id: "mine", name: "我的库", access: "owner" }]);
  render(<PublicShareSave kind="reports" token="link" title="报告" expectedRevision="rev" />);
  const trigger = screen.getByRole("button", { name: "保存到我的笔记本" });
  for (const close of ["cancel", "escape", "backdrop"]) {
    await user.click(trigger);
    const dialog = await screen.findByRole("dialog", { name: "保存到我的笔记本" });
    await screen.findByLabelText("保存标题");
    expect(dialog).toHaveFocus();
    await user.tab({ shift: true });
    expect(within(dialog).getByRole("button", { name: "确认保存" })).toHaveFocus();
    await user.tab();
    expect(within(dialog).getByRole("button", { name: "关闭" })).toHaveFocus();
    if (close === "cancel") await user.click(within(dialog).getByRole("button", { name: "取消" }));
    else if (close === "escape") await user.keyboard("{Escape}");
    else fireEvent.click(dialog);
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(trigger).toHaveFocus();
  }
});

test("加载失败可就地重试，已有笔记本可切换到新建，并编辑保存标题", async () => {
  const user = userEvent.setup();
  mocks.request.mockRejectedValueOnce(new Error("diagnostic")).mockResolvedValueOnce([{ id: "mine", name: "我的库", access: "owner" }]);
  render(<PublicShareSave kind="reports" token="link" title="报告" expectedRevision="rev" />);
  await user.click(screen.getByRole("button", { name: "保存到我的笔记本" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("笔记本加载失败，请重试。");
  expect(screen.getByRole("button", { name: "确认保存" })).toBeDisabled();
  await user.click(screen.getByRole("button", { name: "重新加载" }));
  await screen.findByLabelText("目标笔记本");
  await user.click(screen.getByRole("button", { name: "新建笔记本" }));
  expect(screen.getByRole("button", { name: "确认保存" })).toBeDisabled();
  await user.type(screen.getByLabelText("笔记本名称"), "新库");
  await user.clear(screen.getByLabelText("保存标题"));
  expect(screen.getByRole("button", { name: "确认保存" })).toBeDisabled();
  await user.type(screen.getByLabelText("保存标题"), "我的标题");
  expect(screen.getByRole("button", { name: "确认保存" })).toBeEnabled();
});

test("加载时取消弹窗不会被迟到的列表响应重新打开", async () => {
  const user = userEvent.setup();
  let finish!: (value: unknown) => void;
  mocks.request.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
  render(<PublicShareSave kind="reports" token="link" title="报告" expectedRevision="rev" />);
  const trigger = screen.getByRole("button", { name: "保存到我的笔记本" });
  await user.click(trigger);
  expect(screen.getByText("正在加载笔记本…")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "取消" }));
  await act(async () => finish([{ id: "mine", name: "迟到的库", access: "owner" }]));
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(trigger).toHaveFocus();
});
