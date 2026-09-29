import { StrictMode } from "react";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  completeSsoLogin: vi.fn(), confirmIdentityBinding: vi.fn(), cancelIdentityBinding: vi.fn(),
  fetchAuthCapabilities: vi.fn(),
}));

vi.mock("../../app/auth", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../app/auth")>()),
  completeSsoLogin: mocks.completeSsoLogin,
  confirmIdentityBinding: mocks.confirmIdentityBinding,
  cancelIdentityBinding: mocks.cancelIdentityBinding,
  fetchAuthCapabilities: mocks.fetchAuthCapabilities,
  getToken: () => "local-migration-token",
  setToken: () => undefined,
}));

import SsoCallbackPage from "../../app/auth/sso/callback/page";
import { startIdentityBinding, startSsoLogin } from "../../app/auth";
import { consumeSsoReturnLocation } from "../../app/auth-return-location";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  window.sessionStorage.clear();
  window.history.replaceState(null, "", "/");
});

function observeNavigation() {
  const browser = window;
  const replace = vi.fn();
  vi.stubGlobal("window", new Proxy(browser, {
    get(target, key) {
      if (key === "location") return {
        origin: browser.location.origin, pathname: browser.location.pathname,
        search: browser.location.search, hash: browser.location.hash, replace,
      };
      return Reflect.get(target, key);
    },
  }));
  return replace;
}

async function beginSso(binding = false) {
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ authorization_url: "https://identity.example/authorize" })));
  window.history.replaceState(null, "", "/?group_invite=invite-token#notebook=notebook-1");
  if (binding) await startIdentityBinding("current-password");
  else await startSsoLogin();
  window.history.replaceState(null, "", "/auth/sso/callback?code=handoff");
}

test("callback consumes the one-time handoff exactly once under StrictMode and removes it from the URL", async () => {
  window.history.replaceState(null, "", "/auth/sso/callback?code=one-time-code");
  mocks.completeSsoLogin.mockResolvedValue({
    status: "binding_required", pending_id: "p", local_login_name: "a12345678", external_username: "alice", display_name: "Alice",
  });
  render(<StrictMode><SsoCallbackPage /></StrictMode>);

  await waitFor(() => expect(screen.getByRole("heading", { name: "确认关联身份" })).toBeInTheDocument());
  expect(mocks.completeSsoLogin).toHaveBeenCalledTimes(1);
  expect(mocks.completeSsoLogin).toHaveBeenCalledWith("one-time-code");
  expect(window.location.search).toBe("");
});

test("SSO login returns to the initiating invite and notebook exactly once", async () => {
  await beginSso();
  const replace = observeNavigation();
  mocks.completeSsoLogin.mockResolvedValue({ status: "authenticated", token: "sso", user: {} });
  render(<SsoCallbackPage />);
  await waitFor(() => expect(replace).toHaveBeenCalledWith("/?group_invite=invite-token#notebook=notebook-1"));
  expect(consumeSsoReturnLocation()).toBe("/");
});

test("confirmed identity binding restores the initiating invite and notebook", async () => {
  await beginSso(true);
  const replace = observeNavigation();
  mocks.completeSsoLogin.mockResolvedValue({
    status: "binding_required", pending_id: "p", local_login_name: "a12345678", external_username: "alice", display_name: "Alice",
  });
  mocks.confirmIdentityBinding.mockResolvedValue({ token: "sso", user: {} });
  render(<SsoCallbackPage />);
  await userEvent.setup().click(await screen.findByRole("button", { name: "确认关联" }));
  await waitFor(() => expect(replace).toHaveBeenCalledWith("/?group_invite=invite-token#notebook=notebook-1"));
  expect(consumeSsoReturnLocation()).toBe("/");
});

test.each([
  "https://attacker.example/path", "//attacker.example/path", "/\\attacker.example/path",
  "javascript:alert(1)", "/%2fattacker.example", "/%5cattacker.example", "/one/..//attacker.example",
  "/auth/sso/callback?code=stale", "/auth/sso/callback/", "/auth/sso/%63allback",
])("callback rejects an unsafe stored return location: %s", async (target) => {
  window.sessionStorage.setItem("silicon_notebook_sso_return_location", target);
  window.history.replaceState(null, "", "/auth/sso/callback?code=handoff");
  const replace = observeNavigation();
  mocks.completeSsoLogin.mockResolvedValue({ status: "authenticated", token: "sso", user: {} });
  render(<SsoCallbackPage />);
  await waitFor(() => expect(replace).toHaveBeenCalledWith("/"));
  expect(window.sessionStorage.getItem("silicon_notebook_sso_return_location")).toBeNull();
});

const autoEnrollPreview = {
  status: "confirmation_required", pending_id: "auto-p", external_username: "e12345678", display_name: "张三",
  purpose: "auto_enroll", target_user_id: null, target_username: null,
};

test("auto enrollment creates the account only after explicit confirmation", async () => {
  await beginSso();
  const replace = observeNavigation();
  mocks.completeSsoLogin.mockResolvedValue(autoEnrollPreview);
  mocks.fetchAuthCapabilities.mockResolvedValue({ local_login: true });
  mocks.confirmIdentityBinding.mockResolvedValue({ token: "sso", user: {} });
  render(<SsoCallbackPage />);
  const confirm = await screen.findByRole("button", { name: "确认新建账号" });
  expect(mocks.confirmIdentityBinding).not.toHaveBeenCalled();
  await userEvent.setup().click(confirm);
  expect(mocks.confirmIdentityBinding).toHaveBeenCalledWith("auto-p");
  await waitFor(() => expect(replace).toHaveBeenCalledWith("/?group_invite=invite-token#notebook=notebook-1"));
});

test("an old-account holder abandons auto enrollment and returns to local sign-in", async () => {
  window.history.replaceState(null, "", "/auth/sso/callback?code=handoff");
  const replace = observeNavigation();
  mocks.completeSsoLogin.mockResolvedValue(autoEnrollPreview);
  mocks.fetchAuthCapabilities.mockResolvedValue({ local_login: true });
  mocks.cancelIdentityBinding.mockResolvedValue(undefined);
  render(<SsoCallbackPage />);
  const actor = userEvent.setup();
  await actor.click(await screen.findByRole("button", { name: "我已有本站旧账号" }));
  await actor.click(await screen.findByRole("button", { name: "去本站登录" }));
  expect(mocks.cancelIdentityBinding).toHaveBeenCalledWith("auto-p");
  expect(mocks.confirmIdentityBinding).not.toHaveBeenCalled();
  await waitFor(() => expect(replace).toHaveBeenCalledWith("/"));
});

test("auto enrollment points to an administrator when capabilities cannot be read", async () => {
  window.history.replaceState(null, "", "/auth/sso/callback?code=handoff");
  mocks.completeSsoLogin.mockResolvedValue(autoEnrollPreview);
  mocks.fetchAuthCapabilities.mockRejectedValue(new Error("offline"));
  render(<SsoCallbackPage />);
  await userEvent.setup().click(await screen.findByRole("button", { name: "我已有本站旧账号" }));
  expect(await screen.findByText("本站密码登录已停用，请联系管理员迁移旧账号。")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "去本站登录" })).not.toBeInTheDocument();
});
