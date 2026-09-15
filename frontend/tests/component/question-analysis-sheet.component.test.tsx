import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";

// F3 回归门:提问分析页签必须只提供「问答/深度报告」两个类型按钮(不是 /dev/logs
// 默认的四选一),并且这个子集要真的传到 ActivityView 里生效——不 mock
// QuestionAnalysisSheet 本身,只 mock 它往下取数用的两个客户端模块,这样断言才是
// 在真实组件树上跑的。
const mocks = vi.hoisted(() => ({
  fetchUserActivity: vi.fn(),
  fetchUserAskDetail: vi.fn(),
  fetchUserReportDetail: vi.fn(),
  fetchUserNotebookSource: vi.fn(),
  fetchUserNotebookSources: vi.fn(),
  fetchUserNotebooks: vi.fn(),
}));

vi.mock("../../app/dev/logs/activity/api.ts", () => ({
  FORBIDDEN_SENTINEL: "forbidden",
  fetchUserActivity: mocks.fetchUserActivity,
  fetchUserAskDetail: mocks.fetchUserAskDetail,
  fetchUserReportDetail: mocks.fetchUserReportDetail,
  fetchUserNotebookSource: mocks.fetchUserNotebookSource,
  fetchUserNotebookSources: mocks.fetchUserNotebookSources,
}));
vi.mock("../../app/admin/usage/notebooks.ts", () => ({
  fetchUserNotebooks: mocks.fetchUserNotebooks,
  notebookStatusLabel: (value: string) => value,
}));

import { QuestionAnalysisSheet } from "../../app/admin/usage/QuestionAnalysisSheet";
import type { AdminUserUsage } from "../../app/admin/usage/api.ts";

function adminUser(overrides: Partial<AdminUserUsage> = {}): AdminUserUsage {
  return {
    id: "user-1",
    username: "user-one",
    role: "user",
    created_at: "2026-07-01T00:00:00",
    notebooks: 0,
    sources: 0,
    conversations: 0,
    questions: 0,
    reports: 0,
    last_active: null,
    is_online: false,
    role_mutable: true,
    upload_limit: 20,
    upload_limit_overridden: false,
    last_seen: null,
    storage_bytes: 0,
    questions_30d: 0,
    questions_failed: 0,
    reports_failed: 0,
    kg_builds: 0,
    memory_count: 0,
    knowhow_tables: 0,
    joined_notebooks: 0,
    groups: 0,
    ...overrides,
  };
}

const USERS = [
  adminUser({ id: "user-1", username: "user-one" }),
  adminUser({ id: "user-2", username: "user-two" }),
];

function page() {
  return { items: [], has_more: false, next_cursor: null };
}

beforeEach(() => {
  window.history.replaceState({}, "", "/admin/usage?sheet=questions");
  mocks.fetchUserNotebooks.mockResolvedValue([]);
  mocks.fetchUserActivity.mockResolvedValue(page());
});

function typeFilter() {
  return screen.findByRole("group", { name: "按活动类型筛选" });
}

test("提问分析页签只渲染问答/深度报告两个活动类型按钮", async () => {
  render(<QuestionAnalysisSheet currentUserId="user-1" users={USERS} />);

  const filter = await typeFilter();
  const buttons = within(filter).getAllByRole("button");
  expect(buttons).toHaveLength(2);
  expect(within(filter).getByRole("button", { name: "问答" })).toBeInTheDocument();
  expect(within(filter).getByRole("button", { name: "深度报告" })).toBeInTheDocument();
});

test("点击「深度报告」后按 activityType=report 重新取数", async () => {
  const user = userEvent.setup();
  render(<QuestionAnalysisSheet currentUserId="user-1" users={USERS} />);

  await typeFilter();
  expect(mocks.fetchUserActivity).toHaveBeenCalledWith(
    "user-1", expect.objectContaining({ activityType: "ask" }),
  );

  await user.click(screen.getByRole("button", { name: "深度报告" }));

  await waitFor(() => {
    expect(mocks.fetchUserActivity).toHaveBeenLastCalledWith(
      "user-1", expect.objectContaining({ activityType: "report" }),
    );
  });
});

test("从 owner+activity_type=report 深链进入时首个请求就是 report、按钮已按下", async () => {
  window.history.replaceState(
    {}, "", "/admin/usage?sheet=questions&owner=user-2&activity_type=report",
  );

  render(<QuestionAnalysisSheet currentUserId="user-1" users={USERS} />);

  await waitFor(() => {
    expect(mocks.fetchUserActivity).toHaveBeenCalledWith(
      "user-2", expect.objectContaining({ activityType: "report" }),
    );
  });
  // 首个(也是目前唯一的)请求就得是 report——不是先发 ask 再纠正成 report。
  expect(mocks.fetchUserActivity).toHaveBeenCalledTimes(1);

  const filter = await typeFilter();
  expect(within(filter).getByRole("button", { name: "深度报告" })).toHaveAttribute("aria-pressed", "true");
  expect(within(filter).getByRole("button", { name: "问答" })).toHaveAttribute("aria-pressed", "false");
});

test("切换用户后仍停留在「深度报告」类型", async () => {
  const user = userEvent.setup();
  render(<QuestionAnalysisSheet currentUserId="user-1" users={USERS} />);

  await typeFilter();
  await user.click(screen.getByRole("button", { name: "深度报告" }));
  await waitFor(() => {
    expect(mocks.fetchUserActivity).toHaveBeenLastCalledWith(
      "user-1", expect.objectContaining({ activityType: "report" }),
    );
  });

  await user.selectOptions(screen.getByLabelText("用户"), "user-2");

  await waitFor(() => {
    expect(mocks.fetchUserActivity).toHaveBeenLastCalledWith(
      "user-2", expect.objectContaining({ activityType: "report" }),
    );
  });
  const filter = await typeFilter();
  expect(within(filter).getByRole("button", { name: "深度报告" })).toHaveAttribute("aria-pressed", "true");
});
