import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";

vi.mock("../../app/group-api.ts", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../app/group-api.ts")>();
  return {
    ...actual,
    listGroups: vi.fn(),
    getGroup: vi.fn(),
    getGroupInvite: vi.fn(),
    createGroupInvite: vi.fn(),
    listGroupSharedNotebooks: vi.fn(),
    listGroupShareRequests: vi.fn(),
    listMyPendingShareRequests: vi.fn(),
    shareNotebookToGroup: vi.fn(),
    transferGroupOwner: vi.fn(),
    leaveGroup: vi.fn(),
  };
});

import {
  createGroupInvite,
  getGroup,
  getGroupInvite,
  listGroupShareRequests,
  listGroupSharedNotebooks,
  listGroups,
  listMyPendingShareRequests,
  leaveGroup,
  shareNotebookToGroup,
  transferGroupOwner,
  type GroupDetail,
} from "../../app/group-api.ts";
import { GroupsPage } from "../../app/groups-page.tsx";
import type { NotebookSummary } from "../../app/workspace-model.ts";

const OWNER_DETAIL: GroupDetail = {
  id: "g1",
  name: "先进封装项目",
  kind: "project",
  description: "跨团队共享封装工艺资料",
  owner_id: "u1",
  my_role: "admin",
  member_count: 2,
  created_at: "",
  members: [
    { id: "u1", username: "alice", display_name: "爱丽丝", role: "admin" },
    { id: "u2", username: "bob", display_name: "", role: "member" },
  ],
};

const NOTEBOOKS: NotebookSummary[] = [
  {
    id: "nb-owned",
    name: "先进封装工艺",
    purpose: "项目资料",
    primary_domain: "Packaging",
    status: "ready",
    counts: {},
    created_label: "刚刚",
    access: "owner",
  },
  {
    id: "nb-reader",
    name: "只读参考库",
    purpose: "",
    primary_domain: "",
    status: "ready",
    counts: {},
    created_label: "昨天",
    access: "reader",
  },
];

beforeEach(() => {
  vi.mocked(listGroups).mockResolvedValue([OWNER_DETAIL]);
  vi.mocked(getGroup).mockResolvedValue(OWNER_DETAIL);
  vi.mocked(getGroupInvite).mockResolvedValue({ active: false, token: "", created_at: null });
  vi.mocked(listGroupSharedNotebooks).mockResolvedValue([
    { notebook_id: "nb-shared", name: "共享可靠性资料", owner_username: "carol", roles: ["viewer"] },
  ]);
  vi.mocked(listGroupShareRequests).mockResolvedValue([]);
  vi.mocked(listMyPendingShareRequests).mockResolvedValue([]);
});

function renderPage(detail: GroupDetail = OWNER_DETAIL) {
  vi.mocked(listGroups).mockResolvedValue([detail]);
  vi.mocked(getGroup).mockResolvedValue(detail);
  const onChanged = vi.fn();
  const onNavigate = vi.fn();
  render(
    <GroupsPage
      currentUserId="u1"
      isSystemAdmin={false}
      notebooks={NOTEBOOKS}
      initialGroupId={detail.id}
      onBack={vi.fn()}
      onChanged={onChanged}
      onOpenNotebook={vi.fn()}
      onNavigate={onNavigate}
    />,
  );
  return { onChanged, onNavigate };
}

test("独立页面集中展示群组知识库，并只列出当前用户可管理的待添加 Notebook", async () => {
  const user = userEvent.setup();
  renderPage();

  expect(await screen.findByRole("heading", { name: "先进封装项目" })).toBeInTheDocument();
  expect(screen.getByText("共享可靠性资料")).toBeInTheDocument();
  expect(screen.getByText("先进封装工艺")).toBeInTheDocument();
  expect(screen.queryByText("只读参考库")).not.toBeInTheDocument();

  vi.mocked(shareNotebookToGroup).mockResolvedValue({
    id: "grant-1", principal_type: "group", principal_id: "g1", role: "viewer",
    principal_name: "先进封装项目", principal_kind: "project", created_at: "",
  });
  await user.click(screen.getByRole("checkbox", { name: /先进封装工艺/ }));
  await user.click(screen.getByRole("button", { name: "添加已选（1）" }));
  await waitFor(() => expect(shareNotebookToGroup).toHaveBeenCalledWith("nb-owned", "g1", { manage: false }));
});

test("owner 转让需要二次确认，新 owner 由服务端返回且旧 owner 保留管理员提示", async () => {
  const user = userEvent.setup();
  const transferred: GroupDetail = {
    ...OWNER_DETAIL,
    owner_id: "u2",
    my_role: "admin",
    members: OWNER_DETAIL.members.map((member) => member.id === "u2" ? { ...member, role: "admin" } : member),
  };
  vi.mocked(transferGroupOwner).mockResolvedValue(transferred);
  renderPage();

  await screen.findByRole("heading", { name: "先进封装项目" });
  await user.click(screen.getByRole("button", { name: "设置" }));
  await user.selectOptions(screen.getByLabelText("选择新的群组所有者"), "u2");
  await user.click(screen.getByRole("button", { name: "转让群组" }));
  expect(transferGroupOwner).not.toHaveBeenCalled();
  await user.click(screen.getByRole("button", { name: "确认转让" }));

  await waitFor(() => expect(transferGroupOwner).toHaveBeenCalledWith("g1", "u2"));
  expect(await screen.findByText("群组所有权已转让，你仍是组管理员。")).toBeInTheDocument();
});

test("组管理员可在成员页生成邀请链接", async () => {
  const user = userEvent.setup();
  vi.mocked(createGroupInvite).mockResolvedValue({
    active: true,
    token: "gri_test-token",
    created_at: "2026-08-21T00:00:00+00:00",
  });
  renderPage();

  await screen.findByRole("heading", { name: "先进封装项目" });
  await user.click(screen.getByRole("button", { name: "成员" }));
  await user.click(screen.getByRole("button", { name: "生成邀请链接" }));

  await waitFor(() => expect(createGroupInvite).toHaveBeenCalledWith("g1"));
  expect(screen.getByDisplayValue(/group_invite=gri_test-token/)).toBeInTheDocument();
});

test("普通成员仍可查看群组知识库，但不加载审批队列且设置中只能退出", async () => {
  const user = userEvent.setup();
  const memberDetail: GroupDetail = { ...OWNER_DETAIL, owner_id: "u2", my_role: "member" };
  renderPage(memberDetail);

  expect(await screen.findByText("共享可靠性资料")).toBeInTheDocument();
  expect(listGroupSharedNotebooks).toHaveBeenCalledWith("g1");
  expect(listGroupShareRequests).not.toHaveBeenCalled();
  await user.click(screen.getByRole("button", { name: "设置" }));
  expect(screen.getByRole("button", { name: "退出群组" })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "删除群组" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "转让群组" })).not.toBeInTheDocument();
});

test("退出后更新 hash 并自动选择剩余群组", async () => {
  const user = userEvent.setup();
  const memberDetail: GroupDetail = { ...OWNER_DETAIL, owner_id: "u2", my_role: "member" };
  const remaining: GroupDetail = {
    ...OWNER_DETAIL,
    id: "g2",
    name: "剩余群组",
    owner_id: "u2",
    my_role: "member",
  };
  vi.mocked(leaveGroup).mockResolvedValue(undefined);
  const { onNavigate } = renderPage(memberDetail);

  await screen.findByRole("heading", { name: "先进封装项目" });
  vi.mocked(listGroups).mockResolvedValue([remaining]);
  vi.mocked(getGroup).mockResolvedValue(remaining);
  await user.click(screen.getByRole("button", { name: "设置" }));
  await user.click(screen.getByRole("button", { name: "退出群组" }));
  await user.click(screen.getByRole("button", { name: "确认退出" }));

  await waitFor(() => expect(onNavigate).toHaveBeenCalledWith("g2", "notebooks"));
  expect(await screen.findByRole("heading", { name: "剩余群组" })).toBeInTheDocument();
});

test("退出最后一个群组时立即失效旧 hash 请求且不把 404 写回空态", async () => {
  const user = userEvent.setup();
  const memberDetail: GroupDetail = { ...OWNER_DETAIL, owner_id: "u2", my_role: "member" };
  vi.mocked(leaveGroup).mockResolvedValue(undefined);
  const { onNavigate } = renderPage(memberDetail);

  await screen.findByRole("heading", { name: "先进封装项目" });
  let releaseList!: (groups: GroupDetail[]) => void;
  vi.mocked(listGroups).mockImplementationOnce(() => new Promise((resolve) => {
    releaseList = resolve;
  }));
  vi.mocked(getGroup).mockRejectedValueOnce(new Error("群组不存在"));

  await user.click(screen.getByRole("button", { name: "设置" }));
  await user.click(screen.getByRole("button", { name: "退出群组" }));
  await user.click(screen.getByRole("button", { name: "确认退出" }));

  await waitFor(() => expect(onNavigate).toHaveBeenCalledWith("", "notebooks"));
  expect(getGroup).toHaveBeenCalledTimes(1);
  releaseList([]);
  await waitFor(() => expect(screen.getByText("还没有群组")).toBeInTheDocument());
  expect(screen.queryByText("群组详情加载失败")).not.toBeInTheDocument();
});
