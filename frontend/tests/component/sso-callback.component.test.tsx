import { StrictMode } from "react";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  completeSsoLogin: vi.fn(), linkSsoAccount: vi.fn(), createSsoAccount: vi.fn(), cancelSsoChoice: vi.fn(),
  setToken: vi.fn(),
}));

vi.mock("../../app/auth", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../app/auth")>()),
  completeSsoLogin: mocks.completeSsoLogin,
  linkSsoAccount: mocks.linkSsoAccount,
  createSsoAccount: mocks.createSsoAccount,
  cancelSsoChoice: mocks.cancelSsoChoice,
  setToken: mocks.setToken,
}));

import SsoCallbackPage from "../../app/auth/sso/callback/page";
import { startSsoLogin } from "../../app/auth";
import { consumeSsoReturnLocation } from "../../app/auth-return-location";
import { humanizedError } from "../../app/errors";

beforeEach(() => {
  for (const mock of Object.values(mocks)) mock.mockReset();
});

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

async function beginSso() {
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ authorization_url: "https://identity.example/authorize" })));
  window.history.replaceState(null, "", "/?group_invite=invite-token#notebook=notebook-1");
  await startSsoLogin();
  window.history.replaceState(null, "", "/auth/sso/callback?code=handoff");
}

const RETURN = "/?group_invite=invite-token#notebook=notebook-1";
const choice = { status: "choice_required", pending_id: "p1", external_username: "e12345678", display_name: "张三" };
const signedIn = { token: "sso-token", user: { id: "u1" } };

test("callback consumes the one-time handoff exactly once under StrictMode and removes it from the URL", async () => {
  window.history.replaceState(null, "", "/auth/sso/callback?code=one-time-code");
  mocks.completeSsoLogin.mockResolvedValue(choice);
  render(<StrictMode><SsoCallbackPage /></StrictMode>);

  await screen.findByRole("heading", { name: "统一认证账号 e12345678 在本站还没有对应账号" });
  expect(mocks.completeSsoLogin).toHaveBeenCalledTimes(1);
  expect(mocks.completeSsoLogin).toHaveBeenCalledWith("one-time-code");
  expect(window.location.search).toBe("");
});

test("authenticated: installs the session and returns to the initiating invite and notebook exactly once", async () => {
  await beginSso();
  const replace = observeNavigation();
  mocks.completeSsoLogin.mockResolvedValue({ status: "authenticated", ...signedIn });
  render(<SsoCallbackPage />);
  await waitFor(() => expect(replace).toHaveBeenCalledWith(RETURN));
  expect(mocks.setToken).toHaveBeenCalledWith("sso-token");
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
  mocks.completeSsoLogin.mockResolvedValue({ status: "authenticated", ...signedIn });
  render(<SsoCallbackPage />);
  await waitFor(() => expect(replace).toHaveBeenCalledWith("/"));
  expect(window.sessionStorage.getItem("silicon_notebook_sso_return_location")).toBeNull();
});

test("error: a provider error or a failed completion is reported with a way back", async () => {
  window.history.replaceState(null, "", "/auth/sso/callback?error=cancelled");
  render(<SsoCallbackPage />);
  expect(await screen.findByRole("alert")).toHaveTextContent("已取消统一认证登录，请返回后重试。");
  expect(mocks.completeSsoLogin).not.toHaveBeenCalled();
  cleanup();

  window.history.replaceState(null, "", "/auth/sso/callback?code=expired");
  mocks.completeSsoLogin.mockRejectedValue(humanizedError("登录凭证已失效，请重新登录"));
  render(<SsoCallbackPage />);
  expect(await screen.findByRole("alert")).toHaveTextContent("登录凭证已失效，请重新登录");
  expect(screen.getByRole("link", { name: "返回登录页" })).toBeInTheDocument();
});

test("choice_required shows both options with the new-account consequence spelled out", async () => {
  window.history.replaceState(null, "", "/auth/sso/callback?code=handoff");
  mocks.completeSsoLogin.mockResolvedValue(choice);
  render(<SsoCallbackPage />);

  expect(await screen.findByRole("form", { name: "关联老账号" })).toBeInTheDocument();
  expect(screen.getByText("将以 e12345678 新建账号，老账号的数据不会出现在新账号里。", { exact: false })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "使用新账号" })).toBeEnabled();
  expect(screen.getByRole("button", { name: "返回登录" })).toBeEnabled();
  expect(mocks.linkSsoAccount).not.toHaveBeenCalled();
  expect(mocks.createSsoAccount).not.toHaveBeenCalled();
});

test("link: a rejected password is shown inside the form and the same choice can be retried", async () => {
  await beginSso();
  const replace = observeNavigation();
  mocks.completeSsoLogin.mockResolvedValue(choice);
  mocks.linkSsoAccount
    .mockRejectedValueOnce(humanizedError("用户名或密码不正确"))
    .mockResolvedValueOnce(signedIn);
  render(<SsoCallbackPage />);
  const actor = userEvent.setup();

  await actor.type(await screen.findByLabelText("老账号用户名"), "a12345678");
  await actor.type(screen.getByLabelText("老账号密码"), "wrong");
  await actor.click(screen.getByRole("button", { name: "关联并登录" }));
  await waitFor(() => expect(screen.getByRole("form", { name: "关联老账号" })).toHaveTextContent("用户名或密码不正确"));
  expect(mocks.linkSsoAccount).toHaveBeenLastCalledWith("p1", "a12345678", "wrong");
  expect(screen.getByLabelText("老账号密码")).toHaveValue("");
  expect(screen.getByLabelText("老账号用户名")).toHaveValue("a12345678");
  expect(replace).not.toHaveBeenCalled();

  await actor.type(screen.getByLabelText("老账号密码"), "right");
  await actor.click(screen.getByRole("button", { name: "关联并登录" }));
  await waitFor(() => expect(replace).toHaveBeenCalledWith(RETURN));
  expect(mocks.linkSsoAccount).toHaveBeenLastCalledWith("p1", "a12345678", "right");
  expect(mocks.setToken).toHaveBeenCalledWith("sso-token");
});

test("link: the button shows progress and every action is locked while it runs", async () => {
  window.history.replaceState(null, "", "/auth/sso/callback?code=handoff");
  observeNavigation();
  mocks.completeSsoLogin.mockResolvedValue(choice);
  mocks.linkSsoAccount.mockReturnValue(new Promise(() => undefined));
  render(<SsoCallbackPage />);
  const actor = userEvent.setup();

  await actor.type(await screen.findByLabelText("老账号用户名"), "a12345678");
  await actor.type(screen.getByLabelText("老账号密码"), "pw");
  await actor.click(screen.getByRole("button", { name: "关联并登录" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "正在关联…" })).toBeDisabled());
  expect(screen.getByRole("button", { name: "使用新账号" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "返回登录" })).toBeDisabled();
});

test("link: empty fields are caught before any request", async () => {
  window.history.replaceState(null, "", "/auth/sso/callback?code=handoff");
  mocks.completeSsoLogin.mockResolvedValue(choice);
  render(<SsoCallbackPage />);
  await userEvent.setup().click(await screen.findByRole("button", { name: "关联并登录" }));
  await waitFor(() => expect(screen.getByRole("form", { name: "关联老账号" })).toHaveTextContent("请输入老账号的用户名和密码"));
  expect(mocks.linkSsoAccount).not.toHaveBeenCalled();
});

test("create: a new account is created only on the explicit button and then signs in", async () => {
  await beginSso();
  const replace = observeNavigation();
  mocks.completeSsoLogin.mockResolvedValue(choice);
  mocks.createSsoAccount.mockResolvedValue(signedIn);
  render(<SsoCallbackPage />);

  await userEvent.setup().click(await screen.findByRole("button", { name: "使用新账号" }));
  await waitFor(() => expect(replace).toHaveBeenCalledWith(RETURN));
  expect(mocks.createSsoAccount).toHaveBeenCalledWith("p1");
  expect(mocks.setToken).toHaveBeenCalledWith("sso-token");
  expect(mocks.linkSsoAccount).not.toHaveBeenCalled();
});

test("create: a failure is reported beside the new-account button", async () => {
  window.history.replaceState(null, "", "/auth/sso/callback?code=handoff");
  observeNavigation();
  mocks.completeSsoLogin.mockResolvedValue(choice);
  mocks.createSsoAccount.mockRejectedValue(humanizedError("本次统一认证已过期，请返回重新登录"));
  render(<SsoCallbackPage />);

  await userEvent.setup().click(await screen.findByRole("button", { name: "使用新账号" }));
  await waitFor(() => expect(screen.getByRole("region", { name: "不关联，使用新账号" }))
    .toHaveTextContent("本次统一认证已过期，请返回重新登录"));
  expect(screen.getByRole("button", { name: "使用新账号" })).toBeEnabled();
});

test("cancel: returning to login discards the pending choice", async () => {
  window.history.replaceState(null, "", "/auth/sso/callback?code=handoff");
  const replace = observeNavigation();
  mocks.completeSsoLogin.mockResolvedValue(choice);
  mocks.cancelSsoChoice.mockResolvedValue(undefined);
  render(<SsoCallbackPage />);

  await userEvent.setup().click(await screen.findByRole("button", { name: "返回登录" }));
  await waitFor(() => expect(replace).toHaveBeenCalledWith("/"));
  expect(mocks.cancelSsoChoice).toHaveBeenCalledWith("p1");
  expect(mocks.setToken).not.toHaveBeenCalled();
});

test("cancel: a failed discard still returns to login (the choice expires on its own)", async () => {
  window.history.replaceState(null, "", "/auth/sso/callback?code=handoff");
  const replace = observeNavigation();
  mocks.completeSsoLogin.mockResolvedValue(choice);
  mocks.cancelSsoChoice.mockRejectedValue(new Error("offline"));
  render(<SsoCallbackPage />);

  await userEvent.setup().click(await screen.findByRole("button", { name: "返回登录" }));
  await waitFor(() => expect(replace).toHaveBeenCalledWith("/"));
});

test("link: a voided choice (410) shows the backend text, locks link and create, leaves only return to login", async () => {
  window.history.replaceState(null, "", "/auth/sso/callback?code=handoff");
  mocks.completeSsoLogin.mockResolvedValue(choice);
  mocks.linkSsoAccount.mockRejectedValueOnce(humanizedError("本次选择已过期，请返回登录重新开始", 410));
  render(<SsoCallbackPage />);
  const actor = userEvent.setup();

  await actor.type(await screen.findByLabelText("老账号用户名"), "a12345678");
  await actor.type(screen.getByLabelText("老账号密码"), "pw");
  await actor.click(screen.getByRole("button", { name: "关联并登录" }));
  await waitFor(() => expect(screen.getByRole("form", { name: "关联老账号" })).toHaveTextContent("本次选择已过期，请返回登录重新开始"));
  expect(screen.getByRole("button", { name: "关联并登录" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "使用新账号" })).toBeDisabled();
  expect(screen.getByLabelText("老账号用户名")).toBeDisabled();
  expect(screen.getByRole("button", { name: "返回登录" })).toBeEnabled();
});

test("create: a voided choice (410) locks everything but return to login; 409 stays retryable", async () => {
  window.history.replaceState(null, "", "/auth/sso/callback?code=handoff");
  mocks.completeSsoLogin.mockResolvedValue(choice);
  mocks.createSsoAccount
    .mockRejectedValueOnce(humanizedError("本站已有只差大小写的同名账号，请选择「关联老账号」", 409))
    .mockRejectedValueOnce(humanizedError("本次选择已被使用", 410));
  render(<SsoCallbackPage />);
  const actor = userEvent.setup();

  await actor.click(await screen.findByRole("button", { name: "使用新账号" }));
  await waitFor(() => expect(screen.getByRole("region", { name: "不关联，使用新账号" })).toHaveTextContent("只差大小写的同名账号"));
  expect(screen.getByRole("button", { name: "使用新账号" })).toBeEnabled();
  expect(screen.getByRole("button", { name: "关联并登录" })).toBeEnabled();

  await actor.click(screen.getByRole("button", { name: "使用新账号" }));
  await waitFor(() => expect(screen.getByRole("region", { name: "不关联，使用新账号" })).toHaveTextContent("本次选择已被使用"));
  expect(screen.getByRole("button", { name: "使用新账号" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "关联并登录" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "返回登录" })).toBeEnabled();
});
