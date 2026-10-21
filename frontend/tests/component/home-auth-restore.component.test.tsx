import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import userEvent from "@testing-library/user-event";

// jsdom cannot navigate: returning to another page after sign-in leaves through this seam.
const navigation = vi.hoisted(() => ({ replace: vi.fn() }));
vi.mock("../../app/auth-return-location", async (importOriginal) => ({
  ...await importOriginal<typeof import("../../app/auth-return-location")>(),
  replacePageLocation: navigation.replace,
}));

import Home from "../../app/page";
import { getToken, setToken } from "../../app/auth";
import { rememberLoginEntrance, consumeLoginEntrance } from "../../app/login-entrance";

afterEach(() => {
  cleanup();
  navigation.replace.mockReset();
  vi.unstubAllGlobals();
  window.localStorage.clear();
  window.sessionStorage.clear();
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
    if (path === "/api/auth/login") return Response.json({ token: "new-local-session", user: { id: "user-1", username: "alice", role: "user", ui_mode: "auto", search_profile: null } });
    if (path === "/api/notebooks") return Response.json([]);
    if (path === "/api/me/pending-actions") return Response.json({ count: 0, items: [] });
    // The notebook list's deletion-window block reads its notices (fail-closed
    // when unreadable, so the fixture answers it).
    if (path === "/api/me/notebook-deletions") return Response.json({ items: [] });
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
  expect(screen.queryByText("登录成功，正在翻开笔记本…")).not.toBeInTheDocument();
});

test("a successful local login opens the book over the app and releases its controls", async () => {
  authenticationServer({ ssoLogin: false });
  render(<Home />);
  const actor = userEvent.setup();
  await actor.type(await screen.findByLabelText("用户名"), "alice");
  await actor.type(screen.getByLabelText("密码"), "pw");
  await actor.click(screen.getByRole("button", { name: "本地登录" }));
  const status = await screen.findByText("登录成功，正在翻开笔记本…");
  const app = screen.getByRole("button", { name: "账户菜单" }).closest(".app")!;
  expect(app).toHaveAttribute("inert");
  const end = new Event("animationend", { bubbles: true });
  Object.defineProperty(end, "animationName", { value: "auth-book-reveal" });
  fireEvent(status.closest(".auth-book-entrance")!, end);
  expect(app).not.toHaveAttribute("inert");
  expect(screen.queryByText("登录成功，正在翻开笔记本…")).not.toBeInTheDocument();
});

test("SSO return opens the book once after the session is confirmed", async () => {
  authenticationServer();
  setToken("sso-token");
  rememberLoginEntrance("user-1");
  render(<Home />);
  expect(await screen.findByText("登录成功，正在翻开笔记本…")).toBeInTheDocument();
  expect(consumeLoginEntrance("user-1")).toBe(false);
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

const signInFrom = (page: string) => `/?next=${encodeURIComponent(page)}`;

async function signInLocally() {
  const actor = userEvent.setup();
  await actor.type(await screen.findByLabelText("用户名"), "alice");
  await actor.type(screen.getByLabelText("密码"), "pw");
  await actor.click(screen.getByRole("button", { name: "本地登录" }));
}

test("local sign-in reached from another page returns there with its path and query", async () => {
  const requests = authenticationServer({ ssoLogin: false });
  window.history.replaceState(null, "", signInFrom("/admin/usage?sheet=questions"));
  render(<Home />);
  await signInLocally();
  await waitFor(() => expect(navigation.replace).toHaveBeenCalledWith("/admin/usage?sheet=questions"));
  expect(getToken()).toBe("new-local-session");
  expect(screen.getByText("加载中…")).toBeInTheDocument();
  expect(screen.queryByText("登录成功，正在翻开笔记本…")).not.toBeInTheDocument();
  expect(requests).not.toContain("/api/notebooks");
});

test("unified sign-in comes back through the workspace and goes on to the page it started from", async () => {
  // The provider returns to the saved location, which is the sign-in page with its return parameter.
  const requests = authenticationServer();
  setToken("sso-token");
  rememberLoginEntrance("user-1");
  window.history.replaceState(null, "", signInFrom("/wishes?kind=bug"));
  render(<Home />);
  await waitFor(() => expect(navigation.replace).toHaveBeenCalledWith("/wishes?kind=bug"));
  expect(getToken()).toBe("sso-token");
  expect(screen.getByText("加载中…")).toBeInTheDocument();
  // The entrance animation belongs to the workspace and must not replay on its next visit.
  expect(consumeLoginEntrance("user-1")).toBe(false);
  expect(requests).not.toContain("/api/notebooks");
});

test("a rejected session on the sign-in page keeps the return target for the next sign-in", async () => {
  authenticationServer({ ssoLogin: false, rejected: true });
  setToken("stale-session");
  window.history.replaceState(null, "", signInFrom("/updates"));
  render(<Home />);
  expect(await screen.findByLabelText("用户名")).toBeInTheDocument();
  expect(getToken()).toBe("");
  expect(navigation.replace).not.toHaveBeenCalled();
  expect(window.location.search).toBe(`?next=${encodeURIComponent("/updates")}`);
});

test.each(["https://evil.example/admin", "//evil.example/admin", "/\\evil.example"])(
  "sign-in refuses the external return target %s and opens the workspace",
  async (target) => {
    authenticationServer({ ssoLogin: false });
    window.history.replaceState(null, "", signInFrom(target));
    render(<Home />);
    await signInLocally();
    expect(await screen.findByText("登录成功，正在翻开笔记本…")).toBeInTheDocument();
    expect(navigation.replace).not.toHaveBeenCalled();
    expect(window.location.search).toBe("");
  },
);

test("a restored session refuses an external return target and stays in the workspace", async () => {
  const requests = authenticationServer();
  setToken("sso-token");
  window.history.replaceState(null, "", signInFrom("https://evil.example/"));
  render(<Home />);
  await waitFor(() => expect(requests).toContain("/api/notebooks"));
  expect(navigation.replace).not.toHaveBeenCalled();
  expect(window.location.search).toBe("");
});
