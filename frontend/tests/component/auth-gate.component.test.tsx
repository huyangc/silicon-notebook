import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";

import type { AuthCapabilities, AuthUser } from "../../app/auth";
import { AuthGate } from "../../app/AuthGate";

const user: AuthUser = {
  id: "u1", email: "", display_name: "", role: "user", username: "a12345678", ui_mode: "auto", search_profile: null,
};

function capabilities(overrides: Partial<AuthCapabilities> = {}): AuthCapabilities {
  return {
    mode: "local", local_login: true, local_registration: true, sso_login: false,
    binding_allowed: false, provider_label: "", ...overrides,
  };
}

test("dual mode keeps local credentials and presents a generic unified-login action", () => {
  render(<AuthGate capabilities={capabilities({ mode: "dual", sso_login: true, binding_allowed: true })} onAuthenticated={() => undefined} />);

  expect(screen.getByRole("button", { name: "本地登录" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "统一登录" })).toBeInTheDocument();
  expect(screen.queryByText("W3")).not.toBeInTheDocument();
});

test("SSO-only mode hides all local password and registration controls", () => {
  render(<AuthGate capabilities={capabilities({ mode: "sso_only", local_login: false, local_registration: false, sso_login: true })} onAuthenticated={() => undefined} />);

  expect(screen.getByRole("button", { name: "统一登录" })).toBeInTheDocument();
  expect(screen.queryByLabelText("用户名")).not.toBeInTheDocument();
  expect(screen.queryByText("注册")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "需要迁移帮助？" })).toBeInTheDocument();
});

test("migration mode verifies local credentials without entering a workspace", async () => {
  const onAuthenticated = vi.fn();
  const actor = userEvent.setup();
  render(<AuthGate capabilities={capabilities({ mode: "binding_required", local_login: true, sso_login: true })} onAuthenticated={onAuthenticated} />);

  expect(screen.getByLabelText("用户名")).toBeInTheDocument();
  expect(screen.queryByText("注册")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "统一登录" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "验证并关联统一身份" })).toBeEnabled();
  await actor.type(screen.getByLabelText("用户名"), "a12345678");
  await actor.type(screen.getByLabelText("密码"), "pw");
  expect(screen.getByRole("button", { name: "验证并关联统一身份" })).toBeEnabled();
  expect(onAuthenticated).not.toHaveBeenCalled();
});

test("migration help requires an explicit grant purpose", async () => {
  const actor = userEvent.setup();
  render(<AuthGate capabilities={capabilities({ mode: "sso_only", local_login: false, local_registration: false, sso_login: true })} onAuthenticated={() => undefined} />);

  await actor.click(screen.getByRole("button", { name: "需要迁移帮助？" }));
  expect(screen.getByLabelText("迁移凭证")).toBeInTheDocument();
  expect(screen.getByRole("radio", { name: "新建账号" })).toBeChecked();
  expect(screen.getByRole("radio", { name: "恢复账号" })).not.toBeChecked();
  expect(screen.getByRole("radio", { name: "更换统一账号" })).not.toBeChecked();
  await actor.click(screen.getByRole("radio", { name: "更换统一账号" }));
  expect(screen.getByRole("radio", { name: "更换统一账号" })).toBeChecked();
});
