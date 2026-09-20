import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
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

function authenticationServer(migration: boolean, firstCapabilitiesFailure?: 404 | 503 | "network") {
  const requests: string[] = [];
  let capabilitiesAttempts = 0;
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const path = new URL(String(input), window.location.origin).pathname;
    requests.push(path);
    if (path === "/api/ready") return Response.json({ ready: true });
    if (path === "/api/auth/capabilities") {
      if (capabilitiesAttempts++ === 0 && firstCapabilitiesFailure) {
        if (firstCapabilitiesFailure === "network") throw new TypeError("offline");
        return Response.json({ detail: "capabilities unavailable" }, { status: firstCapabilitiesFailure });
      }
      return Response.json({
        mode: "binding_required", local_login: true, local_registration: false,
        sso_login: true, binding_allowed: true, provider_label: "统一登录",
      });
    }
    if (path === "/api/me") return migration
      ? Response.json({ detail: "migration only" }, { status: 401 })
      : Response.json({ id: "user-1", username: "alice", role: "user", ui_mode: "auto", search_profile: null });
    if (path === "/api/me/identities") return Response.json({
      linked: !migration, local_login_name: "a12345678", external_username: migration ? null : "alice", display_name: "Alice",
    });
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

test("restoring a migration token keeps binding available without business requests or subscriptions", async () => {
  const requests = authenticationServer(true);
  setToken("migration-token");
  render(<Home />);
  await screen.findByRole("heading", { name: "关联统一身份" });
  await act(async () => {
    window.history.pushState(null, "", "/#notebook=private-notebook");
    window.dispatchEvent(new PopStateEvent("popstate"));
  });
  expect(getToken()).toBe("migration-token");
  expect(requests).toEqual([
    "/api/ready", "/api/auth/capabilities", "/api/me", "/api/me/identities",
  ]);
});

test("a business user restored in the same migration stage still receives pending actions", async () => {
  const requests = authenticationServer(false);
  setToken("sso-token");
  render(<Home />);
  await waitFor(() => expect(requests).toContain("/api/me/pending-actions/stream"));
  expect(requests).toContain("/api/me/pending-actions");
  expect(getToken()).toBe("sso-token");
});

test.each([503, "network"] as const)("capabilities failure %s preserves a migration token and retries before presenting login", async (failure) => {
  const requests = authenticationServer(true, failure);
  setToken("migration-token");
  render(<Home />);
  expect(await screen.findByRole("alert")).toHaveTextContent("认证状态暂时无法确认");
  expect(screen.queryByLabelText("用户名")).not.toBeInTheDocument();
  expect(screen.queryByLabelText("密码")).not.toBeInTheDocument();
  expect(getToken()).toBe("migration-token");
  expect(requests).toEqual(["/api/ready", "/api/auth/capabilities"]);

  await userEvent.setup().click(screen.getByRole("button", { name: "重试" }));
  await screen.findByRole("heading", { name: "关联统一身份" });
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  expect(getToken()).toBe("migration-token");
  expect(requests).not.toContain("/api/me/pending-actions");
  expect(requests).not.toContain("/api/me/pending-actions/stream");
});

test("an explicitly absent capabilities endpoint retains legacy local login", async () => {
  authenticationServer(false, 404);
  render(<Home />);
  expect(await screen.findByRole("button", { name: "本地登录" })).toBeInTheDocument();
  expect(screen.getByLabelText("用户名")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "重试" })).not.toBeInTheDocument();
});
