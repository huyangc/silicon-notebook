import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";

import { AccountMenu } from "../../app/account-menu";


function renderMenu(onLogout = vi.fn()) {
  render(
    <AccountMenu
      username="admin"
      role="admin"
      initials="AD"
      memoryActive={false}
      showAdminUsage
      onOpenMemory={() => undefined}
      onLogout={onLogout}
    />,
  );
  return onLogout;
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
  const onLogout = renderMenu();

  await user.click(screen.getByRole("button", { name: "账户菜单" }));
  await user.click(screen.getByRole("menuitem", { name: "退出登录" }));

  expect(onLogout).toHaveBeenCalledOnce();
});
