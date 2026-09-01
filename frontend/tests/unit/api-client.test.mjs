import test from "node:test";
import assert from "node:assert/strict";

import { API_BASE } from "../../app/api-config.ts";
import { clearToken, getToken, setToken } from "../../app/auth-session.ts";
import { performApiRequest, requestBlob, requestJson, requestVoid } from "../../app/api-client.ts";

const storage = new Map();
let reloads = 0;
const originalFetch = globalThis.fetch;
const originalWindow = globalThis.window;

globalThis.window = {
  localStorage: {
    getItem: (key) => storage.get(key) ?? null,
    setItem: (key, value) => storage.set(key, String(value)),
    removeItem: (key) => storage.delete(key),
  },
  location: { reload: () => { reloads += 1; } },
};

test.afterEach(() => {
  clearToken();
  reloads = 0;
});

test.after(() => {
  globalThis.fetch = originalFetch;
  globalThis.window = originalWindow;
});

test("requestJson applies bearer and JSON headers and parses the body", async () => {
  setToken("tok-1");
  let captured;
  globalThis.fetch = async (url, init) => {
    captured = { url, init };
    return new Response(JSON.stringify({ ok: true }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  };

  assert.deepEqual(await requestJson("/health", { method: "POST", body: "{}", tag: "test" }), { ok: true });
  assert.equal(captured.url, "http://127.0.0.1:8000/api/health");
  assert.equal(captured.init.headers.get("Authorization"), "Bearer tok-1");
  assert.equal(captured.init.headers.get("Content-Type"), "application/json");
});

test("unauthenticated requests omit the bearer header", async () => {
  setToken("tok-1");
  let captured;
  globalThis.fetch = async (_url, init) => {
    captured = init;
    return new Response(JSON.stringify({ ok: true }), { status: 200 });
  };

  await requestJson("/auth/login", { auth: "none", method: "POST", body: "{}", tag: "auth" });
  assert.equal(captured.headers.has("Authorization"), false);
});

test("FormData keeps its browser-owned content type boundary", async () => {
  let captured;
  globalThis.fetch = async (_url, init) => {
    captured = init;
    return new Response(null, { status: 204 });
  };
  const body = new FormData();
  body.set("source", "notes");

  await requestVoid("/sources", { method: "POST", body, tag: "upload" });
  assert.equal(captured.headers.has("Content-Type"), false);
});

test("requestVoid accepts 204 without parsing JSON", async () => {
  globalThis.fetch = async () => new Response(null, { status: 204 });
  await requestVoid("/notebooks/nb/share", { method: "DELETE", tag: "test" });
});

test("requestVoid accepts 202 with a JSON body without parsing it (batch 3·W1 PR-3 zero-change anchor)", async () => {
  // DELETE /notebooks/{id} moved from 204 (no body) to 202 with a
  // {"status":"deleting"} JSON body (design doc §T-2/§5). requestVoid's own
  // contract is "checked(...) then discard the body" — it only inspects
  // response.ok (any 2xx), so this status/body change is transparent to
  // every caller, notebook-api.ts's deleteNotebook included. This test pins
  // that transparency directly: if requestVoid ever started requiring a
  // specific status code or attempted to parse the body, this would fail.
  globalThis.fetch = async () =>
    new Response(JSON.stringify({ status: "deleting" }), {
      status: 202,
      headers: { "Content-Type": "application/json" },
    });
  await requestVoid("/notebooks/nb", { method: "DELETE", tag: "test" });
});

test("requestBlob returns the response blob", async () => {
  const bytes = new Uint8Array([1, 2, 3]);
  globalThis.fetch = async () => new Response(bytes, { status: 200, headers: { "Content-Type": "image/png" } });

  const blob = await requestBlob("/sources/file", { tag: "download" });
  assert.equal(blob.type, "image/png");
  assert.deepEqual([...new Uint8Array(await blob.arrayBuffer())], [1, 2, 3]);
});

test("performApiRequest passes caller abort signals directly to fetch", async () => {
  const controller = new AbortController();
  let captured;
  globalThis.fetch = async (_url, init) => {
    captured = init;
    return new Response(null, { status: 204 });
  };

  await performApiRequest("/jobs", { signal: controller.signal, tag: "job" });
  assert.strictEqual(captured.signal, controller.signal);
});

test("requestJson delegates marked HTTP failures to the trusted error layer", async () => {
  globalThis.fetch = async () => new Response(JSON.stringify({ detail: "请求无效" }), {
    status: 422,
    headers: { "Content-Type": "application/json", "X-User-Message": "1" },
  });

  await assert.rejects(
    requestJson("/notebooks", { method: "POST", body: "{}", tag: "notebook" }),
    { message: "请求无效" },
  );
});

test("performApiRequest exposes a reviewed raw response for special policy", async () => {
  globalThis.fetch = async () => new Response("forbidden", { status: 403 });
  const response = await performApiRequest("/admin/users", { tag: "admin" });
  assert.equal(response.status, 403);
});

test("default 401 policy preserves the stored session", async () => {
  setToken("tok-1");
  globalThis.fetch = async () => new Response(null, { status: 401 });

  await performApiRequest("/me", { tag: "auth" });
  assert.equal(getToken(), "tok-1");
  assert.equal(reloads, 0);
});

test("clear-and-reload 401 policy clears the token before reloading", async () => {
  setToken("tok-1");
  globalThis.fetch = async () => new Response(null, { status: 401 });

  await performApiRequest("/me", { tag: "auth", unauthorized: "clear-and-reload" });
  assert.equal(getToken(), "");
  assert.equal(reloads, 1);
});

test("fetch rejections propagate without being rewritten", async () => {
  const failure = new TypeError("network down");
  globalThis.fetch = async () => { throw failure; };

  await assert.rejects(
    requestJson("/health", { tag: "test" }),
    (error) => error === failure,
  );
});

test("authenticated API calls reject URLs outside the configured API base", async () => {
  await assert.rejects(
    performApiRequest("https://outside.example/api", { tag: "test" }),
    { name: "TypeError", message: "authenticated API requests must stay under API_BASE" },
  );
});

test("normalized API base and child URLs remain valid", async () => {
  const urls = [];
  globalThis.fetch = async (url) => {
    urls.push(url);
    return new Response(null, { status: 204 });
  };

  await performApiRequest(API_BASE, { tag: "test" });
  await performApiRequest(`${API_BASE}/me?view=profile`, { tag: "test" });
  assert.deepEqual(urls, [API_BASE, `${API_BASE}/me?view=profile`]);
});

test("authenticated requests reject normalized traversal before fetch", async () => {
  setToken("tok-1");
  let fetchCalls = 0;
  globalThis.fetch = async () => {
    fetchCalls += 1;
    return new Response(null, { status: 204 });
  };

  for (const escape of [
    `${API_BASE}/../me`,
    `${API_BASE}/%2e%2e/me`,
    "/../me",
    "/%2e%2e/me",
  ]) {
    await assert.rejects(
      performApiRequest(escape, { tag: "security" }),
      { name: "TypeError", message: "authenticated API requests must stay under API_BASE" },
    );
  }
  assert.equal(fetchCalls, 0, "the bearer-bearing request must not reach fetch");
  assert.equal(getToken(), "tok-1");
});
