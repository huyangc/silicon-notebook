import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";

import type { AuthCapabilities, AuthUser } from "../../app/auth";
import { AuthGate } from "../../app/AuthGate";
import { getToken } from "../../app/auth";

afterEach(() => { vi.unstubAllGlobals(); localStorage.clear(); sessionStorage.clear(); });

const user: AuthUser = {
  id: "u1", email: "", display_name: "", role: "user", username: "a12345678", ui_mode: "auto", search_profile: null,
};

function capabilities(overrides: Partial<AuthCapabilities> = {}): AuthCapabilities {
  return { sso_login: false, local_login: true, local_registration: true, provider_label: "", ...overrides };
}

const ssoOn = capabilities({ sso_login: true, local_login: false, local_registration: false, provider_label: "W3" });

test("local mode keeps username, password and registration, with no unified-login button", () => {
  render(<AuthGate capabilities={capabilities()} onAuthenticated={() => undefined} />);

  expect(screen.getByLabelText("用户名")).toBeInTheDocument();
  expect(screen.getByLabelText("密码")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "注册" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "本地登录" })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /^使用.*登录$/ })).not.toBeInTheDocument();
});

test("unified authentication shows only the provider button and hides passwords and registration", () => {
  render(<AuthGate capabilities={ssoOn} onAuthenticated={() => undefined} />);

  expect(screen.getByRole("button", { name: "使用W3登录" })).toBeInTheDocument();
  expect(screen.queryByLabelText("用户名")).not.toBeInTheDocument();
  expect(screen.queryByLabelText("密码")).not.toBeInTheDocument();
  expect(screen.queryByText("注册")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "本地登录" })).not.toBeInTheDocument();
});

test("an empty provider label falls back to a generic unified-authentication label", () => {
  render(<AuthGate capabilities={{ ...ssoOn, provider_label: "  " }} onAuthenticated={() => undefined} />);
  expect(screen.getByRole("button", { name: "使用统一认证登录" })).toBeInTheDocument();
});

test("the provider button shows a busy state and redirects to the authorization URL", async () => {
  const browser = window;
  const assign = vi.fn();
  vi.stubGlobal("window", new Proxy(browser, { get(target, key) {
    if (key === "location") return { origin: browser.location.origin, pathname: "/", search: "?group_invite=x", hash: "", assign };
    return Reflect.get(target, key);
  } }));
  let respond: (value: Response) => void = () => undefined;
  const fetch = vi.fn((_input: RequestInfo | URL, _init?: RequestInit) => new Promise<Response>((resolve) => { respond = resolve; }));
  vi.stubGlobal("fetch", fetch);
  render(<AuthGate capabilities={ssoOn} onAuthenticated={() => undefined} />);

  await userEvent.setup().click(screen.getByRole("button", { name: "使用W3登录" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "正在跳转…" })).toBeDisabled());
  respond(Response.json({ authorization_url: "https://identity.example/authorize" }));
  await waitFor(() => expect(assign).toHaveBeenCalledWith("https://identity.example/authorize"));
  expect(String(fetch.mock.calls[0][0])).toContain("/auth/sso/start");
  expect(sessionStorage.getItem("silicon_notebook_sso_return_location")).toBe("/?group_invite=x");
});

test("a failed start is reported beside the provider button and the button is usable again", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => new Response("", { status: 503 })));
  render(<AuthGate capabilities={ssoOn} onAuthenticated={() => undefined} />);

  await userEvent.setup().click(screen.getByRole("button", { name: "使用W3登录" }));
  expect(await screen.findByRole("alert")).toBeInTheDocument();
  await waitFor(() => expect(screen.getByRole("button", { name: "使用W3登录" })).toBeEnabled());
});

test("local sign-in stores the token and enters the workspace", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ token: "local-token", user })));
  const onAuthenticated = vi.fn();
  const actor = userEvent.setup();
  render(<AuthGate capabilities={capabilities()} onAuthenticated={onAuthenticated} />);

  await actor.type(screen.getByLabelText("用户名"), "a12345678");
  await actor.type(screen.getByLabelText("密码"), "pw");
  await actor.click(screen.getByRole("button", { name: "本地登录" }));
  await waitFor(() => expect(onAuthenticated).toHaveBeenCalledWith(expect.objectContaining({ id: "u1" })));
  expect(getToken()).toBe("local-token");
});
