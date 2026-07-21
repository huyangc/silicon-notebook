import test from "node:test";
import assert from "node:assert/strict";
import {
  buildPutPayload,
  fetchModelServiceStatus,
  MODEL_ROLES,
  testAllCurrentModelServices,
  testCurrentModelService,
  testModelService,
} from "./model-settings.ts";

test("buildPutPayload omits api_key when not dirty", () => {
  const forms = Object.fromEntries(MODEL_ROLES.map((r) => [r,
    { base_url: " https://u/v1 ", model: " m ", api_key: "x", keyDirty: false }]));
  const p = buildPutPayload(forms);
  assert.equal(p.llm.base_url, "https://u/v1");
  assert.equal(p.llm.model, "m");
  assert.equal("api_key" in p.llm, false);
});

test("buildPutPayload includes api_key (even empty) when dirty", () => {
  const forms = Object.fromEntries(MODEL_ROLES.map((r) => [r,
    { base_url: "", model: "", api_key: "", keyDirty: true }]));
  assert.equal(buildPutPayload(forms).llm.api_key, "");
});

test("saved model status reads without a body or an implicit provider test", async () => {
  const calls = [];
  const originalFetch = globalThis.fetch;
  const originalWindow = globalThis.window;
  globalThis.window = { localStorage: { getItem: () => "saved-status-token" } };
  globalThis.fetch = async (url, init = {}) => {
    calls.push({ url: String(url), init });
    return new Response(JSON.stringify({ services: [] }), { status: 200 });
  };

  try {
    await fetchModelServiceStatus();
  } finally {
    globalThis.fetch = originalFetch;
    globalThis.window = originalWindow;
  }

  assert.equal(calls.length, 1);
  assert.match(calls[0].url, /\/me\/model-services\/status$/);
  assert.equal(calls[0].init.method, undefined);
  assert.equal(calls[0].init.body, undefined);
  assert.equal(calls[0].init.headers.Authorization, "Bearer saved-status-token");
  assert.equal(calls.some((call) => /\/test(?:-all)?$/.test(call.url)), false);
});

test("current and all model tests use authenticated POST endpoints", async () => {
  const calls = [];
  const originalFetch = globalThis.fetch;
  const originalWindow = globalThis.window;
  globalThis.window = { localStorage: { getItem: () => "explicit-test-token" } };
  globalThis.fetch = async (url, init = {}) => {
    calls.push({ url: String(url), init });
    return new Response(JSON.stringify({ services: [] }), { status: 200 });
  };

  try {
    await testCurrentModelService("reasoning_llm");
    await testAllCurrentModelServices();
  } finally {
    globalThis.fetch = originalFetch;
    globalThis.window = originalWindow;
  }

  assert.deepEqual(calls.map((call) => call.url.replace(/^.*\/api/, "")), [
    "/me/model-services/reasoning_llm/test",
    "/me/model-services/test-all",
  ]);
  for (const { init } of calls) {
    assert.equal(init.method, "POST");
    assert.equal(init.headers.Authorization, "Bearer explicit-test-token");
  }
});

test("draft model tests discard legacy raw diagnostics from a 200 response", async () => {
  const originalFetch = globalThis.fetch;
  const originalWindow = globalThis.window;
  globalThis.window = { localStorage: { getItem: () => "draft-test-token" } };
  globalThis.fetch = async () => new Response(JSON.stringify({
    ok: false,
    latency_ms: 17,
    code: "upstream_error",
    error: "RuntimeError: provider https://10.0.0.8 leaked sk-private-secret",
    provider_diagnostic: "secondary secret response body",
  }), { status: 200 });

  try {
    const result = await testModelService(
      "llm", "https://provider.example/v1", "model-x", null,
    );
    assert.equal(result.error, "");
    assert.equal(result.code, "upstream_error");
    assert.doesNotMatch(
      JSON.stringify(result),
      /10\.0\.0\.8|sk-private-secret|RuntimeError|secondary secret/,
    );
  } finally {
    globalThis.fetch = originalFetch;
    globalThis.window = originalWindow;
  }
});
