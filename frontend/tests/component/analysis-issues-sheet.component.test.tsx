import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  fetchAnalysisIssues: vi.fn(),
  fetchAnalysisIssueModelArtifact: vi.fn(),
}));

vi.mock("../../app/admin/usage/api.ts", async (importOriginal) => {
  const original = await importOriginal<typeof import("../../app/admin/usage/api.ts")>();
  return {
    ...original,
    fetchAnalysisIssues: mocks.fetchAnalysisIssues,
    fetchAnalysisIssueModelArtifact: mocks.fetchAnalysisIssueModelArtifact,
  };
});

import { AnalysisIssuesSheet } from "../../app/admin/usage/AnalysisIssuesSheet.tsx";
import type { AdminUserUsage } from "../../app/admin/usage/api.ts";

test("模型格式问题按需显示完整提问和原始回答", async () => {
  window.history.replaceState({}, "", "/admin/usage?sheet=issues");
  mocks.fetchAnalysisIssues.mockResolvedValue([{
    id: "analysis-model-case-1",
    category: "model_output",
    status: "open",
    code: "MODEL_OUTPUT_INVALID_JSON_CONTRACT",
    summary: "模型回答未通过 JSON 协议校验",
    owner_id: "user-1",
    notebook_id: "nb-1",
    notebook_name: "",
    source_id: "",
    source_title: "",
    file_name: "",
    source_type: "",
    workload_id: "ask_answer",
    workload_label: "问答回答",
    model_area: "ask",
    failure_kind: "schema_mismatch",
    support_id: "mdl-safe",
    parent_id: "ask-1",
    created_at: "2026-08-31T01:00:00+00:00",
    updated_at: "2026-08-31T01:00:00+00:00",
    resolved_at: "",
    expires_at: "2026-09-30T01:00:00+00:00",
    artifact_available: true,
    source_deleted: false,
    notebook_deleted: false,
  }]);
  mocks.fetchAnalysisIssueModelArtifact.mockResolvedValue({
    issue_id: "analysis-model-case-1",
    question: "原始问题",
    messages: [{ role: "user", content: "完整请求" }],
    schema_hint: '{"answer":""}',
    response: '{"answer":[]}',
    workload_id: "ask_answer",
    workload_label: "问答回答",
    model_area: "ask",
    failure_kind: "schema_mismatch",
    support_id: "mdl-safe",
    parent_id: "ask-1",
    reason: "invalid_type",
    occurred_at: "2026-08-31T01:00:00+00:00",
  });
  const users: AdminUserUsage[] = [{
    id: "user-1", username: "a12345678", role: "user",
    created_at: "2026-08-01T00:00:00+00:00", notebooks: 1, sources: 1,
    conversations: 0, questions: 0, reports: 0, last_active: null,
    is_online: false, role_mutable: true, upload_limit: 20,
    upload_limit_overridden: false,
  }];

  render(<AnalysisIssuesSheet users={users} />);
  await userEvent.click(await screen.findByRole("button", { name: "查看提问与回答" }));

  expect(await screen.findByText("原始问题")).toBeInTheDocument();
  expect(screen.getByText('{"answer":[]}')).toBeInTheDocument();
  expect(mocks.fetchAnalysisIssueModelArtifact).toHaveBeenCalledWith(
    "analysis-model-case-1",
  );
  const summary = screen.getByText("查看完整模型请求与 JSON 契约");
  expect(summary.closest("details")).not.toHaveAttribute("open");
  await userEvent.click(summary);
  expect(summary.closest("details")).toHaveAttribute("open");
  expect(screen.getByText("完整请求")).toBeInTheDocument();
});

test("存活解析问题链接到管理员只读来源详情而非普通用户工作区", async () => {
  window.history.replaceState({}, "", "/admin/usage?sheet=issues");
  mocks.fetchAnalysisIssues.mockResolvedValue([{
    id: "issue-1",
    category: "spreadsheet_analysis",
    status: "open",
    code: "SPREADSHEET_INVALID_OOXML",
    summary: "无法读取工作簿",
    owner_id: "user-1",
    notebook_id: "nb-1",
    notebook_name: "Private notebook",
    source_id: "src-1",
    source_title: "Private source",
    file_name: "private.xlsx",
    source_type: "xlsx",
    workload_id: "",
    workload_label: "",
    model_area: "",
    failure_kind: "",
    support_id: "",
    parent_id: "",
    created_at: "2026-08-31T01:00:00+00:00",
    updated_at: "2026-08-31T01:00:00+00:00",
    resolved_at: "",
    expires_at: "2026-09-30T01:00:00+00:00",
    artifact_available: true,
    source_deleted: false,
    notebook_deleted: false,
  }]);
  const users: AdminUserUsage[] = [{
    id: "user-1",
    username: "a12345678",
    role: "user",
    created_at: "2026-08-01T00:00:00+00:00",
    notebooks: 1,
    sources: 1,
    conversations: 0,
    questions: 0,
    reports: 0,
    last_active: null,
    is_online: false,
    role_mutable: true,
    upload_limit: 20,
    upload_limit_overridden: false,
  }];

  render(<AnalysisIssuesSheet users={users} />);

  const link = await screen.findByRole("link", { name: "查看来源详情" });
  expect(link).toHaveAttribute(
    "href",
    "/dev/logs?view=activity&owner=user-1&activity_type=source&notebook_id=nb-1&source_id=src-1",
  );
});
