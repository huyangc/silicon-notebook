import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  fetchMe: vi.fn(),
  fetchAdminUsers: vi.fn(),
  fetchAdminQuestions: vi.fn(),
  fetchSystemConfiguration: vi.fn(),
}));

vi.mock("../../app/auth.ts", () => ({ fetchMe: mocks.fetchMe }));
vi.mock("../../app/admin/usage/api.ts", () => ({ fetchAdminUsers: mocks.fetchAdminUsers }));
vi.mock("../../app/admin/questions/api.ts", () => ({
  ADMIN_QUESTIONS_DEFAULT_LIMIT: 50,
  ADMIN_QUESTIONS_MAX_LIMIT: 200,
  ADMIN_QUESTIONS_QUERY_MAX_CHARS: 200,
  fetchAdminQuestions: mocks.fetchAdminQuestions,
}));
vi.mock("../../app/system-api.ts", () => ({ fetchSystemConfiguration: mocks.fetchSystemConfiguration }));

import AdminQuestionsPage from "../../app/admin/questions/page";

const page = {
  items: [
    {
      type: "ask",
      id: "ask-1",
      user_id: "user-1",
      username: "小林",
      notebook_id: "notebook-1",
      notebook_name: "产品研究",
      question: "这个市场的主要竞争者是谁？",
      status: "completed",
      created_at: "2026-08-31T10:00:00+08:00",
    },
    {
      type: "report",
      id: "report-1",
      user_id: "user-2",
      username: "阿青",
      notebook_id: "notebook-2",
      notebook_name: "行业资料",
      question: "分析未来三年的增长驱动因素",
      status: "running",
      created_at: "2026-08-30T09:00:00+08:00",
    },
  ],
  stats: { total: 2, asks: 1, reports: 1, active_users: 2 },
  total: 2,
  offset: 0,
  limit: 50,
};

beforeEach(() => {
  mocks.fetchMe.mockResolvedValue({ id: "admin", role: "admin" });
  mocks.fetchAdminUsers.mockResolvedValue([
    { id: "user-1", username: "小林" },
    { id: "user-2", username: "阿青" },
  ]);
  mocks.fetchAdminQuestions.mockResolvedValue(page);
  mocks.fetchSystemConfiguration.mockResolvedValue({ user_activity_view_enabled: true });
});

test("管理员可在同一处查看问答与深度报告提问及汇总", async () => {
  render(<AdminQuestionsPage />);

  expect(await screen.findByText("这个市场的主要竞争者是谁？")).toBeInTheDocument();
  expect(screen.getByText("分析未来三年的增长驱动因素")).toBeInTheDocument();
  expect(screen.getByText("产品研究")).toBeInTheDocument();
  expect(screen.getByText("行业资料")).toBeInTheDocument();
  expect(screen.getByText("活跃用户")).toBeInTheDocument();
});

test("筛选只重新加载提问，不重复获取用户目录", async () => {
  const user = userEvent.setup();
  render(<AdminQuestionsPage />);
  await screen.findByText("这个市场的主要竞争者是谁？");

  await user.click(screen.getByRole("button", { name: "深度报告" }));

  await waitFor(() => expect(mocks.fetchAdminQuestions).toHaveBeenLastCalledWith({
    kind: "report",
    userId: undefined,
    query: "",
    offset: 0,
  }));
  expect(mocks.fetchMe).toHaveBeenCalledTimes(1);
  expect(mocks.fetchAdminUsers).toHaveBeenCalledTimes(1);
});

test("普通用户不能看到全局提问数据", async () => {
  mocks.fetchMe.mockResolvedValue({ id: "user-1", role: "user" });
  render(<AdminQuestionsPage />);

  expect(await screen.findByText("仅管理员可以查看全局提问分析。")).toBeInTheDocument();
  expect(mocks.fetchAdminQuestions).not.toHaveBeenCalled();
  expect(mocks.fetchAdminUsers).not.toHaveBeenCalled();
});

test("部署能力关闭时显示明确说明且不请求提问数据", async () => {
  mocks.fetchSystemConfiguration.mockResolvedValue({ user_activity_view_enabled: false });
  render(<AdminQuestionsPage />);

  expect(await screen.findByText("当前部署未开启提问分析。")).toBeInTheDocument();
  expect(mocks.fetchAdminUsers).not.toHaveBeenCalled();
  expect(mocks.fetchAdminQuestions).not.toHaveBeenCalled();
});

test("规划中与启动中断状态使用明确文案", async () => {
  mocks.fetchAdminQuestions.mockResolvedValue({
    ...page,
    items: [
      { ...page.items[0], id: "ask-interrupted", status: "interrupted" },
      { ...page.items[1], id: "report-planning", status: "planning" },
    ],
  });
  render(<AdminQuestionsPage />);

  expect(await screen.findByText("已中断")).toBeInTheDocument();
  expect(screen.getByText("规划中")).toBeInTheDocument();
});

test("迟到的旧筛选响应不会覆盖当前提问结果", async () => {
  let resolveAsk!: (value: typeof page) => void;
  let resolveReport!: (value: typeof page) => void;
  const askResponse = new Promise<typeof page>((resolve) => { resolveAsk = resolve; });
  const reportResponse = new Promise<typeof page>((resolve) => { resolveReport = resolve; });
  mocks.fetchAdminQuestions.mockReset();
  mocks.fetchAdminQuestions
    .mockResolvedValueOnce(page)
    .mockReturnValueOnce(askResponse)
    .mockReturnValueOnce(reportResponse);
  const user = userEvent.setup();
  render(<AdminQuestionsPage />);
  await screen.findByText("这个市场的主要竞争者是谁？");

  await user.click(screen.getByRole("button", { name: "问答" }));
  await user.click(screen.getByRole("button", { name: "深度报告" }));
  resolveReport({
    ...page,
    items: [{ ...page.items[1], question: "当前筛选的深度报告" }],
    total: 1,
  });
  expect(await screen.findByText("当前筛选的深度报告")).toBeInTheDocument();

  resolveAsk({
    ...page,
    items: [{ ...page.items[0], question: "迟到的问答结果" }],
    total: 1,
  });
  await waitFor(() => expect(screen.queryByText("迟到的问答结果")).not.toBeInTheDocument());
  expect(screen.getByText("当前筛选的深度报告")).toBeInTheDocument();
});

test("加载中重复选择当前来源不会作废在途结果", async () => {
  let resolveCurrent!: (value: typeof page) => void;
  const currentResponse = new Promise<typeof page>((resolve) => { resolveCurrent = resolve; });
  mocks.fetchAdminQuestions.mockReset();
  mocks.fetchAdminQuestions.mockReturnValueOnce(currentResponse);
  const user = userEvent.setup();
  render(<AdminQuestionsPage />);
  await waitFor(() => expect(mocks.fetchAdminQuestions).toHaveBeenCalledTimes(1));

  await user.click(screen.getByRole("button", { name: "全部" }));
  resolveCurrent(page);

  expect(await screen.findByText("这个市场的主要竞争者是谁？")).toBeInTheDocument();
  expect(mocks.fetchAdminQuestions).toHaveBeenCalledTimes(1);
});

test("搜索 rail 按 Unicode 字符计数且不会静默截断 emoji", async () => {
  const user = userEvent.setup();
  render(<AdminQuestionsPage />);
  await screen.findByText("这个市场的主要竞争者是谁？");
  mocks.fetchAdminQuestions.mockClear();
  const input = screen.getByLabelText("搜索提问内容");
  const accepted = "😀".repeat(200);
  fireEvent.change(input, { target: { value: accepted } });
  await user.click(screen.getByRole("button", { name: "搜索" }));
  await waitFor(() => expect(mocks.fetchAdminQuestions).toHaveBeenCalledWith({
    kind: undefined,
    userId: undefined,
    query: accepted,
    offset: 0,
  }));

  const overLimit = "😀".repeat(201);
  fireEvent.change(input, { target: { value: overLimit } });
  await user.click(screen.getByRole("button", { name: "搜索" }));
  expect(await screen.findByText("搜索内容不能超过 200 个字符")).toBeInTheDocument();
  expect(input).toHaveValue(overLimit);
  expect(mocks.fetchAdminQuestions).toHaveBeenCalledTimes(1);
});
