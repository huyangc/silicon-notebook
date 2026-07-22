import test from "node:test";
import assert from "node:assert/strict";

import {
  fetchModelServiceStatus,
  mergeModelServiceStatus,
  modelFailureText,
  modelServiceDisplayName,
  summarizeModelServices,
  testAllSystemModelServices,
  testSystemModelService,
} from "./model-services.ts";


function status(serviceId, serviceStatus, overrides = {}) {
  return {
    service_id: serviceId,
    display_name: "通用模型服务",
    kind: "chat",
    model: "runtime-model",
    workloads: [{ id: "ask_answer", label: "问答生成" }],
    status: serviceStatus,
    active: 1,
    maximum: 4,
    queued: 2,
    oldest_wait_ms: 120,
    latency_ms: 42,
    checked_at: "2030-01-01T00:00:00Z",
    trigger: "manual_test",
    code: serviceStatus === "error" ? "provider_unavailable" : "",
    support_id: serviceStatus === "error" ? "mdl-support_123" : "",
    ...overrides,
  };
}


test("dynamic service ids merge by service_id without a fixed role registry", () => {
  const prior = { services: [status("vendor-a", "untested"), status("future-rerank", "ok")] };
  const replacement = status("vendor-a", "busy", { active: 4, maximum: 4 });
  const merged = mergeModelServiceStatus(prior, replacement);

  assert.deepEqual(merged.services.map((item) => item.service_id), ["vendor-a", "future-rerank"]);
  assert.equal(merged.services[0].status, "busy");
  assert.equal(prior.services[0].status, "untested");
});


test("friendly labels never fall back to a raw service id", () => {
  assert.equal(modelServiceDisplayName(status("private-provider-id", "ok", { display_name: "" })), "模型服务");
  assert.equal(modelServiceDisplayName(status("private-provider-id", "ok", { display_name: "  系统推理服务  " })), "系统推理服务");
  assert.equal(
    modelFailureText({ service_id: "private-provider-id", service_name: "", model: "" }),
    "模型服务调用失败，本次回答可能不完整。",
  );
  assert.doesNotMatch(
    modelFailureText({ service_id: "private-provider-id", service_name: "", model: "" }),
    /private-provider-id/,
  );
});


test("summary distinguishes pending, busy, circuit and failures", () => {
  assert.equal(summarizeModelServices([status("a", "ok")]).text, "服务正常");
  assert.equal(summarizeModelServices([status("a", "untested")]).text, "API 正常 · 模型待检查");
  assert.equal(summarizeModelServices([status("a", "busy")]).text, "API 正常 · 1 个模型繁忙");
  assert.equal(summarizeModelServices([status("a", "circuit_open")]).text, "API 正常 · 1 个模型异常");
});


test("status GET is passive and admin tests use the new system endpoints", async () => {
  const calls = [];
  const originalFetch = globalThis.fetch;
  const originalWindow = globalThis.window;
  globalThis.window = { localStorage: { getItem: () => "system-status-token" } };
  globalThis.fetch = async (url, init = {}) => {
    calls.push({ url: String(url), init });
    const row = status("service-next", "ok");
    const body = String(url).endsWith("/test-all") || init.method !== "POST"
      ? { services: [row] }
      : row;
    return new Response(JSON.stringify(body), { status: 200 });
  };

  try {
    await fetchModelServiceStatus();
    await testSystemModelService("service-next");
    await testAllSystemModelServices();
  } finally {
    globalThis.fetch = originalFetch;
    globalThis.window = originalWindow;
  }

  assert.deepEqual(calls.map((call) => call.url.replace(/^.*\/api/, "")), [
    "/model-services/status",
    "/admin/model-services/service-next/test",
    "/admin/model-services/test-all",
  ]);
  assert.equal(calls[0].init.method, undefined);
  assert.equal(calls[0].init.body, undefined);
  assert.equal(calls[0].init.headers.get("Authorization"), "Bearer system-status-token");
  assert.equal(calls[1].init.method, "POST");
  assert.equal(calls[2].init.method, "POST");
});


test("the client rejects malformed untrusted response scalars", async () => {
  const originalFetch = globalThis.fetch;
  const originalWindow = globalThis.window;
  globalThis.window = { localStorage: { getItem: () => "validation-token" } };
  globalThis.fetch = async () => new Response(JSON.stringify({
    services: [status("service-a", "ok", { active: "four" })],
  }), { status: 200 });

  try {
    await assert.rejects(fetchModelServiceStatus(), /模型服务状态格式无效/);
  } finally {
    globalThis.fetch = originalFetch;
    globalThis.window = originalWindow;
  }
});
