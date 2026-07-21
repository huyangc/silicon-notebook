import test from "node:test";
import assert from "node:assert/strict";

import {
  mergeModelServiceStatus,
  modelFailureText,
  summarizeModelServices,
} from "./model-settings.ts";

function status(service, serviceStatus, overrides = {}) {
  return {
    service,
    model: `${service}-runtime`,
    source: "system",
    kind: service === "embedding" ? "embedding" : service === "rerank" ? "rerank" : "llm",
    configured: true,
    required: service === "llm",
    status: serviceStatus,
    latency_ms: 0,
    checked_at: "",
    trigger: "",
    code: "",
    ...overrides,
  };
}

test("model failure text uses the backend model name dynamically", () => {
  assert.equal(
    modelFailureText({
      service: "reasoning_llm", model: "runtime-name-A", message: "upstream_error",
    }),
    "推理模型 runtime-name-A 调用失败，本次回答可能不完整。",
  );
  assert.equal(
    modelFailureText({
      service: "reasoning_llm", model: "runtime-name-B", message: "upstream_error",
    }),
    "推理模型 runtime-name-B 调用失败，本次回答可能不完整。",
  );
});

test("only missing_config describes a blank model as unconfigured", () => {
  assert.equal(
    modelFailureText({ service: "embedding", model: "", message: "missing_config" }),
    "嵌入模型尚未配置，本次回答可能不完整。",
  );
  assert.equal(
    modelFailureText({ service: "embedding", model: "", code: "missing_config" }),
    "嵌入模型尚未配置，本次回答可能不完整。",
  );
});

test("an upstream failure with a sanitized blank model stays a generic failure", () => {
  assert.equal(
    modelFailureText({ service: "embedding", model: "", message: "upstream_error" }),
    "嵌入模型调用失败，本次回答可能不完整。",
  );
});

test("collection summary does not treat optional unconfigured models as broken", () => {
  assert.deepEqual(
    summarizeModelServices([
      status("llm", "ok"),
      status("rerank", "unconfigured", { configured: false, required: false }),
      status("embedding", "unconfigured", { configured: false, required: false }),
    ]),
    { text: "服务正常", tone: "ok", abnormal: [] },
  );
});

test("configured untested and failed services have distinct summaries", () => {
  assert.equal(summarizeModelServices([status("llm", "untested")]).text,
    "API 正常 · 模型待测试");
  assert.equal(summarizeModelServices([status("llm", "error")]).text,
    "API 正常 · 1 个模型异常");
});

test("status merge replaces by stable service role without mutating prior state", () => {
  const prior = { services: [status("llm", "untested"), status("rerank", "ok")] };
  const replacement = status("llm", "ok", { model: "live-primary", latency_ms: 42 });

  const merged = mergeModelServiceStatus(prior, replacement);

  assert.notEqual(merged, prior);
  assert.notEqual(merged.services, prior.services);
  assert.deepEqual(merged.services.map((item) => item.service), ["llm", "rerank"]);
  assert.equal(merged.services[0].model, "live-primary");
  assert.equal(merged.services[0].latency_ms, 42);
  assert.equal(prior.services[0].status, "untested");
});
