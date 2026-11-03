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
  cancelSsoChoice,
  createSsoAccount,
  linkSsoAccount,
  startSsoLogin,
  fetchMe,
  normalizeMemoryMode,
  setMemoryMode,
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

test("memory mode accepts suggest and folds missing or unknown backend values to manual", async () => {
  for (const [value, expected] of [
    [undefined, "manual"], [null, "manual"], ["manual", "manual"],
    ["SUGGEST", "manual"], ["future", "manual"], ["suggest", "suggest"],
  ]) {
    assert.equal(normalizeMemoryMode(value), expected);
    globalThis.fetch = async () => new Response(JSON.stringify({ id: "u1", memory_mode: value }));
    assert.equal((await fetchMe()).memory_mode, expected);
  }
});

test("setMemoryMode preserves the updated profile and server deletion count", async () => {
  setToken("memory-mode-token");
  let captured;
  globalThis.fetch = async (url, init) => {
    captured = { url, init };
    return new Response(JSON.stringify({
      user: { id: "u1", memory_mode: "manual", ui_mode: "advanced", search_profile: { version: 1 } },
      deleted_suggestion_count: 4,
    }));
  };
  const result = await setMemoryMode("manual", true);
  assert.equal(captured.url, "http://127.0.0.1:8000/api/me/memory-mode");
  assert.equal(captured.init.method, "PATCH");
  assert.equal(captured.init.headers.get("Authorization"), "Bearer memory-mode-token");
  assert.deepEqual(JSON.parse(captured.init.body), { memory_mode: "manual", delete_unadopted: true });
  assert.equal(result.deleted_suggestion_count, 4);
  assert.equal(result.user.memory_mode, "manual");
  assert.equal(result.user.ui_mode, "advanced");
  assert.deepEqual(result.user.search_profile, { version: 1 });
});

test("setMemoryMode enables without deletion by default and rejects an invalid count", async () => {
  globalThis.fetch = async (_url, init) => {
    assert.deepEqual(JSON.parse(init.body), { memory_mode: "suggest", delete_unadopted: false });
    return new Response(JSON.stringify({ user: { id: "u1", memory_mode: "suggest" }, deleted_suggestion_count: 0 }));
  };
  assert.equal((await setMemoryMode("suggest")).user.memory_mode, "suggest");
  for (const count of [-1, 0.5, "2", undefined]) {
    globalThis.fetch = async () => new Response(JSON.stringify({
      user: { id: "u1", memory_mode: "manual" }, deleted_suggestion_count: count,
    }));
    await assert.rejects(setMemoryMode("manual"), /记忆建议切换结果格式无效/);
  }
});

test("setMemoryMode preserves the deployment-off instruction from its named 409 contract", async () => {
  const message = "这项功能当前未开启，请联系管理员开启记忆建议";
  globalThis.fetch = async () => new Response(JSON.stringify({
    detail: { code: "memory_suggest_unavailable", message },
  }), { status: 409 });
  await assert.rejects(setMemoryMode("suggest"), (error) => error.message === message);
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

test("logout includes browser credentials so it revokes pending browser-bound authentication", async () => {
  let captured;
  globalThis.fetch = async (_url, init) => {
    captured = init;
    return new Response(null, { status: 204 });
  };
  await logoutUser();
  assert.equal(captured.credentials, "include");
});

test("public capabilities decide the visible authentication modes", async () => {
  globalThis.fetch = async (url, init) => {
    assert.equal(url, "http://127.0.0.1:8000/api/auth/capabilities");
    assert.equal(init.credentials, "include");
    assert.equal(init.headers.has("Authorization"), false);
    return new Response(JSON.stringify({
      sso_login: true, local_login: false, local_registration: false, provider_label: "internal",
    }), { headers: { "Content-Type": "application/json" } });
  };
  const result = await fetchAuthCapabilities();
  assert.deepEqual(result, { sso_login: true, local_login: false, local_registration: false, provider_label: "internal" });
});

test("a capabilities response missing a field is rejected rather than guessed", async () => {
  globalThis.fetch = async () => new Response(JSON.stringify({
    sso_login: true, local_login: false, provider_label: "internal",
  }), { headers: { "Content-Type": "application/json" } });
  await assert.rejects(fetchAuthCapabilities(), TypeError);
});

test("SSO starts and completion keep browser proof in cookies and tokens out of URLs", async () => {
  const calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push({ url, init });
    if (String(url).endsWith("/auth/sso/start")) {
      return new Response(JSON.stringify({ authorization_url: "https://identity.example/authorize" }), { headers: { "Content-Type": "application/json" } });
    }
    return new Response(JSON.stringify({
      status: "choice_required", pending_id: "p1", external_username: "e12345678", display_name: "Alice",
    }), { headers: { "Content-Type": "application/json" } });
  };
  assert.equal(await startSsoLogin(), "https://identity.example/authorize");
  const completion = await completeSsoLogin("handoff-code");
  assert.equal(completion.status, "choice_required");
  assert.equal(calls[0].init.credentials, "include");
  assert.equal(calls[1].init.credentials, "include");
  assert.equal(calls[1].init.body, JSON.stringify({ code: "handoff-code" }));
});

test("link, create and cancel send only the pending id (and the old credentials) in cookie-bound bodies", async () => {
  const calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push({ url: String(url), init });
    if (String(url).endsWith("/auth/sso/cancel")) return new Response(null, { status: 204 });
    return new Response(JSON.stringify({ token: "sso-token", user: { id: "u1", ui_mode: "nonsense" } }), { headers: { "Content-Type": "application/json" } });
  };
  setToken("unrelated-bearer");
  const linked = await linkSsoAccount("p1", "a12345678", "old-pw");
  assert.equal(linked.token, "sso-token");
  assert.equal(linked.user.ui_mode, "auto");
  await createSsoAccount("p1");
  await cancelSsoChoice("p1");
  assert.deepEqual(calls.map((call) => call.url), [
    "http://127.0.0.1:8000/api/auth/sso/link",
    "http://127.0.0.1:8000/api/auth/sso/create",
    "http://127.0.0.1:8000/api/auth/sso/cancel",
  ]);
  assert.equal(calls[0].init.body, JSON.stringify({ pending_id: "p1", login_name: "a12345678", password: "old-pw" }));
  assert.equal(calls[1].init.body, JSON.stringify({ pending_id: "p1" }));
  assert.equal(calls[2].init.body, JSON.stringify({ pending_id: "p1" }));
  for (const call of calls) {
    assert.equal(call.init.credentials, "include");
    assert.equal(call.init.headers.has("Authorization"), false);
  }
  clearToken();
});

test("a rejected link surfaces the server's Chinese detail for the form", async () => {
  globalThis.fetch = async () => new Response(JSON.stringify({ detail: "用户名或密码不正确" }), {
    status: 409, headers: { "Content-Type": "application/json", "X-User-Message": "1" },
  });
  await assert.rejects(linkSsoAccount("p1", "a12345678", "wrong"), (error) => error.message === "用户名或密码不正确");
});
