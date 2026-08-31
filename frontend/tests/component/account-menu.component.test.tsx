import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";

import { AccountMenu } from "../../app/account-menu";


function renderMenu(
  onLogout = vi.fn(),
  advancedMode = false,
  onToggleAdvancedMode = vi.fn(),
  {
    canChangePassword = true,
    onChangePassword = vi.fn(),
    searchProfileEnabled = true,
    activityViewEnabled = true,
    onOpenSearchProfile = vi.fn(),
  } = {},
) {
  render(
    <AccountMenu
      username="admin"
      role="admin"
      initials="AD"
      memoryActive={false}
      showAdminUsage
      canChangePassword={canChangePassword}
      advancedMode={advancedMode}
      searchProfileEnabled={searchProfileEnabled}
      activityViewEnabled={activityViewEnabled}
      onOpenMemory={() => undefined}
      onOpenGroups={() => undefined}
      onToggleAdvancedMode={onToggleAdvancedMode}
      onOpenSearchProfile={onOpenSearchProfile}
      onChangePassword={onChangePassword}
      onLogout={onLogout}
    />,
  );
  return { onLogout, onToggleAdvancedMode, onChangePassword, onOpenSearchProfile };
}


test("account menu is accessible and closes on Escape or outside click", async () => {
  const user = userEvent.setup();
  renderMenu();
  const trigger = screen.getByRole("button", { name: "账户菜单" });

  expect(trigger).toHaveAttribute("aria-expanded", "false");
  expect(screen.queryByRole("menu")).not.toBeInTheDocument();

  await user.click(trigger);
  expect(trigger).toHaveAttribute("aria-expanded", "true");
  expect(screen.getByRole("menu", { name: "账户菜单" })).toBeInTheDocument();

  await user.keyboard("{Escape}");
  expect(screen.queryByRole("menu")).not.toBeInTheDocument();

  await user.click(trigger);
  await user.click(document.body);
  expect(screen.queryByRole("menu")).not.toBeInTheDocument();
});


test("logout is available as a menu action", async () => {
  const user = userEvent.setup();
  const { onLogout } = renderMenu();

  await user.click(screen.getByRole("button", { name: "账户菜单" }));
  await user.click(screen.getByRole("menuitem", { name: "退出登录" }));

  expect(onLogout).toHaveBeenCalledOnce();
});


test("修改密码是菜单动作,点击关闭菜单并触发回调", async () => {
  const user = userEvent.setup();
  const { onChangePassword } = renderMenu();

  await user.click(screen.getByRole("button", { name: "账户菜单" }));
  await user.click(screen.getByRole("menuitem", { name: "修改密码" }));

  expect(onChangePassword).toHaveBeenCalledOnce();
  expect(screen.queryByRole("menu")).not.toBeInTheDocument();
});


test("内置管理员(canChangePassword=false)不显示修改密码入口", async () => {
  const user = userEvent.setup();
  renderMenu(vi.fn(), false, vi.fn(), { canChangePassword: false });

  await user.click(screen.getByRole("button", { name: "账户菜单" }));
  expect(screen.queryByRole("menuitem", { name: "修改密码" })).not.toBeInTheDocument();
});


test("高级模式开关显示当前状态并在点击时触发回调", async () => {
  const user = userEvent.setup();
  const { onToggleAdvancedMode } = renderMenu(vi.fn(), false);

  await user.click(screen.getByRole("button", { name: "账户菜单" }));
  const toggle = screen.getByRole("menuitemcheckbox", { name: /高级模式/ });
  expect(toggle).toHaveAttribute("aria-checked", "false");
  expect(toggle).toHaveTextContent("已关闭");

  await user.click(toggle);
  expect(onToggleAdvancedMode).toHaveBeenCalledOnce();
});


test("高级模式开启时显示已开启状态", async () => {
  const user = userEvent.setup();
  renderMenu(vi.fn(), true);

  await user.click(screen.getByRole("button", { name: "账户菜单" }));
  const toggle = screen.getByRole("menuitemcheckbox", { name: /高级模式/ });
  expect(toggle).toHaveAttribute("aria-checked", "true");
  expect(toggle).toHaveTextContent("已开启");
});

test("「我的回答偏好」是菜单动作,点击关闭菜单并触发回调", async () => {
  const user = userEvent.setup();
  const { onOpenSearchProfile } = renderMenu();

  await user.click(screen.getByRole("button", { name: "账户菜单" }));
  await user.click(screen.getByRole("menuitem", { name: "我的回答偏好" }));

  expect(onOpenSearchProfile).toHaveBeenCalledOnce();
  expect(screen.queryByRole("menu")).not.toBeInTheDocument();
});

test("部署总闸关闭(searchProfileEnabled=false)时不显示「我的回答偏好」入口", async () => {
  const user = userEvent.setup();
  renderMenu(vi.fn(), false, vi.fn(), { searchProfileEnabled: false });

  await user.click(screen.getByRole("button", { name: "账户菜单" }));
  expect(screen.queryByRole("menuitem", { name: "我的回答偏好" })).not.toBeInTheDocument();
});

test("菜单提供许愿墙与管理员提问分析入口", async () => {
  const user = userEvent.setup();
  renderMenu();

  await user.click(screen.getByRole("button", { name: "账户菜单" }));

  expect(screen.getByRole("menuitem", { name: "许愿墙" })).toHaveAttribute("href", "/wishes");
  expect(screen.getByRole("menuitem", { name: "提问分析" })).toHaveAttribute("href", "/admin/questions");
});

test("提问分析能力关闭时隐藏该入口但保留用户总览", async () => {
  const user = userEvent.setup();
  renderMenu(vi.fn(), false, vi.fn(), { activityViewEnabled: false });

  await user.click(screen.getByRole("button", { name: "账户菜单" }));

  expect(screen.queryByRole("menuitem", { name: "提问分析" })).not.toBeInTheDocument();
  expect(screen.getByRole("menuitem", { name: "用户总览" })).toBeInTheDocument();
});
