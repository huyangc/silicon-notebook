import test from "node:test";
import assert from "node:assert/strict";

import {
  isTrackedKgTerminal,
  reconcileTrackedKgPoll,
  ownsKgBuildRequest,
  kgBuildPresentation,
  kgBuildTerminalToast,
} from "../../app/kg-build-status.ts";
import {
  callbackFlowsIn,
  findFunction,
  parseModule,
  stringLiterals,
} from "../../test-support/semantic-source.mjs";

function job(status, stage, extra = {}) {
  return {
    job_id: "job-1",
    mode: "incremental",
    status,
    stage,
    total_sources: 80,
    completed_sources: 0,
    failed_sources: 0,
    error_code: "",
    user_message: "",
    updated_at: "2026-07-20T00:00:00Z",
    ...extra,
  };
}

test("probing/extracting/stopping labels are explicit", () => {
  assert.equal(
    kgBuildPresentation(job("running", "probing"), 80, false).label,
    "正在连接模型服务…",
  );
  const extracting = kgBuildPresentation(
    job("running", "extracting", {
      completed_sources: 12,
      total_sources: 80,
    }),
    68,
    true,
  );
  assert.equal(extracting.label, "正在分析 12/80 项内容");
  assert.equal(extracting.detail, "正在提取知识对象与关系");
  assert.equal(
    kgBuildPresentation(
      job("running", "extracting", { failed_sources: 2 }),
      78,
      true,
    ).detail,
    "2 项内容未完成；其余内容继续处理",
  );
  assert.equal(
    kgBuildPresentation(job("running", "stopping"), 68, true).label,
    "模型服务异常，正在停止本次分析…",
  );
});

test("an operator interrupt while stopping is not reported as a model failure", () => {
  // 离线批量分析被 Ctrl-C / 终止信号结束时也会经过「正在停止」,不能沿用模型
  // 故障文案冤枉模型服务。
  const view = kgBuildPresentation(
    job("running", "stopping", { error_code: "worker_interrupted" }),
    68,
    false,
  );
  assert.equal(view.label, "正在停止本次分析…");
  assert.ok(!view.label.includes("模型服务异常"));
  assert.equal(view.detail, "正在等待已开始的请求安全退出，已完成内容会保留");
});

test("model failure preserves progress and exposes continuation", () => {
  const view = kgBuildPresentation(
    job("failed", "finished", {
      completed_sources: 12,
      total_sources: 80,
      error_code: "model_unavailable",
      user_message:
        "模型服务暂时不可用，本次分析已停止；已完成内容已保留，请在服务恢复后继续分析未完成内容。",
    }),
    68,
    true,
  );
  assert.equal(view.label, "分析已中断 · 已完成 12/80 项内容");
  assert.equal(view.detail.includes("已完成内容已保留"), true);
  assert.equal(view.actionLabel, "继续分析未完成内容");
  assert.equal(view.tone, "error");
});

test("successful completion with source warnings remains retryable", () => {
  const completed = job("succeeded", "finished", {
    completed_sources: 77,
    failed_sources: 3,
  });
  const view = kgBuildPresentation(completed, 3, true);
  assert.equal(view.label, "分析完成 · 3 项内容未完成");
  assert.equal(view.detail, "已完成内容已保留，可继续处理未完成来源");
  assert.equal(view.actionLabel, "继续分析未完成内容");
  assert.equal(view.tone, "warning");
  assert.equal(
    kgBuildTerminalToast(completed),
    "知识图谱分析完成，但有 3 项内容未完成",
  );
});

test("terminal toast uses reviewed user_message verbatim", () => {
  const failed = job("failed", "finished", {
    user_message: "安全的后端提示",
  });
  assert.equal(kgBuildTerminalToast(failed), "安全的后端提示");
  assert.equal(kgBuildTerminalToast(job("running", "extracting")), null);
});

test("old terminal response cannot finish a newer tracked job", () => {
  assert.equal(
    isTrackedKgTerminal(
      "new-job",
      job("failed", "finished", { job_id: "old-job" }),
    ),
    false,
  );
  assert.equal(
    isTrackedKgTerminal(
      "new-job",
      job("succeeded", "finished", { job_id: "new-job" }),
    ),
    true,
  );
});

test("durable poll adopts a newer cross-tab job and later observes its terminal state", () => {
  assert.deepEqual(
    reconcileTrackedKgPoll(
      "old-job",
      job("running", "extracting", { job_id: "new-job" }),
    ),
    { terminal: false, trackedJobId: "new-job" },
  );
  assert.deepEqual(
    reconcileTrackedKgPoll(
      "new-job",
      job("failed", "finished", { job_id: "new-job" }),
    ),
    { terminal: true, trackedJobId: null },
  );
  assert.deepEqual(
    reconcileTrackedKgPoll(
      "old-job",
      job("succeeded", "finished", { job_id: "new-job" }),
    ),
    { terminal: true, trackedJobId: null },
  );
});

test("KG start callbacks are owned by notebook, workspace, and request epoch", () => {
  const request = {
    notebookId: "notebook-a",
    workspaceEpoch: 7,
    requestEpoch: 3,
  };
  assert.equal(
    ownsKgBuildRequest(request, "notebook-a", 7, 3),
    true,
  );
  assert.equal(
    ownsKgBuildRequest(request, "notebook-b", 7, 3),
    false,
  );
  assert.equal(
    ownsKgBuildRequest(request, "notebook-a", 8, 3),
    false,
  );
  assert.equal(
    ownsKgBuildRequest(request, "notebook-a", 7, 4),
    false,
  );
});

test("durable KG polling never synthesizes completion after a time cap", async () => {
  const hook = await parseModule("use-kg-workspace.ts");
  const owner = findFunction(hook, "useKgWorkspace");
  const kgPoll = callbackFlowsIn(owner, "useEffect").find(
    ({ otherArguments }) => (
      otherArguments[0]
      === "[buildingKg, ownerVersion, policy.externalBuildPolling]"
    ),
  );

  assert.ok(kgPoll);
  assert.match(JSON.stringify(kgPoll), /window\.setInterval/);
  assert.doesNotMatch(JSON.stringify(kgPoll), /setTimeout/);
  assert.equal(
    stringLiterals(hook).includes("仍在后台进行，请稍后刷新"),
    false,
  );
});
