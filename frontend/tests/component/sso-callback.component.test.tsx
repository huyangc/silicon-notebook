import { StrictMode } from "react";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  completeSsoLogin: vi.fn(), confirmIdentityBinding: vi.fn(), cancelIdentityBinding: vi.fn(),
}));

vi.mock("../../app/auth", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../app/auth")>()),
  completeSsoLogin: mocks.completeSsoLogin,
  confirmIdentityBinding: mocks.confirmIdentityBinding,
  cancelIdentityBinding: mocks.cancelIdentityBinding,
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
