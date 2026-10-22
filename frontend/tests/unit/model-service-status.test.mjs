import test from "node:test";
import assert from "node:assert/strict";

import {
  fetchModelServiceStatus,
  mergeModelServiceStatus,
  modelFailurePhenomenon,
  modelFailureText,
  modelServiceDisplayName,
  summarizeModelServices,
  testAllSystemModelServices,
  testSystemModelService,
} from "../../app/model-services.ts";
import {
  assignmentsIn,
  findFunction,
  jsxElements,
  parseModule,
} from "../../test-support/semantic-source.mjs";


const supportIdCopyModule = await parseModule("support-id-copy.tsx");


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
    "模型服务调用失败：调用未成功。",
  );
  assert.doesNotMatch(
    modelFailureText({ service_id: "private-provider-id", service_name: "", model: "" }),
    /private-provider-id/,
  );
});


test("failure rows say which model and what the reply did", () => {
  const base = {
    service_id: "chat-a", service_name: "推理服务", model: "deepseek-reasoner",
    workload_label: "问答回答",
  };
  assert.equal(
    modelFailureText({ ...base, message: "provider_rate_limited" }),
    "推理服务 deepseek-reasoner（问答回答）调用失败：上游服务限流。",
  );
  assert.equal(
    modelFailureText({ ...base, message: "model_not_found" }),
    "推理服务 deepseek-reasoner（问答回答）调用失败：模型名称不存在。",
  );
  assert.equal(
    modelFailureText({ ...base, message: "malformed_response" }),
    "推理服务 deepseek-reasoner（问答回答）调用失败：返回格式异常。",
  );
  assert.equal(
    modelFailureText({ ...base, message: "malformed_response", detail: "empty_answer" }),
    "推理服务 deepseek-reasoner（问答回答）调用失败：模型两次尝试都没有给出答案正文。",
  );
  assert.equal(
    modelFailureText({
      ...base, message: "malformed_response", detail: "invalid_enum", finish_reason: "stop",
    }),
    "推理服务 deepseek-reasoner（问答回答）调用失败：返回字段的取值不在允许范围内。",
  );
  assert.equal(
    modelFailurePhenomenon({ message: "malformed_response", detail: "incomplete_object", finish_reason: "length" }),
    "返回内容不完整，像是被截断了（输出达到长度上限被截断）。",
  );
  // Unknown or tampered codes never reach the screen as raw text.
  assert.equal(
    modelFailurePhenomenon({ message: "malformed_response", detail: "Traceback: sk-secret" }),
    "返回格式异常。",
  );
  assert.equal(
    modelFailurePhenomenon({ message: "malformed_response", detail: "future_reason" }),
    "返回内容不符合要求。",
  );
  assert.equal(modelFailurePhenomenon({ message: "future_code" }), "调用未成功。");
  assert.equal(
    modelFailureText({ service_id: "", service_name: "推理服务", message: "missing_config" }),
    "推理服务尚未配置。",
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


test("support id copy keys a pure wrapper instead of mutating refs during render", () => {
  const wrapper = findFunction(supportIdCopyModule, "SupportIdCopy");
  assert.deepEqual(
    assignmentsIn(wrapper).filter(({ target }) => target.endsWith(".current")),
    [],
  );
  assert.ok(findFunction(supportIdCopyModule, "SupportIdCopyValue"));
  assert.deepEqual(jsxElements(supportIdCopyModule, "SupportIdCopyValue"), [{
    scope: "<module>.SupportIdCopy",
    attributes: {},
    bindings: {
      key: "safeSupportId",
      supportId: "safeSupportId",
      className: "className",
    },
  }]);
});


async function withStatusBody(body, run) {
  const originalFetch = globalThis.fetch;
  const originalWindow = globalThis.window;
  globalThis.window = { localStorage: { getItem: () => "gaps-token" } };
  globalThis.fetch = async () => new Response(JSON.stringify(body), { status: 200 });
  try {
    return await run();
  } finally {
    globalThis.fetch = originalFetch;
    globalThis.window = originalWindow;
  }
}


test("binding gaps parse when present and default to empty for an older backend", async () => {
  const withGaps = await withStatusBody({
    services: [status("service-a", "ok")],
    unbound_workloads: [{ id: "agent_profile_consolidate", label: "库理解整理", feature_enabled: true }],
    stale_binding_ids: ["ask_anwser"],
    stale_binding_hidden_count: 1,
  }, fetchModelServiceStatus);
  assert.deepEqual(withGaps.unbound_workloads, [
    { id: "agent_profile_consolidate", label: "库理解整理", feature_enabled: true },
  ]);
  assert.deepEqual(withGaps.stale_binding_ids, ["ask_anwser"]);
  assert.equal(withGaps.stale_binding_hidden_count, 1);

  const legacy = await withStatusBody({ services: [status("service-a", "ok")] }, fetchModelServiceStatus);
  assert.deepEqual(legacy.unbound_workloads, []);
  assert.deepEqual(legacy.stale_binding_ids, []);
  assert.equal(legacy.stale_binding_hidden_count, 0);
});


test("malformed binding gaps are rejected like any other untrusted scalar", async () => {
  for (const extra of [
    { unbound_workloads: "agent_profile_consolidate" },
    { unbound_workloads: [{ id: "../private", label: "x", feature_enabled: true }] },
    { unbound_workloads: [{ id: "kg_glean", label: "x", feature_enabled: "yes" }] },
    { stale_binding_ids: ["/etc/silicon/secret.toml"] },
    { stale_binding_hidden_count: -1 },
  ]) {
    await assert.rejects(
      withStatusBody({ services: [status("service-a", "ok")], ...extra }, fetchModelServiceStatus),
      /模型服务状态格式无效/,
    );
  }
});


test("a single-service test result keeps the snapshot's gaps; a test-all result replaces them", () => {
  const prior = {
    services: [status("vendor-a", "untested")],
    unbound_workloads: [{ id: "kg_glean", label: "知识补充分析", feature_enabled: false }],
    stale_binding_ids: ["ask_anwser"],
    stale_binding_hidden_count: 1,
  };
  const single = mergeModelServiceStatus(prior, status("vendor-a", "ok"));
  assert.deepEqual(single.unbound_workloads, prior.unbound_workloads);
  assert.deepEqual(single.stale_binding_ids, ["ask_anwser"]);
  assert.equal(single.stale_binding_hidden_count, 1);

  const whole = mergeModelServiceStatus(prior, {
    services: [status("vendor-a", "ok")],
    unbound_workloads: [],
    stale_binding_ids: [],
    stale_binding_hidden_count: 0,
  });
  assert.deepEqual(whole.unbound_workloads, []);
  assert.deepEqual(whole.stale_binding_ids, []);
  assert.equal(whole.stale_binding_hidden_count, 0);
});
