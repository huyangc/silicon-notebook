import test from "node:test";
import assert from "node:assert/strict";

import {
  ModelTestCoordinator,
  acceptModelServiceStatusSnapshot,
  applyModelServiceTestResult,
  deriveModelServiceSummaryView,
} from "../../app/model-service-orchestration.ts";


function status(serviceId, serviceStatus, overrides = {}) {
  return {
    service_id: serviceId,
    display_name: `${serviceId} 服务`,
    kind: "chat",
    model: "runtime",
    workloads: [],
    status: serviceStatus,
    active: 0,
    maximum: 3,
    queued: 0,
    oldest_wait_ms: 0,
    latency_ms: 42,
    checked_at: "2030-01-01T00:00:00Z",
    trigger: "manual_test",
    code: "",
    support_id: "",
    ...overrides,
  };
}


test("operational progress and API errors take precedence over model summary", () => {
  const modelStatus = { services: [status("dynamic-chat", "ok")] };
  assert.equal(deriveModelServiceSummaryView({
    apiStatus: "ok", statusText: "正在处理来源（已 3s）", modelStatus,
    modelStatusUnavailable: false,
  }).tone, "connecting");
  assert.equal(deriveModelServiceSummaryView({
    apiStatus: "error", statusText: "", modelStatus,
    modelStatusUnavailable: false,
  }).text, "服务连接异常");
});


test("GET snapshots accept dynamic service sets and single probes merge by id", () => {
  const accepted = acceptModelServiceStatusSnapshot({ services: [status("new-chat-service", "ok")] });
  assert.equal(accepted.unavailable, false);

  const afterSingle = applyModelServiceTestResult(
    accepted,
    status("new-chat-service", "busy", { active: 3 }),
    "single",
  );
  assert.equal(afterSingle.status.services[0].status, "busy");
  assert.equal(afterSingle.unavailable, false);

  const fromUnavailable = applyModelServiceTestResult(
    { status: null, unavailable: true }, status("new-chat-service", "ok"), "single",
  );
  assert.equal(fromUnavailable.unavailable, true);
  const afterAll = applyModelServiceTestResult(
    fromUnavailable, { services: [status("new-chat-service", "ok")] }, "all",
  );
  assert.equal(afterAll.unavailable, false);
});


test("one/all test ownership uses dynamic service ids and survives panel close", () => {
  const coordinator = new ModelTestCoordinator();
  const one = coordinator.beginOne("runtime-chat-west");
  assert.ok(one);
  assert.deepEqual(coordinator.snapshot(), {
    services: { "runtime-chat-west": true }, all: false,
  });
  assert.equal(coordinator.beginOne("runtime-chat-west"), null);
  assert.equal(coordinator.beginAll(), null);
  assert.equal(coordinator.isCurrent(one), true);
  coordinator.finish(one);
  assert.equal(coordinator.hasInFlight(), false);

  const all = coordinator.beginAll();
  assert.ok(all);
  assert.equal(coordinator.beginOne("runtime-rerank-east"), null);
  coordinator.finish(all);
  assert.deepEqual(coordinator.snapshot(), { services: {}, all: false });
});
