import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";

vi.mock("../../app/group-api.ts", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../app/group-api.ts")>();
  return {
    ...actual,
    listGroups: vi.fn(),
    getGroup: vi.fn(),
    approveShareRequest: vi.fn(),
    rejectShareRequest: vi.fn(),
    getGroupInvite: vi.fn(),
    createGroupInvite: vi.fn(),
    listGroupSharedNotebooks: vi.fn(),
    listGroupShareRequests: vi.fn(),
    listMyPendingShareRequests: vi.fn(),
    shareNotebookToGroup: vi.fn(),
    transferGroupOwner: vi.fn(),
    leaveGroup: vi.fn(),
    getGroupLeavePreview: vi.fn(),
    removeGroupMember: vi.fn(),
  };
});

import {
  approveShareRequest,
  createGroupInvite,
  getGroup,
  getGroupInvite,
  listGroupShareRequests,
  listGroupSharedNotebooks,
  listGroups,
  listMyPendingShareRequests,
  leaveGroup,
  getGroupLeavePreview,
  rejectShareRequest,
  removeGroupMember,
  shareNotebookToGroup,
  transferGroupOwner,
  type GroupDetail,
} from "../../app/group-api.ts";
import { GroupsPage } from "../../app/groups-page.tsx";
import { COPY_RESULT_HOLD_MS } from "../../app/copy-result";
import { humanizedError } from "../../app/errors.ts";
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
  vi.mocked(getGroupLeavePreview).mockResolvedValue({ notebook_count: 0, memory_count: 0, notebooks: [] });
});

function renderPage(detail: GroupDetail = OWNER_DETAIL, openingNotebookId: string | null = null) {
  vi.mocked(listGroups).mockResolvedValue([detail]);
  vi.mocked(getGroup).mockResolvedValue(detail);
  const onChanged = vi.fn();
  const onNavigate = vi.fn();
  const onOpenNotebook = vi.fn();
  render(
    <GroupsPage
      currentUserId="u1"
      isSystemAdmin={false}
      notebooks={NOTEBOOKS}
      initialGroupId={detail.id}
      onBack={vi.fn()}
      onChanged={onChanged}
      openingNotebookId={openingNotebookId}
      onOpenNotebook={onOpenNotebook}
      onNavigate={onNavigate}
    />,
  );
  return { onChanged, onNavigate, onOpenNotebook };
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

test("正在打开的群组知识库显示忙碌反馈：按钮禁用、aria-busy、点击不触发 onOpenNotebook", async () => {
  // C3(codex #621 R1 P2):群组页的「打开笔记本」入口与集合页同权,不再是「按下
  // 整页就切走所以不用管」的已知余量。
  const user = userEvent.setup();
  const { onOpenNotebook } = renderPage(OWNER_DETAIL, "nb-shared");

  const openButton = await screen.findByRole("button", { name: /共享可靠性资料/ });
  expect(openButton).toBeDisabled();
  expect(openButton).toHaveAttribute("aria-busy", "true");
  expect(openButton).toHaveClass("is-opening");
  expect(within(openButton).getByText("打开中…")).toBeInTheDocument();

  await user.click(openButton);
  expect(onOpenNotebook).not.toHaveBeenCalled();
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

// 成员清单分页:接口整份返回(契约上不分页),界面每页 20 人。
function withMembers(count: number, base: GroupDetail = OWNER_DETAIL): GroupDetail {
  const members = Array.from({ length: count }, (_, index) => ({
    id: `u${index + 1}`,
    username: `user${String(index + 1).padStart(2, "0")}`,
    display_name: "",
    role: index === 0 ? "admin" : "member",
  }));
  return { ...base, member_count: count, members };
}

function memberNames(): string[] {
  return Array.from(document.querySelectorAll(".group-member-row .group-member-copy strong"))
    .map((node) => node.textContent ?? "");
}

test("成员超过一页时分页展示，翻页后显示下一批成员", async () => {
  const user = userEvent.setup();
  renderPage(withMembers(45));

  await screen.findByRole("heading", { name: "先进封装项目" });
  await user.click(screen.getByRole("button", { name: "成员" }));

  expect(memberNames()).toHaveLength(20);
  expect(memberNames()[0]).toBe("user01");
  const pager = screen.getByRole("navigation", { name: "成员分页" });
  expect(within(pager).getByText("1–20 / 45")).toBeInTheDocument();

  await user.click(within(pager).getByRole("button", { name: "下一页" }));
  expect(memberNames()[0]).toBe("user21");
  expect(memberNames()).toHaveLength(20);

  await user.click(within(pager).getByRole("button", { name: "下一页" }));
  expect(memberNames()).toEqual(["user41", "user42", "user43", "user44", "user45"]);
  expect(within(pager).getByRole("button", { name: "下一页" })).toBeDisabled();
});

test("一页放得下时不显示成员分页控件", async () => {
  const user = userEvent.setup();
  renderPage(withMembers(20));

  await screen.findByRole("heading", { name: "先进封装项目" });
  await user.click(screen.getByRole("button", { name: "成员" }));

  expect(memberNames()).toHaveLength(20);
  expect(screen.queryByRole("navigation", { name: "成员分页" })).not.toBeInTheDocument();
});

test("移出最后一页唯一的成员后退回上一页，而不是停在空页", async () => {
  const user = userEvent.setup();
  const before = withMembers(41);
  const after = withMembers(40);
  vi.mocked(listGroups).mockResolvedValue([before]);
  vi.mocked(getGroup).mockResolvedValue(before);
  vi.mocked(removeGroupMember).mockResolvedValue(undefined);
  // 真实页面里 onNavigate 会把 tab 写进 hash;这里 onNavigate 是桩,直接以成员页进入,
  // 否则移出后的群组清单刷新会让 hash effect 把页签拽回默认的「知识库」。
  render(
    <GroupsPage
      currentUserId="u1"
      isSystemAdmin={false}
      notebooks={NOTEBOOKS}
      initialGroupId="g1"
      initialTab="members"
      onBack={vi.fn()}
      onChanged={vi.fn()}
      openingNotebookId={null}
      onOpenNotebook={vi.fn()}
      onNavigate={vi.fn()}
    />,
  );

  await screen.findByRole("heading", { name: "先进封装项目" });
  const pager = screen.getByRole("navigation", { name: "成员分页" });
  await user.click(within(pager).getByRole("button", { name: "下一页" }));
  await user.click(within(pager).getByRole("button", { name: "下一页" }));
  expect(memberNames()).toEqual(["user41"]);

  vi.mocked(getGroup).mockResolvedValue(after);
  vi.mocked(listGroups).mockResolvedValue([after]);
  await user.click(screen.getByRole("button", { name: "移出" }));
  await user.click(screen.getByRole("button", { name: "确认移出" }));

  await waitFor(() => expect(memberNames()[0]).toBe("user21"));
  expect(memberNames()).toHaveLength(20);
  expect(within(screen.getByRole("navigation", { name: "成员分页" })).getByText("21–40 / 40")).toBeInTheDocument();
});

test("切换到另一个群组时成员分页回到第一页", async () => {
  const user = userEvent.setup();
  const first = withMembers(45);
  const second = withMembers(30, { ...OWNER_DETAIL, id: "g2", name: "另一个群组" });
  vi.mocked(listGroups).mockResolvedValue([first, second]);
  vi.mocked(getGroup).mockImplementation(async (id: string) => (id === "g2" ? second : first));
  // 不传 initialGroupId,原因同下方「剪贴板挂着时切了群组」那条用例。
  render(
    <GroupsPage
      currentUserId="u1"
      isSystemAdmin={false}
      notebooks={NOTEBOOKS}
      onBack={vi.fn()}
      onChanged={vi.fn()}
      openingNotebookId={null}
      onOpenNotebook={vi.fn()}
      onNavigate={vi.fn()}
    />,
  );

  await screen.findByRole("heading", { name: "先进封装项目" });
  await user.click(screen.getByRole("button", { name: "成员" }));
  await user.click(within(screen.getByRole("navigation", { name: "成员分页" })).getByRole("button", { name: "下一页" }));
  expect(memberNames()[0]).toBe("user21");

  await user.click(screen.getByRole("button", { name: /另一个群组/ }));
  await screen.findByRole("heading", { name: "另一个群组" });
  await user.click(screen.getByRole("button", { name: "成员" }));
  expect(memberNames()[0]).toBe("user01");
  expect(within(screen.getByRole("navigation", { name: "成员分页" })).getByText("1–20 / 30")).toBeInTheDocument();
});

test("群组清单超过一页时侧栏分页，深链到后面一页的群组会自动翻到它所在的页", async () => {
  const groups = Array.from({ length: 25 }, (_, index) => ({
    ...OWNER_DETAIL,
    id: `g${index + 1}`,
    name: `群组${String(index + 1).padStart(2, "0")}`,
  }));
  vi.mocked(listGroups).mockResolvedValue(groups);
  vi.mocked(getGroup).mockImplementation(async (id: string) => groups.find((group) => group.id === id)!);
  render(
    <GroupsPage
      currentUserId="u1"
      isSystemAdmin={false}
      notebooks={NOTEBOOKS}
      initialGroupId="g23"
      onBack={vi.fn()}
      onChanged={vi.fn()}
      openingNotebookId={null}
      onOpenNotebook={vi.fn()}
      onNavigate={vi.fn()}
    />,
  );

  await screen.findByRole("heading", { name: "群组23" });
  const pager = await screen.findByRole("navigation", { name: "群组清单分页" });
  await waitFor(() => expect(within(pager).getByText("21–25 / 25")).toBeInTheDocument());
  expect(screen.getByRole("button", { name: /群组23/ })).toHaveClass("active");
  expect(screen.queryByRole("button", { name: /群组01/ })).not.toBeInTheDocument();
});

test("群组知识库与可添加的笔记本分页；勾选跨页保留", async () => {
  const user = userEvent.setup();
  vi.mocked(listGroupSharedNotebooks).mockResolvedValue(Array.from({ length: 23 }, (_, index) => ({
    notebook_id: `nb-shared-${index + 1}`,
    name: `共享库${String(index + 1).padStart(2, "0")}`,
    owner_username: "carol",
    roles: ["viewer"],
  })));
  const candidates: NotebookSummary[] = Array.from({ length: 22 }, (_, index) => ({
    ...NOTEBOOKS[0],
    id: `nb-own-${index + 1}`,
    name: `我的库${String(index + 1).padStart(2, "0")}`,
  }));
  vi.mocked(listGroups).mockResolvedValue([OWNER_DETAIL]);
  vi.mocked(getGroup).mockResolvedValue(OWNER_DETAIL);
  vi.mocked(shareNotebookToGroup).mockResolvedValue({
    id: "grant-1", principal_type: "group", principal_id: "g1", role: "viewer",
    principal_name: "先进封装项目", principal_kind: "project", created_at: "",
  });
  render(
    <GroupsPage
      currentUserId="u1"
      isSystemAdmin={false}
      notebooks={candidates}
      initialGroupId="g1"
      onBack={vi.fn()}
      onChanged={vi.fn()}
      openingNotebookId={null}
      onOpenNotebook={vi.fn()}
      onNavigate={vi.fn()}
    />,
  );

  expect(await screen.findByText("共享库01")).toBeInTheDocument();
  expect(screen.queryByText("共享库21")).not.toBeInTheDocument();
  await user.click(within(screen.getByRole("navigation", { name: "群组知识库分页" })).getByRole("button", { name: "下一页" }));
  expect(screen.getByText("共享库23")).toBeInTheDocument();
  expect(screen.queryByText("共享库01")).not.toBeInTheDocument();

  await user.click(screen.getByRole("checkbox", { name: /我的库01/ }));
  expect(screen.queryByRole("checkbox", { name: /我的库21/ })).not.toBeInTheDocument();
  await user.click(within(screen.getByRole("navigation", { name: "可添加笔记本分页" })).getByRole("button", { name: "下一页" }));
  await user.click(screen.getByRole("checkbox", { name: /我的库22/ }));
  // 第一页勾的那本不在屏上,仍计入「添加已选」。
  await user.click(screen.getByRole("button", { name: "添加已选（2）" }));
  await waitFor(() => expect(shareNotebookToGroup).toHaveBeenCalledTimes(2));
  expect(shareNotebookToGroup).toHaveBeenCalledWith("nb-own-1", "g1", { manage: false });
  expect(shareNotebookToGroup).toHaveBeenCalledWith("nb-own-22", "g1", { manage: false });
});

test("待审批申请与我发起的申请分页", async () => {
  const user = userEvent.setup();
  const request = (index: number, prefix: string) => ({
    id: `${prefix}-${index}`, notebook_id: `nb-${prefix}-${index}`,
    notebook_name: `${prefix}${String(index).padStart(2, "0")}`, group_id: "g1", group_name: "先进封装项目",
    requested_by: "u2", requested_by_username: "bob", status: "pending",
    decided_by: null, decided_at: null, created_at: "",
  });
  vi.mocked(listGroupShareRequests).mockResolvedValue(Array.from({ length: 21 }, (_, i) => request(i + 1, "待审库")));
  vi.mocked(listMyPendingShareRequests).mockResolvedValue(Array.from({ length: 24 }, (_, i) => request(i + 1, "我申请库")));
  vi.mocked(listGroups).mockResolvedValue([OWNER_DETAIL]);
  vi.mocked(getGroup).mockResolvedValue(OWNER_DETAIL);
  render(
    <GroupsPage
      currentUserId="u1"
      isSystemAdmin={false}
      notebooks={NOTEBOOKS}
      initialGroupId="g1"
      initialTab="requests"
      onBack={vi.fn()}
      onChanged={vi.fn()}
      openingNotebookId={null}
      onOpenNotebook={vi.fn()}
      onNavigate={vi.fn()}
    />,
  );

  expect(await screen.findByText("待审库01")).toBeInTheDocument();
  expect(screen.queryByText("待审库21")).not.toBeInTheDocument();
  await user.click(within(screen.getByRole("navigation", { name: "待审批申请分页" })).getByRole("button", { name: "下一页" }));
  expect(screen.getByText("待审库21")).toBeInTheDocument();

  expect(screen.getByText("我申请库01")).toBeInTheDocument();
  expect(screen.queryByText("我申请库21")).not.toBeInTheDocument();
  await user.click(within(screen.getByRole("navigation", { name: "我发起的申请分页" })).getByRole("button", { name: "下一页" }));
  expect(screen.getByText("我申请库24")).toBeInTheDocument();
  expect(screen.queryByText("我申请库01")).not.toBeInTheDocument();
});

function pendingRequest(id: string, name: string) {
  return {
    id, notebook_id: `nb-${id}`, notebook_name: name, group_id: "g1", group_name: "先进封装项目",
    requested_by: "u2", requested_by_username: "bob", status: "pending",
    decided_by: null, decided_at: null, created_at: "",
  };
}

function renderRequestsTab() {
  const onChanged = vi.fn();
  render(
    <GroupsPage
      currentUserId="u1"
      isSystemAdmin={false}
      notebooks={NOTEBOOKS}
      initialGroupId="g1"
      initialTab="requests"
      onBack={vi.fn()}
      onChanged={onChanged}
      openingNotebookId={null}
      onOpenNotebook={vi.fn()}
      onNavigate={vi.fn()}
    />,
  );
  return { onChanged };
}

test("批准被拒而申请仍在队列(笔记本正在删除):重拉队列,原因标在那一行旁边,不只报在顶部", async () => {
  const user = userEvent.setup();
  const stalled = pendingRequest("sr-stalled", "正在删除的库");
  const other = pendingRequest("sr-other", "另一本库");
  vi.mocked(listGroupShareRequests).mockResolvedValue([stalled, other]);
  vi.mocked(approveShareRequest).mockRejectedValueOnce(
    humanizedError("这本笔记本正在删除,无法批准这条申请", 409),
  );
  renderRequestsTab();

  const row = (await screen.findByText("正在删除的库")).closest(".group-request-row") as HTMLElement;
  const callsBefore = vi.mocked(listGroupShareRequests).mock.calls.length;
  await user.click(within(row).getByRole("button", { name: "批准" }));

  expect(await within(row).findByRole("alert")).toHaveTextContent("这本笔记本正在删除,无法批准这条申请");
  expect(vi.mocked(listGroupShareRequests).mock.calls.length).toBe(callsBefore + 1);
  // 另一行不受牵连,顶部状态条也不重复报同一句。
  const otherRow = screen.getByText("另一本库").closest(".group-request-row") as HTMLElement;
  expect(within(otherRow).queryByRole("alert")).not.toBeInTheDocument();
  expect(screen.getAllByText("这本笔记本正在删除,无法批准这条申请")).toHaveLength(1);
  // 按钮松手还原,可以再点。
  expect(within(row).getByRole("button", { name: "批准" })).toBeEnabled();
});

test("批准被拒且申请已不在队列(被别人处理过):那一行随重拉消失,原因走顶部状态条", async () => {
  const user = userEvent.setup();
  const gone = pendingRequest("sr-gone", "已被处理的库");
  vi.mocked(listGroupShareRequests).mockResolvedValueOnce([gone]).mockResolvedValue([]);
  vi.mocked(approveShareRequest).mockRejectedValueOnce(
    humanizedError("这条待审批的共享申请不存在或已被处理", 404),
  );
  renderRequestsTab();

  await user.click(await screen.findByRole("button", { name: "批准" }));

  expect(await screen.findByText("这条待审批的共享申请不存在或已被处理")).toBeInTheDocument();
  await waitFor(() => expect(screen.queryByText("已被处理的库")).not.toBeInTheDocument());
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

const APPROVED_REFRESH_NOTICE = "已批准，但列表没能刷新，重新打开这个群组可看到最新状态。";

test("批准成功、队列重拉成功、共享库列表拉取失败:已批准的申请照样离开队列,不报「批准失败」,审批列表上方如实说明", async () => {
  const user = userEvent.setup();
  const target = pendingRequest("sr-ok", "要批准的库");
  vi.mocked(listGroupShareRequests).mockResolvedValueOnce([target]).mockResolvedValue([]);
  vi.mocked(approveShareRequest).mockResolvedValueOnce({ ...target, status: "approved" });
  const { onChanged } = renderRequestsTab();
  const row = (await screen.findByText("要批准的库")).closest(".group-request-row") as HTMLElement;
  vi.mocked(listGroupSharedNotebooks).mockRejectedValueOnce(new Error("network down"));

  await user.click(within(row).getByRole("button", { name: "批准" }));

  expect(await screen.findByRole("status")).toHaveTextContent(APPROVED_REFRESH_NOTICE);
  expect(screen.queryByText("要批准的库")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "批准" })).not.toBeInTheDocument();
  expect(screen.queryByText("批准申请失败")).not.toBeInTheDocument();
  expect(onChanged).toHaveBeenCalled();
});

test("批准成功但队列重拉失败:这条已批准的申请从本地列表拿掉,不再挂着可点的按钮", async () => {
  const user = userEvent.setup();
  const target = pendingRequest("sr-ok", "要批准的库");
  const other = pendingRequest("sr-other", "另一本库");
  vi.mocked(listGroupShareRequests).mockResolvedValueOnce([target, other]);
  vi.mocked(approveShareRequest).mockResolvedValueOnce({ ...target, status: "approved" });
  renderRequestsTab();
  const row = (await screen.findByText("要批准的库")).closest(".group-request-row") as HTMLElement;
  vi.mocked(listGroupShareRequests).mockRejectedValueOnce(new Error("network down"));

  await user.click(within(row).getByRole("button", { name: "批准" }));

  expect(await screen.findByRole("status")).toHaveTextContent(APPROVED_REFRESH_NOTICE);
  expect(screen.queryByText("要批准的库")).not.toBeInTheDocument();
  expect(screen.getByText("另一本库")).toBeInTheDocument();
  expect(screen.queryByText("批准申请失败")).not.toBeInTheDocument();
});

test("驳回成功但队列重拉失败:这条申请同样从本地列表拿掉,不报「驳回失败」", async () => {
  const user = userEvent.setup();
  const target = pendingRequest("sr-no", "要驳回的库");
  vi.mocked(listGroupShareRequests).mockResolvedValueOnce([target]);
  vi.mocked(rejectShareRequest).mockResolvedValueOnce({ ...target, status: "rejected" });
  renderRequestsTab();
  const row = (await screen.findByText("要驳回的库")).closest(".group-request-row") as HTMLElement;
  vi.mocked(listGroupShareRequests).mockRejectedValueOnce(new Error("network down"));

  await user.click(within(row).getByRole("button", { name: "驳回" }));

  expect(await screen.findByRole("status")).toHaveTextContent("已驳回，但列表没能刷新，重新打开这个群组可看到最新状态。");
  expect(screen.queryByText("要驳回的库")).not.toBeInTheDocument();
  expect(screen.queryByText("驳回申请失败")).not.toBeInTheDocument();
});

test("批准被拒且队列重拉也失败:那一行还在屏上,原因标在它旁边", async () => {
  const user = userEvent.setup();
  const stalled = pendingRequest("sr-stalled", "正在删除的库");
  vi.mocked(listGroupShareRequests).mockResolvedValueOnce([stalled]);
  vi.mocked(approveShareRequest).mockRejectedValueOnce(
    humanizedError("这本笔记本正在删除,无法批准这条申请", 409),
  );
  renderRequestsTab();
  const row = (await screen.findByText("正在删除的库")).closest(".group-request-row") as HTMLElement;
  vi.mocked(listGroupShareRequests).mockRejectedValueOnce(new Error("network down"));

  await user.click(within(row).getByRole("button", { name: "批准" }));

  expect(await within(row).findByRole("alert")).toHaveTextContent("这本笔记本正在删除,无法批准这条申请");
  expect(screen.getAllByText("这本笔记本正在删除,无法批准这条申请")).toHaveLength(1);
});

async function switchGroupWhileApprovalRefreshIsPending(approveOutcome: "refused" | "approved") {
  const user = userEvent.setup();
  const second: GroupDetail = { ...OWNER_DETAIL, id: "g2", name: "另一个群组" };
  const stalled = pendingRequest("sr-stalled", "正在删除的库");
  const secondRequest = { ...pendingRequest("sr-second", "第二组的库"), group_id: "g2", group_name: "另一个群组" };
  let releaseRefresh: (rows: (typeof stalled)[]) => void = () => {};
  let g1Calls = 0;
  vi.mocked(listGroups).mockResolvedValue([OWNER_DETAIL, second]);
  vi.mocked(getGroup).mockImplementation(async (id: string) => (id === "g2" ? second : OWNER_DETAIL));
  vi.mocked(listGroupShareRequests).mockImplementation((id: string) => {
    if (id === "g2") return Promise.resolve([secondRequest]);
    g1Calls += 1;
    if (g1Calls === 1) return Promise.resolve([stalled]);
    return new Promise((resolve) => { releaseRefresh = resolve; });
  });
  if (approveOutcome === "refused") {
    vi.mocked(approveShareRequest).mockRejectedValueOnce(
      humanizedError("这本笔记本正在删除,无法批准这条申请", 409),
    );
  } else {
    vi.mocked(approveShareRequest).mockResolvedValueOnce({ ...stalled, status: "approved" });
  }
  // 不传 initialGroupId:onNavigate 是桩,传了的话切组后 effect 会按旧 initialGroupId 把 g1
  // 重新载回来(同「切换到另一个群组时成员分页回到第一页」)。
  render(
    <GroupsPage
      currentUserId="u1"
      isSystemAdmin={false}
      notebooks={NOTEBOOKS}
      onBack={vi.fn()}
      onChanged={vi.fn()}
      openingNotebookId={null}
      onOpenNotebook={vi.fn()}
      onNavigate={vi.fn()}
    />,
  );

  await screen.findByRole("heading", { name: "先进封装项目" });
  await user.click(screen.getByRole("button", { name: /共享申请/ }));
  const row = (await screen.findByText("正在删除的库")).closest(".group-request-row") as HTMLElement;
  await user.click(within(row).getByRole("button", { name: "批准" }));
  await waitFor(() => expect(g1Calls).toBe(2)); // 重拉已发出、还挂着

  await user.click(screen.getByRole("button", { name: /另一个群组/ }));
  await screen.findByRole("heading", { name: "另一个群组" });
  await user.click(screen.getByRole("button", { name: /共享申请/ }));
  expect(await screen.findByText("第二组的库")).toBeInTheDocument();

  await act(async () => { releaseRefresh([stalled]); });

  expect(screen.getByText("第二组的库")).toBeInTheDocument();
  expect(screen.queryByText("正在删除的库")).not.toBeInTheDocument();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  expect(screen.queryByText("这本笔记本正在删除,无法批准这条申请")).not.toBeInTheDocument();
}

test("批准失败后的重拉还没回来就切到别的组:旧组的队列与行内提示被丢弃,不盖住新组", async () => {
  await switchGroupWhileApprovalRefreshIsPending("refused");
});

test("批准成功后的重拉还没回来就切到别的组:旧组的队列不盖住新组", async () => {
  await switchGroupWhileApprovalRefreshIsPending("approved");
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

// 组名 120 / 组说明 1000(GROUP_INPUT_LIMITS)。余量提示在剩最后一成时才出现。
const LENGTH_HINT = /还可输入|已达上限/;

test("新建群组：还早时不出声，快到上限给余量，粘贴超长被 maxLength 截满时明说已达上限", async () => {
  const user = userEvent.setup();
  renderPage();

  await screen.findByRole("heading", { name: "先进封装项目" });
  await user.click(screen.getByRole("button", { name: "新建群组" }));

  const name = screen.getByRole("textbox", { name: "群组名称" });
  await user.click(name);
  await user.paste("组".repeat(100));
  expect(screen.queryByText(LENGTH_HINT)).not.toBeInTheDocument();

  await user.paste("组".repeat(10));
  expect(screen.getByRole("status")).toHaveTextContent("群组名称还可输入 10 个字");

  // 粘贴会被 maxLength 当场截短、浏览器不给任何反馈:提示必须替它把截断说出来。
  await user.clear(name);
  await user.paste("组".repeat(130));
  expect(name).toHaveValue("组".repeat(120));
  expect(screen.getByRole("status")).toHaveTextContent("群组名称已达上限 120 个字");

  const description = screen.getByRole("textbox", { name: "新群组的说明" });
  await user.click(description);
  await user.paste("述".repeat(1200));
  expect(description).toHaveValue("述".repeat(1000));
  expect(screen.getByText("群组说明已达上限 1000 个字")).toBeInTheDocument();
});

test("群组设置：改名与改说明同样给出余量提示，还早时不出声", async () => {
  const user = userEvent.setup();
  renderPage();

  await screen.findByRole("heading", { name: "先进封装项目" });
  await user.click(screen.getByRole("button", { name: "设置" }));
  expect(screen.queryByText(LENGTH_HINT)).not.toBeInTheDocument();

  const name = screen.getByLabelText("群组名称");
  await user.clear(name);
  await user.paste("组".repeat(112));
  expect(screen.getByRole("status")).toHaveTextContent("群组名称还可输入 8 个字");

  const description = screen.getByLabelText("群组说明");
  await user.clear(description);
  await user.paste("述".repeat(1001));
  expect(description).toHaveValue("述".repeat(1000));
  expect(screen.getByText("群组说明已达上限 1000 个字")).toBeInTheDocument();
});

/** 退出群组走确认面板:等退群预览读到、「确认退出」可用之后再按。 */
async function confirmLeaveInDialog(user: ReturnType<typeof userEvent.setup>) {
  const dialog = await screen.findByRole("dialog", { name: "退出群组" });
  const confirm = within(dialog).getByRole("button", { name: "确认退出" });
  await waitFor(() => expect(confirm).toBeEnabled());
  await user.click(confirm);
}

const MEMBER_DETAIL: GroupDetail = { ...OWNER_DETAIL, owner_id: "u2", my_role: "member" };

test("退出群组先告知退出后会看不到的笔记本与记忆,确认后才发退出请求", async () => {
  const user = userEvent.setup();
  vi.mocked(getGroupLeavePreview).mockResolvedValue({
    notebook_count: 1,
    memory_count: 4,
    notebooks: [{ notebook_id: "nb-shared", name: "共享可靠性资料", memory_count: 4 }],
  });
  vi.mocked(leaveGroup).mockResolvedValue(undefined);
  renderPage(MEMBER_DETAIL);

  await screen.findByRole("heading", { name: "先进封装项目" });
  await user.click(screen.getByRole("button", { name: "设置" }));
  await user.click(screen.getByRole("button", { name: "退出群组" }));
  const dialog = await screen.findByRole("dialog", { name: "退出群组" });
  expect(await within(dialog).findByText("退出后，你将看不到 1 个笔记本里你的 4 条记忆。")).toBeInTheDocument();
  expect(getGroupLeavePreview).toHaveBeenCalledWith("g1", expect.anything());
  expect(leaveGroup).not.toHaveBeenCalled();
  await user.click(within(dialog).getByRole("button", { name: "取消" }));
  expect(screen.queryByRole("dialog", { name: "退出群组" })).not.toBeInTheDocument();
  expect(leaveGroup).not.toHaveBeenCalled();
  // 焦点还给「退出群组」按钮。
  await waitFor(() => expect(screen.getByRole("button", { name: "退出群组" })).toHaveFocus());
});

test("退出被拒(唯一的组管理员):原因落在面板里,面板不关、仍是这个群组", async () => {
  const user = userEvent.setup();
  vi.mocked(leaveGroup).mockRejectedValue(
    humanizedError("你是这个群组唯一的组管理员,请先指定其他组管理员再退出", 409),
  );
  const { onNavigate } = renderPage(MEMBER_DETAIL);

  await screen.findByRole("heading", { name: "先进封装项目" });
  await user.click(screen.getByRole("button", { name: "设置" }));
  await user.click(screen.getByRole("button", { name: "退出群组" }));
  await confirmLeaveInDialog(user);

  const dialog = screen.getByRole("dialog", { name: "退出群组" });
  expect(await within(dialog).findByRole("alert")).toHaveTextContent("唯一的组管理员");
  expect(within(dialog).getByRole("button", { name: "确认退出" })).toBeEnabled();
  expect(screen.getByRole("heading", { name: "先进封装项目" })).toBeInTheDocument();
  expect(onNavigate).not.toHaveBeenCalledWith("", "notebooks");
});

test("退出在途时取消面板:入口按钮占住为「正在退出…」,失败原因落在按钮旁", async () => {
  const user = userEvent.setup();
  let reject!: (reason: unknown) => void;
  vi.mocked(leaveGroup).mockImplementation(() => new Promise<void>((_, fail) => { reject = fail; }));
  renderPage(MEMBER_DETAIL);

  await screen.findByRole("heading", { name: "先进封装项目" });
  await user.click(screen.getByRole("button", { name: "设置" }));
  await user.click(screen.getByRole("button", { name: "退出群组" }));
  await confirmLeaveInDialog(user);
  const dialog = screen.getByRole("dialog", { name: "退出群组" });
  expect(within(dialog).getByRole("button", { name: "正在退出…" })).toBeDisabled();
  await user.click(within(dialog).getByRole("button", { name: "取消" }));
  expect(screen.queryByRole("dialog", { name: "退出群组" })).not.toBeInTheDocument();
  const entry = screen.getByRole("button", { name: "正在退出…" });
  expect(entry).toBeDisabled();

  await act(async () => { reject(humanizedError("退出群组失败", 500)); });
  expect(await screen.findByRole("alert")).toHaveTextContent("退出群组失败");
  expect(screen.getByRole("button", { name: "退出群组" })).toBeEnabled();
  expect(leaveGroup).toHaveBeenCalledTimes(1);
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
  await confirmLeaveInDialog(user);

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
  await confirmLeaveInDialog(user);

  await waitFor(() => expect(onNavigate).toHaveBeenCalledWith("", "notebooks"));
  expect(getGroup).toHaveBeenCalledTimes(1);
  releaseList([]);
  await waitFor(() => expect(screen.getByText("还没有群组")).toBeInTheDocument());
  expect(screen.queryByText("群组详情加载失败")).not.toBeInTheDocument();
});

// 复制邀请链接:结果必须画在按钮自身上。
//
// 缺陷来源:唯一的反馈曾经是页面**顶部**那条 notice 横幅——在长页面上它常常滚出视口,
// 用户看到的是「按钮纹丝不动」,于是判定没生效。按下态由 globals.css 的
// `button:...:active` 负责(那条没有 AST,由 tests/guards/button-press-feedback-guard
// 钉住),这里钉的是它管不到的另一半:**结果**态与它的自动还原。
const ACTIVE_INVITE = {
  active: true,
  token: "gri_copy-token",
  created_at: "2026-08-21T00:00:00+00:00",
} as const;

async function openInviteCard(user: ReturnType<typeof userEvent.setup>) {
  vi.mocked(getGroupInvite).mockResolvedValue({ ...ACTIVE_INVITE });
  renderPage();
  await screen.findByRole("heading", { name: "先进封装项目" });
  await user.click(screen.getByRole("button", { name: "成员" }));
  return await screen.findByRole("button", { name: "复制" });
}

test("复制成功时按钮自己变成「已复制」并换成成功配色，随后自动还原", async () => {
  const user = userEvent.setup();
  let completeCopy!: () => void;
  const writeText = vi.fn(() => new Promise<void>((resolve) => { completeCopy = resolve; }));
  Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
  try {
    const copy = await openInviteCard(user);
    await user.click(copy);

    expect(writeText).toHaveBeenCalledWith(
      expect.stringContaining("group_invite=gri_copy-token"),
    );
    // Keep userEvent on the real clock; only the pending result uses fake time.
    vi.useFakeTimers();
    await act(async () => { completeCopy(); });
    const copied = screen.getByRole("button", { name: "已复制" });
    expect(copied).toHaveClass("copy-result-copied");
    expect(document.querySelector(".group-page-status.success")).not.toBeInTheDocument();

    // 结果态是 JS 状态,不像 :active 那样松手自动还原——忘了摘掉就会一直挂着,
    // 下一次点击反而看不出有没有点上。
    await act(async () => { await vi.advanceTimersByTimeAsync(COPY_RESULT_HOLD_MS - 1); });
    expect(screen.getByRole("button", { name: "已复制" })).toHaveClass("copy-result-copied");
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    const restored = screen.getByRole("button", { name: "复制" });
    expect(restored).not.toHaveClass("copy-result-copied");
    expect(restored).not.toHaveClass("copy-result-failed");
  } finally {
    vi.useRealTimers();
    Reflect.deleteProperty(navigator, "clipboard");
  }
});

test("复制失败时按钮说「复制失败」，并把链接选中好让用户自己复制", async () => {
  // 剪贴板 API 存在却被拒(权限/非安全上下文/文档失焦),DOM 兜底的 execCommand 在 jsdom
  // 里又不存在——就是那台真的复制不了的浏览器。此时最要紧的不是那句提示,而是让用户当场
  // 就能 ⌘C。⚠ 不能靠「jsdom 没有剪贴板」来造这个场景:userEvent.setup() 自己会装一份
  // 可用的 navigator.clipboard,不打掉它这条用例会反过来测成功路径(实测过)。
  const user = userEvent.setup();
  vi.spyOn(navigator.clipboard, "writeText").mockRejectedValue(new Error("denied"));
  const copy = await openInviteCard(user);
  await user.click(copy);

  const failed = await screen.findByRole("button", { name: "复制失败" });
  expect(failed).toHaveClass("copy-result-failed");
  expect(document.querySelector(".group-page-status.success")).not.toBeInTheDocument();
  const link = screen.getByDisplayValue(/group_invite=gri_copy-token/);
  expect(document.activeElement).toBe(link);
  expect((link as HTMLInputElement).selectionEnd).toBe((link as HTMLInputElement).value.length);
});

test("剪贴板挂着时切了群组，失败不去选中另一个群组的邀请链接", async () => {
  // codex #612 R3 P2。剪贴板那一步可以挂很久(权限提示、非安全上下文),期间侧栏没有禁用。
  // 用户切走后 inviteLinkRef 指向**新群组**的输入框,旧的那次失败如果照样 focus+select,
  // 用户 ⌘C 拿到的是另一个群组的邀请链接——比「复制失败」严重得多。
  const user = userEvent.setup();
  const other: GroupDetail = { ...OWNER_DETAIL, id: "g2", name: "另一个群组" };
  vi.mocked(listGroups).mockResolvedValue([OWNER_DETAIL, other]);
  vi.mocked(getGroup).mockImplementation(async (id: string) => (id === "g2" ? other : OWNER_DETAIL));
  vi.mocked(getGroupInvite).mockImplementation(async (id: string) => ({
    active: true,
    token: id === "g2" ? "gri_second-group" : "gri_first-group",
    created_at: "2026-08-21T00:00:00+00:00",
  }));

  // 剪贴板写入挂起，由测试决定什么时候失败。
  let rejectWrite: (reason: Error) => void = () => undefined;
  vi.spyOn(navigator.clipboard, "writeText").mockReturnValue(
    new Promise<void>((_resolve, reject) => { rejectWrite = reject; }),
  );

  // ⚠ 不传 initialGroupId:传了之后「hash 回跳」那个 effect 会在 detail 变化时把页面
  // 拽回 initialGroupId 那个群组,切组根本切不过去(实测过)。
  render(
    <GroupsPage
      currentUserId="u1"
      isSystemAdmin={false}
      notebooks={NOTEBOOKS}
      onBack={vi.fn()}
      onChanged={vi.fn()}
      openingNotebookId={null}
      onOpenNotebook={vi.fn()}
      onNavigate={vi.fn()}
    />,
  );

  await screen.findByRole("heading", { name: "先进封装项目" });
  await user.click(screen.getByRole("button", { name: "成员" }));
  await user.click(await screen.findByRole("button", { name: "复制" }));

  // 复制还挂着就切到另一个群组，等新群组的链接渲染出来。
  await user.click(screen.getByRole("button", { name: /另一个群组/ }));
  await screen.findByRole("heading", { name: "另一个群组" });
  await user.click(screen.getByRole("button", { name: "成员" }));
  const secondLink = await screen.findByDisplayValue(/group_invite=gri_second-group/) as HTMLInputElement;
  const secondCopy = await screen.findByRole("button", { name: "复制中…" });
  expect(secondCopy).toBeDisabled();

  // 现在让第一次复制失败：它不能碰这个属于另一个群组的输入框。
  rejectWrite(new Error("denied"));
  await waitFor(() => expect(secondCopy).toBeEnabled());
  expect(secondCopy).toHaveTextContent("复制");
  expect(document.querySelector(".group-page-status.success")).not.toBeInTheDocument();
  expect(document.activeElement).not.toBe(secondLink);
  expect(secondLink.selectionStart).toBe(secondLink.selectionEnd);
});
