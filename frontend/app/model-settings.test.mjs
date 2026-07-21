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

function form(overrides = {}) {
  return {
    base_url: "https://saved.example/v1",
    model: "saved-model",
    api_key: "",
    baseUrlDirty: false,
    modelDirty: false,
    keyDirty: false,
    ...overrides,
  };
}

function forms(overrides = {}) {
  return Object.fromEntries(MODEL_ROLES.map((role) => [
    role,
    form(overrides[role]),
  ]));
}

test("buildPutPayload returns an empty patch for untouched forms", () => {
  assert.deepEqual(buildPutPayload(forms()), {});
});

test("buildPutPayload emits only the dirty role and dirty field", () => {
  const payload = buildPutPayload(forms({
    llm: { model: " changed-model ", modelDirty: true },
  }));

  assert.deepEqual(payload, { llm: { model: "changed-model" } });
  assert.equal("reasoning_llm" in payload, false);
});

test("buildPutPayload preserves explicit empty clears for every dirty field", () => {
  assert.deepEqual(buildPutPayload(forms({
    llm: {
      base_url: "",
      model: "",
      api_key: "",
      baseUrlDirty: true,
      modelDirty: true,
      keyDirty: true,
    },
  })), {
    llm: { base_url: "", model: "", api_key: "" },
  });
});

test("two stale tabs editing disjoint fields produce disjoint patches", () => {
  const tabA = buildPutPayload(forms({
    llm: { model: "tab-a-model", modelDirty: true },
  }));
  const tabB = buildPutPayload(forms({
    rerank: { base_url: " https://tab-b.example/v1 ", baseUrlDirty: true },
  }));

  assert.deepEqual(tabA, { llm: { model: "tab-a-model" } });
  assert.deepEqual(tabB, { rerank: { base_url: "https://tab-b.example/v1" } });
});

test("modelSettingsForms resets every field dirty flag", async () => {
  const modelSettings = await import("./model-settings.ts");
  assert.equal(typeof modelSettings.modelSettingsForms, "function");
  const view = Object.fromEntries(MODEL_ROLES.map((role) => [role, {
    base_url: `https://${role}.example/v1`,
    model: `${role}-model`,
    has_key: true,
    key_hint: "…cret",
    source: "user",
  }]));

  const reset = modelSettings.modelSettingsForms(view);
  for (const role of MODEL_ROLES) {
    assert.deepEqual(reset[role], {
      base_url: view[role].base_url,
      model: view[role].model,
      api_key: "",
      baseUrlDirty: false,
      modelDirty: false,
      keyDirty: false,
    });
  }
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
  assert.equal(calls[0].init.headers.get("Authorization"), "Bearer saved-status-token");
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
    assert.equal(init.headers.get("Authorization"), "Bearer explicit-test-token");
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
