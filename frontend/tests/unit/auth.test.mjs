import { test } from "node:test";
import assert from "node:assert/strict";
import { clearToken, isValidUsername, loginUser, logoutUser, setToken } from "../../app/auth.ts";

const storage = new Map();
const originalFetch = globalThis.fetch;
const originalWindow = globalThis.window;

globalThis.window = {
  localStorage: {
    getItem: (key) => storage.get(key) ?? null,
    setItem: (key, value) => storage.set(key, String(value)),
    removeItem: (key) => storage.delete(key),
  },
  location: { reload: () => {} },
};

test.afterEach(() => clearToken());
test.after(() => {
  globalThis.fetch = originalFetch;
  globalThis.window = originalWindow;
});

test("username accepts a single lowercase letter + 8 digits", () => {
  assert.ok(isValidUsername("a12345678"));
  assert.ok(isValidUsername("b01999999"));
  assert.ok(isValidUsername("m00000042"));
});

test("username rejects bad shapes (incl. uppercase)", () => {
  assert.ok(!isValidUsername("00123456"));
  assert.ok(!isValidUsername("A00123456"));   // 大写
  assert.ok(!isValidUsername("ab00123456"));  // 多个字母
  assert.ok(!isValidUsername("a1234567"));
  assert.ok(!isValidUsername("a123456789"));
  assert.ok(!isValidUsername("a１２３４５６７８"));
});

test("login remains unauthenticated even when a stale token exists", async () => {
  setToken("stale-token");
  let captured;
  globalThis.fetch = async (url, init) => {
    captured = { url, init };
    return new Response(JSON.stringify({ token: "new-token", user: { id: "u", username: "a00123456" } }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  };

  await loginUser("a00123456", "pw");
  assert.equal(captured.url, "http://127.0.0.1:8000/api/auth/login");
  assert.equal(captured.init.headers.has("Authorization"), false);
});

test("logout clears the local token when the network request fails", async () => {
  setToken("tok-1");
  globalThis.fetch = async () => { throw new TypeError("offline"); };

  await logoutUser();
  assert.equal(storage.has("silicon_notebook_token"), false);
});
