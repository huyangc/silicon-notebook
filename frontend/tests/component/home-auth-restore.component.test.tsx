import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";

import Home from "../../app/page";
import { getToken, setToken } from "../../app/auth";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  window.localStorage.clear();
  window.history.replaceState(null, "", "/");
});

function authenticationServer(migration: boolean) {
  const requests: string[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const path = new URL(String(input), window.location.origin).pathname;
    requests.push(path);
    if (path === "/api/ready") return Response.json({ ready: true });
    if (path === "/api/auth/capabilities") return Response.json({
      mode: "binding_required", local_login: true, local_registration: false,
      sso_login: true, binding_allowed: true, provider_label: "统一登录",
    });
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
