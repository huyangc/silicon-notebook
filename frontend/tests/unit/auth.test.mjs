import { test } from "node:test";
import assert from "node:assert/strict";
import {
  clearToken,
  completeSsoLogin,
  fetchAuthCapabilities,
  isValidUsername,
  loginUser,
  logoutUser,
  setToken,
  startIdentityBinding,
  startSsoLogin,
} from "../../app/auth.ts";

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

test("public capabilities decide the visible authentication modes", async () => {
  globalThis.fetch = async (url, init) => {
    assert.equal(url, "http://127.0.0.1:8000/api/auth/capabilities");
    assert.equal(init.credentials, "include");
    assert.equal(init.headers.has("Authorization"), false);
    return new Response(JSON.stringify({
      mode: "dual", local_login: true, local_registration: false, sso_login: true,
      binding_allowed: true, provider_label: "internal",
    }), { headers: { "Content-Type": "application/json" } });
  };
  const result = await fetchAuthCapabilities();
  assert.equal(result.mode, "dual");
  assert.equal(result.sso_login, true);
});

test("SSO starts and completion keep browser proof in cookies and tokens out of URLs", async () => {
  const calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push({ url, init });
    if (String(url).endsWith("/auth/sso/start")) {
      return new Response(JSON.stringify({ authorization_url: "https://identity.example/authorize" }), { headers: { "Content-Type": "application/json" } });
    }
    return new Response(JSON.stringify({
      status: "binding_required", pending_id: "p1", local_login_name: "a12345678", external_username: "alice", display_name: "Alice",
    }), { headers: { "Content-Type": "application/json" } });
  };
  assert.equal(await startSsoLogin(), "https://identity.example/authorize");
  const completion = await completeSsoLogin("handoff-code");
  assert.equal(completion.status, "binding_required");
  assert.equal(calls[0].init.credentials, "include");
  assert.equal(calls[1].init.credentials, "include");
  assert.equal(calls[1].init.body, JSON.stringify({ code: "handoff-code" }));
});

test("binding start sends the local verification only in its request body", async () => {
  globalThis.fetch = async (url, init) => {
    assert.equal(url, "http://127.0.0.1:8000/api/me/identity-binding/start");
    assert.equal(init.body, JSON.stringify({ current_password: "verify-me" }));
    assert.equal(init.credentials, "include");
    return new Response(JSON.stringify({ authorization_url: "https://identity.example/authorize" }), { headers: { "Content-Type": "application/json" } });
  };
  assert.equal(await startIdentityBinding("verify-me"), "https://identity.example/authorize");
});
