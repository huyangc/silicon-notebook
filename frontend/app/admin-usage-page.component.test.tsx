import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  fetchMe: vi.fn(),
  fetchAdminUsers: vi.fn(),
  fetchOnlineIds: vi.fn(),
  updateAdminUserRole: vi.fn(),
  fetchUserNotebooks: vi.fn(),
}));

vi.mock("./auth.ts", () => ({ fetchMe: mocks.fetchMe }));
vi.mock("./admin/usage/api.ts", () => ({
  FORBIDDEN_SENTINEL: "forbidden",
  fetchAdminUsers: mocks.fetchAdminUsers,
  fetchOnlineIds: mocks.fetchOnlineIds,
  updateAdminUserRole: mocks.updateAdminUserRole,
}));
vi.mock("./admin/usage/notebooks.ts", () => ({
  fetchUserNotebooks: mocks.fetchUserNotebooks,
  notebookStatusLabel: (value: string) => value,
}));

import AdminUsagePage from "./admin/usage/page";

const rows = [
  {
    id: "user-local",
    username: "admin",
    role: "admin",
    created_at: "2026-07-01T00:00:00",
    notebooks: 1,
    sources: 2,
    conversations: 3,
    reports: 4,
    last_active: null,
    is_online: false,
    role_mutable: false,
  },
  {
    id: "user-target",
    username: "a00123456",
    role: "user",
    created_at: "2026-07-02T00:00:00",
    notebooks: 0,
    sources: 0,
    conversations: 0,
    reports: 0,
    last_active: null,
    is_online: false,
    role_mutable: true,
  },
];

test("管理员可在用户总览中二次确认并授予管理员权限", async () => {
  mocks.fetchMe.mockResolvedValue({ id: "user-local", role: "admin" });
  mocks.fetchAdminUsers.mockResolvedValue(rows);
  mocks.fetchOnlineIds.mockResolvedValue([]);
  mocks.updateAdminUserRole.mockResolvedValue({
    id: "user-target",
    username: "a00123456",
    role: "admin",
  });
  const user = userEvent.setup();

  render(<AdminUsagePage />);
  const targetName = await screen.findByText("a00123456");
  const targetRow = targetName.closest("tr");
  expect(targetRow).not.toBeNull();
  const target = within(targetRow as HTMLTableRowElement);

  await user.click(target.getByRole("button", { name: "设为管理员" }));
  expect(mocks.updateAdminUserRole).not.toHaveBeenCalled();
  await user.click(target.getByRole("button", { name: "确认" }));

  expect(await screen.findByText("已授予 a00123456 管理员权限")).toBeInTheDocument();
  expect(mocks.updateAdminUserRole).toHaveBeenCalledWith("user-target", "admin");
  expect(target.getByRole("button", { name: "撤销管理员" })).toBeInTheDocument();
  const builtinRow = screen.getByText("admin").closest("tr");
  expect(within(builtinRow as HTMLTableRowElement).getByText("当前账户")).toBeInTheDocument();
});
