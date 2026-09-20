import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";

import { IdentityBindingConfirmation } from "../../app/identity-binding-confirmation";

test("binding preview makes the authoritative name and preserved business identity explicit", async () => {
  const actor = userEvent.setup();
  const onConfirm = vi.fn();
  const onCancel = vi.fn();
  render(<IdentityBindingConfirmation
    pending={{ status: "binding_required", pending_id: "p", local_login_name: "a12345678", external_username: "alice", display_name: "Alice" }}
    busy={false}
    onConfirm={onConfirm}
    onCancel={onCancel}
  />);

  expect(screen.getByText("a12345678")).toBeInTheDocument();
  expect(screen.getByText("alice")).toBeInTheDocument();
  expect(screen.getByText(/用户 ID、笔记本、角色、群组和历史数据不会改变/)).toBeInTheDocument();
  await actor.click(screen.getByRole("button", { name: "取消" }));
  expect(onCancel).toHaveBeenCalledOnce();
  await actor.click(screen.getByRole("button", { name: "确认关联" }));
  expect(onConfirm).toHaveBeenCalledOnce();
});

test("grant recovery confirmation discloses recovery rather than silently creating a user", () => {
  render(<IdentityBindingConfirmation
    pending={{ status: "confirmation_required", pending_id: "p", external_username: "alice", display_name: "Alice", purpose: "recover", target_user_id: "u1", target_username: "原账号" }}
    busy={false}
    onConfirm={() => undefined}
    onCancel={() => undefined}
  />);

  expect(screen.getByText("确认后将恢复原有账号及其业务数据。")).toBeInTheDocument();
  expect(screen.queryByText("本地登录名")).not.toBeInTheDocument();
});

test("replacement confirmation makes the preserved original account and revoked old identity explicit", () => {
  render(<IdentityBindingConfirmation
    pending={{ status: "confirmation_required", pending_id: "p", external_username: "alice-new", display_name: "Alice", purpose: "replace", target_user_id: "u1", target_username: "原账号", previous_external_username: "alice-old" }}
    busy={false}
    onConfirm={() => undefined}
    onCancel={() => undefined}
  />);

  expect(screen.getByRole("heading", { name: "更换统一账号" })).toBeInTheDocument();
  expect(screen.getByText("alice-new")).toBeInTheDocument();
  expect(screen.getAllByText("原账号")).toHaveLength(2);
  expect(screen.getByText("alice-old")).toBeInTheDocument();
  expect(screen.getByText(/用户 ID、业务数据和角色保持不变/)).toBeInTheDocument();
  expect(screen.getByText(/旧统一身份和会话将不能继续使用/)).toBeInTheDocument();
});
