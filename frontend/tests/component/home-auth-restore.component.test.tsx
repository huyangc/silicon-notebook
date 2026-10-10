import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import userEvent from "@testing-library/user-event";

import Home from "../../app/page";
import { getToken, setToken } from "../../app/auth";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  window.localStorage.clear();
  window.history.replaceState(null, "", "/");
});

type ServerOptions = {
  /** /me rejects the bearer (expired, or a local session after SSO was switched on). */
  rejected?: boolean;
  firstCapabilitiesFailure?: 404 | 503 | "network";
  repeatFailure?: boolean;
  role?: string;
  ssoLogin?: boolean;
};

function authenticationServer(options: ServerOptions = {}) {
  const requests: string[] = [];
  let capabilitiesAttempts = 0;
  const ssoLogin = options.ssoLogin ?? true;
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const path = new URL(String(input), window.location.origin).pathname;
    requests.push(path);
    if (path === "/api/ready") return Response.json({ ready: true });
    if (path === "/api/auth/capabilities") {
      if ((capabilitiesAttempts++ === 0 || options.repeatFailure) && options.firstCapabilitiesFailure) {
        if (options.firstCapabilitiesFailure === "network") throw new TypeError("offline");
        return Response.json({ detail: "capabilities unavailable" }, { status: options.firstCapabilitiesFailure });
      }
      return Response.json({
        sso_login: ssoLogin, local_login: !ssoLogin, local_registration: !ssoLogin, provider_label: "W3",
      });
    }
    if (path === "/api/me") return options.rejected
      ? Response.json({ detail: "session rejected" }, { status: 401 })
      : Response.json({ id: "user-1", username: "alice", role: options.role ?? "user", ui_mode: "auto", search_profile: null });
    if (path === "/api/health") return Response.json({ status: "ok", llm_configured: false });
    if (path === "/api/notebooks") return Response.json([]);
    if (path === "/api/me/pending-actions") return Response.json({ count: 0, items: [] });
    if (path === "/api/me/pending-actions/stream") {
      // Keep the request pending: the test observes admission, not reconnects.
      return new Promise<Response>(() => {});
    }
    return Response.json({ detail: "not provided by this fixture" }, { status: 503 });
  }));
  return requests;
}

test("a rejected session under unified authentication returns to the provider button only", async () => {
  const requests = authenticationServer({ rejected: true });
  setToken("old-local-session");
  render(<Home />);
  expect(await screen.findByRole("button", { name: "使用W3登录" })).toBeInTheDocument();
  expect(screen.queryByLabelText("用户名")).not.toBeInTheDocument();
  expect(screen.queryByLabelText("密码")).not.toBeInTheDocument();
  expect(getToken()).toBe("");
  expect(requests).toEqual(["/api/ready", "/api/auth/capabilities", "/api/me"]);
});

test("a restored SSO session receives pending actions", async () => {
  const requests = authenticationServer();
  setToken("sso-token");
  render(<Home />);
  await waitFor(() => expect(requests).toContain("/api/me/pending-actions/stream"));
  expect(requests).toContain("/api/me/pending-actions");
  expect(getToken()).toBe("sso-token");
});

test.each([503, "network"] as const)("capabilities failure %s keeps login closed until a retry succeeds", async (failure) => {
  const requests = authenticationServer({ firstCapabilitiesFailure: failure });
  render(<Home />);
  expect(await screen.findByRole("alert")).toHaveTextContent("认证状态暂时无法确认");
  expect(screen.queryByLabelText("用户名")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "使用W3登录" })).not.toBeInTheDocument();
  expect(requests).toEqual(["/api/ready", "/api/auth/capabilities"]);

  await userEvent.setup().click(screen.getByRole("button", { name: "重试" }));
  expect(await screen.findByRole("button", { name: "使用W3登录" })).toBeInTheDocument();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

test("an explicitly absent capabilities endpoint retains legacy local login", async () => {
  authenticationServer({ firstCapabilitiesFailure: 404 });
  render(<Home />);
  expect(await screen.findByRole("button", { name: "本地登录" })).toBeInTheDocument();
  expect(screen.getByLabelText("用户名")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "重试" })).not.toBeInTheDocument();
});

test("provider maintenance does not block an existing administrator SSO session", async () => {
  const requests = authenticationServer({ firstCapabilitiesFailure: 503, repeatFailure: true, role: "admin" });
  setToken("valid-admin-sso");
  render(<Home />);
  await waitFor(() => expect(requests).toContain("/api/me/pending-actions/stream"));
  expect(requests).toContain("/api/notebooks");
  expect(requests).toContain("/api/me/pending-actions");
  expect(getToken()).toBe("valid-admin-sso");
  expect(screen.queryByRole("button", { name: "重试" })).not.toBeInTheDocument();
  await userEvent.setup().click(screen.getByRole("button", { name: "账户菜单" }));
  expect(screen.getByRole("menuitem", { name: "用户总览" })).toBeInTheDocument();
  expect(screen.queryByRole("menuitem", { name: "认证迁移" })).not.toBeInTheDocument();
  expect(screen.queryByRole("menuitem", { name: "修改密码" })).not.toBeInTheDocument();
  expect(screen.queryByRole("menuitem", { name: "关联统一身份" })).not.toBeInTheDocument();
});

test("a signed-in user may change their password only while local login is on", async () => {
  authenticationServer({ ssoLogin: false });
  setToken("local-session");
  render(<Home />);
  await userEvent.setup().click(await screen.findByRole("button", { name: "账户菜单" }));
  expect(screen.getByRole("menuitem", { name: "修改密码" })).toBeInTheDocument();
});

test("a rejected session with unknown capabilities is cleared and login stays closed", async () => {
  const requests = authenticationServer({ rejected: true, firstCapabilitiesFailure: 503, repeatFailure: true });
  setToken("invalid-token");
  render(<Home />);
  await screen.findByRole("alert");
  expect(getToken()).toBe("");
  expect(requests).toEqual(["/api/ready", "/api/auth/capabilities", "/api/me"]);
  expect(screen.queryByLabelText("密码")).not.toBeInTheDocument();
});
