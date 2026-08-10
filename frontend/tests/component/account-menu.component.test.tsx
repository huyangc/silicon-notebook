import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";

import { AccountMenu } from "../../app/account-menu";


function renderMenu(onLogout = vi.fn(), advancedMode = false, onToggleAdvancedMode = vi.fn()) {
  render(
    <AccountMenu
      username="admin"
      role="admin"
      initials="AD"
      memoryActive={false}
      showAdminUsage
      advancedMode={advancedMode}
      onOpenMemory={() => undefined}
      onToggleAdvancedMode={onToggleAdvancedMode}
      onLogout={onLogout}
    />,
  );
  return { onLogout, onToggleAdvancedMode };
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
