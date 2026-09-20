import { StrictMode } from "react";
import { render, screen, waitFor } from "@testing-library/react";
import { expect, test, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  completeSsoLogin: vi.fn(), confirmIdentityBinding: vi.fn(), cancelIdentityBinding: vi.fn(),
}));

vi.mock("../../app/auth", () => ({
  completeSsoLogin: mocks.completeSsoLogin,
  confirmIdentityBinding: mocks.confirmIdentityBinding,
  cancelIdentityBinding: mocks.cancelIdentityBinding,
  getToken: () => "local-migration-token",
  setToken: () => undefined,
}));

import SsoCallbackPage from "../../app/auth/sso/callback/page";

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
