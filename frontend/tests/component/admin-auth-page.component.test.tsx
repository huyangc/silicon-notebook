import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
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

afterEach(() => { vi.useRealTimers(); });

function actionSection(name: string) {
  return within(screen.getByRole("heading", { name }).closest("section")!);
}

test.each(["success", "failure"])("maintenance %s stays beside its controls, blocks duplicate requests and clears itself", async (outcome) => {
  const pending = deferred<AuthPolicy>();
  api.prepareAuthProviderMaintenance.mockReturnValue(pending.promise);
  render(<AdminAuthPage />);
  await screen.findByText("first-account");
  const section = actionSection("认证提供方维护");
  const button = section.getByRole("button", { name: "准备维护" });
  vi.useFakeTimers();
  act(() => { button.click(); button.click(); });
  expect(button).toBeDisabled();
  expect(api.prepareAuthProviderMaintenance).toHaveBeenCalledTimes(1);
  await act(async () => {
    if (outcome === "success") pending.resolve({ ...policy, revision: 2 });
    else pending.reject(new TypeError("offline"));
  });
  const role = outcome === "success" ? "status" : "alert";
  expect(section.getByRole(role)).toHaveTextContent(outcome === "success" ? "已进入认证提供方维护准备状态" : "维护准备失败");
  expect(screen.getAllByRole(role).filter((element) => element.textContent?.includes(outcome === "success" ? "已进入" : "维护准备失败"))).toHaveLength(1);
  expect(button).toBeEnabled();
  act(() => { vi.runOnlyPendingTimers(); });
  expect(section.queryByRole(role)).not.toBeInTheDocument();
});

test("policy and grant validation and server failures stay next to their own controls", async () => {
  api.updateAuthPolicy.mockRejectedValue(new TypeError("offline"));
  api.issueAuthGrant.mockRejectedValue(new TypeError("offline"));
  render(<AdminAuthPage />);
  await screen.findByText("first-account");
  const actor = userEvent.setup();
  const policies = actionSection("认证策略");
  await actor.selectOptions(policies.getByLabelText("目标模式"), "retired");
  await actor.click(policies.getByRole("button", { name: "更新策略" }));
  expect(policies.getByRole("alert")).toHaveTextContent("请输入“退役本地凭据”");
  expect(api.updateAuthPolicy).not.toHaveBeenCalled();
  await actor.selectOptions(policies.getByLabelText("目标模式"), "sso_only");
  await actor.click(policies.getByRole("button", { name: "更新策略" }));
  expect(policies.getByRole("alert")).toHaveTextContent("认证策略更新失败");
  const grants = actionSection("签发迁移凭证");
  await actor.click(grants.getByRole("button", { name: "签发凭证" }));
  expect(grants.getByRole("alert")).toHaveTextContent("请填写迁移凭证的使用人标识");
  expect(api.issueAuthGrant).not.toHaveBeenCalled();
  await actor.type(grants.getByLabelText("使用人标识"), "person");
  await actor.click(grants.getByRole("button", { name: "签发凭证" }));
  expect(grants.getByRole("alert")).toHaveTextContent("签发迁移凭证失败");
});

test("successful policy and grant actions show local results while the issued credential remains available", async () => {
  const updated = { ...policy, mode: "sso_only", revision: 2 };
  api.updateAuthPolicy.mockResolvedValue(updated);
  api.issueAuthGrant.mockResolvedValue({ grant_token: "one-time-grant", purpose: "enroll", expires_in: 300 });
  render(<AdminAuthPage />);
  await screen.findByText("first-account");
  api.fetchAuthPolicy.mockResolvedValue(updated);
  const actor = userEvent.setup();
  const policies = actionSection("认证策略");
  await actor.selectOptions(policies.getByLabelText("目标模式"), "sso_only");
  await actor.click(policies.getByRole("button", { name: "更新策略" }));
  expect(policies.getByRole("status")).toHaveTextContent("认证策略已更新");
  const grants = actionSection("签发迁移凭证");
  await actor.type(grants.getByLabelText("使用人标识"), "person");
  vi.useFakeTimers();
  await act(async () => { grants.getByRole("button", { name: "签发凭证" }).click(); });
  expect(grants.getByRole("status")).toHaveTextContent("已签发迁移凭证");
  act(() => { vi.runOnlyPendingTimers(); });
  expect(grants.queryByRole("status")).not.toBeInTheDocument();
  expect(grants.getByText("one-time-grant")).toBeInTheDocument();
});

test.each(["success", "failure", "refresh failure"])("account mutation %s reports only at the affected row", async (outcome) => {
  const initial = page(0, "first-account");
  initial.items.push({ ...initial.items[0], id: "other-account", display_name: "other-account" });
  api.fetchAuthAccounts.mockResolvedValue(initial);
  render(<AdminAuthPage />);
  await screen.findByText("first-account");
  if (outcome === "failure") api.updateAuthAccountStatus.mockRejectedValue(new TypeError("offline"));
  else if (outcome === "refresh failure") api.fetchAuthAccounts.mockRejectedValue(new TypeError("offline"));
  else api.fetchAuthAccounts.mockResolvedValue({ ...initial, items: initial.items.map((account) => account.id === "first-account" ? { ...account, status: "disabled" } : account) });
  const row = within(screen.getByText("first-account").closest("tr")!);
  await userEvent.setup().click(row.getByRole("button", { name: "停用" }));
  expect(row.getByRole(outcome === "success" ? "status" : "alert")).toHaveTextContent(
    outcome === "success" ? "已停用该账号" : outcome === "failure" ? "账号状态更新失败" : "已停用该账号。但列表刷新失败",
  );
  const other = within(screen.getByText("other-account").closest("tr")!);
  expect(other.queryByRole("status")).not.toBeInTheDocument();
  expect(other.queryByRole("alert")).not.toBeInTheDocument();
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
