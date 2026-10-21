import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import type { ComponentType } from "react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

// jsdom cannot navigate: the sign-in round trip leaves through this one seam.
const navigation = vi.hoisted(() => ({ replace: vi.fn() }));
vi.mock("../../app/auth-return-location.ts", async (importOriginal) => ({
  ...await importOriginal<typeof import("../../app/auth-return-location.ts")>(),
  replacePageLocation: navigation.replace,
}));

import { fetchPageUser } from "../../app/page-user.ts";
import { getToken, setToken } from "../../app/auth-session.ts";
import { signInLocation, takeSignInReturnLocation } from "../../app/auth-return-location.ts";
import AdminUsagePage from "../../app/admin/usage/page";
import AdminQuestionsPage from "../../app/admin/questions/page";
import AdminExtensionsPage from "../../app/admin/extensions/page";
import AgentAccessPage from "../../app/agents/page";
import UpdatesPage from "../../app/updates/page";
import WishWallPage from "../../app/wishes/page";
import LogsPage from "../../app/dev/logs/page";
import GlobalAskPage from "../../app/ask/page";

beforeEach(() => {
  navigation.replace.mockReset();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  window.localStorage.clear();
  window.sessionStorage.clear();
  window.history.replaceState(null, "", "/");
});

const rejected = () => Response.json({ detail: "invalid or expired session" }, { status: 401 });

/** `/me` answers with `me`; every other read stays in flight, so a page can only
 * react to its session check. */
function serveMe(me: () => Response) {
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const path = new URL(String(input), window.location.origin).pathname;
    if (path === "/api/me") return me();
    return new Promise<Response>(() => undefined);
  }));
}

function settled(promise: Promise<unknown>): Promise<boolean> {
  return Promise.race([
    promise.then(() => true, () => true),
    new Promise<boolean>((resolve) => { setTimeout(() => resolve(false), 30); }),
  ]);
}

test("a rejected session clears the token it sent and leaves for sign-in, carrying this page back", async () => {
  window.history.replaceState(null, "", "/admin/usage?sheet=questions#top");
  setToken("stale-session");
  serveMe(rejected);

  const user = fetchPageUser();

  await waitFor(() => expect(navigation.replace).toHaveBeenCalledWith(
    `/?next=${encodeURIComponent("/admin/usage?sheet=questions#top")}`,
  ));
  expect(getToken()).toBe("");
  // The page keeps its loading state while it navigates away.
  expect(await settled(user)).toBe(false);
});

test("a session another tab switched in while the check was in flight is kept", async () => {
  window.history.replaceState(null, "", "/wishes");
  setToken("stale-session");
  serveMe(() => {
    setToken("fresh-session");
    return rejected();
  });

  void fetchPageUser();

  await waitFor(() => expect(navigation.replace).toHaveBeenCalledWith(`/?next=${encodeURIComponent("/wishes")}`));
  expect(getToken()).toBe("fresh-session");
});

test("no token at all also goes to sign-in", async () => {
  window.history.replaceState(null, "", "/agents");
  serveMe(() => Response.json({ detail: "authentication required" }, { status: 401 }));

  void fetchPageUser();

  await waitFor(() => expect(navigation.replace).toHaveBeenCalledWith(`/?next=${encodeURIComponent("/agents")}`));
});

test.each([
  ["a server error", () => Response.json({ detail: "boom" }, { status: 503 })],
  ["a network error", () => { throw new TypeError("Failed to fetch"); }],
])("%s is rethrown for the page's own error and retry, keeping the session", async (_name, me) => {
  window.history.replaceState(null, "", "/updates");
  setToken("live-session");
  serveMe(me);

  await expect(fetchPageUser()).rejects.toThrow();
  expect(getToken()).toBe("live-session");
  expect(navigation.replace).not.toHaveBeenCalled();
});

test("the workspace itself signs in without a return parameter", () => {
  window.history.replaceState(null, "", "/");
  expect(signInLocation()).toBe("/");
});

test("signing in returns to the same-site page the sign-in page was reached from", () => {
  window.history.replaceState(null, "", `/?next=${encodeURIComponent("/admin/usage?sheet=questions#top")}`);
  expect(takeSignInReturnLocation()).toBe("/admin/usage?sheet=questions#top");
});

test.each([
  "https://evil.example/",
  "//evil.example/",
  "/\\evil.example",
  "/%2F%2Fevil.example",
  "javascript:alert(1)",
  "admin/usage",
  "/auth/sso/callback?code=replayed",
  "/auth//sso/callback?code=replayed",
  "/",
])("return target %s is refused and dropped from the address bar", (target) => {
  window.history.replaceState(null, "", `/?group_invite=invite-1&next=${encodeURIComponent(target)}#notebook=nb-1`);

  expect(takeSignInReturnLocation()).toBe("");
  expect(window.location.search).toBe("?group_invite=invite-1");
  expect(window.location.hash).toBe("#notebook=nb-1");
});

test("without a return parameter the address bar is left alone", () => {
  window.history.replaceState(null, "", "/?group_invite=invite-1");
  expect(takeSignInReturnLocation()).toBe("");
  expect(window.location.search).toBe("?group_invite=invite-1");
});

const STANDALONE_PAGES: Array<[string, ComponentType]> = [
  ["/admin/usage?sheet=questions", AdminUsagePage],
  ["/admin/questions", AdminQuestionsPage],
  ["/admin/extensions", AdminExtensionsPage],
  ["/agents", AgentAccessPage],
  ["/updates", UpdatesPage],
  ["/wishes", WishWallPage],
  ["/dev/logs?view=llm", LogsPage],
  ["/ask?conversation_id=conv-1", GlobalAskPage],
];

test.each(STANDALONE_PAGES)("%s leaves a rejected session for sign-in instead of showing the rejection", async (path, Page) => {
  window.history.replaceState(null, "", path);
  setToken("stale-session");
  serveMe(rejected);

  render(<Page />);

  await waitFor(() => expect(navigation.replace).toHaveBeenCalledWith(`/?next=${encodeURIComponent(path)}`));
  expect(getToken()).toBe("");
  expect(screen.queryByText(/登录状态已失效/)).not.toBeInTheDocument();
});

// Without a session every read is rejected at once, not only the session check:
// none of them may show its rejection while the page leaves for sign-in.
test.each([
  ...STANDALONE_PAGES,
  ["/dev/logs", LogsPage],
  ["/dev/logs?view=activity&owner=user-2", LogsPage],
] as Array<[string, ComponentType]>)("%s shows no read's rejection when every read is rejected", async (path, Page) => {
  window.history.replaceState(null, "", path);
  const fetch = vi.fn(async () => Response.json({ detail: "authentication required" }, { status: 401 }));
  vi.stubGlobal("fetch", fetch);

  render(<Page />);

  await waitFor(() => expect(navigation.replace).toHaveBeenCalledWith(`/?next=${encodeURIComponent(path)}`));
  // Let every other rejected read land before looking.
  await act(async () => { await new Promise((resolve) => { setTimeout(resolve, 50); }); });
  expect(screen.queryByText(/登录状态已失效/)).not.toBeInTheDocument();
});

test("a standalone page keeps its error and the session when the check fails for another reason", async () => {
  window.history.replaceState(null, "", "/admin/usage");
  setToken("live-session");
  serveMe(() => Response.json({ detail: "boom" }, { status: 503 }));

  render(<AdminUsagePage />);

  expect(await screen.findByText(/加载失败/)).toBeInTheDocument();
  expect(getToken()).toBe("live-session");
  expect(navigation.replace).not.toHaveBeenCalled();
});
