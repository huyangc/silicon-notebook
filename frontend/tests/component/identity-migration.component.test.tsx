import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";

import { AccountMenu } from "../../app/account-menu";
import { IdentityBindingConfirmation } from "../../app/identity-binding-confirmation";
import { IdentityMigrationForm } from "../../app/identity-migration-form";

function deferred() {
  let resolve!: () => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<void>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function renderMenu(canMigrateIdentity: boolean, onMigrateIdentity = vi.fn(async () => undefined)) {
  render(
    <AccountMenu
      username="e12345678" role="user" initials="E" memoryActive={false} showAdminUsage={false}
      canChangePassword={false} advancedMode={false} searchProfileEnabled activityViewEnabled
      onOpenMemory={() => undefined} onOpenGroups={() => undefined} onToggleAdvancedMode={() => undefined}
      onOpenSearchProfile={() => undefined} onChangePassword={() => undefined}
      canBindIdentity={false} linkedIdentityName="e12345678" onStartIdentityBinding={async () => undefined}
      canMigrateIdentity={canMigrateIdentity} onMigrateIdentity={onMigrateIdentity}
      onLogout={() => undefined}
    />,
  );
  return onMigrateIdentity;
}

test("account menu offers legacy migration only when the server says it is available", async () => {
  const actor = userEvent.setup();
  renderMenu(false);
  await actor.click(screen.getByRole("button", { name: "账户菜单" }));
  expect(screen.queryByRole("menuitem", { name: "迁移旧账号" })).not.toBeInTheDocument();
});

test("migration submits the old login name and password and reports success inside the form", async () => {
  const actor = userEvent.setup();
  const pending = deferred();
  const onMigrate = renderMenu(true, vi.fn(() => pending.promise));
  await actor.click(screen.getByRole("button", { name: "账户菜单" }));
  await actor.click(screen.getByRole("menuitem", { name: "迁移旧账号" }));
  const form = screen.getByRole("form", { name: "迁移旧账号" });
  await actor.type(screen.getByLabelText("旧账号登录名"), " a12345678 ");
  await actor.type(screen.getByLabelText("旧账号密码"), "old-password");
  await actor.click(screen.getByRole("button", { name: "迁移" }));
  expect(onMigrate).toHaveBeenCalledWith("a12345678", "old-password");
  expect(screen.getByRole("button", { name: "正在迁移…" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "取消" })).toBeEnabled();
  pending.resolve();
  await waitFor(() => expect(form).toHaveTextContent("迁移完成，正在切换到旧账号…"));
});

test("migration failure stays in the form and the form can be closed while a request is in flight", async () => {
  const actor = userEvent.setup();
  const onClose = vi.fn();
  const failing = vi.fn(async () => { throw new Error("boom"); });
  const { unmount } = render(<IdentityMigrationForm onSubmit={failing} onClose={onClose} />);
  await actor.click(screen.getByRole("button", { name: "迁移" }));
  expect(screen.getByRole("alert")).toHaveTextContent("请输入旧账号登录名和密码");
  expect(failing).not.toHaveBeenCalled();
  await actor.type(screen.getByLabelText("旧账号登录名"), "a12345678");
  await actor.type(screen.getByLabelText("旧账号密码"), "wrong");
  await actor.click(screen.getByRole("button", { name: "迁移" }));
  await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("迁移失败，请稍后重试"));
  expect(screen.getByRole("button", { name: "迁移" })).toBeEnabled();
  unmount();

  const pending = deferred();
  render(<IdentityMigrationForm onSubmit={() => pending.promise} onClose={onClose} />);
  await actor.type(screen.getByLabelText("旧账号登录名"), "a12345678");
  await actor.type(screen.getByLabelText("旧账号密码"), "pw");
  await actor.click(screen.getByRole("button", { name: "迁移" }));
  await actor.click(screen.getByRole("button", { name: "取消" }));
  expect(onClose).toHaveBeenCalledOnce();
});

const autoEnroll = {
  status: "confirmation_required" as const, pending_id: "p", external_username: "e12345678",
  display_name: "张三", purpose: "auto_enroll" as const, target_user_id: null, target_username: null,
};

test("auto enrollment names the employee number and warns that old data is not carried over", async () => {
  const actor = userEvent.setup();
  const onConfirm = vi.fn();
  render(<IdentityBindingConfirmation pending={autoEnroll} busy={false} onConfirm={onConfirm} onCancel={() => undefined} localLoginAllowed />);
  expect(screen.getByRole("heading", { name: "确认新建账号" })).toBeInTheDocument();
  expect(screen.getByText(/系统中没有与工号 e12345678 关联的账号。继续将为你新建一个本站账号/)).toBeInTheDocument();
  expect(screen.getByText(/之前在本站的笔记本等数据不会出现在新账号里/)).toBeInTheDocument();
  await actor.click(screen.getByRole("button", { name: "确认新建账号" }));
  expect(onConfirm).toHaveBeenCalledOnce();
});

test("an existing old account is sent to local sign-in when local login is allowed", async () => {
  const actor = userEvent.setup();
  const onConfirm = vi.fn();
  const onCancel = vi.fn();
  render(<IdentityBindingConfirmation pending={autoEnroll} busy={false} onConfirm={onConfirm} onCancel={onCancel} localLoginAllowed />);
  await actor.click(screen.getByRole("button", { name: "我已有本站旧账号" }));
  expect(screen.getByText(/用本站旧账号登录，登录后在右上角账户菜单选择「关联统一身份」/)).toBeInTheDocument();
  await actor.click(screen.getByRole("button", { name: "去本站登录" }));
  expect(onCancel).toHaveBeenCalledOnce();
  expect(onConfirm).not.toHaveBeenCalled();
});

test("an existing old account is sent to an administrator when local login is closed", async () => {
  const actor = userEvent.setup();
  const onConfirm = vi.fn();
  const onCancel = vi.fn();
  render(<IdentityBindingConfirmation pending={autoEnroll} busy={false} onConfirm={onConfirm} onCancel={onCancel} localLoginAllowed={false} />);
  await actor.click(screen.getByRole("button", { name: "我已有本站旧账号" }));
  expect(screen.getByRole("status")).toHaveTextContent("请联系管理员迁移旧账号");
  expect(screen.queryByRole("button", { name: "去本站登录" })).not.toBeInTheDocument();
  await actor.click(screen.getByRole("button", { name: "返回" }));
  expect(screen.getByRole("button", { name: "确认新建账号" })).toBeInTheDocument();
  expect(onConfirm).not.toHaveBeenCalled();
  expect(onCancel).not.toHaveBeenCalled();
});
