import test from "node:test";
import assert from "node:assert/strict";

import {
  ModelTestCoordinator,
  acceptModelServiceStatusSnapshot,
  applyModelServiceTestResult,
  deriveModelServiceSummaryView,
} from "./model-service-orchestration.ts";


function status(service, serviceStatus, overrides = {}) {
  return {
    service,
    model: `${service}-runtime`,
    source: "system",
    kind: service === "embedding" ? "embedding" : service === "rerank" ? "rerank" : "llm",
    configured: true,
    required: service === "llm",
    status: serviceStatus,
    latency_ms: 42,
    checked_at: "2030-01-01T00:00:00Z",
    trigger: "manual_test",
    code: serviceStatus === "error" ? "upstream_error" : "",
    ...overrides,
  };
}


test("operational progress and errors take precedence over an idle model summary", () => {
  const modelStatus = { services: [status("llm", "ok")] };

  assert.deepEqual(
    deriveModelServiceSummaryView({
      apiStatus: "ok",
      statusText: "正在处理来源（已 3s · 2 个）",
      modelStatus,
      modelStatusUnavailable: false,
    }),
    {
      text: "正在处理来源（已 3s · 2 个）",
      tone: "connecting",
      title: "正在处理来源（已 3s · 2 个）",
    },
  );
  assert.deepEqual(
    deriveModelServiceSummaryView({
      apiStatus: "ok",
      statusText: "服务出了点问题，请稍后重试",
      modelStatus,
      modelStatusUnavailable: false,
    }),
    {
      text: "服务出了点问题，请稍后重试",
      tone: "bad",
      title: "服务出了点问题，请稍后重试",
    },
  );
  assert.equal(
    deriveModelServiceSummaryView({
      apiStatus: "ok",
      statusText: "https://share.example/notebook",
      modelStatus,
      modelStatusUnavailable: false,
    }).tone,
    "warn",
  );
});


test("idle service text yields to persisted model state and status availability", () => {
  assert.equal(
    deriveModelServiceSummaryView({
      apiStatus: "ok",
      statusText: "服务正常",
      modelStatus: { services: [status("llm", "error")] },
      modelStatusUnavailable: false,
    }).text,
    "API 正常 · 1 个模型异常",
  );
  assert.equal(
    deriveModelServiceSummaryView({
      apiStatus: "ok",
      statusText: "服务正常 · 模型未配置",
      modelStatus: null,
      modelStatusUnavailable: true,
    }).text,
    "API 正常 · 模型状态未知",
  );
});


test("one successful role test keeps a failed or partial status snapshot unavailable", () => {
  const fromFailedGet = applyModelServiceTestResult(
    { status: null, unavailable: true },
    status("llm", "ok"),
    "single",
  );
  assert.equal(fromFailedGet.unavailable, true);
  assert.deepEqual(fromFailedGet.status.services.map((item) => item.service), ["llm"]);
  assert.equal(
    deriveModelServiceSummaryView({
      apiStatus: "ok",
      statusText: "服务正常",
      modelStatus: fromFailedGet.status,
      modelStatusUnavailable: fromFailedGet.unavailable,
    }).text,
    "API 正常 · 模型状态未知",
  );

  const fromPartialSnapshot = applyModelServiceTestResult(
    { status: { services: [status("llm", "untested")] }, unavailable: false },
    status("reasoning_llm", "ok"),
    "single",
  );
  assert.equal(fromPartialSnapshot.unavailable, true);
});


test("single tests update a complete snapshot and all-service tests restore availability", () => {
  const completeSnapshot = {
    services: [
      status("llm", "untested"),
      status("reasoning_llm", "untested"),
      status("rewrite_llm", "untested"),
      status("kg_llm", "untested"),
      status("rerank", "untested"),
      status("embedding", "untested"),
    ],
  };
  const afterSingle = applyModelServiceTestResult(
    { status: completeSnapshot, unavailable: false },
    status("llm", "ok", { model: "tested-primary" }),
    "single",
  );
  assert.equal(afterSingle.unavailable, false);
  assert.equal(afterSingle.status.services[0].model, "tested-primary");
  assert.equal(afterSingle.status.services[0].status, "ok");

  const afterAll = applyModelServiceTestResult(
    { status: null, unavailable: true },
    completeSnapshot,
    "all",
  );
  assert.equal(afterAll.unavailable, false);
  assert.equal(afterAll.status.services.length, 6);
});


test("only a complete GET snapshot clears model status unavailability", () => {
  const partial = acceptModelServiceStatusSnapshot({
    services: [status("llm", "ok")],
  });
  assert.equal(partial.unavailable, true);

  const complete = acceptModelServiceStatusSnapshot({
    services: [
      status("llm", "ok"),
      status("reasoning_llm", "ok"),
      status("rewrite_llm", "ok"),
      status("kg_llm", "ok"),
      status("rerank", "ok"),
      status("embedding", "ok"),
    ],
  });
  assert.equal(complete.unavailable, false);
});


test("persisted single and Ask test ownership survives panel close and rejects stale completions", () => {
  const coordinator = new ModelTestCoordinator();
  const ticket = coordinator.beginOne("reasoning_llm");
  assert.ok(ticket);
  assert.deepEqual(coordinator.snapshot(), {
    roles: { reasoning_llm: true },
    all: false,
  });

  // Closing/reopening the panel does not replace the page-owned coordinator.
  assert.equal(coordinator.beginOne("reasoning_llm"), null);
  assert.equal(coordinator.beginAll(), null);
  assert.equal(coordinator.hasInFlight(), true);
  assert.equal(coordinator.isCurrent(ticket), true);

  coordinator.invalidateConfiguration();
  assert.equal(coordinator.isCurrent(ticket), false);
  coordinator.finish(ticket);
  assert.equal(coordinator.hasInFlight(), false);
});


test("all-model ownership blocks role tests and becomes stale after a config epoch change", () => {
  const coordinator = new ModelTestCoordinator();

  const allTicket = coordinator.beginAll();
  assert.ok(allTicket);
  assert.equal(coordinator.beginOne("llm"), null);
  assert.deepEqual(coordinator.snapshot(), { roles: {}, all: true });
  coordinator.invalidateConfiguration();
  assert.equal(coordinator.isCurrent(allTicket), false);
  coordinator.finish(allTicket);
  assert.deepEqual(coordinator.snapshot(), { roles: {}, all: false });
});
