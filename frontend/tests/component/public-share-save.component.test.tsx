import { fireEvent, render, screen, waitFor } from "@testing-library/react";
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

test("登录返回后仅恢复指定轮次的保存，并清除恢复参数", async () => {
  window.history.replaceState(null, "", "/c/test?save=0");
  mocks.request.mockResolvedValue([{ id: "mine", name: "我的库", access: "owner" }]);
  render(<>
    <PublicShareSave kind="conversations" token="private-link" title="整份" expectedRevision="rev" />
    <PublicShareSave kind="conversations" token="private-link" title="本轮" turnIndex={0} expectedRevision="rev" />
  </>);
  await screen.findByRole("group", { name: "保存到我的笔记本" });
  await waitFor(() => expect(mocks.request).toHaveBeenCalledTimes(1));
  expect(screen.getByLabelText("保存标题")).toHaveValue("本轮");
  expect(window.location.search).toBe("");
});

test("选择自己的笔记本，保存本轮时传精确轮次，进行中不能重复提交", async () => {
  const user = userEvent.setup();
  mocks.request.mockResolvedValue([{ id: "mine", name: "我的库", access: "owner" }, { id: "other", name: "共享只读库", access: "reader" }]);
  let finish!: (value: unknown) => void;
  mocks.save.mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
  render(<PublicShareSave kind="conversations" token="private-link" title="问题" turnIndex={1} expectedRevision="rev" />);
  await user.click(screen.getByRole("button", { name: "保存本轮" }));
  await screen.findByRole("option", { name: "我的库" });
  expect(screen.queryByRole("option", { name: "共享只读库" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "保存" }));
  expect(mocks.save).toHaveBeenCalledWith("conversations", "private-link", { notebook_id: "mine", title: "问题", turn_index: 1, expected_revision: "rev" });
  expect(screen.getByRole("button", { name: "保存中…" })).toBeDisabled();
  finish({ notebook_id: "mine", source_id: "source", reused: false });
  expect(await screen.findByRole("button", { name: "已保存" })).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "打开笔记本" })).toHaveAttribute("href", "/#notebook=mine&source=source");
});

test("新建目标后保存整份报告，失败保留表单供重试", async () => {
  const user = userEvent.setup();
  mocks.request.mockResolvedValueOnce([]).mockResolvedValueOnce({ id: "new-id", name: "新库" });
  mocks.save.mockRejectedValueOnce(new Error("private diagnostic"));
  render(<PublicShareSave kind="reports" token="private-link" title="报告" expectedRevision="rev" />);
  await user.click(screen.getByRole("button", { name: "保存到我的笔记本" }));
  await user.type(await screen.findByLabelText("笔记本名称"), "新库");
  await user.click(screen.getByRole("button", { name: "保存" }));
  await waitFor(() => expect(mocks.save).toHaveBeenCalledWith("reports", "private-link", { notebook_id: "new-id", title: "报告", expected_revision: "rev" }));
  expect(await screen.findByText("保存失败，请重试。")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "保存" })).toBeEnabled();
  expect(screen.queryByText("private diagnostic")).toBeNull();
});
