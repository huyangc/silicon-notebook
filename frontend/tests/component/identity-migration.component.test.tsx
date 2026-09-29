import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";

import { AccountMenu } from "../../app/account-menu";
import { performApiRequest } from "../../app/api-client";
import { getToken, setToken, clearToken } from "../../app/auth-session";
import type { AuthUser } from "../../app/auth";
import { IdentityBindingConfirmation } from "../../app/identity-binding-confirmation";
import { IdentityMigrationForm } from "../../app/identity-migration-form";
import { useIdentityMigration } from "../../app/use-identity-migration";

afterEach(() => { clearToken(); vi.unstubAllGlobals(); });

type MigrationResult = { token: string; user: AuthUser };

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

/** The page's wiring: the hook owns single-flight, the menu only renders it. */
function MigrationHarness({ migrate, reload, canMigrateIdentity = true }: {
  migrate: (loginName: string, password: string) => Promise<MigrationResult>;
  reload: () => void;
  canMigrateIdentity?: boolean;
}) {
  const identityMigration = useIdentityMigration({ migrate, reload });
  return (
    <AccountMenu
      username="e12345678" role="user" initials="E" memoryActive={false} showAdminUsage={false}
      canChangePassword={false} advancedMode={false} searchProfileEnabled activityViewEnabled
      onOpenMemory={() => undefined} onOpenGroups={() => undefined} onToggleAdvancedMode={() => undefined}
      onOpenSearchProfile={() => undefined} onChangePassword={() => undefined}
      canBindIdentity={false} linkedIdentityName="e12345678" onStartIdentityBinding={async () => undefined}
      canMigrateIdentity={canMigrateIdentity} onMigrateIdentity={identityMigration.run}
      migrationInFlight={identityMigration.inFlight}
      onLogout={() => undefined}
    />
  );
}

async function openMigrationForm(actor: ReturnType<typeof userEvent.setup>) {
  if (!screen.queryByRole("menu")) await actor.click(screen.getByRole("button", { name: "账户菜单" }));
  // The menu remembers an expanded form across closing its popover.
  if (!screen.queryByRole("form", { name: "迁移旧账号" })) {
    await actor.click(screen.getByRole("menuitem", { name: "迁移旧账号" }));
  }
  return screen.getByRole("form", { name: "迁移旧账号" });
}

async function submitMigration(actor: ReturnType<typeof userEvent.setup>) {
  await actor.type(screen.getByLabelText("旧账号登录名"), " a12345678 ");
  await actor.type(screen.getByLabelText("旧账号密码"), "old-password");
  await actor.click(screen.getByRole("button", { name: "迁移" }));
}

const migrated: MigrationResult = { token: "legacy-account-token", user: { id: "legacy" } as AuthUser };

test("account menu offers legacy migration only when the server says it is available", async () => {
  const actor = userEvent.setup();
  render(<MigrationHarness migrate={vi.fn()} reload={vi.fn()} canMigrateIdentity={false} />);
  await actor.click(screen.getByRole("button", { name: "账户菜单" }));
  expect(screen.queryByRole("menuitem", { name: "迁移旧账号" })).not.toBeInTheDocument();
});

test("closing the form or the menu mid-flight keeps the migration in progress and blocks a second submit", async () => {
  const actor = userEvent.setup();
  setToken("auto-account-token");
  const pending = deferred<MigrationResult>();
  const migrate = vi.fn(() => pending.promise);
  const reload = vi.fn();
  render(<MigrationHarness migrate={migrate} reload={reload} />);
  await openMigrationForm(actor);
  await submitMigration(actor);
  expect(migrate).toHaveBeenCalledWith("a12345678", "old-password");
  expect(screen.getByRole("button", { name: "正在迁移…" })).toBeDisabled();

  await actor.click(screen.getByRole("button", { name: "取消" }));
  expect(screen.queryByRole("form", { name: "迁移旧账号" })).not.toBeInTheDocument();
  await openMigrationForm(actor);
  expect(screen.getByRole("button", { name: "正在迁移…" })).toBeDisabled();
  expect(screen.getByLabelText("旧账号登录名")).toBeDisabled();

  await actor.keyboard("{Escape}");
  expect(screen.queryByRole("menu")).not.toBeInTheDocument();
  await openMigrationForm(actor);
  expect(screen.getByRole("button", { name: "正在迁移…" })).toBeDisabled();
  await actor.click(screen.getByRole("button", { name: "正在迁移…" }));
  expect(migrate).toHaveBeenCalledOnce();

  pending.resolve(migrated);
  await waitFor(() => expect(reload).toHaveBeenCalledOnce());
  expect(getToken()).toBe("legacy-account-token");
});

test("a success reported inside the form switches the session and reloads", async () => {
  const actor = userEvent.setup();
  setToken("auto-account-token");
  const reload = vi.fn();
  render(<MigrationHarness migrate={vi.fn(async () => migrated)} reload={reload} />);
  const form = await openMigrationForm(actor);
  await submitMigration(actor);
  await waitFor(() => expect(form).toHaveTextContent("迁移完成，正在切换到旧账号…"));
  expect(getToken()).toBe("legacy-account-token");
  expect(reload).toHaveBeenCalledOnce();
});

test("a success that returns after the session changed elsewhere is discarded", async () => {
  const actor = userEvent.setup();
  setToken("auto-account-token");
  const pending = deferred<MigrationResult>();
  const reload = vi.fn();
  render(<MigrationHarness migrate={() => pending.promise} reload={reload} />);
  const form = await openMigrationForm(actor);
  await submitMigration(actor);
  setToken("another-tab-account-token");
  pending.resolve(migrated);
  await waitFor(() => expect(form).toHaveTextContent("当前页面的登录状态已变化，本次结果未应用到此页面，请刷新页面后查看。"));
  expect(getToken()).toBe("another-tab-account-token");
  expect(reload).not.toHaveBeenCalled();
  expect(screen.getByRole("button", { name: "迁移" })).toBeEnabled();
});

test("a failure is reported inside the form and releases the in-flight guard", async () => {
  const actor = userEvent.setup();
  setToken("auto-account-token");
  const migrate = vi.fn(async () => { throw new Error("boom"); });
  const reload = vi.fn();
  render(<MigrationHarness migrate={migrate} reload={reload} />);
  await openMigrationForm(actor);
  await actor.click(screen.getByRole("button", { name: "迁移" }));
  expect(screen.getByRole("alert")).toHaveTextContent("请输入旧账号登录名和密码");
  expect(migrate).not.toHaveBeenCalled();
  await submitMigration(actor);
  await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("迁移失败，请稍后重试"));
  expect(screen.getByRole("button", { name: "迁移" })).toBeEnabled();
  expect(getToken()).toBe("auto-account-token");
  expect(reload).not.toHaveBeenCalled();
});

/** Background requests (report polling etc.) answer 401 once the server has revoked the old session. */
function revokedBackend() {
  const browser = window;
  const pageReload = vi.fn();
  vi.stubGlobal("fetch", vi.fn(async () => new Response(null, { status: 401 })));
  vi.stubGlobal("window", new Proxy(browser, {
    get(target, key) {
      if (key === "location") return { ...browser.location, reload: pageReload };
      return Reflect.get(target, key);
    },
  }));
  return pageReload;
}

test("a background 401 while the migration is in flight neither logs out nor reloads", async () => {
  const actor = userEvent.setup();
  setToken("auto-account-token");
  const pending = deferred<MigrationResult>();
  const reload = vi.fn();
  render(<MigrationHarness migrate={() => pending.promise} reload={reload} />);
  await openMigrationForm(actor);
  await submitMigration(actor);
  const pageReload = revokedBackend();
  const polled = await performApiRequest("/notebooks/n1/reports", { tag: "report" });
  expect(polled.status).toBe(401);
  expect(getToken()).toBe("auto-account-token");
  expect(pageReload).not.toHaveBeenCalled();
  pending.resolve(migrated);
  await waitFor(() => expect(reload).toHaveBeenCalledOnce());
  expect(getToken()).toBe("legacy-account-token");
});

test.each(["failed", "discarded"] as const)("a %s migration ends the handoff so later 401s clear the old session", async (ending) => {
  const actor = userEvent.setup();
  setToken("auto-account-token");
  const pending = deferred<MigrationResult>();
  render(<MigrationHarness migrate={() => pending.promise} reload={vi.fn()} />);
  const form = await openMigrationForm(actor);
  await submitMigration(actor);
  if (ending === "failed") {
    pending.reject(new Error("boom"));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("迁移失败，请稍后重试"));
  } else {
    setToken("another-tab-account-token");
    pending.resolve(migrated);
    await waitFor(() => expect(form).toHaveTextContent("本次结果未应用到此页面"));
    setToken("auto-account-token");
  }
  const pageReload = revokedBackend();
  await performApiRequest("/me", { tag: "auth" });
  expect(getToken()).toBe("");
  expect(pageReload).toHaveBeenCalledOnce();
});

test("the form's close control stays available while the page reports a migration in flight", async () => {
  const actor = userEvent.setup();
  const onClose = vi.fn();
  const onSubmit = vi.fn();
  render(<IdentityMigrationForm inFlight onSubmit={onSubmit} onClose={onClose} />);
  expect(screen.getByRole("button", { name: "正在迁移…" })).toBeDisabled();
  await actor.click(screen.getByRole("button", { name: "取消" }));
  expect(onClose).toHaveBeenCalledOnce();
  expect(onSubmit).not.toHaveBeenCalled();
});

const autoEnroll = {
  status: "confirmation_required" as const, pending_id: "p", external_username: "e12345678",
  display_name: "张三", purpose: "auto_enroll" as const, target_user_id: null, target_username: null,
};

test("auto enrollment names the employee number and warns that old data is not carried over", async () => {
  const actor = userEvent.setup();
  const onConfirm = vi.fn();
  render(<IdentityBindingConfirmation pending={autoEnroll} busy={false} onConfirm={onConfirm} onCancel={() => undefined} localLogin="allowed" />);
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
  render(<IdentityBindingConfirmation pending={autoEnroll} busy={false} onConfirm={onConfirm} onCancel={onCancel} localLogin="allowed" />);
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
  render(<IdentityBindingConfirmation pending={autoEnroll} busy={false} onConfirm={onConfirm} onCancel={onCancel} localLogin="closed" />);
  await actor.click(screen.getByRole("button", { name: "我已有本站旧账号" }));
  expect(screen.getByRole("status")).toHaveTextContent("请联系管理员迁移旧账号");
  expect(screen.queryByRole("button", { name: "去本站登录" })).not.toBeInTheDocument();
  await actor.click(screen.getByRole("button", { name: "返回" }));
  expect(screen.getByRole("button", { name: "确认新建账号" })).toBeInTheDocument();
  expect(onConfirm).not.toHaveBeenCalled();
  expect(onCancel).not.toHaveBeenCalled();
});

test.each([
  ["loading", "正在确认本站登录是否可用…"],
  ["unknown", "暂时无法确认本站登录是否可用，请稍后重试或联系管理员迁移旧账号。"],
] as const)("an unresolved local-login state (%s) neither claims passwords are closed nor offers local sign-in", async (state, copy) => {
  const actor = userEvent.setup();
  const onRetry = vi.fn();
  render(<IdentityBindingConfirmation pending={autoEnroll} busy={false} onConfirm={() => undefined} onCancel={() => undefined} localLogin={state} onRetryLocalLogin={onRetry} />);
  await actor.click(screen.getByRole("button", { name: "我已有本站旧账号" }));
  expect(screen.getByRole("status")).toHaveTextContent(copy);
  expect(screen.queryByText(/本站密码登录已停用/)).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "去本站登录" })).not.toBeInTheDocument();
  if (state === "unknown") {
    await actor.click(screen.getByRole("button", { name: "重试" }));
    expect(onRetry).toHaveBeenCalledOnce();
  } else {
    expect(screen.queryByRole("button", { name: "重试" })).not.toBeInTheDocument();
  }
});
