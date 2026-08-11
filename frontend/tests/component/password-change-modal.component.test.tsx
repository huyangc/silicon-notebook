import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  changeMyPassword: vi.fn(),
}));

vi.mock("../../app/auth.ts", () => ({ changeMyPassword: mocks.changeMyPassword }));

import { humanizedError } from "../../app/errors.ts";
import { PasswordChangeModal } from "../../app/password-change-modal";

test("两次输入的新密码不一致时不发请求，并显示提示", async () => {
  const user = userEvent.setup();
  render(<PasswordChangeModal onClose={() => undefined} />);

  await user.type(screen.getByLabelText("当前密码"), "old-pass");
  await user.type(screen.getByLabelText("新密码"), "new-pass-1");
  await user.type(screen.getByLabelText("确认新密码"), "new-pass-2");
  await user.click(screen.getByRole("button", { name: "确认修改" }));

  expect(await screen.findByText("两次输入的新密码不一致")).toBeInTheDocument();
  expect(mocks.changeMyPassword).not.toHaveBeenCalled();
});

test("空白新密码在客户端拦截，不发请求", async () => {
  const user = userEvent.setup();
  render(<PasswordChangeModal onClose={() => undefined} />);

  await user.type(screen.getByLabelText("当前密码"), "old-pass");
  await user.type(screen.getByLabelText("新密码"), "   ");
  await user.type(screen.getByLabelText("确认新密码"), "   ");
  await user.click(screen.getByRole("button", { name: "确认修改" }));

  expect(await screen.findByText("新密码不能为空")).toBeInTheDocument();
  expect(mocks.changeMyPassword).not.toHaveBeenCalled();
});

test("提交中按钮立即禁用并显示进行态文案", async () => {
  // 长任务按钮红线:点击后立即不可点 + 进行态文案。用挂起的 promise 卡住请求窗口。
  let release: () => void = () => undefined;
  mocks.changeMyPassword.mockImplementation(
    () => new Promise<void>((resolve) => { release = resolve; }),
  );
  const user = userEvent.setup();
  render(<PasswordChangeModal onClose={() => undefined} />);

  await user.type(screen.getByLabelText("当前密码"), "old-pass");
  await user.type(screen.getByLabelText("新密码"), "new-pass-1");
  await user.type(screen.getByLabelText("确认新密码"), "new-pass-1");
  await user.click(screen.getByRole("button", { name: "确认修改" }));

  const busyButton = await screen.findByRole("button", { name: "保存中…" });
  expect(busyButton).toBeDisabled();
  expect(screen.getByRole("button", { name: "取消" })).toBeDisabled();
  release();
  expect(await screen.findByText("密码已修改。其他设备将需要重新登录。")).toBeInTheDocument();
});

test("成功路径:调用 changeMyPassword 并显示成功态", async () => {
  mocks.changeMyPassword.mockResolvedValue(undefined);
  const user = userEvent.setup();
  const onClose = vi.fn();
  render(<PasswordChangeModal onClose={onClose} />);

  await user.type(screen.getByLabelText("当前密码"), "old-pass");
  await user.type(screen.getByLabelText("新密码"), "new-pass-1");
  await user.type(screen.getByLabelText("确认新密码"), "new-pass-1");
  await user.click(screen.getByRole("button", { name: "确认修改" }));

  expect(mocks.changeMyPassword).toHaveBeenCalledWith("old-pass", "new-pass-1");
  expect(await screen.findByText("密码已修改。其他设备将需要重新登录。")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "关闭" })).toBeInTheDocument();
  expect(onClose).not.toHaveBeenCalled();
});

test("失败路径:显示人话层错误文案，弹窗保持打开", async () => {
  mocks.changeMyPassword.mockRejectedValue(humanizedError("当前密码不正确"));
  const user = userEvent.setup();
  render(<PasswordChangeModal onClose={() => undefined} />);

  await user.type(screen.getByLabelText("当前密码"), "wrong-pass");
  await user.type(screen.getByLabelText("新密码"), "new-pass-1");
  await user.type(screen.getByLabelText("确认新密码"), "new-pass-1");
  await user.click(screen.getByRole("button", { name: "确认修改" }));

  expect(await screen.findByText("当前密码不正确")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "确认修改" })).toBeInTheDocument();
});
