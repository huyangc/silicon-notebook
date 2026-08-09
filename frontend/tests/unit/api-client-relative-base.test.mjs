import test from "node:test";
import assert from "node:assert/strict";

const originalApiBase = process.env.NEXT_PUBLIC_API_BASE_URL;
const originalWindow = globalThis.window;
const originalFetch = globalThis.fetch;
process.env.NEXT_PUBLIC_API_BASE_URL = "/api";

const storage = new Map();
globalThis.window = {
  localStorage: {
    getItem: (key) => storage.get(key) ?? null,
    setItem: (key, value) => storage.set(key, String(value)),
    removeItem: (key) => storage.delete(key),
  },
  location: {
    origin: "https://frontend.example",
    reload: () => {},
  },
};

const { API_BASE } = await import("../../app/api-config.ts");
const { clearToken, getToken, setToken } = await import("../../app/auth-session.ts");
const { performApiRequest } = await import("../../app/api-client.ts");

test.afterEach(() => clearToken());
test.after(() => {
  if (originalApiBase === undefined) delete process.env.NEXT_PUBLIC_API_BASE_URL;
  else process.env.NEXT_PUBLIC_API_BASE_URL = originalApiBase;
  globalThis.window = originalWindow;
  globalThis.fetch = originalFetch;
});

test("relative API_BASE resolves browser API paths with bearer authentication", async () => {
  setToken("relative-token");
  const captures = [];
  globalThis.fetch = async (url, init) => {
    captures.push({ url, init });
    return new Response(null, { status: 204 });
  };

  await performApiRequest("/me", { tag: "relative" });
  await performApiRequest("https://frontend.example/api/", { tag: "relative" });
  assert.equal(API_BASE, "/api");
  assert.deepEqual(captures.map(({ url }) => url), [
    "https://frontend.example/api/me",
    "https://frontend.example/api/",
  ]);
  assert.equal(captures[0].init.headers.get("Authorization"), "Bearer relative-token");
});

test("relative API_BASE rejects traversal before an authenticated fetch", async () => {
  setToken("relative-token");
  let fetchCalls = 0;
  globalThis.fetch = async () => {
    fetchCalls += 1;
    return new Response(null, { status: 204 });
  };

  for (const escape of ["/../me", "/%2e%2e/me", "https://frontend.example/api/../me"]) {
    await assert.rejects(
      performApiRequest(escape, { tag: "relative" }),
      { name: "TypeError", message: "authenticated API requests must stay under API_BASE" },
    );
  }
  assert.equal(fetchCalls, 0);
  assert.equal(getToken(), "relative-token");
});
