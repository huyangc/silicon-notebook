import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  fetchMe: vi.fn(),
  fetchAdminUsers: vi.fn(),
  fetchOnlineIds: vi.fn(),
  updateAdminUserRole: vi.fn(),
  updateAdminUserUploadLimit: vi.fn(),
  fetchUploadLimitDefault: vi.fn(),
  updateUploadLimitDefault: vi.fn(),
  resetAdminUserPassword: vi.fn(),
  fetchUserNotebooks: vi.fn(),
  fetchAnalysisIssues: vi.fn(),
}));

vi.mock("../../app/auth.ts", () => ({ fetchMe: mocks.fetchMe }));
vi.mock("../../app/admin/usage/api.ts", () => ({
  FORBIDDEN_SENTINEL: "forbidden",
  fetchAdminUsers: mocks.fetchAdminUsers,
  fetchOnlineIds: mocks.fetchOnlineIds,
  updateAdminUserRole: mocks.updateAdminUserRole,
  updateAdminUserUploadLimit: mocks.updateAdminUserUploadLimit,
  fetchUploadLimitDefault: mocks.fetchUploadLimitDefault,
  updateUploadLimitDefault: mocks.updateUploadLimitDefault,
  resetAdminUserPassword: mocks.resetAdminUserPassword,
  fetchAnalysisIssues: mocks.fetchAnalysisIssues,
}));
vi.mock("../../app/admin/usage/notebooks.ts", () => ({
  fetchUserNotebooks: mocks.fetchUserNotebooks,
  notebookStatusLabel: (value: string) => value,
}));
vi.mock("../../app/admin/usage/QuestionAnalysisSheet.tsx", () => ({
  QuestionAnalysisSheet: () => <div>提问分析内容</div>,
}));
vi.mock("../../app/admin/usage/AnalysisIssuesSheet.tsx", () => ({
  AnalysisIssuesSheet: () => <div>解析问题内容</div>,
}));

import AdminUsagePage from "../../app/admin/usage/page";

beforeEach(() => {
  window.history.replaceState({}, "", "/admin/usage");
});

const rows = [
  {
    id: "user-local",
    username: "admin",
    role: "admin",
    created_at: "2026-07-01T00:00:00",
    notebooks: 1,
    sources: 2,
    conversations: 2,
    questions: 3,
    reports: 4,
    last_active: null,
    is_online: false,
    role_mutable: false,
    upload_limit: 20,
    upload_limit_overridden: false,
  },
  {
    id: "user-target",
    username: "a00123456",
    role: "user",
    created_at: "2026-07-02T00:00:00",
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
  },
];

// 每个用例都要自备实现(vitest 配了 restoreMocks,测试间会清实现)。
function primeCommonMocks() {
  mocks.fetchMe.mockResolvedValue({ id: "user-local", role: "admin" });
  mocks.fetchAdminUsers.mockResolvedValue(rows);
  mocks.fetchOnlineIds.mockResolvedValue([]);
  mocks.fetchUploadLimitDefault.mockResolvedValue(20);
}

async function targetRow() {
  const targetName = await screen.findByText("a00123456");
  const row = targetName.closest("tr");
  expect(row).not.toBeNull();
  return within(row as HTMLTableRowElement);
}

test("管理员可在用户总览中二次确认并授予管理员权限", async () => {
  primeCommonMocks();
  mocks.updateAdminUserRole.mockResolvedValue({
    id: "user-target",
    username: "a00123456",
    role: "admin",
  });
  const user = userEvent.setup();

  render(<AdminUsagePage />);
  const target = await targetRow();
  expect(screen.getByRole("columnheader", { name: /提问/ })).toBeInTheDocument();
  expect(screen.queryByRole("columnheader", { name: /对话/ })).not.toBeInTheDocument();
  expect(screen.queryByRole("columnheader", { name: "Excel 分析次数" })).not.toBeInTheDocument();
  expect(screen.queryByRole("columnheader", { name: "未解决解析问题数量" })).not.toBeInTheDocument();

  await user.click(target.getByRole("button", { name: "设为管理员" }));
  expect(mocks.updateAdminUserRole).not.toHaveBeenCalled();
  await user.click(target.getByRole("button", { name: "确认" }));

  expect(await screen.findByText("已授予 a00123456 管理员权限")).toBeInTheDocument();
  expect(mocks.updateAdminUserRole).toHaveBeenCalledWith("user-target", "admin");
  expect(target.getByRole("button", { name: "撤销管理员" })).toBeInTheDocument();
  const builtinRow = screen.getByText("admin").closest("tr");
  expect(within(builtinRow as HTMLTableRowElement).getByText("当前账户")).toBeInTheDocument();
});

test("用户总览用两个新页签承载只读分析，不给用户列表增加列", async () => {
  primeCommonMocks();
  const user = userEvent.setup();
  render(<AdminUsagePage />);
  await targetRow();

  await user.click(screen.getByRole("button", { name: "提问分析" }));
  expect(screen.getByText("提问分析内容")).toBeInTheDocument();
  expect(window.location.search).toContain("sheet=questions");

  await user.click(screen.getByRole("button", { name: "解析问题" }));
  expect(screen.getByText("解析问题内容")).toBeInTheDocument();
  expect(window.location.search).toContain("sheet=issues");
});

test("用户列表同时保留提问分析与该用户的 LLM 日志入口", async () => {
  primeCommonMocks();
  render(<AdminUsagePage />);
  const target = await targetRow();

  expect(target.getByRole("link", { name: "查看提问" })).toHaveAttribute(
    "href",
    "/admin/usage?sheet=questions&owner=user-target",
  );
  expect(target.getByRole("link", { name: "LLM 日志" })).toHaveAttribute(
    "href",
    "/dev/logs?owner=user-target&view=llm",
  );
});

test("管理员可保存普通用户默认文档上限", async () => {
  primeCommonMocks();
  mocks.updateUploadLimitDefault.mockResolvedValue(35);
  const user = userEvent.setup();

  render(<AdminUsagePage />);
  await targetRow(); // 等页面就绪

  const input = screen.getByLabelText("普通用户默认文档上限");
  await user.clear(input);
  await user.type(input, "35");
  // 无行处于编辑态时,页面上唯一的「保存」就是默认上限控件。
  await user.click(screen.getByRole("button", { name: "保存" }));

  expect(mocks.updateUploadLimitDefault).toHaveBeenCalledWith(35);
  expect(await screen.findByText("已将默认文档上限设为 35")).toBeInTheDocument();
});

test("非法默认上限即时拦截,不发起请求", async () => {
  primeCommonMocks();
  const user = userEvent.setup();

  render(<AdminUsagePage />);
  await targetRow();

  const input = screen.getByLabelText("普通用户默认文档上限");
  await user.clear(input);
  await user.type(input, "0");
  await user.click(screen.getByRole("button", { name: "保存" }));

  expect(mocks.updateUploadLimitDefault).not.toHaveBeenCalled();
  expect(await screen.findByText("请输入 1 到 100000 之间的整数")).toBeInTheDocument();
});

test("管理员可为某用户单独设置文档上限,行内标记转为自定义", async () => {
  primeCommonMocks();
  mocks.updateAdminUserUploadLimit.mockResolvedValue({
    id: "user-target",
    username: "a00123456",
    upload_limit: 50,
    upload_limit_overridden: true,
  });
  const user = userEvent.setup();

  render(<AdminUsagePage />);
  let target = await targetRow();
  expect(target.getByText("默认")).toBeInTheDocument();

  await user.click(target.getByRole("button", { name: "编辑" }));
  const input = target.getByLabelText("a00123456 的文档上限");
  await user.clear(input);
  await user.type(input, "50");
  await user.click(target.getByRole("button", { name: "保存" }));

  expect(mocks.updateAdminUserUploadLimit).toHaveBeenCalledWith("user-target", 50);
  expect(await screen.findByText("已将 a00123456 的文档上限设为 50")).toBeInTheDocument();
  target = await targetRow();
  expect(target.getByText("50")).toBeInTheDocument();
  expect(target.getByText("自定义")).toBeInTheDocument();
});

test("重置默认发送 null 覆盖并回落默认值", async () => {
  primeCommonMocks();
  mocks.updateAdminUserUploadLimit.mockResolvedValue({
    id: "user-target",
    username: "a00123456",
    upload_limit: 20,
    upload_limit_overridden: false,
  });
  const user = userEvent.setup();

  render(<AdminUsagePage />);
  const target = await targetRow();

  await user.click(target.getByRole("button", { name: "编辑" }));
  await user.click(target.getByRole("button", { name: "重置默认" }));

  expect(mocks.updateAdminUserUploadLimit).toHaveBeenCalledWith("user-target", null);
  expect(await screen.findByText("已恢复 a00123456 的文档上限为默认值（20）")).toBeInTheDocument();
});

test("文档上限编辑态是锚定弹出层,行内查看内容保持不变", async () => {
  primeCommonMocks();
  const user = userEvent.setup();

  render(<AdminUsagePage />);
  const target = await targetRow();

  await user.click(target.getByRole("button", { name: "编辑" }));
  // 编辑控件承载在 dialog 弹出层里;单元格自身仍是「数值 + 标签 + 编辑」,不内联展开
  // (此前内联展开会把整张表顶宽,出现横向滚动、末尾按钮被截断)。
  const popover = target.getByRole("dialog", { name: "设置 a00123456 的文档上限" });
  expect(within(popover).getByRole("button", { name: "保存" })).toBeInTheDocument();
  expect(within(popover).getByRole("button", { name: "重置默认" })).toBeInTheDocument();
  expect(within(popover).getByRole("button", { name: "取消" })).toBeInTheDocument();
  expect(target.getByText("20")).toBeInTheDocument();
  expect(target.getByText("默认")).toBeInTheDocument();
  expect(target.getByRole("button", { name: "编辑" })).toHaveAttribute("aria-expanded", "true");

  await user.keyboard("{Escape}");
  expect(target.queryByRole("dialog")).toBeNull();
  expect(mocks.updateAdminUserUploadLimit).not.toHaveBeenCalled();
});

test("管理员行的文档上限显示不限且不可编辑", async () => {
  primeCommonMocks();
  render(<AdminUsagePage />);
  await targetRow(); // 等页面就绪

  const builtinRow = screen.getByText("admin").closest("tr");
  const builtin = within(builtinRow as HTMLTableRowElement);
  expect(builtin.getByText("不限")).toBeInTheDocument();
  expect(builtin.queryByRole("button", { name: "编辑" })).toBeNull();
});

test("管理员可为普通用户重置密码;内置管理员与本人行受保护", async () => {
  primeCommonMocks();
  mocks.resetAdminUserPassword.mockResolvedValue({ id: "user-target", username: "a00123456" });
  const user = userEvent.setup();

  render(<AdminUsagePage />);
  const target = await targetRow();

  await user.click(target.getByRole("button", { name: "重置密码" }));
  const input = target.getByLabelText("a00123456 的新密码");
  await user.type(input, "new-secret-1");
  await user.click(target.getByRole("button", { name: "确认重置" }));

  expect(mocks.resetAdminUserPassword).toHaveBeenCalledWith("user-target", "new-secret-1");
  expect(await screen.findByText("已重置 a00123456 的密码，该用户需用新密码重新登录")).toBeInTheDocument();

  // 内置管理员(user-local)也是这份 fixture 里当前登录的用户,取先判定的
  // "受保护"分支,而不是"本人"。
  const builtinRow = screen.getByText("admin").closest("tr");
  expect(within(builtinRow as HTMLTableRowElement).getByText("受保护")).toBeInTheDocument();
});

test("非内置管理员看自己那行显示「本人」而非重置按钮", async () => {
  // 评审 P2:该分支是 load-bearing——被删后管理员能在自己那行点「确认重置」,
  // 后端会吊销其全部会话(reset 不带 keep_token),当场把自己登出。默认 fixture
  // 里 user-local 抢先命中「受保护」,所以要用非内置管理员当 current user 才测得到。
  const selfAdmin = {
    ...rows[1],
    id: "user-admin2",
    username: "b00123456",
    role: "admin",
    role_mutable: true,
    upload_limit: 20,
  };
  mocks.fetchMe.mockResolvedValue({ id: "user-admin2", role: "admin" });
  mocks.fetchAdminUsers.mockResolvedValue([...rows, selfAdmin]);
  mocks.fetchOnlineIds.mockResolvedValue([]);
  mocks.fetchUploadLimitDefault.mockResolvedValue(20);

  render(<AdminUsagePage />);
  const selfRow = within((await screen.findByText("b00123456")).closest("tr") as HTMLTableRowElement);
  expect(selfRow.getByText("本人")).toBeInTheDocument();
  expect(selfRow.queryByRole("button", { name: "重置密码" })).toBeNull();
  // 别的普通用户行仍有重置入口
  const target = await targetRow();
  expect(target.getByRole("button", { name: "重置密码" })).toBeInTheDocument();
});

test("用户总览按 20 条分页并支持切换每页数量", async () => {
  primeCommonMocks();
  const pagedRows = Array.from({ length: 22 }, (_, index) => ({
    ...rows[1],
    id: `user-${index}`,
    username: `user-${String(index).padStart(2, "0")}`,
    created_at: `2026-07-${String(index + 1).padStart(2, "0")}T00:00:00`,
  }));
  mocks.fetchAdminUsers.mockResolvedValue(pagedRows);
  const user = userEvent.setup();

  render(<AdminUsagePage />);

  expect(await screen.findByText("第 1 / 2 页，共 22 位用户")).toBeInTheDocument();
  expect(screen.getByText("user-00")).toBeInTheDocument();
  expect(screen.queryByText("user-20")).toBeNull();

  await user.click(screen.getByRole("button", { name: "下一页" }));
  expect(await screen.findByText("第 2 / 2 页，共 22 位用户")).toBeInTheDocument();
  expect(screen.getByText("user-20")).toBeInTheDocument();
  expect(screen.queryByText("user-00")).toBeNull();

  await user.selectOptions(screen.getByRole("combobox", { name: "每页用户数" }), "50");
  expect(await screen.findByText("第 1 / 1 页，共 22 位用户")).toBeInTheDocument();
  expect(screen.getByText("user-00")).toBeInTheDocument();
  expect(screen.getByText("user-21")).toBeInTheDocument();
});

test("点击可排序表头会对完整用户集合切换升降序", async () => {
  primeCommonMocks();
  mocks.fetchAdminUsers.mockResolvedValue([
    { ...rows[1], id: "u-a", username: "alpha", notebooks: 3, created_at: "2026-07-01T00:00:00" },
    { ...rows[1], id: "u-b", username: "beta", notebooks: 1, created_at: "2026-07-02T00:00:00" },
    { ...rows[1], id: "u-c", username: "gamma", notebooks: 2, created_at: "2026-07-03T00:00:00" },
  ]);
  const user = userEvent.setup();

  render(<AdminUsagePage />);
  await screen.findByText("alpha");
  const table = screen.getByRole("table");
  const sortButton = within(table).getByRole("button", { name: "笔记本" });

  await user.click(sortButton);
  expect(sortButton.closest("th")).toHaveAttribute("aria-sort", "ascending");
  expect(within(table).getAllByRole("row").slice(1).map((row) => row.children[1]?.textContent)).toEqual([
    "beta", "gamma", "alpha",
  ]);

  await user.click(sortButton);
  expect(sortButton.closest("th")).toHaveAttribute("aria-sort", "descending");
  expect(within(table).getAllByRole("row").slice(1).map((row) => row.children[1]?.textContent)).toEqual([
    "alpha", "gamma", "beta",
  ]);
});
