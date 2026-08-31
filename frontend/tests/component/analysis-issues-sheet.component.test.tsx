import { render, screen } from "@testing-library/react";
import { expect, test, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  fetchAnalysisIssues: vi.fn(),
}));

vi.mock("../../app/admin/usage/api.ts", async (importOriginal) => {
  const original = await importOriginal<typeof import("../../app/admin/usage/api.ts")>();
  return { ...original, fetchAnalysisIssues: mocks.fetchAnalysisIssues };
});

import { AnalysisIssuesSheet } from "../../app/admin/usage/AnalysisIssuesSheet.tsx";
import type { AdminUserUsage } from "../../app/admin/usage/api.ts";

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
