import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";
import type { AuthAccountPage, AuthPolicy } from "../../app/admin/auth/api";

const api = vi.hoisted(() => ({
  fetchAuthAccounts: vi.fn(), fetchAuthAudit: vi.fn(), fetchAuthMigration: vi.fn(),
  fetchAuthPolicy: vi.fn(), issueAuthGrant: vi.fn(), prepareAuthProviderMaintenance: vi.fn(),
  updateAuthAccountStatus: vi.fn(), updateAuthPolicy: vi.fn(),
}));
vi.mock("../../app/admin/auth/api", () => api);
vi.mock("../../app/auth", () => ({ fetchMe: async () => ({ id: "admin", role: "admin" }) }));

import AdminAuthPage from "../../app/admin/auth/page";

const policy: AuthPolicy = {
  mode: "dual", revision: 1, provider_id: "corp", provider_namespace: "corp.production",
  plugin_id: "corp", config_generation: "generation-1", retired_at: null, updated_by: "admin",
};

function page(offset: number, name: string, status: "active" | "disabled" = "active"): AuthAccountPage {
  return { offset, limit: 50, total: 120, items: [{
    id: name, username: name, display_name: name, status,
    local_login_name: name, provider_namespace: null, subject: null,
    identity_status: null, last_login_at: null,
  }] };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function accountSection() {
  return within(screen.getByRole("heading", { name: "账号状态" }).closest("section")!);
}

beforeEach(() => {
  vi.resetAllMocks();
  api.fetchAuthPolicy.mockResolvedValue(policy);
  api.fetchAuthMigration.mockResolvedValue({ policy, active_users: 120, unready_users: 1, ready_admins: 2, ready: false });
  api.fetchAuthAccounts.mockResolvedValue(page(0, "first-account"));
  api.fetchAuthAudit.mockResolvedValue({ items: [], offset: 0, limit: 100, total: 0 });
  api.updateAuthAccountStatus.mockResolvedValue({});
});

test("account pagination blocks duplicate clicks and retries the failed destination beside the controls", async () => {
  const pending = deferred<AuthAccountPage>();
  api.fetchAuthAccounts.mockResolvedValueOnce(page(0, "first-account"))
    .mockReturnValueOnce(pending.promise).mockResolvedValueOnce(page(50, "second-account"));
  render(<AdminAuthPage />);
  await screen.findByText("first-account");
  const section = accountSection();
  const next = section.getByRole("button", { name: "下一页" });
  act(() => { next.click(); next.click(); });
  expect(next).toBeDisabled();
  expect(section.getByRole("status")).toHaveTextContent("正在加载账号列表");
  expect(api.fetchAuthAccounts).toHaveBeenCalledTimes(2);
  await act(async () => pending.reject(new TypeError("offline")));
  expect(section.getByRole("alert")).toHaveTextContent("加载账号列表失败");
  expect(section.getByText("first-account")).toBeInTheDocument();

  await userEvent.setup().click(section.getByRole("button", { name: "重试加载账号" }));
  await screen.findByText("second-account");
  expect(api.fetchAuthAccounts.mock.calls.map(([offset]) => offset)).toEqual([0, 50, 50]);
  expect(section.queryByRole("alert")).not.toBeInTheDocument();
  expect(section.getByRole("status")).toHaveTextContent("第 2 页");
  // Paging does not reset a separately edited policy/maintenance form.
  expect(api.fetchAuthPolicy).toHaveBeenCalledTimes(1);
});

test.each(["success", "failure"])("a stale page %s cannot replace a newer post-mutation account refresh", async (outcome) => {
  const oldPage = deferred<AuthAccountPage>();
  api.fetchAuthAccounts.mockResolvedValueOnce(page(0, "first-account"))
    .mockReturnValueOnce(oldPage.promise).mockResolvedValueOnce(page(0, "updated-account", "disabled"));
  render(<AdminAuthPage />);
  await screen.findByText("first-account");
  const section = accountSection();
  const actor = userEvent.setup();
  await actor.click(section.getByRole("button", { name: "下一页" }));
  await actor.click(section.getByRole("button", { name: "停用" }));
  await screen.findByText("updated-account");
  await act(async () => {
    if (outcome === "success") oldPage.resolve(page(50, "obsolete-account"));
    else oldPage.reject(new TypeError("obsolete failure"));
  });
  expect(section.getByText("updated-account")).toBeInTheDocument();
  expect(section.queryByText("obsolete-account")).not.toBeInTheDocument();
  expect(section.queryByRole("alert")).not.toBeInTheDocument();
  expect(section.getByRole("status")).toHaveTextContent("第 1 页");
  await waitFor(() => expect(section.getByRole("button", { name: "下一页" })).toBeEnabled());
});
