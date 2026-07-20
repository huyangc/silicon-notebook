import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import {
  isTrackedKgTerminal,
  ownsKgBuildRequest,
  kgBuildPresentation,
  kgBuildTerminalToast,
} from "./kg-build-status.ts";

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
  assert.equal(
    kgBuildPresentation(
      job("running", "extracting", {
        completed_sources: 12,
        total_sources: 80,
      }),
      68,
      true,
    ).label,
    "正在分析 12/80",
  );
  assert.equal(
    kgBuildPresentation(job("running", "stopping"), 68, true).label,
    "模型服务异常，正在停止本次分析…",
  );
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
  assert.equal(view.label, "分析已中断 · 已完成 12/80");
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
  assert.equal(view.label, "分析完成 · 3 篇未完成");
  assert.equal(view.actionLabel, "继续分析未完成内容");
  assert.equal(view.tone, "warning");
  assert.equal(
    kgBuildTerminalToast(completed),
    "知识图谱分析完成，但有 3 篇未完成",
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

test("durable KG polling never synthesizes completion after a time cap", () => {
  const source = readFileSync(new URL("./page.tsx", import.meta.url), "utf8");
  const start = source.indexOf("// While a build runs");
  const end = source.indexOf("// Backfill completion polling", start);
  assert.notEqual(start, -1);
  assert.notEqual(end, -1);
  const kgPoll = source.slice(start, end);
  assert.doesNotMatch(kgPoll, /setTimeout\s*\(/);
  assert.doesNotMatch(kgPoll, /仍在后台进行，请稍后刷新/);
});
